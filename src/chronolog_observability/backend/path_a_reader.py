"""ChronoLog-backed read functions, shaped like the legacy runtime client's `get_*` API.

Each function returns data in the EXACT shape the live-runtime client returned —
`{node_id: ...}` outer wrapping included — so the generic blueprints don't need
to know which source served the request. We use a single virtual node id
("chronolog") since ChronoLog merges across producers transparently.

This module is the read half of the default `ChronoLogAdapter`; it is the
generic path that works on any ChronoLog deployment. It depends only on ChronoLog.

Functions return empty results when ChronoLog is offline/unreachable
(`get_backend()` is `None`), so callers degrade gracefully.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import constants
from .client import _env, get_backend


log = logging.getLogger(__name__)

VIRTUAL_NODE_ID = _env("CHRONOLOG_VIRTUAL_NODE", "DTP_CHRONOLOG_VIRTUAL_NODE", default="chronolog")


def _spool():
    """Offline (``CHRONOLOG_OFFLINE=1``) read source: the local capture spool.

    With no visor there is nothing to replay, but anything recorded through
    the spool is sitting right here in JSONL — serving it makes the offline
    demo a faithful, fully-populated dashboard instead of an empty shell.
    The capture→backend layering is inverted only on this demo path, hence
    the lazy import.
    """
    from ..capture.spool import get_spool

    return get_spool()


_SPOOL_KIND_BY_CHRONICLE = {
    constants.CHRONICLE_LLM_INTERACTIONS: "interactions",
    constants.CHRONICLE_CONTEXT_GRAPHS: "context_graph",
    constants.CHRONICLE_RECOVERY_EVENTS: "recovery",
}


def _replay(chronicle: str, story: str) -> List[Dict[str, Any]]:
    be = get_backend()
    if be is None:
        kind = _SPOOL_KIND_BY_CHRONICLE.get(chronicle)
        if kind is None:
            return []
        try:
            return _spool().read(story, kind)
        except Exception as exc:
            log.warning("offline spool read %s/%s failed: %s", chronicle, story, exc)
            return []
    try:
        return be.replay_events(chronicle, story)
    except Exception as exc:
        log.warning("chronolog replay %s/%s failed: %s", chronicle, story, exc)
        return []


def _strip_chronolog_meta(ev: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in ev.items() if not k.startswith("_chronolog_")}


# ----------------------------------------------------------------------
# Read functions — same names + signatures as the old live-runtime client
# ----------------------------------------------------------------------

def get_sessions() -> Dict[str, Any]:
    """Sessions known to the source, as ``{node: [{"session_id": ...}, ...]}``.

    The list-of-dicts payload is the shape every consumer flattens
    (``flatten_monitor_result`` / ``_flatten_results``) — a bare
    ``{sid: attrs}`` mapping silently flattens to zero sessions.
    """
    be = get_backend()
    if be is None:
        try:
            sids = _spool().list_sessions()
        except Exception as exc:
            log.warning("offline spool list_sessions failed: %s", exc)
            return {}
    else:
        try:
            sids = be.index_list(constants.INDEX_STORY_SESSIONS)
        except Exception as exc:
            log.warning("index_list sessions failed: %s", exc)
            return {}
    return {VIRTUAL_NODE_ID: [{"session_id": sid} for sid in sids]}


def get_session_interactions(session_id: str) -> Dict[str, Any]:
    """One session's interactions as ``{node: [record, ...]}``, seq-ordered.

    List payload, same reason as :func:`get_sessions` — consumers flatten,
    and a ``{seq: record}`` mapping flattens into one meaningless wrapper
    dict (every turn collapses into a single bogus row).
    """
    events = _replay(constants.CHRONICLE_LLM_INTERACTIONS, session_id)
    by_seq: Dict[int, Any] = {}
    for ev in events:
        ev = _strip_chronolog_meta(ev)
        try:
            seq = int(ev.get("sequence_id"))
        except (TypeError, ValueError):
            continue
        by_seq[seq] = ev  # replays may duplicate; last write wins
    return {VIRTUAL_NODE_ID: [by_seq[s] for s in sorted(by_seq)]}


def get_interaction(session_id: str, seq_id) -> Dict[str, Any]:
    """One interaction as ``{node: record}`` (the record itself, unwrapped)."""
    seq = str(seq_id)
    events = _replay(constants.CHRONICLE_LLM_INTERACTIONS, session_id)
    for ev in events:
        ev = _strip_chronolog_meta(ev)
        if str(ev.get("sequence_id")) == seq:
            return {VIRTUAL_NODE_ID: ev}
    return {}


def get_context_graphs() -> Dict[str, List[str]]:
    be = get_backend()
    if be is None:
        try:
            sids = _spool().list_sessions()
        except Exception:
            return {}
        visible = [sid for sid in sids if _replay(constants.CHRONICLE_CONTEXT_GRAPHS, sid)]
        return {VIRTUAL_NODE_ID: visible}
    try:
        sids = be.index_list(constants.INDEX_STORY_SESSIONS)
    except Exception:
        sids = []
    # Only return sessions that actually have a context_graphs story —
    # cheap check: replay returns [] if the story doesn't exist.
    visible: List[str] = []
    for sid in sids:
        evs = _replay(constants.CHRONICLE_CONTEXT_GRAPHS, sid)
        if evs:
            visible.append(sid)
    return {VIRTUAL_NODE_ID: visible}


def get_context_graph(session_id: str, since: int = 0) -> Dict[str, List[Dict[str, Any]]]:
    events = _replay(constants.CHRONICLE_CONTEXT_GRAPHS, session_id)
    out: List[Dict[str, Any]] = []
    for ev in events:
        ev = _strip_chronolog_meta(ev)
        seq = ev.get("sequence_id") or 0
        try:
            if int(seq) > int(since):
                out.append(ev)
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda e: int(e.get("sequence_id") or 0))
    return {VIRTUAL_NODE_ID: out}


def get_context_node(session_id: str, seq_id) -> Dict[str, Dict[str, Any]]:
    seq = str(seq_id)
    events = _replay(constants.CHRONICLE_CONTEXT_GRAPHS, session_id)
    for ev in events:
        ev = _strip_chronolog_meta(ev)
        if str(ev.get("sequence_id")) == seq:
            return {VIRTUAL_NODE_ID: {seq: ev}}
    return {VIRTUAL_NODE_ID: {}}


def get_recovery_events(session_id: str) -> List[Dict[str, Any]]:
    events = _replay(constants.CHRONICLE_RECOVERY_EVENTS, session_id)
    out: List[Dict[str, Any]] = [_strip_chronolog_meta(ev) for ev in events]
    out.sort(key=lambda e: e.get("timestamp", ""))
    return out
