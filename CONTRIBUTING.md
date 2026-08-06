# Contributing

Thanks for your interest. This project started as a master's thesis and is
maintained as a research prototype, so issues and small focused pull requests
are more useful than large redesigns.

## Development setup

```bash
git clone https://github.com/annamonso/context-visualizer.git
cd context-visualizer

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

make workspace        # build the React SPA into the Python package
```

`py_chronolog_client` (ChronoLog's Python client) is **not** on PyPI — it is built
from the ChronoLog C++ source tree and placed on `PYTHONPATH` by the deployment.
You do not need it to develop or run the test suite: the backend probes for it at
runtime, and offline mode never constructs a live client.

Run the dashboard without any cluster:

```bash
CHRONOLOG_OFFLINE=1 chronolog-observe    # http://localhost:5000
```

## Checks

Everything CI runs, you can run locally:

```bash
ruff check .                             # lint
pytest                                   # tests + coverage gate
cd frontend && npx tsc --noEmit          # frontend typecheck
cd frontend && npm run build             # frontend build
```

Please make sure all four pass before opening a pull request.

### Tests

`tests/` mirrors `src/`:

| Directory | Covers |
|---|---|
| `tests/api/` | The HTTP surface via Flask's test client, including a sweep asserting no GET route 500s on empty state |
| `tests/capture/` | The Path-A spool — ordering, sequence ids, session-id round-tripping |
| `tests/adapters/` | Registry discovery and capability resolution |
| `tests/test_*.py` | Pure logic: diagnostics, fleet rollups, tracing |

Everything runs offline against a throwaway state directory (see
`tests/conftest.py`), so the suite needs no ChronoLog cluster and no
`py_chronolog_client`.

Coverage has a floor of 50% (`fail_under` in `pyproject.toml`). It ratchets:
raise it as coverage rises, never lower it to turn a red build green. Long-running
daemons (`collector/daemon.py`, `capture/sync_worker.py`) and `checkpointing/`
are excluded — they are exercised on a real cluster, and counting them would make
the number claim more than the suite actually verifies.

If you add a route with a new path parameter, add a value for it in
`tests/api/test_route_smoke.py::_PARAM_VALUES`; the inventory test fails
otherwise, on purpose, so new routes cannot silently escape the sweep.

## Architecture constraints

A few invariants keep the codebase decoupled — please preserve them:

- **Data sources go through adapters.** New sources register under the
  `chronolog_observability.adapters` entry-point group; do not hardcode a source
  in a blueprint.
- **Blueprints declare a `Capability`.** The app factory mounts only what some
  active adapter can serve. A new endpoint needs a capability.
- **Dependency direction is `capture → backend`**, never the reverse.
- **`src/chronolog_observability/static/workspace/` is a build artefact.** Edit
  `frontend/`, not the bundle.

There is more detail in [`CLAUDE.md`](./CLAUDE.md) and
[`docs/features.md`](./docs/features.md).

## Commits

Use `type(scope): summary`, e.g. `feat(fleet): add per-node cost rollup`. Keep
the subject under ~72 characters and explain the *why* in the body when it is not
obvious from the diff.

## Reporting bugs

Please include: what you ran, what you expected, what happened, and whether you
were in offline mode or against a live ChronoLog cluster. Deployment-specific
issues are much easier to reproduce with the ChronoLog version and node count.
