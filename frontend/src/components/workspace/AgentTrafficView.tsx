import { useEffect, useMemo, useState } from "react";
import ResizableSplit from "./ResizableSplit";
import ViewToolbar, { type Scope, type ViewMode } from "./ViewToolbar";
import AgentFlowGraph from "./AgentFlowGraph";
import TimelineView from "./TimelineView";
import DetailPanel from "./DetailPanel";
import ErrorBoundary from "../ErrorBoundary";
import Toast, { type ToastState } from "../ui/Toast";
import type { ConversationData, NormalizedTurn } from "../../hooks/useConversationData";
import { usePlayhead } from "../../hooks/usePlayhead";

function errorToastMessage(turn: NormalizedTurn): string {
  const raw = turn.error ?? `HTTP ${turn.statusCode ?? "?"}`;
  const trimmed = raw.length > 160 ? raw.slice(0, 160) + "…" : raw;
  return `ERROR: ${trimmed}`;
}

interface Props {
  /** Turn stream + graph to render. Both useConversationData and useHostData
   *  produce this shape, so the view is agnostic to how it was scoped. */
  data: ConversationData;
  /** Shown centered when there is nothing to render (mode-specific copy). */
  emptyHint?: string | null;
  /** Namespace for persisted layout state (split fractions, detail-hidden)
   *  so the node-inspector modal doesn't fight other embeddings. */
  storagePrefix?: string;
}

/**
 * The reusable heart of the old Workspace page: sequential/aggregate agent
 * flow graph + per-turn detail panel + scrubbable timeline, synchronized by
 * a shared playhead. Extracted so the Scenarios node-inspector modal (and
 * any future embedding) can render per-node agent traffic without owning a
 * whole tab. Keyboard shortcuts (space/arrows/Home/End) are registered for
 * the lifetime of the component — mount it only while it should own them.
 */
export default function AgentTrafficView({
  data: rawData,
  emptyHint: emptyHintProp,
  storagePrefix = "workspace",
}: Props) {
  const [viewMode, setViewMode] = useState<ViewMode>("sequential");
  const [scope, setScope] = useState<Scope>("workflow");
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);

  // Auto-select the first lane when scope flips to "session" and nothing is
  // picked yet, or when the selected one disappears (target change).
  useEffect(() => {
    if (scope !== "session") return;
    if (selectedSessionId && rawData.lanes.includes(selectedSessionId)) return;
    setSelectedSessionId(rawData.lanes[0] ?? null);
  }, [scope, rawData.lanes, selectedSessionId]);

  // Scope filter: when "session", restrict turns/lanes to the chosen session.
  const data: ConversationData = useMemo(() => {
    if (scope !== "session" || !selectedSessionId) return rawData;
    const turns = rawData.turns.filter((t) => t.sessionId === selectedSessionId);
    const lanes = rawData.lanes.filter((s) => s === selectedSessionId);
    return { ...rawData, turns, lanes };
  }, [rawData, scope, selectedSessionId]);

  const playhead = usePlayhead(data.turns.length);
  const currentTurn = data.turns[playhead.idx] ?? null;

  // Error toast lives exactly as long as the playhead is on an error turn.
  const errorToast = useMemo<ToastState | null>(
    () =>
      currentTurn?.isError
        ? { type: "error", message: errorToastMessage(currentTurn) }
        : null,
    [currentTurn],
  );

  // Collapsible detail panel. Persist the choice so a full refresh preserves it.
  const detailKey = `${storagePrefix}.detail.hidden`;
  const [detailHidden, setDetailHidden] = useState<boolean>(() => {
    return window.localStorage.getItem(detailKey) === "1";
  });
  useEffect(() => {
    window.localStorage.setItem(detailKey, detailHidden ? "1" : "0");
  }, [detailKey, detailHidden]);

  // Keyboard shortcuts — active while mounted.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLSelectElement
      )
        return;
      if (e.key === " ") { e.preventDefault(); playhead.toggle(); }
      else if (e.key === "ArrowLeft")  { e.preventDefault(); playhead.step(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); playhead.step( 1); }
      else if (e.key === "Home") { e.preventDefault(); playhead.setIdx(0); }
      else if (e.key === "End")  { e.preventDefault(); playhead.setIdx(data.turns.length - 1); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [playhead, data.turns.length]);

  const emptyHint =
    emptyHintProp ??
    (data.loading
      ? "Loading agent activity…"
      : data.error
        ? `Error: ${data.error}`
        : data.turns.length === 0
          ? "No LLM turns recorded yet."
          : null);

  const graphAndDetail = detailHidden ? (
    <div className="relative h-full">
      <ErrorBoundary label="Agent graph">
        <AgentFlowGraph data={data} playhead={playhead} viewMode={viewMode} />
      </ErrorBoundary>
      <button
        type="button"
        onClick={() => setDetailHidden(false)}
        className="absolute top-2 right-2 z-10 text-xs px-2.5 py-1 rounded-md border border-border bg-surface/90 text-fg-secondary hover:text-fg-primary hover:bg-elevate shadow-sm backdrop-blur-sm"
        title="Show detail panel"
      >
        Show details
      </button>
    </div>
  ) : (
    <ResizableSplit
      direction="horizontal"
      initial={0.66}
      min={0.3}
      max={0.85}
      storageKey={`${storagePrefix}.split.horizontal`}
      first={
        <ErrorBoundary label="Agent graph">
          <AgentFlowGraph data={data} playhead={playhead} viewMode={viewMode} />
        </ErrorBoundary>
      }
      second={
        <ErrorBoundary label="Detail panel">
          <DetailPanel
            turn={currentTurn}
            onClose={() => setDetailHidden(true)}
          />
        </ErrorBoundary>
      }
    />
  );

  return (
    <div className="flex flex-col h-full min-h-0">
      <ViewToolbar
        viewMode={viewMode}
        onViewModeChange={setViewMode}
        scope={scope}
        onScopeChange={setScope}
        lanes={rawData.lanes}
        nodeBySession={rawData.nodeBySession}
        selectedSessionId={selectedSessionId}
        onSelectedSessionChange={setSelectedSessionId}
      />

      {emptyHint ? (
        <div className="flex-1 flex items-center justify-center text-fg-muted text-sm">
          {emptyHint}
        </div>
      ) : (
        <div className="flex-1 min-h-0">
          {viewMode === "aggregate" ? (
            graphAndDetail
          ) : (
            <ResizableSplit
              direction="vertical"
              initial={0.62}
              min={0.25}
              max={0.85}
              storageKey={`${storagePrefix}.split.vertical`}
              first={graphAndDetail}
              second={
                <ErrorBoundary label="Timeline">
                  <TimelineView data={data} playhead={playhead} />
                </ErrorBoundary>
              }
            />
          )}
        </div>
      )}

      <Toast
        toast={errorToast}
        onDismiss={() => { /* controlled by playhead */ }}
        autoDismissMs={null}
        hideIcon
        hideClose
      />
    </div>
  );
}
