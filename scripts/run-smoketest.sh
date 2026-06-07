#!/bin/bash
# run-smoketest.sh — wire the ARES env and run the live ChronoLog smoke test.
#
# Prereq: a ChronoLog cluster is already up on a SLURM allocation (see
# chronolog-thesis/launch_chronolog.sh). This script must run ON A COMPUTE NODE
# that is a member of that allocation — Mercury's ofi+sockets provider does not
# cross the master(172.20.x)/compute(172.25.x) subnet boundary.
#
# Usage (from a compute node in the allocation):
#     scripts/run-smoketest.sh <SLURM_JOB_ID> [-- <extra smoketest args>]
#
# Example:
#     ssh ares-comp-11
#     cd <repo> && scripts/run-smoketest.sh 20685 -- --read-timeout 300
#
# It resolves the visor IP from the allocation's FIRST node (the visor), via the
# -40g hostname -> raw IPv4 (Mercury rejects the hostname form: HG_PROTONOSUPPORT).

set -euo pipefail

JOB_ID="${1:-}"
shift || true
# allow "-- extra args" passthrough
if [[ "${1:-}" == "--" ]]; then shift; fi
EXTRA_ARGS=("$@")

if [[ -z "$JOB_ID" ]]; then
  echo "Usage: $0 <SLURM_JOB_ID> [-- <extra smoketest args>]" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHRONO_HOME="${CHRONOLOG_HOME:-$HOME/chronolog-install/chronolog}"

# --- resolve the visor node + raw IPv4 ---------------------------------------
NODES=( $(scontrol show hostnames "$(squeue -j "$JOB_ID" -h -o '%N')") )
if [[ ${#NODES[@]} -eq 0 ]]; then
  echo "FATAL: SLURM job $JOB_ID has no nodes — is the allocation alive?" >&2
  exit 1
fi
VISOR_NODE="${NODES[0]}"
VISOR_IP="$(getent hosts "${VISOR_NODE}-40g" | awk '{print $1; exit}')"
if [[ -z "$VISOR_IP" ]]; then
  echo "FATAL: cannot resolve ${VISOR_NODE}-40g to an IPv4 (allocation gone?)" >&2
  exit 1
fi

# --- environment (per the ARES gotchas) --------------------------------------
source /etc/profile.d/lmod.sh 2>/dev/null || true
module load python/3.11.9-zg4555e

export TMPDIR="/mnt/nvme/$USER/chronolog-tmp"      # / is full on ARES; use NVMe
mkdir -p "$TMPDIR"

export LD_LIBRARY_PATH="$CHRONO_HOME/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$CHRONO_HOME/lib:$REPO_ROOT/src:${PYTHONPATH:-}"

export CHRONOLOG_VISOR_IP="$VISOR_IP"              # RAW IP, not the -40g name
export CHRONOLOG_VISOR_PROTOCOL="${CHRONOLOG_VISOR_PROTOCOL:-ofi+sockets}"
unset CHRONOLOG_OFFLINE                            # make sure we hit the live path

echo "=== chronolog-observability live smoke test ==="
echo "  SLURM job:   $JOB_ID  (nodes: ${NODES[*]})"
echo "  Visor:       $VISOR_IP  (on $VISOR_NODE, via ${VISOR_NODE}-40g)"
echo "  Running on:  $(hostname)"
echo "  ChronoLog:   $CHRONO_HOME"
echo "  Python:      $(command -v python3)  ($(python3 -V 2>&1))"
echo "  Extra args:  ${EXTRA_ARGS[*]:-(none)}"
echo

python3 -c "import py_chronolog_client; print('  py_chronolog_client: import OK')" \
  || { echo "FATAL: py_chronolog_client not importable — check module + LD_LIBRARY_PATH/PYTHONPATH" >&2; exit 1; }
echo

exec python3 "$REPO_ROOT/scripts/chronolog_smoketest.py" "${EXTRA_ARGS[@]}"
