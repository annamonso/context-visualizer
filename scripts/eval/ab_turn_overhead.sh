#!/usr/bin/env bash
# E1c — End-to-end turn delta: instrumented vs. uninstrumented, interleaved A/B.
#
# Runs real_agent_chronolog.py repeatedly with --rounds 1, alternating capture
# ON (normal) and OFF (EVAL_NO_CAPTURE=1 turns spool_turn + emit_edge into
# no-ops), interleaved to absorb provider latency drift. Per-turn wall-clock
# lands in a shared timing file via EVAL_TIMING_FILE (mode,role,kind,seq,
# wall_ms,api_ms), then the stats block reports mean delta ± CI and %% of turn.
#
# Prereqs: same as the live harness — conda env with claude_agent_sdk + `claude`
# CLI auth; a running collector+dashboard (or at least reachable FLASK_URL).
# Run it INSIDE the env:  conda run -n iowarp scripts/eval/ab_turn_overhead.sh
#
# Env (required): FLASK_URL, MY_HOST, PEERS   (as for real_agent_chronolog.py)
# Env (optional): N_PAIRS=10  SCENARIO=eval-ab  MY_ROLE=planner
#                 COLLECTOR_URL=http://127.0.0.1:5650  STATE_DIR=$DTP_STATE_DIR
#                 MODEL=claude-haiku-4-5  DELEGATION_MODEL=claude-sonnet-4-6
#
# Fills: Table tab:overhead "End-to-end turn delta — % of turn" (§6.2 headline).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

: "${FLASK_URL:?set FLASK_URL (dashboard base URL)}"
: "${MY_HOST:?set MY_HOST (this node, as SLURM names it)}"
: "${PEERS:?set PEERS (host:role[,host:role...])}"
N_PAIRS="${N_PAIRS:-10}"
SCENARIO="${SCENARIO:-eval-ab-$(date +%s)}"
MY_ROLE="${MY_ROLE:-planner}"
COLLECTOR_URL="${COLLECTOR_URL:-http://127.0.0.1:5650}"
STATE_DIR="${STATE_DIR:-${DTP_STATE_DIR:-}}"
MODEL="${MODEL:-claude-haiku-4-5}"
DELEGATION_MODEL="${DELEGATION_MODEL:-claude-sonnet-4-6}"

mkdir -p "$HERE/out"
TF="$HERE/out/e1c_turn_timings.csv"
if [ ! -s "$TF" ]; then
  echo "mode,role,kind,seq,wall_ms,api_ms" > "$TF"
fi

run_one() { # $1 = "on"|"off"
  local extra_env=()
  if [ "$1" = off ]; then extra_env+=(EVAL_NO_CAPTURE=1); fi
  env "${extra_env[@]}" EVAL_TIMING_FILE="$TF" PYTHONPATH="$REPO/src" \
    python3 "$REPO/scripts/real_agent_chronolog.py" \
      --flask-url "$FLASK_URL" --collector-url "$COLLECTOR_URL" \
      --scenario "$SCENARIO-$1" --my-host "$MY_HOST" --my-role "$MY_ROLE" \
      --peers "$PEERS" --rounds 1 --model "$MODEL" \
      --delegation-model "$DELEGATION_MODEL" \
      ${STATE_DIR:+--state-dir "$STATE_DIR"} \
    || echo "  ! run ($1) failed — continuing" >&2
}

echo "[ab_turn_overhead] $N_PAIRS interleaved pairs, 3 turns each -> ~$((N_PAIRS*3)) turns/mode"
for i in $(seq 1 "$N_PAIRS"); do
  echo "── pair $i/$N_PAIRS ──"
  if [ $((i % 2)) -eq 1 ]; then run_one on;  run_one off
  else                          run_one off; run_one on
  fi
done

# ── Stats ────────────────────────────────────────────────────────────────────
TF="$TF" python3 - <<'PY'
import csv, math, os, statistics, time
tf = os.environ["TF"]
by = {"instrumented": [], "uninstrumented": []}
with open(tf) as f:
    for row in csv.DictReader(f):
        try:
            by[row["mode"]].append(float(row["wall_ms"]))
        except (KeyError, ValueError):
            pass
a, b = by["instrumented"], by["uninstrumented"]
if len(a) < 2 or len(b) < 2:
    raise SystemExit(f"not enough samples (on={len(a)}, off={len(b)})")
ma, mb = statistics.fmean(a), statistics.fmean(b)
va, vb = statistics.variance(a), statistics.variance(b)
delta = ma - mb
se = math.sqrt(va/len(a) + vb/len(b))
ci = 1.96 * se
t = delta / se if se else float("inf")
print(f"\n  instrumented:   n={len(a)}  mean={ma:.0f}ms  sd={math.sqrt(va):.0f}ms")
print(f"  uninstrumented: n={len(b)}  mean={mb:.0f}ms  sd={math.sqrt(vb):.0f}ms")
print(f"  delta = {delta:+.0f}ms ± {ci:.0f}ms (95% CI)  = {100*delta/mb:+.2f}% of mean turn"
      f"  (Welch t={t:.2f})")
if abs(t) < 1.96:
    print("  -> statistically indistinguishable from zero at α=0.05 "
          "(the honest headline combines this with E1a+E1b: overhead = "
          "spool+emit ms on a turn of mean-turn s)")
out = os.path.join(os.path.dirname(tf), "summary.csv")
new = not (os.path.isfile(out) and os.path.getsize(out) > 0)
with open(out, "a", newline="") as f:
    w = csv.writer(f)
    if new:
        w.writerow(["experiment", "metric", "value", "unit", "n", "notes", "utc"])
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n = f"{len(a)}+{len(b)}"
    notes = f"ci95={ci:.1f}ms;welch_t={t:.2f}"
    w.writerow(["E1c", "turn_delta_mean", f"{delta:.1f}", "ms", n, notes, ts])
    w.writerow(["E1c", "turn_delta_pct_of_turn", f"{100*delta/mb:.2f}", "%", n, notes, ts])
    w.writerow(["E1c", "turn_mean_uninstrumented", f"{mb:.0f}", "ms", len(b), "", ts])
PY
