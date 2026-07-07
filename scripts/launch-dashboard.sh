#!/bin/bash
# launch-dashboard.sh — start the chronolog-observability dashboard on a compute
# node that is a member of the ChronoLog allocation.
#
# Must run ON a compute node in the allocation: Mercury's ofi+sockets provider
# does not cross the master(172.20.x)/compute(172.25.x) subnet boundary, and the
# dashboard's ChronoLog client registers a query callback on this node's 40g IP.
#
# Wires, per the ARES gotchas:
#   * module python/3.11.9 (ABI of py_chronolog_client.cpython-311 .so)
#   * LD_LIBRARY_PATH / PYTHONPATH to the ChronoLog install + repo/src
#   * TMPDIR on NVMe (/ is full on ARES compute nodes)
#   * CHRONOLOG_VISOR_IP = RAW IP of the visor node (NOT the -40g hostname)
#   * CHRONOLOG_NODES = the allocation nodelist (Cluster tab membership filter)
#   * CHRONOLOG_CAPTURE=1 → drain the shared Path-A spool into ChronoLog
#   * DTP_STATE_DIR = shared NFS spool dir (agents write here; this worker drains)
#
# Usage (from the master; it ssh-es to the flask node):
#   scripts/launch-dashboard.sh <SLURM_JOBID> <FLASK_NODE> [PORT] [QUERY_PORT]

set -euo pipefail

JOB_ID="${1:-}"
FLASK_NODE="${2:-}"
PORT="${3:-5000}"
QUERY_PORT="${4:-5557}"

if [[ -z "$JOB_ID" || -z "$FLASK_NODE" ]]; then
  echo "Usage: $0 <SLURM_JOBID> <FLASK_NODE> [PORT] [QUERY_PORT]" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHRONO_HOME="${CHRONOLOG_HOME:-$HOME/chronolog-install/chronolog}"
STATE_DIR="${DTP_STATE_DIR:-/mnt/common/$USER/observe-state}"

NODES=( $(scontrol show hostnames "$(squeue -j "$JOB_ID" -h -o '%N')") )
if [[ ${#NODES[@]} -eq 0 ]]; then
  echo "FATAL: SLURM job $JOB_ID has no nodes" >&2; exit 1
fi
VISOR_NODE="${NODES[0]}"
NODELIST="$(squeue -j "$JOB_ID" -h -o '%N')"
VISOR_IP="$(getent hosts "${VISOR_NODE}-40g" | awk '{print $1; exit}')"
if [[ -z "$VISOR_IP" ]]; then
  echo "FATAL: cannot resolve ${VISOR_NODE}-40g to an IPv4" >&2; exit 1
fi

echo "=== launch chronolog-observability dashboard ==="
echo "  SLURM job:    $JOB_ID  (nodes: ${NODES[*]})"
echo "  Flask node:   $FLASK_NODE:$PORT"
echo "  Visor IP:     $VISOR_IP  (on $VISOR_NODE)"
echo "  Spool dir:    $STATE_DIR  (shared, capture-on)"
echo "  Query port:   $QUERY_PORT"

mkdir -p "$STATE_DIR"

ssh -o BatchMode=yes "$FLASK_NODE" bash -s -- \
    "$JOB_ID" "$VISOR_IP" "$NODELIST" "$REPO_ROOT" "$CHRONO_HOME" \
    "$PORT" "$QUERY_PORT" "$STATE_DIR" <<'REMOTE'
set -euo pipefail
JOB_ID="$1"; VISOR_IP="$2"; NODELIST="$3"; REPO_ROOT="$4"; CHRONO_HOME="$5"
PORT="$6"; QUERY_PORT="$7"; STATE_DIR="$8"

# kill any prior dashboard on this node
pkill -f 'backend.app' 2>/dev/null || true
pkill -f 'chronolog-observe' 2>/dev/null || true
sleep 1

source /etc/profile.d/lmod.sh 2>/dev/null || true
module load python/3.11.9-zg4555e

export TMPDIR="/mnt/nvme/$USER/chronolog-tmp"; mkdir -p "$TMPDIR"
export LD_LIBRARY_PATH="$CHRONO_HOME/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$CHRONO_HOME/lib:$REPO_ROOT/src:${PYTHONPATH:-}"

export CHRONOLOG_VISOR_IP="$VISOR_IP"
export CHRONOLOG_VISOR_PROTOCOL="ofi+sockets"
export CHRONOLOG_QUERY_PORT="$QUERY_PORT"
export CHRONOLOG_NODES="$NODELIST"
export CHRONOLOG_OUTPUT_DIR="$CHRONO_HOME/output"
# Dashboard is a PURE READER. We deliberately do NOT set CHRONOLOG_CAPTURE here:
# the capture worker's startup warm_up does a blocking ReplayStory (the
# py_chronolog_client holds the GIL through it), which would stall Flask's
# app.run() from ever binding. Path-A draining runs as a SEPARATE process
# (scripts/launch-capture.sh) so the dashboard stays responsive.
export DTP_STATE_DIR="$STATE_DIR"
export OBSERVE_PORT="$PORT"
unset CHRONOLOG_OFFLINE

cd "$REPO_ROOT"
LOGDIR="$REPO_ROOT/logs"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/dashboard-${JOB_ID}-$(hostname).log"

setsid nohup python3 -m backend.app > "$LOG" 2>&1 &
echo "[remote $(hostname)] dashboard pid=$! port=$PORT query_port=$QUERY_PORT (log $LOG)"
REMOTE

echo
echo "Dashboard URL: http://${FLASK_NODE}:${PORT}/"
echo "Tail log:      tail -f $REPO_ROOT/logs/dashboard-${JOB_ID}-${FLASK_NODE}.log"
