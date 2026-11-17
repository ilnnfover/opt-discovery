"""Evidence and confidence model.

A diagnosis is only acceptable if every claim points at a fact, the fact's
provenance, and the threshold that was compared against.  :class:`MissingEvidence`
is a first-class output, not an error: "we cannot classify this operator yet"
is a valid and useful diagnosis in the MVP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .metrics import Provenance

__all__ = ["ConfidenceLevel", "EvidenceItem", "MissingEvidence", "LEVEL_CAP"]


class ConfidenceLevel(str, Enum):
    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    HYPOTHESIS = "hypothesis"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


#: upper bound of the numeric confidence for each level
LEVEL_CAP: dict[ConfidenceLevel, float] = {
    ConfidenceLevel.CONFIRMED: 0.95,
    ConfidenceLevel.PROBABLE: 0.80,
    ConfidenceLevel.HYPOTHESIS: 0.50,
    ConfidenceLevel.INSUFFICIENT_EVIDENCE: 0.0,
}


@dataclass(frozen=True)
class EvidenceItem:
    """One comparison that supported or contradicted a rule."""

    statement: str
    fact: str | None = None
    observed: float | None = None
    unit: str | None = None
    comparator: str | None = None
    threshold: float | None = None
    threshold_provenance: Provenance | None = None
    provenance: Provenance | None = None
    role: str = "required"  # "required" | "supporting"
    satisfied: bool = True

    def __post_init__(self) -> None:
        for name in ("provenance", "threshold_provenance"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Provenance):
                object.__setattr__(self, name, Provenance(str(value)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "fact": self.fact,
            "observed": self.observed,
            "unit": self.unit,
            "comparator": self.comparator,
            "threshold": self.threshold,
            "threshold_provenance": self.threshold_provenance.value
            if self.threshold_provenance
            else None,
            "provenance": self.provenance.value if self.provenance else None,
            "role": self.role,
            "satisfied": self.satisfied,
        }


@dataclass(frozen=True)
class MissingEvidence:
    """A fact (or metric) that would be needed to make progress."""

    name: str
    reason: str
    needed_for: str | None = None
    how_to_collect: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "reason": self.reason,
            "needed_for": self.needed_for,
            "how_to_collect": self.how_to_collect,
        }


@dataclass
class DiagnosisCandidate:
    """A non-exclusive bottleneck candidate produced by one rule."""

    rule_id: str
    bottleneck: str
    level: ConfidenceLevel
    confidence: float
    evidence: list[EvidenceItem] = field(default_factory=list)
    missing_evidence: list[MissingEvidence] = field(default_factory=list)
    suggested_actions: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    hypothesis_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "bottleneck": self.bottleneck,
            "level": self.level.value,
            "confidence": round(self.confidence, 4),
            "hypothesis_only": self.hypothesis_only,
            "evidence": [e.to_dict() for e in self.evidence],
            "missing_evidence": [m.to_dict() for m in self.missing_evidence],
            "suggested_actions": list(self.suggested_actions),
            "caveats": list(self.caveats),
        }
