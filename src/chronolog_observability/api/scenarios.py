"""Flask blueprint: scenario listing + per-scenario peer-agent graph.

Reads from two sources:
  - ``inter_agent.InterAgentStore`` — the set of scenarios and their edges.
  - The existing proxy ``list_sessions`` + ``query_session`` pipeline for
    per-peer interaction aggregation.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from datetime import timedelta

from ..adapters.base import Capability
from ..adapters.registry import registry
from ..adapters.shape import (
    build_scenario_graph,
    build_scenario_summary,
    build_scenario_timeline,
    flatten_monitor_result,
    parse_iso,
    participants,
    strip_scope,
)
from ..capture.inter_agent import get_store
from ..capture.llm_demo_store import get_store as get_llm_store


bp = Blueprint("scenarios", __name__)


def _source():
    """Active data source for per-peer session reads (ChronoLog on a bare host)."""
    return registry.source_for(Capability.SCENARIOS)


# ─────────────────────────── data-source fetch helpers ───────────────────────────


def _fetch_all_sessions() -> list[dict]:
    try:
        raw = _source().get_sessions()
    except Exception:
        return []
    return flatten_monitor_result(raw)


def _fetch_session_bundle(session_id: str) -> dict:
    try:
        raw_i = _source().get_session_interactions(session_id)
    except Exception:
        raw_i = None
    try:
        raw_n = _source().get_context_graph(session_id)
    except Exception:
        raw_n = None
    return {
        "session_id": session_id,
        "interactions": flatten_monitor_result(raw_i),
        "context_nodes": flatten_monitor_result(raw_n),
    }


def _fetch_peer_subtrees(peer_roots: list[str]) -> list[dict]:
    """For each peer root, fetch its full orchestrator+subagent session subtree."""
    if not peer_roots:
        return []
    all_sessions = _fetch_all_sessions()
    known_ids = {
        s.get("session_id") for s in all_sessions
        if isinstance(s, dict) and isinstance(s.get("session_id"), str)
    }
    targets: set[str] = set()
    for root in peer_roots:
        targets.add(root)
        prefix = root + "."
        for sid in known_ids:
            if isinstance(sid, str) and sid.startswith(prefix):
                targets.add(sid)

    bundles: list[dict] = []
    for sid in sorted(targets):
        bundles.append(_fetch_session_bundle(sid))
    return bundles


# ─────────────────────────────── route handlers ───────────────────────────────


@bp.route("/scenarios", methods=["GET"])
def list_scenarios():
    """``GET /api/scenarios`` — summary per scenario seen in the inter-agent store."""
    store = get_store()
    ids = store.list_scenarios()
    out = []
    for sid in ids:
        events = store.read_stitched(sid)
        out.append(build_scenario_summary(sid, events))
    # Most-recently-active scenario first.
    out.sort(key=lambda s: s.get("lastEvent", ""), reverse=True)
    return jsonify(out)


@bp.route("/scenarios/<scenario_id>", methods=["GET"])
def get_scenario(scenario_id):
    """``GET /api/scenarios/<sid>`` — scenario summary (same shape as list entry)."""
    store = get_store()
    events = store.read_stitched(scenario_id)
    return jsonify(build_scenario_summary(scenario_id, events))


@bp.route("/scenarios/<scenario_id>/graph", methods=["GET"])
def get_scenario_graph(scenario_id):
    """``GET /api/scenarios/<sid>/graph`` — peer-agent graph + inter-agent edges."""
    store = get_store()
    events = store.read_stitched(scenario_id)
    peer_roots = participants(events)
    bundles = _fetch_peer_subtrees(peer_roots)
    graph = build_scenario_graph(scenario_id, bundles, events)
    return jsonify(graph)


@bp.route("/scenarios/<scenario_id>/timeline", methods=["GET"])
def get_scenario_timeline(scenario_id):
    """``GET /api/scenarios/<sid>/timeline`` — unified, time-ordered event stream
    merging cross-node calls with each agent's LLM turns and tool calls.

    LLM/tool events come from the per-agent ``LLMDemoStore`` and are scoped to
    this scenario by (a) agent session id, (b) host (when known), and (c) the
    scenario's active time window (padded), so we don't pull an agent's turns
    from other runs.
    """
    store = get_store()
    events = store.read_stitched(scenario_id)
    peer_roots = participants(events)
    bundles = _fetch_peer_subtrees(peer_roots)
    graph = build_scenario_graph(scenario_id, bundles, events)
    summary = build_scenario_summary(scenario_id, events)

    # Padded scenario window — LLM timestamps are second-resolution, so give a
    # generous margin around the sub-second inter-agent edges.
    pad = timedelta(seconds=30)
    win_start = parse_iso(summary.get("firstEvent"))
    win_end = parse_iso(summary.get("lastEvent"))
    if win_start is not None:
        win_start = win_start - pad
    if win_end is not None:
        win_end = win_end + pad

    # graph agents carry the display id (scenario suffix stripped); peer_roots
    # are the scoped ids used to read the per-session store. Label llm_by_agent
    # with the stripped id so it joins the (also-stripped) edge columns.
    host_by_agent = {a.get("agent_id", ""): a.get("host", "") for a in graph.get("agents", [])}
    llm_store = get_llm_store()
    llm_by_agent: list[tuple[str, str, list[dict]]] = []
    for scoped in peer_roots:
        bare = strip_scope(scoped, scenario_id)
        host = host_by_agent.get(bare, "")
        inters = llm_store.read_interactions(scoped)
        if host:
            # Same session id can recur on different hosts across runs; keep only
            # this run's host (tolerating records that never recorded a host).
            inters = [it for it in inters if not it.get("host") or it.get("host") == host]
        llm_by_agent.append((bare, host, inters))

    timeline = build_scenario_timeline(graph, llm_by_agent, win_start, win_end)
    return jsonify({
        "scenario_id": scenario_id,
        "agents": graph.get("agents", []),
        "events": timeline,
    })
