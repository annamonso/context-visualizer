import { useEffect, useMemo, useState } from "react";
import {
  getTraceScenarios,
  getTraces,
  type Trace,
  type TraceSpan,
} from "../api";

/**
 * Traces tab — distributed critical-path tracing. Reconstructs cross-host call
 * trees from inter-agent edges and renders each as a waterfall. The critical
 * path (the slowest root-to-leaf chain) is highlighted and the bottleneck hop
 * called out — the answer to "why is this scenario slow across N nodes?" that
 * per-node logs can't give.
 */

const POLL_MS = 4000;
const CRIT = "#ef4444";
const ERR = "#f97316";

export default function TracesPage() {
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [scenario, setScenario] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("trace"),
  );
  const [traces, setTraces] = useState<Trace[]>([]);
  const [selectedRoot, setSelectedRoot] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getTraceScenarios()
      .then((s) => {
        if (cancelled) return;
        setScenarios(s);
        setScenario((cur) => cur ?? s[0] ?? null);
      })
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!scenario) return;
    let cancelled = false;
    const tick = () =>
      getTraces(scenario)
        .then((t) => !cancelled && (setTraces(t), setError(null)))
        .catch((e) => !cancelled && setError(String(e)));
    void tick();
    const id = setInterval(tick, POLL_MS);
    const params = new URLSearchParams(window.location.search);
    params.set("trace", scenario);
    window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [scenario]);

  const trace = useMemo(
    () => traces.find((t) => t.root_id === selectedRoot) ?? traces[0] ?? null,
    [traces, selectedRoot],
  );

  return (
    <div className="h-full flex flex-col">
      <div
        className="flex items-center gap-3 px-4 py-2.5 border-b border-border-soft shrink-0"
        style={{ backgroundColor: "rgb(var(--bg-surface))" }}
      >
        <span className="text-sm font-semibold">Critical-Path Tracing</span>
        <select
          value={scenario ?? ""}
          onChange={(e) => setScenario(e.target.value)}
          className="bg-canvas border border-border-soft rounded px-2 py-1 text-xs ml-2"
        >
          {scenarios.length === 0 && <option value="">(no scenarios)</option>}
          {scenarios.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <span className="ml-auto text-[11px] text-fg-muted">{traces.length} trace(s)</span>
      </div>

      {error && (
        <div className="px-4 py-2 text-xs" style={{ color: ERR }}>
          {error}
        </div>
      )}

      <div className="flex-1 min-h-0 flex">
        {/* Trace list */}
        <div className="w-[260px] shrink-0 overflow-auto border-r border-border-soft">
          {traces.length === 0 ? (
            <div className="p-4 text-sm text-fg-muted">No traces in this scenario yet.</div>
          ) : (
            traces.map((t) => (
              <button
                key={t.root_id}
                type="button"
                onClick={() => setSelectedRoot(t.root_id)}
                className="w-full text-left px-3 py-2 border-b border-border-soft hover:bg-canvas"
                style={{
                  backgroundColor: trace?.root_id === t.root_id ? "rgb(var(--bg-surface))" : undefined,
                }}
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium tabular-nums">
                    {(t.wall_ms / 1000).toFixed(2)}s
                  </span>
                  <span className="text-[10px] text-fg-muted">{t.span_count} spans</span>
                  {t.error_count > 0 && (
                    <span className="ml-auto text-[10px]" style={{ color: ERR }}>
                      {t.error_count} err
                    </span>
                  )}
                </div>
                <div className="text-[10px] text-fg-muted truncate mt-0.5">
                  {t.spans[0]?.from_session ?? t.root_id.slice(0, 10)}
                </div>
              </button>
            ))
          )}
        </div>

        {/* Waterfall */}
        <div className="flex-1 min-w-0 overflow-auto">
          {trace ? <Waterfall trace={trace} /> : <div className="p-6 text-sm text-fg-muted">Select a trace.</div>}
        </div>
      </div>
    </div>
  );
}

