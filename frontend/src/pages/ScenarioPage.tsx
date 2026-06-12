import { useEffect, useMemo, useState } from "react";
import ScenarioFlowGraph, {
  type TopologyView,
} from "../components/workspace/ScenarioFlowGraph";
import ScenarioList from "../components/workspace/ScenarioList";
import ScenarioDetailPanel, {
  type DetailSelection,
} from "../components/workspace/ScenarioDetailPanel";
import ErrorBoundary from "../components/ErrorBoundary";
import { getScenarioGraph } from "../api";
import type { ScenarioGraph } from "../types";

interface Props {
  /**
   * Called when the user clicks an agent node. Caller is responsible for
   * navigating to the Workspace tab with ``conv`` set to ``agentId`` (i.e. the
   * base session id). Keeping navigation in the parent means App.tsx owns
   * the tab state.
   */
  onOpenAgent?: (agentId: string) => void;
  /**
   * Called when the user clicks a host node — navigates to the Workspace in
   * host mode, showing every agent on that node (calls, tools, LLM turns).
   */
  onOpenHost?: (host: string, scenarioId: string) => void;
}

/** Wall-clock seconds a full scenario replay takes at 1× speed. */
const REPLAY_SECONDS = 20;
const REPLAY_TICK_MS = 100;
const REPLAY_SPEEDS = [0.5, 1, 2, 4];

