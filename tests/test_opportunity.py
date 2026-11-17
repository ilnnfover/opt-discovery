from __future__ import annotations

import pytest

from opt_discovery.schemas.bottleneck import BottleneckType
from opt_discovery.schemas.evidence import ConfidenceLevel, EvidenceItem, MissingEvidence
from opt_discovery.schemas.metrics import Provenance
from opt_discovery.schemas.opportunity import (
    Opportunity,
    OpportunitySchemaError,
    OpportunityStatus,
    PriorityScore,
    ValidationPlan,
    read_opportunities,
    sort_opportunities,
    write_opportunities,
)


def _plan() -> ValidationPlan:
    return ValidationPlan(
        kernel=("compare kernel latency",),
        model=("compare prefill latency",),
        serving=("compare TTFT",),
        correctness=("compare numerics",),
    )


def test_priority_score_formula_and_bounds():
    score = PriorityScore(impact=0.5, feasibility=0.5, confidence=0.5, cost=0.5)
    assert score.value == pytest.approx(0.25)
    with pytest.raises(OpportunitySchemaError):
        PriorityScore(impact=0.5, feasibility=0.5, confidence=0.5, cost=0.0)
    with pytest.raises(OpportunitySchemaError):
        PriorityScore(impact=1.5, feasibility=0.5, confidence=0.5, cost=0.5)


def test_validation_plan_requires_kernel_and_correctness():
    with pytest.raises(OpportunitySchemaError):
        ValidationPlan(kernel=(), model=(), serving=(), correctness=("x",))


def _make(**overrides) -> Opportunity:
    payload = dict(
        opportunity_id="OPP-001",
        operator="MatMul",
        workload_ids=("wl-prefill-graph",),
        observation="MatMul 累计耗时占比 0.5",
        hypothesis="可能受限于计算流水",
        bottleneck=BottleneckType.COMPUTE_BOUND,
        evidence_level=ConfidenceLevel.PROBABLE,
        validation_plan=_plan(),
    )
    payload.update(overrides)
    return Opportunity(**payload)


def test_opportunity_id_format():
    with pytest.raises(OpportunitySchemaError):
        _make(opportunity_id="1")


def test_opportunity_requires_workload():
    with pytest.raises(OpportunitySchemaError):
        _make(workload_ids=())


def test_hypothesis_level_requires_missing_evidence():
    with pytest.raises(OpportunitySchemaError):
        _make(evidence_level=ConfidenceLevel.HYPOTHESIS, missing_evidence=[])
    opp = _make(
        evidence_level=ConfidenceLevel.HYPOTHESIS,
        missing_evidence=[MissingEvidence(name="cpu_submit_time_us", reason="未采集")],
    )
    assert opp.missing_evidence[0].name == "cpu_submit_time_us"


def test_opportunity_json_roundtrip(tmp_path):
    opp = _make(
        evidence=[
            EvidenceItem(
                statement="aicore_time_fraction=0.92 >= 0.85",
                fact="aicore_time_fraction",
                observed=0.92,
                unit="ratio",
                comparator=">=",
                threshold=0.85,
                threshold_provenance=Provenance.ASSUMED,
                provenance=Provenance.MEASURED,
            )
        ],
        scores=PriorityScore(impact=0.5, feasibility=0.75, confidence=0.5, cost=0.5),
        status=OpportunityStatus.TRIAGED,
    )
    path = write_opportunities([opp], tmp_path / "opportunities.json")
    restored = read_opportunities(path)
    assert len(restored) == 1
    assert restored[0].evidence_level is ConfidenceLevel.PROBABLE
    assert restored[0].status is OpportunityStatus.TRIAGED
    assert restored[0].evidence[0].threshold_provenance is Provenance.ASSUMED
    assert restored[0].scores is not None
    assert restored[0].scores.value == pytest.approx(0.375)


def test_sort_opportunities_is_deterministic():
    low = _make(
        opportunity_id="OPP-002",
        scores=PriorityScore(impact=0.1, feasibility=0.1, confidence=0.1, cost=0.9),
    )
    high = _make(
        opportunity_id="OPP-001",
        scores=PriorityScore(impact=0.9, feasibility=0.9, confidence=0.9, cost=0.1),
    )
    ordered = sort_opportunities([low, high])
    assert [o.opportunity_id for o in ordered] == ["OPP-001", "OPP-002"]
