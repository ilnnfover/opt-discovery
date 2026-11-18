"""Parser tests driven by small inline mappings.

The real Ascend headers are exercised in ``test_real_data.py``; here we pin the
*behavior* (rollup semantics, N/A handling, aggregation functions) so it stays
stable without depending on a machine-specific export.
"""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO_ROOT

from opt_discovery.diagnosis.facts import build_facts
from opt_discovery.parsers.base import ParserError
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
from opt_discovery.schemas.metrics import Provenance
from opt_discovery.schemas.operator import TaskType

STATISTIC_MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_statistic.yaml"
SUMMARY_MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_summary.yaml"


def _mapping(tmp_path, payload: dict, name: str = "m.yaml") -> CsvMapping:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return CsvMapping.load(path)


def _csv(tmp_path, header: str, rows: list[str], name: str = "data.csv"):
    path = tmp_path / name
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _aggregate_payload(by: list[str]) -> dict:
    return {
        "parser": "test_statistic",
        "row_semantics": "aggregate",
        "aggregate": {"by": by},
        "labels": {"core_type": {"names": ["Core Type"]}},
        "columns": {
            "operator": {"names": ["OP Type"], "required": True},
            "calls": {"names": ["Count"], "required": True},
            "total_time_us": {"names": ["Total Time(us)"], "unit": "us"},
            "min_time_us": {"names": ["Min Time(us)"], "unit": "us"},
            "max_time_us": {"names": ["Max Time(us)"], "unit": "us"},
            "avg_time_us": {"names": ["Avg Time(us)"], "unit": "us"},
            "task_type": {"names": ["Core Type"]},
        },
        "task_type_map": {
            "AI_CORE": "kernel",
            "MIX_AIC": "kernel",
            "COMMUNICATION": "communication",
        },
    }


def _instance_payload() -> dict:
    return {
        "parser": "test_summary",
        "row_semantics": "instance",
        "aggregate": {"by": ["operator", "task_type_raw"]},
        "labels": {"task_type_raw": {"names": ["Task Type"]}},
        "columns": {
            "operator": {"names": ["OP Type"], "required": True},
            "task_type": {"names": ["Task Type"]},
            "total_time_us": {"names": ["Task Duration(us)"], "unit": "us", "required": True},
            "shape": {"names": ["Input Shapes"]},
            "dtype": {"names": ["Input Data Types"]},
        },
        "extra_metrics": [
            {"name": "task_wait_time_us", "columns": ["Task Wait Time(us)"], "unit": "us", "agg": "sum"},
            {
                "name": "mte2_time_us",
                "columns": ["aic_mte2_time(us)", "aiv_mte2_time(us)"],
                "unit": "us",
                "agg": "sum",
                "combine": "sum",
            },
            {"name": "cube_utilization", "columns": ["cube_utilization(%)"], "unit": "%", "agg": "mean"},
            {
                "name": "communication_time_us",
                "columns": ["Task Duration(us)"],
                "unit": "us",
                "agg": "sum",
                # 与官方 mapping 一致：COMMUNICATION 行的 Task Duration 是 ns
                "scale": 0.001,
                "when": {"task_type": ["communication"]},
            },
        ],
        "task_type_map": {"AI_VECTOR_CORE": "kernel", "COMMUNICATION": "communication"},
    }


def test_shipped_mappings_are_verified():
    statistic = CsvMapping.load(STATISTIC_MAPPING)
    summary = CsvMapping.load(SUMMARY_MAPPING)
    assert statistic.needs_verification is False
    assert statistic.row_semantics == "aggregate"
    assert statistic.aggregate.by == ("operator", "core_type")
    assert summary.needs_verification is False
    assert summary.row_semantics == "instance"
    assert "calls" not in summary.fields  # calls 由行数得到
    assert summary.missing_required_fields == ("operator", "total_time_us")
    assert {m.name for m in summary.extra_metrics} >= {
        "mte2_time_us",
        "cube_utilization",
        "communication_time_us",
    }


