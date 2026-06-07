"""ChronoLog substrate — the storage layer this plugin assumes is running.

In the old project ChronoLog was an *optional* backend gated by
``DTP_CHRONOLOG_BACKEND`` / ``DUAL_WRITE`` / ``READS`` flags, sitting behind a
legacy JSONL/CSV store. As a ChronoLog *plugin* the relationship inverts:
ChronoLog is the required substrate, so those phase-migration flags are gone.
``get_backend()`` returns a live backend in normal operation, and ``None`` only
in ``CHRONOLOG_OFFLINE=1`` demo mode (CSV replay served by the adapter).

What stays from the original backend:
  * the lazy import of ``py_chronolog_client`` (a C++-built module placed on
    PYTHONPATH by the deployment, never pip-installed),
  * the process-wide writer/reader singleton + StoryHandle cache,
  * the visor-IP resolution (raw IP, not the ``-40g`` hostname — Mercury returns
    HG_PROTONOSUPPORT for hostnames).

Env vars are read under the ``CHRONOLOG_*`` prefix with legacy ``DTP_CHRONOLOG_*``
names accepted as fallback (see ``client._env``).

NOTE: the old ``sync_worker.py`` (which polled the chimaera runtime and wrote
deltas to ChronoLog) is Path-B *capture*, not storage — it moves to
``chronolog_observability.capture`` so this package stays chimaera-free.
"""

from __future__ import annotations

import importlib.util

from . import constants
from .client import (
    CL_SUCCESS,
    CL_ERR_CHRONICLE_EXISTS,
    BackendConfig,
    ChronoLogBackend,
    ChronoLogUnavailable,
    get_backend,
    offline_mode,
    reset_backend,
)

__all__ = [
    "CL_SUCCESS",
    "CL_ERR_CHRONICLE_EXISTS",
    "BackendConfig",
    "ChronoLogBackend",
    "ChronoLogUnavailable",
    "get_backend",
    "reset_backend",
    "offline_mode",
    "is_reachable",
    "constants",
]


def is_reachable() -> bool:
    """True when the ChronoLogAdapter can serve reads.

    * offline demo mode  -> True (reads come from drained CSVs)
    * live mode          -> True only when ``py_chronolog_client`` is importable
      (the C++ client built into the deployment). A deeper liveness probe
      (resolve visor IP + connect) happens lazily on first ``get_backend()`` use.
    """
    if offline_mode():
        return True
    return importlib.util.find_spec("py_chronolog_client") is not None
