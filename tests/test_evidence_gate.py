"""AE-07: structured validation schema and trusted-read/report boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from opt_discovery.report.generator import build_report
from opt_discovery.schemas.bottleneck import BottleneckType
from opt_discovery.schemas.evidence import ConfidenceLevel, MissingEvidence
from opt_discovery.schemas.evidence_gate import (
    ArtifactReference,
    EvidenceGateError,
    EvidenceSource,
    ValidationBundle,
    ValidationResult,
    ValidationScope,
    _apply_promotion,
    evaluate_validation,
    promote_evidence,
    trusted_evidence_level,
    trusted_level_for_mapping,
    withdraw_evidence,
)
from opt_discovery.schemas.opportunity import (
    Opportunity,
    OpportunityStatus,
    PriorityScore,
    ValidationPlan,
)


def _plan() -> ValidationPlan:
    return ValidationPlan(
        kernel=("kernel check",), model=(), serving=(), correctness=("compare output",)
    )


def _opportunity(
    *,
    level: ConfidenceLevel = ConfidenceLevel.HYPOTHESIS,
    status: OpportunityStatus = OpportunityStatus.DISCOVERED,
    is_synthetic: bool = False,
    instance_id: str = "instance-1",
) -> Opportunity:
    missing = (
        [MissingEvidence(name="calibrated_threshold_or_metric", reason="fixture")]
        if level in (ConfidenceLevel.HYPOTHESIS, ConfidenceLevel.INSUFFICIENT_EVIDENCE)
        else []
    )
    return Opportunity(
        opportunity_id="OPP-001",
        operator="MatMul",
        workload_ids=("wl",),
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


def _artifact(tmp_path: Path, name: str) -> ArtifactReference:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"artifact": name}), encoding="utf-8")
    return ArtifactReference(
        name=name,
        path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _bundle(
    tmp_path: Path,
    *,
    scopes: tuple[ValidationScope, ...] = (ValidationScope.OPERATOR,),
    source: EvidenceSource = EvidenceSource.MEASURED,
) -> ValidationBundle:
    artifacts = tuple(_artifact(tmp_path, f"{scope.value}-{index}") for index, scope in enumerate(scopes))
    results = tuple(
        ValidationResult(
            scope=scope,
            baseline="base",
            candidate="cand",
            input={"batch_size": 1, "input_length": 1024},
            comparable=True,
            correctness=True,
            performance_passed=True,
            effect=0.20,
            samples=5,
            uncertainty=0.01,
            artifacts=(artifacts[index].name,),
        )
        for index, scope in enumerate(scopes)
    )
    return ValidationBundle(
        opportunity_id="OPP-001",
        opportunity_instance_id="instance-1",
        operator="MatMul",
        workload_id="wl",
        change_id="change-1",
        source=source,
        baseline="base",
        candidate="cand",
        input={"batch_size": 1, "input_length": 1024},
        results=results,
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# schema: fail closed on malformed structured input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.pop("opportunity_id"),
        lambda data: data.update(operator=""),
        lambda data: data.update(source="external"),
        lambda data: data.update(workload_id=123),
        lambda data: data.update(baseline="cand"),
        lambda data: data.update(change_id=""),
        lambda data: data.update(input={}),
        lambda data: data.update(results=[]),
        lambda data: data.update(extra_field=True),
        lambda data: data.update(schema_version=2),
        lambda data: data["results"][0].update(scope="cluster"),
        lambda data: data["results"][0].update(samples=True),
        lambda data: data["results"][0].update(comparable="yes"),
        lambda data: data["results"][0].update(correctness=1),
        lambda data: data["results"][0].update(effect="0.2"),
        lambda data: data["results"][0].update(artifacts=[]),
        lambda data: data["artifacts"][0].update(sha256="not-a-digest"),
        lambda data: data["results"][0].update(unknown_field=1),
    ],
)
def test_invalid_bundle_fails_closed(tmp_path: Path, mutate) -> None:
    payload = _bundle(tmp_path).to_dict()
    mutate(payload)
    with pytest.raises(EvidenceGateError):
        ValidationBundle.from_dict(payload)


def test_manual_source_is_accepted(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, source=EvidenceSource.MANUAL)
    decision = _evaluate(item, bundle)
    assert decision.allowed is True


def test_benchmark_source_never_promotes(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, source=EvidenceSource.BENCHMARK)
    decision = _evaluate(item, bundle)
    assert decision.allowed is False
    assert any("source_not_verified" in reason for reason in decision.reasons)


def _evaluate(item: Opportunity, bundle: ValidationBundle):
    from opt_discovery.schemas.evidence_gate import evaluate_validation

    return evaluate_validation(item, bundle)


# ---------------------------------------------------------------------------
# lifecycle vs evidence level
# ---------------------------------------------------------------------------
def test_accepted_is_independent_of_confirmed(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE, status=OpportunityStatus.ACCEPTED)
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    assert item.status is OpportunityStatus.ACCEPTED


def test_failed_validation_does_not_negate_existing_conclusion(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE, status=OpportunityStatus.ACCEPTED)
    bundle = _bundle(tmp_path, scopes=(ValidationScope.WORKLOAD,))
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    assert item.status is OpportunityStatus.ACCEPTED


# ---------------------------------------------------------------------------
# trusted read / report
# ---------------------------------------------------------------------------
def test_trusted_level_for_mapping_rejects_unverified_confirmed() -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    payload = item.to_dict()
    payload["evidence_level"] = "confirmed"
    level, note = trusted_level_for_mapping(payload)
    assert level == "probable"
    assert note and "已保守降级" in note


def test_trusted_evidence_level_accepts_valid_confirmed(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)
    )
    promote_evidence(item, bundle)
    level, reasons = trusted_evidence_level(item)
    assert level is ConfidenceLevel.CONFIRMED
    assert reasons == ()


def test_direct_confirmed_construction_is_not_trusted() -> None:
    item = _opportunity(level=ConfidenceLevel.CONFIRMED)
    level, reasons = trusted_evidence_level(item)
    assert level is ConfidenceLevel.PROBABLE
    assert reasons
    assert item.extra["evidence_gate"]["trust"]["trusted"] is False


def test_public_field_change_after_construction_is_not_trusted(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.PROBABLE
    item.evidence_level = ConfidenceLevel.CONFIRMED
    level, reasons = trusted_evidence_level(item)
    assert level is ConfidenceLevel.PROBABLE
    assert reasons


def test_tampered_confirmed_json_is_downgraded_on_reread(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)
    )
    promote_evidence(item, bundle)
    payload = item.to_dict()
    payload["evidence_gate"]["validations"][0]["bundle"]["results"][0][
        "correctness"
    ] = False
    path = tmp_path / "opportunities.json"
    path.write_text(
        json.dumps({"schema_version": 1, "count": 1, "opportunities": [payload]}),
        encoding="utf-8",
    )
    from opt_discovery.schemas.opportunity import read_opportunities

    restored = read_opportunities(path)[0]
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    assert "audit_digest_mismatch" in restored.extra["evidence_gate"]["trust"]["reasons"]


def _write_report_inputs(root: Path, payload: dict) -> None:
    workload = root / "wl"
    workload.mkdir(parents=True)
    (workload / "operator_table.json").write_text(
        json.dumps(
            {
                "workload": {"execution": {"phase": "decode", "mode": "eager"}},
                "records": [],
                "meta": {"row_count": 0, "warnings": [], "extra": {}},
            }
        ),
        encoding="utf-8",
    )
    (workload / "opportunities.json").write_text(
        json.dumps({"schema_version": 1, "count": 1, "opportunities": [payload]}),
        encoding="utf-8",
    )


def test_report_does_not_show_untrusted_confirmed(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    payload = item.to_dict()
    payload["evidence_level"] = "confirmed"
    out = tmp_path / "out"
    _write_report_inputs(out, payload)
    text = build_report(out)
    assert "| probable（不可信） |" in text
    assert "已按 probable（不可信）展示" in text


def test_report_shows_valid_confirmed(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)
    )
    promote_evidence(item, bundle)
    out = tmp_path / "out"
    _write_report_inputs(out, item.to_dict())
    text = build_report(out)
    assert "| confirmed |" in text
    assert "已按 probable 展示" not in text


def test_report_reads_normal_historical_opportunity(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    out = tmp_path / "out"
    _write_report_inputs(out, item.to_dict())
    text = build_report(out)
    assert "| probable |" in text


# ---------------------------------------------------------------------------
# withdrawal keeps the original record and blocks reuse
# ---------------------------------------------------------------------------
def test_withdrawal_preserves_original_validation_record(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)
    )
    promote_evidence(item, bundle)
    records_before = copy.deepcopy(item.extra["evidence_gate"]["validations"])
    withdraw_evidence(
        item,
        reason="regression reproduced",
        source=EvidenceSource.MANUAL,
        change_id="change-1",
    )
    audit = item.extra["evidence_gate"]
    assert audit["validations"][0]["bundle"] == records_before[0]["bundle"]
    assert audit["validations"][0]["decision"] == records_before[0]["decision"]
    assert audit["validations"][0]["bundle_digest"] == records_before[0]["bundle_digest"]
    assert audit["withdrawals"][0]["original_records"][0]["bundle_digest"]
    assert audit["validations"][0]["withdrawn"] is True
    assert item.evidence_level is ConfidenceLevel.PROBABLE


def test_withdrawal_requires_a_lower_level() -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    with pytest.raises(EvidenceGateError):
        withdraw_evidence(
            item,
            reason="noop",
            source=EvidenceSource.MANUAL,
            to_level=ConfidenceLevel.PROBABLE,
        )


# ---------------------------------------------------------------------------
# P1-1: instance binding, real e2e scope and historical provenance
# ---------------------------------------------------------------------------
def test_operator_scope_is_not_end_to_end(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(
        tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.OPERATOR)
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is True
    assert decision.confirmed_ready is False
    assert decision.target_level is ConfidenceLevel.PROBABLE
    assert any("single_scope_capped_at_probable" in r for r in decision.reasons)


def test_kernel_and_model_confirms_from_insufficient_in_one_call(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.INSUFFICIENT_EVIDENCE)
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.CONFIRMED


def test_repeated_single_scope_does_not_climb_two_levels(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.INSUFFICIENT_EVIDENCE)
    bundle = _bundle(tmp_path, scopes=(ValidationScope.OPERATOR,))
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS
    last = item.extra["evidence_gate"]["validations"][-1]
    assert "duplicate_validation_no_repeat_promotion" in last["decision"]["reasons"]


def test_bundle_must_reference_the_generated_instance(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = replace(
        _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL)),
        opportunity_instance_id="another-instance",
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is False
    assert "opportunity_instance_mismatch" in decision.reasons
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS
    recorded = item.extra["evidence_gate"]["validations"][-1]["decision"]
    assert recorded["allowed"] is False
    assert "opportunity_instance_mismatch" in recorded["reasons"]


def test_audit_copied_to_a_new_instance_is_not_trusted(tmp_path: Path) -> None:
    source = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    promote_evidence(source, bundle)
    assert source.evidence_level is ConfidenceLevel.CONFIRMED

    other = _opportunity(instance_id="instance-2")
    other.observation = "a different diagnosis output with the same local labels"
    other.extra = copy.deepcopy(source.extra)
    other.evidence_level = ConfidenceLevel.CONFIRMED
    level, reasons = trusted_evidence_level(other, mark=False)
    assert level is ConfidenceLevel.PROBABLE
    assert "opportunity_instance_mismatch" in reasons


def test_historical_json_without_source_fields_cannot_upgrade(tmp_path: Path) -> None:
    item = _opportunity()
    payload = item.to_dict()
    payload.pop("instance_id")
    payload.pop("is_synthetic")
    payload["evidence_level"] = "probable"
    restored = Opportunity.from_dict(payload)
    assert restored.instance_id == ""
    assert restored.is_synthetic is None
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    decision = evaluate_validation(restored, bundle)
    assert decision.allowed is False
    assert "historical_opportunity_source_unknown" in decision.reasons
    assert "unknown_opportunity_source" in decision.reasons
    promote_evidence(restored, bundle)
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    recorded = restored.extra["evidence_gate"]["validations"][-1]["decision"]
    assert recorded["allowed"] is False
    assert "historical_opportunity_source_unknown" in recorded["reasons"]
    assert "unknown_opportunity_source" in recorded["reasons"]


def test_historical_confirmed_with_audit_is_downgraded_when_source_missing(
    tmp_path: Path,
) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    promote_evidence(item, bundle)
    payload = item.to_dict()
    payload.pop("is_synthetic")
    path = tmp_path / "opportunities.json"
    path.write_text(
        json.dumps({"schema_version": 1, "count": 1, "opportunities": [payload]}),
        encoding="utf-8",
    )
    from opt_discovery.schemas.opportunity import read_opportunities

    restored = read_opportunities(path)[0]
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    assert "unknown_opportunity_source" in restored.extra["evidence_gate"]["trust"]["reasons"]


def test_result_input_uses_typed_equality(tmp_path: Path) -> None:
    item = _opportunity()
    artifact = _artifact(tmp_path, "typed")
    result = ValidationResult(
        scope=ValidationScope.KERNEL,
        baseline="base",
        candidate="cand",
        input={"flag": True},
        comparable=True,
        correctness=True,
        performance_passed=True,
        effect=0.2,
        samples=5,
        uncertainty=0.01,
        artifacts=(artifact.name,),
    )
    bundle = ValidationBundle(
        opportunity_id="OPP-001",
        opportunity_instance_id="instance-1",
        operator="MatMul",
        workload_id="wl",
        change_id="change-1",
        source=EvidenceSource.MEASURED,
        baseline="base",
        candidate="cand",
        input={"flag": 1},
        results=(result,),
        artifacts=(artifact,),
    )
    decision = evaluate_validation(item, bundle)
    assert decision.allowed is False
    assert any("result_input_mismatch" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# P1-2: validated policy, zero effect and artifact verification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, 1.01])
def test_invalid_min_effect_is_rejected(tmp_path: Path, bad: float) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    with pytest.raises(EvidenceGateError):
        evaluate_validation(item, bundle, min_effect=bad)
    with pytest.raises(EvidenceGateError):
        promote_evidence(item, bundle, min_effect=bad)


def test_zero_effect_is_never_a_gain_even_with_zero_threshold(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    zeroed = replace(
        bundle,
        results=tuple(replace(result, effect=0.0) for result in bundle.results),
    )
    decision = evaluate_validation(item, zeroed, min_effect=0.0)
    assert decision.allowed is False
    assert any("effect_not_positive" in r for r in decision.reasons)


def test_custom_threshold_is_persisted_and_replayed(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    low = replace(
        bundle,
        results=tuple(replace(result, effect=0.04) for result in bundle.results),
    )
    promote_evidence(item, low, min_effect=0.03)
    assert item.evidence_level is ConfidenceLevel.CONFIRMED
    assert item.extra["evidence_gate"]["validations"][-1]["policy"]["min_effect"] == 0.03
    path = tmp_path / "opportunities.json"
    from opt_discovery.schemas.opportunity import read_opportunities, write_opportunities

    restored = read_opportunities(write_opportunities([item], path))[0]
    assert restored.evidence_level is ConfidenceLevel.CONFIRMED


def test_promotion_with_default_policy_rejects_004(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    low = replace(
        bundle,
        results=tuple(replace(result, effect=0.04) for result in bundle.results),
    )
    decision = evaluate_validation(item, low)
    assert decision.allowed is False
    assert any("effect_below_threshold" in r for r in decision.reasons)


def test_missing_artifact_downgrades_on_reread_and_report(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    promote_evidence(item, bundle)
    assert item.evidence_level is ConfidenceLevel.CONFIRMED

    first = item.extra["evidence_gate"]["validations"][-1]["bundle"]["artifacts"][0]
    payload = copy.deepcopy(item.to_dict())
    Path(first["path"]).unlink()

    from opt_discovery.schemas.opportunity import read_opportunities, write_opportunities

    path = tmp_path / "opportunities.json"
    restored = read_opportunities(write_opportunities([item], path))[0]
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    assert "artifact_missing" in " ".join(
        restored.extra["evidence_gate"]["trust"]["reasons"]
    )

    out = tmp_path / "report"
    _write_report_inputs(out, payload)
    text = build_report(out)
    assert "probable（不可信）" in text


def test_withdrawn_record_cannot_be_revived_by_field_edit(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    promote_evidence(item, bundle)
    withdraw_evidence(item, reason="regression", source=EvidenceSource.MANUAL)
    item.evidence_level = ConfidenceLevel.CONFIRMED
    level, reasons = trusted_evidence_level(item, mark=False)
    assert level is ConfidenceLevel.PROBABLE
    assert "withdrawn_record_present" in reasons


def test_changed_artifact_digest_downgrades_on_reread(tmp_path: Path) -> None:
    item = _opportunity()
    bundle = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    promote_evidence(item, bundle)
    first = item.extra["evidence_gate"]["validations"][-1]["bundle"]["artifacts"][0]
    Path(first["path"]).write_text("tampered", encoding="utf-8")

    from opt_discovery.schemas.opportunity import read_opportunities, write_opportunities

    restored = read_opportunities(write_opportunities([item], tmp_path / "o.json"))[0]
    assert restored.evidence_level is ConfidenceLevel.PROBABLE
    assert "artifact_digest_mismatch" in " ".join(
        restored.extra["evidence_gate"]["trust"]["reasons"]
    )


def test_applying_a_decision_to_a_different_bundle_is_rejected(tmp_path: Path) -> None:
    item = _opportunity()
    good = _bundle(tmp_path, scopes=(ValidationScope.KERNEL, ValidationScope.MODEL))
    decision = evaluate_validation(item, good)
    other = replace(
        good,
        opportunity_instance_id="instance-2",
        source=EvidenceSource.SYNTHETIC,
    )
    with pytest.raises(EvidenceGateError):
        _apply_promotion(
            item,
            other,
            decision,
            artifact_root=None,
            policy=0.05,
        )
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS


def test_report_keeps_untrusted_marker_on_probable(tmp_path: Path) -> None:
    item = _opportunity(level=ConfidenceLevel.PROBABLE)
    payload = item.to_dict()
    payload["evidence_gate"] = {
        "validations": [],
        "withdrawals": [],
        "trust": {"trusted": False, "reasons": ["artifact_missing:kernel"]},
    }
    out = tmp_path / "out"
    _write_report_inputs(out, payload)
    text = build_report(out)
    assert "probable（不可信）" in text
