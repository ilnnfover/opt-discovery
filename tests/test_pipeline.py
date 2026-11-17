from __future__ import annotations

import json
import shutil

import pytest
from conftest import REPO_ROOT

from opt_discovery.cli import main
from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.discovery.generator import opportunities_from_diagnosis
from opt_discovery.parsers.synthetic import build_synthetic_table
from opt_discovery.schemas.evidence import ConfidenceLevel
from opt_discovery.schemas.workload import WorkloadSpec

RULES = REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"


@pytest.fixture(scope="module")
def ruleset() -> RuleSet:
    return RuleSet.load(RULES)


def test_config_workloads_load():
    specs = {p.stem: WorkloadSpec.from_yaml(p) for p in sorted(WORKLOAD_DIR.glob("*.yaml"))}
    assert set(specs) == {"prefill_eager", "prefill_graph", "decode_eager", "decode_graph"}
    assert {s.comparison_key for s in specs.values()} == {
        "prefill-eager",
        "prefill-graph",
        "decode-eager",
        "decode-graph",
    }

    # prefill-eager 已按 $PROF_ROOT/qwen35-prefill/workload.log 核对
    verified = specs["prefill_eager"]
    assert verified.verified is True
    assert verified.hardware.device_count == 2
    assert verified.input.input_length == 1024
    assert verified.execution.mode.value == "eager"

    # 四组采集条件均已核对，因此同阶段的 Eager/Graph 对比可以通过可比性检查
    assert all(spec.verified for spec in specs.values())
    graph = specs["prefill_graph"]
    ok, diffs = verified.comparable_with(graph)
    assert ok is True, diffs
    assert graph.execution.graph_mode.value == "full_and_piecewise"
    # 采集参数差异（max_num_seqs）不属于可比性判定，但应由 compare 以 warning 提示
    assert graph.input.extra.get("max_num_seqs") == 32


def test_sparse_data_reports_missing_metrics(ruleset):
    workload = WorkloadSpec.from_yaml(WORKLOAD_DIR / "decode_eager.yaml")
    table = build_synthetic_table("sparse", workload)
    report = DiagnosisEngine(ruleset).diagnose_table(table)
    summary = report.summary()

    assert report.is_synthetic is True
    assert summary["blocked_rules"], "缺少硬件指标时必须报告 blocked 规则"
    missing = {item["fact"] for item in summary["missing_facts_top"]}
    assert "compute_pipe_time_fraction" in missing
    assert "cpu_submit_over_device" in missing
    # 只有 calls/avg 可用的规则最多给出假设
    assert all(c.level is ConfidenceLevel.HYPOTHESIS for c in report.candidates())


def test_rich_data_produces_candidates_and_opportunities(ruleset):
    workload = WorkloadSpec.from_yaml(WORKLOAD_DIR / "prefill_graph.yaml")
    table = build_synthetic_table("rich", workload)
    report = DiagnosisEngine(ruleset).diagnose_table(table)

    fused = {c.rule_id for c in report.candidates()}
    assert "compute_bound.pipe_dominant" in fused
    assert "communication_bound.share_dominant" in fused
    assert "synchronization_bound.wait_dominant" in fused

    opportunities = opportunities_from_diagnosis(report)
    assert opportunities
    for opp in opportunities:
        assert opp.workload_ids == (workload.workload_id,)
        assert opp.diagnosis_refs
        assert opp.validation_plan is not None
        if opp.evidence_level in (ConfidenceLevel.HYPOTHESIS, ConfidenceLevel.INSUFFICIENT_EVIDENCE):
            assert opp.missing_evidence, "假设级机会必须说明缺什么证据"
        # 未校准时不得出现 confirmed
        assert opp.evidence_level is not ConfidenceLevel.CONFIRMED


def test_opportunity_ids_are_unique(ruleset):
    workload = WorkloadSpec.from_yaml(WORKLOAD_DIR / "prefill_graph.yaml")
    report = DiagnosisEngine(ruleset).diagnose_table(build_synthetic_table("rich", workload))
    opportunities = opportunities_from_diagnosis(report)
    ids = [o.opportunity_id for o in opportunities]
    assert len(ids) == len(set(ids))


def test_cli_demo_writes_outputs(tmp_path, capsys):
    wl_dir = tmp_path / "workloads"
    wl_dir.mkdir()
    shutil.copy(WORKLOAD_DIR / "decode_graph.yaml", wl_dir / "decode_graph.yaml")
    out = tmp_path / "out"

    code = main(
        [
            "demo",
            "--workloads-dir",
            str(wl_dir),
            "--out",
            str(out),
            "--scenario",
            "rich",
        ]
    )
    assert code == 0
    captured = capsys.readouterr().out
    assert "合成数据" in captured

    workload_out = out / "qwen35-35b-a3b-decode-graph"
    assert (workload_out / "operator_table.json").is_file()
    assert (workload_out / "diagnosis.json").is_file()
    payload = json.loads((workload_out / "opportunities.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["count"] == len(payload["opportunities"])

    diagnosis = json.loads((workload_out / "diagnosis.json").read_text(encoding="utf-8"))
    assert diagnosis["is_synthetic"] is True
    assert diagnosis["threshold_status"] == "uncalibrated"


def test_cli_validate_ok(capsys):
    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "ascend_910b@v1" in out
    assert "未校准" in out


def test_opportunities_are_ordered_by_score(ruleset):
    workload = WorkloadSpec.from_yaml(WORKLOAD_DIR / "prefill_graph.yaml")
    report = DiagnosisEngine(ruleset).diagnose_table(build_synthetic_table("rich", workload))
    opportunities = opportunities_from_diagnosis(report)
    scores = [o.scores.value for o in opportunities]
    assert scores == sorted(scores, reverse=True)
    assert [o.opportunity_id for o in opportunities] == [
        f"OPP-{i:03d}" for i in range(1, len(opportunities) + 1)
    ]
