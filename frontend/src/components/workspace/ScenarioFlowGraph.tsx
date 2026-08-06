import { useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  type Edge,
  type Node,
  type ReactFlowInstance,
  useNodesState,
  useEdgesState,
  MarkerType,
} from "reactflow";
import dagre from "dagre";
import "reactflow/dist/style.css";

import AgentNodeComp, { type AgentNodeData } from "./nodes/AgentNode";
import HostGroupNode, { type HostGroupData } from "./nodes/HostGroupNode";
import HostTileNode, { type HostTileData } from "./nodes/HostTileNode";
import type { ScenarioGraph } from "../../types";

/**
 * Auto view switch: past this many agents (or hosts) the per-agent card
 * layout stops being readable, so we fall back to one compact tile per
 * host on a grid. Tuned so the 3–4 node demos keep the detailed view.
 */
const DETAIL_MAX_AGENTS = 24;
const DETAIL_MAX_HOSTS = 8;

/** Cap on simultaneously drawn host-level edges (a 100-host all-to-all
 *  mesh would be ~10k paths). Failing flows always win a slot. */
const MAX_HOST_EDGES = 150;

export type TopologyView = "auto" | "detail" | "hosts";

interface Props {
  graph: ScenarioGraph;
  /**
   * "detail": per-agent cards grouped by host (the original view).
   * "hosts": one compact tile per host on a grid — readable at 100+ nodes.
   * "auto" (default): pick based on graph size.
   */
  view?: TopologyView;
  /**
   * Replay position (unix ms). When set, edges are drawn per *event* and
   * filtered by time: calls that started before the playhead appear, calls
   * still in flight at the playhead pulse, agents on in-flight calls light
   * up. ``null`` = live (aggregated) rendering.
   */
  playheadMs?: number | null;
  onAgentClick?: (agentId: string) => void;
  onHostClick?: (host: string) => void;
  onEdgeSelect?: (correlationId: string) => void;
}

const parseTs = (s: string | null): number => {
  if (!s) return NaN;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : NaN;
};

/**
 * Never-completed detection, shared by every edge-render path. An edge is
 * open while it has a start and no completion frame; open beyond
 * ORPHAN_AFTER_MS it is "hung" — the killed-delegation pathology
 * PrismaDoctor reports as orphan_call — and must read as neither error-red
 * (it did not fail; it vanished) nor success. `refMs` is the playhead
 * during replay, wall-clock now otherwise.
 */
const ORPHAN_AFTER_MS = 120_000;
type EdgeOpenState = "done" | "inflight" | "hung";
const edgeOpenState = (
  tsStart: string | null,
  tsDone: string | null,
  refMs: number,
): EdgeOpenState => {
  const done = parseTs(tsDone);
  if (Number.isFinite(done)) return done <= refMs ? "done" : "inflight";
  const start = parseTs(tsStart);
  if (!Number.isFinite(start)) return "done";
  return refMs - start > ORPHAN_AFTER_MS ? "hung" : "inflight";
};
const ORPHAN_COLOR = "rgb(var(--role-orphan, 245 158 11))";
const ORPHAN_DASH = "2 4";

const NODE_W = 200;
const NODE_H = 110;
const GROUP_PAD_X = 28;
const GROUP_PAD_TOP = 56;   // room for the "node · <host>" header + totals line
const GROUP_PAD_BOTTOM = 20;

const TILE_W = 200;
const TILE_H = 84;
const TILE_GAP_X = 72;
const TILE_GAP_Y = 48;

const nodeTypes = {
  agent: AgentNodeComp,
  hostGroup: HostGroupNode,
  hostTile: HostTileNode,
};

// Host colors come from the shared recipe so hosts look identical across the
// topology, node inspector, and trace waterfall. (Only used in the detailed
// view — at cluster scale color encodes health, not identity.)
import { buildHostHueMap } from "../../lib/hostHue";

function layoutDagreFlat(nodes: Node[], edges: Edge[]) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 60, ranksep: 120, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  dagre.layout(g);
  return nodes.map((n) => {
    const pos = g.node(n.id);
    return { ...n, position: { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 } };
  });
}

