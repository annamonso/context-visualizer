#!/usr/bin/env python3
"""E1a — Spool append latency (Path-A fast path).

Microbenchmarks ``CaptureSpool.record_interaction`` with a realistic ~2 KB
payload. Run it on a COMPUTE node with the spool on the same NFS mount the
real runs use (`--state-dir` = the production ``DTP_STATE_DIR``) — the NFS
latency is the honest number, not tmpfs.

    python3 scripts/eval/bench_spool.py --state-dir /mnt/common/$USER/observe-state-eval

Fills: Table tab:overhead "Spool append — ms, p50/p95". Also reports measured
bytes/record (feeds E1e's spool number).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

from _eval_common import (append_summary, bootstrap_src, fs_type,
                          print_summary, summarize_ms, write_csv)


def realistic_record(target_bytes: int, seq: int) -> dict:
    """One LLM-turn record shaped like the real harness spools (spool_turn)."""
    rec = {
        "scenario_id": "eval-bench",
        "host": socket.gethostname(),
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "request": {"method": "POST", "path": "/v1/messages",
                    "body": {"system": "You are the planner agent in a multi-agent system.",
                             "messages": [{"role": "user", "content": ""}]}},
        "response": {"status_code": 200, "text": "", "tool_calls": [], "is_streaming": False},
        "metrics": {"total_latency_ms": 1234, "delta_input_tokens": 1500,
                    "delta_output_tokens": 220, "delta_cost_usd": 0.0031},
        "tool_calls": [],
    }
    # Pad prompt+response so the serialized record hits ~target_bytes.
    base = len(json.dumps(rec, separators=(",", ":")))
    pad = max(0, target_bytes - base - 80)
    rec["request"]["body"]["messages"][0]["content"] = "eval prompt " + ("x" * (pad // 2))
    rec["response"]["text"] = "eval response " + ("y" * (pad - pad // 2))
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument("--warmup", type=int, default=100)
    ap.add_argument("--payload-bytes", type=int, default=2048)
    ap.add_argument("--state-dir", default=os.environ.get("DTP_STATE_DIR", ""),
                    help="DTP_STATE_DIR to bench against (default: $DTP_STATE_DIR). "
                         "Use the production NFS location for the honest number.")
    ap.add_argument("--tag", default="", help="label suffix for the output CSV")
    args = ap.parse_args()

    if not args.state_dir:
        raise SystemExit("--state-dir (or $DTP_STATE_DIR) is required — point it at "
                         "the same NFS mount production uses")
    os.environ["DTP_STATE_DIR"] = args.state_dir
    bootstrap_src()
    from chronolog_observability.capture.spool import CaptureSpool

    spool_root = Path(args.state_dir) / "path_a_spool"
    spool = CaptureSpool()  # picks up DTP_STATE_DIR
    session = f"eval-spool-{int(time.time())}"
    host = socket.gethostname()
    fst = fs_type(spool_root)
    print(f"[bench_spool] host={host} state_dir={args.state_dir} fs={fst} "
          f"n={args.n} payload~{args.payload_bytes}B")

    for i in range(args.warmup):
        spool.record_interaction(session, realistic_record(args.payload_bytes, i))

    lat_ms: list[float] = []
    for i in range(args.n):
        rec = realistic_record(args.payload_bytes, i)
        t0 = time.perf_counter_ns()
        spool.record_interaction(session, rec)
        lat_ms.append((time.perf_counter_ns() - t0) / 1e6)

    ms = summarize_ms(lat_ms)
    print_summary("record_interaction", ms)

    # Bytes per record as actually landed on disk (spool side of E1e).
    f = spool_root / f"{session}__interactions.jsonl"
    total = f.stat().st_size
    per_rec = total / (args.n + args.warmup)
    print(f"  spool bytes/record: {per_rec:.0f} B  (file={total} B, {args.n + args.warmup} records)")

    tag = f"_{args.tag}" if args.tag else ""
    write_csv(f"e1a_spool_latency{tag}.csv", ["i", "latency_ms"],
              [(i, f"{v:.4f}") for i, v in enumerate(lat_ms)])
    notes = f"host={host};fs={fst};payload={args.payload_bytes}B;state={args.state_dir}"
    for k in ("p50", "p95", "p99", "mean"):
        append_summary({"experiment": "E1a", "metric": f"spool_append_{k}",
                        "value": f"{ms[k]:.3f}", "unit": "ms", "n": ms["n"], "notes": notes})
    append_summary({"experiment": "E1a", "metric": "spool_bytes_per_record",
                    "value": f"{per_rec:.0f}", "unit": "bytes", "n": args.n + args.warmup,
                    "notes": notes})

    # Leave no eval garbage behind in a production state dir.
    for k in ("interactions", "context_graph", "recovery"):
        p = spool_root / f"{session}__{k}.jsonl"
        if p.is_file():
            p.unlink()


if __name__ == "__main__":
    main()
