"""Hotspot matrix and cross-workload comparison."""

from __future__ import annotations

import pytest

from opt_discovery.analyzer.compare import compare_tables
from opt_discovery.analyzer.hotspot import hotspot_matrix, matrix_to_text, operator_hotspots
from opt_discovery.schemas.metrics import Metric
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from opt_discovery.schemas.workload import WorkloadSpec


def _workload(workload_id: str, *, mode: str = "eager", length: int = 1) -> WorkloadSpec:
    return WorkloadSpec.from_dict(
        {
            "workload_id": workload_id,
            "model": {"name": "Qwen3.5-35B-A3B", "dtype": "bfloat16"},
            "hardware": {"device": "Ascend 910B", "device_count": 2},
            "execution": {"phase": "decode", "mode": mode},
            "input": {"batch_size": 1, "input_length": length},
            "verified": True,
        }
    )


def _table(workload: WorkloadSpec, rows, window=None) -> OperatorTable:
    records = []
    for operator, calls, total_us, task_type in rows:
        records.append(
            OperatorRecord(
                workload_id=workload.workload_id,
                operator=operator,
                calls=calls,
                total_time_us=Metric(total_us, "us"),
                task_type=task_type,
            )
        )
    meta = ParseMeta(parser="test", extra={"window": window})
    return OperatorTable(workload=workload, records=records, meta=meta)


def test_hotspots_rank_and_share():
    workload = _workload("wl-a")
    table = _table(
        workload,
        [
            ("Add", 10, 3000.0, TaskType.KERNEL),
            ("MatMul", 2, 1000.0, TaskType.KERNEL),
            ("hcom_allReduce_", 4, 6000.0, TaskType.COMMUNICATION),
        ],
    )
    hotspots = operator_hotspots(table)
    assert [h.operator for h in hotspots] == ["hcom_allReduce_", "Add", "MatMul"]
    assert hotspots[0].share == pytest.approx(0.6)
    # 不要用精确相等：CPython 3.12 起 sum() 对浮点使用补偿求和，3.10/3.11 会得到
    # 0.9999999999999999；该断言必须跨解释器稳定（AE-01 确定性要求）。
    assert sum(h.share for h in hotspots) == pytest.approx(1.0, abs=1e-12)
    assert hotspots[0].avg_us == 1500.0


def test_hotspot_matrix_marks_absent_operator():
    a = _table(_workload("wl-a"), [("Add", 10, 5000.0, TaskType.KERNEL)])
    b = _table(_workload("wl-b"), [("MatMul", 5, 4000.0, TaskType.KERNEL)])
    matrix = hotspot_matrix({"wl-a": a, "wl-b": b}, top=10)
    rows = {row["operator"]: row for row in matrix["rows"]}
    assert set(rows) == {"Add", "MatMul"}
    assert rows["Add"]["workloads"]["wl-b"] is None
    text = matrix_to_text(matrix)
    assert "operator" in text and "Add" in text


def test_compare_refuses_when_input_length_unknown():
    a = _table(_workload("wl-a", mode="eager", length=1), [("Add", 10, 5000.0, TaskType.KERNEL)])
    b = _table(_workload("wl-b", mode="graph", length=128), [("Add", 10, 5000.0, TaskType.KERNEL)])
    report = compare_tables(a, b)
    assert report.comparable is False
    assert report.reasons
    assert report.deltas == []
    assert "不可比" in report.observations[0]


def test_compare_computes_delta_share():
    a = _table(
        _workload("wl-a", mode="eager"),
        [("Add", 10, 8000.0, TaskType.KERNEL), ("MatMul", 10, 2000.0, TaskType.KERNEL)],
    )
    b = _table(
        _workload("wl-b", mode="graph"),
        [("Add", 10, 2000.0, TaskType.KERNEL), ("MatMul", 10, 8000.0, TaskType.KERNEL)],
    )
    report = compare_tables(a, b)
    assert report.comparable is True
    deltas = {d.operator: d for d in report.deltas}
    assert deltas["Add"].delta_share == pytest.approx(-0.6)
    assert deltas["MatMul"].delta_share == pytest.approx(0.6)
    assert deltas["Add"].calls_ratio == 1.0


