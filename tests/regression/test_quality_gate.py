"""AE-03：质量异常必须传到 compare 消费端并阻断结论（R05/R10 转正式断言）。

这些用例取代第二轮登记的两条 strict xfail：那时 `compare_tables` 不读质量标记，
现在质量标记随 ``ParseMeta.extra["quality_flags"]`` 落盘，比较前先过闸门。
"""

from __future__ import annotations

import json
from pathlib import Path

from _helpers import make_workload
from opt_discovery.analyzer.compare import compare_tables
from opt_discovery.analyzer.rank_quality import assess_rank_tables
from opt_discovery.cli import main
from opt_discovery.experiment.run_manifest import sha256_of, verify_manifest
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
    assert manifest["inputs"]["a"]["sha256"] == sha256_of(str(path_a))
    assert manifest["inputs"]["b"]["sha256"] == sha256_of(str(path_b))

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


def test_contract_mismatch_blocks_compare() -> None:
    """P1-3：比较入口必须应用采集契约（轮次窗口不同 -> incomparable）。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    target = _table(graph, {"MatMul": 1100.0})
    base.meta.extra["window"] = {"rounds": [1, 2], "duration_us": 3000.0}
    target.meta.extra["window"] = {"rounds": [1, 2, 3], "duration_us": 4000.0}

    report = compare_tables(base, target)

    assert report.comparable is False
    assert any("采集契约不通过" in reason for reason in report.reasons)
    assert report.deltas == []
    assert report.to_dict()["total_ratio"] is None


def test_invalid_quality_flag_is_blocking(tmp_path: Path) -> None:
    """P1-7：标记存在但 severity 非法时必须按未验证处理，不能静默丢弃。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    base.meta.extra["quality_flags"] = [
        {"code": RANK_MERGE_INCONSISTENT, "severity": "bogus", "message": "bad"}
    ]
    target = _table(graph, {"MatMul": 1100.0})

    report = compare_tables(base, target)

    assert report.comparable is False
    assert any("quality_flag_invalid" in reason for reason in report.reasons)
    assert "quality_flag_invalid" in {flag.code for flag in report.quality_flags}
    assert report.deltas == []

    # JSON 往返后同样阻断（独立 compare 读取磁盘文件）
    path_a = base.write_json(tmp_path / "a.json")
    path_b = target.write_json(tmp_path / "b.json")
    code = main(
        [
            "compare",
            "--a",
            str(path_a),
            "--b",
            str(path_b),
            "--out-dir",
            str(tmp_path / "cmp"),
            "--strict",
        ]
    )
    assert code == 4


def test_more_than_two_ranks_are_all_compared() -> None:
    """P1-2：超过两个 rank 时，任一 peer 不一致都要判不可合并。"""
    eager, _graph = _pair()
    tables = {
        0: _table(eager, {"MatMul": 1000.0}),
        1: _table(eager, {"MatMul": 1000.0}),
        2: _table(eager, {"MatMul": 4000.0}),
    }
    assessment = assess_rank_tables(
        tables,
        {rank: "op_summary(窗口)" for rank in tables},
        {rank: "window" for rank in tables},
    )

    assert assessment.consistent is False
    assert any("rank2" in reason for reason in assessment.reasons)
    assert len(assessment.digests) == 3


