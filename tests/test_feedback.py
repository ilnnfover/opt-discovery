"""迭代 6：bench 结果回填到 Opportunity。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.discovery.generator import opportunities_from_diagnosis
from opt_discovery.experiment.feedback import apply_feedback
from opt_discovery.parsers.synthetic import build_synthetic_table
from opt_discovery.schemas.evidence import ConfidenceLevel
from opt_discovery.schemas.opportunity import OpportunityStatus, read_opportunities, write_opportunities
from opt_discovery.schemas.workload import WorkloadSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"


def _opportunities():
    workload = WorkloadSpec.from_yaml(WORKLOAD_DIR / "prefill_graph.yaml")
    table = build_synthetic_table("rich", workload)
    report = DiagnosisEngine(RuleSet.load(RULES)).diagnose_table(table)
    items = opportunities_from_diagnosis(report)
    assert items
    return items


def _comparison(verdict: str, delta: float | None) -> dict:
    metrics = {}
    if delta is not None:
        metrics = {
            "latency_median_s": {"a": 1.0, "b": 1 + delta / 100, "ratio": 1 + delta / 100, "delta_pct": delta},
            "latency_p90_s": {"a": 1.1, "b": 1.1, "ratio": 1.0, "delta_pct": 0.0},
            "tokens_per_s_median": {"a": 64, "b": 64, "ratio": 1.0, "delta_pct": 0.0},
        }
    return {"a": "base", "b": "cand", "verdict": verdict, "rounds_a": 5, "rounds_b": 5, "metrics": metrics}


def test_operator_scoped_improvement_upgrades_to_probable():
    items = _opportunities()
    target = items[0]
    result = apply_feedback(
        items, _comparison("b_faster", -20.0), operator=target.operator, change="fused"
    )
    assert result.upgraded == 1
    assert target.status is OpportunityStatus.ACCEPTED
    assert target.evidence_level is ConfidenceLevel.PROBABLE
    assert target.scores is not None and target.scores.confidence == pytest.approx(0.72)
    assert target.extra["measured"]["operator_scoped"] is True
    assert any("kernel 级证据仍缺" in caveat for caveat in target.caveats)


def test_workload_level_result_does_not_attribute_to_operator():
    items = _opportunities()
    before = {item.opportunity_id: item.evidence_level for item in items}
    result = apply_feedback(items, _comparison("b_faster", -20.0))
    assert result.upgraded == 0
    assert result.matched == len(items)
    for item in items:
        assert item.evidence_level is before[item.opportunity_id]  # 级别不变
        assert item.status is OpportunityStatus.VALIDATING
        assert any("不能归因到单个算子" in caveat for caveat in item.caveats)
        assert item.extra["measured"]["verdict"] == "b_faster"


def test_slower_scoped_experiment_is_rejected():
    items = _opportunities()
    target = items[0]
    result = apply_feedback(items, _comparison("b_slower", 12.0), operator=target.operator)
    assert result.rejected == 1
    assert target.status is OpportunityStatus.REJECTED
    assert any("应回退" in caveat for caveat in target.caveats)


def test_no_significant_difference_stays_validating():
    items = _opportunities()
    target = items[0]
    apply_feedback(items, _comparison("no_significant_difference", 1.0), operator=target.operator)
    assert target.status is OpportunityStatus.VALIDATING
    assert target.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert any("未超过判定阈值" in caveat for caveat in target.caveats)


def test_insufficient_samples_changes_nothing():
    items = _opportunities()
    snapshot = [(item.status, item.evidence_level, item.extra) for item in items]
    result = apply_feedback(items, _comparison("insufficient_samples", None))
    assert result.matched == 0
    assert [(item.status, item.evidence_level, item.extra) for item in items] == snapshot


def test_feedback_round_trips_through_json(tmp_path: Path):
    items = _opportunities()
    target = items[0]
    apply_feedback(items, _comparison("b_faster", -30.0), operator=target.operator, change="x")
    path = write_opportunities(items, tmp_path / "opp.json")
    reloaded = read_opportunities(path)
    item = next(o for o in reloaded if o.opportunity_id == target.opportunity_id)
    assert item.status is OpportunityStatus.ACCEPTED
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    assert item.extra["measured"]["latency_median_delta_pct"] == pytest.approx(-30.0)
    assert item.extra["measured"]["change"] == "x"
