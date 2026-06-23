"""Persist and read Fleet rollup buckets.

Buckets live in the ChronoLog ``metrics_rollup`` chronicle (story ``minute``);
in offline demo mode they live in a local JSONL file. Because each emit writes a
*recomputed* snapshot of a (host, minute) cell, the reader keeps the last value
seen per (host, minute) — the most complete snapshot wins, so re-emits never
double-count.

Reading rollups is O(nodes x buckets) regardless of raw event volume; that is
what the Fleet view trades the per-event scan for.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..backend import constants
from .rollup import Bucket

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _local_path() -> Path:
    base = os.environ.get("DTP_STATE_DIR", "") or str(Path.home() / ".dt_provenance")
    p = Path(base) / "fleet"
    p.mkdir(parents=True, exist_ok=True)
    return p / "rollup.jsonl"


def _backend():
    try:
        from ..backend.client import get_backend

        return get_backend()
    except Exception:
        return None


def write_buckets(buckets: List[Bucket]) -> int:
    """Append rollup buckets to ChronoLog (or local JSONL offline). Returns count."""
    if not buckets:
        return 0
    be = _backend()
    if be is None:
        path = _local_path()
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                for b in buckets:
                    f.write(json.dumps(b.to_dict(), separators=(",", ":")) + "\n")
        return len(buckets)
    n = 0
    for b in buckets:
        try:
            be.append_event(constants.CHRONICLE_METRICS_ROLLUP, constants.ROLLUP_STORY_MINUTE, b.to_dict())
            n += 1
        except Exception as exc:
            log.warning("rollup append failed: %s", exc)
    return n


def _read_local() -> List[Bucket]:
    path = _local_path()
    if not path.is_file():
        return []
    out: List[Bucket] = []
    with _LOCK:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Bucket.from_dict(json.loads(line)))
                except Exception:
                    continue
    return out


def _read_chronolog(be) -> List[Bucket]:
    try:
        events = be.replay_events(constants.CHRONICLE_METRICS_ROLLUP, constants.ROLLUP_STORY_MINUTE)
    except Exception as exc:
        log.warning("rollup replay failed: %s", exc)
        return []
    out: List[Bucket] = []
    for ev in events:
        ev = {k: v for k, v in ev.items() if not k.startswith("_chronolog_")}
        try:
            out.append(Bucket.from_dict(ev))
        except Exception:
            continue
    return out


def read_buckets() -> List[Bucket]:
    """Read all rollup buckets, last-snapshot-wins per (host, minute)."""
    be = _backend()
    raw = _read_local() if be is None else (_read_chronolog(be) + _read_local())
    dedup: Dict[Tuple[str, int], Bucket] = {}
    for b in raw:
        dedup[(b.host, b.minute)] = b  # later entries (more complete) win
    return sorted(dedup.values(), key=lambda x: (x.host, x.minute))
