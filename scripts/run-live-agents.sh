#!/bin/bash
# run-live-agents.sh — full real-agent test against a live ChronoLog cluster,
# wired into the NEW chronolog-observability plugin (no clio-core endpoints).
#
# Pipeline:
#   1. cluster healthcheck
#   2. launch the dashboard on NODES[1] (capture-on, shared spool)
#   3. launch one collector per node (Path-B inter-agent edges)
#   4. preflight SDK + CLI + subscription auth on each agent node
#   5. run REAL agents (Claude Agent SDK + real MCP tool call) on 3 nodes
#   6. curl the dashboard to confirm scenarios / interactions populated
#
# Usage:  scripts/run-live-agents.sh <SLURM_JOBID> [<scenario_id>]
# Env:    AGENT_ENV (default iowarp), AGENT_MODEL (default claude-haiku-4-5),
#         ROUNDS (default 1), FLASK_PORT (default 5000)

set -uo pipefail   # no -e: tolerate per-node ssh glitches

JOB_ID="${1:-}"
SCENARIO="${2:-live-$(date +%Y%m%d-%H%M%S)}"
AGENT_ENV="${AGENT_ENV:-iowarp}"
AGENT_MODEL="${AGENT_MODEL:-claude-haiku-4-5}"
ROUNDS="${ROUNDS:-1}"
FLASK_PORT="${FLASK_PORT:-5000}"
COLLECTOR_PORT="${COLLECTOR_PORT:-5650}"

if [[ -z "$JOB_ID" ]]; then
  echo "Usage: $0 <SLURM_JOBID> [<scenario_id>]" >&2; exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
THESIS="/mnt/common/$USER/chronolog-thesis"
STATE_DIR="${DTP_STATE_DIR:-/mnt/common/$USER/observe-state}"
CONDA_SH="$HOME/miniconda3/etc/profile.d/conda.sh"

