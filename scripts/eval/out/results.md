# Evaluation results — running summary

Per `docs/evaluation.md`. Raw data: the CSVs in this directory (`summary.csv`
is the machine-readable index of everything below).

**Status: ALL measurements FINAL — every `\tbd` in 06-evaluation.tex now has a number.**
The E5 allocation ran in two sittings: 2026-07-30 (job on ares-comp-10..17,
cut short by a lost connection after E3c K=2) and 2026-07-31 (job 22491, same
node set, state dir `/mnt/common/$USER/observe-state-e5`), which
finished E2c live, E3c K=4, and uncovered + fixed the replay-timeout pathology
(see "New workaround" below). Gateway dry-run values are kept in parentheses
where they differ meaningfully from the live number.

## E1 — Capture overhead (tab:overhead)

| Marker | FINAL value (live, compute node, production NFS state dir) |
|---|---|
| Spool append p50/p95 | **0.825 / 1.088 ms** (n=10 000, ~2 KB records, NFS; gateway xfs dry-run: 0.074 / 0.121) |
| Collector POST p50/p95 | **2.82 / 6.22 ms** (n=10 000, real dashboard behind it) |
| — fallback (collector down → direct Flask ingest) | **8.95 / 12.75 ms** (n=2 000) — the predicted asymmetry flip vs. the sink dry-run |
| End-to-end turn delta | **+365 ms ± 2 398 ms (95 % CI), +4.7 % of mean turn, Welch t=0.30 — statistically indistinguishable from zero** (n=30+30 interleaved real-agent turns; instrumented 8 099 ± 5 147 ms, uninstrumented 7 734 ± 4 291 ms). Honest headline: measured mechanism overhead = spool + emit ≈ 0.8 + 2.8 ≈ **3.6 ms on a 7.7 s mean turn ≈ 0.05 %**, far below turn variance. |
| Collector daemon CPU/RSS | steady 20 ev/s synth: **32.4 % mean / 42.0 % max CPU, 40 MB RSS** (comp-12, 10 min). Idle-adjacent collector on another node: 0.06 % / 39 MB. |
| Capture worker CPU/RSS | under the same load: **0.84 % mean / 84.9 % max CPU (drain bursts), 50.5 MB mean / 54 MB max RSS** |
| Interaction record bytes (spool) | **2 093 B/record** at ~2 KB payload (spool envelope ≈ +2 %) |
| — archive side (CSV envelope) | **2 172.7 B/record** for the same corpus (keeper CSV envelope ≈ +4 % over spool) |
| Edge event bytes (hot) | **612 B/event**; archive CSV: 479–689 B/event across scenarios (mean ≈ 560) |
| Live forward bytes/event | **658 B single / 646 B amortized** (batch ≤ 64) |

## E2 — Freshness & query latency (tab:freshness)

| Marker | FINAL value |
|---|---|
| Emit → SSE frame p50/p95 | full pipeline (emit→collector→forward→ingest→bus→SSE): **10.7 / 11.8 ms**; direct ingest: **8.5 / 8.9 ms** (n=1 000 @ 20 ev/s, 0 lost, live cluster, dashboard node) |
| Cold-path (archive) availability | **p50 20 s, min 20 s, max 30 s** (n=10, 5 s poll granularity). **Revises the "~180 s" claim** — see below. |
| Scenario graph p50 (p95) | **470 ms (594)** |
| Cluster comms p50 (p95) | **2 009 ms (2 043)** |
| Fleet health p50 (p95) | **1 881 ms (2 000)** |
| Doctor scan p50 (p95) | **1 807 ms (1 824)** |
| Trace reconstruction p50 (p95) | **282 ms (313)** (all n=100, on the dashboard node, archive-first serving, corpus ≈ 22 k events / ~450 archive CSVs) |

E2c context: the gateway dry-run (thin corpus) gave 54–58 ms for the first
four views. The live numbers are dominated by corpus size, not the cluster:
comms / fleet / doctor glob and fold the *whole* CSV archive, so they scale
with total archived events (the same effect E3a quantifies — this is the raw
side; the rollup exists precisely to flatten fleet health, and the rollup
story was replay-broken in this deployment, so fleet health here is the
raw-fold upper bound). Scenario-scoped views (graph, traces) stay sub-second.

**The ~180 s number was never the drain.** The archive drain is 20–30 s. The
~180 s people observed is the ChronoLog client's replay-query timeout
(`CL_ERR_QUERY_TIMED_OUT = -12`, ~180 s): every `ReplayStory` whose playback
response cannot reach the caller's receiver service burns exactly that long.
§6.3's "~180 s observed" sentence should be rewritten to report the 20–30 s
drain and attribute the 180 s to the replay-timeout pathology (evidence
below).

## New workaround (2026-07-31): archive-first replay (`CHRONOLOG_ARCHIVE_FIRST=1`)

Found while finishing E2c — and it is why E2c produced a header-only CSV in
the first sitting:

