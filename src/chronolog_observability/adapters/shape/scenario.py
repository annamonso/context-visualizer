"""Map scenario-wide state (inter-agent events + per-peer session subtrees) onto a graph.

A "scenario" is the HPC analogue of a conversation but for *peer* agents: several
independent agents (each possibly an orchestrator with its own subagents) that
talk to one another via inter-agent calls. One such peer agent is rooted at its
base session id (``planner-a`` vs. ``planner-a.1`` handled via the existing
conversation adapter).

The scenario graph returned by ``build_scenario_graph`` therefore has:

    agents : list of peer-agent nodes (one per base session id participating
             in the scenario). Each agent aggregates interaction/token/cost
             metrics across its own subtree (orchestrator + subagents).
    edges  : one per stitched inter-agent call (start+done merged into one
             event). Kind is ``inter_agent_msg``.

Scenario membership for Phase 1 is defined by "any session named as
``from_session`` or ``to_session`` in an InterAgentMessage". Sessions with a
matching ``scenario_id`` on their InteractionRecord but no inter-agent events
will appear once Phase 2 walks interaction metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from .conversation import (
    base_session_id,
)


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO-8601 parse (handles a trailing ``Z``). Returns None on miss."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except Exception:
        return None


def _peer_bundle(
    peer_root: str,
    session_bundles_by_id: dict[str, dict],
) -> tuple[dict, list[str]]:
    """Aggregate one peer agent (base session + its subagents) from session bundles.

    Returns ``(agent_node, sub_session_ids)``. Missing session bundles are
    skipped silently — a peer can have been named in an inter-agent event
    before its first LLM turn lands.
    """
    interaction_count = 0
    total_tokens = 0
    total_cost = 0.0
    host_votes: dict[str, int] = {}
    sub_sessions: list[str] = []

    for sid, bundle in session_bundles_by_id.items():
        if sid != peer_root and not sid.startswith(peer_root + "."):
            continue
        sub_sessions.append(sid)
        inters = [i for i in (bundle.get("interactions") or []) if isinstance(i, dict)]
        ctxs = [n for n in (bundle.get("context_nodes") or []) if isinstance(n, dict)]
        ctx_by_seq = {n.get("sequence_id", -1): n for n in ctxs}

        interaction_count += len(inters)
        for inter in inters:
            ctx = ctx_by_seq.get(inter.get("sequence_id", -1), {})
            total_tokens += (ctx.get("delta_input_tokens") or 0)
            total_tokens += (ctx.get("delta_output_tokens") or 0)
            total_cost += ctx.get("delta_cost_usd") or 0.0
            h = inter.get("host")
            if isinstance(h, str) and h:
                host_votes[h] = host_votes.get(h, 0) + 1

    # Most-frequent host wins; ties broken by first-seen.
    host = ""
    if host_votes:
        host = max(host_votes.items(), key=lambda kv: kv[1])[0]

    sub_sessions.sort()

    node = {
        "agent_id": peer_root,
        "session_id": peer_root,
        "agent_role": "peer",  # peer == scenario-level role (not orchestrator/subagent)
        "host": host,
        "interaction_count": interaction_count,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "sub_sessions": sub_sessions,
    }
    return node, sub_sessions


def _host_from_events(peer_root: str, inter_agent_events: Iterable[dict]) -> str:
    """Vote across inter-agent events for a peer with no LLM interactions
    (e.g., a star-topology controller that only fires edge POSTs)."""
    votes: dict[str, int] = {}
    for ev in inter_agent_events:
        if not isinstance(ev, dict):
            continue
        from_sid = ev.get("from_session") or ""
        to_sid = ev.get("to_session") or ""
        if base_session_id(from_sid) == peer_root:
            h = (ev.get("from_host") or "").strip()
            if h:
                votes[h] = votes.get(h, 0) + 1
        if base_session_id(to_sid) == peer_root:
            h = (ev.get("to_host") or "").strip()
            if h:
                votes[h] = votes.get(h, 0) + 1
    if not votes:
        return ""
    return max(votes.items(), key=lambda kv: kv[1])[0]


def participants(inter_agent_events: Iterable[dict]) -> list[str]:
    """Return the set of peer agent roots named in any inter-agent event, sorted."""
    roots: set[str] = set()
    for ev in inter_agent_events:
        for key in ("from_session", "to_session"):
            sid = ev.get(key) if isinstance(ev, dict) else None
            if isinstance(sid, str) and sid:
                roots.add(base_session_id(sid))
    return sorted(roots)


def strip_scope(sid: str, scenario_id: str) -> str:
    """Remove the ``@<scenario_id>`` suffix from a scenario-scoped session id so
    the UI shows the bare role (``planner`` not ``planner@research-2026...``).

    Session ids are scoped per scenario at write time so each run's
    per-role conversation is distinct; within a single scenario graph the
    suffix is redundant, so we drop it for display. Ids that don't carry the
    suffix (legacy / un-scoped data) pass through unchanged."""
    if not sid or not scenario_id:
        return sid
    suffix = "@" + scenario_id
    return sid[: -len(suffix)] if sid.endswith(suffix) else sid


def build_scenario_graph(
    scenario_id: str,
    session_bundles: list[dict],
    inter_agent_events: list[dict],
) -> dict:
    """Build a ``ScenarioGraph`` dict.

    Parameters
    ----------
    scenario_id
        The scenario tag.
    session_bundles
        Pre-fetched ``{session_id, interactions, context_nodes}`` for every
        session implicated in the scenario (peers + their subagents). Callers
        can over-fetch; extra sessions are ignored.
    inter_agent_events
        Stitched events from ``InterAgentStore.read_stitched``.
    """
    bundles_by_id = {b.get("session_id", ""): b for b in session_bundles if b.get("session_id")}
    peer_roots = participants(inter_agent_events)

    agents = []
    for root in peer_roots:
        node, _subs = _peer_bundle(root, bundles_by_id)
        if not node["host"]:
            node["host"] = _host_from_events(root, inter_agent_events)
        agents.append(node)

    edges = []
    for ev in inter_agent_events:
        from_sid = ev.get("from_session", "")
        to_sid = ev.get("to_session", "")
        edges.append({
            "kind": "inter_agent_msg",
            "event_id": ev.get("correlation_id") or "",  # correlation_id is the stable key
            "correlation_id": ev.get("correlation_id", ""),
            "from_session_id": base_session_id(from_sid) if from_sid else "",
            "from_sub_session_id": from_sid,
            "to_session_id": base_session_id(to_sid) if to_sid else "",
            "to_sub_session_id": to_sid,
            "from_host": ev.get("from_host", ""),
            "to_host": ev.get("to_host", ""),
            "ts_start": ev.get("ts_start"),
            "ts_done": ev.get("ts_done"),
            "tool_name": ev.get("tool_name", ""),
            "status": ev.get("status", ""),
            "latency_ms": ev.get("latency_ms") or 0.0,
            "payload_preview": ev.get("payload_preview", ""),
        })

    # Strip the scenario suffix for display — consistently across agent ids
    # AND edge endpoints so the frontend still links edges to nodes. The
    # pre-strip id is kept as ``scoped_session_id``: it is the real session
    # key, and the frontend needs it to fetch the agent's conversation
    # (fetching with the stripped id silently returns an empty tree).
    for a in agents:
        a["scoped_session_id"] = a.get("session_id", "") or a.get("agent_id", "")
        a["agent_id"] = strip_scope(a.get("agent_id", ""), scenario_id)
        a["session_id"] = strip_scope(a.get("session_id", ""), scenario_id)
        a["sub_sessions"] = [strip_scope(s, scenario_id) for s in a.get("sub_sessions", [])]
    for e in edges:
        for k in ("from_session_id", "from_sub_session_id",
                  "to_session_id", "to_sub_session_id"):
            e[k] = strip_scope(e.get(k, ""), scenario_id)

    return {
        "scenario_id": scenario_id,
        "agents": agents,
        "edges": edges,
    }


def build_scenario_summary(
    scenario_id: str,
    inter_agent_events: list[dict],
) -> dict:
    """Lightweight summary for the scenario list — no session fetches needed."""
    peers = participants(inter_agent_events)
    hosts = sorted({
        h for ev in inter_agent_events
        for h in (ev.get("from_host", ""), ev.get("to_host", ""))
        if h
    })
    first_ts = ""
    last_ts = ""
    for ev in inter_agent_events:
        for key in ("ts_start", "ts_done"):
            t = ev.get(key)
            if isinstance(t, str) and t:
                if not first_ts or t < first_ts:
                    first_ts = t
                if not last_ts or t > last_ts:
                    last_ts = t
    return {
        "scenario_id": scenario_id,
        "agent_count": len(peers),
        "agents": [strip_scope(p, scenario_id) for p in peers],
        "hosts": hosts,
        "event_count": len(inter_agent_events),
        "firstEvent": first_ts,
        "lastEvent": last_ts,
    }


def build_scenario_timeline(
    graph: dict,
    llm_by_agent: list[tuple[str, str, list[dict]]],
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> list[dict]:
    """Merge inter-agent edges and per-agent LLM interactions into one typed,
    time-ordered event stream for the scenario-play timeline.

    Each event carries a ``type`` of ``agent_msg`` (cross-node call),
    ``llm_call`` (an agent's LLM turn), or ``tool_call`` (a tool the LLM
    invoked during that turn). ``agent`` is the column the event renders in;
    ``agent_msg`` additionally spans ``from``→``to``.

    ``llm_by_agent`` is ``[(agent_id, host, [interaction, ...]), ...]``.
    LLM interactions outside ``[window_start, window_end]`` are dropped so we
    only show turns belonging to *this* scenario run, not the agent's whole
    history.
    """
    events: list[dict] = []

    for e in graph.get("edges", []):
        events.append({
            "type": "agent_msg",
            "ts_start": e.get("ts_start"),
            "ts_done": e.get("ts_done"),
            "latency_ms": e.get("latency_ms"),
            "agent": e.get("from_session_id", ""),
            "host": e.get("from_host", ""),
            "from": e.get("from_session_id", ""),
            "from_host": e.get("from_host", ""),
            "to": e.get("to_session_id", ""),
            "to_host": e.get("to_host", ""),
            "tool_name": e.get("tool_name", ""),
            "status": e.get("status", ""),
            "payload_preview": e.get("payload_preview", ""),
            "correlation_id": e.get("correlation_id", ""),
        })

    for agent, host, inters in llm_by_agent:
        for it in inters:
            ts = it.get("timestamp", "")
            dt = parse_iso(ts)
            if dt is not None:
                if window_start is not None and dt < window_start:
                    continue
                if window_end is not None and dt > window_end:
                    continue
            metrics = it.get("metrics") or {}
            resp = it.get("response") or {}
            tool_calls = it.get("tool_calls") or resp.get("tool_calls") or []
            req_body = (it.get("request") or {}).get("body") or {}
            prompt_parts: list[str] = []
            if req_body.get("system"):
                prompt_parts.append("system: " + str(req_body["system"]))
            for m in req_body.get("messages") or []:
                if isinstance(m, dict):
                    prompt_parts.append(f"{m.get('role', '?')}: {m.get('content', '')}")
            base = {
                "agent": agent,
                "host": host or it.get("host", ""),
                "ts_start": ts,
                "ts_done": ts,
                "latency_ms": metrics.get("total_latency_ms"),
                "model": it.get("model", ""),
                "provider": it.get("provider", ""),
                "tokens_in": metrics.get("delta_input_tokens"),
                "tokens_out": metrics.get("delta_output_tokens"),
                "cost_usd": metrics.get("delta_cost_usd"),
                "status_code": resp.get("status_code"),
                "prompt": "\n".join(prompt_parts),
                "text": resp.get("text", ""),
                "sequence_id": it.get("sequence_id"),
            }
            events.append({**base, "type": "llm_call"})
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                events.append({
                    **base,
                    "type": "tool_call",
                    "tool_name": tc.get("name", ""),
                    "tool_args": tc.get("args") or tc.get("arguments") or {},
                    # "ok" | "error" | "" (unknown — legacy records carry no result)
                    "tool_status": tc.get("status", ""),
                    "tool_error": tc.get("error", ""),
                })

    events.sort(key=lambda ev: ev.get("ts_start") or "")
    return events
