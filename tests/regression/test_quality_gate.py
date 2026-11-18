"""AE-03：质量异常必须传到 compare 消费端并阻断结论（R05/R10 转正式断言）。

这些用例取代第二轮登记的两条 strict xfail：那时 `compare_tables` 不读质量标记，
现在质量标记随 ``ParseMeta.extra["quality_flags"]`` 落盘，比较前先过闸门。
"""

from __future__ import annotations

import json

from _helpers import make_workload
from opt_discovery.analyzer.compare import compare_tables
from opt_discovery.analyzer.rank_quality import assess_rank_tables
from opt_discovery.cli import main
from opt_discovery.experiment.run_manifest import verify_manifest
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta
from opt_discovery.schemas.quality import (
    BLOCKING,
    DEGRADED,
    RANK_DURATION_INCONSISTENT,
    RANK_MERGE_INCONSISTENT,
    RANK_SCOPE_MISMATCH,
    QualityFlag,
    attach,
)
from opt_discovery.schemas.workload import ExecutionMode, GraphMode


def _table(workload, totals: dict[str, float], calls: dict[str, int] | None = None) -> OperatorTable:
    calls = calls or {operator: 10 for operator in totals}
    records = [
        OperatorRecord(
            workload_id=workload.workload_id,
            operator=operator,
            calls=calls[operator],
            total_time_us=Metric(total, "us", Provenance.MEASURED, "fixture"),
            avg_time_us=Metric(total / calls[operator], "us", Provenance.DERIVED, "total / calls"),
        )
        for operator, total in totals.items()
    ]
    return OperatorTable(workload=workload, records=records, meta=ParseMeta(parser="fixture"))


def _pair():
    eager = make_workload(
        "qwen35-prefill-eager", mode=ExecutionMode.EAGER, graph_mode=GraphMode.NONE
    )
    graph = make_workload(
        "qwen35-prefill-graph",
        mode=ExecutionMode.GRAPH,
        graph_mode=GraphMode.FULL_AND_PIECEWISE,
    )
    return eager, graph


def test_rank_inconsistency_blocks_compare_through_the_chain() -> None:
    """R05 链路：两个 rank 4x → 评估 → 挂到表 → compare 拒绝并说明原因。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0, "Add": 500.0})
    peer = _table(eager, {"MatMul": 4000.0, "Add": 2000.0})

    assessment = assess_rank_tables(
        {0: base, 1: peer},
        {0: "op_summary(窗口)", 1: "op_summary(窗口)"},
        {0: "window", 1: "window"},
    )
    assert assessment.consistent is False
    flags = assessment.quality_flags()
    assert [flag.code for flag in flags] == [RANK_MERGE_INCONSISTENT]
    attach(base.meta, flags)

    target = _table(graph, {"MatMul": 1100.0, "Add": 550.0})
    report = compare_tables(base, target)

    assert report.comparable is False
    assert any("rank_merge_inconsistent" in reason for reason in report.reasons)
    assert report.deltas == []
    assert report.observations
    assert "质量闸门" in report.observations[0]
    assert {flag.code for flag in report.quality_flags} == {RANK_MERGE_INCONSISTENT}
    # 机器可读：写盘后仍保留结论（不允许下游把不可比读成可比）
    payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
    assert payload["comparable"] is False
    assert payload["deltas"] == []
    assert payload["quality_flags"][0]["code"] == RANK_MERGE_INCONSISTENT


def test_truncated_duration_flag_blocks_compare() -> None:
    """R10：时长截断标记必须在 compare 前生效，而不是只写在 timeline notes。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    attach(
        base.meta,
        (
            QualityFlag(
                code=RANK_DURATION_INCONSISTENT,
                severity=BLOCKING,
                message="rank 之间任务数接近但累计时长相差数倍",
                source="timeline",
            ),
        ),
    )
    target = _table(graph, {"MatMul": 1100.0})

    report = compare_tables(base, target)

    assert report.comparable is False
    assert any("rank_duration_inconsistent" in reason for reason in report.reasons)
    assert report.deltas == []


def test_degraded_flag_only_warns_and_comparison_still_runs() -> None:
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    target = _table(graph, {"MatMul": 1100.0})
    attach(
        base.meta,
        (
            QualityFlag(
                code=RANK_SCOPE_MISMATCH,
                severity=DEGRADED,
                message="两个 rank 统计口径不同（窗口 vs 全量）",
            ),
        ),
    )

    report = compare_tables(base, target)

    assert report.comparable is True
    assert report.deltas  # 仍产出描述性差异
    assert any(RANK_SCOPE_MISMATCH in warning for warning in report.warnings)


def test_compare_cli_strict_refuses_incomparable(tmp_path, capsys) -> None:
    """严格模式：不可比 -> 退出码 4，并写 run_manifest（comparison_status=incomparable）。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    attach(
        base.meta,
        (QualityFlag(code=RANK_MERGE_INCONSISTENT, severity=BLOCKING, message="rank 不可合并"),),
    )
    target = _table(graph, {"MatMul": 1100.0})
    path_a = base.write_json(tmp_path / "a.json")
    path_b = target.write_json(tmp_path / "b.json")
    out = tmp_path / "cmp"

    strict_code = main(
        ["compare", "--a", str(path_a), "--b", str(path_b), "--out-dir", str(out), "--strict"]
    )
    assert strict_code == 4
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomparable"
    assert manifest["comparison_status"] == "incomparable"
    assert manifest["strict"] is True
    assert RANK_MERGE_INCONSISTENT in manifest["quality_flags"]
    assert verify_manifest(out / "run_manifest.json") == []

    # 普通模式保持兼容：退出 0，但产物仍标记为不可比
    normal_code = main(
        ["compare", "--a", str(path_a), "--b", str(path_b), "--out-dir", str(out)]
    )
    assert normal_code == 0
    payload = json.loads(
        (out / "compare_qwen35-prefill-eager_vs_qwen35-prefill-graph.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["comparable"] is False
    assert payload["deltas"] == []
    capsys.readouterr()
