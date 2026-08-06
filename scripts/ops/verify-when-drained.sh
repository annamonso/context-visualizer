#!/bin/bash
# verify-when-drained.sh — restart the dashboard, wait until the scenario's
# Path-A LLM stories have drained to the grapher CSVs (GIL-safe Cluster-tab
# read, never blocks), then run the full dashboard verification.
#
# Verifying mid-drain triggers a blocking ReplayStory that freezes the whole
# dashboard, so this gates the verify on a real drain-complete signal.
#
# Usage: verify-when-drained.sh <SLURM_JOBID> <FLASK_NODE> <SCENARIO> <N_AGENTS>

set -uo pipefail

JOB_ID="${1:?jobid}"; FLASK_NODE="${2:?flask node}"; SCEN="${3:?scenario}"
N_AGENTS="${4:?n agents}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
URL="http://${FLASK_NODE}:5000"

echo "=== restart dashboard on $FLASK_NODE (clean, no request backlog) ==="
ssh -o BatchMode=yes "$FLASK_NODE" "pkill -9 -f chronolog_observability.app 2>/dev/null; true" 2>&1
bash "$REPO_ROOT/scripts/ops/launch-dashboard.sh" "$JOB_ID" "$FLASK_NODE" 5000 5557 >/dev/null 2>&1
for i in $(seq 1 30); do
  curl -sf --max-time 3 "$URL/api/config" >/dev/null 2>&1 && { echo "dashboard up (${i}s)"; break; }
  sleep 1
done

echo "=== poll Cluster topology until $N_AGENTS llm_interactions stories for '$SCEN' drained ==="
ready=0
for i in $(seq 1 96); do   # up to 8 min
  topo=$(curl -sf --max-time 8 "$URL/api/chronolog/topology" 2>/dev/null || true)
  cnt=$(printf '%s' "$topo" \
        | grep -o "\"chronicle\":\"llm_interactions\"[^}]*\"story\":\"[a-zA-Z0-9_.-]*@${SCEN}\"" \
        | grep -o "\"story\":\"[a-zA-Z0-9_.-]*@${SCEN}\"" | sort -u | wc -l)
  echo "  poll $i: $cnt/$N_AGENTS llm_interactions@$SCEN stories drained"
  if (( cnt >= N_AGENTS )); then ready=1; break; fi
  sleep 5
done
(( ready )) && echo "DRAIN COMPLETE" || echo "WARN: drain incomplete after timeout — verifying anyway"

echo "=== FULL VERIFICATION ==="
python3 "$REPO_ROOT/scripts/ops/verify_dashboard.py" "$URL" "$SCEN"
rc=$?
echo "VERIFY_DONE rc=$rc"
exit $rc
