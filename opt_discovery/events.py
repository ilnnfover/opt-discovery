"""Standardized, streaming profiler event readers.

The readers below are the shared boundary between raw Ascend Profiler artefacts
and analyzers:

* :class:`KernelEvent` — one per-instance ``op_summary`` row
  (freeze: never build these from aggregate ``op_statistic`` rows);
* :class:`TaskIntervalEvent` — the explicit ``TASK`` table fallback; it has no
  operator identity and is therefore a different type;
* :class:`HostCall` — ACL/CANN host-side calls (``CANN_API``).

Readers return microseconds, using epoch or an explicit relative origin.
Conversions happen at the source boundary. TASK SQLite timestamps use ns and
the CSV COMMUNICATION duration quirk belongs to ``normalize_duration``.
"""

from __future__ import annotations

import csv
import posixpath
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from .primitives import (
    NS_PER_US,
    Interval,
    Window,
    normalize_duration,
    window_span,
)
from .schemas.operator import TaskType

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids schemas -> events cycle
    from .schemas.critical_path import ExecutionNode

__all__ = [
    "EventReadError",
    "HostCall",
    "KernelEvent",
    "SourceReference",
    "NormalizedRow",
    "TaskExecutionEvent",
    "TaskIntervalEvent",
    "TASK_SERIAL_TYPES",
    "iter_host_calls",
    "iter_host_spans",
    "iter_op_summary_events",
    "iter_op_summary_intervals",
    "iter_op_summary_spans",
    "iter_task_execution_events",
    "iter_task_intervals",
    "iter_task_span_rows",
    "iter_task_spans",
    "ns_from_us",
    "task_origin_ns",
    "task_relative_window",
    "op_summary_paths",
]

OP_SUMMARY_START = "Task Start Time(us)"
OP_SUMMARY_DURATION = "Task Duration(us)"
OP_SUMMARY_TYPE = "Task Type"
OP_SUMMARY_OPERATOR = "OP Type"
OP_SUMMARY_STREAM = "Stream ID"

_KERNEL_TYPES = frozenset({"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV"})

#: TASK types whose stream order is proved by the official CANN FIFO contract.
#: Everything outside this exact allow list is descriptive only and may not
#: become a verified path endpoint.
TASK_SERIAL_TYPES = frozenset({"KERNEL_AICORE", "KERNEL_AIVEC", "MEMCPY_ASYNC"})

#: Observed UINT32_MAX placeholder used by model/context on standalone runs.
_UINT32_MAX = 4_294_967_295

_TASK_REQUIRED_COLUMNS = (
    "startNs",
    "endNs",
    "deviceId",
    "globalPid",
    "contextId",
    "streamId",
    "taskId",
    "globalTaskId",
    "modelId",
    "taskType",
)


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
class TaskExecutionEvent:
    """One ``TASK`` row with its raw identity and a descriptive node.

    ``raw_*_ns`` keep the exact integer source values; ``start_us`` and
    ``duration_us`` are relative to :attr:`origin_ns` (normally the selected
    window start) so an epoch-scale float subtraction cannot hide sub-µs time.

    ``node`` is descriptive: unknown task types and boundary crossers can still
    be inspected, but only records accepted by
    :data:`TASK_SERIAL_TYPES` with a known duration may become verified path
    endpoints. A missing identity stays ``None``. The raw ``context_id`` can
    carry the observed subgraph placeholder and does not identify a runtime
    context.
    """

    capture_id: str
    source: SourceReference
    node: ExecutionNode | None
    rank: int
    device_id: int | None
    stream_id: int | None
    global_pid: int | None
    context_id: int | None
    task_id: int | None
    global_task_id: int | None
    model_id: int | None
    task_type_name: str | None
    task_type: TaskType
    raw_start_ns: int | None
    raw_end_ns: int | None
    origin_ns: int
    in_window: bool
    crosses_boundary: bool
    #: ``non_graph`` only when the raw model ID is the observed UINT32_MAX
    #: sentinel; ``graph`` when a real model ID exists; ``missing`` otherwise.
    model_id_state: str = "missing"
    reasons: tuple[str, ...] = ()

    @property
    def start_us(self) -> float | None:
        if self.raw_start_ns is None:
            return None
        return (self.raw_start_ns - self.origin_ns) / NS_PER_US

    @property
    def end_us(self) -> float | None:
        duration = self.duration_us
        if duration is None or self.start_us is None:
            return None
        return self.start_us + duration

    @property
    def raw_end_us(self) -> float | None:
        """Observed end even when the interval crosses the window boundary."""
        if self.raw_end_ns is None:
            return None
        return (self.raw_end_ns - self.origin_ns) / NS_PER_US

    @property
    def duration_us(self) -> float | None:
        if not self.duration_valid or self.raw_end_ns is None or self.raw_start_ns is None:
            return None
        return (self.raw_end_ns - self.raw_start_ns) / NS_PER_US

    @property
    def duration_valid(self) -> bool:
        return (
            self.raw_start_ns is not None
            and self.raw_end_ns is not None
            and self.raw_end_ns >= self.raw_start_ns
            and not self.crosses_boundary
        )


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
    task_type_override: TaskType | None = None,
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
    task_type = task_type_override or resolver(raw_type)
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


