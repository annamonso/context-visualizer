import { useEffect, useMemo, useState } from "react";
import InteractionDrawer from "../components/InteractionDrawer";
import DemoBurstButton from "../components/DemoBurstButton";
import ErrorBoundary from "../components/ErrorBoundary";
import { useLiveInteractions } from "../hooks/useLiveInteractions";
import {
  getIncidents,
  runDoctorScan,
  openDoctorStream,
  getDoctorStatus,
  setDoctorWatch,
  getIncidentMemory,
  type DoctorReport,
  type Incident,
} from "../api";
import type { InteractionSummary, Provider } from "../types";
import { formatLatency, statusTone, type Tone } from "../lib/format";

/**
 * Health tab — the merged Interactions + Doctor view.
 *
 * PrismaDoctor's analysis sits on top of the raw evidence it analyzes:
 *  - top: doctor controls + severity KPIs (Scan now / Activate / Memory)
 *    plus the Generate-traffic button, so "make traffic → watch it get
 *    diagnosed" is one screen;
 *  - left rail: ranked incidents (clustered failure signatures);
 *  - center: the live interaction feed — every LLM call — with an incident
 *    badge on rows the doctor has clustered, plus errors-only / provider
 *    filters. Selecting an incident narrows the feed to its evidence;
 *  - right: shared detail pane — full interaction (Summary/Messages/Tools/
 *    Raw) when a row is selected, otherwise the selected incident's
 *    diagnosis, or the doctor's cross-run incident memory.
 */

// Severity + provider colors come from the theme tokens (theme-aware, one
// source of truth) — never hardcoded hex.
const SEV_COLOR: Record<string, string> = {
  critical: "rgb(var(--sev-critical))",
  error: "rgb(var(--sev-error))",
  warning: "rgb(var(--sev-warning))",
  info: "rgb(var(--sev-info))",
};

const PROVIDER_TOKEN: Record<Provider, string> = {
  openai: "--ok",
  anthropic: "--role-subagent",
  ollama: "--role-orchestrator",
  unknown: "--fg-muted",
};

const TONE_COLOR: Record<Tone, string> = {
  ok: "rgb(var(--ok))",
  warn: "rgb(var(--warn))",
  error: "rgb(var(--error))",
  muted: "rgb(var(--fg-muted))",
};

/** How many feed rows to keep live. */
const FEED_LIMIT = 200;

/** Interaction ids referenced by an incident's evidence samples. */
function sampleIds(inc: Incident): string[] {
  const ids: string[] = [];
  for (const s of inc.samples) {
    const id =
      (s as { id?: unknown }).id ?? (s as { interaction_id?: unknown }).interaction_id;
    if (typeof id === "string") ids.push(id);
  }
  return ids;
}

function rowMatchesIncident(row: InteractionSummary, inc: Incident): boolean {
  if (row.session_id && inc.sessions.includes(row.session_id)) return true;
  return sampleIds(inc).includes(row.id);
}

