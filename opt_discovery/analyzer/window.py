"""Steady-state window selection (round policy).

Policy decision (see README): the window is defined by **rounds recorded by the
harness**, not by device idle gaps.  Gap-based windowing is confounded when
comparing Eager vs Graph, because graph mode changes exactly those gaps.

The window carries its own provenance, and each window is *quality checked*
after parsing (busy union vs span, largest internal gap).  A degraded window is
reported as degraded -- it is never silently widened or narrowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..collectors.workload_log import RunLog

__all__ = [
    "Window",
    "WindowQuality",
    "IntervalUnion",
    "WindowError",
    "select_rounds_window",
    "DEFAULT_MAX_GAP_US",
    "DEFAULT_MIN_BUSY_RATIO",
]

#: 窗口内允许的最大空闲空洞；超过则判定窗口质量下降
DEFAULT_MAX_GAP_US = 100_000.0
#: 窗口内设备忙碌比例下限
DEFAULT_MIN_BUSY_RATIO = 0.10


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


def select_rounds_window(
    run_log: RunLog,
    rounds: Iterable[int] = (1, 2, 3, 4),
    *,
    policy: str = "rounds",
) -> Window:
    """Build the window covering the requested round indices.

    Round ``0`` is the first measured round and is *not* part of the default
    steady window: it still contains first-call compilation / cache effects.
    """
    wanted = tuple(int(r) for r in rounds)
    if not wanted:
        raise WindowError("rounds 不能为空")
    by_index = {round_.index: round_ for round_ in run_log.rounds}
    missing = [r for r in wanted if r not in by_index]
    if missing:
        raise WindowError(
            f"请求的轮次 {missing} 不存在；日志中只有 {sorted(by_index)}。"
            "请检查 workload.log 是否覆盖了这些轮次。"
        )
    selected = [by_index[r] for r in sorted(wanted)]
    start = min(item.start_us for item in selected)
    end = max(item.end_us for item in selected)
    return Window(
        start_us=start,
        end_us=end,
        rounds=tuple(sorted(wanted)),
        policy=policy,
        provenance="measured",
        source=run_log.path,
        round_spans=tuple((item.index, item.start_us, item.end_us) for item in selected),
    )
