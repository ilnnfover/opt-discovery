"""R08：COMMUNICATION 行的 Task Duration 是 ns，只能缩放一次。

历史错误（README 已知约束 8）：``hcom_allReduce`` 单行 110,462,688 实际是 ns
（≈110 ms），按 µs 解释会得到 110 秒。解析层与 timeline 必须共用同一次换算。
不变量 INV-03：单位转换只在标准化边界发生，不得重复缩放。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _helpers import SUMMARY_MAPPING, make_workload, write_op_summary
from opt_discovery.analyzer.timeline import load_op_intervals
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser

HEADER = ("OP Type", "Task Type", "Task Start Time(us)", "Task Duration(us)")

#: 实测原始值：单位 ns
RAW_NS = 110_462_688
EXPECTED_US = 110_462.688


def _fixture(tmp_path: Path) -> Path:
    return write_op_summary(
        tmp_path / "op_summary_0.csv",
        [
            ("hcom_allReduce", "COMMUNICATION", 1_000.0, RAW_NS),
            ("MatMul", "AI_CORE", 2_000.0, RAW_NS),
        ],
        header=HEADER,
    )


def test_csv_parser_scales_communication_exactly_once(tmp_path: Path) -> None:
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    table = parser.parse(_fixture(tmp_path), make_workload())
    by_op = {record.operator: record for record in table.records}

    communication = by_op["hcom_allReduce"]
    kernel = by_op["MatMul"]

    assert communication.total_time_us.value == pytest.approx(EXPECTED_US)
    # 只缩放一次：既不是原始 ns 数值，也不是缩放两次
    assert communication.total_time_us.value != pytest.approx(RAW_NS)
    assert communication.total_time_us.value != pytest.approx(EXPECTED_US / 1000)
    # 非通信行遵循各自契约，不缩放
    assert kernel.total_time_us.value == pytest.approx(RAW_NS)


def test_timeline_uses_the_same_single_scale(tmp_path: Path) -> None:
    intervals = load_op_intervals(_fixture(tmp_path))
    durations = sorted(interval.end_us - interval.start_us for interval in intervals)

    assert durations == pytest.approx([EXPECTED_US, RAW_NS])


def test_communication_time_metric_is_only_on_communication_rows(tmp_path: Path) -> None:
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    table = parser.parse(_fixture(tmp_path), make_workload())
    by_op = {record.operator: record for record in table.records}

    assert by_op["hcom_allReduce"].metric("communication_time_us").value == pytest.approx(
        EXPECTED_US
    )
    # kernel 行没有通信时间，未采集 != 0
    assert by_op["MatMul"].metric("communication_time_us") is None


def test_missing_declared_scale_would_reintroduce_the_error(tmp_path: Path) -> None:
    """负向：映射漏声明 ``scale`` 时，该指标会退回 ns 数值（1000x）。

    单位修正必须由源契约显式声明，测试锁定「配置是承重的」，漏写会被发现。
    """
    import yaml

    raw = yaml.safe_load(SUMMARY_MAPPING.read_text(encoding="utf-8"))
    for item in raw["extra_metrics"]:
        if item["name"] == "communication_time_us":
            item.pop("scale", None)
    mapping_path = tmp_path / "op_summary_no_scale.yaml"
    mapping_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    parser = MappingCsvParser(CsvMapping.load(mapping_path))
    table = parser.parse(_fixture(tmp_path), make_workload())
    communication = next(
        record for record in table.records if record.operator == "hcom_allReduce"
    )

    assert communication.extra["communication_time_us"].value == pytest.approx(RAW_NS)
