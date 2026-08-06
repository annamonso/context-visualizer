"""Shared helpers for the thesis evaluation scripts (docs/eval-plan.md).

Every bench writes machine-readable CSVs into ``scripts/eval/out/`` and prints
a human summary. All timing uses ``time.perf_counter_ns()`` on a single host —
never compare stamps across machines.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE / "out"


def bootstrap_src() -> None:
    """Put <repo>/src on sys.path so ``chronolog_observability`` imports."""
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def out_dir() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT


def write_csv(name: str, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> Path:
    path = out_dir() / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {path}")
    return path


def append_summary(row: Dict[str, Any], name: str = "summary.csv") -> None:
    """Append one experiment-summary row to the shared summary CSV."""
    path = out_dir() / name
    exists = path.is_file()
    fields = ["experiment", "metric", "value", "unit", "n", "notes", "utc"]
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        row = dict(row)
        row.setdefault("utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        w.writerow(row)


def pctl(sorted_vals: List[float], p: float) -> float:
    """Nearest-rank percentile on an already-sorted list."""
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round(p / 100.0 * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


def summarize_ms(samples_ms: List[float]) -> Dict[str, float]:
    s = sorted(samples_ms)
    return {
        "n": len(s),
        "mean": statistics.fmean(s) if s else float("nan"),
        "p50": pctl(s, 50),
        "p95": pctl(s, 95),
        "p99": pctl(s, 99),
        "max": s[-1] if s else float("nan"),
    }


def print_summary(label: str, ms: Dict[str, float]) -> None:
    print(f"  {label}: n={ms['n']}  mean={ms['mean']:.3f}ms  "
          f"p50={ms['p50']:.3f}ms  p95={ms['p95']:.3f}ms  "
          f"p99={ms['p99']:.3f}ms  max={ms['max']:.3f}ms")


def fs_type(path: Path) -> str:
    """Best-effort filesystem type for *path* (so results record NFS vs local)."""
    try:
        best, best_fs = "", "?"
        real = str(Path(path).resolve())
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and real.startswith(parts[1]) and len(parts[1]) > len(best):
                    best, best_fs = parts[1], parts[2]
        return best_fs
    except OSError:
        return "?"


# ── HTTP helpers (stdlib only, one method for every timed request) ──────────


def http_json(url: str, body: Optional[dict] = None, timeout: float = 30.0,
              headers: Optional[dict] = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def timed_http_ms(url: str, body: Optional[dict] = None, timeout: float = 30.0) -> float:
    t0 = time.perf_counter_ns()
    http_json(url, body, timeout)
    return (time.perf_counter_ns() - t0) / 1e6


def wait_http(url: str, tries: int = 60, delay: float = 0.5) -> bool:
    for _ in range(tries):
        try:
            http_json(url, timeout=2.0)
            return True
        except Exception:
            time.sleep(delay)
    return False
