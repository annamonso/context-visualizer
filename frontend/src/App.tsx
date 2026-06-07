import { useEffect, useState } from "react";
import WorkspacePage from "./pages/WorkspacePage";
import ScenarioPage from "./pages/ScenarioPage";
import InteractionsPage from "./pages/InteractionsPage";
import Toast, { type ToastState } from "./components/ui/Toast";
import ErrorBoundary from "./components/ErrorBoundary";
import { getConfig } from "./api";

type Tab = "workspace" | "scenarios" | "interactions";

// Each tab maps to the backend capability that powers it. A tab is only shown
// when an active adapter provides its capability (see /api/config). On a bare
// ChronoLog deployment all three are present; the SPA has no live-runtime tabs,
// so it never depends on chimaera.
const TAB_CAPABILITY: Record<Tab, string> = {
  workspace: "conversations",
  scenarios: "scenarios",
  interactions: "interactions",
};

const ALL_TABS: Tab[] = ["workspace", "scenarios", "interactions"];

function getInitialTab(): Tab {
  const params = new URLSearchParams(window.location.search);
  const v = params.get("tab");
  if (v === "scenarios") return "scenarios";
  if (v === "interactions") return "interactions";
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
      <header className="border-b border-border-soft px-4 py-2 flex items-center gap-3 shrink-0">
        <span className="text-base font-semibold tracking-tight">
          Clio Call Graph
        </span>
        <span className="text-[10px] uppercase tracking-widest text-fg-muted">
          Multi-agent timeline
        </span>
        <nav className="ml-auto flex items-center gap-1 text-xs">
          {enabledTabs.includes("workspace") && (
            <TabButton label="Workspace"    active={tab === "workspace"}    onClick={() => setTab("workspace")} />
          )}
          {enabledTabs.includes("scenarios") && (
            <TabButton label="Scenarios"    active={tab === "scenarios"}    onClick={() => setTab("scenarios")} />
          )}
          {enabledTabs.includes("interactions") && (
            <TabButton label="Interactions" active={tab === "interactions"} onClick={() => setTab("interactions")} />
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
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-2.5 py-1 rounded-md font-medium"
      style={{
        backgroundColor: active ? "rgb(var(--bg-elevated))" : "transparent",
        color: active ? "rgb(var(--fg-primary))" : "rgb(var(--fg-muted))",
        border: active ? "1px solid rgb(var(--border))" : "1px solid transparent",
      }}
    >
      {label}
    </button>
  );
}