def _sqlite_int(value: object) -> int | None:
    """Use the SQLite integer storage class for exact identities and times."""
    return value if type(value) is int else None


def _identity_id(value: object) -> int | None:
    """Return a real non-negative ID; sentinels and negatives are not identities."""
    number = _sqlite_int(value)
    if number is None or number < 0 or number >= _UINT32_MAX:
        return None
    return number


def _task_type_for_name(name: str | None) -> TaskType:
    if name is None:
        return TaskType.UNKNOWN
    text = name.strip().upper()
    if not text:
        return TaskType.UNKNOWN
    if text.startswith("KERNEL_"):
        return TaskType.KERNEL
    if text.startswith("MEMCPY"):
        return TaskType.MEMCPY
    if "COMMUNICATION" in text or text.startswith("HCCL"):
        return TaskType.COMMUNICATION
    if any(token in text for token in ("WAIT", "EVENT", "NOTIFY", "SYNC")):
        return TaskType.SYNC
    return TaskType.OTHER


def _capture_relative_path(path: object) -> str:
    if not isinstance(path, str):
        raise EventReadError("source_path 必须是字符串")
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if (
        not text
        or text.startswith("/")
        or (len(text) >= 2 and text[0].isalpha() and text[1] == ":")
    ):
        raise EventReadError(f"source_path 必须是 capture 相对路径: {path!r}")
    logical = posixpath.normpath(text)
    if logical in ("", ".", "..") or logical.startswith("../"):
        raise EventReadError(f"source_path 不能逃逸 capture 根目录: {path!r}")
    return logical


def _task_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute("pragma table_info(TASK)")
        if len(row) > 1
    }


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


def ns_from_us(value_us: float) -> int:
    """µs -> integer ns without losing fractional µs at epoch scale.

    ``value * 1000`` is evaluated as one float multiplication, whose ULP at
    1.79e18 is 256 ns. Splitting the integer and fractional parts keeps
    sub-µs window boundaries exact (e.g. E+0.25 -> E*1000+250).
    """
    whole = int(value_us)
    return whole * 1000 + int(round((value_us - whole) * 1000))


_ns = ns_from_us


def task_origin_ns(db_path: str | Path) -> int:
    """First TASK start in integer ns (used as a common attribution origin)."""
    path = Path(db_path)
    if not path.is_file():
        raise EventReadError(f"{path}: 文件不存在")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise EventReadError(f"{path}: 无法打开 sqlite: {exc}") from exc
    try:
        if not _has_table(connection, "TASK"):
            raise EventReadError(f"{path}: 缺少 TASK 表（该 rank 的聚合可能失败）")
        row = connection.execute(
            "select startNs from TASK order by startNs limit 1"
        ).fetchone()
        if row is None:
            raise EventReadError(f"{path}: TASK 表为空")
        return int(row[0])
    except sqlite3.Error as exc:
        raise EventReadError(f"{path}: 读取 TASK 失败: {exc}") from exc
    finally:
        connection.close()


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return any(
        row[0] == name
        for row in connection.execute("select name from sqlite_master where type='table'")
    )


