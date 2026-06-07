"""Flask application factory — adapter-aware, no hard chimaera import.

Contrast with the old app.py, whose very first line was ``from . import
chimaera_client``. Here we discover adapters at startup and register only the
blueprints whose capabilities some active adapter can serve. On a bare ChronoLog
host the live-runtime blueprints simply don't mount; on a clio-core host they do.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify

from .adapters.base import Capability
from .adapters.registry import registry
from .config import Config

log = logging.getLogger(__name__)


# Maps each blueprint to the capability it needs. Generic ones are always served
# by ChronoLogAdapter; live ones only when chimaera is active.
#
# url_prefix mirrors the original app.py: conversations / interactions /
# llm_dispatch declare ABSOLUTE routes (``/api/...``, ``/_interceptor/...``) so
# they mount with NO prefix; every other blueprint declares bare paths and gets
# ``/api``.
_BLUEPRINTS = [
    # (import_path, attr, url_prefix, required_capability)
    ("chronolog_observability.api.conversations", "bp", "", Capability.CONVERSATIONS),
    ("chronolog_observability.api.interactions", "bp", "", Capability.INTERACTIONS),
    ("chronolog_observability.api.scenarios", "bp", "/api", Capability.SCENARIOS),
    ("chronolog_observability.api.provenance", "bp", "/api", Capability.PROVENANCE),
    ("chronolog_observability.api.semantic", "bp", "/api", Capability.SEMANTIC),
    ("chronolog_observability.api.inter_agent", "bp", "/api", Capability.INTER_AGENT),
    # Live-runtime (chimaera) — mount only when available:
    ("chronolog_observability.api.topology", "bp", "/api", Capability.TOPOLOGY),
    ("chronolog_observability.api.workers", "bp", "/api", Capability.WORKERS),
    ("chronolog_observability.api.pools", "bp", "/api", Capability.POOLS),
    ("chronolog_observability.api.node", "bp", "/api", Capability.NODE),
    ("chronolog_observability.api.system", "bp", "/api", Capability.SYSTEM),
    ("chronolog_observability.api.recovery", "bp", "/api", Capability.RECOVERY),
    ("chronolog_observability.api.checkpoints", "bp", "/api", Capability.CHECKPOINTS),
    # OVERHEAD reads chimaera's own instrumentation — live-runtime only.
    ("chronolog_observability.api.overhead", "bp", "/api", Capability.OVERHEAD),
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

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = Config.from_env()
    app = create_app(cfg)
    app.run(host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
