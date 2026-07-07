"""Unit tests for Fleet rollup aggregation (pure)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.fleet.rollup import (  # noqa: E402
    bucketize,
    health_from_raw,
    minute_key,
    parse_ts_ns,
)


def test_parse_ts_handles_iso_and_short_fractions():
    a = parse_ts_ns("2026-06-22T10:00:00.000000Z")
    b = parse_ts_ns("2026-06-22T10:00:00.6Z")  # single-digit fraction (3.10-safe)
    assert a > 0 and b > a
    assert parse_ts_ns("garbage") == 0


def test_minute_key_aligns_to_minute():
    ns = parse_ts_ns("2026-06-22T10:05:37.000000Z")
    assert minute_key(ns) % 60 == 0


def test_bucketize_groups_by_host_and_minute():
    inter = [
        {"host": "h1", "session_id": "a", "timestamp": "2026-06-22T10:00:10.000000Z",
         "response": {"status_code": 200}, "metrics": {"total_latency_ms": 100}},
        {"host": "h1", "session_id": "a", "timestamp": "2026-06-22T10:00:50.000000Z",
         "response": {"status_code": 500}, "metrics": {"total_latency_ms": 9000}},
        {"host": "h1", "session_id": "a", "timestamp": "2026-06-22T10:01:10.000000Z",
         "response": {"status_code": 200}, "metrics": {"total_latency_ms": 120}},
    ]
    buckets = bucketize(inter, [])
    # Two distinct minutes for h1.
    assert len(buckets) == 2
    first = [b for b in buckets if b.minute == minute_key(parse_ts_ns("2026-06-22T10:00:00.000000Z"))][0]
    assert first.events == 2
    assert first.errors == 1


def test_health_flags_bad_node_crit():
    inter = []
    for i in range(10):
        inter.append({"host": "good", "session_id": "g", "timestamp": "2026-06-22T10:00:%02d.000000Z" % i,
                      "response": {"status_code": 200}, "metrics": {"total_latency_ms": 200}})
        inter.append({"host": "bad", "session_id": "b", "timestamp": "2026-06-22T10:00:%02d.000000Z" % i,
                      "response": {"status_code": 500}, "metrics": {"total_latency_ms": 9000}})
    health = health_from_raw(inter, [])
    assert health["good"].status == "ok"
    assert health["bad"].status == "crit"
    assert health["bad"].error_rate == 1.0


def test_edges_attributed_to_callee_host():
    edges = [{"to_host": "h2", "to_session": "x", "ts_done": "2026-06-22T10:00:01.000000Z",
              "status": "error:timeout", "latency_ms": 8000}]
    health = health_from_raw([], edges)
    assert "h2" in health
    assert health["h2"].errors == 1
