"""Optional adapter: live-runtime panels via the chimaera C++ runtime.

Only activates when ``chimaera_runtime_ext`` is importable, so the package
imports and runs cleanly on a host that has only ChronoLog. This is where the
old hard coupling is quarantined — the modules that used to do
``from .. import chimaera_client`` will call into this adapter instead.

It implements the same generic read primitives as ChronoLogAdapter (live
sessions/interactions) PLUS the live-only reads (topology/workers/…), which
blueprints reach by duck-typing the adapter the registry returns for a live
Capability.

MIGRATION: port the old ``chimaera_client.py`` body (already staged at
``adapters/_chimaera_client.py``) and delegate the primitives to it.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Dict, List

from .base import BaseAdapter, Capability

_LIVE_CAPS = frozenset(
    {
        Capability.TOPOLOGY,
        Capability.WORKERS,
        Capability.POOLS,
        Capability.NODE,
        Capability.SYSTEM,
        Capability.RECOVERY,
        Capability.CHECKPOINTS,
        # OVERHEAD measures the chimaera proxy/tracker/untangler instrumentation
        # (get_proxy_dispatch_stats, ...) — not reconstructable from ChronoLog,
        # so it is a live-runtime capability, not a generic one.
        Capability.OVERHEAD,
    }
)


class ChimaeraAdapter(BaseAdapter):
    name = "chimaera"

    def is_available(self) -> bool:
        # Cheap probe — does NOT import the native module, just checks presence.
        return importlib.util.find_spec("chimaera_runtime_ext") is not None

    def capabilities(self) -> "frozenset[Capability]":
        return _LIVE_CAPS

    # --- generic read primitives (live runtime), ported on migration ---

    def get_sessions(self) -> Dict[str, Dict[str, Any]]:
        return self._client().get_sessions()

    def get_session_interactions(self, session_id: str) -> Dict[str, Dict[str, Any]]:
        return self._client().get_session_interactions(session_id)

    def get_context_graph(self, session_id: str, since: int = 0) -> Dict[str, List[Dict[str, Any]]]:
        return self._client().get_context_graph(session_id, since)

    # --- live-only reads (topology/workers/...) added during group-B port ---

    def _client(self):
        # TODO(migration): return the ported chimaera_client wrapper.
        from . import _chimaera_client  # noqa: F401

        raise NotImplementedError(
            "ChimaeraAdapter not yet wired — port chimaera_client.py "
            "(staged at adapters/_chimaera_client.py) and the live endpoints."
        )
