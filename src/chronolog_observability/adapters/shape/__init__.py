"""Story->view shapers (conversation graph, scenario graph). chimaera-free.

Pure functions over already-fetched data, re-exported here so blueprints can
``from ..adapters.shape import build_agent_graph`` without reaching into modules.
"""

from .conversation import (
    base_session_id,
    build_agent_graph,
    build_conversation_summary,
    build_conversation_turns,
    build_interaction_detail,
    flatten_monitor_result,
    infer_role,
)
from .scenario import (
    build_scenario_graph,
    build_scenario_summary,
    build_scenario_timeline,
    parse_iso,
    participants,
    strip_scope,
)

__all__ = [
    "base_session_id",
    "build_agent_graph",
    "build_conversation_summary",
    "build_conversation_turns",
    "build_interaction_detail",
    "flatten_monitor_result",
    "infer_role",
    "build_scenario_graph",
    "build_scenario_summary",
    "build_scenario_timeline",
    "parse_iso",
    "participants",
    "strip_scope",
]
