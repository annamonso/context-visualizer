# Thesis Evaluation — Measurement Plan

Fills the 18 `\tbd{...}` markers in `thesis/chapters/06-evaluation.tex`.
Status legend: each experiment lists the **marker(s)** it fills (by table row /
line), the **method**, the **script to build** (under `scripts/eval/`), and the
**runtime**. Experiments E1–E2 are mostly cluster-free or single-node; E3–E5
need a live SLURM allocation. One ~3-hour allocation session covers everything
in E3–E5.

**Already done (no work):** §6.5 Functional validation — 28/28 on live 4- and
8-node clusters via `scripts/run-live-agents.sh` + `verify-when-drained.sh`.

---

## E1 — Capture overhead (Table `tab:overhead`, 8 markers)

### E1a. Spool append latency (Path-A fast path)
- **Fills:** "Spool append — ms, p50/p95".
- **Method:** microbenchmark `CaptureSpool.record_interaction` with a
  realistic ~2 KB payload, N=10,000 appends, on a **compute node** with the
  spool on the same NFS the real runs use (`DTP_STATE_DIR` as in production —
  NFS latency is the honest number, not tmpfs). Warm up 100, then time each
  call with `time.perf_counter_ns()`; report p50/p95/p99.
- **Script:** `scripts/eval/bench_spool.py` (no cluster, just the NFS mount).
- **Runtime:** minutes.

