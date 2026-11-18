"""把 benchmark 结果回填到 Opportunity（迭代 6）。

规则是刻意保守的：

* **工作负载级 A/B**（例如 Eager vs Graph）**不能**归因到单个算子。此时只把实测结果
  记进 ``extra["measured"]``、状态置 ``validating``、附 caveat，证据级别不变。
* **单变量实验**（显式 ``--operator X --change "..."``，且本次只改了 X）才允许升级：
  ``b_faster`` 且变化 >= ``min_effect`` → ``status=accepted``，
  证据级别最多升到 ``probable``（``confirmed`` 还要求 kernel 级 + 端到端同时验证，
  本工具目前没有 kernel 级实测，因此不冒充）。
* ``b_slower`` → ``rejected``；``no_significant_difference`` → 保持 ``validating``，
  并提示"收益可能被其他因素掩盖"；``insufficient_samples`` → 一律不动。

所有改动都写在机会对象上，便于直接覆盖 ``opportunities.json``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..schemas.evidence import LEVEL_CAP, ConfidenceLevel
from ..schemas.opportunity import Opportunity, OpportunityStatus, PriorityScore

__all__ = ["FeedbackResult", "apply_feedback", "load_comparison"]

_LEVEL_ORDER = {
    ConfidenceLevel.INSUFFICIENT_EVIDENCE: 0,
    ConfidenceLevel.HYPOTHESIS: 1,
    ConfidenceLevel.PROBABLE: 2,
    ConfidenceLevel.CONFIRMED: 3,
}


@dataclass
class FeedbackResult:
    verdict: str
    operator: str | None
    matched: int = 0
    upgraded: int = 0
    rejected: int = 0
    untouched: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "operator": self.operator,
            "matched": self.matched,
            "upgraded": self.upgraded,
            "rejected": self.rejected,
            "untouched": self.untouched,
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
    }


def _append_caveat(opportunity: Opportunity, text: str) -> None:
    if text in opportunity.caveats:
        return
    opportunity.caveats = tuple(opportunity.caveats) + (text,)


def apply_feedback(
    opportunities: Iterable[Opportunity],
    comparison: dict,
    *,
    operator: str | None = None,
    change: str | None = None,
    min_effect: float = 0.05,
) -> FeedbackResult:
    verdict = str(comparison.get("verdict", "undetermined"))
    operator_scoped = bool(operator)
    result = FeedbackResult(verdict=verdict, operator=operator)
    measured = _measured_payload(comparison, operator_scoped=operator_scoped, change=change)
    delta = measured.get("latency_median_delta_pct")

    if verdict == "insufficient_samples":
        result.notes = ("样本不足，未回填任何机会（需 >= 3 轮）",)
        return result
    if verdict == "undetermined":
        result.notes = ("比较结果未定（缺少可比较指标），未回填",)
        return result

    for opportunity in opportunities:
        if operator and opportunity.operator != operator:
            result.untouched += 1
            continue
        result.matched += 1
        opportunity.extra = {**dict(opportunity.extra), "measured": measured}

        if not operator_scoped:
            # 工作负载级 A/B：只能记录，不能归因
            if opportunity.status in (OpportunityStatus.DISCOVERED, OpportunityStatus.TRIAGED):
                opportunity.status = OpportunityStatus.VALIDATING
            _append_caveat(
                opportunity,
                "工作负载级 A/B 结果不能归因到单个算子；需单变量实验才能升级证据级别",
            )
            continue

        if verdict == "b_faster" and delta is not None and delta <= -min_effect * 100:
            current = _LEVEL_ORDER[opportunity.evidence_level]
            target = min(current + 1, _LEVEL_ORDER[ConfidenceLevel.PROBABLE])
            if target > current:
                opportunity.evidence_level = ConfidenceLevel.PROBABLE
                result.upgraded += 1
                _append_caveat(
                    opportunity,
                    f"单变量实验端到端改善 {delta:.1f}%；kernel 级证据仍缺，故最多到 probable",
                )
                if opportunity.scores:
                    opportunity.scores = PriorityScore(
                        impact=opportunity.scores.impact,
                        feasibility=opportunity.scores.feasibility,
                        confidence=round(LEVEL_CAP[ConfidenceLevel.PROBABLE] * 0.9, 3),
                        cost=opportunity.scores.cost,
                    )
                    opportunity.score_basis = {
                        **dict(opportunity.score_basis),
                        "confidence": f"端到端实测改善 {delta:.1f}% 后提升（上限 probable）",
                    }
            opportunity.status = OpportunityStatus.ACCEPTED
        elif verdict == "b_slower" and delta is not None and delta >= min_effect * 100:
            opportunity.status = OpportunityStatus.REJECTED
            result.rejected += 1
            _append_caveat(opportunity, f"单变量实验端到端变慢 {delta:+.1f}%，应回退")
        else:
            opportunity.status = OpportunityStatus.VALIDATING
            _append_caveat(
                opportunity,
                f"端到端变化 {delta}% 未超过判定阈值 ±{min_effect * 100:.0f}%，"
                "收益可能被其他因素掩盖，需 kernel 级测量",
            )

    return result
