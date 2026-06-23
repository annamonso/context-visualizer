"""Flask application factory.

Discovers the data-source adapters at startup and registers only the blueprints
whose capability some active adapter can serve. The shipped adapter reads from
ChronoLog, so the dashboard runs against any ChronoLog deployment.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify

from .adapters.base import Capability
from .adapters.registry import registry
from .config import Config

log = logging.getLogger(__name__)


# Maps each blueprint to the capability it needs. All are served from ChronoLog.
#
# url_prefix: conversations / interactions declare ABSOLUTE routes (``/api/...``,
# ``/_interceptor/...``) so they mount with NO prefix; every other blueprint
# declares bare paths and gets ``/api``.
_BLUEPRINTS = [
    # (import_path, attr, url_prefix, required_capability)
    ("chronolog_observability.api.conversations", "bp", "", Capability.CONVERSATIONS),
    ("chronolog_observability.api.interactions", "bp", "", Capability.INTERACTIONS),
    ("chronolog_observability.api.scenarios", "bp", "/api", Capability.SCENARIOS),
    ("chronolog_observability.api.provenance", "bp", "/api", Capability.PROVENANCE),
    ("chronolog_observability.api.semantic", "bp", "/api", Capability.SEMANTIC),
    ("chronolog_observability.api.inter_agent", "bp", "/api", Capability.INTER_AGENT),
    ("chronolog_observability.api.chronolog_view", "bp", "/api", Capability.CLUSTER),
    ("chronolog_observability.api.diagnostics", "bp", "/api", Capability.DIAGNOSTICS),
    ("chronolog_observability.api.fleet", "bp", "/api", Capability.FLEET),
    ("chronolog_observability.api.tracing", "bp", "/api", Capability.TRACING),
]


def create_app(config: Config | None = None) -> Flask:
    config = config or Config.from_env()
    app = Flask(__name__)
    app.config["OBSERVE"] = config

    registry.discover()
    log.info("active adapters: %s", [a.name for a in registry.active()])

    import importlib

    for module_path, attr, prefix, cap in _BLUEPRINTS:
        if not registry.has(cap):
            log.info("skip blueprint %s — no adapter provides %s", module_path, cap.value)
            continue
        try:
            mod = importlib.import_module(module_path)
            app.register_blueprint(getattr(mod, attr), url_prefix=prefix or None)
        except ModuleNotFoundError:
            # Blueprint not ported yet during migration — non-fatal.
            log.info("blueprint %s not present yet (migration)", module_path)

    @app.get("/api/config")
    def _config():
        return jsonify(
            {
                "adapters": [a.name for a in registry.active()],
                "capabilities": registry.capability_map(),
                "offline": config.offline,
            }
        )

    _maybe_start_capture(config)
    _maybe_start_doctor(config)
    _register_spa(app)
    return app


def _maybe_start_capture(config: Config) -> None:
    """Start the Path-A capture worker if opted in via ``CHRONOLOG_CAPTURE=1``.

    The dashboard is primarily a reader, so draining the local capture spool
    into ChronoLog is off by default. When enabled (and not offline), the worker
    runs as a daemon thread for the life of the process. It is a no-op without a
    live backend, so offline/demo mode never starts it.
    """
    import os

    flag = os.environ.get("CHRONOLOG_CAPTURE", os.environ.get("DTP_CHRONOLOG_CAPTURE", ""))
    if config.offline or flag.strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        from .capture.sync_worker import start_sync_worker

        interval = float(os.environ.get("CHRONOLOG_CAPTURE_INTERVAL", "5"))
        worker = start_sync_worker(interval_sec=interval)
        if worker is not None:
            log.info("path-a capture worker started (interval=%.1fs)", interval)
    except Exception as exc:  # never let capture wiring break the dashboard
        log.warning("path-a capture worker not started: %s", exc)


def _maybe_start_doctor(config: Config) -> None:
    """Start the ChronoDoctor background scanner if opted in via ``CHRONOLOG_DOCTOR=1``.

    ChronoDoctor's detection always runs on demand (the Doctor tab / API trigger
    a scan). The daemon is the *live* mode: it scans on a timer so incidents pop
    in without a click and the incident-memory digest accrues across the run. Off
    by default — like capture, the dashboard is a reader first.
    """
    import os

    flag = os.environ.get("CHRONOLOG_DOCTOR", "")
    if flag.strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        from .diagnostics.worker import start_doctor_worker

        interval = float(os.environ.get("CHRONOLOG_DOCTOR_INTERVAL", "30"))
        worker = start_doctor_worker(interval_sec=interval)
        if worker is not None:
            log.info("chronodoctor worker started (interval=%.1fs)", interval)
    except Exception as exc:  # never let diagnostics wiring break the dashboard
        log.warning("chronodoctor worker not started: %s", exc)


def _register_spa(app: Flask) -> None:
    """Serve the built React SPA at ``/``.

    Vite writes its bundle into ``static/workspace/`` with ``base=/static/workspace/``,
    so ``index.html`` already references hashed asset URLs that Flask's default
    static handler serves — we just hand back the entry HTML. No runtime dependency, no
    template indirection. If the SPA hasn't been built, return a 503 with a
    build hint rather than a confusing 404.
    """
    from pathlib import Path

    from flask import send_file

    workspace_index = Path(app.static_folder) / "workspace" / "index.html"

    @app.get("/")
    def index():
        if not workspace_index.is_file():
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>UI not built</title>"
                "<style>body{font-family:sans-serif;padding:40px;max-width:640px}"
                "code{background:#eee;padding:2px 6px;border-radius:4px}</style>"
                "<h1>Dashboard UI not built yet</h1>"
                "<p>The SPA bundle is missing from <code>static/workspace/</code>. "
                "Run <code>make workspace</code> (or "
                "<code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>) "
                "and reload.</p>"
            )
            return body, 503, {"Content-Type": "text/html; charset=utf-8"}
        return send_file(workspace_index)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = Config.from_env()
    app = create_app(cfg)
    app.run(host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
