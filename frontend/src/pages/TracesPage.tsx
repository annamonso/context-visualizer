import { useEffect, useMemo, useState } from "react";
import {
  getTraceScenarios,
  getTraces,
  type Trace,
  type TraceSpan,
} from "../api";

/**
 * Traces tab — distributed critical-path tracing. Reconstructs cross-host call
 * trees from inter-agent edges and renders each as a waterfall on a shared
 * time axis. Span bars are colored by destination host (this is *distributed*
 * tracing — the hop's target machine matters), the critical path is outlined
 * in red, and a per-hop self-time breakdown answers "where did the time go?".
 * Click any span for its full detail.
 */

import { buildHostHueMap, hostColor } from "../lib/hostHue";
import { formatLatency } from "../lib/format";

const POLL_MS = 4000;
// Critical path / error hops share the severity palette with the Health tab.
const CRIT = "rgb(var(--sev-critical))";
const ERR = "rgb(var(--sev-error))";
// Never-completed (orphaned) spans: amber, hollow — neither success nor error.
const ORPHAN = "rgb(var(--role-orphan, 245 158 11))";

const fmtMs = (ms: number) => formatLatency(ms);

/** ~5 pleasant axis ticks across [0, total] ms. */
function buildTicks(totalMs: number): number[] {
  if (totalMs <= 0) return [];
  const rough = totalMs / 5;
  const pow = 10 ** Math.floor(Math.log10(rough));
  const norm = rough / pow;
  const step = (norm < 1.5 ? 1 : norm < 3.5 ? 2 : norm < 7.5 ? 5 : 10) * pow;
  const ticks: number[] = [];
  for (let t = step; t < totalMs; t += step) ticks.push(t);
  return ticks;
}

function shortHost(host: string): string {
  const parts = host.split(".")[0].split("-");
  return parts.length > 2 ? parts.slice(-2).join("-") : host;
}