def _iter_task_rows(
    db_path: str | Path,
    window: Window | None,
    origin_us: float | None = 0.0,
    origin_ns: int | None = None,
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
            params = [_ns(window.start_us), _ns(window.end_us)]
        sql += " order by startNs"
        # origin_us=None：读到首行后再锁定原点（相对坐标）；0.0/显式 ns：绝对坐标
        resolved_origin = (
            origin_ns if origin_ns is not None else (None if origin_us is None else _ns(origin_us))
        )
        for row in connection.execute(sql, params):
            start_ns = int(row[0])
            if resolved_origin is None:
                resolved_origin = start_ns
            duration_ns = int(row[1]) - start_ns
            yield (start_ns - resolved_origin) / NS_PER_US, duration_ns / NS_PER_US
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
    origin_ns: int | None = None,
) -> Iterator[tuple[float, float]]:
    """``(start_us, duration_us)`` TASK view (epoch-safe duration).

    ``origin_us`` 把绝对 epoch 时间平移到小坐标；不传时以首个任务为原点。
    """
    yield from _iter_task_rows(db_path, window, origin_us, origin_ns)


def iter_task_spans(
    db_path: str | Path, *, window: Window | None = None
) -> Iterator[Interval]:
    """Interval-only TASK view (timeline fallback)."""
    for start_us, duration_us in _iter_task_rows(db_path, window):
        yield Interval(start_us, start_us + duration_us)


def task_relative_window(window: Window) -> tuple[Window, int]:
    """Rebase an epoch-µs window onto integer-ns origin and return it in µs.

    The integer ``origin_ns`` is exposed so callers can label a clock domain
    without losing sub-µs precision.  ``round_spans`` are shifted by the same
    origin; no rounding or rescaling is done here.
    """
    if not isinstance(window, Window):
        raise TypeError("window must be a primitives.Window")
    origin_ns = ns_from_us(window.start_us)
    end_ns = ns_from_us(window.end_us)
    relative_spans = tuple(
        (
            index,
            (ns_from_us(start_us) - origin_ns) / NS_PER_US,
            (ns_from_us(end_us) - origin_ns) / NS_PER_US,
        )
        for index, start_us, end_us in window.round_spans
    )
    relative = Window(
        start_us=0.0,
        end_us=(end_ns - origin_ns) / NS_PER_US,
        rounds=window.rounds,
        policy=window.policy,
        provenance=window.provenance,
        source=window.source,
        round_spans=relative_spans,
    )
    return relative, origin_ns


