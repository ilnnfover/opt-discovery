"""R07：Graph 下算子集合收缩 != 算子未执行/被删除。

历史错误（README 已知约束 7）：71 个算子降到 44 个，被图捕获/融合的算子不再单独上报。
不变量 INV-04/INV-07：不得据此计算「消除收益」，只能作为线索并提示执行模式差异。
"""

from __future__ import annotations

import json

import pytest

from _claims import assert_no_claims, find_claims
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


def test_no_savings_claims_in_any_field_or_text(comparison) -> None:
    """用户可见输出（含嵌套字段、键名与文本）不得出现「消除/节省」型结论。"""
    payload = comparison.to_dict()

    assert find_claims(payload) == []
    # 实际写盘/传给用户的 JSON 形态同样干净
    assert find_claims(json.loads(json.dumps(payload, ensure_ascii=False))) == []
    assert_no_claims(payload)


def test_claims_guard_rejects_injected_conclusions(comparison) -> None:
    """反例：把结论注入到不同位置（文本/嵌套字段/深层键名）都必须被拒绝。"""
    base = json.loads(json.dumps(comparison.to_dict(), ensure_ascii=False))

    text_claim = json.loads(json.dumps(base, ensure_ascii=False))
    text_claim["observations"].append("FusedBias 被融合，预计收益 8%")

    nested_claim = json.loads(json.dumps(base, ensure_ascii=False))
    nested_claim["deltas"][0]["saved_us"] = 120.0

    deep_claim = json.loads(json.dumps(base, ensure_ascii=False))
    deep_claim["window"] = {"note": {"estimated_gain_pct": 8.0}}

    for corrupted in (text_claim, nested_claim, deep_claim):
        assert find_claims(corrupted), corrupted
        with pytest.raises(AssertionError):
            assert_no_claims(corrupted)
