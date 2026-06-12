"""Path-A sync worker — drains the local capture spool into ChronoLog.

This is the ChronoLog-native successor of the original
``context_visualizer/chronolog/sync_worker.py``. That worker polled the live
the legacy runtime's CTE blobs and wrote deltas into ChronoLog.
This plugin has no such runtime, so the source is the local
:class:`~chronolog_observability.capture.spool.CaptureSpool` (see its module
docstring) — whatever instruments the user's agents appends events there, and
this worker forwards them into the same ChronoLog stories that
``backend.path_a_reader`` reads back out.

Design (unchanged in spirit from the original):
  * Per-(session, kind) high-water mark on ``sequence_id`` for interactions and
    context-graph nodes; recovery events dedup on ``blob_name``.
  * The HWM dict is rebuilt on startup by replaying ChronoLog, so a worker
    restart never re-writes events already accepted by a keeper.
  * Sync is single-threaded.

Usage from Flask startup (opt-in via ``CHRONOLOG_CAPTURE=1``):

    from chronolog_observability.capture.sync_worker import start_sync_worker
    start_sync_worker(interval_sec=5)

Standalone:

    python3 -m chronolog_observability.capture.sync_worker --interval 5
    chronolog-capture --interval 5        # console-script alias
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..backend import constants
from ..backend.client import ChronoLogBackend, get_backend
from . import spool as _spool
from .spool import (
    ALL_KINDS,
    KIND_CONTEXT_GRAPH,
    KIND_INTERACTIONS,
    KIND_RECOVERY,
    CaptureSpool,
)


log = logging.getLogger(__name__)


@dataclass
class SyncStats:
    polls: int = 0
    sessions_seen: Set[str] = field(default_factory=set)
    events_written: Dict[str, int] = field(default_factory=dict)
    last_error: Optional[str] = None
    last_poll_started_ns: int = 0
    last_poll_finished_ns: int = 0

    def to_dict(self) -> dict:
        return {
            "polls": self.polls,
            "sessions_seen": sorted(self.sessions_seen),
            "events_written": dict(self.events_written),
            "last_error": self.last_error,
            "last_poll_started_ns": self.last_poll_started_ns,
            "last_poll_finished_ns": self.last_poll_finished_ns,
        }


class SpoolToChronoLogSync:
    """Forwards spooled Path-A events into ChronoLog Stories.

    The spool is injectable so tests can drive the worker against an in-memory
    fixture without any instrumentation running.
    """

    def __init__(self, backend: ChronoLogBackend, spool: Optional[CaptureSpool] = None):
        self._backend = backend
        self._spool = spool if spool is not None else _spool.get_spool()
        # High water marks per (session_id, kind) -> last sequence_id written.
        self._hwm: Dict[Tuple[str, str], int] = {}
        # Recovery events dedup on blob_name (uuid), not seq_id.
        self._recovery_seen: Dict[str, Set[str]] = {}
        self.stats = SyncStats()
        self._index_seen: Set[str] = set()

    # ---- HWM rebuild on startup -------------------------------------

    def warm_up_from_chronolog(self) -> None:
        """Rebuild HWMs by replaying ChronoLog. Idempotent."""
        try:
            sessions = self._backend.index_list(constants.INDEX_STORY_SESSIONS)
        except Exception as exc:
            log.warning("warm_up: index_list failed (%s); starting from zero", exc)
            sessions = []

        for sid in sessions:
            for kind in (KIND_INTERACTIONS, KIND_CONTEXT_GRAPH):
                chronicle, story = _chronicle_story(sid, kind)
                try:
                    events = self._backend.replay_events(chronicle, story)
                except Exception as exc:
                    log.warning("warm_up replay %s/%s failed: %s", chronicle, story, exc)
                    continue
                max_seq = 0
                for ev in events:
                    s = ev.get("sequence_id") or ev.get("seq_id")
                    if isinstance(s, int) and s > max_seq:
                        max_seq = s
                if max_seq:
                    self._hwm[(sid, kind)] = max_seq

            # Recovery events — track blob_names already seen.
            chronicle, story = _chronicle_story(sid, KIND_RECOVERY)
            try:
                events = self._backend.replay_events(chronicle, story)
            except Exception:
                events = []
            self._recovery_seen[sid] = {ev["blob_name"] for ev in events if "blob_name" in ev}

        log.info("warm_up complete: %d sessions, %d HWM rows", len(sessions), len(self._hwm))

    # ---- One sync pass ----------------------------------------------

    def sync_once(self) -> SyncStats:
        self.stats.polls += 1
        self.stats.last_poll_started_ns = time.time_ns()
        try:
            sessions = self._spool.list_sessions()
        except Exception as exc:
            self.stats.last_error = f"list_sessions: {exc}"
            log.error("sync_once: list_sessions failed: %s", exc)
            self.stats.last_poll_finished_ns = time.time_ns()
            return self.stats

        for sid in sessions:
            self._index_session(sid)
            self.stats.sessions_seen.add(sid)
            for kind in ALL_KINDS:
                try:
                    n = self._sync_one(sid, kind)
                except Exception as exc:
                    self.stats.last_error = f"{sid}/{kind}: {exc}"
                    log.error("sync_once %s/%s failed: %s", sid, kind, exc)
                    continue
                if n > 0:
                    self.stats.events_written[kind] = self.stats.events_written.get(kind, 0) + n
                    log.debug("synced %d %s events for %s", n, kind, sid)

        self.stats.last_poll_finished_ns = time.time_ns()
        return self.stats

    # ---- per-kind sync ----------------------------------------------

    def _sync_one(self, session_id: str, kind: str) -> int:
        chronicle, story = _chronicle_story(session_id, kind)

        if kind in (KIND_INTERACTIONS, KIND_CONTEXT_GRAPH):
            items = self._spool.read(session_id, kind)
            return self._append_new_seq(chronicle, story, session_id, kind, items)

        if kind == KIND_RECOVERY:
            events = self._spool.read(session_id, kind)
            seen = self._recovery_seen.setdefault(session_id, set())
            wrote = 0
            for ev in events:
                blob = ev.get("blob_name")
                if not blob or blob in seen:
                    continue
                self._backend.append_event(chronicle, story, ev)
                seen.add(blob)
                wrote += 1
            return wrote

        return 0

    # ---- helpers ----------------------------------------------------

    def _index_session(self, session_id: str) -> None:
        if session_id in self._index_seen:
            return
        try:
            self._backend.index_add(constants.INDEX_STORY_SESSIONS, session_id)
            self._index_seen.add(session_id)
        except Exception as exc:
            log.warning("index_add session %s failed: %s", session_id, exc)

    def _append_new_seq(
        self,
        chronicle: str,
        story:     str,
        session_id: str,
        kind:       str,
        items:      Iterable[dict],
    ) -> int:
        hwm = self._hwm.get((session_id, kind), 0)
        wrote = 0
        max_seen = hwm
        for item in items:
            seq = _to_int(item.get("sequence_id") or item.get("seq_id"))
            if seq is None or seq <= hwm:
                continue
            payload = dict(item)
            payload["session_id"] = session_id
            payload["sequence_id"] = seq
            self._backend.append_event(chronicle, story, payload)
            wrote += 1
            if seq > max_seen:
                max_seen = seq
        if max_seen > hwm:
            self._hwm[(session_id, kind)] = max_seen
        return wrote


def _chronicle_story(session_id: str, kind: str) -> Tuple[str, str]:
    if kind == KIND_INTERACTIONS:
        return constants.CHRONICLE_LLM_INTERACTIONS, session_id
    if kind == KIND_CONTEXT_GRAPH:
        return constants.CHRONICLE_CONTEXT_GRAPHS, session_id
    if kind == KIND_RECOVERY:
        return constants.CHRONICLE_RECOVERY_EVENTS, session_id
    raise ValueError(f"unknown sync kind {kind!r}")


def _to_int(v) -> Optional[int]:
    try:
        if isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Background driver
# ----------------------------------------------------------------------

class SyncWorker(threading.Thread):
    def __init__(self, sync: SpoolToChronoLogSync, interval_sec: float = 5.0):
        super().__init__(name="chronolog-path-a-sync", daemon=True)
        self._sync = sync
        self._interval = interval_sec
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("path-a sync worker starting; interval=%.1fs", self._interval)
        # Best-effort warm-up so we don't re-write existing events.
        try:
            self._sync.warm_up_from_chronolog()
        except Exception as exc:
            log.warning("warm-up failed (non-fatal): %s", exc)

        while not self._stop.is_set():
            try:
                self._sync.sync_once()
            except Exception as exc:
                log.error("sync pass crashed: %s", exc)
            self._stop.wait(self._interval)
        log.info("path-a sync worker stopped")


def start_sync_worker(interval_sec: float = 5.0) -> Optional[SyncWorker]:
    """Spin up the worker iff a live ChronoLog backend is available.

    Returns the started worker, or ``None`` in ``CHRONOLOG_OFFLINE=1`` mode where
    there is nothing to drain into. Callers that auto-start from Flask should
    gate on their own opt-in (e.g. ``CHRONOLOG_CAPTURE=1``) since the dashboard
    is primarily a reader.
    """
    backend = get_backend()
    if backend is None:
        log.info("ChronoLog offline; path-a sync worker not started")
        return None
    sync = SpoolToChronoLogSync(backend)
    worker = SyncWorker(sync, interval_sec=interval_sec)
    worker.start()
    return worker


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="ChronoLog Path-A sync worker (spool -> ChronoLog)")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--once", action="store_true", help="Run one sync pass and exit")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    backend = get_backend()
    if backend is None:
        print("ChronoLog is offline (CHRONOLOG_OFFLINE=1) — nothing to drain into.")
        return 2

    sync = SpoolToChronoLogSync(backend)
    if args.once:
        sync.warm_up_from_chronolog()
        stats = sync.sync_once()
        print(stats.to_dict())
        return 0

    w = SyncWorker(sync, interval_sec=args.interval)
    w.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nstopping…")
        w.stop()
        w.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
