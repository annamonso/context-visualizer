#!/usr/bin/env python3
"""Offline self-test for the live-view port (bus, SSE stream, batch ingest, collector).

Runs entirely in-process with CHRONOLOG_OFFLINE=1 (JSONL store, no visor) plus
injected fakes for the collector's ChronoLog backend. Exercises:

  1. single + batch ingest through the Flask blueprint
  2. forwarded-batch dedup semantics (ids preserved, store skips chronolog)
  3. SSE backlog + live frames from /api/_inter-agent/stream
  4. /_interceptor/live route presence
  5. CollectorDaemon submit -> flush -> chronolog write + forward marker

Usage:  CHRONOLOG_OFFLINE=1 python3 scripts/selftest_live.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("CHRONOLOG_OFFLINE", "1")
os.environ["DTP_STATE_DIR"] = tempfile.mkdtemp(prefix="live-selftest-")

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    status = "ok" if cond else "FAIL"
    print(f"[selftest] {name}: {status}" + (f" — {detail}" if detail else ""))
    if cond:
        PASS += 1
    else:
        sys.exit(1)


def sse_events(body_iter, max_frames: int, timeout_s: float = 10.0):
    """Collect up to max_frames (event, data) tuples from an SSE byte iterator."""
    frames, buf = [], ""
    deadline = time.monotonic() + timeout_s
    for chunk in body_iter:
        buf += chunk.decode() if isinstance(chunk, bytes) else chunk
        while "\n\n" in buf:
            raw, buf = buf.split("\n\n", 1)
            ev, data = None, None
            for line in raw.splitlines():
                if line.startswith("event: "):
                    ev = line[len("event: "):]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: "):])
            if ev is not None:
                frames.append((ev, data))
            if len(frames) >= max_frames:
                return frames
        if time.monotonic() > deadline:
            return frames
    return frames


def main() -> int:
    from chronolog_observability.app import create_app
    from chronolog_observability.config import Config

    app = create_app(Config.from_env())
    client = app.test_client()

    base = {
        "scenario_id": "selftest", "from_host": "node-a", "from_session": "planner",
        "to_host": "node-b", "to_session": "executor",
    }

    # 1. single ingest
    r = client.post("/api/_inter-agent/ingest", json={**base, "phase": "start", "payload": {"q": 1}})
    check("single ingest", r.status_code == 200 and r.get_json().get("event_id"), str(r.get_json()))

    # 2. batch ingest
    r = client.post("/api/_inter-agent/ingest", json={"events": [
        {**base, "phase": "done", "status": "ok", "latency_ms": 12.5},
        {**base, "phase": "start"},
    ]})
    j = r.get_json()
    check("batch ingest", r.status_code == 200 and j["accepted"] == 2 and not j["errors"], str(j))

    # 3. forwarded batch preserves ids (collector relay path)
    r = client.post(
        "/api/_inter-agent/ingest",
        json={"events": [{**base, "phase": "start", "event_id": "fixed123", "ts": "2026-06-11T00:00:00.000000Z"}]},
        headers={"X-DTP-Forwarded": "chronolog"},
    )
    j = r.get_json()
    check("forwarded ids preserved", j["results"][0]["event_id"] == "fixed123", str(j))

    # 4. SSE: backlog + catchup, then a live frame
    resp = client.get("/api/_inter-agent/stream?scenario=selftest&heartbeat_sec=1")
    check("stream content-type", resp.content_type.startswith("text/event-stream"), resp.content_type)

    got: list = []
    done = threading.Event()

    def consume():
        got.extend(sse_events(resp.response, max_frames=6))
        done.set()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    time.sleep(0.5)  # let the backlog drain, then ingest a live event
    client.post("/api/_inter-agent/ingest", json={**base, "phase": "done", "status": "ok"})
    done.wait(timeout=10)
    kinds = [k for k, _ in got]
    check("stream backlog frames", kinds.count("backlog") == 4, str(kinds))
    check("stream catchup frame", "catchup" in kinds, str(kinds))
    check("stream live frame", "message" in kinds, str(kinds))

    # 5. /_interceptor/live exists (route registered; stream starts)
    rules = {r.rule for r in app.url_map.iter_rules()}
    check("live route registered", "/_interceptor/live" in rules, str(sorted(rules))[:200])

    # 6. collector daemon: fake backend + forward capture
    from chronolog_observability.collector.daemon import CollectorDaemon

    writes, forwards = [], []

    class FakeBackend:
        def append_event(self, ch, story, ev):
            writes.append((ch, story, ev["event_id"]))
        def index_add(self, story, sid):
            writes.append(("index", story, sid))

    d = CollectorDaemon(
        "http://dashboard:5000",
        chronolog_backend=FakeBackend(),
        forward=lambda url, events, fw: forwards.append((url, len(events), fw)),
    )
    res = d.submit([{**base, "phase": "start"}, {"bad": "event"}])
    check("collector validate", res["accepted"] == 1 and len(res["errors"]) == 1, str(res))
    d._flush(d._drain_batch())
    check("collector chronolog write", any(w[0] != "index" for w in writes), str(writes))
    check("collector forwarded marker", forwards == [("http://dashboard:5000", 1, True)], str(forwards))

    print(f"\n[selftest] all {PASS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
