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
├── capture/          # inter-agent edge store + Path-B ingest + demo store
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

## Status

Functional. Backend, shape adapters, all generic blueprints (conversations,
interactions, provenance, semantic, scenarios, inter_agent), capture stores, and
the React frontend are ported and serve from ChronoLog. See
[`MIGRATION.md`](./MIGRATION.md) for the port record.
