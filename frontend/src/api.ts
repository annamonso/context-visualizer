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
