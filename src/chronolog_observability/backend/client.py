"""Thin Python facade over `py_chronolog_client`.

Design choices:
  * Lazy import — `py_chronolog_client` is only imported the first time a
    caller asks for the backend. Tests and disconnected dev environments are
    not impacted by ChronoLog not being installed.
  * Process-wide singleton — `get_backend()`. Flask's worker model means we
    re-use one writer-mode `Client` for hot-path appends and one reader-mode
    `Client` for `ReplayStory`. Both are guarded by a single `RLock`.
  * StoryHandle cache — `AcquireStory` is per-call expensive; we keep handles
    keyed by `(chronicle, story)` for the lifetime of the process.
  * Idempotent setup — `CreateChronicle` is called on first use of a chronicle
    and `CL_ERR_CHRONICLE_EXISTS (-6)` is treated as success.
  * The visor IP is resolved at startup using the same `getent` strategy that
    the deploy-time `run_agent.sh` uses (hostnames return `HG_PROTONOSUPPORT`
    from Mercury — see the deploy notes).

Plugin change from the original context-visualizer backend:
  ChronoLog is now the *required substrate*, not an opt-in backend. The old
  `DTP_CHRONOLOG_BACKEND / DUAL_WRITE / READS` on/off flags are gone —
  `get_backend()` always returns a live backend, except in `CHRONOLOG_OFFLINE=1`
  demo mode (CSV replay handled by the adapter), where it returns `None`.

Environment variables are read under the `CHRONOLOG_*` prefix, with the legacy
`DTP_CHRONOLOG_*` names accepted as a fallback so existing deployment scripts
keep working.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger(__name__)


# ChronoLog client return codes — only the ones we actually branch on.
# Full table: ChronoLog/client/include/client_errcode.h
CL_SUCCESS                  = 0
CL_ERR_UNKNOWN              = -1
CL_ERR_INVALID_ARG          = -2
CL_ERR_NOT_EXIST            = -3
CL_ERR_ACQUIRED             = -4
CL_ERR_NOT_ACQUIRED         = -5
CL_ERR_CHRONICLE_EXISTS     = -6
CL_ERR_NO_KEEPERS           = -7
CL_ERR_NO_CONNECTION        = -8
CL_ERR_NOT_AUTHORIZED       = -9
CL_ERR_NO_PLAYERS           = -10
CL_ERR_NOT_READER_MODE      = -11
CL_ERR_QUERY_TIMED_OUT      = -12


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among `names` (new CHRONOLOG_* preferred, legacy
    DTP_CHRONOLOG_* accepted as fallback)."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


def offline_mode() -> bool:
    return _env("CHRONOLOG_OFFLINE").lower() in ("1", "true", "yes", "on")


class ChronoLogUnavailable(RuntimeError):
    """ChronoLog backend was requested but cannot be reached."""


@dataclass(frozen=True)
class Endpoint:
    protocol: str
    ip: str
    port: int
    provider_id: int


@dataclass(frozen=True)
class BackendConfig:
    portal: Endpoint
    query:  Endpoint

    @classmethod
    def from_env(cls) -> "BackendConfig":
        protocol  = _env("CHRONOLOG_VISOR_PROTOCOL", "DTP_CHRONOLOG_PROTOCOL", default="ofi+sockets")
        visor_ip  = _resolve_visor_ip()
        portal_port = int(_env("CHRONOLOG_PORTAL_PORT", "DTP_CHRONOLOG_PORTAL_PORT", default="5555"))
        portal_pid  = int(_env("CHRONOLOG_PORTAL_PROVIDER", "DTP_CHRONOLOG_PORTAL_PROVIDER", default="55"))
        # The query (reader) service is a CLIENT-SIDE callback endpoint — the
        # player sends event chunks back to it. So its IP is the local 40g IP
        # that the player can reach, NOT the visor's.
        query_ip   = _env("CHRONOLOG_QUERY_IP", "DTP_CHRONOLOG_QUERY_IP", default="") or _resolve_local_40g_ip()
        query_port = int(_env("CHRONOLOG_QUERY_PORT", "DTP_CHRONOLOG_QUERY_PORT", default="5557"))
        query_pid  = int(_env("CHRONOLOG_QUERY_PROVIDER", "DTP_CHRONOLOG_QUERY_PROVIDER", default="57"))
        return cls(
            portal=Endpoint(protocol, visor_ip, portal_port, portal_pid),
            query=Endpoint(protocol, query_ip, query_port, query_pid),
        )


def _resolve_visor_ip() -> str:
    """Pick up the visor IP, honouring envs but falling back to SLURM-aware discovery.

    Order:
      1. CHRONOLOG_VISOR_IP (or DTP_CHRONOLOG_VISOR_IP) — explicit IPv4 override
      2. CHRONOLOG_VISOR_HOST (or DTP_CHRONOLOG_VISOR_HOST) — a `-40g` hostname we'll `getent`
      3. SLURM_JOB_NODELIST — first node, with `-40g` suffix, via `getent`
    """
    ip = _env("CHRONOLOG_VISOR_IP", "DTP_CHRONOLOG_VISOR_IP")
    if ip:
        return ip

    host = _env("CHRONOLOG_VISOR_HOST", "DTP_CHRONOLOG_VISOR_HOST")
    if not host:
        nodelist = os.environ.get("SLURM_JOB_NODELIST", "").strip()
        if nodelist:
            host = _first_node(nodelist) + "-40g"

    if not host:
        # Last resort — assume a local single-node ChronoLog (Stage-4 layout).
        return "127.0.0.1"

    return _getent_ipv4(host)


def _first_node(nodelist: str) -> str:
    try:
        if shutil.which("scontrol"):
            out = subprocess.check_output(
                ["scontrol", "show", "hostnames", nodelist],
                stderr=subprocess.DEVNULL,
            ).decode().split()
            if out:
                return out[0]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Fallback heuristic for "ares-comp-[11-14]" → "ares-comp-11"
    if "[" in nodelist:
        prefix, rest = nodelist.split("[", 1)
        first_idx = rest.split(",", 1)[0].split("-", 1)[0]
        return f"{prefix}{first_idx}"
    return nodelist.split(",", 1)[0]


def _getent_ipv4(host: str) -> str:
    try:
        info = socket.getaddrinfo(host, None, socket.AF_INET)
        if info:
            return info[0][4][0]
    except socket.gaierror:
        pass
    raise ChronoLogUnavailable(
        f"Cannot resolve ChronoVisor host {host!r}. Set CHRONOLOG_VISOR_IP "
        f"to an IPv4 reachable on the same fabric as the visor."
    )


def _resolve_local_40g_ip() -> str:
    """Return this node's IPv4 on the 40 GbE fabric (172.25.101.x).

    The player calls back to the query endpoint on this address, so it must
    be reachable from the player node — `127.0.0.1` only works if the player
    and the client live on the same host.
    """
    explicit = _env("CHRONOLOG_LOCAL_IP", "DTP_CHRONOLOG_LOCAL_IP")
    if explicit:
        return explicit

    me = socket.gethostname().split(".")[0]
    try:
        info = socket.getaddrinfo(f"{me}-40g", None, socket.AF_INET)
        if info:
            return info[0][4][0]
    except socket.gaierror:
        pass

    # Fall back to any non-loopback IPv4 — better than 127.0.0.1 even if wrong.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("172.25.1.1", 1))   # the 40g gateway, no traffic actually sent
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class ChronoLogBackend:
    """Process-wide ChronoLog client wrapper. Thread-safe."""

    def __init__(self, cfg: BackendConfig):
        self.cfg = cfg
        self._lock = threading.RLock()
        # ChronoLog's pybind11 binding takes EITHER a single portal config
        # (writer-only) OR portal+query (reader+writer). We construct the
        # dual-config Client up front so the same instance can both append
        # and replay — that's what client/python/examples/reader_client.py
        # does, and a separate writer+reader pair fails ReplayStory with -11.
        self._client = None          # py_chronolog_client.Client
        self._chronolog_mod = None   # the imported py_chronolog_client module
        self._chronicles_seen: set[str] = set()
        self._story_handles: Dict[Tuple[str, str], Any] = {}    # write handles
        self._acquired: set[Tuple[str, str]] = set()            # (chronicle, story) ever acquired

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _import(self):
        if self._chronolog_mod is None:
            try:
                import py_chronolog_client as _cl  # type: ignore
            except ImportError as exc:
                raise ChronoLogUnavailable(
                    "py_chronolog_client is not importable. The chronolog backend "
                    "requires the ChronoLog install on LD_LIBRARY_PATH and "
                    "PYTHONPATH, and a Python ABI that matches the installed .so "
                    "(currently cpython-311)."
                ) from exc
            self._chronolog_mod = _cl
        return self._chronolog_mod

    def _client_inst(self):
        if self._client is not None:
            return self._client
        cl = self._import()
        portal = cl.ClientPortalServiceConf(
            self.cfg.portal.protocol, self.cfg.portal.ip,
            self.cfg.portal.port, self.cfg.portal.provider_id,
        )
        query  = cl.ClientQueryServiceConf(
            self.cfg.query.protocol, self.cfg.query.ip,
            self.cfg.query.port, self.cfg.query.provider_id,
        )
        client = cl.Client(portal, query)
        rc = client.Connect()
        if rc != CL_SUCCESS:
            raise ChronoLogUnavailable(f"chronolog Connect() returned {rc}")
        self._client = client
        log.info("chronolog client connected: portal=%s:%d query=%s:%d",
                 self.cfg.portal.ip, self.cfg.portal.port,
                 self.cfg.query.ip,  self.cfg.query.port)
        return client

    def close(self):
        with self._lock:
            for ch, st in list(self._acquired):
                try:
                    if self._client is not None:
                        self._client.ReleaseStory(ch, st)
                except Exception:
                    pass
            self._story_handles.clear()
            self._acquired.clear()
            if self._client is not None:
                try:
                    self._client.Disconnect()
                except Exception:
                    pass
                self._client = None

    # ------------------------------------------------------------------
    # Chronicle / Story helpers
    # ------------------------------------------------------------------

    def _ensure_chronicle(self, chronicle: str):
        if chronicle in self._chronicles_seen:
            return
        client = self._client_inst()
        rc = client.CreateChronicle(chronicle, {}, 1)
        if rc not in (CL_SUCCESS, CL_ERR_CHRONICLE_EXISTS):
            raise RuntimeError(f"CreateChronicle({chronicle}) -> {rc}")
        self._chronicles_seen.add(chronicle)

    def _story_handle(self, chronicle: str, story: str):
        key = (chronicle, story)
        h = self._story_handles.get(key)
        if h is not None:
            return h
        self._ensure_chronicle(chronicle)
        client = self._client_inst()
        rc, handle = client.AcquireStory(chronicle, story, {}, 1)
        if rc != CL_SUCCESS:
            raise RuntimeError(f"AcquireStory({chronicle}, {story}) -> {rc}")
        self._story_handles[key] = handle
        self._acquired.add(key)
        return handle

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_event(self, chronicle: str, story: str, payload: Dict[str, Any]) -> int:
        """Serialise `payload` to JSON and append to (chronicle, story).

        Returns the keeper-assigned event timestamp (nanoseconds, uint64). The
        caller doesn't usually need it — ChronoLog orders events by that ts on
        replay.
        """
        line = json.dumps(payload, separators=(",", ":"), default=_jsonable)
        with self._lock:
            handle = self._story_handle(chronicle, story)
            return int(handle.log_event(line))

    def replay_events(
        self,
        chronicle: str,
        story:     str,
        start_ns:  int = 0,
        end_ns:    Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Pull all events in `[start_ns, end_ns]`, decode as JSON, return in time order.

        `end_ns=None` means "up to now" — translated to a hard upper bound
        that's safely past the keeper's clock so we don't miss anything.

        ReplayStory requires AcquireStory to have been called first on the
        same Client (see ChronoLog's `client/python/examples/reader_client.py`).
        `_story_handle` already does that for writes, so the simplest correct
        path is to ensure the same handle exists before replaying.
        """
        if end_ns is None:
            end_ns = time.time_ns() + 60_000_000_000  # +60s of slack

        cl = self._import()
        with self._lock:
            # AcquireStory is idempotent for this Client; ensure it.
            self._story_handle(chronicle, story)
            client  = self._client_inst()
            ev_list = cl.EventList()
            rc = client.ReplayStory(chronicle, story, start_ns, end_ns, ev_list)
        if rc not in (CL_SUCCESS, CL_ERR_NOT_EXIST):
            archived = _csv_archive_events(chronicle, story, start_ns, end_ns)
            if archived:
                return archived
            raise RuntimeError(f"ReplayStory({chronicle}, {story}) -> {rc}")

        out: List[Dict[str, Any]] = []
        for ev in ev_list:
            rec = ev.log_record()
            if isinstance(rec, (bytes, bytearray)):
                rec = rec.decode("utf-8", errors="replace")
            try:
                obj = json.loads(rec)
            except Exception:
                obj = {"_raw": rec}
            obj.setdefault("_chronolog_time_ns", int(ev.time()))
            obj.setdefault("_chronolog_index",   int(ev.index()))
            obj.setdefault("_chronolog_client",  int(ev.client_id()))
            out.append(obj)
        if not out:
            # The player can serve nothing for stories the cluster HAS
            # accepted (archive read flakiness, or the chunk never became
            # replayable). The grapher's drained CSVs are the keepers'
            # durable output — never look empty when they hold the data.
            out = _csv_archive_events(chronicle, story, start_ns, end_ns)
        return out

    # ------------------------------------------------------------------
    # Index helpers (workaround for no ShowChronicles binding)
    # ------------------------------------------------------------------

    def index_add(self, story: str, value: str) -> None:
        """Append a one-off `{value: ...}` to (CHRONICLE_INDEX, story) so
        downstream `index_list` can replay and dedup."""
        from .constants import CHRONICLE_INDEX
        self.append_event(CHRONICLE_INDEX, story, {"v": value})

    def index_list(self, story: str) -> List[str]:
        """List the distinct values appended to (CHRONICLE_INDEX, story).

        CSV-archive-first, by necessity. A live ``ReplayStory`` against an index
        story that was never written blocks *forever* holding the GIL — the
        client does not release it and there is no Python-level timeout — which
        wedges the whole dashboard/capture process on a fresh cluster (the
        dashboard's topology read and the sync worker's warm-up both land here).
        The index chronicle drains to the grapher CSVs like any other story, so
        we read those instead: fast, never blocks, and at-worst ~drain-window
        stale (acceptable for an eventually-consistent listing index). A live
        replay is opt-in via ``CHRONOLOG_INDEX_LIVE=1`` for callers that have
        already ensured the story is drained.
        """
        from .constants import CHRONICLE_INDEX
        evs = _csv_archive_events(CHRONICLE_INDEX, story)
        if not evs and _env("CHRONOLOG_INDEX_LIVE").lower() in ("1", "true", "yes"):
            try:
                evs = self.replay_events(CHRONICLE_INDEX, story)
            except RuntimeError:
                evs = []
        seen: List[str] = []
        seen_set: set[str] = set()
        for ev in evs:
            v = ev.get("v")
            if isinstance(v, str) and v and v not in seen_set:
                seen_set.add(v)
                seen.append(v)
        return seen


