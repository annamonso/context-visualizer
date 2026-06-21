import { useEffect, useState } from "react";
import {
  getMemoryContext,
  getMemorySessions,
  type MemoryContext,
  type MemorySession,
} from "../api";

/**
 * Memory tab: watch each agent's working memory (context graph) evolve in real
 * time. Served from the capture spool the agents write to (instant, GIL-safe),
 * polled on a tick — so it never waits on the ~180s ChronoLog drain. Shows the
 * running context-token total, a sparkline of its growth, and the stream of
 * add/evict diff nodes.
 */

const POLL_MS = 2500;

export default function MemoryPage() {
  const [sessions, setSessions] = useState<MemorySession[]>([]);
  const [selected, setSelected] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("mem"),
  );
  const [ctx, setCtx] = useState<MemoryContext | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Poll the agent index. Functional setSelected default avoids a deps cycle.
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      getMemorySessions()
        .then((s) => {
          if (cancelled) return;
          setSessions(s);
          setError(null);
          setSelected((cur) => cur ?? s[0]?.session_id ?? null);
        })
        .catch((e) => {
          if (!cancelled) setError(e instanceof Error ? e.message : String(e));
        });
    void tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Poll the selected agent's context.
  useEffect(() => {
    if (!selected) {
      setCtx(null);
      return;
    }
    let cancelled = false;
    const tick = () =>
      getMemoryContext(selected)
        .then((c) => {
          if (!cancelled) setCtx(c);
        })
        .catch(() => {
          /* transient — keep last good */
        });
    void tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [selected]);

  // Persist selection to the URL so the tab is shareable / survives reload.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (selected) params.set("mem", selected);
    else params.delete("mem");
    const qs = params.toString();
    window.history.replaceState(
      null,
      "",
      qs ? `${window.location.pathname}?${qs}` : window.location.pathname,
    );
  }, [selected]);

  return (
    <div className="flex h-full min-h-0">
      <aside className="w-64 shrink-0 border-r border-border-soft overflow-y-auto">
        <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-fg-muted border-b border-border-soft flex items-center gap-2">
          Agent memory
          <span
            className="inline-block w-1.5 h-1.5 rounded-full animate-pulse"
            style={{ backgroundColor: "rgb(var(--accent))" }}
            title="Live from the capture spool"
          />
        </div>
        {error && sessions.length === 0 && (
          <div className="px-3 py-2 text-[11px] text-fg-muted">{error}</div>
        )}
        {sessions.length === 0 && !error && (
          <div className="px-3 py-3 text-[11px] text-fg-muted">
            No agent memory captured yet.
          </div>
        )}
        {sessions.map((s) => (
          <button
            key={s.session_id}
            type="button"
            onClick={() => setSelected(s.session_id)}
            className="w-full text-left px-3 py-2 border-b border-border-soft hover:bg-bg-elevated"
            style={{
              backgroundColor:
                selected === s.session_id ? "rgb(var(--bg-elevated))" : undefined,
            }}
          >
            <div className="font-mono text-xs font-semibold truncate">{s.role}</div>
            <div className="text-[10px] text-fg-muted truncate">{s.scenario}</div>
            <div className="text-[10px] text-fg-muted tabular-nums mt-0.5">
              {s.live_nodes} nodes · {fmtK(s.total_tokens)} tok · {s.interactions} turns
            </div>
          </button>
        ))}
      </aside>

      <section className="flex-1 min-w-0 flex flex-col">
        {!selected || !ctx ? (
          <div className="h-full flex items-center justify-center text-fg-muted text-sm">
            {selected ? "Loading memory…" : "Pick an agent to watch its memory grow."}
          </div>
        ) : (
          <MemoryDetail ctx={ctx} />
        )}
      </section>
    </div>
  );
}

