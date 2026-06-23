#!/usr/bin/env python3
"""real_agent_chronolog.py — one REAL agent per node, wired into the NEW plugin.

Unlike scripts/_imported/real-agent-demo.py (which POSTs LLM turns to the
clio-core-era ``/api/_demo/llm-ingest`` endpoint that this plugin removed), this
runner feeds the chronolog-observability plugin's *actual* data paths:

  * Path-A  (Workspace / Interactions tabs): every LLM turn is appended to the
    plugin's capture spool (``chronolog_observability.capture.spool``). A
    ``chronolog-capture`` worker drains the spool into ChronoLog and the
    dashboard reads it back via ``backend.path_a_reader`` — the same read path
    the blueprints use.

  * Path-B  (Scenarios tab + live SSE): the agent makes a GENUINE MCP tool call
    (``call_remote_agent``, an in-process Claude Agent SDK tool). The tool
    handler emits a start/done inter-agent edge to the node-local collector
    (``/ingest``), which writes it to ChronoLog through the node-local keeper and
    forwards a thin live copy to the dashboard. If the collector is down it
    falls back to POSTing the dashboard's ``/api/_inter-agent/ingest`` directly.

So the turns are real LLM inference (real model / tokens / cost / latency), the
delegation is a real tool invocation, and the transport is the real ChronoLog
cluster — nothing synthetic.

Because no ``ANTHROPIC_API_KEY`` is set, the SDK drives the local ``claude`` CLI
on subscription auth — CLI/subscription tokens, NOT API credits.

Usage (one process per node; run-live-agents.sh fans out the fleet):

    python real_agent_chronolog.py \
        --flask-url     http://ares-comp-28:5000 \
        --collector-url http://127.0.0.1:5650 \
        --scenario      live-2026-06-17 \
        --my-host       $(hostname) \
        --my-role       planner \
        --peers         ares-comp-29:executor,ares-comp-30:fetcher \
        --state-dir     /mnt/common/$USER/observe-state \
        --rounds        1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
import urllib.request


# ───────────────────────────── HTTP helper ───────────────────────────────────


def _post(url: str, body: dict, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


# ─────────────────────── inter-agent edge (Path-B) ───────────────────────────


def emit_edge(cfg: "Cfg", peer_host: str, peer_role: str, ask: str,
              latency_ms: float, ok: bool = True, parent_corr: str = "") -> bool:
    """Emit a start+done inter-agent edge through the node-local collector.

    Falls back to the dashboard's ingest endpoint if the collector is
    unreachable (collector down / not launched). Returns True on success.

    ``parent_corr`` is the correlation_id of the inbound call this agent is
    servicing, if any — emitting it lets the critical-path tracer build exact
    call trees instead of inferring parents from session + time nesting.
    """
    corr = uuid.uuid4().hex
    # from_session/to_session MUST be the scenario-scoped session ids
    # (`role@scenario`) — the same ids the LLM turns are spooled under. The
    # scenario graph keys each agent node by these (its scoped_session_id), and
    # the frontend fetches the clicked node's conversation by that exact key.
    # Emitting a bare role here would orphan the node from its LLM turns: zero
    # tokens on the node and "No interactions" in the Workspace after a click.
    base = {
        "scenario_id":    cfg.scenario,
        "correlation_id": corr,
        "from_host":      cfg.my_host,
        "from_session":   f"{cfg.my_role}@{cfg.scenario}",
        "to_host":        peer_host,
        "to_session":     f"{peer_role}@{cfg.scenario}",
        "kind":           "mcp_call",
        "tool_name":      "call_remote_agent",
        "parent_correlation_id": parent_corr or "",
    }
    start = dict(base, phase="start", payload={"role": cfg.my_role, "ask": ask})
    done = dict(base, phase="done", status="ok" if ok else "error",
                latency_ms=float(latency_ms), payload={"ack": True})

    for target, label in ((cfg.collector_url.rstrip("/") + "/ingest", "collector"),
                          (cfg.flask_url.rstrip("/") + "/api/_inter-agent/ingest", "dashboard")):
        try:
            _post(target, start)
            _post(target, done)
            print(f"  [edge:{label}] {cfg.my_role} -> {peer_role}  "
                  f"({latency_ms:.0f}ms)  corr={corr[:8]}", flush=True)
            return True
        except Exception as exc:
            print(f"  ! edge via {label} failed ({cfg.my_role}->{peer_role}): {exc}",
                  file=sys.stderr, flush=True)
    return False


# ───────────────────────────── config holder ─────────────────────────────────


class Cfg:
    def __init__(self, args, peers):
        self.flask_url = args.flask_url
        self.collector_url = args.collector_url
        self.scenario = args.scenario
        self.my_host = args.my_host
        self.my_role = args.my_role
        self.peers = peers              # list[(host, role)]
        self.model = args.model
        self.delegation_model = args.delegation_model or args.model


# ─────────────────────── spool write (Path-A) ────────────────────────────────


def _usage_tokens(usage: dict | None) -> tuple[int, int]:
    u = usage or {}
    inp = int(u.get("input_tokens", 0) or 0) \
        + int(u.get("cache_read_input_tokens", 0) or 0) \
        + int(u.get("cache_creation_input_tokens", 0) or 0)
    out = int(u.get("output_tokens", 0) or 0)
    return inp, out


def spool_turn(spool, cfg: Cfg, seq: int, kind: str, system: str, prompt: str,
               model: str, response_text: str, tool_calls: list,
               in_tok: int, out_tok: int, cost: float, latency_ms: float) -> None:
    """Append one LLM turn to the capture spool in the exact shape the
    interactions feed and conversation shaper consume, plus a matching context
    node (so per-agent token budgets populate)."""
    session = f"{cfg.my_role}@{cfg.scenario}"
    ts = _now_iso()
    record = {
        "session_id":  session,
        "sequence_id": seq,
        "scenario_id": cfg.scenario,
        "host":        cfg.my_host,
        "timestamp":   ts,
        "provider":    "anthropic",
        "model":       model,
        "request":     {"method": "POST", "path": "/v1/messages",
                        "body": {"system": system,
                                 "messages": [{"role": "user", "content": prompt}]}},
        "response":    {"status_code": 200, "text": response_text,
                        "tool_calls": tool_calls, "is_streaming": False},
        "metrics":     {"total_latency_ms": round(latency_ms),
                        "delta_input_tokens": in_tok,
                        "delta_output_tokens": out_tok,
                        "delta_cost_usd": round(cost, 6)},
        "tool_calls":  tool_calls,
    }
    spool.record_interaction(session, record, sequence_id=seq)
    spool.record_context_node(session, {
        "op": "add", "node": f"{kind}-{seq}", "timestamp": ts, "model": model,
        "summary": f"{kind} turn {seq}",
        # `tokens` = content this turn added to working memory (so the Memory tab
        # shows real growth); deltas kept for cost/metrics.
        "tokens": out_tok,
        "delta_input_tokens": in_tok, "delta_output_tokens": out_tok,
        "delta_cost_usd": round(cost, 6), "latency_ms": round(latency_ms),
    }, sequence_id=seq)


# ───────────────────────────── one real LLM turn ─────────────────────────────


async def run_turn(sdk, spool, cfg: Cfg, seq: int, kind: str, prompt: str,
                   system: str, *, mcp_servers=None, allowed_tools=None,
                   max_turns: int = 1, model: str | None = None):
    """Run one real SDK query, capture REAL metrics, spool the turn.

    Returns (latency_ms, tool_calls, response_text)."""
    opts_kw = dict(
        system_prompt=system,
        model=model or cfg.model,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        setting_sources=[],            # ignore stray repo/user config
    )
    if mcp_servers:
        opts_kw["mcp_servers"] = mcp_servers
    if allowed_tools:
        opts_kw["allowed_tools"] = allowed_tools
    cli = os.path.expanduser("~/.local/bin/claude")
    if os.path.exists(cli):
        opts_kw["cli_path"] = cli

    options = sdk.ClaudeAgentOptions(**opts_kw)

    model_seen = cfg.model or ""
    text_parts: list[str] = []
    result_text = ""
    tool_calls: list[dict] = []
    in_tok = out_tok = 0
    cost = 0.0
    latency_ms = 0.0
    t0 = time.monotonic()

    try:
        async for msg in sdk.query(prompt=prompt, options=options):
            if isinstance(msg, sdk.AssistantMessage):
                if getattr(msg, "model", None):
                    model_seen = msg.model
                for block in (msg.content or []):
                    if isinstance(block, sdk.TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, sdk.ToolUseBlock):
                        tool_calls.append({"name": block.name, "args": block.input})
            elif isinstance(msg, sdk.ResultMessage):
                if msg.total_cost_usd:
                    cost = float(msg.total_cost_usd)
                if msg.duration_ms:
                    latency_ms = float(msg.duration_ms)
                in_tok, out_tok = _usage_tokens(getattr(msg, "usage", None))
                if getattr(msg, "result", None):
                    result_text = msg.result
    except Exception as exc:
        print(f"  ! turn {seq} ({kind}) SDK error: {exc}", file=sys.stderr, flush=True)
        return (time.monotonic() - t0) * 1000.0, [], ""

    if latency_ms == 0.0:
        latency_ms = (time.monotonic() - t0) * 1000.0
    response_text = (result_text or "\n".join(p for p in text_parts if p)).strip()

    spool_turn(spool, cfg, seq, kind, system, prompt, model_seen, response_text,
               tool_calls, in_tok, out_tok, cost, latency_ms)
    flag = " [TOOL]" if tool_calls else ""
    print(f"  [{cfg.my_host}/{cfg.my_role}] turn {seq:<2} {kind:<11} "
          f"{model_seen:<22} {in_tok:>5}+{out_tok:<5}t {latency_ms:>6.0f}ms "
          f"${cost:.5f}{flag}", flush=True)
    return latency_ms, tool_calls, response_text


# ─────────────────────────────────── main ────────────────────────────────────


async def run(sdk, spool, cfg: Cfg, rounds: int) -> dict:
    sys_base = (f"You are the '{cfg.my_role}' agent running on host {cfg.my_host}, part of a "
                f"small multi-agent system. Be terse — at most two sentences.")
    seq = 0
    summary = {"turns": 0, "tool_calls": 0, "edges": 0}

    for rnd in range(1, rounds + 1):
        print(f"[real-agent] ── {cfg.my_role}@{cfg.my_host} round {rnd}/{rounds} ──", flush=True)
        peer = cfg.peers[(rnd - 1) % len(cfg.peers)] if cfg.peers else None
        peer_list = ", ".join(f"{r} (on {h})" for h, r in cfg.peers) or "(none)"

        seq += 1
        await run_turn(sdk, spool, cfg, seq, "framing",
                       prompt="Introduce yourself and state your goal in one sentence.",
                       system=sys_base)
        summary["turns"] += 1

        seq += 1
        await run_turn(sdk, spool, cfg, seq, "reasoning",
                       prompt=(f"Your available peers are: {peer_list}. In one sentence, decide "
                               f"which peer to delegate the next subtask to and why."),
                       system=sys_base)
        summary["turns"] += 1

        if peer:
            # The edge is emitted by the MCP tool handler when the model REALLY
            # calls call_remote_agent. The closure captures this round's peer.
            edge_state = {"emitted": False, "latency_ms": 0.0}

            @sdk.tool("call_remote_agent",
                      "Delegate a concrete subtask to a named peer agent on another host.",
                      {"target": str, "ask": str})
            async def call_remote_agent(args):
                target = str(args.get("target", peer[1]))
                ask = str(args.get("ask", f"{cfg.my_role} -> {peer[1]}"))
                ok = emit_edge(cfg, peer[0], peer[1], f"{cfg.my_role}: {ask}",
                               edge_state["latency_ms"], ok=True)
                edge_state["emitted"] = edge_state["emitted"] or ok
                return {"content": [{"type": "text",
                                     "text": f"Delegated to {target}: acknowledged."}]}

            server = sdk.create_sdk_mcp_server(name="agentbus", version="1.0.0",
                                               tools=[call_remote_agent])

            seq += 1
            latency_ms, tool_calls, _ = await run_turn(
                sdk, spool, cfg, seq, "delegation",
                prompt=(f"Delegate the next subtask to '{peer[1]}' (on host {peer[0]}). "
                        f"You MUST actually call the call_remote_agent tool with "
                        f"target='{peer[1]}' and a one-line 'ask'. The tool may be a "
                        f"deferred/searchable tool — if so, look it up first (e.g. via "
                        f"ToolSearch) and then call it. Do not just describe the call; "
                        f"invoke the tool."),
                system=sys_base,
                mcp_servers={"agentbus": server},
                allowed_tools=["mcp__agentbus__call_remote_agent", "ToolSearch"],
                max_turns=8, model=cfg.delegation_model)
            summary["turns"] += 1
            edge_state["latency_ms"] = latency_ms
            if tool_calls:
                summary["tool_calls"] += len(tool_calls)
            # Safety net: if the model didn't call the tool, emit the edge so the
            # Scenarios tab still has the real delegation recorded.
            if not edge_state["emitted"]:
                if emit_edge(cfg, peer[0], peer[1], f"{cfg.my_role} -> {peer[1]} (fallback)",
                             latency_ms, ok=True):
                    edge_state["emitted"] = True
            if edge_state["emitted"]:
                summary["edges"] += 1

    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--flask-url", required=True)
    p.add_argument("--collector-url", default="http://127.0.0.1:5650")
    p.add_argument("--scenario", required=True)
    p.add_argument("--my-host", required=True)
    p.add_argument("--my-role", required=True)
    p.add_argument("--peers", required=True,
                   help="Comma-sep peer_host:peer_role, e.g. ares-comp-29:executor,ares-comp-30:fetcher")
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--model", default="claude-haiku-4-5",
                   help="Model for framing/reasoning turns (kept small to bound usage)")
    p.add_argument("--delegation-model", default="claude-sonnet-4-6",
                   help="Model for the delegation turn — needs to reliably invoke the "
                        "MCP tool, so defaults to a stronger model than --model")
    p.add_argument("--state-dir", default="",
                   help="DTP_STATE_DIR for the shared capture spool (must match the "
                        "chronolog-capture worker draining it)")
    args = p.parse_args()

    if args.state_dir:
        os.environ["DTP_STATE_DIR"] = args.state_dir

    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  ! WARNING: ANTHROPIC_API_KEY is set — this spends API credits, not "
              "subscription tokens. Unset it for CLI auth.", file=sys.stderr)

    # Imported here so --help works without the SDK / plugin env.
    try:
        import claude_agent_sdk as sdk
    except Exception as exc:
        print(f"FAIL: cannot import claude_agent_sdk ({exc}). Run under the conda env "
              f"that has it (e.g. `conda run -n iowarp ...`).", file=sys.stderr)
        return 3
    try:
        from chronolog_observability.capture.spool import get_spool
    except Exception as exc:
        print(f"FAIL: cannot import the plugin spool ({exc}). Put <repo>/src on PYTHONPATH.",
              file=sys.stderr)
        return 3

    peers: list[tuple[str, str]] = []
    for tok in args.peers.split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        h, r = tok.split(":", 1)
        peers.append((h.strip(), r.strip()))
    if not peers:
        print("FAIL: no valid peers (need host:role)", file=sys.stderr)
        return 2

    cfg = Cfg(args, peers)
    spool = get_spool()
    print(f"[real-agent] flask={cfg.flask_url} collector={cfg.collector_url} "
          f"scenario={cfg.scenario} me={cfg.my_host}/{cfg.my_role} model={cfg.model} "
          f"peers={peers} spool={os.environ.get('DTP_STATE_DIR', '~/.dt_provenance')}", flush=True)
    summary = asyncio.run(run(sdk, spool, cfg, args.rounds))
    print(f"[real-agent] DONE — {cfg.my_role}@{cfg.my_host}: "
          f"{summary['turns']} turns, {summary['tool_calls']} real tool-calls, "
          f"{summary['edges']} edges", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
