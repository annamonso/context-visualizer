#!/usr/bin/env python3
"""Seed a LARGE synthetic scenario (default: 100 hosts) and serve it.

Benchmark fixture for the cluster-scale UI: the Scenarios tab should
auto-switch to the host-tile grid, the KPI strip should show cluster
totals, and the edge cap should kick in with the "busiest flows" notice.
Runs in CHRONOLOG_OFFLINE=1 mode on its own DTP_STATE_DIR, so it needs no
visor and touches none of the real ~/.dt_provenance data.

    python scripts/demos/seed-scale-demo.py                  # 100 hosts
    python scripts/demos/seed-scale-demo.py --hosts 12       # mid-size cluster
    python scripts/demos/seed-scale-demo.py --hosts 4 --scenario demo-small

Then open  http://<host>:<port>/static/workspace/index.html?tab=scenarios&scenario=demo-scale
"""
import argparse
import json
import os
import random
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("CHRONOLOG_OFFLINE", "1")
os.environ.setdefault("DTP_STATE_DIR", "/tmp/chronolog_scale_demo")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chronolog_observability.app import create_app  # noqa: E402
from chronolog_observability.capture.llm_demo_store import get_store as get_llm_store  # noqa: E402
from chronolog_observability.capture.spool import get_spool  # noqa: E402

TOOLS = ["call_remote_agent", "shard_query", "merge_results", "fetch_chunk", "broadcast_plan"]
MODELS = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
THOUGHTS = [
    "Reviewing prior turn for context handoff.",
    "Identifying which peer to delegate to.",
    "Estimating token budget for the next call.",
    "Drafting the prompt for the downstream agent.",
    "Summarizing shard results before merging.",
]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def seed(client, rng, scenario: str, hosts, sessions, n_pairs: int, err_rate: float) -> int:
    now = datetime.now(timezone.utc)
    sent = 0
    for _ in range(n_pairs):
        # Traffic shape: 30% of calls involve a "coordinator" on the first
        # few hosts, the rest is random peer-to-peer — a realistic mix of
        # hot and quiet flows instead of a uniform mesh.
        if rng.random() < 0.3:
            from_host = hosts[rng.randrange(min(3, len(hosts)))]
        else:
            from_host = rng.choice(hosts)
        to_host = rng.choice(hosts)
        while to_host == from_host and len(hosts) > 1:
            to_host = rng.choice(hosts)

        corr = uuid.uuid4().hex[:16]
        latency = rng.uniform(40, 2500)
        failed = rng.random() < err_rate
        when = now - timedelta(seconds=rng.uniform(0, 600))
        base = {
            "scenario_id": scenario,
            "correlation_id": corr,
            "from_host": from_host,
            "from_session": rng.choice(sessions[from_host]),
            "to_host": to_host,
            "to_session": rng.choice(sessions[to_host]),
            "kind": "mcp_call",
            "tool_name": rng.choice(TOOLS),
        }
        r1 = client.post("/api/_inter-agent/ingest", json={
            **base, "phase": "start", "ts": iso(when),
            "payload": {"req": f"chunk-{rng.randrange(10_000)}"},
        })
        r2 = client.post("/api/_inter-agent/ingest", json={
            **base, "phase": "done",
            "ts": iso(when + timedelta(milliseconds=latency)),
            "latency_ms": round(latency, 1),
            "status": "error:upstream timeout" if failed else "ok",
            "payload": {"rows": rng.randrange(1, 5000)},
        })
        sent += (r1.status_code == 200) + (r2.status_code == 200)
    return sent


