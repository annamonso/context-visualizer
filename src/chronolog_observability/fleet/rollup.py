"""Pure aggregation for Fleet Health. No I/O — trivially unit-testable.

A ``Bucket`` is one (host, minute) cell holding pre-summed counters. Buckets are
the on-the-wire rollup unit: emitting and reading them is O(nodes x buckets),
independent of how many raw events occurred — that is the scalability property
the Fleet view depends on.

``NodeHealth`` is the per-node summary the heatmap renders, folded from the
buckets in a time window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts_ns(ts: Any) -> int:
    """Parse an ISO-8601 timestamp (or epoch ns/seconds) into epoch nanoseconds.

    Tolerant: returns 0 on anything unparseable so a bad record can't crash a
    rollup.
    """
    if ts is None or ts == "":
        return 0
    if isinstance(ts, (int, float)):
        v = float(ts)
        # Heuristic: > 1e17 already ns; ~1e9 seconds; ~1e12 ms.
        if v > 1e16:
            return int(v)
        if v > 1e11:
            return int(v * 1e6)
        return int(v * 1e9)
    s = str(ts).strip().replace("Z", "+00:00")
    # Python 3.10's fromisoformat only accepts 3- or 6-digit fractional seconds;
    # pad/truncate so single- or odd-digit fractions (e.g. "...04.6+00:00") parse.
    import re as _re

    m = _re.search(r"\.(\d+)", s)
    if m:
        frac = m.group(1)
        fixed = (frac + "000000")[:6]
        s = s[: m.start()] + "." + fixed + s[m.end():]
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except ValueError:
        return 0


def minute_key(ts_ns: int) -> int:
    """Truncate an epoch-ns timestamp to the start of its minute (epoch seconds)."""
    return (ts_ns // 1_000_000_000) // 60 * 60


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


# ---------------------------------------------------------------------------
# Bucket
# ---------------------------------------------------------------------------

@dataclass
class Bucket:
    host: str
    minute: int                      # epoch seconds, minute-aligned
    events: int = 0
    errors: int = 0
    latency_sum: float = 0.0
    latency_count: int = 0
    latency_p95: float = 0.0         # p95 within this minute (computed at build)
    latency_max: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    agents: int = 0                  # distinct sessions seen this minute

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["cost_usd"] = round(self.cost_usd, 6)
        d["latency_sum"] = round(self.latency_sum, 1)
        d["latency_p95"] = round(self.latency_p95, 1)
        d["latency_max"] = round(self.latency_max, 1)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Bucket":
        return cls(
            host=str(d.get("host") or ""),
            minute=int(d.get("minute") or 0),
            events=int(d.get("events") or 0),
            errors=int(d.get("errors") or 0),
            latency_sum=float(d.get("latency_sum") or 0.0),
            latency_count=int(d.get("latency_count") or 0),
            latency_p95=float(d.get("latency_p95") or 0.0),
            latency_max=float(d.get("latency_max") or 0.0),
            input_tokens=int(d.get("input_tokens") or 0),
            output_tokens=int(d.get("output_tokens") or 0),
            cost_usd=float(d.get("cost_usd") or 0.0),
            agents=int(d.get("agents") or 0),
        )


# ---------------------------------------------------------------------------
# NodeHealth
# ---------------------------------------------------------------------------

@dataclass
class NodeHealth:
    host: str
    events: int = 0
    errors: int = 0
    error_rate: float = 0.0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    agents: int = 0
    last_minute: int = 0
    in_allocation: bool = True
    status: str = "ok"               # ok | warn | crit (derived)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "events": self.events,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "agents": self.agents,
            "last_minute": self.last_minute,
            "in_allocation": self.in_allocation,
            "status": self.status,
        }


# Thresholds for the heatmap status colour.
WARN_ERROR_RATE = 0.05
CRIT_ERROR_RATE = 0.20
WARN_P95_MS = 5000.0
CRIT_P95_MS = 15000.0


def _status(error_rate: float, p95: float) -> str:
    if error_rate >= CRIT_ERROR_RATE or p95 >= CRIT_P95_MS:
        return "crit"
    if error_rate >= WARN_ERROR_RATE or p95 >= WARN_P95_MS:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _extract(interactions: Iterable[Dict[str, Any]], edges: Iterable[Dict[str, Any]]):
    """Yield unified (host, ts_ns, latency_ms, is_error, in_tok, out_tok, cost, session)."""
    for r in interactions:
        resp = r.get("response") or {}
        sc = resp.get("status_code", r.get("status_code"))
        try:
            sc = int(sc) if sc is not None else None
        except (TypeError, ValueError):
            sc = None
        is_err = bool(r.get("error")) or (sc is not None and sc >= 400)
        m = r.get("metrics") or {}
        lat = float(m.get("total_latency_ms") or m.get("latency_ms") or 0.0)
        in_tok = int(m.get("delta_input_tokens") or m.get("input_tokens") or 0)
        out_tok = int(m.get("delta_output_tokens") or m.get("output_tokens") or 0)
        cost = float(m.get("delta_cost_usd") or m.get("cost_usd") or 0.0)
        yield (
            str(r.get("host") or "?"),
            parse_ts_ns(r.get("timestamp")),
            lat, is_err, in_tok, out_tok, cost,
            str(r.get("session_id") or ""),
        )
    for e in edges:
        status = str(e.get("status") or "")
        is_err = status.startswith("error")
        lat = float(e.get("latency_ms") or 0.0)
        yield (
            str(e.get("to_host") or "?"),
            parse_ts_ns(e.get("ts_done") or e.get("ts_start")),
            lat, is_err, 0, 0, 0.0,
            str(e.get("to_session") or ""),
        )


def bucketize(
    interactions: Iterable[Dict[str, Any]],
    edges: Iterable[Dict[str, Any]],
) -> List[Bucket]:
    """Fold raw records into per-(host, minute) buckets, p95 computed per minute."""
    acc: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for host, ts_ns, lat, is_err, in_tok, out_tok, cost, session in _extract(interactions, edges):
        if not ts_ns:
            continue
        key = (host, minute_key(ts_ns))
        b = acc.get(key)
        if b is None:
            b = {"events": 0, "errors": 0, "lat": [], "in": 0, "out": 0, "cost": 0.0, "sessions": set()}
            acc[key] = b
        b["events"] += 1
        if is_err:
            b["errors"] += 1
        if lat > 0:
            b["lat"].append(lat)
        b["in"] += in_tok
        b["out"] += out_tok
        b["cost"] += cost
        if session:
            b["sessions"].add(session)

    out: List[Bucket] = []
    for (host, minute), b in acc.items():
        lat = sorted(b["lat"])
        out.append(Bucket(
            host=host, minute=minute,
            events=b["events"], errors=b["errors"],
            latency_sum=sum(lat), latency_count=len(lat),
            latency_p95=_percentile(lat, 95), latency_max=(lat[-1] if lat else 0.0),
            input_tokens=b["in"], output_tokens=b["out"], cost_usd=b["cost"],
            agents=len(b["sessions"]),
        ))
    out.sort(key=lambda x: (x.host, x.minute))
    return out


def health_from_buckets(
    buckets: Iterable[Bucket],
    *,
    now_ns: int = 0,
    window_sec: int = 0,
    allocation: Optional[List[str]] = None,
) -> Dict[str, NodeHealth]:
    """Fold buckets into per-node health, optionally restricted to a recent window."""
    cutoff = 0
    if window_sec and now_ns:
        cutoff = (now_ns // 1_000_000_000) - window_sec
    alloc = set(allocation or [])

    by_host: Dict[str, NodeHealth] = {}
    p95_by_host: Dict[str, float] = {}
    for b in buckets:
        if cutoff and b.minute < cutoff:
            continue
        h = by_host.get(b.host)
        if h is None:
            h = NodeHealth(host=b.host, in_allocation=(b.host in alloc) if alloc else True)
            by_host[b.host] = h
        h.events += b.events
        h.errors += b.errors
        h.input_tokens += b.input_tokens
        h.output_tokens += b.output_tokens
        h.cost_usd += b.cost_usd
        h.agents = max(h.agents, b.agents)
        h.last_minute = max(h.last_minute, b.minute)
        # avg accumulates via latency_sum/count; p95 approximated by the max
        # per-minute p95 in the window (honest upper-ish estimate without
        # keeping every sample around).
        h._lat_sum = getattr(h, "_lat_sum", 0.0) + b.latency_sum
        h._lat_cnt = getattr(h, "_lat_cnt", 0) + b.latency_count
        p95_by_host[b.host] = max(p95_by_host.get(b.host, 0.0), b.latency_p95)

    for host, h in by_host.items():
        cnt = getattr(h, "_lat_cnt", 0)
        h.avg_latency_ms = (getattr(h, "_lat_sum", 0.0) / cnt) if cnt else 0.0
        h.p95_latency_ms = p95_by_host.get(host, 0.0)
        h.error_rate = (h.errors / h.events) if h.events else 0.0
        h.status = _status(h.error_rate, h.p95_latency_ms)
    return by_host


def health_from_raw(
    interactions: Iterable[Dict[str, Any]],
    edges: Iterable[Dict[str, Any]],
    *,
    now_ns: int = 0,
    window_sec: int = 0,
    allocation: Optional[List[str]] = None,
) -> Dict[str, NodeHealth]:
    """Convenience: bucketize raw records then fold to health."""
    return health_from_buckets(
        bucketize(interactions, edges),
        now_ns=now_ns, window_sec=window_sec, allocation=allocation,
    )
