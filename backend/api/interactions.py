"""Flask blueprint: recent-interactions feed for the new Interactions tab.

The reference project (JaimeCernuda/agent-interception @ ed04e49) exposes
``/_interceptor/interactions`` returning a flat list of ``InteractionSummary``
rows across every session, most-recent first. The React ``useLiveInteractions``
hook polls this every 5 s and, when SSE is available, subscribes to
``/_interceptor/live`` for push updates (it degrades back to polling on its
own when the stream drops).

This blueprint stitches the same shape on top of the active data-source
adapter. ``/_interceptor/live`` is a server-side poll over the same cached
feed — interactions arrive via the Path-A capture spool from other processes,
so there is no in-process ingest moment to hook a bus onto (unlike the
inter-agent stream).
"""

from __future__ import annotations

import json
import time
from threading import Lock

from flask import Blueprint, Response, request

from ..adapters.base import Capability
from ..adapters.registry import registry
from ..adapters.shape import flatten_monitor_result


bp = Blueprint("interactions", __name__)


def _source():
    """Active data source for interaction reads (ChronoLog on a bare host)."""
    return registry.source_for(Capability.INTERACTIONS)

# Cheap cache: computing the flat feed is O(sessions * avg_interactions) and
# we don't want a 5 s poll from every open tab hammering the backend. Bounded
# by wall-clock staleness, not by count — the underlying list changes by at
# most one row per forward, so a 1 s TTL is plenty live.
_CACHE_TTL_SECONDS = 1.0
_cache_lock = Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}


def _interaction_summary(session_id: str, record: dict) -> dict | None:
    """Flatten a raw tracker record into the InteractionSummary shape.

    Returns ``None`` when the record can't be coerced (skip it rather than
    crash the whole feed).
    """
    if not isinstance(record, dict):
        return None
    try:
        seq = int(record.get("sequence_id", 0))
    except (TypeError, ValueError):
        return None

    req = record.get("request") or {}
    resp = record.get("response") or {}

    response_text = resp.get("text") if isinstance(resp.get("text"), str) else None
    preview = None
    if response_text:
        preview = response_text[:200] + ("…" if len(response_text) > 200 else "")

    return {
        "id": f"{session_id}:{seq}",
        "session_id": session_id,
        "timestamp": record.get("timestamp") or "",
        "provider": record.get("provider") or "unknown",
        "model": record.get("model"),
        "method": req.get("method", "POST"),
        "path": req.get("path", "/"),
        "status_code": resp.get("status_code"),
        "is_streaming": bool(resp.get("is_streaming", False)),
        "total_latency_ms": (record.get("metrics") or {}).get("total_latency_ms"),
        "response_text_preview": preview,
    }


def _compute_feed(
    limit: int,
    provider_filter: str | None,
    model_filter: str | None,
    session_filter: str | None,
) -> list[dict]:
    """Build the interaction feed by fanning out over known sessions."""
    # 1. Enumerate sessions via the existing proxy handler.
    try:
        raw_sessions = _source().get_sessions()
    except Exception:
        return []

    session_ids: list[str] = []
    for _cid, payload in raw_sessions.items():
        if isinstance(payload, list):
            for s in payload:
                if isinstance(s, dict):
                    sid = s.get("session_id")
                    if isinstance(sid, str) and sid:
                        session_ids.append(sid)
        elif isinstance(payload, dict):
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid:
                session_ids.append(sid)

    if session_filter:
        session_ids = [s for s in session_ids if s == session_filter]

    # 2. For each session, pull its interactions. Cap per-session fetches to
    # keep latency bounded on very chatty sessions; we only need the most
    # recent ``limit`` rows globally.
    rows: list[dict] = []
    for sid in session_ids:
        try:
            raw = _source().get_session_interactions(sid)
        except Exception:
            continue
        for record in flatten_monitor_result(raw):
            summary = _interaction_summary(sid, record)
            if summary is None:
                continue
            if provider_filter and summary["provider"] != provider_filter:
                continue
            if model_filter and summary["model"] != model_filter:
                continue
            rows.append(summary)

    # 3. Sort most-recent first; ties broken by session + sequence so the
    # order stays stable across polls.
    rows.sort(
        key=lambda r: (r.get("timestamp") or "", r.get("session_id") or "", r.get("id") or ""),
        reverse=True,
    )
    return rows[:limit]


def _feed_params() -> tuple[int, str | None, str | None, str | None]:
    """Shared query-param parsing for the list and live routes."""
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    return (
        limit,
        request.args.get("provider") or None,
        request.args.get("model") or None,
        request.args.get("session_id") or None,
    )


def _cached_feed(
    limit: int,
    provider_filter: str | None,
    model_filter: str | None,
    session_filter: str | None,
) -> list[dict]:
    """The feed with the staleness-bounded cache in front of it."""
    cache_key = f"{limit}|{provider_filter or ''}|{model_filter or ''}|{session_filter or ''}"
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]

    rows = _compute_feed(limit, provider_filter, model_filter, session_filter)

    with _cache_lock:
        _cache[cache_key] = (now, rows)
    return rows


@bp.route("/_interceptor/interactions", methods=["GET"])
def list_interactions():
    """Reference-compatible ``InteractionSummary[]`` over the last N records."""
    limit, provider_filter, model_filter, session_filter = _feed_params()
    rows = _cached_feed(limit, provider_filter, model_filter, session_filter)
    return Response(json.dumps(rows), content_type="application/json")


def stream_interactions(
    limit: int,
    provider_filter: str | None,
    model_filter: str | None,
    session_filter: str | None,
    poll_sec: float = 2.0,
    keepalive_sec: float = 15.0,
):
    """Generator behind ``/_interceptor/live``.

    Server-side polls the cached feed and pushes only unseen rows as
    ``event: interaction`` frames; a comment frame goes out after
    ``keepalive_sec`` of silence so proxies don't reap the connection.
    """
    known = {r["id"] for r in _cached_feed(limit, provider_filter, model_filter, session_filter)}
    idle = 0.0
    while True:
        time.sleep(poll_sec)
        rows = _cached_feed(limit, provider_filter, model_filter, session_filter)
        fresh = [r for r in rows if r["id"] not in known]
        if fresh:
            idle = 0.0
            # Feed is most-recent-first; emit oldest-first so the hook's
            # prepend keeps chronological order.
            for row in reversed(fresh):
                known.add(row["id"])
                yield f"event: interaction\ndata: {json.dumps(row)}\n\n"
            # Bound the dedup set on very long-lived connections.
            if len(known) > 50_000:
                known = {r["id"] for r in rows}
        else:
            idle += poll_sec
            if idle >= keepalive_sec:
                idle = 0.0
                yield ": keepalive\n\n"


@bp.route("/_interceptor/live", methods=["GET"])
def live():
    """SSE push feed of new interactions (``event: interaction`` frames)."""
    limit, provider_filter, model_filter, session_filter = _feed_params()
    return Response(
        stream_interactions(limit, provider_filter, model_filter, session_filter),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
