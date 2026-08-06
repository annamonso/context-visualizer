# Deployment

How to run ChronoLog Observability, from a laptop with no cluster to a live
multi-node ChronoLog deployment on SLURM.

- [Offline mode](#offline-mode-no-cluster) — no cluster, no keys, no cost
- [Live deployment](#live-deployment) — against a real ChronoLog cluster
- [Capture](#capture-getting-data-in) — getting data in
- [Operational notes](#operational-notes) — the load-bearing ones
- [Troubleshooting](#troubleshooting)

---

## Offline mode (no cluster)

Everything the UI does is reconstructed from stored data, so the dashboard runs
standalone against a local spool.

```bash
pip install -e .
make workspace                          # build the React SPA into the package
CHRONOLOG_OFFLINE=1 chronolog-observe    # http://localhost:5000
```

Seed it with data so the tabs are not empty:

```bash
python3 scripts/demos/demo_doctor.py         # error detection, diagnosis, memory
python3 scripts/demos/demo_fleet_scale.py    # 100 nodes summarised in milliseconds
python3 scripts/demos/demo_tracing.py        # bottleneck hop in a 6-host mesh
```

Each demo prints the exact command to open its data in the dashboard.
`py_chronolog_client` is **not** needed here — offline mode never constructs a
live client.

## Live deployment

### Prerequisites

- A ChronoLog cluster (visor + keepers; a keeper on every node you want to
  observe).
- `py_chronolog_client` importable by the same Python that has `flask`. It is not
  on PyPI — it is built from the ChronoLog C++ tree and put on `PYTHONPATH` by
  the deployment.
- The dashboard must run **inside the cluster subnet**: Mercury (ChronoLog's RPC
  layer) will not route from outside it.

### By hand

```bash
export CHRONOLOG_VISOR_IP=10.x.x.x    # the raw IP, NOT the -40g hostname
chronolog-observe                      # binds :5000 on the node it runs on
```

Reach it from your workstation over an SSH tunnel:

```bash
ssh -N -L 23456:<dashboard-node>:5000 <user>@<cluster-login-host>
# then open http://localhost:23456
```

### One command, on SLURM

`scripts/ops/chronolog-live.sh` does the whole bring-up: allocate N idle nodes →
deploy ChronoLog (a keeper on each) → wait for the healthcheck → start the
dashboard against the live visor → start the capture worker and a collector on
every node.

```bash
scripts/ops/chronolog-live.sh 4 compute
```

It prints the job id, the dashboard node and a ready-made tunnel command. The
tunnel hint reads `SSH_GATEWAY`, `SSH_USER` and `SSH_KEY` — set them for your
site:

```bash
SSH_GATEWAY=login.mycluster.edu SSH_KEY=~/.ssh/id_ed25519 \
  scripts/ops/chronolog-live.sh 4 compute
```

Release the nodes with `scancel <jobid>` when you are done.

To drive the live views at **zero LLM cost**, use the dashboard's *Generate
Traffic* button or `scripts/demos/synth_live_traffic.py` — both write synthetic
inter-agent edges through the real ingest path.

## Capture: getting data in

The dashboard is a **reader** by default. Two independent paths feed it.

### Path-A — LLM turns

Instrument your agents to append to the local capture spool, then run the sync
worker to drain that spool into ChronoLog:

```python
from chronolog_observability.capture.spool import get_spool
spool = get_spool()
spool.record_interaction("my-session", {
    "prompt": "...", "response": "...", "model": "claude-opus-4-8",
})
```

```bash
chronolog-capture --interval 5      # a SEPARATE process — see operational notes
```

The worker dedups against ChronoLog using per-session high-water marks and
rebuilds its state from ChronoLog on restart, so capture is at-least-once and
ingest is effectively exactly-once. It is a no-op in offline mode.

### Path-B — inter-agent calls

Run one **collector** per compute node. Local agents POST to it on localhost; it
writes through the node-local keeper and forwards a thin copy to the dashboard
for the live stream.

```bash
scripts/ops/launch-collectors.sh <SLURM_JOBID> http://<dashboard-node>:5000
# or by hand on one node:
chronolog-collector --flask-url http://<dashboard-node>:5000 --port 5650
```

Agents emit via `chronolog_observability.collector.emit(event)` or a plain
`POST http://127.0.0.1:5650/ingest`. If the collector is down, `emit()` falls
back to the dashboard's ingest endpoint directly.

**Session ids must be `role@scenario`.** A scenario-graph node joins to its
conversation on click only if the inter-agent edge's `from_session` /
`to_session` carry the same scoped id the LLM turns are spooled under.

## Operational notes

These are load-bearing on a live cluster — each one was a real failure.

- **Run the dashboard as a pure reader.** Do *not* set `CHRONOLOG_CAPTURE=1` in
  the dashboard process. The capture worker's startup `warm_up` performs a
  blocking `ReplayStory`, and `py_chronolog_client` holds the GIL throughout, so
  Flask's `app.run()` never finishes binding. Drain Path-A from a separate
  `chronolog-capture` process.
- **Read the data-heavy tabs only after the drain.** A `ReplayStory` against a
  story still inside the ~180 s keeper→grapher drain window blocks for *minutes*
  and freezes the dashboard; after the drain it returns promptly. The Cluster tab
  is CSV-backed and safe at any time, and the live views read the hot store, not
  ChronoLog.
- **Use the raw visor IP**, not the `-40g` hostname, in `CHRONOLOG_VISOR_IP`.
- **Node names differ between sources.** Reverse DNS gives `ares-comp-3` while
  SLURM zero-pads to `ares-comp-03`. The cluster view canonicalises both; if you
  add a new source, canonicalise it too or single-digit nodes silently disappear.

## Troubleshooting

**The dashboard hangs on startup, never binds the port.**
`CHRONOLOG_CAPTURE=1` is set in the dashboard process. Unset it and run
`chronolog-capture` separately.

**A tab spins forever on a live cluster.**
You are reading a story still inside the drain window. Wait ~180 s from the last
write. The Cluster tab and the live views stay responsive meanwhile.

**Path-A never drains on a brand-new cluster.**
The worker's `warm_up` can hang on a session index that has never been written.
Kick a one-shot `SpoolToChronoLogSync(get_backend()).sync_once()` — it is
append-only and does no `ReplayStory` — to seed the index.

**The Cluster tab is missing nodes.**
Single-digit nodes hidden by the padding mismatch above, or the node has no
keeper. Check `sinfo` against what the tab draws.

**`py_chronolog_client` not found.**
It is not installable from PyPI. Point `PYTHONPATH` at the ChronoLog build's
`lib/` directory, and make sure it is the *same* interpreter that has `flask`.
