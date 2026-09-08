import { useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  type Edge,
  type Node,
  type ReactFlowInstance,
  useEdgesState,
  useNodesState,
} from "reactflow";
import "reactflow/dist/style.css";
import type { ClusterComms, ClusterCommsHost } from "../../api";

/**
 * Live node-to-node communication graph for the Cluster tab: one node per ARES
 * host (green ring = in the current allocation / active). Edges are the
 * inter-agent message flows between nodes.
 *
 * ``activeOnly`` (default) draws ONLY flows with traffic in the last few
 * seconds, so an idle cluster is clean (just the nodes) and lines appear when
 * nodes actually talk — instead of the cumulative grey mesh of every pair that
 * ever exchanged a message. Recency is computed against the server clock
 * (``comms.now_ns``) so it's accurate regardless of browser clock skew.
 */

const RADIUS = 200;
const CENTER = { x: 300, y: 240 };
const RECENT_MS = 9000;

// Above this many nodes a ring is unreadable (labels overlap, edges cross into a
// hairball), so we lay the nodes out on a grid instead. Below it the ring reads
// better for a small cluster.
const GRID_THRESHOLD = 10;
const GRID_DX = 180;
const GRID_DY = 96;

/** Position node i: ring for small clusters, grid for large ones. */
function nodePosition(i: number, n: number): { x: number; y: number } {
  if (n <= GRID_THRESHOLD) {
    const ang = (2 * Math.PI * i) / n - Math.PI / 2;
    return { x: CENTER.x + RADIUS * Math.cos(ang), y: CENTER.y + RADIUS * Math.sin(ang) };
  }
  const cols = Math.ceil(Math.sqrt(n));
  const row = Math.floor(i / cols);
  const col = i % cols;
  return { x: 40 + col * GRID_DX, y: 40 + row * GRID_DY };
}

function HostLabel({ h }: { h: ClusterCommsHost }) {
  return (
    <div className="text-left leading-tight">
      <div className="flex items-center gap-1">
        <span
          className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
          style={{ backgroundColor: h.in_allocation ? "rgb(var(--ok))" : "rgb(var(--fg-muted))" }}
        />
        <span className="font-mono text-[11px] font-semibold truncate">{h.host}</span>
      </div>
      <div className="text-[9px] text-fg-muted tabular-nums mt-0.5">
        {h.agents} agent{h.agents === 1 ? "" : "s"} · ↑{h.out} ↓{h.in}
        {h.errors > 0 && (
          <span style={{ color: "rgb(var(--error))" }}> · ⚠{h.errors}</span>
        )}
      </div>
    </div>
  );
}

