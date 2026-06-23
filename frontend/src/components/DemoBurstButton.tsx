import { useEffect, useState } from "react";
import { triggerDemoBurst, getClusterCapacity } from "../api";

/**
 * Configurable "Generate traffic" control. The button fires a burst with the
 * current settings; the ▾ opens a small panel to choose the demo shape
 * (topology pattern, agent count, rounds, error rate, speed). All synthetic,
 * no LLM cost — edges stream into the Live views and the convo is spooled into
 * Workspace/Interactions/Memory.
 */

const PATTERNS = [
  { value: "mesh", label: "Mesh — random peer-to-peer" },
  { value: "star", label: "Star — one coordinator → peers" },
  { value: "pipeline", label: "Pipeline — chain a→b→c" },
  { value: "ring", label: "Ring — a→b→c→a" },
];
const ERRORS = [
  { label: "none", v: 0 },
  { label: "some", v: 0.12 },
  { label: "many", v: 0.35 },
];
const SPEEDS = [
  { label: "slow", v: 2 },
  { label: "normal", v: 1 },
  { label: "fast", v: 0.4 },
];

export default function DemoBurstButton(
  { defaultScenario = "demo-live", onFired }: { defaultScenario?: string; onFired?: (scenario: string) => void },
) {
  const [open, setOpen] = useState(false);
  const [scenario, setScenario] = useState(defaultScenario);
  const [agents, setAgents] = useState(8);
  const [nodes, setNodes] = useState(0);
  const [rounds, setRounds] = useState(30);
  const [pattern, setPattern] = useState("mesh");
  const [errIdx, setErrIdx] = useState(1);
  const [speedIdx, setSpeedIdx] = useState(1);
  const [running, setRunning] = useState(false);
  const [idleMax, setIdleMax] = useState<number | null>(null);

  // When the panel opens, probe real cluster capacity so the Nodes field can be
  // capped to the number of idle nodes ("Max").
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    getClusterCapacity()
      .then((c) => {
        if (cancelled) return;
        if (c.available && typeof c.idle_count === "number") setIdleMax(c.idle_count);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open]);

  const run = async () => {
    const sc = (scenario || "demo-live").trim();
    setRunning(true);
    try {
      await triggerDemoBurst({
        scenario: sc,
        agents,
        nodes,
        rounds,
        interval: SPEEDS[speedIdx].v,
        pattern,
        errRate: ERRORS[errIdx].v,
      });
      onFired?.(sc);
      setOpen(false);
    } catch {
      /* best-effort */
    }
    window.setTimeout(
      () => setRunning(false),
      Math.min(90000, rounds * SPEEDS[speedIdx].v * 1000 + 4000),
    );
  };

  const accent = { backgroundColor: "rgb(var(--accent) / 0.12)", color: "rgb(var(--accent))" };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={running}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium disabled:opacity-60"
        style={accent}
        title="Generate synthetic agent traffic (configurable, no LLM cost)"
      >
        ⚡ {running ? "generating…" : "Generate traffic"} <span className="opacity-70">▾</span>
      </button>

      {open && (
        <div
          className="absolute right-0 mt-1 z-50 w-72 p-3 rounded-md border border-border-soft shadow-lg space-y-2.5 text-[11px]"
          style={{ backgroundColor: "rgb(var(--bg-surface))" }}
        >
          <Row label="Scenario">
            <input
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
              className="w-40 px-1.5 py-0.5 rounded border border-border-soft bg-transparent font-mono"
            />
          </Row>
          <Row label="Pattern">
            <select
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
              className="w-40 px-1 py-0.5 rounded border border-border-soft bg-transparent"
            >
              {PATTERNS.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          </Row>
          <Row label="Agents">
            <input
              type="number" min={2} max={200} value={agents}
              onChange={(e) => setAgents(Math.max(2, Math.min(200, Number(e.target.value) || 2)))}
              className="w-20 px-1.5 py-0.5 rounded border border-border-soft bg-transparent tabular-nums"
            />
          </Row>
          <Row label="Nodes">
            <div className="flex items-center gap-1.5">
              <input
                type="number" min={0} max={idleMax ?? 200}
                value={nodes}
                onChange={(e) => {
                  const cap = idleMax ?? 200;
                  setNodes(Math.max(0, Math.min(cap, Number(e.target.value) || 0)));
                }}
                title="How many hosts to spread the agents across. 0 = use the real SLURM allocation."
                className="w-16 px-1.5 py-0.5 rounded border border-border-soft bg-transparent tabular-nums"
              />
              {idleMax != null && (
                <button
                  type="button"
                  onClick={() => setNodes(idleMax)}
                  className="px-1.5 py-0.5 rounded border border-border-soft text-[10px] hover:bg-canvas"
                  title={`Use all ${idleMax} idle node(s)`}
                >
                  Max {idleMax}
                </button>
              )}
            </div>
          </Row>
          {idleMax != null && (
            <p className="text-[10px] text-fg-muted -mt-1">
              {idleMax} node{idleMax === 1 ? "" : "s"} idle on the cluster right now.
            </p>
          )}
          <Row label="Rounds">
            <input
              type="number" min={1} max={300} value={rounds}
              onChange={(e) => setRounds(Math.max(1, Math.min(300, Number(e.target.value) || 1)))}
              className="w-20 px-1.5 py-0.5 rounded border border-border-soft bg-transparent tabular-nums"
            />
          </Row>
          <Row label="Errors">
            <Segmented options={ERRORS.map((e) => e.label)} idx={errIdx} onPick={setErrIdx} />
          </Row>
          <Row label="Speed">
            <Segmented options={SPEEDS.map((s) => s.label)} idx={speedIdx} onPick={setSpeedIdx} />
          </Row>
          <button
            type="button"
            onClick={run}
            disabled={running}
            className="w-full mt-1 px-2 py-1 rounded-md font-medium disabled:opacity-60"
            style={{ backgroundColor: "rgb(var(--accent))", color: "rgb(var(--bg-canvas))" }}
          >
            {running ? "generating…" : "Run demo"}
          </button>
          <p className="text-[10px] text-fg-muted leading-snug">
            Synthetic, no LLM cost. Edges show live; the convo lands in
            Workspace/Interactions after the ~180s drain.
          </p>
        </div>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-fg-muted">{label}</span>
      {children}
    </div>
  );
}

function Segmented({ options, idx, onPick }: { options: string[]; idx: number; onPick: (i: number) => void }) {
  return (
    <div className="flex rounded border border-border-soft overflow-hidden">
      {options.map((o, i) => (
        <button
          key={o}
          type="button"
          onClick={() => onPick(i)}
          className="px-2 py-0.5"
          style={{
            backgroundColor: i === idx ? "rgb(var(--accent))" : "transparent",
            color: i === idx ? "rgb(var(--bg-canvas))" : "rgb(var(--fg-muted))",
          }}
        >
          {o}
        </button>
      ))}
    </div>
  );
}
