import type { AgentGraph, ClearScope, ConversationSummary, ConversationTurn, Interaction, InteractionSummary, ScenarioGraph, ScenarioSummary, SessionGraph, SessionInfo, ToolCallStep } from "./types";

interface ListParams {
  limit?: number;
  offset?: number;
  provider?: string;
  model?: string;
  session_id?: string;
}

export interface ServerConfig {
  adapters: string[];
  /** capability name -> list of adapters providing it */
  capabilities: Record<string, string[]>;
  offline: boolean;
}

/**
 * Fetch the active server configuration: which data-source adapters are live
 * and which capabilities they provide. The SPA gates its tabs on this so a bare
 * ChronoLog deployment only shows views it can actually serve. Degrades to "all
 * generic capabilities present" if the endpoint is unreachable.
 */
export async function getConfig(): Promise<ServerConfig> {
  const res = await fetch("/api/config");
  if (!res.ok) throw new Error(`Failed to get config: ${res.status}`);
  return res.json();
}

export async function listInteractions(
  params: ListParams = {}
): Promise<InteractionSummary[]> {
  const query = new URLSearchParams();
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  if (params.provider) query.set("provider", params.provider);
  if (params.model) query.set("model", params.model);
  if (params.session_id) query.set("session_id", params.session_id);
  const qs = query.toString();
  const url = `/_interceptor/interactions${qs ? `?${qs}` : ""}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to list interactions: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? (data as InteractionSummary[]) : [];
}

/**
 * SSE stream of new interactions (``/_interceptor/live``). When the stream
 * drops, ``useLiveInteractions`` falls back to polling on its own and retries
 * the stream later.
 */
export function openInteractionStream(
  onEvent: (row: InteractionSummary) => void,
  onError: () => void,
): EventSource {
  const source = new EventSource("/_interceptor/live");
  source.addEventListener("interaction", (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data) as InteractionSummary;
      onEvent(data);
    } catch {
      // Bad frame — the polling fallback will eventually resync.
    }
  });
  source.addEventListener("error", () => {
    onError();
  });
  return source;
}

export async function getInteraction(id: string): Promise<Interaction> {
  const res = await fetch(`/api/interactions/${id}`);
  if (!res.ok) throw new Error(`Failed to get interaction ${id}: ${res.status}`);
  return res.json();
}

export function downloadUrl(id: string): string {
  return `/api/interactions/${id}/download`;
}

export async function getSessions(): Promise<SessionInfo[]> {
  const res = await fetch("/api/sessions");
  if (!res.ok) throw new Error(`Failed to list sessions: ${res.status}`);
  return res.json();
}

export async function getSessionGraph(sessionId: string): Promise<SessionGraph> {
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/graph`);
  if (!res.ok) throw new Error(`Failed to get session graph: ${res.status}`);
  return res.json();
}

export async function getSessionToolSequence(sessionId: string): Promise<ToolCallStep[]> {
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/tool-sequence`);
  if (!res.ok) throw new Error(`Failed to get tool sequence: ${res.status}`);
  return res.json();
}

export async function getConversations(): Promise<ConversationSummary[]> {
  const res = await fetch("/api/conversations");
  if (!res.ok) throw new Error(`Failed to list conversations: ${res.status}`);
  const data: unknown = await res.json();
  return Array.isArray(data) ? data : [];
}

export async function getConversationTurns(conversationId: string): Promise<ConversationTurn[]> {
  const res = await fetch(`/_interceptor/conversations/${encodeURIComponent(conversationId)}`);
  if (!res.ok) throw new Error(`Failed to get conversation turns: ${res.status}`);
  const data: unknown = await res.json();
  return Array.isArray(data) ? (data as ConversationTurn[]) : [];
}

export async function getAgentGraph(conversationId: string): Promise<AgentGraph> {
  const res = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/agent-graph`);
  if (!res.ok) throw new Error(`Failed to get agent graph: ${res.status}`);
  const data = await res.json() as Partial<AgentGraph>;
  return {
    conversation_id: data.conversation_id ?? conversationId,
    nodes: Array.isArray(data.nodes) ? data.nodes : [],
    edges: Array.isArray(data.edges) ? data.edges : [],
  };
}

