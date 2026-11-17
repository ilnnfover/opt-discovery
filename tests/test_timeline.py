"""Timeline (TASK + CANN_API) idle attribution."""

from __future__ import annotations

import sqlite3

import pytest

from opt_discovery.analyzer.timeline import (
    CLASS_ACL_BUSY,
    CLASS_HOST_SILENT,
    TimelineError,
    annotate_diagnosis,
    summarize,
)
from opt_discovery.analyzer.window import Window
from opt_discovery.schemas.evidence import ConfidenceLevel
from opt_discovery.schemas.evidence import DiagnosisCandidate

NS = 1000  # us -> ns


def _make_db(path, tasks, host, names=("launch", "release")) -> str:
    """tasks/host are (start_us, end_us[, name]) tuples."""
    connection = sqlite3.connect(path)
    connection.execute("create table STRING_IDS (id integer primary key, value text)")
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (startNs integer, endNs integer, name integer)")
    for index, name in enumerate(names):
        connection.execute("insert into STRING_IDS values (?, ?)", (index, name))
    connection.executemany(
        "insert into TASK values (?, ?)",
        [(int(s * NS), int(e * NS)) for s, e in tasks],
    )
    connection.executemany(
        "insert into CANN_API values (?, ?, ?)",
        [(int(s * NS), int(e * NS), int(tag)) for s, e, tag in host],
    )
    connection.commit()
    connection.close()
    return str(path)


def test_gap_attribution_uses_union_not_sum(tmp_path):
    """同一 gap 内的并发 Host 事件必须取并集，否则覆盖率会超过 100%。"""
    db = _make_db(
        tmp_path / "prof.db",
        tasks=[(0, 100), (200, 300), (500, 600)],
        host=[
            # gap1 (100-200): 无事件 -> host_silent
            # gap2 (300-500): 两个重叠事件，并集 350-480 = 130us
            (350, 450, 1),
            (400, 480, 0),
        ],
    )
    summary = summarize(db, Window(0, 600, rounds=(1,)))
    assert summary.task_count == 3
    assert summary.device_busy_us == pytest.approx(300.0)
    assert summary.device_idle_us == pytest.approx(300.0)
    assert summary.device_idle_ratio == pytest.approx(0.5)
    assert summary.gap_count == 2
    assert summary.analyzed_gaps == 2
    assert summary.gaps_by_class[CLASS_HOST_SILENT] == 1
    assert summary.gaps_by_class[CLASS_ACL_BUSY] == 1
    # 并集 130us，而不是 100+80=180us
    assert summary.idle_covered_by_acl_us == pytest.approx(130.0)
    assert summary.acl_coverage_of_analyzed_idle == pytest.approx(130.0 / 300.0)
    assert summary.submit_calls == 1
    assert summary.host_acl_calls == 2


def test_window_filters_out_other_rounds(tmp_path):
    db = _make_db(
        tmp_path / "prof.db",
        tasks=[(0, 100), (1000, 1100)],
        host=[(0, 100, 0), (1000, 1100, 0)],
    )
    summary = summarize(db, Window(1000, 1100, rounds=(1,)))
    assert summary.task_count == 1
    assert summary.host_acl_calls == 1
    assert summary.device_idle_us == pytest.approx(0.0)


def test_small_gaps_are_not_attributed(tmp_path):
    db = _make_db(
        tmp_path / "prof.db",
        tasks=[(0, 100), (150, 250)],
        host=[(100, 150, 0)],
    )
    summary = summarize(db, Window(0, 250, rounds=(1,)), min_gap_us=100.0)
    assert summary.gap_count == 1
    assert summary.analyzed_gaps == 0  # 50us 的 gap 不参与归因
    assert summary.idle_analyzed_us == pytest.approx(0.0)


def test_missing_tables_raise(tmp_path):
    path = tmp_path / "empty.db"
    connection = sqlite3.connect(path)
    connection.execute("create table OTHER (x integer)")
    connection.commit()
    connection.close()
    with pytest.raises(TimelineError) as exc:
        summarize(str(path), Window(0, 100, rounds=(1,)))
    assert "TASK" in str(exc.value)


