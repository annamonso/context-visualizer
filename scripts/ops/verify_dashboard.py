#!/usr/bin/env python3
"""verify_dashboard.py — assert the live dashboard serves REAL, correct data.

Drives the dashboard exactly as the React SPA does and checks the data behind
every tab plus the scenario→workspace click-through the user asked about:

  Scenarios tab   GET /api/scenarios , /api/scenarios/<sid>/graph
  Click an agent  -> the frontend uses the node's scoped_session_id and opens
                     the Workspace conversation for it:
                       GET /api/conversations/<scoped>/agent-graph   (metrics)
                       GET /_interceptor/conversations/<scoped>      (per-turn)
  Click a host    -> Workspace host mode: every agent on that node merged
  Workspace tab   GET /api/conversations
  Interactions    GET /_interceptor/interactions
  Cluster tab     GET /api/chronolog/topology
  Live SSE        GET /api/_inter-agent/stream?scenario=<sid>&backlog=1

Exit 0 iff every REQUIRED check passes. Usage:
    python verify_dashboard.py http://ares-comp-28:5000 live-4node
"""
from __future__ import annotations

import json
import sys
import urllib.request


def _get(url: str, timeout: float = 150.0):
    # Long timeout on purpose: the read endpoints do ReplayStory, which holds
    # the GIL and can take many seconds per call (and freezes the dashboard
    # entirely if a story is still inside the ~180s drain window). Verify only
    # AFTER the drain so these return promptly.
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


PASS, FAIL, WARN = [], [], []


