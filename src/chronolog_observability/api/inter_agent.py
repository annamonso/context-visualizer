"""Flask blueprint: inter-agent message ingest + read API + live SSE stream.

The capture path is:
    MCP relay (or any wrapper) --POST--> /api/_inter-agent/ingest
                                            ├── start (at call initiation)
                                            └── done  (at call completion)

Per-node collectors relay batches with the ``X-DTP-Forwarded: chronolog``
header after writing the same events to ChronoLog themselves; the store then
skips its own ChronoLog write so each event is persisted exactly once.

Reads go through /api/scenarios/<sid>/inter-agent which returns stitched
events. /api/_inter-agent/stream is the live SSE feed behind the dashboard's
real-time view — ChronoLog's chunk acceptance window (~180 s keeper→grapher
drain) makes it unusable as a real-time read source, so ingested events are
fanned out to subscribers in-process at the moment of ingest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from flask import Blueprint, Response, request

from ..capture.inter_agent import InterAgentMessage, get_store
from ..capture.inter_agent.bus import get_bus
from ..capture.inter_agent.store import new_message

bp = Blueprint("inter_agent", __name__)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Ingest (write)
# ──────────────────────────────────────────────────────────────────────────


def _required(data: dict, key: str) -> Any:
    v = data.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"missing required field: {key}")
    return v


def _digest_payload(payload: Any) -> tuple[str, str]:
    """Return (sha256-digest, short-preview) for an arbitrary payload.

    Preview is truncated to 512 chars so we don't smuggle huge strings into
    the event store. Full payloads remain in the LLM InteractionRecord blobs.
    """
    if payload is None:
        return "", ""
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    preview = text[:512]
    return digest, preview


def _parse_event(data: dict, forwarded: bool = False) -> InterAgentMessage:
    """Validate one ingest body and build the message. Raises ValueError.

    ``forwarded=True`` (per-node collector relays) preserves the supplied
    ``event_id``/``ts``/``ingest_ns`` instead of regenerating them — the
    collector already stamped them before writing the same dict to
    ChronoLog, and the ids must match across both copies.
    """
    if not isinstance(data, dict):
        raise ValueError("event must be a JSON object")
    scenario_id = str(_required(data, "scenario_id")).strip()
    phase = str(_required(data, "phase")).strip()
    if phase not in ("start", "done"):
        raise ValueError("phase must be 'start' or 'done'")
    from_host = str(_required(data, "from_host")).strip()
    from_session = str(_required(data, "from_session")).strip()
    to_host = str(_required(data, "to_host")).strip()
    to_session = str(_required(data, "to_session")).strip()

    correlation_id = str(data.get("correlation_id") or uuid.uuid4().hex)
    kind = str(data.get("kind") or "mcp_call")
    tool_name = str(data.get("tool_name") or "")
    digest, preview = _digest_payload(data.get("payload"))
    if not digest and data.get("payload_digest"):
        # Forwarded events arrive pre-digested (the collector strips the raw
        # payload before relaying).
        digest = str(data["payload_digest"])
        preview = str(data.get("payload_preview") or "")

    status = str(data.get("status") or "") if phase == "done" else ""
    try:
        latency_ms = float(data.get("latency_ms") or 0.0) if phase == "done" else 0.0
    except (TypeError, ValueError):
        latency_ms = 0.0

    msg = new_message(
        scenario_id=scenario_id,
        from_host=from_host,
        from_session=from_session,
        to_host=to_host,
        to_session=to_session,
        phase=phase,
        correlation_id=correlation_id,
        kind=kind,
        tool_name=tool_name,
        payload_preview=preview,
        payload_digest=digest,
        status=status,
        latency_ms=latency_ms,
        ingest_ns=time.time_ns(),
    )
    if forwarded:
        if data.get("event_id"):
            msg.event_id = str(data["event_id"])
        if data.get("ts"):
            msg.ts = str(data["ts"])
        if data.get("ingest_ns"):
            msg.ingest_ns = int(data["ingest_ns"])
    return msg


def _publish(msg: InterAgentMessage) -> None:
    """Fan out to live SSE subscribers. Must never fail the ingest — the
    event is already durable in the store at this point."""
    try:
        get_bus().publish(msg.scenario_id, msg.to_dict())
    except Exception:
        log.warning("bus publish failed (event=%s)", msg.event_id, exc_info=True)


@bp.route("/_inter-agent/ingest", methods=["POST"])
def ingest():
    """Record one inter-agent event — or a batch.

    Single-event body (response unchanged for existing callers):
      {
        "scenario_id": "expt-1",                     # required
        "phase": "start" | "done",                   # required
        "correlation_id": "...",                     # optional (auto-generated if missing)
        "from_host": "ares-comp-11",                 # required
        "from_session": "planner-a",                 # required
        "to_host": "ares-comp-12",                   # required
        "to_session": "fetcher-b",                   # required
        "kind": "mcp_call",                          # optional (default)
        "tool_name": "call_remote_agent",            # optional
        "payload": {...} | "raw string",             # optional — hashed + truncated
        "status": "ok" | "error:<msg>",              # only on phase=done
        "latency_ms": 1234.0                         # only on phase=done
      }

    Batch bodies: a top-level array of such objects, or {"events": [...]}.
    Batch response: {"accepted": N, "results": [...], "errors": [{"index", "error"}]}
    with 200 when at least one event was accepted, 400 when none were.

    The ``X-DTP-Forwarded: chronolog`` header marks relays from a per-node
    collector that already wrote the events to ChronoLog: supplied ids are
    preserved and the store skips its own ChronoLog write so each event is
    persisted exactly once per backend.
    """
    data = request.get_json(silent=True)
    forwarded = request.headers.get("X-DTP-Forwarded") == "chronolog"

    # ── Batch shapes ──────────────────────────────────────────────────────
    if isinstance(data, list) or (isinstance(data, dict) and isinstance(data.get("events"), list)):
        items = data if isinstance(data, list) else data["events"]
        msgs: list[InterAgentMessage] = []
        errors: list[dict] = []
        for i, item in enumerate(items):
            try:
                msgs.append(_parse_event(item, forwarded=forwarded))
            except ValueError as exc:
                errors.append({"index": i, "error": str(exc)})
        if msgs:
            try:
                get_store().append_many(msgs, skip_chronolog=forwarded)
            except Exception as exc:
                return Response(
                    json.dumps({"error": f"ingest failed: {exc}"}),
                    status=500,
                    content_type="application/json",
                )
            for msg in msgs:
                _publish(msg)
        return Response(
            json.dumps({
                "accepted": len(msgs),
                "results": [
                    {"event_id": m.event_id, "correlation_id": m.correlation_id, "ts": m.ts}
                    for m in msgs
                ],
                "errors": errors,
            }),
            status=200 if msgs else 400,
            content_type="application/json",
        )

    # ── Single event (legacy shape) ───────────────────────────────────────
    try:
        msg = _parse_event(data or {}, forwarded=forwarded)
    except ValueError as exc:
        return Response(
            json.dumps({"error": str(exc)}),
            status=400,
            content_type="application/json",
        )
    try:
        get_store().append(msg, skip_chronolog=forwarded)
    except Exception as exc:
        return Response(
            json.dumps({"error": f"ingest failed: {exc}"}),
            status=500,
            content_type="application/json",
        )
    _publish(msg)

    return Response(
        json.dumps({
            "event_id": msg.event_id,
            "correlation_id": msg.correlation_id,
            "ts": msg.ts,
        }),
        content_type="application/json",
    )


# ──────────────────────────────────────────────────────────────────────────
# Live stream (SSE)
# ──────────────────────────────────────────────────────────────────────────


def _sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _backlog_default() -> bool:
    """Whether the stream should read the stored backlog before going live.

    Safe only when the store read cannot stall: a ChronoLog replay of a
    story still inside the keeper→grapher drain window blocks for minutes
    *holding the GIL* (py_chronolog_client does not release it), freezing
    the whole dashboard process — threads and timeouts cannot help. Live
    scenarios are by definition inside that window, so against a live
    backend the stream is live-only by default and backlog is opt-in
    (``?backlog=1``, for already-drained scenarios). Offline mode reads
    JSONL, which is instant, so the backlog defaults on.
    """
    from ..backend.client import get_backend

    try:
        return get_backend() is None
    except Exception:
        return False


def stream_events(scenario_id: str, heartbeat_sec: float = 15.0, with_backlog: bool = False):
    """Generator behind /stream — separated from the route for testability.

    Late-join protocol: subscribe FIRST, then read the backlog from the
    store. Any event published between those two steps lands in both the
    backlog and the queue, so deduping live frames against the backlog's
    event_ids is exhaustive — no time-window heuristics needed.
    """
    sub = get_bus().subscribe(scenario_id)
    try:
        backlog = []
        if with_backlog:
            try:
                backlog = get_store().read(scenario_id)
            except Exception as exc:
                log.warning("stream backlog read failed for %s: %s", scenario_id, exc)
        seen = set()
        for ev in backlog:
            seen.add(ev.event_id)
            yield _sse_frame("backlog", ev.to_dict())
        yield _sse_frame("catchup", {"scenario_id": scenario_id, "count": len(backlog)})

        while True:
            ev = sub.get(timeout=heartbeat_sec)
            if ev is None:
                # Comment frame keeps proxies/browsers from timing the
                # connection out and lets us notice a closed socket.
                yield ": keepalive\n\n"
                continue
            eid = ev.get("event_id")
            if eid in seen:
                continue
            yield _sse_frame("message", ev)
    finally:
        sub.close()


@bp.route("/_inter-agent/stream", methods=["GET"])
def stream():
    """SSE feed of inter-agent events for one scenario.

    Emits the stored backlog first (``event: backlog`` frames followed by one
    ``event: catchup``), then live events (``event: message``) as they are
    ingested. ``?scenario=<sid>`` is required; ``?heartbeat_sec=`` optional.
    ``?backlog=0|1`` overrides the backlog default (on offline, off against a
    live backend — see ``_backlog_default``).
    """
    scenario_id = (request.args.get("scenario") or "").strip()
    if not scenario_id:
        return Response(
            json.dumps({"error": "missing required query param: scenario"}),
            status=400,
            content_type="application/json",
        )
    try:
        heartbeat = float(request.args.get("heartbeat_sec", "15"))
    except ValueError:
        heartbeat = 15.0
    heartbeat = max(1.0, min(heartbeat, 60.0))
    backlog_arg = (request.args.get("backlog") or "").strip()
    with_backlog = backlog_arg in ("1", "true", "yes") if backlog_arg else _backlog_default()

    return Response(
        stream_events(scenario_id, heartbeat_sec=heartbeat, with_backlog=with_backlog),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ──────────────────────────────────────────────────────────────────────────
# Read
# ──────────────────────────────────────────────────────────────────────────


@bp.route("/scenarios/<scenario_id>/inter-agent", methods=["GET"])
def list_inter_agent(scenario_id):
    """Return the stitched inter-agent edges for a scenario."""
    try:
        events = get_store().read_stitched(scenario_id)
    except Exception as exc:
        return Response(
            json.dumps({"error": str(exc)}),
            status=500,
            content_type="application/json",
        )
    return Response(
        json.dumps({"scenario_id": scenario_id, "events": events}),
        content_type="application/json",
    )


@bp.route("/scenarios/<scenario_id>/inter-agent/raw", methods=["GET"])
def list_inter_agent_raw(scenario_id):
    """Return every start/done event as stored (no stitching) — debugging aid."""
    try:
        events = get_store().read(scenario_id)
    except Exception as exc:
        return Response(
            json.dumps({"error": str(exc)}),
            status=500,
            content_type="application/json",
        )
    return Response(
        json.dumps({
            "scenario_id": scenario_id,
            "events": [ev.to_dict() for ev in events],
        }),
        content_type="application/json",
    )
