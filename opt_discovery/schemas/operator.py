"""Operator profile schema.

This is the P0 contract of the discovery pipeline.  Rules, hotspot analysis and
opportunity generation all consume :class:`OperatorTable` and nothing else, so a
new profiler version only requires a new parser.

Field conventions:

* time fields are :class:`Metric` in microseconds, or ``None`` when not observed;
* ``operator`` is the canonical name, ``raw_name`` keeps the profiler string;
* ``extra`` holds numeric metrics whose availability depends on the profiler
  version and the enabled collection switches (utilizations, busy times, bytes);
* ``attributes`` holds textual leftovers (raw shape strings, raw task type).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .metrics import Metric, Provenance
from .workload import WorkloadSpec

__all__ = [
    "TaskType",
    "SourceRef",
    "OperatorRecord",
    "ParseWarning",
    "ParseMeta",
    "OperatorTable",
    "OperatorSchemaError",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1

#: canonical field -> unit, used by :meth:`OperatorRecord.metric`
_CORE_METRIC_UNITS: dict[str, str] = {
    "calls": "count",
    "total_time_us": "us",
    "avg_time_us": "us",
    "min_time_us": "us",
    "max_time_us": "us",
    "device_time_us": "us",
}


class OperatorSchemaError(ValueError):
    """Raised when an operator record violates the schema."""


class TaskType(str, Enum):
    KERNEL = "kernel"
    MEMCPY = "memcpy"
    COMMUNICATION = "communication"
    SYNC = "sync"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceRef:
    """Where an operator is implemented / called from (optional)."""

    file: str | None = None
    line: int | None = None
    symbol: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "symbol": self.symbol}


@dataclass(frozen=True)
class OperatorRecord:
    workload_id: str
    operator: str
    calls: int
    total_time_us: Metric | None = None
    avg_time_us: Metric | None = None
    min_time_us: Metric | None = None
    max_time_us: Metric | None = None
    device_time_us: Metric | None = None
    kernel: str | None = None
    shape: tuple[int, ...] | None = None
    dtype: str | None = None
    task_type: TaskType = TaskType.KERNEL
    source: SourceRef | None = None
    extra: Mapping[str, Metric] = field(default_factory=dict)
    attributes: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.workload_id:
            raise OperatorSchemaError("workload_id is required")
        if not self.operator:
            raise OperatorSchemaError("operator is required")
        if isinstance(self.calls, bool) or not isinstance(self.calls, int):
            raise OperatorSchemaError(f"calls must be int, got {self.calls!r}")
        if self.calls < 0:
            raise OperatorSchemaError(f"calls must be non-negative, got {self.calls}")
        for name in _CORE_METRIC_UNITS:
            metric = getattr(self, name, None)
            if isinstance(metric, Metric) and metric.unit != _CORE_METRIC_UNITS[name]:
                # normalize instead of rejecting; unit is part of the parser contract
                if _CORE_METRIC_UNITS[name] == "us":
                    object.__setattr__(self, name, metric.as_time_us())
        if self.shape is not None and not isinstance(self.shape, tuple):
            object.__setattr__(self, "shape", tuple(int(d) for d in self.shape))
        if not isinstance(self.task_type, TaskType):
            object.__setattr__(self, "task_type", TaskType(str(self.task_type)))

    # -- derived views ---------------------------------------------------
    @property
    def effective_avg_time_us(self) -> Metric | None:
        """Explicit average if measured, otherwise ``total / calls``."""
        if self.avg_time_us is not None:
            return self.avg_time_us
        if self.total_time_us is not None and self.calls > 0:
            return Metric(
                self.total_time_us.value / self.calls,
                "us",
                Provenance.DERIVED,
                f"{self.total_time_us.source or 'total_time_us'} / calls",
            )
        return None

    def metric(self, name: str) -> Metric | None:
        """Look up a numeric field by canonical name (core first, then extra)."""
        if name == "calls":
            return Metric(float(self.calls), "count", Provenance.MEASURED, "record.calls")
        if name in _CORE_METRIC_UNITS:
            return getattr(self, name, None)
        return self.extra.get(name)

    # -- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "workload_id": self.workload_id,
            "operator": self.operator,
            "raw_name": self.attributes.get("raw_operator"),
            "kernel": self.kernel,
            "shape": list(self.shape) if self.shape is not None else None,
            "shape_raw": self.attributes.get("raw_shape"),
            "dtype": self.dtype,
            "task_type": self.task_type.value,
            "calls": self.calls,
            "total_time_us": _metric_dict(self.total_time_us),
            "avg_time_us": _metric_dict(self.avg_time_us),
            "min_time_us": _metric_dict(self.min_time_us),
            "max_time_us": _metric_dict(self.max_time_us),
            "device_time_us": _metric_dict(self.device_time_us),
            "metrics": {k: m.to_dict() for k, m in self.extra.items()},
            "source": self.source.to_dict() if self.source else None,
            "attributes": dict(self.attributes),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperatorRecord":
        source = data.get("source")
        return cls(
            workload_id=data["workload_id"],
            operator=data["operator"],
            calls=int(data.get("calls", 0)),
            total_time_us=_metric_or_none(data.get("total_time_us")),
            avg_time_us=_metric_or_none(data.get("avg_time_us")),
            min_time_us=_metric_or_none(data.get("min_time_us")),
            max_time_us=_metric_or_none(data.get("max_time_us")),
            device_time_us=_metric_or_none(data.get("device_time_us")),
            kernel=data.get("kernel"),
            shape=tuple(data["shape"]) if data.get("shape") else None,
            dtype=data.get("dtype"),
            task_type=TaskType(str(data.get("task_type", TaskType.KERNEL.value))),
            source=SourceRef(**source) if source else None,
            extra={k: Metric.from_dict(v) for k, v in (data.get("metrics") or {}).items()},
            attributes=dict(data.get("attributes") or {}),
            notes=tuple(data.get("notes") or ()),
        )


@dataclass(frozen=True)
class ParseWarning:
    code: str
    message: str
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "location": self.location}


@dataclass
class ParseMeta:
    """Audit trail of a parse run.  Downstream code must surface warnings."""

    parser: str
    parser_version: str = "0"
    source_files: tuple[str, ...] = ()
    row_count: int = 0
    skipped_rows: int = 0
    warnings: list[ParseWarning] = field(default_factory=list)
    unmapped_columns: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    is_synthetic: bool = False
    #: 解析器附加的结构化审计信息（窗口质量、文件切片、通信量等）。
    #: 使用 dict 而非 Mapping：解析器需要在其上增量写入（见 csv_mapping.parse）。
    extra: dict[str, Any] = field(default_factory=dict)

    #: 同一 code 最多保留的告警数，避免逐行告警淹没报告
    WARNINGS_PER_CODE = 50

    def add_warning(
        self, code: str, message: str, location: str | None = None, cap: int | None = None
    ) -> None:
        limit = self.WARNINGS_PER_CODE if cap is None else cap
        if sum(1 for warning in self.warnings if warning.code == code) >= limit:
            return
        self.warnings.append(ParseWarning(code, message, location))

    def to_dict(self) -> dict[str, Any]:
        return {
            "parser": self.parser,
            "parser_version": self.parser_version,
            "source_files": list(self.source_files),
            "row_count": self.row_count,
            "skipped_rows": self.skipped_rows,
            "warnings": [w.to_dict() for w in self.warnings],
            "unmapped_columns": list(self.unmapped_columns),
            "missing_columns": list(self.missing_columns),
            "is_synthetic": self.is_synthetic,
            "extra": dict(self.extra),
        }


@dataclass
class OperatorTable:
    """All operator records of one workload, plus how they were obtained."""

    workload: WorkloadSpec
    records: list[OperatorRecord] = field(default_factory=list)
    meta: ParseMeta = field(default_factory=lambda: ParseMeta(parser="unknown"))
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for record in self.records:
            if record.workload_id != self.workload.workload_id:
                raise OperatorSchemaError(
                    f"record workload_id {record.workload_id!r} != table workload_id "
                    f"{self.workload.workload_id!r}"
                )

    # -- aggregates needed by rules --------------------------------------
    @property
    def workload_id(self) -> str:
        return self.workload.workload_id

    def total_operator_time_us(self) -> float | None:
        """Sum of measured operator total time.

        Returns ``None`` when no record reports total time, so callers can tell
        "unknown" from "zero".  In parallel execution this sum can exceed
        end-to-end latency; it is a time-share denominator only.
        """
        values = [r.total_time_us.value for r in self.records if r.total_time_us is not None]
        if not values:
            return None
        return float(sum(values))

    def by_operator(self) -> dict[str, list[OperatorRecord]]:
        grouped: dict[str, list[OperatorRecord]] = {}
        for record in self.records:
            grouped.setdefault(record.operator, []).append(record)
        return grouped

    def iter_kernels(self) -> Iterator[OperatorRecord]:
        return (r for r in self.records if r.task_type is TaskType.KERNEL)

    # -- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workload": self.workload.to_dict(),
            "meta": self.meta.to_dict(),
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperatorTable":
        meta_raw = dict(data.get("meta") or {})
        meta = ParseMeta(
            parser=meta_raw.get("parser", "unknown"),
            parser_version=meta_raw.get("parser_version", "0"),
            source_files=tuple(meta_raw.get("source_files") or ()),
            row_count=meta_raw.get("row_count", 0),
            skipped_rows=meta_raw.get("skipped_rows", 0),
            warnings=[ParseWarning(**w) for w in meta_raw.get("warnings") or []],
            unmapped_columns=tuple(meta_raw.get("unmapped_columns") or ()),
            missing_columns=tuple(meta_raw.get("missing_columns") or ()),
            is_synthetic=bool(meta_raw.get("is_synthetic", False)),
            extra=meta_raw.get("extra") or {},
        )
        return cls(
            workload=WorkloadSpec.from_dict(data["workload"]),
            records=[OperatorRecord.from_dict(r) for r in data.get("records") or []],
            meta=meta,
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target

    @classmethod
    def read_json(cls, path: str | Path) -> "OperatorTable":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _metric_dict(metric: Metric | None) -> dict[str, Any] | None:
    return metric.to_dict() if metric is not None else None


def _metric_or_none(data: Mapping[str, Any] | None) -> Metric | None:
    return Metric.from_dict(dict(data)) if data else None


def records_by_task_type(records: Iterable[OperatorRecord]) -> dict[TaskType, int]:
    """Small helper used by parsers/reports to summarize task composition."""
    counts: dict[TaskType, int] = {}
    for record in records:
        counts[record.task_type] = counts.get(record.task_type, 0) + 1
    return counts
