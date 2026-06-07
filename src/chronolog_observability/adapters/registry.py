"""Discover, probe, and select data-source adapters at runtime.

Adapters register under the ``chronolog_observability.adapters`` entry-point
group (see pyproject.toml). At startup the registry instantiates each, calls
``is_available()``, and keeps the ones that say yes. Blueprints then resolve a
capability to *some* active adapter that provides it.

This is the mechanism that lets the same wheel run:
  * on a bare ChronoLog host  -> only ChronoLogAdapter activates
  * inside a clio-core stack   -> ChronoLogAdapter + ChimaeraAdapter both activate
without any feature flags or conditional imports scattered through the code.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Optional

from .base import BaseAdapter, Capability, SourceAdapter

log = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "chronolog_observability.adapters"


class AdapterRegistry:
    def __init__(self) -> None:
        self._active: list[SourceAdapter] = []

    def discover(self) -> "AdapterRegistry":
        """Load every registered adapter and keep the available ones."""
        self._active = []
        for ep in entry_points(group=_ENTRY_POINT_GROUP):
            try:
                cls = ep.load()
            except Exception as exc:  # an optional adapter's deps may be absent
                log.info("adapter %r not loadable: %s", ep.name, exc)
                continue
            try:
                adapter = cls()
                if adapter.is_available():
                    self._active.append(adapter)
                    log.info("adapter %r active (%r)", ep.name, adapter)
                else:
                    log.info("adapter %r present but inactive", ep.name)
            except Exception as exc:
                log.warning("adapter %r failed to initialise: %s", ep.name, exc)
        return self

    def active(self) -> list[SourceAdapter]:
        return list(self._active)

    def for_capability(self, capability: Capability) -> Optional[SourceAdapter]:
        """First active adapter that provides ``capability`` (priority = entry
        order; ChronoLog is listed first so it wins generic capabilities)."""
        for adapter in self._active:
            if capability in adapter.capabilities():
                return adapter
        return None

    def has(self, capability: Capability) -> bool:
        return self.for_capability(capability) is not None

    def capability_map(self) -> dict[str, list[str]]:
        """For the ``/api/config`` endpoint and the frontend feature gating."""
        out: dict[str, list[str]] = {}
        for adapter in self._active:
            for cap in adapter.capabilities():
                out.setdefault(cap.value, []).append(adapter.name)
        return out


# Process-wide singleton, populated by app.create_app().
registry = AdapterRegistry()