### E1b. Collector POST latency (Path-B fast path)
- **Fills:** "Collector POST — ms, p50/p95".
- **Method:** start `chronolog-collector` on the same node (offline mode is
  fine — we're timing the agent-side round-trip, which returns after enqueue);
  time `collector.emit(event)` for N=10,000 realistic edge events. Report
  p50/p95. Also record the **fallback path** number (collector down → direct
  dashboard POST) for the degradation claim.
- **Script:** `scripts/eval/bench_emit.py`.
- **Runtime:** minutes.

### E1c. End-to-end turn delta (the headline %)
- **Fills:** "End-to-end turn delta — % of turn".
- **Method:** paired comparison with **real agents** (needs LLM budget, small):
  run `real_agent_chronolog.py` with identical prompts/rounds in two modes —
  instrumented (spool + emit as normal) and uninstrumented (env flag
  `EVAL_NO_CAPTURE=1` that turns `record_*`/`emit` into no-ops — add this
  flag to the agent script). N ≥ 30 turns per mode, interleaved A/B to absorb
  provider latency drift. Report mean per-turn wall-clock delta ± CI, and as
  % of mean turn time. Expectation: delta ≪ turn variance; the honest
  headline is then "overhead = E1a+E1b = X ms on a turn of Y s = Z%", with
  the A/B run showing the delta is statistically indistinguishable.
- **Script:** `scripts/eval/ab_turn_overhead.sh` + the no-op flag patch.
- **Runtime:** ~30 min incl. LLM calls. Needs: conda env + `claude` CLI auth
  (same prereqs as the live harness).

### E1d. Sidecar footprint (collector + capture worker)
- **Fills:** "Collector daemon — CPU/RSS", "Capture worker — CPU/RSS".
- **Method:** on one node of a live deployment, drive steady synthetic load
  (`synth_live_traffic.py`, e.g. 6 agents / mesh / speed 1.0) for 10 min;
  sample both PIDs every 2 s from `/proc/<pid>/stat` + `status` (or psutil if
  available). Report mean/max CPU% and RSS MB, with the offered event rate.
- **Script:** `scripts/eval/sample_sidecars.py <pid>...` (CSV out).
- **Runtime:** 15 min, inside the E3 allocation.

### E1e. Storage bytes per event
- **Fills:** "Interaction record — bytes (spool/archive)", "Edge event —
  bytes (hot/archive)".
- **Method:** after a synth run with known event counts: spool bytes =
  `wc -c` of the JSONL files / record count; hot store likewise; archive =
  size delta of the relevant `{chronicle}.{story}.*.csv` files / event count
  (the CSV envelope adds keeper metadata — that's the point of measuring it).
- **Script:** `scripts/eval/measure_storage.sh <state_dir> <output_dir>`.
- **Runtime:** minutes, piggybacks on any run.

### E1f. Live-forward wire bytes
- **Fills:** "Live forward — bytes per event".
- **Method:** `len(json.dumps(payload))` of the forwarded `{"events":[...]}`
  body per event, averaged over the same synth corpus (can be computed
  offline from the hot JSONL — the forward body is the event dict).
- **Script:** fold into `measure_storage.sh`.

## E2 — Freshness & query latency (Table `tab:freshness`, 6 markers)

### E2a. Emit → SSE frame latency
- **Fills:** "Emit → SSE frame at client — p50/p95 ms".
- **Method:** subscriber connects to `/api/_inter-agent/stream?scenario=eval`;
  emitter sends N=1,000 paced events through the **collector** (full pipeline:
  emit → collector → forward → ingest → bus → SSE). **Run emitter and
  subscriber on the same host** (the dashboard node) so one clock stamps both
  ends; latency = receipt `perf_counter` − send stamp embedded in the payload.
  Report p50/p95. Repeat once via direct-ingest fallback for comparison.
- **Script:** `scripts/eval/bench_freshness.py` (threads: emit + SSE client).
- **Runtime:** 10 min, needs the dashboard up (offline mode OK for the
  pipeline itself; do it live in E3 for the real number).

### E2b. Cold-path availability (confirm the ~180 s)
- **Fills:** the "$\sim$180 s" observed row (already stated; confirm with data).
- **Method:** append a marked event, poll the CSV archive dir every 5 s until
  it appears; report the distribution over 10 trials. Live cluster only.
- **Script:** `scripts/eval/measure_drain.py`.
- **Runtime:** ~30 min wall (mostly waiting), runs concurrently with E3.

### E2c. Per-view query latency
- **Fills:** "Scenario graph", "Cluster comms", "Fleet health", "Doctor
  scan", "Trace reconstruction" — p50 ms each.
- **Method:** against the populated live deployment (post-E3 traffic), 100
  timed requests per endpoint: `/api/scenarios/<sid>/graph`,
  `/api/chronolog/comms`, `/api/fleet/health?window=15m`,
  `POST /api/diagnostics/scan`, `/api/tracing/<sid>/traces`. Use a Python
  timer (not curl) to keep one method. Report p50 (p95 in an appendix table).
- **Script:** `scripts/eval/bench_queries.py <base_url> <scenario>`.
- **Runtime:** 10 min.

## E3 — Scalability (3 markers) — needs allocation

### E3a. Rollup vs raw query latency vs event volume (the plot)
- **Fills:** the `\tbd{plot: ...}` in §6.4 → becomes Figure (eval plot #3).
- **Method:** offline (no cluster needed): seed the fleet store with growing
  event counts — 10k, 50k, 100k, 250k, 500k — at fixed 16 nodes / 60-minute
  window (extend `demo_fleet_scale.py` with `--events N --no-rollup` mode).
  Time `/api/fleet/health` (a) rollup-backed, (b) raw-fold fallback, 20
  requests each. Plot latency vs. event count: flat vs. linear.
- **Script:** `scripts/eval/bench_rollup_scaling.py` (+ small
  `demo_fleet_scale.py` extension).
- **Runtime:** ~30 min, laptop/gateway OK.

### E3b. Heatmap at 100 / 250 nodes
- **Fills:** `\tbd{ms at 100 / 250 nodes}`.
- **Method:** `demo_fleet_scale.py --nodes 100` and `--nodes 250`, then time
  `/api/fleet/health` (20 requests, p50). Offline.
- **Script:** part of `bench_rollup_scaling.py`.

### E3c. Agents-per-node sweep
- **Fills:** `\tbd{max K×M exercised and per-node sidecar load}`.
- **Method:** on the live 8-node allocation, `AGENTS_PER_NODE ∈ {1, 2, 4}`
  with the real-agent harness (or synth at matching rates for the load curve
  without LLM cost); record E1d sidecar samples per K. Report the largest
  K×M run cleanly sustained + sidecar CPU/RSS vs K.
- **Runtime:** ~45 min inside the allocation session.

## E4 — Case-study timing (1 marker)

- **Fills:** `\tbd{timed comparison: minutes via dashboard vs raw logs}`.
- **Method:** scripted fault injection (Generate-Traffic API: node-scoped
  retry storm + one killed delegation leg), then two timed diagnosis runs:
  (a) dashboard path — Doctor scan → Fleet → Traces → conversation,
  timed step-by-step (n=3 trials); (b) raw-log baseline — same questions
  answered from the per-node agent logs with grep/less only, 15-minute cap
  per question; record time-or-timeout. Honest reporting: (b) is expected to
  time out on the orphan-call question — report that as the result.
- **Script:** `scripts/eval/inject_fault.sh` (the injection is already
  supported by the synth generator's error knobs; the timing is manual with
  a stopwatch protocol written down beforehand).
- **Runtime:** ~30 min inside the allocation session.

## E5 — The allocation session (one sitting, ~3 h)

```
1. scripts/chronolog-live.sh 8 debug          # deploy, note dashboard node
2. E2b drain measurement (starts, runs in background)
3. synth steady load → E1d sidecar sampling → E1e/E1f storage
4. E2a freshness (on dashboard node), E2c query latencies
5. E3c agents-per-node sweep (real agents, K=1,2,4)
6. E4 fault injection + timed diagnosis
7. Copy out: all CSVs from scripts/eval/out/, state-dir sizes, sinfo snapshot
```

ARES gotchas that will bite otherwise (see memory notes): visor by **raw IP**;
dashboard as **pure reader** (separate `chronolog-capture`); kill synth by PID
(not `pkill -f`); tunnel to the **compute** node with the raw IP + real
terminal for any browser steps.

## Deliverables → thesis mapping

| Output | Goes to |
|---|---|
| `scripts/eval/out/*.csv` + a `results.md` summary | fills all 18 `\tbd` |
| rollup-vs-raw plot (PNG/PGF) | §6.4 figure |
| freshness distribution | §6.3 table (p50/p95) |
| A/B turn delta + CI | §6.2 headline |
| case-study timing table | §6.6 |

Order of work: build the six eval scripts first (all runnable offline except
E2b/E3c/E4), dry-run them offline, then book the allocation and run E5.