def test_annotate_diagnosis_marks_launch_candidates(tmp_path):
    class _Report:
        def __init__(self) -> None:
            self.notes = ()
            self._candidates = [
                DiagnosisCandidate(
                    rule_id="launch_bound.small_kernel_many_calls",
                    bottleneck="launch_bound",
                    level=ConfidenceLevel.HYPOTHESIS,
                    confidence=0.4,
                )
            ]

        def candidates(self):
            return list(self._candidates)

    report = _Report()
    db = _make_db(
        tmp_path / "prof.db",
        tasks=[(0, 100), (500, 600)],
        host=[(100, 500, 0)],
    )
    summary = summarize(db, Window(0, 600, rounds=(1,)))
    notes = annotate_diagnosis(report, summary)
    assert notes
    caveats = report.candidates()[0].caveats
    assert any("Timeline" in item for item in caveats)


def _add_overlap(db_path: str, rows) -> None:
    """rows: (type, start_us, end_us)"""
    connection = sqlite3.connect(db_path)
    connection.execute("create table OVERLAP_ANALYSIS (type integer, startNs integer, endNs integer)")
    connection.executemany(
        "insert into OVERLAP_ANALYSIS values (?, ?, ?)",
        [(int(t), int(s * NS), int(e * NS)) for t, s, e in rows],
    )
    connection.commit()
    connection.close()


def test_overlap_analysis_feeds_communication_hint(tmp_path):
    db = _make_db(
        tmp_path / "prof.db",
        tasks=[(0, 400), (600, 700)],
        host=[],
    )
    _add_overlap(
        db,
        [
            (0, 0, 400),     # compute 400us
            (1, 0, 600),     # comm 600us
            (2, 400, 600),   # comm 未重叠 200us
            (3, 600, 1000),  # free 400us
        ],
    )
    summary = summarize(db, Window(0, 1000, rounds=(1,)))
    assert summary.overlap_available is True
    assert summary.compute_us == pytest.approx(400.0)
    assert summary.communication_us == pytest.approx(600.0)
    assert summary.comm_not_overlapped_us == pytest.approx(200.0)
    assert summary.free_us == pytest.approx(400.0)
    assert summary.comm_exposed_share_of_span == pytest.approx(0.2)
    assert summary.comm_exposed_ratio == pytest.approx(200 / 600)
    assert summary.bottleneck_hint == "communication_exposed"


def test_overlap_window_clipping(tmp_path):
    db = _make_db(tmp_path / "prof.db", tasks=[(0, 100)], host=[])
    _add_overlap(db, [(1, 0, 400)])  # comm 400us 跨越窗口
    summary = summarize(db, Window(0, 100, rounds=(1,)))
    assert summary.communication_us == pytest.approx(100.0)  # 只计窗口内


def test_hint_undetermined_without_overlap_table(tmp_path):
    db = _make_db(
        tmp_path / "prof.db",
        tasks=[(0, 100), (900, 1000)],
        host=[],
    )
    summary = summarize(db, Window(0, 1000, rounds=(1,)))
    assert summary.overlap_available is False
    assert summary.bottleneck_hint == "host_gap_unattributed"


def test_sync_verdict_added_to_synchronization_candidates(tmp_path):
    db = _make_db(tmp_path / "prof.db", tasks=[(0, 100), (500, 600)], host=[(100, 500, 0)])
    summary = summarize(db, Window(0, 600, rounds=(1,)))

    class _Report:
        def __init__(self) -> None:
            self.notes = ()
            self._candidates = [
                DiagnosisCandidate(
                    rule_id="synchronization_bound.wait_dominant",
                    bottleneck="synchronization_bound",
                    level=ConfidenceLevel.HYPOTHESIS,
                    confidence=0.4,
                )
            ]

        def candidates(self):
            return list(self._candidates)

    report = _Report()
    annotate_diagnosis(report, summary)
    caveats = report.candidates()[0].caveats
    assert any("Event/Barrier" in item for item in caveats)
