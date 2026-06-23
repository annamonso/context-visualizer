import { useEffect, useMemo, useState } from "react";
import {
  getFleetHealth,
  getFleetNode,
  getClusterCapacity,
  type FleetHealth,
  type FleetNodeDetail,
  type NodeHealth,
  type ClusterCapacity,
} from "../api";

// A node as drawn in the grid: its health (if it produced any data) plus its
// real cluster availability (idle / busy with other work / down), so we can
// grey out nodes that aren't ours instead of inventing data for them.
type GridNode = NodeHealth & { cluster?: "idle" | "busy" | "down"; hasData: boolean };

/**
 * Fleet tab — node health at scale. The Cluster tab's node-link graph is
 * unreadable past ~12 nodes; this view renders a heatmap grid (one cell per
 * node, coloured by the chosen metric) that stays legible at 100+ nodes because
 * it reads pre-aggregated per-(host, minute) rollups, not raw events. Worst
 * nodes sort first; click a cell to drill into its time series.
 */

const POLL_MS = 4000;

type Metric = "status" | "error_rate" | "p95_latency_ms" | "cost_usd" | "events";

const METRICS: { key: Metric; label: string }[] = [
  { key: "status", label: "Health" },
  { key: "error_rate", label: "Error rate" },
  { key: "p95_latency_ms", label: "p95 latency" },
  { key: "events", label: "Throughput" },
  { key: "cost_usd", label: "Cost" },
];

// "" = all time. Default to it so the Fleet keeps showing captured data after a
// run ends (a recent window would empty out once nodes stop producing events).
const WINDOWS = ["", "5m", "15m", "1h"];

const STATUS_COLOR: Record<string, string> = {
  crit: "#ef4444",
  warn: "#eab308",
  ok: "#22c55e",
};

export default function FleetPage() {
  const [data, setData] = useState<FleetHealth | null>(null);
  const [metric, setMetric] = useState<Metric>("status");
  const [windowSel, setWindowSel] = useState<string>("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<FleetNodeDetail | null>(null);
  const [capacity, setCapacity] = useState<ClusterCapacity | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Real cluster node list (so we draw EVERY node, greying the unavailable).
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      getClusterCapacity()
        .then((c) => !cancelled && setCapacity(c))
        .catch(() => {});
    void tick();
    const id = setInterval(tick, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      getFleetHealth(windowSel || undefined)
        .then((d) => !cancelled && (setData(d), setError(null)))
        .catch((e) => !cancelled && setError(String(e)));
    void tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [windowSel]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    getFleetNode(selected, windowSel || undefined)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [selected, windowSel, data]);

  // Cells are sorted worst-first within the chosen metric. We draw EVERY real
  // cluster node: ones that produced data carry health; the rest are greyed by
  // their real SLURM state (idle / busy with other work / down).
  const cells = useMemo<GridNode[]>(() => {
    const byHost = new Map<string, GridNode>();
    const norm = (h: string) => h.replace(/-40g$/, "").split(".")[0];
    for (const n of data?.nodes ?? []) {
      byHost.set(norm(n.host), { ...n, hasData: n.events > 0, cluster: undefined });
    }
    const capNodes = capacity?.nodes ?? {};
    for (const [host, info] of Object.entries(capNodes)) {
      const key = norm(host);
      const existing = byHost.get(key);
      if (existing) existing.cluster = info.status;
      else
        byHost.set(key, {
          ...emptyNode(host),
          cluster: info.status,
          hasData: false,
        });
    }
    // Fleet shows EVERY node — it's the reference for comparing which nodes
    // perform better (active ones coloured by health, the rest greyed by state).
    const nodes = [...byHost.values()];
    nodes.sort((a, b) => {
      // Active nodes first; then within them sort by the chosen metric; greyed
      // (no data) nodes sink to the bottom, idle before busy/down.
      if (a.hasData !== b.hasData) return a.hasData ? -1 : 1;
      if (!a.hasData) {
        const rank = (c?: string) => (c === "idle" ? 0 : c === "busy" ? 1 : 2);
        return rank(a.cluster) - rank(b.cluster) || a.host.localeCompare(b.host);
      }
      if (metric === "status") {
        const order = { crit: 0, warn: 1, ok: 2 } as Record<string, number>;
        return (order[a.status] - order[b.status]) || b.error_rate - a.error_rate;
      }
      return metricValue(b, metric) - metricValue(a, metric);
    });
    return nodes;
  }, [data, capacity, metric]);

  const maxVal = useMemo(() => {
    if (metric === "status") return 1;
    return Math.max(1e-9, ...cells.map((n) => metricValue(n, metric)));
  }, [cells, metric]);

  const summary = data?.summary ?? {};

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div
        className="flex items-center gap-3 px-4 py-2.5 border-b border-border-soft shrink-0 flex-wrap"
        style={{ backgroundColor: "rgb(var(--bg-surface))" }}
      >
        <span className="text-sm font-semibold">Fleet Health</span>
        <span className="text-[10px] uppercase tracking-widest text-fg-muted">
          {summary.nodes ?? 0} nodes
        </span>
        <div className="flex items-center gap-2 text-xs">
          <Chip color={STATUS_COLOR.crit} label={`${summary.crit ?? 0} crit`} />
          <Chip color={STATUS_COLOR.warn} label={`${summary.warn ?? 0} warn`} />
          <Chip color={STATUS_COLOR.ok} label={`${summary.ok ?? 0} ok`} />
          {capacity?.available && (
            <>
              <span className="text-fg-muted/40">|</span>
              <Chip color={GREY.idle} label={`${capacity.idle_count ?? 0} idle`} />
              <Chip color={GREY.busy} label={`${capacity.busy_count ?? 0} busy`} />
            </>
          )}
        </div>

        <div className="ml-auto flex items-center gap-3 text-xs">
          <span className="text-fg-muted">
            {summary.events ?? 0} events · {summary.errors ?? 0} errs · $
            {(summary.cost_usd ?? 0).toFixed(3)}
          </span>
          <Select value={metric} onChange={(v) => setMetric(v as Metric)} options={METRICS.map((m) => ({ value: m.key, label: m.label }))} />
          <Select
            value={windowSel}
            onChange={setWindowSel}
            options={WINDOWS.map((w) => ({ value: w, label: w || "all" }))}
          />
        </div>
      </div>

      {error && (
        <div className="px-4 py-2 text-xs" style={{ color: STATUS_COLOR.crit }}>
          {error}
        </div>
      )}

      <div className="flex-1 min-h-0 flex">
        {/* Heatmap grid */}
        <div className="flex-1 min-w-0 overflow-auto p-3">
          {cells.length === 0 ? (
            <div className="p-6 text-sm text-fg-muted">
              No node activity yet. Start traffic on the cluster (or run a demo)
              and the fleet lights up here.
            </div>
          ) : (
            <div
              className="grid gap-1.5"
              style={{ gridTemplateColumns: "repeat(auto-fill, minmax(86px, 1fr))" }}
            >
              {cells.map((n) => (
                <Cell
                  key={n.host}
                  node={n}
                  metric={metric}
                  maxVal={maxVal}
                  active={selected === n.host}
                  onClick={() => setSelected(n.host)}
                />
              ))}
            </div>
          )}
        </div>

        {/* Node drill-down */}
        {selected && (
          <div className="w-[320px] shrink-0 border-l border-border-soft overflow-auto">
            <NodeDetail host={selected} detail={detail} onClose={() => setSelected(null)} />
          </div>
        )}
      </div>
    </div>
  );
}

