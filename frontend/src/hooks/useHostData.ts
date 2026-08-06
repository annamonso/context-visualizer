import { useEffect, useMemo, useState } from "react";
import { getAgentGraph, getConversationTurns, getScenarioGraph } from "../api";
import type { AgentEdge, AgentGraph, AgentNode, ConversationTurn, ScenarioGraph } from "../types";
import type { ConversationData, NormalizedTurn } from "./useConversationData";

/** Bound the per-host fan-out: one turns + graph fetch per agent. */
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

interface PerAgent {
  turns: ConversationTurn[];
  agraph: AgentGraph | null;
}

/**
 * Host-scoped Workspace data: every agent on one node of a scenario, their
 * turns merged into a single chronological stream. Returns the exact
 * ``ConversationData`` shape ``useConversationData`` produces, so the agent
 * graph, timeline, playhead, and detail panel work on it unchanged.
 *
 * Each agent's *own* conversation graph is fetched and merged in, so the
 * node view keeps the inner structure the interceptor reconstructs —
 * orchestrator/subagent roles, sub-sessions, handoffs — rather than
 * flattening every conversation to one anonymous "peer" card. Turns keep
 * their real per-record session id whenever it maps to a merged graph node,
 * so the playhead lights up the right sub-agent and tool-call animations
 * attach to the right card.
 */
export function useHostData(
  host: string | null,
  scenarioId: string | null,
): ConversationData & { agentCount: number; truncated: number } {
  const [graph, setGraph] = useState<ScenarioGraph | null>(null);
  const [byAgent, setByAgent] = useState<Map<string, PerAgent>>(new Map());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!host || !scenarioId) {
      setGraph(null);
      setByAgent(new Map());
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
          agents.map(async (a) => {
            // Fetch by the scoped key (real session id); lane by display id.
            const key = a.scoped_session_id || a.agent_id;
            const [turns, agraph] = await Promise.all([
              getConversationTurns(key).catch(() => [] as ConversationTurn[]),
              getAgentGraph(key).catch(() => null),
            ]);
            return [a.agent_id, { turns, agraph }] as const;
          }),
        );
        if (cancelled) return;
        setByAgent(new Map(per));
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

    // ── Merge per-agent conversation graphs ──────────────────────────────
    // Each fetched agent contributes its full conversation subtree (root +
    // subagents + tool sessions, with real roles). Agents whose graph is
    // missing fall back to a synthetic peer node so they still render.
    const graphNodes: AgentNode[] = [];
    const graphEdges: AgentEdge[] = [];
    const nodeIds = new Set<string>();
    // Scenario-level agent_id → the merged node that represents its root
    // (used to attach intra-host inter-agent edges and orphan turns).
    const rootOf = new Map<string, string>();

    for (const a of hostAgents.slice(0, MAX_AGENTS)) {
      const per = byAgent.get(a.agent_id);
      const scoped = a.scoped_session_id || a.agent_id;
      const anodes = per?.agraph?.nodes ?? [];
      if (anodes.length > 0) {
        for (const n of anodes) {
          if (nodeIds.has(n.session_id)) continue;
          nodeIds.add(n.session_id);
          graphNodes.push({ ...n, host, scenario_id: scenarioId });
        }
        graphEdges.push(...(per?.agraph?.edges ?? []));
        const root =
          anodes.find((n) => n.session_id === scoped) ??
          anodes.find((n) => n.session_id === a.session_id) ??
          anodes.find((n) => n.agent_role === "orchestrator") ??
          anodes[0];
        rootOf.set(a.agent_id, root.session_id);
      } else {
        if (!nodeIds.has(a.agent_id)) {
          nodeIds.add(a.agent_id);
          graphNodes.push({
            session_id: a.agent_id,
            agent_role: "peer",
            interaction_count: a.interaction_count,
            total_tokens: a.total_tokens,
            total_cost_usd: a.total_cost_usd,
            host,
            scenario_id: scenarioId,
          });
        }
        rootOf.set(a.agent_id, a.agent_id);
      }
    }

    // Intra-host inter-agent calls, remapped onto the merged roots.
    const agentIds = new Set(hostAgents.map((a) => a.agent_id));
    (graph?.edges ?? [])
      .filter(
        (e) => agentIds.has(e.from_session_id) && agentIds.has(e.to_session_id),
      )
      .forEach((e, i) => {
        graphEdges.push({
          from_session_id: rootOf.get(e.from_session_id) ?? e.from_session_id,
          to_session_id: rootOf.get(e.to_session_id) ?? e.to_session_id,
          interaction_id: e.correlation_id,
          turn_number: i,
          latency_ms: e.latency_ms ?? null,
          kind: "inter_agent_msg" as const,
        });
      });

    const roleBySession = new Map<string, string>();
    const nodeBySession = new Map<string, AgentNode>();
    for (const n of graphNodes) {
      roleBySession.set(n.session_id, n.agent_role ?? "unknown");
      nodeBySession.set(n.session_id, n);
    }

    const hostGraph: AgentGraph = {
      conversation_id: `${host}@${scenarioId}`,
      nodes: graphNodes,
      edges: graphEdges,
    };

    // ── Merge every agent's turns into one chronological stream ─────────
    interface Pending { agentId: string; src: ConversationTurn; parsed: number | null }
    const pending: Pending[] = [];
    for (const [agentId, per] of byAgent) {
      for (const t of per.turns) {
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
      // Keep the record's own session id when it maps to a merged node so
      // sub-agent lanes and tool animations resolve; else pin to the root.
      const sid =
        t.session_id && nodeIds.has(t.session_id)
          ? t.session_id
          : rootOf.get(agentId) ?? agentId;
      return {
        // Keep the raw interaction id — DetailPanel fetches by it, and a
        // synthetic `${agentId}::` prefix 404s the lookup. Ids are already
        // session-scoped upstream, so cross-agent collisions can't happen.
        id: t.id,
        sessionId: sid,
        agentRole: roleBySession.get(sid) ?? "peer",
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
  }, [host, scenarioId, graph, byAgent, loading, error]);
}
