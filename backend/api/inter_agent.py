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
import threading
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
        parent_correlation_id=str(data.get("parent_correlation_id") or ""),
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
    from ..chronolog.client import get_backend

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


# ──────────────────────────────────────────────────────────────────────────
# Demo traffic generator — synthetic, NO LLM cost (backs the dashboard button)
# ──────────────────────────────────────────────────────────────────────────

_DEMO_ROLES = ["planner", "executor", "fetcher", "summarizer",
               "retriever", "analyst", "validator", "coordinator"]
_DEMO_TOOLS = ["call_remote_agent", "shard_query", "merge_results", "fetch_chunk", "broadcast_plan"]
_DEMO_MODELS = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
_DEMO_THOUGHTS = ["Selecting which peer to delegate to.", "Merging shard results.",
                  "Estimating the token budget.", "Checking the retrieved chunk.",
                  "Drafting the downstream prompt."]


def _demo_role(n: int) -> str:
    base = _DEMO_ROLES[n % len(_DEMO_ROLES)]
    rep = n // len(_DEMO_ROLES)
    return base if rep == 0 else f"{base}{rep + 1}"


def _demo_hosts(n_nodes: int = 0) -> list:
    """REAL hosts to spread the burst across — never invented names.

    Preference order: the live SLURM allocation, then the cluster's currently
    idle nodes (from ``sinfo``). ``n_nodes`` caps how many are used (the UI
    already caps it to the idle count). Only if there is no SLURM client at all
    do we fall back to a labelled placeholder set, so demos still run offline.
    """
    pool: list = []
    try:
        from .chronolog_view import _allocation_nodes, idle_node_names
        pool = _allocation_nodes() or idle_node_names()
    except Exception:
        pool = []
    if pool:
        return pool[:n_nodes] if n_nodes and n_nodes > 0 else pool
    # No SLURM client (pure offline demo): use clearly-synthetic placeholders.
    count = n_nodes if n_nodes and n_nodes > 0 else 4
    return [f"demo-node-{i:02d}" for i in range(count)]


def _demo_pair(agents, pattern, r, rng):
    """Pick (from, to) agents for round r given a topology pattern."""
    n = len(agents)
    if pattern == "star":
        fa = agents[0]
        ta = rng.choice([a for a in agents if a is not fa])
    elif pattern == "pipeline":
        i = r % max(1, n - 1)
        fa, ta = agents[i], agents[i + 1]
    elif pattern == "ring":
        i = r % n
        fa, ta = agents[i], agents[(i + 1) % n]
    else:  # mesh — 35% coordinator-out, else random peer-to-peer
        fa = agents[0] if rng.random() < 0.35 else rng.choice(agents)
        ta = rng.choice([a for a in agents if a is not fa])
    return fa, ta


