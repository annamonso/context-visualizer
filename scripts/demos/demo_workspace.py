#!/usr/bin/env python3
"""DEMO 4 — Workspace: one run replayed as an orchestrator + subagent tree.

The other demos seed flat, standalone agents (``role@scenario``), so the
Workspace tab shows each as its own single block. This one seeds a single
*conversation* the way the real capture path records a delegating run: the
session ids are nested with dotted suffixes (``parent`` → ``parent.1`` →
``parent.1.1``), which is exactly what the Workspace grouping keys off to draw
the orchestrator-plus-subagents tree.

    python3 scripts/demos/demo_workspace.py

What it seeds — one request that an orchestrator decomposes and delegates:

    orchestrator@ws-demo            (the base session = the orchestrator)
    ├─ orchestrator@ws-demo.1       researcher subagent
    │  └─ orchestrator@ws-demo.1.1  web-fetch sub-subagent
    └─ orchestrator@ws-demo.2       coder subagent (hits one failed turn)

Turns are interleaved in time so the handoffs between agents render as edges,
and one coder turn returns HTTP 500 so the error-toast-on-error-turn feature
lights up when you scrub onto it. Fully offline.
"""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _demo_common import bootstrap, color, h1, h2, now, turn, view_hint  # noqa: E402

STATE_DIR = os.environ.get("DEMO_STATE_DIR", "/tmp/chronolog_demo_workspace")
SCENARIO = "ws-demo"

# role -> (session id, host). The dotted suffix is what makes the tree: the
# base session is the orchestrator; every deeper id is a subagent.
ORCH = f"orchestrator@{SCENARIO}"
RESEARCHER = f"{ORCH}.1"
WEBFETCH = f"{ORCH}.1.1"
CODER = f"{ORCH}.2"

HOST = {
    ORCH: "ares-comp-01",
    RESEARCHER: "ares-comp-02",
    WEBFETCH: "ares-comp-03",
    CODER: "ares-comp-04",
}

# The run as an ordered script. Each entry is one LLM turn; the global order
# (not the per-session sequence) is what interleaves the agents so control
# visibly hands off between them. Fields: session, per-session seq, text, and
# optional status_code for the one failed turn.
SCRIPT = [
    (ORCH,       1, "You are the orchestrator. Decompose the task and delegate.", 200),
    (ORCH,       2, "Plan: (1) research the topic, (2) implement the fix.",       200),
    (ORCH,       3, "Delegating research to subagent .1.",                        200),
    (RESEARCHER, 1, "Researching the topic across sources.",                      200),
    (RESEARCHER, 2, "Need the upstream doc — delegating fetch to .1.1.",          200),
    (WEBFETCH,   1, "Fetching https://docs.example/spec …",                       200),
    (WEBFETCH,   2, "Retrieved the page; extracted 3 relevant facts.",            200),
    (RESEARCHER, 3, "Synthesizing the findings for the orchestrator.",            200),
    (ORCH,       4, "Research complete. Now implement the change.",               200),
    (ORCH,       5, "Delegating implementation to subagent .2.",                  200),
    (CODER,      1, "Writing the patch.",                                         200),
    (CODER,      2, "Build failed: ModuleNotFoundError: no module 'spec'.",       500),
    (CODER,      3, "Added the missing import; tests pass.",                      200),
    (ORCH,       6, "Assembling the final answer from both subagents.",           200),
]


def seed() -> None:
    t0 = now() - timedelta(minutes=2)
    for k, (session, seq, text, status) in enumerate(SCRIPT):
        turn(
            session, seq,
            host=HOST[session],
            scenario=SCENARIO,
            status_code=status,
            text=text,
            latency_ms=400 + (k % 4) * 350,
            in_tok=600 + seq * 120,
            out_tok=40 + (k % 5) * 30,
            cost=round(0.0008 + k * 0.00015, 5),
            prompt=text,
            when=t0 + timedelta(seconds=k * 6),
        )


def main() -> None:
    bootstrap(STATE_DIR)

    h1("DEMO 4 — Workspace: orchestrator + subagents, one run replayed")
    print("  Seeding one delegating request as a nested session tree")
    print("  (orchestrator → researcher → web-fetch, and orchestrator → coder)…")
    seed()

    # Prove the tree the way the Workspace tab reads it: gather the conversation's
    # sessions from the spool and build the same agent graph the UI renders. (We
    # read the spool directly rather than through the Flask blueprint so the demo
    # stays import-light and offline.)
    from backend.capture.spool import (
        get_spool, KIND_INTERACTIONS, KIND_CONTEXT_GRAPH,
    )
    from backend.adapters.shape import base_session_id, build_agent_graph

    sp = get_spool()
    members = [s for s in sp.list_sessions() if base_session_id(s) == ORCH]
    # Orchestrator (base) first, so the shaper treats it as the parent.
    members.sort(key=lambda s: (s != ORCH, s))
    tree = [
        {
            "session_id": s,
            "interactions": sp.read(s, KIND_INTERACTIONS),
            "context_nodes": sp.read(s, KIND_CONTEXT_GRAPH),
        }
        for s in members
    ]
    graph = build_agent_graph(ORCH, tree)
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    h2(f"Reconstructed conversation — {len(nodes)} agents, {len(edges)} handoffs")
    for n in nodes:
        role = n["agent_role"]
        tag = color(role, "mag" if role == "orchestrator" else "blue")
        print(f"    {n['session_id']:<32} {tag:<20} "
              f"{n['interaction_count']} turns · {n['total_tokens']} tok")

    view_hint(STATE_DIR, "workspace", f"&conv={ORCH}")


if __name__ == "__main__":
    main()
