#!/usr/bin/env python3
"""DEMO 4 — a realistic multi-agent run, replayed LIVE against the dashboard.

The story (what you would actually see in production):

    Overnight climate-sim run 4187 lost ~40% throughput around 02:40. A user
    asks the agent swarm to find the cause and write an incident summary.

    coordinator (ares-comp-02) receives the task, plans, and delegates:
      1. retriever (ares-comp-03) — searches the cluster logs (tool: search_logs)
         and finds ECC errors + Lustre OST timeouts on ares-comp-17;
      2. analyst  (ares-comp-04) — quantifies the correlation with pandas
         (tool: run_python). The FIRST delegation times out and is retried —
         a real error edge for Health/Doctor to catch;
      3. writer   (ares-comp-05) — writes the incident report (tool: write_file).
    The coordinator then produces the final summary and the run completes.

Every LLM turn is a full Anthropic-shaped interaction (system prompt, message
history with tool_use / tool_result blocks, usage, cost, latency) written to
the Path-A spool + demo LLM store, so ZOOMING INTO ANY NODE shows the real
conversation: the agent thinking, calling tools, reading tool results.
Every delegation is a Path-B start/done edge POSTed to the live dashboard
ingest, with parent_correlation_id set, so the run animates live on the
Scenarios graph AND stitches into one waterfall on the Traces tab.

Usage (dashboard already running):

    PYTHONPATH=src python3 scripts/demos/demo_realworld.py \
        --flask-url http://127.0.0.1:8011 --state-dir /tmp/chronolog_demo_all

    --scenario   scenario id (default anomaly-hunt; use a fresh id per take)
    --pace       sleep multiplier; 1.0 ~= a 2-minute story, 0 = instant seed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------- plumbing

USD_PER_TOKEN = {  # (input, output) $/token — realistic per-model pricing
    "claude-sonnet-5": (3e-6, 15e-6),
    "claude-haiku-4-5": (1e-6, 5e-6),
}


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def toks(text: str) -> int:
    """Cheap-but-consistent token estimate (chars/4)."""
    return max(1, len(text) // 4)


def post(url: str, body: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # keep the story going through transient hiccups
        print(f"  [warn] ingest post failed: {exc}", file=sys.stderr)


class Story:
    """Holds the stores, per-agent message history, and per-agent sequence ids."""

    def __init__(self, args):
        self.args = args
        self.ingest = args.flask_url.rstrip("/") + "/api/_inter-agent/ingest"
        os.environ.setdefault("DTP_STATE_DIR", args.state_dir)
        os.environ["DTP_STATE_DIR"] = args.state_dir
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
        from chronolog_observability.capture.spool import get_spool
        from chronolog_observability.capture.llm_demo_store import get_store
        self.spool = get_spool()
        self.llm = get_store()
        self.seq: dict[str, int] = {}
        self.history: dict[str, list] = {}   # per-agent anthropic message list
        self.sessions_added: set[str] = set()

    def zzz(self, seconds: float) -> None:
        time.sleep(seconds * self.args.pace)

    # ---- Path-B: one delegation edge (start now, done when the callee returns)

    def edge_start(self, fa: dict, ta: dict, tool: str, ask: str, parent: str = "") -> str:
        corr = uuid.uuid4().hex
        post(self.ingest, {
            "scenario_id": self.args.scenario, "phase": "start",
            "correlation_id": corr, "parent_correlation_id": parent,
            "kind": "mcp_call", "tool_name": tool,
            "from_host": fa["host"], "from_session": fa["sid"],
            "to_host": ta["host"], "to_session": ta["sid"],
            "payload": {"ask": ask},
        })
        return corr

    def edge_done(self, corr: str, fa: dict, ta: dict, tool: str,
                  latency_ms: float, status: str = "ok", result: str = "") -> None:
        post(self.ingest, {
            "scenario_id": self.args.scenario, "phase": "done",
            "correlation_id": corr, "kind": "mcp_call", "tool_name": tool,
            "from_host": fa["host"], "from_session": fa["sid"],
            "to_host": ta["host"], "to_session": ta["sid"],
            "status": status, "latency_ms": latency_ms,
            "payload": {"result": result[:200]},
        })

    # ---- Path-A: one full LLM turn for an agent -------------------------

    def turn(self, agent: dict, user_content, text: str,
             tool_uses: list[tuple[str, dict]] | None = None,
             latency_ms: float = 1800.0, evict: str | None = None) -> None:
        """Append ``user_content`` to the agent's history, record the assistant
        reply (``text`` + optional tool_use blocks) as a real Anthropic turn.

        ``user_content`` may be a plain string (a normal user/handoff message)
        or a list of content blocks (a tool_result round-trip).
        """
        sid, host, model = agent["sid"], agent["host"], agent["model"]
        seq = self.seq[sid] = self.seq.get(sid, 0) + 1
        hist = self.history.setdefault(sid, [])
        hist.append({"role": "user", "content": user_content})

        content: list[dict] = [{"type": "text", "text": text}]
        tool_calls = []
        for name, tool_input in (tool_uses or []):
            tid = "toolu_" + uuid.uuid4().hex[:12]
            content.append({"type": "tool_use", "id": tid, "name": name, "input": tool_input})
            tool_calls.append({"id": tid, "name": name, "input": tool_input})

        prompt_chars = len(agent["system"]) + len(json.dumps(hist, default=str))
        in_tok = 180 + prompt_chars // 4          # ~real: system+history+overhead
        out_tok = toks(text) + sum(toks(json.dumps(i)) for _, i in (tool_uses or []))
        c_in, c_out = USD_PER_TOKEN[model]
        cost = round(in_tok * c_in + out_tok * c_out, 6)
        ts = iso_now()
        stop = "tool_use" if tool_uses else "end_turn"

        response_body = {
            "id": "msg_" + uuid.uuid4().hex[:16], "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
        record = {
            "session_id": sid, "sequence_id": seq, "scenario_id": self.args.scenario,
            "host": host, "timestamp": ts, "provider": "anthropic", "model": model,
            "request": {"method": "POST", "path": "/v1/messages",
                        "body": {"model": model, "system": agent["system"],
                                 "messages": list(hist)}},
            "response": {"status_code": 200, "body": response_body,
                         "text": text, "is_streaming": False},
            "tool_calls": tool_calls,
            "metrics": {"total_latency_ms": latency_ms, "delta_input_tokens": in_tok,
                        "delta_output_tokens": out_tok, "delta_cost_usd": cost},
        }
        self.spool.record_interaction(sid, dict(record), sequence_id=seq)
        # Working-memory growth this turn = what entered the context (the new
        # user/tool content) + what the model produced.
        ctx_tokens = toks(json.dumps(user_content, default=str)) + out_tok
        self.spool.record_context_node(sid, {
            "session_id": sid, "sequence_id": seq, "timestamp": ts,
            "op": "add", "node": f"{agent['role']}-{seq}",
            "summary": (text if isinstance(text, str) else str(text))[:70],
            "tokens": ctx_tokens, "model": model, "latency_ms": latency_ms,
            "delta_input_tokens": in_tok, "delta_output_tokens": out_tok,
            "delta_cost_usd": cost,
            "total_effective_input_tokens": in_tok,
        }, sequence_id=seq)
        if evict:
            eseq = self.seq[sid] = self.seq[sid] + 1
            self.spool.record_context_node(sid, {
                "session_id": sid, "sequence_id": eseq, "timestamp": iso_now(),
                "op": "evict", "node": evict, "summary": f"evicted {evict} (compaction)",
                "tokens": 120,
            }, sequence_id=eseq)

        # Timeline copy (LLMDemoStore) — same turn, flattened message history so
        # the timeline prompt column stays readable.
        if sid not in self.sessions_added:
            self.llm.add_session(sid, {"scenario_id": self.args.scenario, "host": host})
            self.sessions_added.add(sid)
        flat_msgs = [{"role": m["role"],
                      "content": m["content"] if isinstance(m["content"], str)
                      else " · ".join(str(b.get("content", b.get("text", b.get("name", ""))))[:160]
                                      for b in m["content"] if isinstance(b, dict))}
                     for m in hist]
        self.llm.append_interaction(sid, {**record, "request": {
            "method": "POST", "path": "/v1/messages",
            "body": {"model": model, "system": agent["system"], "messages": flat_msgs}}})

        # Close the loop: the assistant blocks become part of the next prompt.
        hist.append({"role": "assistant", "content": content})
        tool_note = "".join(f"  ⚙ {n}" for n, _ in (tool_uses or []))
        print(f"  {agent['role']:>11} · turn {seq:<2} {latency_ms/1000:4.1f}s "
              f"{in_tok:>5}in/{out_tok:>4}out{tool_note}")

    def tool_result(self, agent: dict, result_text: str) -> list:
        """Build the tool_result user content answering the agent's LAST tool_use."""
        last = self.history[agent["sid"]][-1]["content"]
        tid = next(b["id"] for b in reversed(last)
                   if isinstance(b, dict) and b.get("type") == "tool_use")
        return [{"type": "tool_result", "tool_use_id": tid, "content": result_text}]