export default function TracesPage() {
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [scenario, setScenario] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("trace"),
  );
  const [traces, setTraces] = useState<Trace[]>([]);
  const [selectedRoot, setSelectedRoot] = useState<string | null>(null);
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
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

  useEffect(() => {
    setSelectedSpanId(null);
  }, [selectedRoot, scenario]);

  // Slowest-first: the outlier is what you came here to find.
  const sortedTraces = useMemo(
    () => [...traces].sort((a, b) => b.wall_ms - a.wall_ms),
    [traces],
  );
  const maxWall = Math.max(1, ...sortedTraces.map((t) => t.wall_ms));

  const trace = useMemo(
    () => sortedTraces.find((t) => t.root_id === selectedRoot) ?? sortedTraces[0] ?? null,
    [sortedTraces, selectedRoot],
  );

  const hueMap = useMemo(() => {
    const hosts: string[] = [];
    for (const s of trace?.spans ?? []) {
      hosts.push(s.to_host || "unknown");
      hosts.push(s.from_host || "unknown");
    }
    return buildHostHueMap(hosts);
  }, [trace]);

  const selectedSpan = useMemo(
    () => trace?.spans.find((s) => s.correlation_id === selectedSpanId) ?? null,
    [trace, selectedSpanId],
  );

  const hostsInTrace = useMemo(
    () => new Set((trace?.spans ?? []).map((s) => s.to_host || "unknown")).size,
    [trace],
  );

  return (
    <div className="h-full flex flex-col">
      {/* ── Header ─────────────────────────────────────────────────────── */}
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
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        {scenario && (
          <a
            href={`?tab=scenarios&scenario=${encodeURIComponent(scenario)}`}
            className="text-[11px] text-fg-muted hover:text-fg-primary"
            title="Open this scenario's topology"
          >
            topology ↗
          </a>
        )}
        <span className="ml-auto text-[11px] text-fg-muted">{traces.length} trace(s)</span>
      </div>

      {/* ── Selected-trace KPIs ────────────────────────────────────────── */}
      {trace && (
        <div className="px-4 py-2 border-b border-border-soft flex items-center gap-6 shrink-0 overflow-x-auto">
          <Kpi label="Wall clock" value={fmtMs(trace.wall_ms)} />
          <Kpi
            label="Critical path"
            value={fmtMs(trace.critical_path.total_latency_ms)}
            color={CRIT}
          />
          <Kpi label="Hops" value={String(trace.critical_path.hops)} />
          <Kpi label="Spans" value={String(trace.span_count)} />
          <Kpi label="Hosts" value={String(hostsInTrace)} />
          <Kpi
            label="Errors"
            value={String(trace.error_count)}
            color={trace.error_count > 0 ? ERR : undefined}
          />
          {(trace.open_count ?? 0) > 0 && (
            <Kpi
              label="Never completed"
              value={String(trace.open_count)}
              color={ORPHAN}
            />
          )}
          {trace.critical_path.bottleneck && (
            <div className="flex flex-col shrink-0">
              <span className="text-[9px] uppercase tracking-widest text-fg-muted">
                Bottleneck
              </span>
              <span className="text-sm leading-tight">
                <span className="font-medium">
                  {trace.critical_path.bottleneck.tool_name || "call"}
                </span>
                {" → "}
                <span className="font-medium">
                  {shortHost(trace.critical_path.bottleneck.to_host)}
                </span>{" "}
                <span className="tabular-nums font-semibold" style={{ color: CRIT }}>
                  {fmtMs(trace.critical_path.bottleneck.latency_ms)}
                </span>
              </span>
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="px-4 py-2 text-xs" style={{ color: ERR }}>{error}</div>
      )}

      <div className="flex-1 min-h-0 flex">
        {/* ── Trace rail: compare traces at a glance ─────────────────────── */}
        <div className="w-[280px] shrink-0 overflow-auto border-r border-border-soft">
          <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-fg-muted border-b border-border-soft sticky top-0 bg-canvas">
            Traces · slowest first
          </div>
          {sortedTraces.length === 0 ? (
            <div className="p-4 text-sm text-fg-muted">No traces in this scenario yet.</div>
          ) : (
            sortedTraces.map((t) => (
              <button
                key={t.root_id}
                type="button"
                onClick={() => setSelectedRoot(t.root_id)}
                className="w-full text-left px-3 py-2 border-b border-border-soft hover:bg-canvas"
                style={{
                  backgroundColor:
                    trace?.root_id === t.root_id ? "rgb(var(--bg-surface))" : undefined,
                }}
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold tabular-nums">
                    {fmtMs(t.wall_ms)}
                  </span>
                  <span className="text-[10px] text-fg-muted">
                    {t.span_count} span{t.span_count === 1 ? "" : "s"}
                  </span>
                  {((t.open_count ?? 0) > 0 || t.error_count > 0) && (
                    <span className="ml-auto text-[10px] flex items-center gap-2">
                      {(t.open_count ?? 0) > 0 && (
                        <span className="font-semibold" style={{ color: ORPHAN }}>
                          ⚠ {t.open_count} open
                        </span>
                      )}
                      {t.error_count > 0 && (
                        <span style={{ color: ERR }}>{t.error_count} err</span>
                      )}
                    </span>
                  )}
                </div>
                {/* Relative-duration bar: how this trace compares to the slowest. */}
                <div className="mt-1 h-1.5 rounded bg-elevate overflow-hidden">
                  <div
                    className="h-full rounded"
                    style={{
                      width: `${Math.max(2, (t.wall_ms / maxWall) * 100)}%`,
                      backgroundColor:
                        (t.open_count ?? 0) > 0
                          ? ORPHAN
                          : t.error_count > 0
                            ? ERR
                            : "rgb(var(--accent))",
                    }}
                  />
                </div>
                <div className="text-[10px] text-fg-muted truncate mt-1">
                  {t.spans[0]?.from_session ?? t.root_id.slice(0, 10)}
                </div>
              </button>
            ))
          )}
        </div>

        {/* ── Waterfall ──────────────────────────────────────────────────── */}
        <div className="flex-1 min-w-0 overflow-auto">
          {trace ? (
            <Waterfall
              trace={trace}
              hueMap={hueMap}
              selectedSpanId={selectedSpanId}
              onSelectSpan={(id) =>
                setSelectedSpanId((cur) => (cur === id ? null : id))
              }
            />
          ) : (
            <div className="p-6 text-sm text-fg-muted">Select a trace.</div>
          )}
        </div>

        {/* ── Span detail panel ──────────────────────────────────────────── */}
        {selectedSpan && trace && (
          <SpanDetail
            span={selectedSpan}
            trace={trace}
            hueMap={hueMap}
            onClose={() => setSelectedSpanId(null)}
          />
        )}
      </div>
    </div>
  );
}

function Kpi({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex flex-col shrink-0">
      <span className="text-[9px] uppercase tracking-widest text-fg-muted">{label}</span>
      <span
        className="text-sm font-semibold tabular-nums leading-tight"
        style={{ color: color ?? "rgb(var(--fg-primary))" }}
      >
        {value}
      </span>
    </div>
  );
}

/* ── waterfall ──────────────────────────────────────────────────────────── */

function Waterfall({
  trace,
  hueMap,
  selectedSpanId,
  onSelectSpan,
}: {
  trace: Trace;
  hueMap: Map<string, number>;
  selectedSpanId: string | null;
  onSelectSpan: (correlationId: string) => void;
}) {
  const critIds = new Set(trace.critical_path?.chain?.map((s) => s.correlation_id) ?? []);
  const bottleneckId = trace.critical_path?.bottleneck?.correlation_id ?? null;
  const total = Math.max(1, trace.wall_ms);
  const ticks = buildTicks(total);
  const hosts = Array.from(
    new Set(trace.spans.map((s) => s.to_host || "unknown")),
  ).sort();

  return (
    <div className="p-4 flex flex-col gap-3">
      {/* Where did the time go? — critical-path self-time breakdown */}
      <TimeBreakdown trace={trace} hueMap={hueMap} />

      {/* Legend: host colors + markers */}
      <div className="flex items-center gap-3 text-[10px] text-fg-muted flex-wrap">
        {hosts.map((h) => {
          const hue = hueMap.get(h) ?? 200;
          return (
            <span key={h} className="flex items-center gap-1">
              <span
                className="w-3 h-2 rounded-sm"
                style={{ backgroundColor: hostColor(hue) }}
              />
              {shortHost(h)}
            </span>
          );
        })}
        <span className="flex items-center gap-1">
          <span className="w-3 h-2 rounded-sm border-2" style={{ borderColor: CRIT }} />
          critical path
        </span>
        <span className="flex items-center gap-1">
          <span className="w-3 h-2 rounded-sm" style={{ backgroundColor: ERR }} />
          error
        </span>
        <span className="flex items-center gap-1">
          <span
            className="w-3 h-2 rounded-sm"
            style={{ border: `2px dashed ${ORPHAN}` }}
          />
          never completed
        </span>
        <span className="ml-auto">click a bar for details</span>
      </div>

      {/* Time axis */}
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-fg-muted">
        <span className="w-[200px] shrink-0">hop (tool → host)</span>
        <div className="flex-1 relative h-4">
          <span className="absolute left-0 bottom-0">0</span>
          {ticks.map((t) => (
            <span
              key={t}
              className="absolute bottom-0 -translate-x-1/2 tabular-nums normal-case tracking-normal"
              style={{ left: `${(t / total) * 100}%` }}
            >
              {fmtMs(t)}
            </span>
          ))}
          <span className="absolute right-0 bottom-0 tabular-nums normal-case tracking-normal">
            {fmtMs(total)}
          </span>
        </div>
        <span className="w-[56px] shrink-0 text-right">latency</span>
      </div>

      {/* Span rows */}
      <div className="flex flex-col gap-1">
        {trace.spans.map((s) => (
          <SpanRow
            key={s.correlation_id}
            span={s}
            total={total}
            ticks={ticks}
            hue={hueMap.get(s.to_host || "unknown") ?? 200}
            onCritical={critIds.has(s.correlation_id)}
            isBottleneck={s.correlation_id === bottleneckId}
            selected={s.correlation_id === selectedSpanId}
            onClick={() => onSelectSpan(s.correlation_id)}
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

/** Critical-path stacked bar: one segment per hop, sized by self-time
 *  (exclusive wall-clock), host-colored — the "where did the time go" view. */
function TimeBreakdown({
  trace,
  hueMap,
}: {
  trace: Trace;
  hueMap: Map<string, number>;
}) {
  const chain = trace.critical_path?.chain ?? [];
  if (chain.length < 2) return null;
  const segs = chain.map((s) => ({
    span: s,
    ms: s.self_time_ms ?? s.latency_ms,
  }));
  const sum = Math.max(1, segs.reduce((a, b) => a + b.ms, 0));
  const bottleneckId = trace.critical_path?.bottleneck?.correlation_id ?? null;

  return (
    <div
      className="rounded border border-border-soft p-3"
      style={{ backgroundColor: "rgb(var(--bg-surface))" }}
    >
      <div className="text-[10px] uppercase tracking-widest text-fg-muted mb-2">
        Where did the time go — critical path, self-time per hop
      </div>
      <div className="flex h-6 rounded overflow-hidden border border-border-soft">
        {segs.map(({ span, ms }) => {
          const hue = hueMap.get(span.to_host || "unknown") ?? 200;
          const frac = ms / sum;
          const isBn = span.correlation_id === bottleneckId;
          return (
            <div
              key={span.correlation_id}
              className="h-full flex items-center justify-center overflow-hidden"
              style={{
                width: `${frac * 100}%`,
                backgroundColor: span.is_error ? ERR : hostColor(hue),
                boxShadow: isBn ? `inset 0 0 0 2px ${CRIT}` : undefined,
                minWidth: 3,
              }}
              title={`${span.tool_name || "call"} → ${shortHost(span.to_host)}\nself-time ${fmtMs(ms)} (${Math.round(frac * 100)}%)${isBn ? "\n← bottleneck" : ""}`}
            >
              {frac > 0.12 && (
                <span className="text-[9px] px-1 truncate" style={{ color: "rgb(var(--fg-on-fill) / 0.95)" }}>
                  {shortHost(span.to_host)} · {fmtMs(ms)}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SpanRow({
  span,
  total,
  ticks,
  hue,
  onCritical,
  isBottleneck,
  selected,
  onClick,
}: {
  span: TraceSpan;
  total: number;
  ticks: number[];
  hue: number;
  onCritical: boolean;
  isBottleneck: boolean;
  selected: boolean;
  onClick: () => void;
}) {
  const left = Math.min(99, (span.start_offset_ms / total) * 100);
  const isOpen = Boolean(span.is_open);
  // An open span has no latency to plot — give it a fixed marker width so it
  // is visible, hollow, and clearly not a measured duration.
  const width = isOpen
    ? Math.min(100 - left, 4)
    : Math.max(0.8, Math.min(100 - left, (span.latency_ms / total) * 100));
  const fill = isOpen ? "transparent" : span.is_error ? ERR : hostColor(hue);
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-2 text-[11px] w-full text-left group"
    >
      <div
        className="w-[200px] shrink-0 truncate"
        style={{ paddingLeft: span.depth * 12 }}
      >
        <span className="text-fg-muted">{span.tool_name || "call"} →</span>{" "}
        <span className="font-medium">{shortHost(span.to_host)}</span>
        {isOpen && (
          <span className="ml-1 text-[9px] font-semibold" style={{ color: ORPHAN }}>
            ⚠ never completed
          </span>
        )}
        {isBottleneck && (
          <span className="ml-1 text-[9px] font-semibold" style={{ color: CRIT }}>
            ⏱ bottleneck
          </span>
        )}
      </div>
      <div
        className="flex-1 relative h-4 rounded bg-canvas border group-hover:border-border"
        style={{
          borderColor: selected ? "rgb(var(--accent))" : "rgb(var(--border-soft))",
        }}
      >
        {/* Gridlines aligned with the shared axis ticks. */}
        {ticks.map((t) => (
          <span
            key={t}
            className="absolute top-0 h-full w-px"
            style={{
              left: `${(t / total) * 100}%`,
              backgroundColor: "rgb(var(--border-soft))",
            }}
          />
        ))}
        <div
          className="absolute top-0 h-full rounded flex items-center"
          style={{
            left: `${left}%`,
            width: `${width}%`,
            backgroundColor: fill,
            border: isOpen ? `2px dashed ${ORPHAN}` : undefined,
            boxShadow: onCritical ? `inset 0 0 0 2px ${CRIT}` : undefined,
          }}
          title={`${span.from_session} → ${span.to_session}\n${
            isOpen
              ? "started, never completed — no done frame"
              : `${fmtMs(span.latency_ms)}${span.is_error ? " (error)" : ""}`
          }`}
        >
          {onCritical && <span className="w-1 h-1 rounded-full ml-1" style={{ backgroundColor: "rgb(var(--fg-on-fill) / 0.9)" }} />}
        </div>
      </div>
      <span
        className="w-[56px] shrink-0 text-right tabular-nums"
        style={{ color: isOpen ? ORPHAN : "rgb(var(--fg-muted))" }}
      >
        {isOpen ? "open" : fmtMs(span.latency_ms)}
      </span>
    </button>
  );
}

/* ── span detail panel ──────────────────────────────────────────────────── */

function SpanDetail({
  span,
  trace,
  hueMap,
  onClose,
}: {
  span: TraceSpan;
  trace: Trace;
  hueMap: Map<string, number>;
  onClose: () => void;
}) {
  const onChain = (trace.critical_path?.chain ?? []).some(
    (s) => s.correlation_id === span.correlation_id,
  );
  const isBottleneck =
    trace.critical_path?.bottleneck?.correlation_id === span.correlation_id;
  const parent = span.parent_id
    ? trace.spans.find((s) => s.correlation_id === span.parent_id) ?? null
    : null;
  const hue = hueMap.get(span.to_host || "unknown") ?? 200;

  return (
    <aside className="w-80 shrink-0 border-l border-border-soft overflow-auto">
      <div className="px-3 py-2 border-b border-border-soft flex items-center gap-2 sticky top-0 bg-canvas">
        <span
          className="w-2.5 h-2.5 rounded-sm shrink-0"
          style={
            span.is_open
              ? { border: `2px dashed ${ORPHAN}` }
              : { backgroundColor: span.is_error ? ERR : hostColor(hue) }
          }
        />
        <span className="text-xs font-semibold truncate">
          {span.tool_name || "call"}
        </span>
        {span.is_open && (
          <span className="text-[9px] font-semibold" style={{ color: ORPHAN }}>
            ⚠ never completed
          </span>
        )}
        {isBottleneck && (
          <span className="text-[9px] font-semibold" style={{ color: CRIT }}>
            bottleneck
          </span>
        )}
        <button
          type="button"
          onClick={onClose}
          className="ml-auto text-fg-muted hover:text-fg-primary px-1"
          aria-label="Close span detail"
        >
          ×
        </button>
      </div>
      <div className="p-3 flex flex-col gap-3 text-xs">
        <Field label="From">
          <span className="font-mono break-all">{span.from_session}</span>
          <span className="text-fg-muted"> @ {span.from_host || "unknown"}</span>
        </Field>
        <Field label="To">
          <span className="font-mono break-all">{span.to_session}</span>
          <span className="text-fg-muted"> @ {span.to_host || "unknown"}</span>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Latency">
            <span
              className="tabular-nums font-semibold"
              style={{ color: span.is_open ? ORPHAN : undefined }}
            >
              {span.is_open ? "open — no done frame" : fmtMs(span.latency_ms)}
            </span>
          </Field>
          {span.self_time_ms != null && (
            <Field label="Self-time">
              <span className="tabular-nums font-semibold" style={{ color: CRIT }}>
                {fmtMs(span.self_time_ms)}
              </span>
            </Field>
          )}
          <Field label="Starts at">
            <span className="tabular-nums">+{fmtMs(span.start_offset_ms)}</span>
          </Field>
          <Field label="Depth">
            <span className="tabular-nums">{span.depth}</span>
          </Field>
          <Field label="Status">
            <span
              style={{
                color: span.is_open ? ORPHAN : span.is_error ? ERR : undefined,
              }}
            >
              {span.status || "ok"}
            </span>
          </Field>
          <Field label="Children">
            <span className="tabular-nums">{span.children.length}</span>
          </Field>
        </div>
        <Field label="On critical path">
          <span style={{ color: onChain ? CRIT : undefined }}>
            {onChain ? "yes" : "no"}
          </span>
        </Field>
        {parent && (
          <Field label="Parent hop">
            <span className="font-mono break-all">
              {parent.tool_name || "call"} → {shortHost(parent.to_host)}
            </span>
          </Field>
        )}
        <Field label="Correlation id">
          <span className="font-mono break-all text-fg-muted">{span.correlation_id}</span>
        </Field>
      </div>
    </aside>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[9px] uppercase tracking-widest text-fg-muted">{label}</span>
      <span>{children}</span>
    </div>
  );
}
