"""Cluster ``Signal`` objects into ``Incident`` objects. Pure, no I/O.

Clustering is the move that makes ChronoDoctor usable at scale: 500 identical
timeouts become ONE incident with ``count=500`` spanning a list of hosts, not
500 rows. The signature is ``(kind, normalized_message)`` — host/tool/model are
aggregated *inside* the incident so a fleet-wide tool bug stays one card while
its host distribution is still visible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .detectors import SEVERITY_ORDER, Signal

# Above these counts an incident is escalated one severity rung — a symptom that
# recurs often matters more than the same symptom seen once.
_ESCALATE_AT = [(50, 2), (10, 1)]  # (count threshold, rungs to bump)

_SAMPLE_CAP = 5  # evidence samples kept per incident


@dataclass
class Incident:
    signature: str
    kind: str
    title: str
    severity: str
    count: int = 0
    first_ts: str = ""
    last_ts: str = ""
    hosts: List[str] = field(default_factory=list)
    sessions: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)
    samples: List[Dict[str, Any]] = field(default_factory=list)
    diagnosis: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signature": self.signature,
            "kind": self.kind,
            "title": self.title,
            "severity": self.severity,
            "count": self.count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "hosts": self.hosts,
            "sessions": self.sessions,
            "tools": self.tools,
            "models": self.models,
            "scenarios": self.scenarios,
            "samples": self.samples,
            "diagnosis": self.diagnosis,
        }


def _signature(sig: Signal) -> str:
    raw = f"{sig.kind}|{sig.message}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _escalate(base: str, count: int) -> str:
    idx = SEVERITY_ORDER.index(base) if base in SEVERITY_ORDER else 1
    for threshold, bump in _ESCALATE_AT:
        if count >= threshold:
            idx = min(len(SEVERITY_ORDER) - 1, idx + bump)
            break
    return SEVERITY_ORDER[idx]


def _add(lst: List[str], val: str, cap: int = 25) -> None:
    if val and val not in lst and len(lst) < cap:
        lst.append(val)


def cluster_signals(signals: List[Signal]) -> List[Incident]:
    """Group signals by signature; return incidents sorted worst-first."""
    by_sig: Dict[str, Incident] = {}
    base_sev: Dict[str, str] = {}

    for s in signals:
        key = _signature(s)
        inc = by_sig.get(key)
        if inc is None:
            inc = Incident(
                signature=key,
                kind=s.kind,
                title=_title_for(s),
                severity=s.severity,
                first_ts=s.ts,
                last_ts=s.ts,
            )
            by_sig[key] = inc
            base_sev[key] = s.severity
        inc.count += 1
        if s.ts:
            if not inc.first_ts or s.ts < inc.first_ts:
                inc.first_ts = s.ts
            if not inc.last_ts or s.ts > inc.last_ts:
                inc.last_ts = s.ts
        _add(inc.hosts, s.host)
        _add(inc.sessions, s.session)
        _add(inc.tools, s.tool)
        _add(inc.models, s.model)
        _add(inc.scenarios, s.scenario)
        if len(inc.samples) < _SAMPLE_CAP:
            inc.samples.append(s.to_dict())

    incidents = list(by_sig.values())
    for inc in incidents:
        inc.severity = _escalate(base_sev[inc.signature], inc.count)

    incidents.sort(key=_rank, reverse=True)
    return incidents


def _rank(inc: Incident) -> tuple:
    sev = SEVERITY_ORDER.index(inc.severity) if inc.severity in SEVERITY_ORDER else 0
    return (sev, inc.count, len(inc.hosts))


_TITLES = {
    "http_error": "HTTP errors on LLM calls",
    "rate_limit": "Rate-limited LLM calls",
    "interaction_error": "Failed LLM turns",
    "edge_error": "Failed inter-agent calls",
    "orphan_call": "Hung / dropped inter-agent calls",
    "latency_outlier": "Slow LLM turns",
    "slow_edge": "Slow inter-agent calls",
    "retry_storm": "Retry storms",
    "recovery": "Checkpoint restores / backtracks",
}


def _title_for(sig: Signal) -> str:
    base = _TITLES.get(sig.kind, sig.kind)
    if sig.message and sig.kind in ("edge_error", "http_error", "rate_limit", "recovery"):
        return f"{base}: {sig.message}"
    return base
