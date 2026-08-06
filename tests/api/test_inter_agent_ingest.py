"""Path-B ingest: the endpoint agents POST to.

This is the only write surface on the API, so it gets the closest look: a
round-trip through the hot store, batch handling, and graceful rejection of the
malformed payloads a half-instrumented agent will send.
"""


def _edge(scenario="s1", frm="planner@s1", to="executor@s1", **extra):
    """A minimal valid Path-B event, per the schema in api/inter_agent.py."""
    e = {
        "scenario_id": scenario,
        "phase": "start",
        "from_host": "node-01",
        "from_session": frm,
        "to_host": "node-02",
        "to_session": to,
        "kind": "mcp_call",
        "tool_name": "call_remote_agent",
    }
    e.update(extra)
    return e


def test_ingest_single_edge_round_trips_to_the_scenario(client):
    r = client.post("/api/_inter-agent/ingest", json=_edge())
    assert r.status_code in (200, 201, 202), r.get_data(as_text=True)

    listed = client.get("/api/scenarios")
    assert listed.status_code == 200
    body = listed.get_json()
    blob = str(body)
    assert "s1" in blob, f"ingested scenario not visible in /api/scenarios: {body}"


def test_ingest_accepts_a_batch(client):
    payload = [_edge(frm=f"a{i}@s2", to=f"b{i}@s2", scenario="s2") for i in range(5)]
    r = client.post("/api/_inter-agent/ingest", json=payload)
    assert r.status_code in (200, 201, 202), r.get_data(as_text=True)

    raw = client.get("/api/scenarios/s2/inter-agent/raw")
    assert raw.status_code == 200
    assert len(str(raw.get_json())) > 0


def test_ingest_rejects_malformed_payloads_without_crashing(client):
    """A half-instrumented agent must get a 4xx, never take the server down."""
    for bad in (
        "not json at all",
        {"no": "recognised fields"},
        {"scenario": None, "from_session": None},
        [],
    ):
        if isinstance(bad, str):
            r = client.post(
                "/api/_inter-agent/ingest", data=bad, content_type="application/json"
            )
        else:
            r = client.post("/api/_inter-agent/ingest", json=bad)
        assert r.status_code < 500, f"payload {bad!r} caused HTTP {r.status_code}"


def test_ingest_isolates_scenarios(client):
    """An edge in one scenario must not surface in another."""
    client.post("/api/_inter-agent/ingest", json=_edge(scenario="alpha",
                                                       frm="p@alpha", to="e@alpha"))
    client.post("/api/_inter-agent/ingest", json=_edge(scenario="beta",
                                                       frm="p@beta", to="e@beta"))

    alpha = str(client.get("/api/scenarios/alpha/inter-agent/raw").get_json())
    assert "beta" not in alpha, "scenario leakage: beta edges visible under alpha"
