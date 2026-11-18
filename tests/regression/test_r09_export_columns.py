"""R09：33 列精简导出与 46 列导出的差异必须可解析且可审计。

历史错误（README 已知约束 9）：统一补采起只有 Block Num / aic_cube_fops /
aiv_vector_fops，没有 aic_mte*/cube_utilization 分列；FLOPs 单位是 FLOPs。
不变量 INV-01/INV-11：可选缺列不阻塞，但必须留下缺失标记；未知列进入审计。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _helpers import SUMMARY_MAPPING, make_workload, write_op_summary
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser

BASE = (
    "Device_id",
    "Model ID",
    "Task Type",
    "OP Type",
    "Task Start Time(us)",
    "Task Duration(us)",
    "Task Wait Time(us)",
    "Input Shapes",
    "Input Data Types",
    "Output Shapes",
    "Output Data Types",
)

HEADER_33 = BASE + (
    "Block Num",
    "aicore_time(us)",
    "aiv_time(us)",
    "aic_cube_fops",
    "aiv_vector_fops",
    "Mystery Col",
)

HEADER_46 = BASE + (
    "Block Num",
    "aicore_time(us)",
    "aiv_time(us)",
    "aic_mac_time(us)",
    "aic_mte2_time(us)",
    "aiv_mte2_time(us)",
    "cube_utilization(%)",
    "aic_cube_fops",
    "aiv_vector_fops",
)


def _row(header: tuple[str, ...], values: dict[str, object]) -> list[object]:
    return [values.get(name, "") for name in header]


def _parse(tmp_path: Path, header: tuple[str, ...], rows: list[list[object]]):
    path = write_op_summary(tmp_path / "op_summary_0.csv", rows, header=header)
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    return parser.parse(path, make_workload())


def test_compact_export_parses_and_marks_missing_optional_columns(tmp_path: Path) -> None:
    rows = [
        _row(HEADER_33, {
            "Task Type": "AI_CORE", "OP Type": "MatMul", "Task Start Time(us)": 0,
            "Task Duration(us)": 100.0, "Block Num": 24, "aicore_time(us)": 90.0,
            "aic_cube_fops": 8192 ** 3, "Mystery Col": "x",
        }),
        _row(HEADER_33, {
            "Task Type": "AI_CORE", "OP Type": "MatMul", "Task Start Time(us)": 200,
            "Task Duration(us)": 100.0, "Block Num": 12, "aicore_time(us)": 80.0,
            "aic_cube_fops": 8192 ** 3,
        }),
    ]
    table = _parse(tmp_path, HEADER_33, rows)
    record = next(r for r in table.records if r.operator == "MatMul")

    # 可解析、不丢行
    assert record.calls == 2
    assert record.total_time_us.value == pytest.approx(200.0)
    # 缺失的可选指标必须被机读记录，而不是静默消失或填 0
    missing = set(table.meta.extra["missing_extra_metrics"])
    assert {"mac_time_us", "mte2_time_us", "cube_utilization"} <= missing
    assert not {"block_num", "flops", "aicore_time_us"} & missing
    assert any(w.code == "missing_extra_metric" for w in table.meta.warnings)
    assert all(r.metric("mac_time_us") is None for r in table.records)
    # 未知列进入映射审计
    assert "Mystery Col" in table.meta.unmapped_columns
    assert any(w.code == "unmapped_column" for w in table.meta.warnings)


def test_flops_and_block_num_units(tmp_path: Path) -> None:
    rows = [
        _row(HEADER_33, {
            "Task Type": "AI_CORE", "OP Type": "MatMul", "Task Start Time(us)": 0,
            "Task Duration(us)": 100.0, "Block Num": 24, "aicore_time(us)": 90.0,
            "aic_cube_fops": 8192 ** 3,
        }),
        _row(HEADER_33, {
            "Task Type": "AI_CORE", "OP Type": "MatMul", "Task Start Time(us)": 200,
            "Task Duration(us)": 100.0, "Block Num": 12, "aicore_time(us)": 80.0,
            "aic_cube_fops": 8192 ** 3,
        }),
    ]
    record = _parse(tmp_path, HEADER_33, rows).records[0]

    # FLOPs 是计数，不是 µs/GFLOP：不得二次缩放
    assert record.extra["flops"].unit == "flop"
    assert record.extra["flops"].value == pytest.approx(2 * 8192 ** 3)
    assert record.extra["block_num"].value == pytest.approx(18.0)  # mean(24, 12)


def test_wide_export_keeps_pipe_split_metrics(tmp_path: Path) -> None:
    rows = [
        _row(HEADER_46, {
            "Task Type": "AI_CORE", "OP Type": "FusedInferAttentionScore",
            "Task Start Time(us)": 0, "Task Duration(us)": 100.0, "Block Num": 24,
            "aicore_time(us)": 90.0, "aic_mac_time(us)": 70.0,
            "aic_mte2_time(us)": 6.0, "aiv_mte2_time(us)": 4.0,
            "cube_utilization(%)": 55.0,
        }),
    ]
    table = _parse(tmp_path, HEADER_46, rows)
    record = table.records[0]

    assert record.extra["mac_time_us"].value == pytest.approx(70.0)
    # aic_ 与 aiv_ 两条同名流水必须相加
    assert record.extra["mte2_time_us"].value == pytest.approx(10.0)
    assert record.extra["cube_utilization"].unit == "%"
    assert record.extra["cube_utilization"].value == pytest.approx(55.0)
    assert not {"mac_time_us", "mte2_time_us", "cube_utilization"} & set(
        table.meta.extra["missing_extra_metrics"]
    )
