import { useEffect, useState } from "react";
import WorkspacePage from "./pages/WorkspacePage";
import ScenarioPage from "./pages/ScenarioPage";
import InteractionsPage from "./pages/InteractionsPage";
import ClusterPage from "./pages/ClusterPage";
import Toast, { type ToastState } from "./components/ui/Toast";
import ErrorBoundary from "./components/ErrorBoundary";
import { getConfig } from "./api";

type Tab = "workspace" | "scenarios" | "interactions" | "cluster";

// Each tab maps to the backend capability that powers it. A tab is only shown
// when an active adapter provides its capability (see /api/config). On a bare
// ChronoLog deployment all of them are present.
const TAB_CAPABILITY: Record<Tab, string> = {
  workspace: "conversations",
  scenarios: "scenarios",
  interactions: "interactions",
  cluster: "cluster",
};

const ALL_TABS: Tab[] = ["workspace", "scenarios", "interactions", "cluster"];

function getInitialTab(): Tab {
  const params = new URLSearchParams(window.location.search);
  const v = params.get("tab");
  if (v === "scenarios") return "scenarios";
  if (v === "interactions") return "interactions";
  if (v === "cluster") return "cluster";
  return "workspace";
}

/**
 * Workspace shell. Intentionally trimmed vs. the upstream reference
 * (agent-interception's App.tsx): the raw-log modal, clear-interactions
 * button, and their dependencies (InteractionsTable, InteractionDrawer,
 * ClearModal) were not ported. This view is purely for multi-agent
 * conversation inspection; destructive interactions-log operations stay
 * out of the workspace UI.
 *
 * Theme: handled globally by the Flask base.html navbar. The SPA reads
 * the current palette from `data-theme` on <html>, which the base layout
 * sets before first paint. No per-SPA toggle needed — one source of
 * truth.
 */
export default function App() {
  const [toast, setToast] = useState<ToastState | null>(null);
  const [tab, setTab] = useState<Tab>(getInitialTab);
  // Tabs whose capability an active adapter provides. Defaults to all (optimistic)
  // so the UI works even if /api/config is briefly unreachable.
  const [enabledTabs, setEnabledTabs] = useState<Tab[]>(ALL_TABS);

  useEffect(() => {
    let cancelled = false;
    getConfig()
      .then((cfg) => {
        if (cancelled) return;
        const caps = cfg.capabilities ?? {};
        const allowed = ALL_TABS.filter((t) => caps[TAB_CAPABILITY[t]]?.length);
        if (allowed.length) setEnabledTabs(allowed);
      })
      .catch(() => {
        /* keep optimistic default */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // If the active tab gets gated out, fall back to the first enabled one.
  useEffect(() => {
    if (!enabledTabs.includes(tab) && enabledTabs.length) setTab(enabledTabs[0]);
  }, [enabledTabs, tab]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (tab === "workspace") params.delete("tab");
    else params.set("tab", tab);
    const qs = params.toString();
    const url = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
    window.history.replaceState(null, "", url);
  }, [tab]);

  return (
    <div className="h-screen flex flex-col bg-canvas text-fg-primary">
      <header
        className="border-b border-border-soft px-4 flex items-center gap-3 shrink-0"
        style={{ backgroundColor: "rgb(var(--bg-surface))" }}
      >
        <div className="flex items-center gap-2 py-2.5">
          <span
            className="w-2.5 h-2.5 rounded-sm shrink-0"
            style={{ backgroundColor: "rgb(var(--accent))" }}
          />
          <span className="text-sm font-semibold tracking-tight whitespace-nowrap">
            ChronoLog Observability
          </span>
          <span className="hidden sm:inline text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">
            multi-agent context visualizer
          </span>
        </div>
        <nav className="ml-auto flex items-stretch self-stretch text-xs">
          {enabledTabs.includes("workspace") && (
            <TabButton label="Workspace"    active={tab === "workspace"}    onClick={() => setTab("workspace")} />
          )}
          {enabledTabs.includes("scenarios") && (
            <TabButton label="Scenarios"    active={tab === "scenarios"}    onClick={() => setTab("scenarios")} />
          )}
          {enabledTabs.includes("interactions") && (
            <TabButton label="Interactions" active={tab === "interactions"} onClick={() => setTab("interactions")} />
          )}
          {enabledTabs.includes("cluster") && (
            <TabButton label="Cluster"      active={tab === "cluster"}      onClick={() => setTab("cluster")} />
          )}
        </nav>
      </header>

      <main className="flex-1 min-h-0">
        <ErrorBoundary
          label={
            tab === "scenarios"
              ? "Scenarios"
              : tab === "interactions"
                ? "Interactions"
                : tab === "cluster"
                  ? "Cluster"
                  : "Workspace"
          }
        >
          {tab === "scenarios" ? (
            <ScenarioPage
              onOpenAgent={(agentId) => {
                // Seed ?conv=<agentId> before flipping tabs so WorkspacePage
                // reads the right conversation on mount.
                const params = new URLSearchParams(window.location.search);
                params.set("conv", agentId);
                params.delete("host");
                params.delete("hostScenario");
                params.delete("tab");
                params.delete("scenario");
                const qs = params.toString();
                const url = qs
                  ? `${window.location.pathname}?${qs}`
                  : window.location.pathname;
                window.history.replaceState(null, "", url);
                setTab("workspace");
              }}
              onOpenHost={(host, scenarioId) => {
                // Host mode: WorkspacePage merges every agent on the node.
                const params = new URLSearchParams(window.location.search);
                params.set("host", host);
                params.set("hostScenario", scenarioId);
                params.delete("conv");
                params.delete("tab");
                params.delete("scenario");
                const qs = params.toString();
                const url = qs
                  ? `${window.location.pathname}?${qs}`
                  : window.location.pathname;
                window.history.replaceState(null, "", url);
                setTab("workspace");
              }}
            />
          ) : tab === "interactions" ? (
            <InteractionsPage />
          ) : tab === "cluster" ? (
            <ClusterPage />
          ) : (
            <WorkspacePage />
          )}
        </ErrorBoundary>
      </main>

      <Toast toast={toast} onDismiss={() => setToast(null)} />
    </div>
  );
}

function TabButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  // Underline-style nav: a 2px accent bar pinned to the header's bottom
  // edge marks the active tab — quieter than filled pills at this size.
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-3 font-medium flex items-center hover:text-fg-primary"
      style={{
        color: active ? "rgb(var(--fg-primary))" : "rgb(var(--fg-muted))",
        boxShadow: active ? "inset 0 -2px 0 rgb(var(--accent))" : undefined,
      }}
    >
      {label}
    </button>
  );
}
