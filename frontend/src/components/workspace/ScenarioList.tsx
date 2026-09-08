import { useEffect, useMemo, useState } from "react";
import { getScenarios } from "../../api";
import type { ScenarioSummary } from "../../types";

interface Props {
  selected: string | null;
  onSelect: (scenarioId: string) => void;
}

export default function ScenarioList({ selected, onSelect }: Props) {
  const [items, setItems] = useState<ScenarioSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getScenarios()
      .then((rows) => { if (!cancelled) { setItems(rows); setError(null); } })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [refreshTick]);

  // Most recent activity first; on big clusters the interesting scenario
  // is almost always the one that just ran.
  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const filtered = q
      ? items.filter((s) => s.scenario_id.toLowerCase().includes(q))
      : items;
    return [...filtered].sort((a, b) =>
      (b.lastEvent || "").localeCompare(a.lastEvent || ""),
    );
  }, [items, filter]);

  if (loading && items.length === 0) {
    return <div className="p-4 text-fg-muted text-xs">Loading scenarios…</div>;
  }
  if (error) return <div className="p-4 text-error text-xs">Error: {error}</div>;
  if (!items.length) {
    return (
      <div className="p-4 text-fg-muted text-xs">
        No multi-agent scenarios recorded yet. Launch agents with
        <code className="mx-1 px-1 rounded bg-bg-elevated">/_scenario/&lt;id&gt;/_session/&lt;sess&gt;/</code>
        and use the <code>inter-agent-relay</code> MCP tool.
      </div>
    );
  }

  return (
    <div className="flex flex-col min-h-0">
      <div className="px-3 py-2 border-b border-border-soft flex items-center gap-1.5">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={`filter ${items.length} scenario${items.length === 1 ? "" : "s"}…`}
          className="flex-1 min-w-0 px-2 py-1 text-xs rounded border border-border-soft bg-canvas placeholder:text-fg-muted"
        />
        <button
          type="button"
          onClick={() => setRefreshTick((t) => t + 1)}
          title="Refresh scenario list"
          className="px-1.5 py-1 text-xs rounded border border-border-soft text-fg-muted hover:text-fg-primary hover:bg-bg-elevated"
        >
          ↻
        </button>
      </div>
      {visible.length === 0 ? (
        <div className="p-4 text-fg-muted text-xs">
          No scenario matches “{filter.trim()}”.
        </div>
      ) : (
        <ul className="flex flex-col overflow-y-auto">
          {visible.map((s) => {
            const active = s.scenario_id === selected;
            return (
              <li key={s.scenario_id}>
                <button
                  type="button"
                  onClick={() => onSelect(s.scenario_id)}
                  className="w-full text-left px-3 py-2 hover:bg-bg-elevated border-b border-border-soft"
                  style={{
                    backgroundColor: active ? "rgb(var(--bg-elevated))" : undefined,
                    boxShadow: active
                      ? "inset 2px 0 0 rgb(var(--accent))"
                      : undefined,
                  }}
                >
                  <div className="font-mono text-xs truncate" title={s.scenario_id}>{s.scenario_id}</div>
                  <div className="text-[10px] text-fg-muted tabular-nums flex items-center gap-2 mt-0.5">
                    <span>{s.agent_count} peer{s.agent_count === 1 ? "" : "s"}</span>
                    <span>·</span>
                    <span>{s.event_count} msg{s.event_count === 1 ? "" : "s"}</span>
                    {s.hosts.length > 0 && (
                      <>
                        <span>·</span>
                        <span className="truncate" title={s.hosts.join(", ")}>
                          {s.hosts.length} host{s.hosts.length === 1 ? "" : "s"}
                        </span>
                      </>
                    )}
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
