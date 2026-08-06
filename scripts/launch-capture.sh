#!/bin/bash
# launch-capture.sh — start the standalone Path-A capture worker on a node.
#
# Drains the shared capture spool (DTP_STATE_DIR/path_a_spool) into ChronoLog.
# Runs as its OWN process so its startup warm_up (a blocking, GIL-holding
# ReplayStory) never stalls the dashboard's Flask server. Uses a query port
# distinct from the dashboard (5557) and the collectors (5680+).
#
# Usage (from the master): scripts/launch-capture.sh <SLURM_JOBID> <NODE> [QUERY_PORT]

set -euo pipefail

JOB_ID="${1:-}"
NODE="${2:-}"
QUERY_PORT="${3:-5570}"

if [[ -z "$JOB_ID" || -z "$NODE" ]]; then
  echo "Usage: $0 <SLURM_JOBID> <NODE> [QUERY_PORT]" >&2; exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHRONO_HOME="${CHRONOLOG_HOME:-$HOME/chronolog-install/chronolog}"
STATE_DIR="${DTP_STATE_DIR:-/mnt/common/$USER/observe-state}"

NODES=( $(scontrol show hostnames "$(squeue -j "$JOB_ID" -h -o '%N')") )
VISOR_IP="$(getent hosts "${NODES[0]}-40g" | awk '{print $1; exit}')"
[[ -n "$VISOR_IP" ]] || { echo "FATAL: cannot resolve visor IP" >&2; exit 1; }

ssh -o BatchMode=yes "$NODE" bash -s -- \
    "$JOB_ID" "$VISOR_IP" "$REPO_ROOT" "$CHRONO_HOME" "$QUERY_PORT" "$STATE_DIR" <<'REMOTE'
set -euo pipefail
JOB_ID="$1"; VISOR_IP="$2"; REPO_ROOT="$3"; CHRONO_HOME="$4"; QUERY_PORT="$5"; STATE_DIR="$6"
pkill -f 'chronolog_observability.capture.sync_worker' 2>/dev/null || true
source /etc/profile.d/lmod.sh 2>/dev/null || true
module load python/3.11.9-zg4555e
export TMPDIR="/mnt/nvme/$USER/chronolog-tmp"; mkdir -p "$TMPDIR"
export LD_LIBRARY_PATH="$CHRONO_HOME/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$CHRONO_HOME/lib:$REPO_ROOT/src:${PYTHONPATH:-}"
export CHRONOLOG_VISOR_IP="$VISOR_IP"
export CHRONOLOG_QUERY_PORT="$QUERY_PORT"
export DTP_STATE_DIR="$STATE_DIR"
# warm_up replays every known session; a story whose playback response never
# arrives burns the ~180 s query timeout EACH — with dozens of sessions the
# worker spends an hour "warming up" before it drains a single spool record.
# Archive-first makes warm-up read the drained CSVs in milliseconds; the
# drain writes (append_event) don't go through this path and are unaffected.
export CHRONOLOG_ARCHIVE_FIRST=1
unset CHRONOLOG_OFFLINE
cd "$REPO_ROOT"; LOGDIR="$REPO_ROOT/logs"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/capture-${JOB_ID}-$(hostname).log"
setsid nohup python3 -m chronolog_observability.capture.sync_worker --interval 5 \
      > "$LOG" 2>&1 &
echo "[remote $(hostname)] capture worker pid=$! query_port=$QUERY_PORT (log $LOG)"
REMOTE