export async function getScenarios(): Promise<ScenarioSummary[]> {
  const res = await fetch("/api/scenarios");
  if (!res.ok) throw new Error(`Failed to list scenarios: ${res.status}`);
  const data: unknown = await res.json();
  return Array.isArray(data) ? (data as ScenarioSummary[]) : [];
}

export async function getScenarioGraph(scenarioId: string): Promise<ScenarioGraph> {
  const res = await fetch(`/api/scenarios/${encodeURIComponent(scenarioId)}/graph`);
  if (!res.ok) throw new Error(`Failed to get scenario graph: ${res.status}`);
  const data = await res.json() as Partial<ScenarioGraph>;
  return {
    scenario_id: data.scenario_id ?? scenarioId,
    agents: Array.isArray(data.agents) ? data.agents : [],
    edges: Array.isArray(data.edges) ? data.edges : [],
  };
}

// ── Live inter-agent edges (hot-store backlog + SSE) ───────────────────────
//
// The scenario graph above (/graph) reconstructs per-agent LLM subtrees from
// ChronoLog, which is unreadable for ~180s on an in-flight scenario (and a
// ReplayStory mid-drain blocks the dashboard). For the *live* view we instead
// read the inter-agent edges directly: the backlog from the dashboard's hot
// store (instant, GIL-safe) and live updates from the in-process SSE bus.

/** One merged call (start+done stitched on correlation_id). */
export interface InterAgentStitched {
  correlation_id: string;
  scenario_id: string;
  from_host: string;
  from_session: string;
  to_host: string;
  to_session: string;
  kind: string;
  tool_name: string;
  payload_digest: string;
  payload_preview: string;
  ts_start: string | null;
  ts_done: string | null;
  status: string;
  latency_ms: number;
}

/** One raw SSE frame — a single start or done phase. */
export interface InterAgentEvent {
  event_id: string;
  scenario_id: string;
  correlation_id: string;
  phase: "start" | "done";
  from_host: string;
  from_session: string;
  to_host: string;
  to_session: string;
  ts: string;
  kind: string;
  tool_name: string;
  payload_digest: string;
  payload_preview: string;
  status: string;
  latency_ms: number;
  ingest_ns: number;
}

