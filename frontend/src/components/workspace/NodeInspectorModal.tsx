import { useEffect, useMemo, useRef, useState } from "react";
import Modal from "../ui/Modal";
import AgentTrafficView from "./AgentTrafficView";
import ErrorBoundary from "../ErrorBoundary";
import { useConversationData, type ConversationData } from "../../hooks/useConversationData";
import { useHostData } from "../../hooks/useHostData";
import { getMemoryContext, type MemoryContext } from "../../api";
import { formatCost, formatTokens } from "../../lib/format";
import type { ScenarioEdge, ScenarioGraph } from "../../types";

/** What the user clicked in the scenario topology. */
export type InspectTarget =
  | { kind: "agent"; agentId: string } // scoped session id (fetch key)
  | { kind: "host"; host: string };

interface Props {
  target: InspectTarget;
  scenarioId: string;
  /** The already-loaded scenario graph — powers the I/O tab and the agent
   *  roster without refetching. */
  scenarioGraph: ScenarioGraph | null;
  onClose: () => void;
}

type TabId = "activity" | "io" | "context";

/* ── tiny inline SVG sparkline (no chart lib needed at this size) ───────── */

function Sparkline({
  values,
  color,
  width = 88,
  height = 22,
}: {
  values: number[];
  color: string;
  width?: number;
  height?: number;
}) {
  if (values.length < 2) return null;
  const max = Math.max(...values, 1);
  const step = width / (values.length - 1);
  const pts = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - (v / max) * (height - 2) - 1).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={width} height={height} className="shrink-0" aria-hidden>
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

/* ── KPI strip ──────────────────────────────────────────────────────────── */

const fmtK = (n: number) => formatTokens(n);

function percentile(sorted: number[], p: number): number | null {
  if (!sorted.length) return null;
  const idx = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[Math.max(0, idx)];
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-2 py-0.5 text-[11px] rounded-full border font-mono whitespace-nowrap transition-colors"
      style={{
        borderColor: active ? "rgb(var(--accent))" : "rgb(var(--border-soft))",
        color: active ? "rgb(var(--accent-strong))" : "rgb(var(--fg-muted))",
        backgroundColor: active ? "rgb(var(--accent) / 0.1)" : "transparent",
      }}
    >
      {children}
    </button>
  );
}

function Kpi({
  label,
  value,
  danger,
  spark,
}: {
  label: string;
  value: string;
  danger?: boolean;
  spark?: React.ReactNode;
}) {
  return (
    <div className="flex items-end gap-2 shrink-0">
      <div className="flex flex-col">
        <span className="text-[9px] uppercase tracking-widest text-fg-muted">{label}</span>
        <span
          className="text-sm font-semibold tabular-nums leading-tight"
          style={{ color: danger ? "rgb(var(--error))" : "rgb(var(--fg-primary))" }}
        >
          {value}
        </span>
      </div>
      {spark}
    </div>
  );
}

/* ── I/O tab: inter-agent calls touching this agent/host ────────────────── */

function edgeTouches(e: ScenarioEdge, target: InspectTarget): boolean {
  if (target.kind === "host") return e.from_host === target.host || e.to_host === target.host;
  return e.from_session_id === target.agentId || e.to_session_id === target.agentId;
}

function edgeIsOutbound(e: ScenarioEdge, target: InspectTarget): boolean {
  return target.kind === "host"
    ? e.from_host === target.host
    : e.from_session_id === target.agentId;
}

