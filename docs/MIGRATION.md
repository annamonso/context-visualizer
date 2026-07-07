# Migration: `context-visualizer` → `chronolog-observability`

This scaffold gives you the target structure and the **decoupling seam**. The
remaining work is porting code bodies into it. Do it in the order below — each
step leaves the package importable.

## File map (old → new)

| old (`context-visualizer/context_visualizer/…`) | new (`backend/…`) | notes |
|---|---|---|
| `chronolog/client.py` | `backend/client.py` | drop the `DTP_CHRONOLOG_BACKEND` on/off gate; ChronoLog is now assumed. Keep `CHRONOLOG_OFFLINE` CSV fallback. |
| `chronolog/constants.py` | `backend/constants.py` | |
| `chronolog/path_a_reader.py` | `backend/path_a_reader.py` | |
| `chronolog/sync_worker.py` | `capture/sync_worker.py` (+ new `capture/spool.py`) | ✅ chimaera-free rewrite. Source is now the local **capture spool** (`CaptureSpool`), not the live chimaera runtime; the worker drains it into the same Path-A chronicles `path_a_reader` reads. Lives under `capture/` (Path-A ingest) to keep `capture → backend` layering. |
| `adapters/conversation_adapter.py` | `adapters/shape/conversation.py` | already chimaera-free ✔ |
| `adapters/scenario_adapter.py` | `adapters/shape/scenario.py` | already chimaera-free ✔ |
| `chimaera_client.py` | `adapters/_chimaera_client.py` | the *only* place allowed to `import chimaera_runtime_ext`. |
| `api/*.py` | `api/*.py` | see capability table below |
| `app.py` | `app.py` | already replaced by the adapter-aware factory in this scaffold. |
| `llm_demo_store.py`, `chimaera_client.py` glue | `capture/` | Path-A/Path-B ingest. |
| `static/`, `templates/` | same | built SPA + Flask shell. |
| `frontend/` (React) | `frontend/` | copy `src/`, configs; **not** `node_modules`. |
| `scripts/*demo*`, `docs/*` | `scripts/`, `docs/` | demo scripts; rename `context_visualizer` imports. |
| `CMakeLists.txt` | *(drop)* | no longer built by the clio-core CMake; pip + a frontend build step replace it. |
| `pyproject.toml` | *(replaced)* | new src-layout + entry points already written. |

## The chimaera decoupling — the actual work

16 modules did `from .. import chimaera_client` (or imported the ext). For each,
the disposition is one of:

**A. Generic view — re-source from ChronoLog (port into `ChronoLogAdapter`).**
The view is reconstructable from stories; replace the chimaera read with the
Path-A reader + shapers. These power capabilities served on a bare host:

- `api/conversations.py`  → `Capability.CONVERSATIONS`
- `api/interactions.py`   → `Capability.INTERACTIONS`  ✅ ported
- `api/provenance.py`     → `Capability.PROVENANCE`    ✅ ported (+ `analysis/` pkg)
- `api/semantic.py`       → `Capability.SEMANTIC`      ✅ ported (+ `semantic/` + `checkpointing/` pkgs)
- `api/scenarios.py`      → `Capability.SCENARIOS`     ✅ ported (+ `capture/` stores)
- `api/inter_agent.py`    → `Capability.INTER_AGENT`   ✅ ported (Path-B ingest endpoint)
- `api/llm_dispatch.py`, `api/demo_llm.py` → fold into capture/demo, drop live reads.

Note: ``api/conversations.py`` (CONVERSATIONS) is also ported ✅.

**B. Live-runtime views — DROPPED (chimaera removed).**
The original plan kept an optional `ChimaeraAdapter` for views that introspect
the running chimaera runtime (`topology`, `workers`, `pools`, `node`, `system`,
`overhead`, and live checkpoint create/restore). Per the decision to not depend
on chimaera at all, these are **not ported** and the chimaera adapter,
entry-point, capabilities, and legacy Flask HTML templates were removed. The
plugin is ChronoLog-only.

`recovery` survives as a *generic* capability — recovery events live in ChronoLog
(`CHRONICLE_RECOVERY_EVENTS`), so `ChronoLogAdapter` serves them via
`path_a_reader.get_recovery_events`; the `checkpointing/` package is ported
(chimaera-free) for the rollback analyzer's `Checkpoint` type.

**Invariant to enforce:** nothing may `import chimaera` anywhere. A grep gate:

```bash
grep -rn "import chimaera" backend \
  --include='*.py' && echo "LEAK" || echo "clean"
```

## Suggested order

1. `backend/` — port client/constants/path_a_reader; collapse the on/off flag.
2. `adapters/shape/` — copy the two shapers (already clean).
3. `ChronoLogAdapter.fetch` — wire generic capabilities (group A).
4. Port group-A blueprints; verify on a bare host (`CHRONOLOG_OFFLINE=1`).
5. `_chimaera_client.py` + `ChimaeraAdapter.fetch`; port group-B blueprints.
6. `frontend/` — copy, gate live-panel UI on `/api/config` capabilities.
7. Run the grep gate; delete `CMakeLists.txt`.

## Run the import helper

`scripts/import_from_clio_core.sh` copies the reusable trees into place (skipping
`node_modules`/`__pycache__`) so you start from real code, then you refactor.
