# Repo Analysis — ChronoLog Observability

> Starter orientation doc for analyzing this repo. High-level only — what it is,
> how it's laid out, and where to start reading.

## What it is

**ChronoLog Observability** is a drop-in **observability dashboard + trace-capture
layer** for [ChronoLog](https://github.com/grc-iit/ChronoLog) deployments (a
distributed log/storage substrate). You point it at a running ChronoLog visor and
it reconstructs **conversation / scenario / interaction / provenance / cluster /
fleet / doctor / trace** views entirely from ChronoLog stories — no other backend
required.

It's a **Flask (Python) backend + React (TypeScript) single-page app**, packaged as
a standalone pip-installable plugin. It is a repackaging of a former
`clio-core/context-visualizer`, with ChronoLog promoted from optional backend to
the required substrate.

**Key design idea:** every view is a `Capability` served by a pluggable **adapter**.
The app discovers active adapters at startup and mounts only the blueprints some
adapter can serve. The shipped `ChronoLogAdapter` serves them all; third parties
can add their own data source without forking.

## At a glance

| | |
|---|---|
| **Backend** | Python ≥3.10, Flask · ~12.7k LOC · 59 files in `src/` |
| **Frontend** | React 18 + TypeScript + Vite + Tailwind · ReactFlow / visx / dagre · ~10.2k LOC · 56 files |
| **Scripts** | 21 (live cluster bring-up, real-agent harness, offline demos) |
| **Tests** | 3 pytest files over the pure logic (detectors, fleet rollups, tracing) |
| **Console entry points** | `chronolog-observe` (dashboard), `chronolog-capture`, `chronolog-collector`, `chronolog-doctor`, `chronolog-fleet-rollup` |
| **History** | 18 commits · sole author: Anna Monsó |
| **Docs** | `README.md` (setup/ops), `FEATURES.md` (feature reference), `MIGRATION.md` (port record) |

## Architecture (data flow)

```
agents ──Path-A (LLM turns)─────▶ spool ──capture worker──▶ ChronoLog stories ─┐
       ──Path-B (agent→agent calls)─▶ per-node collector ──▶ ChronoLog keeper ─┤
                                                                               ▼
                                                       dashboard (Flask)
                                                       reads ChronoLog (cold path)
                                                       + local hot store (live views)
                                                               │
                                                               ▼
                                                       React SPA (8 tabs)
```

- **ChronoLog is the backbone** — durable distributed storage; a keeper runs on every
  node, a visor coordinates, a grapher/player drains keepers to a CSV/HDF5 archive.
- **Path-A** = LLM interactions (prompts, responses, tokens, cost, latency, context
  growth) → local spool → capture worker drains into ChronoLog.
- **Path-B** = inter-agent calls (agent→agent MCP tool invocations) → per-node
  collector → that node's keeper.
- **Live views** are served from a local **hot store** (instant, GIL-safe) because
  ChronoLog has a ~180 s keeper→grapher drain and a `ReplayStory` on an un-drained
  story blocks the process. Full-fidelity views read ChronoLog after the drain.

## Top-level layout

| Path | What it is |
|------|------------|
| `backend/` | The Python backend package (Flask app, adapters, capture, analytics) |
| `frontend/` | React + Vite single-page app (the dashboard UI) |
| `scripts/` | Operational scripts — live cluster bring-up, real-agent harness, offline demos |
| `tests/` | pytest unit tests over the pure analysis logic |
| `docker/` | Offline/demo image — `Dockerfile`, `docker-compose.yml`, `entrypoint.sh`, `README.md` (`make demo`) |
| `docs/` | Project docs — this file, `FEATURES.md`, `MIGRATION.md`, `analysis.html` |
| `README.md` | Setup & ops (kept at repo root) |
| `pyproject.toml` / `Makefile` | Python packaging + build tasks (`make workspace` builds the SPA) |

## Backend map (`backend/`)

