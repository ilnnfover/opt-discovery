"""R02：``op_statistic`` 不含 HCCL，不能被当作完整通信时间。

历史错误（README 已知约束 2）：只解析 op_statistic 会漏掉约 30% 通信时间，
且容易被误读成「通信占比很低」。不变量 INV-05/INV-11：能力缺失必须显式暴露。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _helpers import STATISTIC_MAPPING, make_workload, write_csv
from opt_discovery.analyzer.hotspot import hotspot_matrix
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
from opt_discovery.schemas.operator import TaskType

STATISTIC_HEADER = (
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


@pytest.fixture()
def statistic_table(tmp_path: Path):
    path = write_csv(
        tmp_path / "op_statistic_0.csv",
        STATISTIC_HEADER,
        [
            (0, "MatMulV3", "AI_CORE", 10, 1000.0, 90.0, 100.0, 110.0, 99.0),
            (0, "Add", "AI_VECTOR_CORE", 5, 10.0, 1.0, 2.0, 3.0, 1.0),
            # 通信算子不会出现在该文件里（这是数据源的真实能力边界）
        ],
    )
    parser = MappingCsvParser(CsvMapping.load(STATISTIC_MAPPING))
    return parser.parse(path, make_workload())


def test_no_communication_is_fabricated(statistic_table) -> None:
    assert [r for r in statistic_table.records if r.operator.startswith("hcom_")] == []
    assert all(r.task_type is not TaskType.COMMUNICATION for r in statistic_table.records)
    # 不能因为缺通信数据就给出 0 秒通信时间：未采集必须是 None
    assert all(r.metric("communication_time_us") is None for r in statistic_table.records)
    assert statistic_table.total_operator_time_us() == pytest.approx(1010.0)


def test_hotspot_matrix_states_the_capability_gap(statistic_table) -> None:
    """输出必须带「该口径不含 HCCL」的机读提示，避免跨口径比较。"""
    matrix = hotspot_matrix({"qwen35-prefill-eager": statistic_table})
    assert "不含 HCCL" in matrix["note"]
    assert "不可直接比较" in matrix["note"]
