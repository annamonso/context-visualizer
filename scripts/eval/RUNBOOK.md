# E5 — Allocation-session runbook (one sitting, ~3 h)

Executes the live half of `docs/evaluation.md` (E1c/E1d/E1e-live, E2a-live,
E2b, E2c, E3c, E4). Everything offline-runnable was already measured and dry-run
— see `scripts/eval/out/` and `scripts/eval/out/results.md`.

All commands from the repo root on the ARES login node unless noted.
**ARES gotchas** (memory notes): visor by **raw IP**; dashboard is a **pure
reader** (capture runs as its own process); kill synth by **PID**, not
`pkill -f`; tunnel to the **compute** node with raw IP + a real terminal;
archive stale grapher CSVs before starting (`_keeper_topology` globs them all):

```bash
OUT=~/chronolog-install/chronolog/output
mkdir -p ~/chronolog-output-backup
find "$OUT" -maxdepth 1 -type f ! -newermt "$(date +%F)" -print0 | xargs -0 -r mv -t ~/chronolog-output-backup/
```

## 0. Deploy (≈15 min)

```bash
scripts/ops/chronolog-live.sh 8 debug          # note JOB_ID + dashboard node
export DASH_NODE=<dashboard compute node>  # printed by the script
export DASH=http://$DASH_NODE:5000
export STATE=/mnt/common/$USER/observe-state   # the DTP_STATE_DIR it used
```

Confirm `curl $DASH/api/config` responds and the Cluster tab shows all 8 nodes.

## 1. E2b drain measurement — start FIRST, runs in background (≈30 min wall)

On the dashboard node (or any node with a collector):

```bash
python3 scripts/eval/measure_drain.py \
  --collector-url http://127.0.0.1:5650 --dashboard-url $DASH \
  --archive-dir ~/chronolog-install/chronolog/output --trials 10 \
  > scripts/eval/out/e2b_drain.log 2>&1 &
echo $! > /tmp/drain.pid
```

## 2. Steady synth load → sidecars + storage (≈25 min)

```bash
# on the dashboard node: steady load, note the offered rate it prints
python3 scripts/demos/synth_live_traffic.py --scenario eval-live --agents 6 \
  --pattern mesh --speed 1.0 --spool &   # kill later by THIS pid
echo $! > /tmp/synth.pid

# find the sidecar pids on one deploy node (ssh in; allocation makes ssh work)
pgrep -af "chronolog_observability.collector"      # collector daemon
pgrep -af "capture.sync_worker"                    # capture worker
python3 scripts/eval/sample_sidecars.py <collector_pid> <capture_pid> \
  --duration 600 --interval 2 --tag synth6
```

After ~10 min of load (E1e/E1f — needs known event counts, so do it while
synth is still the only writer):

```bash
scripts/eval/measure_storage.sh $STATE ~/chronolog-install/chronolog/output
```

## 3. E2a freshness + E2c query latency (≈20 min, ON the dashboard node)

```bash
# full pipeline (emit → collector → forward → ingest → bus → SSE)
python3 scripts/eval/bench_freshness.py --dashboard http://127.0.0.1:5000 \
  --collector-url http://127.0.0.1:5650
# fallback comparison row
python3 scripts/eval/bench_freshness.py --dashboard http://127.0.0.1:5000 --direct

# E2c — after the synth traffic has populated the views; NB: the scenario-graph
# endpoint replays ChronoLog — wait for the drain (or use a drained scenario)
python3 scripts/eval/bench_queries.py http://127.0.0.1:5000 eval-live --tag live
```

Also the live-node numbers for E1a/E1b (the honest NFS/compute numbers — the
gateway dry-run only validated the scripts):

```bash
python3 scripts/eval/bench_spool.py --state-dir $STATE --tag compute
python3 scripts/eval/bench_emit.py --flask-url http://127.0.0.1:5000 --live
```

## 4. E1c + E3c — real agents, A/B overhead + agents-per-node sweep (≈45 min)

