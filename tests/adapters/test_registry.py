"""The adapter registry — the seam the whole design rests on.

Blueprints are mounted only when some active adapter provides their capability,
so the registry's answers decide which API exists. These tests pin that
contract, including the failure mode: an unprovided capability must raise
``SourceUnavailable``, not return ``None`` for a caller to dereference.
"""

import pytest

from chronolog_observability.adapters.base import Capability
from chronolog_observability.adapters.registry import (
    AdapterRegistry,
    SourceUnavailable,
)


def test_discover_finds_the_builtin_chronolog_adapter(state_dir):
    reg = AdapterRegistry().discover()
    assert "chronolog" in [a.name for a in reg.active()]


def test_capability_map_is_json_serialisable(state_dir):
    """/api/config serialises this straight to the SPA."""
    import json

    reg = AdapterRegistry().discover()
    json.dumps(reg.capability_map())


def test_has_and_for_capability_agree(state_dir):
    reg = AdapterRegistry().discover()
    for cap in Capability:
        if reg.has(cap):
            assert reg.for_capability(cap) is not None, f"{cap} claimed but unresolvable"
        else:
            assert reg.for_capability(cap) is None


def test_source_for_raises_on_an_unprovided_capability(state_dir):
    """Callers rely on the raise; returning None would NoneType-crash a view."""
    reg = AdapterRegistry()  # deliberately not discovered — nothing is active
    with pytest.raises(SourceUnavailable):
        reg.source_for(Capability.FLEET)


def test_discover_is_idempotent(state_dir):
    """The factory may rediscover; adapters must not accumulate."""
    reg = AdapterRegistry()
    first = len(reg.discover().active())
    second = len(reg.discover().active())
    assert first == second


def test_every_mapped_capability_is_a_real_enum_member(state_dir):
    reg = AdapterRegistry().discover()
    known = {c.value for c in Capability}
    assert set(reg.capability_map()) <= known
