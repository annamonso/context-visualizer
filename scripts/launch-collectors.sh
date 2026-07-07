#!/bin/bash
# launch-collectors.sh — start one collector daemon per compute node.
#
# Each collector accepts events from local agents on 127.0.0.1:<port>,
# writes them straight to ChronoLog via the node-local keeper, and forwards
# a thin copy to the central dashboard for the live SSE view (see
# backend/collector/).
#
# Usage (from the master, after the ChronoLog cluster is up):
#
#     scripts/launch-collectors.sh <SLURM_JOBID> <DASHBOARD_URL> [<port>]
#
# Example:
#     scripts/launch-collectors.sh 20847 http://ares-comp-10:5000 5650
#
# Stop:
#     for n in $(scontrol show hostnames "$(squeue -j <JOBID> -h -o '%N')"); do
#       ssh "$n" 'pkill -f backend.collector'; done

set -euo pipefail

JOB_ID="${1:-}"
DASHBOARD_URL="${2:-}"
COLLECTOR_PORT="${3:-5650}"

if [[ -z "$JOB_ID" || -z "$DASHBOARD_URL" ]]; then
  echo "Usage: $0 <SLURM_JOBID> <DASHBOARD_URL> [collector-port]"
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHRONO_HOME="${CHRONOLOG_HOME:-$HOME/chronolog-install/chronolog}"

NODES=( $(scontrol show hostnames "$(squeue -j "$JOB_ID" -h -o '%N')") )
VISOR_NODE="${NODES[0]}"
VISOR_IP=$(getent hosts "${VISOR_NODE}-40g" | awk '{print $1; exit}')

if [[ -z "$VISOR_IP" ]]; then
  echo "FATAL: cannot resolve ${VISOR_NODE}-40g — is the allocation still alive?"
  exit 1
fi

echo "=== per-node collector launch ==="
echo "  SLURM job:  $JOB_ID"
echo "  Nodes:      ${NODES[*]}"
echo "  Visor IP:   $VISOR_IP (on $VISOR_NODE)"
echo "  Dashboard:  $DASHBOARD_URL"
echo "  Port:       127.0.0.1:$COLLECTOR_PORT on each node"

IDX=0
for NODE in "${NODES[@]}"; do
  # Node-unique reader/query port: the ChronoLog client registers a query
  # callback on its node-local 40g IP, and stale registrations from killed
  # processes linger — offset by node index to stay clash-free. 5680+ keeps
  # clear of the dashboard (5557) and the context-visualizer range (5660+).
  QUERY_PORT=$((5680 + IDX))
  IDX=$((IDX + 1))

  ssh -o BatchMode=yes "$NODE" bash -s -- \
      "$JOB_ID" "$VISOR_IP" "$DASHBOARD_URL" "$REPO_ROOT" "$CHRONO_HOME" \
      "$COLLECTOR_PORT" "$QUERY_PORT" <<'REMOTE_SCRIPT'
set -euo pipefail
JOB_ID="$1"; VISOR_IP="$2"; DASHBOARD_URL="$3"; REPO_ROOT="$4"; CHRONO_HOME="$5"
COLLECTOR_PORT="$6"; QUERY_PORT="$7"

source /etc/profile.d/lmod.sh 2>/dev/null || true
module load python/3.11.9-zg4555e

mkdir -p "/mnt/nvme/$USER/chronolog-tmp"
export TMPDIR="/mnt/nvme/$USER/chronolog-tmp"

export LD_LIBRARY_PATH="$CHRONO_HOME/lib:${LD_LIBRARY_PATH:-}"
USER_310="$HOME/.local/lib/python3.10/site-packages"
export PYTHONPATH="$CHRONO_HOME/lib:$REPO_ROOT/src:$USER_310:${PYTHONPATH:-}"

export CHRONOLOG_VISOR_IP="$VISOR_IP"
export CHRONOLOG_QUERY_PORT="$QUERY_PORT"

cd "$REPO_ROOT"
LOGDIR="$REPO_ROOT/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/collector-${JOB_ID}-$(hostname).log"

nohup python3 -m backend.collector \
      --flask-url "$DASHBOARD_URL" --port "$COLLECTOR_PORT" \
      > "$LOG" 2>&1 &

echo "[remote $(hostname)] collector pid=$! port=$COLLECTOR_PORT query_port=$QUERY_PORT (log $LOG)"
REMOTE_SCRIPT
done

echo
echo "Agents on each node can now POST to http://127.0.0.1:$COLLECTOR_PORT/ingest"
echo "(or use backend.collector.emit)."
