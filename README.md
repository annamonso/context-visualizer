# ChronoLog Observability

A drop-in observability dashboard and trace-capture layer for **ChronoLog**
deployments. Point it at a running ChronoLog visor and get conversation /
scenario / interaction / provenance views reconstructed from ChronoLog stories —
no chimaera or clio-core required.

This is a repackaging of the former `clio-core/context-visualizer` into a
standalone, pip-installable plugin. The central change is that the two external
couplings swapped roles:

| | old (`context-visualizer`) | new (this plugin) |
|---|---|---|
| **ChronoLog** | optional backend behind `DTP_CHRONOLOG_*` flags | **required substrate** |
| **chimaera** | hard dependency (16 modules import it) | **optional adapter**, auto-enabled when present |

## How it works

Data sources are **adapters** selected at runtime via the
`chronolog_observability.adapters` entry-point group:

- **`ChronoLogAdapter`** (default) — serves the generic views from ChronoLog
  stories alone. Available on any ChronoLog host.
- **`ChimaeraAdapter`** (optional) — adds live-runtime panels (topology, workers,
  pools, node, system, recovery). Activates only when `chimaera_runtime_ext` is
  importable.

Blueprints declare the `Capability` they need; `app.create_app()` mounts only the
ones an active adapter can serve. So the same wheel runs on a bare ChronoLog host
and inside a full clio-core stack, with no feature flags.

```
src/chronolog_observability/
├── app.py            # adapter-aware Flask factory (no hard chimaera import)
├── config.py         # visor endpoint, offline mode, bind
├── backend/          # ChronoLog substrate (client, path_a_reader, sync_worker)
├── adapters/
│   ├── base.py       # SourceAdapter + Capability  <- the decoupling seam
│   ├── registry.py   # entry-point discovery + capability resolution
│   ├── chronolog_source.py   # default adapter (generic)
│   ├── chimaera_source.py    # optional adapter (live runtime)
│   └── shape/        # story -> conversation/scenario graph shapers
├── capture/          # Path-A proxy + Path-B inter-agent ingest
├── api/              # Flask blueprints (one per capability)
├── static/           # built React SPA lands here
└── templates/
```

## Install & run

```bash
pip install -e .                     # py_chronolog_client must be on PYTHONPATH
export CHRONOLOG_VISOR_IP=10.x.x.x   # raw IP, NOT the -40g hostname
chronolog-observe                    # serves on :5000
```

Demo / dev without a live deployment:

```bash
CHRONOLOG_OFFLINE=1 chronolog-observe  # replays from drained CSVs
```

## Status

Scaffold. The seam (adapters, registry, app factory, backend probe) is in place;
the read/shape/capture bodies are ported from `context-visualizer` per
[`MIGRATION.md`](./MIGRATION.md).