| Subpackage | Responsibility |
|------------|----------------|
| `app.py` | Flask factory — discovers adapters, mounts capability blueprints, serves the SPA |
| `config.py` | Runtime config — visor endpoint, offline mode, bind host/port |
| `chronolog/` | ChronoLog substrate access — `client`, `constants`, `path_a_reader` |
| `adapters/` | **The data-source seam** — `base.py` (`SourceAdapter` + `Capability`), `registry.py` (entry-point discovery), `chronolog_source.py` (the shipped adapter), `shape/` (story → conversation/scenario graph shapers) |
| `api/` | Flask blueprints, **one per capability**: `conversations`, `interactions`, `scenarios`, `provenance`, `semantic`, `inter_agent`, `chronolog_view` (cluster), `diagnostics` (doctor), `fleet`, `tracing` |
| `capture/` | Path-A spool + sync worker; Path-B inter-agent store/bus/ingest; live demo store |
| `collector/` | Per-node collector daemon (agents → ChronoLog keeper + live forward to dashboard) |
| `analysis/` | Pure analysis — `call_graph`, `context_graph`, `trace` (critical-path) |
| `diagnostics/` | **ChronoDoctor** — `detectors`, `diagnoser`, `incidents`, `memory`, `scan`, `gather`, `worker` |
| `fleet/` | **Fleet Health** — per-`(host, minute)` rollups: `rollup`, `store`, `worker` |
| `semantic/` | Semantic checks — `checker`, `config`, `rollback_analyzer` |
| `checkpointing/` | `checkpoint_manager` |
| `static/` | Built SPA bundle lands here at build time (gitignored artifact) |

## Frontend map (`frontend/src/`)

| Path | What it is |
|------|------------|
| `pages/` | One component per dashboard tab — `Workspace`, `Scenario`, `Interactions`, `Cluster`, `Fleet`, `Doctor`, `Traces`, `Memory` |
| `components/workspace/` | Graph & timeline views (agent flow, cluster comms, scenario flow, timeline) + ReactFlow `nodes/` |
| `components/detail/` | Interaction detail panels — messages, tokens, stream timeline, KPIs, context budget |
| `components/ui/` | Reusable widgets — `CopyButton`, `JsonViewer`, `Toast` |
| `hooks/` | Data hooks — `useConversationData`, `useHostData`, `useLiveInteractions`, `useLiveScenario`, `usePlayhead` |
| `lib/` | Pure helpers — `contextBudget`, `cost`, `format`, `parseError` |
| `api.ts` / `types.ts` | Backend client + shared TypeScript types |

## Dashboard tabs → source

| Tab | Purpose | Data source |
|-----|---------|-------------|
| **Workspace** | Per-agent conversation tree — LLM turns, handoffs, subagents | Path-A (ChronoLog) |
| **Scenarios** | Multi-agent scenario graph — peers + inter-agent edges, live | Path-B (hot store + SSE) |
| **Interactions** | Flat feed of every LLM turn | Path-A (ChronoLog) |
| **Cluster** | ChronoLog topology + node-to-node comms + idle/available nodes | topology + hot store + `sinfo` |
| **Fleet** | Per-node health heatmap (errors / p95 / throughput / cost), scales to 100+ nodes | rollups / spool |
| **Doctor** | ChronoDoctor — error detection, diagnosis, self-compacting memory | all stories |
| **Traces** | Distributed critical-path tracing waterfall | inter-agent edges |
| **Memory** | Per-agent working memory (context-graph tokens growing/evicting) | Path-A spool |

## Where to start reading

1. `README.md` — setup, run modes (live / offline), operational notes.
2. `FEATURES.md` — the complete feature reference (every tab, capture model, scripts).
3. `backend/app.py` — the wiring: how adapters → capabilities → blueprints → SPA fit together.
4. `backend/adapters/base.py` — the `Capability` / adapter seam everything hangs off.
5. `backend/api/*.py` — per-feature backend behavior, one blueprint per tab.
6. `frontend/src/pages/*` — the matching UI for each tab.
