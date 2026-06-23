import { useEffect, useMemo, useState } from "react";
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

/**
 * Doctor tab — ChronoDoctor. Detects failures across every ChronoLog story
 * (LLM errors, failed/hung inter-agent calls, retry storms, recoveries),
 * clusters them into ranked incidents, and explains the likely root cause and
 * fix for each. Live-streams the latest report; a manual "Scan now" forces a
 * fresh pass. "Recurring" badges come from the self-compacting incident memory.
 */

const SEV_COLOR: Record<string, string> = {
  critical: "#ef4444",
  error: "#f97316",
  warning: "#eab308",
  info: "#60a5fa",
};

export default function DoctorPage() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [active, setActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [memoryText, setMemoryText] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  // Initial load + live stream.
  useEffect(() => {
    let cancelled = false;
    getIncidents()
      .then((r) => !cancelled && setReport(r))
      .catch((e) => !cancelled && setError(String(e)));
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
    setError(null);
    try {
      setReport(await runDoctorScan());
    } catch (e) {
      setError(String(e));
    } finally {
      setScanning(false);
    }
  };

  // The headline button: activate ChronoDoctor's live background monitoring.
  const toggleActive = async () => {
    setBusy(true);
    setError(null);
    try {
      const { running } = await setDoctorWatch(!active);
      setActive(running);
      if (running) setReport(await getIncidents());
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const openMemory = async () => {
    setShowMemory((v) => !v);
    if (!showMemory) {
      try {
        setMemoryText(await getIncidentMemory());
      } catch (e) {
        setMemoryText(String(e));
      }
    }
  };

  const incidents = report?.incidents ?? [];
  const selectedInc = useMemo(
    () => incidents.find((i) => i.signature === selected) ?? incidents[0] ?? null,
    [incidents, selected],
  );
  const totals = report?.totals ?? {};

  return (
    <div className="h-full flex flex-col">
      {/* Header / KPI strip */}
      <div
        className="flex items-center gap-4 px-4 py-2.5 border-b border-border-soft shrink-0"
        style={{ backgroundColor: "rgb(var(--bg-surface))" }}
      >
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">ChronoDoctor</span>
          <span className="text-[10px] uppercase tracking-widest text-fg-muted">
            error detection &amp; diagnosis
          </span>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <Kpi label="critical" value={totals.critical ?? 0} color={SEV_COLOR.critical} />
          <Kpi label="error" value={totals.error ?? 0} color={SEV_COLOR.error} />
          <Kpi label="warning" value={totals.warning ?? 0} color={SEV_COLOR.warning} />
          <Kpi label="incidents" value={totals.incidents ?? 0} />
        </div>
        <div className="ml-auto flex items-center gap-3">
          {report?.scanned && (
            <span className="text-[11px] text-fg-muted">
              scanned {report.scanned.interactions ?? 0} turns ·{" "}
              {report.scanned.edges ?? 0} edges · {report.scanned.recoveries ?? 0} recoveries
            </span>
          )}
          <button
            type="button"
            onClick={openMemory}
            className="px-3 py-1 rounded text-xs font-medium border border-border-soft hover:bg-canvas"
            style={{ color: showMemory ? "rgb(var(--accent))" : undefined }}
            title="What ChronoDoctor remembers across runs (self-compacting incident log)"
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
            title={active ? "ChronoDoctor is monitoring continuously — click to stop" : "Activate continuous monitoring"}
            className="px-3 py-1 rounded text-xs font-semibold disabled:opacity-50 flex items-center gap-1.5"
            style={{
              backgroundColor: active ? "rgba(34,197,94,0.15)" : "rgb(var(--accent))",
              color: active ? "#22c55e" : "#fff",
              border: active ? "1px solid #22c55e" : "none",
            }}
          >
            <span
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: active ? "#22c55e" : "rgba(255,255,255,0.85)" }}
            />
            {busy ? "…" : active ? "Doctor active" : "Activate Doctor"}
          </button>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2 text-xs" style={{ color: SEV_COLOR.error }}>
          {error}
        </div>
      )}

      <div className="flex-1 min-h-0 flex">
        {/* Incident list */}
        <div className="w-[42%] min-w-[320px] overflow-auto border-r border-border-soft">
          {incidents.length === 0 ? (
            <div className="p-6 text-sm text-fg-muted">
              No incidents detected. ChronoDoctor is watching — run some traffic
              (or click <em>Scan now</em>) and failures will surface here.
            </div>
          ) : (
            incidents.map((inc) => (
              <IncidentRow
                key={inc.signature}
                inc={inc}
                active={selectedInc?.signature === inc.signature}
                onClick={() => setSelected(inc.signature)}
              />
            ))
          )}
        </div>

        {/* Diagnosis detail (or the incident-memory document) */}
        <div className="flex-1 min-w-0 overflow-auto">
          {showMemory ? (
            <MemoryView text={memoryText} />
          ) : selectedInc ? (
            <IncidentDetail inc={selectedInc} />
          ) : (
            <div className="p-6 text-sm text-fg-muted">Select an incident.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function Kpi({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="font-semibold tabular-nums" style={{ color }}>
        {value}
      </span>
      <span className="text-fg-muted">{label}</span>
    </span>
  );
}

function SevDot({ severity }: { severity: string }) {
  return (
    <span
      className="w-2 h-2 rounded-full shrink-0"
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
            style={{ backgroundColor: "rgba(234,179,8,0.15)", color: SEV_COLOR.warning }}
          >
            recurring ×{inc.prior_count}
          </span>
        )}
      </div>
    </button>
  );
}

function IncidentDetail({ inc }: { inc: Incident }) {
  const d = inc.diagnosis;
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
          ChronoDoctor's long-term log. Each scan appends one line per incident;
          when it grows past a threshold the oldest entries fold into a
          <em> Digest</em> with running counts, so it stays small while keeping
          history. This is how a repeat failure earns its <span style={{ color: "#22c55e" }}>recurring ×N</span> badge —
          the doctor recognises a pattern it has seen on earlier runs, even after
          a restart.
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
