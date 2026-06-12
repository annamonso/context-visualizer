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
