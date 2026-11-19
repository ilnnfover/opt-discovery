"""Steady-state window selection (round policy).

The low-level ``Window`` / ``WindowQuality`` / ``IntervalUnion`` types live in
``opt_discovery.primitives`` so parsers and analyzers can share them without a
``parsers -> analyzer`` dependency.  This module keeps the historical import
path and the round-selection policy.
"""

from __future__ import annotations

from typing import Iterable

from ..collectors.workload_log import RunLog
from ..primitives import (
    DEFAULT_MAX_GAP_US,
    DEFAULT_MIN_BUSY_RATIO,
    IntervalUnion,
    Window,
    WindowError,
    WindowQuality,
)

__all__ = [
    "Window",
    "WindowQuality",
    "IntervalUnion",
    "WindowError",
    "select_rounds_window",
    "DEFAULT_MAX_GAP_US",
    "DEFAULT_MIN_BUSY_RATIO",
]


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