function IoTab({ edges, target }: { edges: ScenarioEdge[]; target: InspectTarget }) {
  const [dir, setDir] = useState<"all" | "in" | "out">("all");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const rows = useMemo(() => {
    let r = edges.filter((e) => edgeTouches(e, target));
    if (dir !== "all") r = r.filter((e) => edgeIsOutbound(e, target) === (dir === "out"));
    if (errorsOnly) r = r.filter((e) => (e.status || "").startsWith("error"));
    // Newest first — this is a traffic feed.
    return [...r].sort((a, b) => (b.ts_start ?? "").localeCompare(a.ts_start ?? ""));
  }, [edges, target, dir, errorsOnly]);

  if (!edges.some((e) => edgeTouches(e, target))) {
    return (
      <div className="h-full flex items-center justify-center text-fg-muted text-sm">
        No inter-agent calls touch this {target.kind} yet.
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="px-4 py-2 border-b border-border-soft flex items-center gap-3 text-[11px] shrink-0">
        <div className="flex items-center gap-0.5 rounded border border-border-soft overflow-hidden">
          {(["all", "in", "out"] as const).map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDir(d)}
              className="px-2 py-1 font-medium"
              style={{
                backgroundColor: dir === d ? "rgb(var(--bg-elevated))" : "transparent",
                color: dir === d ? "rgb(var(--fg-primary))" : "rgb(var(--fg-muted))",
              }}
            >
              {d === "all" ? "All" : d === "in" ? "Inbound" : "Outbound"}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-1.5 text-fg-muted cursor-pointer">
          <input
            type="checkbox"
            checked={errorsOnly}
            onChange={(e) => setErrorsOnly(e.target.checked)}
          />
          errors only
        </label>
        <span className="ml-auto text-fg-muted tabular-nums">{rows.length} calls</span>
      </div>
      <div className="flex-1 overflow-auto min-h-0">
        <table className="w-full text-xs">
          <thead className="sticky top-0" style={{ backgroundColor: "rgb(var(--bg-surface))" }}>
            <tr className="text-left text-[10px] uppercase tracking-wider text-fg-muted">
              <th className="px-4 py-1.5 font-medium">Dir</th>
              <th className="px-2 py-1.5 font-medium">Peer</th>
              <th className="px-2 py-1.5 font-medium">Tool</th>
              <th className="px-2 py-1.5 font-medium">Status</th>
              <th className="px-2 py-1.5 font-medium text-right">Latency</th>
              <th className="px-2 py-1.5 font-medium">Started</th>
              <th className="px-2 py-1.5 font-medium">Payload</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => {
              const out = edgeIsOutbound(e, target);
              const isErr = (e.status || "").startsWith("error");
              const peer =
                target.kind === "host"
                  ? out ? `${e.to_session_id} @ ${e.to_host}` : `${e.from_session_id} @ ${e.from_host}`
                  : out ? e.to_session_id : e.from_session_id;
              const key = e.event_id || e.correlation_id;
              const open = expanded === key;
              return (
                <tr
                  key={key}
                  onClick={() => setExpanded(open ? null : key)}
                  className="border-t border-border-soft cursor-pointer hover:bg-elevate/50 align-top"
                  style={isErr ? { backgroundColor: "rgb(var(--error) / 0.06)" } : undefined}
                >
                  <td className="px-4 py-1.5">
                    <span
                      className="font-semibold"
                      style={{ color: out ? "rgb(var(--accent-strong))" : "rgb(var(--role-subagent))" }}
                      title={out ? "outbound" : "inbound"}
                    >
                      {out ? "→" : "←"}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 font-mono truncate max-w-[220px]" title={peer}>{peer}</td>
                  <td className="px-2 py-1.5 font-mono">{e.tool_name || "call"}</td>
                  <td className="px-2 py-1.5">
                    <span style={{ color: isErr ? "rgb(var(--error))" : "rgb(var(--fg-secondary))" }}>
                      {e.status || (e.ts_done ? "ok" : "in-flight")}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums">
                    {e.latency_ms ? `${e.latency_ms.toFixed(0)}ms` : "—"}
                  </td>
                  <td className="px-2 py-1.5 text-fg-muted tabular-nums whitespace-nowrap">
                    {e.ts_start ? new Date(e.ts_start).toLocaleTimeString() : "—"}
                  </td>
                  <td className="px-2 py-1.5 text-fg-muted max-w-[280px]">
                    <span className={open ? "whitespace-pre-wrap break-all" : "truncate block"}>
                      {e.payload_preview || "—"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Context tab: per-agent working memory (context-graph growth) ───────── */

function ContextTab({
  sessions,
}: {
  /** [display label, fetch key] pairs for the agents in scope. */
  sessions: [string, string][];
}) {
  const [selected, setSelected] = useState<string | null>(sessions[0]?.[1] ?? null);
  const [ctx, setCtx] = useState<MemoryContext | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getMemoryContext(selected)
      .then((c) => { if (!cancelled) setCtx(c); })
      .catch((e) => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [selected]);

  if (!sessions.length) {
    return (
      <div className="h-full flex items-center justify-center text-fg-muted text-sm">
        No agents in scope.
      </div>
    );
  }

  const maxRunning = Math.max(1, ...(ctx?.nodes ?? []).map((n) => n.running_tokens));

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="px-4 py-2 border-b border-border-soft flex items-center gap-3 text-[11px] shrink-0">
        <span className="uppercase tracking-widest text-fg-muted">agent</span>
        <select
          value={selected ?? ""}
          onChange={(e) => setSelected(e.target.value || null)}
          className="px-2 py-1 text-xs rounded-md border border-border bg-surface text-fg-primary max-w-[320px]"
        >
          {sessions.map(([label, key]) => (
            <option key={key} value={key}>{label}</option>
          ))}
        </select>
        {ctx && (
          <span className="ml-auto text-fg-muted tabular-nums">
            {ctx.nodes.length} context ops · {fmtK(ctx.live_tokens)} live tok / {fmtK(ctx.total_tokens)} total
          </span>
        )}
      </div>
      <div className="flex-1 overflow-auto min-h-0 px-4 py-2">
        {loading && <div className="text-fg-muted text-sm">Loading context…</div>}
        {error && <div className="text-sm" style={{ color: "rgb(var(--error))" }}>Error: {error}</div>}
        {!loading && !error && ctx && ctx.nodes.length === 0 && (
          <div className="text-fg-muted text-sm">
            No context-graph events captured for this agent (Path-A spool empty or not yet drained).
          </div>
        )}
        {!loading && !error && ctx && ctx.nodes.length > 0 && (
          <ol className="space-y-1">
            {ctx.nodes.map((n, i) => {
              const evict = n.op === "evict" || n.op === "remove";
              return (
                <li key={`${n.sequence_id ?? i}`} className="flex items-center gap-2 text-xs">
                  <span
                    className="w-12 shrink-0 text-[10px] uppercase tracking-wider font-semibold"
                    style={{ color: evict ? "rgb(var(--error))" : "rgb(var(--role-subagent))" }}
                  >
                    {n.op}
                  </span>
                  <span className="font-mono text-fg-secondary truncate max-w-[260px]" title={n.node}>
                    {n.node}
                  </span>
                  <span className="text-fg-muted truncate flex-1" title={n.summary}>{n.summary}</span>
                  <span className="tabular-nums text-fg-muted shrink-0 w-16 text-right">
                    {evict ? "−" : "+"}{fmtK(n.tokens)}
                  </span>
                  {/* Running-total bar: how full this agent's context is over time. */}
                  <span className="w-28 h-1.5 rounded bg-elevate overflow-hidden shrink-0">
                    <span
                      className="block h-full rounded"
                      style={{
                        width: `${Math.round((n.running_tokens / maxRunning) * 100)}%`,
                        backgroundColor: "rgb(var(--accent))",
                      }}
                    />
                  </span>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </div>
  );
}

/* ── The modal itself ───────────────────────────────────────────────────── */

export default function NodeInspectorModal({
  target,
  scenarioId,
  scenarioGraph,
  onClose,
}: Props) {
  // Host mode drill-in: pick one agent on the node to see its conversation
  // alone (scoped session id), or null for the merged all-agents view.
  const [agentFocus, setAgentFocus] = useState<string | null>(null);

  // All hooks are always called (rules of hooks); only the matching one is
  // given a non-null key — exactly the pattern the old WorkspacePage used.
  const convData = useConversationData(target.kind === "agent" ? target.agentId : null);
  const hostData = useHostData(
    target.kind === "host" ? target.host : null,
    target.kind === "host" ? scenarioId : null,
  );
  const focusData = useConversationData(target.kind === "host" ? agentFocus : null);
  const data: ConversationData =
    target.kind === "host" ? (agentFocus ? focusData : hostData) : convData;

  // What the tabs/KPIs should scope to: the focused agent when drilled in,
  // else the clicked target itself.
  const effTarget: InspectTarget = useMemo(
    () =>
      target.kind === "host" && agentFocus
        ? { kind: "agent", agentId: agentFocus }
        : target,
    [target, agentFocus],
  );

  const kpis = useMemo(() => {
    const errors = data.turns.filter((t) => t.isError).length;
    const lat = data.turns
      .filter((t) => t.hasLatency)
      .map((t) => t.latencyMs)
      .sort((a, b) => a - b);
    return {
      errors,
      p95: percentile(lat, 95),
      tokenSeries: data.turns.map((t) => t.totalTokens ?? 0),
      latencySeries: data.turns.filter((t) => t.hasLatency).map((t) => t.latencyMs),
    };
  }, [data.turns]);

  // Inter-agent edges touching the effective target (I/O tab + header count).
  const ioEdges = scenarioGraph?.edges ?? [];
  const ioCount = useMemo(
    () => ioEdges.filter((e) => edgeTouches(e, effTarget)).length,
    [ioEdges, effTarget],
  );

  // Every agent on the clicked node: [display, fetch-key] pairs. Drives the
  // drill-in chips (host mode) and the Context tab roster.
  const hostAgentPairs = useMemo<[string, string][]>(() => {
    const agents = scenarioGraph?.agents ?? [];
    const inScope =
      target.kind === "host"
        ? agents.filter((a) => (a.host || "unknown") === target.host)
        : agents.filter(
            (a) => (a.scoped_session_id || a.agent_id) === target.agentId,
          );
    if (target.kind === "agent" && inScope.length === 0) {
      // Agent opened without graph context (shouldn't happen, but degrade).
      return [[target.agentId, target.agentId]];
    }
    return inScope.map((a) => [a.session_id, a.scoped_session_id || a.agent_id]);
  }, [scenarioGraph, target]);

  const contextSessions = useMemo<[string, string][]>(
    () =>
      agentFocus
        ? hostAgentPairs.filter(([, key]) => key === agentFocus)
        : hostAgentPairs,
    [hostAgentPairs, agentFocus],
  );

  const [tab, setTab] = useState<TabId>("activity");

  // Drain-window UX: on an in-flight scenario the LLM turns haven't drained
  // out of ChronoLog yet (~180 s), but inter-agent traffic is live. If the
  // Activity tab would be empty and I/O has data, land on I/O instead.
  // Only auto-switch once, and never against an explicit user choice.
  const autoSwitched = useRef(false);
  useEffect(() => {
    if (autoSwitched.current || data.loading) return;
    if (tab === "activity" && data.turns.length === 0 && ioCount > 0) {
      autoSwitched.current = true;
      setTab("io");
    }
  }, [data.loading, data.turns.length, ioCount, tab]);

  const title =
    target.kind === "host" ? (
      <>
        <span className="text-[10px] uppercase tracking-widest text-fg-muted">node</span>
        <span className="font-mono text-sm font-semibold">{target.host}</span>
      </>
    ) : (
      <>
        <span className="text-[10px] uppercase tracking-widest text-fg-muted">agent</span>
        <span className="font-mono text-sm font-semibold truncate max-w-[320px]" title={target.agentId}>
          {target.agentId}
        </span>
      </>
    );

  const drainNotice =
    !data.loading && data.turns.length === 0 && ioCount > 0
      ? "LLM turns appear once ChronoLog's keeper→grapher drain completes — live inter-agent traffic is on the I/O tab."
      : null;

  return (
    <Modal onClose={onClose} ariaLabel={target.kind === "host" ? `Node ${target.host}` : `Agent ${target.agentId}`}>
      {/* Header: identity + scenario + close */}
      <div
        className="px-4 py-2.5 border-b border-border-soft flex items-center gap-3 shrink-0"
        style={{ backgroundColor: "rgb(var(--bg-surface))" }}
      >
        {title}
        <span className="text-[11px] text-fg-muted font-mono truncate">{scenarioId}</span>
        {target.kind === "host" && hostData.truncated > 0 && (
          <span className="text-[10px] text-fg-muted">
            first {hostData.agentCount - hostData.truncated} of {hostData.agentCount} agents shown
          </span>
        )}
        <button
          type="button"
          onClick={onClose}
          className="ml-auto text-fg-muted hover:text-fg-primary text-lg leading-none px-1.5"
          aria-label="Close inspector"
          title="Close (Esc)"
        >
          ×
        </button>
      </div>

      {/* Agent drill-in chips (host mode): merged view or one agent's own
          conversation — the interceptor-style per-conversation inspector. */}
      {target.kind === "host" && hostAgentPairs.length > 0 && (
        <div className="px-4 py-1.5 border-b border-border-soft flex items-center gap-1.5 overflow-x-auto shrink-0">
          <span className="text-[9px] uppercase tracking-widest text-fg-muted mr-1 shrink-0">
            agents
          </span>
          <Chip active={agentFocus == null} onClick={() => setAgentFocus(null)}>
            All ({hostAgentPairs.length})
          </Chip>
          {hostAgentPairs.map(([label, key]) => (
            <Chip key={key} active={agentFocus === key} onClick={() => setAgentFocus(key)}>
              {label}
            </Chip>
          ))}
        </div>
      )}

      {/* KPI strip */}
      <div className="px-4 py-2 border-b border-border-soft flex items-center gap-6 shrink-0 overflow-x-auto">
        {target.kind === "host" && !agentFocus && (
          <Kpi label="Agents" value={String(hostData.agentCount)} />
        )}
        <Kpi label="LLM calls" value={String(data.totals.calls)} />
        <Kpi
          label="Tokens"
          value={fmtK(data.totals.tokens)}
          spark={<Sparkline values={kpis.tokenSeries} color="rgb(var(--accent))" />}
        />
        {data.totals.costUsd > 0 && (
          <Kpi label="Cost" value={formatCost(data.totals.costUsd)} />
        )}
        <Kpi
          label="p95 latency"
          value={kpis.p95 != null ? `${kpis.p95.toFixed(0)}ms` : "—"}
          spark={<Sparkline values={kpis.latencySeries} color="rgb(var(--role-subagent))" />}
        />
        <Kpi label="Errors" value={String(kpis.errors)} danger={kpis.errors > 0} />
        <Kpi label="Agent msgs" value={String(ioCount)} />
      </div>

      {/* Tab bar */}
      <div className="flex border-b border-border-soft shrink-0 px-2">
        {(
          [
            ["activity", "Activity"],
            ["io", "I/O"],
            ["context", "Context"],
          ] as [TabId, string][]
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => { autoSwitched.current = true; setTab(id); }}
            className={`px-3 py-1.5 text-xs border-b-2 transition-colors ${
              tab === id
                ? "border-accent text-fg-primary"
                : "border-transparent text-fg-muted hover:text-fg-secondary"
            }`}
          >
            {label}
          </button>
        ))}
        {drainNotice && (
          <span className="ml-auto self-center text-[10px] text-fg-muted pr-2">{drainNotice}</span>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 min-h-0">
        {tab === "activity" && (
          <ErrorBoundary label="Node activity">
            <AgentTrafficView
              data={data}
              storagePrefix="nodeInspector"
              emptyHint={
                data.loading
                  ? "Loading agent activity…"
                  : data.error
                    ? `Error: ${data.error}`
                    : data.turns.length === 0
                      ? effTarget.kind === "host"
                        ? "No LLM turns recorded on this node yet."
                        : "No LLM turns recorded for this agent yet."
                      : null
              }
            />
          </ErrorBoundary>
        )}
        {tab === "io" && (
          <ErrorBoundary label="Inter-agent I/O">
            <IoTab edges={ioEdges} target={effTarget} />
          </ErrorBoundary>
        )}
        {tab === "context" && (
          <ErrorBoundary label="Agent context">
            <ContextTab sessions={contextSessions} />
          </ErrorBoundary>
        )}
      </div>
    </Modal>
  );
}
