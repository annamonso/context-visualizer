"""Flask blueprint: Fleet Health — per-node rollups for 100+ node deployments.

Mounted under ``/api``. Serves the heatmap/treemap view. Reads pre-aggregated
rollup buckets when the emitter is running; otherwise computes node health from
raw records on demand so the view is never empty.

Routes:
    GET /api/fleet/health[?window=5m]    per-node health (heatmap rows)
    GET /api/fleet/node/<host>           one node's bucket time series + health
    GET /api/fleet/stream[?interval=]    SSE: periodic health snapshots
"""

from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

from flask import Blueprint, Response, request

from ..fleet import rollup as _rollup
from ..fleet.store import read_buckets

log = logging.getLogger(__name__)

bp = Blueprint("fleet", __name__)


def _json(payload, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, content_type="application/json")


def _parse_window(s: Optional[str]) -> int:
    """'5m' / '90s' / '2h' -> seconds. 0 (or unset) means 'all time'."""
    if not s:
        return 0
    s = s.strip().lower()
    try:
        if s.endswith("ms"):
            return max(0, int(float(s[:-2]) / 1000))
        if s.endswith("s"):
            return int(float(s[:-1]))
        if s.endswith("m"):
            return int(float(s[:-1]) * 60)
        if s.endswith("h"):
            return int(float(s[:-1]) * 3600)
        return int(float(s))
    except ValueError:
        return 0


def _allocation() -> List[str]:
    try:
        from .chronolog_view import _allocation_nodes

        return _allocation_nodes() or []
    except Exception:
        return []


def _buckets() -> List[_rollup.Bucket]:
    """Pre-aggregated buckets if present, else compute from raw on demand."""
    buckets = read_buckets()
    if buckets:
        return buckets
    # Fallback: derive buckets from raw so the view works without the emitter.
    try:
        from ..diagnostics.gather import gather

        interactions, edges, _ = gather()
        return _rollup.bucketize(interactions, edges)
    except Exception as exc:
        log.warning("fleet raw fallback failed: %s", exc)
        return []


def _health(window_sec: int):
    buckets = _buckets()
    alloc = _allocation()
    health = _rollup.health_from_buckets(
        buckets, now_ns=time.time_ns(), window_sec=window_sec, allocation=alloc
    )
    # ``_allocation()`` returns canonicalised (digit-unpadded) names
    # (``ares-comp-8``) while bucket hosts keep whatever form the data used
    # (usually SLURM-padded ``ares-comp-08``). Compare on the canonical form so
    # a padded bucket node and its allocation entry are recognised as the SAME
    # physical node — otherwise ``ares-comp-08`` (from data) and ``ares-comp-8``
    # (from the allocation) both render as separate cards, and the deploy nodes
    # are wrongly flagged out-of-allocation.
    from .chronolog_view import _short_host

    def canon(h: str) -> str:
        return _short_host(h) or h

    alloc_canon = {canon(a) for a in alloc}
    present = set()
    for h in health.values():
        h.in_allocation = canon(h.host) in alloc_canon
        present.add(canon(h.host))
    # Allocation nodes that produced no events still belong in the grid (idle/
    # silent nodes are themselves a signal at scale).
    for host in alloc:
        if canon(host) not in present:
            health[host] = _rollup.NodeHealth(host=host, in_allocation=True)
            present.add(canon(host))
    return list(health.values()), alloc


@bp.route("/fleet/health", methods=["GET"])
def fleet_health():
    window_sec = _parse_window(request.args.get("window"))
    nodes, alloc = _health(window_sec)
    rows = [n.to_dict() for n in nodes]
    rows.sort(key=lambda r: (r["status"] != "crit", r["status"] != "warn", -r["error_rate"], -r["p95_latency_ms"]))
    summary = {
        "nodes": len(rows),
        "crit": sum(1 for r in rows if r["status"] == "crit"),
        "warn": sum(1 for r in rows if r["status"] == "warn"),
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "events": sum(r["events"] for r in rows),
        "errors": sum(r["errors"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "total_tokens": sum(r["total_tokens"] for r in rows),
    }
    return _json({
        "window_sec": window_sec,
        "now_ns": time.time_ns(),
        "allocation": alloc,
        "summary": summary,
        "nodes": rows,
    })


@bp.route("/fleet/node/<host>", methods=["GET"])
def fleet_node(host: str):
    window_sec = _parse_window(request.args.get("window"))
    buckets = [b for b in _buckets() if b.host == host]
    cutoff = 0
    if window_sec:
        cutoff = (time.time_ns() // 1_000_000_000) - window_sec
    series = [b.to_dict() for b in buckets if not cutoff or b.minute >= cutoff]
    health = _rollup.health_from_buckets(buckets, now_ns=time.time_ns(), window_sec=window_sec)
    h = health.get(host)
    return _json({
        "host": host,
        "health": h.to_dict() if h else _rollup.NodeHealth(host=host).to_dict(),
        "series": series,
    })


@bp.route("/fleet/stream", methods=["GET"])
def fleet_stream():
    try:
        interval = float(request.args.get("interval", "5"))
    except ValueError:
        interval = 5.0
    interval = max(2.0, min(interval, 30.0))
    window_sec = _parse_window(request.args.get("window"))

    def _gen():
        while True:
            nodes, alloc = _health(window_sec)
            rows = [n.to_dict() for n in nodes]
            payload = {"now_ns": time.time_ns(), "allocation": alloc, "nodes": rows}
            yield f"event: health\ndata: {json.dumps(payload)}\n\n"
            time.sleep(interval)

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
