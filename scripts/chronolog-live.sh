#!/usr/bin/env bash
# chronolog-live.sh — bring ChronoLog LIVE and serve the dashboard from it.
#
# ChronoLog being live is the whole point of this project, so this is the
# canonical "real, not offline" bring-up:
#   1. allocate N idle ARES nodes (SLURM only grants idle ones)
#   2. deploy the ChronoLog cluster on them (visor/keepers/grapher/player)
#   3. wait until the healthcheck is green
#   4. start the dashboard ON a compute node, connected to the LIVE visor
#      (NOT offline) — Mercury requires the dashboard to sit inside the subnet.
#
# Then drive it from the UI's "Generate traffic" (synthetic, real nodes, NO LLM
# cost) — every edge is written through the live ChronoLog. For real agents +
# LLM use scripts/allocate-and-run.sh instead.
#
# Usage:
#   scripts/chronolog-live.sh [num_nodes] [partition]
#   scripts/chronolog-live.sh 4 compute
#
# Release when done:  scancel <jobid>   (printed at the end)
set -uo pipefail

N="${1:-4}"
PARTITION="${2:-compute}"
TIME="${ALLOC_TIME:-02:00:00}"
HERE="$(cd "$(dirname "$0")" && pwd)"
THESIS="${THESIS_DIR:-/mnt/common/$USER/chronolog-thesis}"
export DTP_STATE_DIR="${DTP_STATE_DIR:-/mnt/common/$USER/observe-state}"

# Login host used only for the SSH tunnel hint printed at the end. Override for
# your own cluster: SSH_GATEWAY=login.mycluster.edu scripts/chronolog-live.sh
SSH_GATEWAY="${SSH_GATEWAY:-<cluster-login-host>}"
SSH_USER="${SSH_USER:-$USER}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

echo "[live] idle nodes on '$PARTITION' now:"; sinfo -p "$PARTITION" -t idle -h -o '   %D idle: %N' || true

# 1. allocate ---------------------------------------------------------------
echo "[live] requesting $N node(s) on '$PARTITION' for $TIME ..."
salloc -N "$N" -p "$PARTITION" -t "$TIME" --no-shell -J chronolog-live \
  > /tmp/cl_salloc.out 2>&1 || { echo "[live] salloc failed:"; cat /tmp/cl_salloc.out; exit 1; }
cat /tmp/cl_salloc.out
JOB_ID="$(grep -oE 'Granted job allocation [0-9]+' /tmp/cl_salloc.out | grep -oE '[0-9]+' | tail -1)"
[ -n "$JOB_ID" ] || { echo "[live] no job id parsed" >&2; exit 1; }

NODES=()
for _ in $(seq 1 60); do
  ST="$(squeue -j "$JOB_ID" -h -o '%T' 2>/dev/null || echo GONE)"
  [ "$ST" = GONE ] && { echo "[live] job vanished" >&2; exit 1; }
  if [ "$ST" = RUNNING ]; then
    NODES=( $(scontrol show hostnames "$(squeue -j "$JOB_ID" -h -o '%N')") )
    [ "${#NODES[@]}" -gt 0 ] && break
  fi
  echo "[live] state=$ST — waiting ..."; sleep 4
done
[ "${#NODES[@]}" -ge 3 ] || { echo "[live] need >=3 nodes for a ChronoLog layout (got ${#NODES[*]})"; scancel "$JOB_ID"; exit 1; }
FLASK_NODE="${NODES[1]}"
echo "[live] job $JOB_ID on: ${NODES[*]}   (dashboard -> $FLASK_NODE)"

# 2. deploy ChronoLog -------------------------------------------------------
if [ -x "$THESIS/launch_chronolog.sh" ]; then
  echo "[live] deploying ChronoLog ..."
  "$THESIS/launch_chronolog.sh" "$JOB_ID" || { echo "[live] deploy failed"; scancel "$JOB_ID"; exit 1; }
else
  echo "[live] FATAL: $THESIS/launch_chronolog.sh not found" >&2; scancel "$JOB_ID"; exit 1
fi

# 3. wait for green ---------------------------------------------------------
echo "[live] waiting for ChronoLog healthcheck ..."
ok=0
for _ in $(seq 1 36); do
  if "$THESIS/healthcheck.sh" "$JOB_ID" >/tmp/cl_health.out 2>&1; then ok=1; break; fi
  sleep 5
done
[ "$ok" = 1 ] || { echo "[live] cluster not healthy:"; tail -20 /tmp/cl_health.out; scancel "$JOB_ID"; exit 1; }
echo "[live] ChronoLog is healthy."

# 4. dashboard (live reader on the flask node) ------------------------------
echo "[live] starting dashboard on $FLASK_NODE (connected to the live visor) ..."
DTP_STATE_DIR="$DTP_STATE_DIR" "$HERE/launch-dashboard.sh" "$JOB_ID" "$FLASK_NODE" 5000 || {
  echo "[live] dashboard launch reported an error"; }
DASH_URL="http://${FLASK_NODE}:5000"

# 5. complete the per-host capture stack ------------------------------------
# Keepers are already on every node (the deploy). Now add a Path-A capture
# worker (drains the shared spool -> ChronoLog) and a collector ON EVERY NODE
# (Path-B inter-agent edges -> that node's keeper). After this every host has
# keeper + collector and ALL data is captured into ChronoLog.
echo "[live] starting Path-A capture worker on $FLASK_NODE ..."
DTP_STATE_DIR="$DTP_STATE_DIR" "$HERE/launch-capture.sh" "$JOB_ID" "$FLASK_NODE" 5570 \
  || echo "[live] WARN: capture worker launch reported an error"

echo "[live] starting a collector on EVERY node (per-host Path-B capture) ..."
"$HERE/launch-collectors.sh" "$JOB_ID" "$DASH_URL" 5650 \
  || echo "[live] WARN: collector launch reported an error"

echo
echo "=================================================================="
echo "  ChronoLog is LIVE.  job=$JOB_ID   dashboard on $FLASK_NODE:5000"
echo "  Drive it: open the dashboard, click 'Generate traffic' (real nodes,"
echo "  no LLM cost) — edges are written through the live ChronoLog."
echo
echo "  Tunnel (run on your workstation, in a real terminal):"
echo "    ssh -i $SSH_KEY -N -L 23456:${FLASK_NODE}:5000 $SSH_USER@$SSH_GATEWAY"
echo "    then open  http://localhost:23456"
echo
echo "  Free the nodes when done:  scancel $JOB_ID"
echo "=================================================================="
