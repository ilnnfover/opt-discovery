"""留出验证：标定阈值能否推广到没参与标定的数据（P2 收尾）。

做法：**留一 Workload 交叉验证**。

对每个 Workload w：
1. 用其余 3 组标定 → T_fold；
2. 只用 w 自己标定 → T_self（代表"这组数据的真实水位"）；
3. 比较 T_fold 与 T_self → 漂移；
4. 分别用「全量标定」与「T_fold」诊断 w，看候选与级别是否一致。

这样不用重新采集，就能回答"阈值是不是过拟合了这一批数据"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..diagnosis.engine import DiagnosisEngine
from ..diagnosis.rules import RuleSet
from ..diagnosis.thresholds import ThresholdOverlay, apply_overlay
from ..schemas.operator import OperatorTable
from .calibration import calibrate

__all__ = ["ThresholdDrift", "FoldResult", "ValidationReport", "validate_thresholds"]

STABLE = 0.10
DRIFT = 0.25
MIN_OVERLAP_PASS = 0.90
MIN_OVERLAP_WARN = 0.70


@dataclass(frozen=True)
class ThresholdDrift:
    name: str
    calibration_value: float
    fold_values: tuple[float, ...]
    holdout_values: tuple[float, ...]
    rel_drift: float | None
    status: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calibration_value": round(self.calibration_value, 4),
            "fold_values": [round(value, 4) for value in self.fold_values],
            "holdout_values": [round(value, 4) for value in self.holdout_values],
            "rel_drift": None if self.rel_drift is None else round(self.rel_drift, 4),
            "status": self.status,
            "note": self.note,
        }


@dataclass
class FoldResult:
    held_out: str
    calibrated_on: tuple[str, ...]
    threshold_count: int
    candidates_baseline: int = 0
    candidates_fold: int = 0
    rule_overlap: float = 1.0
    level_changes: tuple[str, ...] = ()
    only_baseline: tuple[str, ...] = ()
    only_fold: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "held_out": self.held_out,
            "calibrated_on": list(self.calibrated_on),
            "threshold_count": self.threshold_count,
            "candidates_baseline": self.candidates_baseline,
            "candidates_fold": self.candidates_fold,
            "rule_overlap": round(self.rule_overlap, 3),
            "level_changes": list(self.level_changes),
            "only_baseline": list(self.only_baseline),
            "only_fold": list(self.only_fold),
        }


@dataclass
class ValidationReport:
    generated_at: str
    source: str
    mode: str
    folds: tuple[FoldResult, ...] = ()
    drifts: tuple[ThresholdDrift, ...] = ()
    verdict: str = "pass"
    conclusion_stability: str = "pass"
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "source": self.source,
            "mode": self.mode,
            "verdict": self.verdict,
            "conclusion_stability": self.conclusion_stability,
            "unstable_thresholds": [item.name for item in self.drifts if item.status == "unstable"],
            "drifts": [item.to_dict() for item in self.drifts],
            "folds": [item.to_dict() for item in self.folds],
            "notes": list(self.notes),
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def _rel_drift(predicted: float, observed: float) -> float:
    if observed == 0:
        return float("inf") if predicted else 0.0
    return abs(predicted - observed) / abs(observed)


def _candidate_keys(report) -> dict[tuple[str, str], str]:
    keys: dict[tuple[str, str], str] = {}
    for operator_report in report.operators:
        for candidate in operator_report.candidates:
            keys[(operator_report.operator, candidate.rule_id)] = candidate.level.value
    return keys


def validate_thresholds(
    tables: Mapping[str, OperatorTable],
    ruleset: RuleSet,
    *,
    source: str = "",
    min_samples: int = 3,
    timelines: Mapping[str, Mapping[str, Any]] | None = None,
    validation_min_samples: int | None = None,
    device_profile: Mapping[str, Any] | None = None,
) -> ValidationReport:
    """``min_samples`` 用于全量标定（生产口径，默认较严）；
    ``validation_min_samples`` 只用于折内/留出组标定，默认更宽松——
    否则子集样本必然不足，验证会退化成"全部 insufficient"。

    ``device_profile`` 透传给标定与诊断（aicore_utilization_* 等档案标定项）。
    """
    if len(tables) < 3:
        raise ValueError(f"留一验证至少需要 3 个 Workload，当前 {len(tables)}")

    names = sorted(tables)
    fold_floor = min_samples if validation_min_samples is None else validation_min_samples
    full = calibrate(
        tables, ruleset, min_samples=min_samples, timelines=timelines,
        source=source, device_profile=device_profile,
    )
    full_values = {name: item.value for name, item in full.to_thresholds().items()}

    fold_values: dict[str, list[float]] = {}
    holdout_values: dict[str, list[float]] = {}
    folds: list[FoldResult] = []

    for held_out in names:
        others = {name: tables[name] for name in names if name != held_out}
        others_timelines = (
            {name: payload for name, payload in timelines.items() if name != held_out}
            if timelines
            else None
        )
        fold_cal = calibrate(
            others, ruleset, min_samples=fold_floor, timelines=others_timelines,
            device_profile=device_profile,
        )
        fold_thresholds = fold_cal.to_thresholds()
        for name, item in fold_thresholds.items():
            fold_values.setdefault(name, []).append(item.value)

        self_cal = calibrate(
            {held_out: tables[held_out]},
            ruleset,
            min_samples=fold_floor,
            timelines=(
                {held_out: timelines[held_out]}
                if timelines and held_out in timelines
                else None
            ),
            device_profile=device_profile,
        )
        for name, item in self_cal.to_thresholds().items():
            holdout_values.setdefault(name, []).append(item.value)

        baseline = _diagnose(tables[held_out], ruleset, full_values, device_profile)
        folded = _diagnose(
            tables[held_out], ruleset, {k: v.value for k, v in fold_thresholds.items()},
            device_profile,
        )
        base_keys, fold_keys = _candidate_keys(baseline), _candidate_keys(folded)
        union = set(base_keys) | set(fold_keys)
        overlap = len(set(base_keys) & set(fold_keys)) / len(union) if union else 1.0
        level_changes = tuple(
            sorted(
                f"{op}:{rule}"
                for (op, rule) in set(base_keys) & set(fold_keys)
                if base_keys[(op, rule)] != fold_keys[(op, rule)]
            )
        )
        folds.append(
            FoldResult(
                held_out=held_out,
                calibrated_on=tuple(sorted(others)),
                threshold_count=len(fold_thresholds),
                candidates_baseline=len(base_keys),
                candidates_fold=len(fold_keys),
                rule_overlap=overlap,
                level_changes=level_changes,
                only_baseline=tuple(
                    sorted(f"{a}:{b}" for a, b in set(base_keys) - set(fold_keys))
                )[:8],
                only_fold=tuple(
                    sorted(f"{a}:{b}" for a, b in set(fold_keys) - set(base_keys))
                )[:8],
            )
        )

    drifts: list[ThresholdDrift] = []
    for name in sorted(ruleset.thresholds):
        calibration_value = full_values.get(name)
        if calibration_value is None:
            drifts.append(
                ThresholdDrift(name, 0.0, (), (), None, "unmeasurable", "全量标定未得到该阈值")
            )
            continue
        folds_v = tuple(fold_values.get(name, ()))
        holds = tuple(holdout_values.get(name, ()))
        if not folds_v or not holds:
            drifts.append(
                ThresholdDrift(
                    name, calibration_value, folds_v, holds, None, "insufficient",
                    "留出集样本不足以单独标定该阈值",
                )
            )
            continue
        ratios = [
            _rel_drift(fold, hold) for fold, hold in zip(folds_v, holds) if hold is not None
        ]
        worst = max(ratios) if ratios else None
        if worst is None:
            status, note = "insufficient", "缺少可比数值"
        elif worst <= STABLE:
            status, note = "stable", f"最大漂移 {worst:.1%}（≤{STABLE:.0%}）"
        elif worst <= DRIFT:
            status, note = "drift", f"最大漂移 {worst:.1%}（>{STABLE:.0%}，≤{DRIFT:.0%}）"
        else:
            status, note = "unstable", f"最大漂移 {worst:.1%}（>{DRIFT:.0%}）"
        drifts.append(ThresholdDrift(name, calibration_value, folds_v, holds, worst, status, note))

    unstable = [item for item in drifts if item.status == "unstable"]
    # 结论稳定性单独判定：阈值漂移不等于结论翻转，两者必须分开说
    if any(fold.rule_overlap < MIN_OVERLAP_WARN for fold in folds):
        conclusion = "fail"
    elif any(fold.rule_overlap < MIN_OVERLAP_PASS for fold in folds) or any(
        fold.level_changes for fold in folds
    ):
        conclusion = "warn"
    else:
        conclusion = "pass"

    if conclusion == "fail":
        verdict = "fail"
    elif conclusion == "warn" or unstable or any(item.status == "drift" for item in drifts):
        verdict = "warn"
    else:
        verdict = "pass"

    notes = [
        f"留一验证：{len(folds)} 折，每折用 {len(names) - 1} 组标定、在留出组上诊断",
        f"结论稳定性={conclusion}（4 折候选集合与级别是否一致）；阈值稳定性单独列出，两者不可混用",
        f"阈值：稳定 {sum(1 for d in drifts if d.status == 'stable')}、"
        f"漂移 {sum(1 for d in drifts if d.status == 'drift')}、"
        f"不稳定 {len(unstable)}、"
        f"不可测/样本不足 {sum(1 for d in drifts if d.status in ('unmeasurable', 'insufficient'))}",
        "候选重叠 = 两套阈值下 (算子, 规则) 集合的 Jaccard；<90% 视为结论不稳",
    ]
    if unstable:
        notes.append(
            "不稳定项多来自 p75/p90 分位数在留出子集上的抽样波动（每折样本更少）："
            "增加 Workload 数量，或改用固定阈值并人工复核"
        )
    return ValidationReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=source,
        mode="leave_one_workload_out",
        folds=tuple(folds),
        drifts=tuple(drifts),
        verdict=verdict,
        conclusion_stability=conclusion,
        notes=tuple(notes),
    )


def _diagnose(
    table: OperatorTable,
    ruleset: RuleSet,
    values: Mapping[str, float],
    device_profile: Mapping[str, Any] | None = None,
):
    thresholds = {}
    for name, value in values.items():
        base = ruleset.thresholds.get(name)
        if base is None:
            continue
        thresholds[name] = type(base)(
            name=name,
            value=value,
            unit=base.unit,
            provenance="measured",
            basis="fold calibration",
        )
    merged = apply_overlay(
        ruleset,
        ThresholdOverlay(
            path="<fold>",
            device_profile_id=ruleset.device_profile_id,
            thresholds=thresholds,
        ),
    )
    return DiagnosisEngine(merged, device_profile=device_profile).diagnose_table(table)
