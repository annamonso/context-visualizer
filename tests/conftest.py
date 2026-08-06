"""Shared fixtures.

Every test runs the app in offline mode against a throwaway state directory, so
the suite needs no ChronoLog cluster, never touches a developer's real spool,
and stays order-independent.

``py_chronolog_client`` is deliberately absent here: offline mode never
constructs a live client, and CI has no ChronoLog build. A test that needs it
would be testing the deployment, not this package.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """An isolated state directory, exported the way the app expects it."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("DTP_STATE_DIR", str(d))
    monkeypatch.setenv("DEMO_STATE_DIR", str(d))
    monkeypatch.setenv("CHRONOLOG_OFFLINE", "1")
    # Keep background workers out of the tests: they would race the assertions
    # and touch the network.
    monkeypatch.delenv("CHRONOLOG_CAPTURE", raising=False)
    monkeypatch.delenv("CHRONOLOG_DOCTOR", raising=False)
    monkeypatch.delenv("CHRONOLOG_FLEET_ROLLUP", raising=False)
    monkeypatch.delenv("CHRONOLOG_DOCTOR_LLM", raising=False)
    return d


@pytest.fixture
def app(state_dir):
    """The Flask app, built through the real factory in offline mode."""
    # Imported inside the fixture so the env above is already in place when the
    # module-level config is read.
    from chronolog_observability.app import create_app

    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()