def test_duplicate_operator_rows_are_rolled_up(tmp_path, workload):
    """同一 OP Type 按 Core Type 拆成多行时，必须按算子合并，不能重复计数。"""
    header = "OP Type,Core Type,Count,Total Time(us),Min Time(us),Max Time(us),Avg Time(us)"
    path = _csv(
        tmp_path,
        header,
        [
            "MatMulV3,AI_CORE,717,160911.466,79.081,1459.210,224.423",
            "MatMulV3,MIX_AIC,54,65566.793,407.248,2081.422,1214.199",
        ],
    )
    table = MappingCsvParser(_mapping(tmp_path, _aggregate_payload(["operator"]))).parse(
        path, workload
    )
    assert len(table.records) == 1
    record = table.records[0]
    assert record.calls == 771
    assert record.total_time_us is not None
    assert record.total_time_us.value == pytest.approx(226478.259)
    assert record.min_time_us is not None and record.min_time_us.value == pytest.approx(79.081)
    assert record.max_time_us is not None and record.max_time_us.value == pytest.approx(2081.422)
    # 平均时间必须由 total/calls 重新推导，不能平均两个 avg
    assert record.avg_time_us is not None
    assert record.avg_time_us.value == pytest.approx(226478.259 / 771)
    assert record.avg_time_us.provenance is Provenance.DERIVED
    assert record.total_time_us.provenance is Provenance.MEASURED


def test_grouping_by_core_type_keeps_variants(tmp_path, workload):
    header = "OP Type,Core Type,Count,Total Time(us),Min Time(us),Max Time(us)"
    path = _csv(
        tmp_path,
        header,
        [
            "MatMulV3,AI_CORE,717,160911.466,79.081,1459.210",
            "MatMulV3,MIX_AIC,54,65566.793,407.248,2081.422",
        ],
    )
    table = MappingCsvParser(
        _mapping(tmp_path, _aggregate_payload(["operator", "core_type"]))
    ).parse(path, workload)
    assert len(table.records) == 2
    assert [r.operator for r in table.records] == ["MatMulV3", "MatMulV3"]
    assert {r.attributes["core_type"] for r in table.records} == {"AI_CORE", "MIX_AIC"}
    assert sorted(r.calls for r in table.records) == [54, 717]


def test_mixed_task_types_are_reported(tmp_path, workload):
    header = "OP Type,Core Type,Count,Total Time(us)"
    path = _csv(
        tmp_path,
        header,
        [
            "OpA,AI_CORE,10,100.0",
            "OpA,COMMUNICATION,2,900.0",
        ],
    )
    record = MappingCsvParser(_mapping(tmp_path, _aggregate_payload(["operator"]))).parse(
        path, workload
    ).records[0]
    assert record.task_type is TaskType.KERNEL  # 主导类型
    assert "communication" in record.attributes["task_types"]


def test_instance_rows_are_counted_and_summed(tmp_path, workload):
    header = (
        "OP Type,Task Type,Task Duration(us),Task Wait Time(us),"
        "aic_mte2_time(us),aiv_mte2_time(us),Input Shapes,Input Data Types,cube_utilization(%)"
    )
    path = _csv(
        tmp_path,
        header,
        [
            'Add,AI_VECTOR_CORE,10.0,2.0,1.0,0.5,"256,4",FLOAT16,0.0',
            'Add,AI_VECTOR_CORE,20.0,4.0,1.0,0.5,"256,4",FLOAT16,50.0',
            "hcom_allReduce_,COMMUNICATION,600.0,8.0,0,0,N/A,N/A,N/A",
        ],
    )
    table = MappingCsvParser(_mapping(tmp_path, _instance_payload())).parse(path, workload)
    by_op = {record.operator: record for record in table.records}
    assert set(by_op) == {"Add", "hcom_allReduce_"}

    add = by_op["Add"]
    assert add.calls == 2
    assert add.total_time_us.value == pytest.approx(30.0)
    assert add.avg_time_us.value == pytest.approx(15.0)
    assert add.min_time_us.value == pytest.approx(10.0)
    assert add.max_time_us.value == pytest.approx(20.0)
    assert add.shape == (256, 4)
    assert add.dtype == "FLOAT16"
    assert add.extra["task_wait_time_us"].value == pytest.approx(6.0)
    # combine: sum 把 aic_ 与 aiv_ 两列相加
    assert add.extra["mte2_time_us"].value == pytest.approx(3.0)
    # mean 聚合
    assert add.extra["cube_utilization"].value == pytest.approx(25.0)
    # when 过滤：非通信任务不应出现通信时间
    assert "communication_time_us" not in add.extra

    comm = by_op["hcom_allReduce_"]
    assert comm.task_type is TaskType.COMMUNICATION
    # 源列 600 在 COMMUNICATION 行是 ns -> 0.6 µs（与 total_time_us 同一换算，只做一次）
    assert comm.extra["communication_time_us"].value == pytest.approx(0.6)
    assert comm.total_time_us.value == pytest.approx(0.6)
    assert comm.shape is None  # N/A 视为未采集


