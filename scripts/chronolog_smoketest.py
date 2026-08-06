#!/usr/bin/env python3
"""End-to-end smoke test for chronolog-observability against a LIVE ChronoLog visor.

Unlike the offline (`CHRONOLOG_OFFLINE=1`) self-checks, this exercises the real
write -> ChronoLog -> read path through the plugin's own modules:

  * Path-A: CaptureSpool -> SpoolToChronoLogSync -> ChronoLog, read back via
    backend.path_a_reader (the same reader the Flask blueprints use).
  * Path-B: InterAgentStore append -> ChronoLog, read back via read_stitched.
  * Index workaround: index_add / index_list.

Run it on a COMPUTE NODE co-located with the cluster — Mercury's ofi+sockets
provider does not cross the master(172.20.x)/compute(172.25.x) subnet boundary.
See scripts/ops/run-smoketest.sh for the env wiring, or set up manually:

    source /etc/profile.d/lmod.sh
    module load python/3.11.9-zg4555e
    export LD_LIBRARY_PATH=$HOME/chronolog-install/chronolog/lib:$LD_LIBRARY_PATH
    export PYTHONPATH=$HOME/chronolog-install/chronolog/lib:<repo>/src:$PYTHONPATH
    export TMPDIR=/mnt/nvme/$USER/chronolog-tmp && mkdir -p $TMPDIR
    export CHRONOLOG_VISOR_IP=<raw IPv4 of the visor node>   # NOT the -40g name
    python3 scripts/chronolog_smoketest.py

NOTE on the drain lag: a freshly appended event is not replayable until the
keeper hands its chunk to the grapher's HDF5 archive (~30-180s). This script
polls for the events it wrote, up to --read-timeout seconds, before declaring
a read failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid


def _p(tag: str, msg: str) -> None:
    print(f"[smoke/{tag}] {msg}", flush=True)


def _poll(fn, predicate, timeout_s: float, interval_s: float = 5.0):
    """Call fn() until predicate(result) is truthy or timeout. Returns last result."""
    deadline = time.monotonic() + timeout_s
    result = fn()
    while not predicate(result):
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval_s)
        result = fn()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Live ChronoLog smoke test for chronolog-observability")
    ap.add_argument("--read-timeout", type=float, default=240.0,
                    help="seconds to poll for written events to become replayable (drain lag)")
    ap.add_argument("--no-wait", action="store_true",
                    help="read back immediately; do not poll past the drain window")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Imported here (not at module top) so --help works without the package env.
    from chronolog_observability.backend import constants
    from chronolog_observability.backend.client import BackendConfig, get_backend, reset_backend
    from chronolog_observability.backend import path_a_reader as par
    from chronolog_observability.capture.spool import CaptureSpool
    from chronolog_observability.capture.sync_worker import SpoolToChronoLogSync
    from chronolog_observability.capture.inter_agent import store as ia

    cfg = BackendConfig.from_env()
    _p("cfg", f"visor portal={cfg.portal.ip}:{cfg.portal.port} "
              f"query={cfg.query.ip}:{cfg.query.port} protocol={cfg.portal.protocol}")

    backend = get_backend()
    if backend is None:
        _p("cfg", "ChronoLog is OFFLINE (CHRONOLOG_OFFLINE=1) — this test needs a live visor.")
        return 2

    timeout = 0.0 if args.no_wait else args.read_timeout
    nonce = uuid.uuid4().hex[:8]
    session = f"smoke-{nonce}"
    failures = []

    # ---- connectivity: force a connect early with a clear error ----------
    try:
        backend.append_event(constants.CHRONICLE_INDEX, "smoke_ping", {"nonce": nonce})
        _p("conn", f"connected to visor; ping appended (nonce={nonce})")
    except Exception as exc:
        _p("conn", f"FATAL: cannot reach the visor: {exc}")
        reset_backend()
        return 3

    # ---- Path-A: spool -> worker -> ChronoLog -> path_a_reader -----------
    spool = CaptureSpool()
    spool.record_interaction(session, {"prompt": "ping", "response": "pong", "model": "claude", "nonce": nonce})
    spool.record_interaction(session, {"prompt": "again", "response": "ok", "model": "claude"})
    spool.record_context_node(session, {"op": "add", "node": f"ctx-{nonce}"})
    rec_blob = spool.record_recovery(session, {"reason": "smoke-checkpoint", "nonce": nonce})
    _p("path-a", f"spooled 2 interactions + 1 context node + 1 recovery for session={session}")

    sync = SpoolToChronoLogSync(backend, spool=spool)
    sync.warm_up_from_chronolog()
    stats = sync.sync_once()
    _p("path-a", f"drained to ChronoLog: {stats.events_written}")

    _p("path-a", f"reading back (poll up to {timeout:.0f}s for drain)…")
    ints = _poll(lambda: par.get_session_interactions(session),
                 lambda r: len(r.get(par.VIRTUAL_NODE_ID, {})) >= 2, timeout)
    n_ints = len(ints.get(par.VIRTUAL_NODE_ID, {}))
    (failures.append("interactions") if n_ints < 2 else None)
    _p("path-a", f"  interactions read back: {n_ints}/2 {'OK' if n_ints >= 2 else 'MISSING'}")

    ctx = _poll(lambda: par.get_context_graph(session)[par.VIRTUAL_NODE_ID],
                lambda r: any(e.get("node") == f"ctx-{nonce}" for e in r), timeout)
    ctx_ok = any(e.get("node") == f"ctx-{nonce}" for e in ctx)
    (failures.append("context_graph") if not ctx_ok else None)
    _p("path-a", f"  context node read back: {'OK' if ctx_ok else 'MISSING'}")

    recs = _poll(lambda: par.get_recovery_events(session),
                 lambda r: any(e.get("blob_name") == rec_blob for e in r), timeout)
    rec_ok = any(e.get("blob_name") == rec_blob for e in recs)
    (failures.append("recovery") if not rec_ok else None)
    _p("path-a", f"  recovery event read back: {'OK' if rec_ok else 'MISSING'}")

    # get_sessions() returns {node: [{"session_id": sid}, ...]} — a list of
    # dicts, so we must extract the ids before membership-testing (a bare
    # `session in sessions` compares the str against dicts and is always False).
    # Polled like the other read-backs: the session index drains on the same
    # ~180s keeper→grapher window.
    def _session_ids():
        raw = par.get_sessions().get(par.VIRTUAL_NODE_ID, []) or []
        return {s.get("session_id") for s in raw if isinstance(s, dict)}
    sess_ids = _poll(_session_ids, lambda s: session in s, timeout)
    sess_ok = session in sess_ids
    (failures.append("session-index") if not sess_ok else None)
    _p("path-a", f"  session in index: {'OK' if sess_ok else 'MISSING'}")

    # ---- Path-B: inter-agent store round trip ----------------------------
    store = ia.InterAgentStore(chronolog_backend=backend)
    m = ia.new_message(scenario_id=session, from_host="A", from_session="s1",
                       to_host="B", to_session="s2", phase="start", tool_name="smoke")
    store.append(m)
    store.append(ia.new_message(scenario_id=session, correlation_id=m.correlation_id,
                                from_host="A", from_session="s1", to_host="B", to_session="s2",
                                phase="done", status="ok", latency_ms=1.0))
    _p("path-b", f"appended 2 inter-agent events for scenario={session}")

    stitched = _poll(lambda: store.read_stitched(session), lambda r: len(r) >= 1, timeout)
    pb_ok = len(stitched) >= 1 and stitched[0].get("status") == "ok"
    (failures.append("inter_agent") if not pb_ok else None)
    _p("path-b", f"  stitched edges read back: {len(stitched)} {'OK' if pb_ok else 'MISSING/incomplete'}")

    # ---- Index workaround -------------------------------------------------
    backend.index_add(constants.INDEX_STORY_SCENARIOS, session)
    scen = _poll(lambda: backend.index_list(constants.INDEX_STORY_SCENARIOS),
                 lambda r: session in r, timeout)
    idx_ok = session in scen
    (failures.append("index") if not idx_ok else None)
    _p("idx", f"  scenario index contains session: {'OK' if idx_ok else 'MISSING'}")

    reset_backend()

    print("-" * 60, flush=True)
    if failures:
        _p("result", f"FAIL — {len(failures)} check(s) did not read back: {', '.join(failures)}")
        _p("result", "If only fresh writes are missing, the drain window may exceed "
                     "--read-timeout; raise it and re-run.")
        return 1
    _p("result", "PASS — every write was read back through the plugin's own read path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
