#!/usr/bin/env python3
"""E3a/E3b — Rollup vs. raw query latency vs. event volume (the §6.4 plot).

Fully offline (laptop/gateway OK). For each event count it seeds a dedicated
state dir via ``demo_fleet_scale.py --events N --window-min 60`` (16 nodes),
starts the real dashboard on that state dir, and times ``/api/fleet/health``
over HTTP:

  (a) rollup-backed  — the pre-aggregated ``fleet/rollup.jsonl`` in place;
  (b) raw-fold       — rollup file moved aside, so the API falls back to
                       folding every raw event on each request.

Then (E3b) seeds 100- and 250-node fleets (default turns) and times the same
query, filling "\\tbd{ms at 100 / 250 nodes}".

    python3 scripts/eval/bench_rollup_scaling.py
    python3 scripts/eval/bench_rollup_scaling.py --sizes 10000,50000 --requests 5

Outputs: out/e3a_rollup_scaling.csv, out/e3b_heatmap_nodes.csv, summary rows,
and out/fig_rollup_scaling.tex — a pgfplots figure that reads the CSV directly
(no matplotlib needed on the cluster; \\input it from the thesis).
State dirs are cached under --state-root and reused; delete them to reseed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from _eval_common import (REPO, append_summary, fs_type, out_dir, print_summary,
                          summarize_ms, timed_http_ms, wait_http)

DEMO = REPO / "scripts" / "demos" / "demo_fleet_scale.py"


def append_rows(name: str, header: list[str], rows: list[list]) -> Path:
    """Append data rows to out/<name>, writing the header only when new.

    Sizes can be benchmarked across separate invocations (the state dirs are
    cached); the plot is regenerated from the accumulated file every run.
    """
    path = out_dir() / name
    new = not (path.is_file() and path.stat().st_size > 0)
    with open(path, "a", newline="", encoding="utf-8") as f:
        import csv
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)
    return path


def read_rows(name: str) -> list[list]:
    import csv
    path = out_dir() / name
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))[1:]


def seed(state_dir: Path, extra_args: list[str]) -> None:
    if (state_dir / "path_a_spool").is_dir():
        print(f"  reusing cached seed at {state_dir}")
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DEMO_STATE_DIR=str(state_dir))
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, str(DEMO), *extra_args], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        raise SystemExit(f"seeding failed for {state_dir}")
    print(f"  seeded {state_dir.name} in {time.perf_counter()-t0:.0f}s")


def start_dashboard(state_dir: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ,
               CHRONOLOG_OFFLINE="1",
               DTP_STATE_DIR=str(state_dir),
               OBSERVE_HOST="127.0.0.1",
               OBSERVE_PORT=str(port),
               PYTHONPATH=str(REPO / "src"))
    proc = subprocess.Popen([sys.executable, "-m", "chronolog_observability"],
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if not wait_http(f"http://127.0.0.1:{port}/api/config", tries=120):
        proc.terminate()
        raise SystemExit(f"dashboard on :{port} did not come up")
    return proc


def time_health(port: int, n: int, warmup: int, window: str) -> list[float]:
    url = f"http://127.0.0.1:{port}/api/fleet/health?window={window}"
    for _ in range(warmup):
        timed_http_ms(url, timeout=300)
    return [timed_http_ms(url, timeout=300) for _ in range(n)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", default="10000,50000,100000,250000,500000")
    ap.add_argument("--nodes", type=int, default=16)
    ap.add_argument("--heatmap-nodes", default="100,250")
    ap.add_argument("--requests", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--window", default="60m")
    ap.add_argument("--state-root", default="/tmp/chronolog_eval_scale",
                    help="where the seeded state dirs live (local disk is fine — "
                         "this measures the query path's algorithmic scaling)")
    ap.add_argument("--port", type=int, default=5820)
    ap.add_argument("--skip-e3b", action="store_true")
    args = ap.parse_args()

    root = Path(args.state_root)
    root.mkdir(parents=True, exist_ok=True)
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    print(f"[bench_rollup_scaling] sizes={sizes} nodes={args.nodes} "
          f"requests={args.requests} state_root={root} (fs={fs_type(root)})")

    # ── E3a: latency vs. event volume, rollup vs. raw ──────────────────────
    rows: list[list] = []
    for size in sizes:
        state = root / f"ev{size}"
        seed(state, ["--nodes", str(args.nodes), "--events", str(size),
                     "--window-min", "60"])
        proc = start_dashboard(state, args.port)
        try:
            lat = time_health(args.port, args.requests, args.warmup, args.window)
            ms = summarize_ms(lat)
            print_summary(f"{size:>7} events, rollup", ms)
            rows += [["rollup", size, i, f"{v:.3f}"] for i, v in enumerate(lat)]
            append_summary({"experiment": "E3a", "metric": f"health_rollup_p50_ev{size}",
                            "value": f"{ms['p50']:.2f}", "unit": "ms", "n": ms["n"],
                            "notes": f"nodes={args.nodes};window={args.window}"})

            # Raw-fold fallback: move the rollup store aside mid-flight.
            rollup = state / "fleet" / "rollup.jsonl"
            moved = rollup.with_suffix(".jsonl.bak")
            if rollup.is_file():
                rollup.rename(moved)
            try:
                lat = time_health(args.port, args.requests, args.warmup, args.window)
            finally:
                if moved.is_file():
                    moved.rename(rollup)
            ms = summarize_ms(lat)
            print_summary(f"{size:>7} events, raw-fold", ms)
            rows += [["raw", size, i, f"{v:.3f}"] for i, v in enumerate(lat)]
            append_summary({"experiment": "E3a", "metric": f"health_raw_p50_ev{size}",
                            "value": f"{ms['p50']:.2f}", "unit": "ms", "n": ms["n"],
                            "notes": f"nodes={args.nodes};window={args.window}"})
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    if rows:
        append_rows("e3a_rollup_scaling.csv", ["mode", "events", "i", "latency_ms"], rows)
    all_rows = read_rows("e3a_rollup_scaling.csv")
    if all_rows:
        write_plot_files(all_rows)

    # ── E3b: heatmap query at 100 / 250 nodes ──────────────────────────────
    if not args.skip_e3b:
        hm_rows: list[list] = []
        for n_nodes in (int(s) for s in args.heatmap_nodes.split(",") if s.strip()):
            state = root / f"nodes{n_nodes}"
            seed(state, ["--nodes", str(n_nodes)])
            proc = start_dashboard(state, args.port)
            try:
                lat = time_health(args.port, args.requests, args.warmup, "60m")
                ms = summarize_ms(lat)
                print_summary(f"heatmap {n_nodes} nodes", ms)
                hm_rows += [[n_nodes, i, f"{v:.3f}"] for i, v in enumerate(lat)]
                append_summary({"experiment": "E3b", "metric": f"health_p50_{n_nodes}nodes",
                                "value": f"{ms['p50']:.2f}", "unit": "ms", "n": ms["n"],
                                "notes": "default demo turns, rollup-backed"})
            finally:
                proc.terminate()
                proc.wait(timeout=10)
        if hm_rows:
            append_rows("e3b_heatmap_nodes.csv", ["nodes", "i", "latency_ms"], hm_rows)


def write_plot_files(rows: list[list]) -> None:
    """Aggregate p50 per (mode, size) and emit a ready-to-\\input pgfplots figure
    (+ a PNG via matplotlib when available)."""
    from collections import defaultdict

    from _eval_common import out_dir, pctl
    agg: dict[tuple[str, int], list[float]] = defaultdict(list)
    for mode, size, _, v in rows:
        agg[(mode, int(size))].append(float(v))
    series = {m: sorted((s, pctl(sorted(vs), 50))
                        for (mm, s), vs in agg.items() if mm == m)
              for m in ("rollup", "raw")}

    p50_csv = out_dir() / "e3a_rollup_scaling_p50.csv"
    with open(p50_csv, "w", encoding="utf-8") as f:
        f.write("events,rollup_p50_ms,raw_p50_ms\n")
        raw = dict(series["raw"])
        for s, v in series["rollup"]:
            f.write(f"{s},{v:.3f},{raw.get(s, float('nan')):.3f}\n")
    print(f"  wrote {p50_csv}")

    tex = out_dir() / "fig_rollup_scaling.tex"
    tex.write_text(r"""% Auto-generated by scripts/eval/bench_rollup_scaling.py (E3a).
