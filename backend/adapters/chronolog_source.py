"""Default adapter: serve every generic view from ChronoLog stories alone.

This is what makes the plugin usable by *any* ChronoLog operator. The read
primitives delegate to ``backend.path_a_reader``, which replays ChronoLog
stories and returns the exact ``{node_id: ...}`` monitor shapes the old
the legacy runtime client returned — so the shape adapters and blueprints are
source-agnostic. Everything here is served from ChronoLog.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseAdapter, Capability

_GENERIC_CAPS = frozenset(
    {
        Capability.CONVERSATIONS,
        Capability.SCENARIOS,
        Capability.INTERACTIONS,
        Capability.PROVENANCE,
        Capability.SEMANTIC,
        Capability.INTER_AGENT,
        Capability.RECOVERY,
        Capability.CLUSTER,
        # New analytics views — all reconstructable from the same ChronoLog
        # stories (interactions + inter-agent edges + recovery events), so the
        # default adapter serves them on any deployment.
        Capability.DIAGNOSTICS,
        Capability.FLEET,
        Capability.TRACING,
    }
)


class ChronoLogAdapter(BaseAdapter):
    name = "chronolog"

    def is_available(self) -> bool:
        # Available whenever ChronoLog can serve reads (live client present, or
        # offline CSV-replay demo mode). See backend.is_reachable().
        try:
            from .. import chronolog

            return chronolog.is_reachable()
        except Exception:
            return False

    def capabilities(self) -> "frozenset[Capability]":
        return _GENERIC_CAPS

    # --- generic read primitives (delegate to the ChronoLog read path) ---

    def get_sessions(self) -> Dict[str, Dict[str, Any]]:
        from ..chronolog import path_a_reader

        return path_a_reader.get_sessions()

    def get_session_interactions(self, session_id: str) -> Dict[str, Dict[str, Any]]:
        from ..chronolog import path_a_reader

        return path_a_reader.get_session_interactions(session_id)

    def get_interaction(self, session_id: str, seq_id: Any) -> Dict[str, Dict[str, Any]]:
        from ..chronolog import path_a_reader

        return path_a_reader.get_interaction(session_id, seq_id)

    def get_context_graphs(self) -> Dict[str, List[str]]:
        from ..chronolog import path_a_reader

        return path_a_reader.get_context_graphs()

    def get_context_graph(self, session_id: str, since: int = 0) -> Dict[str, List[Dict[str, Any]]]:
        from ..chronolog import path_a_reader

        return path_a_reader.get_context_graph(session_id, since)

    def get_context_node(self, session_id: str, seq_id: Any) -> Dict[str, Dict[str, Any]]:
        from ..chronolog import path_a_reader

        return path_a_reader.get_context_node(session_id, seq_id)

    def get_recovery_events(self, session_id: str) -> List[Dict[str, Any]]:
        from ..chronolog import path_a_reader

        return path_a_reader.get_recovery_events(session_id)
