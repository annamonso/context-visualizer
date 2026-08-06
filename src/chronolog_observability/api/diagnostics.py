"""Flask blueprint: PrismaDoctor — error detection & diagnosis API.

Mounted under ``/api`` (see ``app._BLUEPRINTS``). Detection runs on demand: the
first read scans if no report is cached, ``POST /diagnostics/scan`` forces a
fresh scan, and the SSE stream pushes the latest report to the Doctor tab.

Routes:
    GET  /api/diagnostics/incidents            ranked incidents + totals
    POST /api/diagnostics/scan                 force a fresh scan
    GET  /api/diagnostics/incidents/<sig>      one incident (full diagnosis)
    GET  /api/diagnostics/memory               incident-memory markdown
    GET  /api/diagnostics/stream               SSE: report snapshots
"""

from __future__ import annotations

import json
import logging
import time

from flask import Blueprint, Response, request

from ..diagnostics import scan as _scan

log = logging.getLogger(__name__)

bp = Blueprint("diagnostics", __name__)


def _json(payload, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, content_type="application/json")


@bp.route("/diagnostics/incidents", methods=["GET"])
def incidents():
    """Latest ranked incidents. Scans once if nothing is cached yet."""
    rep = _scan.latest(refresh_if_empty=True)
    return _json(rep.to_dict())


@bp.route("/diagnostics/status", methods=["GET"])
def status():
    """Whether the live background scanner is running, plus the latest totals."""
    from ..diagnostics import worker

    rep = _scan.latest(refresh_if_empty=False)
    return _json({
        "running": worker.is_running(),
        "generated_ns": rep.generated_ns if rep else 0,
        "totals": rep.totals if rep else {},
    })


@bp.route("/diagnostics/watch", methods=["POST"])
def watch():
    """Activate / deactivate PrismaDoctor's live background scanner.

    Body: {"enabled": true|false, "interval"?: seconds}. Activating runs an
    immediate scan so the tab fills at once, then keeps scanning on the timer.
    """
    from ..diagnostics import worker

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    if enabled:
        try:
            interval = float(data.get("interval", 30) or 30)
        except (TypeError, ValueError):
            interval = 30.0
        worker.start_doctor_worker(interval_sec=max(5.0, min(interval, 600.0)))
        _scan.run_scan()  # immediate first result
    else:
        worker.stop_doctor_worker()
    return _json({"running": worker.is_running()})


@bp.route("/diagnostics/scan", methods=["POST"])
def scan():
    """Force a fresh scan. Body (optional): {diagnose, remember, max_sessions, max_scenarios}."""
    data = request.get_json(silent=True) or {}
    rep = _scan.run_scan(
        diagnose=bool(data.get("diagnose", True)),
        remember=bool(data.get("remember", True)),
        max_sessions=int(data.get("max_sessions", 400) or 400),
        max_scenarios=int(data.get("max_scenarios", 200) or 200),
    )
    return _json(rep.to_dict())


@bp.route("/diagnostics/incidents/<sig>", methods=["GET"])
def incident(sig: str):
    rep = _scan.latest(refresh_if_empty=True)
    for inc in rep.incidents:
        if inc.get("signature") == sig:
            return _json(inc)
    return _json({"error": f"no incident with signature {sig}"}, status=404)


@bp.route("/diagnostics/memory", methods=["GET"])
def memory():
    """The self-compacting incident-memory document (markdown)."""
    text = _scan.memory_document()
    if request.args.get("format") == "json":
        return _json({"markdown": text})
    return Response(text, content_type="text/markdown; charset=utf-8")


@bp.route("/diagnostics/stream", methods=["GET"])
def stream():
    """SSE: push the latest report whenever it changes (and on a heartbeat)."""
    try:
        interval = float(request.args.get("interval", "5"))
    except ValueError:
        interval = 5.0
    interval = max(1.0, min(interval, 30.0))

    def _gen():
        last_ns = 0
        # Emit the current snapshot immediately so the tab paints without waiting.
        rep = _scan.latest(refresh_if_empty=True)
        last_ns = rep.generated_ns
        yield f"event: report\ndata: {json.dumps(rep.to_dict())}\n\n"
        while True:
            time.sleep(interval)
            rep = _scan.latest(refresh_if_empty=False)
            if rep and rep.generated_ns != last_ns:
                last_ns = rep.generated_ns
                yield f"event: report\ndata: {json.dumps(rep.to_dict())}\n\n"
            else:
                yield ": keepalive\n\n"

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