E1c (any one deploy node, conda env + `claude` CLI auth):

```bash
conda run -n iowarp env FLASK_URL=$DASH MY_HOST=$(hostname) \
  PEERS=<peer_host>:executor N_PAIRS=10 STATE_DIR=$STATE \
  scripts/eval/ab_turn_overhead.sh
```

E3c — `AGENTS_PER_NODE ∈ {1,2,4}` with `scripts/ops/run-live-agents.sh` (or synth
at matching rates for the load curve without LLM cost); during each K, rerun:

```bash
python3 scripts/eval/sample_sidecars.py <collector_pid> <capture_pid> \
  --duration 300 --tag k$K
```

Record the largest K×M that ran cleanly (28/28 verify) → fills
"max K×M exercised and per-node sidecar load".

## 5. E4 — fault injection + timed diagnosis (≈30 min)

```bash
DTP_STATE_DIR=$STATE scripts/eval/inject_fault.sh $DASH eval-case $DASH_NODE
```

**Stopwatch protocol — write it down BEFORE starting; n=3 trials per path.**
Questions (same for both paths):

  Q1. Which node is unhealthy, and what is its dominant error?
  Q2. Which session is stuck in a retry loop, and on what prompt?
  Q3. Is there a delegation that started and never completed? Which
      caller→callee pair (correlation id)?

Path (a) — dashboard: Doctor scan → Fleet → Traces → conversation click-through.
Time each question separately from "hands on keyboard" to "answer stated aloud".
Path (b) — raw logs baseline: same questions from the per-node agent logs with
`grep`/`less` only (no dashboard), **15-minute cap per question**; record
time-or-timeout. Honest reporting: (b) is expected to TIME OUT on Q3 (the
orphan has no done-frame to grep for) — report that as the result.

Record into `scripts/eval/out/e4_case_study.csv`:

```csv
trial,path,question,seconds,timeout,answer_correct
1,dashboard,Q1,42,0,1
1,rawlogs,Q3,900,1,0
...
```

## 6. Copy out & release

```bash
tar czf ~/eval-results-$(date +%F).tgz scripts/eval/out/
du -sh $STATE ~/chronolog-install/chronolog/output >> scripts/eval/out/state_sizes.txt
sinfo > scripts/eval/out/sinfo_snapshot.txt
kill $(cat /tmp/synth.pid)          # by pid — never pkill -f
scancel <JOB_ID>
```

## Marker → script map (all 18 `\tbd`)

| `\tbd` marker (06-evaluation.tex) | Filled by |
|---|---|
| Spool append p50/p95 | `bench_spool.py` (step 3, compute node) |
| Collector POST p50/p95 | `bench_emit.py` (step 3) |
| End-to-end turn delta % | `ab_turn_overhead.sh` (step 4) |
| Collector daemon CPU/RSS | `sample_sidecars.py` (step 2) |
| Capture worker CPU/RSS | `sample_sidecars.py` (step 2) |
| Interaction record bytes | `measure_storage.sh` (step 2) |
| Edge event bytes | `measure_storage.sh` (step 2) |
| Live forward bytes | `measure_storage.sh` (step 2) |
| Emit → SSE p50/p95 | `bench_freshness.py` (step 3) |
| ~180 s observed drain | `measure_drain.py` (step 1) |
| Scenario graph p50 | `bench_queries.py` (step 3) |
| Cluster comms p50 | `bench_queries.py` (step 3) |
| Fleet health p50 | `bench_queries.py` (step 3) |
| Doctor scan wall | `bench_queries.py` (step 3) |
| Trace reconstruction p50 | `bench_queries.py` (step 3) |
| §6.4 plot rollup vs raw | `bench_rollup_scaling.py` (offline — DONE) |
| ms at 100/250 nodes | `bench_rollup_scaling.py` (offline — DONE) |
| max K×M + sidecar load | step 4 sweep |
| timed comparison (case study) | step 5 |
