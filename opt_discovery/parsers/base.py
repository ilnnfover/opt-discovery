"""Parser interface for profiler exports.

A parser is responsible for two things only: read a file, produce
:class:`~opt_discovery.schemas.operator.OperatorTable` with an honest
:class:`~opt_discovery.schemas.operator.ParseMeta`.  Analysis never happens here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..schemas.operator import OperatorTable
from ..schemas.workload import WorkloadSpec

__all__ = ["ParserError", "OperatorParser", "ParserCapabilities", "BaseParser"]


class ParserError(RuntimeError):
    """Raised when a file cannot be parsed at all (as opposed to partly)."""


@dataclass(frozen=True)
class ParserCapabilities:
    """What a parser can promise; used to fail fast instead of half-guessing."""

    provides_timeline: bool = False
    provides_shape: bool = False
    provides_hardware_metrics: bool = False
    provides_source_location: bool = False
    requires_column_mapping: bool = True


@runtime_checkable
class OperatorParser(Protocol):
    """Structural interface every profiler parser must satisfy."""

    name: str
    version: str
    capabilities: ParserCapabilities

    def can_parse(self, path: Path) -> bool:
        """Cheap sniff test (file existence, header names), no full parse."""

    def parse(self, path: Path, workload: WorkloadSpec) -> OperatorTable:
        """Parse one file for one workload."""
