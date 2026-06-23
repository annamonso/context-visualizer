"""Store for InterAgentMessage events.

ChronoLog is the substrate. There is a single backend selection rule, decided
per call by whether a live ChronoLog backend is available:

  ====================  ==========================  =========================
  Mode                  When                        Writes / Reads
  ====================  ==========================  =========================
  ChronoLog (normal)    live backend present        ChronoLog / ChronoLog
  Local (offline demo)  CHRONOLOG_OFFLINE=1         JSONL / JSONL
  ====================  ==========================  =========================

The migration-era ``DTP_CHRONOLOG_BACKEND / DUAL_WRITE / READS`` flags are gone
— this is a ChronoLog-native plugin, not a JSONL deployment being cut over. The
JSONL path now exists only so the demo Workspace can capture and replay locally
in ``CHRONOLOG_OFFLINE=1`` mode (where ``get_backend()`` returns ``None``); it
is never a silent fallback when a real visor was expected.

Other invariants:
  - Two-phase records ("start"/"done") stitched by ``correlation_id``.
  - Records are immutable; "updates" emit a second record.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional


log = logging.getLogger(__name__)


_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).strftime(_ISO)


@dataclass
class InterAgentMessage:
    """One inter-agent call event.

    A single logical call emits two of these: one with ``phase='start'`` when
    the caller MCP tool fires, and one with ``phase='done'`` when the remote
    replies. Adapters stitch them on the shared ``correlation_id``.
    """

    event_id: str
    scenario_id: str
    correlation_id: str
    phase: str  # "start" | "done"
    from_host: str
    from_session: str
    to_host: str
    to_session: str
    ts: str  # ISO 8601 UTC
    kind: str = "mcp_call"  # mcp_call | tool_invoke | direct_message
    tool_name: str = ""
    payload_digest: str = ""
    payload_preview: str = ""
    status: str = ""  # "ok" | "error:<msg>" | "" while in-flight
    latency_ms: float = 0.0
    ingest_ns: int = 0  # server-side monotonic stamp; survives collector relays
    # Optional explicit parent link: the correlation_id of the call this one was
    # made *while servicing*. Lets the tracer build exact call trees; when absent
    # the tracer infers parents from session + time nesting. Additive, default "".
    parent_correlation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "InterAgentMessage":
        # Tolerate missing fields on pre-existing records.
        return cls(
            event_id=str(d.get("event_id") or uuid.uuid4().hex),
            scenario_id=str(d.get("scenario_id") or ""),
            correlation_id=str(d.get("correlation_id") or ""),
            phase=str(d.get("phase") or "done"),
            from_host=str(d.get("from_host") or ""),
            from_session=str(d.get("from_session") or ""),
            to_host=str(d.get("to_host") or ""),
            to_session=str(d.get("to_session") or ""),
            ts=str(d.get("ts") or _iso_now()),
            kind=str(d.get("kind") or "mcp_call"),
            tool_name=str(d.get("tool_name") or ""),
            payload_digest=str(d.get("payload_digest") or ""),
            payload_preview=str(d.get("payload_preview") or ""),
            status=str(d.get("status") or ""),
            latency_ms=float(d.get("latency_ms") or 0.0),
            ingest_ns=int(d.get("ingest_ns") or 0),
            parent_correlation_id=str(d.get("parent_correlation_id") or ""),
        )


def _safe_scenario(sid: str) -> str:
    """Sanitize scenario_id into a safe filename fragment."""
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in sid)[:120]


class InterAgentStore:
    """Scenario store with pluggable backend (JSONL and/or ChronoLog).

    Backend selection is per-call via env flags so a single process can serve
    different routes from different backends during cutover. The constructor
    accepts an optional `chronolog_backend` injection point so tests can run
    the ChronoLog code path against an in-process fake.
    """

    def __init__(
        self,
        root: Optional[os.PathLike] = None,
        *,
        chronolog_backend: Optional[object] = None,
    ) -> None:
        if root is None:
            root = Path(os.environ.get("DTP_STATE_DIR", "")) if os.environ.get("DTP_STATE_DIR") else Path.home() / ".dt_provenance"
        self._root = Path(root) / "inter_agent"
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # `_chronolog_override` is set by tests; production discovers via
        # the chronolog package singleton at call time so flag changes take
        # effect without restarting the store.
        self._chronolog_override = chronolog_backend

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    def _chronolog(self):
        """Return the live ChronoLog backend, or ``None`` in offline demo mode.

        ``get_backend()`` is the single source of truth: it returns a connected
        backend in normal operation and ``None`` only under ``CHRONOLOG_OFFLINE=1``
        (see ``backend.client``). The lazy import keeps importing this module
        from pulling in ``py_chronolog_client``.
        """
        if self._chronolog_override is not None:
            return self._chronolog_override
        from ...backend.client import get_backend
        return get_backend()

    # ------------------------------------------------------------------
    # JSONL backend (legacy)
    # ------------------------------------------------------------------

    def _path_for(self, scenario_id: str) -> Path:
        return self._root / f"{_safe_scenario(scenario_id)}.jsonl"

    def _append_jsonl(self, msg: "InterAgentMessage") -> None:
        path = self._path_for(msg.scenario_id)
        line = json.dumps(msg.to_dict(), separators=(",", ":")) + "\n"
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    def _read_jsonl(self, scenario_id: str) -> List["InterAgentMessage"]:
        path = self._path_for(scenario_id)
        if not path.is_file():
            return []
        out: List[InterAgentMessage] = []
        with self._lock:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        out.append(InterAgentMessage.from_dict(json.loads(raw)))
                    except Exception:
                        continue
        return out

    def _list_scenarios_jsonl(self) -> List[str]:
        out: List[str] = []
        if not self._root.is_dir():
            return out
        for p in self._root.iterdir():
            if p.is_file() and p.suffix == ".jsonl":
                out.append(p.stem)
        out.sort()
        return out

    # ------------------------------------------------------------------
    # ChronoLog backend
    # ------------------------------------------------------------------

    def _append_chronolog(self, msg: "InterAgentMessage", backend) -> None:
        from ...backend import constants
        ch = constants.inter_agent_chronicle(msg.scenario_id)
        backend.append_event(ch, constants.INTER_AGENT_STORY_EDGES, msg.to_dict())
        # Index the scenario so list_scenarios() has something to enumerate
        # once the writer-side index Story drains. Idempotent — dup events
        # are dedup'd on read.
        try:
            backend.index_add(constants.INDEX_STORY_SCENARIOS, msg.scenario_id)
        except Exception as exc:  # never fail an ingest on the index update
            log.warning("chronolog index_add failed: %s", exc)

    def _read_chronolog(self, scenario_id: str, backend) -> List["InterAgentMessage"]:
        from ...backend import constants
        ch = constants.inter_agent_chronicle(scenario_id)
        try:
            events = backend.replay_events(ch, constants.INTER_AGENT_STORY_EDGES)
        except RuntimeError as exc:
            log.warning("chronolog replay failed for %s: %s", scenario_id, exc)
            return []
        out: List[InterAgentMessage] = []
        for ev in events:
            # Strip the internal `_chronolog_*` annotation keys before
            # rehydrating, so InterAgentMessage.from_dict isn't surprised.
            ev = {k: v for k, v in ev.items() if not k.startswith("_chronolog_")}
            try:
                out.append(InterAgentMessage.from_dict(ev))
            except Exception:
                continue
        return out

    def _list_scenarios_chronolog(self, backend) -> List[str]:
        from ...backend import constants
        try:
            ids = backend.index_list(constants.INDEX_STORY_SCENARIOS)
        except RuntimeError as exc:
            log.warning("chronolog index_list failed: %s", exc)
            return []
        ids.sort()
        return ids

    # ------------------------------------------------------------------
    # Public API (unchanged signatures)
    # ------------------------------------------------------------------

    def append(self, msg: InterAgentMessage, *, skip_chronolog: bool = False) -> None:
        """Append a single event; safe under concurrent writers.

        Normal operation writes to ChronoLog and propagates any append failure
        up so the Flask ingest returns 5xx — silent loss is worse than visible
        loss. In ``CHRONOLOG_OFFLINE=1`` demo mode (no live backend) the event
        is persisted to the local JSONL store instead so the Workspace can
        replay it without a visor.

        ``skip_chronolog=True`` marks events relayed by a per-node collector
        that already wrote them to ChronoLog — the store must not write a
        second copy. In offline mode the JSONL write still happens (there is
        no other durable copy on this host).
        """
        if not msg.scenario_id:
            raise ValueError("scenario_id is required")

        backend = self._chronolog()
        # Always keep a local hot copy. The live view cannot wait out the
        # ~180s keeper->grapher drain, and a ReplayStory on an un-drained story
        # blocks for *minutes* holding the GIL — so reads must be served from a
        # local, instant, GIL-safe store. ChronoLog stays the durable cold path.
        self._append_jsonl(msg)
        if backend is None or skip_chronolog:
            # Offline: JSONL is the only copy. Collector-forwarded events
            # (skip_chronolog) are already durable in ChronoLog via the
            # node-local keeper, so we keep only the hot copy and don't
            # double-write — but we DO keep it, so the dashboard can serve the
            # full picture instantly instead of waiting on the drain.
            return
        # ChronoLog append failures bubble up to the ingest API as 5xx.
        self._append_chronolog(msg, backend)

    def append_many(self, msgs: Iterable[InterAgentMessage], *, skip_chronolog: bool = False) -> None:
        """Append a batch; same semantics per event as :meth:`append`."""
        for msg in msgs:
            self.append(msg, skip_chronolog=skip_chronolog)

    def list_scenarios(self) -> List[str]:
        """Return scenario ids that have at least one recorded event.

        Served from ChronoLog in normal operation; from the local JSONL store
        in offline demo mode. The ChronoLog result may lag writes by up to one
        chunk acceptance window (~180s) until the index Story is drained — the
        first call within that window can look empty even if writes succeeded.
        """
        backend = self._chronolog()
        if backend is None:
            return self._list_scenarios_jsonl()
        # Union of the local hot store (instant; the current run) and the
        # ChronoLog index (CSV-backed, GIL-safe; historical runs). Either may
        # lag the other within the drain window, so merge both.
        merged = set(self._list_scenarios_jsonl())
        merged.update(self._list_scenarios_chronolog(backend))
        return sorted(merged)

    def list_local_scenarios(self) -> List[str]:
        """Scenario ids with a local hot-store (JSONL) copy.

        These are the current run's live scenarios — readable instantly and
        without ANY ChronoLog replay, so callers that poll frequently (the
        Cluster comms view) stay completely off the GIL-blocking path.
        """
        return self._list_scenarios_jsonl()

    def read(self, scenario_id: str) -> List[InterAgentMessage]:
        """Read all events for one scenario, in chronological order.

        Hot local copy first: it is instant and GIL-safe, and holds everything
        this dashboard ingested (it survives a restart — the JSONL lives on the
        shared state dir). ChronoLog is consulted only for scenarios absent
        locally (historical runs, or events this process never saw), where the
        story is long drained so the replay returns promptly (or hits the CSV
        archive fallback). This keeps the live view off the GIL-blocking
        ReplayStory path for anything currently in flight.
        """
        local = self._read_jsonl(scenario_id)
        if local:
            return local
        backend = self._chronolog()
        if backend is None:
            return []
        return self._read_chronolog(scenario_id, backend)

    def read_stitched(self, scenario_id: str) -> List[dict]:
        """Return one merged event per correlation_id.

        A completed call merges start + done (picking ts_start, ts_done,
        status, latency_ms from the done record). An in-flight call keeps
        just the start event with ts_done=None.
        """
        events = self.read(scenario_id)
        bucket: dict[str, dict] = {}
        order: list[str] = []
        for ev in events:
            cid = ev.correlation_id or ev.event_id
            slot = bucket.get(cid)
            if slot is None:
                slot = {
                    "correlation_id": cid,
                    "scenario_id": ev.scenario_id,
                    "from_host": ev.from_host,
                    "from_session": ev.from_session,
                    "to_host": ev.to_host,
                    "to_session": ev.to_session,
                    "kind": ev.kind,
                    "tool_name": ev.tool_name,
                    "payload_digest": ev.payload_digest,
                    "payload_preview": ev.payload_preview,
                    "parent_correlation_id": ev.parent_correlation_id,
                    "ts_start": None,
                    "ts_done": None,
                    "status": "",
                    "latency_ms": 0.0,
                }
                bucket[cid] = slot
                order.append(cid)
            if ev.phase == "start":
                slot["ts_start"] = ev.ts
                # Start events seed routing / payload info if not yet set.
                for key in ("from_host", "from_session", "to_host",
                            "to_session", "kind", "tool_name",
                            "payload_digest", "payload_preview",
                            "parent_correlation_id"):
                    if not slot[key]:
                        slot[key] = getattr(ev, key)
            else:  # done
                slot["ts_done"] = ev.ts
                slot["status"] = ev.status or "ok"
                slot["latency_ms"] = ev.latency_ms
                # Done events are authoritative for payload/digest if provided.
                for key in ("payload_digest", "payload_preview"):
                    v = getattr(ev, key)
                    if v:
                        slot[key] = v
        return [bucket[cid] for cid in order]


_DEFAULT_STORE: Optional[InterAgentStore] = None
_DEFAULT_LOCK = threading.Lock()


def get_store() -> InterAgentStore:
    """Process-wide singleton. First call creates the directory."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = InterAgentStore()
    return _DEFAULT_STORE


def new_message(
    *,
    scenario_id: str,
    from_host: str,
    from_session: str,
    to_host: str,
    to_session: str,
    phase: str,
    correlation_id: str = "",
    kind: str = "mcp_call",
    tool_name: str = "",
    payload_preview: str = "",
    payload_digest: str = "",
    status: str = "",
    latency_ms: float = 0.0,
    ingest_ns: int = 0,
    parent_correlation_id: str = "",
) -> InterAgentMessage:
    """Construct a new event with auto-generated event_id + timestamp."""
    return InterAgentMessage(
        event_id=uuid.uuid4().hex,
        scenario_id=scenario_id,
        correlation_id=correlation_id or uuid.uuid4().hex,
        phase=phase,
        from_host=from_host,
        from_session=from_session,
        to_host=to_host,
        to_session=to_session,
        ts=_iso_now(),
        kind=kind,
        tool_name=tool_name,
        payload_digest=payload_digest,
        payload_preview=payload_preview,
        status=status,
        latency_ms=latency_ms,
        ingest_ns=ingest_ns,
        parent_correlation_id=parent_correlation_id,
    )
