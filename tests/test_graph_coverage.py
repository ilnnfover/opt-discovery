"""P1-2：Graph 覆盖率。"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from opt_discovery.analyzer.graph_coverage import summarize_coverage
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
from opt_discovery.schemas.metrics import Metric
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from opt_discovery.schemas.workload import WorkloadSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_summary.yaml"


def _workload(mode: str = "graph") -> WorkloadSpec:
    return WorkloadSpec.from_dict(
        {
            "workload_id": f"wl-{mode}",
            "model": {"name": "m"},
            "hardware": {"device": "d"},
            "execution": {"phase": "decode", "mode": mode},
        }
    )


def _record(
    operator: str, calls: int, in_graph: int, total_us: float, in_graph_us: float,
    workload_id: str = "wl-graph",
):
    return OperatorRecord(
        workload_id=workload_id,
        operator=operator,
        calls=calls,
        total_time_us=Metric(total_us, "us"),
        task_type=TaskType.KERNEL,
        extra={
            "in_graph_tasks": Metric(float(in_graph), "count"),
            "in_graph_time_us": Metric(in_graph_us, "us"),
        },
    )


def test_coverage_reports_task_and_time_ratio():
    table = OperatorTable(
        workload=_workload(),
        records=[
            _record("MatMulV2", 100, 99, 900.0, 800.0),
            _record("AllReduce", 10, 0, 400.0, 0.0),
        ],
        meta=ParseMeta(parser="t"),
    )
    coverage = summarize_coverage(table)
    assert coverage.in_graph_calls == 99
    assert coverage.task_ratio == pytest.approx(99 / 110)
    assert coverage.time_ratio == pytest.approx(800 / 1300)
    assert coverage.top_fallback[0].operator == "AllReduce"
    assert any("按时间加权" in note for note in coverage.notes)


def test_eager_mode_is_reported_as_zero_not_missing():
    table = OperatorTable(
        workload=_workload("eager"),
        records=[_record("MatMulV2", 10, 0, 100.0, 0.0, workload_id="wl-eager")],
        meta=ParseMeta(parser="t"),
    )
    coverage = summarize_coverage(table)
    assert coverage.available is False
    assert coverage.task_ratio == 0.0
    assert any("eager" in note for note in coverage.notes)


def test_table_without_flag_is_flagged():
    table = OperatorTable(
        workload=_workload(),
        records=[
            OperatorRecord(
                workload_id="wl-graph",
                operator="MatMulV2",
                calls=10,
                total_time_us=Metric(100.0, "us"),
            )
        ],
        meta=ParseMeta(parser="t"),
    )
    coverage = summarize_coverage(table)
    assert coverage.total_calls == 0
    assert any("没有 in_graph 标记" in note for note in coverage.notes)


def test_parser_collects_in_graph_flag(tmp_path):
    header = "OP Type,Task Type,Model ID,Task Start Time(us),Task Duration(us)\n"
    rows = [
        "MatMulV2,AI_CORE,125,1000,100",
        "MatMulV2,AI_CORE,125,1200,100",
        "MatMulV2,AI_CORE,4294967295,1400,300",  # 图外
    ]
    path = tmp_path / "op_summary.csv"
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")

    parser = MappingCsvParser(CsvMapping.load(MAPPING))
    table = parser.parse(path, _workload())
    record = table.records[0]
    assert record.calls == 3
    assert record.extra["in_graph_tasks"].value == pytest.approx(2)
    assert record.extra["in_graph_time_us"].value == pytest.approx(200.0)
    assert record.attributes["in_graph_ratio"] == "0.6667"

    coverage = summarize_coverage(table)
    assert coverage.task_ratio == pytest.approx(2 / 3)
    assert coverage.time_ratio == pytest.approx(200 / 500)