NODES=( $(scontrol show hostnames "$(squeue -j "$JOB_ID" -h -o '%N')") )
NUM=${#NODES[@]}
if (( NUM < 3 )); then
  echo "FAIL: need >=3 nodes (got $NUM: ${NODES[*]})" >&2; exit 1
fi

VISOR_NODE="${NODES[0]}"
FLASK_NODE="${NODES[1]}"
# Agents per node. Default 1 (one role per node). Set AGENTS_PER_NODE=K to run K
# REAL agents on EVERY node — each writes through that node's local collector +
# keeper, so ChronoLog records every agent's turns/memory/edges per host. Roles
# cycle with a numeric suffix so every agent has a unique role@scenario id.
ALL_ROLES=(planner executor fetcher analyst retriever summarizer)
AGENTS_PER_NODE="${AGENTS_PER_NODE:-1}"
AGENT_NODES=()
ROLES=()
_idx=0
for _node in "${NODES[@]}"; do
  for (( _k=0; _k<AGENTS_PER_NODE; _k++ )); do
    _base="${ALL_ROLES[$(( _idx % ${#ALL_ROLES[@]} ))]}"
    _rep=$(( _idx / ${#ALL_ROLES[@]} ))
    _role="$_base"; (( _rep > 0 )) && _role="${_base}${_rep}"
    AGENT_NODES+=( "$_node" )
    ROLES+=( "$_role" )
    _idx=$(( _idx + 1 ))
  done
done
NUM_AGENTS=${#AGENT_NODES[@]}
FLASK_URL="http://${FLASK_NODE}:${FLASK_PORT}"

echo "=================================================================="
echo "  REAL-agent live test (chronolog-observability plugin)"
echo "    SLURM job:  $JOB_ID   nodes($NUM): ${NODES[*]}"
echo "    Dashboard:  $FLASK_URL   (capture-on, spool $STATE_DIR)"
echo "    Agents:     $NUM_AGENTS total ($AGENTS_PER_NODE per node) — ${ROLES[*]}"
echo "    Model:      $AGENT_MODEL   rounds: $ROUNDS   scenario: $SCENARIO"
echo "=================================================================="

# ── 1. health ────────────────────────────────────────────────────────
echo "[1/6] cluster healthcheck"
"$THESIS/healthcheck.sh" "$JOB_ID" || { echo "FAIL: healthcheck"; exit 1; }

# ── 2. dashboard ─────────────────────────────────────────────────────
echo "[2/6] launching dashboard on $FLASK_NODE"
DTP_STATE_DIR="$STATE_DIR" "$REPO_ROOT/scripts/launch-dashboard.sh" "$JOB_ID" "$FLASK_NODE" "$FLASK_PORT"
echo -n "  waiting for $FLASK_URL/api/config "
up=0
# Allow generous time: on a polluted ChronoLog index the capture worker's
# warm-up replay holds the GIL and Flask can be unresponsive for a while.
for _ in $(seq 1 90); do
  if curl -sf --max-time 3 "$FLASK_URL/api/config" >/dev/null 2>&1; then echo "— up."; up=1; break; fi
  echo -n "."; sleep 1
done
(( up )) || { echo " FAIL: dashboard did not come up"; exit 1; }

# ── 2b. standalone Path-A capture worker (drains spool -> ChronoLog) ──
echo "[2b/6] launching standalone capture worker on $FLASK_NODE"
DTP_STATE_DIR="$STATE_DIR" "$REPO_ROOT/scripts/launch-capture.sh" "$JOB_ID" "$FLASK_NODE" 5570 || \
  echo "  WARN: capture worker launch reported an error (Path-A interactions may not drain)"

# ── 3. collectors ────────────────────────────────────────────────────
echo "[3/6] launching collectors (one per node)"
"$REPO_ROOT/scripts/launch-collectors.sh" "$JOB_ID" "$FLASK_URL" "$COLLECTOR_PORT" || \
  echo "  WARN: collector launch reported an error (agents fall back to dashboard ingest)"
sleep 3

# ── 4. preflight ─────────────────────────────────────────────────────
echo "[4/6] preflight SDK + CLI + auth on agent nodes"
preflight_fail=0
for node in $(printf '%s\n' "${AGENT_NODES[@]}" | sort -u); do
  out="$(ssh -o BatchMode=yes "$node" "
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    source '$CONDA_SH' 2>/dev/null || true
    conda activate '$AGENT_ENV' 2>/dev/null || true
    python3 -c 'import claude_agent_sdk as s; print(\"sdk\", s.__version__)' || exit 11
    CLI=\"\$(command -v claude || true)\"; [ -x \"\$CLI\" ] || CLI=\"\$HOME/.local/bin/claude\"
    [ -x \"\$CLI\" ] || { echo NO_CLI; exit 12; }
    \"\$CLI\" --version 2>/dev/null | head -1
    [ -e \"\$HOME/.claude\" ] || echo 'WARN: ~/.claude missing'
    [ -z \"\${ANTHROPIC_API_KEY:-}\" ] || echo 'WARN: ANTHROPIC_API_KEY set'
  " 2>&1)"
  rc=$?
  if (( rc != 0 )); then echo "  ✗ $node (rc=$rc): $out"; preflight_fail=1
  else echo "  ✓ $node: $(echo "$out" | tr '\n' ' ')"; fi
done
(( preflight_fail )) && { echo "FAIL: preflight failed; aborting before any LLM spend"; exit 1; }

peer_spec_for () {
  local self_ix="$1"; local out=""
  for j in "${!AGENT_NODES[@]}"; do
    [[ "$j" == "$self_ix" ]] && continue
    [[ -n "$out" ]] && out+=","
    out+="${AGENT_NODES[$j]}:${ROLES[$j]}"
  done
  printf '%s' "$out"
}

# ── 5. real agents ───────────────────────────────────────────────────
echo "[5/6] running real agents (genuine LLM + real MCP tool call)"
pids=()
for i in "${!AGENT_NODES[@]}"; do
  node="${AGENT_NODES[$i]}"; role="${ROLES[$i]}"; peers="$(peer_spec_for "$i")"
  echo "  $node/$role -> peers $peers"
  ssh -o BatchMode=yes "$node" "
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    source '$CONDA_SH' 2>/dev/null || true
    conda activate '$AGENT_ENV' 2>/dev/null || true
    unset ANTHROPIC_API_KEY
    export PYTHONPATH='$REPO_ROOT/src':\${PYTHONPATH:-}
    export TMPDIR=/mnt/nvme/$USER/chronolog-tmp; mkdir -p \"\$TMPDIR\"
    python3 '$REPO_ROOT/scripts/real_agent_chronolog.py' \
      --flask-url     '$FLASK_URL' \
      --collector-url 'http://127.0.0.1:$COLLECTOR_PORT' \
      --scenario      '$SCENARIO' \
      --my-host       '$node' \
      --my-role       '$role' \
      --peers         '$peers' \
      --model         '$AGENT_MODEL' \
      --state-dir     '$STATE_DIR' \
      --rounds        '$ROUNDS'
  " 2>&1 | sed "s|^|    [$node $role] |" &
  pids+=( "$!" )
done
for p in "${pids[@]}"; do wait "$p" || true; done

# ── 6. verify ────────────────────────────────────────────────────────
echo "[6/6] dashboard verification"
echo "  scenario index (/api/scenarios):"
curl -sf "$FLASK_URL/api/scenarios" 2>/dev/null | head -c 1200; echo
echo "  inter-agent edges (/api/scenarios/$SCENARIO/inter-agent):"
curl -sf "$FLASK_URL/api/scenarios/$SCENARIO/inter-agent" 2>/dev/null | head -c 1200; echo
echo "  cluster topology (/api/chronolog/topology):"
curl -sf "$FLASK_URL/api/chronolog/topology" 2>/dev/null | head -c 1500; echo
echo
echo "Done. Scenario='$SCENARIO'. Give the Path-A spool ~30-180s to drain into"
echo "ChronoLog, then check /_interceptor/interactions and /api/conversations."
echo "Dashboard: $FLASK_URL/   (tunnel: ssh -L $FLASK_PORT:$FLASK_NODE:$FLASK_PORT $USER@ares-ext.ares.local)"