interface HostViewResult {
  nodes: Node[];
  edges: Edge[];
  /** Host-level flows not drawn because of MAX_HOST_EDGES. */
  hiddenFlows: number;
}

/** Cluster-scale view: one tile per host on a near-square grid, edges
 *  aggregated per host pair. O(hosts + edges); no dagre involved.
 *  With a playhead, only calls started before it are aggregated and pairs
 *  with a call still in flight are drawn animated. */
function buildHostView(graph: ScenarioGraph, playheadMs: number | null = null): HostViewResult {
  const hostOf = new Map<string, string>();
  for (const a of graph.agents) hostOf.set(a.agent_id, a.host || "unknown");

  // ── Per-host aggregates ──────────────────────────────────────────────
  interface HostAgg {
    agentCount: number;
    totalTokens: number;
    totalCostUsd: number;
    msgsOut: number;
    msgsIn: number;
    errCount: number;
  }
  const hosts = new Map<string, HostAgg>();
  const hostAgg = (h: string): HostAgg => {
    let agg = hosts.get(h);
    if (!agg) {
      agg = {
        agentCount: 0, totalTokens: 0, totalCostUsd: 0,
        msgsOut: 0, msgsIn: 0, errCount: 0,
      };
      hosts.set(h, agg);
    }
    return agg;
  };
  for (const a of graph.agents) {
    const agg = hostAgg(a.host || "unknown");
    agg.agentCount += 1;
    agg.totalTokens += a.total_tokens;
    agg.totalCostUsd += a.total_cost_usd;
  }

  // ── Host-pair aggregates from the scenario edges ─────────────────────
  interface PairAgg {
    from: string;
    to: string;
    count: number;
    errCount: number;
    inFlight: number;
    hung: number;
    correlationId: string;
  }
  const pairs = new Map<string, PairAgg>();
  const refMs = playheadMs != null ? playheadMs : Date.now();
  for (const e of graph.edges) {
    if (playheadMs != null) {
      const start = parseTs(e.ts_start);
      if (!Number.isFinite(start) || start > playheadMs) continue; // not happened yet
    }
    const state = edgeOpenState(e.ts_start, e.ts_done, refMs);
    const inFlight = state === "inflight";
    const fh = e.from_host || hostOf.get(e.from_session_id) || "unknown";
    const th = e.to_host || hostOf.get(e.to_session_id) || "unknown";
    const isErr = state === "done" && (e.status || "").startsWith("error");
    hostAgg(fh).msgsOut += 1;
    hostAgg(th).msgsIn += 1;
    if (isErr) {
      hostAgg(fh).errCount += 1;
      if (th !== fh) hostAgg(th).errCount += 1;
    }
    if (fh === th) continue; // intra-host traffic: counted above, not drawn
    const key = `${fh}__${th}`;
    let pair = pairs.get(key);
    if (!pair) {
      pair = { from: fh, to: th, count: 0, errCount: 0, inFlight: 0, hung: 0, correlationId: e.correlation_id };
      pairs.set(key, pair);
    }
    pair.count += 1;
    if (isErr) pair.errCount += 1;
    if (inFlight) pair.inFlight += 1;
    if (state === "hung") {
      pair.hung += 1;
      pair.correlationId = e.correlation_id; // clicking a hung flow opens the orphan
    }
  }

  // ── Grid layout: natural-sorted hosts on a near-square grid ──────────
  const hostNames = Array.from(hosts.keys()).sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true }),
  );
  const cols = Math.max(1, Math.ceil(Math.sqrt(hostNames.length)));
  const maxTraffic = Math.max(
    1,
    ...Array.from(hosts.values()).map((h) => h.msgsOut + h.msgsIn),
  );

  const nodes: Node<HostTileData>[] = hostNames.map((host, i) => {
    const agg = hosts.get(host)!;
    return {
      id: `host__${host}`,
      type: "hostTile",
      position: {
        x: (i % cols) * (TILE_W + TILE_GAP_X),
        y: Math.floor(i / cols) * (TILE_H + TILE_GAP_Y),
      },
      style: { width: TILE_W, height: TILE_H },
      data: {
        host,
        agentCount: agg.agentCount,
        totalTokens: agg.totalTokens,
        totalCostUsd: agg.totalCostUsd,
        msgsOut: agg.msgsOut,
        msgsIn: agg.msgsIn,
        errCount: agg.errCount,
        trafficFrac: (agg.msgsOut + agg.msgsIn) / maxTraffic,
      },
    };
  });

  // ── Edges: hung flows first, then failing, then busiest, capped ──────
  const ranked = Array.from(pairs.values()).sort((a, b) => {
    if ((a.hung > 0) !== (b.hung > 0)) return (b.hung > 0 ? 1 : 0) - (a.hung > 0 ? 1 : 0);
    const aFailing = a.errCount >= 2 && a.errCount / a.count >= 0.25 ? 1 : 0;
    const bFailing = b.errCount >= 2 && b.errCount / b.count >= 0.25 ? 1 : 0;
    if (aFailing !== bFailing) return bFailing - aFailing;
    return b.count - a.count;
  });
  const drawn = ranked.slice(0, MAX_HOST_EDGES);
  // On a dense mesh, 150 "×N" labels are pure noise — keep labels only
  // while the graph is sparse enough to read them, plus on failing/hung flows.
  const showLabels = drawn.length <= 60;

  const edges: Edge[] = drawn.map((p) => {
    // Red is reserved for flows that are *persistently* failing — a stray
    // error among many calls shouldn't paint the whole cluster red. A hung
    // flow (a call that started and never completed) outranks both: it is
    // the orphan-call pathology, drawn in the dedicated orphan color.
    const failing = p.errCount >= 2 && p.errCount / p.count >= 0.25;
    const hung = p.hung > 0;
    const live = !hung && p.inFlight > 0;
    const color = hung
      ? ORPHAN_COLOR
      : live
        ? "rgb(var(--accent))"
        : failing
          ? "rgb(var(--error))"
          : "rgb(var(--fg-muted))";
    return {
      id: `hostedge__${p.from}__${p.to}`,
      source: `host__${p.from}`,
      target: `host__${p.to}`,
      animated: live,
      style: {
        stroke: color,
        strokeWidth: hung ? 2.5 : live ? 2.5 : Math.min(3, 1 + Math.log10(p.count)),
        strokeDasharray: hung ? ORPHAN_DASH : undefined,
        opacity: hung ? 1 : live ? 1 : failing ? 0.95 : 0.35,
        cursor: "pointer",
      },
      label: hung
        ? `⚠ ${p.hung} never completed`
        : (showLabels || failing) && p.count > 1
          ? `×${p.count}`
          : undefined,
      labelStyle: {
        fill: hung ? ORPHAN_COLOR : "rgb(var(--fg-secondary))",
        fontSize: 10,
        fontWeight: hung ? 600 : undefined,
      },
      labelBgStyle: { fill: "rgb(var(--bg-surface))" },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
      data: { correlationId: p.correlationId },
    };
  });

  return { nodes, edges, hiddenFlows: ranked.length - drawn.length };
}

