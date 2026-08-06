"""PrismaDoctor — error detection, clustering, and diagnosis over ChronoLog stories.

PrismaDoctor reads the same stories the dashboard already serves (LLM
interactions, inter-agent edges, recovery events), turns failures into
``Signal`` objects, clusters them into ``Incident`` objects by signature, and
attaches a root-cause + remediation ``diagnosis``. A self-compacting
``IncidentMemory`` document accrues findings across runs so a repeat offender is
recognised instantly.

Layering (each layer is import-light and unit-testable in isolation):

  gather  -> raw records pulled from the read path (the only I/O layer)
  detect  -> records  -> List[Signal]            (pure)
  cluster -> signals  -> List[Incident]          (pure)
  diagnose-> incident -> diagnosis dict          (pure for the heuristic engine)
  memory  -> incidents -> self-compacting markdown digest
  scan    -> orchestrates the above into a Report
  worker  -> runs scan() on a timer (opt-in daemon)
"""

from .detectors import Signal, detect_all
from .incidents import Incident, cluster_signals
from .diagnoser import diagnose, HeuristicDiagnoser
from .scan import Report, run_scan

__all__ = [
    "Signal",
    "detect_all",
    "Incident",
    "cluster_signals",
    "diagnose",
    "HeuristicDiagnoser",
    "Report",
    "run_scan",
]
