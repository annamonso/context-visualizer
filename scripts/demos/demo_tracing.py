#!/usr/bin/env python3
"""DEMO 3 — Distributed critical-path tracing: find the bottleneck in a mesh.

Seeds a realistic distributed request that fans out across six hosts: an
orchestrator calls a retriever, which calls a vector-db node and a (slow) web
fetcher in parallel, then a ranker and a summarizer. Parent links are emitted
explicitly so the tracer reconstructs the exact call tree, then it computes the
**critical path** — the slowest root-to-leaf chain — and names the single
bottleneck hop.

    python3 scripts/demos/demo_tracing.py

The lesson: across many nodes, total wall-clock is set by ONE chain. Per-node
logs can't show it; the trace can. Fully offline.
"""

import os
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _demo_common import bootstrap, color, edge, h1, h2, now, view_hint  # noqa: E402

STATE_DIR = os.environ.get("DEMO_STATE_DIR", "/tmp/chronolog_demo_tracing")
SCENARIO = "trace-demo"


def seed() -> None:
    t0 = now() - timedelta(minutes=1)
    H = {  # role -> host
        "orchestrator": "ares-comp-01", "retriever": "ares-comp-02",
        "vectordb": "ares-comp-03", "webfetch": "ares-comp-04",
        "ranker": "ares-comp-05", "summarizer": "ares-comp-06",
    }

    def sid(role: str) -> str:
        return f"{role}@{SCENARIO}"

    # Root: orchestrator -> retriever (the whole request), 9.2s wall.
    root = uuid.uuid4().hex
    edge(SCENARIO, H["orchestrator"], sid("orchestrator"), H["retriever"], sid("retriever"),
         tool="call_remote_agent", status="ok", latency_ms=9200, when=t0, correlation_id=root)

    # retriever fans out to vectordb (fast) and webfetch (SLOW — the bottleneck).
    edge(SCENARIO, H["retriever"], sid("retriever"), H["vectordb"], sid("vectordb"),
         tool="shard_query", status="ok", latency_ms=600,
         when=t0 + timedelta(milliseconds=200), parent_corr=root)
    edge(SCENARIO, H["retriever"], sid("retriever"), H["webfetch"], sid("webfetch"),
         tool="fetch_chunk", status="ok", latency_ms=6800,   # <-- bottleneck hop
         when=t0 + timedelta(milliseconds=300), parent_corr=root)

    # retriever -> ranker (after the fetch), ranker -> summarizer.
    rank = uuid.uuid4().hex
    edge(SCENARIO, H["retriever"], sid("retriever"), H["ranker"], sid("ranker"),
         tool="merge_results", status="ok", latency_ms=1500,
         when=t0 + timedelta(milliseconds=7300), parent_corr=root, correlation_id=rank)
    edge(SCENARIO, H["ranker"], sid("ranker"), H["summarizer"], sid("summarizer"),
         tool="broadcast_plan", status="ok", latency_ms=900,
         when=t0 + timedelta(milliseconds=7500), parent_corr=rank)


def waterfall(trace) -> None:
    crit = {s["correlation_id"] for s in trace["critical_path"]["chain"]}
    total = max(1.0, trace["wall_ms"])
    width = 44
    print(color(f"    {'hop':<34}{'timeline':<46}{'lat':>7}", "dim"))
    for s in trace["spans"]:
        label = f"{'  ' * s['depth']}{s['tool_name']}→{s['to_host'].split('-')[-1]}"
        left = int((s["start_offset_ms"] / total) * width)
        bar_len = max(1, int((s["latency_ms"] / total) * width))
        on_crit = s["correlation_id"] in crit
        bar = " " * left + ("█" * bar_len)
        bar = bar[:width].ljust(width)
        bar = color(bar, "red" if on_crit else "blue")
        marker = color(" ◀ critical", "red") if on_crit else ""
        print(f"    {label:<34}{bar}{s['latency_ms']/1000:>6.1f}s{marker}")


def main() -> None:
    bootstrap(STATE_DIR)
    from chronolog_observability.analysis.trace import build_traces, critical_path

    h1("DEMO 3 — Distributed critical-path tracing")
    print("  Seeding one request that fans out across 6 hosts (orchestrator →")
    print("  retriever → {vectordb, webfetch} → ranker → summarizer)…")
    seed()

    traces = build_traces(__import__("chronolog_observability.capture.inter_agent",
                                     fromlist=["get_store"]).get_store().read_stitched(SCENARIO))
    if not traces:
        print(color("  no traces built", "red"))
        return
    trace = traces[0]

    h2(f"Reconstructed call tree — {trace['span_count']} spans, "
       f"{trace['wall_ms']/1000:.1f}s wall-clock")
    waterfall(trace)

    cp = critical_path(
        __import__("chronolog_observability.capture.inter_agent",
                   fromlist=["get_store"]).get_store().read_stitched(SCENARIO))
    h2("Critical path (what actually sets the wall-clock)")
    chain = " → ".join(f"{s['tool_name']}→{s['to_host'].split('-')[-1]}({s['latency_ms']/1000:.1f}s)"
                       for s in cp["chain"])
    print(f"    {chain}")
    total_s = "%.1fs" % (cp["total_latency_ms"] / 1000)
    print(f"    total: {color(total_s, 'bold')} across {cp['hops']} hops")
    b = cp["bottleneck"]
    if b:
        b_s = "%.1fs" % (b["latency_ms"] / 1000)
        print(f"    {color('bottleneck:', 'red')} {b['tool_name']} → {b['to_host']} "
              f"({color(b_s, 'red')}) — optimise THIS hop to shrink the whole request")

    view_hint(STATE_DIR, "traces", f"&trace={SCENARIO}")


if __name__ == "__main__":
    main()
