"""Orchestrate a PrismaDoctor scan: gather -> detect -> cluster -> diagnose -> remember.

A scan is cheap and side-effect-light: the only write is appending to the
incident memory (and only when ``remember=True``). The latest report is cached
process-wide so the API and the Doctor tab can read it without re-scanning on
every poll; the worker (or an explicit API trigger) refreshes it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .detectors import SEVERITY_ORDER, detect_all
from .diagnoser import get_diagnoser
from .gather import gather
from .incidents import cluster_signals
from .memory import IncidentMemory

log = logging.getLogger(__name__)


@dataclass
class Report:
    generated_ns: int = 0
    incidents: List[Dict[str, Any]] = field(default_factory=list)
    totals: Dict[str, int] = field(default_factory=dict)
    scanned: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_ns": self.generated_ns,
            "incidents": self.incidents,
            "totals": self.totals,
            "scanned": self.scanned,
            "error": self.error,
        }


_LATEST: Optional[Report] = None
_LOCK = threading.Lock()
_MEMORY: Optional[IncidentMemory] = None


def _memory() -> IncidentMemory:
    global _MEMORY
    if _MEMORY is None:
        _MEMORY = IncidentMemory()
    return _MEMORY


def run_scan(
    *,
    diagnose: bool = True,
    remember: bool = True,
    max_sessions: int = 400,
    max_scenarios: int = 200,
) -> Report:
    """Run a full scan and cache the result. Never raises — errors land in ``Report.error``."""
    rep = Report(generated_ns=time.time_ns())
    try:
        interactions, edges, recoveries = gather(
            max_sessions=max_sessions, max_scenarios=max_scenarios
        )
        signals = detect_all(interactions, edges, recoveries)
        incidents = cluster_signals(signals)

        diagnoser = get_diagnoser() if diagnose else None
        mem = _memory()
        known = mem.known_signatures() if remember else {}

        out: List[Dict[str, Any]] = []
        for inc in incidents:
            d = inc.to_dict()
            if diagnoser is not None:
                d["diagnosis"] = diagnoser.diagnose(d)
            # Recurring? Match this incident's "[kind] title" against the digest.
            key = f"[{d['kind']}] {d['title']}"
            prior = known.get(key) or known.get(d["title"])
            d["recurring"] = bool(prior)
            d["prior_count"] = int(prior or 0)
            out.append(d)

        rep.incidents = out
        rep.scanned = {
            "interactions": len(interactions),
            "edges": len(edges),
            "recoveries": len(recoveries),
        }
        rep.totals = _totals(out)

        if remember and out:
            mem.record(out)
    except Exception as exc:  # a scan must never crash the worker / request
        log.warning("doctor scan failed: %s", exc, exc_info=True)
        rep.error = str(exc)

    with _LOCK:
        global _LATEST
        _LATEST = rep
    return rep


def _totals(incidents: List[Dict[str, Any]]) -> Dict[str, int]:
    t = {s: 0 for s in SEVERITY_ORDER}
    t["incidents"] = len(incidents)
    t["events"] = 0
    for inc in incidents:
        sev = inc.get("severity", "info")
        t[sev] = t.get(sev, 0) + 1
        t["events"] += int(inc.get("count", 0))
    return t


def latest(refresh_if_empty: bool = True) -> Report:
    """Return the cached report, scanning once if none exists yet."""
    with _LOCK:
        rep = _LATEST
    if rep is None and refresh_if_empty:
        return run_scan()
    return rep or Report(generated_ns=time.time_ns())


def memory_document() -> str:
    return _memory().render()
