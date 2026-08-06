# ChronoLog Observability — Features

A drop-in observability dashboard and trace-capture layer for **ChronoLog**
deployments. It reconstructs everything it shows from ChronoLog stories, so it
runs against any ChronoLog cluster — and it ships a no-cost offline demo mode for
viewing the UI without a cluster.

This document is the complete feature reference. For setup and operational
notes see [`README.md`](./README.md).

---

## 1. Architecture in one picture

```
agents ──Path-A (LLM turns)──▶ spool ──capture worker──▶ ChronoLog stories ─┐
       ──Path-B (agent calls)─▶ per-node collector ─────▶ ChronoLog keeper ─┤
                                                                            ▼
                                                          dashboard (reads ChronoLog
                                                          + a local hot store for live)
```

- **ChronoLog is the backbone** — durable, distributed storage. A **keeper runs on
  every node** (the recording group); a visor coordinates; a grapher/player drain
  keepers to a CSV/HDF5 archive.
- **Path-A** (LLM interactions: prompts, responses, tokens, cost, latency,
  context-graph growth) → local **spool** (shared NFS) → **capture worker** drains
  it into ChronoLog.
- **Path-B** (inter-agent calls: agent→agent MCP tool invocations) → per-node
  **collector** → that node's keeper.
- **Live views** are served from a local **hot store** (instant, GIL-safe) because
  ChronoLog has a ~180 s keeper→grapher drain and a `ReplayStory` on an un-drained
  story blocks the process. Full-fidelity views read ChronoLog after the drain.
- **Source-agnostic seam**: every view is a `Capability` served by an adapter; the
  shipped `ChronoLogAdapter` serves them all. `/api/config` advertises which are
  active, and the SPA shows only those tabs.

---

## 2. Dashboard tabs

| Tab | Purpose | Source |
|-----|---------|--------|
| **Scenarios** | Multi-agent scenario graph — peers + inter-agent edges, live. Clicking an agent or host opens the **node inspector** pop-up: per-node LLM activity (flow graph + playhead timeline + turn detail), inter-agent I/O feed, and per-agent context memory | Path-B (hot store + SSE) + Path-A (ChronoLog) |
| **Health** | Merged live-traffic + **PrismaDoctor** view — incident rail (clustered failures with diagnosis) beside the flat feed of every LLM turn; incidents filter the feed, feed rows badge their incident | Path-A (ChronoLog) + all stories |
| **Cluster** | ChronoLog deployment topology + node-to-node comms + **idle/available nodes** | topology + hot store + `sinfo` |
| **Fleet** | Per-node **health heatmap** (errors / p95 / throughput / cost), scales to 100+ nodes | rollups / spool |
| **Traces** | Distributed **critical-path tracing** waterfall | inter-agent edges |
| **Memory** | Per-agent **working memory** (context-graph tokens growing/evicting) | Path-A spool |

The former **Workspace** tab lives on as the Scenarios node-inspector pop-up;
the former **Interactions** and **Doctor** tabs are merged into **Health**.
Legacy `?tab=interactions` / `?tab=doctor` links redirect to Health.

A **cluster-capacity badge** in the header shows the underlying SLURM cluster at a
glance — total / idle / busy / down nodes, refreshed every 30 s from a live
`sinfo`.

---

## 3. PrismaDoctor — error detection & diagnosis (Health tab)

Reads every ChronoLog story, turns failures into signals, clusters them, and
explains each one.

- **Detection** (pure, unit-tested): HTTP 4xx/5xx & explicit errors on LLM turns,
  latency outliers vs the per-model median, retry storms (same prompt re-issued),
  failed inter-agent calls, **hung/dropped calls** (a `start` with no `done`),
  slow edges, and recovery/backtrack events.
- **Clustering**: groups by signature so 500 identical timeouts become **one
  ranked incident** (count + affected hosts), not 500 rows — essential at scale.
- **Diagnosis**: a deterministic, zero-cost **heuristic engine** gives a concrete
  *root cause* + *remediation* + *blast radius* + confidence per incident.
  `CHRONOLOG_DOCTOR_LLM=1` swaps in a Claude-backed diagnoser (falls back to the
  heuristic if unavailable).
- **Self-compacting incident memory** (`incident_memory.md`): one line per
  incident; when it grows past a threshold the oldest entries fold into a *Digest*
  with running counts. This is what earns a repeat failure a **`recurring ×N`**
  badge — cross-run learning that survives restarts. Viewable via the Doctor
  tab's **Memory** button.
- **Controls**: **Scan now** (one-shot) and **Activate Doctor** (live background
  scanning, also `CHRONOLOG_DOCTOR=1` / `chronolog-doctor`).
- **Fast & non-blocking**: scans read the local spool + hot store (instant,
  GIL-safe), never the GIL-blocking `ReplayStory` path.

---

## 4. Fleet Health — node health at scale (Fleet tab)

The Cluster node-link graph is unreadable past ~12 nodes; Fleet is the heatmap
that scales.

- **Heatmap grid**: one cell per node, colored by the chosen metric (health /
  error-rate / p95 latency / throughput / cost), worst nodes sorted first; click
  a node to drill into its per-minute time series.
