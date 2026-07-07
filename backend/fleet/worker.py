"""Fleet rollup emitter — periodically folds raw records into rollup buckets.

Opt-in (``CHRONOLOG_FLEET_ROLLUP=1``). The Fleet API works without this (it
falls back to computing health from raw on demand); the emitter is what makes
reads cheap at scale by persisting pre-aggregated (host, minute) buckets so the
view never re-scans raw events.

Standalone::

    python3 -m backend.fleet.worker --interval 30 --once
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Optional

from .rollup import bucketize
from .store import write_buckets

log = logging.getLogger(__name__)

_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()


def emit_once(max_sessions: int = 400, max_scenarios: int = 200) -> int:
    """Gather raw, bucketize, persist. Returns buckets written."""
    from ..diagnostics.gather import gather

    interactions, edges, _ = gather(max_sessions=max_sessions, max_scenarios=max_scenarios)
    buckets = bucketize(interactions, edges)
    return write_buckets(buckets)


def _loop(interval_sec: float) -> None:
    log.info("fleet rollup loop start (interval=%.1fs)", interval_sec)
    while not _STOP.is_set():
        try:
            n = emit_once()
            log.info("fleet rollup: wrote %d bucket snapshots", n)
        except Exception as exc:
            log.warning("fleet rollup error: %s", exc)
        _STOP.wait(interval_sec)


def start_rollup_worker(interval_sec: float = 30.0) -> Optional[threading.Thread]:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return None
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, args=(interval_sec,), name="fleet-rollup", daemon=True)
    _THREAD.start()
    return _THREAD


def stop_rollup_worker() -> None:
    _STOP.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Fleet rollup emitter")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        print(f"wrote {emit_once()} buckets")
        return
    start_rollup_worker(args.interval)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_rollup_worker()


if __name__ == "__main__":
    main()
