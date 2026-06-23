"""JSON API exposing ChronoLog deployment state for the Cluster tab.

Shows where the ChronoLog components run (visor, keepers per node, grapher/
player) and what they hold (chronicles → stories → events). Reads through the
same ``backend.client.get_backend()`` the rest of the plugin uses, but falls
back to the grapher's drained CSVs (under ``$CHRONOLOG_OUTPUT_DIR``) so the
view never looks empty for events the cluster has accepted but not yet made
replayable (the ~180 s acceptance window).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set

from flask import Blueprint, Response, jsonify, request

from ..backend import constants
from ..backend.client import ChronoLogUnavailable, get_backend


bp = Blueprint("chronolog_view", __name__)


def _output_dir() -> Path:
    return Path(
        os.environ.get("CHRONOLOG_OUTPUT_DIR")
        or os.environ.get("DTP_CHRONOLOG_OUTPUT_DIR")
        or os.path.expanduser("~/chronolog-install/chronolog/output")
    )


_CSV_LINE_RE = re.compile(r"^event\s*:\s*(\d+):(\d+):(\d+):(\d+):(.*)$")


def _csv_events(chronicle: str, story: str) -> List[Dict]:
    out_dir = _output_dir()
    if not out_dir.is_dir():
        return []
    files = sorted(out_dir.glob(f"{chronicle}.{story}.*.csv"))
    if not files:
        return []
    out: List[Dict] = []
    for f in files:
        parts = f.name.split(".")
        keeper_ip = ".".join(parts[2:6]) if len(parts) > 5 else "?"
        with f.open() as h:
            for line in h:
                line = line.strip()
                if not line:
                    continue
                m = _CSV_LINE_RE.match(line)
                if not m:
                    continue
                _sid, evtime, cid, ix, payload = m.groups()
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    obj = {"_raw": payload}
                obj["_chronolog_time_ns"] = int(evtime)
                obj["_chronolog_index"] = int(ix)
                obj["_chronolog_client"] = int(cid)
                obj["_drained_from_keeper_ip"] = keeper_ip
                out.append(obj)
    out.sort(key=lambda e: e.get("_chronolog_time_ns", 0))
    return out


@lru_cache(maxsize=256)
def _host_for_ip(ip: str) -> Optional[str]:
    """Best-effort reverse-DNS so the UI can show which node a keeper runs on.

    Returns a short node name (e.g. ``ares-comp-11``) — domain and the ``-40g``
    interface suffix the cluster uses are stripped. Returns None if the IP does
    not resolve, so the frontend falls back to the raw IP.
    """
    try:
        name = socket.gethostbyaddr(ip)[0]
    except OSError:
        return None
    short = name.split(".")[0]
    if short.endswith("-40g"):
        short = short[: -len("-40g")]
    return short


def _short_host(name: Optional[str]) -> Optional[str]:
    """Normalize a hostname for allocation comparison: drop domain + ``-40g``
    interface suffix, lowercase, and canonicalise digit groups. Returns None
    for falsy input.

    The digit canonicalisation is load-bearing: ARES reverse-DNS returns
    *unpadded* names (``ares-comp-3``) while ``scontrol`` / ``SLURM_JOB_NODELIST``
    zero-pad (``ares-comp-03``). Without normalising, an in-allocation keeper on
    a single-digit node compares unequal to its allocation entry and is wrongly
    flagged out-of-allocation — so the Cluster tab hides every keeper on nodes
    03–09 and shows no active nodes at all."""
    if not name:
        return None
    s = name.split(".")[0]
    if s.endswith("-40g"):
        s = s[: -len("-40g")]
    s = s.lower()
    return re.sub(r"\d+", lambda m: str(int(m.group(0))), s)


def _parse_nodelist(spec: str) -> List[str]:
    """Expand a SLURM nodelist like ``ares-comp-[10-13,15]`` into individual
    hostnames, without shelling out. Handles comma-separated groups, numeric
    ranges, and zero-padded widths. Used as a fallback when ``scontrol`` is
    unavailable on the node running the dashboard."""
    spec = spec.strip()
    if not spec:
        return []
    # split top-level on commas that are NOT inside brackets
    parts, depth, cur = [], 0, ""
    for ch in spec:
        if ch == "[":
            depth += 1
            cur += ch
        elif ch == "]":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)

    nodes: List[str] = []
    for p in parts:
        m = re.match(r"^(.*?)\[([0-9,\-]+)\](.*)$", p)
        if not m:
            nodes.append(p)
            continue
        prefix, ranges, suffix = m.groups()
        for r in ranges.split(","):
            if "-" in r:
                a, b = r.split("-", 1)
                width = len(a)
                for n in range(int(a), int(b) + 1):
                    nodes.append(f"{prefix}{str(n).zfill(width)}{suffix}")
            else:
                nodes.append(f"{prefix}{r}{suffix}")
    return nodes


def _expand_nodelist(spec: str) -> List[str]:
    """Resolve a nodelist spec to an ordered list of short hostnames. Prefers
    ``scontrol show hostnames`` (authoritative), falls back to the local parser.
    Order matters: the deploy convention places the visor on the FIRST node and
    the grapher + player on the LAST."""
    spec = (spec or "").strip()
    if not spec:
        return []
    try:
        out = subprocess.run(
            ["scontrol", "show", "hostnames", spec],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return [h for h in (_short_host(x) for x in out.stdout.split()) if h]
    except (OSError, subprocess.SubprocessError):
        pass
    return [h for h in (_short_host(x) for x in _parse_nodelist(spec)) if h]


def _allocation_nodes() -> List[str]:
    """Ordered short hostnames of the nodes in the *current* allocation.

    Source order: ``CHRONOLOG_NODES`` (explicit override) then
    ``SLURM_JOB_NODELIST``. Empty list means "unknown" — callers then treat
    every keeper as in-allocation so the page still works when the dashboard
    is launched outside the SLURM helper."""
    spec = (
        os.environ.get("CHRONOLOG_NODES")
        or os.environ.get("DTP_CHRONOLOG_NODES")
        or os.environ.get("SLURM_JOB_NODELIST", "")
    )
    return _expand_nodelist(spec)


def _components(allocation: List[str]) -> Dict:
    """Where the non-keeper ChronoLog components run.

    The visor location is authoritative (it is the endpoint this dashboard is
    connected to, from ``CHRONOLOG_VISOR_IP``). Grapher and player placement
    follows the deploy convention (last allocation node, see
    deploy_cluster.sh) and is labeled as such — this process has no live RPC
    to those daemons to confirm it.
    """
    visor_ip = (
        os.environ.get("CHRONOLOG_VISOR_IP")
        or os.environ.get("DTP_CHRONOLOG_VISOR_IP")
        or ""
    ).strip()
    visor = {
        "ip": visor_ip or None,
        "host": _host_for_ip(visor_ip) if visor_ip else (allocation[0] if allocation else None),
    }
    tail = allocation[-1] if allocation else None
    return {
        "visor": visor,
        "grapher": {"host": tail, "by_convention": True},
        "player": {"host": tail, "by_convention": True},
    }


def _keeper_topology(allocation: Optional[Set[str]] = None,
                     stale_cutoff_sec: float = 3600.0,
                     now_ns: Optional[int] = None) -> Dict:
    """Aggregate the grapher output dir into a keeper-centric view.

    One entry per keeper (the ChronoLog ingest daemon on a compute node). A
    single pass over each CSV yields the per-keeper event totals, the
    per-(chronicle, story) breakdown, and the newest event timestamp per
    keeper so the UI can show whether a keeper is still inside the ~180 s
    drain window.
    """
    keepers: Dict[str, Dict] = {}
    pairs: Dict[tuple, Dict] = {}
    out_dir = _output_dir()
    if not out_dir.is_dir():
        return {"keepers": [], "chronicles": []}

    for f in sorted(out_dir.glob("*.csv")):
        parts = f.name.split(".")
        # chronicle.story.ip.ip.ip.ip.port.startSec.csv = at least 9 tokens
        if len(parts) < 9:
            continue
        ip_segments = parts[-7:-3]
        if not all(seg.isdigit() for seg in ip_segments):
            continue
        story = parts[-8]
        chronicle = ".".join(parts[:-8])
        keeper_ip = ".".join(ip_segments)
        port = parts[-3]

        n = 0
        last_ns = 0
        try:
            with f.open() as h:
                for line in h:
                    if not line.startswith("event :"):
                        continue
                    n += 1
                    m = _CSV_LINE_RE.match(line.strip())
                    if m:
                        last_ns = max(last_ns, int(m.group(2)))
        except OSError:
            continue
        if n == 0:
            continue

        k = keepers.setdefault(keeper_ip, {
            "ip": keeper_ip, "host": _host_for_ip(keeper_ip), "ports": set(),
            "event_count": 0, "last_event_ns": 0, "_stories": {},
        })
        k["ports"].add(port)
        k["event_count"] += n
        k["last_event_ns"] = max(k["last_event_ns"], last_ns)
        k["_stories"][(chronicle, story)] = k["_stories"].get((chronicle, story), 0) + n

        p = pairs.setdefault((chronicle, story), {
            "chronicle": chronicle, "story": story,
            "event_count": 0, "keeper_ips": set(),
        })
        p["event_count"] += n
        p["keeper_ips"].add(keeper_ip)

    if now_ns is None:
        now_ns = time.time_ns()
    keeper_list = []
    for k in keepers.values():
        stories = [
            {"chronicle": c, "story": s, "event_count": cnt}
            for (c, s), cnt in sorted(k.pop("_stories").items())
        ]
        k["ports"] = sorted(int(p) for p in k["ports"])
        k["stories"] = stories

        # liveness annotations: allocation membership is authoritative;
        # staleness (drain recency) is a secondary, informational signal.
        age = (now_ns - k["last_event_ns"]) / 1e9 if k["last_event_ns"] else None
        in_alloc = (not allocation) or (_short_host(k.get("host")) in allocation)
        k["age_sec"] = round(age, 1) if age is not None else None
        k["in_allocation"] = in_alloc
        k["stale"] = (not in_alloc) or (age is not None and age > stale_cutoff_sec)

        keeper_list.append(k)
    # in-allocation, freshest first
    keeper_list.sort(key=lambda k: (not k["in_allocation"], k["ip"]))

    chronicle_list = []
    for p in pairs.values():
        p["keeper_ips"] = sorted(p["keeper_ips"])
        chronicle_list.append(p)
    chronicle_list.sort(key=lambda d: (d["chronicle"], d["story"]))

    return {"keepers": keeper_list, "chronicles": chronicle_list}


@bp.route("/chronolog/topology", methods=["GET"])
def topology():
    """Deployment view powering the Cluster tab.

    Everything below is derived from the grapher's drained CSVs (reliable
    within the acceptance window), the backend scenario index, the SLURM
    allocation, and the visor endpoint this dashboard is connected to.
    """
    backend = get_backend()
    via = "csv"
    scenarios: List[str] = []
    if backend is not None:
        try:
            scenarios = backend.index_list(constants.INDEX_STORY_SCENARIOS) or []
            via = "chronolog_index"
        except Exception:
            scenarios = []

    allocation = _allocation_nodes()
    alloc_set = set(allocation)
    try:
        stale_cutoff = float(
            os.environ.get("CHRONOLOG_KEEPER_STALE_SEC")
            or os.environ.get("DTP_CHRONOLOG_KEEPER_STALE_SEC")
            or "3600"
        )
    except ValueError:
        stale_cutoff = 3600.0
    now_ns = time.time_ns()

    topo = _keeper_topology(allocation=alloc_set,
                            stale_cutoff_sec=stale_cutoff,
                            now_ns=now_ns)

    # By default show only keepers in the current allocation — leftover CSVs
    # from prior runs/allocations are not live and must not look live. Pass
    # ?all=1 to include them (the frontend dims them as history).
    include_stale = request.args.get("all", "").lower() in ("1", "true", "yes")
    keepers = topo["keepers"]
    if not include_stale:
        keepers = [k for k in keepers if k["in_allocation"]]
    hidden = len(topo["keepers"]) - len(keepers)

    return jsonify({
        "via":               via,
        "connected":         backend is not None,
        "output_dir":        str(_output_dir()),
        "scenarios":         scenarios,
        "now_ns":            now_ns,
        "drain_window_sec":  180,
        "allocation":        allocation,
        "components":        _components(allocation),
        "stale_cutoff_sec":  stale_cutoff,
        "hidden_stale_keepers": hidden,
        "keepers":           keepers,
        "chronicles":        topo["chronicles"],
    })


@bp.route("/chronolog/comms", methods=["GET"])
def comms():
    """Node-to-node inter-agent communication for the Cluster tab.

    Answers "which ARES nodes are active, and which are passing messages to
    each other" — aggregated by (from_host, to_host) across the current run's
    scenarios. Served entirely from the dashboard's local hot store (instant,
    GIL-safe): it never touches the ~180s-lagged ChronoLog ReplayStory path, so
    it is safe to poll on a tick. ``?scenario=<sid>`` narrows to one scenario.

    Host keys are canonicalised (``_short_host``) so the unpadded reverse-DNS
    form and the zero-padded SLURM form collapse to one node per physical host,
    and every allocation node appears even before it has exchanged a message —
    so an idle-but-alive node still shows as active.
    """
    from ..capture.inter_agent import get_store

    store = get_store()
    scenario = (request.args.get("scenario") or "").strip()
    scenarios = [scenario] if scenario else store.list_local_scenarios()

    def norm(h: str) -> str:
        return _short_host(h) or "unknown"

    hosts: Dict[str, Dict] = {}
    pairs: Dict[tuple, Dict] = {}

    def host_slot(h: str) -> Dict:
        return hosts.setdefault(h, {
            "host": h, "out": 0, "in": 0, "errors": 0, "agents": set(),
        })

    contributing: List[str] = []
    for sid in scenarios:
        try:
            edges = store.read_stitched(sid)
        except Exception:
            continue
        if edges:
            contributing.append(sid)
        for e in edges:
            fh = norm(e.get("from_host") or "")
            th = norm(e.get("to_host") or "")
            is_err = str(e.get("status") or "").startswith("error")
            sf = host_slot(fh)
            st = host_slot(th)
            sf["out"] += 1
            st["in"] += 1
            if e.get("from_session"):
                sf["agents"].add(e["from_session"])
            if e.get("to_session"):
                st["agents"].add(e["to_session"])
            if is_err:
                sf["errors"] += 1
            if fh == th:
                continue  # intra-host traffic: counted per host, not drawn
            key = (fh, th)
            p = pairs.setdefault(key, {
                "from_host": fh, "to_host": th, "count": 0, "errors": 0,
                "last_ts": "", "tools": set(), "scenarios": set(),
            })
            p["count"] += 1
            if is_err:
                p["errors"] += 1
            ts = e.get("ts_done") or e.get("ts_start") or ""
            if ts and ts > p["last_ts"]:
                p["last_ts"] = ts
            if e.get("tool_name"):
                p["tools"].add(e["tool_name"])
            p["scenarios"].add(sid)

    allocation = _allocation_nodes()
    alloc_set = set(allocation)
    for h in allocation:           # every allocation node is shown, even if idle
        host_slot(h)

    host_list = []
    for h in hosts.values():
        h["agents"] = len(h["agents"])
        h["in_allocation"] = (h["host"] in alloc_set)
        host_list.append(h)
    host_list.sort(key=lambda x: (not x["in_allocation"], x["host"]))

    edge_list = []
    for p in pairs.values():
        p["tools"] = sorted(p["tools"])
        p["scenarios"] = sorted(p["scenarios"])
        edge_list.append(p)
    edge_list.sort(key=lambda x: -x["count"])

    return jsonify({
        "allocation": allocation,
        "hosts": host_list,
        "edges": edge_list,
        "scenarios": contributing,
        "scenario_filter": scenario or None,
        "now_ns": time.time_ns(),
    })


@bp.route("/chronolog/events", methods=["GET"])
def list_events():
    """GET /api/chronolog/events?chronicle=X&story=Y → events for that story."""
    chronicle = request.args.get("chronicle", "").strip()
    story = request.args.get("story", "").strip()
    if not chronicle or not story:
        return Response(json.dumps({"error": "chronicle and story required"}),
                        status=400, content_type="application/json")

    # Drained archive FIRST. It is the keepers' durable output — fast, safe,
    # and complete for anything past the ~180s drain window. A live
    # ReplayStory is only needed for genuinely fresh, un-drained events, and
    # it is hazardous in a serving process: it can block indefinitely holding
    # the GIL and wedge the whole dashboard. So it is opt-in (?replay=1) and
    # only attempted when the archive has nothing.
    events: List[Dict] = _csv_events(chronicle, story)
    via = "csv"

    if not events and request.args.get("replay", "").lower() in ("1", "true", "yes"):
        backend = get_backend()
        if backend is not None:
            try:
                replayed = backend.replay_events(chronicle, story) or []
                if replayed:
                    events = replayed
                    via = "chronolog_replay"
            except (RuntimeError, ChronoLogUnavailable):
                events = []

    return jsonify({
        "chronicle": chronicle,
        "story":     story,
        "via":       via,
        "events":    events,
        "count":     len(events),
    })


# ──────────────────────────────────────────────────────────────────────────
# Cluster capacity — what the underlying SLURM cluster looks like right now
# ──────────────────────────────────────────────────────────────────────────
#
# Single source of truth for "what nodes exist and what state are they in",
# from a live `sinfo`. The capacity badge, the Fleet/Cluster greying of
# unavailable nodes, and the demo-traffic host selection all read from here, so
# the dashboard never invents node names — it uses the cluster's real ones.
# Degrades to available=False where the SLURM CLI is absent.

def sinfo_nodes() -> Optional[Dict[str, Dict]]:
    """Real per-node SLURM state, or None if `sinfo` is unavailable.

    Returns ``{"nodes": {name: {"status": idle|busy|down, "partitions": [...]}},
    "idle": [...], "busy": [...], "down": [...], "partitions": [...]}``.
    """
    import shutil
    import subprocess

    if not shutil.which("sinfo"):
        return None
    try:
        out = subprocess.run(
            ["sinfo", "-h", "-N", "-o", "%N|%t|%P"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None

    raw: Dict[str, Dict] = {}
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1].strip().rstrip("*")
        partition = (parts[2].rstrip("*") if len(parts) > 2 else "").strip()
        rec = raw.setdefault(name, {"state": state, "partitions": set()})
        if partition:
            rec["partitions"].add(partition)
        order = {"idle": 0, "mix": 1, "alloc": 2}  # prefer the most-usable state
        if order.get(state, 9) < order.get(rec["state"], 9):
            rec["state"] = state

    down_states = ("down", "drain", "drng", "draining", "inval", "fail", "maint")

    def _cat(state: str) -> str:
        if state.startswith("idle"):
            return "idle"
        if state.startswith(("mix", "alloc")):
            return "busy"
        if state in down_states:
            return "down"
        return "busy"  # unknown -> treat as unavailable (grey)

    nodes = {
        name: {"status": _cat(r["state"]), "partitions": sorted(r["partitions"])}
        for name, r in raw.items()
    }
    idle = sorted(n for n, r in nodes.items() if r["status"] == "idle")
    busy = sorted(n for n, r in nodes.items() if r["status"] == "busy")
    down = sorted(n for n, r in nodes.items() if r["status"] == "down")
    partitions = sorted({p for r in nodes.values() for p in r["partitions"]})
    return {"nodes": nodes, "idle": idle, "busy": busy, "down": down, "partitions": partitions}


def idle_node_names() -> List[str]:
    """The cluster's currently-idle node names (real), or [] if unknown."""
    info = sinfo_nodes()
    return info["idle"] if info else []


@bp.route("/cluster/capacity")
def cluster_capacity():
    """Per-node SLURM state for the whole cluster (real, from sinfo)."""
    info = sinfo_nodes()
    if info is None:
        return jsonify({"available": False, "reason": "sinfo not found (no SLURM client here)"})
    return jsonify({
        "available": True,
        "total": len(info["nodes"]),
        "idle_count": len(info["idle"]),
        "busy_count": len(info["busy"]),
        "down_count": len(info["down"]),
        "idle": info["idle"],
        "busy": info["busy"],
        "down": info["down"],
        "partitions": info["partitions"],
        # Full per-node status map so the Fleet/Cluster views can render EVERY
        # real node and grey the unavailable ones.
        "nodes": info["nodes"],
    })
