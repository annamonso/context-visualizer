"""Call graph analysis: computes call topology and tool sequence from interactions + context nodes."""

from __future__ import annotations

import json
import statistics
from typing import Any


def _parse_body(body: Any) -> dict:
    if isinstance(body, str):
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {}
    return body if isinstance(body, dict) else {}


def _extract_tool_calls_anthropic(response_body: dict) -> list[dict]:
    tool_calls = []
    for block in response_body.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "name": block.get("name", "?"),
                "input": block.get("input", {}),
            })
    return tool_calls


def _extract_tool_calls_openai(response_body: dict) -> list[dict]:
    tool_calls = []
    choices = response_body.get("choices", [])
    if not choices:
        return tool_calls
    for tc in (choices[0].get("message", {}).get("tool_calls") or []):
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        tool_calls.append({
            "id": tc.get("id"),
            "name": fn.get("name", "?"),
            "input": args,
        })
    return tool_calls


def _extract_tool_results_anthropic(request_body: dict) -> list[dict]:
    results = []
    for msg in request_body.get("messages", []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            rc = block.get("content", "")
            if isinstance(rc, list):
                text_parts = [rb.get("text", "") for rb in rc if isinstance(rb, dict) and rb.get("type") == "text"]
                rc = " ".join(text_parts)
            results.append({"toolCallId": block.get("tool_use_id"), "content": str(rc)})
    return results


def _extract_tool_results_openai(request_body: dict) -> list[dict]:
    results = []
    for msg in request_body.get("messages", []):
        if msg.get("role") == "tool":
            results.append({
                "toolCallId": msg.get("tool_call_id"),
                "content": str(msg.get("content", "")),
            })
    return results


def _extract_system_prompt(request_body: dict, provider: str) -> str | None:
    if "anthropic" in provider.lower():
        system = request_body.get("system", "")
        if isinstance(system, list):
            system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
        return str(system) if system else None
    for msg in request_body.get("messages", []):
        if msg.get("role") == "system":
            content = msg.get("content", "")
            return str(content) if content else None
    return None


def _extract_tool_calls(interaction: dict, response_body: dict, provider: str) -> list[dict]:
    """Extract tool calls, preferring raw body parse over pre-extracted data.

    Note: response.body is not stored for streaming interactions (only response.tool_calls,
    response.text, and response.stop_reason are). We fall back to the pre-extracted list
    which always includes the 'id' field from the C++ parser.
    """
    if "anthropic" in provider.lower():
        tcs = _extract_tool_calls_anthropic(response_body)
    elif "openai" in provider.lower() or "ollama" in provider.lower():
        tcs = _extract_tool_calls_openai(response_body)
    else:
        tcs = []
    if not tcs:
        pre = (interaction.get("response") or {}).get("tool_calls") or []
        tcs = [{"id": tc.get("id"), "name": tc.get("name", "?"), "input": tc.get("input", {})} for tc in pre]
    return tcs


def _build_node_metrics(latencies: list, errors: int, calls: int, tokens: int, cost: float) -> dict:
    avg_lat = statistics.mean(latencies) if latencies else None
    p95_lat = None
    if latencies:
        idx = int(len(latencies) * 0.95)
        p95_lat = sorted(latencies)[min(idx, len(latencies) - 1)]
    return {
        "callCount": calls,
        "errorRate": errors / calls if calls > 0 else 0.0,
        "avgLatencyMs": avg_lat,
        "p95LatencyMs": p95_lat,
        "totalTokens": tokens,
        "totalCostUsd": cost,
    }


def compute_tool_sequence(
    interactions: list,
    context_nodes: list,
    session_id: str = "",
    is_subagent: bool = False,
) -> list[dict]:
    """Produce one entry per interaction ordered by sequence_id.

    Each step includes sessionId and isSubagent for multi-session workflow views.
    Tool results are extracted from the next interaction's request body (the full
    conversation history sent to the LLM contains tool_result blocks referencing
    the previous step's tool call IDs).
    """
    node_map = {n.get("sequence_id", 0): n for n in context_nodes}
    sorted_interactions = sorted(interactions, key=lambda i: i.get("sequence_id", 0))

    result = []
    for idx, interaction in enumerate(sorted_interactions):
        seq_id = interaction.get("sequence_id", 0)
        provider = interaction.get("provider", "unknown")
        ctx_node = node_map.get(seq_id, {})
        req = interaction.get("request", {})
        resp = interaction.get("response", {})
        request_body = _parse_body(req.get("body", {}))
        response_body = _parse_body(resp.get("body", {}))

        tool_calls = _extract_tool_calls(interaction, response_body, provider)

        if "anthropic" in provider.lower():
            tool_results = _extract_tool_results_anthropic(request_body)
        elif "openai" in provider.lower() or "ollama" in provider.lower():
            tool_results = _extract_tool_results_openai(request_body)
        else:
            tool_results = []

        response_text = resp.get("text") or None
        if not response_text:
            if "anthropic" in provider.lower():
                texts = [b.get("text", "") for b in response_body.get("content", [])
                         if isinstance(b, dict) and b.get("type") == "text"]
                response_text = " ".join(texts) or None
            elif "openai" in provider.lower():
                choices = response_body.get("choices", [])
                if choices:
                    response_text = choices[0].get("message", {}).get("content") or None

        result.append({
            "interactionId": str(seq_id),
            "interactionIndex": idx,
            "timestamp": interaction.get("timestamp", ""),
            "provider": provider,
            "model": ctx_node.get("model") or interaction.get("model"),
            "latencyMs": ctx_node.get("latency_ms"),
            "statusCode": resp.get("status_code"),
            "error": resp.get("error"),
            "toolCalls": tool_calls,
            "toolResults": tool_results,
            "responseText": response_text,
            "systemPromptPreview": _extract_system_prompt(request_body, provider),
            "inputTokens": ctx_node.get("delta_input_tokens"),
            "outputTokens": ctx_node.get("delta_output_tokens"),
            # Multi-session fields
            "sessionId": session_id,
            "isSubagent": is_subagent,
        })
    return result


def compute_workflow_sequence(sessions: list[dict]) -> list[dict]:
    """Combine tool sequences from multiple sessions, sorted by timestamp.

    Each entry in `sessions` is:
      {session_id, interactions, context_nodes, is_subagent}

    Returns all steps interleaved chronologically. The globalResultMap in the
    JavaScript renderer covers all sessions, so tool result matching works
    across session boundaries.
    """
    all_steps = []
    for sess in sessions:
        steps = compute_tool_sequence(
            sess["interactions"],
            sess["context_nodes"],
            session_id=sess["session_id"],
            is_subagent=sess.get("is_subagent", False),
        )
        all_steps.extend(steps)
    # Sort by timestamp, then session_id for determinism when timestamps match
    all_steps.sort(key=lambda s: (s.get("timestamp", ""), s.get("sessionId", ""), s.get("interactionId", "")))
    return all_steps


def _accum_entity(acc: dict, lat_ms, is_err: bool, tokens: int, cost: float) -> None:
    acc["calls"] += 1
    if lat_ms:
        acc["latencies"].append(lat_ms)
    if is_err:
        acc["errors"] += 1
    acc["tokens"] += tokens
    acc["cost"] += cost


def _process_session_interactions(
    interactions: list,
    context_nodes: list,
    entity: dict,
    edge_acc: dict,
    timeline: list | None,
    agent_id: str,
    agent_type: str,
    agent_label: str,
) -> None:
    """Accumulate one session's interactions into shared entity/edge maps."""
    node_map = {n.get("sequence_id", 0): n for n in context_nodes}
    sorted_interactions = sorted(interactions, key=lambda i: i.get("sequence_id", 0))

    def get_entity(eid: str, etype: str, label: str) -> dict:
        if eid not in entity:
            entity[eid] = {"type": etype, "label": label, "latencies": [], "errors": 0, "calls": 0, "tokens": 0, "cost": 0.0}
        return entity[eid]

    def get_edge(from_id: str, to_id: str) -> dict:
        key = (from_id, to_id)
        if key not in edge_acc:
            edge_acc[key] = {"latencies": [], "errors": 0, "calls": 0, "tokens": 0, "cost": 0.0}
        return edge_acc[key]

    # Ensure agent node and proxy node exist
    get_entity(agent_id, agent_type, agent_label)
    get_entity("proxy", "proxy", "dt_proxy")

    for interaction in sorted_interactions:
        seq_id = interaction.get("sequence_id", 0)
        provider = interaction.get("provider", "unknown")
        ctx_node = node_map.get(seq_id, {})
        resp = interaction.get("response", {})
        response_body = _parse_body(resp.get("body", {}))

        lat_ms = ctx_node.get("latency_ms")
        status_code = resp.get("status_code")
        is_error = bool(resp.get("error") or (status_code and status_code >= 400))
        tokens = (ctx_node.get("delta_input_tokens") or 0) + (ctx_node.get("delta_output_tokens") or 0)
        cost = ctx_node.get("delta_cost_usd") or 0.0
        model_name = ctx_node.get("model") or interaction.get("model")

        provider_id = f"provider/{provider}"
        model_id = f"model/{model_name}" if model_name else None

        _accum_entity(get_entity(agent_id, agent_type, agent_label), lat_ms, is_error, tokens, cost)
        _accum_entity(get_entity("proxy", "proxy", "dt_proxy"), lat_ms, is_error, tokens, cost)
        _accum_entity(get_entity(provider_id, "provider", provider), lat_ms, is_error, tokens, cost)
        if model_id:
            _accum_entity(get_entity(model_id, "model", model_name), lat_ms, is_error, tokens, cost)

        _accum_entity(get_edge(agent_id, "proxy"), lat_ms, is_error, tokens, cost)
        _accum_entity(get_edge("proxy", provider_id), lat_ms, is_error, tokens, cost)
        if model_id:
            _accum_entity(get_edge(provider_id, model_id), lat_ms, is_error, tokens, cost)

        tool_calls = _extract_tool_calls(interaction, response_body, provider)
        for tc in tool_calls:
            tool_name = tc.get("name", "?")
            tool_id = f"tool/{tool_name}"
            src_id = model_id if model_id else provider_id
            _accum_entity(get_entity(tool_id, "tool", tool_name), None, False, 0, 0.0)
            _accum_entity(get_edge(src_id, tool_id), None, False, 0, 0.0)

        if timeline is not None:
            timeline.append({
                "interactionId": str(seq_id),
                "timestamp": interaction.get("timestamp", ""),
                "status": status_code,
                "latencyMs": lat_ms,
                "provider": provider,
                "isStreaming": bool(interaction.get("is_streaming") or resp.get("is_streaming")),
                "error": resp.get("error"),
            })


def compute_call_graph(interactions: list, context_nodes: list) -> dict:
    """Compute single-session call graph {nodes, edges, timeline}."""
    entity: dict[str, dict] = {}
    edge_acc: dict[tuple, dict] = {}
    timeline: list = []

    _process_session_interactions(
        interactions, context_nodes, entity, edge_acc, timeline,
        agent_id="agent", agent_type="agent", agent_label="Agent",
    )
    return _build_graph_output(entity, edge_acc, timeline)


def compute_workflow_graph(sessions: list[dict]) -> dict:
    """Compute merged call graph for parent + child sessions.

    Each entry in `sessions` is:
      {session_id, interactions, context_nodes, is_subagent}

    The main session gets a single "agent" node. Each child session gets a
    "subagent/{session_id}" node. All sessions share proxy/provider/model/tool nodes
    (metrics are aggregated across all sessions).
    """
    entity: dict[str, dict] = {}
    edge_acc: dict[tuple, dict] = {}
    timeline: list = []

    for sess in sessions:
        is_sub = sess.get("is_subagent", False)
        sid = sess["session_id"]
        if is_sub:
            # Use the last component of the session ID as a short label
            suffix = sid.rsplit(".", 1)[-1]
            agent_id = f"subagent/{sid}"
            agent_label = f"Sub-agent .{suffix}"
            agent_type = "subagent"
        else:
            agent_id = "agent"
            agent_label = "Agent"
            agent_type = "agent"

        _process_session_interactions(
            sess["interactions"], sess["context_nodes"],
            entity, edge_acc,
            timeline if not is_sub else None,  # only include parent session in timeline
            agent_id=agent_id, agent_type=agent_type, agent_label=agent_label,
        )

    return _build_graph_output(entity, edge_acc, timeline)


def _build_graph_output(entity: dict, edge_acc: dict, timeline: list) -> dict:
    nodes = [
        {
            "id": nid,
            "type": ndata["type"],
            "label": ndata["label"],
            "metrics": _build_node_metrics(ndata["latencies"], ndata["errors"], ndata["calls"], ndata["tokens"], ndata["cost"]),
        }
        for nid, ndata in entity.items()
    ]

    edges = [
        {
            "from": from_id,
            "to": to_id,
            **_build_node_metrics(edata["latencies"], edata["errors"], edata["calls"], edata["tokens"], edata["cost"]),
        }
        for (from_id, to_id), edata in edge_acc.items()
    ]

    return {"nodes": nodes, "edges": edges, "timeline": timeline}
