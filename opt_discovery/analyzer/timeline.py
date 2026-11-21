"""Timeline analysis: device idle attribution from the merged msprof sqlite.

Why this module exists
----------------------
Operator statistics can only say "this kernel is short and called often", which
is **not** proof of launch-bound.  To decide, we need to look at what happens
between two device tasks:

* is the host busy issuing ACL calls during the gap?  -> dispatch (ACL) bound
* is the host silent during the gap?                -> Python/scheduling/wait,
  which this data **cannot** attribute -- and we say so instead of guessing.

Data sources (tables verified on ``$PROF_ROOT``):

    op_summary CSV (mindstudio_profiler_output/op_summary_*.csv)  -- **preferred**
        per-operator epoch-µs start/duration, includes COMMUNICATION rows,
        unit is consistent (µs).  Verified: two TP ranks report near-equal
        op totals, matching physical reality.
    TASK      (startNs, endNs, ...)                         -- fallback only.
        Proven unreliable: unit is ns for COMMUNICATION rows but µs for
        AI_CORE rows, and two ranks with identical task counts report
        duration sums differing 18.6x.  Busy stats fall back to this table
        (flagged) only when no op_summary CSV exists.
    CANN_API  (startNs, endNs, name -> STRING_IDS, ...)     -- host ACL calls
    STRING_IDS(0='release', 1='allocate', ...)              -- name dictionary

Timestamps are epoch nanoseconds, same clock as ``Task Start Time(us) * 1000``.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ..events import (
    EventReadError,
    iter_host_spans,
    iter_op_summary_intervals,
    iter_op_summary_spans,
    iter_task_span_rows,
    iter_task_spans,
    ns_from_us,
    op_summary_paths,
    task_origin_ns,
)
from ..primitives import NS_PER_US, Interval, Window

__all__ = [
    "Interval",
    "GapAttribution",
    "TimelineSummary",
    "TimelineError",
    "load_task_intervals",
    "load_host_intervals",
    "load_op_intervals",
    "find_op_summary",
    "summarize",
    "SUBMIT_APIS",
    "HOST_BUSY_RATIO",
    "HOST_SILENT_RATIO",
]

_NS_PER_US = NS_PER_US

#: 视为「下发调用」的 ACL API 名称
SUBMIT_APIS = frozenset({"launch", "aclrtLaunchKernelWithHostArgs"})

#: gap 内 Host ACL 忙碌比例 >= 该值 -> 视为 ACL 下发受限候选
HOST_BUSY_RATIO = 0.5
#: gap 内 Host ACL 忙碌比例 <= 该值 -> 视为 Host 静默
HOST_SILENT_RATIO = 0.1

#: OVERLAP_ANALYSIS.type -> 名称（来自 ENUM_OVERLAP_ANALYSIS_TYPE）
OVERLAP_TYPES = {
    0: "compute",
    1: "communication",
    2: "comm_not_overlapped",
    3: "free",
}

CLASS_ACL_BUSY = "acl_busy"
CLASS_HOST_SILENT = "host_silent"
CLASS_MIXED = "mixed"


class TimelineError(RuntimeError):
    """Raised when the sqlite file cannot provide a device/host timeline."""


@dataclass(frozen=True)
class GapAttribution:
    start_us: float
    end_us: float
    duration_us: float
    host_acl_overlap_us: float
    classification: str

    @property
    def host_acl_ratio(self) -> float:
        return self.host_acl_overlap_us / self.duration_us if self.duration_us else 0.0

    def to_dict(self) -> dict:
        return {
            "start_us": self.start_us,
            "end_us": self.end_us,
            "duration_us": self.duration_us,
            "host_acl_overlap_us": self.host_acl_overlap_us,
            "host_acl_ratio": round(self.host_acl_ratio, 4),
            "classification": self.classification,
        }


@dataclass
class TimelineSummary:
    db_path: str
    window: dict | None
    busy_source: str
    device_busy_us: float
    device_idle_us: float
    span_us: float
    task_count: int
    gap_count: int
    gap_p50_us: float
    gap_p90_us: float
    gap_max_us: float
    analyzed_gaps: int = 0
    gap_threshold_us: float = 100.0
    idle_analyzed_us: float = 0.0
    idle_covered_by_acl_us: float = 0.0
    idle_by_class_us: dict[str, float] = field(default_factory=dict)
    gaps_by_class: dict[str, int] = field(default_factory=dict)
    host_acl_calls: int = 0
    host_acl_busy_us: float = 0.0
    submit_calls: int = 0
    submit_time_us: float = 0.0
    host_rank_task_count: int | None = None
    top_acl_names: tuple[tuple[str, int, float], ...] = ()
    device_busy_all_us: float = 0.0
    per_rank: tuple[dict, ...] = ()
    rank_asymmetry_pp: float = 0.0
    rank_duration_inconsistent: bool = False
    mstx_available: bool = False
    mstx_ranges: int = 0
    mstx_db: str | None = None
    mstx_sync_us: float = 0.0
    silent_covered_by_mstx_us: float = 0.0
    silent_unattributed_us: float = 0.0
    silent_by_range: tuple[tuple[str, float], ...] = ()
    overlap_available: bool = False
    compute_us: float = 0.0
    communication_us: float = 0.0
    comm_not_overlapped_us: float = 0.0
    free_us: float = 0.0
    notes: tuple[str, ...] = ()

    # -- derived ---------------------------------------------------------
    @property
    def device_idle_ratio(self) -> float:
        return self.device_idle_us / self.span_us if self.span_us else 0.0

    @property
    def acl_coverage_of_analyzed_idle(self) -> float:
        """已分析 gap 中，被 Host ACL 活动覆盖的比例（并集口径）。"""
        return (
            self.idle_covered_by_acl_us / self.idle_analyzed_us
            if self.idle_analyzed_us
            else 0.0
        )

    @property
    def host_busy_ratio_of_span(self) -> float:
        return self.host_acl_busy_us / self.span_us if self.span_us else 0.0

    @property
    def comm_exposed_ratio(self) -> float:
        """通信中未与计算重叠的比例（1.0 = 完全暴露）。"""
        return self.comm_not_overlapped_us / self.communication_us if self.communication_us else 0.0

    @property
    def comm_exposed_share_of_span(self) -> float:
        return self.comm_not_overlapped_us / self.span_us if self.span_us else 0.0

    @property
    def compute_share_of_span(self) -> float:
        return self.compute_us / self.span_us if self.span_us else 0.0

    @property
    def bottleneck_hint(self) -> str:
        """工作负载级的方向性提示；证据不足时明确返回 undetermined。"""
        if self.rank_duration_inconsistent:
            return "undetermined"
        if self.overlap_available:
            if self.comm_exposed_share_of_span >= 0.15:
                return "communication_exposed"
            if self.device_idle_ratio < 0.10 and self.compute_share_of_span >= 0.50:
                return "device_compute_saturated"
        if self.device_idle_ratio < 0.10:
            return "device_saturated_other"
        if self.acl_coverage_of_analyzed_idle < 0.2:
            return "host_gap_unattributed"
        return "undetermined"

    @property
    def submit_per_task(self) -> float:
        tasks = self.task_count if self.host_rank_task_count is None else self.host_rank_task_count
        return self.submit_calls / tasks if tasks else 0.0

    def to_dict(self) -> dict:
        return {
            "db_path": self.db_path,
            "window": self.window,
            "busy_source": self.busy_source,
            "span_us": self.span_us,
            "device_busy_us": self.device_busy_us,
            "device_busy_all_us": round(self.device_busy_all_us, 3),
            "per_rank": list(self.per_rank),
            "rank_asymmetry_pp": round(self.rank_asymmetry_pp, 1),
            "rank_duration_inconsistent": self.rank_duration_inconsistent,
            "device_idle_us": self.device_idle_us,
            "device_idle_ratio": round(self.device_idle_ratio, 4),
            "task_count": self.task_count,
            "gap_count": self.gap_count,
            "gap_p50_us": self.gap_p50_us,
            "gap_p90_us": self.gap_p90_us,
            "gap_max_us": self.gap_max_us,
            "gap_threshold_us": self.gap_threshold_us,
            "analyzed_gaps": self.analyzed_gaps,
            "gaps_by_class": self.gaps_by_class,
            "idle_by_class_us": self.idle_by_class_us,
            "host_acl_calls": self.host_acl_calls,
            "host_acl_busy_us": self.host_acl_busy_us,
            "host_busy_ratio_of_span": round(self.host_busy_ratio_of_span, 4),
            "idle_analyzed_us": self.idle_analyzed_us,
            "idle_covered_by_acl_us": self.idle_covered_by_acl_us,
            "acl_coverage_of_analyzed_idle": round(self.acl_coverage_of_analyzed_idle, 4),
            "submit_calls": self.submit_calls,
            "submit_time_us": self.submit_time_us,
            "host_rank_task_count": self.host_rank_task_count,
            "submit_per_task": round(self.submit_per_task, 3),
            "top_acl_names": [list(item) for item in self.top_acl_names],
            "mstx_available": self.mstx_available,
            "mstx_ranges": self.mstx_ranges,
            "mstx_db": self.mstx_db,
            "mstx_sync_us": round(self.mstx_sync_us, 3),
            "silent_covered_by_mstx_us": round(self.silent_covered_by_mstx_us, 3),
            "silent_unattributed_us": round(self.silent_unattributed_us, 3),
            "silent_by_range": [[name, round(value, 3)] for name, value in self.silent_by_range],
            "overlap_available": self.overlap_available,
            "compute_us": self.compute_us,
            "communication_us": self.communication_us,
            "comm_not_overlapped_us": self.comm_not_overlapped_us,
            "free_us": self.free_us,
            "comm_exposed_ratio": round(self.comm_exposed_ratio, 4),
            "comm_exposed_share_of_span": round(self.comm_exposed_share_of_span, 4),
            "compute_share_of_span": round(self.compute_share_of_span, 4),
            "bottleneck_hint": self.bottleneck_hint,
            "notes": list(self.notes),
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------
def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise TimelineError(f"{path}: 文件不存在")
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise TimelineError(f"{path}: 无法打开 sqlite: {exc}") from exc


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "select count(*) from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return bool(row and row[0])


def load_task_intervals(
    db_path: str | Path, start_us: float | None = None, end_us: float | None = None
) -> list[Interval]:
    """TASK 表回退路径（ns -> us）；委托共享 :func:`iter_task_intervals`。"""
    if start_us is not None and end_us is not None and end_us <= start_us:
        # 历史行为：点窗口按“覆盖该点”过滤，而不是构造非法 Window
        try:
            spans = list(iter_task_spans(db_path))
        except EventReadError as exc:
            raise TimelineError(str(exc)) from exc
        return [
            span for span in spans if span.end_us >= start_us and span.start_us <= end_us
        ]
    window = (
        Window(start_us, end_us, rounds=())
        if start_us is not None and end_us is not None
        else None
    )
    try:
        return list(iter_task_spans(db_path, window=window))
    except EventReadError as exc:
        raise TimelineError(str(exc)) from exc


def find_op_summary(db_path: str | Path) -> Path | None:
    """从 rank 的 msprof_*.db 所在目录发现 op_summary CSV。

    布局：``PROF_xxx/msprof_*.db`` 与 ``PROF_xxx/mindstudio_profiler_output/op_summary_*.csv``
    同级。多份 CSV 时取最大（最大的那份通常是完整解析产物）。
    """
    candidates = sorted(
        Path(db_path).parent.glob("mindstudio_profiler_output/op_summary_*.csv")
    )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_size)


def load_op_intervals(
    op_summary_path: str | Path, start_us: float | None = None, end_us: float | None = None
) -> list[Interval]:
    """op_summary 逐实例区间；委托共享 :func:`iter_op_summary_events`。

    保留旧语义：与窗口相交的事件整体保留（不裁剪），按开始时间排序。
    """
    lower = start_us if start_us is not None else float("-inf")
    upper = end_us if end_us is not None else float("inf")
    try:
        intervals = [
            interval
            for interval in iter_op_summary_intervals(op_summary_path)
            if interval.start_us <= upper and interval.duration_us >= lower - interval.start_us
        ]
    except EventReadError as exc:
        raise TimelineError(str(exc)) from exc
    intervals.sort(key=lambda item: item.start_us)
    return intervals


def load_host_intervals(
    db_path: str | Path, start_us: float | None = None, end_us: float | None = None
) -> list[tuple[Interval, str]]:
    """CANN_API Host 调用；委托共享 :func:`iter_host_calls`。"""
    point = start_us is not None and end_us is not None and end_us <= start_us
    window = (
        Window(start_us, end_us, rounds=())
        if start_us is not None and end_us is not None and not point
        else None
    )
    try:
        rows = [
            (Interval(span_start, span_start + duration_us), name)
            for span_start, duration_us, name in iter_host_spans(db_path, window=window)
        ]
    except EventReadError as exc:
        raise TimelineError(str(exc)) from exc
    if point:
        assert start_us is not None and end_us is not None
        rows = [
            (span, name)
            for span, name in rows
            if span.end_us >= start_us and span.start_us <= end_us
        ]
    return rows


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
def _merge(intervals: Iterable[Interval]) -> list[list[float]]:
    merged: list[list[float]] = []
    for item in intervals:
        if merged and item.start_us <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], item.end_us)
        else:
            merged.append([item.start_us, item.end_us])
    return merged


def _overlap_with_merged(merged: Sequence[Sequence[float]], starts: Sequence[float], start: float, end: float) -> float:
    """Busy time of ``merged`` inside ``[start, end)``, via binary search."""
    index = max(0, bisect_right(starts, end) - 1)
    overlap = 0.0
    while index < len(merged) and merged[index][0] < end:
        overlap += max(0.0, min(end, merged[index][1]) - max(start, merged[index][0]))
        index += 1
    return overlap


def _percentile(values: Sequence[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, int(len(ordered) * ratio))
    return ordered[position]


def _merge_all(merged_list: Sequence[Sequence[Sequence[float]]]) -> list[list[float]]:
    """所有 rank 区间的并集（任一设备忙）。"""
    flat = sorted(
        (Interval(s, e) for merged in merged_list for s, e in merged),
        key=lambda item: item.start_us,
    )
    return _merge(flat)


def _intersect_merged(
    merged_list: Sequence[Sequence[Sequence[float]]]
) -> list[list[float]]:
    """所有 rank 区间的交集（两设备都忙）。"""
    if not merged_list:
        return []
    current = [list(item) for item in merged_list[0]]
    for other in merged_list[1:]:
        result: list[list[float]] = []
        i = j = 0
        while i < len(current) and j < len(other):
            start = max(current[i][0], other[j][0])
            end = min(current[i][1], other[j][1])
            if start < end:
                result.append([start, end])
            if current[i][1] < other[j][1]:
                i += 1
            else:
                j += 1
        current = result
        if not current:
            break
    return current


def _gaps_from_merged(
    merged: Sequence[Sequence[float]], span: float, min_gap_us: float, top: int
) -> tuple[list[float], list[tuple[float, float]]]:
    """从忙碌区间并集反推空洞（系统空闲区间）。

    cursor 从首个忙碌区间起点开始：epoch 原点到首个算子之间不是设备空洞
    （span - busy 的口径已由调用方统计），否则会产生 1e15 量级的伪 gap。
    """
    durations: list[float] = []
    big: list[tuple[float, float]] = []
    if not merged:
        return durations, big
    cursor = float(merged[0][0])
    for start, end in merged:
        if start > cursor:
            durations.append(start - cursor)
            if start - cursor >= min_gap_us:
                big.append((cursor, start))
        cursor = max(cursor, end)
    return durations, big


def _rank_intervals(
    db_path: str | Path, window: Window
) -> tuple[list[Interval], str, str | None]:
    """选取该 rank 的区间数据源：优先 op_summary CSV，缺失时回退 TASK 表。

    Returns ``(intervals, source, op_csv_path)``，source 为 ``op_summary`` 或
    ``task_table``。
    """
    origin = window.start_us
    origin_ns = ns_from_us(window.start_us)
    paths = op_summary_paths(db_path)
    if paths:
        intervals: list[Interval] = []
        for path in paths:
            for start_us, duration_us in iter_op_summary_spans(path, window=window):
                intervals.append(
                    Interval(start_us - origin, start_us - origin + duration_us)
                )
        intervals.sort(key=lambda item: item.start_us)
        return intervals, "op_summary", str(paths[0])
    try:
        spans = list(
            iter_task_span_rows(db_path, window=window, origin_us=origin, origin_ns=origin_ns)
        )
    except EventReadError as exc:
        raise TimelineError(str(exc)) from exc
    return (
        [Interval(start, start + duration) for start, duration in spans],
        "task_table",
        None,
    )


def _rank_summary(db_path: str | Path, window: Window, span_us: float) -> dict:
    intervals, source, op_csv = _rank_intervals(db_path, window)
    merged = _merge(intervals)
    busy = sum(item[1] - item[0] for item in merged)
    summary = {
        "db": Path(db_path).name,
        "source": source,
        "tasks": len(intervals),
        "duration_sum_us": round(sum(item.end_us - item.start_us for item in intervals), 3),
        "busy_us": round(busy, 3),
        "busy_ratio": round(busy / span_us, 4) if span_us else 0.0,
        "_merged": merged,
    }
    if op_csv is not None:
        summary["_op_csv"] = op_csv
    return summary


def _device_gap_stats(
    db_path: str | Path, window: Window | None, min_gap_us: float
) -> tuple[
    float, int, int, list[float], list[tuple[float, float]], float, int
]:
    """Stream TASK spans: busy union, gap durations, and gaps >= threshold."""
    busy = 0.0
    durations: list[float] = []
    big_gaps: list[tuple[float, float]] = []
    tasks = 0
    try:
        origin_ns = task_origin_ns(db_path)
    except EventReadError as exc:
        raise TimelineError(str(exc)) from exc
    origin = origin_ns / NS_PER_US
    segment_start: float | None = None
    segment_end: float | None = None
    try:
        for start, duration in iter_task_span_rows(
            db_path, window=window, origin_ns=origin_ns
        ):
            end = start + duration
            tasks += 1
            if segment_start is None:
                segment_start, segment_end = start, end
                continue
            assert segment_end is not None
            if start <= segment_end:
                segment_end = max(segment_end, end)
            else:
                busy += segment_end - segment_start
                gap = start - segment_end
                durations.append(gap)
                if gap >= min_gap_us:
                    big_gaps.append((segment_end, start))
                segment_start, segment_end = start, end
    except EventReadError as exc:
        raise TimelineError(str(exc)) from exc
    if segment_start is not None and segment_end is not None:
        busy += segment_end - segment_start
    if tasks == 0:
        raise TimelineError(f"{db_path}: 窗口内没有 device task，无法分析")
    return busy, tasks, len(durations), durations, big_gaps, origin, origin_ns


def _host_stream(
    db_path: str | Path,
    window: Window | None,
    origin_ns: int | None = None,
):
    """Yield (start_us, duration_us, name) in start order via the shared reader."""
    try:
        yield from iter_host_spans(db_path, window=window, origin_ns=origin_ns)
    except EventReadError as exc:
        raise TimelineError(str(exc)) from exc


def load_overlap_totals(
    db_path: str | Path, window: Window | None
) -> tuple[dict[str, float], bool]:
    """Read ``OVERLAP_ANALYSIS`` (profiler's own compute/comm/free classification)."""
    connection = _connect(db_path)
    if not _has_table(connection, "OVERLAP_ANALYSIS"):
        return {}, False
    sql = "select type, startNs, endNs from OVERLAP_ANALYSIS"
    params: list[float] = []
    if window is not None:
        sql += " where endNs >= ? and startNs <= ?"
        params = [window.start_us * _NS_PER_US, window.end_us * _NS_PER_US]
    totals: dict[str, float] = {}
    with connection:
        for type_id, start_ns, end_ns in connection.execute(sql, params):
            name = OVERLAP_TYPES.get(int(type_id))
            if name is None:
                continue
            start = start_ns / _NS_PER_US
            end = end_ns / _NS_PER_US
            if window is not None:
                start = max(start, window.start_us)
                end = min(end, window.end_us)
            if end > start:
                totals[name] = totals.get(name, 0.0) + (end - start)
    return totals, True


def summarize(
    db_path: str | Path,
    window: Window | None = None,
    *,
    min_gap_us: float = 100.0,
    top_n: int = 8,
    mstx=None,
    extra_db_paths: Sequence[str | Path] = (),
) -> TimelineSummary:
    """``extra_db_paths`` 用于 TP>1：只读一个 rank 的 busy 会随 rank 顺序翻转。"""
    """Build a device-idle attribution summary for one rank and one window.

    Implementation notes: both tables are streamed (the decode rank has 756k
    tasks and 3.5M host API events inside the window), and the host events are
    merge-joined against the recorded gaps, so memory stays bounded.
    """
    per_rank: tuple[dict, ...] = ()
    #: Device/Host/MSTX 共用的归属原点
    gap_origin: float | None = None
    gap_origin_ns: int | None = None
    busy_all = 0.0
    asymmetry = 0.0
    duration_inconsistent = False
    busy_source = "task_table"
    task_fallback_ranks: list[str] = []
    if extra_db_paths and window is not None:
        # 多 rank（TP>1）：单读一个 rank 的 busy 会随排序翻转，必须合并口径
        span_us = window.duration_us
        ranks = [_rank_summary(db_path, window, span_us)] + [
            _rank_summary(path, window, span_us) for path in extra_db_paths
        ]
        merged_list = [item["_merged"] for item in ranks]
        any_merged = _merge_all(merged_list)
        all_merged = _intersect_merged(merged_list)
        device_busy = sum(end - start for start, end in any_merged)
        busy_all = sum(end - start for start, end in all_merged)
        gap_durations, big_gaps = _gaps_from_merged(any_merged, span_us, min_gap_us, top_n)
        gap_count = len(gap_durations)
        tasks = sum(item["tasks"] for item in ranks)
        ratios = [item["busy_ratio"] for item in ranks]
        asymmetry = (max(ratios) - min(ratios)) * 100 if ratios else 0.0
        # 任务数接近但累计时长差数倍 => 记录时长不可比，
        # 此时 per-rank busy 不能解释为"某卡在等"
        task_counts = [max(1, item["tasks"]) for item in ranks]
        durations = [item["duration_sum_us"] for item in ranks]
        count_close = max(task_counts) / min(task_counts) < 1.05
        if count_close and max(durations) / max(1.0, min(durations)) >= 2.0:
            duration_inconsistent = True
        sources = {item.get("source", "task_table") for item in ranks}
        busy_source = "op_summary" if "op_summary" in sources else "task_table"
        task_fallback_ranks = [
            item["db"] for item in ranks if item.get("source") != "op_summary"
        ]
        per_rank = tuple(
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in ranks
        )
    else:
        if window is not None:
            intervals, busy_source, _ = _rank_intervals(db_path, window)
            if busy_source != "op_summary":
                task_fallback_ranks = [Path(db_path).name]
            merged = _merge(intervals)
            device_busy = sum(end - start for start, end in merged)
            gap_durations, big_gaps = _gaps_from_merged(
                merged, window.duration_us, min_gap_us, top_n
            )
            gap_count = len(gap_durations)
            tasks = len(intervals)
        else:
            (
                device_busy,
                tasks,
                gap_count,
                gap_durations,
                big_gaps,
                gap_origin,
                gap_origin_ns,
            ) = _device_gap_stats(db_path, window, min_gap_us)

    if window is not None:
        gap_origin_ns = ns_from_us(window.start_us)
        gap_origin = gap_origin_ns / NS_PER_US
        span = window.duration_us
    else:
        span = device_busy + sum(gap_durations)
    span = max(span, 0.0)
    device_idle = max(0.0, span - device_busy)

    mstx_origin = gap_origin if gap_origin is not None else 0.0
    # 同一 gap 内可能有多线程并发事件，必须取「并集」而不是累加，否则会超过 100%
    overlaps = [0.0] * len(big_gaps)
    gap_seg_start = [None] * len(big_gaps)
    gap_seg_end = [None] * len(big_gaps)

    host_calls = 0
    host_busy = 0.0
    host_seg_start: float | None = None
    host_seg_end: float | None = None
    submit_calls = 0
    submit_time = 0.0
    name_counts: dict[str, list[float]] = {}
    gap_ends = [item[1] for item in big_gaps]
    cursor = 0

    for start, duration, name in _host_stream(db_path, window, origin_ns=gap_origin_ns):
        end = start + duration
        host_calls += 1
        bucket = name_counts.setdefault(name, [0, 0.0])
        bucket[0] += 1
        bucket[1] += duration
        if name in SUBMIT_APIS:
            submit_calls += 1
            submit_time += duration

        # 全局 Host 忙碌并集
        if host_seg_start is None or host_seg_end is None:
            host_seg_start, host_seg_end = start, end
        elif start <= host_seg_end:
            host_seg_end = max(host_seg_end, end)
        else:
            host_busy += host_seg_end - host_seg_start
            host_seg_start, host_seg_end = start, end

        if not big_gaps:
            continue
        while cursor < len(big_gaps) and gap_ends[cursor] <= start:
            cursor += 1
        index = cursor
        while index < len(big_gaps) and big_gaps[index][0] < end:
            a = max(start, big_gaps[index][0])
            b = min(end, big_gaps[index][1])
            if b > a:
                if gap_seg_start[index] is None:
                    gap_seg_start[index], gap_seg_end[index] = a, b
                elif a <= (gap_seg_end[index] or a):
                    gap_seg_end[index] = max(gap_seg_end[index] or b, b)
                else:
                    overlaps[index] += (gap_seg_end[index] or a) - gap_seg_start[index]
                    gap_seg_start[index], gap_seg_end[index] = a, b
            index += 1

    if host_seg_start is not None and host_seg_end is not None:
        host_busy += host_seg_end - host_seg_start
    for index in range(len(big_gaps)):
        seg_start = gap_seg_start[index]
        if seg_start is not None:
            seg_end = gap_seg_end[index]
            overlaps[index] += (seg_end if seg_end is not None else seg_start) - seg_start

    idle_by_class: dict[str, float] = {CLASS_ACL_BUSY: 0.0, CLASS_HOST_SILENT: 0.0, CLASS_MIXED: 0.0}
    gaps_by_class: dict[str, int] = {CLASS_ACL_BUSY: 0, CLASS_HOST_SILENT: 0, CLASS_MIXED: 0}
    idle_analyzed = 0.0
    idle_covered = 0.0
    for (start, end), overlap in zip(big_gaps, overlaps, strict=True):
        idle_analyzed += end - start
        idle_covered += min(overlap, end - start)
        duration = end - start
        ratio = overlap / duration if duration else 0.0
        if ratio >= HOST_BUSY_RATIO:
            label = CLASS_ACL_BUSY
        elif ratio <= HOST_SILENT_RATIO:
            label = CLASS_HOST_SILENT
        else:
            label = CLASS_MIXED
        idle_by_class[label] += duration
        gaps_by_class[label] += 1

    silent_gaps = [
        (start, end)
        for (start, end), overlap in zip(big_gaps, overlaps, strict=True)
        if (overlap / (end - start) if end > start else 0.0) <= HOST_SILENT_RATIO
    ]
    silent_total = sum(end - start for start, end in silent_gaps)
    silent_covered = 0.0
    silent_by_range: list[tuple[str, float]] = []
    mstx_ranges = []
    mstx_sync = 0.0
    mstx_available = bool(getattr(mstx, "available", False))
    if mstx_available and getattr(mstx, "ranges", None):
        mstx_ranges = sorted(mstx.ranges, key=lambda item: item.start_us)
        mstx_sync = sum(
            end - start for start, end in _merge(
                Interval(item.start_us, item.end_us)
                for item in mstx_ranges
                if getattr(item, "is_sync", False)
            )
        )
        per_name: dict[str, float] = {}
        for gap_start, gap_end in silent_gaps:
            pieces = [
                (
                    max(gap_start, item.start_us - mstx_origin),
                    min(gap_end, item.end_us - mstx_origin),
                    item.name,
                )
                for item in mstx_ranges
                if item.end_us - mstx_origin > gap_start
                and item.start_us - mstx_origin < gap_end
            ]
            if not pieces:
                continue
            pieces.sort()
            covered = 0.0
            cursor_start, cursor_end = pieces[0][0], pieces[0][1]
            best_name, best_overlap = pieces[0][2], cursor_end - cursor_start
            for start, end, name in pieces[1:]:
                if start <= cursor_end:
                    cursor_end = max(cursor_end, end)
                else:
                    covered += cursor_end - cursor_start
                    cursor_start, cursor_end = start, end
                if end - start > best_overlap:
                    best_name, best_overlap = name, end - start
            covered += cursor_end - cursor_start
            silent_covered += covered
            per_name[best_name] = per_name.get(best_name, 0.0) + (gap_end - gap_start)
        silent_by_range = sorted(per_name.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    top_names = tuple(
        (name, int(count), total_us)
        for name, (count, total_us) in sorted(
            name_counts.items(), key=lambda item: item[1][0], reverse=True
        )[:top_n]
    )

    overlap_totals, overlap_available = load_overlap_totals(db_path, window)
    compute_us = overlap_totals.get("compute", 0.0)
    communication_us = overlap_totals.get("communication", 0.0)
    exposed_us = overlap_totals.get("comm_not_overlapped", 0.0)
    free_us = overlap_totals.get("free", 0.0)

    notes: list[str] = [
        "gap 归因基于 Host CANN_API 是否在 gap 内活动：acl_busy=Host 在下发，"
        "host_silent=Host 无 ACL 调用（可能是 Python/调度/等待，本数据无法进一步归因）"
    ]
    if busy_source == "op_summary":
        notes.append(
            "busy 统计基于 op_summary 算子区间（epoch µs、单位统一、含通信算子）；"
            "TASK 表已证实不可信（COMMUNICATION 行单位为 ns、两 rank 时长和差 18.6 倍），仅作回退"
        )
    if task_fallback_ranks:
        notes.append(
            f"以下 rank 缺少 op_summary CSV，busy 回退到不可信的 TASK 表："
            f"{', '.join(task_fallback_ranks)}——其 busy/idle 数字仅供参考"
        )
    if per_rank:
        details = ", ".join(
            f"{item['db'][-12:]}={item['busy_ratio']:.1%}" for item in per_rank
        )
        notes.append(f"多 rank 合并口径：任一设备忙 {device_busy / span:.1%}、两设备都忙 {busy_all / span:.1%}（{details}）")
        if duration_inconsistent:
            notes.append(
                "两 rank 的 task 数量接近但累计时长相差数倍：profiler 记录的 task 时长不可比，"
                "per-rank busy 不能解释为“某张卡在等”，busy/idle 结论整体不可用"
            )
        elif asymmetry >= 30:
            notes.append(
                f"TP 两卡明显不对称（busy 相差 {asymmetry:.0f}pp）："
                "单看一个 rank 的空闲率会得出相反结论，"
                "且该 rank 的 TASK 时长分布可能异常（p99 差数倍）"
            )
    if mstx is not None and not mstx_available:
        notes.append(
            "MSTX 未开启或当前库缺少对应的打点表：host_silent 只能停在“无法进一步归因”，"
            "需要带打点复采（scripts/enable_mstx.sh）"
        )
    if mstx_available:
        if silent_total > 0:
            ratio = silent_covered / silent_total
            notes.append(
                f"MSTX 打点覆盖了 {ratio:.0%} 的静默空洞（{len(mstx_ranges)} 个范围）；"
                + (
                    "这些空洞落在 Python 阶段内，属上层调度/等待，不是下发受限"
                    if ratio >= 0.5
                    else "仍有较多空洞未被任何打点覆盖，归因能力有限"
                )
            )
        if mstx_sync > 0:
            notes.append(
                f"打点中的同步等待合计 {mstx_sync / 1000:.2f}ms，"
                "可作为 synchronization_bound 的实测支撑"
            )
    if overlap_available and span > 0:
        notes.append(
            f"重叠分析：compute={compute_us / span * 100:.1f}% "
            f"comm={communication_us / span * 100:.1f}% "
            f"comm_not_overlapped={exposed_us / span * 100:.1f}% "
            f"free={free_us / span * 100:.1f}%（占窗口）"
        )
        if exposed_us / span >= 0.15:
            notes.append(
                "通信未与计算重叠的时间占比 >=15%，通信受限是主要候选，"
                "应优先评估通信/计算重叠而非单个算子调优"
            )
        elif communication_us and exposed_us / communication_us <= 0.2:
            notes.append("通信基本被计算掩盖（暴露比例 <=20%），通信不是主要瓶颈")
    notes.append(
        "Event/Barrier 级同步等待不在本数据中：aclrtSynchronizeStream 平均 <1µs（异步返回），"
        "不能用其时长判定同步受限"
    )
    if idle_analyzed and (idle_covered / idle_analyzed) < 0.2:
        notes.append(
            "已分析的空洞里 Host ACL 活动覆盖不足 20%，不能据此判定为下发受限；"
            "需要 Host 侧 Python 栈或上层调度信息才能继续归因"
        )
    if device_idle <= span * 0.05:
        notes.append(
            "设备空闲占比 <5%，该窗口内下发/同步不是主要瓶颈"
        )

    return TimelineSummary(
        db_path=str(db_path),
        window=window.to_dict() if window else None,
        busy_source=busy_source,
        device_busy_us=device_busy,
        device_busy_all_us=busy_all,
        per_rank=per_rank,
        rank_asymmetry_pp=asymmetry,
        rank_duration_inconsistent=duration_inconsistent,
        device_idle_us=device_idle,
        span_us=span,
        task_count=tasks,
        gap_count=gap_count,
        gap_p50_us=_percentile(gap_durations, 0.5),
        gap_p90_us=_percentile(gap_durations, 0.9),
        gap_max_us=max(gap_durations) if gap_durations else 0.0,
        analyzed_gaps=len(big_gaps),
        gap_threshold_us=min_gap_us,
        idle_analyzed_us=idle_analyzed,
        idle_covered_by_acl_us=idle_covered,
        idle_by_class_us={k: round(v, 3) for k, v in idle_by_class.items()},
        gaps_by_class=gaps_by_class,
        host_acl_calls=host_calls,
        host_acl_busy_us=host_busy,
        submit_calls=submit_calls,
        submit_time_us=submit_time,
        host_rank_task_count=per_rank[0]["tasks"] if per_rank else tasks,
        top_acl_names=top_names,
        mstx_available=mstx_available,
        mstx_ranges=len(mstx_ranges),
        mstx_db=getattr(mstx, "db_path", None) if mstx_available else None,
        mstx_sync_us=mstx_sync,
        silent_covered_by_mstx_us=silent_covered,
        silent_unattributed_us=max(0.0, silent_total - silent_covered),
        silent_by_range=tuple(silent_by_range),
        overlap_available=overlap_available,
        compute_us=compute_us,
        communication_us=communication_us,
        comm_not_overlapped_us=exposed_us,
        free_us=free_us,
        notes=tuple(notes),
    )


def annotate_diagnosis(
    report, summary: TimelineSummary, *, graph: bool = False,
) -> list[str]:  # noqa: ANN001
    """Attach workload-level timeline evidence to a diagnosis report.

    Returns the appended notes.  Launch/sync candidates get an extra caveat when
    the timeline contradicts (or supports) the operator-level hypothesis.
    """
    appended: list[str] = []
    idle = summary.device_idle_ratio
    acl_share = summary.acl_coverage_of_analyzed_idle
    appended.append(
        f"Timeline（窗口内）：设备空闲 {idle * 100:.1f}%，其中 Host ACL 活动覆盖 "
        f"{acl_share * 100:.1f}% 的空闲时间；下发调用 {summary.submit_calls} 次"
        f"（主库 rank {summary.submit_per_task:.2f}/task）"
    )

    if graph:
        appended.append(
            "图重放可以一次提交多个设备任务。下发、ACL 覆盖和 gap 归因仅描述 workload 窗口，"
            "CANN_API 与 TASK 的 connectionId 不用于逐算子关联。"
        )
    if summary.rank_duration_inconsistent:
        appended.append("rank 时长记录不一致，本窗口不提供下发、同步或设备瓶颈判断。")
        return appended

    launched = [c for c in report.candidates() if c.bottleneck == "launch_bound"]
    if idle < 0.10:
        verdict = (
            f"Timeline 证据：设备接近满负荷（空闲 {idle * 100:.1f}%），"
            "下发受限假设缺少空间，应先看 kernel 执行时长"
        )
    elif not summary.analyzed_gaps:
        verdict = "Timeline 证据：没有达到归因阈值的 gap，下发受限证据不足"
    elif acl_share >= 0.5:
        verdict = "Timeline 证据：空闲期间 Host 在下发 ACL 调用，支持下发受限假设"
    elif summary.silent_covered_by_mstx_us > 0 and summary.silent_unattributed_us < summary.silent_covered_by_mstx_us:
        verdict = (
            "Timeline 证据：静默空洞主要落在 MSTX 打点的 Python 阶段内，"
            "属上层调度/等待；下发受限假设缺少支撑"
        )
    elif acl_share >= 0.2:
        verdict = (
            f"Timeline 证据：Host 只覆盖 {acl_share * 100:.0f}% 的空闲，"
            "下发受限只能算部分支撑"
        )
    else:
        verdict = "Timeline 证据：空闲主要由 Host 静默 gap 构成，下发受限假设缺少直接支撑"
    if graph:
        appended.append(verdict)
    else:
        for candidate in launched:
            candidate.caveats = tuple(candidate.caveats) + (verdict,)

    if summary.overlap_available:
        comm_verdict = (
            f"Timeline 证据：通信未重叠时间占窗口 {summary.comm_exposed_share_of_span * 100:.1f}%"
            f"（暴露比例 {summary.comm_exposed_ratio * 100:.0f}%），支持通信受限"
            if summary.comm_exposed_share_of_span >= 0.15
            else f"Timeline 证据：通信有 {summary.comm_exposed_ratio * 100:.0f}% 未被计算掩盖，"
            "但占窗口比例不高，通信不是主要瓶颈"
        )
        for candidate in report.candidates():
            if candidate.bottleneck == "communication_bound":
                candidate.caveats = tuple(candidate.caveats) + (comm_verdict,)

    if summary.mstx_sync_us > 0:
        share = summary.mstx_sync_us / summary.span_us if summary.span_us else 0.0
        if share >= 0.05:
            sync_verdict = (
                f"Timeline 证据：MSTX 同步打点合计 {summary.mstx_sync_us / 1000:.1f}ms"
                f"（占窗口 {share * 100:.1f}%），支持同步受限假设"
            )
        else:
            sync_verdict = (
                f"Timeline 证据：MSTX 同步打点仅 {summary.mstx_sync_us / 1000:.1f}ms"
                f"（占窗口 {share * 100:.1f}%），同步受限证据不足"
            )
    else:
        sync_verdict = (
            "Timeline 证据：窗口内没有用于识别 Event/Barrier 等等待的 MSTX 区间，同步受限证据不足"
        )
    if graph:
        appended.append(sync_verdict)
    else:
        for candidate in report.candidates():
            if candidate.bottleneck == "synchronization_bound":
                candidate.caveats = tuple(candidate.caveats) + (sync_verdict,)
    if launched and not graph:
        appended.append(
            f"已为 {len(launched)} 个 launch_bound 候选附加 Timeline 证据说明"
            f"（Host ACL 覆盖空闲 {acl_share * 100:.1f}%）"
        )
    return appended
