#!/bin/bash
# burst.sh — fire synthetic live inter-agent traffic at the CURRENT cluster, on
# demand, with NO LLM cost. Watch it land in Scenarios (Live) + Cluster + Memory.
#
# Auto-detects your running SLURM job and the dashboard node (NODES[1] by the
# launch convention; override with FLASK_NODE=...).
#
# Usage:
#   scripts/ops/burst.sh [scenario] [agents] [rounds] [interval]
#
# Examples:
#   scripts/ops/burst.sh                      # one agent per node, 40 rounds, scenario=demo-live
#   scripts/ops/burst.sh demo-multi 12        # 12 agents over the nodes -> some nodes get 2-3 agents
#   scripts/ops/burst.sh quick 6 15 0.5       # short, fast burst
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SCEN="${1:-demo-live}"
AGENTS="${2:-}"
ROUNDS="${3:-40}"
INTERVAL="${4:-1}"

JOB="$(squeue -u "$USER" -h -o '%i' | head -1)"
[ -z "$JOB" ] && { echo "no running SLURM job for $USER — bring a cluster up first"; exit 1; }

NODES=( $(scontrol show hostnames "$(squeue -j "$JOB" -h -o '%N')") )
[ "${#NODES[@]}" -eq 0 ] && { echo "job $JOB has no nodes"; exit 1; }
FLASK_NODE="${FLASK_NODE:-${NODES[1]:-${NODES[0]}}}"
HOSTS="$(IFS=,; echo "${NODES[*]}")"
[ -z "$AGENTS" ] && AGENTS="${#NODES[@]}"
export DTP_STATE_DIR="${DTP_STATE_DIR:-/mnt/common/$USER/observe-state}"

echo "burst -> http://$FLASK_NODE:5000  scenario=$SCEN  agents=$AGENTS  rounds=$ROUNDS  over ${#NODES[@]} nodes"
exec python3 "$REPO/scripts/demos/synth_live_traffic.py" \
  --flask-url "http://$FLASK_NODE:5000" --scenario "$SCEN" \
  --hosts "$HOSTS" --agents "$AGENTS" --rounds "$ROUNDS" --interval "$INTERVAL" \
  --spool --state-dir "$DTP_STATE_DIR"