% \input this inside a figure environment; needs \usepackage{pgfplots}.
\begin{tikzpicture}
\begin{axis}[
    width=0.85\linewidth, height=6cm,
    xlabel={raw events in store}, ylabel={\texttt{/api/fleet/health} p50 (ms)},
    xmode=log, log basis x=10, ymin=0,
    legend pos=north west, legend cell align=left,
    grid=major, grid style={dashed,gray!25},
]
\addplot+[mark=*] table [x=events, y=rollup_p50_ms, col sep=comma]
    {figures/e3a_rollup_scaling_p50.csv};
\addlegendentry{rollup-backed (flat)}
\addplot+[mark=square*] table [x=events, y=raw_p50_ms, col sep=comma]
    {figures/e3a_rollup_scaling_p50.csv};
\addlegendentry{raw fold (linear)}
\end{axis}
\end{tikzpicture}
""")
    print(f"  wrote {tex} (copy e3a_rollup_scaling_p50.csv to thesis figures/)")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 4))
        for mode, marker in (("rollup", "o"), ("raw", "s")):
            xs = [s for s, _ in series[mode]]
            ys = [v for _, v in series[mode]]
            ax.plot(xs, ys, marker=marker, label={"rollup": "rollup-backed",
                                                  "raw": "raw fold"}[mode])
        ax.set_xscale("log")
        ax.set_xlabel("raw events in store")
        ax.set_ylabel("/api/fleet/health p50 (ms)")
        ax.legend()
        ax.grid(True, ls="--", alpha=0.4)
        png = out_dir() / "fig_rollup_scaling.png"
        fig.savefig(png, dpi=160, bbox_inches="tight")
        print(f"  wrote {png}")
    except ImportError:
        print("  (matplotlib not available — PNG skipped; the .tex figure is the deliverable)")


if __name__ == "__main__":
    main()