* The **dashboard process's playback receiver never receives player
  responses**. Player log (`chrono-player-ares-comp-17.log`) shows every
  `story_playback_request` from the dashboard's receiver service (ports
  5557/5560) arriving, and **zero** `Successfully transfered
  PlaybackQueryResponse` toward it — while the capture worker's receiver
  (5570) gets responses fine on the same host pair. Each dashboard replay
  therefore blocks ~180 s under the client lock (wedging unrelated requests,
  including live-forward ingest) and fails with -12, *per story, per request*.
* The capture worker's startup `warm_up` replays every known session — for a
  fresh session there is nothing to replay, but the attempt still burns the
  timeout, so with 30+ sessions the worker "warmed up" for an hour before
  draining a single spool record (chicken-and-egg: the sessions it stalls on
  are the ones it should be draining).

Fix (same pattern as the pre-existing CSV-first `index_list` workaround):
`CHRONOLOG_ARCHIVE_FIRST=1` makes `replay_events` serve from the grapher's
drained CSVs only, never asking the player. Worst case ~drain-window (20–30 s)
stale; HWM rebuilds tolerate it via read-side `sequence_id` dedup. Enabled in
`launch-dashboard.sh` and `launch-capture.sh`; code in
`backend/client.py::replay_events`. After the fix: warm-up 34 sessions in
seconds, all five E2c endpoints healthy.

## E3 — Scalability

**E3a (FINAL — the §6.4 plot).** `/api/fleet/health` p50, 16 nodes, 60-min
window, 20 requests/point (`e3a_rollup_scaling_p50.csv`,
`fig_rollup_scaling.tex`):

| events | rollup-backed | raw fold |
|---|---|---|
| 10 000 | 23.5 ms | 107 ms |
| 50 000 | 19.6 ms | 634 ms |
| 100 000 | 24.4 ms | 1 386 ms |
| 250 000 | 19.5 ms | 3 814 ms |
| 500 000 | 18.5 ms | 8 455 ms |

Rollup-backed is flat (~19–24 ms, O(nodes × buckets)); raw fold is linear in
event count (~17 ms per 1 000 events) — a 457× gap at 500 k events.

**E3b (FINAL).** Heatmap query, rollup-backed, default demo density:
**100 nodes: 15.1 ms p50; 250 nodes: 35.6 ms p50** (20 requests each).

**E3c (FINAL).** Agents-per-node sweep, 8 nodes, real agents (haiku turns +
sonnet delegation), ring topology so every edge crosses the collector path:

| K | agents (K×M) | clean exits | edge records archived | collector CPU mean/max | collector RSS |
|---|---|---|---|---|---|
| 1 | 8 | 8/8 | 16 | 0.34 % / 20.5 % | 37 MB |
| 2 | 16 | 16/16 | 32 | 0.13 % / 2.5 % | 39 MB |
| 4 | 32 | 32/32 | 64 | 0.25 % / 21.0 % | 37 MB |

Max K×M exercised cleanly: **4 × 8 = 32 real agents**, per-node sidecar load
flat in K (CPU maxima are transient forward bursts; capture worker during the
K=4 drain: 0.3 % mean / 4.5 % max, 48 MB). Every K also passed the archive
round-trip (edge records = 2 × edges, start+done).

## E4 — Case study (FINAL — measured 2026-07-31, live cluster job 22491)

Injection: `inject_fault.sh` → scenario `eval-case`, target `ares-comp-15`
(6-turn identical-prompt 5xx retry storm, 12 peer-timeout edges, 1 orphaned
delegation, corr `87a888d8…`). Operator: the thesis author (first-ever
attempt at this diagnosis task). Raw rows: `e4_case_study.csv`.

| operator / path | Q1 | Q2 | Q3 | total | correct |
|---|---|---|---|---|---|
| human + dashboard, trial 1 | 35 s | 17 s | 20 s | **72 s** | 3/3 |
| human + dashboard, trial 2 | 10 s | 5 s | 5 s | 20 s | 3/3 |
| human + dashboard, trial 3 | 5 s | 5 s | 5 s | 15 s | 3/3 |
| human + raw logs (grep/less) | — | — | — | **abandoned at ~5–10 min** | 0/3 |
| LLM agent + raw logs (supplementary) | — | — | — | 44 s wall-clock, 4 commands | 3/3 |

Honest reporting, in order of importance:

