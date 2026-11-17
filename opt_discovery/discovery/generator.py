"""Diagnosis -> Opportunity conversion (rule-based, no LLM, no source mapping).

Scope of this increment: turn a fired diagnosis candidate into a schema-valid
opportunity so the contract can be exercised end-to-end.  Two things are
deliberately *not* done here and are marked in ``caveats`` / ``score_basis``:
kernel-to-source mapping and calibrated impact estimation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ..diagnosis.engine import DiagnosisReport, OperatorDiagnosis
from ..schemas.bottleneck import BottleneckType, get_bottleneck
from ..schemas.evidence import ConfidenceLevel, MissingEvidence
from ..schemas.opportunity import (
    Opportunity,
    OpportunityStatus,
    PriorityScore,
    ValidationPlan,
)

__all__ = ["opportunities_from_diagnosis", "DEFAULT_FEASIBILITY", "DEFAULT_COST"]

#: placeholder feasibility per bottleneck category (rule of thumb, not measured)
DEFAULT_FEASIBILITY: dict[BottleneckType, float] = {
    BottleneckType.COMPUTE_BOUND: 0.45,
    BottleneckType.MEMORY_BOUND: 0.55,
    BottleneckType.LAUNCH_BOUND: 0.75,
    BottleneckType.SYNCHRONIZATION_BOUND: 0.65,
    BottleneckType.PARALLELISM_BOUND: 0.55,
    BottleneckType.COMMUNICATION_BOUND: 0.35,
    BottleneckType.UNKNOWN: 0.20,
}

DEFAULT_COST = 0.50
DEFAULT_IMPACT = 0.20

_LEVEL_ORDER = {
    ConfidenceLevel.CONFIRMED: 3,
    ConfidenceLevel.PROBABLE: 2,
    ConfidenceLevel.HYPOTHESIS: 1,
    ConfidenceLevel.INSUFFICIENT_EVIDENCE: 0,
}


@dataclass(frozen=True)
class _Impact:
    value: float
    basis: str


def opportunities_from_diagnosis(
    report: DiagnosisReport,
    *,
    start_index: int = 1,
    min_level: ConfidenceLevel = ConfidenceLevel.HYPOTHESIS,
    max_per_operator: int = 1,
) -> list[Opportunity]:
    """Convert fired candidates into opportunities, ordered by priority score.

    ``OPP-001`` is always the highest-scoring opportunity of this run: IDs are
    assigned *after* sorting, so an ID never implies discovery order.
    """
    threshold = _LEVEL_ORDER[min_level]
    drafts: list[dict] = []

    for operator_diagnosis in report.operators:
        selected = [
            candidate
            for candidate in operator_diagnosis.candidates
            if _LEVEL_ORDER[candidate.level] >= threshold
        ][:max_per_operator]
        for candidate in selected:
            impact = _impact_of(operator_diagnosis)
            bottleneck = _as_bottleneck(candidate.bottleneck)
            scores = PriorityScore(
                impact=round(impact.value, 3),
                feasibility=DEFAULT_FEASIBILITY.get(bottleneck, 0.2),
                confidence=round(candidate.confidence, 3),
                cost=DEFAULT_COST,
            )
            caveats = tuple(candidate.caveats)
            competing = [note for note in operator_diagnosis.notes if "并存" in note]
            caveats += tuple(competing) + ("尚未建立 Kernel -> 源码映射，source_refs 为空",)
            drafts.append(
                {
                    "operator": operator_diagnosis.operator,
                    "workload_ids": (operator_diagnosis.workload_id,),
                    "observation": _observation(operator_diagnosis, candidate),
                    "hypothesis": _hypothesis(operator_diagnosis, candidate, bottleneck),
                    "bottleneck": bottleneck,
                    "evidence_level": candidate.level,
                    "evidence": list(candidate.evidence),
                    "missing_evidence": _missing(candidate),
                    "suggested_actions": candidate.suggested_actions,
                    "validation_plan": _validation_plan(operator_diagnosis.operator),
                    "source_refs": (),
                    "status": OpportunityStatus.DISCOVERED,
                    "scores": scores,
                    "score_basis": {
                        "impact": impact.basis,
                        "feasibility": "按瓶颈类别的经验占位值，未经实测校准",
                        "confidence": f"规则 {candidate.rule_id} 计算得到",
                        "cost": "默认占位值，未评估实现工作量",
                    },
                    "diagnosis_refs": (candidate.rule_id,),
                    "caveats": caveats,
                    "_sort_key": (scores.value, scores.impact, operator_diagnosis.operator),
                }
            )

    drafts.sort(key=lambda draft: (-draft["_sort_key"][0], -draft["_sort_key"][1], draft["_sort_key"][2]))
    opportunities: list[Opportunity] = []
    for offset, draft in enumerate(drafts):
        draft.pop("_sort_key")
        opportunities.append(
            Opportunity(opportunity_id=f"OPP-{start_index + offset:03d}", **draft)
        )
    return opportunities


def _impact_of(operator_diagnosis: OperatorDiagnosis) -> _Impact:
    facts = operator_diagnosis.facts
    for name in ("end_to_end_share", "time_share"):
        value = facts.value(name)
        if value is not None:
            return _Impact(min(value, 1.0), f"取自 {name}（累计时间占比，非关键路径占比）")
    return _Impact(DEFAULT_IMPACT, "缺少时间占比数据，使用默认占位值 0.2")


def _as_bottleneck(value: str) -> BottleneckType:
    try:
        return BottleneckType(value)
    except ValueError:
        return BottleneckType.UNKNOWN


def _observation(operator_diagnosis: OperatorDiagnosis, candidate) -> str:  # noqa: ANN001
    satisfied = [e.statement for e in candidate.evidence if e.satisfied]
    head = (
        f"{operator_diagnosis.operator} 在 {operator_diagnosis.workload_id} 中"
        f"被调用 {operator_diagnosis.calls} 次"
    )
    if satisfied:
        return f"{head}；{'; '.join(satisfied)}"
    return head


def _hypothesis(operator_diagnosis: OperatorDiagnosis, candidate, bottleneck: BottleneckType) -> str:  # noqa: ANN001
    info = get_bottleneck(bottleneck)
    directions = "、".join(info.candidate_directions[:2]) or "待定"
    return (
        f"{operator_diagnosis.operator} 的性能可能受限于{info.label}"
        f"（候选，待验证），可先评估：{directions}"
    )


def _missing(candidate) -> list[MissingEvidence]:  # noqa: ANN001
    items = list(candidate.missing_evidence)
    if not items and candidate.level in (
        ConfidenceLevel.HYPOTHESIS,
        ConfidenceLevel.INSUFFICIENT_EVIDENCE,
    ):
        items.append(
            MissingEvidence(
                name="calibrated_threshold_or_metric",
                reason="当前判断依赖未校准阈值或非实测指标，不能视为已确认结论",
                needed_for=candidate.rule_id,
                how_to_collect="在目标设备上采集基线算子指标或标定设备可达峰值",
            )
        )
    return items


def _validation_plan(operator: str) -> ValidationPlan:
    return ValidationPlan(
        kernel=(
            f"固定 Shape 与输入，对比 {operator} 单次延迟与调用次数",
            "确认优化后 Kernel 名称/数量变化，排除退化为其他算子",
        ),
        model=(
            "在相同 Workload 配置下对比 Prefill/Decode 延迟",
            "对比端到端延迟与设备忙碌时间，确认收益未被其他阶段抵消",
        ),
        serving=("对比 TTFT / TPOT / 吞吐（需真实请求负载）",),
        correctness=("与基线输出做数值对比，确认精度一致且在容差内",),
    )


def to_opportunity_dicts(items: Iterable[Opportunity]) -> Sequence[dict]:
    return [item.to_dict() for item in items]
