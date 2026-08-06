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


def seed(hosts: list[str], turns_per_node: int,
         edge_hosts: list[str] | None = None,
         spread_sec: float = 480.0) -> tuple[int, list[str]]:
    rng = random.Random(7)
    t0 = now() - timedelta(seconds=spread_sec)
    n_nodes = len(hosts)

    # Inter-agent edges are captured on the *caller's* node, so they only exist
    # for nodes that run a collector — i.e. the deployment/allocation nodes. When
    # ``edge_hosts`` is given (a subset of the fleet), the peer-to-peer traffic is
    # confined to it, so the Cluster comms graph shows lines only between real
    # keeper nodes instead of every node in the fleet.
    comms_hosts = edge_hosts or hosts

    # Pick the trouble spots.
    bad_5xx = hosts[n_nodes // 3]                  # error hotspot (fleet health)
    slow = hosts[2 * n_nodes // 3]                 # latency hotspot (fleet health)
    timeout_node = comms_hosts[len(comms_hosts) // 2]  # inter-agent timeout hotspot
    trouble = {bad_5xx, slow, timeout_node}

    events = 0
    for h in hosts:
        for s in range(1, turns_per_node + 1):
            when = t0 + timedelta(seconds=rng.uniform(0, spread_sec))
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

    # Inter-agent timeouts hammering one node — callers are deployment nodes.
    for i in range(40):
        when = t0 + timedelta(seconds=rng.uniform(0, spread_sec))
        edge(SCENARIO, f"agent@{rng.choice(comms_hosts)}", "caller", timeout_node, "callee",
             status="error:peer timed out", latency_ms=rng.uniform(20000, 35000), when=when)
        events += 1

    return events, sorted(trouble)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=100)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--hosts", type=str, default=None,
                    help="comma-separated node names to seed (overrides --nodes); "
                         "use this to mirror a real cluster's node list instead of "
                         "the synthetic ares-comp-000.. range")
    ap.add_argument("--edge-hosts", type=str, default=None,
                    help="comma-separated subset that carries the inter-agent "
                         "traffic (the deployment/allocation nodes with collectors); "
                         "keeps the Cluster comms graph to real keeper nodes. "
                         "Defaults to all --hosts.")
    ap.add_argument("--events", type=int, default=0,
                    help="total raw-event target; overrides --turns "
                         "(turns_per_node = events // nodes). Used by the E3 "
                         "rollup-scaling eval (scripts/eval/bench_rollup_scaling.py).")
    ap.add_argument("--no-rollup", action="store_true",
                    help="seed raw events only — do NOT emit rollup buckets, so "
                         "the Fleet API exercises its raw-fold fallback (E3a).")
    ap.add_argument("--window-min", type=float, default=8.0,
                    help="spread seeded event timestamps over the last N minutes "
                         "(default 8; the E3 eval uses 60)")
    args = ap.parse_args()

    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    else:
        hosts = [f"ares-comp-{i:03d}" for i in range(args.nodes)]

    edge_hosts = None
    if args.edge_hosts:
        edge_hosts = [h.strip() for h in args.edge_hosts.split(",") if h.strip()]

    bootstrap(STATE_DIR)
    import time as _t
    from chronolog_observability.fleet.worker import emit_once
    from chronolog_observability.fleet.store import read_buckets
    from chronolog_observability.fleet import rollup as R

    if args.events:
        args.turns = max(1, args.events // len(hosts))

    h1(f"DEMO 2 — Fleet Health: {len(hosts)} nodes at a glance")
    print(f"  Seeding ~{len(hosts) * args.turns + 40} events across {len(hosts)} nodes")
    print("  (with a 5xx hotspot, a slow node, and a node bleeding peer timeouts)…")
    events, trouble = seed(hosts, args.turns, edge_hosts=edge_hosts,
                           spread_sec=args.window_min * 60.0)

    if args.no_rollup:
        print(f"\n  --no-rollup: seeded {events} raw events; rollup store left "
              f"empty so /api/fleet/health folds from raw.")
        view_hint(STATE_DIR, "fleet")
        return

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
