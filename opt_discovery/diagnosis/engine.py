"""Rule evaluation engine.

The engine never invents a bottleneck.  For every rule it records one of four
outcomes: ``fired``, ``not_matched``, ``blocked`` (required facts missing) or
``not_applicable``.  ``blocked`` outcomes are aggregated in the report so the
next collection step is data-driven instead of guesswork.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..schemas.bottleneck import BottleneckType
from ..schemas.evidence import (
    LEVEL_CAP,
    ConfidenceLevel,
    DiagnosisCandidate,
    EvidenceItem,
    MissingEvidence,
)
from ..schemas.metrics import Provenance, weakest_provenance
from ..schemas.operator import OperatorRecord, OperatorTable, TaskType
from ..schemas.workload import ExecutionMode, Phase, WorkloadSpec
from .facts import Fact, FactSet, build_facts
from .rules import BOOL_OPS, COMPARATORS, OP_SYMBOLS, Condition, Rule, RuleSet, Threshold

__all__ = [
    "RuleEvaluation",
    "OperatorDiagnosis",
    "DiagnosisReport",
    "DiagnosisEngine",
    "LEVEL_BY_WORST_PROVENANCE",
]

#: how strong a conclusion may be, given the weakest input it relied on
#
# 注意：即使观测与阈值都是 measured，规则命中也只能证明「该算子的流水占比达到
# 设备标定水位」，不能证明「优化它一定有端到端收益」。因此规则上限是 PROBABLE；
# CONFIRMED 只保留给做过端到端 + kernel 级验证的机会（见 experiment/feedback.py）。
LEVEL_BY_WORST_PROVENANCE: dict[Provenance, ConfidenceLevel] = {
    Provenance.MEASURED: ConfidenceLevel.PROBABLE,
    Provenance.DERIVED: ConfidenceLevel.PROBABLE,
    Provenance.DECLARED: ConfidenceLevel.PROBABLE,
    Provenance.ASSUMED: ConfidenceLevel.HYPOTHESIS,
}

_STATUS_FIRED = "fired"
_STATUS_NOT_MATCHED = "not_matched"
_STATUS_BLOCKED = "blocked"
_STATUS_NOT_APPLICABLE = "not_applicable"


@dataclass
class RuleEvaluation:
    """Audit record for one rule on one operator."""

    rule_id: str
    status: str
    required_total: int = 0
    required_satisfied: int = 0
    supporting_total: int = 0
    supporting_satisfied: int = 0
    missing_facts: tuple[str, ...] = ()
    failed_facts: tuple[str, ...] = ()
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "required": f"{self.required_satisfied}/{self.required_total}",
            "supporting": f"{self.supporting_satisfied}/{self.supporting_total}",
            "missing_facts": list(self.missing_facts),
            "failed_facts": list(self.failed_facts),
            "note": self.note,
        }


@dataclass
class OperatorDiagnosis:
    workload_id: str
    operator: str
    kernel: str | None
    task_type: TaskType
    calls: int
    facts: FactSet
    candidates: list[DiagnosisCandidate] = field(default_factory=list)
    evaluations: list[RuleEvaluation] = field(default_factory=list)
    notes: tuple[str, ...] = ()

    @property
    def confirmed_candidates(self) -> list[DiagnosisCandidate]:
        return [
            c
            for c in self.candidates
            if c.level in (ConfidenceLevel.CONFIRMED, ConfidenceLevel.PROBABLE)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload_id": self.workload_id,
            "operator": self.operator,
            "kernel": self.kernel,
            "task_type": self.task_type.value,
            "calls": self.calls,
            "candidates": [c.to_dict() for c in self.candidates],
            "evaluations": [e.to_dict() for e in self.evaluations],
            "facts": self.facts.to_dict()["facts"],
            "notes": list(self.notes),
        }


@dataclass
class DiagnosisReport:
    workload_id: str
    ruleset_id: str
    operators: list[OperatorDiagnosis] = field(default_factory=list)
    engine_version: str = "0.1.0"
    is_synthetic: bool = False
    threshold_status: str = "uncalibrated"
    generated_at: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- aggregates ------------------------------------------------------
    def candidates(self) -> list[DiagnosisCandidate]:
        return [c for op in self.operators for c in op.candidates]

    def summary(self) -> dict[str, Any]:
        by_bottleneck: dict[str, int] = {}
        by_level: dict[str, int] = {}
        for candidate in self.candidates():
            by_bottleneck[candidate.bottleneck] = by_bottleneck.get(candidate.bottleneck, 0) + 1
            by_level[candidate.level.value] = by_level.get(candidate.level.value, 0) + 1

        blocked: dict[str, dict[str, Any]] = {}
        missing_counter: dict[str, int] = {}
        for operator in self.operators:
            for evaluation in operator.evaluations:
                if evaluation.status != _STATUS_BLOCKED:
                    continue
                entry = blocked.setdefault(
                    evaluation.rule_id, {"rule_id": evaluation.rule_id, "operators": 0, "missing": set()}
                )
                entry["operators"] += 1
                entry["missing"].update(evaluation.missing_facts)
                for fact_name in evaluation.missing_facts:
                    missing_counter[fact_name] = missing_counter.get(fact_name, 0) + 1

        blocked_list = [
            {"rule_id": v["rule_id"], "operators": v["operators"], "missing": sorted(v["missing"])}
            for v in sorted(blocked.values(), key=lambda item: (-item["operators"], item["rule_id"]))
        ]
        missing_top = [
            {"fact": name, "blocked_rules": count}
            for name, count in sorted(missing_counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        competing = [
            {
                "operator": operator.operator,
                "candidates": sorted({c.bottleneck for c in operator.confirmed_candidates}),
            }
            for operator in self.operators
            if len({c.bottleneck for c in operator.confirmed_candidates}) > 1
        ]
        return {
            "operators_analyzed": len(self.operators),
            "candidates_total": len(self.candidates()),
            "candidates_by_bottleneck": by_bottleneck,
            "candidates_by_level": by_level,
            "blocked_rules": blocked_list,
            "missing_facts_top": missing_top,
            "competing_operators": competing,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "engine_version": self.engine_version,
            "ruleset_id": self.ruleset_id,
            "threshold_status": self.threshold_status,
            "is_synthetic": self.is_synthetic,
            "generated_at": self.generated_at,
            "workload_id": self.workload_id,
            "notes": list(self.notes),
            "summary": self.summary(),
            "operators": [op.to_dict() for op in self.operators],
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


class DiagnosisEngine:
    """Applies a :class:`RuleSet` to operator records."""

    VERSION = "0.1.0"

    def __init__(
        self,
        ruleset: RuleSet,
        *,
        min_confidence: float = 0.30,
        device_profile: Mapping[str, Any] | None = None,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be within [0, 1]")
        self.ruleset = ruleset
        self.min_confidence = min_confidence
        #: micro-benchmark 设备档案（peak_bf16_tflops / ai_core_count / roofline）
        self.device_profile = device_profile

    # -- public API ------------------------------------------------------
    def diagnose(
        self,
        record: OperatorRecord,
        workload: WorkloadSpec,
        *,
        workload_total_time_us: float | None = None,
        end_to_end_us: float | None = None,
    ) -> OperatorDiagnosis:
        facts = build_facts(
            record,
            workload_total_time_us=workload_total_time_us,
            end_to_end_us=end_to_end_us,
            device_profile=self.device_profile,
        )
        diagnosis = OperatorDiagnosis(
            workload_id=record.workload_id,
            operator=record.operator,
            kernel=record.kernel,
            task_type=record.task_type,
            calls=record.calls,
            facts=facts,
            notes=tuple(facts.issues),
        )
        phase = workload.execution.phase if isinstance(workload.execution.phase, Phase) else Phase.UNKNOWN
        mode = (
            workload.execution.mode
            if isinstance(workload.execution.mode, ExecutionMode)
            else ExecutionMode.UNKNOWN
        )

        for rule in self.ruleset.rules:
            if not rule.applies_to.matches(phase, mode, record.task_type):
                diagnosis.evaluations.append(
                    RuleEvaluation(
                        rule_id=rule.id,
                        status=_STATUS_NOT_APPLICABLE,
                        note=f"applies_to: {rule.applies_to.describe()}",
                    )
                )
                continue
            evaluation, candidate = self._evaluate(rule, facts)
            diagnosis.evaluations.append(evaluation)
            if candidate is not None:
                diagnosis.candidates.append(candidate)

        diagnosis.candidates.sort(key=lambda c: (-c.confidence, c.rule_id))
        competing = {c.bottleneck for c in diagnosis.candidates}
        if len(competing) > 1:
            diagnosis.notes = diagnosis.notes + (
                "多个瓶颈候选并存（" + ", ".join(sorted(competing)) + "），"
                "单靠占比类指标无法区分，需要能区分它们的证据（见各候选的 missing_evidence）",
            )
        return diagnosis

    def diagnose_table(
        self, table: OperatorTable, *, end_to_end_us: float | None = None
    ) -> DiagnosisReport:
        total = table.total_operator_time_us()
        operators = [
            self.diagnose(
                record,
                table.workload,
                workload_total_time_us=total,
                end_to_end_us=end_to_end_us,
            )
            for record in table.records
        ]
        notes: list[str] = []
        if table.meta.is_synthetic:
            notes.append("输入数据为合成数据，仅用于验证流水线，不代表真实 Profiling 结果")
        if not self.ruleset.is_calibrated:
            notes.append(
                f"阈值未校准（ruleset status={self.ruleset.status}），"
                "因此基于阈值的判断最高只能到 hypothesis"
            )
        warnings = table.meta.warnings
        if warnings:
            notes.append(f"解析阶段有 {len(warnings)} 条告警，详见 meta.warnings")
        return DiagnosisReport(
            workload_id=table.workload_id,
            ruleset_id=self.ruleset.identifier,
            operators=operators,
            engine_version=self.VERSION,
            is_synthetic=table.meta.is_synthetic,
            threshold_status=self.ruleset.status,
            notes=tuple(notes),
        )

    # -- internals -------------------------------------------------------
    def _evaluate(
        self, rule: Rule, facts: FactSet
    ) -> tuple[RuleEvaluation, DiagnosisCandidate | None]:
        missing: list[str] = []
        missing_required: list[str] = []
        failed: list[str] = []
        evidence: list[EvidenceItem] = []

        required_satisfied = 0
        for condition in rule.required:
            status, item, fact = self._compare(condition, facts, role="required")
            if status == "missing":
                missing.append(condition.fact)
                missing_required.append(condition.fact)
            elif status == "satisfied":
                required_satisfied += 1
                if item:
                    evidence.append(item)
            else:
                failed.append(condition.fact)
                if item:
                    evidence.append(item)

        supporting_satisfied = 0
        for condition in rule.supporting:
            status, item, _ = self._compare(condition, facts, role="supporting")
            if status == "missing":
                missing.append(condition.fact)
            elif status == "satisfied":
                supporting_satisfied += 1
                if item:
                    evidence.append(item)
            elif item:
                evidence.append(item)

        base = RuleEvaluation(
            rule_id=rule.id,
            status=_STATUS_NOT_MATCHED,
            required_total=len(rule.required),
            required_satisfied=required_satisfied,
            supporting_total=len(rule.supporting),
            supporting_satisfied=supporting_satisfied,
            failed_facts=tuple(sorted(set(failed))),
        )

        if rule.required and missing_required:
            base.status = _STATUS_BLOCKED
            base.missing_facts = tuple(sorted(set(missing_required)))
            base.note = "必要指标未采集，无法判定（支持性指标缺失不影响判定）"
            return base, None
        if rule.required and (failed or required_satisfied < len(rule.required)):
            base.note = "必要条件不满足"
            return base, None
        if not rule.required and supporting_satisfied == 0:
            base.note = "仅有支持性条件且均不满足"
            return base, None

        level, confidence = self._score(rule, supporting_satisfied, evidence)
        if confidence < self.min_confidence:
            base.note = f"置信度 {confidence:.2f} 低于阈值 {self.min_confidence:.2f}"
            return base, None

        missing_items = tuple(
            item for item in self._missing_items(rule, facts)
        )
        for name in sorted(set(missing)):
            if not any(item.name == name for item in missing_items):
                missing_items = missing_items + (
                    MissingEvidence(
                        name=name,
                        reason="支持性指标缺失，仅影响置信度",
                        needed_for=rule.id,
                    ),
                )

        candidate = DiagnosisCandidate(
            rule_id=rule.id,
            bottleneck=rule.bottleneck.value,
            level=level,
            confidence=confidence,
            evidence=evidence,
            missing_evidence=list(missing_items),
            suggested_actions=rule.suggested_actions,
            caveats=rule.caveats,
            hypothesis_only=rule.is_hypothesis_only,
        )
        base.status = _STATUS_FIRED
        return base, candidate

    def _compare(
        self, condition: Condition, facts: FactSet, *, role: str
    ) -> tuple[str, EvidenceItem | None, Fact | None]:
        fact = facts.get(condition.fact)
        if fact is None:
            return "missing", None, None

        threshold = self._resolve_threshold(condition, fact)
        if threshold is None:
            return "failed", None, fact

        if condition.op in BOOL_OPS:
            flag = fact.value >= 0.5
            satisfied = flag if condition.op == "is_true" else not flag
            statement = (
                f"{fact.name}={flag} {OP_SYMBOLS[condition.op]}"
                f"（来源 {fact.source or '-'}, {fact.provenance.value}）"
            )
        else:
            if threshold.unit != fact.unit:
                return "failed", None, fact
            satisfied = COMPARATORS[condition.op](fact.value, threshold.value)
            statement = (
                f"{fact.name}={_fmt(fact.value, fact.unit)} {condition.op} "
                f"{_fmt(threshold.value, threshold.unit)}"
                f"（观测 {fact.provenance.value}，阈值 {threshold.name}/{threshold.provenance.value}）"
            )
            if fact.unit == "ratio" and fact.value > 1.0:
                # 占比类指标 >1 说明存在跨实例/跨核重叠，不能按单实例解释
                statement += "；注意：占比>1，该指标跨实例或跨流水重叠，不能按单实例解释"

        item = EvidenceItem(
            statement=statement,
            fact=fact.name,
            observed=fact.value,
            unit=fact.unit,
            comparator=OP_SYMBOLS[condition.op],
            threshold=threshold.value if condition.op not in BOOL_OPS else None,
            threshold_provenance=threshold.provenance if condition.op not in BOOL_OPS else None,
            provenance=fact.provenance,
            role=role,
            satisfied=satisfied,
        )
        return ("satisfied" if satisfied else "failed"), item, fact

    def _resolve_threshold(self, condition: Condition, fact: Fact) -> Threshold | None:
        if condition.threshold is not None:
            return self.ruleset.thresholds.get(condition.threshold)
        if condition.value is None:
            return None
        return Threshold(
            name=f"{condition.fact}:literal",
            value=condition.value,
            unit=fact.unit,
            provenance=Provenance.DECLARED,
            basis=condition.justification or "",
        )

    def _missing_items(self, rule: Rule, facts: FactSet) -> Iterable[MissingEvidence]:
        for item in rule.missing_evidence:
            if facts.has(item.name):
                continue
            yield item

    def _score(
        self,
        rule: Rule,
        supporting_satisfied: int,
        evidence: list[EvidenceItem],
    ) -> tuple[ConfidenceLevel, float]:
        provenances: list[Provenance] = []
        for item in evidence:
            if item.satisfied and item.provenance is not None:
                provenances.append(item.provenance)
            if item.satisfied and item.threshold_provenance is not None:
                provenances.append(item.threshold_provenance)
        worst = weakest_provenance(provenances) if provenances else Provenance.ASSUMED

        if rule.is_hypothesis_only:
            level = ConfidenceLevel.HYPOTHESIS
        else:
            level = LEVEL_BY_WORST_PROVENANCE[worst]

        support_ratio = (
            supporting_satisfied / len(rule.supporting) if rule.supporting else 0.0
        )
        if rule.is_hypothesis_only:
            raw = 0.20 + 0.25 * support_ratio + 0.05 * _weight(worst)
        else:
            raw = 0.55 + 0.30 * support_ratio + 0.15 * _weight(worst)
        cap = LEVEL_CAP[level]
        if rule.confidence_cap is not None:
            cap = min(cap, rule.confidence_cap)
        return level, min(raw, cap)


def _weight(prov: Provenance) -> float:
    from ..schemas.metrics import PROVENANCE_WEIGHT

    return PROVENANCE_WEIGHT[prov]


def _fmt(value: float, unit: str) -> str:
    if unit == "ratio":
        return f"{value:.3f}"
    if unit == "bool":
        return "true" if value >= 0.5 else "false"
    if unit in ("us", "bytes", "count"):
        if abs(value) >= 1000:
            return f"{value:.0f} {unit}"
        return f"{value:g} {unit}"
    return f"{value:g} {unit}"
