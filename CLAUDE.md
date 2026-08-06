# CLAUDE.md

Guidance for AI coding agents working in this repository.

## What this is

An observability dashboard for multi-agent LLM systems running across an HPC
cluster. It stores nothing of its own: every view is reconstructed from
[ChronoLog](https://github.com/grc-iit/ChronoLog) stories. Python/Flask backend,
React/Vite frontend, originally a master's thesis prototype.

## Commands

```bash
pip install -e ".[dev]"           # install with dev tooling
make workspace                    # build the React SPA into the Python package
make workspace-dev                # Vite dev server on :5173, proxied to :5000

ruff check .                      # lint                    <- CI runs this
pytest                            # tests + coverage gate   <- CI runs this
cd frontend && npx tsc --noEmit   # typecheck               <- CI runs this
cd frontend && npm run build      # build                   <- CI runs this

CHRONOLOG_OFFLINE=1 chronolog-observe   # run the dashboard with no cluster
```

Run all four before considering a change done. They are exactly what CI runs, so
a local pass means a green build.

CI has a third job beyond those: it builds the SPA, installs the package
**non-editable**, boots it offline and probes the API. That is what catches the
bundle silently ceasing to be package-data — a failure where every unit test
still passes and the shipped wheel serves no UI.

Tests live in `tests/` mirroring `src/` (`tests/api/`, `tests/capture/`,
`tests/adapters/`) and run entirely offline against a throwaway state dir from
`tests/conftest.py`. Coverage floor is 50% and ratchets upward — never lower it
to make a build pass. `tests/api/test_route_smoke.py` sweeps every non-streaming
GET route and fails if a new one appears with a path parameter it does not know,
so routes cannot escape the sweep unnoticed.

## Layout

```
src/chronolog_observability/
├── app.py            # Flask factory: discovers adapters, mounts blueprints
├── config.py         # visor endpoint, offline mode, bind address
├── backend/          # ChronoLog substrate: client, constants, path_a_reader
├── adapters/
│   ├── base.py       # SourceAdapter + Capability — the data-source seam
│   ├── registry.py   # entry-point discovery + capability resolution
│   └── shape/        # story -> conversation / scenario graph shapers
├── capture/          # Path-A spool + sync worker; Path-B store + live bus
├── collector/        # per-node collector daemon
├── api/              # Flask blueprints, one per capability
├── analysis/         # trace, call_graph, context_graph (pure functions)
├── diagnostics/      # ChronoDoctor: detectors, diagnoser, incidents, memory
├── fleet/            # rollup, store, worker
└── static/workspace/ # BUILD ARTEFACT — the compiled SPA lands here

frontend/src/         # React sources. Edit here, never the built bundle.
scripts/demos/        # offline seeders + synthetic traffic
scripts/ops/          # cluster bring-up, launchers, verification (SLURM)
scripts/eval/         # thesis evaluation benchmarks
tests/                # pytest over the pure logic
thesis/               # LaTeX source + built PDF
```

## Architecture invariants

These are not obvious from reading a single file, and breaking them breaks the
design:

- **Data sources go through adapters.** New sources register under the
  `chronolog_observability.adapters` entry-point group. Never hardcode a data
  source inside a blueprint.
- **Blueprints declare a `Capability`.** `app.create_app()` mounts only the
  blueprints some active adapter can serve, and `/api/config` advertises which
  are live so the SPA shows only those tabs. A new endpoint needs a capability.
- **Dependency direction is `capture → backend`, never the reverse.**
- **`src/chronolog_observability/static/workspace/` is generated.** It is
  gitignored and rebuilt by `make workspace`. Never edit it; edit `frontend/`.
- **Path-A and Path-B are separate.** Path-A is LLM turns (agent → local spool →
  sync worker → ChronoLog). Path-B is inter-agent calls (agent → per-node
  collector → that node's keeper). They have different schemas, different
  latencies and different failure modes. Do not merge them.
- **Live views read the hot store, not ChronoLog.** ChronoLog's keeper→grapher
  drain lags ~180 s and a `ReplayStory` on an un-drained story blocks the process
  for minutes while holding the GIL. Anything that must be real-time reads the
  local hot store; ChronoLog is the durable cold path.
- **`py_chronolog_client` is not on PyPI.** It is built from the ChronoLog C++
  tree and placed on `PYTHONPATH` by the deployment. It must stay an optional
  runtime probe, never a hard dependency — and the test suite must never require
  it.
- **Session ids are `role@scenario`.** Inter-agent edges must carry the same
  scoped id the LLM turns are spooled under, or the scenario→conversation
  click-through silently breaks.
- **Node names need canonicalising.** Reverse DNS yields `ares-comp-3`, SLURM
  zero-pads to `ares-comp-03`. Any new source of node names must canonicalise, or
  single-digit nodes vanish from the cluster view.

## Conventions

- Docstrings and comments in English. Explain *why*, not *what* — the existing
  code comments the non-obvious reasoning, and that is the standard to match.
- Commits: `type(scope): summary`, e.g. `feat(fleet): add per-node cost rollup`.
- Lint is deliberately narrow (`E`, `F`, `W`; `E501` ignored). The stylistic and
  modernisation families (`I`, `UP`, `B`) are **off on purpose** — enabling them
  rewrites ~560 lines. Do not turn them on as a side effect of another change.
- Never hardcode hostnames, IPs, usernames or local paths. Read them from
  environment variables with a placeholder default — `scripts/ops/chronolog-live.sh`
  is the pattern to follow.

## Gotchas

- Do not set `CHRONOLOG_CAPTURE=1` in the dashboard process: the capture
  worker's startup `warm_up` blocks on a `ReplayStory` holding the GIL, so Flask
  never finishes binding its port. Capture runs as a separate process.
- `thesis/` builds with a local driver that is intentionally not in the repo;
  `main.pdf` ships pre-built. Do not add a build script back.
- `scripts/eval/out/*.csv` are gitignored raw measurement data; `results.md` in
  that directory is the tracked summary.
