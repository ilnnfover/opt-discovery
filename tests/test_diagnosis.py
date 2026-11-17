from __future__ import annotations

import pytest
from conftest import REPO_ROOT, build_ruleset

from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.facts import FACT_CATALOG, build_facts
from opt_discovery.diagnosis.rules import (
    Condition,
    Rule,
    RuleError,
    RuleSet,
    Threshold,
)
from opt_discovery.schemas.bottleneck import BottleneckType
from opt_discovery.schemas.evidence import ConfidenceLevel
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, TaskType
from opt_discovery.schemas.workload import WorkloadSpec


def test_facts_from_record(matmul_record):
    facts = build_facts(matmul_record, workload_total_time_us=48000)
    assert facts.value("calls") == 64
    assert facts.value("total_time_us") == pytest.approx(24000)
    assert facts.value("aicore_time_fraction") == pytest.approx(22000 / 24000)
    assert facts.value("mte2_time_fraction") == pytest.approx(1000 / 24000)
    assert facts.value("time_share") == pytest.approx(0.5)
    assert facts.value("is_kernel") == 1.0


def test_facts_do_not_invent_missing_metrics(matmul_record):
    facts = build_facts(matmul_record)
    assert facts.has("cpu_submit_time_us") is False
    assert facts.has("cpu_submit_over_device") is False
    assert facts.has("memory_bandwidth_utilization") is False


def test_ratio_metric_percent_is_normalized():
    record = OperatorRecord(
        workload_id="wl",
        operator="MatMul",
        calls=1,
        total_time_us=Metric(100, "us"),
        extra={"aicore_utilization": Metric(85, "%", Provenance.MEASURED)},
    )
    facts = build_facts(record)
    assert facts.value("aicore_utilization") == pytest.approx(0.85)


def test_measured_thresholds_raise_to_probable_but_not_confirmed(matmul_record, workload):
    ruleset = build_ruleset(Provenance.MEASURED)
    engine = DiagnosisEngine(ruleset)
    diagnosis = engine.diagnose(matmul_record, workload)
    assert len(diagnosis.candidates) == 1
    candidate = diagnosis.candidates[0]
    # 校准阈值只能把结论抬到 probable；confirmed 需要端到端 + kernel 级验证
    assert candidate.level is ConfidenceLevel.PROBABLE
    # 置信度受 PROBABLE 的 LEVEL_CAP(0.80) 约束，不能到 0.95
    assert 0.55 <= candidate.confidence <= 0.80


def test_declared_threshold_caps_at_probable(matmul_record, workload):
    diagnosis = DiagnosisEngine(build_ruleset(Provenance.DECLARED)).diagnose(matmul_record, workload)
    candidate = diagnosis.candidates[0]
    assert candidate.level is ConfidenceLevel.PROBABLE
    assert candidate.confidence <= 0.80


def test_assumed_threshold_is_hypothesis_only(matmul_record, workload):
    diagnosis = DiagnosisEngine(build_ruleset(Provenance.ASSUMED)).diagnose(matmul_record, workload)
    candidate = diagnosis.candidates[0]
    assert candidate.level is ConfidenceLevel.HYPOTHESIS
    assert candidate.confidence <= 0.50
    # 每条证据都要能回溯到事实与阈值来源
    assert all(item.fact and item.threshold is not None for item in candidate.evidence if item.satisfied)


def test_missing_required_fact_blocks_instead_of_guessing(workload):
    record = OperatorRecord(
        workload_id=workload.workload_id,
        operator="Add",
        calls=512,
        total_time_us=Metric(512, "us"),
    )
    diagnosis = DiagnosisEngine(build_ruleset(Provenance.ASSUMED)).diagnose(record, workload)
    assert diagnosis.candidates == []
    blocked = [e for e in diagnosis.evaluations if e.status == "blocked"]
    assert len(blocked) == 1
    assert "compute_pipe_time_fraction" in blocked[0].missing_facts
    assert "必要指标未采集" in (blocked[0].note or "")


