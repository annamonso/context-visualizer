"""Chronicle and Story naming conventions.

Centralised here so callers never embed raw strings. Unchanged from the
original context-visualizer backend — these names define the on-disk ChronoLog
layout a deployment already holds, so they are a compatibility contract.
"""

# Stand-alone chronicles, each holding one or more Stories.
CHRONICLE_LLM_INTERACTIONS = "llm_interactions"
CHRONICLE_CONTEXT_GRAPHS   = "context_graphs"
CHRONICLE_RECOVERY_EVENTS  = "recovery_events"
CHRONICLE_CHECKPOINTS      = "checkpoints"

# Fleet rollups: one Chronicle holding pre-aggregated per-(host, minute) metric
# buckets. Reading these is O(nodes x buckets) instead of O(events), which is
# what lets the Fleet Health view stay responsive at 100+ nodes. One Story per
# bucket granularity; only "minute" is emitted today.
CHRONICLE_METRICS_ROLLUP   = "metrics_rollup"
ROLLUP_STORY_MINUTE        = "minute"

# Inter-agent edges are one Chronicle per scenario, one Story (edges) per chronicle.
INTER_AGENT_CHRONICLE_PREFIX = "ia_"
INTER_AGENT_STORY_EDGES      = "edges"

# The index chronicle / stories solve the missing `ShowChronicles` Python binding.
# Every scenario, session, and host that's ever been seen appends a one-off
# event here so listing operations have something to ReplayStory against.
CHRONICLE_INDEX           = "__index__"
INDEX_STORY_SCENARIOS     = "scenarios"
INDEX_STORY_SESSIONS      = "sessions"
INDEX_STORY_HOSTS         = "hosts"


def inter_agent_chronicle(scenario_id: str) -> str:
    """ia_<sanitised-scenario-id>. Sanitisation matches the legacy JSONL store."""
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in scenario_id)[:120]
    return f"{INTER_AGENT_CHRONICLE_PREFIX}{safe}"
