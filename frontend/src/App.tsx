import { useEffect, useState } from "react";
import ScenarioPage from "./pages/ScenarioPage";
import HealthPage from "./pages/HealthPage";
import ClusterPage from "./pages/ClusterPage";
import FleetPage from "./pages/FleetPage";
import TracesPage from "./pages/TracesPage";
import Toast, { type ToastState } from "./components/ui/Toast";
import ErrorBoundary from "./components/ErrorBoundary";
import { getConfig, getClusterCapacity, type ClusterCapacity } from "./api";

type Tab =
  | "scenarios"
  | "health"
  | "cluster"
  | "fleet"
  | "traces";

// Each tab maps to the backend capabilities that power it. A tab is shown
// when an active adapter provides ANY of its capabilities (see /api/config).
// On a bare ChronoLog deployment all of them are present. Health is the
// merged Interactions + Doctor view, so either capability lights it up.
const TAB_CAPABILITY: Record<Tab, string[]> = {
  scenarios: ["scenarios"],
  health: ["interactions", "diagnostics"],
  cluster: ["cluster"],
  fleet: ["fleet"],
  traces: ["tracing"],
};

const ALL_TABS: Tab[] = [
  "scenarios",
  "health",
  "cluster",
  "fleet",
  "traces",
];

function getInitialTab(): Tab {
  const params = new URLSearchParams(window.location.search);
  const v = params.get("tab");
  if (v && (ALL_TABS as string[]).includes(v)) return v as Tab;
  // Legacy deep links from before the tab merges. Memory's per-agent view
  // lives on inside the Scenarios node inspector (Context tab).
  if (v === "interactions" || v === "doctor") return "health";
  if (v === "memory" || v === "workspace") return "scenarios";
  return "scenarios";
}

/**
 * Dashboard shell. Scenarios is the home tab; per-node/per-agent traffic
 * (the old Workspace page) now opens as a pop-up inspector from the
 * scenario topology, so there is no separate Workspace tab.
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
        const allowed = ALL_TABS.filter((t) =>
          TAB_CAPABILITY[t].some((c) => caps[c]?.length),
        );
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
    if (tab === "scenarios") params.delete("tab");
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
            Prism Observability
          </span>
          <span className="hidden sm:inline text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">
            multi-agent context visualizer
          </span>
        </div>
        <CapacityBadge cap={capacity} />
        <nav className="flex items-stretch self-stretch text-xs">
          {enabledTabs.includes("scenarios") && (
            <TabButton label="Scenarios"    active={tab === "scenarios"}    onClick={() => setTab("scenarios")} />
          )}
          {enabledTabs.includes("health") && (
            <TabButton label="Health"       active={tab === "health"}       onClick={() => setTab("health")} />
          )}
          {enabledTabs.includes("cluster") && (
            <TabButton label="Cluster"      active={tab === "cluster"}      onClick={() => setTab("cluster")} />
          )}
          {enabledTabs.includes("fleet") && (
            <TabButton label="Fleet"        active={tab === "fleet"}        onClick={() => setTab("fleet")} />
          )}
          {enabledTabs.includes("traces") && (
            <TabButton label="Traces"       active={tab === "traces"}       onClick={() => setTab("traces")} />
          )}
        </nav>
      </header>

      <main className="flex-1 min-h-0">
        <ErrorBoundary
          label={
            tab === "health"
              ? "Health"
              : tab === "cluster"
                ? "Cluster"
                : tab === "fleet"
                  ? "Fleet"
                  : tab === "traces"
                    ? "Traces"
                    : "Scenarios"
          }
        >
          {tab === "health" ? (
            <HealthPage />
          ) : tab === "cluster" ? (
            <ClusterPage />
          ) : tab === "fleet" ? (
            <FleetPage />
          ) : tab === "traces" ? (
            <TracesPage />
          ) : (
            <ScenarioPage />
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