def _llm_turn(rng, seq: int, sid: str, role: str, host: str, scenario: str,
              when, kind: str, peer: str | None = None, tool: str | None = None) -> dict:
    """One LLM interaction record in the exact shape the conversation shaper,
    interactions feed, and scenario timeline all consume."""
    out_tokens = rng.randint(60, 240)
    in_tokens = rng.randint(800, 2400) if seq > 1 else rng.randint(120, 320)
    latency = rng.randint(540, 1700)
    cost = round((in_tokens * 3 + out_tokens * 15) / 1_000_000, 5)
    model = rng.choice(MODELS)

    if kind == "system":
        body = {"system": f"You are the {role} agent on {host}.",
                "messages": [{"role": "user", "content": "Coordinate with peers; report back."}]}
        text = f"Acknowledged. I am {role} on {host}. Awaiting tasks."
        tool_calls = []
    elif kind == "tool_use":
        body = {"messages": [{"role": "user", "content": "Issue the cross-agent call."}],
                "tools": [{"name": tool or "call_remote_agent",
                           "input_schema": {"type": "object", "properties": {}}}]}
        text = f"Calling {tool or 'call_remote_agent'} -> {peer}"
        tool_calls = [{"name": tool or "call_remote_agent",
                       "args": {"target": peer, "ask": f"{role} -> {peer}"}}]
    else:  # think
        body = {"messages": [{"role": "user", "content": "Plan the next step toward your goal."}]}
        text = rng.choice(THOUGHTS)
        tool_calls = []

    return {
        "session_id": sid,
        "sequence_id": seq,
        "scenario_id": scenario,
        "host": host,
        "timestamp": iso(when),
        "provider": "anthropic",
        "model": model,
        "request": {"method": "POST", "path": "/v1/messages", "body": json.dumps(body)},
        "response": {"status_code": 200, "text": text,
                     "tool_calls": tool_calls, "is_streaming": False},
        "metrics": {"total_latency_ms": latency,
                    "delta_input_tokens": in_tokens,
                    "delta_output_tokens": out_tokens,
                    "delta_cost_usd": cost},
        "tool_calls": tool_calls,
        # ctx fields the conversation shaper reads off the matching context node
        "_ctx": {"delta_input_tokens": in_tokens, "delta_output_tokens": out_tokens,
                 "delta_cost_usd": cost, "model": model, "latency_ms": latency},
    }


def seed_llm(rng, scenario: str, hosts, sessions, turns_per_agent: int) -> int:
    """Per-agent LLM traces: spool (Workspace + Interactions tabs, offline read
    path) and the LLM demo store (scenario timeline / per-agent KPIs)."""
    spool = get_spool()
    llm_store = get_llm_store()
    now = datetime.now(timezone.utc)
    written = 0
    for host in hosts:
        for sid in sessions[host]:
            role = sid.split("@", 1)[0]
            peer = rng.choice(hosts)
            start = now - timedelta(seconds=rng.uniform(60, 600))
            kinds = ["system"] + ["think"] * max(turns_per_agent - 2, 1) + ["tool_use"]
            for i, kind in enumerate(kinds, start=1):
                when = start + timedelta(seconds=i * rng.uniform(2, 9))
                rec = _llm_turn(rng, i, sid, role, host, scenario, when, kind,
                                peer=peer, tool=rng.choice(TOOLS))
                ctx = rec.pop("_ctx")
                spool.record_interaction(sid, rec, sequence_id=i)
                spool.record_context_node(
                    sid, {**ctx, "timestamp": rec["timestamp"], "op": "add"}, sequence_id=i)
                llm_store.append_interaction(sid, rec)
                written += 1
            llm_store.add_session(sid, {"host": host, "scenario_id": scenario})
    return written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", default="demo-scale")
    p.add_argument("--hosts", type=int, default=100)
    p.add_argument("--agents-per-host", type=int, default=2)
    p.add_argument("--pairs", type=int, default=2000, help="call pairs (2 events each)")
    p.add_argument("--err-rate", type=float, default=0.02)
    p.add_argument("--port", type=int, default=5079)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--llm-turns", type=int, default=4,
                   help="LLM turns per agent (Workspace/Interactions data); 0 disables")
    p.add_argument("--no-serve", action="store_true", help="seed only, don't start the server")
    args = p.parse_args()

    rng = random.Random(args.seed)
    hosts = [f"node-{i:03d}" for i in range(1, args.hosts + 1)]
    # Session ids carry the @scenario scope so the same worker name on the
    # same node stays distinct across the demo scenarios (the UI strips it).
    sessions = {
        h: [f"worker-{h.split('-')[-1]}-{j}@{args.scenario}" for j in range(args.agents_per_host)]
        for h in hosts
    }

    app = create_app()
    sent = seed(app.test_client(), rng, args.scenario, hosts, sessions, args.pairs, args.err_rate)
    print(f"[seed] scenario '{args.scenario}': {sent} events across {len(hosts)} hosts")
    if args.llm_turns > 0:
        n_llm = seed_llm(rng, args.scenario, hosts, sessions, args.llm_turns)
        print(f"[seed] scenario '{args.scenario}': {n_llm} LLM interactions for "
              f"{len(hosts) * args.agents_per_host} agents")

    if args.no_serve:
        return 0
    fqdn = socket.gethostname()
    print(f"[seed] open:  http://{fqdn}:{args.port}/static/workspace/index.html?tab=scenarios&scenario={args.scenario}")
    print(f"[seed] state: {os.environ['DTP_STATE_DIR']}  (delete to reset)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
