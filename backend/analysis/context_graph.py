"""Context graph analysis: decomposes interactions into per-component token estimates."""

from __future__ import annotations

import json
from collections import Counter


def _estimate_tokens(char_count: int) -> int:
    return char_count // 4


def _decompose_anthropic(body: dict) -> dict:
    system = body.get("system", "")
    if isinstance(system, list):
        system_chars = sum(
            len(b.get("text", "")) for b in system if isinstance(b, dict)
        )
    else:
        system_chars = len(system)

    user_chars = 0
    assistant_chars = 0
    tool_calls_chars = 0
    tool_results_chars = 0

    for msg in body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            if role == "user":
                user_chars += len(content)
            elif role == "assistant":
                assistant_chars += len(content)
            continue
        if not isinstance(content, list):
            continue
        if role == "user":
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, list):
                        for rb in rc:
                            if isinstance(rb, dict) and rb.get("type") == "text":
                                tool_results_chars += len(rb.get("text", ""))
                    else:
                        tool_results_chars += len(str(rc))
                elif btype == "text":
                    user_chars += len(block.get("text", ""))
        elif role == "assistant":
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    assistant_chars += len(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls_chars += len(json.dumps(block.get("input", {})))

    return {
        "system_prompt": _estimate_tokens(system_chars),
        "user_messages": _estimate_tokens(user_chars),
        "assistant_messages": _estimate_tokens(assistant_chars),
        "tool_calls": _estimate_tokens(tool_calls_chars),
        "tool_results": _estimate_tokens(tool_results_chars),
    }


def _decompose_openai(body: dict) -> dict:
    system_chars = 0
    user_chars = 0
    assistant_chars = 0
    tool_calls_chars = 0
    tool_results_chars = 0

    for msg in body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_chars += len(content) if isinstance(content, str) else 0
        elif role == "user":
            if isinstance(content, str):
                user_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "tool_result":
                        rc = block.get("content", "")
                        tool_results_chars += len(str(rc))
                    elif btype == "text":
                        user_chars += len(block.get("text", ""))
        elif role == "assistant":
            if isinstance(content, str) and content:
                assistant_chars += len(content)
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                tool_calls_chars += len(fn.get("arguments", ""))
        elif role == "tool":
            if isinstance(content, str):
                tool_results_chars += len(content)

    return {
        "system_prompt": _estimate_tokens(system_chars),
        "user_messages": _estimate_tokens(user_chars),
        "assistant_messages": _estimate_tokens(assistant_chars),
        "tool_calls": _estimate_tokens(tool_calls_chars),
        "tool_results": _estimate_tokens(tool_results_chars),
    }


def _decompose_ollama(body: dict, path: str) -> dict:
    if "/api/generate" in path:
        prompt = body.get("prompt", "")
        system = body.get("system", "")
        return {
            "system_prompt": _estimate_tokens(len(system)),
            "user_messages": _estimate_tokens(len(prompt)),
            "assistant_messages": 0,
            "tool_calls": 0,
            "tool_results": 0,
        }
    # /api/chat — delegate to OpenAI-style decomposer, add top-level system
    result = _decompose_openai(body)
    top_system = body.get("system", "")
    if top_system:
        result["system_prompt"] += _estimate_tokens(len(top_system))
    return result


def decompose_interaction(interaction: dict) -> dict:
    zero = {
        "system_prompt": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "tool_results": 0,
    }
    try:
        seq_id = interaction.get("sequence_id", 0)
        provider = interaction.get("provider", "")
        req = interaction.get("request", {})
        body = req.get("body", {})
        if isinstance(body, str):
            body = json.loads(body)
        if not isinstance(body, dict):
            body = {}

        if "anthropic" in provider.lower():
            decomp = _decompose_anthropic(body)
        elif "openai" in provider.lower():
            decomp = _decompose_openai(body)
        elif "ollama" in provider.lower():
            path = req.get("path", "")
            decomp = _decompose_ollama(body, path)
        else:
            decomp = zero.copy()

        decomp["sequence_id"] = seq_id
        return decomp
    except Exception:
        result = zero.copy()
        result["sequence_id"] = interaction.get("sequence_id", 0)
        return result


def compute_analysis(interactions: list, nodes: list) -> dict:
    nodes = sorted(nodes, key=lambda n: n.get("sequence_id", 0))

    decomp_map = {
        i.get("sequence_id", 0): decompose_interaction(i) for i in interactions
    }
    zero = {
        "system_prompt": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "tool_results": 0,
    }

    # Chart 1 — Context Composition (from interactions, aligned to nodes)
    comp_steps = []
    comp_system = []
    comp_user = []
    comp_assistant = []
    comp_tool_calls = []
    comp_tool_results = []
    for node in nodes:
        seq = node.get("sequence_id", 0)
        d = decomp_map.get(seq, zero)
        comp_steps.append(seq)
        comp_system.append(d.get("system_prompt", 0))
        comp_user.append(d.get("user_messages", 0))
        comp_assistant.append(d.get("assistant_messages", 0))
        comp_tool_calls.append(d.get("tool_calls", 0))
        comp_tool_results.append(d.get("tool_results", 0))

    # Chart 2 — Token Deltas
    delta_steps = [n.get("sequence_id", 0) for n in nodes]
    delta_values = [n.get("delta_effective_input_tokens", 0) for n in nodes]
    delta_event_types = [n.get("event_type", "continuation") for n in nodes]

    # Chart 3 — Context Window Growth (per model)
    models_seen: dict[str, list] = {}
    for node in nodes:
        model = node.get("model") or "unknown"
        seq = node.get("sequence_id", 0)
        val = node.get("total_effective_input_tokens", 0)
        if model not in models_seen:
            models_seen[model] = []
        models_seen[model].append((seq, val))
    growth_series = {
        m: {"steps": [x[0] for x in pts], "values": [x[1] for x in pts]}
        for m, pts in models_seen.items()
    }

    # Chart 4 — Latency Over Time
    lat_steps = [n.get("sequence_id", 0) for n in nodes]
    lat_latency = [n.get("latency_ms") or 0 for n in nodes]
    lat_ttft = [n.get("ttft_ms") or 0 for n in nodes]

    # Chart 5 — Token Usage Per Step
    tu_steps = [n.get("sequence_id", 0) for n in nodes]
    tu_delta_input = [n.get("delta_input_tokens", 0) for n in nodes]
    tu_delta_output = [n.get("delta_output_tokens", 0) for n in nodes]

    # Chart 6 — Cumulative Cost
    cum_steps = []
    cum_values = []
    running_cost = 0.0
    for node in nodes:
        running_cost += node.get("delta_cost_usd", 0) or 0
        cum_steps.append(node.get("sequence_id", 0))
        cum_values.append(running_cost)

    # Chart 7 — Model Distribution
    model_counts = Counter(n.get("model") or "unknown" for n in nodes)
    model_labels = list(model_counts.keys())
    model_values = [model_counts[k] for k in model_labels]

    # Chart 8 — Latency Histogram
    hist_values = [n.get("latency_ms") or 0 for n in nodes if n.get("latency_ms")]
    hist_mean = sum(hist_values) / len(hist_values) if hist_values else 0.0

    return {
        "composition": {
            "steps": comp_steps,
            "system_prompt": comp_system,
            "user_messages": comp_user,
            "assistant_messages": comp_assistant,
            "tool_calls": comp_tool_calls,
            "tool_results": comp_tool_results,
        },
        "token_deltas": {
            "steps": delta_steps,
            "values": delta_values,
            "event_types": delta_event_types,
        },
        "context_growth": {
            "models": list(models_seen.keys()),
            "series": growth_series,
        },
        "latency": {
            "steps": lat_steps,
            "latency_ms": lat_latency,
            "ttft_ms": lat_ttft,
        },
        "token_usage": {
            "steps": tu_steps,
            "delta_input": tu_delta_input,
            "delta_output": tu_delta_output,
        },
        "cumulative_cost": {
            "steps": cum_steps,
            "values": cum_values,
        },
        "model_distribution": {
            "labels": model_labels,
            "values": model_values,
        },
        "latency_histogram": {
            "values": hist_values,
            "mean": hist_mean,
        },
    }