def test_compare_warns_about_hccl_under_reporting_in_graph():
    a = _table(
        _workload("wl-a", mode="eager"),
        [("hcom_allReduce_", 1000, 5000.0, TaskType.COMMUNICATION), ("Add", 10, 5000.0, TaskType.KERNEL)],
    )
    b = _table(
        _workload("wl-b", mode="graph"),
        [("hcom_allReduce_", 10, 5.0, TaskType.COMMUNICATION), ("Add", 10, 5000.0, TaskType.KERNEL)],
    )
    report = compare_tables(a, b)
    assert any("HCCL" in warning for warning in report.warnings)
    assert any("执行模式不同" in warning for warning in report.warnings)


def test_compare_warns_when_windows_differ():
    a = _table(_workload("wl-a", mode="eager"), [("Add", 10, 5000.0, TaskType.KERNEL)],
               window={"rounds": [1, 2, 3, 4]})
    b = _table(_workload("wl-b", mode="eager"), [("Add", 10, 5000.0, TaskType.KERNEL)],
               window={"rounds": [2, 3]})
    report = compare_tables(a, b)
    assert report.same_window is False
    assert any("轮次窗口不同" in warning for warning in report.warnings)


def test_compare_warns_about_collection_params():
    a = _table(_workload("wl-a"), [("Add", 1, 1.0, TaskType.KERNEL)])
    b_workload = WorkloadSpec.from_dict(
        {
            "workload_id": "wl-b",
            "model": {"name": "Qwen3.5-35B-A3B", "dtype": "bfloat16"},
            "hardware": {"device": "Ascend 910B", "device_count": 2},
            "execution": {"phase": "decode", "mode": "eager"},
            "input": {"batch_size": 1, "input_length": 1, "max_num_seqs": 32},
        }
    )
    b = _table(b_workload, [("Add", 1, 1.0, TaskType.KERNEL)])
    report = compare_tables(a, b)
    assert any("采集参数差异" in warning for warning in report.warnings)


def test_compare_warns_when_one_side_is_sqlite_fallback():
    from opt_discovery.schemas.operator import ParseMeta

    a = _table(_workload("wl-a"), [("Add", 10, 5000.0, TaskType.KERNEL)])
    b = _table(_workload("wl-b"), [("Add", 10, 5000.0, TaskType.KERNEL)])
    b.meta = ParseMeta(parser="ascend_op_report_sqlite")
    report = compare_tables(a, b)
    assert any("sqlite 回填" in warning for warning in report.warnings)


def test_compare_uses_comm_bytes_instead_of_call_count():
    """字节量守恒时，告警必须指向「记录被合并」而不是「通信变少」。"""
    from opt_discovery.schemas.operator import ParseMeta

    a = _table(_workload("wl-a", mode="eager"), [("Add", 10, 5000.0, TaskType.KERNEL)])
    b = _table(_workload("wl-b", mode="graph"), [("Add", 10, 5000.0, TaskType.KERNEL)])
    a.meta = ParseMeta(parser="t", extra={"comm_volume": {"bytes_total": 1024.0, "unique_tasks": 100}})
    b.meta = ParseMeta(parser="t", extra={"comm_volume": {"bytes_total": 1024.0, "unique_tasks": 10}})
    report = compare_tables(a, b)
    text = " ".join(report.warnings)
    assert "通信字节量基本一致" in text
    assert "记录被合并" in text


def test_compare_flags_comm_bytes_mismatch():
    from opt_discovery.schemas.operator import ParseMeta

    a = _table(_workload("wl-a", mode="eager"), [("Add", 10, 5000.0, TaskType.KERNEL)])
    b = _table(_workload("wl-b", mode="graph"), [("Add", 10, 5000.0, TaskType.KERNEL)])
    a.meta = ParseMeta(parser="t", extra={"comm_volume": {"bytes_total": 1000.0, "unique_tasks": 10}})
    b.meta = ParseMeta(parser="t", extra={"comm_volume": {"bytes_total": 500.0, "unique_tasks": 10}})
    report = compare_tables(a, b)
    assert any("通信字节量差异" in warning for warning in report.warnings)
