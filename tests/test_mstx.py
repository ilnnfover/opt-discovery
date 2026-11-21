"""B：MSTX 打点解析与静默空洞归因。"""

from __future__ import annotations

import sqlite3

import pytest

from opt_discovery.analyzer.timeline import annotate_diagnosis, summarize
from opt_discovery.analyzer.window import Window
from opt_discovery.collectors.mstx import load_mstx

NS = 1000  # us -> ns


def _make_db(path, *, tasks, host, mstx=None, strings=None) -> str:
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
    connection.executemany("insert into STRING_IDS values (?, ?)", sorted((strings or {}).items()))
    if mstx is not None:
        connection.execute(
            "create table MsprofTx (category text, pid integer, tid integer, start_time integer,"
            " end_time integer, payload_type integer, payload_value integer,"
            " message_type integer, message text, event_type integer)"
        )
        connection.executemany(
            "insert into MsprofTx values ('', 1, 1, ?, ?, 0, 0, 0, ?, 2)",
            [(int(s * NS), int(e * NS), name) for s, e, name in mstx],
        )
    connection.commit()
    connection.close()
    return str(path)


def test_missing_table_is_reported_not_faked(tmp_path):
    db = _make_db(tmp_path / "p.db", tasks=[(0, 100)], host=[])
    trace = load_mstx(db, Window(0, 1000, rounds=(1,)))
    assert trace.available is False
    assert "缺少 MsprofTx / MSTX_EVENTS 表" in trace.notes[0]


def test_ranges_are_windowed_and_unit_detected(tmp_path):
    db = _make_db(
        tmp_path / "p.db",
        tasks=[(0, 100)],
        host=[],
        mstx=[(10, 50, "engine_step"), (5000, 6000, "outside")],
    )
    trace = load_mstx(db, Window(0, 1000, rounds=(1,)))
    assert trace.available is True
    assert trace.unit == "ns"
    assert [item.name for item in trace.ranges] == ["engine_step"]
    assert trace.ranges[0].duration_us == pytest.approx(40.0)


def test_silent_gap_attributed_to_python_range(tmp_path):
    db = _make_db(
        tmp_path / "p.db",
        tasks=[(0, 100), (900, 1000)],
        host=[(0, 50, 0), (950, 980, 0)],
        mstx=[(100, 900, "engine_step")],
    )
    summary = summarize(db, Window(0, 1000, rounds=(1,)), mstx=load_mstx(db, Window(0, 1000, rounds=(1,))))
    assert summary.gap_count == 1
    assert summary.gaps_by_class["host_silent"] == 1
    assert summary.silent_covered_by_mstx_us == pytest.approx(800.0)
    assert summary.silent_by_range[0][0] == "engine_step"
    assert any("Python 阶段" in note for note in summary.notes)


def test_sync_range_is_detected_and_used_in_caveat(tmp_path):
    db = _make_db(
        tmp_path / "p.db",
        tasks=[(0, 100), (900, 1000)],
        host=[(0, 50, 0), (950, 980, 0)],
        mstx=[(100, 900, "engine_step"), (200, 400, "sync_wait_stream")],
    )
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db, window, mstx=load_mstx(db, window))
    assert summary.mstx_sync_us == pytest.approx(200.0)
    assert any("同步等待合计" in note for note in summary.notes)

    class _Report:
        def __init__(self) -> None:
            self.notes = ()
            self._items = [
                type(
                    "C",
                    (),
                    {
                        "bottleneck": "synchronization_bound",
                        "caveats": ("原始",),
                        "rule_id": "synchronization_bound.wait_dominant",
                    },
                )()
            ]

        def candidates(self):
            return list(self._items)

    report = _Report()
    annotate_diagnosis(report, summary)
    assert any("支持同步受限假设" in caveat for caveat in report.candidates()[0].caveats)


def test_without_mstx_the_sync_caveat_stays_honest(tmp_path):
    db = _make_db(tmp_path / "p.db", tasks=[(0, 100), (900, 1000)], host=[(950, 980, 0)])
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db, window, mstx=load_mstx(db, window))
    assert summary.mstx_available is False
    assert summary.silent_covered_by_mstx_us == 0.0


# ---------------------------------------------------------------------------
# 多 rank 合并口径（TP>1 时单读一个 rank 会翻转结论）
# ---------------------------------------------------------------------------
def test_multi_rank_uses_union_of_all_ranks(tmp_path):
    """两个 rank 各忙一半且互补时，任一设备忙应接近 100%。"""
    db0 = _make_db(tmp_path / "a.db", tasks=[(0, 500)], host=[])
    db1 = _make_db(tmp_path / "b.db", tasks=[(500, 1000)], host=[])
    window = Window(0, 1000, rounds=(1,))
    single = summarize(db0, window)
    assert single.device_idle_ratio == pytest.approx(0.5, abs=0.01)

    merged = summarize(db0, window, extra_db_paths=[db1])
    assert merged.device_idle_ratio == pytest.approx(0.0, abs=0.01)
    assert merged.device_busy_all_us == pytest.approx(0.0, abs=1.0)
    assert len(merged.per_rank) == 2
    assert merged.rank_asymmetry_pp == pytest.approx(0.0, abs=0.1)


def test_multi_rank_reports_asymmetry_warning(tmp_path):
    """task 数不同、单条时长相近 => 真不对称，报不对称。"""
    db0 = _make_db(tmp_path / "a.db", tasks=[(0, 900)], host=[])
    db1 = _make_db(tmp_path / "b.db", tasks=[(0, 100), (200, 300), (400, 500)], host=[])
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db0, window, extra_db_paths=[db1])
    assert summary.rank_asymmetry_pp > 50
    assert summary.rank_duration_inconsistent is False
    assert any("不对称" in note for note in summary.notes)


def test_rank_duration_mismatch_flagged_not_silent(tmp_path):
    """task 数几乎相同但累计时长差数倍 => profiler 记录不可比，必须告警。"""
    db0 = _make_db(tmp_path / "a.db", tasks=[(0, 900)], host=[])
    db1 = _make_db(tmp_path / "b.db", tasks=[(0, 100)], host=[])
    window = Window(0, 1000, rounds=(1,))
    summary = summarize(db0, window, extra_db_paths=[db1])
    assert summary.rank_duration_inconsistent is True
    assert any("累计时长相差数倍" in note for note in summary.notes)
    assert not any("不对称" in note for note in summary.notes)


def test_torch_mstx_ns_uses_table_schema_and_resolves_string_ids(tmp_path):
    db = tmp_path / "torch_npu.db"
    with sqlite3.connect(db) as connection:
        connection.executescript("""
            CREATE TABLE STRING_IDS (id INTEGER, value TEXT);
            INSERT INTO STRING_IDS VALUES (1, 'sync_wait'), (2, 'python');
            CREATE TABLE MsprofTx (start_time INTEGER, end_time INTEGER, message TEXT);
            CREATE TABLE MSTX_EVENTS (startNs INTEGER, endNs INTEGER, eventType INTEGER,
                                      message INTEGER, globalTid INTEGER, category INTEGER);
            INSERT INTO MSTX_EVENTS VALUES
                (10000, 30000, 2, 1, 17, 2),
                (14000, 16000, 2, 1, 18, 4294967295),
                (20000, 16000, 2, 1, 18, 4294967295);
        """)
    trace = load_mstx(db, Window(12, 18, rounds=(1,)))
    assert trace.available
    assert trace.unit == "ns"
    first, second = trace.ranges
    assert (first.start_us, first.end_us, first.name, first.tid, first.category) == (
        12, 18, "sync_wait", 17, "python",
    )
    assert second.category == ""