function metricValue(n: NodeHealth, metric: Metric): number {
  switch (metric) {
    case "error_rate":
      return n.error_rate;
    case "p95_latency_ms":
      return n.p95_latency_ms;
    case "cost_usd":
      return n.cost_usd;
    case "events":
      return n.events;
    default:
      return n.status === "crit" ? 1 : n.status === "warn" ? 0.5 : 0;
  }
}

function cellColor(n: NodeHealth, metric: Metric, maxVal: number): string {
  if (metric === "status") return STATUS_COLOR[n.status] ?? "#888";
  const v = metricValue(n, metric);
  const t = Math.min(1, v / maxVal);
  // green -> amber -> red ramp
  if (t < 0.5) {
    const k = t / 0.5;
    return `rgb(${Math.round(34 + k * 200)}, ${Math.round(197 - k * 18)}, ${Math.round(94 - k * 80)})`;
  }
  const k = (t - 0.5) / 0.5;
  return `rgb(${Math.round(234 + k * 5)}, ${Math.round(179 - k * 111)}, ${Math.round(8 + k * 60)})`;
}

function metricText(n: NodeHealth, metric: Metric): string {
  switch (metric) {
    case "error_rate":
      return `${(n.error_rate * 100).toFixed(0)}%`;
    case "p95_latency_ms":
      return `${(n.p95_latency_ms / 1000).toFixed(1)}s`;
    case "cost_usd":
      return `$${n.cost_usd.toFixed(3)}`;
    case "events":
      return `${n.events}`;
    default:
      return n.status;
  }
}

// Greys for nodes we have no data for: light grey = idle/available, darker =
// busy with other work, darkest = down.
const GREY: Record<string, string> = { idle: "#4b5563", busy: "#374151", down: "#1f2937" };

