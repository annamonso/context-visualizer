import { useEffect, useState } from "react";
import WorkspacePage from "./pages/WorkspacePage";
import ScenarioPage from "./pages/ScenarioPage";
import InteractionsPage from "./pages/InteractionsPage";
import ClusterPage from "./pages/ClusterPage";
import MemoryPage from "./pages/MemoryPage";
import DoctorPage from "./pages/DoctorPage";
import FleetPage from "./pages/FleetPage";
import TracesPage from "./pages/TracesPage";
import Toast, { type ToastState } from "./components/ui/Toast";
import ErrorBoundary from "./components/ErrorBoundary";
import { getConfig, getClusterCapacity, type ClusterCapacity } from "./api";

type Tab =
  | "workspace"
  | "scenarios"
  | "interactions"
  | "cluster"
  | "memory"
  | "doctor"
  | "fleet"
  | "traces";

// Each tab maps to the backend capability that powers it. A tab is only shown
// when an active adapter provides its capability (see /api/config). On a bare
// ChronoLog deployment all of them are present.
const TAB_CAPABILITY: Record<Tab, string> = {
  workspace: "conversations",
  scenarios: "scenarios",
  interactions: "interactions",
  cluster: "cluster",
  memory: "provenance",
  doctor: "diagnostics",
  fleet: "fleet",
  traces: "tracing",
};

const ALL_TABS: Tab[] = [
  "workspace",
  "scenarios",
  "interactions",
  "cluster",
  "fleet",
  "doctor",
  "traces",
  "memory",
];

function getInitialTab(): Tab {
  const params = new URLSearchParams(window.location.search);
  const v = params.get("tab");
  if (v && (ALL_TABS as string[]).includes(v)) return v as Tab;
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
  const [capacity, setCapacity] = useState<ClusterCapacity | null>(null);

  // Probe the underlying SLURM cluster once at load (and refresh every 30s) so
  // the header shows the server we're on: total nodes + how many are free.
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      getClusterCapacity()
        .then((c) => !cancelled && setCapacity(c))
        .catch(() => {});
    void tick();
    const id = setInterval(tick, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

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
        <CapacityBadge cap={capacity} />
        <nav className="flex items-stretch self-stretch text-xs">
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
          {enabledTabs.includes("fleet") && (
            <TabButton label="Fleet"        active={tab === "fleet"}        onClick={() => setTab("fleet")} />
          )}
          {enabledTabs.includes("doctor") && (
            <TabButton label="Doctor"       active={tab === "doctor"}       onClick={() => setTab("doctor")} />
          )}
          {enabledTabs.includes("traces") && (
            <TabButton label="Traces"       active={tab === "traces"}       onClick={() => setTab("traces")} />
          )}
          {enabledTabs.includes("memory") && (
            <TabButton label="Memory"       active={tab === "memory"}       onClick={() => setTab("memory")} />
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
                  : tab === "fleet"
                    ? "Fleet"
                    : tab === "doctor"
                      ? "Doctor"
                      : tab === "traces"
                        ? "Traces"
                        : tab === "memory"
                          ? "Memory"
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
          ) : tab === "fleet" ? (
            <FleetPage />
          ) : tab === "doctor" ? (
            <DoctorPage />
          ) : tab === "traces" ? (
            <TracesPage />
          ) : tab === "memory" ? (
            <MemoryPage />
          ) : (
            <WorkspacePage />
          )}
        </ErrorBoundary>
      </main>

      <Toast toast={toast} onDismiss={() => setToast(null)} />
    </div>
  );
}

function CapacityBadge({ cap }: { cap: ClusterCapacity | null }) {
  // Compact "what server am I on" readout: total nodes + idle/busy/down split.
  // Hidden when there's no SLURM client (e.g. the offline gateway demo).
  if (!cap || !cap.available) return null;
  const idle = cap.idle_count ?? 0;
  const busy = cap.busy_count ?? 0;
  const down = cap.down_count ?? 0;
  return (
    <div
      className="ml-auto mr-3 flex items-center gap-2 text-[11px] px-2 py-1 rounded-md"
      style={{ backgroundColor: "rgb(var(--bg-surface))" }}
      title={`SLURM cluster: ${cap.total} nodes (${idle} idle, ${busy} busy, ${down} down)\nidle: ${(cap.idle ?? []).join(", ") || "none"}`}
    >
      <span className="text-fg-muted">{cap.total} nodes</span>
      <Dot color="#22c55e" /> <span className="tabular-nums">{idle} idle</span>
      <Dot color="#eab308" /> <span className="tabular-nums">{busy} busy</span>
      {down > 0 && (<><Dot color="#ef4444" /> <span className="tabular-nums">{down} down</span></>)}
    </div>
  );
}

function Dot({ color }: { color: string }) {
  return <span className="w-2 h-2 rounded-full inline-block" style={{ backgroundColor: color }} />;
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
