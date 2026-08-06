#!/usr/bin/env python3
"""E2c — Per-view query latency against a populated deployment.

100 timed requests per endpoint, one method (Python perf_counter over urllib)
for all of them. Run against the live dashboard after the E3 traffic (or any
populated state dir in offline mode).

    python3 scripts/eval/bench_queries.py http://<dashboard>:5000 <scenario_id>

Fills: tab:freshness rows "Scenario graph", "Cluster comms", "Fleet health",
"Doctor scan", "Trace reconstruction" (p50; p95 goes to the appendix table).
"""

from __future__ import annotations

import argparse

from _eval_common import (append_summary, print_summary, summarize_ms,
                          timed_http_ms, write_csv)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url")
    ap.add_argument("scenario")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--window", default="15m", help="fleet health window")
    ap.add_argument("--tag", default="", help="suffix for output files (e.g. 'live')")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    endpoints = [
        ("scenario_graph", f"{base}/api/scenarios/{args.scenario}/graph", None),
        ("cluster_comms", f"{base}/api/chronolog/comms", None),
        ("fleet_health", f"{base}/api/fleet/health?window={args.window}", None),
        ("doctor_scan", f"{base}/api/diagnostics/scan", {"diagnose": True, "remember": False}),
        ("trace_reconstruction", f"{base}/api/tracing/{args.scenario}/traces", None),
    ]

    tag = f"_{args.tag}" if args.tag else ""
    rows: list[list] = []
    print(f"[bench_queries] {base} scenario={args.scenario} n={args.n}/endpoint")
    for name, url, body in endpoints:
        try:
            for _ in range(args.warmup):
                timed_http_ms(url, body, timeout=120)
            lat = [timed_http_ms(url, body, timeout=120) for _ in range(args.n)]
        except Exception as exc:
            print(f"  ! {name}: FAILED ({exc}) — skipping")
            continue
        ms = summarize_ms(lat)
        print_summary(name, ms)
        rows.extend([name, i, f"{v:.3f}"] for i, v in enumerate(lat))
        for k in ("p50", "p95", "mean"):
            append_summary({"experiment": "E2c", "metric": f"{name}_{k}",
                            "value": f"{ms[k]:.2f}", "unit": "ms", "n": ms["n"],
                            "notes": f"base={base};scenario={args.scenario};tag={args.tag}"})
    write_csv(f"e2c_query_latency{tag}.csv", ["endpoint", "i", "latency_ms"], rows)


if __name__ == "__main__":
    main()