/** Stitched backlog for a scenario — instant (served from the hot store). */
export async function getInterAgentEdges(scenarioId: string): Promise<InterAgentStitched[]> {
  const res = await fetch(`/api/scenarios/${encodeURIComponent(scenarioId)}/inter-agent`);
  if (!res.ok) throw new Error(`Failed to get inter-agent edges: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data?.events) ? (data.events as InterAgentStitched[]) : [];
}

/**
 * Live SSE feed of inter-agent events for one scenario. Emits ``message``
 * frames as calls start/complete; the EventSource auto-reconnects on drop.
 */
export function openInterAgentStream(
  scenarioId: string,
  onEvent: (ev: InterAgentEvent) => void,
  onError: () => void,
): EventSource {
  const source = new EventSource(
    `/api/_inter-agent/stream?scenario=${encodeURIComponent(scenarioId)}`,
  );
  const handle = (ev: Event) => {
    try {
      onEvent(JSON.parse((ev as MessageEvent).data) as InterAgentEvent);
    } catch {
      // Bad frame — backlog refetch / next event will resync.
    }
  };
  source.addEventListener("message", handle); // live events
  source.addEventListener("backlog", handle); // stored backlog (if ?backlog=1)
  source.addEventListener("error", () => onError());
  return source;
}

// ── Cluster tab: ChronoLog deployment topology ────────────────────────────

export interface ClusterKeeperStory {
  chronicle: string;
  story: string;
  event_count: number;
}

export interface ClusterKeeper {
  ip: string;
  host: string | null;
  ports: number[];
  event_count: number;
  last_event_ns: number;
  age_sec: number | null;
  in_allocation: boolean;
  stale: boolean;
  stories: ClusterKeeperStory[];
}

export interface ClusterChronicle {
  chronicle: string;
  story: string;
  event_count: number;
  keeper_ips: string[];
}

export interface ClusterComponents {
  visor: { ip: string | null; host: string | null };
  grapher: { host: string | null; by_convention: boolean };
  player: { host: string | null; by_convention: boolean };
}

export interface ClusterTopology {
  via: string;
  connected: boolean;
  output_dir: string;
  scenarios: string[];
  now_ns: number;
  drain_window_sec: number;
  allocation: string[];
  components: ClusterComponents;
  stale_cutoff_sec: number;
  hidden_stale_keepers: number;
  keepers: ClusterKeeper[];
  chronicles: ClusterChronicle[];
}

export async function getClusterTopology(includeStale = false): Promise<ClusterTopology> {
  const res = await fetch(`/api/chronolog/topology${includeStale ? "?all=1" : ""}`);
  if (!res.ok) throw new Error(`Failed to get cluster topology: ${res.status}`);
  return res.json();
}

// ── Cluster capacity (SLURM): total / idle / busy / down ───────────────────

export interface ClusterCapacity {
  available: boolean;
  reason?: string;
  total?: number;
  idle_count?: number;
  busy_count?: number;
  down_count?: number;
  idle?: string[];
  busy?: string[];
  down?: string[];
  partitions?: string[];
  /** Full per-node status map: name -> { status: idle|busy|down, partitions }. */
  nodes?: Record<string, { status: "idle" | "busy" | "down"; partitions: string[] }>;
}

export async function getClusterCapacity(): Promise<ClusterCapacity> {
  const res = await fetch("/api/cluster/capacity");
  if (!res.ok) throw new Error(`Failed to get cluster capacity: ${res.status}`);
  return res.json();
}

// ── Cluster tab: live node-to-node communication ──────────────────────────

export interface ClusterCommsHost {
  host: string;
  out: number;
  in: number;
  errors: number;
  agents: number;
  in_allocation: boolean;
}

export interface ClusterCommsEdge {
  from_host: string;
  to_host: string;
  count: number;
  errors: number;
  last_ts: string;
  tools: string[];
  scenarios: string[];
}

export interface ClusterComms {
  allocation: string[];
  hosts: ClusterCommsHost[];
  edges: ClusterCommsEdge[];
  scenarios: string[];
  scenario_filter: string | null;
  now_ns: number;
}

/**
 * Node-to-node inter-agent comms, aggregated from the dashboard's hot store
 * (instant, GIL-safe — safe to poll on a tick). Optionally narrowed to one
 * scenario.
 */
export async function getClusterComms(scenario?: string): Promise<ClusterComms> {
  const qs = scenario ? `?scenario=${encodeURIComponent(scenario)}` : "";
  const res = await fetch(`/api/chronolog/comms${qs}`);
  if (!res.ok) throw new Error(`Failed to get cluster comms: ${res.status}`);
  return res.json();
}

export interface ChronologEvents {
  chronicle: string;
  story: string;
  via: string;
  count: number;
  events: Record<string, unknown>[];
}

export async function getChronologEvents(
  chronicle: string,
  story: string,
): Promise<ChronologEvents> {
  const qs = new URLSearchParams({ chronicle, story });
  const res = await fetch(`/api/chronolog/events?${qs}`);
  if (!res.ok) throw new Error(`Failed to get story events: ${res.status}`);
  return res.json();
}

// ── Memory tab: live agent context, served from the capture spool ──────────

export interface MemorySession {
  session_id: string;
  role: string;
  scenario: string;
  context_nodes: number;
  live_nodes: number;
  interactions: number;
  total_tokens: number;
  last_ts: string;
}

export interface MemoryContextNode {
  sequence_id: number | null;
  op: string;
  node: string;
  summary: string;
  tokens: number;
  running_tokens: number;
  timestamp: string;
}

export interface MemoryContext {
  session_id: string;
  nodes: MemoryContextNode[];
  interaction_count: number;
  total_tokens: number;
  live_tokens: number;
}

export async function getMemorySessions(): Promise<MemorySession[]> {
  const res = await fetch("/api/memory/sessions");
  if (!res.ok) throw new Error(`Failed to get memory sessions: ${res.status}`);
  const d = await res.json();
  return Array.isArray(d?.sessions) ? (d.sessions as MemorySession[]) : [];
}

export async function getMemoryContext(sessionId: string): Promise<MemoryContext> {
  const res = await fetch(`/api/memory/${encodeURIComponent(sessionId)}/context`);
  if (!res.ok) throw new Error(`Failed to get memory context: ${res.status}`);
  return res.json();
}

/**
 * Fire a synthetic inter-agent burst on the server (NO LLM cost) — backs the
 * "Generate traffic" button. Edges then stream into the Live views.
 */
export interface DemoBurstOpts {
  scenario?: string;
  agents?: number;
  nodes?: number; // spread the agents across this many synthetic hosts
  rounds?: number;
  interval?: number;
  pattern?: string; // mesh | star | pipeline | ring
  errRate?: number; // 0..1
}

export async function triggerDemoBurst(
  opts: DemoBurstOpts = {},
): Promise<{ started: boolean; scenario: string }> {
  const { errRate, ...rest } = opts;
  const body: Record<string, unknown> = { ...rest };
  if (errRate != null) body.err_rate = errRate;
  const res = await fetch("/api/_demo/burst", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to start demo burst: ${res.status}`);
  return res.json();
}

