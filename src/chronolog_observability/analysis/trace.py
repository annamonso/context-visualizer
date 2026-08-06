"""Distributed critical-path tracing across inter-agent edges. Pure, no I/O.

Each completed inter-agent call (start+done stitched on ``correlation_id``) is a
span with a duration. Spans form call trees: a span is a child of the call its
caller was servicing. The tracer reconstructs those trees and finds the
**critical path** — the root-to-leaf chain with the greatest cumulative latency
— so "why is this scenario slow across 100 nodes?" has a concrete answer instead
of a pile of per-node logs.

Parent links are taken from an explicit ``parent_correlation_id`` when present;
otherwise inferred: span B is a child of span A when B's caller is A's callee
(``B.from_session == A.to_session``) and B starts within A's active window. The
innermost (latest-starting) eligible parent wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..fleet.rollup import parse_ts_ns


@dataclass
class Span:
    correlation_id: str
    parent_id: Optional[str]
    from_host: str
    from_session: str
    to_host: str
    to_session: str
    tool_name: str
    status: str
    start_ns: int
    end_ns: int
    latency_ms: float
    # Started but never completed (no ``done`` frame): the orphan-call
    # pathology. Zero-latency, so it never wins critical-path or self-time
    # math — but it must surface in the waterfall as "open", not as a
    # zero-width "ok" span (which is how it rendered before).
    open: bool = False
    depth: int = 0
    children: List[str] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return self.status.startswith("error")

    def to_dict(self, trace_start_ns: int = 0) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "parent_id": self.parent_id,
            "from_host": self.from_host,
            "from_session": self.from_session,
            "to_host": self.to_host,
            "to_session": self.to_session,
            "tool_name": self.tool_name,
            "status": "open (never completed)" if self.open else (self.status or "ok"),
            "is_error": self.is_error,
            "is_open": self.open,
            "latency_ms": round(self.latency_ms, 1),
            "start_offset_ms": round((self.start_ns - trace_start_ns) / 1e6, 1) if trace_start_ns else 0.0,
            "depth": self.depth,
            "children": self.children,
        }


def _spans_from_stitched(stitched: List[Dict[str, Any]]) -> List[Span]:
    spans: List[Span] = []
    for ev in stitched:
        if not ev.get("ts_start"):
            continue  # need at least a start to place it on the timeline
        start_ns = parse_ts_ns(ev.get("ts_start"))
        lat = float(ev.get("latency_ms") or 0.0)
        end_ns = parse_ts_ns(ev.get("ts_done")) if ev.get("ts_done") else start_ns + int(lat * 1e6)
        spans.append(Span(
            correlation_id=str(ev.get("correlation_id") or ""),
            parent_id=(str(ev.get("parent_correlation_id")) or None) or None,
            from_host=str(ev.get("from_host") or ""),
            from_session=str(ev.get("from_session") or ""),
            to_host=str(ev.get("to_host") or ""),
            to_session=str(ev.get("to_session") or ""),
            tool_name=str(ev.get("tool_name") or ""),
            status=str(ev.get("status") or ""),
            start_ns=start_ns,
            end_ns=max(end_ns, start_ns),
            latency_ms=lat,
            open=not ev.get("ts_done"),
        ))
    return spans


def _infer_parents(spans: List[Span]) -> None:
    """Fill ``parent_id`` for spans lacking an explicit one, by session+time nesting."""
    by_id = {s.correlation_id: s for s in spans}
    for s in spans:
        if s.parent_id and s.parent_id in by_id:
            continue
        s.parent_id = None
        best: Optional[Span] = None
        for cand in spans:
            if cand is s or cand.correlation_id == s.correlation_id:
                continue
            # The caller of s was being driven by cand's callee.
            if cand.to_session and cand.to_session == s.from_session:
                # s must start within cand's active window.
                if cand.start_ns <= s.start_ns <= cand.end_ns:
                    if best is None or cand.start_ns > best.start_ns:
                        best = cand
        s.parent_id = best.correlation_id if best else None


def build_traces(stitched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group spans into trace trees (one per root). Returns a list of traces."""
    spans = _spans_from_stitched(stitched)
    if not spans:
        return []
    _infer_parents(spans)
    by_id = {s.correlation_id: s for s in spans}

    # Wire children + compute depth from roots.
    roots: List[Span] = []
    for s in spans:
        if s.parent_id and s.parent_id in by_id:
            by_id[s.parent_id].children.append(s.correlation_id)
        else:
            roots.append(s)

    def _set_depth(span: Span, depth: int, seen: set) -> None:
        if span.correlation_id in seen:  # guard against cycles
            return
        seen.add(span.correlation_id)
        span.depth = depth
        for cid in span.children:
            _set_depth(by_id[cid], depth + 1, seen)

    for r in roots:
        _set_depth(r, 0, set())

    traces: List[Dict[str, Any]] = []
    for r in roots:
        members = _collect(r, by_id)
        trace_start = min(m.start_ns for m in members)
        trace_end = max(m.end_ns for m in members)
        cp = _critical_path(r, by_id)
        traces.append({
            "root_id": r.correlation_id,
            "scenario_id": "",
            "span_count": len(members),
            "start_ns": trace_start,
            "end_ns": trace_end,
            "wall_ms": round((trace_end - trace_start) / 1e6, 1),
            "error_count": sum(1 for m in members if m.is_error),
            "open_count": sum(1 for m in members if m.open),
            "spans": [m.to_dict(trace_start) for m in sorted(members, key=lambda x: x.start_ns)],
            "critical_path": cp,
        })
    # Slowest trace first.
    traces.sort(key=lambda t: t["wall_ms"], reverse=True)
    return traces


