#!/usr/bin/env bash
# E4 — Scripted fault injection for the timed-diagnosis case study.
#
# Injects, into a LIVE deployment:
#   1. a node-scoped retry storm  — one session on $TARGET_HOST re-issuing the
#      identical prompt 6x with 5xx responses (Path-A spool; trips
#      detect_retry_storms + detect_interaction_errors),
#   2. an edge-error burst        — 12 "error:peer timed out" edges INTO
#      $TARGET_HOST (trips detect_edge_errors),
#   3. ONE killed delegation leg  — a start edge with no done frame (trips
#      detect_orphan_calls; the "orphan-call question" of the case study).
#
# Usage:
#   DTP_STATE_DIR=<shared state dir> scripts/eval/inject_fault.sh \
#       <dashboard_url> <scenario> <target_host>
#
# Then run the two timed diagnosis protocols (stopwatch; write times into
# scripts/eval/out/e4_case_study.csv — see scripts/eval/RUNBOOK.md §E4 for the
# exact question list and rules).
set -euo pipefail
DASH="${1:?usage: inject_fault.sh <dashboard_url> <scenario> <target_host>}"
SCENARIO="${2:?scenario id}"
TARGET="${3:?target host (as SLURM names it)}"
: "${DTP_STATE_DIR:?DTP_STATE_DIR must point at the deployment shared state dir}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

DASH="$DASH" SCENARIO="$SCENARIO" TARGET="$TARGET" PYTHONPATH="$REPO/src" python3 - <<'PY'
import json, os, time, urllib.request, uuid

dash = os.environ["DASH"].rstrip("/")
scenario = os.environ["SCENARIO"]
target = os.environ["TARGET"]

def post(path, body):
    req = urllib.request.Request(dash + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")

def iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

# ── 1. node-scoped retry storm (Path-A, via the shared spool) ──────────────
from chronolog_observability.capture.spool import get_spool
spool = get_spool()
sid = f"retrier@{scenario}"
prompt = "Fetch the shard manifest from the object store and verify its checksum."
for s in range(1, 7):
    spool.record_interaction(sid, {
        "session_id": sid, "sequence_id": s, "scenario_id": scenario,
        "host": target, "timestamp": iso(), "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "request": {"method": "POST", "path": "/v1/messages",
                    "body": {"messages": [{"role": "user", "content": prompt}]}},
        "response": {"status_code": 500, "text": "upstream connect error", "is_streaming": False},
        "metrics": {"total_latency_ms": 4200 + 300 * s,
                    "delta_input_tokens": 900, "delta_output_tokens": 0,
                    "delta_cost_usd": 0.0027},
    }, sequence_id=s)
print(f"  [1] retry storm: 6 identical failing turns for {sid} on {target}")

# ── 2. edge-error burst into the target node (Path-B) ──────────────────────
callers = ["planner", "executor", "fetcher", "critic"]
for i in range(12):
    role = callers[i % len(callers)]
    corr = uuid.uuid4().hex
    base = {"scenario_id": scenario, "correlation_id": corr,
            "from_host": f"caller-{i % 4}", "from_session": f"{role}@{scenario}",
            "to_host": target, "to_session": f"worker@{scenario}",
            "kind": "mcp_call", "tool_name": "call_remote_agent"}
    post("/api/_inter-agent/ingest", dict(base, phase="start"))
    post("/api/_inter-agent/ingest", dict(base, phase="done",
         status="error:peer timed out", latency_ms=25000 + 500 * i))
print(f"  [2] edge-error burst: 12 peer-timeout edges into {target}")

# ── 3. the killed delegation leg (orphan start, no done) ────────────────────
corr = uuid.uuid4().hex
post("/api/_inter-agent/ingest", {
    "scenario_id": scenario, "phase": "start", "correlation_id": corr,
    "from_host": "caller-0", "from_session": f"planner@{scenario}",
    "to_host": target, "to_session": f"worker@{scenario}",
    "kind": "mcp_call", "tool_name": "call_remote_agent",
    "payload": {"ask": "compact the level-2 index"},
})
print(f"  [3] killed delegation leg: orphan start corr={corr[:12]} -> {target}")
print(f"\n  Injection complete (scenario={scenario}, target={target}).")
print("  START THE STOPWATCH when the diagnostician first touches the UI/logs.")
PY
