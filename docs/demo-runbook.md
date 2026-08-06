c# ChronoLog Observability — Demo Runbook (advisor / research-group talk)

**Format:** live demo on ARES, ~8–10 min, inside a systems-talk. **Hero view:** the
Scenarios + Cluster *live graph* (agent semantics mapped onto physical cluster
topology — the one thing no other tool shows). **Fallback:** recorded video +
offline `demo_*.py` on a laptop (see §6).

This runbook is the *day-of script*: for each beat — what to run, what to click,
what the audience sees, what to say, and the aha to land. Slide/positioning notes
live in `presentation-plan.md`; the picture to project is `demo-dataflow.html`.

---

## 0. The one thing to land

> Every existing tool answers **"what did my agent do?"** with a span tree of one
> run on one machine. None answers **"what is my agent *fleet* doing across the
> cluster, and which node is dragging it down?"** — because none couples agent
> semantics to physical cluster topology and node-health-at-scale.

The demo is the proof of that sentence, made live. Everything below serves it.

---

## 1. The arc at a glance (map each beat to a research-backed pattern)

| # | Beat | ~min | In the app | Pattern it borrows |
|---|------|------|-----------|--------------------|
| 0 | **Flying blind** | 1.0 | raw multi-node logs (terminal), then cut to dashboard | before/after motivation (Dapper "too intrusive / too blind") |
| 1 | **See the swarm** ★ | 2.0 | **Scenarios → Live** + **Cluster** comms graph + capacity badge | service/dependency map — but on *physical nodes* |
| 2 | **Scale it** | 1.5 | **Fleet** heatmap (node-link dies ~12; heatmap holds 100+) | scalability = credibility; honest limits |
| 3 | **Break it** ★★ | 2.5 | **Generate Traffic** (errors on) → **Doctor** scan → **Traces** waterfall | inject → detect → localize → explain |
| 4 | **Who & why** | 1.0 | click Scenario node → **Workspace** conversation; **Memory** tab | cross-signal pivot (fleet symptom → one agent) |
| 5 | **The substrate** | 1.0 | `chronolog-workarounds.html` / architecture | the one key insight (shared-log = native causal substrate) |

★ = hero moment · ★★ = the money demo. If time is short, cut beats 2 and 4 (they
degrade gracefully); beats 1, 3, 5 are the irreducible spine.

---

## 2. Pre-flight (do this BEFORE the talk — an hour ahead, not live)

SLURM queueing is the classic live-demo killer. Warm everything in advance.

1. **Bring ChronoLog live and let it drain.** From the repo root on ARES:
   ```bash
   scripts/chronolog-live.sh 8 debug        # allocate 8 idle nodes, deploy keepers,
                                            # start collectors + capture + dashboard. No LLM.
   ```
   Note the dashboard node (e.g. `ares-comp-11`) and the SLURM job id. Wait past the
   **~180 s keeper→grapher drain** so the data-heavy tabs (Workspace, Interactions)
   are ready — the live tabs (Scenarios/Cluster/Fleet/Doctor/Traces/Memory) are
   instant and safe immediately.
2. **Seed a baseline so nothing opens empty.** Drive a little steady traffic so the
   graphs have something on open:
   ```bash
   PYTHONPATH=src python3 scripts/synth_live_traffic.py --pattern mesh --agents 6 \
     --nodes 8 --rounds 9999 --speed 1.0 --spool &   # leave running at low rate
   ```
   (Kill it by explicit PID afterward — `pkill -f synth_live_traffic` self-kills the shell.)
3. **Tunnel from your laptop** to the dashboard node (Mercury subnet — must forward to
   the *compute* node, use the raw IP + real Terminal + your key):
   ```bash
   ssh -i ~/.ssh/id_ed25519 -L 24000:ares-comp-11:5000 <gateway>
   # browser → http://localhost:24000
   ```
4. **Open the tabs you'll use in separate browser tabs** so switching is instant:
   `?tab=scenarios` · `?tab=cluster` · `?tab=fleet` · `?tab=doctor` · `?tab=traces`.
5. **Record the whole flow once** as a 3–5 min backup video, and stage the offline
   fallback on the laptop (§6). If SLURM hiccups, you switch to the recording without
   dead air.

