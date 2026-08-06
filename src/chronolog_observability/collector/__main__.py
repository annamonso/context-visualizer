"""CLI: python3 -m chronolog_observability.collector --flask-url http://node:5000

Environment expected on ARES (scripts/ops/launch-collectors.sh sets these):
  CHRONOLOG_VISOR_IP=...     raw 40g IP of the visor node (NOT the -40g hostname)
  CHRONOLOG_QUERY_PORT=...   node-unique reader/query port
"""

from __future__ import annotations

import argparse
import logging
import sys

from .daemon import DEFAULT_PORT, CollectorDaemon


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--flask-url", required=True,
                   help="Central dashboard base URL (live-stream forward target)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"Localhost ingest port (default {DEFAULT_PORT})")
    p.add_argument("--flush-interval", type=float, default=0.5)
    p.add_argument("--max-batch", type=int, default=64)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    CollectorDaemon(
        flask_url=args.flask_url,
        port=args.port,
        flush_interval=args.flush_interval,
        max_batch=args.max_batch,
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