def iter_task_execution_events(
    db_path: str | Path,
    *,
    capture_id: str,
    source_path: str,
    rank: int,
    device_id: int,
    stream_id: int,
    window: Window,
) -> Iterator[TaskExecutionEvent]:
    """Stream one capture/device/stream's ``TASK`` rows intersecting a window.

    The half-open window is ``[start, end)``.  Rows ending at ``start`` or
    starting at ``end`` are excluded.  Every retained row is yielded, including
    unknown task types, malformed durations and boundary crossers, so callers
    cannot silently bridge across them.  Only the exact
    :data:`TASK_SERIAL_TYPES` allow list can become a verified endpoint later.

    ``source_path`` is the caller-supplied capture-relative logical path stored
    in :class:`SourceReference`; ``db_path`` is the filesystem location used for
    the read-only SQLite URI.
    """
    path = Path(db_path)
    if not path.is_file():
        raise EventReadError(f"{path}: 文件不存在")
    if not isinstance(window, Window):
        raise TypeError("window must be a primitives.Window")
    if not isinstance(capture_id, str) or not capture_id.strip():
        raise ValueError("capture_id must not be empty")
    for name, value in (
        ("rank", rank),
        ("device_id", device_id),
        ("stream_id", stream_id),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    logical_path = _capture_relative_path(source_path)

    origin_ns = ns_from_us(window.start_us)
    window_end_ns = ns_from_us(window.end_us)

    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise EventReadError(f"{path}: 无法打开 sqlite: {exc}") from exc
    try:
        if not _has_table(connection, "TASK"):
            raise EventReadError(f"{path}: 缺少 TASK 表")
        columns = _task_columns(connection)
        missing = [name for name in _TASK_REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise EventReadError(f"{path}: TASK 缺少列 {', '.join(missing)}")

        # Resolve STRING_IDS once per ID.  Duplicate IDs are ambiguous and stay
        # unknown instead of taking an arbitrary first row or scanning per row.
        type_names: dict[int, str | None] = {}
        if _has_table(connection, "STRING_IDS"):
            for id_value, total, value in connection.execute(
                "select id, count(*), min(value) from STRING_IDS group by id"
            ):
                key = _sqlite_int(id_value)
                if key is None or total != 1 or value is None:
                    continue
                text = str(value).strip()
                type_names[key] = text or None

        sql = (
            "select t.rowid, t.startNs, t.endNs, t.deviceId, t.globalPid, "
            "t.contextId, t.streamId, t.taskId, t.globalTaskId, t.modelId, "
            "t.taskType from TASK t "
            "where (t.deviceId = ? or typeof(t.deviceId) != 'integer' "
            "       or t.deviceId < 0 or t.deviceId >= 4294967295) "
            "and (t.streamId = ? or typeof(t.streamId) != 'integer' "
            "     or t.streamId < 0 or t.streamId >= 4294967295) "
            # A row whose start is inside the window is kept even when its end
            # is broken (end <= window start), so it still acts as a barrier.
            "and (typeof(t.startNs) != 'integer' or t.startNs < ?) "
            "and (typeof(t.endNs) != 'integer' or t.endNs > ? "
            "     or (t.startNs is not null and t.startNs >= ?)) "
            "order by t.startNs, t.rowid"
        )
        rows = connection.execute(
            sql,
            (device_id, stream_id, window_end_ns, origin_ns, origin_ns),
        )
        for row in rows:
            yield _task_execution_event(
                row,
                capture_id=capture_id.strip(),
                source_path=logical_path,
                rank=rank,
                origin_ns=origin_ns,
                window_end_ns=window_end_ns,
                type_names=type_names,
            )
    except sqlite3.Error as exc:
        raise EventReadError(f"{path}: 读取 TASK 失败: {exc}") from exc
    finally:
        connection.close()


def _task_execution_event(
    row: tuple[object, ...],
    *,
    capture_id: str,
    source_path: str,
    rank: int,
    origin_ns: int,
    window_end_ns: int,
    type_names: dict[int, str | None],
) -> TaskExecutionEvent:
    from .schemas.critical_path import ActivityKind, ExecutionNode

    rowid = _sqlite_int(row[0])
    source = SourceReference(
        kind="task_table", path=source_path, row=rowid if rowid is not None and rowid > 0 else None
    )
    raw_start_ns = _sqlite_int(row[1])
    raw_end_ns = _sqlite_int(row[2])
    row_device = _identity_id(row[3])
    row_pid = _identity_id(row[4])
    # contextId is a subgraph distinguishing field, not runtime Context identity.
    # Keep the raw value, including the explicit UINT32_MAX "subgraph N/A" sentinel.
    raw_context = _sqlite_int(row[5])
    row_context = raw_context if raw_context is not None and raw_context >= 0 else None
    row_stream = _identity_id(row[6])
    task_id = _identity_id(row[7])
    global_task_id = _identity_id(row[8])
    raw_model_id = _sqlite_int(row[9])
    if raw_model_id is None or raw_model_id < 0:
        model_id = None
        model_id_state = "missing"
    elif raw_model_id == _UINT32_MAX:
        model_id = None
        model_id_state = "non_graph"
    else:
        model_id = raw_model_id
        model_id_state = "graph"
    type_id = _sqlite_int(row[10]) if len(row) > 10 else None
    type_name = type_names.get(type_id) if type_id is not None else None

    reasons: list[str] = []
    if source.row is None:
        reasons.append("invalid_source_row")
    if raw_start_ns is None:
        reasons.append("missing_start")
    if raw_end_ns is None:
        reasons.append("missing_end")
    if (
        raw_start_ns is not None
        and raw_end_ns is not None
        and raw_end_ns < raw_start_ns
    ):
        reasons.append("invalid_duration")
    if row_device is None or row_device < 0:
        reasons.append("invalid_device_id")
    if row_stream is None or row_stream < 0:
        reasons.append("invalid_stream_id")
    if row_pid is None:
        reasons.append("missing_global_pid")
    if row_context is None:
        reasons.append("missing_context_id")
    if task_id is None:
        reasons.append("missing_task_id")
    if global_task_id is None:
        reasons.append("missing_global_task_id")
    if model_id_state == "missing":
        reasons.append("missing_model_id")
    elif model_id_state == "graph":
        reasons.append("model_is_graph")
    if type_name is None:
        reasons.append("unknown_task_type")

    crosses_boundary = False
    in_window = False
    if raw_start_ns is not None and raw_end_ns is not None:
        crosses_boundary = (
            raw_start_ns < origin_ns or raw_end_ns > window_end_ns
        )
        in_window = (
            not crosses_boundary
            and raw_start_ns >= origin_ns
            and raw_end_ns <= window_end_ns
            and raw_end_ns >= raw_start_ns
        )
        if crosses_boundary:
            reasons.append("window_crossing")

    node_duration_us: float | None = None
    if (
        raw_start_ns is not None
        and raw_end_ns is not None
        and raw_end_ns >= raw_start_ns
        and not crosses_boundary
    ):
        node_duration_us = (raw_end_ns - raw_start_ns) / NS_PER_US

    node: ExecutionNode | None = None
    if raw_start_ns is not None and source.row is not None:
        node = ExecutionNode(
            capture_id=capture_id,
            source=source,
            activity=ActivityKind.DEVICE_TASK,
            start_us=(raw_start_ns - origin_ns) / NS_PER_US,
            duration_us=node_duration_us,
            rank=rank,
            stream_id=row_stream,
            operator=None,
            task_type=_task_type_for_name(type_name),
        )

    return TaskExecutionEvent(
        capture_id=capture_id,
        source=source,
        node=node,
        rank=rank,
        device_id=row_device,
        stream_id=row_stream,
        global_pid=row_pid,
        context_id=row_context,
        task_id=task_id,
        global_task_id=global_task_id,
        model_id=model_id,
        task_type_name=type_name,
        task_type=_task_type_for_name(type_name),
        raw_start_ns=raw_start_ns,
        raw_end_ns=raw_end_ns,
        origin_ns=origin_ns,
        in_window=in_window,
        crosses_boundary=crosses_boundary,
        model_id_state=model_id_state,
        reasons=tuple(reasons),
    )


def iter_host_spans(
    db_path: str | Path,
    *,
    window: Window | None = None,
    origin_us: float | None = 0.0,
    origin_ns: int | None = None,
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
            params = [_ns(window.start_us), _ns(window.end_us)]
        sql += " order by a.startNs"
        resolved_origin = (
            origin_ns if origin_ns is not None else (None if origin_us is None else _ns(origin_us))
        )
        for row in connection.execute(sql, params):
            start_ns = int(row[0])
            if resolved_origin is None:
                resolved_origin = start_ns
            duration_ns = int(row[1]) - start_ns
            yield (
                (start_ns - resolved_origin) / NS_PER_US,
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
