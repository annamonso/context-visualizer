#!/usr/bin/env python3
"""E1b — Collector POST latency (Path-B fast path).

Times the agent-side round-trip of ``collector.client.emit(event)`` against a
node-local collector daemon. The collector enqueues and replies immediately;
ChronoLog/forward I/O happens on its writer thread — so this measures exactly
what a traced agent pays per edge event.

Two phases:
  1. collector path (the fast path)          → "Collector POST — ms, p50/p95"
  2. fallback path (collector down → direct dashboard POST)
     → the degradation claim in §6.2

Offline by default (no ChronoLog needed; the write side is off the timed path
either way). The dashboard is stood in for by a local HTTP sink unless
--flask-url points at a real one (do that inside the E5 allocation).

    python3 scripts/eval/bench_emit.py
    python3 scripts/eval/bench_emit.py --flask-url http://<dashboard>:5000
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _eval_common import (append_summary, bootstrap_src, http_json,
                          print_summary, summarize_ms, wait_http, write_csv)


def http_json_health(collector_url: str):
    try:
        return http_json(f"{collector_url}/health", timeout=5.0)
    except Exception as exc:
        return f"unavailable ({exc})"


def start_sink() -> tuple[str, ThreadingHTTPServer]:
    """A stand-in dashboard: accepts any POST, replies 200. Counts events."""

    class Sink(BaseHTTPRequestHandler):
        received = 0

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n)
            try:
                evs = json.loads(body or b"{}").get("events", [])
                Sink.received += len(evs) if isinstance(evs, list) else 1
            except json.JSONDecodeError:
                pass
            data = b'{"accepted": 1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def edge_event(scenario: str, host: str, i: int, phase: str = "start") -> dict:
    ev = {
        "scenario_id": scenario,
        "phase": phase,
        "correlation_id": uuid.uuid4().hex,
        "from_host": host,
        "from_session": f"planner@{scenario}",
        "to_host": "peer-node",
        "to_session": f"executor@{scenario}",
        "kind": "mcp_call",
        "tool_name": "call_remote_agent",
        "payload": {"role": "planner", "ask": f"delegate subtask {i} to the executor "
                                              "and report back with a one-line summary"},
    }
    if phase == "done":
        ev["status"] = "ok"
        ev["latency_ms"] = 321.0
    return ev


def bench(emit, n: int, warmup: int, mk, **emit_kw) -> list[float]:
    for i in range(warmup):
        emit(mk(i), **emit_kw)
    lat: list[float] = []
    for i in range(n):
        ev = mk(i)
        t0 = time.perf_counter_ns()
        emit(ev, **emit_kw)
        lat.append((time.perf_counter_ns() - t0) / 1e6)
    return lat


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument("--warmup", type=int, default=100)
    ap.add_argument("--port", type=int, default=5713,
                    help="port for the bench's own collector instance")
    ap.add_argument("--flask-url", default="",
                    help="real dashboard URL for the fallback phase "
                         "(default: local HTTP sink stand-in)")
    ap.add_argument("--n-fallback", type=int, default=2_000,
                    help="fallback phase is a slower path; fewer samples suffice")
    ap.add_argument("--live", action="store_true",
                    help="do NOT force CHRONOLOG_OFFLINE=1 (collector writes ChronoLog)")
    ap.add_argument("--collector-url", default="",
                    help="time against an EXISTING collector (e.g. the node's real "
                         "one at http://127.0.0.1:5650) instead of starting a "
                         "bench-owned daemon — the production fast path")
    args = ap.parse_args()

    if not args.live:
        os.environ["CHRONOLOG_OFFLINE"] = "1"
    bootstrap_src()
    from chronolog_observability.collector.client import emit
    from chronolog_observability.collector.daemon import CollectorDaemon

    host = socket.gethostname()
    scenario = f"eval-emit-{int(time.time())}"
    sink_url, sink_srv = start_sink()
    dashboard = args.flask_url.rstrip("/") or sink_url
    print(f"[bench_emit] host={host} collector_port={args.port} "
          f"dashboard={'REAL ' + dashboard if args.flask_url else 'sink ' + sink_url} "
          f"offline={not args.live}")

    daemon = None
    if args.collector_url:
        collector_url = args.collector_url.rstrip("/")
        print(f"  timing against existing collector {collector_url}")
    else:
        daemon = CollectorDaemon(flask_url=dashboard, port=args.port)
        threading.Thread(target=daemon.run, daemon=True).start()
        collector_url = f"http://127.0.0.1:{args.port}"
    if not wait_http(f"{collector_url}/health", tries=20):
        raise SystemExit(f"collector not reachable at {collector_url}")

    # Phase 1 — the fast path.
    mk = lambda i: edge_event(scenario, host, i)  # noqa: E731
    lat = bench(emit, args.n, args.warmup, mk,
                collector_url=collector_url, flask_url=dashboard)
    ms = summarize_ms(lat)
    print_summary("emit via collector", ms)
    write_csv("e1b_emit_collector.csv", ["i", "latency_ms"],
              [(i, f"{v:.4f}") for i, v in enumerate(lat)])
    notes = f"host={host};dashboard={'real' if args.flask_url else 'sink'}"
    for k in ("p50", "p95", "p99", "mean"):
        append_summary({"experiment": "E1b", "metric": f"emit_collector_{k}",
                        "value": f"{ms[k]:.3f}", "unit": "ms", "n": ms["n"], "notes": notes})

    # Let the writer thread flush, then report pipeline integrity.
    if daemon is not None:
        deadline = time.time() + 15
        while daemon.flushed < daemon.accepted and time.time() < deadline:
            time.sleep(0.2)
        print(f"  collector: accepted={daemon.accepted} flushed={daemon.flushed} "
              f"dropped={daemon.dropped}")
    else:
        health = http_json_health(collector_url)
        print(f"  collector health after run: {health}")

    # Phase 2 — collector down → direct dashboard POST (degradation number).
    dead = "http://127.0.0.1:1"  # nothing listens: instant connection-refused
    lat_fb = bench(emit, args.n_fallback, min(args.warmup, 50), mk,
                   collector_url=dead, flask_url=dashboard)
    ms_fb = summarize_ms(lat_fb)
    print_summary("emit fallback (collector down → dashboard)", ms_fb)
    write_csv("e1b_emit_fallback.csv", ["i", "latency_ms"],
              [(i, f"{v:.4f}") for i, v in enumerate(lat_fb)])
    for k in ("p50", "p95", "p99", "mean"):
        append_summary({"experiment": "E1b", "metric": f"emit_fallback_{k}",
                        "value": f"{ms_fb[k]:.3f}", "unit": "ms", "n": ms_fb["n"],
                        "notes": notes})
    sink_srv.shutdown()


if __name__ == "__main__":
    main()