function build(
  comms: ClusterComms,
  activeOnly: boolean,
  idleHosts: string[] = [],
): { nodes: Node[]; edges: Edge[] } {
  const hosts = comms.hosts;
  // Same canonicalisation as the backend's _short_host: the comms API returns
  // UNPADDED names (ares-comp-3) while sinfo zero-pads (ares-comp-03). Without
  // collapsing digit groups the same node is drawn twice — once with its live
  // traffic, once again as a green "idle · available" node.
  const norm = (h: string) =>
    h.replace(/-40g$/, "").split(".")[0].toLowerCase()
      .replace(/\d+/g, (d) => String(Number(d)));
  // Idle nodes the cluster has free, that aren't already part of our deployment
  // — drawn as standalone "available" nodes so the Cluster page shows free
  // capacity alongside the live deployment.
  const present = new Set(hosts.map((h) => norm(h.host)));
  const freeHosts = idleHosts.filter((h) => !present.has(norm(h)));
  const n = Math.max(hosts.length + freeHosts.length, 1);
  const now = comms.now_ns ? comms.now_ns / 1e6 : Date.now();

  const nodes: Node[] = hosts.map((h, i) => {
    return {
      id: h.host,
      position: nodePosition(i, n),
      data: { label: <HostLabel h={h} /> },
      draggable: true,
      style: {
        width: 152,
        borderRadius: 10,
        border: `2px solid ${h.in_allocation ? "rgb(var(--ok))" : "rgb(var(--fg-muted))"}`,
        background: "rgb(var(--bg-surface))",
        color: "rgb(var(--fg-primary))",
        opacity: h.in_allocation ? 1 : 0.55,
        padding: 6,
      },
    };
  });

  // Free/idle nodes: green dashed outline, labelled "idle · available".
  freeHosts.forEach((host, j) => {
    nodes.push({
      id: host,
      position: nodePosition(hosts.length + j, n),
      data: {
        label: (
          <div className="text-left leading-tight">
            <div className="flex items-center gap-1">
              <span className="inline-block w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: "rgb(var(--ok))" }} />
              <span className="font-mono text-[11px] font-semibold truncate">{host.split(".")[0]}</span>
            </div>
            <div className="text-[9px] text-fg-muted mt-0.5">idle · available</div>
          </div>
        ),
      },
      draggable: true,
      style: {
        width: 152,
        borderRadius: 10,
        border: "2px dashed rgb(var(--ok))",
        background: "rgb(var(--bg-surface))",
        color: "rgb(var(--fg-primary))",
        opacity: 0.7,
        padding: 6,
      },
    });
  });

  const edges: Edge[] = [];
  for (const e of comms.edges) {
    const recent = now - (Date.parse(e.last_ts) || 0) < RECENT_MS;
    if (activeOnly && !recent) continue; // hide the cumulative grey history
    const failing = e.errors >= 2 && e.errors / e.count >= 0.25;
    const color = failing
      ? "rgb(var(--error))"
      : recent
        ? "rgb(var(--accent))"
        : "rgb(var(--fg-muted))";
    edges.push({
      id: `${e.from_host}__${e.to_host}`,
      source: e.from_host,
      target: e.to_host,
      animated: recent,
      // Labels only in the full ("show all") view — they're noise on the live one.
      label: activeOnly ? undefined : `×${e.count}${e.errors ? ` ⚠${e.errors}` : ""}`,
      labelStyle: { fontSize: 10, fill: "rgb(var(--fg-secondary))" },
      labelBgStyle: { fill: "rgb(var(--bg-surface))" },
      style: {
        stroke: color,
        strokeWidth: recent ? 2.5 : Math.min(4, 1 + Math.log10(e.count + 1) * 1.8),
        opacity: recent ? 1 : failing ? 0.9 : 0.4,
      },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
    });
  }
  return { nodes, edges };
}

export default function ClusterCommsGraph(
  { comms, activeOnly = true, idleHosts = [] }:
    { comms: ClusterComms; activeOnly?: boolean; idleHosts?: string[] },
) {
  const idleKey = idleHosts.join(",");
  const { nodes: builtNodes, edges: builtEdges } = useMemo(
    () => build(comms, activeOnly, idleHosts),
    // idleKey stands in for idleHosts (array identity changes each poll).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [comms, activeOnly, idleKey],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState(builtNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(builtEdges);
  const [rf, setRf] = useState<ReactFlowInstance | null>(null);

  // Refresh data each poll, but keep any positions the user dragged.
  useEffect(() => {
    setNodes((prev) => {
      if (!prev.length) return builtNodes;
      const byId = new Map(prev.map((p) => [p.id, p]));
      return builtNodes.map((nd) => {
        const old = byId.get(nd.id);
        return old ? { ...nd, position: old.position } : nd;
      });
    });
  }, [builtNodes, setNodes]);
  useEffect(() => {
    setEdges(builtEdges);
  }, [builtEdges, setEdges]);
  useEffect(() => {
    if (rf && nodes.length) {
      const id = requestAnimationFrame(() => rf.fitView({ padding: 0.2 }));
      return () => cancelAnimationFrame(id);
    }
  }, [rf, nodes.length]);

  if (!comms.hosts.length) {
    return <div className="text-xs text-fg-muted">No nodes in the allocation yet.</div>;
  }

  return (
    <div className="h-[420px] rounded border border-border-soft relative">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onInit={setRf}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        maxZoom={1.8}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="rgb(var(--border-soft))" gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
      {activeOnly && edges.length === 0 && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 px-2.5 py-1 rounded-md border border-border-soft text-[10px] text-fg-muted"
             style={{ backgroundColor: "rgb(var(--bg-surface) / 0.92)" }}>
          idle — lines appear when nodes communicate (click "Generate traffic")
        </div>
      )}
    </div>
  );
}
