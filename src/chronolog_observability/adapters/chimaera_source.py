"""Optional adapter: live-runtime panels via the chimaera C++ runtime.

Only activates when ``chimaera_runtime_ext`` is importable, so the package
imports and runs cleanly on a host that has only ChronoLog. This is where the
old hard coupling is quarantined — the 16 modules that used to do
``from .. import chimaera_client`` should call into this adapter (or the
registry) instead of importing chimaera directly.

MIGRATION: move the old `chimaera_client.py` body next to this file (e.g.
`_chimaera_client.py`) and port the live-state endpoints (topology/workers/
pools/node/system/recovery/checkpoints) to delegate here.
"""

from __future__ import annotations

import importlib.util
from typing import Any

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
    }
)


class ChimaeraAdapter(BaseAdapter):
    name = "chimaera"

    def is_available(self) -> bool:
        # Cheap probe — does NOT import the native module, just checks presence.
        return importlib.util.find_spec("chimaera_runtime_ext") is not None

    def capabilities(self) -> "frozenset[Capability]":
        return _LIVE_CAPS

    def fetch(self, capability: Capability, **params: Any) -> Any:
        # TODO(migration): delegate to the ported chimaera_client wrapper.
        from . import _chimaera_client  # noqa: F401  (ported on migration)

        raise NotImplementedError(
            f"ChimaeraAdapter.fetch({capability}) not yet wired — "
            "port chimaera_client.py and the live-state endpoints."
        )
