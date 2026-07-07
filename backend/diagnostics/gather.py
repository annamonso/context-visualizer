"""The only I/O layer of ChronoDoctor: pull raw records from the read path.

Everything downstream (detectors, clustering, diagnosis) is pure and operates
on the lists this module returns. Reads go through the same seams the dashboard
uses — ``backend.path_a_reader`` for Path-A and the inter-agent store for
Path-B — so ChronoDoctor sees exactly what the tabs see and inherits offline
(spool/JSONL) mode for free.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

# Bounds so a scan stays cheap on a large deployment. Override via run_scan().
DEFAULT_MAX_SESSIONS = 400
DEFAULT_MAX_SCENARIOS = 200


def _flatten(node_map: Dict[str, Any]) -> List[Any]:
    out: List[Any] = []
    for v in (node_map or {}).values():
        if isinstance(v, list):
            out.extend(v)
    return out


def gather(
    *,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    max_scenarios: int = DEFAULT_MAX_SCENARIOS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return ``(interactions, stitched_edges, recoveries)``.

    Each interaction record is annotated with its ``session_id`` and ``host`` if
    missing, so detectors can attribute it without a second lookup.
    """
    interactions: List[Dict[str, Any]] = []
    recoveries: List[Dict[str, Any]] = []
    stitched: List[Dict[str, Any]] = []

    # ---- Path-A: read the local capture spool FIRST (instant, GIL-safe). ----
    # Reading via path_a_reader would replay ChronoLog stories, and a ReplayStory
    # on an un-drained story BLOCKS the whole process holding the GIL — that is
    # what froze every tab while a scan ran. The spool holds the same records
    # locally, so we serve from it (like the Memory tab) and only fall back to
    # ChronoLog for a pure-historical reader with no spool.
    used_spool = False
    try:
        from ..capture.spool import KIND_INTERACTIONS, KIND_RECOVERY, get_spool

        spool = get_spool()
        spool_sessions = [s for s in (spool.list_sessions() or []) if s][:max_sessions]
        if spool_sessions:
            used_spool = True
            for sid in spool_sessions:
                for r in spool.read(sid, KIND_INTERACTIONS) or []:
                    if isinstance(r, dict):
                        r.setdefault("session_id", sid)
                        interactions.append(r)
                for rec in spool.read(sid, KIND_RECOVERY) or []:
                    if isinstance(rec, dict):
                        rec.setdefault("session_id", sid)
                        recoveries.append(rec)
    except Exception as exc:
        log.warning("doctor gather spool read failed: %s", exc)

    if not used_spool:
        # Historical mode: no spool on this host, fall back to the ChronoLog
        # reader (may block on un-drained stories — acceptable when there is no
        # live run producing spool data).
        try:
            from ..chronolog import path_a_reader

            sessions = [s.get("session_id") for s in _flatten(path_a_reader.get_sessions())]
            for sid in [s for s in sessions if s][:max_sessions]:
                for r in _flatten(path_a_reader.get_session_interactions(sid)):
                    if isinstance(r, dict):
                        r.setdefault("session_id", sid)
                        interactions.append(r)
                for rec in path_a_reader.get_recovery_events(sid) or []:
                    if isinstance(rec, dict):
                        rec.setdefault("session_id", sid)
                        recoveries.append(rec)
        except Exception as exc:
            log.warning("doctor gather chronolog fallback failed: %s", exc)

    # ---- Path-B: inter-agent edges from the LOCAL hot store (instant). ----
    # list_local_scenarios + read (JSONL hot copy) never touch ChronoLog, so
    # this stays off the GIL-blocking ReplayStory path.
    try:
        from ..capture.inter_agent import get_store

        store = get_store()
        scenarios = store.list_local_scenarios() or []
        if not scenarios:
            scenarios = store.list_scenarios() or []  # historical fallback
        for scn in scenarios[:max_scenarios]:
            try:
                for ev in store.read_stitched(scn) or []:
                    ev.setdefault("scenario_id", scn)
                    stitched.append(ev)
            except Exception:
                continue
    except Exception as exc:
        log.warning("doctor gather edges failed: %s", exc)

    return interactions, stitched, recoveries
