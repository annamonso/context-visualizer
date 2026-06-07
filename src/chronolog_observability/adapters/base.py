"""The data-source seam that makes this a *generic* ChronoLog plugin.

The original ``context-visualizer`` read live state directly from the chimaera
C++ runtime — 16 API modules did ``import chimaera_client`` unconditionally, so
the package could not even be imported on a host without chimaera. A generic
ChronoLog user has no chimaera, so that coupling has to move behind an interface.

A ``SourceAdapter`` is one way of supplying the views the dashboard renders.
Two adapters ship in-tree:

  * ``ChronoLogAdapter``  — the default. Reconstructs conversations, scenarios,
    interactions, provenance and overhead purely from ChronoLog stories
    (this is the existing Path-A reader / ReplayStory read path). Available on
    *any* ChronoLog deployment.

  * ``ChimaeraAdapter``   — optional. Adds the *live runtime* panels (topology,
    workers, pools, node, system, recovery) by introspecting a running chimaera
    runtime. Only activates when ``chimaera_runtime_ext`` is importable.

Each adapter declares which ``Capability`` values it provides. Blueprints ask
the registry for a capability and degrade gracefully (HTTP 501) when no active
adapter offers it, instead of crashing on import.
"""

from __future__ import annotations

import abc
import enum
from typing import Any, Iterable, Protocol, runtime_checkable


class Capability(enum.Enum):
    """A view the dashboard can render, supplied by zero or more adapters."""

    # Reconstructable from ChronoLog stories alone (the generic path):
    CONVERSATIONS = "conversations"
    SCENARIOS = "scenarios"
    INTERACTIONS = "interactions"
    PROVENANCE = "provenance"
    SEMANTIC = "semantic"
    OVERHEAD = "overhead"
    INTER_AGENT = "inter_agent"

    # Require a live runtime to introspect (chimaera-only today):
    TOPOLOGY = "topology"
    WORKERS = "workers"
    POOLS = "pools"
    NODE = "node"
    SYSTEM = "system"
    RECOVERY = "recovery"
    CHECKPOINTS = "checkpoints"


@runtime_checkable
class SourceAdapter(Protocol):
    """Minimal contract every data source implements.

    Concrete adapters subclass :class:`BaseAdapter`; the Protocol is here so
    the registry can structurally type-check third-party adapters loaded via
    entry points without importing them at definition time.
    """

    name: str

    def is_available(self) -> bool:
        """Cheap probe: can this adapter actually serve data right now?

        Must NOT raise and must NOT import heavy/native modules at call time
        beyond what is needed to answer. Used by the registry to decide whether
        to activate the adapter.
        """
        ...

    def capabilities(self) -> "frozenset[Capability]":
        """Which views this adapter can supply when available."""
        ...

    def fetch(self, capability: Capability, **params: Any) -> Any:
        """Return the view payload for ``capability`` (already JSON-shaped)."""
        ...


class BaseAdapter(abc.ABC):
    """Convenience base with sensible defaults."""

    name: str = "base"

    @abc.abstractmethod
    def is_available(self) -> bool:  # pragma: no cover - interface
        ...

    @abc.abstractmethod
    def capabilities(self) -> "frozenset[Capability]":  # pragma: no cover
        ...

    def provides(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    @abc.abstractmethod
    def fetch(self, capability: Capability, **params: Any) -> Any:  # pragma: no cover
        ...

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        avail = "available" if self.is_available() else "inactive"
        caps = ",".join(sorted(c.value for c in self.capabilities()))
        return f"<{type(self).__name__} name={self.name!r} {avail} caps=[{caps}]>"