_CSV_LINE_PREFIX = "event :"


def _csv_archive_dir() -> str:
    return (
        os.environ.get("CHRONOLOG_OUTPUT_DIR")
        or os.environ.get("DTP_CHRONOLOG_OUTPUT_DIR")
        or os.path.expanduser("~/chronolog-install/chronolog/output")
    )


def _csv_archive_events(
    chronicle: str, story: str, start_ns: int = 0, end_ns: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Read a story from the grapher's drained CSV archive.

    File naming: ``{chronicle}.{story}.{ip}.{port}.{startSec}.csv``, one file
    per keeper. Line format: ``event : sid:time_ns:client:index:payload``.
    Used as the fallback when a live ``ReplayStory`` serves nothing for data
    the cluster has accepted (see ``replay_events``).
    """
    import glob
    import re

    line_re = re.compile(r"^event\s*:\s*(\d+):(\d+):(\d+):(\d+):(.*)$")
    out: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(_csv_archive_dir(), f"{chronicle}.{story}.*.csv"))):
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line.startswith(_CSV_LINE_PREFIX):
                    continue
                m = line_re.match(line)
                if not m:
                    continue
                _sid, evtime, cid, ix, payload = m.groups()
                t = int(evtime)
                if t < start_ns or (end_ns is not None and t > end_ns):
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    obj = {"_raw": payload}
                obj.setdefault("_chronolog_time_ns", t)
                obj.setdefault("_chronolog_index", int(ix))
                obj.setdefault("_chronolog_client", int(cid))
                obj["_chronolog_archived"] = True
                out.append(obj)
    out.sort(key=lambda e: e.get("_chronolog_time_ns", 0))
    if out:
        log.info("csv archive fallback served %d events for %s/%s", len(out), chronicle, story)
    return out


def _jsonable(obj):
    """Best-effort JSON-default that won't crash hot-path writes."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return repr(obj)


# ----------------------------------------------------------------------
# Module-level singleton
# ----------------------------------------------------------------------

_BACKEND: Optional[ChronoLogBackend] = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> Optional[ChronoLogBackend]:
    """Return the process-wide ChronoLog backend.

    ChronoLog is the assumed substrate, so this returns a live backend in all
    normal operation. It returns `None` only in `CHRONOLOG_OFFLINE=1` demo mode,
    where reads are served from drained CSVs by the adapter instead. Callers
    already treat `None` as "no live ChronoLog — fall back" (see path_a_reader).

    The backend is constructed lazily on first call so importing this module
    never triggers a connection attempt.
    """
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if offline_mode():
        return None
    cfg = BackendConfig.from_env()
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = ChronoLogBackend(cfg)
    return _BACKEND


def reset_backend() -> None:
    """Tear down the singleton — used by tests and on Flask reload."""
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            _BACKEND.close()
            _BACKEND = None
