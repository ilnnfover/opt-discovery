"""Analysis layer: steady-state window, hotspots, cross-workload comparison."""

from .comm import CommVolume, CommVolumeError, summarize_comm
from .contract import ContractCheck, ContractReport, compare_contracts
from .graph_coverage import GraphCoverage, OpCoverage, summarize_coverage
from .compare import ComparisonReport, OperatorDelta, compare_tables
from .hotspot import OperatorHotspot, hotspot_matrix, operator_hotspots
from .timeline import (
    SUBMIT_APIS,
    GapAttribution,
    TimelineError,
    TimelineSummary,
    annotate_diagnosis,
    summarize,
)
from .window import Window, WindowQuality, select_rounds_window

__all__ = [
    "GraphCoverage",
    "OpCoverage",
    "summarize_coverage",
    "ContractCheck",
    "ContractReport",
    "compare_contracts",
    "CommVolume",
    "CommVolumeError",
    "summarize_comm",
    "ComparisonReport",
    "OperatorDelta",
    "compare_tables",
    "OperatorHotspot",
    "hotspot_matrix",
    "operator_hotspots",
    "Window",
    "WindowQuality",
    "select_rounds_window",
    "SUBMIT_APIS",
    "GapAttribution",
    "TimelineError",
    "TimelineSummary",
    "annotate_diagnosis",
    "summarize",
]