def check(name: str, cond: bool, detail: str = "", *, required: bool = True) -> bool:
    bucket = (PASS if cond else (FAIL if required else WARN))
    bucket.append(name)
    tag = "OK  " if cond else ("FAIL" if required else "warn")
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return cond


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: verify_dashboard.py <flask_url> <scenario>", file=sys.stderr)
        return 2
    base = sys.argv[1].rstrip("/")
    scenario = sys.argv[2]
    print(f"=== verifying {base}  scenario={scenario} ===\n")

    # ── config / adapters ────────────────────────────────────────────────
    print("[config]")
    cfg = _get(f"{base}/api/config")
    caps = cfg.get("capabilities", {})
    check("adapter active + all caps present",
          all(caps.get(c) for c in ("conversations", "scenarios", "interactions",
                                    "inter_agent", "cluster")),
          f"caps={sorted(caps)}")
    check("not offline", cfg.get("offline") is False)

    # ── Scenarios tab ────────────────────────────────────────────────────
    print("\n[scenarios tab]")
    scens = _get(f"{base}/api/scenarios")
    sids = [s.get("scenario_id") for s in scens]
    check(f"scenario '{scenario}' listed", scenario in sids, f"scenarios={sids}")
    summary = next((s for s in scens if s.get("scenario_id") == scenario), {})
    check("scenario has >=2 agents", summary.get("agent_count", 0) >= 2,
          f"agent_count={summary.get('agent_count')} agents={summary.get('agents')}")
    check("scenario has >=1 host", len(summary.get("hosts", [])) >= 1,
          f"hosts={summary.get('hosts')}")

    graph = _get(f"{base}/api/scenarios/{scenario}/graph")
    agents = graph.get("agents", [])
    edges = graph.get("edges", [])
    check("graph has agent nodes", len(agents) >= 2, f"{len(agents)} agents")
    check("graph has inter-agent edges", len(edges) >= 1, f"{len(edges)} edges")
    check("edges carry tool_name=call_remote_agent",
          any(e.get("tool_name") == "call_remote_agent" for e in edges),
          f"tools={sorted({e.get('tool_name') for e in edges})}")
    check("edges link real role nodes (from/to session ids set)",
          all(e.get("from_session_id") and e.get("to_session_id") for e in edges))

    # per-agent metrics on the scenario node (what the node card shows)
    toks = sum(a.get("total_tokens", 0) for a in agents)
    cost = sum(a.get("total_cost_usd", 0.0) for a in agents)
    inter = sum(a.get("interaction_count", 0) for a in agents)
    check("scenario nodes show real token totals (>0)", toks > 0, f"sum tokens={toks}")
    check("scenario nodes show real cost (>0)", cost > 0, f"sum cost=${cost:.5f}")
    check("scenario nodes show interaction counts (>0)", inter > 0, f"sum interactions={inter}")
    check("every agent node has a host",
          all(a.get("host") for a in agents),
          f"hosts={sorted({a.get('host') for a in agents})}")
    check("every agent node carries scoped_session_id",
          all(a.get("scoped_session_id") for a in agents),
          f"scoped={[a.get('scoped_session_id') for a in agents]}")

    # ── CLICK-THROUGH: agent node -> Workspace conversation ──────────────
    # Frontend onAgentClick(scoped_session_id) -> ?conv=<scoped> ->
    #   getAgentGraph(scoped) + getConversationTurns(scoped)
    print("\n[click agent node -> workspace]")
    click_ok = True
    for a in agents:
        scoped = a.get("scoped_session_id") or a.get("agent_id")
        role = a.get("agent_id")
        ag = _get(f"{base}/api/conversations/{urllib_quote(scoped)}/agent-graph")
        nodes = ag.get("nodes", [])
        node_tok = sum(n.get("total_tokens", 0) for n in nodes)
        node_cost = sum(n.get("total_cost_usd", 0.0) for n in nodes)
        turns = _get(f"{base}/_interceptor/conversations/{urllib_quote(scoped)}")
        has_turns = len(turns) >= 1
        has_metrics = node_tok > 0 and node_cost > 0
        models = sorted({t.get("model") for t in turns if t.get("model")})
        has_real_model = any(m and ("claude" in m) for m in models)
        # per-turn metrics the DetailPanel renders
        any_turn_tokens = any((t.get("total_tokens") or 0) > 0 for t in turns)
        any_turn_latency = any((t.get("total_latency_ms") or 0) > 0 for t in turns)
        ok = has_turns and has_metrics and has_real_model and any_turn_tokens
        click_ok = click_ok and ok
        print(f"    · {role:<9} scoped={scoped!r}: turns={len(turns)} "
              f"node_tok={node_tok} node_cost=${node_cost:.5f} "
              f"models={models} turn_tok={any_turn_tokens} turn_lat={any_turn_latency} "
              f"-> {'OK' if ok else 'INCOMPLETE'}", flush=True)
    check("clicking each agent node loads its conversation with real metrics",
          click_ok)

    # interconnections inside the workspace conversation (handoff edges) —
    # single-session peers may legitimately have 0 handoffs, so this is a warn.
    sample = agents[0].get("scoped_session_id") if agents else None
    if sample:
        ag = _get(f"{base}/api/conversations/{urllib_quote(sample)}/agent-graph")
        check("workspace agent-graph has nodes (interconnection view)",
              len(ag.get("nodes", [])) >= 1, f"{len(ag.get('nodes', []))} nodes")

    # ── CLICK-THROUGH: host node -> Workspace host mode ──────────────────
    print("\n[click host node -> workspace host mode]")
    hosts = sorted({a.get("host") for a in agents if a.get("host")})
    host_ok = True
    for h in hosts:
        on_host = [a for a in agents if a.get("host") == h]
        merged_turns = 0
        for a in on_host:
            scoped = a.get("scoped_session_id") or a.get("agent_id")
            merged_turns += len(_get(f"{base}/_interceptor/conversations/{urllib_quote(scoped)}"))
        ok = merged_turns >= 1
        host_ok = host_ok and ok
        print(f"    · host {h}: {len(on_host)} agent(s), {merged_turns} merged turns "
              f"-> {'OK' if ok else 'EMPTY'}", flush=True)
    check("clicking each host loads merged node activity", host_ok)

    # ── Workspace tab (conversation list) ────────────────────────────────
    print("\n[workspace tab]")
    convs = _get(f"{base}/api/conversations")
    conv_ids = {c.get("agentId") or c.get("conversationId") for c in convs}
    check("workspace lists the scenario's agents",
          any((a.get("agent_id") in conv_ids) for a in agents),
          f"conversation agentIds={sorted(x for x in conv_ids if x)[:8]}")

    # ── Interactions tab ─────────────────────────────────────────────────
    print("\n[interactions tab]")
    feed = _get(f"{base}/_interceptor/interactions?limit=200")
    check("interactions feed non-empty", len(feed) >= 1, f"{len(feed)} rows")
    fmodels = sorted({r.get("model") for r in feed if r.get("model")})
    check("interactions carry real claude models",
          any(m and "claude" in m for m in fmodels), f"models={fmodels}")
    check("interactions carry latency metrics",
          any((r.get("total_latency_ms") or 0) > 0 for r in feed))

    # ── Cluster tab ──────────────────────────────────────────────────────
    print("\n[cluster tab]")
    topo = _get(f"{base}/api/chronolog/topology")
    check("cluster connected to ChronoLog", topo.get("connected") is True)
    comp = topo.get("components", {})
    check("cluster shows a visor host", bool(comp.get("visor", {}).get("host")),
          f"visor={comp.get('visor')}")
    check("cluster shows grapher/player host",
          bool(comp.get("grapher", {}).get("host")), f"grapher={comp.get('grapher')}")
    keepers = topo.get("keepers", [])
    in_alloc = [k for k in keepers if k.get("in_allocation")]
    check("cluster shows in-allocation keepers", len(in_alloc) >= 1,
          f"{len(in_alloc)} in-alloc keepers of {len(keepers)} shown; "
          f"alloc={topo.get('allocation')}")
    check("cluster lists the scenario in its index",
          scenario in (topo.get("scenarios") or []),
          f"scenarios={topo.get('scenarios')}", required=False)
    check("cluster reports chronicles with events",
          len(topo.get("chronicles", [])) >= 1,
          f"{len(topo.get('chronicles', []))} chronicles", required=False)

    # ── Live SSE (inter-agent backlog) ───────────────────────────────────
    print("\n[live SSE stream]")
    try:
        req = urllib.request.Request(
            f"{base}/api/_inter-agent/stream?scenario={scenario}&backlog=1&heartbeat_sec=2")
        frames = []
        with urllib.request.urlopen(req, timeout=12) as r:
            buf = b""
            for _ in range(40):
                chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
                if not chunk:
                    break
                buf += chunk
                if b"event: catchup" in buf:
                    frames.append("catchup")
                    break
        check("SSE stream emits backlog/catchup", "catchup" in frames or b"backlog" in buf,
              required=False)
    except Exception as exc:
        check("SSE stream reachable", False, str(exc), required=False)

    # ── verdict ──────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}  WARN={len(WARN)}")
    if FAIL:
        print("FAILED checks: " + ", ".join(FAIL))
        return 1
    print("ALL REQUIRED CHECKS PASSED" + (f"  ({len(WARN)} warnings)" if WARN else ""))
    return 0


def urllib_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


if __name__ == "__main__":
    sys.exit(main())
