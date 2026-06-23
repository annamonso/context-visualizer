"""Detectors: turn raw records into ``Signal`` objects. Pure, no I/O.

Every detector takes already-fetched records (see :mod:`.gather`) and yields
``Signal`` objects. Keeping them pure makes them trivially unit-testable — feed
a list of dicts, assert on the signals — which is exactly what the ChronoDoctor
demo's pytest suite does.

The records carry failure signals the system already records:
  * LLM interactions  -> non-200 ``response.status_code`` / non-null ``error`` /
                         latency outliers / retry storms (identical prompts).
  * Inter-agent edges -> ``status`` starting ``error:`` / orphaned ``start`` with
                         no ``done`` (a hung or dropped peer call) / slow calls.
  * Recovery events   -> every backtrack / checkpoint-restore is a recorded
                         failure of the forward path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# Severity ladder, low -> high. Used for ranking and colour in the UI.
SEVERITY_ORDER = ["info", "warning", "error", "critical"]


@dataclass
class Signal:
    """One observed failure symptom."""

    kind: str                      # http_error | interaction_error | edge_error | ...
    severity: str                  # info | warning | error | critical
    message: str                   # short, human-readable; numbers stripped where noisy
    host: str = ""
    session: str = ""
    scenario: str = ""
    tool: str = ""
    model: str = ""
    ts: str = ""
    latency_ms: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "host": self.host,
            "session": self.session,
            "scenario": self.scenario,
            "tool": self.tool,
            "model": self.model,
            "ts": self.ts,
            "latency_ms": self.latency_ms,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)
_HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_NUM_RE = re.compile(r"\d+")


def normalize_message(msg: str) -> str:
    """Collapse a noisy error string into a stable template for clustering.

    ``"peer ares-comp-12 timed out after 30001ms"`` and
    ``"peer ares-comp-07 timed out after 5004ms"`` both normalise to
    ``"peer ares-comp-<n> timed out after <n>ms"`` so they land in one incident.
    """
    s = (msg or "").strip().lower()
    s = _UUID_RE.sub("<id>", s)
    s = _HEX_RE.sub("<id>", s)
    s = _NUM_RE.sub("<n>", s)
    s = re.sub(r"\s+", " ", s)
    return s[:200]


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _interaction_status(rec: Dict[str, Any]) -> Optional[int]:
    resp = rec.get("response") or {}
    sc = resp.get("status_code", rec.get("status_code"))
    try:
        return int(sc) if sc is not None else None
    except (TypeError, ValueError):
        return None


def _interaction_latency(rec: Dict[str, Any]) -> float:
    m = rec.get("metrics") or {}
    return _f(m.get("total_latency_ms") or m.get("latency_ms") or rec.get("latency_ms"))


def _prompt_fingerprint(rec: Dict[str, Any]) -> str:
    """A cheap fingerprint of the user prompt, for retry-storm detection."""
    req = rec.get("request") or {}
    body = req.get("body") or req
    msgs = body.get("messages") or []
    last = ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") in (None, "user"):
            last = str(m.get("content") or "")
    return normalize_message(last)[:120]


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_interaction_errors(records: Iterable[Dict[str, Any]]) -> List[Signal]:
    """HTTP-level and explicit ``error`` failures on LLM turns."""
    out: List[Signal] = []
    for rec in records:
        sc = _interaction_status(rec)
        err = rec.get("error")
        if (sc is not None and sc >= 400) or err:
            resp = rec.get("response") or {}
            detail = err or resp.get("text") or f"HTTP {sc}"
            kind = "rate_limit" if sc == 429 else "http_error"
            sev = "critical" if (sc and sc >= 500) else "error"
            out.append(Signal(
                kind=kind,
                severity=sev,
                message=normalize_message(str(detail)),
                host=str(rec.get("host") or ""),
                session=str(rec.get("session_id") or ""),
                scenario=str(rec.get("scenario_id") or ""),
                model=str(rec.get("model") or ""),
                ts=str(rec.get("timestamp") or ""),
                latency_ms=_interaction_latency(rec),
                evidence={
                    "session_id": rec.get("session_id"),
                    "sequence_id": rec.get("sequence_id"),
                    "status_code": sc,
                    "raw": str(detail)[:280],
                },
            ))
    return out


def detect_latency_outliers(
    records: Iterable[Dict[str, Any]],
    *,
    floor_ms: float = 1500.0,
    factor: float = 3.0,
) -> List[Signal]:
    """Flag turns far slower than the per-model median (and above an absolute floor).

    A turn is an outlier when its latency exceeds both ``floor_ms`` and
    ``factor x median(model)``. The median (not mean) keeps a few slow turns
    from hiding the rest.
    """
    recs = [r for r in records if _interaction_latency(r) > 0]
    by_model: Dict[str, List[float]] = {}
    for r in recs:
        by_model.setdefault(str(r.get("model") or "?"), []).append(_interaction_latency(r))
    median: Dict[str, float] = {}
    for model, vals in by_model.items():
        s = sorted(vals)
        median[model] = s[len(s) // 2]

    out: List[Signal] = []
    for r in recs:
        model = str(r.get("model") or "?")
        lat = _interaction_latency(r)
        thresh = max(floor_ms, factor * median.get(model, 0.0))
        if lat >= thresh and median.get(model, 0.0) > 0:
            out.append(Signal(
                kind="latency_outlier",
                severity="warning",
                message=f"turn {factor:g}x slower than the {model} median",
                host=str(r.get("host") or ""),
                session=str(r.get("session_id") or ""),
                scenario=str(r.get("scenario_id") or ""),
                model=model,
                ts=str(r.get("timestamp") or ""),
                latency_ms=lat,
                evidence={
                    "session_id": r.get("session_id"),
                    "sequence_id": r.get("sequence_id"),
                    "latency_ms": lat,
                    "model_median_ms": round(median.get(model, 0.0), 1),
                },
            ))
    return out


def detect_retry_storms(
    records: Iterable[Dict[str, Any]],
    *,
    min_repeats: int = 3,
) -> List[Signal]:
    """A session re-issuing the same prompt ``min_repeats`` times in a row.

    Records must be grouped per session and ordered by sequence_id for the run
    to be meaningful; :func:`.gather` provides them that way.
    """
    out: List[Signal] = []
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        by_session.setdefault(str(r.get("session_id") or ""), []).append(r)

    for sid, recs in by_session.items():
        recs = sorted(recs, key=lambda r: _f(r.get("sequence_id")))
        run_fp = None
        run = []
        for r in recs:
            fp = _prompt_fingerprint(r)
            if fp and fp == run_fp:
                run.append(r)
            else:
                if len(run) >= min_repeats:
                    out.append(_retry_signal(sid, run))
                run_fp, run = fp, [r]
        if len(run) >= min_repeats:
            out.append(_retry_signal(sid, run))
    return out


def _retry_signal(sid: str, run: List[Dict[str, Any]]) -> Signal:
    first = run[0]
    return Signal(
        kind="retry_storm",
        # Count goes in evidence, not the message, so all retry storms cluster
        # into one incident instead of fragmenting by their (varying) length.
        severity="warning",
        message="identical prompt re-issued repeatedly (stuck agent)",
        host=str(first.get("host") or ""),
        session=sid,
        scenario=str(first.get("scenario_id") or ""),
        model=str(first.get("model") or ""),
        ts=str(first.get("timestamp") or ""),
        evidence={
            "session_id": sid,
            "repeat_count": len(run),
            "sequence_ids": [r.get("sequence_id") for r in run],
        },
    )


def detect_edge_errors(stitched: Iterable[Dict[str, Any]]) -> List[Signal]:
    """Inter-agent calls that completed with an error status."""
    out: List[Signal] = []
    for ev in stitched:
        status = str(ev.get("status") or "")
        if status.startswith("error:") or status.startswith("error"):
            detail = status.split(":", 1)[1].strip() if ":" in status else status
            out.append(Signal(
                kind="edge_error",
                severity="error",
                message=normalize_message(detail or "inter-agent call failed"),
                host=str(ev.get("to_host") or ""),
                session=str(ev.get("to_session") or ""),
                scenario=str(ev.get("scenario_id") or ""),
                tool=str(ev.get("tool_name") or ""),
                ts=str(ev.get("ts_done") or ev.get("ts_start") or ""),
                latency_ms=_f(ev.get("latency_ms")),
                evidence={
                    "correlation_id": ev.get("correlation_id"),
                    "from_host": ev.get("from_host"),
                    "from_session": ev.get("from_session"),
                    "to_host": ev.get("to_host"),
                    "to_session": ev.get("to_session"),
                    "status": status,
                },
            ))
    return out


def detect_orphan_calls(stitched: Iterable[Dict[str, Any]]) -> List[Signal]:
    """Calls that started but never completed — a hung or dropped peer.

    These are invisible in the per-edge views (no ``done`` frame to render) yet
    are among the most actionable failures at scale: a wedged node silently
    swallows requests.
    """
    out: List[Signal] = []
    for ev in stitched:
        if ev.get("ts_start") and not ev.get("ts_done"):
            out.append(Signal(
                kind="orphan_call",
                severity="critical",
                message="call started but never completed (hung/dropped peer)",
                host=str(ev.get("to_host") or ""),
                session=str(ev.get("to_session") or ""),
                scenario=str(ev.get("scenario_id") or ""),
                tool=str(ev.get("tool_name") or ""),
                ts=str(ev.get("ts_start") or ""),
                evidence={
                    "correlation_id": ev.get("correlation_id"),
                    "from_host": ev.get("from_host"),
                    "to_host": ev.get("to_host"),
                    "to_session": ev.get("to_session"),
                },
            ))
    return out


def detect_slow_edges(
    stitched: Iterable[Dict[str, Any]],
    *,
    floor_ms: float = 2000.0,
    factor: float = 3.0,
) -> List[Signal]:
    """Inter-agent calls far slower than the per-tool median."""
    done = [e for e in stitched if e.get("ts_done") and _f(e.get("latency_ms")) > 0]
    by_tool: Dict[str, List[float]] = {}
    for e in done:
        by_tool.setdefault(str(e.get("tool_name") or "?"), []).append(_f(e.get("latency_ms")))
    median = {t: sorted(v)[len(v) // 2] for t, v in by_tool.items()}

    out: List[Signal] = []
    for e in done:
        tool = str(e.get("tool_name") or "?")
        lat = _f(e.get("latency_ms"))
        thresh = max(floor_ms, factor * median.get(tool, 0.0))
        if lat >= thresh and median.get(tool, 0.0) > 0:
            out.append(Signal(
                kind="slow_edge",
                severity="warning",
                message=f"call {factor:g}x slower than the {tool} median",
                host=str(e.get("to_host") or ""),
                session=str(e.get("to_session") or ""),
                scenario=str(e.get("scenario_id") or ""),
                tool=tool,
                ts=str(e.get("ts_done") or ""),
                latency_ms=lat,
                evidence={
                    "correlation_id": e.get("correlation_id"),
                    "latency_ms": lat,
                    "tool_median_ms": round(median.get(tool, 0.0), 1),
                },
            ))
    return out


def detect_recovery_events(records: Iterable[Dict[str, Any]]) -> List[Signal]:
    """Each backtrack / checkpoint-restore is a recorded failure of the forward path."""
    out: List[Signal] = []
    for rec in records:
        reason = str(rec.get("reason") or "recovery")
        out.append(Signal(
            kind="recovery",
            severity="warning",
            message=normalize_message(reason),
            host=str(rec.get("host") or ""),
            session=str(rec.get("session_id") or ""),
            ts=str(rec.get("timestamp") or ""),
            evidence={
                "session_id": rec.get("session_id"),
                "blob_name": rec.get("blob_name"),
                "checkpoint_id": rec.get("checkpoint_id"),
                "reason": reason,
            },
        ))
    return out


def detect_all(
    interactions: Iterable[Dict[str, Any]],
    stitched_edges: Iterable[Dict[str, Any]],
    recoveries: Iterable[Dict[str, Any]],
) -> List[Signal]:
    """Run every detector and return the combined signal list."""
    interactions = list(interactions)
    stitched_edges = list(stitched_edges)
    recoveries = list(recoveries)
    signals: List[Signal] = []
    signals += detect_interaction_errors(interactions)
    signals += detect_latency_outliers(interactions)
    signals += detect_retry_storms(interactions)
    signals += detect_edge_errors(stitched_edges)
    signals += detect_orphan_calls(stitched_edges)
    signals += detect_slow_edges(stitched_edges)
    signals += detect_recovery_events(recoveries)
    return signals
