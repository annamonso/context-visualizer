"""The data-source seam of the plugin.

A ``SourceAdapter`` supplies the **read primitives** the dashboard's shape layer
turns into views, returning the ``{node_id: ...}`` monitor shapes the blueprints
and shape adapters expect — so the rest of the code never knows where the data
came from. The shipped adapter is:

  * ``ChronoLogAdapter`` — serves the session / interaction / context-graph /
    recovery reads from ChronoLog stories (via ``backend.path_a_reader``).
    Available on *any* ChronoLog deployment.

The interface is kept so third parties can register additional data sources
under the ``chronolog_observability.adapters`` entry-point group; each declares
which ``Capability`` values it provides, and ``app.create_app`` mounts only the
blueprints whose capability some active adapter offers.
"""

from __future__ import annotations

import abc
import enum
from typing import Any, Dict, List, Protocol, runtime_checkable


class Capability(enum.Enum):
    """A view the dashboard can render, supplied by zero or more adapters.

    All capabilities are reconstructable from ChronoLog stories."""

    CONVERSATIONS = "conversations"
    SCENARIOS = "scenarios"
    INTERACTIONS = "interactions"
    PROVENANCE = "provenance"
    SEMANTIC = "semantic"
    INTER_AGENT = "inter_agent"
    RECOVERY = "recovery"
    CLUSTER = "cluster"  # ChronoLog deployment view: keepers per node, chronicles
    DIAGNOSTICS = "diagnostics"  # PrismaDoctor: detect/diagnose errors across stories
    FLEET = "fleet"  # Fleet health: per-node rollups, scales to 100+ nodes
    TRACING = "tracing"  # Distributed critical-path tracing across inter-agent edges


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
    # Generic read primitives — same names/shapes as the legacy runtime client.
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
