#!/usr/bin/env python3
"""E2a — Emit → SSE-frame-at-client latency (the freshness number).

Full pipeline: emit → collector → forward → dashboard ingest → bus → SSE.
Emitter and subscriber run in THIS process (run it on the dashboard node), so
one ``perf_counter_ns`` clock stamps both ends; the send stamp travels inside
the event payload and comes back in the SSE frame's ``payload_preview``.

    python3 scripts/eval/bench_freshness.py --dashboard http://127.0.0.1:5000
    python3 scripts/eval/bench_freshness.py --dashboard ... --direct   # fallback path

By default the bench starts its own collector daemon (port --collector-port)
forwarding to --dashboard, i.e. the production Path-B pipeline; --direct posts
straight to the dashboard ingest for the comparison row.

Fills: Table tab:freshness "Emit → SSE frame at client — p50/p95 ms".
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import urllib.request

from _eval_common import (append_summary, bootstrap_src, print_summary,
                          summarize_ms, wait_http, write_csv)


def sse_subscriber(url: str, out: "queue.Queue[tuple[int, dict]]", stop: threading.Event) -> None:
    """Minimal SSE client: push (recv_perf_ns, event_dict) for every message frame."""
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        event_type = ""
        while not stop.is_set():
            raw = resp.readline()
            if not raw:
                break
            t_recv = time.perf_counter_ns()
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: ") and event_type in ("message", "backlog"):
                try:
                    out.put((t_recv, json.loads(line[6:])))
                except json.JSONDecodeError:
                    pass
            elif not line:
                event_type = ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dashboard", required=True, help="dashboard base URL")
    ap.add_argument("--n", type=int, default=1_000)
    ap.add_argument("--rate", type=float, default=20.0, help="events/sec pacing")
    ap.add_argument("--collector-port", type=int, default=5714)
    ap.add_argument("--collector-url", default="",
                    help="reuse an existing collector instead of starting one")
    ap.add_argument("--direct", action="store_true",
                    help="skip the collector: direct dashboard ingest (fallback path)")
    ap.add_argument("--scenario", default=f"eval-fresh-{int(time.time())}")
    args = ap.parse_args()

    dashboard = args.dashboard.rstrip("/")
    label = "direct" if args.direct else "collector"
    print(f"[bench_freshness] mode={label} dashboard={dashboard} "
          f"n={args.n} rate={args.rate}/s scenario={args.scenario}")

    bootstrap_src()
    from chronolog_observability.collector.client import emit

    collector_url = ""
    if not args.direct:
        if args.collector_url:
            collector_url = args.collector_url.rstrip("/")
        else:
            from chronolog_observability.collector.daemon import CollectorDaemon
            daemon = CollectorDaemon(flask_url=dashboard, port=args.collector_port,
                                     flush_interval=0.5)
            threading.Thread(target=daemon.run, daemon=True).start()
            collector_url = f"http://127.0.0.1:{args.collector_port}"
        if not wait_http(f"{collector_url}/health", tries=20):
            raise SystemExit(f"collector not reachable at {collector_url}")

    stream_url = (f"{dashboard}/api/_inter-agent/stream"
                  f"?scenario={args.scenario}&backlog=0&heartbeat_sec=5")
    frames: "queue.Queue[tuple[int, dict]]" = queue.Queue()
    stop = threading.Event()
    sub = threading.Thread(target=sse_subscriber, args=(stream_url, frames, stop), daemon=True)
    sub.start()
    time.sleep(1.0)  # let the subscription register on the bus

    def send(i: int) -> None:
        t_send = time.perf_counter_ns()
        ev = {
            "scenario_id": args.scenario, "phase": "start",
            "from_host": "eval-emitter", "from_session": f"emitter@{args.scenario}",
            "to_host": "eval-sink", "to_session": f"sink@{args.scenario}",
            "kind": "mcp_call", "tool_name": "freshness_probe",
            "payload": {"i": i, "t_ns": t_send},
        }
        if args.direct:
            req = urllib.request.Request(
                f"{dashboard}/api/_inter-agent/ingest",
                data=json.dumps(ev).encode(), headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        else:
            emit(ev, collector_url=collector_url, flask_url=dashboard)

    period = 1.0 / args.rate if args.rate > 0 else 0.0
    t_next = time.perf_counter()
    for i in range(args.n):
        send(i)
        t_next += period
        lag = t_next - time.perf_counter()
        if lag > 0:
            time.sleep(lag)

    # Collect: every probe frame carries {"i":..,"t_ns":..} in payload_preview.
    lat_ms: dict[int, float] = {}
    deadline = time.time() + 30
    while len(lat_ms) < args.n and time.time() < deadline:
        try:
            t_recv, ev = frames.get(timeout=2.0)
        except queue.Empty:
            continue
        try:
            p = json.loads(ev.get("payload_preview") or "{}")
            i, t_send = int(p["i"]), int(p["t_ns"])
        except (KeyError, ValueError, json.JSONDecodeError):
            continue
        lat_ms.setdefault(i, (t_recv - t_send) / 1e6)
    stop.set()

    lost = args.n - len(lat_ms)
    if lost:
        print(f"  ! {lost}/{args.n} probes never arrived on the stream")
    vals = [lat_ms[i] for i in sorted(lat_ms)]
    if not vals:
        raise SystemExit("no latencies measured — is the dashboard's SSE bus up?")
    ms = summarize_ms(vals)
    print_summary(f"emit→SSE ({label})", ms)
    write_csv(f"e2a_freshness_{label}.csv", ["i", "latency_ms"],
              [(i, f"{lat_ms[i]:.3f}") for i in sorted(lat_ms)])
    notes = f"mode={label};rate={args.rate}/s;lost={lost}"
    for k in ("p50", "p95", "p99", "mean"):
        append_summary({"experiment": "E2a", "metric": f"freshness_{label}_{k}",
                        "value": f"{ms[k]:.2f}", "unit": "ms", "n": ms["n"], "notes": notes})


if __name__ == "__main__":
    main()
