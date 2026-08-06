#!/usr/bin/env python3
"""E2b — Cold-path availability: confirm the ~180 s keeper→grapher drain.

Live cluster only. Each trial appends a uniquely-marked edge event through the
collector (so it lands in ChronoLog via the production path), then polls the
grapher CSV archive dir every --poll seconds until a CSV line contains the
marker. Reports the distribution over --trials trials.

    python3 scripts/eval/measure_drain.py \
        --collector-url http://127.0.0.1:5650 \
        --archive-dir ~/chronolog-install/chronolog/output \
        --trials 10

Runs happily in the background alongside E3 (it is mostly waiting).
Fills: the "~180 s observed" row of tab:freshness.
"""

from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

from _eval_common import append_summary, bootstrap_src, pctl, write_csv


def find_marker(archive: Path, marker: str, since_mtime: float) -> bool:
    """Scan CSVs touched since the trial started for the marker string."""
    for p in archive.glob("*.csv"):
        try:
            if p.stat().st_mtime < since_mtime - 5:
                continue
            if marker in p.read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collector-url", default="http://127.0.0.1:5650")
    ap.add_argument("--dashboard-url", default="", help="fallback ingest if collector down")
    ap.add_argument("--archive-dir", required=True)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=900.0, help="per-trial cap (s)")
    ap.add_argument("--scenario", default=f"eval-drain-{int(time.time())}")
    args = ap.parse_args()

    bootstrap_src()
    from chronolog_observability.collector.client import emit

    archive = Path(args.archive_dir).expanduser()
    if not archive.is_dir():
        raise SystemExit(f"archive dir not found: {archive}")
    print(f"[measure_drain] {args.trials} trials, poll every {args.poll}s, "
          f"archive={archive}")

    rows: list[list] = []
    times: list[float] = []
    for trial in range(1, args.trials + 1):
        marker = f"drainprobe-{uuid.uuid4().hex}"
        t0 = time.time()
        emit({
            "scenario_id": args.scenario, "phase": "start",
            "from_host": "eval-drain", "from_session": f"probe@{args.scenario}",
            "to_host": "eval-drain", "to_session": f"sink@{args.scenario}",
            "kind": "mcp_call", "tool_name": marker,
        }, collector_url=args.collector_url,
           flask_url=args.dashboard_url or None)
        elapsed = None
        while time.time() - t0 < args.timeout:
            if find_marker(archive, marker, t0):
                elapsed = time.time() - t0
                break
            time.sleep(args.poll)
        if elapsed is None:
            print(f"  trial {trial}: TIMEOUT after {args.timeout:.0f}s")
            rows.append([trial, "timeout"])
        else:
            print(f"  trial {trial}: visible in archive after {elapsed:.0f}s")
            rows.append([trial, f"{elapsed:.1f}"])
            times.append(elapsed)

    write_csv("e2b_drain.csv", ["trial", "seconds_to_archive"], rows)
    if times:
        s = sorted(times)
        print(f"  drain: n={len(s)} min={s[0]:.0f}s p50={pctl(s,50):.0f}s "
              f"max={s[-1]:.0f}s (poll granularity ±{args.poll:.0f}s)")
        append_summary({"experiment": "E2b", "metric": "drain_p50", "value": f"{pctl(s,50):.0f}",
                        "unit": "s", "n": len(s), "notes": f"min={s[0]:.0f};max={s[-1]:.0f};poll={args.poll}"})


if __name__ == "__main__":
    main()
