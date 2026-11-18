"""R10：rank 的 Task Duration 截断必须转为数据质量标记。

历史错误（README 已知约束 10）：decode-eager rank1 的长 kernel 时长被截断
（CSV 34.1 µs vs 硬件 146,589 µs），各 rank 任务数相同但累计时长差数倍。
不变量 INV-05：不得把它解释为「某卡负载不均/在等」，相关结论必须受限。
"""

from __future__ import annotations

from pathlib import Path

from _helpers import make_rank_export, make_timeline_db, summary_row, write_op_summary
from opt_discovery.analyzer.timeline import summarize
from opt_discovery.analyzer.window import Window

WINDOW = Window(start_us=0.0, end_us=10_000.0, rounds=(1,))


def _rank(tmp_path: Path, rank: int, duration_us: float) -> Path:
    prof = make_rank_export(tmp_path / "export", rank)
    write_op_summary(
        prof / "mindstudio_profiler_output" / f"op_summary_{rank}.csv",
        [
            summary_row("MatMul", duration_us, start_us=100.0 + index * 1000)
            for index in range(4)
        ],
    )
    return make_timeline_db(prof / f"msprof_{rank}.db")


def test_equal_task_counts_but_huge_duration_gap_is_flagged(tmp_path: Path) -> None:
    db0 = _rank(tmp_path, 0, 1000.0)   # 4 x 1000us
    db1 = _rank(tmp_path, 1, 34.0)     # 同一 task 数，时长被截断

    summary = summarize(db0, WINDOW, extra_db_paths=[db1])

    assert summary.busy_source == "op_summary"
    assert summary.rank_duration_inconsistent is True
    notes = " ".join(summary.notes)
    assert "累计时长相差数倍" in notes
    assert "不可比" in notes
    # 负向：不得走「TP 两卡明显不对称」的解释分支
    assert "TP 两卡明显不对称" not in notes
    assert not any("负载不均" in note for note in summary.notes)


def test_consistent_ranks_are_not_flagged(tmp_path: Path) -> None:
    db0 = _rank(tmp_path, 0, 1000.0)
    db1 = _rank(tmp_path, 1, 1000.0)

    summary = summarize(db0, WINDOW, extra_db_paths=[db1])

    assert summary.rank_duration_inconsistent is False
