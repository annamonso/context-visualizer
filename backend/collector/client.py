"""Agent-side emit helper: collector first, direct dashboard as fallback.

Dependency-free (stdlib urllib) so agent wrappers and MCP relays can vendor
or import it without pulling in Flask.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_COLLECTOR_URL = "http://127.0.0.1:5650"


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def emit(
    event: dict,
    *,
    collector_url: Optional[str] = None,
    flask_url: Optional[str] = None,
    timeout: float = 0.5,
) -> dict:
    """Send one inter-agent event, preferring the node-local collector.

    Falls back to a direct dashboard POST (no forwarded marker, so the
    dashboard persists it normally) when the collector is down or
    unreachable. URLs default from CHRONOLOG_COLLECTOR_URL /
    CHRONOLOG_DASHBOARD_URL (legacy DTP_COLLECTOR_URL / DTP_FLASK_URL
    accepted). Raises the underlying error only when both paths fail.
    """
    collector = collector_url or _env("CHRONOLOG_COLLECTOR_URL", "DTP_COLLECTOR_URL") or DEFAULT_COLLECTOR_URL
    flask = flask_url or _env("CHRONOLOG_DASHBOARD_URL", "DTP_FLASK_URL")

    try:
        return _post(f"{collector.rstrip('/')}/ingest", event, timeout)
    except (urllib.error.URLError, ConnectionError, OSError):
        if not flask:
            raise
    return _post(f"{flask.rstrip('/')}/api/_inter-agent/ingest", event, max(timeout, 5.0))
