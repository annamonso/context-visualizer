#!/usr/bin/env python3
"""Synthetic live inter-agent traffic — exercise the live views with NO LLM cost.

POSTs ``start``/``done`` inter-agent edges to the dashboard ingest endpoint,
which persists them to ChronoLog *and* fans them out to the in-process SSE bus
(``/api/_inter-agent/stream``) the live view consumes. With ``--spool`` it also
writes Path-A LLM-interaction + context-graph events into the shared capture
spool, so the Interactions feed and the agent memory/context views populate too.

Agent model:
  * ``--hosts`` is the comma list of nodes.
  * ``--agents N`` spreads N agents across those nodes (round-robin). Passing
    MORE agents than hosts puts 2-3 agents on a node — handy for seeing how the
    Scenarios "Agents" view groups several agents under one host, and how the
    Workspace host view merges them.
  * Every agent gets a UNIQUE session id (``role@scenario``, suffixed when a role
    repeats), so N agents really render as N distinct nodes/agents — no
    collision like the old role-cycling version.

Examples:
    # one distinct agent per node (8 nodes -> 8 agents):
    python scripts/synth_live_traffic.py --flask-url http://HOST:5000 \\
        --scenario demo-8node --hosts h1,h2,...,h8 --rounds 40 --spool

    # 12 agents over 8 nodes -> some nodes carry 2 agents:
    python scripts/synth_live_traffic.py --flask-url http://HOST:5000 \\
        --scenario demo-multi --hosts h1,...,h8 --agents 12 --rounds 40 --spool
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _post(url: str, body: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        # Tolerate transient dashboard restarts so a long-running emitter
        # survives a redeploy — a dropped synthetic edge is acceptable.
        return {}


DEFAULT_HOSTS = ["ares-comp-03", "ares-comp-04", "ares-comp-05", "ares-comp-06"]
ROLE_POOL = [
    "planner", "executor", "fetcher", "summarizer",
    "retriever", "analyst", "validator", "coordinator",
]
TOOLS = ["call_remote_agent", "shard_query", "merge_results", "fetch_chunk", "broadcast_plan"]
MODELS = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
THOUGHTS = [
    "Reviewing prior turn for context handoff.",
    "Selecting which peer to delegate to.",
    "Estimating token budget for the downstream call.",
    "Merging shard results before summarising.",
    "Checking the retrieved chunk against the plan.",
]


def agent_role(n: int) -> str:
    """Unique, readable role label for agent #n (planner, …, coordinator, planner2, …)."""
    base = ROLE_POOL[n % len(ROLE_POOL)]
    rep = n // len(ROLE_POOL)
    return base if rep == 0 else f"{base}{rep + 1}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flask-url", required=True, help="dashboard base URL, e.g. http://ares-comp-10:5000")
    ap.add_argument("--scenario", default="demo-live")
    ap.add_argument("--hosts", default="", help="comma list of nodes (default: a 4-node set)")
    ap.add_argument("--agents", type=int, default=0, help="number of agents to spread across the hosts (default: one per host)")
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--interval", type=float, default=1.5, help="seconds between rounds")
    ap.add_argument("--err-rate", type=float, default=0.12)
    ap.add_argument("--spool", action="store_true", help="also write Path-A interactions + context to the capture spool")
    ap.add_argument("--state-dir", default=os.environ.get("DTP_STATE_DIR", ""))
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()] or list(DEFAULT_HOSTS)
    n_agents = args.agents if args.agents > 0 else len(hosts)
    if n_agents < 2:
        print("need at least 2 agents", file=sys.stderr)
        return 2

    # Build agents with UNIQUE session ids, round-robin across the hosts.
    agents = [
        {"sid": f"{agent_role(n)}@{args.scenario}", "role": agent_role(n), "host": hosts[n % len(hosts)]}
        for n in range(n_agents)
    ]

    ingest = f"{args.flask_url.rstrip('/')}/api/_inter-agent/ingest"

    spool = None
    if args.spool:
        if args.state_dir:
            os.environ["DTP_STATE_DIR"] = args.state_dir
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        from chronolog_observability.capture.spool import get_spool  # noqa: E402
        spool = get_spool()

    rng = random.Random(args.seed)
    seq = {a["sid"]: 0 for a in agents}
    coordinator = agents[0]
    edges = errors = 0

    per_host = Counter(a["host"] for a in agents)
    multi = {h: c for h, c in per_host.items() if c > 1}
    print(f"[synth] scenario={args.scenario}  agents={n_agents} across {len(hosts)} nodes  spool={bool(spool)}")
    print(f"[synth] per-node agent counts: {dict(per_host)}"
          + (f"   (multi-agent nodes: {multi})" if multi else ""))

    for r in range(args.rounds):
        # 35% coordinator -> peer, else random peer -> peer (distinct agents).
        if rng.random() < 0.35:
            fa = coordinator
        else:
            fa = rng.choice(agents)
        ta = rng.choice([a for a in agents if a is not fa])

        cid = uuid.uuid4().hex
        tool = rng.choice(TOOLS)
        base = dict(
            scenario_id=args.scenario, correlation_id=cid, kind="mcp_call", tool_name=tool,
            from_host=fa["host"], from_session=fa["sid"],
            to_host=ta["host"], to_session=ta["sid"],
        )

        _post(ingest, dict(base, phase="start", payload={"ask": f"{fa['role']}->{ta['role']}:{tool}", "round": r + 1}))
        edges += 1
        time.sleep(min(0.5, args.interval / 3))

        err = rng.random() < args.err_rate
        if err:
            errors += 1
        _post(ingest, dict(
            base, phase="done",
            status=("error:peer timed out" if err else "ok"),
            latency_ms=round(rng.uniform(80, 1900), 1),
            payload={"result": "merged shards" if not err else "no response"},
        ))

        if spool is not None:
            for a in (fa, ta):
                seq[a["sid"]] += 1
                s = seq[a["sid"]]
                spool.record_interaction(a["sid"], {
                    "provider": "anthropic",
                    "model": rng.choice(MODELS),
                    "method": "POST", "path": "/v1/messages",
                    "status_code": 500 if (err and a is ta) else 200,
                    "is_streaming": True,
                    "total_latency_ms": round(rng.uniform(200, 2400), 1),
                    "request": {"messages": [
                        {"role": "system", "content": f"You are the {a['role']} agent on {a['host']}."},
                        {"role": "user", "content": f"[turn {s}] {rng.choice(THOUGHTS)}"},
                    ]},
                    "response_text": f"{a['role']} step {s}: {rng.choice(THOUGHTS)}",
                    "usage": {"input_tokens": rng.randint(400, 4000), "output_tokens": rng.randint(40, 600)},
                }, sequence_id=s)
                spool.record_context_node(a["sid"], {
                    "op": rng.choice(["add", "add", "add", "evict"]),
                    "node": f"ctx-{s}",
                    "summary": f"{a['role']}: {rng.choice(THOUGHTS)}",
                    "tokens": rng.randint(50, 600),
                }, sequence_id=s)

        print(f"  round {r+1}/{args.rounds}: {fa['role']}@{fa['host']} -> {ta['role']}@{ta['host']} "
              f"{tool} {'ERR' if err else 'ok'}")
        time.sleep(args.interval)

    print(f"[synth] done: {edges} edges ({errors} errors), {n_agents} agents, scenario={args.scenario}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
