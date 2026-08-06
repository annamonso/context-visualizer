# ChronoLog Observability — Data Schema & Capture Reference

**The complete, field-level specification of every data structure the observability
platform captures, at every phase of every pipeline** — what each field is, *why*
it is captured, where it is stored (the ChronoLog Chronicle/Story or the on-disk
path), and the exact structure of the record.

This is the ground-truth schema contract. It is derived directly from the code;
each section cites the `file:line` where the shape is defined so the doc can be
re-verified. For the narrative feature tour see [`FEATURES.md`](../FEATURES.md);
for setup see [`README.md`](../README.md).

---

## Table of contents

1. [Model: phases, paths, and the storage substrate](#1-model)
2. [Path-A — LLM interactions (capture → spool → ChronoLog → read)](#2-path-a)
   - 2.1 [Interaction record](#21-interaction-record)
   - 2.2 [Context-graph node](#22-context-graph-node)
   - 2.3 [Recovery event](#23-recovery-event)
   - 2.4 [Spool sequencing & high-water state](#24-spool-state)
3. [Path-B — Inter-agent edges (emit → collector → ChronoLog + live bus)](#3-path-b)
   - 3.1 [InterAgentMessage edge](#31-interagentmessage)
   - 3.2 [Ingest wire payload (single + batch)](#32-ingest-payload)
   - 3.3 [Stitched edge (read shape)](#33-stitched-edge)
   - 3.4 [Live-bus SSE frames](#34-sse-frames)
4. [Fleet health rollups (metrics_rollup)](#4-fleet)
5. [PrismaDoctor diagnostics (gather → detect → cluster → diagnose → remember)](#5-doctor)
6. [Semantic checks, checkpoints & rollback](#6-semantic)
7. [The `__index__` chronicle (listing without ShowChronicles)](#7-index)
8. [ChronoLog storage envelope & CSV archive](#8-envelope)
9. [API read models (what each dashboard tab consumes)](#9-api)
10. [Reference tables — Chronicle/Story map, spool layout, dedup keys, env vars](#10-reference)

---

<a name="1-model"></a>
## 1. Model: phases, paths, and the storage substrate

### The two capture paths

Every datum enters through one of two ingest paths, each with its own record
types, storage, and phase pipeline:

```
                            ┌───────── Path-A: LLM turns ─────────┐
 agent / proxy ──record_*──▶│ capture spool (JSONL, shared NFS)   │──sync worker──▶ ChronoLog stories
                            └─────────────────────────────────────┘                (llm_interactions,
                                                                                     context_graphs,
                                                                                     recovery_events)
                            ┌──── Path-B: inter-agent calls ──────┐
 agent MCP wrapper ──emit──▶│ per-node collector daemon (:5650)   │──┬─▶ ChronoLog keeper (ia_<scenario>/edges)
                            └─────────────────────────────────────┘  └─▶ dashboard /ingest ──▶ live bus (SSE)

 dashboard reads: ChronoLog (cold, full fidelity, after ~180s drain) + local hot store (live, instant)
```

- **Path-A** carries LLM interactions — prompts, responses, tokens, cost, latency,
  and working-memory (context-graph) growth. Producers append to a local **capture
  spool**; the **sync worker** drains the spool into ChronoLog.
- **Path-B** carries inter-agent calls — agent→agent MCP tool invocations, emitted
  as a `start`/`done` pair. A per-node **collector** writes them to that node's
  ChronoLog keeper *and* forwards a thin copy to the dashboard for the live stream.

### The four phases (both paths share this shape)

| Phase | What happens | Where the data lives |
|-------|--------------|----------------------|
| **Capture** | Producer builds a free-form record and calls `record_*` / `emit()`. | in-memory dict |
| **Spool / relay** | Record is appended to a local durable buffer (Path-A spool JSONL, Path-B collector queue + hot JSONL). | local disk (`~/.dt_provenance/…` or `$DTP_STATE_DIR`) |
| **ChronoLog** | Sync worker / collector writes the record to a Chronicle/Story via the node-local keeper. | ChronoLog (distributed, durable) |
| **Read** | Reader replays ChronoLog (or the CSV archive after drain, or the hot store for live) and shapers build the API view models. | dashboard JSON |

### Why ChronoLog is *not* read in real time

ChronoLog is the durable cold path but is unreadable live: the keeper→grapher
drain lags **~180 s**, and a `ReplayStory` on an un-drained story blocks the
process for minutes holding the GIL. So live views (Scenarios, Fleet, Doctor,
Traces, Memory) are served from **local hot stores / spools** at the moment of
ingest; full-fidelity views (Workspace, Interactions) fill in from ChronoLog
after the drain. This split is the reason many records are written to **two**
places (a durable ChronoLog copy and a local hot copy) — see the
`X-DTP-Forwarded` contract in [§3.2](#32-ingest-payload).

### ChronoLog naming contract (`backend/constants.py`)

Chronicle/Story names are a **compatibility contract** — they define the on-disk
layout an existing deployment already holds.

| Chronicle | Story | Holds | Path |
|-----------|-------|-------|------|
| `llm_interactions` | `<session_id>` | Interaction records | A |
| `context_graphs` | `<session_id>` | Context-graph diff nodes | A |
| `recovery_events` | `<session_id>` | Recovery events | A |
| `checkpoints` | — | *reserved, currently unused* (checkpoints persist to a local JSON file) | A |
| `metrics_rollup` | `minute` | Fleet per-`(host,minute)` buckets | B/rollup |
| `ia_<scenario>` | `edges` | Inter-agent start/done edge events | B |
| `__index__` | `scenarios` / `sessions` / `hosts` | Listing index (`{"v": …}` entries) | both |

`inter_agent_chronicle(scenario_id)` sanitizes the scenario id: non-`[alnum -_.]`
→ `_`, truncated to 120 chars (`constants.py:34-37`). The Story name for Path-A
chronicles is **the session id itself** (e.g. `writer@scenario-x`).

---

<a name="2-path-a"></a>
## 2. Path-A — LLM interactions

**Pipeline:** producer calls `spool.record_*` → append-only JSONL spool →
`SpoolToChronoLogSync` drains to ChronoLog → `path_a_reader` replays for views.

**Capture is at-least-once, ingest is effectively exactly-once:** the worker
dedups against per-`(session, kind)` high-water marks and rebuilds that state from
ChronoLog on restart, so a restart never re-writes accepted events.

**Producers** (the de-facto field contract): `scripts/real_agent_chronolog.py`,
`scripts/demos/_demo_common.py`, `scripts/synth_live_traffic.py`.
**Consumers** (which fields each view reads): `adapters/shape/conversation.py`,
`analysis/*`, `api/provenance.py`.

> **Field enforcement note.** The spool payload is a **free-form dict**. The spool
> code itself only *injects/guarantees* three fields (`session_id`,
> `sequence_id` or `blob_name`, `timestamp`). Everything else is a contract
> established by producers and relied on by consumers. Fields are tagged below as
> **[spool]** (injected/guaranteed) or **[producer]** (contract).

### 2.1 Interaction record

One LLM turn. Write API: `CaptureSpool.record_interaction(session_id, payload, *, sequence_id=None)`
(`capture/spool.py:141`). Spool kind `interactions`. Lands in Chronicle
`llm_interactions`, Story `<session_id>`. Read by
`path_a_reader.get_session_interactions` / `get_interaction` (`backend/path_a_reader.py:99`),
deduped by `sequence_id` (last-write-wins), seq-ordered.

| Field | Type | Tag | Why captured |
|-------|------|-----|--------------|
| `session_id` | str | [spool] | The agent session (`role@scenario`); **becomes the ChronoLog Story name.** Read by every Path-A view. |
| `sequence_id` | int | [spool] | Monotonic per-`(session,kind)` id (auto-allocated if omitted). **The dedup / high-water / ordering key** for exactly-once ingest. |
| `timestamp` | str ISO `…%fZ` | [spool] | Turn wall-clock (`setdefault`). Timeline ordering. |
| `scenario_id` | str | [producer] | Ties the turn to the inter-agent scenario graph. |
| `host` | str | [producer] | Node that ran the turn; Fleet/host attribution. |
| `provider` | str (`anthropic`/`openai`/`ollama`) | [producer] | Selects the parse branch in the shaper and the checkpoint detector. |
| `model` | str | [producer] | Model id; per-turn display, cost. |
| `request` | dict | [producer] | The LLM request (nested below). |
| `request.method` | str (`POST`) | [producer] | HTTP verb of the proxied call. |
| `request.path` | str (`/v1/messages`) | [producer] | API endpoint. |
| `request.body.system` | str \| list | [producer] | System prompt; extracted for the turn view + token estimate. |
| `request.body.messages` | list[`{role, content}`] | [producer] | The prompt turns; rendered in the conversation, and replayed by rollback (`request.body.messages`). |
| `response` | dict | [producer] | The LLM response (nested below). |
| `response.status_code` | int | [producer] | HTTP status; turns ≥400 flagged as errors. |
| `response.text` | str | [producer] | Raw response text; builds the response preview. |
| `response.tool_calls` | list[dict] | [producer] | Tool invocations (esp. streaming). |
| `response.is_streaming` | bool | [producer] | Streaming flag. |
| `metrics.total_latency_ms` | number | [producer] | Turn latency; latency views. |
| `metrics.delta_input_tokens` | int | [producer] | Prompt tokens this turn; token/cost rollups. |
| `metrics.delta_output_tokens` | int | [producer] | Completion tokens this turn. |
| `metrics.delta_cost_usd` | number | [producer] | Turn cost (USD); cost rollups. |
| `tool_calls` (top level) | list[dict] | [producer] | Duplicate of tool calls at top level (real-agent producer). |

**Alternate flat shape** (`synth_live_traffic.py`): some producers use flat keys
instead of the nested `metrics` block — `method`, `path`, `status_code`,
`is_streaming`, `total_latency_ms`, `response_text`, and
`usage:{input_tokens, output_tokens}` (may also carry `prompt_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`). The shaper tolerates both.

```json
{
  "session_id": "writer@scenario-x",
  "sequence_id": 3,
  "scenario_id": "scenario-x",
  "host": "ares-comp-11",
  "timestamp": "2026-07-20T10:15:30.123456Z",
  "provider": "anthropic",
  "model": "claude-opus-4-8",
  "request":  {"method": "POST", "path": "/v1/messages",
               "body": {"messages": [{"role": "user", "content": "do the work"}]}},
  "response": {"status_code": 200, "text": "…", "is_streaming": false},
  "metrics":  {"total_latency_ms": 320, "delta_input_tokens": 500,
               "delta_output_tokens": 50, "delta_cost_usd": 0.001}
}
```

### 2.2 Context-graph node

A working-memory diff (a node added to / evicted from context). This is the
**authoritative per-turn metrics carrier** — the conversation shaper reads
tokens/cost/latency from the context node, *not* the interaction
(`conversation.py:239-246`). Write API:
`CaptureSpool.record_context_node(session_id, payload, *, sequence_id=None)`
(`spool.py:153`). Spool kind `context_graph`. Chronicle `context_graphs`,
Story `<session_id>`. Read by `path_a_reader.get_context_graph(session_id, since=0)`
(filters `sequence_id > since`).

| Field | Type | Tag | Why captured |
|-------|------|-----|--------------|
| `session_id` | str | [spool] | Owning session / Story. |
| `sequence_id` | int | [spool] | Dedup / high-water / ordering key. |
| `timestamp` | str ISO | [spool] | Node time (`setdefault`). |
| `op` | str (`add`/`evict`) | [producer] | Diff operation — node added or evicted; drives the Memory tab growth/eviction curve. |
| `node` | str (`ctx-3`, `writer-3`) | [producer] | Node identifier in the context graph. |
| `summary` | str | [producer] | Short human label (prompt/turn snippet, `prompt[:60]`). |
| `tokens` | int | [producer] | **Content this turn added to working memory** — the value the Memory tab plots as real context growth. |
| `delta_input_tokens` | int | [producer] | Prompt tokens (mirrors the interaction); token/cost views. |
| `delta_output_tokens` | int | [producer] | Completion tokens. |
| `delta_cost_usd` | number | [producer] | Turn cost. |
| `latency_ms` | number | [producer] | Turn latency (real-agent producer) → `total_latency_ms` in views. |
| `model` | str | [producer] | Model id (real-agent producer). |
| `ttft_ms` | number | [producer] | Time-to-first-token (analysis latency chart, when present). |
| `event_type` | str | [producer] | Turn type label (default `continuation`) for the token-delta chart. |
| `delta_effective_input_tokens` | int | [producer] | Effective (post-cache) prompt tokens this turn; token-delta chart. |
| `total_effective_input_tokens` | int | [producer] | Cumulative effective context size; context-growth chart. |

```json
{
  "session_id": "writer@scenario-x", "sequence_id": 3,
  "timestamp": "2026-07-20T10:15:30.123456Z",
  "op": "add", "node": "ctx-3", "summary": "do the work",
  "tokens": 550, "delta_input_tokens": 500,
  "delta_output_tokens": 50, "delta_cost_usd": 0.001
}
```

### 2.3 Recovery event

A checkpoint-restore / backtrack signal. Write API:
`CaptureSpool.record_recovery(session_id, payload, *, blob_name=None)`
(`spool.py:165`). Spool kind `recovery`. Chronicle `recovery_events`,
Story `<session_id>`. Read by `path_a_reader.get_recovery_events(session_id)`
(sorted by `timestamp`). **Dedup key is `blob_name`, not `sequence_id`.**

| Field | Type | Tag | Why captured |
|-------|------|-----|--------------|
| `session_id` | str | [spool] | Owning session / Story. |
| `blob_name` | str (uuid4 hex if absent) | [spool] | **Unique dedup id** — the worker dedups recovery events on this instead of `sequence_id`. |
| `timestamp` | str ISO | [spool] | Event time (`setdefault`); read path sorts on it. |
| `reason` | str (`checkpoint-restore`, …) | [producer] | Human reason for the restore/backtrack. |
| `host` | str | [producer] | Node where recovery happened. |
| `source_session_id` | str | [producer] | Used by `build_rollback_plan` to find peers with unacknowledged events from the trigger session. |
| `acknowledged` | bool | [producer] | Whether the event was acknowledged; the rollback planner filters unacked. |

### 2.4 Spool sequencing & high-water state

Not records — the control state that turns at-least-once capture into
exactly-once ingest.

**On-disk spool layout** (`capture/spool.py`):

- **Root:** `$DTP_STATE_DIR/path_a_spool/` else `~/.dt_provenance/path_a_spool/`.
- **Format:** JSONL, one record per line, compact separators, `O_APPEND`
  (POSIX-atomic per line).
- **File naming:** `<safe_session>__<kind>.jsonl` (separator `__`):
  - `<safe_session>__interactions.jsonl`
  - `<safe_session>__context_graph.jsonl`
  - `<safe_session>__recovery.jsonl`
- **`_safe()`** keeps `alnum - _ . @`, truncates to 120 chars. **`@` is
  deliberately preserved** so the spool filename → ChronoLog Story name round-trips
  with the `role@scenario` session id. (Mapping `@`→`_` once desynced the stored
  story from the looked-up id, showing zero tokens / an empty conversation.)

**In-memory sequencing (`CaptureSpool`):**

| State | Type | Purpose |
|-------|------|---------|
| `_next_seq` | `{(session,kind) → int}` | Next monotonic sequence id per stream; seeded from `_max_seq_on_disk` so ids stay monotonic across restarts. |
| `_read_cache` | `{(path,size,mtime_ns) → [dict]}` | Memoized JSONL parse (append-only ⇒ unchanged stat = unchanged parse); capped at 2048. |

**Sync-worker high-water state (`capture/sync_worker.py`):**

| State | Type | Purpose |
|-------|------|---------|
| `_hwm` | `{(session_id, kind) → int}` | Last `sequence_id` forwarded to ChronoLog; only `seq > hwm` is appended. Rebuilt on startup by replaying ChronoLog. |
| `_recovery_seen` | `{session_id → set[blob_name]}` | Recovery dedup set (blob_name-based); warm-seeded from replay. |
| `_index_seen` | `set[str]` | Sessions already `index_add`-ed (avoids redundant index appends). |
| `SyncStats` | dataclass | Telemetry: `polls`, `sessions_seen`, `events_written{kind:int}`, `last_error`, `last_poll_started_ns`, `last_poll_finished_ns`. Exposed via `to_dict()`. |

**Chronicle/Story routing** happens in `sync_worker._chronicle_story`
(`sync_worker.py:219`); session indexing in `_index_session` →
`index_add(INDEX_STORY_SESSIONS, session_id)`.

---

<a name="3-path-b"></a>
## 3. Path-B — Inter-agent edges

**Pipeline:** agent MCP wrapper `emit()` → per-node **collector daemon** (`:5650`)
stamps ids → writer thread writes to ChronoLog keeper *and* forwards to the
dashboard → dashboard persists a hot copy + publishes to the live bus → SSE
subscribers. The store & bus live in `capture/inter_agent/store.py` and
`capture/inter_agent/bus.py` (not `spool.py`).

Each logical call emits **two** records sharing one `correlation_id`: a `start`
and a `done`.

### 3.1 InterAgentMessage (the stored edge event)

`@dataclass InterAgentMessage` (`capture/inter_agent/store.py:47`). Chronicle
`ia_<sanitised-scenario>`, Story `edges`. Written by the collector
(`daemon.py:153`) and the store (`store.py:199`). Offline: JSONL at
`<state>/inter_agent/<scenario>.jsonl`.

| Field | Type | Default | Why captured / consumer |
|-------|------|---------|-------------------------|
| `event_id` | str | auto `uuid4().hex` | Unique per-record id; the **SSE dedup key** and the collector-preserved id on forward. |
| `scenario_id` | str | required | Selects the ChronoLog Chronicle (`ia_<scenario>`) and the bus topic. |
| `correlation_id` | str | auto uuid if missing | **Stitches the `start`+`done` pair into one edge.** |
| `phase` | str `start`\|`done` | required | Which half of the two-phase call. `done` is authoritative for status/latency. |
| `from_host` | str | required | Source node — edge tail. |
| `from_session` | str | required | Source agent session (`role@scenario`); joins to Path-A session records. |
| `to_host` | str | required | Destination node — edge head; the host attributed in fleet bucketize. |
| `to_session` | str | required | Destination agent session; the `agents` dimension in fleet rollup. |
| `ts` | str ISO `…%fZ` | required | Event wall-clock. Becomes `ts_start`/`ts_done`; parsed to ns for fleet minute-bucketing. Preserved verbatim on forward. |
| `kind` | str | `mcp_call` | Edge type (`mcp_call`/`tool_invoke`/`direct_message`); graph label. |
| `tool_name` | str | `""` | MCP tool invoked (e.g. `call_remote_agent`); shown on the edge. |
| `payload_digest` | str | `""` | `sha256:<hex>` of the call payload — reference a payload without storing it. |
| `payload_preview` | str | `""` | First 512 chars of the payload — a peek for the detail pane. |
| `status` | str | `""` | Set only on `done`: `ok` or `error:<msg>`. `startswith("error")` drives the fleet error count. Empty = in-flight. |
| `latency_ms` | float | `0.0` | Set only on `done`. Call duration; fleet latency sum/p95/max. |
| `ingest_ns` | int | `0` | Server-side `time.time_ns()` stamp; survives collector relays; monotonic ordering. |
| `parent_correlation_id` | str | `""` | The `correlation_id` of the call this one was made *while servicing* — lets the tracer build exact call trees; empty ⇒ tracer infers parents from session+time nesting. |

### 3.2 Ingest wire payload (single + batch)

`POST /api/_inter-agent/ingest` (dashboard) and collector `POST /ingest`.

**Single-event body.** Required (validated): `scenario_id`, `phase`
(`start`\|`done`), `from_host`, `from_session`, `to_host`, `to_session`. Optional:
`correlation_id` (auto uuid), `kind` (default `mcp_call`), `tool_name`, `payload`
(object *or* raw string — hashed + truncated to `payload_digest`/`payload_preview`),
`status` (honored only on `done`), `latency_ms` (only on `done`).

**Batch body:** a bare array `[ {…}, … ]` *or* `{"events": [ {…}, … ]}`. The
collector always relays the `{"events":[…]}` shape.

**Collector stamping (`daemon.py:105`):** before enqueue, the collector fills
`event_id` (uuid, setdefault), `correlation_id` (uuid, setdefault), `ts`
(setdefault), and **unconditionally** `ingest_ns = time.time_ns()`. These stamped
ids are what the dashboard later preserves — ids must match across the ChronoLog
copy and the forwarded copy.

**Responses.** Single: `{"event_id", "correlation_id", "ts"}`. Batch:
`{"accepted": n, "results": [...], "errors": [{"index", "error"}]}` — HTTP 200 if
`accepted>0` else 400; store failure → 500. Collector `GET /health`:
`{"ok", "queued", "accepted", "flushed", "dropped"}`.

**`X-DTP-Forwarded: chronolog` — the exactly-once contract.**
- The collector sets the header **only when it already wrote the event to
  ChronoLog**. If the ChronoLog write failed, it forwards *without* the header so
  the dashboard persists it per its own backend — degraded, not lost.
- On the dashboard, `forwarded = header == "chronolog"`. When true: (a) the parser
  **preserves** the supplied `event_id`/`ts`/`ingest_ns`/digest instead of
  regenerating, and (b) `store.append(skip_chronolog=True)` writes only the hot
  JSONL copy and skips the second ChronoLog write. Net effect: **each event
  persisted exactly once per backend.**
- The direct agent→dashboard fallback (collector down) sends no header, so the
  dashboard persists normally.

### 3.3 Stitched edge (read shape)

`read_stitched()` (`store.py:325`) — **not** the stored shape. One merged dict per
`correlation_id`, returned by `GET /api/scenarios/<sid>/inter-agent` and consumed
by the scenario graph, tracing, and fleet:

`correlation_id, scenario_id, from_host, from_session, to_host, to_session, kind,
tool_name, payload_digest, payload_preview, parent_correlation_id,
ts_start (str|None), ts_done (str|None), status (defaults "ok" on done),
latency_ms (float)`. In-flight calls keep `ts_done = None`.

### 3.4 Live-bus SSE frames

In-process fan-out (`EventBus`) because ChronoLog's ~180 s drain makes it unusable
live. Frame format: `event: <name>\ndata: <compact-json>\n\n`. Route
`GET /api/_inter-agent/stream?scenario=<sid>&heartbeat_sec=&backlog=`.

| SSE `event:` | `data` payload | Reason |
|--------------|----------------|--------|
| `backlog` | full `InterAgentMessage` dict | Late-join catch-up (replayed first). On by default offline, off vs live backend; override `?backlog=0\|1`. |
| `catchup` | `{"scenario_id", "count"}` | Marks end of backlog / start of live. |
| `message` | full `InterAgentMessage` dict | A newly-ingested event. Deduped against backlog `event_id`s. |
| *(keepalive)* | `: keepalive\n\n` comment | Keeps proxies/browsers alive; lets the server notice a closed socket (interval `heartbeat_sec`, clamped 1–60 s). |

Bus internals: per-subscription bounded queue (`maxsize=1000`, drop-oldest with a
`dropped` counter); topic = `scenario_id`, `"*"` = firehose; `publish()` never
blocks/raises into ingest (the event is already durable). Late-join protocol:
**subscribe first, then read backlog**, so a gap event lands in both and dedup is
exhaustive.

---

<a name="4-fleet"></a>
## 4. Fleet health rollups (`metrics_rollup`)

Pre-aggregated per-`(host, minute)` buckets so the Fleet overview is
`O(nodes × buckets)` instead of `O(events)` — the thing that keeps it responsive at
100+ nodes. Emitter opt-in (`CHRONOLOG_FLEET_ROLLUP=1` / `chronolog-fleet-rollup`);
the API falls back to computing from the spool on demand so the tab is never empty.

### 4.1 Bucket (`metrics_rollup` / `minute` cell)

`@dataclass Bucket` (`fleet/rollup.py:74`). Built by `bucketize()`, persisted by
`write_buckets()`. Chronicle `metrics_rollup`, Story `minute`. Offline: JSONL
`<state>/fleet/rollup.jsonl`.

**Key:** composite `(host: str, minute: int)` where `minute` = epoch **seconds**
minute-aligned (`(ts_ns//1e9)//60*60`). Each emit writes a **recomputed full
snapshot** of the cell; the reader keeps the last value seen per key, so re-emits
never double-count ("most complete snapshot wins").

| Field | Type | How aggregated |
|-------|------|----------------|
| `host` | str | Interaction `host` or edge `to_host`. Key part 1. |
| `minute` | int (epoch s) | Key part 2. |
| `events` | int | Count of records in the cell (interactions + edges). |
| `errors` | int | Records where `is_err` (interaction `error`/`status_code>=400`, or edge `status` starting `error`). |
| `latency_sum` | float | Σ latencies > 0 in the minute. Numerator of avg. |
| `latency_count` | int | Count of latency samples > 0. Denominator of avg. |
| `latency_p95` | float | p95 **within this minute**, at build time. |
| `latency_max` | float | Max latency in the minute. |
| `input_tokens` | int | Σ `delta_input_tokens` / `input_tokens` (edges contribute 0). |
| `output_tokens` | int | Σ `delta_output_tokens` / `output_tokens`. |
| `cost_usd` | float | Σ `delta_cost_usd` / `cost_usd`. |
| `agents` | int | Count of distinct sessions (`session_id` / `to_session`). |

### 4.2 NodeHealth (computed on read)

`@dataclass NodeHealth` (`rollup.py:119`), folded from a window of buckets by
`health_from_buckets()`. **Not persisted** — computed per read. Served by
`GET /api/fleet/health`.

Fold rules: `events/errors/tokens/cost` summed; `agents = max` per-minute;
`last_minute = max`; `avg_latency_ms = Σlatency_sum / Σlatency_count`;
`p95_latency_ms = max` of the per-minute bucket p95s in the window (an honest
upper-ish estimate without keeping raw samples); `error_rate = errors/events`.

`status` (`ok`/`warn`/`crit`): **crit** if `error_rate ≥ 0.20` OR `p95 ≥ 15000 ms`;
**warn** if `error_rate ≥ 0.05` OR `p95 ≥ 5000 ms`; else **ok**.

`to_dict()` — the exact heatmap payload:

```json
{"host": "ares-comp-08", "events": 0, "errors": 0, "error_rate": 0.0,
 "avg_latency_ms": 0.0, "p95_latency_ms": 0.0,
 "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
 "cost_usd": 0.0, "agents": 0, "last_minute": 0,
 "in_allocation": true, "status": "ok"}
```

`total_tokens` is derived (`input + output`), present only in `to_dict()`.
`in_allocation` is canonicalized so a SLURM-padded bucket host (`ares-comp-08`)
matches its allocation entry (`ares-comp-8`); allocation nodes that produced no
events are injected as empty `NodeHealth` so **idle/silent nodes still appear** —
silence is itself a signal at scale.

**Fleet API responses.** `GET /api/fleet/health?window=5m` →
`{window_sec, now_ns, allocation[], summary{nodes,crit,warn,ok,events,errors,cost_usd,total_tokens}, nodes[NodeHealth]}`
(rows sorted crit→warn→ok, then desc error_rate, then desc p95).
`GET /api/fleet/node/<host>` → `{host, health: NodeHealth, series: [Bucket]}`.
`GET /api/fleet/stream?interval=` → SSE `event: health` frames every `interval` s
(clamped 2–30). Window parser accepts `ms/s/m/h`, `0` = all-time.

---

<a name="5-doctor"></a>
## 5. PrismaDoctor diagnostics

**Pipeline:** `gather (I/O) → detect (Signal) → cluster (Incident) →
diagnose (Diagnosis) → remember (incident_memory.md)`, wrapped in a `Report`.
Detection always runs on demand (Doctor tab / API); the daemon
(`CHRONOLOG_DOCTOR=1`) scans on a timer (default 30 s, clamped 5–600).

### 5.1 gather (the only I/O)

`gather()` returns `(interactions, stitched_edges, recoveries)` — each a list of
dicts. Reads the Path-A **spool first** (GIL-safe) and falls back to
`path_a_reader` only if no spool; Path-B edges from the local hot store
`read_stitched`. Bounds: `max_sessions=400`, `max_scenarios=200`.

### 5.2 Signal (raw failure symptom)

`@dataclass Signal` (`diagnostics/detectors.py:27`). The atomic unit each detector
yields.

| Field | Type | Default | Why |
|-------|------|---------|-----|
| `kind` | str | required | Detector category (`http_error`/`rate_limit`/`interaction_error`/`edge_error`/`orphan_call`/`latency_outlier`/`slow_edge`/`retry_storm`/`recovery`). Half of the clustering signature; drives title + diagnosis. |
| `severity` | str | required | `info`\|`warning`\|`error`\|`critical`. Ranking, colour, escalation base. |
| `message` | str | required | Short human text, **numbers stripped** by `normalize_message`. Second half of the clustering signature. |
| `host` | str | `""` | Node → `Incident.hosts` (blast radius). |
| `session` | str | `""` | → `Incident.sessions`. |
| `scenario` | str | `""` | → `Incident.scenarios`. |
| `tool` | str | `""` | → `Incident.tools`. |
| `model` | str | `""` | → `Incident.models`. |
| `ts` | str | `""` | → incident `first_ts`/`last_ts`. |
| `latency_ms` | float | `0.0` | For latency/slow signals. |
| `evidence` | dict | `{}` | Per-kind raw pointers (below); kept in incident `samples` (cap 5) for drill-down. |

**`evidence` by detector:** interaction errors `{session_id, sequence_id,
status_code, raw}` (429 ⇒ `rate_limit`, 5xx ⇒ `critical`); latency outliers
`{…, latency_ms, model_median_ms}` (fires ≥ `max(1500, 3×median)`); retry
`{session_id, repeat_count, sequence_ids}` (**count kept out of `message`** so all
storms cluster); edge errors `{correlation_id, from/to host/session, status}`;
orphan calls `{correlation_id, from_host, to_host, to_session}` (start present,
done absent ⇒ always `critical`); slow edges `{…, latency_ms, tool_median_ms}`
(≥ `max(2000, 3×median)`); recovery `{session_id, blob_name, checkpoint_id, reason}`.

**`normalize_message`** — the clustering-key normalizer: lowercases, replaces
UUIDs/long-hex with `<id>`, digit runs with `<n>`, collapses whitespace,
truncates to 200 chars. E.g. `"peer ares-comp-12 timed out after 30001ms"` →
`"peer ares-comp-<n> timed out after <n>ms"`.

### 5.3 Incident (clustered failure)

`@dataclass Incident` (`diagnostics/incidents.py:25`).

| Field | Type | Default | Why |
|-------|------|---------|-----|
| `signature` | str | required | 12-char SHA1 of `f"{kind}\|{message}"`. **THE clustering key.** |
| `kind` | str | required | From first signal; matched by diagnoser rules. |
| `title` | str | required | Human title (`_title_for`). |
| `severity` | str | required | Signal severity, then escalated by count. |
| `count` | int | `0` | Collapsed-signal count (the "500 timeouts → 1" number). |
| `first_ts` / `last_ts` | str | `""` | Time span. |
| `hosts` | list[str] | `[]` | Deduped, cap 25. Blast radius. |
| `sessions` / `tools` / `models` / `scenarios` | list[str] | `[]` | Deduped, cap 25 each. |
| `samples` | list[dict] | `[]` | Up to 5 full `Signal.to_dict()` for drill-down + diagnosis text. |
| `diagnosis` | dict | `{}` | Filled by the diagnoser (§5.4). |

**Clustering key = `(kind, normalized_message)`** hashed to 12-char SHA1. Host /
tool / model / session / scenario are aggregated *inside* the incident, not part of
the key — so a fleet-wide bug stays one card while host distribution stays visible.
That is why 500 identical timeouts (numbers stripped to `<n>`) collapse to one
incident with `count=500`.

**Escalation:** `count ≥ 50` bumps severity 2 rungs, `≥ 10` bumps 1 rung (clamped
to `critical`). **Ranking:** `(severity_index, count, len(hosts))`, worst-first.

After clustering, the scan adds two keys to the dict: `recurring: bool` and
`prior_count: int` (§5.6).

### 5.4 Diagnosis (per incident)

Stable dict (`diagnostics/diagnoser.py`):

| Field | Type | Why |
|-------|------|-----|
| `root_cause` | str | WHY, from the matched rule or `_FALLBACK`. |
| `blast_radius` | str | `"{count} occurrence(s), {N} host(s), {N} agent(s), {N} scenario(s)"` (empty parts omitted). |
| `remediation` | str | HOW to fix, from the matched rule. |
| `confidence` | float | Rule confidence 0.0–1.0. |
| `source` | str | `heuristic` / `llm` / `heuristic-fallback`. |

`HeuristicDiagnoser` (default, zero-cost) walks a first-match-wins rule list keyed
on kind + sample text: orphan_call (0.70), rate-limit/429 (0.85), timeout (0.70),
transport/`ofi`/`mercury`/`protonosupport` (0.65), context/token-overflow (0.75),
retry_storm (0.70), latency/slow_edge (0.55), recovery (0.60), http 5xx (0.60),
fallback (0.30). `LLMDiagnoser` (opt-in `CHRONOLOG_DOCTOR_LLM=1`) asks Claude for
JSON `{root_cause, blast_radius, remediation, confidence}` (incident JSON truncated
to 6000 chars); any failure → heuristic fallback (`source="heuristic-fallback"`).

### 5.5 Report (scan envelope)

`@dataclass Report` (`diagnostics/scan.py:26`):

| Field | Type | Why |
|-------|------|-----|
| `generated_ns` | int | `time.time_ns()` stamp; SSE change-detection key. |
| `incidents` | list[dict] | Clustered incidents + `diagnosis` + `recurring` + `prior_count`. |
| `totals` | `{severity:int, …, incidents, events}` | Per-severity counts + incident count + Σ`count`. |
| `scanned` | `{interactions, edges, recoveries}` | Raw record counts. |
| `error` | str \| null | Scan never raises; failure message lands here. |

### 5.6 incident_memory.md (self-compacting cross-run memory)

**Path:** `{DTP_STATE_DIR or ~/.dt_provenance}/diagnostics/incident_memory.md`.

```
# ChronoLog Incident Memory

## Digest (compacted)                       ← present only if non-empty
- [<kind>] <title> — seen <N>x
…

## Recent
- <ts> [<sev>] <kind> ×<N> — <title> (<hosts>)
…
```

- **Recent line:** `- {%Y-%m-%dT%H:%MZ} [{severity}] {kind} ×{count} — {title} ({hosts})`
  (`hosts` = first 3 joined by `,` + `…` if more).
  E.g. `- 2026-06-22T10:30Z [critical] orphan_call ×7 — hung/dropped peer (comp-12)`.
- **Digest line:** `- [{kind}] {title} — seen {N}x`.
- **Compaction:** append one Recent line per incident; when Recent exceeds
  `compact_at=200`, keep the newest `keep_recent=60` and fold the rest into the
  Digest (summing counts per `(kind,title)` key). Deterministic default summariser.

**The `recurring ×N` badge** = `prior_count`, computed in `run_scan` against
`IncidentMemory.known_signatures()`: `key = f"[{kind}] {title}"`,
`prior = known.get(key) or known.get(title)`, then `recurring = bool(prior)` and
`prior_count = int(prior or 0)`. `known_signatures()` parses `seen (\d+)x` from
Digest lines *and* the `×N` from uncompacted Recent lines — so a repeat is
recognized on the very next scan, not only after compaction. This is the
cross-run learning that survives restarts.

### 5.7 Diagnostics API

`GET /diagnostics/incidents` → `Report.to_dict()` · `GET /diagnostics/status` →
`{running, generated_ns, totals}` · `POST /diagnostics/watch` (start/stop worker) ·
`POST /diagnostics/scan` (force, body `{diagnose, remember, max_sessions,
max_scenarios}`) · `GET /diagnostics/incidents/<sig>` (single, 404 `{error}`) ·
`GET /diagnostics/memory` (markdown, or `{markdown}` if `?format=json`) ·
`GET /diagnostics/stream` (SSE `event: report`, change-detected on `generated_ns`).

---

<a name="6-semantic"></a>
## 6. Semantic checks, checkpoints & rollback

### 6.1 SemanticCheck (a check definition)

`@dataclass SemanticCheck` (`semantic/config.py:32`). Config discovered at
`DTP_SEMANTIC_CHECKS` → `./semantic_checks.yaml` → `~/.dt_provenance/semantic_checks.yaml`.

| Field | Type | Default | Why |
|-------|------|---------|-----|
| `name` | str | required | Identifier; used in `CheckResult.check_name`. |
| `description` | str | required | Plain-English criterion → the `{criterion}` in the eval prompt. |
| `trigger` | str | `per_query` | `per_query` (after every response) or `on_strict_error` (only after a strict HTTP/stream error); `always` matches any. |
| `severity` | str | `error` | `error` (emits a recovery event, surfaced to the agent) or `warning` (recorded only). |
| `enabled` | bool | `True` | Gate. |

`SemanticChecksConfig`: `checks: [SemanticCheck]`, `model` (default
`claude-haiku-4-5-20251001`), `max_tokens` (default 512).

### 6.2 CheckResult / CheckBatch (outcomes)

`CheckResult` (`semantic/checker.py:22`): `check_name`, `passed` (bool),
`confidence` (0–1, 0.0 on evaluator failure), `explanation`, `severity`,
`error_introduced_at_sequence` (Optional[int] — where the LLM thinks the error was
first introduced; feeds rollback). The evaluator uses the Anthropic SDK **directly,
bypassing the proxy** so eval calls aren't recorded as agent interactions; on any
failure it defaults to `passed=True, confidence=0.0` so infra issues don't block.

`CheckBatch` (`checker.py:46`): `session_id`, `sequence_id`, `results:[CheckResult]`;
`to_dict()` adds `has_failures`, `error_count`, `warning_count`.

### 6.3 Checkpoint (restore-point record)

`@dataclass Checkpoint` (`checkpointing/checkpoint_manager.py:22`). **Separate store
— not ChronoLog.** Persisted to `$DTP_CHECKPOINT_STORE` or
`~/.dt_provenance/checkpoints.json` (single JSON object `{session_id:
[checkpoint,…]}`, atomic temp-file + rename). *(The `checkpoints` Chronicle
constant exists but no code writes to it — reserved.)*

| Field | Type | Default | Why |
|-------|------|---------|-----|
| `checkpoint_id` | str (uuid4) | required | Unique id → `recommended_checkpoint_id`. |
| `session_id` | str | required | Owning session. |
| `sequence_id` | int | required | The turn this restore-point sits at; "before error" filter + replay distance. |
| `name` | str | required | Label, e.g. `auto_end_turn@7`. |
| `checkpoint_type` | enum → str | required | `auto_end_turn`/`auto_tool_boundary`/`auto_delegation`/`manual`; base of the score. |
| `timestamp` | str ISO | required | Creation time. |
| `score` | float (0–20) | required | Quality/rollback score: type-base + token-component (fewer ⇒ higher, 0–3) + latency-component (faster ⇒ higher, 0–2). |
| `token_count` | int | `0` | Context size; lowers score as it grows. |
| `latency_ms` | float | `0.0` | Latency at checkpoint; lowers score. |
| `metadata` | dict | `{}` | Free-form. |

Auto-checkpoints are detected from the LLM **response body**: anthropic
`stop_reason` + `content[].type=="tool_use"`; openai/ollama `finish_reason` +
`tool_calls`. `end_turn`/`stop` (no tools) → `AUTO_END_TURN`; tool use →
`AUTO_TOOL_BOUNDARY`.

### 6.4 Rollback analysis

`CheckpointCandidate` (`semantic/rollback_analyzer.py:45`): `checkpoint`,
`fix_probability` (0–1), `replay_token_estimate`, `cost_effectiveness` (sort key),
`rationale`.
`RollbackAnalysis` (`:65`): `session_id`, `error_sequence`, `error_description`,
`candidates` (ranked desc), `analysis_summary`, `recommended_checkpoint_id`.
Cost-effectiveness: `CE = (fix_probability × quality_score) / (1 + log2(1 +
replay_tokens/10000))`. Attribution asks the LLM for JSON
`{primary_origin_sequence, primary_confidence, fix_probability_by_sequence,
rationale_by_sequence, analysis}`.

`RollbackPlan` (`checkpoint_manager.py:53`, in-memory only): `plan_id`,
`trigger_session_id`, `trigger_sequence_id`, `error_description`,
`failed_messages[]` (from interaction `request.body.messages`),
`sessions{session_id:{checkpoint, score}}`, `timestamp`.

### 6.5 Semantic API

`GET /api/semantic-checks/config` → `{config_path, model, max_tokens, checks:[…]}` ·
`POST /api/semantic-checks/reload` → `{reloaded, config_path, check_count}` ·
`POST /api/session/<sid>/semantic-checks/run` (body `{sequence_id, trigger}`) →
`CheckBatch.to_dict()` · `POST /api/session/<sid>/rollback/analyze` (body
`{error_sequence, error_description, avg_tokens_per_turn}`) → `RollbackAnalysis.to_dict()`.

---

<a name="7-index"></a>
## 7. The `__index__` chronicle

Solves the missing `ShowChronicles` Python binding: every scenario / session /
host ever seen appends a one-off event so listing operations have something to
`ReplayStory` (or CSV-read) against.

- Chronicle `__index__`, three stories: `scenarios`, `sessions`, `hosts`.
- **On-wire entry:** `index_add(story, value)` appends exactly `{"v": "<value>"}`.
- **Read:** `index_list(story)` reads **CSV-archive-first** (a live `ReplayStory`
  on a never-written index story blocks forever holding the GIL; live replay is
  opt-in via `CHRONOLOG_INDEX_LIVE=1`), pulls each event's `"v"`, keeps distinct
  strings in first-seen order.
- **Writers/readers:** `scenarios` written by the inter-agent store + collector;
  `sessions` written by the sync worker; `hosts` story is declared but has no
  in-tree writer/reader (reserved for symmetry).

---

<a name="8-envelope"></a>
## 8. ChronoLog storage envelope & CSV archive

On replay, every stored record is wrapped with keeper metadata that
`path_a_reader._strip_chronolog_meta` removes before views see it:

| Field | Type | Source |
|-------|------|--------|
| `_chronolog_time_ns` | int | Keeper-assigned event timestamp (ns); replay-ordering key. |
| `_chronolog_index` | int | Event index within the story. |
| `_chronolog_client` | int | Producer client id. |
| `_chronolog_archived` | bool | Present only when served from the CSV-archive fallback. |
| `_drained_from_keeper_ip` | str | Present on CSV-drained cluster events (comms/topology view). |

**CSV archive fallback:** grapher-drained keeper output at `$CHRONOLOG_OUTPUT_DIR`
(else `~/chronolog-install/chronolog/output`), file naming
`{chronicle}.{story}.{ip}.{port}.{startSec}.csv`, line format
`event : sid:time_ns:client:index:payload`. Used when a live `ReplayStory` returns
nothing for accepted data, and it is the **primary** path for `index_list`.

---

<a name="9-api"></a>
## 9. API read models (per dashboard tab)

Every view is a `Capability` served by an adapter; the shipped `ChronoLogAdapter`
(`name="chronolog"`, available when `backend.is_reachable()`) serves them all.
`/api/config` advertises `{adapters, capabilities, offline}`; the SPA shows only
active tabs. Capabilities: `CONVERSATIONS, SCENARIOS, INTERACTIONS, PROVENANCE,
SEMANTIC, INTER_AGENT, RECOVERY, CLUSTER, DIAGNOSTICS, FLEET, TRACING`.

The adapter exposes 7 generic Path-A read primitives (all delegating to
`path_a_reader`): `get_sessions`, `get_session_interactions`, `get_interaction`,
`get_context_graphs`, `get_context_graph`, `get_context_node`,
`get_recovery_events`.

### 9.1 Workspace / Conversations (`CONVERSATIONS`, Path-A)

`GET /api/conversations` → `ConversationSummary[]` (5 s TTL cache, sorted by
`lastTurn` desc): `conversationId`, `agentId` (bare role), `turnCount`,
`firstTurn`, `lastTurn`, `scenarioId` (plurality vote).

`GET /_interceptor/conversations/<id>` → `ConversationTurn[]` (chronological merge
of all sessions in the tree). Per turn: `id` (`<session>:<seq>`), `session_id`,
`scenario_id`, `turn_number`, `turn_type` (`initial`/`handoff`/`tool_result`/
`continuation`), `timestamp`, `provider`, `model`, `parent_interaction_id`,
`context_metrics{input_tokens, output_tokens, total_tokens, cost_usd}`,
`response_text_preview` (≤240 chars), `tool_calls[{id,name,input}]`,
`total_latency_ms`, `status_code`, `error`, plus flat mirrors `input_tokens`,
`output_tokens`, `total_tokens`, `total_cost_usd` (backward compat).

`GET /api/conversations/<id>/agent-graph` → `{conversation_id, nodes[], edges[]}`.
Node: `session_id`, `agent_role` (`orchestrator`/`subagent`), `interaction_count`,
`total_tokens`, `total_cost_usd`, `host`, `scenario_id`. Edge (per handoff):
`kind:"handoff"`, `from_session_id`, `to_session_id`, `interaction_id`,
`turn_number`, `latency_ms`.

`GET /api/interactions/<path:id>` → full `Interaction` detail: `id`, `session_id`,
`timestamp`, `method`, `path`, `request_headers`, `request_body`, `raw_request_body`,
`provider`, `model`, `system_prompt`, `messages`, `tools`, `image_metadata`(null),
`status_code`, `response_headers`, `response_body`, `raw_response_body`,
`is_streaming`, `stream_chunks`([]), `response_text`, `tool_calls[{id,name,input}]`,
`token_usage{input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
total_tokens}`, `cost_estimate`(null), `cost_usd`, `time_to_first_token_ms`(null),
`total_latency_ms`, `error`.

### 9.2 Interactions feed (`INTERACTIONS`, Path-A)

`GET /_interceptor/interactions` (params `limit` 1–500, `provider`, `model`,
`session_id`; 1 s TTL) → `InteractionSummary[]`: `id`, `session_id`, `timestamp`,
`provider`, `model`, `method`, `path`, `status_code`, `is_streaming`,
`total_latency_ms`, `response_text_preview` (≤200 chars).
`GET /_interceptor/live` → SSE `event: interaction` (polls the cached feed every
2 s, keepalive after 15 s idle, dedup set capped 50 000).

### 9.3 Scenarios (`SCENARIOS`, Path-B edges + Path-A metrics)

`GET /api/scenarios` (+ `/<id>`) → `ScenarioSummary`: `scenario_id`, `agent_count`,
`agents[]`, `hosts[]`, `event_count`, `firstEvent`, `lastEvent`.

`GET /api/scenarios/<id>/graph` → `{scenario_id, agents[], edges[]}`. Agent node:
`agent_id`, `session_id`, `agent_role:"peer"`, `host` (majority vote),
`interaction_count`, `total_tokens`, `total_cost_usd`, `sub_sessions[]`,
`scoped_session_id` (**the real key the frontend must use to fetch the agent's
conversation** — clicking a node joins on this). Edge (per stitched call):
`kind:"inter_agent_msg"`, `event_id`, `correlation_id`, `from_session_id`,
`from_sub_session_id`, `to_session_id`, `to_sub_session_id`, `from_host`, `to_host`,
`ts_start`, `ts_done`, `tool_name`, `status`, `latency_ms`, `payload_preview`.

`GET /api/scenarios/<id>/timeline` → `{scenario_id, agents[], events[]}`; each event
has a `type`:
- `agent_msg`: `ts_start, ts_done, latency_ms, agent, host, from, from_host, to, to_host, tool_name, status, payload_preview, correlation_id`.
- `llm_call`: `agent, host, ts_start, ts_done, latency_ms, model, provider, tokens_in, tokens_out, cost_usd, status_code, prompt, text, sequence_id`.
- `tool_call`: base + `tool_name, tool_args, tool_status, tool_error`.

### 9.4 Memory & Provenance (`PROVENANCE`, Path-A spool direct)

`GET /api/memory/sessions` → `{sessions[]}` per agent: `session_id`, `role`,
`scenario`, `context_nodes`, `live_nodes` (adds − evicts), `interactions`,
`total_tokens`, `last_ts`.
`GET /api/memory/<sid>/context` → `{session_id, nodes[], interaction_count,
total_tokens, live_tokens}`; node: `sequence_id`, `op`, `node`, `summary`, `tokens`,
`running_tokens` (clamped ≥0), `timestamp`.
Plus raw: `GET /api/sessions`, `/api/session/<sid>/interactions`,
`/api/session/<sid>/graph[?since=]`, `/graph/<seq>`, `/stream` (SSE), `/analysis`,
`/call-graph[?scope=workflow]`, `/tool-sequence`.

### 9.5 Analysis, Call graph & Tool sequence (Path-A)

`compute_analysis` → 8 chart blocks: `composition`, `token_deltas`,
`context_growth`, `latency`, `token_usage`, `cumulative_cost`, `model_distribution`,
`latency_histogram` (fields per block detailed in `analysis/context_graph.py`).
`compute_call_graph` → `{nodes[], edges[], timeline[]}`: node `{id, type, label,
metrics{callCount, errorRate, avgLatencyMs, p95LatencyMs, totalTokens,
totalCostUsd}}`; fixed topology agent→proxy→provider→model→tool. Tool sequence
steps carry `toolCalls`, `toolResults`, `responseText`, `systemPromptPreview`,
`inputTokens`, `outputTokens`, per interaction.

### 9.6 Traces (`TRACING`, Path-B)

`GET /api/tracing/scenarios` → `{scenarios[]}`.
`GET /api/tracing/<scenario>/traces?limit=` → `{scenario_id, trace_count, traces[]}`.
Trace: `root_id`, `scenario_id`, `span_count`, `start_ns`, `end_ns`, `wall_ms`,
`error_count`, `spans[]`, `critical_path`. Span: `correlation_id`, `parent_id`
(explicit `parent_correlation_id` else inferred by session+time nesting),
`from_host`, `from_session`, `to_host`, `to_session`, `tool_name`, `status`,
`is_error`, `latency_ms`, `start_offset_ms`, `depth`, `children[]`.
`GET /api/tracing/<scenario>/critical-path` → `{total_latency_ms, hops, chain[],
bottleneck, root_id, wall_ms, scenario_id}`; each chain span adds `self_time_ms`
(exclusive time = latency − slowest child's latency); `bottleneck` = max-self-time
hop.

### 9.7 Cluster (`CLUSTER`, grapher CSVs + Path-B + SLURM)

`GET /api/chronolog/topology` → `{via, connected, output_dir, scenarios[], now_ns,
drain_window_sec:180, allocation[], components{visor, grapher, player}, keepers[],
chronicles[]}`. Keeper: `ip`, `host`, `ports[]`, `event_count`, `last_event_ns`,
`stories[{chronicle, story, event_count}]`, `age_sec`, `in_allocation`, `stale`.
`GET /api/chronolog/comms[?scenario=]` → `{allocation[], hosts[], edges[],
scenarios[], scenario_filter, now_ns}`; host `{host, out, in, errors, agents,
in_allocation}`; edge `{from_host, to_host, count, errors, last_ts, tools[],
scenarios[]}`.
`GET /api/chronolog/events?chronicle=&story=[&replay=1]` → `{chronicle, story, via,
events[], count}`.
`GET /api/cluster/capacity` → live `sinfo`: `{available, total, idle_count,
busy_count, down_count, idle[], busy[], down[], partitions[], nodes{name:{status,
partitions[]}}}`.

---

<a name="10-reference"></a>
## 10. Reference tables

### 10.1 Record → storage → dedup key

| Record | Path | Spool file / store | Chronicle | Story | Dedup key |
|--------|------|--------------------|-----------|-------|-----------|
| Interaction | A | `<sid>__interactions.jsonl` | `llm_interactions` | `<session_id>` | `sequence_id` |
| Context node | A | `<sid>__context_graph.jsonl` | `context_graphs` | `<session_id>` | `sequence_id` |
| Recovery | A | `<sid>__recovery.jsonl` | `recovery_events` | `<session_id>` | `blob_name` |
| Inter-agent edge | B | `inter_agent/<scenario>.jsonl` | `ia_<scenario>` | `edges` | `event_id` (SSE) / `correlation_id` (stitch) |
| Fleet bucket | rollup | `fleet/rollup.jsonl` | `metrics_rollup` | `minute` | `(host, minute)` last-wins |
| Session index | — | (worker) | `__index__` | `sessions` | `v` distinct |
| Scenario index | — | (store/collector) | `__index__` | `scenarios` | `v` distinct |
| Checkpoint | A | `checkpoints.json` | *reserved (unused)* | — | `(session, sequence_id, type)` |
| Incident memory | doctor | `diagnostics/incident_memory.md` | — | — | `(kind, title)` |

### 10.2 On-disk state layout (`$DTP_STATE_DIR` else `~/.dt_provenance/`)

```
path_a_spool/    <safe_session>__{interactions|context_graph|recovery}.jsonl   (Path-A spool)
inter_agent/     <scenario>.jsonl                                              (Path-B hot store, offline)
fleet/           rollup.jsonl                                                  (fleet buckets, offline)
diagnostics/     incident_memory.md                                           (PrismaDoctor memory)
demo_llm/        sessions.jsonl, interactions/<sid>.jsonl, context_graphs/<sid>.jsonl  (offline demo store)
checkpoints.json  (or $DTP_CHECKPOINT_STORE)                                   (restore points)
```
CSV archive (grapher drain): `$CHRONOLOG_OUTPUT_DIR` else
`~/chronolog-install/chronolog/output` — `{chronicle}.{story}.{ip}.{port}.{startSec}.csv`.

### 10.3 Environment variables

| Var | Effect |
|-----|--------|
| `CHRONOLOG_VISOR_IP` | Visor endpoint — **raw IP, not the `-40g` hostname** (Mercury rejects hostnames with `HG_PROTONOSUPPORT`). |
| `CHRONOLOG_VISOR_PROTOCOL` | Default `ofi+sockets`. |
| `CHRONOLOG_OFFLINE=1` | Replay from drained CSVs / local stores; disables live capture/doctor workers. |
| `OBSERVE_HOST` / `OBSERVE_PORT` | Flask bind (default `0.0.0.0:5000`). |
| `CHRONOLOG_CAPTURE=1` (`DTP_CHRONOLOG_CAPTURE`) | Auto-start the Path-A sync worker as a daemon thread. |
| `CHRONOLOG_CAPTURE_INTERVAL` | Sync-worker drain interval (s, default 5). |
| `CHRONOLOG_DOCTOR=1` | Auto-start the PrismaDoctor background scanner. |
| `CHRONOLOG_DOCTOR_INTERVAL` | Doctor scan interval (s, default 30, clamped 5–600). |
| `CHRONOLOG_DOCTOR_LLM=1` | Use the Claude-backed diagnoser (falls back to heuristic). |
| `CHRONOLOG_FLEET_ROLLUP=1` | Auto-start the fleet rollup emitter. |
| `CHRONOLOG_INDEX_LIVE=1` | Allow live `ReplayStory` on `__index__` (off by default — it blocks). |
| `DTP_STATE_DIR` | Root for all local spools/stores (default `~/.dt_provenance`). |
| `DTP_CHECKPOINT_STORE` | Checkpoint JSON path. |
| `DTP_SEMANTIC_CHECKS` | Semantic-checks YAML path. |
| `CHRONOLOG_OUTPUT_DIR` | Grapher-drained CSV archive dir. |
| `AGENTS_PER_NODE` | Real-agent harness: agents launched per node. |

### 10.4 Session-id convention (load-bearing)

Path-A turns and Path-B edges **must** use the scenario-scoped id `role@scenario`
as the session key. The scenario graph keys each agent node by this
(`scoped_session_id`), and the frontend fetches a clicked node's conversation by
that exact key. Emitting a bare `role` on an edge orphans the node from its LLM
turns (zero tokens, "No interactions" after a click). The `@` is preserved through
spool filenames and ChronoLog Story names so the id round-trips end to end.

---

*Generated from the `chronolog-observability` source. Re-verify any section against
the cited `file:line` — the code is the authority.*
