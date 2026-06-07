"""Path-A capture spool — a chimaera-free local buffer for trace events.

In the original context-visualizer, Path-A events (LLM interactions, context-graph
diff nodes, recovery events) lived in the chimaera C++ runtime's in-memory CTE
blobs, and ``sync_worker`` drained them into ChronoLog by polling
``chimaera_client.get_*``. There is no chimaera runtime in this plugin, so the
source is now this local **spool**: whatever instruments the user's agents (an
LLM proxy, an SDK callback, a shim) appends events here, and the Path-A
``SyncWorker`` drains them into ChronoLog.

Why a spool and not a direct ChronoLog write?
  * Capture stays fast and never blocks the traced application on a ChronoLog
    append or a transiently-unreachable visor — the same reason the Path-B
    inter-agent store keeps a local JSONL copy.
  * The worker can dedup against ChronoLog (high-water marks) and replay the
    spool after a crash, so at-least-once capture becomes exactly-once ingest.

Layout (one JSONL file per session per kind, under ``<state>/path_a_spool/``):

    <safe_session>__interactions.jsonl
    <safe_session>__context_graph.jsonl
    <safe_session>__recovery.jsonl

Record shapes match what ``backend.path_a_reader`` expects to read back out of
ChronoLog:
  * interactions / context_graph: carry a monotonic integer ``sequence_id``
    (assigned here if the caller doesn't supply one) — the worker dedups on it.
  * recovery: carry a unique ``blob_name`` (a uuid if not supplied) and a
    ``timestamp`` — the worker dedups on ``blob_name``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)


KIND_INTERACTIONS  = "interactions"
KIND_CONTEXT_GRAPH = "context_graph"
KIND_RECOVERY      = "recovery"
ALL_KINDS = (KIND_INTERACTIONS, KIND_CONTEXT_GRAPH, KIND_RECOVERY)

_SEP = "__"  # session<sep>kind.jsonl


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe(token: str) -> str:
    """Sanitise a session id into a filename fragment (matches the JSONL store)."""
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in token)[:120]


class CaptureSpool:
    """Append-only local buffer for Path-A trace events.

    Thread-safe for concurrent in-process producers. Cross-process producers are
    safe too: each append is a single ``write`` of one line in ``O_APPEND`` mode,
    which POSIX keeps atomic for line-sized payloads.
    """

    def __init__(self, root: Optional[os.PathLike] = None) -> None:
        if root is None:
            base = os.environ.get("DTP_STATE_DIR", "")
            root = (Path(base) if base else Path.home() / ".dt_provenance") / "path_a_spool"
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Lazily-initialised next-sequence cache per (session, kind). Seeded from
        # the file on first use so sequence ids stay monotonic across restarts.
        self._next_seq: Dict[tuple, int] = {}

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _path(self, session_id: str, kind: str) -> Path:
        return self._root / f"{_safe(session_id)}{_SEP}{kind}.jsonl"

    def _append(self, session_id: str, kind: str, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        path = self._path(session_id, kind)
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    def _alloc_seq(self, session_id: str, kind: str) -> int:
        """Return the next monotonic sequence id for (session, kind)."""
        key = (session_id, kind)
        with self._lock:
            nxt = self._next_seq.get(key)
            if nxt is None:
                nxt = self._max_seq_on_disk(session_id, kind) + 1
            self._next_seq[key] = nxt + 1
            return nxt

    def _max_seq_on_disk(self, session_id: str, kind: str) -> int:
        path = self._path(session_id, kind)
        if not path.is_file():
            return 0
        mx = 0
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    s = int(json.loads(raw).get("sequence_id") or 0)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if s > mx:
                    mx = s
        return mx

    # ------------------------------------------------------------------
    # Write API — called by the user's instrumentation
    # ------------------------------------------------------------------

    def record_interaction(
        self, session_id: str, payload: Dict[str, Any], *, sequence_id: Optional[int] = None
    ) -> int:
        """Buffer one LLM interaction. Returns the assigned ``sequence_id``."""
        seq = sequence_id if sequence_id is not None else self._alloc_seq(session_id, KIND_INTERACTIONS)
        rec = dict(payload)
        rec["session_id"] = session_id
        rec["sequence_id"] = int(seq)
        rec.setdefault("timestamp", _iso_now())
        self._append(session_id, KIND_INTERACTIONS, rec)
        return int(seq)

    def record_context_node(
        self, session_id: str, payload: Dict[str, Any], *, sequence_id: Optional[int] = None
    ) -> int:
        """Buffer one context-graph diff node. Returns the assigned ``sequence_id``."""
        seq = sequence_id if sequence_id is not None else self._alloc_seq(session_id, KIND_CONTEXT_GRAPH)
        rec = dict(payload)
        rec["session_id"] = session_id
        rec["sequence_id"] = int(seq)
        rec.setdefault("timestamp", _iso_now())
        self._append(session_id, KIND_CONTEXT_GRAPH, rec)
        return int(seq)

    def record_recovery(
        self, session_id: str, payload: Dict[str, Any], *, blob_name: Optional[str] = None
    ) -> str:
        """Buffer one recovery event. Returns the ``blob_name`` used for dedup."""
        name = blob_name or payload.get("blob_name") or uuid.uuid4().hex
        rec = dict(payload)
        rec["session_id"] = session_id
        rec["blob_name"] = name
        rec.setdefault("timestamp", _iso_now())
        self._append(session_id, KIND_RECOVERY, rec)
        return name

    # ------------------------------------------------------------------
    # Read API — called by the SyncWorker
    # ------------------------------------------------------------------

    def list_sessions(self) -> List[str]:
        """Distinct session ids that have at least one spooled event of any kind."""
        seen: set[str] = set()
        if not self._root.is_dir():
            return []
        for p in self._root.iterdir():
            if not (p.is_file() and p.suffix == ".jsonl"):
                continue
            stem = p.stem
            if _SEP in stem:
                seen.add(stem.rsplit(_SEP, 1)[0])
        return sorted(seen)

    def read(self, session_id: str, kind: str) -> List[Dict[str, Any]]:
        """Return all spooled records for (session, kind), in write order."""
        path = self._path(session_id, kind)
        if not path.is_file():
            return []
        out: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out


_DEFAULT_SPOOL: Optional[CaptureSpool] = None
_DEFAULT_LOCK = threading.Lock()


def get_spool() -> CaptureSpool:
    """Process-wide singleton spool."""
    global _DEFAULT_SPOOL
    if _DEFAULT_SPOOL is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_SPOOL is None:
                _DEFAULT_SPOOL = CaptureSpool()
    return _DEFAULT_SPOOL