**Risk note (be honest in the talk, per systems-demo etiquette):** the live tabs are
served from the in-process *hot store* (instant); the full-fidelity ChronoLog reads
lag the drain. Say so — it's a design point (§5), not an apology.

---

## 3. The runbook — beat by beat

### Beat 0 · Flying blind (1.0 min) — the motivation, made live
- **Show:** a terminal `tail -f` of agent logs on 3–4 nodes at once (or a `pdsh`
  fan-out). It's an unreadable interleaved wall. *Do not* explain it — that's the point.
- **Say:** *"This is a multi-agent LLM run across 8 nodes of a cluster, in the raw. Can
  anyone tell me what the agents are doing, or whether anything's wrong? Neither can I."*
- **Cut** to the dashboard (already open on Scenarios). The contrast IS the motivation slide.

### Beat 1 · See the swarm ★ (2.0 min) — the hero
- **Scenarios tab, Live mode.** The agent-to-agent edge graph builds in real time as
  agents message each other (SSE `/api/_inter-agent/stream` + hot-store backlog).
  - **Say:** *"Nodes are agents, edges are agent-to-agent calls, updating live. This is
    the swarm's logical structure — what every LLM-observability tool gives you, roughly."*
- **Switch to the Cluster tab.** The node-to-node comms graph: which *physical* nodes are
  exchanging messages; idle/available nodes drawn green-dashed. Point at the header
  **cluster-capacity badge** (total / idle / busy / down, live from `sinfo`).
  - **Say (the hero line):** *"Now the same traffic on the physical cluster. THIS is the
    part no LLM tool has — the agent graph and the machine graph in one place. Agent
    semantics on real topology."*
- **Aha:** the two graphs are the same story at two layers. That coupling is the whitespace.

### Beat 2 · Scale it (1.5 min) — the HPC flex (+ honest limit)
- **Say the honest bit first:** *"That node-link view is great up to about a dozen nodes
  — then it's spaghetti. So the fleet view isn't a graph."*
- **Fleet tab.** One cell per node, colored by health; worst nodes sorted first. If your
  live allocation is small, this is where you either (a) drive `Generate Traffic` across
  the real idle nodes, or (b) show the pre-seeded 100–250-node heatmap
  (`demo_fleet_scale.py --nodes 250`) — *say which you're doing*.
  - **Say:** *"Per-(host, minute) rollups make this O(nodes × buckets), independent of
    event volume — so it stays instant at 100+ nodes where the graph can't."*
- **Aha:** the tool scales to cluster size; the visual changes form to match.

### Beat 3 · Break it ★★ (2.5 min) — the money demo
> Let the audience choose the fault if you can — it kills the "cherry-picked run" suspicion.
- **Generate Traffic** button → pattern `pipeline` or `star` (these produce nested
  traces), **Errors slider up**, a few rounds, across real node names. Fire it.
- **Doctor tab → "Scan now".** The failures don't come back as 500 rows — they collapse
  into **one ranked incident card**: root cause + remediation + **blast radius (affected
  hosts)** + confidence. Point at the **hung/dropped-call** incident (a `start` with no
  `done`) — invisible in any per-edge view.
  - **Say:** *"The count is the story. 500 identical timeouts across hosts become one
    incident, with a cause and a fix — and it remembers, so a repeat offender gets a
    `recurring ×N` badge across runs."*
- **Traces tab.** The cross-host **waterfall**; the **critical path** highlighted; the
  **bottleneck hop named by self-time** (exclusive wall-clock, ignoring parent/child overlap).
  - **Say:** *"And here's *why it's slow across N nodes* — the long-pole hop, by self-time."*
- **The population-comparison aha (if the fault is node-scoped):** jump back to **Fleet** —
  the one bad node is red among the greyed rest. *"All the stalls share one node."* That
  single reveal is the demo's peak.

### Beat 4 · Who & why (1.0 min) — the cross-signal pivot
- Back on **Scenarios**, click the offending agent's node → it **joins to its Workspace
  conversation** (session id is `role@scenario`, so the click-through works). You're now
  looking at the actual LLM turns of the culprit.
