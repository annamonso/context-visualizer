"""Runtime configuration for the plugin.

Resolution order: explicit kwarg > environment > sane default. The two things a
ChronoLog operator must be able to point at are the visor endpoint and where the
built frontend lives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # ChronoLog visor — RAW IP, not the `-40g` hostname (Mercury rejects
    # hostnames with HG_PROTONOSUPPORT). See backend/__init__.py.
    visor_ip: str = ""
    visor_protocol: str = "ofi+sockets"
    # Demo/dev: replay from drained CSVs instead of a live deployment.
    offline: bool = False
    # Flask bind.
    host: str = "0.0.0.0"
    port: int = 5000

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            visor_ip=os.environ.get("CHRONOLOG_VISOR_IP", ""),
            visor_protocol=os.environ.get("CHRONOLOG_VISOR_PROTOCOL", "ofi+sockets"),
            offline=os.environ.get("CHRONOLOG_OFFLINE") == "1",
            host=os.environ.get("OBSERVE_HOST", "0.0.0.0"),
            port=int(os.environ.get("OBSERVE_PORT", "5000")),
        )
