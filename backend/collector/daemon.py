"""The collector daemon: localhost HTTP in, ChronoLog + dashboard forward out.

Transport is plain localhost HTTP (stdlib ThreadingHTTPServer) rather than a
unix socket: agents already speak urllib JSON POST, and it keeps
collector/client.py dependency-free.

Threading model mirrors the sync worker's discipline: HTTP handler threads
only validate, stamp, and enqueue; a SINGLE writer thread owns all ChronoLog
and forward I/O (py_chronolog_client is not thread-safe).

Durability note: events accepted but not yet flushed are lost if the
collector dies (≤ flush_interval worth of telemetry). Acceptable for
observability data; a local JSONL journal is future hardening. When the
ChronoLog write fails, the batch is forwarded WITHOUT the forwarded marker
so the dashboard persists it per its own backend — degraded, not lost.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional

from ..capture.inter_agent.store import _iso_now

log = logging.getLogger(__name__)

DEFAULT_PORT = 5650
REQUIRED_FIELDS = (
    "scenario_id", "phase", "from_host", "from_session", "to_host", "to_session",
)


def _validate(event: dict) -> str | None:
    """Light validation — full validation stays the dashboard's job. Returns error or None."""
    if not isinstance(event, dict):
        return "event must be a JSON object"
    for key in REQUIRED_FIELDS:
        v = event.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            return f"missing required field: {key}"
    if event.get("phase") not in ("start", "done"):
        return "phase must be 'start' or 'done'"
    return None


def _default_forward(flask_url: str, events: List[dict], forwarded: bool, timeout: float = 10.0) -> None:
    headers = {"Content-Type": "application/json"}
    if forwarded:
        headers["X-DTP-Forwarded"] = "chronolog"
    req = urllib.request.Request(
        f"{flask_url.rstrip('/')}/api/_inter-agent/ingest",
        data=json.dumps({"events": events}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


class CollectorDaemon:
    def __init__(
        self,
        flask_url: str,
        port: int = DEFAULT_PORT,
        flush_interval: float = 0.5,
        max_batch: int = 64,
        *,
        chronolog_backend: Optional[object] = None,
        forward: Optional[Callable[[str, List[dict], bool], None]] = None,
    ) -> None:
        """``chronolog_backend`` and ``forward`` are test injection points;
        production discovers the backend via the backend.client singleton."""
        self.flask_url = flask_url
        self.port = port
        self.flush_interval = flush_interval
        self.max_batch = max_batch
        self._backend_override = chronolog_backend
        self._forward = forward or _default_forward
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self.accepted = 0
        self.flushed = 0
        self.dropped = 0

    # ── Backend discovery ───────────────────────────────────────────────

    def _chronolog(self):
        if self._backend_override is not None:
            return self._backend_override
        try:
            from ..chronolog.client import get_backend
        except ImportError:
            return None
        return get_backend()

    # ── Ingest side (HTTP handler threads) ──────────────────────────────

    def submit(self, events: List[dict]) -> dict:
        """Validate + stamp + enqueue. Called from HTTP handler threads."""
        accepted = 0
        errors: List[dict] = []
        for i, ev in enumerate(events):
            err = _validate(ev)
            if err:
                errors.append({"index": i, "error": err})
                continue
            ev.setdefault("event_id", uuid.uuid4().hex)
            ev.setdefault("correlation_id", uuid.uuid4().hex)
            ev.setdefault("ts", _iso_now())
            ev["ingest_ns"] = time.time_ns()
            try:
                self._queue.put_nowait(ev)
                accepted += 1
            except queue.Full:
                self.dropped += 1
                errors.append({"index": i, "error": "collector queue full"})
        self.accepted += accepted
        return {"accepted": accepted, "errors": errors}

    # ── Writer side (single thread) ─────────────────────────────────────

    def _drain_batch(self) -> List[dict]:
        """Block up to flush_interval for the first event, then grab what's there."""
        batch: List[dict] = []
        try:
            batch.append(self._queue.get(timeout=self.flush_interval))
        except queue.Empty:
            return batch
        while len(batch) < self.max_batch:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _flush(self, batch: List[dict]) -> None:
        if not batch:
            return
        backend = self._chronolog()
        wrote_chronolog = False
        if backend is not None:
            try:
                from ..chronolog import constants
                for ev in batch:
                    ch = constants.inter_agent_chronicle(ev["scenario_id"])
                    backend.append_event(ch, constants.INTER_AGENT_STORY_EDGES, ev)
                for sid in sorted({ev["scenario_id"] for ev in batch}):
                    try:
                        backend.index_add(constants.INDEX_STORY_SCENARIOS, sid)
                    except Exception as exc:
                        log.warning("collector index_add failed: %s", exc)
                wrote_chronolog = True
            except Exception as exc:
                log.error("collector chronolog write failed (%d events) — "
                          "forwarding to dashboard for persistence: %s", len(batch), exc)
        try:
            # forwarded=True only when ChronoLog already holds the events;
            # otherwise the dashboard persists them per its own backend.
            self._forward(self.flask_url, batch, wrote_chronolog)
            self.flushed += len(batch)
        except Exception as exc:
            if wrote_chronolog:
                # Durable in ChronoLog; only the live view missed the events.
                log.warning("collector forward failed (%d events, chronolog "
                            "already has them): %s", len(batch), exc)
                self.flushed += len(batch)
            else:
                log.error("collector forward failed and chronolog unavailable "
                          "— %d events LOST: %s", len(batch), exc)

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            self._flush(self._drain_batch())
        # Final drain on shutdown.
        self._flush(self._drain_batch())

    # ── HTTP server ─────────────────────────────────────────────────────

    def _make_handler(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet by default
                log.debug("collector http: " + fmt, *args)

            def _reply(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    self._reply(200, {
                        "ok": True,
                        "queued": daemon._queue.qsize(),
                        "accepted": daemon.accepted,
                        "flushed": daemon.flushed,
                        "dropped": daemon.dropped,
                    })
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/ingest":
                    self._reply(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    data = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._reply(400, {"error": "invalid JSON"})
                    return
                if isinstance(data, list):
                    events = data
                elif isinstance(data, dict) and isinstance(data.get("events"), list):
                    events = data["events"]
                else:
                    events = [data]
                result = daemon.submit(events)
                self._reply(200 if result["accepted"] else 400, result)

        return Handler

    def run(self) -> None:
        writer = threading.Thread(target=self._writer_loop, name="collector-writer", daemon=True)
        writer.start()
        server = ThreadingHTTPServer(("127.0.0.1", self.port), self._make_handler())
        log.info("collector listening on 127.0.0.1:%d → %s", self.port, self.flask_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            writer.join(timeout=2 * self.flush_interval + 1)
            server.server_close()