function Waterfall({ trace }: { trace: Trace }) {
  const critIds = new Set(trace.critical_path?.chain?.map((s) => s.correlation_id) ?? []);
  const bottleneck = trace.critical_path?.bottleneck ?? null;
  const total = Math.max(1, trace.wall_ms);

  return (
    <div className="p-4 flex flex-col gap-4">
      {/* What am I looking at? */}
      <div className="text-[11px] text-fg-muted leading-snug">
        Each row is one agent-to-agent call; bars are placed on a shared timeline
        and sized by duration. Indentation = call depth (a child call made while
        its parent was running). The <span style={{ color: CRIT }}>red</span> bars
        are the critical path — the chain that determines wall-clock; the marked
        hop is the bottleneck to optimise.
      </div>
      <div className="flex items-center gap-4 text-[10px] text-fg-muted">
        <Legend color="rgb(var(--accent))" label="call" />
        <Legend color={CRIT} label="critical path" />
        <Legend color={ERR} label="error" />
      </div>

      {/* Critical path callout */}
      <div className="rounded border border-border-soft p-3" style={{ backgroundColor: "rgb(var(--bg-surface))" }}>
        <div className="text-[10px] uppercase tracking-widest text-fg-muted mb-1">Critical path</div>
        <div className="text-sm">
          <span className="font-semibold tabular-nums" style={{ color: CRIT }}>
            {(trace.critical_path.total_latency_ms / 1000).toFixed(2)}s
          </span>{" "}
          across {trace.critical_path.hops} hop(s)
          {bottleneck && (
            <>
              {" · bottleneck "}
              <span className="font-medium">{bottleneck.tool_name || "call"}</span>
              {" → "}
              <span className="font-medium">{bottleneck.to_host}</span>{" "}
              <span className="tabular-nums" style={{ color: CRIT }}>
                ({(bottleneck.latency_ms / 1000).toFixed(2)}s)
              </span>
            </>
          )}
        </div>
      </div>

      {/* Span rows */}
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-fg-muted mt-1">
        <span className="w-[200px] shrink-0">hop (tool → host)</span>
        <span className="flex-1">timeline →</span>
        <span className="w-[56px] shrink-0 text-right">latency</span>
      </div>
      <div className="flex flex-col gap-1">
        {trace.spans.map((s) => (
          <SpanRow
            key={s.correlation_id}
            span={s}
            total={total}
            onCritical={critIds.has(s.correlation_id)}
          />
        ))}
      </div>
      {trace.span_count <= 1 && (
        <div className="text-[11px] text-fg-muted mt-1">
          This trace has a single hop, so there's no chain to analyse. Multi-hop
          traces (an agent that calls a peer which calls another) show the tree
          structure and a meaningful critical path — try a{" "}
          <span className="font-medium">pipeline</span> or{" "}
          <span className="font-medium">star</span> traffic pattern, or run the
          real agents.
        </div>
      )}
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="w-3 h-2 rounded-sm" style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}

function SpanRow({
  span,
  total,
  onCritical,
}: {
  span: TraceSpan;
  total: number;
  onCritical: boolean;
}) {
  const left = Math.min(99, (span.start_offset_ms / total) * 100);
  const width = Math.max(0.8, Math.min(100 - left, (span.latency_ms / total) * 100));
  const color = span.is_error ? ERR : onCritical ? CRIT : "rgb(var(--accent))";
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <div className="w-[200px] shrink-0 truncate" style={{ paddingLeft: span.depth * 12 }}>
        <span className="text-fg-muted">{span.tool_name || "call"} →</span>{" "}
        <span className="font-medium">{shortHost(span.to_host)}</span>
      </div>
      <div className="flex-1 relative h-4 rounded bg-canvas border border-border-soft">
        <div
          className="absolute top-0 h-full rounded flex items-center"
          style={{ left: `${left}%`, width: `${width}%`, backgroundColor: color }}
          title={`${span.from_session} → ${span.to_session}\n${span.latency_ms.toFixed(0)}ms${span.is_error ? " (error)" : ""}`}
        >
          {onCritical && <span className="w-1 h-1 rounded-full bg-white/90 ml-1" />}
        </div>
      </div>
      <span className="w-[56px] shrink-0 text-right tabular-nums text-fg-muted">
        {(span.latency_ms / 1000).toFixed(2)}s
      </span>
    </div>
  );
}

function shortHost(host: string): string {
  const parts = host.split(".")[0].split("-");
  return parts.length > 2 ? parts.slice(-2).join("-") : host;
}
