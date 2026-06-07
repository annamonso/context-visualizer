"""Default adapter: serve every generic view from ChronoLog stories alone.

This is what makes the plugin usable by *any* ChronoLog operator. It wraps the
existing read path that already lives in the codebase:

    backend.get_backend()            -> writer/reader ChronoLog client facade
    backend.path_a_reader            -> decode Path-A proxy-capture stories
    adapters.shape.conversation_*    -> shape stories into the conversation graph
    adapters.shape.scenario_*        -> shape inter-agent events into scenarios

MIGRATION: port the bodies of the old `api/chronolog_view.py`,
`chronolog/path_a_reader.py`, and the conversation/scenario shapers in here (or
have this delegate to them). Nothing here may import chimaera.
"""

from __future__ import annotations

from typing import Any

from .base import BaseAdapter, Capability

_GENERIC_CAPS = frozenset(
    {
        Capability.CONVERSATIONS,
        Capability.SCENARIOS,
        Capability.INTERACTIONS,
        Capability.PROVENANCE,
        Capability.SEMANTIC,
        Capability.OVERHEAD,
        Capability.INTER_AGENT,
    }
)


class ChronoLogAdapter(BaseAdapter):
    name = "chronolog"

    def is_available(self) -> bool:
        # Available whenever the ChronoLog Python client can be reached.
        try:
            from .. import backend

            return backend.is_reachable()
        except Exception:
            return False

    def capabilities(self) -> "frozenset[Capability]":
        return _GENERIC_CAPS

    def fetch(self, capability: Capability, **params: Any) -> Any:
        # TODO(migration): dispatch to the ported read/shape functions.
        raise NotImplementedError(
            f"ChronoLogAdapter.fetch({capability}) not yet wired — "
            "port the read path from the old chronolog/ + adapters/ modules."
        )
