# Feature demos — ChronoDoctor · Fleet Health · Critical-Path Tracing

Three self-contained demos for the new analytics features. Each one:

- runs **fully offline** (`CHRONOLOG_OFFLINE=1` on a throwaway `DTP_STATE_DIR`) —
  **no ChronoLog cluster, no API keys, no cost**;
- seeds a realistic multi-agent run into the same spool + inter-agent store the
  dashboard reads;
- prints a narrated, colour-coded walk-through of what the feature found; and
- ends with the exact command to open the **same data live in the dashboard**.

They are the fast way to show what each feature does on a laptop, then prove it
at scale on ARES by pointing the dashboard at the real visor (the analysis code
path is identical — only the data source changes).

```bash
# from the repo root
python3 scripts/demos/demo_doctor.py        # Demo 1 — error detection & diagnosis
python3 scripts/demos/demo_fleet_scale.py   # Demo 2 — 100 nodes at a glance
python3 scripts/demos/demo_tracing.py       # Demo 3 — find the bottleneck in a mesh
```

To view any demo live afterwards, run the command the demo prints, e.g.:

```bash
CHRONOLOG_OFFLINE=1 DTP_STATE_DIR=/tmp/chronolog_demo_doctor \
  PYTHONPATH=src python3 -m backend
# then open http://<host>:5000/?tab=doctor
```

---

## Demo 1 — ChronoDoctor (`demo_doctor.py`)

**What it shows.** ChronoDoctor reads every ChronoLog story, turns failures into
signals, **clusters** them into ranked incidents (500 identical timeouts → one
card), and for each one explains **why it's happening** and **how to fix it**.
Running the scan twice shows the **self-compacting incident memory** flagging
*recurring* incidents across runs.

**What to point at.** The single critical card that collapses dozens of peer
timeouts across many hosts; the **hung/dropped call** incident (invisible in the
per-edge views); and the `RECURRING ×N` badge on the second pass.

**Talk track.** "The system already records failures in three places. ChronoDoctor
fuses them, clusters by signature so the count is the story, and tells the
operator the likely cause and fix — and it remembers, so a repeat offender is
recognised instantly. Diagnosis is heuristic by default (zero cost); set
`CHRONOLOG_DOCTOR_LLM=1` to have Claude write the analysis instead."

## Demo 2 — Fleet Health (`demo_fleet_scale.py`)

**What it shows.** 100 nodes summarised at a glance. Raw events are folded into
per-`(host, minute)` **rollup buckets**; the Fleet view reads those, so
summarising the fleet is `O(nodes × buckets)` — independent of event volume. A
handful of injected trouble nodes float to the top.

```bash
python3 scripts/demos/demo_fleet_scale.py --nodes 250   # push it harder
```

**What to point at.** The timing line ("summarised 100 nodes from N buckets in a
few ms") and that the 3 injected hotspots are exactly the 3 `crit` nodes. In the
dashboard, the **heatmap** colours every node by error-rate / p95 / cost and
sorts the worst first — the node-link graph can't do that past ~12 nodes.

**Talk track.** "At 100 nodes a graph is noise. The win is the rollup: we never
re-scan raw events for the overview, so the heatmap stays instant whether the
fleet did a thousand calls or ten million."

## Demo 3 — Critical-Path Tracing (`demo_tracing.py`)

**What it shows.** One request fanning out across six hosts. The tracer rebuilds
the cross-host **call tree** and computes the **critical path** — the long-pole
chain — then names the **bottleneck hop** by *self-time* (exclusive wall-clock),
correctly ignoring the double-counting of parent/child overlap.

**What to point at.** The ASCII waterfall, and that the bottleneck is the slow
`fetch_chunk` hop (not the root that merely contains it). Total = wall-clock, not
the naive sum.

**Talk track.** "Across many nodes the wall-clock is set by one chain. Per-node
logs can't show it. The trace can — and it points at the one hop worth
optimising."

---

## Tests

The pure logic behind all three features is covered by `pytest` (no cluster
needed):

```bash
python3 -m pytest tests/ -q
```

`tests/test_diagnostics.py`, `tests/test_fleet.py`, `tests/test_tracing.py`.