1. **Human + dashboard: 72 s to a complete, correct first-time diagnosis**
   (all answers verified against the injected ground truth, including the
   orphan's correlation id). Trial 1's Q3 includes a wrong first pick — the
   errored-but-completed edges render adjacent to the true orphan and the
   operator initially named one of them; the span's correlation id caught it.
   Worth a UI note (surface "never completed" more distinctly), and it is
   exactly why the protocol verifies answers against ground truth.
2. **Human + raw logs: zero of three questions answered.** The operator did
   not reach the formal 15-min/question cap — they could not establish a
   starting point in the two JSONL dirs and abandoned after ~5–10 min
   (self-timed, coarse). Recorded as abandonment, not timeout.
3. **Supplementary LLM-operator baseline** (fresh agent, only the three
   questions + the two data dirs, no ground truth, shell text tools only):
   all three correct in 44 s wall-clock / 4 commands — including the orphan
   via `sort | uniq -c` on correlation ids, which the plan predicted would
   "time out" for a grep user. Machine wall-clock is not comparable to human
   minutes; the row is there because it sharpens the claim, not because it
   competes with it.

**The defensible §6.6 claim is therefore:** the raw capture is *sufficient*
for diagnosis (an expert — here, an LLM agent — extracts everything from the
JSONL alone), but not *accessible*: an untrained human failed outright on raw
logs yet produced a complete verified diagnosis in 72 s through the
dashboard. The dashboard substitutes for operator expertise, not for data.
Caveats to state: single injected incident on a small scenario corpus (25
edge events); raw-log attempt ran after the dashboard trials, so answer
leakage would have *favored* the raw-log path — the failure is conservative;
n=1 operator.

## Thesis mapping

* All tab:overhead + tab:freshness markers → values above (E1/E2 tables).
* `fig_rollup_scaling.tex` + CSVs → §6.4 figure (already copied to
  `thesis/figures/`); E3b → the "ms at 100/250 nodes" sentence.
* E3c table → "max K×M exercised and per-node sidecar load".
* §6.3: rewrite the "~180 s observed" sentence per the drain finding above.
* The archive-first workaround → the workarounds section/appendix (player-log
  evidence quoted above; this is the strongest new workaround finding of the
  eval).
* E4 table + the "sufficient but not accessible" framing → §6.6.

## E6 — PrismaDoctor detection quality (FINAL — offline by design)

Measured 2026-07-30, `scripts/eval/e6_doctor_quality.py` (fresh
`/tmp/chronolog_eval_e6_doctor_*` state dir, `CHRONOLOG_OFFLINE=1`, no
cluster/LLM). Ground truth = a manifest of planted faults seeded through the
real spool / inter-agent-store write paths on top of a clean background
(216 healthy turns, 120 healthy edges); scoring against `run_scan()`
(diagnose=True, remember=True). Canonical run: n=10 per kind + one 500×
identical-fault storm across 40 hosts, seed 42. Per-kind rows:
`e6_doctor_quality.csv`; E6 rows in `summary.csv` (the file also carries two
robustness re-runs — n=10/seed=42 and n=25/seed=7 — all configs scored
identically).

| fault kind | injected | detected | missed | FP | cards | diagnosis ok | recurring ok |
|---|---|---|---|---|---|---|---|
| interaction 5xx | 10 | 10 | 0 | 0 | 1 | yes (0.60) | yes |
| 429 rate limit | 10 | 10 | 0 | 0 | 1 | yes (0.85) | yes |
| retry storm | 10 | 10 | 0 | 0 | 1 | yes (0.70) | yes |
| latency outlier | 10 | 10 | 0 | 0 | 1 | yes (0.55) | yes |
| edge error (peer timeout) | 10 | 10 | 0 | 0 | 1 | yes (0.70) | yes |
| orphan call (start, no done) | 10 | 10 | 0 | 0 | 1 | yes (0.70) | yes |
| slow edge | 10 | 10 | 0 | 0 | 1 | yes (0.55) | yes |
| recovery event | 10 | 10 | 0 | 0 | 1 | yes (0.60) | yes |
| 500× storm (edge timeouts, 40 hosts) | 500 | 500 | 0 | 0 | 1 | yes (0.70) | yes |
| clean background | 0 | — | — | 0 | 0 | — | — |

Headline: **580/580 signals detected (100 %), 0 false positives; storm
compression 500 raw → 1 ranked card (overall 580 → 9 cards, 64×); diagnosis
routing 9/9 kinds land on the intended heuristic rule; recurring recognition
9/9 on the second scan (badged with the correct prior_count via
incident_memory.md).**

Honest scope: faults were planted as *single-label* symptoms comfortably
above the detector thresholds (e.g. planted error-edge latencies held below
the 2 000 ms slow-edge floor so each fault trips exactly one detector) — the
experiment establishes that detection, clustering, diagnosis routing, and
cross-run memory are *correct on unambiguous faults*, not the detectors'
threshold sensitivity on borderline cases, and diagnosis correctness means
the incident reached the intended rule of the hand-built rule set (open-world
diagnosis is out of scope per §6.7 limitations). The perfect table is the
expected outcome for deterministic threshold detectors fed in-distribution
faults; the informative results are the zero-FP clean background, the exact
500→1 compression, and the memory round-trip.

* `e6_doctor_quality.csv` → §6.4 (new Detection Quality section) booktabs
  table in the thesis.
