"""Unit tests for ChronoDoctor's pure layers: detectors, clustering, memory.

These exercise the deterministic core with no I/O, so they run anywhere with no
cluster and no API keys — the same property that makes the demo reproducible.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from backend.diagnostics.detectors import (  # noqa: E402
    detect_all,
    detect_orphan_calls,
    detect_retry_storms,
    normalize_message,
)
from backend.diagnostics.incidents import cluster_signals  # noqa: E402
from backend.diagnostics.diagnoser import HeuristicDiagnoser  # noqa: E402
from backend.diagnostics.memory import IncidentMemory  # noqa: E402


def test_normalize_message_collapses_numbers_and_ids():
    a = normalize_message("peer ares-comp-12 timed out after 30001ms")
    b = normalize_message("peer ares-comp-07 timed out after 5004ms")
    assert a == b
    assert "<n>" in a


def test_detect_interaction_errors_flags_5xx_and_429():
    recs = [
        {"session_id": "a", "host": "h1", "model": "m", "sequence_id": 1,
         "response": {"status_code": 500, "text": "boom"}, "metrics": {"total_latency_ms": 10}},
        {"session_id": "a", "host": "h1", "model": "m", "sequence_id": 2,
         "response": {"status_code": 429, "text": "rate limit"}, "metrics": {"total_latency_ms": 5}},
        {"session_id": "a", "host": "h1", "model": "m", "sequence_id": 3,
         "response": {"status_code": 200, "text": "ok"}, "metrics": {"total_latency_ms": 5}},
    ]
    sigs = detect_all(recs, [], [])
    kinds = {s.kind for s in sigs}
    assert "http_error" in kinds
    assert "rate_limit" in kinds


def test_detect_orphan_calls():
    stitched = [
        {"correlation_id": "c1", "ts_start": "t", "ts_done": None, "to_host": "h9",
         "to_session": "x", "scenario_id": "s", "tool_name": "call_remote_agent"},
        {"correlation_id": "c2", "ts_start": "t", "ts_done": "t2", "status": "ok",
         "to_host": "h9", "scenario_id": "s"},
    ]
    sigs = detect_orphan_calls(stitched)
    assert len(sigs) == 1
    assert sigs[0].kind == "orphan_call"
    assert sigs[0].severity == "critical"


def test_detect_retry_storm():
    recs = [
        {"session_id": "a", "sequence_id": i,
         "request": {"body": {"messages": [{"role": "user", "content": "same prompt"}]}},
         "response": {"status_code": 200}, "metrics": {"total_latency_ms": 1}}
        for i in range(1, 6)
    ]
    sigs = detect_retry_storms(recs, min_repeats=3)
    assert len(sigs) == 1
    assert sigs[0].kind == "retry_storm"


def test_clustering_collapses_identical_failures_across_hosts():
    # 50 timeouts across 10 hosts -> ONE incident with count 50.
    stitched = [
        {"correlation_id": f"c{i}", "ts_start": "t", "ts_done": "t2",
         "status": f"error:peer timed out after {1000+i}ms", "latency_ms": 1000 + i,
         "to_host": f"h{i % 10}", "scenario_id": "s", "tool_name": "call_remote_agent"}
        for i in range(50)
    ]
    sigs = detect_all([], stitched, [])
    edge_sigs = [s for s in sigs if s.kind == "edge_error"]
    incidents = cluster_signals(edge_sigs)
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.count == 50
    assert len(inc.hosts) == 10
    assert inc.severity == "critical"  # escalated by volume


def test_heuristic_diagnoser_returns_actionable_fields():
    d = HeuristicDiagnoser()
    inc = {"kind": "rate_limit", "title": "Rate-limited LLM calls: rate limit exceeded",
           "samples": [{"message": "rate limit exceeded"}], "count": 12,
           "hosts": ["h1"], "sessions": ["a"], "scenarios": ["s"]}
    out = d.diagnose(inc)
    assert out["root_cause"]
    assert out["remediation"]
    assert 0 <= out["confidence"] <= 1
    assert out["source"] == "heuristic"


def test_incident_memory_compacts_and_tracks_counts(tmp_path):
    mem = IncidentMemory(tmp_path / "mem.md", compact_at=5, keep_recent=2)
    for _ in range(10):
        mem.record([{"severity": "error", "kind": "edge_error", "count": 3,
                     "title": "peer timed out", "hosts": ["h1"]}])
    doc = mem.render()
    assert "Digest" in doc  # compaction happened
    known = mem.known_signatures()
    # 10 records x count 3, minus the 2 kept in Recent x 3 = 24 in the digest.
    assert known.get("[edge_error] peer timed out", 0) >= 20
