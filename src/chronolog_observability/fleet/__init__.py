"""Fleet Health — per-node rollups that scale the dashboard to 100+ nodes.

The Cluster tab draws a node-link graph: gorgeous at 8 nodes, unreadable at 100,
and it re-scans raw events (O(events)) on every poll. Fleet Health instead works
off **per-(host, minute) rollup buckets**: aggregating those is O(nodes x
buckets), so the view stays responsive no matter how many events flowed.

Layers:
  rollup -> pure aggregation: raw records -> Buckets -> NodeHealth
  store  -> persist/read rollup buckets to the ChronoLog ``metrics_rollup``
            chronicle (local JSONL in offline mode)
  worker -> periodically emit rollups (opt-in)

The Fleet API reads pre-aggregated buckets when present and otherwise computes
NodeHealth on the fly from raw records, so the view works immediately — with or
without the emitter running.
"""

from .rollup import Bucket, NodeHealth, bucketize, health_from_buckets, health_from_raw

__all__ = [
    "Bucket",
    "NodeHealth",
    "bucketize",
    "health_from_buckets",
    "health_from_raw",
]