export async function clearInteractions(
  scope: ClearScope,
  sessionId?: string
): Promise<{ deleted: number }> {
  const res = await fetch("/api/interactions/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, sessionId }),
  });
  if (!res.ok) throw new Error(`Clear failed: ${res.status}`);
  return res.json();
}

// ── Doctor tab: ChronoDoctor error detection & diagnosis ───────────────────

export interface IncidentDiagnosis {
  root_cause: string;
  blast_radius: string;
  remediation: string;
  confidence: number;
  source: string;
}

export interface Incident {
  signature: string;
  kind: string;
  title: string;
  severity: "info" | "warning" | "error" | "critical";
  count: number;
  first_ts: string;
  last_ts: string;
  hosts: string[];
  sessions: string[];
  tools: string[];
  models: string[];
  scenarios: string[];
  samples: Record<string, unknown>[];
  diagnosis: IncidentDiagnosis;
  recurring?: boolean;
  prior_count?: number;
}

export interface DoctorReport {
  generated_ns: number;
  incidents: Incident[];
  totals: Record<string, number>;
  scanned: Record<string, number>;
  error: string | null;
}

export async function getIncidents(): Promise<DoctorReport> {
  const res = await fetch("/api/diagnostics/incidents");
  if (!res.ok) throw new Error(`Failed to get incidents: ${res.status}`);
  return res.json();
}

export async function runDoctorScan(): Promise<DoctorReport> {
  const res = await fetch("/api/diagnostics/scan", { method: "POST" });
  if (!res.ok) throw new Error(`Doctor scan failed: ${res.status}`);
  return res.json();
}

export interface DoctorStatus {
  running: boolean;
  generated_ns: number;
  totals: Record<string, number>;
}

export async function getDoctorStatus(): Promise<DoctorStatus> {
  const res = await fetch("/api/diagnostics/status");
  if (!res.ok) throw new Error(`Failed to get doctor status: ${res.status}`);
  return res.json();
}