function MemoryDetail({ ctx }: { ctx: MemoryContext }) {
  const max = Math.max(1, ...ctx.nodes.map((n) => n.running_tokens));
  // Show the most recent slice for the sparkline + the node stream.
  const spark = ctx.nodes.slice(-80);
  const stream = [...ctx.nodes].reverse().slice(0, 200);

  return (
    <div className="flex flex-col h-full min-h-0">
      <header className="px-4 py-3 border-b border-border-soft shrink-0">
        <div className="flex items-center gap-3 flex-wrap">
          <span className="font-mono text-sm font-semibold truncate">{ctx.session_id}</span>
          <span
            className="flex items-center gap-1 text-[10px] font-medium"
            style={{ color: "rgb(var(--accent))" }}
          >
            <span
              className="inline-block w-1.5 h-1.5 rounded-full animate-pulse"
              style={{ backgroundColor: "currentColor" }}
            />
            LIVE
          </span>
          <div className="ml-auto flex items-center gap-5">
            <Stat label="Live context" value={`${fmtK(ctx.live_tokens)} tok`} />
            <Stat label="Nodes" value={String(ctx.nodes.length)} />
            <Stat label="LLM turns" value={String(ctx.interaction_count)} />
          </div>
        </div>
        {/* Running-token sparkline: working-memory size over the sequence. */}
        <div className="mt-3 flex items-end gap-[2px] h-16">
          {spark.map((n, i) => (
            <div
              key={n.sequence_id ?? i}
              className="flex-1 min-w-[2px] rounded-t"
              style={{
                height: `${Math.max(3, (n.running_tokens / max) * 100)}%`,
                backgroundColor:
                  n.op === "evict" ? "rgb(234 179 8)" : "rgb(var(--accent))",
                opacity: 0.5 + 0.5 * (i / Math.max(1, spark.length - 1)),
              }}
              title={`#${n.sequence_id}: ${n.running_tokens} tok`}
            />
          ))}
        </div>
        <div className="text-[10px] text-fg-muted mt-1">
          working-memory tokens over time (newest at right) · peak {fmtK(max)}
        </div>
      </header>

      <div className="flex-1 min-h-0 overflow-y-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0" style={{ backgroundColor: "rgb(var(--bg-canvas))" }}>
            <tr className="text-left text-[10px] uppercase tracking-widest text-fg-muted">
              <th className="py-1.5 px-3 font-medium">#</th>
              <th className="py-1.5 pr-3 font-medium">Op</th>
              <th className="py-1.5 pr-3 font-medium">Context node</th>
              <th className="py-1.5 pr-3 font-medium text-right">Δtok</th>
              <th className="py-1.5 pr-3 font-medium text-right">Live tok</th>
            </tr>
          </thead>
          <tbody>
            {stream.map((n, i) => {
              const evict = n.op === "evict";
              return (
                <tr key={n.sequence_id ?? i} className="border-t border-border-soft">
                  <td className="py-1.5 px-3 tabular-nums text-fg-muted">{n.sequence_id}</td>
                  <td className="py-1.5 pr-3">
                    <span
                      className="px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wide font-medium"
                      style={{
                        color: evict ? "rgb(234 179 8)" : "rgb(34 197 94)",
                        backgroundColor: evict
                          ? "rgb(234 179 8 / 0.12)"
                          : "rgb(34 197 94 / 0.12)",
                      }}
                    >
                      {n.op}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3">
                    <span className="font-mono text-[10px] text-fg-muted">{n.node}</span>
                    {n.summary && <span className="ml-2">{n.summary}</span>}
                  </td>
                  <td
                    className="py-1.5 pr-3 text-right tabular-nums"
                    style={{ color: evict ? "rgb(234 179 8)" : undefined }}
                  >
                    {evict ? "−" : "+"}
                    {n.tokens}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums font-medium">
                    {n.running_tokens}
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

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[9px] uppercase tracking-widest text-fg-muted">{label}</span>
      <span className="text-sm font-semibold tabular-nums leading-tight">{value}</span>
    </div>
  );
}

function fmtK(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