def test_failed_required_condition_does_not_fire(workload, matmul_record):
    record = OperatorRecord(
        workload_id=matmul_record.workload_id,
        operator="MatMul",
        calls=1,
        total_time_us=Metric(100, "us"),
        extra={"aicore_time_us": Metric(10, "us")},
    )
    diagnosis = DiagnosisEngine(build_ruleset(Provenance.ASSUMED)).diagnose(record, workload)
    assert diagnosis.candidates == []
    matched = [e for e in diagnosis.evaluations if e.status == "not_matched"]
    assert matched and "compute_pipe_time_fraction" in matched[0].failed_facts


def test_applies_to_filters_task_type(workload):
    record = OperatorRecord(
        workload_id=workload.workload_id,
        operator="HcclAllReduce",
        calls=8,
        total_time_us=Metric(600, "us"),
        task_type=TaskType.COMMUNICATION,
        extra={"aicore_time_us": Metric(500, "us")},
    )
    diagnosis = DiagnosisEngine(build_ruleset(Provenance.ASSUMED)).diagnose(record, workload)
    assert diagnosis.candidates == []
    assert all(e.status == "not_applicable" for e in diagnosis.evaluations)


def _hypothesis_rule() -> Rule:
    return Rule(
        id="launch_bound.small_kernel_many_calls",
        bottleneck=BottleneckType.LAUNCH_BOUND,
        description="many tiny kernels",
        supporting=(
            Condition(fact="calls", op=">=", value=128, justification="test fixture"),
            Condition(fact="avg_time_us", op="<=", value=20.0, justification="test fixture"),
            Condition(fact="is_kernel", op="is_true"),
        ),
        allow_hypothesis_only=True,
        missing_evidence=(),
        suggested_actions=("collect host timeline",),
    )


def test_hypothesis_only_rule_never_exceeds_hypothesis(workload):
    ruleset = RuleSet(
        version=1,
        device_profile_id="test",
        rules=(_hypothesis_rule(),),
        status="uncalibrated",
    )
    ruleset.validate()
    record = OperatorRecord(
        workload_id=workload.workload_id,
        operator="Add",
        calls=512,
        total_time_us=Metric(512, "us"),
    )
    diagnosis = DiagnosisEngine(ruleset).diagnose(record, workload)
    candidate = diagnosis.candidates[0]
    assert candidate.hypothesis_only is True
    assert candidate.level is ConfidenceLevel.HYPOTHESIS
    assert candidate.confidence <= 0.50


def test_hypothesis_rule_requires_explicit_opt_in():
    rule = Rule(
        id="launch_bound.x",
        bottleneck=BottleneckType.LAUNCH_BOUND,
        description="bad rule",
        supporting=(Condition(fact="calls", op=">=", value=1, justification="test"),),
    )
    ruleset = RuleSet(version=1, device_profile_id="t", rules=(rule,))
    with pytest.raises(RuleError):
        ruleset.validate()


def test_literal_threshold_requires_justification():
    rule = Rule(
        id="memory_bound.x",
        bottleneck=BottleneckType.MEMORY_BOUND,
        description="literal without justification",
        required=(Condition(fact="mte2_time_fraction", op=">=", value=0.5),),
    )
    ruleset = RuleSet(version=1, device_profile_id="t", rules=(rule,))
    with pytest.raises(RuleError):
        ruleset.validate()


def test_unknown_fact_is_rejected():
    rule = Rule(
        id="memory_bound.y",
        bottleneck=BottleneckType.MEMORY_BOUND,
        description="unknown fact",
        required=(Condition(fact="made_up_metric", op=">=", threshold="t"),),
    )
    ruleset = RuleSet(
        version=1,
        device_profile_id="t",
        rules=(rule,),
        thresholds={},
    )
    with pytest.raises(RuleError):
        ruleset.validate()


def test_threshold_unit_must_match_fact_unit():
    from opt_discovery.diagnosis.rules import Threshold

    rule = Rule(
        id="launch_bound.z",
        bottleneck=BottleneckType.LAUNCH_BOUND,
        description="unit mismatch",
        required=(Condition(fact="avg_time_us", op="<=", threshold="bad"),),
    )
    ruleset = RuleSet(
        version=1,
        device_profile_id="t",
        rules=(rule,),
        thresholds={"bad": Threshold("bad", 1.0, "ratio")},
    )
    with pytest.raises(RuleError):
        ruleset.validate()