/** Activate / deactivate ChronoDoctor's live background scanner. */
export async function setDoctorWatch(enabled: boolean): Promise<{ running: boolean }> {
  const res = await fetch("/api/diagnostics/watch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`Failed to set doctor watch: ${res.status}`);
  return res.json();
}

export async function getIncidentMemory(): Promise<string> {
  const res = await fetch("/api/diagnostics/memory");
  if (!res.ok) throw new Error(`Failed to get incident memory: ${res.status}`);
  return res.text();
}

/** SSE: the latest Doctor report whenever it changes. */
export function openDoctorStream(
  onReport: (rep: DoctorReport) => void,
  onError: () => void,
): EventSource {
  const source = new EventSource("/api/diagnostics/stream");
  source.addEventListener("report", (ev) => {
    try {
      onReport(JSON.parse((ev as MessageEvent).data) as DoctorReport);
    } catch {
      /* next frame resyncs */
    }
  });
  source.addEventListener("error", () => onError());
  return source;
}

// ── Fleet tab: per-node health rollups (scales to 100+ nodes) ──────────────

export interface NodeHealth {
  host: string;
  events: number;
  errors: number;
  error_rate: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  agents: number;
  last_minute: number;
  in_allocation: boolean;
  status: "ok" | "warn" | "crit";
}

export interface FleetHealth {
  window_sec: number;
  now_ns: number;
  allocation: string[];
  summary: Record<string, number>;
  nodes: NodeHealth[];
}

export async function getFleetHealth(window?: string): Promise<FleetHealth> {
  const qs = window ? `?window=${encodeURIComponent(window)}` : "";
  const res = await fetch(`/api/fleet/health${qs}`);
  if (!res.ok) throw new Error(`Failed to get fleet health: ${res.status}`);
  return res.json();
}

export interface FleetBucket {
  host: string;
  minute: number;
  events: number;
  errors: number;
  latency_p95: number;
  cost_usd: number;
  agents: number;
}

export interface FleetNodeDetail {
  host: string;
  health: NodeHealth;
  series: FleetBucket[];
}

export async function getFleetNode(host: string, window?: string): Promise<FleetNodeDetail> {
  const qs = window ? `?window=${encodeURIComponent(window)}` : "";
  const res = await fetch(`/api/fleet/node/${encodeURIComponent(host)}${qs}`);
  if (!res.ok) throw new Error(`Failed to get fleet node: ${res.status}`);
  return res.json();
}

// ── Traces tab: distributed critical-path tracing ──────────────────────────

export interface TraceSpan {
  correlation_id: string;
  parent_id: string | null;
  from_host: string;
  from_session: string;
  to_host: string;
  to_session: string;
  tool_name: string;
  status: string;
  is_error: boolean;
  latency_ms: number;
  start_offset_ms: number;
  depth: number;
  children: string[];
  /** Exclusive time (set on critical-path chain spans). */
  self_time_ms?: number;
}

export interface CriticalPath {
  total_latency_ms: number;
  hops: number;
  chain: TraceSpan[];
  bottleneck: TraceSpan | null;
  root_id?: string;
  wall_ms?: number;
  scenario_id?: string;
}

export interface Trace {
  root_id: string;
  scenario_id: string;
  span_count: number;
  start_ns: number;
  end_ns: number;
  wall_ms: number;
  error_count: number;
  spans: TraceSpan[];
  critical_path: CriticalPath;
}

export async function getTraceScenarios(): Promise<string[]> {
  const res = await fetch("/api/tracing/scenarios");
  if (!res.ok) throw new Error(`Failed to get trace scenarios: ${res.status}`);
  const d = await res.json();
  return Array.isArray(d?.scenarios) ? d.scenarios : [];
}

export async function getTraces(scenario: string): Promise<Trace[]> {
  const res = await fetch(`/api/tracing/${encodeURIComponent(scenario)}/traces`);
  if (!res.ok) throw new Error(`Failed to get traces: ${res.status}`);
  const d = await res.json();
  return Array.isArray(d?.traces) ? (d.traces as Trace[]) : [];
}

export async function getCriticalPath(scenario: string): Promise<CriticalPath> {
  const res = await fetch(`/api/tracing/${encodeURIComponent(scenario)}/critical-path`);
  if (!res.ok) throw new Error(`Failed to get critical path: ${res.status}`);
  return res.json();
}
