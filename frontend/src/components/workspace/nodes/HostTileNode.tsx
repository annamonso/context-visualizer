import { Handle, Position } from "reactflow";

/**
 * Compact host tile for cluster-scale view. One tile per physical node,
 * laid out on a grid — readable at 100+ hosts where per-agent cards are
 * not. Deliberately neutral colors: at cluster scale, color encodes
 * health (ok / failing), not host identity.
 */
export interface HostTileData {
  host: string;
  agentCount: number;
  totalTokens: number;
  totalCostUsd: number;
  msgsOut: number;
  msgsIn: number;
  errCount: number;
  /** This host's share of total cluster traffic (0–1), drives the bar. */
  trafficFrac: number;
}

function formatK(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

export default function HostTileNode({ data }: { data: HostTileData }) {
  // Proportional health: a couple of failures among hundreds of calls is
  // normal background noise (amber count badge only); red is reserved for
  // hosts where a meaningful fraction of traffic is failing.
  const totalMsgs = data.msgsOut + data.msgsIn;
  const errFrac = totalMsgs > 0 ? data.errCount / totalMsgs : 0;
  const unhealthy = errFrac >= 0.1 && data.errCount >= 3;
  const hasErr = data.errCount > 0;
  const statusColor = unhealthy ? "rgb(var(--error))" : "rgb(var(--ok))";

  return (
    <div
      className="rounded-md border"
      style={{
        width: "100%",
        height: "100%",
        backgroundColor: "rgb(var(--bg-surface))",
        borderColor: unhealthy
          ? "rgb(var(--error) / 0.6)"
          : "rgb(var(--border))",
        boxShadow: "0 1px 2px rgb(0 0 0 / 0.25)",
        transition: "border-color 200ms",
        cursor: "pointer",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: "rgb(var(--border))", border: 0, width: 6, height: 6 }} />
      <Handle type="source" position={Position.Right} style={{ background: "rgb(var(--border))", border: 0, width: 6, height: 6 }} />

      <div className="flex items-center gap-1.5 px-2.5 pt-2">
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            backgroundColor: statusColor,
            flexShrink: 0,
          }}
        />
        <span
          className="font-mono text-[11px] font-semibold truncate text-fg-primary"
          title={data.host}
        >
          {data.host || "unknown"}
        </span>
        {hasErr && (
          <span
            className="ml-auto text-[9px] font-semibold tabular-nums px-1 rounded"
            style={{
              color: unhealthy ? "rgb(var(--error))" : "rgb(var(--warn))",
              backgroundColor: unhealthy
                ? "rgb(var(--error) / 0.12)"
                : "rgb(var(--warn) / 0.12)",
            }}
            title={`${data.errCount} failed call${data.errCount === 1 ? "" : "s"}`}
          >
            {data.errCount}✕
          </span>
        )}
      </div>

      <div className="px-2.5 pt-1 text-[9px] text-fg-muted tabular-nums flex items-center gap-2">
        <span title="agents on this node">{data.agentCount} ag</span>
        <span title="messages out / in">{formatK(data.msgsOut)}↗ {formatK(data.msgsIn)}↙</span>
        {data.totalTokens > 0 && (
          <span title="total tokens">{formatK(data.totalTokens)} tok</span>
        )}
      </div>

      <div className="mt-auto px-2.5 pb-2" title="share of cluster traffic">
        <div
          style={{
            height: 3,
            borderRadius: 2,
            backgroundColor: "rgb(var(--border-soft))",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${Math.round(Math.min(1, Math.max(0, data.trafficFrac)) * 100)}%`,
              height: "100%",
              borderRadius: 2,
              backgroundColor: unhealthy ? "rgb(var(--error))" : "rgb(var(--accent))",
            }}
          />
        </div>
      </div>
    </div>
  );
}