function useScenario(scenarioId: string | null) {
  const [graph, setGraph] = useState<ScenarioGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!scenarioId) {
      setGraph(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getScenarioGraph(scenarioId)
      .then((g) => {
        if (!cancelled) setGraph(g);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scenarioId]);

  return { graph, loading, error };
}

export default function ScenarioPage({ onOpenAgent, onOpenHost }: Props) {
  const [scenarioId, setScenarioId] = useState<string | null>(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("scenario");
  });

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (scenarioId) params.set("scenario", scenarioId);
    else params.delete("scenario");
    const qs = params.toString();
    const url = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
    window.history.replaceState(null, "", url);
  }, [scenarioId]);

  const { graph, loading, error } = useScenario(scenarioId);
  const [view, setView] = useState<TopologyView>("auto");

  // Cluster-level KPIs for the strip under the header — cheap aggregates
  // over the already-loaded graph.
  const clusterStats = useMemo(() => {
    if (!graph) return null;
    const hosts = new Set(graph.agents.map((a) => a.host || "unknown")).size;
    let errors = 0;
    let totalTokens = 0;
    for (const e of graph.edges) {
      if ((e.status || "").startsWith("error")) errors += 1;
    }
    for (const a of graph.agents) totalTokens += a.total_tokens;
    return { hosts, agents: graph.agents.length, errors, totalTokens };
  }, [graph]);

  // Detail-panel selection — reset when the loaded scenario changes.
  const [selection, setSelection] = useState<DetailSelection>(null);
  useEffect(() => {
    setSelection(null);
  }, [scenarioId]);

  // ── Replay: a playhead sweeping the scenario's time window ────────────
  const timeDomain = useMemo(() => {
    if (!graph || graph.edges.length === 0) return null;
    let min = Infinity;
    let max = -Infinity;
    for (const e of graph.edges) {
      const s = e.ts_start ? Date.parse(e.ts_start) : NaN;
      const d = e.ts_done ? Date.parse(e.ts_done) : NaN;
      if (Number.isFinite(s)) {
        min = Math.min(min, s);
        max = Math.max(max, s);
      }
      if (Number.isFinite(d)) max = Math.max(max, d);
    }
    if (!Number.isFinite(min) || max <= min) return null;
    return { t0: min, t1: max };
  }, [graph]);

  const [replayOn, setReplayOn] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [playMs, setPlayMs] = useState<number | null>(null);

  useEffect(() => {
    setReplayOn(false);
    setPlaying(false);
    setPlayMs(null);
  }, [scenarioId]);

  useEffect(() => {
    if (!playing || !timeDomain) return;
    // The whole scenario window maps to REPLAY_SECONDS of wall time at 1×,
    // so a 10-minute run and a 30-second run both replay comfortably.
    const perTick =
      ((timeDomain.t1 - timeDomain.t0) / (REPLAY_SECONDS * 1000)) *
      REPLAY_TICK_MS *
      speed;
    const id = window.setInterval(() => {
      setPlayMs((prev) => {
        const next = (prev ?? timeDomain.t0) + perTick;
        if (next >= timeDomain.t1) {
          setPlaying(false);
          return timeDomain.t1;
        }
        return next;
      });
    }, REPLAY_TICK_MS);
    return () => window.clearInterval(id);
  }, [playing, speed, timeDomain]);

  const startReplay = () => {
    if (!timeDomain) return;
    setReplayOn(true);
    setPlayMs((prev) =>
      prev == null || prev >= timeDomain.t1 ? timeDomain.t0 : prev,
    );
    setPlaying(true);
  };

  const exitReplay = () => {
    setReplayOn(false);
    setPlaying(false);
    setPlayMs(null);
  };

  const handleAgent = (agentId: string) => {
    onOpenAgent?.(agentId);
  };

  const handleHost = (host: string) => {
    if (onOpenHost && scenarioId) onOpenHost(host, scenarioId);
    else setSelection({ kind: "host", host });
  };

  const handleEdgeSelect = (correlationId: string) => {
    if (!graph) return;
    const edge = graph.edges.find((e) => e.correlation_id === correlationId);
    if (edge) setSelection({ kind: "edge", edge });
  };

  const openAgentFromPanel = useMemo(
    () => (agentId: string) => {
      onOpenAgent?.(agentId);
    },
    [onOpenAgent],
  );

  return (
    <div className="flex h-full min-h-0">
      <aside className="w-64 shrink-0 border-r border-border-soft overflow-y-auto">
        <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-fg-muted border-b border-border-soft">
          Scenarios
        </div>
        <ScenarioList selected={scenarioId} onSelect={setScenarioId} />
      </aside>

      <section className="flex-1 min-w-0 flex flex-col">
        <header className="px-4 py-2 border-b border-border-soft flex items-center gap-3 shrink-0">
          <span className="font-mono text-sm truncate">
            {scenarioId ? (
              scenarioId
            ) : (
              <span className="text-fg-muted">No scenario selected</span>
            )}
          </span>
          {graph && (
            <div className="ml-auto flex items-center gap-3">
              {timeDomain && !replayOn && (
                <button
                  type="button"
                  onClick={startReplay}
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium"
                  style={{
                    backgroundColor: "rgb(var(--accent) / 0.12)",
                    color: "rgb(var(--accent))",
                  }}
                  title="Replay the scenario's data flow over time"
                >
                  <svg width="10" height="10" viewBox="0 0 12 12">
                    <polygon points="3,2 10,6 3,10" fill="currentColor" />
                  </svg>
                  Replay
                </button>
              )}
              <span className="text-[10px] uppercase tracking-widest text-fg-muted">
                view
              </span>
              <div className="flex items-center gap-0.5 text-[10px] rounded border border-border-soft overflow-hidden">
                <ViewButton label="Auto"   active={view === "auto"}   onClick={() => setView("auto")} />
                <ViewButton label="Agents" active={view === "detail"} onClick={() => setView("detail")} />
                <ViewButton label="Hosts"  active={view === "hosts"}  onClick={() => setView("hosts")} />
              </div>
            </div>
          )}
        </header>

        {replayOn && timeDomain && (
          <div className="px-4 py-2 border-b border-border-soft flex items-center gap-3 shrink-0">
            <button
              type="button"
              onClick={() => {
                if (playing) setPlaying(false);
                else startReplay();
              }}
              className="w-7 h-7 rounded-md flex items-center justify-center"
              style={{ backgroundColor: "rgb(var(--accent))", color: "rgb(var(--bg-canvas))" }}
              title={playing ? "Pause" : "Play"}
            >
              {playing ? (
                <svg width="12" height="12" viewBox="0 0 12 12"><rect x="3" y="2" width="2" height="8" fill="currentColor" /><rect x="7" y="2" width="2" height="8" fill="currentColor" /></svg>
              ) : (
                <svg width="12" height="12" viewBox="0 0 12 12"><polygon points="3,2 10,6 3,10" fill="currentColor" /></svg>
              )}
            </button>

            <div className="flex items-center gap-1">
              {REPLAY_SPEEDS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSpeed(s)}
                  className="px-2 py-0.5 text-[10px] rounded"
                  style={{
                    backgroundColor: speed === s ? "rgb(var(--accent))" : "rgb(var(--bg-elevated))",
                    color: speed === s ? "rgb(var(--bg-canvas))" : "rgb(var(--fg-muted))",
                  }}
                >
                  {s}×
                </button>
              ))}
            </div>

            <input
              type="range"
              className="flex-1 min-w-0 accent-current"
              style={{ color: "rgb(var(--accent))" }}
              min={timeDomain.t0}
              max={timeDomain.t1}
              step={Math.max(1, Math.floor((timeDomain.t1 - timeDomain.t0) / 500))}
              value={playMs ?? timeDomain.t0}
              onChange={(e) => setPlayMs(Number(e.target.value))}
            />

            <span className="text-[11px] text-fg-muted tabular-nums font-mono shrink-0">
              +{(((playMs ?? timeDomain.t0) - timeDomain.t0) / 1000).toFixed(1)}s
              <span className="opacity-60"> / {((timeDomain.t1 - timeDomain.t0) / 1000).toFixed(1)}s</span>
            </span>

            <button
              type="button"
              onClick={exitReplay}
              className="text-[11px] text-fg-muted hover:text-fg-primary px-1.5"
              title="Exit replay"
            >
              ✕ exit
            </button>
          </div>
        )}

        {scenarioId && clusterStats && graph && (
          <div className="px-4 py-2 border-b border-border-soft flex items-center gap-6 shrink-0 overflow-x-auto">
            <Kpi label="Hosts" value={String(clusterStats.hosts)} />
            <Kpi label="Agents" value={String(clusterStats.agents)} />
            <Kpi label="Messages" value={String(graph.edges.length)} />
            {clusterStats.totalTokens > 0 && (
              <Kpi label="Tokens" value={fmtK(clusterStats.totalTokens)} />
            )}
            <Kpi
              label="Errors"
              value={String(clusterStats.errors)}
              danger={clusterStats.errors > 0}
            />
          </div>
        )}

        <div className="flex-1 min-h-0">
          {!scenarioId && (
            <div className="h-full flex items-center justify-center text-fg-muted text-sm">
              Pick a scenario from the left to see the peer-agent graph.
            </div>
          )}
          {scenarioId && loading && (
            <div className="h-full flex items-center justify-center text-fg-muted text-sm">
              Loading scenario…
            </div>
          )}
          {scenarioId && error && (
            <div className="h-full flex items-center justify-center text-red-500 text-sm">
              Error: {error}
            </div>
          )}
          {scenarioId && !loading && !error && graph && (
            <ErrorBoundary label="Scenario graph">
              <ScenarioFlowGraph
                graph={graph}
                view={view}
                playheadMs={replayOn ? playMs : null}
                onAgentClick={handleAgent}
                onHostClick={handleHost}
                onEdgeSelect={handleEdgeSelect}
              />
            </ErrorBoundary>
          )}
        </div>
      </section>

      {graph && (
        <ScenarioDetailPanel
          graph={graph}
          selection={selection}
          onClose={() => setSelection(null)}
          onOpenAgent={openAgentFromPanel}
        />
      )}
    </div>
  );
}

function fmtK(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function Kpi({
  label,
  value,
  danger,
}: {
  label: string;
  value: string;
  danger?: boolean;
}) {
  return (
    <div className="flex flex-col shrink-0">
      <span className="text-[9px] uppercase tracking-widest text-fg-muted">
        {label}
      </span>
      <span
        className="text-sm font-semibold tabular-nums leading-tight"
        style={{
          color: danger ? "rgb(var(--error))" : "rgb(var(--fg-primary))",
        }}
      >
        {value}
      </span>
    </div>
  );
}

function ViewButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-2 py-1 font-medium"
      style={{
        backgroundColor: active ? "rgb(var(--bg-elevated))" : "transparent",
        color: active ? "rgb(var(--fg-primary))" : "rgb(var(--fg-muted))",
      }}
    >
      {label}
    </button>
  );
}
