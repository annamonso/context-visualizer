"""ChronoLog substrate — the storage layer this plugin assumes is running.

In the old project ChronoLog was an *optional* backend gated by
``DTP_CHRONOLOG_BACKEND`` / ``DUAL_WRITE`` / ``READS`` flags, sitting behind a
legacy JSONL/CSV store. As a ChronoLog *plugin* the relationship inverts:
ChronoLog is the required substrate, so those phase-migration flags collapse.

What stays:
  * the lazy import of ``py_chronolog_client`` (it is a C++-built module placed
    on PYTHONPATH by the deployment, never pip-installed),
  * the process-wide writer/reader singleton + StoryHandle cache,
  * the visor-IP resolution (raw IP, not the ``-40g`` hostname — Mercury returns
    HG_PROTONOSUPPORT for hostnames).

What changes:
  * ``DTP_CHRONOLOG_BACKEND`` no longer toggles existence; it is assumed on.
  * keep a single ``CHRONOLOG_OFFLINE=1`` escape hatch for demo/dev that replays
    from drained CSVs instead of a live deployment (the demo already relies on
    this fallback because ReplayStory has a ~180s drain lag).

MIGRATION: move the old `chronolog/client.py`, `constants.py`,
`path_a_reader.py`, `sync_worker.py` into this package and re-export
`get_backend`, `reset_backend`, `ChronoLogBackend`, `ChronoLogUnavailable` here.
"""

from __future__ import annotations

import importlib.util
import os


def is_reachable() -> bool:
    """True when ChronoLog can serve reads — used by ChronoLogAdapter.is_available."""
    if os.environ.get("CHRONOLOG_OFFLINE") == "1":
        return True  # CSV-replay demo mode
    if importlib.util.find_spec("py_chronolog_client") is None:
        return False
    # TODO(migration): after porting client.py, do a real liveness check here
    # (resolve visor IP + cheap connect), mirroring the old get_backend() path.
    return True


# TODO(migration): re-export the ported facade:
# from .client import (
#     get_backend, reset_backend, ChronoLogBackend, ChronoLogUnavailable,
#     CL_SUCCESS, CL_ERR_CHRONICLE_EXISTS,
# )
