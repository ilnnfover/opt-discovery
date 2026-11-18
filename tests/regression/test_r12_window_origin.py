"""R12：epoch 原点不算设备空洞；只有有效分析窗口内的间隔才是 gap。

历史错误（README 已知约束 12）：多 rank 合并曾把 epoch 到首个算子之间算作空洞，
产生 1e15 量级伪 gap。不变量 INV-06：时间线区间必须裁剪到有效分析窗口。
"""

from __future__ import annotations

from pathlib import Path

from _helpers import make_rank_export, make_timeline_db, summary_row, write_op_summary
from opt_discovery.analyzer.timeline import summarize
from opt_discovery.analyzer.window import Window

EPOCH_US = 1_790_000_000_000_000.0  # 真实 epoch µs 量级


def _summary(tmp_path: Path, origin: float):
    prof = make_rank_export(tmp_path / "export", 0)
    # 首个任务在窗口起点后 100us；两段忙区间之间有 50us 真实空洞
    write_op_summary(
        prof / "mindstudio_profiler_output" / "op_summary_0.csv",
        [
            summary_row("MatMul", 100.0, start_us=origin + 100.0),
            summary_row("MatMul", 100.0, start_us=origin + 250.0),
        ],
    )
    db = make_timeline_db(prof / "msprof_0.db")
    window = Window(start_us=origin, end_us=origin + 500.0, rounds=(1,))
    return summarize(db, window)


def test_epoch_origin_does_not_create_a_pseudo_gap(tmp_path: Path) -> None:
    summary = _summary(tmp_path, EPOCH_US)

    # 只有 250-200=50us 这一个真实空洞
    assert summary.gap_count == 1
    assert summary.gap_max_us == 50.0
    assert summary.gap_max_us < 1_000_000
    # 空闲 = 窗口 500 - 忙 200 = 300（首段前的 100us 记为 idle，但不是「空洞事件」）
    assert summary.device_idle_us == 300.0
    assert summary.span_us == 500.0


def test_gap_semantics_are_independent_of_the_epoch_offset(tmp_path: Path) -> None:
    near_zero = _summary(tmp_path / "zero", 0.0)
    far_epoch = _summary(tmp_path / "epoch", EPOCH_US)

    assert far_epoch.gap_max_us == near_zero.gap_max_us == 50.0
    assert far_epoch.gap_count == near_zero.gap_count == 1
    assert far_epoch.device_idle_us == near_zero.device_idle_us


def test_explicit_and_fallback_window_have_distinct_semantics(tmp_path: Path) -> None:
    """显式窗口统计整段窗口；回退窗口（无窗口）只统计首个到末个任务之间。

    任务 100–200 / 250–350 µs：
    * 显式窗口 0–500：span=500、idle=300（含首尾未覆盖区间）；
    * 回退窗口：span=busy+gaps=250、idle=50；真实空洞仍是 50 µs，两种口径都不得出现伪 gap。
    """
    prof = make_rank_export(tmp_path / "fallback", 0)
    db = make_timeline_db(prof / "msprof_0.db", tasks=[(100.0, 200.0), (250.0, 350.0)])

    explicit = summarize(db, Window(start_us=0.0, end_us=500.0, rounds=(1,)))
    fallback = summarize(db, None)

    assert explicit.span_us == 500.0
    assert explicit.device_idle_us == 300.0
    assert fallback.span_us == 250.0
    assert fallback.device_idle_us == 50.0
    assert explicit.gap_count == fallback.gap_count == 1
    assert explicit.gap_max_us == fallback.gap_max_us == 50.0
