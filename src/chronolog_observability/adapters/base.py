"""The data-source seam that makes this a *generic* ChronoLog plugin.

The original ``context-visualizer`` read live state directly from the chimaera
C++ runtime — 16 API modules did ``import chimaera_client`` unconditionally, so
the package could not even be imported on a host without chimaera. A generic
ChronoLog user has no chimaera, so that coupling has to move behind an interface.

A ``SourceAdapter`` supplies the **read primitives** the dashboard's shape layer
turns into views. Both shipped adapters implement the SAME primitive shapes
(``{node_id: ...}`` monitor wrapping included), so blueprints and shape adapters
never know which source served a request:

  * ``ChronoLogAdapter``  — the default. Serves the session / interaction /
    context-graph / recovery reads from ChronoLog stories (via
    ``backend.path_a_reader``). Available on *any* ChronoLog deployment.

  * ``ChimaeraAdapter``   — optional. Adds the *live runtime* reads (topology,
    workers, pools, …) by introspecting a running chimaera runtime. Only
    activates when ``chimaera_runtime_ext`` is importable.

Each adapter declares which ``Capability`` values it provides; ``app.create_app``
mounts only the blueprints whose capability some active adapter offers, so the
dashboard degrades gracefully instead of crashing on a missing import.

NOTE: the read-primitive methods below cover the *generic* (ChronoLog-served)
views. Live-runtime adapters add their own methods (``get_topology`` …); those
are accessed by duck-typing the adapter the registry returns for a live
``Capability``, so they don't bloat this shared interface.
"""

from __future__ import annotations

import abc
import enum
from typing import Any, Dict, List, Protocol, runtime_checkable


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
    """Structural contract the registry checks. Concrete adapters subclass
    :class:`BaseAdapter`; the Protocol lets third-party adapters loaded via
    entry points be duck-typed without importing them at definition time."""

    name: str

    def is_available(self) -> bool: ...
    def capabilities(self) -> "frozenset[Capability]": ...


class BaseAdapter(abc.ABC):
    """Common base: capability declaration + availability + the generic read
    primitives. Primitives default to "empty" so an adapter only overrides the
    ones it can actually serve."""

    name: str = "base"

    @abc.abstractmethod
    def is_available(self) -> bool:  # pragma: no cover - interface
        ...

    @abc.abstractmethod
    def capabilities(self) -> "frozenset[Capability]":  # pragma: no cover
        ...

    def provides(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    # ------------------------------------------------------------------
    # Generic read primitives — same names/shapes as the old chimaera_client.
    # Defaults return "empty" so blueprints degrade gracefully on a source
    # that doesn't implement a given read.
    # ------------------------------------------------------------------

    def get_sessions(self) -> Dict[str, Dict[str, Any]]:
        return {}

    def get_session_interactions(self, session_id: str) -> Dict[str, Dict[str, Any]]:
        return {}

    def get_interaction(self, session_id: str, seq_id: Any) -> Dict[str, Dict[str, Any]]:
        return {}

    def get_context_graphs(self) -> Dict[str, List[str]]:
        return {}

    def get_context_graph(self, session_id: str, since: int = 0) -> Dict[str, List[Dict[str, Any]]]:
        return {}

    def get_context_node(self, session_id: str, seq_id: Any) -> Dict[str, Dict[str, Any]]:
        return {}

    def get_recovery_events(self, session_id: str) -> List[Dict[str, Any]]:
        return []

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        avail = "available" if self.is_available() else "inactive"
        caps = ",".join(sorted(c.value for c in self.capabilities()))
        return f"<{type(self).__name__} name={self.name!r} {avail} caps=[{caps}]>"
