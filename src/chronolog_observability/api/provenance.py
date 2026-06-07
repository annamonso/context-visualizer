"""Provenance API endpoints with SSE for live context graph updates."""

import json
import time

from flask import Blueprint, Response, jsonify, request

from ..adapters.base import Capability
from ..adapters.registry import registry
from ..analysis.context_graph import compute_analysis
from ..analysis.call_graph import (
    compute_call_graph,
    compute_tool_sequence,
    compute_workflow_graph,
    compute_workflow_sequence,
)

bp = Blueprint("provenance", __name__)


def _source():
    """Active data source for provenance reads (ChronoLog on a bare host)."""
    return registry.source_for(Capability.PROVENANCE)


def _flatten_results(raw):
    """Extract data from container-keyed monitor results."""
    for _cid, data in raw.items():
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                return json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return data
    return []


def _flatten_and_parse(raw):
    """Flatten monitor results and parse any JSON-string items."""
    items = _flatten_results(raw)
    if not isinstance(items, list):
        items = [items] if items else []
    parsed = []
    for item in items:
        if isinstance(item, str):
            try:
                parsed.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                parsed.append(item)
        else:
            parsed.append(item)
    return parsed


@bp.route("/sessions")
def list_sessions():
    """List all tracked agent sessions."""
    try:
        raw = _source().get_sessions()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

    sessions = _flatten_results(raw)
    return jsonify({"sessions": sessions})


@bp.route("/session/<session_id>/interactions")
def get_session_interactions(session_id):
    """Get all interaction records for a session."""
    try:
        raw = _source().get_session_interactions(session_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

    interactions = _flatten_results(raw)
    # Parse JSON strings if needed
    parsed = []
    for item in interactions:
        if isinstance(item, str):
            try:
                parsed.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                parsed.append(item)
        else:
            parsed.append(item)

    return jsonify({"interactions": parsed})


@bp.route("/session/<session_id>/graph")
def get_context_graph(session_id):
    """Get all context graph diff nodes for a session."""
    since = request.args.get("since", 0, type=int)
    try:
        raw = _source().get_context_graph(session_id, since=since)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

    nodes = _flatten_results(raw)
    parsed = []
    for item in nodes:
        if isinstance(item, str):
            try:
                parsed.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                parsed.append(item)
        else:
            parsed.append(item)

    return jsonify({"nodes": parsed})


@bp.route("/session/<session_id>/graph/<int:seq_id>")
def get_context_node(session_id, seq_id):
    """Get a single context graph diff node."""
    try:
        raw = _source().get_context_node(session_id, seq_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

    node = _flatten_results(raw)
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except (json.JSONDecodeError, TypeError):
            pass

    return jsonify({"node": node})


@bp.route("/session/<session_id>/stream")
def stream_context_graph(session_id):
    """SSE endpoint for live context graph updates."""
    def event_stream():
        last_seq = 0
        while True:
            try:
                raw = _source().get_context_graph(
                    session_id, since=last_seq)
                nodes = _flatten_results(raw)
                for item in nodes:
                    if isinstance(item, str):
                        try:
                            node = json.loads(item)
                        except (json.JSONDecodeError, TypeError):
                            continue
                    else:
                        node = item

                    yield f"data: {json.dumps(node)}\n\n"
                    seq = node.get("sequence_id", 0)
                    if seq > last_seq:
                        last_seq = seq
            except Exception:
                pass
            time.sleep(1)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@bp.route("/session/<session_id>/analysis")
def get_session_analysis(session_id):
    """Compute rich analysis data for all visualization charts.

    Accepts an optional ?include=id1,id2,... query param to merge additional
    sessions (e.g. child/subagent sessions) into the analysis.
    """
    extra = [s for s in request.args.get("include", "").split(",") if s]
    all_ids = [session_id] + extra

    all_interactions = []
    all_nodes = []
    for sid in all_ids:
        try:
            raw_i = _source().get_session_interactions(sid)
            raw_n = _source().get_context_graph(sid)
        except Exception as exc:
            if sid == session_id:
                return jsonify({"error": str(exc)}), 503
            continue
        all_interactions.extend(_flatten_and_parse(raw_i))
        all_nodes.extend(_flatten_and_parse(raw_n))

    return jsonify(compute_analysis(all_interactions, all_nodes))


def _fetch_child_sessions(parent_id: str) -> list[dict]:
    """Fetch interactions and context nodes for all child sessions of parent_id.

    Returns a list of {session_id, interactions, context_nodes, is_subagent} dicts.
    Child sessions are identified by the naming convention parent_id.N (dot notation).
    """
    try:
        raw_sessions = _source().get_sessions()
        all_sessions = _flatten_results(raw_sessions) or []
        if not isinstance(all_sessions, list):
            all_sessions = []
    except Exception:
        return []

    prefix = parent_id + "."
    child_ids = [
        s.get("session_id") for s in all_sessions
        if isinstance(s, dict) and s.get("session_id", "").startswith(prefix)
    ]

    child_data = []
    for cid in child_ids:
        try:
            raw_i = _source().get_session_interactions(cid)
            raw_n = _source().get_context_graph(cid)
            child_data.append({
                "session_id": cid,
                "interactions": _flatten_and_parse(raw_i),
                "context_nodes": _flatten_and_parse(raw_n),
                "is_subagent": True,
            })
        except Exception:
            pass
    return child_data


@bp.route("/session/<session_id>/call-graph")
def get_call_graph(session_id):
    """Compute call-graph topology for a session.

    Optional query param:
      scope=workflow  — include all child (subagent) sessions in the graph
    """
    try:
        raw_i = _source().get_session_interactions(session_id)
        raw_n = _source().get_context_graph(session_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

    interactions = _flatten_and_parse(raw_i)
    nodes = _flatten_and_parse(raw_n)

    scope = request.args.get("scope", "session")
    if scope == "workflow":
        sessions = [
            {"session_id": session_id, "interactions": interactions, "context_nodes": nodes, "is_subagent": False},
        ] + _fetch_child_sessions(session_id)
        return jsonify(compute_workflow_graph(sessions))

    return jsonify(compute_call_graph(interactions, nodes))


@bp.route("/session/<session_id>/tool-sequence")
def get_tool_sequence(session_id):
    """Compute sequential tool-call view for a session.

    Optional query param:
      scope=workflow  — include all child (subagent) sessions, interleaved by timestamp
    """
    try:
        raw_i = _source().get_session_interactions(session_id)
        raw_n = _source().get_context_graph(session_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503

    interactions = _flatten_and_parse(raw_i)
    nodes = _flatten_and_parse(raw_n)

    scope = request.args.get("scope", "session")
    if scope == "workflow":
        sessions = [
            {"session_id": session_id, "interactions": interactions, "context_nodes": nodes, "is_subagent": False},
        ] + _fetch_child_sessions(session_id)
        return jsonify({"steps": compute_workflow_sequence(sessions)})

    return jsonify({"steps": compute_tool_sequence(interactions, nodes, session_id=session_id)})
