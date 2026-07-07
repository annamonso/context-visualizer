# Running the dashboard locally with Docker (offline demo)

This runs the ChronoLog Observability dashboard **fully offline** — a
self-contained Flask app serving the built React SPA off a local JSONL spool.
No ChronoLog cluster, no `py_chronolog_client` (the C++ client), no API keys, no
cost. Ideal for understanding the whole system on a laptop.

## Quick start

```bash
docker compose up --build      # build image, seed demo data on first boot
# open http://localhost:5000
```

To stop / reseed from scratch:

```bash
docker compose down            # stop, keep the seeded data volume
docker compose down -v         # stop AND wipe the data volume (fresh seed next up)
```

## What actually runs

The image is built in two stages (see `Dockerfile`):

1. **`node:24-slim`** builds the React SPA (`frontend/`) into
   `backend/static/workspace/` — Vite's `outDir`.
2. **`python:3.12-slim`** `pip install`s the package (SPA baked into
   package-data) and runs `chronolog-observe`.

At container start, `docker/entrypoint.sh`:

- seeds the three feature demos into `/data` (only if empty and `SEED_DEMO=1`),
- launches the dashboard with `CHRONOLOG_OFFLINE=1`.

In offline mode `backend.get_backend()` returns `None` **before** any attempt to
import the native ChronoLog client, so the image needs zero HPC/native deps.
Every view is served from the local spool instead of a live visor.

### Environment variables

| Var | Default (image) | Meaning |
|-----|-----------------|---------|
| `CHRONOLOG_OFFLINE` | `1` | Serve from the local spool instead of a live visor. |
| `DTP_STATE_DIR` | `/data` | Where the spool + inter-agent hot store live (the mounted volume). |
| `DEMO_STATE_DIR` | `/data` | Where the demo seeders write (kept equal to `DTP_STATE_DIR`). |
| `SEED_DEMO` | `1` | Seed demo data on first boot. Set `0` to start empty. |
| `OBSERVE_HOST` / `OBSERVE_PORT` | `0.0.0.0` / `5000` | Flask bind. |

## The demo data

`SEED_DEMO=1` runs the three shipped demos (`scripts/demos/`) into a shared
state dir, so the dashboard comes up populated with three simulated multi-agent
runs on a fake `ares-comp-NN` cluster — with realistic failures baked in:

- **`incident-demo`** — a broken run: a node returning HTTP 500s, a rate-limited
  node (429 storm), waves of inter-agent peer timeouts, hung/dropped calls,
  retry storms, and checkpoint restores.
- **`fleet-demo`** — 60 nodes emitting metrics with a couple of hotspots.
- **`trace-demo`** — a clean call tree for critical-path analysis.

## The tabs (what each one shows)

Open a tab directly with `http://localhost:5000/?tab=<name>`.

| Tab | `?tab=` | Backing API | Shows |
|-----|---------|-------------|-------|
| Workspace | `workspace` | `/_interceptor/interactions` | Per-agent conversations rebuilt from LLM turns. |
| Scenarios | `scenarios` | `/api/_inter-agent/*`, `/api/.../graph` | Agent-to-agent call graph; click a node → that agent's conversation. |
| Interactions | `interactions` | `/_interceptor/interactions` | Flat, filterable table of every LLM call (status, latency, tokens, cost). |
| Cluster | `cluster` | `/api/chronolog/comms` | Node-to-node traffic graph (which hosts exchange messages). |
| Fleet | `fleet` | `/api/fleet/health` | Heatmap of all nodes; scales to 100+ via per-(host,minute) rollups. |
| Doctor | `doctor` | `/api/diagnostics/scan` | Failures **clustered** into ranked incidents with root cause + fix. |
| Traces | `traces` | `/api/tracing/<sid>/critical-path` | Critical-path waterfall; names the bottleneck hop by self-time. |
| Memory | `memory` | context-graph nodes | Per-agent working memory (tokens growing / evicting). |

## How the pieces fit (data flow)

```
                 ┌─────────────── the dashboard process ───────────────┐
  agents ──▶ spool (Path-A) ──▶ [OFFLINE: read straight from spool] ──▶ views
                                [LIVE:    sync worker → ChronoLog → replay]

  agents ──▶ ingest (Path-B) ──▶ inter-agent hot store (JSONL) ──▶ live views (SSE)
                              └─▶ ChronoLog (durable cold path)
```

- **Adapters + capabilities.** `app.create_app()` discovers data-source
  *adapters* (entry-point group `backend.adapters`) and mounts
  only the blueprints whose `Capability` some adapter can serve. The shipped
  `ChronoLogAdapter` serves all of them. `/api/config` lists what's active.
- **Path-A (capture → storage).** Agents append events to a local spool; a sync
  worker drains it into ChronoLog (live mode). Offline, there's nothing to
  drain — the reader serves the spool JSONL directly (`backend/path_a_reader.py`).
- **Path-B (live bus).** Inter-agent events are written to a local **hot store**
  and fanned out to SSE subscribers at ingest, because ChronoLog's
  keeper→grapher drain (~180 s) makes it unusable as a real-time read source.
  The hot store is what makes Scenarios-live and the Cluster graph instant.
- **Cold path.** ChronoLog is the durable store in live mode; the hot store /
  spool are the fast local reads. Offline mode simply has no cold path.

## What this image is NOT

This is the **offline/demo** image. A **live** deployment (pointing at a real
ChronoLog visor) additionally needs `py_chronolog_client` — a C++ module built
from the ChronoLog source tree, never pip-installed — plus a running ChronoLog
substrate (visor/keeper/grapher over Mercury/OFI networking, normally on SLURM).
Containerizing that means containerizing ChronoLog itself and is out of scope
here. The analysis code path is identical between the two; only the data source
changes.
