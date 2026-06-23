#!/usr/bin/env bash
# allocate-and-run.sh — grab N real ARES nodes, wait until they're granted, then
# run the live real-agent test on them.
#
# SLURM only ever hands you IDLE nodes, so "run it on real idle nodes we can use"
# is automatic: salloc puts the request in the queue and returns the moment N
# free nodes exist. (Pass IMMEDIATE=1 to instead fail fast right now if N nodes
# are not already free — i.e. "only run if idle this second".)
#
# Usage:
#   scripts/allocate-and-run.sh <num_nodes> [scenario] [partition]
#   IMMEDIATE=1 scripts/allocate-and-run.sh 4            # only if 4 are free now
#   ALLOC_TIME=01:00:00 scripts/allocate-and-run.sh 8 my-run compute
#
# Release the nodes when done:  scancel <jobid>   (printed at the end)
set -euo pipefail

N="${1:?usage: $0 <num_nodes> [scenario] [partition]}"
SCENARIO="${2:-live-$(date +%Y%m%d-%H%M)}"
PARTITION="${3:-compute}"
TIME="${ALLOC_TIME:-02:00:00}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[alloc] idle nodes on '$PARTITION' right now:"
sinfo -p "$PARTITION" -t idle -h -o '   %D idle: %N' || true

IMM=()
if [ "${IMMEDIATE:-0}" = "1" ]; then
  IMM=(-I)   # -I/--immediate: don't queue; fail if N nodes aren't free now.
  echo "[alloc] IMMEDIATE mode — will only proceed if $N node(s) are free this second."
fi

echo "[alloc] requesting $N node(s) on '$PARTITION' for $TIME ..."
# --no-shell holds the allocation as a queryable job without an interactive shell.
salloc -N "$N" -p "$PARTITION" -t "$TIME" --no-shell -J chronolog-live "${IMM[@]}" \
  > /tmp/chronolog_salloc.out 2>&1 || {
    echo "[alloc] salloc failed:"; cat /tmp/chronolog_salloc.out; exit 1; }
cat /tmp/chronolog_salloc.out

JOB_ID="$(grep -oE 'Granted job allocation [0-9]+' /tmp/chronolog_salloc.out | grep -oE '[0-9]+' | tail -1)"
[ -n "$JOB_ID" ] || { echo "[alloc] could not parse a job id" >&2; exit 1; }
echo "[alloc] job $JOB_ID granted"

# Wait until the job is RUNNING and has nodes (usually instant with --no-shell).
NODES=""
for _ in $(seq 1 120); do
  ST="$(squeue -j "$JOB_ID" -h -o '%T' 2>/dev/null || echo GONE)"
  [ "$ST" = "GONE" ] && { echo "[alloc] job $JOB_ID vanished" >&2; exit 1; }
  if [ "$ST" = "RUNNING" ]; then
    NODES="$(squeue -j "$JOB_ID" -h -o '%N')"
    [ -n "$NODES" ] && break
  fi
  echo "[alloc] state=$ST — waiting for nodes ..."; sleep 5
done
[ -n "$NODES" ] || { echo "[alloc] timed out waiting for nodes" >&2; scancel "$JOB_ID"; exit 1; }
echo "[alloc] RUNNING on: $NODES"

# ── deploy ChronoLog on the allocation (run-live-agents.sh only *checks* it) ──
THESIS="${THESIS_DIR:-/mnt/common/$USER/chronolog-thesis}"
if [ "${SKIP_CHRONOLOG:-0}" != "1" ]; then
  if [ -x "$THESIS/launch_chronolog.sh" ]; then
    echo "[chronolog] deploying cluster on job $JOB_ID ..."
    "$THESIS/launch_chronolog.sh" "$JOB_ID" || {
      echo "[chronolog] launch_chronolog.sh failed — freeing nodes" >&2; scancel "$JOB_ID"; exit 1; }
    # Wait until every chrono-* process is up before spending any LLM budget.
    echo "[chronolog] waiting for healthcheck to go green ..."
    ok=0
    for _ in $(seq 1 30); do
      if "$THESIS/healthcheck.sh" "$JOB_ID" >/tmp/chronolog_health.out 2>&1; then ok=1; break; fi
      sleep 5
    done
    if [ "$ok" != "1" ]; then
      echo "[chronolog] cluster did not come up; last healthcheck:" >&2
      tail -20 /tmp/chronolog_health.out >&2
      echo "[chronolog] freeing nodes (scancel $JOB_ID)"; scancel "$JOB_ID"; exit 1
    fi
    echo "[chronolog] cluster healthy."
  else
    echo "[chronolog] WARN: $THESIS/launch_chronolog.sh not found — assuming ChronoLog is already up"
  fi
fi

echo "[run] launching live real-agent test (job $JOB_ID, scenario=$SCENARIO) ..."
echo "[run] when finished, free the nodes with:  scancel $JOB_ID"
"$HERE/run-live-agents.sh" "$JOB_ID" "$SCENARIO"
rc=$?
echo "[done] test finished (rc=$rc). Nodes still held by job $JOB_ID."
echo "[done] dashboard is on the 2nd node:5000 — see the run output above for the tunnel line."
echo "[done] free the nodes when finished:  scancel $JOB_ID"
exit $rc
