"""Unit tests for distributed critical-path tracing (pure)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chronolog_observability.analysis.trace import build_traces, critical_path  # noqa: E402


def _nested_scenario():
    """root (9.2s) -> {vectordb 0.6s, webfetch 6.8s} ; ranker 1.5s -> summarizer 0.9s."""
    return [
        {"correlation_id": "root", "parent_correlation_id": "", "from_session": "orch@s",
         "to_session": "retr@s", "from_host": "h1", "to_host": "h2", "tool_name": "call_remote_agent",
         "ts_start": "2026-06-22T10:00:00.000000Z", "ts_done": "2026-06-22T10:00:09.200000Z",
         "latency_ms": 9200, "status": "ok"},
        {"correlation_id": "vdb", "parent_correlation_id": "root", "from_session": "retr@s",
         "to_session": "vdb@s", "from_host": "h2", "to_host": "h3", "tool_name": "shard_query",
         "ts_start": "2026-06-22T10:00:00.200000Z", "ts_done": "2026-06-22T10:00:00.800000Z",
         "latency_ms": 600, "status": "ok"},
        {"correlation_id": "web", "parent_correlation_id": "root", "from_session": "retr@s",
         "to_session": "web@s", "from_host": "h2", "to_host": "h4", "tool_name": "fetch_chunk",
         "ts_start": "2026-06-22T10:00:00.300000Z", "ts_done": "2026-06-22T10:00:07.100000Z",
         "latency_ms": 6800, "status": "ok"},
        {"correlation_id": "rank", "parent_correlation_id": "root", "from_session": "retr@s",
         "to_session": "rank@s", "from_host": "h2", "to_host": "h5", "tool_name": "merge_results",
         "ts_start": "2026-06-22T10:00:07.300000Z", "ts_done": "2026-06-22T10:00:08.800000Z",
         "latency_ms": 1500, "status": "ok"},
        {"correlation_id": "sum", "parent_correlation_id": "rank", "from_session": "rank@s",
         "to_session": "sum@s", "from_host": "h5", "to_host": "h6", "tool_name": "broadcast_plan",
         "ts_start": "2026-06-22T10:00:07.500000Z", "ts_done": "2026-06-22T10:00:08.400000Z",
         "latency_ms": 900, "status": "ok"},
    ]


def test_explicit_parents_build_one_tree():
    traces = build_traces(_nested_scenario())
    assert len(traces) == 1
    assert traces[0]["span_count"] == 5
    assert traces[0]["root_id"] == "root"


def test_inferred_parents_when_no_explicit_link():
    # Strip explicit parents; tracer must infer from session + time nesting.
    spans = _nested_scenario()
    for s in spans:
        s["parent_correlation_id"] = ""
    traces = build_traces(spans)
    # vdb/web/rank are children of root (retr@s caller == root callee), sum child of rank.
    assert len(traces) == 1
    assert traces[0]["span_count"] == 5


def test_critical_path_bottleneck_is_fetch_by_self_time():
    cp = critical_path(_nested_scenario())
    # Wall-clock = root duration, not the double-counted sum.
    assert cp["total_latency_ms"] == 9200.0
    assert cp["bottleneck"] is not None
    # The long pole at the root is the 6.8s fetch_chunk hop.
    assert cp["bottleneck"]["tool_name"] == "fetch_chunk"
    assert cp["bottleneck"]["self_time_ms"] == 6800.0


def test_empty_scenario_is_safe():
    cp = critical_path([])
    assert cp["hops"] == 0
    assert cp["bottleneck"] is None