def _collect(root: Span, by_id: Dict[str, Span]) -> List[Span]:
    out: List[Span] = []
    stack = [root]
    seen = set()
    while stack:
        s = stack.pop()
        if s.correlation_id in seen:
            continue
        seen.add(s.correlation_id)
        out.append(s)
        for cid in s.children:
            if cid in by_id:
                stack.append(by_id[cid])
    return out


def _self_time(span: Span, by_id: Dict[str, Span]) -> float:
    """Exclusive (self) time: the span's duration not covered by its slowest child.

    In a call tree the parent is active while its children run, so summing
    latencies double-counts. Self-time = latency minus the longest child's
    latency; along the long-pole chain these self-times sum to the wall-clock.
    """
    child_max = max((by_id[c].latency_ms for c in span.children if c in by_id), default=0.0)
    return max(0.0, span.latency_ms - child_max)


def _critical_path(root: Span, by_id: Dict[str, Span]) -> Dict[str, Any]:
    """Long-pole root-to-leaf chain (descend by slowest child at each level).

    The bottleneck is the chain span with the greatest *self-time* — the hop
    where exclusive wall-clock actually goes, i.e. the one worth optimising.
    """
    chain: List[Span] = []
    node: Optional[Span] = root
    seen: set = set()
    while node is not None and node.correlation_id not in seen:
        seen.add(node.correlation_id)
        chain.append(node)
        kids = [by_id[c] for c in node.children if c in by_id]
        node = max(kids, key=lambda k: k.latency_ms) if kids else None

    trace_start = min((s.start_ns for s in chain), default=0)
    self_times = {s.correlation_id: _self_time(s, by_id) for s in chain}
    bottleneck = max(chain, key=lambda s: self_times[s.correlation_id]) if chain else None

    out_chain: List[Dict[str, Any]] = []
    for s in chain:
        d = s.to_dict(trace_start)
        d["self_time_ms"] = round(self_times[s.correlation_id], 1)
        out_chain.append(d)
    bd = None
    if bottleneck:
        bd = bottleneck.to_dict(trace_start)
        bd["self_time_ms"] = round(self_times[bottleneck.correlation_id], 1)

    return {
        # The root's duration is the trace wall-clock; self-times along this
        # chain sum to it.
        "total_latency_ms": round(root.latency_ms, 1),
        "hops": len(chain),
        "chain": out_chain,
        "bottleneck": bd,
    }


def critical_path(stitched: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The single worst critical path across all traces in the scenario."""
    traces = build_traces(stitched)
    if not traces:
        return {"total_latency_ms": 0.0, "hops": 0, "chain": [], "bottleneck": None}
    worst = max(traces, key=lambda t: t["critical_path"]["total_latency_ms"])
    cp = dict(worst["critical_path"])
    cp["root_id"] = worst["root_id"]
    cp["wall_ms"] = worst["wall_ms"]
    return cp