def test_shipped_ruleset_is_valid_and_fact_coherent():
    ruleset = RuleSet.load(REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml")
    assert ruleset.rules
    bound = {
        BottleneckType.COMPUTE_BOUND,
        BottleneckType.MEMORY_BOUND,
        BottleneckType.LAUNCH_BOUND,
        BottleneckType.SYNCHRONIZATION_BOUND,
        BottleneckType.PARALLELISM_BOUND,
        BottleneckType.COMMUNICATION_BOUND,
    }
    assert {rule.bottleneck for rule in ruleset.rules} == bound
    for rule in ruleset.rules:
        for fact in rule.facts_referenced():
            assert fact in FACT_CATALOG
    assert ruleset.is_calibrated is False


def _vector_record(workload) -> OperatorRecord:
    return OperatorRecord(
        workload_id=workload.workload_id,
        operator="RmsNorm",
        calls=4,
        total_time_us=Metric(1000, "us"),
        extra={"vec_time_us": Metric(900, "us"), "mte3_time_us": Metric(100, "us")},
    )


def test_compute_pipe_fact_covers_vector_cores():
    """只用 aicore_time 无法描述纯 Vector 算子，组合事实必须取 max。"""
    workload = WorkloadSpec.from_dict(
        {
            "workload_id": "wl",
            "model": {"name": "m"},
            "hardware": {"device": "d"},
            "execution": {"phase": "decode", "mode": "eager"},
        }
    )
    record = _vector_record(workload)
    facts = build_facts(record, workload_total_time_us=1000)
    assert facts.value("compute_pipe_time_fraction") == pytest.approx(0.9)
    assert facts.value("memory_pipe_time_fraction") == pytest.approx(0.1)
    diagnosis = DiagnosisEngine(build_ruleset(Provenance.ASSUMED)).diagnose(record, workload)
    assert [c.rule_id for c in diagnosis.candidates] == ["compute_bound.pipe_dominant"]


def test_ratio_above_one_is_flagged_in_evidence(workload):
    record = OperatorRecord(
        workload_id=workload.workload_id,
        operator="Attention",
        calls=2,
        total_time_us=Metric(1000, "us"),
        extra={"mte2_time_us": Metric(1500, "us")},
    )
    ruleset = RuleSet(
        version=1,
        device_profile_id="t",
        rules=(
            Rule(
                id="memory_bound.memory_pipe_dominant",
                bottleneck=BottleneckType.MEMORY_BOUND,
                description="t",
                required=(Condition(fact="memory_pipe_time_fraction", op=">=", threshold="m"),),
            ),
        ),
        thresholds={"m": Threshold("m", 0.75, "ratio", Provenance.ASSUMED, "test")},
    )
    ruleset.validate()
    diagnosis = DiagnosisEngine(ruleset).diagnose(record, workload)
    candidate = diagnosis.candidates[0]
    assert candidate.evidence[0].observed == pytest.approx(1.5)
    assert "占比>1" in candidate.evidence[0].statement


def test_competing_candidates_are_reported(workload):
    record = OperatorRecord(
        workload_id=workload.workload_id,
        operator="MatMul",
        calls=8,
        total_time_us=Metric(1000, "us"),
        extra={"aicore_time_us": Metric(950, "us"), "mte2_time_us": Metric(900, "us")},
    )
    diagnosis = DiagnosisEngine(build_ruleset(Provenance.ASSUMED)).diagnose(record, workload)
    # 只有一个规则时不会有竞争提示；用完整规则集验证
    ruleset = RuleSet.load(REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml")
    diagnosis = DiagnosisEngine(ruleset).diagnose(record, workload)
    assert len({c.bottleneck for c in diagnosis.candidates}) > 1
    assert any("多个瓶颈候选并存" in note for note in diagnosis.notes)


def test_communication_rule_only_applies_to_communication_tasks():
    ruleset = RuleSet.load(REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml")
    rule = ruleset.rule("communication_bound.share_dominant")
    from opt_discovery.schemas.workload import ExecutionMode, Phase

    assert rule.applies_to.matches(Phase.PREFILL, ExecutionMode.EAGER, TaskType.COMMUNICATION)
    assert not rule.applies_to.matches(Phase.PREFILL, ExecutionMode.EAGER, TaskType.KERNEL)
