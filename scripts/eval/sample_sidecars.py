#!/usr/bin/env python3
"""E1d — Sidecar footprint: sample CPU%% and RSS of the collector daemon and
capture worker under steady load.

Run on a deployment node while ``synth_live_traffic.py`` (or real agents) is
driving traffic:

    python3 scripts/eval/sample_sidecars.py <pid> [<pid> ...] \
        --duration 600 --interval 2 --tag k1

Reads /proc/<pid>/stat (utime+stime → CPU%% between consecutive samples) and
/proc/<pid>/status (VmRSS). Prints mean/max per pid, writes one CSV row per
sample. Fills: "Collector daemon — CPU/RSS", "Capture worker — CPU/RSS".
"""

from __future__ import annotations

import argparse
import os
import socket
import time
from pathlib import Path

from _eval_common import append_summary, write_csv

CLK = os.sysconf("SC_CLK_TCK")


def read_proc(pid: int) -> tuple[float, float] | None:
    """Return (cpu_seconds_total, rss_mb) or None if the pid is gone."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        # fields after comm: state is parts[0]; utime=parts[11], stime=parts[12]
        cpu_s = (int(parts[11]) + int(parts[12])) / CLK
        rss_mb = 0.0
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) / 1024.0
                    break
        return cpu_s, rss_mb
    except (OSError, IndexError, ValueError):
        return None


def cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()[:120]
    except OSError:
        return "?"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pids", nargs="+", type=int)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--tag", default="", help="suffix for output CSV / summary notes "
                                              "(e.g. offered event rate, K agents/node)")
    args = ap.parse_args()

    host = socket.gethostname()
    labels = {p: cmdline(p) for p in args.pids}
    for p, c in labels.items():
        print(f"[sample_sidecars] pid {p}: {c}")

    prev: dict[int, tuple[float, float]] = {}
    rows: list[list] = []
    per_pid: dict[int, dict[str, list[float]]] = {p: {"cpu": [], "rss": []} for p in args.pids}
    t_end = time.time() + args.duration
    t_prev = time.time()
    for p in args.pids:
        r = read_proc(p)
        if r:
            prev[p] = (r[0], time.time())

    while time.time() < t_end:
        time.sleep(args.interval)
        now = time.time()
        for p in args.pids:
            r = read_proc(p)
            if r is None:
                continue
            cpu_s, rss_mb = r
            if p in prev:
                dt = now - prev[p][1]
                cpu_pct = 100.0 * (cpu_s - prev[p][0]) / dt if dt > 0 else 0.0
                rows.append([f"{now:.1f}", p, f"{cpu_pct:.2f}", f"{rss_mb:.1f}"])
                per_pid[p]["cpu"].append(cpu_pct)
                per_pid[p]["rss"].append(rss_mb)
            prev[p] = (cpu_s, now)
        t_prev = now  # noqa: F841

    tag = f"_{args.tag}" if args.tag else ""
    write_csv(f"e1d_sidecars{tag}.csv", ["unix_ts", "pid", "cpu_pct", "rss_mb"], rows)
    for p in args.pids:
        cpu, rss = per_pid[p]["cpu"], per_pid[p]["rss"]
        if not cpu:
            print(f"  pid {p}: no samples (exited early?)")
            continue
        print(f"  pid {p} ({labels[p][:60]}): "
              f"CPU mean={sum(cpu)/len(cpu):.1f}% max={max(cpu):.1f}%  "
              f"RSS mean={sum(rss)/len(rss):.0f}MB max={max(rss):.0f}MB")
        notes = f"host={host};tag={args.tag};cmd={labels[p][:80]}"
        append_summary({"experiment": "E1d", "metric": f"pid{p}_cpu_mean",
                        "value": f"{sum(cpu)/len(cpu):.2f}", "unit": "%", "n": len(cpu),
                        "notes": notes})
        append_summary({"experiment": "E1d", "metric": f"pid{p}_cpu_max",
                        "value": f"{max(cpu):.2f}", "unit": "%", "n": len(cpu), "notes": notes})
        append_summary({"experiment": "E1d", "metric": f"pid{p}_rss_mean",
                        "value": f"{sum(rss)/len(rss):.1f}", "unit": "MB", "n": len(rss),
                        "notes": notes})
        append_summary({"experiment": "E1d", "metric": f"pid{p}_rss_max",
                        "value": f"{max(rss):.1f}", "unit": "MB", "n": len(rss), "notes": notes})


if __name__ == "__main__":
    main()