def _run_demo_burst(scenario: str, n_agents: int, rounds: int, interval: float,
                    with_spool: bool, err_rate: float, pattern: str,
                    n_nodes: int = 0) -> None:
    import random
    from datetime import datetime, timezone

    rng = random.Random()
    hosts = _demo_hosts(n_nodes)
    agents = [{"sid": f"{_demo_role(n)}@{scenario}", "role": _demo_role(n), "host": hosts[n % len(hosts)]}
              for n in range(n_agents)]
    store = get_store()
    spool = None
    if with_spool:
        try:
            from ..capture.spool import get_spool
            spool = get_spool()
        except Exception:
            spool = None
    seq = {a["sid"]: 0 for a in agents}
    # Track the call each agent is currently servicing, so when it makes an
    # outbound call we can stamp parent_correlation_id and the tracer builds a
    # real multi-hop call tree (otherwise every span is a flat sibling and the
    # Traces view looks "all the same").
    servicing: dict = {}

    def _emit(msg):
        try:
            store.append(msg)
            _publish(msg)
        except Exception:
            log.warning("demo burst emit failed", exc_info=True)

    for r in range(rounds):
        fa, ta = _demo_pair(agents, pattern, r, rng)
        if fa is ta:
            continue
        cid = uuid.uuid4().hex
        tool = rng.choice(_DEMO_TOOLS)
        parent = servicing.get(fa["sid"], "")
        _emit(new_message(
            scenario_id=scenario, from_host=fa["host"], from_session=fa["sid"],
            to_host=ta["host"], to_session=ta["sid"], phase="start",
            correlation_id=cid, kind="mcp_call", tool_name=tool,
            parent_correlation_id=parent, ingest_ns=time.time_ns()))
        # The callee is now servicing this call — its next outbound call nests
        # under it. Cleared after the call completes below.
        servicing[ta["sid"]] = cid
        time.sleep(min(0.4, interval / 3))
        err = rng.random() < err_rate
        _emit(new_message(
            scenario_id=scenario, from_host=fa["host"], from_session=fa["sid"],
            to_host=ta["host"], to_session=ta["sid"], phase="done",
            correlation_id=cid, kind="mcp_call", tool_name=tool,
            status=("error:peer timed out" if err else "ok"),
            parent_correlation_id=parent,
            latency_ms=round(rng.uniform(80, 1900), 1), ingest_ns=time.time_ns()))
        servicing.pop(ta["sid"], None)
        if spool is not None:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for a, peer in ((fa, ta), (ta, fa)):
                seq[a["sid"]] += 1
                s = seq[a["sid"]]
                in_tok = rng.randint(400, 4000)
                out_tok = rng.randint(40, 600)
                thought = rng.choice(_DEMO_THOUGHTS)
                failed = err and a is ta
                # Real-agent record shape so the convo renders fully in the
                # Workspace + Interactions detail (request body, response text,
                # token metrics) — not just an empty skeleton.
                try:
                    spool.record_interaction(a["sid"], {
                        "session_id": a["sid"], "sequence_id": s, "scenario_id": scenario,
                        "host": a["host"], "timestamp": ts, "provider": "anthropic",
                        "model": rng.choice(_DEMO_MODELS),
                        "request": {"method": "POST", "path": "/v1/messages",
                                    "body": {"system": f"You are the {a['role']} agent on {a['host']}.",
                                             "messages": [{"role": "user",
                                                           "content": f"[turn {s}] {thought} "
                                                                      f"(peer: {peer['role']}, tool: {tool})"}]}},
                        "response": {"status_code": 500 if failed else 200,
                                     "text": ("peer timed out" if failed else f"{a['role']}: {thought}"),
                                     "is_streaming": False},
                        "metrics": {"total_latency_ms": round(rng.uniform(200, 2400)),
                                    "delta_input_tokens": in_tok, "delta_output_tokens": out_tok,
                                    "delta_cost_usd": round(in_tok * 3e-6 + out_tok * 1.5e-5, 6)},
                    }, sequence_id=s)
                    spool.record_context_node(a["sid"], {
                        "op": rng.choice(["add", "add", "add", "evict"]), "node": f"ctx-{s}",
                        "summary": f"{a['role']}: {thought}", "timestamp": ts,
                        "tokens": rng.randint(50, 600),
                        "delta_input_tokens": in_tok, "delta_output_tokens": out_tok}, sequence_id=s)
                except Exception:
                    pass
        time.sleep(interval)
    log.info("demo burst done: scenario=%s agents=%d rounds=%d pattern=%s", scenario, n_agents, rounds, pattern)


@bp.route("/_demo/burst", methods=["POST"])
def demo_burst():
    """Fire a synthetic inter-agent burst (NO LLM cost) so the live views light
    up on demand — backs the dashboard's "Generate demo traffic" button. Runs in
    a background thread; edges stream to the SSE bus + hot store, and the convo
    (LLM turns + context) is spooled so it lands in Workspace/Interactions/Memory.

    Body (all optional): {scenario, agents, nodes, rounds, interval, err_rate, pattern, spool}.
    ``pattern`` is one of mesh | star | pipeline | ring. ``nodes`` spreads the
    agents across that many synthetic hosts (0 = use the real allocation).
    """
    data = request.get_json(silent=True) or {}
    scenario = (str(data.get("scenario") or "demo-live").strip() or "demo-live")
    pattern = str(data.get("pattern") or "mesh").strip().lower()
    if pattern not in ("mesh", "star", "pipeline", "ring"):
        pattern = "mesh"
    try:
        n_agents = max(2, min(int(data.get("agents") or 8), 200))
        n_nodes = max(0, min(int(data.get("nodes") or 0), 200))
        rounds = max(1, min(int(data.get("rounds") or 30), 1000))
        interval = max(0.0, min(float(data.get("interval") or 1.0), 5.0))
        raw_err = data.get("err_rate")
        err_rate = 0.12 if raw_err is None else max(0.0, min(float(raw_err), 1.0))
    except (TypeError, ValueError):
        return Response(json.dumps({"error": "agents/nodes/rounds/interval/err_rate must be numeric"}),
                        status=400, content_type="application/json")
    with_spool = bool(data.get("spool", True))
    threading.Thread(
        target=_run_demo_burst,
        args=(scenario, n_agents, rounds, interval, with_spool, err_rate, pattern, n_nodes),
        name="demo-burst", daemon=True,
    ).start()
    return Response(
        json.dumps({"started": True, "scenario": scenario, "agents": n_agents, "nodes": n_nodes,
                    "rounds": rounds, "interval": interval, "pattern": pattern, "err_rate": err_rate}),
        content_type="application/json",
    )