export default function ScenarioFlowGraph({
  graph,
  view = "auto",
  playheadMs = null,
  onAgentClick,
  onHostClick,
  onEdgeSelect,
}: Props) {
  const hostCount = useMemo(
    () => new Set(graph.agents.map((a) => a.host || "unknown")).size,
    [graph.agents],
  );
  const effectiveView: "detail" | "hosts" =
    view !== "auto"
      ? view
      : graph.agents.length > DETAIL_MAX_AGENTS || hostCount > DETAIL_MAX_HOSTS
        ? "hosts"
        : "detail";

  // ─── Cluster-scale path: host tiles on a grid ─────────────────────────
  const hostView = useMemo(
    () => (effectiveView === "hosts" ? buildHostView(graph, playheadMs) : null),
    [effectiveView, graph, playheadMs],
  );

  // ─── Detailed path (small scenarios): per-agent cards via dagre ───────
  // Evenly-spaced per-host hues for this scenario — shared between group
  // rectangles and the agent chips inside them so the colors tie together.
  const hueMap = useMemo(
    () => buildHostHueMap(graph.agents.map((a) => a.host || "unknown")),
    [graph.agents],
  );

  const { rawNodes, rawEdges } = useMemo(() => {
    if (effectiveView !== "detail") {
      return { rawNodes: [] as Node<AgentNodeData>[], rawEdges: [] as Edge[] };
    }

    // Replay flags: which agents are on an in-flight call at the playhead,
    // and which have participated in anything that already started.
    const activeAgents = new Set<string>();
    const startedAgents = new Set<string>();
    if (playheadMs != null) {
      for (const e of graph.edges) {
        const start = parseTs(e.ts_start);
        if (!Number.isFinite(start) || start > playheadMs) continue;
        startedAgents.add(e.from_session_id);
        startedAgents.add(e.to_session_id);
        const done = parseTs(e.ts_done);
        if (!Number.isFinite(done) || done > playheadMs) {
          activeAgents.add(e.from_session_id);
          activeAgents.add(e.to_session_id);
        }
      }
    }

    // ─── 1. Agent child nodes (positioning comes after dagre) ─────────────
    const agentNodes: Node<AgentNodeData>[] = graph.agents.map((a) => ({
      id: a.agent_id,
      type: "agent",
      position: { x: 0, y: 0 },
      data: {
        sessionId: a.session_id,
        scopedSessionId: a.scoped_session_id,
        role: "peer",
        label:
          a.session_id.slice(0, 14) +
          (a.session_id.length > 14 ? "…" : ""),
        callCount: a.interaction_count,
        tokens: a.total_tokens,
        costUsd: a.total_cost_usd,
        active: activeAgents.has(a.agent_id),
        past: playheadMs == null ? true : startedAgents.has(a.agent_id),
        aggregate: true,
        host: a.host,
        hostHueDeg: hueMap.get(a.host || "unknown"),
      },
    }));

    // ─── 1b. Replay mode: one edge per *event*, filtered by the playhead ──
    if (playheadMs != null) {
      const edges: Edge[] = [];
      for (const e of graph.edges) {
        if (!e.from_session_id || !e.to_session_id) continue;
        const start = parseTs(e.ts_start);
        if (!Number.isFinite(start) || start > playheadMs) continue;
        const state = edgeOpenState(e.ts_start, e.ts_done, playheadMs);
        const inFlight = state === "inflight";
        const hung = state === "hung";
        const hasError = state === "done" && (e.status || "").startsWith("error");
        const color = hung
          ? ORPHAN_COLOR
          : inFlight
            ? "rgb(var(--accent))"
            : hasError
              ? "rgb(var(--role-error, 220 38 38))"
              : "rgb(var(--role-subagent))";
        edges.push({
          id: `ev__${e.event_id || e.correlation_id}`,
          source: e.from_session_id,
          target: e.to_session_id,
          animated: inFlight,
          style: {
            stroke: color,
            strokeWidth: inFlight || hung ? 2.5 : 1.5,
            strokeDasharray: inFlight ? undefined : hung ? ORPHAN_DASH : "6 4",
            opacity: inFlight || hung ? 1 : 0.3,
            cursor: "pointer",
          },
          label: hung ? "⚠ never completed" : inFlight ? e.tool_name || "call" : undefined,
          labelStyle: { fill: "rgb(var(--fg-primary))", fontSize: 10 },
          labelBgStyle: { fill: "rgb(var(--bg-surface))" },
          markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
          data: { correlationId: e.correlation_id },
        });
      }
      return { rawNodes: agentNodes, rawEdges: edges };
    }

    // ─── 2. Aggregate inter-agent edges (pair + direction) ───────────────
    // Openness is classified against wall-clock now: an edge with a start
    // and no completion frame is "open"; open past ORPHAN_AFTER_MS it is
    // hung and rendered in the orphan class (previously it fell through to
    // the success styling — the exact confusion the E4 case study hit).
    const nowMs = Date.now();
    const edgeMap = new Map<
      string,
      {
        source: string;
        target: string;
        count: number;
        okCount: number;
        errCount: number;
        hungCount: number;
        hungCorrelationId: string;
        lastTs: string;
        tools: Set<string>;
      }
    >();
    for (const e of graph.edges) {
      if (!e.from_session_id || !e.to_session_id) continue;
      const key = `${e.from_session_id}__${e.to_session_id}`;
      const slot = edgeMap.get(key) ?? {
        source: e.from_session_id,
        target: e.to_session_id,
        count: 0,
        okCount: 0,
        errCount: 0,
        hungCount: 0,
        hungCorrelationId: "",
        lastTs: "",
        tools: new Set<string>(),
      };
      slot.count += 1;
      const state = edgeOpenState(e.ts_start, e.ts_done, nowMs);
      if (state === "hung") {
        slot.hungCount += 1;
        slot.hungCorrelationId = e.correlation_id;
      } else if ((e.status || "").startsWith("error")) slot.errCount += 1;
      else slot.okCount += 1;
      if (e.tool_name) slot.tools.add(e.tool_name);
      const ts = e.ts_done || e.ts_start || "";
      if (ts && ts > slot.lastTs) slot.lastTs = ts;
      edgeMap.set(key, slot);
    }

    const edges: Edge[] = [];
    for (const [key, e] of edgeMap) {
      const hasHung = e.hungCount > 0;
      const hasError = e.errCount > 0;
      const color = hasHung
        ? ORPHAN_COLOR
        : hasError
          ? "rgb(var(--role-error, 220 38 38))"
          : "rgb(var(--role-subagent))";
      const tooltip = Array.from(e.tools).join(", ") || "inter-agent call";
      // Grab the first (earliest) raw edge in this pair so a click can open
      // detail for something specific — preferring the hung edge when there
      // is one, so the orphan is one click away.
      const firstForPair = graph.edges.find(
        (re) => re.from_session_id === e.source && re.to_session_id === e.target,
      );
      edges.push({
        id: key,
        source: e.source,
        target: e.target,
        animated: false,
        style: {
          stroke: color,
          strokeWidth: hasHung ? 2.5 : 1.75,
          strokeDasharray: hasHung ? ORPHAN_DASH : "6 4",
          opacity: hasHung ? 1 : undefined,
          cursor: "pointer",
        },
        label: hasHung
          ? `⚠ ${e.hungCount} never completed`
          : e.count > 1
            ? `×${e.count}`
            : tooltip.slice(0, 24),
        labelStyle: {
          fill: hasHung ? ORPHAN_COLOR : "rgb(var(--fg-secondary))",
          fontSize: 10,
          fontWeight: hasHung ? 600 : undefined,
        },
        labelBgStyle: { fill: "rgb(var(--bg-surface))" },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color,
          width: 16,
          height: 16,
        },
        data: {
          correlationId: hasHung
            ? e.hungCorrelationId
            : firstForPair?.correlation_id ?? "",
        },
      });
    }

    return { rawNodes: agentNodes, rawEdges: edges };
  }, [effectiveView, graph, hueMap, playheadMs]);

  // ─── 3. Run dagre on the flat agent graph first ─────────────────────────
  // The layout input is the *aggregate* pair structure, never the replay
  // edges — otherwise every playhead tick would re-run dagre and the nodes
  // would jump around mid-playback (and clobber user drags).
  const layoutPositions = useMemo(() => {
    if (effectiveView !== "detail" || graph.agents.length === 0) {
      return new Map<string, { x: number; y: number }>();
    }
    const ids: Node[] = graph.agents.map((a) => ({
      id: a.agent_id,
      position: { x: 0, y: 0 },
      data: {},
    }));
    const pairKeys = new Set<string>();
    const pairEdges: Edge[] = [];
    for (const e of graph.edges) {
      if (!e.from_session_id || !e.to_session_id) continue;
      const key = `${e.from_session_id}__${e.to_session_id}`;
      if (pairKeys.has(key)) continue;
      pairKeys.add(key);
      pairEdges.push({ id: key, source: e.from_session_id, target: e.to_session_id });
    }
    const laid = layoutDagreFlat(ids, pairEdges);
    return new Map(laid.map((n) => [n.id, n.position]));
  }, [effectiveView, graph]);

  const layoutedAgents = useMemo(
    () =>
      rawNodes.map((n) => ({
        ...n,
        position: layoutPositions.get(n.id) ?? { x: 0, y: 0 },
      })),
    [rawNodes, layoutPositions],
  );

  // ─── 4. Bucket laid-out agents by host → build parent groups ───────────
  const nodesWithGroups = useMemo(() => {
    if (effectiveView !== "detail") return [];
    // Count inter-agent edges per host for the group header.
    const outgoingByHost = new Map<string, number>();
    const incomingByHost = new Map<string, number>();
    {
      const agentHost = new Map<string, string>();
      for (const a of graph.agents) {
        agentHost.set(a.agent_id, a.host || "unknown");
      }
      for (const e of graph.edges) {
        const fh = agentHost.get(e.from_session_id);
        const th = agentHost.get(e.to_session_id);
        if (fh) outgoingByHost.set(fh, (outgoingByHost.get(fh) ?? 0) + 1);
        if (th) incomingByHost.set(th, (incomingByHost.get(th) ?? 0) + 1);
      }
    }

    const byHost = new Map<string, Node<AgentNodeData>[]>();
    for (const n of layoutedAgents) {
      const host = n.data.host || "unknown";
      if (!byHost.has(host)) byHost.set(host, []);
      byHost.get(host)!.push(n);
    }

    const result: Node[] = [];

    // ReactFlow requires: parent nodes appear BEFORE their children in the
    // array. So we push the group first, then its children.
    for (const [host, members] of byHost) {
      if (!members.length) continue;

      const xs = members.map((a) => a.position.x);
      const ys = members.map((a) => a.position.y);
      const minX = Math.min(...xs) - GROUP_PAD_X;
      const minY = Math.min(...ys) - GROUP_PAD_TOP;
      const maxX = Math.max(...xs.map((x) => x + NODE_W)) + GROUP_PAD_X;
      const maxY = Math.max(...ys.map((y) => y + NODE_H)) + GROUP_PAD_BOTTOM;

      const parentId = `host__${host}`;

      // Compute per-host aggregates from the scenario graph source of truth.
      let agentCount = 0;
      let totalTokens = 0;
      let totalCostUsd = 0;
      for (const a of graph.agents) {
        if ((a.host || "unknown") !== host) continue;
        agentCount += 1;
        totalTokens += a.total_tokens;
        totalCostUsd += a.total_cost_usd;
      }

      const groupData: HostGroupData = {
        host,
        hue: hueMap.get(host) ?? 200,
        agentCount,
        totalTokens,
        totalCostUsd,
        outgoingCalls: outgoingByHost.get(host) ?? 0,
        incomingCalls: incomingByHost.get(host) ?? 0,
      };

      result.push({
        id: parentId,
        type: "hostGroup",
        position: { x: minX, y: minY },
        style: { width: maxX - minX, height: maxY - minY, zIndex: 0 },
        data: groupData,
        // Groups are draggable so small clusters can be pulled apart and
        // inspected separately (children ride along). Clicking still opens
        // the host's Workspace view.
        draggable: true,
        selectable: false,
      });

      for (const a of members) {
        result.push({
          ...a,
          parentNode: parentId,
          extent: "parent",
          // Child coordinates are relative to the parent's origin.
          position: {
            x: a.position.x - minX,
            y: a.position.y - minY,
          },
          // Child nodes paint above the group.
          zIndex: 1,
        });
      }
    }

    return result;
  }, [effectiveView, layoutedAgents, graph, hueMap]);

  const viewNodes = effectiveView === "hosts" ? hostView!.nodes : nodesWithGroups;
  const viewEdges = effectiveView === "hosts" ? hostView!.edges : rawEdges;

  const [nodes, setNodes, onNodesChange] = useNodesState(viewNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(viewEdges);

  useEffect(() => {
    // Recomputes (playhead ticks, data refresh) must not clobber positions
    // the user dragged into place — keep the live position for known ids.
    setNodes((prev) => {
      if (!prev.length) return viewNodes;
      const prevById = new Map(prev.map((n) => [n.id, n]));
      return viewNodes.map((n) => {
        const old = prevById.get(n.id);
        return old ? { ...n, position: old.position } : n;
      });
    });
  }, [viewNodes, setNodes]);
  useEffect(() => {
    setEdges(viewEdges);
  }, [viewEdges, setEdges]);

  // The graph can change shape after mount (scenario switch, view toggle),
  // so a mount-time-only fitView can end up framing a stale extent. Re-fit
  // whenever the node *count* or the view changes — it doesn't fight the
  // user's pan/zoom while they inspect a stable topology.
  const [rfInstance, setRfInstance] = useState<ReactFlowInstance | null>(null);
  const fitPadding = effectiveView === "hosts" ? 0.1 : 0.2;
  useEffect(() => {
    if (!rfInstance || viewNodes.length === 0) return;
    const id = requestAnimationFrame(() =>
      rfInstance.fitView({ padding: fitPadding }),
    );
    return () => cancelAnimationFrame(id);
  }, [rfInstance, viewNodes.length, effectiveView, fitPadding]);

  const handleClick = (_: unknown, node: Node) => {
    if (node.type === "agent") {
      // Conversation fetches need the scoped session key, not the display id.
      const scoped = (node.data as AgentNodeData | undefined)?.scopedSessionId;
      onAgentClick?.(scoped || node.id);
    } else if (node.type === "hostGroup" || node.type === "hostTile") {
      const host = (node.data as { host?: string } | undefined)?.host ?? "";
      onHostClick?.(host);
    }
  };

  const handleEdgeClick = (_: unknown, edge: Edge) => {
    const corr = (edge.data as { correlationId?: string } | undefined)?.correlationId;
    if (corr) onEdgeSelect?.(corr);
  };

  if (graph.agents.length === 0) {
    return (
      <div className="h-full w-full flex items-center justify-center text-fg-muted text-sm">
        No peer agents have exchanged messages in this scenario yet.
      </div>
    );
  }

  return (
    <div className="h-full w-full bg-canvas relative">
      <ReactFlow
        key={`${effectiveView}-${graph.scenario_id}`}
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleClick}
        onEdgeClick={handleEdgeClick}
        nodeTypes={nodeTypes}
        onInit={setRfInstance}
        fitView
        fitViewOptions={{ padding: fitPadding }}
        minZoom={0.1}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="rgb(var(--border-soft))" gap={16} />
        <Controls showInteractive={false} />
        <MiniMap
          maskColor="rgb(var(--bg-canvas) / 0.7)"
          nodeColor={(n) => {
            if (n.type === "hostTile") {
              const d = n.data as HostTileData;
              const total = d.msgsOut + d.msgsIn;
              if (total > 0 && d.errCount >= 3 && d.errCount / total >= 0.1)
                return "rgb(var(--error))";
            }
            return "rgb(var(--bg-hover))";
          }}
          pannable
          zoomable
        />
      </ReactFlow>
      {hostView != null && hostView.hiddenFlows > 0 && (
        <div
          className="absolute bottom-3 left-1/2 -translate-x-1/2 px-2.5 py-1 rounded-md border border-border-soft text-[10px] text-fg-muted tabular-nums"
          style={{ backgroundColor: "rgb(var(--bg-surface) / 0.92)" }}
        >
          showing the {MAX_HOST_EDGES} busiest flows · {hostView.hiddenFlows}{" "}
          quieter flow{hostView.hiddenFlows === 1 ? "" : "s"} hidden (counts stay
          accurate on the tiles)
        </div>
      )}
    </div>
  );
}