- Optional: **Memory tab** — that agent's working-memory (context-graph) tokens growing /
  evicting live.
  - **Say:** *"From 'the fleet looks slow' to *this agent's* conversation and memory, in
    two clicks. Symptom to root cause without leaving the tool."*

### Beat 5 · The substrate (1.0 min) — the one key insight, and close
- Bring up `chronolog-workarounds.html` (or the architecture diagram).
- **Say (the takeaway to remember):** *"Under all of this: multi-agent interactions ARE a
  causally-ordered event log. So we don't bolt tracing on — we make a distributed shared
  log, ChronoLog, the native substrate: a keeper on every node, durable and ordered.
  Live views come from an in-process hot store because the log's keeper→grapher drain is
  ~180 s. That's the design: durable cold path + instant hot path, one substrate."*
- **End on the takeaway, not "Questions?"** — restate the one sentence from §0.

---

## 4. Fault-injection cookbook (which knob → which tab reveals it)

| Fault to inject | How | Tab that localizes it | The reveal |
|-----------------|-----|-----------------------|-----------|
| Retry storm / timeouts | Generate Traffic, Errors up | **Doctor** | 500 → 1 ranked incident + fix |
| Hung / dropped inter-agent call | pattern with a killed leg (or Errors) | **Doctor** | `start` with no `done` — invisible elsewhere |
| Slow hop across hosts | pattern `pipeline`/`star`, Speed down on one leg | **Traces** | bottleneck hop by self-time |
| One degraded node | concentrate errors on a node | **Fleet** (+ Cluster) | one red cell among greyed nodes |
| Recurring failure | run the same fault twice, scan twice | **Doctor** | `recurring ×N` badge (cross-run memory) |

Pre-decide ONE primary fault (recommend: retry storm → Doctor, then Fleet node-red
pivot). Keep the others as "want me to try another?" audience options.

---

## 5. Why the design choices (answers you'll be asked)
- *"Why not just OpenTelemetry?"* — OTel/LLM tools stitch spans **within one app**;
  "multi-agent" there means agents in one process, "distributed" means microservice
  trace-correlation. None models the **physical cluster** (nodes/ranks/health). Camp 1
  (agent semantics, no topology) vs Camp 2 (HPC infra, no agent semantics) — we're the
  intersection. (See `presentation-plan.md` for the quadrant.)
- *"Why a shared log substrate?"* — agent interactions are already a causal event
  stream; a distributed log gives ordering + durability + node-local writes for free,
  and sidesteps head/tail-sampling fragmentation at 100+ hosts.
- *"Is the live view real?"* — yes; it's the hot store written at ingest. ChronoLog is
  the durable cold path that fills in after the ~180 s drain. Be upfront about it.

## 6. Fallback (bulletproof, zero dependencies) — the laptop path
If SLURM/tunnel misbehaves, switch to this without breaking stride. Same tabs, seeded
data, no cluster / no API keys / no cost:
```bash
python3 scripts/demos/demo_doctor.py        # errors → incidents → diagnosis → recurring memory
python3 scripts/demos/demo_fleet_scale.py --nodes 250   # 250 nodes summarised in ms
python3 scripts/demos/demo_tracing.py       # bottleneck hop in a 6-host mesh
# then open the SAME data live in the dashboard the demo prints, e.g.:
CHRONOLOG_OFFLINE=1 DTP_STATE_DIR=/tmp/chronolog_demo_doctor \
  PYTHONPATH=src python3 -m chronolog_observability   # http://localhost:5000/?tab=doctor
```
Plus the pre-recorded 3–5 min video from §2.5. Announce the switch plainly ("let me show
the recorded run") — an academic audience respects a clean fallback over a frozen screen.

## 7. If you get more time / a bigger allocation
- **Audience-chosen fault** (hand them the Generate-Traffic knobs) — highest credibility.
- **Live scale-up** N → 2N nodes while the Fleet heatmap redraws — the HPC flex.
- **Runtime query** ("total token cost of everything that touched agent B?") answered
  across agents — the Pivot-Tracing "ask a question you didn't pre-instrument" move.
