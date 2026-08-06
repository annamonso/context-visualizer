#!/usr/bin/env bash
# E3c — agents-per-node sweep: K real agents on each of M nodes, one round,
# against the ALREADY-RUNNING deployment (does NOT relaunch dashboard or
# collectors, unlike run-live-agents.sh).
#
# Usage:
#   scripts/eval/e3c_sweep.sh <FLASK_URL> <K> <node> [<node>...]
#
# Env: STATE_DIR (default $DTP_STATE_DIR), AGENT_ENV (iowarp), ROUNDS (1),
#      MODEL (claude-haiku-4-5), DELEGATION_MODEL (claude-sonnet-4-6)
#
# Each agent gets a unique role; peers form a ring across ALL agents so edges
# cross nodes. Exit status 0 = every agent process exited cleanly (that is the
# "ran cleanly" criterion for the max-K×M claim; verify edges on the dashboard).
set -uo pipefail
FLASK_URL="${1:?flask url}"; K="${2:?agents per node}"; shift 2
NODES=("$@")
[ "${#NODES[@]}" -ge 2 ] || { echo "need >=2 nodes"; exit 2; }
STATE_DIR="${STATE_DIR:-${DTP_STATE_DIR:?set STATE_DIR}}"
AGENT_ENV="${AGENT_ENV:-iowarp}"
ROUNDS="${ROUNDS:-1}"
MODEL="${MODEL:-claude-haiku-4-5}"
DELEGATION_MODEL="${DELEGATION_MODEL:-claude-sonnet-4-6}"
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
SCENARIO="eval-k${K}-$(date +%H%M%S)"

ALL_ROLES=(planner executor fetcher analyst retriever summarizer)
AGENT_NODES=(); ROLES=()
idx=0
for node in "${NODES[@]}"; do
  for (( k=0; k<K; k++ )); do
    base="${ALL_ROLES[$(( idx % ${#ALL_ROLES[@]} ))]}"
    rep=$(( idx / ${#ALL_ROLES[@]} ))
    role="$base"; [ "$rep" -gt 0 ] && role="${base}$((rep+1))"
    AGENT_NODES+=("$node"); ROLES+=("$role"); idx=$((idx+1))
  done
done
N=${#ROLES[@]}
echo "[e3c] K=$K on ${#NODES[@]} nodes -> $N agents, scenario=$SCENARIO"

pids=(); fails=0
for i in "${!AGENT_NODES[@]}"; do
  node="${AGENT_NODES[$i]}"; role="${ROLES[$i]}"
  j=$(( (i+1) % N ))
  peers="${AGENT_NODES[$j]}:${ROLES[$j]}"
  ssh -n -o BatchMode=yes "$node" "
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    source \"\$HOME/miniconda3/etc/profile.d/conda.sh\" 2>/dev/null || true
    conda activate '$AGENT_ENV' 2>/dev/null || true
    unset ANTHROPIC_API_KEY
    export PYTHONPATH='$REPO/src':\${PYTHONPATH:-}
    export TMPDIR=/mnt/nvme/\$USER/chronolog-tmp; mkdir -p \"\$TMPDIR\"
    python3 '$REPO/scripts/real_agent_chronolog.py' \
      --flask-url '$FLASK_URL' --collector-url http://127.0.0.1:5650 \
      --scenario '$SCENARIO' --my-host '$node' --my-role '$role' \
      --peers '$peers' --model '$MODEL' --delegation-model '$DELEGATION_MODEL' \
      --state-dir '$STATE_DIR' --rounds '$ROUNDS'
  " > "$HERE/out/e3c_k${K}_${node}_${role}.log" 2>&1 &
  pids+=("$!")
done
for p in "${pids[@]}"; do wait "$p" || fails=$((fails+1)); done
edges=$(curl -sf --max-time 20 "$FLASK_URL/api/scenarios/$SCENARIO/inter-agent" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
echo "[e3c] K=$K done: $((N-fails))/$N agents clean, $edges edge records in scenario $SCENARIO"
echo "K=$K,nodes=${#NODES[@]},agents=$N,clean=$((N-fails)),edge_records=$edges,scenario=$SCENARIO" >> "$HERE/out/e3c_sweep.csv"
[ "$fails" -eq 0 ]