export default function HealthPage() {
  // ── Doctor state ─────────────────────────────────────────────────────
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [selectedSig, setSelectedSig] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [active, setActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [memoryText, setMemoryText] = useState("");
  const [doctorError, setDoctorError] = useState<string | null>(null);

  // ── Feed state ───────────────────────────────────────────────────────
  const { rows, error: feedError, loading, isLive, refresh } = useLiveInteractions(FEED_LIMIT);
  const [selectedId, setSelectedId] = useState<string | null>(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("iid");
  });
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [provider, setProvider] = useState<string>("");

  // Initial doctor load + live report stream.
  useEffect(() => {
    let cancelled = false;
    getIncidents()
      .then((r) => !cancelled && setReport(r))
      .catch((e) => !cancelled && setDoctorError(String(e)));
    getDoctorStatus()
      .then((s) => !cancelled && setActive(s.running))
      .catch(() => {});
    const src = openDoctorStream(
      (r) => !cancelled && setReport(r),
      () => {},
    );
    return () => {
      cancelled = true;
      src.close();
    };
  }, []);

  const scanNow = async () => {
    setScanning(true);
    setDoctorError(null);
    try {
      setReport(await runDoctorScan());
    } catch (e) {
      setDoctorError(String(e));
    } finally {
      setScanning(false);
    }
  };

  const toggleActive = async () => {
    setBusy(true);
    setDoctorError(null);
    try {
      const { running } = await setDoctorWatch(!active);
      setActive(running);
      if (running) setReport(await getIncidents());
    } catch (e) {
      setDoctorError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const openMemory = async () => {
    const next = !showMemory;
    setShowMemory(next);
    if (next) {
      setSelectedId(null);
      try {
        setMemoryText(await getIncidentMemory());
      } catch (e) {
        setMemoryText(String(e));
      }
    }
  };

  const incidents = report?.incidents ?? [];
  const totals = report?.totals ?? {};
  const selectedIncident = useMemo(
    () => incidents.find((i) => i.signature === selectedSig) ?? null,
    [incidents, selectedSig],
  );

  // Row → incident lookup for the feed badges. Samples win over sessions so
  // the badge points at the most specific cluster.
  const incidentForRow = useMemo(() => {
    const bySample = new Map<string, Incident>();
    const bySession = new Map<string, Incident>();
    for (const inc of incidents) {
      for (const id of sampleIds(inc)) if (!bySample.has(id)) bySample.set(id, inc);
      for (const s of inc.sessions) if (!bySession.has(s)) bySession.set(s, inc);
    }
    return (row: InteractionSummary): Incident | undefined =>
      bySample.get(row.id) ??
      (row.session_id ? bySession.get(row.session_id) : undefined);
  }, [incidents]);

  const providers = useMemo(
    () => Array.from(new Set(rows.map((r) => r.provider))).sort(),
    [rows],
  );

  const filteredRows = useMemo(() => {
    let r = rows;
    if (selectedIncident) r = r.filter((row) => rowMatchesIncident(row, selectedIncident));
    if (errorsOnly) r = r.filter((row) => row.status_code != null && row.status_code >= 400);
    if (provider) r = r.filter((row) => row.provider === provider);
    return r;
  }, [rows, selectedIncident, errorsOnly, provider]);

  const selectInteraction = (id: string) => {
    setSelectedId(id);
    setShowMemory(false);
    const params = new URLSearchParams(window.location.search);
    params.set("iid", id);
    window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
  };

  const closeInteraction = () => {
    setSelectedId(null);
    const params = new URLSearchParams(window.location.search);
    params.delete("iid");
    const qs = params.toString();
    window.history.replaceState(
      null,
      "",
      qs ? `${window.location.pathname}?${qs}` : window.location.pathname,
    );
  };

  const selectIncident = (sig: string | null) => {
    setSelectedSig((prev) => (prev === sig ? null : sig));
    setShowMemory(false);
    // Reveal the diagnosis in the right pane.
    if (selectedId) closeInteraction();
  };

  return (
    <div className="h-full flex flex-col">
      {/* ── Action / KPI strip ─────────────────────────────────────────── */}
      <div
        className="flex items-center gap-4 px-4 py-2.5 border-b border-border-soft shrink-0 overflow-x-auto"
        style={{ backgroundColor: "rgb(var(--bg-surface))" }}
      >
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-sm font-semibold">Health</span>
          <span className="text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">
            live traffic · PrismaDoctor diagnosis
          </span>
        </div>
        <div className="flex items-center gap-3 text-xs shrink-0">
          <Kpi label="critical" value={totals.critical ?? 0} color={SEV_COLOR.critical} />
          <Kpi label="error" value={totals.error ?? 0} color={SEV_COLOR.error} />
          <Kpi label="warning" value={totals.warning ?? 0} color={SEV_COLOR.warning} />
          <Kpi label="incidents" value={totals.incidents ?? 0} />
        </div>
        {report?.scanned && (
          <span className="text-[11px] text-fg-muted whitespace-nowrap">
            scanned {report.scanned.interactions ?? 0} turns · {report.scanned.edges ?? 0} edges
          </span>
        )}
        <div className="ml-auto flex items-center gap-3 shrink-0">
          <DemoBurstButton />
          <button
            type="button"
            onClick={openMemory}
            className="px-3 py-1 rounded text-xs font-medium border border-border-soft hover:bg-canvas"
            style={{ color: showMemory ? "rgb(var(--accent))" : undefined }}
            title="What PrismaDoctor remembers across runs (self-compacting incident log)"
          >
            Memory
          </button>
          <button
            type="button"
            onClick={scanNow}
            disabled={scanning}
            className="px-3 py-1 rounded text-xs font-medium border border-border-soft hover:bg-canvas disabled:opacity-50"
          >
            {scanning ? "Scanning…" : "Scan now"}
          </button>
          <button
            type="button"
            onClick={toggleActive}
            disabled={busy}
            title={active ? "PrismaDoctor is monitoring continuously — click to stop" : "Activate continuous monitoring"}
            className="px-3 py-1 rounded text-xs font-semibold disabled:opacity-50 flex items-center gap-1.5"
            style={{
              backgroundColor: active ? "rgb(var(--ok) / 0.15)" : "rgb(var(--accent))",
              color: active ? "rgb(var(--ok))" : "#fff",
              border: active ? "1px solid rgb(var(--ok))" : "none",
            }}
          >
            <span
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: active ? "rgb(var(--ok))" : "rgba(255,255,255,0.85)" }}
            />
            {busy ? "…" : active ? "Doctor active" : "Activate Doctor"}
          </button>
        </div>
      </div>

      {doctorError && (
        <div className="px-4 py-2 text-xs" style={{ color: SEV_COLOR.error }}>
          {doctorError}
        </div>
      )}

      <div className="flex-1 min-h-0 flex">
        {/* ── Incident rail ──────────────────────────────────────────────── */}
        <aside className="w-72 shrink-0 overflow-auto border-r border-border-soft">
          <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-fg-muted border-b border-border-soft sticky top-0 bg-canvas">
            Incidents
          </div>
          {incidents.length === 0 ? (
            <div className="p-4 text-xs text-fg-muted leading-relaxed">
              No incidents detected. Generate traffic (or click <em>Scan now</em>)
              and failures will cluster here.
            </div>
          ) : (
            incidents.map((inc) => (
              <IncidentRow
                key={inc.signature}
                inc={inc}
                active={selectedSig === inc.signature}
                onClick={() => selectIncident(inc.signature)}
              />
            ))
          )}
        </aside>

        {/* ── Live feed ──────────────────────────────────────────────────── */}
        <section className="flex-1 min-w-0 flex flex-col">
          <div className="px-4 py-2 border-b border-border-soft flex items-center gap-3 text-[11px] shrink-0">
            {selectedIncident ? (
              <button
                type="button"
                onClick={() => selectIncident(null)}
                className="flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-[11px]"
                style={{
                  borderColor: SEV_COLOR[selectedIncident.severity] ?? "#888",
                  color: SEV_COLOR[selectedIncident.severity] ?? "#888",
                }}
                title="Feed narrowed to this incident's evidence — click to clear"
              >
                <SevDot severity={selectedIncident.severity} />
                <span className="truncate max-w-[260px]">{selectedIncident.title}</span>
                <span className="opacity-70">✕</span>
              </button>
            ) : (
              <span className="text-fg-muted">Every LLM call routed through the proxy.</span>
            )}
            <label className="flex items-center gap-1.5 text-fg-muted cursor-pointer ml-2">
              <input
                type="checkbox"
                checked={errorsOnly}
                onChange={(e) => setErrorsOnly(e.target.checked)}
              />
              errors only
            </label>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="px-2 py-0.5 text-[11px] rounded-md border border-border bg-surface text-fg-primary"
            >
              <option value="">all providers</option>
              {providers.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
            <span className="ml-auto tabular-nums text-fg-muted">
              {filteredRows.length}
              {filteredRows.length !== rows.length ? ` / ${rows.length}` : ""} rows
            </span>
            <LiveIndicator isLive={isLive} />
          </div>

          <div className="flex-1 overflow-auto px-4 py-2 min-h-0">
            <ErrorBoundary label="Interactions feed">
              {loading ? (
                <div className="text-fg-secondary text-sm py-8 text-center">Loading…</div>
              ) : feedError ? (
                <div className="text-red-400 text-sm py-4">
                  Error: {feedError}
                  <button onClick={() => void refresh()} className="ml-3 underline">Retry</button>
                </div>
              ) : filteredRows.length === 0 ? (
                <div className="text-fg-secondary text-sm py-8 text-center">
                  {rows.length === 0
                    ? "No interactions yet. Route LLM calls through the proxy (or generate traffic) to see them here."
                    : "No rows match the current filters."}
                </div>
              ) : (
                <FeedTable
                  rows={filteredRows}
                  selectedId={selectedId}
                  onSelect={selectInteraction}
                  incidentForRow={incidentForRow}
                  onIncidentClick={(inc) => selectIncident(inc.signature)}
                />
              )}
            </ErrorBoundary>
          </div>
        </section>

        {/* ── Shared detail pane ─────────────────────────────────────────── */}
        {selectedId ? (
          <ErrorBoundary label="Interaction drawer">
            <InteractionDrawer interactionId={selectedId} onClose={closeInteraction} />
          </ErrorBoundary>
        ) : showMemory ? (
          <aside className="w-96 shrink-0 border-l border-border-soft overflow-auto">
            <MemoryView text={memoryText} />
          </aside>
        ) : selectedIncident ? (
          <aside className="w-96 shrink-0 border-l border-border-soft overflow-auto">
            <IncidentDetail inc={selectedIncident} onOpenSample={selectInteraction} />
          </aside>
        ) : (
          <aside className="w-96 shrink-0 border-l border-border-soft flex items-center justify-center p-6">
            <span className="text-xs text-fg-muted text-center leading-relaxed">
              Click a feed row for the full interaction,
              <br />
              or an incident for its diagnosis.
            </span>
          </aside>
        )}
      </div>
    </div>
  );
}

/* ── feed table ─────────────────────────────────────────────────────────── */

function FeedTable({
  rows,
  selectedId,
  onSelect,
  incidentForRow,
  onIncidentClick,
}: {
  rows: InteractionSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  incidentForRow: (row: InteractionSummary) => Incident | undefined;
  onIncidentClick: (inc: Incident) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b border-border text-fg-secondary text-xs uppercase tracking-wider">
            <th className="text-left py-2 px-3 font-medium">Time</th>
            <th className="text-left py-2 px-3 font-medium">Provider</th>
            <th className="text-left py-2 px-3 font-medium">Model</th>
            <th className="text-center py-2 px-3 font-medium">Status</th>
            <th className="text-right py-2 px-3 font-medium">Latency</th>
            <th className="text-left py-2 px-3 font-medium">Incident</th>
            <th className="text-left py-2 px-3 font-medium">Preview</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isError = row.status_code != null && row.status_code >= 400;
            const inc = incidentForRow(row);
            const providerToken = PROVIDER_TOKEN[row.provider] ?? PROVIDER_TOKEN.unknown;
            return (
              <tr
                key={row.id}
                onClick={() => onSelect(row.id)}
                className={`border-b border-border/50 cursor-pointer transition-colors hover:bg-elevate/50 ${
                  selectedId === row.id ? "bg-surface" : ""
                }`}
                style={isError ? { backgroundColor: "rgb(var(--error) / 0.08)" } : undefined}
              >
                <td className="py-2 px-3 font-mono text-xs text-fg-secondary whitespace-nowrap">
                  {new Date(row.timestamp).toLocaleTimeString()}
                </td>
                <td className="py-2 px-3">
                  <span
                    className="text-xs px-2 py-0.5 rounded font-medium"
                    style={{
                      backgroundColor: `rgb(var(${providerToken}) / 0.15)`,
                      color: `rgb(var(${providerToken}))`,
                    }}
                  >
                    {row.provider}
                  </span>
                </td>
                <td className="py-2 px-3 text-xs text-fg-primary max-w-[140px] truncate">
                  {row.model ?? <span className="text-fg-muted">—</span>}
                </td>
                <td className="py-2 px-3 text-center">
                  <span
                    className="text-xs font-mono"
                    style={{ color: TONE_COLOR[statusTone(row.status_code)] }}
                  >
                    {row.status_code ?? "—"}
                  </span>
                </td>
                <td className="py-2 px-3 text-right font-mono text-xs text-fg-secondary whitespace-nowrap">
                  {formatLatency(row.total_latency_ms)}
                </td>
                <td className="py-2 px-3">
                  {inc && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onIncidentClick(inc);
                      }}
                      className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border whitespace-nowrap"
                      style={{
                        borderColor: SEV_COLOR[inc.severity] ?? "#888",
                        color: SEV_COLOR[inc.severity] ?? "#888",
                      }}
                      title={`Part of incident: ${inc.title} (×${inc.count}) — click for the diagnosis`}
                    >
                      <SevDot severity={inc.severity} />
                      ×{inc.count}
                    </button>
                  )}
                </td>
                <td className="py-2 px-3 text-xs text-fg-secondary max-w-[220px] truncate">
                  {row.response_text_preview}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ── small pieces (ported from the old Doctor page) ─────────────────────── */

function LiveIndicator({ isLive }: { isLive: boolean }) {
  // Tokens are RGB triples, so the color must go through rgb(var(...)) —
  // a bare var(--ok) in a class is not a valid CSS color.
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-fg-secondary shrink-0">
      <span
        className={`h-2 w-2 rounded-full ${isLive ? "animate-pulse" : ""}`}
        style={{ backgroundColor: isLive ? "rgb(var(--ok))" : "rgb(var(--warn))" }}
        aria-hidden="true"
      />
      {isLive ? "Live" : "Polling"}
    </span>
  );
}

