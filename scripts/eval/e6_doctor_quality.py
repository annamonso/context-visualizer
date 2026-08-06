#!/usr/bin/env python3
"""E6 — PrismaDoctor detection quality (docs/eval-plan.md addendum).

Measures whether PrismaDoctor is *correct*, not just fast, against a
ground-truth manifest of injected faults. Fully OFFLINE: seeds the same
spool + inter-agent hot-store shapes the capture layer writes (via the
demo seeding primitives) into a brand-new DTP_STATE_DIR, then calls
``run_scan()`` from Python — no cluster, no dashboard, no LLM.

Four metrics:
  1. Detection rate per fault kind + false positives on a clean background.
  2. Compression: injected raw signals -> incident cards (incl. a ~500x
     identical-fault storm across many hosts).
  3. Diagnosis correctness: does each incident's heuristic diagnosis name
     the planted root-cause category (diagnostics/diagnoser.py rules)?
  4. Recurring recognition: the same fault mix injected twice, one scan
     after each pass; the second scan must badge incidents recurring with
     a prior_count (diagnostics/memory.py + scan.py).

Ground-truth design (single-label faults): every planted fault is shaped
to trip exactly ONE detector, so a detection can be attributed
unambiguously. Concretely: planted error edges keep latency below the
2000 ms slow-edge floor; planted 5xx/429/outlier turns use one turn per
session with distinct prompts (no accidental retry storm); background
prompts vary in *alphabetic* content because ``normalize_message`` strips
digits and hex, which would otherwise collapse numbered prompts into a
false retry-storm fingerprint.

    python3 scripts/eval/e6_doctor_quality.py [--n 10] [--storm 500]
        [--seed 42] [--state-dir /tmp/chronolog_eval_e6_doctor_...]

Outputs: scripts/eval/out/e6_doctor_quality.csv (one row per fault kind)
+ E6 rows appended to scripts/eval/out/summary.csv + a printed table.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                     # _eval_common
sys.path.insert(0, str(HERE.parent / "demos"))    # _demo_common seeding shapes

from _eval_common import append_summary, write_csv  # noqa: E402

# Distinct alphabetic tokens for prompt uniqueness (digits would be
# normalized away and collapse fingerprints).
WORDS = [
    "alpha", "bravo", "cedar", "delta", "ember", "fjord", "gamma", "harbor",
    "iris", "juno", "krill", "lumen", "maple", "nadir", "onyx", "pelican",
    "quartz", "raven", "sable", "tundra", "umber", "vertex", "willow", "zephyr",
]


def wordpair(i: int) -> str:
    return f"{WORDS[i % len(WORDS)]} {WORDS[(i // len(WORDS) + i) % len(WORDS)]}"


# ---------------------------------------------------------------------------
# Corpus seeding (all shapes via scripts/demos/_demo_common primitives,
# which write through the real spool / inter-agent store code paths).
# ---------------------------------------------------------------------------

T0 = datetime(2026, 7, 30, 8, 0, 0, tzinfo=timezone.utc)  # fixed => deterministic
MODEL = "claude-opus-4-8"


def seed_background(rng, n_sessions: int = 12, turns_per: int = 18) -> dict:
    """Clean traffic: healthy turns + healthy edges, zero fault signals."""
    from _demo_common import edge, turn

    bg_hosts = [f"ares-comp-{i:02d}" for i in range(1, 9)]
    counts = {"turns": 0, "edges": 0}
    p = 0
    for s in range(n_sessions):
        sid = f"e6-bg-{s:02d}"
        host = rng.choice(bg_hosts)
        for q in range(1, turns_per + 1):
            turn(sid, q, host=host, scenario=f"e6-bg-{s % 3}", model=MODEL,
                 status_code=200, text="ok",
                 prompt=f"summarize the {wordpair(p)} shard report",
                 latency_ms=rng.uniform(220, 460),
                 when=T0 + timedelta(seconds=p))
            p += 1
            counts["turns"] += 1
    for s in range(3):
        for i in range(40):
            edge(f"e6-bg-{s}", rng.choice(bg_hosts), f"e6-bg-{s:02d}",
                 rng.choice(bg_hosts), f"e6-bg-{(s + 1) % n_sessions:02d}",
                 tool="call_remote_agent", status="ok",
                 latency_ms=rng.uniform(120, 400),
                 when=T0 + timedelta(seconds=400 + s * 40 + i),
                 correlation_id=f"e6bg-{s}-{i:04d}")
            counts["edges"] += 1
    return counts


def inject_faults(rng, n: int, storm: int, tag: str, t_base: datetime) -> dict:
    """Plant the fault mix; return the ground-truth manifest {kind: raw_signals}."""
    from _demo_common import edge, recovery, turn

    bg_hosts = [f"ares-comp-{i:02d}" for i in range(1, 9)]
    storm_hosts = [f"ares-comp-{i:02d}" for i in range(10, 50)]
    scen = f"e6-faults-{tag}"

    # 1) interaction 5xx — one turn per session, distinct prompts (no storm).
    for i in range(n):
        turn(f"e6-fivexx-{i:02d}-{tag}", 1, host=rng.choice(bg_hosts),
             scenario=scen, model=MODEL, status_code=500,
             text="upstream connect error",
             prompt=f"fetch the {wordpair(i)} manifest",
             latency_ms=350.0, when=t_base + timedelta(seconds=i))

    # 2) 429 rate limits.
    for i in range(n):
        turn(f"e6-ratelim-{i:02d}-{tag}", 1, host=rng.choice(bg_hosts),
             scenario=scen, model=MODEL, status_code=429,
             text="rate limit exceeded, retry after 2s",
             prompt=f"index the {wordpair(i + 40)} partition",
             latency_ms=40.0, when=t_base + timedelta(seconds=20 + i))

    # 3) retry storms — one session each, 4 identical prompts => 1 signal.
    for i in range(n):
        for q in range(1, 5):
            turn(f"e6-retry-{i:02d}-{tag}", q, host=rng.choice(bg_hosts),
                 scenario=scen, model=MODEL, status_code=200,
                 text="still waiting on upstream",
                 prompt=f"validate the {WORDS[i % len(WORDS)]} merge output",
                 latency_ms=600.0, when=t_base + timedelta(seconds=40 + i * 4 + q))

    # 4) latency outliers — well above max(1500 ms, 3x model median ~300 ms).
    for i in range(n):
        turn(f"e6-outlier-{i:02d}-{tag}", 1, host=rng.choice(bg_hosts),
             scenario=scen, model=MODEL, status_code=200, text="ok",
             prompt=f"compact the {wordpair(i + 80)} level",
             latency_ms=6000.0 + i * 100, when=t_base + timedelta(seconds=90 + i))

    # 5) edge errors (peer timeout) — latency held < 2000 ms slow-edge floor.
    for i in range(n):
        edge(scen, rng.choice(bg_hosts), f"e6-caller-{tag}",
             rng.choice(bg_hosts), f"e6-worker-{tag}",
             tool="call_remote_agent", status="error:peer timed out",
             latency_ms=1500.0 + i * 10, when=t_base + timedelta(seconds=120 + i),
             correlation_id=f"e6ee-{tag}-{i:04d}")

    # 6) orphan calls — start with no done.
    for i in range(n):
        edge(scen, rng.choice(bg_hosts), f"e6-caller-{tag}",
             rng.choice(bg_hosts), f"e6-worker-{tag}",
             tool="call_remote_agent", orphan=True,
             when=t_base + timedelta(seconds=140 + i),
             correlation_id=f"e6or-{tag}-{i:04d}")

    # 7) slow edges — far above max(2000 ms, 3x tool median).
    for i in range(n):
        edge(scen, rng.choice(bg_hosts), f"e6-caller-{tag}",
             rng.choice(bg_hosts), f"e6-worker-{tag}",
             tool="call_remote_agent", status="ok",
             latency_ms=9000.0 + i * 50, when=t_base + timedelta(seconds=160 + i),
             correlation_id=f"e6se-{tag}-{i:04d}")

    # 8) recovery events.
    for i in range(n):
        recovery(f"e6-recover-{i:02d}-{tag}", reason="checkpoint-restore",
                 host=rng.choice(bg_hosts), when=t_base + timedelta(seconds=180 + i))

    # 9) THE STORM: ~storm identical faults across many hosts -> 1 card.
    #    Different normalized message than (5) so it clusters separately.
    for i in range(storm):
        edge(f"e6-storm-{tag}", "ares-comp-09", f"e6-coordinator-{tag}",
             storm_hosts[i % len(storm_hosts)], f"e6-executor-{tag}",
             tool="call_remote_agent",
             status=f"error:peer timed out after {30000 + i}ms",
             latency_ms=1850.0 + (i % 50), when=t_base + timedelta(seconds=200 + i // 10),
             correlation_id=f"e6st-{tag}-{i:04d}")

    return {
        "http_5xx": n, "rate_limit_429": n, "retry_storm": n,
        "latency_outlier": n, "edge_error_timeout": n, "orphan_call": n,
        "slow_edge": n, "recovery": n, "edge_error_storm_500x": storm,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def expected_matchers():
    """Manifest kind -> (detector kind, exact incident title or None)."""
    from chronolog_observability.diagnostics.detectors import normalize_message

    return {
        "http_5xx": ("http_error",
                     "HTTP errors on LLM calls: "
                     + normalize_message("upstream connect error")),
        "rate_limit_429": ("rate_limit",
                           "Rate-limited LLM calls: "
                           + normalize_message("rate limit exceeded, retry after 2s")),
        "retry_storm": ("retry_storm", None),
        "latency_outlier": ("latency_outlier", None),
        "edge_error_timeout": ("edge_error",
                               "Failed inter-agent calls: "
                               + normalize_message("peer timed out")),
        "orphan_call": ("orphan_call", None),
        "slow_edge": ("slow_edge", None),
        "recovery": ("recovery", None),
        "edge_error_storm_500x": ("edge_error",
                                  "Failed inter-agent calls: "
                                  + normalize_message("peer timed out after 30000ms")),
    }


# Distinctive substring of the diagnoser rule each kind must land on.
DIAG_KEYWORD = {
    "http_5xx": "server-side",
    "rate_limit_429": "throttling",
    "retry_storm": "re-issuing the same prompt",
    "latency_outlier": "tail-latency",
    "edge_error_timeout": "deadline",
    "orphan_call": "never returned",
    "slow_edge": "tail-latency",
    "recovery": "backtracking",
    "edge_error_storm_500x": "deadline",
}


def match_incidents(report, kind: str, title):
    return [inc for inc in report.incidents
            if inc.get("kind") == kind and (title is None or inc.get("title") == title)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=10, help="planted faults per kind")
    ap.add_argument("--storm", type=int, default=500, help="storm size")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--state-dir", default="")
    args = ap.parse_args()

    state = args.state_dir or "/tmp/chronolog_eval_e6_doctor_" + time.strftime(
        "%Y%m%d-%H%M%S", time.gmtime())
    resolved = str(Path(state).resolve())
    home_default = str(Path.home() / ".dt_provenance")
    if resolved.startswith(home_default) or "chronolog_eval_scale" in resolved:
        sys.exit(f"refusing state dir {resolved}: reserved for other runs")
    if Path(resolved).exists() and any(Path(resolved).iterdir()):
        sys.exit(f"refusing non-empty state dir {resolved}: E6 needs a fresh one")

    # Offline + fresh state, set BEFORE any chronolog_observability import.
    from _demo_common import bootstrap
    bootstrap(resolved)
    assert os.environ["CHRONOLOG_OFFLINE"] == "1"

    import random
    rng = random.Random(args.seed)

    from chronolog_observability.diagnostics.scan import run_scan

    print(f"E6 — PrismaDoctor detection quality  (state={resolved})")
    print(f"  n={args.n}/kind, storm={args.storm}, seed={args.seed}\n")

    # -- Phase 0: clean background, false-positive scan (no memory write). --
    bg = seed_background(rng)
    clean = run_scan(diagnose=True, remember=False)
    if clean.error:
        sys.exit(f"clean scan failed: {clean.error}")
    clean_fp_cards = len(clean.incidents)
    clean_fp_signals = sum(int(i.get("count", 0)) for i in clean.incidents)
    print(f"  [0] clean background: {bg['turns']} turns, {bg['edges']} edges "
          f"-> {clean_fp_cards} incidents ({clean_fp_signals} signals) "
          f"[expect 0]")

    # -- Phase 1: inject the fault mix, scan with diagnose+remember. --------
    manifest = inject_faults(rng, args.n, args.storm, "r1", T0 + timedelta(minutes=10))
    scan1 = run_scan(diagnose=True, remember=True)
    if scan1.error:
        sys.exit(f"scan 1 failed: {scan1.error}")
    print(f"  [1] scan 1: scanned {scan1.scanned} -> "
          f"{len(scan1.incidents)} incidents")

    # -- Phase 2: same mix again, second scan must badge recurring. ---------
    inject_faults(rng, args.n, args.storm, "r2", T0 + timedelta(minutes=40))
    scan2 = run_scan(diagnose=True, remember=True)
    if scan2.error:
        sys.exit(f"scan 2 failed: {scan2.error}")
    print(f"  [2] scan 2 (re-injected mix): {len(scan2.incidents)} incidents\n")

    # -- Score. -------------------------------------------------------------
    matchers = expected_matchers()
    rows = []
    matched_sigs = set()
    tot_inj = tot_det = tot_fp = 0
    n_diag_ok = n_rec_ok = 0

    for mkind, injected in manifest.items():
        dkind, title = matchers[mkind]
        m1 = match_incidents(scan1, dkind, title)
        m2 = match_incidents(scan2, dkind, title)
        matched_sigs.update(i["signature"] for i in m1)
        detected_raw = sum(int(i.get("count", 0)) for i in m1)
        detected = min(injected, detected_raw)
        fp = max(0, detected_raw - injected)
        diag_ok = bool(m1) and all(
            DIAG_KEYWORD[mkind] in str(i.get("diagnosis", {}).get("root_cause", ""))
            for i in m1)
        conf = max((float(i.get("diagnosis", {}).get("confidence", 0)) for i in m1),
                   default=0.0)
        rec_ok = bool(m2) and all(
            i.get("recurring") and int(i.get("prior_count", 0)) > 0 for i in m2)
        rows.append([mkind, injected, detected, injected - detected, fp,
                     len(m1), 1, "yes" if diag_ok else "NO", f"{conf:.2f}",
                     "yes" if rec_ok else "NO"])
        tot_inj += injected
        tot_det += detected
        tot_fp += fp
        n_diag_ok += diag_ok
        n_rec_ok += rec_ok

    # Any scan-1 incident not claimed by the manifest is a false positive.
    unexpected = [i for i in scan1.incidents if i["signature"] not in matched_sigs]
    unexpected_signals = sum(int(i.get("count", 0)) for i in unexpected)
    rows.append(["unexpected_on_fault_scan", 0, 0, 0, unexpected_signals,
                 len(unexpected), 0, "-", "-", "-"])
    rows.append(["clean_background", 0, 0, 0, clean_fp_signals,
                 clean_fp_cards, 0, "-", "-", "-"])
    tot_fp += unexpected_signals + clean_fp_signals

    header = ["kind", "injected", "detected", "missed", "false_positives",
              "incident_cards", "expected_cards", "diagnosis_ok",
              "diag_confidence", "recurring_ok"]
    write_csv("e6_doctor_quality.csv", header, rows)

    # Compression: raw injected signals -> incident cards.
    storm_cards = len(match_incidents(scan1, *matchers["edge_error_storm_500x"])) or 1
    cards1 = len(scan1.incidents)
    notes = f"n={args.n};storm={args.storm};seed={args.seed};state={resolved}"
    for metric, value, unit, nn in [
        ("detection_rate_overall", round(100.0 * tot_det / tot_inj, 2), "%", tot_inj),
        ("false_positives_total", tot_fp, "signals", tot_inj),
        ("compression_storm", round(args.storm / storm_cards, 1), "x", args.storm),
        ("compression_overall", round(tot_inj / max(1, cards1), 1), "x", tot_inj),
        ("diagnosis_correct", n_diag_ok, f"of {len(manifest)} kinds", len(manifest)),
        ("recurring_recognized", n_rec_ok, f"of {len(manifest)} kinds", len(manifest)),
    ]:
        append_summary({"experiment": "E6", "metric": metric, "value": value,
                        "unit": unit, "n": nn, "notes": notes})

    # -- Human table. --------------------------------------------------------
    print(f"  {'kind':<26}{'inj':>5}{'det':>5}{'miss':>5}{'fp':>4}"
          f"{'cards':>6}  {'diag':<5}{'conf':>5}  recur")
    for r in rows:
        print(f"  {r[0]:<26}{r[1]:>5}{r[2]:>5}{r[3]:>5}{r[4]:>4}"
              f"{r[5]:>6}  {r[7]:<5}{r[8]:>5}  {r[9]}")
    print(f"\n  detection {tot_det}/{tot_inj} "
          f"({100.0 * tot_det / tot_inj:.1f}%)  ·  false positives {tot_fp}"
          f"  ·  storm compression {args.storm}->{storm_cards} card(s)"
          f"  ·  overall {tot_inj} signals -> {cards1} cards"
          f"  ·  diagnosis {n_diag_ok}/{len(manifest)}"
          f"  ·  recurring {n_rec_ok}/{len(manifest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
