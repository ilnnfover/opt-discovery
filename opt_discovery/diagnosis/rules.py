"""Declarative bottleneck rules.

Rules live in YAML so thresholds can be tuned per device without touching code.
Two guardrails keep the rule base honest:

1. a numeric literal in a condition must carry ``justification``; otherwise use a
   named threshold, which itself carries ``provenance`` and ``basis``;
2. a rule with no ``required`` condition is only allowed when it explicitly sets
   ``allow_hypothesis_only: true`` -- such rules can never produce a confirmed
   diagnosis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ..schemas.bottleneck import BottleneckType
from ..schemas.evidence import MissingEvidence
from ..schemas.metrics import Provenance
from ..schemas.operator import TaskType
from ..schemas.workload import ExecutionMode, Phase
from .facts import FACT_CATALOG

__all__ = [
    "RuleError",
    "Threshold",
    "Condition",
    "AppliesTo",
    "Rule",
    "RuleSet",
    "COMPARATORS",
    "BOOL_OPS",
    "OP_SYMBOLS",
]

_RULE_ID_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


class RuleError(ValueError):
    """Raised when a rule file is inconsistent or unverifiable."""


COMPARATORS = {
    ">": lambda observed, limit: observed > limit,
    ">=": lambda observed, limit: observed >= limit,
    "<": lambda observed, limit: observed < limit,
    "<=": lambda observed, limit: observed <= limit,
    "==": lambda observed, limit: observed == limit,
    "!=": lambda observed, limit: observed != limit,
}

BOOL_OPS = {"is_true", "is_false"}

OP_SYMBOLS = {**{key: key for key in COMPARATORS}, "is_true": "== True", "is_false": "== False"}


@dataclass(frozen=True)
class Threshold:
    name: str
    value: float
    unit: str
    provenance: Provenance = Provenance.ASSUMED
    basis: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            object.__setattr__(self, "provenance", Provenance(str(self.provenance)))
        if self.provenance is Provenance.MEASURED and not self.basis:
            raise RuleError(
                f"threshold {self.name!r} claims measured provenance but gives no basis"
            )

    def describe(self) -> str:
        return f"{self.name}={self.value:g} {self.unit} [{self.provenance.value}]"


@dataclass(frozen=True)
class Condition:
    fact: str
    op: str
    value: float | None = None
    threshold: str | None = None
    justification: str | None = None
    note: str | None = None

    @property
    def is_threshold_ref(self) -> bool:
        return self.threshold is not None


@dataclass(frozen=True)
class AppliesTo:
    """Which workload/task combinations a rule may be evaluated on."""

    phases: tuple[Phase, ...] = ()
    modes: tuple[ExecutionMode, ...] = ()
    task_types: tuple[TaskType, ...] = ()

    def matches(self, phase: Phase, mode: ExecutionMode, task_type: TaskType) -> bool:
        if self.phases and phase not in self.phases:
            return False
        if self.modes and mode not in self.modes:
            return False
        if self.task_types and task_type not in self.task_types:
            return False
        return True

    def describe(self) -> str:
        parts = []
        if self.phases:
            parts.append("phase=" + "/".join(p.value for p in self.phases))
        if self.modes:
            parts.append("mode=" + "/".join(m.value for m in self.modes))
        if self.task_types:
            parts.append("task_type=" + "/".join(t.value for t in self.task_types))
        return ", ".join(parts) if parts else "any"


@dataclass(frozen=True)
class Rule:
    id: str
    bottleneck: BottleneckType
    description: str
    required: tuple[Condition, ...] = ()
    supporting: tuple[Condition, ...] = ()
    suggested_actions: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    missing_evidence: tuple[MissingEvidence, ...] = ()
    confidence_cap: float | None = None
    allow_hypothesis_only: bool = False
    applies_to: AppliesTo = field(default_factory=AppliesTo)

    @property
    def is_hypothesis_only(self) -> bool:
        return not self.required

    def facts_referenced(self) -> tuple[str, ...]:
        seen: list[str] = []
        for condition in self.required + self.supporting:
            if condition.fact not in seen:
                seen.append(condition.fact)
        return tuple(seen)


@dataclass
class RuleSet:
    version: int
    device_profile_id: str
    rules: tuple[Rule, ...]
    thresholds: Mapping[str, Threshold] = field(default_factory=dict)
    status: str = "uncalibrated"
    provenance_notes: str = ""
    source_path: str | None = None

    # -- lookups ---------------------------------------------------------
    @property
    def identifier(self) -> str:
        return f"{self.device_profile_id}@v{self.version}"

    @property
    def is_calibrated(self) -> bool:
        return self.status == "calibrated"

    def rule(self, rule_id: str) -> Rule:
        for item in self.rules:
            if item.id == rule_id:
                return item
        raise KeyError(rule_id)

    def thresholds_used_by(self, rule: Rule) -> list[Threshold]:
        names = [c.threshold for c in rule.required + rule.supporting if c.threshold is not None]
        return [self.thresholds[n] for n in names if n in self.thresholds]

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "RuleSet":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise RuleError(f"{path}: expected a mapping at top level")
        thresholds = _parse_thresholds(raw.get("thresholds"))
        rules_raw = raw.get("rules")
        if not isinstance(rules_raw, list) or not rules_raw:
            raise RuleError(f"{path}: 'rules' must be a non-empty list")
        rules = tuple(_parse_rule(item, path) for item in rules_raw)
        ruleset = cls(
            version=int(raw.get("version", 1)),
            device_profile_id=str(raw.get("device_profile_id", "unknown")),
            rules=rules,
            thresholds=thresholds,
            status=str(raw.get("status", "uncalibrated")),
            provenance_notes=str(raw.get("provenance_notes", "")),
            source_path=str(path),
        )
        ruleset.validate()
        return ruleset

    # -- validation ------------------------------------------------------
    def validate(self) -> None:
        seen: set[str] = set()
        for rule in self.rules:
            if not _RULE_ID_RE.match(rule.id):
                raise RuleError(f"rule id {rule.id!r} must look like 'category.name'")
            if rule.id in seen:
                raise RuleError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
            if rule.bottleneck is BottleneckType.UNKNOWN:
                raise RuleError(f"{rule.id}: bottleneck must be a concrete category")
            if not rule.description:
                raise RuleError(f"{rule.id}: description is required")
            if rule.is_hypothesis_only and not rule.allow_hypothesis_only:
                raise RuleError(
                    f"{rule.id}: rule without 'required' conditions must set "
                    "allow_hypothesis_only: true"
                )
            if not rule.required and not rule.supporting:
                raise RuleError(f"{rule.id}: at least one condition is required")
            if rule.confidence_cap is not None and not 0.0 < rule.confidence_cap <= 1.0:
                raise RuleError(f"{rule.id}: confidence_cap must be within (0, 1]")
            for condition in rule.required + rule.supporting:
                self._validate_condition(rule.id, condition)

    def _validate_condition(self, rule_id: str, condition: Condition) -> None:
        if condition.op not in COMPARATORS and condition.op not in BOOL_OPS:
            raise RuleError(f"{rule_id}: unknown operator {condition.op!r}")
        if condition.fact not in FACT_CATALOG:
            raise RuleError(
                f"{rule_id}: unknown fact {condition.fact!r}; declare it in "
                "facts.FACT_CATALOG before referencing it"
            )
        spec = FACT_CATALOG[condition.fact]
        if condition.op in BOOL_OPS:
            if spec.unit != "bool":
                raise RuleError(
                    f"{rule_id}: boolean operator used on non-bool fact {condition.fact!r}"
                )
            if condition.value is not None or condition.threshold is not None:
                raise RuleError(
                    f"{rule_id}: {condition.fact!r} with {condition.op} must not define a limit"
                )
            return
        if condition.threshold is None:
            if condition.value is None:
                raise RuleError(f"{rule_id}: condition on {condition.fact!r} needs a limit")
            if not condition.justification:
                raise RuleError(
                    f"{rule_id}: literal limit on {condition.fact!r} requires "
                    "'justification'; prefer a named threshold with provenance"
                )
            return
        threshold = self.thresholds.get(condition.threshold)
        if threshold is None:
            raise RuleError(f"{rule_id}: unknown threshold {condition.threshold!r}")
        if threshold.unit != spec.unit:
            raise RuleError(
                f"{rule_id}: threshold {threshold.name!r} unit {threshold.unit!r} "
                f"!= fact {condition.fact!r} unit {spec.unit!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "device_profile_id": self.device_profile_id,
            "status": self.status,
            "provenance_notes": self.provenance_notes,
            "rule_count": len(self.rules),
            "rule_ids": [r.id for r in self.rules],
            "thresholds": {
                name: {
                    "value": t.value,
                    "unit": t.unit,
                    "provenance": t.provenance.value,
                    "basis": t.basis,
                }
                for name, t in self.thresholds.items()
            },
        }


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
def _parse_thresholds(raw: Any) -> dict[str, Threshold]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise RuleError("'thresholds' must be a mapping of name -> spec")
    result: dict[str, Threshold] = {}
    for name, spec in raw.items():
        if not isinstance(spec, Mapping) or "value" not in spec:
            raise RuleError(f"threshold {name!r} must be a mapping with 'value'")
        result[str(name)] = Threshold(
            name=str(name),
            value=float(spec["value"]),
            unit=str(spec.get("unit", "ratio")),
            provenance=Provenance(str(spec.get("provenance", Provenance.ASSUMED.value))),
            basis=str(spec.get("basis", "")),
        )
    return result


def _parse_condition(raw: Any, rule_id: str) -> Condition:
    if not isinstance(raw, Mapping) or "fact" not in raw or "op" not in raw:
        raise RuleError(f"{rule_id}: each condition needs 'fact' and 'op'")
    value = raw.get("value")
    return Condition(
        fact=str(raw["fact"]),
        op=str(raw["op"]).strip(),
        value=float(value) if value is not None else None,
        threshold=str(raw["threshold"]) if raw.get("threshold") is not None else None,
        justification=raw.get("justification"),
        note=raw.get("note"),
    )


def _parse_applies_to(raw: Any) -> AppliesTo:
    if not raw:
        return AppliesTo()
    if not isinstance(raw, Mapping):
        raise RuleError("'applies_to' must be a mapping")
    return AppliesTo(
        phases=tuple(_enum_values(Phase, raw.get("phase"))),
        modes=tuple(_enum_values(ExecutionMode, raw.get("mode"))),
        task_types=tuple(_enum_values(TaskType, raw.get("task_type"))),
    )


def _enum_values(enum_cls: Any, raw: Any) -> Iterable[Any]:
    if raw is None:
        return ()
    values = [raw] if isinstance(raw, (str, int)) else list(raw)
    resolved = []
    for value in values:
        try:
            resolved.append(enum_cls(str(value).strip().lower()))
        except ValueError as exc:
            raise RuleError(f"unknown {enum_cls.__name__} value {value!r}") from exc
    return resolved


def _parse_missing_evidence(raw: Any, rule_id: str) -> tuple[MissingEvidence, ...]:
    if not raw:
        return ()
    items: list[MissingEvidence] = []
    for entry in raw:
        if isinstance(entry, str):
            items.append(MissingEvidence(name=entry, reason="规则需要该指标", needed_for=rule_id))
            continue
        if not isinstance(entry, Mapping) or "fact" not in entry:
            raise RuleError(f"{rule_id}: missing_evidence entries need 'fact'")
        items.append(
            MissingEvidence(
                name=str(entry["fact"]),
                reason=str(entry.get("why", "规则需要该指标才能判定")),
                needed_for=rule_id,
                how_to_collect=entry.get("how"),
            )
        )
    return tuple(items)


def _parse_rule(raw: Any, path: str | Path) -> Rule:
    if not isinstance(raw, Mapping):
        raise RuleError(f"{path}: each rule must be a mapping")
    rule_id = str(raw.get("id", "")).strip()
    if not rule_id:
        raise RuleError(f"{path}: rule without 'id'")
    bottleneck_raw = str(raw.get("bottleneck", "unknown"))
    try:
        bottleneck = BottleneckType(bottleneck_raw)
    except ValueError as exc:
        raise RuleError(f"{rule_id}: unknown bottleneck {bottleneck_raw!r}") from exc
    required = tuple(_parse_condition(c, rule_id) for c in raw.get("required") or ())
    supporting = tuple(_parse_condition(c, rule_id) for c in raw.get("supporting") or ())
    cap = raw.get("confidence_cap")
    return Rule(
        id=rule_id,
        bottleneck=bottleneck,
        description=str(raw.get("description", "")),
        required=required,
        supporting=supporting,
        suggested_actions=tuple(str(a) for a in raw.get("suggested_actions") or ()),
        caveats=tuple(str(c) for c in raw.get("caveats") or ()),
        missing_evidence=_parse_missing_evidence(raw.get("missing_evidence"), rule_id),
        confidence_cap=float(cap) if cap is not None else None,
        allow_hypothesis_only=bool(raw.get("allow_hypothesis_only", False)),
        applies_to=_parse_applies_to(raw.get("applies_to")),
    )