- **Rollups**: per-`(host, minute)` buckets (`metrics_rollup` chronicle) make the
  overview `O(nodes × buckets)`, independent of event volume. Emitter is opt-in
  (`CHRONOLOG_FLEET_ROLLUP=1` / `chronolog-fleet-rollup`); the API falls back to
  computing from the spool on demand, so the tab is never empty.
- **Shows every node**: active nodes colored by health, the rest **greyed by real
  SLURM state** (idle / busy / down) — using **real node names**, never invented.
- **Persistent by default**: the time window defaults to **all** so captured data
  stays visible after a run ends (narrow to 5m/15m/1h for "what's hot now").
- **The reference view**: shows all nodes so you can compare which perform better.

---

## 5. Distributed critical-path tracing (Traces tab)

- Reconstructs cross-host **call trees** from inter-agent edges (explicit
  `parent_correlation_id` when present, else inferred from session + time nesting).
- Computes the **critical path** — the long-pole root-to-leaf chain — and names the
  **bottleneck hop** by *self-time* (exclusive wall-clock), correctly ignoring the
  parent/child overlap that would otherwise double-count.
- Renders as a **waterfall** with a legend, depth indentation, and the critical
  path highlighted — answering "why is this scenario slow across N nodes?".

---

## 6. Generate Traffic — synthetic demos from the UI (no LLM cost)

A configurable button that drives the live views instantly:
- **Pattern**: mesh / star / pipeline / ring (pipeline/star produce nested traces)
- **Agents**, **Nodes** (capped to the cluster's idle count, with a **Max** quick-set),
  **Rounds**, **Errors**, **Speed**
- Spreads agents across **real node names** (the live allocation, or `sinfo` idle
  nodes) — never fabricated. Writes through ChronoLog when live; local JSONL when
  offline.

---

## 7. Real-agent harness (genuine LLM + ChronoLog)

Drives **real Claude Agent SDK agents** on a live ChronoLog cluster — genuine LLM
turns and a real `call_remote_agent` MCP tool call per agent.

- **Multiple agents per node**: `AGENTS_PER_NODE=K` runs K real agents on every
  node, each with a unique `role@scenario` id, each captured through *its node's*
  collector + keeper. "Deploy however many agents you want."
- Per-agent: framing/reasoning turns + a delegation turn that really invokes the
  MCP tool; turns → spool (Path-A), edges → collector (Path-B).
- **Session-id rule**: edges use the scenario-scoped `role@scenario` so a clicked
  scenario node joins to its conversation.

---

## 8. Scripts

| Script | What it does |
|--------|--------------|
| `scripts/ops/chronolog-live.sh [N] [partition]` | **Bring ChronoLog LIVE**: allocate N idle nodes → deploy ChronoLog (keeper on each) → start collectors + capture worker + dashboard connected to the live visor. No LLM. |
| `scripts/ops/allocate-and-run.sh [N] [scenario] [partition]` | Allocate → deploy ChronoLog → run **real agents** end-to-end (respects `AGENTS_PER_NODE`). Uses LLM. |
| `scripts/ops/run-live-agents.sh <JOBID> [scenario]` | Run real agents on an existing allocation (healthcheck → dashboard → capture → collectors → agents → verify). |
| `scripts/demos/demo_doctor.py` | Offline PrismaDoctor demo (errors → incidents → diagnosis → recurring memory). |
| `scripts/demos/demo_fleet_scale.py` | Offline Fleet demo at 100+ nodes (rollups + heatmap). |
| `scripts/demos/demo_tracing.py` | Offline critical-path demo (nested call tree + bottleneck). |
| `scripts/demos/synth_live_traffic.py` | Synthetic per-node live traffic over the real ingest path. |

Console scripts: `chronolog-observe`, `chronolog-capture`, `chronolog-collector`,
`chronolog-doctor`, `chronolog-fleet-rollup`.

---

## 9. Capacity & cluster awareness

- `/api/cluster/capacity` shells a live `sinfo` and returns total / idle / busy /
  down plus a per-node state map and the idle node names.
- Drives: the header capacity badge, the Generate-Traffic node cap, the Fleet
  greying, and the Cluster tab's **idle/available** nodes (drawn green-dashed
  alongside the deployment, with other jobs' busy/down nodes hidden).

---

## 10. Tests & demos

- **Unit tests** (`tests/`, `pytest`) cover the pure logic: detectors, incident
  clustering, self-compacting memory, fleet rollup aggregation, critical-path
  computation. No cluster needed.
- **Offline demos** (`scripts/demos/`) seed realistic data and print a narrated
  walk-through; each ends with the command to open the same data live.

---

## 11. Operational notes

- **Tunnel from a laptop**: dashboard runs on a compute node (Mercury subnet
  rules), so SSH-forward to that node (e.g. `-L 24000:ares-comp-11:5000`). Offline
  demo mode can run on the gateway and forward to its own `localhost`.
- **Drain window**: live views (Scenarios, Cluster, Fleet, Health incidents,
  Traces, Memory) are instant; per-node LLM turns (node inspector) and the
  Health feed's full fidelity fill in after the ~180 s keeper→grapher drain —
  the node inspector lands on its I/O tab and says so while turns are pending.
- **Capture is per-host**: keeper + collector + capture worker on every node;
  `chronolog-live.sh` sets this up automatically.
