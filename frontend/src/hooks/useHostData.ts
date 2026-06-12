import { useEffect, useMemo, useState } from "react";
import { getConversationTurns, getScenarioGraph } from "../api";
import type { AgentGraph, AgentNode, ConversationTurn, ScenarioGraph } from "../types";
import type { ConversationData, NormalizedTurn } from "./useConversationData";

/** Bound the per-host fan-out: one turns fetch per agent. */
const MAX_AGENTS = 16;

const EMPTY: ConversationData = {
  conversationId: "",
  graph: null,
  turns: [],
  lanes: [],
  roleBySession: new Map(),
  nodeBySession: new Map(),
  totals: { agents: 0, handoffs: 0, calls: 0, tokens: 0, costUsd: 0 },
  loading: false,
  error: null,
};

/**
 * Host-scoped Workspace data: every agent on one node of a scenario, their
 * turns merged into a single chronological stream. Returns the exact
 * ``ConversationData`` shape ``useConversationData`` produces, so the agent
 * graph, timeline, playhead, and detail panel work on it unchanged — each
 * agent simply becomes a lane.
 */
export function useHostData(
  host: string | null,
  scenarioId: string | null,
): ConversationData & { agentCount: number; truncated: number } {
  const [graph, setGraph] = useState<ScenarioGraph | null>(null);
  const [turnsByAgent, setTurnsByAgent] = useState<Map<string, ConversationTurn[]>>(new Map());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!host || !scenarioId) {
      setGraph(null);
      setTurnsByAgent(new Map());
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getScenarioGraph(scenarioId)
      .then(async (g) => {
        if (cancelled) return;
        setGraph(g);
        const agents = g.agents
          .filter((a) => (a.host || "unknown") === host)
          .slice(0, MAX_AGENTS);
        const per = await Promise.all(
          agents.map((a) =>
            // Fetch by the scoped key (real session id); lane by display id.
            getConversationTurns(a.scoped_session_id || a.agent_id)
              .then((t) => [a.agent_id, t] as const)
              .catch(() => [a.agent_id, [] as ConversationTurn[]] as const),
          ),
        );
        if (cancelled) return;
        setTurnsByAgent(new Map(per));
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [host, scenarioId]);

  return useMemo(() => {
    if (!host || !scenarioId) return { ...EMPTY, agentCount: 0, truncated: 0 };

    const hostAgents = (graph?.agents ?? []).filter(
      (a) => (a.host || "unknown") === host,
    );
    const truncated = Math.max(0, hostAgents.length - MAX_AGENTS);

    const roleBySession = new Map<string, string>();
    for (const a of hostAgents) roleBySession.set(a.agent_id, "peer");

    // Synthetic AgentGraph: the node's agents plus their intra-host calls,
    // so the Workspace flow-graph panel renders in host mode unchanged.
    const agentIds = new Set(hostAgents.map((a) => a.agent_id));
    const graphNodes: AgentNode[] = hostAgents.map((a) => ({
      session_id: a.agent_id,
      agent_role: "peer",
      interaction_count: a.interaction_count,
      total_tokens: a.total_tokens,
      total_cost_usd: a.total_cost_usd,
      host,
      scenario_id: scenarioId,
    }));
    const nodeBySession = new Map(graphNodes.map((n) => [n.session_id, n]));
    const hostGraph: AgentGraph = {
      conversation_id: `${host}@${scenarioId}`,
      nodes: graphNodes,
      edges: (graph?.edges ?? [])
        .filter(
          (e) => agentIds.has(e.from_session_id) && agentIds.has(e.to_session_id),
        )
        .map((e, i) => ({
          from_session_id: e.from_session_id,
          to_session_id: e.to_session_id,
          interaction_id: e.correlation_id,
          turn_number: i,
          latency_ms: e.latency_ms ?? null,
          kind: "inter_agent_msg" as const,
        })),
    };

    interface Pending { agentId: string; src: ConversationTurn; parsed: number | null }
    const pending: Pending[] = [];
    for (const [agentId, turns] of turnsByAgent) {
      for (const t of turns) {
        const p = new Date(t.timestamp).getTime();
        pending.push({ agentId, src: t, parsed: Number.isFinite(p) ? p : null });
      }
    }
    pending.sort((a, b) => {
      if (a.parsed != null && b.parsed != null) return a.parsed - b.parsed;
      if (a.parsed != null) return -1;
      if (b.parsed != null) return 1;
      return (a.src.turn_number ?? 0) - (b.src.turn_number ?? 0);
    });
    const firstReal = pending.find((p) => p.parsed != null)?.parsed ?? Date.now();

    const turns: NormalizedTurn[] = pending.map(({ agentId, src: t, parsed }, idx) => {
      const statusCode = typeof t.status_code === "number" ? t.status_code : null;
      const errText = typeof t.error === "string" && t.error ? t.error : null;
      const hasLatency = t.total_latency_ms != null && !Number.isNaN(t.total_latency_ms);
      return {
        id: `${agentId}::${t.id}`,
        sessionId: agentId,
        agentRole: "peer",
        turnNumber: idx,
        turnType: t.turn_type,
        provider: t.provider,
        model: t.model,
        startTs: parsed ?? firstReal + idx * 1000,
        latencyMs: hasLatency ? Math.max(t.total_latency_ms!, 0) : 0,
        hasLatency,
        parentInteractionId: t.parent_interaction_id,
        responsePreview: t.response_text_preview,
        toolCalls: Array.isArray(t.tool_calls) ? t.tool_calls : [],
        statusCode,
        error: errText,
        inputTokens: t.input_tokens ?? null,
        outputTokens: t.output_tokens ?? null,
        totalTokens: t.total_tokens ?? null,
        totalCostUsd: t.total_cost_usd ?? null,
        isError: (statusCode != null && statusCode >= 400) || errText != null,
      };
    });

    const lanes: string[] = [];
    const seen = new Set<string>();
    for (const t of turns) {
      if (!seen.has(t.sessionId)) {
        lanes.push(t.sessionId);
        seen.add(t.sessionId);
      }
    }

    const totals = {
      agents: hostAgents.length,
      handoffs: (graph?.edges ?? []).filter(
        (e) => e.from_host === host || e.to_host === host,
      ).length,
      calls: hostAgents.reduce((s, a) => s + a.interaction_count, 0),
      tokens: hostAgents.reduce((s, a) => s + a.total_tokens, 0),
      costUsd: hostAgents.reduce((s, a) => s + a.total_cost_usd, 0),
    };

    return {
      conversationId: `${host} · ${scenarioId}`,
      graph: hostGraph,
      turns,
      lanes,
      roleBySession,
      nodeBySession,
      totals,
      loading,
      error,
      agentCount: hostAgents.length,
      truncated,
    };
  }, [host, scenarioId, graph, turnsByAgent, loading, error]);
}
