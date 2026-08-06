"""PrismaDoctor background scanner — runs :func:`run_scan` on a timer.

Opt-in (``CHRONOLOG_DOCTOR=1``), mirroring the Path-A capture worker. Detection
always works on demand without this; the daemon is the *live* mode that keeps
the cached report fresh and lets the incident-memory digest accrue across a run
so repeat offenders are recognised without anyone clicking.

Standalone::

    python3 -m chronolog_observability.diagnostics.worker --interval 30
    chronolog-doctor --interval 30        # console-script alias
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Optional

from .scan import run_scan

log = logging.getLogger(__name__)

_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()


def _loop(interval_sec: float) -> None:
    log.info("prismadoctor loop start (interval=%.1fs)", interval_sec)
    while not _STOP.is_set():
        try:
            rep = run_scan()
            t = rep.totals
            log.info(
                "doctor scan: %d incidents (%d critical, %d error) over %s",
                t.get("incidents", 0), t.get("critical", 0), t.get("error", 0),
                rep.scanned,
            )
        except Exception as exc:
            log.warning("doctor scan error: %s", exc)
        _STOP.wait(interval_sec)


def start_doctor_worker(interval_sec: float = 30.0) -> Optional[threading.Thread]:
    """Start the daemon thread (idempotent). Returns the thread, or None if running."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return None
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, args=(interval_sec,), name="prismadoctor", daemon=True)
    _THREAD.start()
    return _THREAD


def stop_doctor_worker() -> None:
    _STOP.set()


def is_running() -> bool:
    """True if the background scanner thread is alive."""
    return _THREAD is not None and _THREAD.is_alive() and not _STOP.is_set()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="PrismaDoctor background scanner")
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between scans")
    ap.add_argument("--once", action="store_true", help="run a single scan and exit")
    args = ap.parse_args()

    if args.once:
        rep = run_scan()
        import json
        print(json.dumps(rep.to_dict(), indent=2)[:4000])
        return

    start_doctor_worker(args.interval)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_doctor_worker()


if __name__ == "__main__":
    main()