# ---------------------------------------------------------------- the story

TASK = ("Overnight climate-sim run 4187 dropped ~40% throughput starting around "
        "02:40 UTC. Find the root cause across the allocation (ares-comp-10..18) "
        "and write an incident summary for the morning standup.")

LOG_EXCERPT = (
    "ares-comp-17 kernel: [Hardware Error] EDAC MC0: 214 CE memory read error on DIMM_A3\n"
    "ares-comp-17 lustre: LNetError: OST0003 request timed out (30s), resending\n"
    "ares-comp-17 slurmd: [4187.0] MPI rank 412: retransmit budget exceeded, backing off\n"
    "ares-comp-16 lustre: OST0003 slow reply (28.4s), 61 similar events suppressed")

STATS_STDOUT = (
    "window 02:41-02:47  mean_step_time: 3.94s -> 6.41s (+62%)\n"
    "corr(step_time, ost3_latency) = 0.91   corr(step_time, ecc_ce_rate) = 0.88\n"
    "ecc CE rate ares-comp-17: 214/min (baseline 5/min, 42x)\n"
    "ranks on ares-comp-17: 408-455 -> all in slowest decile")

REPORT_MD = ("# Incident: run 4187 throughput regression (02:40-03:05 UTC)\n"
             "**Root cause:** failing DIMM_A3 on ares-comp-17 (ECC CE storm, 42x baseline) "
             "degraded that node's memory bandwidth; its MPI ranks straggled and OST0003 "
             "I/O timed out under retry pressure.\n**Action:** drain ares-comp-17, replace "
             "DIMM, requeue run with allocation excluding the node.")