def test_known_blocking_code_without_severity_is_blocking() -> None:
    """旧表没有 severity 时，已知阻断码必须按阻断处理。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    base.meta.extra["quality_flags"] = [
        {"code": RANK_MERGE_INCONSISTENT, "message": "rank 不可合并"}
    ]
    target = _table(graph, {"MatMul": 1100.0})

    report = compare_tables(base, target)

    assert report.comparable is False
    assert report.deltas == []


def test_known_blocking_code_cannot_be_downgraded_in_json(tmp_path: Path) -> None:
    """F1-1：已知阻断码改成 degraded 也必须阻断（code 决定最低严重性）。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    target = _table(graph, {"MatMul": 1100.0})
    path_a = base.write_json(tmp_path / "a.json")
    path_b = target.write_json(tmp_path / "b.json")

    payload = json.loads(path_a.read_text(encoding="utf-8"))
    payload["meta"]["extra"]["quality_flags"] = [
        {"code": RANK_MERGE_INCONSISTENT, "severity": "degraded", "message": "downgraded"}
    ]
    path_a.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "cmp"
    code = main(
        ["compare", "--a", str(path_a), "--b", str(path_b), "--out-dir", str(out), "--strict"]
    )
    assert code == 4

    report = json.loads(
        (out / "compare_qwen35-prefill-eager_vs_qwen35-prefill-graph.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["comparable"] is False
    assert report["deltas"] == []
    assert report["total_ratio"] is None
    assert "quality_flag_invalid" in {flag["code"] for flag in report["quality_flags"]}


def test_empty_table_is_not_comparable() -> None:
    """A3：任一侧记录为空都不得产出差异。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    empty = OperatorTable(
        workload=graph,
        records=[],
        meta=ParseMeta(parser="fixture"),
    )

    report = compare_tables(base, empty)

    assert report.comparable is False
    assert any("empty_table" in reason for reason in report.reasons)
    assert report.deltas == []
    assert report.to_dict()["total_ratio"] is None


def test_quality_flags_empty_mapping_is_invalid() -> None:
    """A4：`quality_flags={}` 是格式错误，不能当作“没有标记”。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0})
    base.meta.extra["quality_flags"] = {}
    target = _table(graph, {"MatMul": 1100.0})

    report = compare_tables(base, target)

    assert report.comparable is False
    assert any("quality_flag_invalid" in reason for reason in report.reasons)


def test_synthetic_and_measured_mix_is_blocked() -> None:
    """A4：合成数据与实测数据混用不得输出定量结论（双向）。"""
    eager, graph = _pair()
    synthetic = OperatorTable(
        workload=eager,
        records=_table(eager, {"MatMul": 1000.0}).records,
        meta=ParseMeta(parser="synthetic", is_synthetic=True),
    )
    measured = _table(graph, {"MatMul": 1100.0})

    for left, right in ((synthetic, measured), (measured, synthetic)):
        report = compare_tables(left, right)
        assert report.comparable is False
        assert any("synthetic_mismatch" in reason for reason in report.reasons)
        assert report.deltas == []


def test_unverified_workload_is_not_comparable() -> None:
    """A4：verified=false 与 README 契约一致，判不可比。"""
    eager = make_workload(
        "qwen35-prefill-eager",
        mode=ExecutionMode.EAGER,
        graph_mode=GraphMode.NONE,
        verified=False,
    )
    graph = make_workload(
        "qwen35-prefill-graph",
        mode=ExecutionMode.GRAPH,
        graph_mode=GraphMode.FULL_AND_PIECEWISE,
    )
    report = compare_tables(_table(eager, {"MatMul": 1000.0}), _table(graph, {"MatMul": 1100.0}))

    assert report.comparable is False
    assert any("verified_false" in reason for reason in report.reasons)
    assert report.deltas == []


def test_mixed_scope_does_not_downgrade_same_scope_pair() -> None:
    """A5：第三个 full-scope rank 不能把两个 window rank 的强校验降级。"""
    eager, _graph = _pair()
    base = _table(eager, {"MatMul": 1000.0}, calls={"MatMul": 10})
    weak_peer = _table(eager, {"MatMul": 4000.0}, calls={"MatMul": 40})
    full_peer = _table(eager, {"MatMul": 4000.0}, calls={"MatMul": 40})

    assessment = assess_rank_tables(
        {0: base, 1: weak_peer, 2: full_peer},
        {0: "op_summary(窗口)", 1: "op_summary(窗口)", 2: "sqlite(全量)"},
        {0: "window", 1: "window", 2: "full"},
    )

    assert assessment.consistent is False
    assert any(
        "rank0 vs rank1" in reason and "calls_ratio" in reason
        for reason in assessment.reasons
    )


def test_compare_contract_sees_operator_counts() -> None:
    """B5：compare 应把算子集合/调用量摘要传给契约，警告不能因 0 条 records 丢失。"""
    eager, graph = _pair()
    base = _table(eager, {"MatMul": 1000.0}, calls={"MatMul": 10})
    target = _table(graph, {"Add": 1100.0}, calls={"Add": 100})

    report = compare_tables(base, target)
    text = " ".join(report.warnings)

    assert "operator_set" in text
    assert "work.count" in text
