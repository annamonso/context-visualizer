#!/usr/bin/env python3
"""DEMO 2 — Fleet Health at scale: 100 nodes at a glance.

Seeds traffic across a configurable fleet (default 100 nodes) with a few
injected trouble spots (a node throwing 5xx, a slow node, a node bleeding
inter-agent timeouts), then:

  1. emits per-(host, minute) rollup buckets to the metrics_rollup store, and
  2. reads them back through the SAME Fleet API the dashboard uses,

to show that summarising 100 nodes is O(nodes x buckets) — independent of how
many raw events flowed. Prints the fleet summary and floats the worst nodes to
the top, exactly as the heatmap does.

    python3 scripts/demos/demo_fleet_scale.py                 # 100 nodes
    python3 scripts/demos/demo_fleet_scale.py --nodes 250     # push it harder

Fully offline — no cluster needed to demo the visualisation. On ARES, run real
agents across N allocation nodes and the same view renders the live fleet.
"""

import argparse
import os
import random
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _demo_common import bootstrap, color, edge, h1, h2, now, turn, view_hint  # noqa: E402

STATE_DIR = os.environ.get("DEMO_STATE_DIR", "/tmp/chronolog_demo_fleet")
SCENARIO = "fleet-demo"


def seed(n_nodes: int, turns_per_node: int) -> tuple[int, list[str]]:
    rng = random.Random(7)
    t0 = now() - timedelta(minutes=8)
    hosts = [f"ares-comp-{i:03d}" for i in range(n_nodes)]

    # Pick the trouble spots.
    bad_5xx = hosts[n_nodes // 3]          # error hotspot
    slow = hosts[2 * n_nodes // 3]         # latency hotspot
    timeout_node = hosts[n_nodes // 7]     # inter-agent timeout hotspot
    trouble = {bad_5xx, slow, timeout_node}

    events = 0
    for h in hosts:
        for s in range(1, turns_per_node + 1):
            when = t0 + timedelta(seconds=rng.uniform(0, 480))
            if h == bad_5xx and rng.random() < 0.6:
                turn(f"agent@{h}", s, host=h, scenario=SCENARIO, status_code=500,
                     text="internal error", latency_ms=rng.uniform(4000, 9000), when=when)
            elif h == slow:
                turn(f"agent@{h}", s, host=h, scenario=SCENARIO, status_code=200,
                     text="ok", latency_ms=rng.uniform(8000, 16000), when=when)
            else:
                turn(f"agent@{h}", s, host=h, scenario=SCENARIO, status_code=200,
                     text="ok", latency_ms=rng.uniform(120, 1200), when=when,
                     in_tok=rng.randint(300, 3000), out_tok=rng.randint(40, 400),
                     cost=round(rng.uniform(0.0005, 0.004), 5))
            events += 1

    # Inter-agent timeouts hammering one node.
    for i in range(40):
        when = t0 + timedelta(seconds=rng.uniform(0, 480))
        edge(SCENARIO, f"agent@{rng.choice(hosts)}", "caller", timeout_node, "callee",
             status="error:peer timed out", latency_ms=rng.uniform(20000, 35000), when=when)
        events += 1

    return events, sorted(trouble)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=100)
    ap.add_argument("--turns", type=int, default=8)
    args = ap.parse_args()

    bootstrap(STATE_DIR)
    import time as _t
    from chronolog_observability.fleet.worker import emit_once
    from chronolog_observability.fleet.store import read_buckets
    from chronolog_observability.fleet import rollup as R

    h1(f"DEMO 2 — Fleet Health: {args.nodes} nodes at a glance")
    print(f"  Seeding ~{args.nodes * args.turns + 40} events across {args.nodes} nodes")
    print("  (with a 5xx hotspot, a slow node, and a node bleeding peer timeouts)…")
    events, trouble = seed(args.nodes, args.turns)

    h2("Roll up raw events into per-(host, minute) buckets")
    t = _t.perf_counter()
    written = emit_once()
    print(f"  emitted {written} rollup buckets from {events} raw events "
          f"in {(_t.perf_counter()-t)*1000:.0f}ms")

    h2("Read the fleet through the rollup store (O(nodes × buckets), not O(events))")
    t = _t.perf_counter()
    buckets = read_buckets()
    health = R.health_from_buckets(buckets, now_ns=now().timestamp().__int__() * 1_000_000_000)
    elapsed = (_t.perf_counter() - t) * 1000
    nodes = list(health.values())
    crit = [n for n in nodes if n.status == "crit"]
    warn = [n for n in nodes if n.status == "warn"]
    print(f"  summarised {len(nodes)} nodes from {len(buckets)} buckets in {elapsed:.0f}ms  "
          f"→ {color(str(len(crit))+' crit', 'red')}, {color(str(len(warn))+' warn', 'yellow')}, "
          f"{len(nodes)-len(crit)-len(warn)} ok")

    h2("Worst nodes (what the heatmap floats to the top)")
    nodes.sort(key=lambda n: ({"crit": 0, "warn": 1, "ok": 2}[n.status], -n.error_rate, -n.p95_latency_ms))
    print(color(f"    {'host':<18}{'status':<8}{'err%':>7}{'p95(s)':>9}{'events':>8}{'cost$':>9}", "dim"))
    for n in nodes[:8]:
        sc = "red" if n.status == "crit" else "yellow" if n.status == "warn" else "green"
        print(f"    {n.host:<18}{color(n.status,sc):<16}{n.error_rate*100:>6.0f}%"
              f"{n.p95_latency_ms/1000:>9.1f}{n.events:>8}{n.cost_usd:>9.3f}")
    print(color(f"\n  injected trouble spots were: {', '.join(trouble)}", "dim"))

    view_hint(STATE_DIR, "fleet")


if __name__ == "__main__":
    main()
