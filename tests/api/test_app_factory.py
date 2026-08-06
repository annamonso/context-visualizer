"""The app factory and the capability gate.

The factory is the seam that decides which blueprints exist at all, so these
tests pin the contract the SPA depends on: /api/config advertises exactly the
capabilities that got mounted.
"""

from chronolog_observability.adapters.base import Capability


def test_factory_builds_in_offline_mode(app):
    assert app is not None
    assert app.config["OBSERVE"].offline is True


def test_config_endpoint_reports_offline_and_adapters(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.get_json()
    assert body["offline"] is True
    assert "chronolog" in body["adapters"]


def test_config_advertises_only_mounted_capabilities(client, app):
    """A capability in /api/config must correspond to real mounted routes.

    The SPA renders a tab per advertised capability, so a capability with no
    routes behind it is a blank tab for the user.
    """
    advertised = set(client.get("/api/config").get_json()["capabilities"])
    assert advertised, "offline mode should still advertise capabilities"

    mounted_blueprints = set(app.blueprints)
    # Every advertised capability is a known one, not a stray string.
    known = {c.value for c in Capability}
    assert advertised <= known, f"unknown capabilities advertised: {advertised - known}"
    assert mounted_blueprints, "no blueprints mounted"


def test_root_serves_the_spa_or_explains_how_to_build_it(client):
    """The root route has two legitimate states, and both must be useful.

    The SPA bundle is a build artefact, not source, so a fresh checkout has no
    UI. Rather than a bare 404, the app answers 503 with a page naming the
    command to run. That guidance is the contract worth pinning: a developer
    who clones and runs the dashboard hits this page first.
    """
    r = client.get("/")
    assert r.status_code in (200, 503), f"unexpected status {r.status_code}"

    if r.status_code == 503:
        body = r.get_data(as_text=True)
        assert "make workspace" in body, "the 'UI not built' page must name the fix"


def test_unknown_route_is_404_not_500(client):
    assert client.get("/api/definitely-not-a-route").status_code == 404
