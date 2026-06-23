#!/usr/bin/env python3
"""DEMO 1 — ChronoDoctor: detect, diagnose, and remember failures.

Seeds a multi-agent run riddled with realistic failures (5xx errors, a
rate-limited node, a wave of peer timeouts, a hung/dropped call, a retry storm,
and checkpoint restores), then runs ChronoDoctor and prints the ranked
incidents with a root-cause + fix for each. Runs the scan twice to show the
self-compacting incident memory recognising a *recurring* incident on the
second pass.

    python3 scripts/demos/demo_doctor.py

No cluster, no API keys — fully offline. Then open the Doctor tab to see the
same incidents live (instructions printed at the end).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _demo_common import bootstrap, color, edge, h1, h2, now, recovery, turn, view_hint  # noqa: E402

STATE_DIR = os.environ.get("DEMO_STATE_DIR", "/tmp/chronolog_demo_doctor")
SCENARIO = "incident-demo"


def seed() -> None:
    from datetime import timedelta

    t0 = now() - timedelta(minutes=5)
    hosts = [f"ares-comp-{i:02d}" for i in range(3, 9)]

    # 1) A genuinely broken node: every call to comp-05 returns HTTP 500.
    for i in range(1, 9):
        turn("planner@%s" % SCENARIO, i, host="ares-comp-05", scenario=SCENARIO,
             status_code=500, text="internal error: worker pool exhausted",
             latency_ms=8200, when=t0 + timedelta(seconds=i))

    # 2) A rate-limited node: HTTP 429 storm when the fan-out spikes.
    for i in range(1, 13):
        turn("fetcher@%s" % SCENARIO, i, host="ares-comp-06", scenario=SCENARIO,
             status_code=429, text="rate limit exceeded, retry after 2s",
             latency_ms=40, when=t0 + timedelta(seconds=i))

    # 3) A wave of inter-agent peer timeouts across MANY hosts -> one incident.
    for i, h in enumerate(hosts * 6):
        edge(SCENARIO, "coordinator@%s" % SCENARIO, "coordinator@%s" % SCENARIO,
             h, "executor@%s" % SCENARIO, tool="call_remote_agent",
             status="error:peer timed out after %dms" % (30000 + i),
             latency_ms=30000 + i, when=t0 + timedelta(seconds=i))

    # 4) Hung / dropped calls — started, never completed (invisible elsewhere).
    for i in range(4):
        edge(SCENARIO, "coordinator@%s" % SCENARIO, "coordinator@%s" % SCENARIO,
             "ares-comp-07", "summarizer@%s" % SCENARIO, orphan=True,
             when=t0 + timedelta(seconds=i))

    # 5) A retry storm: same prompt re-issued over and over (stuck agent).
    for i in range(1, 7):
        turn("validator@%s" % SCENARIO, i, host="ares-comp-08", scenario=SCENARIO,
             status_code=200, text="still waiting on upstream",
             prompt="validate the merged result",  # identical every turn
             latency_ms=600, when=t0 + timedelta(seconds=i))

    # 6) Recovery events: the forward path failed enough to trigger rollbacks.
    for i in range(3):
        recovery("planner@%s" % SCENARIO, reason="checkpoint-restore",
                 host="ares-comp-05", when=t0 + timedelta(seconds=i))


def report(rep) -> None:
    sev_color = {"critical": "red", "error": "yellow", "warning": "yellow", "info": "blue"}
    t = rep.totals
    print(color(
        f"  scanned {rep.scanned.get('interactions',0)} turns · "
        f"{rep.scanned.get('edges',0)} edges · {rep.scanned.get('recoveries',0)} recoveries"
        f"  →  {t.get('incidents',0)} incidents "
        f"({t.get('critical',0)} critical, {t.get('error',0)} error, {t.get('warning',0)} warning)",
        "dim"))
    for inc in rep.incidents:
        d = inc.get("diagnosis", {})
        badge = " ".join(filter(None, [
            color(f"[{inc['severity'].upper()}]", sev_color.get(inc["severity"], "blue")),
            color(f"×{inc['count']}", "bold"),
            color("RECURRING ×%d" % inc.get("prior_count", 0), "mag") if inc.get("recurring") else "",
        ]))
        print(f"\n  {badge}  {color(inc['title'], 'bold')}")
        print(f"    hosts: {', '.join(inc['hosts'][:5])}{' …' if len(inc['hosts'])>5 else ''}")
        print(f"    {color('why:', 'cyan')} {d.get('root_cause','')}")
        print(f"    {color('fix:', 'green')} {d.get('remediation','')}")
        print(color(f"    blast radius: {d.get('blast_radius','')}  ·  "
                    f"confidence {int(d.get('confidence',0)*100)}% ({d.get('source','')})", "dim"))


def main() -> None:
    bootstrap(STATE_DIR)
    from chronolog_observability.diagnostics.scan import run_scan

    h1("DEMO 1 — ChronoDoctor: error detection, diagnosis & memory")
    print("  Seeding a multi-agent run full of realistic failures into a local,")
    print("  offline ChronoLog store (no cluster / no API keys required)…")
    seed()

    h2("First scan — ChronoDoctor detects, clusters, and diagnoses")
    report(run_scan())

    h2("Second scan — incident memory recognises the RECURRING incidents")
    print(color("  (ChronoDoctor wrote the first pass to its self-compacting "
                "incident_memory.md;\n   the same failures now carry a 'recurring' "
                "badge with their prior count.)", "dim"))
    report(run_scan())

    h2("Incident memory document")
    from chronolog_observability.diagnostics.scan import memory_document
    doc = memory_document()
    print(color("\n".join("    " + ln for ln in doc.splitlines()[:14]), "dim"))
    print(f"    {color('(full doc: %s/diagnostics/incident_memory.md)' % STATE_DIR, 'dim')}")

    view_hint(STATE_DIR, "doctor")


if __name__ == "__main__":
    main()