def test_instance_facts_feed_rule_engine(tmp_path, workload):
    header = "OP Type,Task Type,Task Duration(us),aic_mac_time(us),aic_mte2_time(us)"
    path = _csv(
        tmp_path,
        header,
        [
            "MatMul,AI_CORE,100.0,90.0,5.0",
            "MatMul,AI_CORE,100.0,95.0,5.0",
        ],
    )
    payload = _instance_payload()
    payload["extra_metrics"] = [
        {"name": "mac_time_us", "columns": ["aic_mac_time(us)"], "unit": "us", "agg": "sum"},
        {"name": "mte2_time_us", "columns": ["aic_mte2_time(us)"], "unit": "us", "agg": "sum"},
    ]
    table = MappingCsvParser(_mapping(tmp_path, payload)).parse(path, workload)
    facts = build_facts(table.records[0], workload_total_time_us=table.total_operator_time_us())
    assert facts.value("mac_time_fraction") == pytest.approx(0.925)
    assert facts.value("mte2_time_fraction") == pytest.approx(0.05)
    assert facts.value("time_share") == pytest.approx(1.0)


def test_null_tokens_and_thousands_separators(tmp_path, workload):
    header = "OP Type,Core Type,Count,Total Time(us),Min Time(us),Max Time(us)"
    path = _csv(
        tmp_path,
        header,
        ['OpA,AI_CORE,2,"1,234.5",N/A,--'],
    )
    record = MappingCsvParser(_mapping(tmp_path, _aggregate_payload(["operator"]))).parse(
        path, workload
    ).records[0]
    assert record.total_time_us.value == pytest.approx(1234.5)
    assert record.min_time_us is None
    assert record.max_time_us is None


def test_missing_required_column_raises(tmp_path, workload):
    path = _csv(tmp_path, "OP Type,Total Time(us)\n", ["MatMul,100.0"])
    parser = MappingCsvParser(_mapping(tmp_path, _aggregate_payload(["operator"])))
    assert parser.can_parse(path) is False
    with pytest.raises(ParserError) as exc:
        parser.parse(path, workload)
    assert "必需列缺失" in str(exc.value)


def test_time_columns_are_mandatory(tmp_path, workload):
    path = _csv(tmp_path, "OP Type,Count\n", ["MatMul,64"])
    parser = MappingCsvParser(_mapping(tmp_path, _aggregate_payload(["operator"])))
    assert parser.can_parse(path) is False
    with pytest.raises(ParserError) as exc:
        parser.parse(path, workload)
    assert "无法得到累计时间" in str(exc.value)


def test_unmapped_columns_are_reported(tmp_path, workload):
    header = "OP Type,Core Type,Count,Total Time(us),Ratio(%),Mystery Column"
    path = _csv(tmp_path, header, ["OpA,AI_CORE,3,300.0,12.5,x"])
    table = MappingCsvParser(_mapping(tmp_path, _aggregate_payload(["operator"]))).parse(
        path, workload
    )
    assert "Ratio(%)" in table.meta.unmapped_columns
    assert "Mystery Column" in table.meta.unmapped_columns
    assert any(w.code == "unmapped_column" for w in table.meta.warnings)


def test_warning_flood_is_capped(tmp_path, workload):
    header = "OP Type,Core Type,Count,Total Time(us)"
    rows = [",AI_CORE,1,1.0"] * 120  # operator 为空，逐行告警
    path = _csv(tmp_path, header, rows)
    table = MappingCsvParser(_mapping(tmp_path, _aggregate_payload(["operator"]))).parse(
        path, workload
    )
    assert table.meta.skipped_rows == 120
    assert sum(1 for w in table.meta.warnings if w.code == "row_skipped") <= 50


def test_invalid_mapping_is_rejected(tmp_path):
    payload = _aggregate_payload(["operator", "not_declared"])
    with pytest.raises(ParserError):
        _mapping(tmp_path, payload, "bad.yaml")


def test_instance_mapping_rejects_calls_column(tmp_path):
    payload = _instance_payload()
    payload["columns"]["calls"] = {"names": ["Count"], "required": True}
    with pytest.raises(ParserError):
        _mapping(tmp_path, payload, "bad2.yaml")
