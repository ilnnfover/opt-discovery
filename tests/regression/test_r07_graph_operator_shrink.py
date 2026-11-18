"""R07：Graph 下算子集合收缩 != 算子未执行/被删除。

历史错误（README 已知约束 7）：71 个算子降到 44 个，被图捕获/融合的算子不再单独上报。
不变量 INV-04/INV-07：不得据此计算「消除收益」，只能作为线索并提示执行模式差异。
"""

from __future__ import annotations

import pytest

from _helpers import make_workload
from opt_discovery.analyzer.compare import compare_tables
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta
from opt_discovery.schemas.workload import ExecutionMode, GraphMode


def _table(workload, specs: dict[str, int]) -> OperatorTable:
    records = [
        OperatorRecord(
            workload_id=workload.workload_id,
            operator=operator,
            calls=10,
            total_time_us=Metric(float(total_us), "us", Provenance.MEASURED, "fixture"),
            avg_time_us=Metric(
                float(total_us) / 10, "us", Provenance.DERIVED, "fixture:total/calls"
            ),
        )
        for operator, total_us in specs.items()
    ]
    return OperatorTable(workload=workload, records=records, meta=ParseMeta(parser="fixture"))


@pytest.fixture()
def comparison():
    eager = make_workload(
        "qwen35-prefill-eager", mode=ExecutionMode.EAGER, graph_mode=GraphMode.NONE
    )
    graph = make_workload(
        "qwen35-prefill-graph",
        mode=ExecutionMode.GRAPH,
        graph_mode=GraphMode.FULL_AND_PIECEWISE,
    )
    # FusedBias 在 Graph 下被捕获，不再单独上报；它并不等于「没执行」
    return compare_tables(
        _table(eager, {"MatMul": 1000.0, "Add": 100.0, "FusedBias": 500.0}),
        _table(graph, {"MatMul": 1100.0, "Add": 120.0}),
    )


def test_shrunken_operator_is_presence_only(comparison) -> None:
    assert comparison.comparable is True
    delta = next(item for item in comparison.deltas if item.operator == "FusedBias")
    assert delta.presence == "only_a"
    assert delta.share_b is None  # 缺失是 None，不是 0
    assert any("仅在 qwen35-prefill-eager 出现" in note for note in comparison.observations)


def test_warning_states_capture_fusion_not_deletion(comparison) -> None:
    warnings = " ".join(comparison.warnings)
    assert "执行模式不同" in warnings
    assert "捕获/融合" in warnings
    assert "线索" in warnings


def test_no_eliminated_savings_are_claimed(comparison) -> None:
    """负向：输出里不允许出现「消除/节省」型收益字段或结论。"""
    payload = comparison.to_dict()
    keys = " ".join(payload).lower()
    for forbidden in ("saving", "eliminat", "eliminated_us", "estimated_gain", "预计收益"):
        assert forbidden not in keys
    for delta in payload["deltas"]:
        assert not {"saved_us", "eliminated_us", "estimated_gain"} & set(delta)
