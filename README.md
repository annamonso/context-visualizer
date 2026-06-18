# ChronoLog Observability

A drop-in observability dashboard and trace-capture layer for **ChronoLog**
deployments. Point it at a running ChronoLog visor and get conversation /
scenario / interaction / provenance views reconstructed from ChronoLog stories —
no chimaera or clio-core required.

This is a repackaging of the former `clio-core/context-visualizer` into a
standalone, pip-installable plugin. The key change: ChronoLog moved from an
optional backend to the **required substrate**, and the old hard dependency on
the chimaera C++ runtime was removed — every view is served from ChronoLog.

## How it works

Data sources are **adapters** selected at runtime via the
`chronolog_observability.adapters` entry-point group (plus a built-in default):

- **`ChronoLogAdapter`** (default) — serves every view from ChronoLog stories.
  Available on any ChronoLog host.

Blueprints declare the `Capability` they need; `app.create_app()` discovers the
active adapters and mounts only the blueprints some adapter can serve. The
adapter interface is the seam for third parties to add their own data source
without forking.

```
src/chronolog_observability/
├── app.py            # Flask factory: discover adapters, mount blueprints, serve SPA
├── config.py         # visor endpoint, offline mode, bind
├── backend/          # ChronoLog substrate (client, constants, path_a_reader)
├── adapters/
│   ├── base.py       # SourceAdapter + Capability  <- the data-source seam
│   ├── registry.py   # entry-point discovery + capability resolution
│   ├── chronolog_source.py   # the ChronoLog adapter
│   └── shape/        # story -> conversation/scenario graph shapers
├── capture/          # Path-A capture spool + sync worker; Path-B inter-agent store/ingest + live bus; demo store
├── collector/        # per-node collector daemon (agents → ChronoLog + live forward)
├── api/              # Flask blueprints (one per capability)
├── analysis/ semantic/ checkpointing/   # pure analysis + checks packages
└── static/workspace/ # built React SPA lands here (gitignored build artifact)
```

The React dashboard lives in `frontend/` (Vite). Its three tabs — Workspace,
Scenarios, Interactions — are all ChronoLog-served.

## Install & run

```bash
make workspace                       # build the React SPA into static/workspace/
pip install -e .                     # py_chronolog_client must be on PYTHONPATH
export CHRONOLOG_VISOR_IP=10.x.x.x   # raw IP, NOT the -40g hostname
chronolog-observe                    # serves the dashboard on :5000
```

Demo / dev without a live deployment:

```bash
CHRONOLOG_OFFLINE=1 chronolog-observe  # replays from drained CSVs
make workspace-dev                     # Vite dev server on :5173, proxies to :5000
```

### Live capture (Path-A → ChronoLog)

The dashboard is a reader by default. To also *ingest* traces, instrument your
agents to append events to the local capture spool, and run the Path-A sync
worker to drain that spool into ChronoLog:

```python
from chronolog_observability.capture.spool import get_spool
spool = get_spool()
spool.record_interaction("my-session", {"prompt": "...", "response": "...", "model": "claude-opus-4-8"})
spool.record_context_node("my-session", {"op": "add", "node": "..."})
spool.record_recovery("my-session", {"reason": "checkpoint-restore"})
```

```bash
chronolog-capture --interval 5         # standalone drain (spool -> ChronoLog)
# …or auto-start it inside the dashboard process:
CHRONOLOG_CAPTURE=1 chronolog-observe   # runs the worker as a daemon thread
```

The worker dedups against ChronoLog (high-water marks per session) and rebuilds
its state from ChronoLog on restart, so capture is at-least-once and ingest is
effectively exactly-once. It is a no-op in `CHRONOLOG_OFFLINE=1` mode.

### Live view (Path-B → SSE)

ChronoLog's chunk acceptance window (~180 s keeper→grapher drain) makes it
unusable as a real-time read source, so inter-agent events are fanned out to
SSE subscribers in-process at the moment of ingest while ChronoLog stays the
durable cold path:

- `POST /api/_inter-agent/ingest` — single event or batch; publishes to the
  live bus after the durable write.
