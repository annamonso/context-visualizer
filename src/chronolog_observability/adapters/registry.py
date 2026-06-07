"""Discover, probe, and select data-source adapters at runtime.

Adapters register under the ``chronolog_observability.adapters`` entry-point
group (see pyproject.toml), plus a built-in default. At startup the registry
instantiates each, calls ``is_available()``, and keeps the ones that say yes.
Blueprints then resolve a capability to *some* active adapter that provides it.

The shipped default is ``ChronoLogAdapter``; the entry-point group is the seam
for third parties to register additional data sources without forking.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Optional

from .base import Capability, SourceAdapter

log = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "chronolog_observability.adapters"


class SourceUnavailable(RuntimeError):
    """No active adapter provides the requested capability."""

# Built-in adapters, registered even when the package is run from source (no
# editable install, so no entry-point metadata). Third parties extend the set
# by registering under the entry-point group above.
_BUILTIN = [
    ("chronolog", "chronolog_observability.adapters.chronolog_source:ChronoLogAdapter"),
]


def _load(target: str):
    """Load a ``module:attr`` target string into the class object."""
    import importlib

    mod_name, _, attr = target.partition(":")
    return getattr(importlib.import_module(mod_name), attr)


class AdapterRegistry:
    def __init__(self) -> None:
        self._active: list[SourceAdapter] = []

    def discover(self) -> "AdapterRegistry":
        """Load built-in + entry-point adapters; keep the available ones."""
        self._active = []
        seen: set[str] = set()

        candidates: list[tuple[str, object]] = []
        for name, target in _BUILTIN:
            candidates.append((name, target))
        for ep in entry_points(group=_ENTRY_POINT_GROUP):
            candidates.append((ep.name, ep))

        for name, ref in candidates:
            if name in seen:
                continue  # built-in wins over a same-named entry point
            try:
                cls = ref.load() if hasattr(ref, "load") else _load(ref)  # type: ignore[union-attr]
            except Exception as exc:  # an optional adapter's deps may be absent
                log.info("adapter %r not loadable: %s", name, exc)
                continue
            try:
                adapter = cls()
                if adapter.is_available():
                    self._active.append(adapter)
                    seen.add(name)
                    log.info("adapter %r active (%r)", name, adapter)
                else:
                    seen.add(name)
                    log.info("adapter %r present but inactive", name)
            except Exception as exc:
                log.warning("adapter %r failed to initialise: %s", name, exc)
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

    def source_for(self, capability: Capability) -> SourceAdapter:
        """Like ``for_capability`` but raises when nothing provides it — used by
        blueprints, which are only mounted when a provider exists, so this should
        not fire in practice."""
        src = self.for_capability(capability)
        if src is None:
            raise SourceUnavailable(f"no active adapter provides {capability.value}")
        return src

    def capability_map(self) -> dict[str, list[str]]:
        """For the ``/api/config`` endpoint and the frontend feature gating."""
        out: dict[str, list[str]] = {}
        for adapter in self._active:
            for cap in adapter.capabilities():
                out.setdefault(cap.value, []).append(adapter.name)
        return out


# Process-wide singleton, populated by app.create_app().
registry = AdapterRegistry()
