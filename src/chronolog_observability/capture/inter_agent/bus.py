"""In-process pub/sub fan-out for inter-agent events.

This is the hot path of the live view: ChronoLog's chunk acceptance window
(~180 s keeper→grapher drain) makes it unusable as a real-time read source,
so events are fanned out to SSE subscribers at the moment of ingest, while
ChronoLog/JSONL remain the durable cold path.

Design constraints:
  - publish() must NEVER block or raise into the ingest request — a slow or
    stuck browser must not be able to back-pressure agent traffic. Each
    subscription owns a bounded queue; on overflow the oldest event is
    dropped and counted.
  - Subscriptions are keyed by scenario_id; "*" subscribes to everything.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Dict, Optional, Set

log = logging.getLogger(__name__)

FIREHOSE = "*"


class Subscription:
    """One consumer's bounded event queue. Created via EventBus.subscribe()."""

    def __init__(self, bus: "EventBus", scenario_id: str, maxsize: int) -> None:
        self._bus = bus
        self.scenario_id = scenario_id
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._closed = False

    def get(self, timeout: float) -> Optional[dict]:
        """Next event, or None after ``timeout`` seconds (SSE keepalive tick)."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _offer(self, event: dict) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait(event)
        except queue.Full:
            # Drop-oldest: the live view prefers fresh events over complete
            # history — completeness is the store's job, not the bus's.
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(event)
            except queue.Full:
                pass
            self.dropped += 1

    def close(self) -> None:
        self._closed = True
        self._bus._unsubscribe(self)


class EventBus:
    """Thread-safe fan-out of event dicts to per-scenario subscribers."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._subs: Dict[str, Set[Subscription]] = {}

    def subscribe(self, scenario_id: str = FIREHOSE) -> Subscription:
        sub = Subscription(self, scenario_id, self._maxsize)
        with self._lock:
            self._subs.setdefault(scenario_id, set()).add(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            bucket = self._subs.get(sub.scenario_id)
            if bucket is not None:
                bucket.discard(sub)
                if not bucket:
                    self._subs.pop(sub.scenario_id, None)

    def publish(self, scenario_id: str, event: dict) -> None:
        """Fan out one event. Never blocks; never raises."""
        with self._lock:
            targets = list(self._subs.get(scenario_id, ())) + \
                      list(self._subs.get(FIREHOSE, ()))
        for sub in targets:
            try:
                sub._offer(event)
            except Exception:  # pragma: no cover — _offer is already total
                log.warning("bus offer failed", exc_info=True)

    def stats(self) -> dict:
        with self._lock:
            return {
                "scenarios": {k: len(v) for k, v in self._subs.items()},
                "subscribers": sum(len(v) for v in self._subs.values()),
                "dropped": sum(s.dropped for v in self._subs.values() for s in v),
            }


_DEFAULT_BUS: Optional[EventBus] = None
_DEFAULT_LOCK = threading.Lock()


def get_bus() -> EventBus:
    """Process-wide singleton, same pattern as store.get_store()."""
    global _DEFAULT_BUS
    if _DEFAULT_BUS is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_BUS is None:
                _DEFAULT_BUS = EventBus()
    return _DEFAULT_BUS