function Kpi({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="font-semibold tabular-nums" style={{ color }}>{value}</span>
      <span className="text-fg-muted">{label}</span>
    </span>
  );
}

function SevDot({ severity }: { severity: string }) {
  return (
    <span
      className="w-2 h-2 rounded-full shrink-0 inline-block"
      style={{ backgroundColor: SEV_COLOR[severity] ?? "#888" }}
    />
  );
}

function IncidentRow({
  inc,
  active,
  onClick,
}: {
  inc: Incident;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full text-left px-3 py-2.5 border-b border-border-soft hover:bg-canvas flex flex-col gap-1"
      style={{ backgroundColor: active ? "rgb(var(--bg-surface))" : undefined }}
    >
      <div className="flex items-center gap-2">
        <SevDot severity={inc.severity} />
        <span className="text-xs font-medium truncate">{inc.title}</span>
        <span className="ml-auto text-[11px] tabular-nums text-fg-muted">×{inc.count}</span>
      </div>
      <div className="flex items-center gap-2 text-[10px] text-fg-muted pl-4">
        <span>{inc.hosts.length} host(s)</span>
        {inc.tools.length > 0 && <span>· {inc.tools[0]}</span>}
        {inc.recurring && (
          <span
            className="px-1 rounded"
            style={{ backgroundColor: "rgb(var(--sev-warning) / 0.15)", color: SEV_COLOR.warning }}
          >
            recurring ×{inc.prior_count}
          </span>
        )}
      </div>
    </button>
  );
}

