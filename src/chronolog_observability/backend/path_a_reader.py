"""ChronoLog-backed read functions, shaped like the old `chimaera_client.get_*`.

Each function returns data in the EXACT shape the live-runtime client returned —
`{node_id: ...}` outer wrapping included — so the generic blueprints don't need
to know which source served the request. We use a single virtual node id
("chronolog") since ChronoLog merges across producers transparently.

This module is the read half of the default `ChronoLogAdapter`; it is the
generic path that works on any ChronoLog deployment. It never imports chimaera.

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


def _replay(chronicle: str, story: str) -> List[Dict[str, Any]]:
    be = get_backend()
    if be is None:
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

def get_sessions() -> Dict[str, Dict[str, Any]]:
    be = get_backend()
    if be is None:
        return {}
    try:
        sids = be.index_list(constants.INDEX_STORY_SESSIONS)
    except Exception as exc:
        log.warning("index_list sessions failed: %s", exc)
        return {}
    return {VIRTUAL_NODE_ID: {sid: {} for sid in sids}}


def get_session_interactions(session_id: str) -> Dict[str, Dict[str, Any]]:
    events = _replay(constants.CHRONICLE_LLM_INTERACTIONS, session_id)
    bucket: Dict[str, Any] = {}
    for ev in events:
        ev = _strip_chronolog_meta(ev)
        seq = str(ev.get("sequence_id", ""))
        if seq:
            bucket[seq] = ev
    return {VIRTUAL_NODE_ID: bucket}


def get_interaction(session_id: str, seq_id) -> Dict[str, Dict[str, Any]]:
    seq = str(seq_id)
    events = _replay(constants.CHRONICLE_LLM_INTERACTIONS, session_id)
    for ev in events:
        ev = _strip_chronolog_meta(ev)
        if str(ev.get("sequence_id")) == seq:
            return {VIRTUAL_NODE_ID: {seq: ev}}
    return {VIRTUAL_NODE_ID: {}}


def get_context_graphs() -> Dict[str, List[str]]:
    be = get_backend()
    if be is None:
        return {}
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
