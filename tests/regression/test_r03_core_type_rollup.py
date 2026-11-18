"""R03：同一 OP Type 按 Core Type 拆行时必须正确聚合。

历史错误（README 已知约束 3）：MatMulV3 在 AI_CORE / MIX_AIC 各一行，
直接取单行会漏调用数，取「平均值的平均」会得到错误单次耗时。
不变量 INV-01/INV-03：累计时间按行求和，平均值由 total/calls 重算且标记 derived。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _helpers import STATISTIC_MAPPING, make_workload, write_csv
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
from opt_discovery.schemas.metrics import Provenance

HEADER = (
    "Device_id",
    "OP Type",
    "Core Type",
    "Count",
    "Total Time(us)",
    "Min Time(us)",
    "Avg Time(us)",
    "Max Time(us)",
    "Ratio(%)",
)

#: 与 README 记录的历史实测结构一致（两次 Core Type 拆行）
AI_CORE_CALLS, AI_CORE_TOTAL, AI_CORE_AVG = 717, 160911.466, 224.423
MIX_AIC_CALLS, MIX_AIC_TOTAL, MIX_AIC_AVG = 54, 65566.793, 1214.199


@pytest.fixture()
def table(tmp_path: Path):
    path = write_csv(
        tmp_path / "op_statistic_0.csv",
        HEADER,
        [
            (0, "MatMulV3", "AI_CORE", AI_CORE_CALLS, AI_CORE_TOTAL,
             AI_CORE_AVG, AI_CORE_AVG, 99.0),
            (0, "MatMulV3", "MIX_AIC", MIX_AIC_CALLS, MIX_AIC_TOTAL,
             MIX_AIC_AVG, MIX_AIC_AVG, 1.0),
        ],
    )
    parser = MappingCsvParser(CsvMapping.load(STATISTIC_MAPPING))
    # 默认按 operator+core_type 聚合：应保留 2 条变体
    return parser.parse(path, make_workload())


def test_core_type_variants_are_preserved(table) -> None:
    variants = [r for r in table.records if r.operator == "MatMulV3"]
    assert len(variants) == 2
    assert {r.attributes["core_type"] for r in variants} == {"AI_CORE", "MIX_AIC"}


def test_operator_level_rollup_uses_weighted_average(tmp_path: Path) -> None:
    """按算子合并（不按 core_type）时，avg 必须是 total/calls 而不是平均值之平均。"""
    import yaml

    raw = yaml.safe_load(STATISTIC_MAPPING.read_text(encoding="utf-8"))
    raw["aggregate"]["by"] = ["operator"]
    mapping_path = tmp_path / "by_operator.yaml"
    mapping_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    csv_path = write_csv(
        tmp_path / "op_statistic_0.csv",
        HEADER,
        [
            (0, "MatMulV3", "AI_CORE", AI_CORE_CALLS, AI_CORE_TOTAL,
             AI_CORE_AVG, AI_CORE_AVG, 99.0),
            (0, "MatMulV3", "MIX_AIC", MIX_AIC_CALLS, MIX_AIC_TOTAL,
             MIX_AIC_AVG, MIX_AIC_AVG, 1.0),
        ],
    )
    parser = MappingCsvParser(CsvMapping.load(mapping_path))
    merged = parser.parse(csv_path, make_workload())
    record = next(r for r in merged.records if r.operator == "MatMulV3")

    expected_calls = AI_CORE_CALLS + MIX_AIC_CALLS
    expected_total = AI_CORE_TOTAL + MIX_AIC_TOTAL
    average_of_averages = (AI_CORE_AVG + MIX_AIC_AVG) / 2

    assert record.calls == expected_calls == 771
    assert record.total_time_us.value == pytest.approx(expected_total)
    assert record.avg_time_us.value == pytest.approx(expected_total / expected_calls)
    assert record.avg_time_us.provenance is Provenance.DERIVED
    # 负向断言：历史错误算法（平均值的平均）必然不同
    assert record.avg_time_us.value != pytest.approx(average_of_averages, rel=1e-3)
