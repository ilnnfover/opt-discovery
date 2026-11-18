"""R06：只有枚举表 != 已采集 MSTX 事件。

历史错误（README 已知约束 6）：四组导出都带 ``ENUM_MSTX_EVENT_TYPE``，
但没有 ``MsprofTx`` 表；把枚举表当成「已开启打点」会让 host_silent 被错误归因。
不变量 INV-05/INV-11：缺失能力必须显式标记，不能猜测原因。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from _helpers import make_timeline_db, summary_row, write_op_summary
from opt_discovery.analyzer.timeline import summarize
from opt_discovery.analyzer.window import Window
from opt_discovery.collectors.mstx import load_mstx


def _enum_only_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "create table ENUM_MSTX_EVENT_TYPE (id integer, name text, value text)"
    )
    connection.executemany(
        "insert into ENUM_MSTX_EVENT_TYPE values (?, ?, ?)",
        [(0, "marker", "0"), (1, "push_pop", "1"), (2, "start_end", "2")],
    )
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.commit()
    connection.close()
    return path


def _enabled_but_empty_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "create table MsprofTx (start_time integer, end_time integer, event_type integer,"
        " message text, tid integer, category text)"
    )
    connection.commit()
    connection.close()
    return path


def test_enum_table_alone_does_not_mean_mstx_was_collected(tmp_path: Path) -> None:
    trace = load_mstx(_enum_only_db(tmp_path / "msprof_0.db"))

    assert trace.available is False
    assert trace.ranges == ()
    assert trace.sync_us == 0.0
    assert any("没有开启 MSTX" in note for note in trace.notes)
    assert any("无法进一步归因" in note for note in trace.notes)


def test_enabled_but_empty_trace_is_distinguishable(tmp_path: Path) -> None:
    """「开启了但没打点」与「没开启」必须是两种状态，不能混为一谈。"""
    trace = load_mstx(_enabled_but_empty_db(tmp_path / "msprof_1.db"))

    assert trace.available is True
    assert trace.ranges == ()
    assert any("未实际打点" in note for note in trace.notes)


def test_missing_sync_metric_is_not_reported_as_measured_zero(tmp_path: Path) -> None:
    """缺 MSTX 时：同步指标只能是「未采集」状态，不能被当成实测 0 或猜测原因。

    任务 100–200 / 400–500 µs，窗口 0–600 µs：存在 200 µs 的 host_silent 空洞。
    仅枚举表的库必须让 summary 明确「未开启」，且空洞保持未归因。
    """
    prof = tmp_path / "PROF_000000_fixture"
    write_op_summary(
        prof / "mindstudio_profiler_output" / "op_summary_0.csv",
        [
            summary_row("MatMul", 100.0, start_us=100.0),
            summary_row("MatMul", 100.0, start_us=400.0),
        ],
    )
    db = make_timeline_db(prof / "msprof_0.db", enum_mstx=True)

    trace = load_mstx(db)
    assert trace.available is False

    summary = summarize(
        db, Window(start_us=0.0, end_us=600.0, rounds=(1,)), mstx=trace
    )
    assert summary.mstx_available is False
    # 未采集的同步时间不是「实测 0」：必须由 available=False 承载这个事实
    assert summary.mstx_sync_us == 0.0
    assert summary.silent_covered_by_mstx_us == 0.0
    assert summary.silent_unattributed_us > 0.0

    notes = " ".join(summary.notes)
    assert "MSTX 未开启" in notes
    assert "无法进一步归因" in notes
    # 不得出现把缺失指标当实测结论的措辞
    assert "同步等待合计" not in notes
