# Migration: `context-visualizer` → `chronolog-observability`

This scaffold gives you the target structure and the **decoupling seam**. The
remaining work is porting code bodies into it. Do it in the order below — each
step leaves the package importable.

## File map (old → new)

| old (`context-visualizer/context_visualizer/…`) | new (`src/chronolog_observability/…`) | notes |
|---|---|---|
| `chronolog/client.py` | `backend/client.py` | drop the `DTP_CHRONOLOG_BACKEND` on/off gate; ChronoLog is now assumed. Keep `CHRONOLOG_OFFLINE` CSV fallback. |
| `chronolog/constants.py` | `backend/constants.py` | |
| `chronolog/path_a_reader.py` | `backend/path_a_reader.py` | |
| `chronolog/sync_worker.py` | `backend/sync_worker.py` | **remove** its `import chimaera_client`; capture ingest no longer reads live runtime. |
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
- `api/scenarios.py`      → `Capability.SCENARIOS`
- `api/interactions.py`   → `Capability.INTERACTIONS`
- `api/provenance.py`     → `Capability.PROVENANCE`
- `api/semantic.py`       → `Capability.SEMANTIC`
- `api/overhead.py`       → `Capability.OVERHEAD`
- `api/inter_agent.py`    → `Capability.INTER_AGENT`
- `api/llm_dispatch.py`, `api/demo_llm.py` → fold into capture/demo, drop live reads.

**B. Live-runtime view — keep chimaera, route through `ChimaeraAdapter`.**
These genuinely need the running runtime; they mount only when chimaera is
present. Replace direct `chimaera_client` imports with calls into
`adapters/_chimaera_client.py` via the adapter:

- `api/topology.py`  → `Capability.TOPOLOGY`
- `api/workers.py`   → `Capability.WORKERS`
- `api/pools.py`     → `Capability.POOLS`
- `api/node.py`      → `Capability.NODE`
- `api/system.py`    → `Capability.SYSTEM`
- `api/recovery.py`  → `Capability.RECOVERY`
- `checkpointing/checkpoint_manager.py` + `api/checkpoints.py` → `Capability.CHECKPOINTS`

**Invariant to enforce:** outside `adapters/_chimaera_client.py` and
`adapters/chimaera_source.py`, nothing may `import chimaera`. A grep gate:

```bash
grep -rn "import chimaera" src/chronolog_observability \
  --include='*.py' | grep -v 'adapters/_chimaera_client.py' \
                   | grep -v 'adapters/chimaera_source.py' && echo "LEAK" || echo "clean"
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
