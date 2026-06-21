import { useEffect, useRef, useState } from "react";
import {
  getInterAgentEdges,
  openInterAgentStream,
  type InterAgentEvent,
  type InterAgentStitched,
} from "../api";
import type { ScenarioAgentNode, ScenarioEdge, ScenarioGraph } from "../types";

/**
 * Live scenario graph built entirely from inter-agent edges — the backlog
 * comes from the dashboard's hot store (instant, GIL-safe) and updates arrive
 * over the in-process SSE bus the moment a call starts or completes. This is
 * the real-time counterpart to ``getScenarioGraph`` (/graph), which has to
 * replay per-agent LLM subtrees from ChronoLog and therefore can't be read for
 * a scenario still inside the ~180s drain window.
 *
 * Per-agent token/cost totals come from Path-A and are not available here, so
 * nodes report call counts only — the Workspace tab shows LLM detail per agent
 * once the scenario has drained.
 */

export interface LiveScenarioState {
  graph: ScenarioGraph | null;
  loading: boolean;
  error: string | null;
  /** True once the SSE stream is open (edges update in real time). */
  isLive: boolean;
  /** Count of edges seen — handy for a "N messages" live badge. */
  edgeCount: number;
}

function buildGraph(scenarioId: string, slots: InterAgentStitched[]): ScenarioGraph {
  const hostOf = new Map<string, string>();
  const outCount = new Map<string, number>();
  const sessions = new Set<string>();
  const edges: ScenarioEdge[] = [];

  for (const s of slots) {
    if (s.from_session) {
      sessions.add(s.from_session);
      if (s.from_host) hostOf.set(s.from_session, s.from_host);
      outCount.set(s.from_session, (outCount.get(s.from_session) ?? 0) + 1);
    }
    if (s.to_session) {
      sessions.add(s.to_session);
      if (s.to_host) hostOf.set(s.to_session, s.to_host);
    }
    edges.push({
      kind: "inter_agent_msg",
      event_id: s.correlation_id,
      correlation_id: s.correlation_id,
      from_session_id: s.from_session,
      to_session_id: s.to_session,
      from_host: s.from_host,
      to_host: s.to_host,
      ts_start: s.ts_start,
      ts_done: s.ts_done,
      tool_name: s.tool_name,
      status: s.status,
      latency_ms: s.latency_ms,
      payload_preview: s.payload_preview,
    });
  }

  const agents: ScenarioAgentNode[] = Array.from(sessions)
    .sort()
    .map((sid) => ({
      agent_id: sid,
      session_id: sid,
      scoped_session_id: sid,
      agent_role: "peer",
      host: hostOf.get(sid) || "unknown",
      interaction_count: outCount.get(sid) ?? 0,
      total_tokens: 0,
      total_cost_usd: 0,
      sub_sessions: [],
    }));

  return { scenario_id: scenarioId, agents, edges };
}

export function useLiveScenario(scenarioId: string | null): LiveScenarioState {
  const [graph, setGraph] = useState<ScenarioGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLive, setIsLive] = useState(false);
  const [edgeCount, setEdgeCount] = useState(0);

  // Stitched calls keyed by correlation_id; mutated in place across SSE frames.
  const slotsRef = useRef<Map<string, InterAgentStitched>>(new Map());
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    slotsRef.current = new Map();
    setGraph(null);
    setIsLive(false);
    setError(null);
    setEdgeCount(0);
    if (!scenarioId) return;

    let cancelled = false;
    setLoading(true);

    const rebuild = () => {
      const slots = Array.from(slotsRef.current.values());
      setGraph(buildGraph(scenarioId, slots));
      setEdgeCount(slots.length);
    };

    const mergeEvent = (ev: InterAgentEvent) => {
      const cid = ev.correlation_id || ev.event_id;
      const slot: InterAgentStitched = slotsRef.current.get(cid) ?? {
        correlation_id: cid,
        scenario_id: ev.scenario_id,
        from_host: ev.from_host,
        from_session: ev.from_session,
        to_host: ev.to_host,
        to_session: ev.to_session,
        kind: ev.kind,
        tool_name: ev.tool_name,
        payload_digest: ev.payload_digest,
        payload_preview: ev.payload_preview,
        ts_start: null,
        ts_done: null,
        status: "",
        latency_ms: 0,
      };
      if (ev.phase === "start") {
        slot.ts_start = ev.ts;
      } else {
        slot.ts_done = ev.ts;
        slot.status = ev.status || "ok";
        slot.latency_ms = ev.latency_ms;
      }
      // Fill routing/tool from whichever phase carried it.
      slot.from_host = slot.from_host || ev.from_host;
      slot.from_session = slot.from_session || ev.from_session;
      slot.to_host = slot.to_host || ev.to_host;
      slot.to_session = slot.to_session || ev.to_session;
      if (!slot.tool_name && ev.tool_name) slot.tool_name = ev.tool_name;
      if (!slot.payload_preview && ev.payload_preview) slot.payload_preview = ev.payload_preview;
      slotsRef.current.set(cid, slot);
    };

    getInterAgentEdges(scenarioId)
      .then((backlog) => {
        if (cancelled) return;
        for (const s of backlog) slotsRef.current.set(s.correlation_id, s);
        rebuild();
        setLoading(false);
        sourceRef.current = openInterAgentStream(
          scenarioId,
          (ev) => {
            if (cancelled) return;
            setIsLive(true);
            mergeEvent(ev);
            rebuild();
          },
          () => {
            if (!cancelled) setIsLive(false); // EventSource auto-reconnects
          },
        );
        // Optimistically mark live; the error handler clears it on drop.
        setIsLive(true);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });

    return () => {
      cancelled = true;
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, [scenarioId]);

  return { graph, loading, error, isLive, edgeCount };
}