function IncidentDetail({
  inc,
  onOpenSample,
}: {
  inc: Incident;
  onOpenSample: (interactionId: string) => void;
}) {
  const d = inc.diagnosis;
  const evidenceIds = sampleIds(inc);
  return (
    <div className="p-4 flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <SevDot severity={inc.severity} />
        <h2 className="text-sm font-semibold">{inc.title}</h2>
        <span className="ml-auto text-[11px] text-fg-muted">
          {inc.severity} · ×{inc.count}
        </span>
      </div>

      <Section title="Why this is happening">
        <p className="text-sm leading-relaxed">{d?.root_cause}</p>
      </Section>

      <Section title="How to fix it">
        <p className="text-sm leading-relaxed">{d?.remediation}</p>
      </Section>

      <div className="grid grid-cols-2 gap-3">
        <Meta label="Blast radius" value={d?.blast_radius ?? "—"} />
        <Meta
          label="Confidence"
          value={d ? `${Math.round((d.confidence ?? 0) * 100)}% (${d.source})` : "—"}
        />
        <Meta label="Affected hosts" value={inc.hosts.join(", ") || "—"} />
        <Meta label="Scenarios" value={inc.scenarios.join(", ") || "—"} />
      </div>

      {evidenceIds.length > 0 && (
        <Section title="Open evidence">
          <div className="flex flex-wrap gap-1.5">
            {evidenceIds.slice(0, 8).map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => onOpenSample(id)}
                className="px-1.5 py-0.5 rounded border border-border-soft font-mono text-[10px] text-fg-secondary hover:text-fg-primary hover:bg-canvas"
                title="Open this interaction in the drawer"
              >
                {id.length > 14 ? id.slice(0, 14) + "…" : id}
              </button>
            ))}
          </div>
        </Section>
      )}

      <Section title={`Evidence (${inc.samples.length} sample${inc.samples.length === 1 ? "" : "s"})`}>
        <pre className="text-[11px] leading-relaxed overflow-auto p-2 rounded bg-canvas border border-border-soft max-h-72">
          {JSON.stringify(inc.samples, null, 2)}
        </pre>
      </Section>
    </div>
  );
}

function MemoryView({ text }: { text: string }) {
  return (
    <div className="p-4 flex flex-col gap-3">
      <div>
        <h2 className="text-sm font-semibold">Incident memory</h2>
        <p className="text-[11px] text-fg-muted leading-snug mt-1">
          PrismaDoctor's long-term log. Each scan appends one line per incident;
          when it grows past a threshold the oldest entries fold into a
          <em> Digest</em> with running counts, so it stays small while keeping
          history. This is how a repeat failure earns its{" "}
          <span style={{ color: SEV_COLOR.warning }}>recurring ×N</span> badge — the
          doctor recognises a pattern it has seen on earlier runs, even after a
          restart.
        </p>
      </div>
      <pre className="text-[11px] leading-relaxed overflow-auto p-3 rounded bg-canvas border border-border-soft">
        {text || "No incidents recorded yet. Run a scan (or activate the Doctor) and they'll accumulate here."}
      </pre>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-fg-muted mb-1">{title}</div>
      {children}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-widest text-fg-muted">{label}</span>
      <span className="text-xs break-words">{value}</span>
    </div>
  );
}
