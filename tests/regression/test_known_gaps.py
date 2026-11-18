"""已登记的下游质量闸门缺口（AE-03），用 strict xfail 锁定。

这些用例描述「应该发生」的行为：
* R05：rank 不可合并时，跨模式对比必须拒绝（`compare_tables` 目前不读该结论）；
* R10：rank 累计时长被截断时，对比必须受限（`compare_tables` 目前不读该标记）。

当前行为未实现 -> xfail。一旦 AE-03 在 compare 消费质量标记，用例会 XPASS，
`strict=True` 使其变为失败，提醒把 xfail 改成正式断言并同步矩阵状态。
"""

from __future__ import annotations

import pytest

from _helpers import make_workload
from opt_discovery.analyzer.compare import compare_tables
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta
from opt_discovery.schemas.workload import ExecutionMode, GraphMode


def _table(workload, total_us: float = 1000.0) -> OperatorTable:
    record = OperatorRecord(
        workload_id=workload.workload_id,
        operator="MatMul",
        calls=10,
        total_time_us=Metric(total_us, "us", Provenance.MEASURED, "fixture"),
        avg_time_us=Metric(total_us / 10, "us", Provenance.DERIVED, "total / calls"),
    )
    return OperatorTable(workload=workload, records=[record], meta=ParseMeta(parser="fixture"))


def _pair():
    eager = make_workload(
        "qwen35-prefill-eager", mode=ExecutionMode.EAGER, graph_mode=GraphMode.NONE
    )
    graph = make_workload(
        "qwen35-prefill-graph",
        mode=ExecutionMode.GRAPH,
        graph_mode=GraphMode.FULL_AND_PIECEWISE,
    )
    return _table(eager), _table(graph, 1100.0)


@pytest.mark.xfail(strict=True, reason="R05：compare 尚未消费 rank 合并结论（AE-03）")
def test_compare_rejects_unmergeable_rank_quality_flag() -> None:
    a, b = _pair()
    a.meta.extra["rank_mergeable"] = False
    a.meta.extra["rank_merge_reasons"] = ["calls_ratio=4.000 total_ratio=4.000"]

    report = compare_tables(a, b)

    assert report.comparable is False
    assert any("rank" in reason.lower() for reason in report.reasons)


@pytest.mark.xfail(strict=True, reason="R10：compare 尚未消费 rank_duration_inconsistent（AE-03/AE-07）")
def test_compare_restricts_truncated_duration_flag() -> None:
    a, b = _pair()
    a.meta.extra["rank_duration_inconsistent"] = True

    report = compare_tables(a, b)

    assert report.comparable is False
    assert any("rank" in reason.lower() or "duration" in reason.lower() for reason in report.reasons)
