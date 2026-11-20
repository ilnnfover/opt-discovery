"""AE-07: feedback separates bare observations from contract validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from opt_discovery.cli import main
from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.discovery.generator import opportunities_from_diagnosis
from opt_discovery.experiment.feedback import apply_feedback
from opt_discovery.parsers.synthetic import build_synthetic_table
from opt_discovery.schemas.bottleneck import BottleneckType
from opt_discovery.schemas.evidence import ConfidenceLevel, MissingEvidence
from opt_discovery.schemas.evidence_gate import (
    ArtifactReference,
    EvidenceSource,
    ValidationBundle,
    ValidationResult,
    ValidationScope,
    evaluate_validation,
    promote_evidence,
    withdraw_evidence,
)
from opt_discovery.schemas.opportunity import (
    Opportunity,
    OpportunityStatus,
    PriorityScore,
    ValidationPlan,
    read_opportunities,
    write_opportunities,
)
from opt_discovery.schemas.workload import WorkloadSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _plan() -> ValidationPlan:
    return ValidationPlan(
        kernel=("compare kernel latency",),
        model=("compare prefill latency",),
        serving=("compare TTFT",),
        correctness=("compare numerics",),
    )


def _opportunity(
    *,
    level: ConfidenceLevel = ConfidenceLevel.HYPOTHESIS,
    opportunity_id: str = "OPP-001",
    operator: str = "MatMul",
    workload: str = "wl",
    is_synthetic: bool = False,
    status: OpportunityStatus = OpportunityStatus.DISCOVERED,
    instance_id: str = "instance-1",
) -> Opportunity:
    missing = (
        [MissingEvidence(name="calibrated_threshold_or_metric", reason="fixture")]
        if level in (ConfidenceLevel.HYPOTHESIS, ConfidenceLevel.INSUFFICIENT_EVIDENCE)
        else []
    )
    return Opportunity(
        opportunity_id=opportunity_id,
        operator=operator,
        workload_ids=(workload,),
        observation="fixture observation",
        hypothesis="fixture hypothesis",
        bottleneck=BottleneckType.COMPUTE_BOUND,
        evidence_level=level,
        missing_evidence=missing,
        validation_plan=_plan(),
        status=status,
        scores=PriorityScore(impact=0.5, feasibility=0.5, confidence=0.5, cost=0.5),
        is_synthetic=is_synthetic,
        instance_id=instance_id,
    )


def _artifact(tmp_path: Path, name: str, payload: dict | None = None) -> ArtifactReference:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload or {"artifact": name}), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ArtifactReference(name=name, path=str(path), sha256=digest, kind="fixture")


def _result(
    scope: ValidationScope,
    artifacts: tuple[str, ...],
    *,
    baseline: str = "base",
    candidate: str = "cand",
    input_payload: dict | None = None,
    effect: float | None = 0.20,
    samples: int | None = 5,
    uncertainty: float | None = 0.01,
    comparable: bool | None = True,
    correctness: bool | None = True,
    performance_passed: bool | None = True,
) -> ValidationResult:
    return ValidationResult(
        scope=scope,
        baseline=baseline,
        candidate=candidate,
        input=input_payload or {"batch_size": 1, "input_length": 1024},
        comparable=comparable,
        correctness=correctness,
        performance_passed=performance_passed,
        effect=effect,
        samples=samples,
        uncertainty=uncertainty,
        artifacts=artifacts,
    )


def _bundle(
    tmp_path: Path,
    *,
    scopes: tuple[ValidationScope, ...] = (ValidationScope.OPERATOR,),
    source: EvidenceSource = EvidenceSource.MEASURED,
    opportunity_id: str = "OPP-001",
    operator: str = "MatMul",
    workload: str = "wl",
    change: str = "change-1",
    baseline: str = "base",
    candidate: str = "cand",
    effect: float = 0.20,
    samples: int = 5,
    uncertainty: float = 0.01,
    artifacts_per_scope: bool = True,
    instance_id: str = "instance-1",
) -> ValidationBundle:
    artifacts: list[ArtifactReference] = []
    results: list[ValidationResult] = []
    shared = _artifact(tmp_path, "shared")
    if artifacts_per_scope:
        for index, scope in enumerate(scopes):
            artifact = _artifact(tmp_path, f"{scope.value}-{index}")
            artifacts.append(artifact)
            results.append(_result(scope, (artifact.name,), effect=effect, samples=samples,
                                   uncertainty=uncertainty))
    else:
        artifacts.append(shared)
        results = [
            _result(scope, (shared.name,), effect=effect, samples=samples, uncertainty=uncertainty)
            for scope in scopes
        ]
    return ValidationBundle(
        opportunity_id=opportunity_id,
        opportunity_instance_id=instance_id,
        operator=operator,
        workload_id=workload,
        change_id=change,
        source=source,
        baseline=baseline,
        candidate=candidate,
        input={"batch_size": 1, "input_length": 1024},
        results=tuple(results),
        artifacts=tuple(artifacts),
    )


def _comparison(verdict: str = "b_faster", delta: float | None = -20.0) -> dict:
    metrics = {}
    if delta is not None:
        metrics = {
            "latency_median_s": {"a": 1.0, "b": 1 + delta / 100, "ratio": 1 + delta / 100,
                                 "delta_pct": delta},
            "latency_p90_s": {"a": 1.1, "b": 1.1, "ratio": 1.0, "delta_pct": 0.0},
            "tokens_per_s_median": {"a": 64, "b": 64, "ratio": 1.0, "delta_pct": 0.0},
        }
    return {
        "a": "base",
        "b": "cand",
        "verdict": verdict,
        "rounds_a": 5,
        "rounds_b": 5,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# structured validation
# ---------------------------------------------------------------------------
def test_single_operator_validation_upgrades_to_probable(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is True
    assert decision.promoted is True
    assert decision.target_level is ConfidenceLevel.PROBABLE
    assert decision.confirmed_ready is False

    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    assert item.scores is not None and item.scores.confidence == pytest.approx(0.72)
    assert item.extra["evidence_gate"]["validations"]


def test_kernel_only_never_confirms(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL,))
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is True
    assert decision.target_level is ConfidenceLevel.PROBABLE
    assert decision.confirmed_ready is False
    assert any("single_scope_capped_at_probable" in reason for reason in decision.reasons)


def test_kernel_and_e2e_independent_confirms(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is True
    assert decision.confirmed_ready is True
    assert decision.target_level is ConfidenceLevel.CONFIRMED

    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.CONFIRMED


def test_shared_artifact_does_not_confirm(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path,
        scopes=(ValidationScope.KERNEL, ValidationScope.MODEL),
        artifacts_per_scope=False,
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is True
    assert decision.confirmed_ready is False
    assert decision.target_level is ConfidenceLevel.PROBABLE
    assert any("shared_evidence_not_independent" in reason for reason in decision.reasons)


def test_repeated_same_scope_does_not_confirm(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.KERNEL)
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is True
    assert decision.confirmed_ready is False
    assert decision.target_level is ConfidenceLevel.PROBABLE


@pytest.mark.parametrize(
    "scopes,expected",
    [
        ((ValidationScope.WORKLOAD,), False),
        ((ValidationScope.OPERATOR,), True),
    ],
)
def test_workload_scope_is_not_attributed(tmp_path: Path, scopes, expected) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=scopes)
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is expected
    if not expected:
        assert any("workload_scope_not_attributable" in reason for reason in decision.reasons)
        assert item.evidence_level is ConfidenceLevel.HYPOTHESIS


@pytest.mark.parametrize("field", ["opportunity_id", "operator", "workload"])
def test_binding_mismatch_blocks(tmp_path: Path, field: str) -> None:
    item = _opportunity()
    kwargs = {"opportunity_id": "OPP-999", "operator": "Other", "workload": "other"}
    bundle = _bundle(tmp_path, **{field: kwargs[field]})
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is False
    assert decision.promoted is False
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS


def test_result_pairing_and_input_mismatch_block(tmp_path: Path) -> None:
    item = _opportunity()
    artifact = _artifact(tmp_path, "pair")
    bundle = ValidationBundle(
        opportunity_id="OPP-001",
        opportunity_instance_id="instance-1",
        operator="MatMul",
        workload_id="wl",
        change_id="change-1",
        source=EvidenceSource.MEASURED,
        baseline="base",
        candidate="cand",
        input={"batch_size": 1, "input_length": 1024},
        results=(
            _result(ValidationScope.KERNEL, (artifact.name,), baseline="other"),
        ),
        artifacts=(artifact,),
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is False
    assert any("result_baseline_candidate_mismatch" in reason for reason in decision.reasons)

    bundle2 = ValidationBundle(
        opportunity_id="OPP-001",
        opportunity_instance_id="instance-1",
        operator="MatMul",
        workload_id="wl",
        change_id="change-1",
        source=EvidenceSource.MEASURED,
        baseline="base",
        candidate="cand",
        input={"batch_size": 1, "input_length": 1024},
        results=(
            _result(
                ValidationScope.KERNEL,
                (artifact.name,),
                input_payload={"batch_size": 8, "input_length": 1024},
            ),
        ),
        artifacts=(artifact,),
    )
    decision2 = evaluate_validation(item, bundle2)
    assert decision2.allowed is False
    assert any("result_input_mismatch" in reason for reason in decision2.reasons)


def test_synthetic_source_and_synthetic_opportunity_block(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, source=EvidenceSource.SYNTHETIC)
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is False
    assert any("source_not_verified" in reason for reason in decision.reasons)

    real_root = tmp_path / "real"
    real_root.mkdir()
    real_bundle = _bundle(real_root)
    synthetic_item = _opportunity(is_synthetic=True)
    decision2 = evaluate_validation(synthetic_item, real_bundle)
    assert decision2.allowed is False
    assert "synthetic_opportunity" in decision2.reasons


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"comparable": False}, "not_comparable"),
        ({"correctness": False}, "correctness_failed"),
        ({"samples": 2}, "insufficient_samples"),
        ({"uncertainty": None}, "missing_uncertainty"),
        ({"effect": None}, "missing_effect"),
        ({"effect": 0.04}, "effect_below_threshold"),
        ({"performance_passed": False}, "performance_not_improved"),
    ],
)
def test_missing_or_failed_evidence_blocks(tmp_path: Path, overrides, reason) -> None:
    item = _opportunity()
    artifact = _artifact(tmp_path, f"case-{reason}")
    bundle = ValidationBundle(
        opportunity_id="OPP-001",
        opportunity_instance_id="instance-1",
        operator="MatMul",
        workload_id="wl",
        change_id="change-1",
        source=EvidenceSource.MEASURED,
        baseline="base",
        candidate="cand",
        input={"batch_size": 1, "input_length": 1024},
        results=(_result(ValidationScope.OPERATOR, (artifact.name,), **overrides),),
        artifacts=(artifact,),
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is False
    assert any(reason in entry for entry in decision.reasons)


def test_artifact_digest_mismatch_blocks(tmp_path: Path) -> None:
    item = _opportunity()
    artifact = _artifact(tmp_path, "digest")
    (tmp_path / "digest.json").write_text("tampered", encoding="utf-8")
    bundle = ValidationBundle(
        opportunity_id="OPP-001",
        opportunity_instance_id="instance-1",
        operator="MatMul",
        workload_id="wl",
        change_id="change-1",
        source=EvidenceSource.MEASURED,
        baseline="base",
        candidate="cand",
        input={"batch_size": 1, "input_length": 1024},
        results=(_result(ValidationScope.OPERATOR, (artifact.name,)),),
        artifacts=(artifact,),
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is False
    assert any("artifact_digest_mismatch" in reason for reason in decision.reasons)


def test_blocked_validation_does_not_lower_existing_level(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    bundle = _bundle(tmp_path, scopes=(ValidationScope.WORKLOAD,))
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    assert any("证据未升级" in caveat for caveat in item.caveats)


# ---------------------------------------------------------------------------
# bare benchmark observation
# ---------------------------------------------------------------------------
def test_bare_comparison_observes_without_upgrading():
    items = [_opportunity(), _opportunity(opportunity_id="OPP-002", operator="Add")]
    result = apply_feedback(items, _comparison(), operator="MatMul", change="fused")
    assert result.upgraded == 0
    assert result.validated == 0
    for item in items:
        assert item.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert items[0].status is OpportunityStatus.ACCEPTED
    assert items[0].extra["measured"]["source"] == "benchmark_observation"
    assert items[0].extra["measured"]["verified"] is False


def test_workload_level_bare_result_does_not_attribute():
    items = [_opportunity(), _opportunity(opportunity_id="OPP-002", operator="Add")]
    result = apply_feedback(items, _comparison())
    assert result.matched == len(items)
    assert result.upgraded == 0
    for item in items:
        assert item.evidence_level is ConfidenceLevel.HYPOTHESIS
        assert item.status is OpportunityStatus.VALIDATING
        assert any("不能归因到单个算子" in caveat for caveat in item.caveats)


def test_bare_slower_observation_rejects_lifecycle_only():
    item = _opportunity()
    result = apply_feedback([item], _comparison("b_slower", 12.0), operator="MatMul")
    assert result.rejected == 1
    assert item.status is OpportunityStatus.REJECTED
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS


def test_bare_insufficient_samples_changes_nothing():
    item = _opportunity()
    snapshot = (item.status, item.evidence_level, dict(item.extra))
    result = apply_feedback([item], _comparison("insufficient_samples", None))
    assert result.matched == 0
    assert (item.status, item.evidence_level, dict(item.extra)) == snapshot


# ---------------------------------------------------------------------------
# CLI and JSON
# ---------------------------------------------------------------------------
def _write_opportunities(tmp_path: Path, items: list[Opportunity]) -> Path:
    return write_opportunities(items, tmp_path / "opportunities.json")


def _write_validation(tmp_path: Path, bundle: ValidationBundle) -> Path:
    path = tmp_path / "validation.json"
    path.write_text(json.dumps(bundle.to_dict()), encoding="utf-8")
    return path


def test_cli_feedback_with_validation_upgrades(tmp_path: Path) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    val_path = _write_validation(tmp_path, bundle)

    code = main(["feedback", "--opportunities", str(opp_path), "--validation", str(val_path)])
    assert code == 0
    restored = read_opportunities(opp_path)[0]
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    assert restored.extra["measured"]["verified"] is True


def test_cli_bare_comparison_does_not_upgrade(tmp_path: Path) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    compare_path = tmp_path / "compare.json"
    compare_path.write_text(json.dumps(_comparison()), encoding="utf-8")

    code = main(
        [
            "feedback",
            "--opportunities",
            str(opp_path),
            "--compare",
            str(compare_path),
            "--operator",
            "MatMul",
        ]
    )
    assert code == 0
    restored = read_opportunities(opp_path)[0]
    assert restored.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert restored.extra["measured"]["source"] == "benchmark_observation"


def test_cli_rejects_operator_or_change_mismatch(tmp_path: Path) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    val_path = _write_validation(tmp_path, bundle)

    assert main(
        ["feedback", "--opportunities", str(opp_path), "--validation", str(val_path),
         "--operator", "Add"]
    ) == 2
    assert main(
        ["feedback", "--opportunities", str(opp_path), "--validation", str(val_path),
         "--change", "other"]
    ) == 2


def test_cli_rejects_unmatched_opportunity_id(tmp_path: Path) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    bundle = _bundle(tmp_path, opportunity_id="OPP-999")
    val_path = _write_validation(tmp_path, bundle)
    assert main(
        ["feedback", "--opportunities", str(opp_path), "--validation", str(val_path)]
    ) == 2


def test_same_operator_different_opportunity_is_not_matched(tmp_path: Path) -> None:
    first = _opportunity(opportunity_id="OPP-001")
    second = _opportunity(opportunity_id="OPP-002")
    bundle = _bundle(tmp_path, opportunity_id="OPP-002")
    result = apply_feedback([first, second], None, validation=bundle)
    assert result.matched == 1
    assert result.upgraded == 1
    assert first.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert second.evidence_level is ConfidenceLevel.PROBABLE


def test_valid_confirmed_round_trips_through_json(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)
    )
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.CONFIRMED
    path = _write_opportunities(tmp_path, [item])
    restored = read_opportunities(path)[0]
    assert restored.evidence_level is ConfidenceLevel.CONFIRMED
    assert restored.extra["evidence_gate"]["validations"]


def test_untrusted_confirmed_json_is_downgraded(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    payload = item.to_dict()
    payload["evidence_level"] = "confirmed"
    path = tmp_path / "opportunities.json"
    path.write_text(
        json.dumps({"schema_version": 1, "count": 1, "opportunities": [payload]}),
        encoding="utf-8",
    )
    restored = read_opportunities(path)[0]
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    assert restored.extra["evidence_gate"]["trust"]["trusted"] is False
    assert any("confirmed" in caveat for caveat in restored.caveats)


def test_extra_cannot_override_retained_fields(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    item.extra = {"evidence_level": "confirmed", "note": "x"}
    payload = item.to_dict()
    assert payload["evidence_level"] == "probable"
    assert payload["note"] == "x"
    restored = Opportunity.from_dict(payload)
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    assert "evidence_level" not in restored.extra


def test_withdraw_evidence_prevents_revival_and_reuse(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)
    )
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.CONFIRMED
    withdraw_evidence(
        item,
        reason="reproduction failed",
        source=EvidenceSource.MANUAL,
        change_id="change-1",
    )
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    assert item.extra["evidence_gate"]["withdrawals"]

    same_change = evaluate_validation(item, bundle)
    assert same_change.allowed is False
    assert "withdrawn_change" in same_change.reasons

    reused = ValidationBundle(
        opportunity_id=bundle.opportunity_id,
        opportunity_instance_id=bundle.opportunity_instance_id,
        operator=bundle.operator,
        workload_id=bundle.workload_id,
        change_id="change-2",
        source=bundle.source,
        baseline=bundle.baseline,
        candidate=bundle.candidate,
        input=dict(bundle.input),
        results=bundle.results,
        artifacts=bundle.artifacts,
    )
    reuse_decision = evaluate_validation(item, reused)
    assert reuse_decision.allowed is False
    assert "withdrawn_artifact_reuse" in reuse_decision.reasons


# ---------------------------------------------------------------------------
# existing generated pipeline compatibility
# ---------------------------------------------------------------------------
def _generated_opportunities():
    workload = WorkloadSpec.from_yaml(WORKLOAD_DIR / "prefill_graph.yaml")
    table = build_synthetic_table("rich", workload)
    report = DiagnosisEngine(RuleSet.load(RULES)).diagnose_table(table)
    items = opportunities_from_diagnosis(report)
    assert items
    return items


def test_synthetic_pipeline_marks_opportunities_and_blocks_validation(tmp_path: Path) -> None:
    items = _generated_opportunities()
    assert all(item.is_synthetic for item in items)
    bundle = _bundle(
        tmp_path,
        opportunity_id=items[0].opportunity_id,
        operator=items[0].operator,
        workload=items[0].workload_ids[0],
        scopes=(ValidationScope.OPERATOR,),
        instance_id=items[0].instance_id,
    )
    decision = evaluate_validation(items[0], bundle)
    assert decision.allowed is False
    assert "synthetic_opportunity" in decision.reasons


def test_generated_opportunities_round_trip(tmp_path: Path) -> None:
    items = _generated_opportunities()
    path = write_opportunities(items, tmp_path / "opportunities.json")
    restored = read_opportunities(path)
    assert len(restored) == len(items)
    assert all(item.instance_id for item in restored)
    assert [item.instance_id for item in restored] == [item.instance_id for item in items]
    assert all(item.is_synthetic for item in restored)
    assert all(
        item.evidence_level is not ConfidenceLevel.CONFIRMED for item in restored
    )


# ---------------------------------------------------------------------------
# P1-3: blocked CLI/API never claims success or trusted measurement
# ---------------------------------------------------------------------------
def test_blocked_validation_returns_nonzero_and_keeps_audit(
    tmp_path: Path, capsys
) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    bad = replace(
        bundle,
        results=tuple(replace(result, correctness=False) for result in bundle.results),
    )
    val_path = _write_validation(tmp_path, bad)

    code = main(
        ["feedback", "--opportunities", str(opp_path), "--validation", str(val_path)]
    )
    assert code == 1
    assert "verdict=blocked" in capsys.readouterr().out
    restored = read_opportunities(opp_path)[0]
    assert restored.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert "measured" not in restored.extra
    assert restored.extra["evidence_gate"]["validations"]
    assert any("证据未升级" in caveat for caveat in restored.caveats)


def test_rejected_validation_does_not_overwrite_trusted_measured(tmp_path: Path) -> None:
    item = _opportunity()
    good = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    apply_feedback([item], None, validation=good)
    trusted = dict(item.extra["measured"])
    assert trusted["verified"] is True

    bad = replace(
        good,
        results=tuple(replace(result, correctness=False) for result in good.results),
    )
    result = apply_feedback([item], None, validation=bad)
    assert result.blocked == 1
    assert result.validated == 0
    assert item.extra["measured"] == trusted


def test_compare_and_validation_are_mutually_exclusive_api_and_cli(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    result = apply_feedback([item], _comparison(), validation=bundle)
    assert result.matched == 0
    assert "compare_and_validation_mutually_exclusive" in " ".join(result.notes)
    assert "measured" not in item.extra
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS

    opp_path = _write_opportunities(tmp_path, [item])
    val_path = _write_validation(tmp_path, bundle)
    compare_path = tmp_path / "compare.json"
    compare_path.write_text(json.dumps(_comparison()), encoding="utf-8")
    code = main(
        [
            "feedback",
            "--opportunities",
            str(opp_path),
            "--compare",
            str(compare_path),
            "--validation",
            str(val_path),
        ]
    )
    assert code == 2
    assert read_opportunities(opp_path)[0].evidence_level is ConfidenceLevel.HYPOTHESIS


def test_cli_rejects_nan_min_effect(tmp_path: Path) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    val_path = _write_validation(tmp_path, bundle)
    code = main(
        [
            "feedback",
            "--opportunities",
            str(opp_path),
            "--validation",
            str(val_path),
            "--min-effect",
            "nan",
        ]
    )
    assert code == 2
    assert read_opportunities(opp_path)[0].evidence_level is ConfidenceLevel.HYPOTHESIS


def test_cli_zero_effect_with_zero_threshold_is_blocked(tmp_path: Path) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    zero = replace(
        bundle,
        results=tuple(replace(result, effect=0.0) for result in bundle.results),
    )
    val_path = _write_validation(tmp_path, zero)
    code = main(
        [
            "feedback",
            "--opportunities",
            str(opp_path),
            "--validation",
            str(val_path),
            "--min-effect",
            "0",
        ]
    )
    assert code == 1
    restored = read_opportunities(opp_path)[0]
    assert restored.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert "measured" not in restored.extra


def test_cli_missing_artifact_returns_nonzero_and_downgrades(tmp_path: Path) -> None:
    item = _opportunity()
    opp_path = _write_opportunities(tmp_path, [item])
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    val_path = _write_validation(tmp_path, bundle)
    Path(bundle.artifacts[0].path).unlink()

    code = main(
        ["feedback", "--opportunities", str(opp_path), "--validation", str(val_path)]
    )
    assert code == 1
    restored = read_opportunities(opp_path)[0]
    assert restored.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert "measured" not in restored.extra


def test_valid_bundle_promotes_after_untrusted_confirmed_is_normalised(
    tmp_path: Path,
) -> None:
    item = _opportunity(level=ConfidenceLevel.CONFIRMED)
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    result = apply_feedback([item], None, validation=bundle)
    assert result.validated == 1
    assert result.upgraded == 1
    assert item.evidence_level is ConfidenceLevel.CONFIRMED


def test_same_local_id_different_instance_matches_only_the_target(tmp_path: Path) -> None:
    first = _opportunity(instance_id="instance-1")
    second = _opportunity(instance_id="instance-2")
    bundle = _bundle(
        tmp_path,
        scopes=(ValidationScope.KERNEL, ValidationScope.MODEL),
        instance_id="instance-2",
    )
    result = apply_feedback([first, second], None, validation=bundle)
    assert result.matched == 1
    assert result.validated == 1
    assert result.upgraded == 1
    assert result.blocked == 0
    assert result.untouched == 1
    assert first.evidence_level is ConfidenceLevel.HYPOTHESIS
    assert second.evidence_level is ConfidenceLevel.CONFIRMED


def test_allowed_validation_clears_old_trust_marker(tmp_path: Path) -> None:
    item = _opportunity()
    item.extra = {
        "evidence_gate": {
            "validations": [],
            "withdrawals": [],
            "trust": {"trusted": False, "reasons": ["artifact_missing:x"]},
        }
    }
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    apply_feedback([item], None, validation=bundle)
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    assert "trust" not in item.extra["evidence_gate"]
