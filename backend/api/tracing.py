"""Flask blueprint: distributed critical-path tracing.

Mounted under ``/api``. Reconstructs cross-host call trees from the inter-agent
edges of a scenario and exposes the waterfall + the critical path.

Routes:
    GET /api/tracing/scenarios               scenarios that have edges
    GET /api/tracing/<scenario>/traces       trace trees (waterfall spans)
    GET /api/tracing/<scenario>/critical-path  the single worst critical path
"""

from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, request

from ..analysis import trace as _trace

log = logging.getLogger(__name__)

bp = Blueprint("tracing", __name__)


def _json(payload, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, content_type="application/json")


def _stitched(scenario: str):
    from ..capture.inter_agent import get_store

    return get_store().read_stitched(scenario) or []


@bp.route("/tracing/scenarios", methods=["GET"])
def scenarios():
    from ..capture.inter_agent import get_store

    return _json({"scenarios": get_store().list_scenarios() or []})


@bp.route("/tracing/<scenario>/traces", methods=["GET"])
def traces(scenario: str):
    stitched = _stitched(scenario)
    out = _trace.build_traces(stitched)
    for t in out:
        t["scenario_id"] = scenario
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    return _json({
        "scenario_id": scenario,
        "trace_count": len(out),
        "traces": out[: max(1, limit)],
    })


@bp.route("/tracing/<scenario>/critical-path", methods=["GET"])
def critical_path(scenario: str):
    stitched = _stitched(scenario)
    cp = _trace.critical_path(stitched)
    cp["scenario_id"] = scenario
    return _json(cp)
