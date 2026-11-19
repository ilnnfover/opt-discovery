"""Standardized, streaming profiler event readers.

The readers below are the shared boundary between raw Ascend Profiler artefacts
and analyzers:

* :class:`KernelEvent` — one per-instance ``op_summary`` row
  (freeze: never build these from aggregate ``op_statistic`` rows);
* :class:`TaskIntervalEvent` — the explicit ``TASK`` table fallback; it has no
  operator identity and is therefore a different type;
* :class:`HostCall` — ACL/CANN host-side calls (``CANN_API``).

All timestamps are epoch microseconds.  The only unit conversions live in
``primitives.normalize_duration`` / ``window_span`` (``ns -> us`` for sqlite and
the COMMUNICATION ``Task Duration`` ns quirk).  Readers are lazy generators;
callers that need sorting or interval merging materialize the list explicitly.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .primitives import (
    NS_PER_US,
    Interval,
    Window,
    normalize_duration,
    window_span,
)
from .schemas.operator import TaskType

__all__ = [
    "EventReadError",
    "HostCall",
    "KernelEvent",
    "SourceReference",
    "NormalizedRow",
    "TaskIntervalEvent",
    "iter_host_calls",
    "iter_host_spans",
    "iter_op_summary_events",
    "iter_op_summary_intervals",
    "iter_op_summary_spans",
    "iter_task_intervals",
    "iter_task_span_rows",
    "iter_task_spans",
    "op_summary_paths",
]

OP_SUMMARY_START = "Task Start Time(us)"
OP_SUMMARY_DURATION = "Task Duration(us)"
OP_SUMMARY_TYPE = "Task Type"
OP_SUMMARY_OPERATOR = "OP Type"
OP_SUMMARY_STREAM = "Stream ID"

_KERNEL_TYPES = frozenset({"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV"})


class EventReadError(ValueError):
    """Raised when a source cannot provide the contracted event fields."""


@dataclass(frozen=True)
class SourceReference:
    """Where an event came from; enough to locate and audit the input."""

    kind: str
    path: str
    row: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class KernelEvent:
    operator: str
    rank: int | None
    start_us: float
    duration_us: float
    task_type: TaskType
    source: SourceReference
    stream_id: int | None = None

    @property
    def end_us(self) -> float:
        return self.start_us + self.duration_us

    def interval(self) -> Interval:
        return Interval(self.start_us, self.end_us)


@dataclass(frozen=True)
class NormalizedRow:
    """One instance row after unit/task-type/window normalization."""

    start_us: float
    duration_us: float
    task_type: TaskType
    operator: str | None
    stream_id: int | None


@dataclass(frozen=True)
class TaskIntervalEvent:
    """TASK-table fallback: per-task interval without operator identity."""

    rank: int | None
    start_us: float
    duration_us: float
    source: SourceReference

    @property
    def end_us(self) -> float:
        return self.start_us + self.duration_us

    def interval(self) -> Interval:
        return Interval(self.start_us, self.end_us)


@dataclass(frozen=True)
class HostCall:
    name: str
    start_us: float
    duration_us: float
    source: SourceReference

    @property
    def end_us(self) -> float:
        return self.start_us + self.duration_us

    def interval(self) -> Interval:
        return Interval(self.start_us, self.end_us)


def _task_type(raw: str | None) -> TaskType:
    text = (raw or "").strip().upper()
    if not text:
        return TaskType.UNKNOWN
    if text == "COMMUNICATION":
        return TaskType.COMMUNICATION
    if text in _KERNEL_TYPES:
        return TaskType.KERNEL
    if text == "MEMCPY":
        return TaskType.MEMCPY
    if text == "SYNC":
        return TaskType.SYNC
    return TaskType.OTHER


def _indices(
    header: list[str], path: Path
) -> tuple[int, int, int | None, int | None, int | None]:
    try:
        start_idx = header.index(OP_SUMMARY_START)
        duration_idx = header.index(OP_SUMMARY_DURATION)
    except ValueError as exc:
        raise EventReadError(
            f"{path}: 缺少 {OP_SUMMARY_START}/{OP_SUMMARY_DURATION} 列"
        ) from exc
    type_idx = header.index(OP_SUMMARY_TYPE) if OP_SUMMARY_TYPE in header else None
    operator_idx = header.index(OP_SUMMARY_OPERATOR) if OP_SUMMARY_OPERATOR in header else None
    stream_idx = header.index(OP_SUMMARY_STREAM) if OP_SUMMARY_STREAM in header else None
    return start_idx, duration_idx, type_idx, operator_idx, stream_idx


def normalize_instance_row(
    row: list[str],
    *,
    start_idx: int | None,
    duration_idx: int,
    type_idx: int | None = None,
    operator_idx: int | None = None,
    stream_idx: int | None = None,
    start_scale: float = 1.0,
    duration_scale: float = 1.0,
    window: Window | None = None,
    policy: str = "intersect",
    resolver=_task_type,
    number_parser=float,
    allow_zero_duration: bool = False,
) -> NormalizedRow | None:
    """Shared normalization for one instance row (Parser and Timeline both use it).

    ``start_idx=None`` is allowed for no-window Parser paths; the returned start
    is 0.0 and the caller must not use it for window/round logic.
    """
    if duration_idx >= len(row):
        return None
    if start_idx is not None and start_idx >= len(row):
        return None
    try:
        parsed_duration = number_parser(row[duration_idx])
        parsed_start = (
            number_parser(row[start_idx]) if start_idx is not None else 0.0
        )
    except (TypeError, ValueError):
        return None
    if parsed_duration is None or parsed_start is None:
        return None
    duration_value = float(parsed_duration)
    start_value = float(parsed_start)
    if duration_value < 0.0 or (duration_value == 0.0 and not allow_zero_duration):
        return None
    raw_type = row[type_idx] if type_idx is not None and type_idx < len(row) else None
    task_type = resolver(raw_type)
    duration_us = normalize_duration(
        duration_value, unit_scale=duration_scale, task_type=task_type
    )
    span = window_span(start_value * start_scale, duration_us, window, policy=policy)
    if span is None:
        return None
    operator = (
        row[operator_idx] if operator_idx is not None and operator_idx < len(row) else None
    )
    stream_id = (
        _optional_int(row[stream_idx])
        if stream_idx is not None and stream_idx < len(row)
        else None
    )
    return NormalizedRow(
        start_us=span[0],
        duration_us=span[1],
        task_type=task_type,
        operator=operator.strip() if operator is not None else None,
        stream_id=stream_id,
    )


def _optional_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _iter_op_rows(
    path: str | Path,
    window: Window | None,
    clip: bool,
) -> Iterator[tuple[float, float, TaskType, str | None, int | None, int]]:
    source_path = Path(path)
    if not source_path.is_file():
        raise EventReadError(f"{source_path}: 文件不存在")
    with source_path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return
        start_idx, duration_idx, type_idx, operator_idx, stream_idx = _indices(
            header, source_path
        )
        policy = "clip" if clip else "intersect"
        for row_number, row in enumerate(reader, start=2):
            normalized = normalize_instance_row(
                row,
                start_idx=start_idx,
                duration_idx=duration_idx,
                type_idx=type_idx,
                operator_idx=operator_idx,
                stream_idx=stream_idx,
                window=window,
                policy=policy,
            )
            if normalized is None:
                continue
            yield (
                normalized.start_us,
                normalized.duration_us,
                normalized.task_type,
                normalized.operator or None,
                normalized.stream_id,
                row_number,
            )


def iter_op_summary_events(
    path: str | Path,
    *,
    window: Window | None = None,
    clip: bool = False,
    rank: int | None = None,
) -> Iterator[KernelEvent]:
    """Stream per-instance kernel events from an ``op_summary`` CSV.

    Rows without ``OP Type`` have no operator identity; they are only exposed by
    :func:`iter_op_summary_intervals`, not as :class:`KernelEvent`.
    """
    source_path = Path(path)
    for start_us, duration_us, task_type, operator, stream_id, row_number in _iter_op_rows(
        path, window, clip
    ):
        if operator is None:
            continue
        yield KernelEvent(
            operator=operator,
            rank=rank,
            start_us=start_us,
            duration_us=duration_us,
            task_type=task_type,
            source=SourceReference(kind="op_summary", path=str(source_path), row=row_number),
            stream_id=stream_id,
        )


def iter_op_summary_spans(
    path: str | Path,
    *,
    window: Window | None = None,
    clip: bool = False,
) -> Iterator[tuple[float, float]]:
    """``(start_us, duration_us)`` view that preserves epoch-compatible duration."""
    for start_us, duration_us, _task, _operator, _stream, _row in _iter_op_rows(
        path, window, clip
    ):
        yield start_us, duration_us


def iter_op_summary_intervals(
    path: str | Path,
    *,
    window: Window | None = None,
    clip: bool = False,
) -> Iterator[Interval]:
    """Interval-only view (keeps rows without operator identity)."""
    for start_us, duration_us, _task, _operator, _stream, _row in _iter_op_rows(
        path, window, clip
    ):
        yield Interval(start_us, start_us + duration_us)


def _natural_key(path: Path) -> list[object]:
    parts: list[object] = []
    token = ""
    for char in path.name:
        if char.isdigit():
            token += char
            continue
        if token:
            parts.append(int(token))
            token = ""
        parts.append(char)
    if token:
        parts.append(int(token))
    return parts


def op_summary_paths(db_path: str | Path) -> list[Path]:
    """All ``op_summary`` slices for a rank, in profiler natural order."""
    root = Path(db_path).parent / "mindstudio_profiler_output"
    return sorted(root.glob("op_summary_*.csv"), key=_natural_key)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return any(
        row[0] == name
        for row in connection.execute("select name from sqlite_master where type='table'")
    )


def _iter_task_rows(
    db_path: str | Path,
    window: Window | None,
    origin_us: float | None = 0.0,
) -> Iterator[tuple[float, float]]:
    path = Path(db_path)
    if not path.is_file():
        raise EventReadError(f"{path}: 文件不存在")
    connection = sqlite3.connect(str(path))
    try:
        if not _has_table(connection, "TASK"):
            raise EventReadError(f"{path}: 缺少 TASK 表（该 rank 的聚合可能失败）")
        sql = "select startNs, endNs from TASK"
        params: list[float] = []
        if window is not None:
            sql += " where endNs >= ? and startNs <= ?"
            params = [window.start_us * NS_PER_US, window.end_us * NS_PER_US]
        sql += " order by startNs"
        origin_ns = int(origin_us * NS_PER_US) if origin_us is not None else None
        for row in connection.execute(sql, params):
            start_ns = int(row[0])
            if origin_ns is None:
                origin_ns = start_ns
            duration_ns = int(row[1]) - start_ns
            yield (start_ns - origin_ns) / NS_PER_US, duration_ns / NS_PER_US
    finally:
        connection.close()


def iter_task_intervals(
    db_path: str | Path,
    *,
    window: Window | None = None,
    rank: int | None = None,
) -> Iterator[TaskIntervalEvent]:
    """Stream ``TASK`` rows as intervals (integer ns diff -> us)."""
    path = str(Path(db_path))
    for start_us, duration_us in _iter_task_rows(db_path, window):
        yield TaskIntervalEvent(
            rank=rank,
            start_us=start_us,
            duration_us=duration_us,
            source=SourceReference(kind="task_table", path=path),
        )


def iter_task_span_rows(
    db_path: str | Path,
    *,
    window: Window | None = None,
    origin_us: float | None = 0.0,
) -> Iterator[tuple[float, float]]:
    """``(start_us, duration_us)`` TASK view (epoch-safe duration).

    ``origin_us`` 把绝对 epoch 时间平移到小坐标；不传时以首个任务为原点。
    """
    yield from _iter_task_rows(db_path, window, origin_us)


def iter_task_spans(
    db_path: str | Path, *, window: Window | None = None
) -> Iterator[Interval]:
    """Interval-only TASK view (timeline fallback)."""
    for start_us, duration_us in _iter_task_rows(db_path, window):
        yield Interval(start_us, start_us + duration_us)


def iter_host_spans(
    db_path: str | Path,
    *,
    window: Window | None = None,
    origin_us: float | None = 0.0,
) -> Iterator[tuple[float, float, str]]:
    """Tuple view ``(start_us, duration_us, name)`` for timeline attribution."""
    path = Path(db_path)
    if not path.is_file():
        raise EventReadError(f"{path}: 文件不存在")
    connection = sqlite3.connect(str(path))
    try:
        if not _has_table(connection, "CANN_API"):
            raise EventReadError(f"{path}: 缺少 CANN_API 表（Host 侧事件未采集）")
        sql = (
            "select a.startNs, a.endNs, s.value from CANN_API a "
            "left join STRING_IDS s on a.name = s.id"
        )
        params: list[float] = []
        if window is not None:
            sql += " where a.endNs >= ? and a.startNs <= ?"
            params = [window.start_us * NS_PER_US, window.end_us * NS_PER_US]
        sql += " order by a.startNs"
        origin_ns = int(origin_us * NS_PER_US) if origin_us is not None else None
        for row in connection.execute(sql, params):
            start_ns = int(row[0])
            if origin_ns is None:
                origin_ns = start_ns
            duration_ns = int(row[1]) - start_ns
            yield (
                (start_ns - origin_ns) / NS_PER_US,
                duration_ns / NS_PER_US,
                str(row[2] or "unknown"),
            )
    finally:
        connection.close()


def iter_host_calls(
    db_path: str | Path, *, window: Window | None = None
) -> Iterator[HostCall]:
    """Stream ``CANN_API`` host calls (integer ns diff -> us)."""
    path = str(Path(db_path))
    for start_us, duration_us, name in iter_host_spans(db_path, window=window):
        yield HostCall(
            name=name,
            start_us=start_us,
            duration_us=duration_us,
            source=SourceReference(kind="cann_api", path=path),
        )
