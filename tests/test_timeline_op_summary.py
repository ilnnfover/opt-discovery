"""busy 统计切换到 op_summary 算子区间：优先级、回退告警与多 rank 口径。"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from opt_discovery.analyzer.timeline import summarize
from opt_discovery.analyzer.window import Window

NS = 1000  # us -> ns

OP_HEADER = [
    "Device_id",
    "Model ID",
    "Task ID",
    "Stream ID",
    "Op Name",
    "OP Type",
    "OP State",
    "Task Type",
    "Task Start Time(us)",
    "Task Duration(us)",
]


def _make_db(path, *, tasks, host) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.executemany(
        "insert into TASK values (?, ?)", [(int(s * NS), int(e * NS)) for s, e in tasks]
    )
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.executemany(
        "insert into CANN_API values (?, ?, ?)",
        [(name, int(s * NS), int(e * NS)) for s, e, name in host],
    )
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.commit()
    connection.close()
    return str(path)


def _make_op_csv(db_path, rows) -> str:
    """在 rank db 旁建 mindstudio_profiler_output/op_summary_*.csv。

    rows: [(start_us, duration_us, task_type), ...]，与 msprof 实际产物同布局。
    """
    prof_dir = db_path.parent / "mindstudio_profiler_output"
    prof_dir.mkdir(parents=True, exist_ok=True)
    csv_path = prof_dir / "op_summary_0001.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(OP_HEADER)
        for start, duration, task_type in rows:
            writer.writerow([1, -1, 0, 47, "op", "Matmul", "dynamic", task_type, start, duration])
    return str(db_path)


def _rank_dir(tmp_path, name) -> "object":
    """每个 rank 独立 PROF_xxx 目录，模拟真实 bundle 布局（避免 CSV 相互污染）。"""
    return tmp_path / f"PROF_{name}"


def test_op_summary_preferred_over_task_table(tmp_path):
    """db 旁有 op_summary CSV 时用算子区间，忽略 TASK 表。"""
    rank = _rank_dir(tmp_path, "a")
    db = _make_db(rank / "a.db", tasks=[(0, 100)], host=[])
    _make_op_csv(
        rank / "a.db",
        [(0.0, 800.0, "AI_CORE"), (850.0, 100.0, "AI_VECTOR_CORE")],
    )
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db, window)
    assert summary.busy_source == "op_summary"
    assert summary.device_busy_us == pytest.approx(900.0, abs=1.0)
    assert summary.device_idle_ratio == pytest.approx(0.1, abs=0.01)
    assert summary.task_count == 2
    assert any("op_summary 算子区间" in note for note in summary.notes)


def test_fallback_to_task_table_warns(tmp_path):
    """无 op_summary CSV 时回退 TASK 表，但必须显式告知该表不可信。"""
    rank = _rank_dir(tmp_path, "a")
    db = _make_db(rank / "a.db", tasks=[(0, 900)], host=[])
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db, window)
    assert summary.busy_source == "task_table"
    assert summary.device_busy_us == pytest.approx(900.0, abs=1.0)
    assert any("不可信" in note for note in summary.notes)
    assert any("缺少 op_summary" in note for note in summary.notes)


def test_op_summary_parsing_skips_bad_rows(tmp_path):
    """坏行（空值/非数字/零时长）跳过，窗口外区间过滤。"""
    rank = _rank_dir(tmp_path, "a")
    db = _make_db(rank / "a.db", tasks=[], host=[])
    prof_dir = rank / "mindstudio_profiler_output"
    prof_dir.mkdir(parents=True)
    csv_path = prof_dir / "op_summary_0001.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(OP_HEADER)
        writer.writerow([1, -1, 0, 47, "op", "Matmul", "dynamic", "AI_CORE", 1794752767897208.919, 200.0])
        writer.writerow([1, -1, 1, 47, "op", "Matmul", "dynamic", "AI_CORE", "", 100.0])
        writer.writerow([1, -1, 2, 47, "op", "Matmul", "dynamic", "AI_CORE", "abc", 100.0])
        writer.writerow([1, -1, 3, 47, "op", "Matmul", "dynamic", "AI_CORE", 300.0, 0.0])
        writer.writerow([1, -1, 4, 47, "op", "Matmul", "dynamic", "AI_CORE", 500.0, 100.0])
        writer.writerow([1, -1, 5, 47, "op", "Matmul", "dynamic", "AI_CORE", 900.0, 100.0])
    window = Window(0, 600, rounds=(1,))
    summary = summarize(str(db), window)
    assert summary.busy_source == "op_summary"
    # 窗口 [0,600] 内有效区间仅 [500..600]：epoch 行/900 行在窗口外，''/'abc'/0 时长为坏行
    assert summary.task_count == 1
    assert summary.device_busy_us == pytest.approx(100.0, abs=1.0)


def test_multi_rank_op_summary_union(tmp_path):
    """多 rank 各忙互补的算子区间，任一设备忙接近 100%。"""
    rank_a, rank_b = _rank_dir(tmp_path, "a"), _rank_dir(tmp_path, "b")
    db0 = _make_db(rank_a / "a.db", tasks=[(0, 100)], host=[])
    db1 = _make_db(rank_b / "b.db", tasks=[(0, 100)], host=[])
    _make_op_csv(rank_a / "a.db", [(0.0, 500.0, "AI_CORE")])
    _make_op_csv(rank_b / "b.db", [(500.0, 500.0, "AI_CORE")])
    window = Window(0, 1000, rounds=(1,))
    merged = summarize(db0, window, extra_db_paths=[db1])
    assert merged.busy_source == "op_summary"
    assert merged.device_idle_ratio == pytest.approx(0.0, abs=0.01)
    assert merged.device_busy_all_us == pytest.approx(0.0, abs=1.0)
    assert merged.rank_asymmetry_pp == pytest.approx(0.0, abs=0.1)
    assert all(item["source"] == "op_summary" for item in merged.per_rank)


def test_multi_rank_partial_fallback_warns_per_rank(tmp_path):
    """一个 rank 有 CSV、另一个没有：整体仍出结论，但缺 CSV 的 rank 被点名。"""
    rank_a, rank_b = _rank_dir(tmp_path, "a"), _rank_dir(tmp_path, "b")
    db0 = _make_db(rank_a / "a.db", tasks=[(0, 500)], host=[])
    db1 = _make_db(rank_b / "b.db", tasks=[(0, 100)], host=[])
    _make_op_csv(rank_a / "a.db", [(0.0, 500.0, "AI_CORE")])
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db0, window, extra_db_paths=[db1])
    assert summary.busy_source == "op_summary"
    sources = {item["source"] for item in summary.per_rank}
    assert sources == {"op_summary", "task_table"}
    assert any("缺少 op_summary" in note for note in summary.notes)


def test_communication_rows_unit_scaled(tmp_path):
    """op_summary 里 COMMUNICATION 行的 Task Duration 实际是 ns，需缩放为 µs。"""
    rank = _rank_dir(tmp_path, "a")
    db = _make_db(rank / "a.db", tasks=[], host=[])
    _make_op_csv(
        rank / "a.db",
        [(0.0, 800.0, "COMMUNICATION"), (900.0, 50.0, "AI_CORE")],
    )
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db, window)
    assert summary.busy_source == "op_summary"
    # COMMUNICATION 800(ns) -> 0.8us；若不缩放则是 800000us，busy_ratio 远超 1
    assert summary.device_busy_us == pytest.approx(50.8, abs=0.1)
    assert summary.device_busy_us < 1000.0


def test_no_pseudo_gap_before_first_epoch_interval(tmp_path):
    """窗口起点远早于首个算子（epoch 时间戳）时，不得产生 1e15 量级伪 gap。"""
    rank = _rank_dir(tmp_path, "a")
    db = _make_db(rank / "a.db", tasks=[], host=[])
    epoch = 1794752897420819.2
    _make_op_csv(
        rank / "a.db",
        [(epoch + 100.0, 400.0, "AI_CORE"), (epoch + 700.0, 200.0, "AI_CORE")],
    )
    window = Window(epoch, epoch + 1000, rounds=(1,))
    summary = summarize(db, window)
    assert summary.busy_source == "op_summary"
    assert summary.device_busy_us == pytest.approx(600.0, abs=1.0)
    # 首个算子前不是空洞；真正的空洞只有 [500..700] 的 200us
    assert summary.gap_max_us == pytest.approx(200.0, abs=1.0)
    assert summary.gap_count == 1