- `GET /api/_inter-agent/stream?scenario=<sid>` — SSE: stored backlog first,
  then live `event: message` frames as events arrive.
- `GET /_interceptor/live` — SSE push feed of new LLM interactions (the
  Interactions tab upgrades from polling automatically).

For multi-node deployments, run one **collector** per compute node; local
agents POST to it on localhost, it writes to ChronoLog through the node-local
keeper and forwards a thin copy to the dashboard for the live stream (with
`X-DTP-Forwarded: chronolog`, so nothing is persisted twice):

```bash
scripts/launch-collectors.sh <SLURM_JOBID> http://<dashboard-node>:5000   # all nodes
# or by hand on one node:
chronolog-collector --flask-url http://<dashboard-node>:5000 --port 5650
```

Agents emit via `chronolog_observability.collector.emit(event)` or plain
`POST http://127.0.0.1:5650/ingest`; if the collector is down, `emit()` falls
back to the dashboard's ingest endpoint directly.

## Live end-to-end test (real agents on a ChronoLog cluster)

`scripts/` ships a harness that exercises the whole stack against a **live**
ChronoLog cluster with **real Claude Agent SDK agents** — genuine LLM turns and
a genuine `call_remote_agent` MCP tool call per agent, not synthetic data. It
drives the plugin's current endpoints (the legacy `scripts/_imported/` demos
target removed clio-core routes and no longer work).

Prereqs: a ChronoLog cluster up on a SLURM allocation; `flask` importable by the
same Python that has `py_chronolog_client`; and a conda env with
`claude_agent_sdk` + an authenticated `claude` CLI for the agents.

```bash
# one real agent per node, feeding Path-A (spool) and Path-B (collector):
scripts/run-live-agents.sh <SLURM_JOBID> <scenario_id>
#   healthcheck -> dashboard -> capture worker -> collectors -> N real agents
#   (agents scale with the cluster: 4 nodes -> 3 agents, 8 nodes -> 6)

# restart the dashboard, wait for the CSV drain, then assert every tab AND the
# scenario -> workspace click-through (per-agent metrics + inter-agent edges):
scripts/verify-when-drained.sh <SLURM_JOBID> <FLASK_NODE> <scenario_id> <N_agents>
```

Reusable pieces: `real_agent_chronolog.py` (the agent), `launch-dashboard.sh`,
`launch-capture.sh`, `launch-collectors.sh`, `verify_dashboard.py`.

**Operational notes (load-bearing on a live cluster):**

- **Run the dashboard as a pure reader.** Do *not* set `CHRONOLOG_CAPTURE=1` in
  the dashboard process: the capture worker's startup `warm_up` does a blocking
  `ReplayStory`, and `py_chronolog_client` holds the GIL through it, so Flask's
  `app.run()` never finishes binding. Drain Path-A with a separate
  `chronolog-capture` process (`launch-capture.sh`).
- **Read the data-heavy tabs only after the ~180 s keeper→grapher drain.** A
  `ReplayStory` on a story still inside the drain window blocks for minutes and
  freezes the whole dashboard; after the drain it returns promptly. The Cluster
  tab is CSV-backed and safe anytime.
- **Session ids are `role@scenario`** so a scenario-graph node joins to its
  conversation on click — the inter-agent edge `from/to_session` must carry the
  same scoped id the LLM turns are spooled under.
- On a *brand-new* cluster the capture worker's `warm_up` can hang on the
  never-written session index; if Path-A never drains, kick a one-shot
  `SpoolToChronoLogSync(get_backend()).sync_once()` (append-only, no
  `ReplayStory`) to seed it.

Verified **28/28** checks (all four tabs + the click-through) on live **4-node
and 8-node** ChronoLog clusters.

## Status

Functional, and validated end-to-end with real agents on live multi-node
ChronoLog clusters (see the live-test harness above — 28/28 on 4 and 8 nodes).
Backend, shape adapters, all generic blueprints (conversations, interactions,
provenance, semantic, scenarios, inter_agent), capture stores, the chimaera-free
Path-A capture worker (spool → ChronoLog), and the React frontend are ported and
serve from ChronoLog. See [`MIGRATION.md`](./MIGRATION.md) for the port record.
