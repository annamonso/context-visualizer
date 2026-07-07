"""Per-node collector daemon for inter-agent events.

One collector runs on each compute node. Local agents POST to it on
localhost; it batches, writes directly to ChronoLog through the node-local
keeper, and forwards a thin copy to the central dashboard only for the live
SSE stream. The dashboard stops being the durability bottleneck — if it
dies, no event is lost.

    agent --localhost--> CollectorDaemon --batch--> ChronoLog (local keeper)
                                         \\--thin copy--> dashboard /ingest (live view)
"""

from .client import emit  # noqa: F401
from .daemon import CollectorDaemon  # noqa: F401