def run_story(st: Story) -> None:
    S = st.args.scenario
    user = {"sid": f"user@{S}", "role": "user", "host": "ares-login-01"}
    coord = {"sid": f"coordinator@{S}", "role": "coordinator", "host": "ares-comp-02",
             "model": "claude-sonnet-5",
             "system": "You are the coordinator agent of an HPC diagnostics swarm. "
                       "Plan, delegate to remote specialist agents with call_remote_agent, "
                       "and synthesize their findings."}
    retr = {"sid": f"retriever@{S}", "role": "retriever", "host": "ares-comp-03",
            "model": "claude-haiku-4-5",
            "system": "You are the retriever agent. You search cluster logs and telemetry "
                      "with the search_logs tool and report concise findings."}
    anal = {"sid": f"analyst@{S}", "role": "analyst", "host": "ares-comp-04",
            "model": "claude-sonnet-5",
            "system": "You are the analyst agent. You quantify hypotheses against run "
                      "telemetry using the run_python tool (pandas available)."}
    writ = {"sid": f"writer@{S}", "role": "writer", "host": "ares-comp-05",
            "model": "claude-haiku-4-5",
            "system": "You are the writer agent. You draft crisp incident reports and "
                      "save them with the write_file tool."}

    t0 = time.time()
    print(f"[demo] scenario={S} — a user task fans out across 4 agents on 4 nodes\n")

    # ── the user request enters the swarm (root span of the whole trace) ──
    root = st.edge_start(user, coord, "run_scenario", TASK)

    st.turn(coord, TASK,
            "Throughput regressions at this hour are usually storage or a sick node. "
            "Plan: (1) retriever pulls error/I-O logs for the run window, (2) analyst "
            "quantifies the correlation, (3) writer drafts the incident report. "
            "Starting with the logs.",
            [("call_remote_agent", {"agent": "retriever",
                                    "task": "Search logs of ares-comp-10..18, window 02:30-03:05 UTC, "
                                            "pattern error|timeout|ecc|lustre. Summarize anomalies."})],
            latency_ms=2900)
    st.zzz(2)

    # ── retriever: search logs, read the result, answer ──
    c1 = st.edge_start(coord, retr, "call_remote_agent", "pull error/I-O logs 02:30-03:05", root)
    r1 = time.time()
    st.turn(retr, "Search logs of ares-comp-10..18, window 02:30-03:05 UTC, pattern "
                  "error|timeout|ecc|lustre. Summarize anomalies.",
            "Scanning the syslog + Lustre + slurmd streams for the window.",
            [("search_logs", {"hosts": "ares-comp-[10-18]", "window": "02:30-03:05",
                              "pattern": "error|timeout|ecc|lustre"})],
            latency_ms=1400)
    st.zzz(3)
    st.turn(retr, st.tool_result(retr, LOG_EXCERPT),
            "Two co-located anomalies, both concentrated on ares-comp-17: (1) an ECC "
            "correctable-error storm on DIMM_A3 (214/min), and (2) OST0003 request "
            "timeouts with MPI retransmit back-off from ranks 408-455. Everything else "
            "in the allocation is clean.",
            latency_ms=2100)
    st.edge_done(c1, coord, retr, "call_remote_agent",
                 round((time.time() - r1) * 1000, 1),
                 result="ECC storm + OST0003 timeouts on ares-comp-17")
    st.zzz(2)

    # ── coordinator digests, delegates to analyst — first call TIMES OUT ──
    st.turn(coord, st.tool_result(coord, "retriever: ECC CE storm (DIMM_A3, 214/min) and "
                                         "OST0003 timeouts, both on ares-comp-17; rest clean."),
            "A single sick node could explain a fleet-wide stall if its ranks straggle. "
            "Asking the analyst to quantify the correlation before I blame hardware.",
            [("call_remote_agent", {"agent": "analyst",
                                    "task": "Correlate run-4187 step times with OST0003 latency and "
                                            "ares-comp-17 ECC rate, window 02:30-03:05."})],
            latency_ms=3100)
    c2a = st.edge_start(coord, anal, "call_remote_agent", "correlate step times vs OST/ECC", root)
    st.zzz(4)  # ...nothing comes back...
    st.edge_done(c2a, coord, anal, "call_remote_agent", 12000.0,
                 status="error:peer timed out after 12000ms", result="no response")
    st.turn(coord, st.tool_result(coord, "error: call to analyst timed out after 12000ms"),
            "The analyst did not respond in time — its node may be busy. Retrying once "
            "before rerouting the work.",
            [("call_remote_agent", {"agent": "analyst", "retry": 1,
                                    "task": "Correlate run-4187 step times with OST0003 latency and "
                                            "ares-comp-17 ECC rate, window 02:30-03:05."})],
            latency_ms=1600)
    st.zzz(1.5)

    # ── retry succeeds: analyst computes the correlation ──
    c2b = st.edge_start(coord, anal, "call_remote_agent", "correlate step times vs OST/ECC (retry)", root)
    r2 = time.time()
    st.turn(anal, "Correlate run-4187 step times with OST0003 latency and ares-comp-17 "
                  "ECC rate, window 02:30-03:05.",
            "Loading the step-time telemetry and the node counters, then computing "
            "per-window correlations.",
            [("run_python", {"code": "import pandas as pd\ndf = load_run(4187, '02:30','03:05')\n"
                                     "print(correlate(df.step_time, df.ost3_latency, df.ecc_ce))"})],
            latency_ms=1900)
    st.zzz(3)
    st.turn(anal, st.tool_result(anal, STATS_STDOUT),
            "Confirmed: step time rose 62% in 02:41-02:47 and correlates 0.91 with "
            "OST0003 latency and 0.88 with the comp-17 ECC rate. All ranks on "
            "ares-comp-17 sit in the slowest decile — the node is the common factor; "
            "the OST timeouts are downstream retry pressure.",
            latency_ms=2700)
    st.edge_done(c2b, coord, anal, "call_remote_agent",
                 round((time.time() - r2) * 1000, 1),
                 result="corr 0.91 OST / 0.88 ECC; comp-17 ranks slowest decile")
    st.zzz(2)

    # ── writer drafts the report ──
    st.turn(coord, st.tool_result(coord, "analyst: 62% step-time rise, corr 0.91 with OST "
                                         "latency, 0.88 with ECC; comp-17 is the common factor."),
            "Cause established: failing DIMM on ares-comp-17. Handing the findings to "
            "the writer for the standup report.",
            [("call_remote_agent", {"agent": "writer",
                                    "task": "Write reports/run-4187-incident.md: root cause failing "
                                            "DIMM_A3 on ares-comp-17; include the correlations and "
                                            "the drain/replace/requeue action."})],
            latency_ms=2400, evict="coordinator-1")
    c3 = st.edge_start(coord, writ, "call_remote_agent", "draft incident report", root)
    r3 = time.time()
    st.turn(writ, "Write reports/run-4187-incident.md: root cause failing DIMM_A3 on "
                  "ares-comp-17; include the correlations and the drain/replace/requeue action.",
            "Drafting the incident report with the evidence chain and remediation.",
            [("write_file", {"path": "reports/run-4187-incident.md", "content": REPORT_MD})],
            latency_ms=1500)
    st.zzz(2.5)
    st.turn(writ, st.tool_result(writ, "wrote reports/run-4187-incident.md (14 lines)"),
            "Report saved. Root cause, evidence (ECC storm 42x baseline, corr 0.91/0.88) "
            "and the drain-replace-requeue action are in reports/run-4187-incident.md.",
            latency_ms=1300)
    st.edge_done(c3, coord, writ, "call_remote_agent",
                 round((time.time() - r3) * 1000, 1),
                 result="reports/run-4187-incident.md written")
    st.zzz(1.5)

    # ── final synthesis, root span closes ──
    st.turn(coord, st.tool_result(coord, "writer: report saved to reports/run-4187-incident.md"),
            "Run 4187's throughput drop was caused by a failing DIMM (A3) on "
            "ares-comp-17: an ECC correctable-error storm degraded the node, its MPI "
            "ranks straggled, and OST0003 timed out under the retry pressure. "
            "Remediation: drain ares-comp-17, replace the DIMM, requeue excluding the "
            "node. Full report: reports/run-4187-incident.md.",
            latency_ms=3300)
    st.edge_done(root, user, coord, "run_scenario",
                 round((time.time() - t0) * 1000, 1),
                 result="root cause: failing DIMM on ares-comp-17; report written")
    print(f"\n[demo] story complete in {time.time() - t0:.0f}s — "
          f"open ?tab=scenarios scenario={S}, click any agent node to zoom in.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flask-url", default="http://127.0.0.1:8011")
    ap.add_argument("--scenario", default="anomaly-hunt")
    ap.add_argument("--state-dir", default=os.environ.get("DTP_STATE_DIR", "/tmp/chronolog_demo_all"))
    ap.add_argument("--pace", type=float, default=1.0,
                    help="sleep multiplier (1.0 ~= 2 min story; 0 = instant)")
    args = ap.parse_args()
    run_story(Story(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
