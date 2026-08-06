#!/usr/bin/env bash
# E1e/E1f — Storage bytes per event + live-forward wire bytes.
#
# Run AFTER a synth/real run with known event counts:
#   scripts/eval/measure_storage.sh <state_dir> [<chronolog_output_dir>]
#
#   <state_dir>            the run's DTP_STATE_DIR (spool + hot store + rollups)
#   <chronolog_output_dir> the grapher archive, e.g. ~/chronolog-install/chronolog/output
#
# Fills: "Interaction record — bytes (spool/archive)", "Edge event — bytes
# (hot/archive)", "Live forward — bytes per event".
set -euo pipefail
STATE="${1:?usage: measure_storage.sh <state_dir> [<chronolog_output_dir>]}"
ARCHIVE="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

STATE="$STATE" ARCHIVE="$ARCHIVE" EVAL_OUT="$HERE/out" python3 - <<'PY'
import csv, json, os, sys, time
from pathlib import Path

state = Path(os.environ["STATE"])
archive = os.environ.get("ARCHIVE", "")
out_dir = Path(os.environ["EVAL_OUT"]); out_dir.mkdir(parents=True, exist_ok=True)
rows = []

def jsonl_stats(paths):
    total_b = total_n = 0
    for p in paths:
        try:
            b = p.stat().st_size
        except OSError:
            continue
        n = 0
        with open(p, "rb") as f:
            for line in f:
                if line.strip():
                    n += 1
        total_b += b; total_n += n
    return total_b, total_n

def report(label, total_b, total_n):
    per = total_b / total_n if total_n else float("nan")
    print(f"  {label:<42} {total_n:>9} records  {total_b:>12} B  -> {per:8.1f} B/record")
    rows.append([label, total_n, total_b, f"{per:.1f}"])
    return per

print(f"[measure_storage] state={state} archive={archive or '(none)'}")

# ── Spool (Path-A) ─────────────────────────────────────────────────────────
spool = state / "path_a_spool"
report("spool: interactions.jsonl", *jsonl_stats(spool.glob("*__interactions.jsonl")))
report("spool: context_graph.jsonl", *jsonl_stats(spool.glob("*__context_graph.jsonl")))

# ── Hot store (Path-B) + fleet rollups ─────────────────────────────────────
hot_files = list((state / "inter_agent").glob("*.jsonl"))
report("hot store: inter_agent/*.jsonl (edge events)", *jsonl_stats(hot_files))
report("fleet: rollup.jsonl (buckets)", *jsonl_stats((state / "fleet").glob("rollup.jsonl")))

# ── E1f: live-forward wire bytes per event ─────────────────────────────────
# The collector forwards {"events":[...]} whose elements ARE the event dicts —
# so the wire cost is computable offline from the hot store.
events = []
for p in hot_files:
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
if events:
    single = sum(len(json.dumps({"events": [e]}).encode()) for e in events) / len(events)
    B = 64  # collector max_batch
    batched_total = 0
    for i in range(0, len(events), B):
        batched_total += len(json.dumps({"events": events[i:i+B]}).encode())
    batched = batched_total / len(events)
    print(f"  live-forward wire bytes/event: {single:.0f} B (single-event body), "
          f"{batched:.0f} B (amortized, batch<=64)")
    rows.append(["live-forward: single-event body", len(events), "", f"{single:.1f}"])
    rows.append(["live-forward: batch<=64 amortized", len(events), "", f"{batched:.1f}"])
else:
    print("  live-forward: no hot-store events found")

# ── Archive (grapher CSVs), grouped by chronicle.story ─────────────────────
if archive:
    groups = {}
    for p in Path(archive).glob("*.csv"):
        toks = p.name.split(".")
        key = ".".join(toks[:2]) if len(toks) >= 3 else p.name
        b = p.stat().st_size
        n = 0
        with open(p, "rb") as f:
            for line in f:
                if line.strip():
                    n += 1
        gb, gn = groups.get(key, (0, 0))
        groups[key] = (gb + b, gn + n)
    for key in sorted(groups):
        b, n = groups[key]
        report(f"archive: {key}.*.csv", b, n)
    if not groups:
        print(f"  archive: no CSVs under {archive}")

with open(out_dir / "e1e_storage.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["store", "records", "total_bytes", "bytes_per_record"])
    w.writerows(rows)
print(f"  wrote {out_dir / 'e1e_storage.csv'}")

with open(out_dir / "summary.csv", "a", newline="") as f:
    w = csv.writer(f)
    if f.tell() == 0:
        w.writerow(["experiment", "metric", "value", "unit", "n", "notes", "utc"])
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for label, n, b, per in rows:
        exp = "E1f" if label.startswith("live-forward") else "E1e"
        w.writerow([exp, label, per, "bytes/record", n, f"state={state}", ts])
PY
