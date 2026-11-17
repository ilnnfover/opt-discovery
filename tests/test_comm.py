"""Communication volume (the conserved quantity across execution modes)."""

from __future__ import annotations

import sqlite3

import pytest

from opt_discovery.analyzer.comm import CommVolumeError, summarize_comm


def _make_db(path, rows) -> str:
    """rows: (globalTaskId, size_bytes, bandwidth)"""
    connection = sqlite3.connect(path)
    connection.execute(
        "create table COMMUNICATION_TASK_INFO ("
        "globalTaskId integer, planeId integer, groupName text, size integer, bandwidth real)"
    )
    connection.executemany(
        "insert into COMMUNICATION_TASK_INFO values (?, 0, 'g', ?, ?)", rows
    )
    connection.commit()
    connection.close()
    return str(path)


def test_dedup_by_task(tmp_path):
    """同一通信任务的多个 plane 行只应计一次（否则字节数翻倍）。"""
    db = _make_db(
        tmp_path / "p.db",
        [
            (1, 1024, 100e9),
            (1, 1024, 100e9),  # 同任务的第二个 plane
            (2, 2048, 200e9),
        ],
    )
    volume = summarize_comm(db)
    assert volume.available is True
    assert volume.records == 3
    assert volume.unique_tasks == 2
    assert volume.bytes_total == pytest.approx(3072.0)  # 1024 + 2048，不是 4096
    assert volume.dedup_ratio == pytest.approx(1.5)


def test_missing_table_returns_unavailable(tmp_path):
    path = tmp_path / "empty.db"
    connection = sqlite3.connect(path)
    connection.execute("create table OTHER (x integer)")
    connection.commit()
    connection.close()
    volume = summarize_comm(str(path))
    assert volume.available is False
    assert volume.bytes_total == 0.0
    assert volume.notes


def test_missing_file_raises(tmp_path):
    with pytest.raises(CommVolumeError):
        summarize_comm(tmp_path / "nope.db")


def test_per_task_size_reveals_merged_records(tmp_path):
    """记录数少但单条更大 => 记录被合并（这正是 Graph 模式下的现象）。"""
    merged = _make_db(tmp_path / "merged.db", [(i, 4096, 50e9) for i in range(1, 5)])
    split = _make_db(
        tmp_path / "split.db", [(i, 1024, 50e9) for i in range(1, 17)]
    )
    a, b = summarize_comm(merged), summarize_comm(split)
    assert a.bytes_total == pytest.approx(b.bytes_total)  # 总量守恒
    assert a.unique_tasks * 4 == b.unique_tasks  # 记录数差 4 倍
    assert a.kib_per_task == pytest.approx(b.kib_per_task * 4, rel=1e-6)
