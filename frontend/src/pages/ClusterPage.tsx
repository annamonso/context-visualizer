import { useCallback, useEffect, useState } from "react";
import {
  getChronologEvents,
  getClusterTopology,
  type ClusterChronicle,
  type ClusterKeeper,
  type ClusterTopology,
} from "../api";

const REFRESH_MS = 10_000;

/**
 * Cluster tab: where the ChronoLog components run and what they hold.
 *
 * Visor placement is authoritative (it is the endpoint this dashboard is
 * connected to). Keepers are derived from the grapher's drained CSV archive,
 * one card per keeper daemon. Grapher/player placement follows the deploy
 * convention (last allocation node) and is labeled as such.
 */
export default function ClusterPage() {
  const [topo, setTopo] = useState<ClusterTopology | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showStale, setShowStale] = useState(false);
  const [openStory, setOpenStory] = useState<{ chronicle: string; story: string } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const t = await getClusterTopology(showStale);
      setTopo(t);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [showStale]);

  useEffect(() => {
    void refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh]);

  if (error && !topo) {
    return <div className="p-6 text-xs text-fg-muted">Cluster view unavailable: {error}</div>;
  }
  if (!topo) {
    return <div className="p-6 text-xs text-fg-muted">Loading cluster topology…</div>;
  }

  const liveKeepers = topo.keepers.filter((k) => !k.stale);

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-5 py-4 flex items-center gap-3 flex-wrap border-b border-border-soft">
        <span className="text-sm font-semibold">ChronoLog cluster</span>
        <Badge
          tone={topo.connected ? "ok" : "warn"}
          label={topo.connected ? `connected · via ${topo.via}` : "offline (drained archive only)"}
        />
        {topo.allocation.length > 0 && (
          <span className="text-[11px] text-fg-muted font-mono">
            allocation: {topo.allocation.join(", ")}
          </span>
        )}
        <label className="ml-auto flex items-center gap-1.5 text-[11px] text-fg-muted cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showStale}
            onChange={(e) => setShowStale(e.target.checked)}
          />
          show stale keepers
          {!showStale && topo.hidden_stale_keepers > 0 && (
            <span>({topo.hidden_stale_keepers} hidden)</span>
          )}
        </label>
      </div>

      <section className="px-5 py-3 grid grid-cols-2 sm:grid-cols-5 gap-3 border-b border-border-soft">
        <Kpi label="Nodes" value={String(topo.allocation.length || "?")} />
        <Kpi label="Keepers" value={String(liveKeepers.length)} />
        <Kpi label="Chronicles" value={String(new Set(topo.chronicles.map((c) => c.chronicle)).size)} />
        <Kpi label="Scenarios" value={String(topo.scenarios.length)} />
        <Kpi label="Drain window" value={`${topo.drain_window_sec}s`} />
      </section>

      <section className="px-5 py-4 border-b border-border-soft">
        <SectionTitle>Components</SectionTitle>
        <div className="flex gap-3 flex-wrap">
          <ComponentCard
            role="visor"
            host={topo.components.visor.host}
            detail={topo.components.visor.ip ?? undefined}
            note="metadata / connection broker"
          />
          <ComponentCard
            role="grapher"
            host={topo.components.grapher.host}
            note={topo.components.grapher.by_convention ? "drains keepers → archive (by deploy convention)" : "drains keepers → archive"}
          />
          <ComponentCard
            role="player"
            host={topo.components.player.host}
            note={topo.components.player.by_convention ? "serves replays (by deploy convention)" : "serves replays"}
          />
        </div>
      </section>

      <section className="px-5 py-4 border-b border-border-soft">
        <SectionTitle>Keepers (ingest daemons, one per node)</SectionTitle>
        {topo.keepers.length === 0 ? (
          <div className="text-xs text-fg-muted">
            No drained keeper output found under <code className="font-mono">{topo.output_dir}</code>.
            Keepers appear here once their first chunk reaches the grapher archive
            (~{topo.drain_window_sec}s after the first write).
          </div>
        ) : (
          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
            {topo.keepers.map((k) => (
              <KeeperCard key={k.ip} k={k} drainWindowSec={topo.drain_window_sec} />
            ))}
          </div>
        )}
      </section>

      <section className="px-5 py-4">
        <SectionTitle>Chronicles &amp; stories (drained archive)</SectionTitle>
        {topo.chronicles.length === 0 ? (
          <div className="text-xs text-fg-muted">Nothing drained yet.</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-fg-muted">
                <th className="py-1.5 pr-3 font-medium">Chronicle</th>
                <th className="py-1.5 pr-3 font-medium">Story</th>
                <th className="py-1.5 pr-3 font-medium text-right">Events</th>
                <th className="py-1.5 font-medium">Keepers</th>
              </tr>
            </thead>
            <tbody>
              {topo.chronicles.map((c) => (
                <ChronicleRow
                  key={`${c.chronicle}.${c.story}`}
                  c={c}
                  open={openStory?.chronicle === c.chronicle && openStory?.story === c.story}
                  onToggle={() =>
                    setOpenStory((cur) =>
                      cur?.chronicle === c.chronicle && cur?.story === c.story
                        ? null
                        : { chronicle: c.chronicle, story: c.story },
                    )
                  }
                />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-widest text-fg-muted mb-2">{children}</div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-widest text-fg-muted">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function Badge({ tone, label }: { tone: "ok" | "warn"; label: string }) {
  const color = tone === "ok" ? "34 197 94" : "234 179 8";
  return (
    <span
      className="text-[10px] px-2 py-0.5 rounded-full font-medium"
      style={{ color: `rgb(${color})`, backgroundColor: `rgb(${color} / 0.12)` }}
    >
      {label}
    </span>
  );
}

function ComponentCard({
  role,
  host,
  detail,
  note,
}: {
  role: string;
  host: string | null;
  detail?: string;
  note: string;
}) {
  return (
    <div className="rounded border border-border-soft px-3 py-2 min-w-[180px]">
      <div className="text-[9px] uppercase tracking-widest text-fg-muted">{role}</div>
      <div className="font-mono text-sm font-semibold mt-0.5">{host ?? "unknown"}</div>
      {detail && <div className="font-mono text-[10px] text-fg-muted">{detail}</div>}
      <div className="text-[10px] text-fg-muted mt-1">{note}</div>
    </div>
  );
}

function fmtAge(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${(sec / 3600).toFixed(1)}h ago`;
}

function KeeperCard({ k, drainWindowSec }: { k: ClusterKeeper; drainWindowSec: number }) {
  const fresh = k.age_sec != null && k.age_sec <= drainWindowSec * 2;
  return (
    <div
      className="rounded border border-border-soft px-3 py-2"
      style={{ opacity: k.stale ? 0.45 : 1 }}
      title={k.stories.map((s) => `${s.chronicle}/${s.story}: ${s.event_count}`).join("\n")}
    >
      <div className="flex items-center gap-2">
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{
            backgroundColor: k.stale
              ? "rgb(var(--fg-muted))"
              : fresh
                ? "rgb(34 197 94)"
                : "rgb(234 179 8)",
          }}
        />
        <span className="font-mono text-sm font-semibold truncate">{k.host ?? k.ip}</span>
        {!k.in_allocation && (
          <span className="text-[9px] uppercase tracking-widest text-fg-muted">history</span>
        )}
      </div>
      <div className="font-mono text-[10px] text-fg-muted mt-0.5">
        {k.ip}
        {k.ports.length > 0 && ` :${k.ports.join(",")}`}
      </div>
      <div className="text-[11px] tabular-nums mt-1.5 flex gap-3">
        <span>
          <span className="text-fg-muted">events </span>
          {k.event_count}
        </span>
        <span>
          <span className="text-fg-muted">stories </span>
          {k.stories.length}
        </span>
        <span className="text-fg-muted">{fmtAge(k.age_sec)}</span>
      </div>
    </div>
  );
}

function ChronicleRow({
  c,
  open,
  onToggle,
}: {
  c: ClusterChronicle;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr
        className="border-t border-border-soft hover:bg-bg-elevated cursor-pointer"
        onClick={onToggle}
      >
        <td className="py-1.5 pr-3 font-mono">{c.chronicle}</td>
        <td className="py-1.5 pr-3 font-mono">{c.story}</td>
        <td className="py-1.5 pr-3 text-right tabular-nums">{c.event_count}</td>
        <td className="py-1.5 font-mono text-[10px] text-fg-muted">{c.keeper_ips.join(", ")}</td>
      </tr>
      {open && (
        <tr className="border-t border-border-soft">
          <td colSpan={4} className="py-2">
            <StoryEvents chronicle={c.chronicle} story={c.story} />
          </td>
        </tr>
      )}
    </>
  );
}

const EVENT_CAP = 100;

function StoryEvents({ chronicle, story }: { chronicle: string; story: string }) {
  const [events, setEvents] = useState<Record<string, unknown>[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEvents(null);
    getChronologEvents(chronicle, story)
      .then((r) => {
        if (!cancelled) setEvents(r.events);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [chronicle, story]);

  if (error) return <div className="text-xs text-fg-muted px-2">Failed: {error}</div>;
  if (!events) return <div className="text-xs text-fg-muted px-2">Loading events…</div>;
  if (!events.length) return <div className="text-xs text-fg-muted px-2">No drained events yet.</div>;

  return (
    <div className="max-h-72 overflow-auto rounded bg-bg-elevated p-2">
      {events.length > EVENT_CAP && (
        <div className="text-[10px] text-fg-muted mb-1">
          showing first {EVENT_CAP} of {events.length} events
        </div>
      )}
      {events.slice(0, EVENT_CAP).map((ev, i) => (
        <pre
          key={i}
          className="text-[10px] whitespace-pre-wrap break-words border-b border-border-soft last:border-0 py-1 m-0"
        >
          {JSON.stringify(ev)}
        </pre>
      ))}
    </div>
  );
}
