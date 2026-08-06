"""Every GET route, against empty state.

This is the regression net the suite was missing. An observability dashboard is
pointed at a *fresh* cluster all the time, so "no data yet" is a first-class
state, not an edge case — and it is exactly the state where unguarded
`[0]` indexing and `None` arithmetic blow up.

The contract asserted here is narrow on purpose: no route may 500. A route may
legitimately answer 200 with an empty payload, 404 for an unknown id, or 503
when a capability is unavailable. What it may not do is crash.
"""

import pytest
from flask import Flask

# Streaming (SSE) endpoints never terminate, so the test client would block
# buffering them forever. They are exercised by their own non-streaming paths.
_STREAMING = {
    "/_interceptor/live",
    "/api/_inter-agent/stream",
    "/api/diagnostics/stream",
    "/api/fleet/stream",
    "/api/session/<session_id>/stream",
}

# Not API routes. "/" deliberately answers 503 with a "run make workspace" page
# when the SPA bundle has not been built — which is the normal state of a fresh
# checkout, and is asserted properly in test_app_factory.py.
_NON_API = {"/"}

# Plausible values for path parameters. They intentionally do not exist in the
# empty state: "unknown id" is the response we want to check is graceful.
_PARAM_VALUES = {
    "conversation_id": "no-such-conversation",
    "scenario_id": "no-such-scenario",
    "scenario": "no-such-scenario",
    "session_id": "agent@no-such-scenario",
    "sid": "agent@no-such-scenario",
    "interaction_id": "no-such-interaction",
    "sig": "no-such-incident-signature",
    "host": "no-such-host",
    "seq_id": "1",
    "filename": "index.html",
}


def _concrete_get_routes(app: Flask):
    """Every GET rule, with path parameters substituted."""
    out = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        if str(rule) in _STREAMING or str(rule) in _NON_API:
            continue
        path = str(rule)
        skip = False
        for arg in rule.arguments:
            if arg not in _PARAM_VALUES:
                skip = True
                break
            # Rules render as <converter:name> or <name>.
            for token in (f"<{arg}>", f"<path:{arg}>", f"<int:{arg}>", f"<string:{arg}>"):
                path = path.replace(token, _PARAM_VALUES[arg])
        if skip:
            continue
        out.append((str(rule), path))
    return sorted(out)


def test_route_inventory_covers_all_non_streaming_gets(app):
    """Guards the sweep below from silently testing nothing.

    Every GET route must be either covered or explicitly listed as streaming —
    a new route with an unrecognised path parameter would otherwise be skipped
    in silence, and the sweep would quietly stop protecting it.
    """
    covered = {rule for rule, _ in _concrete_get_routes(app)}
    all_gets = {str(r) for r in app.url_map.iter_rules() if "GET" in r.methods}
    unaccounted = all_gets - covered - _STREAMING - _NON_API
    assert not unaccounted, (
        "GET routes neither covered nor declared streaming/non-API (add a "
        "value to "
        f"_PARAM_VALUES): {sorted(unaccounted)}"
    )
    assert len(covered) >= 20, f"expected the full API surface, got {len(covered)}"


def test_every_get_route_survives_empty_state(app, client):
    """No GET route may 500 when there is no data at all."""
    failures = []
    for rule, path in _concrete_get_routes(app):
        try:
            resp = client.get(path)
        except Exception as exc:  # noqa: BLE001 - we want the route name with it
            failures.append(f"{rule}: raised {type(exc).__name__}: {exc}")
            continue
        if resp.status_code >= 500:
            body = resp.get_data(as_text=True)[:200]
            failures.append(f"{rule}: HTTP {resp.status_code} — {body}")

    assert not failures, "routes failed on empty state:\n" + "\n".join(failures)


@pytest.mark.parametrize(
    "path",
    [
        "/api/config",
        "/api/sessions",
        "/api/scenarios",
        "/api/conversations",
        "/api/diagnostics/incidents",
        "/api/diagnostics/status",
        "/api/fleet/health",
        "/api/tracing/scenarios",
        "/api/memory/sessions",
    ],
)
def test_collection_endpoints_return_json_on_empty_state(client, path):
    """Collection endpoints answer 200 with parseable JSON, not an error page."""
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    assert resp.is_json, f"{path} did not return JSON"
    resp.get_json()  # raises if the body is not valid JSON
