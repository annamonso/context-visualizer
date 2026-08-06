#!/usr/bin/env python3
"""E1d helper — paced Path-B load through a node-local collector.

Emits start+done edge pairs at a fixed rate through the REAL collector on this
node, so the sidecar-footprint sampling (sample_sidecars.py) measures the
production pipeline under a known offered load.

    python3 scripts/eval/steady_emit.py --rate 20 --duration 720 \
        --scenario eval-live --collector-url http://127.0.0.1:5650
"""

from __future__ import annotations

import argparse
import socket
import time
import uuid

from _eval_common import bootstrap_src


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collector-url", default="http://127.0.0.1:5650")
    ap.add_argument("--dashboard-url", default="", help="fallback ingest")
    ap.add_argument("--rate", type=float, default=20.0, help="events/sec (pairs count as 2)")
    ap.add_argument("--duration", type=float, default=720.0)
    ap.add_argument("--scenario", default="eval-live")
    ap.add_argument("--peer-host", default="", help="to_host on emitted edges")
    args = ap.parse_args()

    bootstrap_src()
    from chronolog_observability.collector.client import emit

    host = socket.gethostname()
    peer = args.peer_host or host
    period = 2.0 / args.rate  # one iteration = start+done = 2 events
    sent = errors = 0
    t_end = time.time() + args.duration
    t_next = time.perf_counter()
    print(f"[steady_emit] {args.rate} ev/s for {args.duration:.0f}s via "
          f"{args.collector_url} (scenario={args.scenario})", flush=True)
    while time.time() < t_end:
        corr = uuid.uuid4().hex
        base = {"scenario_id": args.scenario, "correlation_id": corr,
                "from_host": host, "from_session": f"loadgen@{args.scenario}",
                "to_host": peer, "to_session": f"sink@{args.scenario}",
                "kind": "mcp_call", "tool_name": "steady_probe"}
        try:
            emit(dict(base, phase="start"),
                 collector_url=args.collector_url,
                 flask_url=args.dashboard_url or None)
            emit(dict(base, phase="done", status="ok", latency_ms=250.0),
                 collector_url=args.collector_url,
                 flask_url=args.dashboard_url or None)
            sent += 2
        except Exception:
            errors += 1
        t_next += period
        lag = t_next - time.perf_counter()
        if lag > 0:
            time.sleep(lag)
    print(f"[steady_emit] done: sent={sent} errors={errors} "
          f"actual_rate={sent/args.duration:.1f} ev/s", flush=True)


if __name__ == "__main__":
    main()
