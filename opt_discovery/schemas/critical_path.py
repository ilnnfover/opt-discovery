"""Critical-path schema contracts (CP-01).

This module defines auditable records only.  It does not read profiler exports,
build a DAG, solve a critical path, infer weights or query source data.

Field conventions:

* every time field is ``*_us`` and already in microseconds -- this module never
  scales units;
* a missing value is ``None`` (serialized as JSON ``null``), never ``0``;
* ``ExecutionNode.duration_us`` is stored independently from ``end_us`` so a
  short event at epoch scale is not lost by subtracting two large floats;
* an :class:`ExecutionNode` is identified by capture, source kind, a
  capture-relative logical path and a positive source row.  Its deterministic
  ``node_id`` deliberately ignores rank, stream, operator, start/duration and
  notes, so window clipping and rank back-fill cannot re-identify a record.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, TypeVar

from ..events import SourceReference
from ..primitives import Window
from .metrics import Provenance
from .operator import TaskType
from .quality import QualityFlag, blocking

__all__ = [
    "ActivityKind",
    "AnalysisScope",
    "CriticalPathResult",
    "CriticalPathStatus",
    "DependencyEdge",
    "DependencyEvidence",
    "DependencyType",
    "EvidenceBasis",
    "ExecutionNode",
    "UnresolvedDependency",
]

_EnumT = TypeVar("_EnumT", bound=Enum)

_STRONG_PROVENANCE = frozenset({Provenance.MEASURED, Provenance.DERIVED})


class ActivityKind(str, Enum):
    """What kind of activity a node records (not its TaskType)."""

    DEVICE_TASK = "device_task"
    HOST_CALL = "host_call"
    MARKER = "marker"


class DependencyType(str, Enum):
    """How a target node depends on a source node.

    ``HOST_DEVICE_SUBMISSION`` links host-call *start* to device-task *start*.
    It does not assert completion-to-start ordering or an implicit wait.
    """

    STREAM_ORDER = "stream_order"
    HOST_DEVICE_SUBMISSION = "host_device_submission"
    EXPLICIT_EVENT_WAIT = "explicit_event_wait"
    COLLECTIVE_DEPENDENCY = "collective_dependency"
    PHASE_DEPENDENCY = "phase_dependency"


class EvidenceBasis(str, Enum):
    """Structured, non-time ordering basis that can prove an edge."""

    STREAM_SEQUENCE = "stream_sequence"
    CONNECTION_ID = "connection_id"
    EVENT_HANDLE = "event_handle"
    COLLECTIVE_ID = "collective_id"
    EXPLICIT_PHASE_LINK = "explicit_phase_link"


#: Each dependency type only accepts the corresponding association basis.
_ALLOWED_BASES: dict[DependencyType, frozenset[EvidenceBasis]] = {
    DependencyType.STREAM_ORDER: frozenset({EvidenceBasis.STREAM_SEQUENCE}),
    DependencyType.HOST_DEVICE_SUBMISSION: frozenset({EvidenceBasis.CONNECTION_ID}),
    DependencyType.EXPLICIT_EVENT_WAIT: frozenset({EvidenceBasis.EVENT_HANDLE}),
    DependencyType.COLLECTIVE_DEPENDENCY: frozenset({EvidenceBasis.COLLECTIVE_ID}),
    DependencyType.PHASE_DEPENDENCY: frozenset({EvidenceBasis.EXPLICIT_PHASE_LINK}),
}


class CriticalPathStatus(str, Enum):
    """Coverage of the path itself, not the existing quality system."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNDETERMINED = "undetermined"
    UNAVAILABLE = "unavailable"


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _finite(value: object, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a finite number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite, got {value!r}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be >= {minimum}, got {number!r}")
    return number


def _optional_finite(
    value: object, field: str, *, minimum: float | None = None
) -> float | None:
    if value is None:
        return None
    return _finite(value, field, minimum=minimum)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer, got {value!r}")
    return value


def _non_negative_integer(value: object, field: str) -> int:
    number = _integer(value, field)
    if number < 0:
        raise ValueError(f"{field} must be non-negative, got {number}")
    return number


def _optional_non_negative_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _non_negative_integer(value, field)


def _positive_row(value: object) -> int:
    row = _integer(value, "source.row")
    if row <= 0:
        raise ValueError(f"source.row must be a positive integer, got {row}")
    return row


def _non_negative_integer_tuple(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list of integers")
    return tuple(_non_negative_integer(item, f"{field}[]") for item in value)


def _mapping_list(value: object, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array")
    items: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}[{index}] must be an object")
        items.append(item)
    return tuple(items)


def _quality_flags_from_dict(data: Mapping[str, Any]) -> tuple[QualityFlag, ...]:
    if "quality_flags" not in data:
        return ()
    raw = data["quality_flags"]
    if not isinstance(raw, list):
        raise ValueError("quality_flags must be a JSON array")
    flags: list[QualityFlag] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or "code" not in item:
            raise ValueError(f"quality_flags[{index}] must be an object with code")
        flags.append(QualityFlag.from_dict(item))
    return tuple(flags)


def _coerce_enum(enum_type: type[_EnumT], value: object, field: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        for member in enum_type.__members__.values():
            if member.value == text:
                return member
    raise ValueError(f"{field} is not a valid {enum_type.__name__}: {value!r}")


def _logical_source_path(path: str) -> str:
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/") or (len(text) >= 2 and text[0].isalpha() and text[1] == ":"):
        raise ValueError("source.path must be capture-relative, not absolute")
    logical = posixpath.normpath(text)
    if logical in ("", "."):
        raise ValueError("source.path must identify a record, not the current directory")
    if logical == ".." or logical.startswith("../"):
        raise ValueError("source.path must not escape the capture root")
    return logical


def _validated_source(source: object, *, require_row: bool) -> str:
    if not isinstance(source, SourceReference):
        raise TypeError(f"source must be SourceReference, got {type(source).__name__}")
    _require_text(source.kind, "source.kind")
    path = _require_text(source.path, "source.path")
    if source.row is None:
        if require_row:
            raise ValueError("source.row is required to identify an ExecutionNode")
    else:
        _positive_row(source.row)
    if source.note is not None and not isinstance(source.note, str):
        raise TypeError("source.note must be a string or None")
    return _logical_source_path(path)


def _source_to_dict(source: SourceReference) -> dict[str, Any]:
    return {
        "kind": source.kind,
        "path": source.path,
        "row": source.row,
        "note": source.note,
    }


def _source_from_dict(data: Mapping[str, Any]) -> SourceReference:
    return SourceReference(
        kind=data["kind"],
        path=data["path"],
        row=data.get("row"),
        note=data.get("note"),
    )


def _node_id(capture_id: str, source: SourceReference, logical_path: str) -> str:
    payload = json.dumps(
        [capture_id, source.kind, logical_path, source.row],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionNode:
    """One observed activity that may participate in a path.

    ``duration_us=None`` means the end is unknown; ``0.0`` is a valid measured
    duration.  ``end_us`` is a convenience property and must not be used to
    reconstruct ``duration_us`` at epoch scale.

    ``source.path`` is expected to be capture-relative, and ``source.row`` must
    be a positive CSV record number or immutable DB row locator supplied by the
    adapter.  A missing row is rejected rather than fabricated.
    """

    capture_id: str
    source: SourceReference
    activity: ActivityKind
    start_us: float
    duration_us: float | None
    rank: int | None = None
    stream_id: int | None = None
    operator: str | None = None
    task_type: TaskType | None = None
    node_id: str = field(init=False)

    def __post_init__(self) -> None:
        capture_id = _require_text(self.capture_id, "capture_id")
        logical_path = _validated_source(self.source, require_row=True)
        activity = _coerce_enum(ActivityKind, self.activity, "activity")
        start_us = _finite(self.start_us, "start_us")
        duration_us = _optional_finite(self.duration_us, "duration_us", minimum=0.0)
        if duration_us is not None:
            end = start_us + duration_us
            if not math.isfinite(end):
                raise ValueError(f"start_us + duration_us overflows: {end!r}")
        rank = _optional_non_negative_integer(self.rank, "rank")
        stream_id = _optional_non_negative_integer(self.stream_id, "stream_id")
        operator = _optional_text(self.operator, "operator")
        task_type = (
            None
            if self.task_type is None
            else _coerce_enum(TaskType, self.task_type, "task_type")
        )
        computed_id = _node_id(capture_id, self.source, logical_path)

        object.__setattr__(self, "capture_id", capture_id)
        object.__setattr__(self, "activity", activity)
        object.__setattr__(self, "start_us", start_us)
        object.__setattr__(self, "duration_us", duration_us)
        object.__setattr__(self, "rank", rank)
        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "task_type", task_type)
        object.__setattr__(self, "node_id", computed_id)

    @property
    def end_us(self) -> float | None:
        if self.duration_us is None:
            return None
        return self.start_us + self.duration_us

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "capture_id": self.capture_id,
            "source": _source_to_dict(self.source),
            "activity": self.activity.value,
            "start_us": self.start_us,
            "duration_us": self.duration_us,
            "rank": self.rank,
            "stream_id": self.stream_id,
            "operator": self.operator,
            "task_type": self.task_type.value if self.task_type is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionNode":
        node = cls(
            capture_id=data["capture_id"],
            source=_source_from_dict(data["source"]),
            activity=data["activity"],
            start_us=data["start_us"],
            duration_us=data.get("duration_us"),
            rank=data.get("rank"),
            stream_id=data.get("stream_id"),
            operator=data.get("operator"),
            task_type=data.get("task_type"),
        )
        if "node_id" in data:
            provided_id = data["node_id"]
            if not isinstance(provided_id, str) or provided_id != node.node_id:
                raise ValueError("node_id does not match the node's source identity")
        return node


@dataclass(frozen=True)
class DependencyEvidence:
    """Locatable source records plus a structured association basis.

    The schema does not query source data: callers remain responsible for
    judging whether the named fields actually establish the association.
    """

    source_refs: tuple[SourceReference, ...]
    basis: EvidenceBasis
    fields: tuple[str, ...]
    provenance: Provenance
    detail: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.source_refs, str) or not isinstance(
            self.source_refs, (tuple, list)
        ):
            raise TypeError("DependencyEvidence.source_refs must be a sequence")
        refs = tuple(self.source_refs)
        if not refs:
            raise ValueError("DependencyEvidence requires at least one source reference")
        for ref in refs:
            _validated_source(ref, require_row=True)

        if isinstance(self.fields, str) or not isinstance(self.fields, (tuple, list)):
            raise TypeError("DependencyEvidence.fields must be a sequence of names")
        fields = tuple(self.fields)
        if not fields:
            raise ValueError("DependencyEvidence requires at least one association field")
        normalized_fields = tuple(_require_text(name, "fields[]") for name in fields)

        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(
            self, "basis", _coerce_enum(EvidenceBasis, self.basis, "basis")
        )
        object.__setattr__(self, "fields", normalized_fields)
        object.__setattr__(
            self,
            "provenance",
            _coerce_enum(Provenance, self.provenance, "provenance"),
        )
        object.__setattr__(self, "detail", _optional_text(self.detail, "detail"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_refs": [_source_to_dict(ref) for ref in self.source_refs],
            "basis": self.basis.value,
            "fields": list(self.fields),
            "provenance": self.provenance.value,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DependencyEvidence":
        refs = tuple(
            _source_from_dict(item)
            for item in _mapping_list(data["source_refs"], "source_refs")
        )
        fields_value = data["fields"]
        if not isinstance(fields_value, list):
            raise ValueError("fields must be a JSON array")
        return cls(
            source_refs=refs,
            basis=data["basis"],
            fields=tuple(fields_value),
            provenance=data["provenance"],
            detail=data.get("detail"),
        )


@dataclass(frozen=True)
class DependencyEdge:
    """A proved ordering relation.  There is deliberately no edge weight."""

    source_node_id: str
    target_node_id: str
    dependency_type: DependencyType
    evidence: DependencyEvidence

    def __post_init__(self) -> None:
        source_id = _require_text(self.source_node_id, "source_node_id")
        target_id = _require_text(self.target_node_id, "target_node_id")
        if source_id == target_id:
            raise ValueError("a dependency edge cannot be a self-edge")
        if not isinstance(self.evidence, DependencyEvidence):
            raise TypeError("evidence must be DependencyEvidence")

        dependency_type = _coerce_enum(
            DependencyType, self.dependency_type, "dependency_type"
        )
        if self.evidence.provenance not in _STRONG_PROVENANCE:
            raise ValueError(
                "only measured/derived evidence can form a DependencyEdge; "
                "declared/assumed relations belong in UnresolvedDependency"
            )
        allowed = _ALLOWED_BASES[dependency_type]
        if self.evidence.basis not in allowed:
            raise ValueError(
                f"{dependency_type.value} requires basis "
                f"{sorted(item.value for item in allowed)}, "
                f"got {self.evidence.basis.value}"
            )

        object.__setattr__(self, "source_node_id", source_id)
        object.__setattr__(self, "target_node_id", target_id)
        object.__setattr__(self, "dependency_type", dependency_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "dependency_type": self.dependency_type.value,
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DependencyEdge":
        return cls(
            source_node_id=data["source_node_id"],
            target_node_id=data["target_node_id"],
            dependency_type=data["dependency_type"],
            evidence=DependencyEvidence.from_dict(data["evidence"]),
        )


@dataclass(frozen=True)
class UnresolvedDependency:
    """A relation that could not be proved with explicit evidence."""

    dependency_type: DependencyType
    reason_code: str
    source_node_id: str | None = None
    target_node_id: str | None = None
    source_refs: tuple[SourceReference, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        dependency_type = _coerce_enum(
            DependencyType, self.dependency_type, "dependency_type"
        )
        reason_code = _require_text(self.reason_code, "reason_code")
        source_id = _optional_text(self.source_node_id, "source_node_id")
        target_id = _optional_text(self.target_node_id, "target_node_id")

        if isinstance(self.source_refs, str) or not isinstance(
            self.source_refs, (tuple, list)
        ):
            raise TypeError("UnresolvedDependency.source_refs must be a sequence")
        refs = tuple(self.source_refs)
        for ref in refs:
            _validated_source(ref, require_row=False)

        object.__setattr__(self, "dependency_type", dependency_type)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "source_node_id", source_id)
        object.__setattr__(self, "target_node_id", target_id)
        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(self, "detail", _optional_text(self.detail, "detail"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency_type": self.dependency_type.value,
            "reason_code": self.reason_code,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "source_refs": [_source_to_dict(ref) for ref in self.source_refs],
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UnresolvedDependency":
        if "source_refs" not in data or data["source_refs"] is None:
            refs: tuple[SourceReference, ...] = ()
        else:
            refs = tuple(
                _source_from_dict(item)
                for item in _mapping_list(data["source_refs"], "source_refs")
            )
        return cls(
            dependency_type=data["dependency_type"],
            reason_code=data["reason_code"],
            source_node_id=data.get("source_node_id"),
            target_node_id=data.get("target_node_id"),
            source_refs=refs,
            detail=data.get("detail"),
        )


@dataclass(frozen=True)
class AnalysisScope:
    """What a result claims to cover.

    Empty ``ranks`` / ``stream_ids`` mean "unspecified", not "covers all".
    ``stream_ids`` are interpreted under each selected rank.  ``clock_domain``
    is only a common-axis label; it never proves that ranks are comparable.
    """

    capture_id: str
    workload_id: str
    is_synthetic: bool
    window: Window | None = None
    ranks: tuple[int, ...] = ()
    stream_ids: tuple[int, ...] = ()
    clock_domain: str | None = None

    def __post_init__(self) -> None:
        capture_id = _require_text(self.capture_id, "capture_id")
        workload_id = _require_text(self.workload_id, "workload_id")
        if not isinstance(self.is_synthetic, bool):
            raise TypeError("is_synthetic must be an explicit bool")

        if self.window is not None:
            if not isinstance(self.window, Window):
                raise TypeError("window must be a primitives.Window or None")
            for name, value in (
                ("window.start_us", self.window.start_us),
                ("window.end_us", self.window.end_us),
                ("window.duration_us", self.window.duration_us),
            ):
                _finite(value, name)

        object.__setattr__(self, "capture_id", capture_id)
        object.__setattr__(self, "workload_id", workload_id)
        object.__setattr__(
            self, "ranks", _non_negative_integer_tuple(self.ranks, "ranks")
        )
        object.__setattr__(
            self,
            "stream_ids",
            _non_negative_integer_tuple(self.stream_ids, "stream_ids"),
        )
        object.__setattr__(
            self, "clock_domain", _optional_text(self.clock_domain, "clock_domain")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "workload_id": self.workload_id,
            "is_synthetic": self.is_synthetic,
            "window": self.window.to_dict() if self.window is not None else None,
            "ranks": list(self.ranks),
            "stream_ids": list(self.stream_ids),
            "clock_domain": self.clock_domain,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisScope":
        if "window" not in data or data["window"] is None:
            window = None
        else:
            window_data = data["window"]
            if not isinstance(window_data, Mapping):
                raise ValueError("window must be a JSON object or null")
            window = Window.from_dict(dict(window_data))
        return cls(
            capture_id=data["capture_id"],
            workload_id=data["workload_id"],
            is_synthetic=data["is_synthetic"],
            window=window,
            ranks=data.get("ranks", ()),
            stream_ids=data.get("stream_ids", ()),
            clock_domain=data.get("clock_domain"),
        )


@dataclass(frozen=True)
class CriticalPathResult:
    """A contract for a caller-supplied path, not a path generator."""

    scope: AnalysisScope
    status: CriticalPathStatus
    path_nodes: tuple[ExecutionNode, ...] = ()
    path_edges: tuple[DependencyEdge, ...] = ()
    critical_path_duration_us: float | None = None
    observed_window_duration_us: float | None = None
    #: coverage = adjudicated recoverable dependencies / independently
    #: evidenced expected dependencies.  ``None`` means no trusted denominator
    #: exists; observed edge count is never used as the denominator.
    dependency_coverage: float | None = None
    unresolved_dependencies: tuple[UnresolvedDependency, ...] = ()
    quality_flags: tuple[QualityFlag, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AnalysisScope):
            raise TypeError("scope must be AnalysisScope")
        status = _coerce_enum(CriticalPathStatus, self.status, "status")

        nodes = tuple(self.path_nodes)
        for node in nodes:
            if not isinstance(node, ExecutionNode):
                raise TypeError("path_nodes must contain ExecutionNode values")
        edges = tuple(self.path_edges)
        for edge in edges:
            if not isinstance(edge, DependencyEdge):
                raise TypeError("path_edges must contain DependencyEdge values")
        unresolved = tuple(self.unresolved_dependencies)
        for item in unresolved:
            if not isinstance(item, UnresolvedDependency):
                raise TypeError(
                    "unresolved_dependencies must contain UnresolvedDependency values"
                )
        flags = tuple(self.quality_flags)
        for flag in flags:
            if not isinstance(flag, QualityFlag):
                raise TypeError("quality_flags must contain QualityFlag values")

        duration = _optional_finite(
            self.critical_path_duration_us, "critical_path_duration_us", minimum=0.0
        )
        coverage = _optional_finite(self.dependency_coverage, "dependency_coverage")
        if coverage is not None and not 0.0 <= coverage <= 1.0:
            raise ValueError(
                f"dependency_coverage must be within [0, 1], got {coverage!r}"
            )

        observed = _optional_finite(
            self.observed_window_duration_us,
            "observed_window_duration_us",
            minimum=0.0,
        )
        if self.scope.window is not None:
            derived = self.scope.window.duration_us
            if observed is None:
                observed = derived
            elif not math.isclose(observed, derived, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(
                    "observed_window_duration_us must match scope.window.duration_us"
                )
        elif observed is not None:
            raise ValueError(
                "observed_window_duration_us requires a known scope.window"
            )

        node_ids = [node.node_id for node in nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("path_nodes must not repeat a node_id")

        rank_filter = set(self.scope.ranks)
        stream_filter = set(self.scope.stream_ids)
        for node in nodes:
            if node.capture_id != self.scope.capture_id:
                raise ValueError("path node capture_id does not match scope.capture_id")
            if rank_filter and (node.rank is None or node.rank not in rank_filter):
                raise ValueError("path node rank is outside scope.ranks")
            if stream_filter and (
                node.stream_id is None or node.stream_id not in stream_filter
            ):
                raise ValueError("path node stream_id is outside scope.stream_ids")

        if len(edges) != max(0, len(nodes) - 1):
            raise ValueError(
                "path_edges must connect each adjacent pair of path_nodes exactly once"
            )
        if any(node.duration_us is None for node in nodes):
            raise ValueError("only nodes with a known duration_us may enter a path")

        for source, target, edge in zip(nodes, nodes[1:], edges, strict=False):
            if edge.source_node_id != source.node_id or edge.target_node_id != target.node_id:
                raise ValueError(
                    "path_edges are not adjacent to the ordered path_nodes"
                )
            if edge.dependency_type is not DependencyType.STREAM_ORDER:
                continue
            if (
                source.activity is not ActivityKind.DEVICE_TASK
                or target.activity is not ActivityKind.DEVICE_TASK
            ):
                raise ValueError("stream_order edges require device_task endpoints")
            if (
                source.stream_id is None
                or target.stream_id is None
                or source.stream_id != target.stream_id
            ):
                raise ValueError(
                    "stream_order edges require a known, identical stream_id"
                )
            if source.rank != target.rank:
                raise ValueError("stream_order edges require matching rank scope")
            source_duration = source.duration_us
            assert source_duration is not None
            if source_duration > target.start_us - source.start_us:
                raise ValueError(
                    "stream_order edges require the source to finish before the "
                    "target starts (compared as a start-time difference)"
                )

        has_blocking = bool(blocking(flags))
        if has_blocking and status is not CriticalPathStatus.UNAVAILABLE:
            raise ValueError("blocking quality flags require unavailable status")

        if status in (CriticalPathStatus.UNDETERMINED, CriticalPathStatus.UNAVAILABLE):
            if nodes or edges:
                raise ValueError(f"{status.value} status must not carry a path")
            if duration is not None:
                raise ValueError(f"{status.value} status must not carry a path duration")
            if status is CriticalPathStatus.UNAVAILABLE and not flags and not unresolved:
                raise ValueError(
                    "unavailable status requires a quality flag or an unresolved reason"
                )
        elif status is CriticalPathStatus.COMPLETE:
            if not nodes:
                raise ValueError("complete status requires an actual path")
            if unresolved:
                raise ValueError("complete status must not carry unresolved dependencies")
            if self.scope.window is None or self.scope.clock_domain is None:
                raise ValueError("complete status requires a known window and clock domain")
            if coverage is None or coverage != 1.0:
                raise ValueError(
                    "complete status requires dependency_coverage == 1.0 "
                    "from an adjudicated denominator"
                )
            if duration is None:
                raise ValueError("complete status requires a path duration")
        elif not nodes:
            raise ValueError("partial status requires an explicit local path")

        object.__setattr__(self, "status", status)
        object.__setattr__(self, "path_nodes", nodes)
        object.__setattr__(self, "path_edges", edges)
        object.__setattr__(self, "critical_path_duration_us", duration)
        object.__setattr__(self, "observed_window_duration_us", observed)
        object.__setattr__(self, "dependency_coverage", coverage)
        object.__setattr__(self, "unresolved_dependencies", unresolved)
        object.__setattr__(self, "quality_flags", flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "status": self.status.value,
            "path_nodes": [node.to_dict() for node in self.path_nodes],
            "path_edges": [edge.to_dict() for edge in self.path_edges],
            "critical_path_duration_us": self.critical_path_duration_us,
            "observed_window_duration_us": self.observed_window_duration_us,
            "dependency_coverage": self.dependency_coverage,
            "unresolved_dependencies": [
                item.to_dict() for item in self.unresolved_dependencies
            ],
            "quality_flags": [flag.to_dict() for flag in self.quality_flags],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CriticalPathResult":
        return cls(
            scope=AnalysisScope.from_dict(data["scope"]),
            status=data["status"],
            path_nodes=tuple(
                ExecutionNode.from_dict(item) for item in data.get("path_nodes") or ()
            ),
            path_edges=tuple(
                DependencyEdge.from_dict(item)
                for item in data.get("path_edges") or ()
            ),
            critical_path_duration_us=data.get("critical_path_duration_us"),
            observed_window_duration_us=data.get("observed_window_duration_us"),
            dependency_coverage=data.get("dependency_coverage"),
            unresolved_dependencies=tuple(
                UnresolvedDependency.from_dict(item)
                for item in _mapping_list(data.get("unresolved_dependencies", []), "unresolved_dependencies")
            ),
            quality_flags=_quality_flags_from_dict(data),
        )
