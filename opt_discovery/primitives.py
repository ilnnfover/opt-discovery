"""Stable low-level time/interval concepts shared by parsers and analyzers.

Nothing in this module imports analyzers, collectors or parsers, so it can be
used from either side of the old ``parsers -> analyzer.window`` dependency.
The historical import path ``opt_discovery.analyzer.window`` keeps working via
re-exports.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas.operator import TaskType

__all__ = [
    "COMMUNICATION_DURATION_SCALE",
    "DEFAULT_MAX_GAP_US",
    "DEFAULT_MIN_BUSY_RATIO",
    "NS_PER_US",
    "Interval",
    "IntervalUnion",
    "Window",
    "WindowError",
    "WindowQuality",
    "normalize_duration",
    "window_span",
]

#: msprof sqlite 时间戳是 epoch 纳秒，分析层统一用微秒
NS_PER_US = 1000.0

#: profiler 的 ``Task Duration(us)`` 列里，COMMUNICATION 行实际是 ns，
#: 其余类型（AI_CORE / AI_VECTOR_CORE / MIX_AIC ...）是 us。
#: 实测 decode-eager：9685 行通信累计 3.399e10，按 ns=34s 与 48s 窗口自洽；
#: 按 us 解释则是 33990s，物理上不可能。
COMMUNICATION_DURATION_SCALE = 0.001

#: 窗口内允许的最大空闲空洞；超过则判定窗口质量下降
DEFAULT_MAX_GAP_US = 100_000.0
#: 窗口内设备忙碌比例下限
DEFAULT_MIN_BUSY_RATIO = 0.10


def normalize_duration(
    duration_value: float, *, unit_scale: float = 1.0, task_type: TaskType
) -> float:
    """Convert a raw duration to µs and apply the COMMUNICATION ns quirk once."""
    duration_us = float(duration_value) * unit_scale
    if task_type is TaskType.COMMUNICATION:
        duration_us *= COMMUNICATION_DURATION_SCALE
    return duration_us


def window_span(
    start_us: float,
    duration_us: float,
    window: "Window | None",
    *,
    policy: str = "intersect",
) -> tuple[float, float] | None:
    """Apply a window policy and return ``(start_us, duration_us)``.

    * ``policy="start"``：只按起点判断（Parser 既有的起点筛选）
    * ``policy="intersect"``：与窗口相交即保留完整时长（Timeline 既有口径）
    * ``policy="clip"``：裁剪到窗口；在 epoch 上尽量用“扣减两侧时长”避免先加后减
    """
    if window is None:
        return start_us, duration_us
    end_us = start_us + duration_us
    if policy == "start":
        if not window.contains(start_us):
            return None
        return start_us, duration_us
    if policy == "intersect":
        if end_us < window.start_us or start_us > window.end_us:
            return None
        return start_us, duration_us
    if policy != "clip":
        raise ValueError(f"unknown window policy: {policy!r}")
    # 右边界为开区间：起点正好落在 window.end_us 的事件没有交集
    if start_us >= window.end_us:
        return None
    left = max(0.0, window.start_us - start_us)
    right = max(0.0, end_us - window.end_us)
    clipped_duration = duration_us - left - right
    if clipped_duration <= 0.0:
        return None
    return start_us + left, clipped_duration


class WindowError(ValueError):
    """Raised when the requested window cannot be built from the run log."""


@dataclass(frozen=True)
class Window:
    start_us: float
    end_us: float
    rounds: tuple[int, ...]
    policy: str = "rounds"
    provenance: str = "measured"
    source: str | None = None
    #: (round_index, start_us, end_us)，用于检测"某一轮完全没数据"
    round_spans: tuple[tuple[int, float, float], ...] = ()

    def __post_init__(self) -> None:
        if self.end_us <= self.start_us:
            raise WindowError(f"window end {self.end_us} <= start {self.start_us}")

    @property
    def duration_us(self) -> float:
        return self.end_us - self.start_us

    def contains(self, timestamp_us: float) -> bool:
        return self.start_us <= timestamp_us <= self.end_us

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "provenance": self.provenance,
            "source": self.source,
            "rounds": list(self.rounds),
            "start_us": self.start_us,
            "end_us": self.end_us,
            "duration_us": self.duration_us,
            "round_spans": [
                {"round": idx, "start_us": start, "end_us": end}
                for idx, start, end in self.round_spans
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Window":
        return cls(
            start_us=float(data["start_us"]),
            end_us=float(data["end_us"]),
            rounds=tuple(int(r) for r in data.get("rounds") or ()),
            policy=str(data.get("policy", "rounds")),
            provenance=str(data.get("provenance", "measured")),
            source=data.get("source"),
            round_spans=tuple(
                (int(item["round"]), float(item["start_us"]), float(item["end_us"]))
                for item in data.get("round_spans") or ()
            ),
        )


@dataclass(frozen=True)
class WindowQuality:
    span_us: float
    busy_us: float
    gap_count: int
    max_gap_us: float
    underfeed_ratio: float
    degraded: bool = False
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "span_us": self.span_us,
            "busy_us": self.busy_us,
            "busy_ratio": 1.0 - self.underfeed_ratio,
            "gap_count": self.gap_count,
            "max_gap_us": self.max_gap_us,
            "underfeed_ratio": self.underfeed_ratio,
            "degraded": self.degraded,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Interval:
    start_us: float
    end_us: float

    @property
    def duration_us(self) -> float:
        return self.end_us - self.start_us

    def overlap_us(self, other: "Interval") -> float:
        return max(0.0, min(self.end_us, other.end_us) - max(self.start_us, other.start_us))

    def clipped(self, window: Window) -> "Interval | None":
        start = max(self.start_us, window.start_us)
        end = min(self.end_us, window.end_us)
        return Interval(start, end) if end > start else None


class IntervalUnion:
    """Streaming busy-union / gap statistics for the rows kept in a window.

    Rows must be time ordered (msprof exports are).  Out-of-order rows are
    counted and reported instead of silently producing wrong numbers.
    """

    def __init__(self) -> None:
        self._seg_start: float | None = None
        self._seg_end: float | None = None
        self._busy = 0.0
        self._gap_count = 0
        self._max_gap = 0.0
        self._out_of_order = 0
        self._first: float | None = None
        self._last: float | None = None
        self._rows = 0

    def add(self, start_us: float, end_us: float) -> None:
        self._rows += 1
        if self._last is not None and start_us < self._last:
            self._out_of_order += 1
        self._last = start_us if self._last is None else max(self._last, start_us)
        if self._seg_start is None:
            self._seg_start, self._seg_end = start_us, end_us
            self._first = start_us
            return
        assert self._seg_end is not None
        if start_us <= self._seg_end:
            self._seg_end = max(self._seg_end, end_us)
        else:
            self._busy += self._seg_end - self._seg_start
            gap = start_us - self._seg_end
            self._gap_count += 1
            self._max_gap = max(self._max_gap, gap)
            self._seg_start, self._seg_end = start_us, end_us

    def finalize(
        self,
        span_us: float,
        *,
        max_gap_us: float = DEFAULT_MAX_GAP_US,
        min_busy_ratio: float = DEFAULT_MIN_BUSY_RATIO,
    ) -> WindowQuality:
        if self._seg_start is not None and self._seg_end is not None:
            self._busy += self._seg_end - self._seg_start
        span = span_us if span_us > 0 else 0.0
        underfeed = 1.0 - (self._busy / span) if span > 0 else 1.0
        reasons: list[str] = []
        if self._max_gap > max_gap_us:
            reasons.append(
                f"窗口内最大空洞 {self._max_gap / 1000:.1f} ms 超过 {max_gap_us / 1000:.0f} ms"
            )
        if span > 0 and (1.0 - underfeed) < min_busy_ratio:
            reasons.append(f"窗口内设备忙碌比例仅 {(1.0 - underfeed):.3f}")
        if self._out_of_order:
            reasons.append(f"{self._out_of_order} 行时间戳乱序，busy 统计可能偏小")
        return WindowQuality(
            span_us=span,
            busy_us=self._busy,
            gap_count=self._gap_count,
            max_gap_us=self._max_gap,
            underfeed_ratio=underfeed,
            degraded=bool(reasons),
            reasons=tuple(reasons),
        )

    @property
    def rows(self) -> int:
        return self._rows
