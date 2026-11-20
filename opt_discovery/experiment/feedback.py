"""把 benchmark/验证结果回填到 Opportunity。

AE-07 之后只有一条升级路径：

* **裸 benchmark 比较**（``--compare``，无论是否带 ``--operator``）只是**观察记录**：
  写入 ``extra["measured"]``、标记 ``source=benchmark_observation``，可以更新生命周期
  状态，但**不能改变 evidence_level**。工作负载级 A/B 更不能归因到单个算子。
* **结构化验证**（``--validation`` 或直接传 :class:`ValidationBundle`）走
  :func:`opt_discovery.schemas.evidence_gate.promote_evidence`，由同一契约校验机会、
  算子、workload、change、baseline/candidate、输入、可比性、正确性、样本、不确定性、
  来源和产物引用。单次有效实验逐级提升且最多 probable；只有同一目标、同一变更、
  同一比较对象/输入、且 kernel 与端到端结果相互独立的验证才可以 confirmed。

CLI ``--compare`` and ``--validation`` are mutually exclusive.  A bare
comparison is only an observation; a structured validation is the only
promotion path.  Supplying both is rejected instead of trying to reconcile two
different measurement objects.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from ..schemas.evidence_gate import (
    MIN_EFFECT,
    EvidenceGateError,
    EvidenceSource,
    ValidationBundle,
    ValidationScope,
    _apply_promotion,
    evaluate_validation,
    trusted_evidence_level,
)
from ..schemas.opportunity import Opportunity, OpportunityStatus

__all__ = [
    "FeedbackResult",
    "apply_feedback",
    "load_comparison",
]


@dataclass
class FeedbackResult:
    verdict: str
    operator: str | None
    matched: int = 0
    upgraded: int = 0
    rejected: int = 0
    untouched: int = 0
    validated: int = 0
    blocked: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "operator": self.operator,
            "matched": self.matched,
            "upgraded": self.upgraded,
            "rejected": self.rejected,
            "untouched": self.untouched,
            "validated": self.validated,
            "blocked": self.blocked,
            "notes": list(self.notes),
        }


def load_comparison(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _measured_payload(comparison: dict, *, operator_scoped: bool, change: str | None) -> dict:
    metrics = comparison.get("metrics") or {}
    median = metrics.get("latency_median_s") or {}
    p90 = metrics.get("latency_p90_s") or {}
    tokens = metrics.get("tokens_per_s_median") or {}
    return {
        "a": comparison.get("a"),
        "b": comparison.get("b"),
        "verdict": comparison.get("verdict"),
        "rounds": [comparison.get("rounds_a"), comparison.get("rounds_b")],
        "latency_median_ratio": median.get("ratio"),
        "latency_median_delta_pct": median.get("delta_pct"),
        "latency_p90_ratio": p90.get("ratio"),
        "tokens_ratio": tokens.get("ratio"),
        "operator_scoped": operator_scoped,
        "change": change,
        "source": "benchmark_observation",
        "verified": False,
        "synthetic": comparison.get("synthetic"),
    }


def _validation_measured(bundle: ValidationBundle) -> dict[str, Any]:
    scopes = sorted({result.scope.value for result in bundle.results})
    effects = [
        result.effect
        for result in bundle.results
        if result.performance_passed is True and result.effect is not None
    ]
    samples = [result.samples for result in bundle.results if result.samples is not None]
    return {
        "a": bundle.baseline,
        "b": bundle.candidate,
        "verdict": "validated",
        "change": bundle.change_id,
        "source": bundle.source.value,
        "verified": True,
        "synthetic": bundle.source is EvidenceSource.SYNTHETIC,
        "operator_scoped": any(scope != ValidationScope.WORKLOAD.value for scope in scopes),
        "scopes": scopes,
        "effect": min(effects) if effects else None,
        "samples": min(samples) if samples else None,
    }


def _append_caveat(opportunity: Opportunity, text: str) -> None:
    if text in opportunity.caveats:
        return
    opportunity.caveats = tuple(opportunity.caveats) + (text,)


def _blocked_result(
    result: FeedbackResult,
    opportunities: Iterable[Opportunity],
    reasons: list[str],
    *,
    change: str | None = None,
) -> FeedbackResult:
    text = "输入不一致或不可信，未产生任何升级：" + ", ".join(reasons)
    result = replace(result, verdict="blocked")
    result.notes = tuple(dict.fromkeys(result.notes + (text,)))
    for opportunity in opportunities:
        if change and change != opportunity.opportunity_id:
            continue
        _append_caveat(opportunity, text)
    return result


def apply_feedback(
    opportunities: Iterable[Opportunity],
    comparison: dict | None,
    *,
    operator: str | None = None,
    change: str | None = None,
    min_effect: float = MIN_EFFECT,
    validation: ValidationBundle | None = None,
    artifact_root: str | Path | None = None,
) -> FeedbackResult:
    """Backfill observations and, when a bundle is supplied, promote evidence."""
    if (
        isinstance(min_effect, bool)
        or not isinstance(min_effect, (int, float))
        or not math.isfinite(float(min_effect))
        or not 0.0 <= float(min_effect) <= 1.0
    ):
        raise EvidenceGateError("min_effect must be a finite number within [0, 1]")
    items = list(opportunities)
    verdict = str(comparison.get("verdict", "undetermined")) if comparison else "validated"
    target_operator = operator or (validation.operator if validation else None)
    result = FeedbackResult(verdict=verdict, operator=target_operator)

    if validation is None and comparison is None:
        result.notes = ("缺少 --compare 或 --validation，未回填任何机会",)
        return result

    if validation is not None:
        if comparison is not None:
            # The bare comparison describes a whole benchmark run, while the
            # bundle describes one target instance; reconciling them invites
            # false positives (different workload/input/rounds still matching a
            # kernel effect).  Keep the two features independent.
            return _blocked_result(
                result,
                items,
                ["compare_and_validation_mutually_exclusive"],
                change=validation.opportunity_id,
            )
        if operator is not None and operator != validation.operator:
            return _blocked_result(
                result, items, ["operator_arg_mismatch"], change=validation.opportunity_id
            )
        if change is not None and change != validation.change_id:
            return _blocked_result(
                result, items, ["change_arg_mismatch"], change=validation.opportunity_id
            )
        result.notes = tuple(dict.fromkeys(result.notes + ("仅使用结构化验证输入",)))

        for opportunity in items:
            # Binding uses both the local id and the generated instance id.
            # Two runs can legitimately share ``OPP-###``; the instance id is
            # what makes the target unique.
            if (
                opportunity.opportunity_id != validation.opportunity_id
                or opportunity.instance_id != validation.opportunity_instance_id
            ):
                result.untouched += 1
                continue
            result.matched += 1
            # Normalise an untrusted pre-existing `confirmed` before deciding,
            # so a valid bundle is evaluated against the level we can trust.
            trusted_evidence_level(opportunity, artifact_root=artifact_root)
            decision = evaluate_validation(
                opportunity,
                validation,
                artifact_root=artifact_root,
                min_effect=min_effect,
            )
            # One gate, one decision: record/apply exactly what was evaluated.
            _apply_promotion(
                opportunity,
                validation,
                decision,
                artifact_root=artifact_root,
                policy=min_effect,
            )
            if not decision.allowed:
                result.blocked += 1
                result.notes = tuple(dict.fromkeys(result.notes + decision.reasons))
                continue

            # Only trusted, validated input may write ``measured.verified`` and
            # bump the validated counter; rejected input never overwrites an
            # existing trusted measurement.
            opportunity.extra = {
                **dict(opportunity.extra),
                "measured": _validation_measured(validation),
            }
            result.validated += 1
            if decision.promoted:
                result.upgraded += 1
            if opportunity.status in (
                OpportunityStatus.DISCOVERED,
                OpportunityStatus.TRIAGED,
                OpportunityStatus.VALIDATING,
            ):
                opportunity.status = OpportunityStatus.ACCEPTED
        if result.blocked:
            result = replace(result, verdict="blocked")
        return result

    # -- observation-only path ------------------------------------------------
    assert comparison is not None
    operator_scoped = bool(operator)
    measured = _measured_payload(comparison, operator_scoped=operator_scoped, change=change)
    delta = measured.get("latency_median_delta_pct")

    if verdict == "insufficient_samples":
        result.notes = ("样本不足，未回填任何机会（需 >= 3 轮）",)
        return result
    if verdict == "undetermined":
        result.notes = ("比较结果未定（缺少可比较指标），未回填",)
        return result
    result.notes = tuple(
        dict.fromkeys(
            result.notes
            + ("裸 bench 比较只作为观察记录；证据升级需 --validation 结构化验证",)
        )
    )

    for opportunity in items:
        if operator and opportunity.operator != operator:
            result.untouched += 1
            continue
        result.matched += 1
        opportunity.extra = {**dict(opportunity.extra), "measured": measured}

        if not operator_scoped:
            if opportunity.status in (OpportunityStatus.DISCOVERED, OpportunityStatus.TRIAGED):
                opportunity.status = OpportunityStatus.VALIDATING
            _append_caveat(
                opportunity,
                "工作负载级 A/B 结果不能归因到单个算子；需单变量实验和结构化验证才能升级证据级别",
            )
            continue

        if verdict == "b_faster" and delta is not None and delta <= -min_effect * 100:
            opportunity.status = OpportunityStatus.ACCEPTED
            _append_caveat(
                opportunity,
                "裸 bench 观察到端到端改善，但未经过结构化验证契约，证据级别保持不变",
            )
        elif verdict == "b_slower" and delta is not None and delta >= min_effect * 100:
            opportunity.status = OpportunityStatus.REJECTED
            result.rejected += 1
            _append_caveat(
                opportunity,
                f"单变量观察端到端变慢 {delta:+.1f}%；证据级别不变，如需否定旧结论请显式撤回",
            )
        else:
            opportunity.status = OpportunityStatus.VALIDATING
            _append_caveat(
                opportunity,
                f"端到端变化 {delta}% 未超过判定阈值 ±{min_effect * 100:.0f}%；"
                "证据级别不变，需结构化验证",
            )

    return result