function emptyNode(host: string): NodeHealth {
  return {
    host, events: 0, errors: 0, error_rate: 0, avg_latency_ms: 0, p95_latency_ms: 0,
    input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_usd: 0, agents: 0,
    last_minute: 0, in_allocation: false, status: "ok",
  };
}

function Cell({
  node,
  metric,
  maxVal,
  active,
  onClick,
}: {
  node: GridNode;
  metric: Metric;
  maxVal: number;
  active: boolean;
  onClick: () => void;
}) {
  // No data for this node → grey it by its real cluster state, don't fake a metric.
  if (!node.hasData) {
    const grey = GREY[node.cluster ?? "busy"] ?? GREY.busy;
    const label =
      node.cluster === "idle" ? "idle" : node.cluster === "down" ? "down" : "busy";
    return (
      <button
        type="button"
        onClick={onClick}
        title={`${node.host}\n${label} (no activity from us)`}
        className="rounded p-1.5 text-left flex flex-col gap-0.5"
        style={{ backgroundColor: grey, outline: active ? "2px solid rgb(var(--accent))" : "none" }}
      >
        <span className="text-[10px] font-medium truncate text-white/70">{shortHost(node.host)}</span>
        <span className="text-[10px] tabular-nums text-white/45">{label}</span>
      </button>
    );
  }
  const bg = cellColor(node, metric, maxVal);
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${node.host}\nerr ${(node.error_rate * 100).toFixed(0)}% · p95 ${(node.p95_latency_ms / 1000).toFixed(1)}s · ${node.events} ev · ${node.agents} agents`}
      className="rounded p-1.5 text-left flex flex-col gap-0.5 transition-transform hover:scale-[1.03]"
      style={{ backgroundColor: bg, outline: active ? "2px solid rgb(var(--accent))" : "none" }}
    >
      <span className="text-[10px] font-medium truncate text-black/80">
        {shortHost(node.host)}
      </span>
      <span className="text-[11px] font-semibold tabular-nums text-black/90">
        {metricText(node, metric)}
      </span>
    </button>
  );
}

function NodeDetail({
  host,
  detail,
  onClose,
}: {
  host: string;
  detail: FleetNodeDetail | null;
  onClose: () => void;
}) {
  const h = detail?.health;
  const series = detail?.series ?? [];
  const maxEv = Math.max(1, ...series.map((s) => s.events));
  return (
    <div className="p-3 flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold truncate">{host}</span>
        <button type="button" onClick={onClose} className="ml-auto text-fg-muted hover:text-fg-primary text-xs">
          ✕
        </button>
      </div>
      {h && (
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Stat label="status" value={h.status} color={STATUS_COLOR[h.status]} />
          <Stat label="error rate" value={`${(h.error_rate * 100).toFixed(1)}%`} />
          <Stat label="p95 latency" value={`${(h.p95_latency_ms / 1000).toFixed(2)}s`} />
          <Stat label="avg latency" value={`${(h.avg_latency_ms / 1000).toFixed(2)}s`} />
          <Stat label="events" value={`${h.events}`} />
          <Stat label="errors" value={`${h.errors}`} />
          <Stat label="agents" value={`${h.agents}`} />
          <Stat label="cost" value={`$${h.cost_usd.toFixed(4)}`} />
          <Stat label="tokens" value={`${h.total_tokens}`} />
        </div>
      )}
      <div>
        <div className="text-[10px] uppercase tracking-widest text-fg-muted mb-1">
          per-minute activity
        </div>
        <div className="flex items-end gap-0.5 h-20 border-b border-border-soft">
          {series.length === 0 ? (
            <span className="text-xs text-fg-muted">no buckets in window</span>
          ) : (
            series.map((b) => (
              <div
                key={b.minute}
                title={`${b.events} events, ${b.errors} errors`}
                className="flex-1 min-w-[2px] rounded-t"
                style={{
                  height: `${Math.max(4, (b.events / maxEv) * 100)}%`,
                  backgroundColor: b.errors > 0 ? STATUS_COLOR.crit : "rgb(var(--accent))",
                }}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-widest text-fg-muted">{label}</span>
      <span className="font-semibold tabular-nums" style={{ color }}>
        {value}
      </span>
    </div>
  );
}

function Chip({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="w-2 h-2 rounded-full" style={{ backgroundColor: color }} />
      <span className="text-fg-muted">{label}</span>
    </span>
  );
}

function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="bg-canvas border border-border-soft rounded px-2 py-1 text-xs"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function shortHost(host: string): string {
  // ares-comp-12 -> comp-12; keep last two dash-segments when long.
  const parts = host.split(".")[0].split("-");
  return parts.length > 2 ? parts.slice(-2).join("-") : host;
}
