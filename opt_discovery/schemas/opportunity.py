"""Opportunity schema -- the interface between Discovery and an optimizer agent.

Discovery produces opportunities; a Kernel Optimization Agent consumes them.
Both sides only depend on this file, so either side can be replaced.

Invariants enforced here:

* an opportunity always names the workloads it was derived from;
* ``observation`` must cite evidence, ``hypothesis`` must stay falsifiable;
* anything below ``confirmed`` must carry ``missing_evidence``;
* priority scores are explicit numbers with a stated basis, never implicit.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .bottleneck import BottleneckType
from .evidence import ConfidenceLevel, EvidenceItem, MissingEvidence

__all__ = [
    "OpportunityStatus",
    "PriorityScore",
    "ValidationPlan",
    "Opportunity",
    "OpportunitySchemaError",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1
_OPP_ID_RE = re.compile(r"^OPP-[A-Za-z0-9][A-Za-z0-9._-]*$")


class OpportunitySchemaError(ValueError):
    """Raised when an opportunity violates the contract."""


class OpportunityStatus(str, Enum):
    DISCOVERED = "discovered"
    TRIAGED = "triaged"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PriorityScore:
    """``Score = Impact * Feasibility * Confidence / Cost`` (all in [0, 1])."""

    impact: float
    feasibility: float
    confidence: float
    cost: float

    def __post_init__(self) -> None:
        for name in ("impact", "feasibility", "confidence", "cost"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise OpportunitySchemaError(f"{name} must be numeric")
            if not 0.0 <= float(value) <= 1.0:
                raise OpportunitySchemaError(f"{name} must be within [0, 1], got {value!r}")
        if float(self.cost) <= 0.0:
            raise OpportunitySchemaError("cost must be > 0 (it is a denominator)")

    @property
    def value(self) -> float:
        return (self.impact * self.feasibility * self.confidence) / self.cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "impact": self.impact,
            "feasibility": self.feasibility,
            "confidence": self.confidence,
            "cost": self.cost,
            "value": round(self.value, 4),
        }


@dataclass(frozen=True)
class ValidationPlan:
    """Three-level acceptance (Kernel -> Model -> Serving) plus correctness."""

    kernel: tuple[str, ...]
    model: tuple[str, ...]
    serving: tuple[str, ...]
    correctness: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.kernel:
            raise OpportunitySchemaError("validation_plan.kernel must not be empty")
        if not self.correctness:
            raise OpportunitySchemaError("validation_plan.correctness must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kernel": list(self.kernel),
            "model": list(self.model),
            "serving": list(self.serving),
            "correctness": list(self.correctness),
        }


@dataclass
class Opportunity:
    opportunity_id: str
    operator: str
    workload_ids: tuple[str, ...]
    observation: str
    hypothesis: str
    bottleneck: BottleneckType
    evidence_level: ConfidenceLevel
    evidence: list[EvidenceItem] = field(default_factory=list)
    missing_evidence: list[MissingEvidence] = field(default_factory=list)
    suggested_actions: tuple[str, ...] = ()
    validation_plan: ValidationPlan | None = None
    source_refs: tuple[str, ...] = ()
    status: OpportunityStatus = OpportunityStatus.DISCOVERED
    scores: PriorityScore | None = None
    score_basis: Mapping[str, str] = field(default_factory=dict)
    diagnosis_refs: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION
    #: Stable identity of this generated instance.  A validation bundle must
    #: cite it; an operator name or a local ``OPP-###`` id is not enough.
    #: Direct construction or :func:`opportunities_from_diagnosis` generates a
    #: fresh UUID.  An empty value means a historical record whose origin is
    #: unknown and which therefore cannot be promoted.
    instance_id: str | None = None
    #: True when the source diagnosis came from synthetic/self-test data.
    #: ``None`` means a historical artifact did not declare its provenance and
    #: must be treated as unknown rather than as a known real measurement.
    is_synthetic: bool | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _OPP_ID_RE.match(self.opportunity_id or ""):
            raise OpportunitySchemaError(
                f"opportunity_id {self.opportunity_id!r} must look like 'OPP-001'"
            )
        if not self.operator:
            raise OpportunitySchemaError("operator is required")
        if not self.workload_ids:
            raise OpportunitySchemaError("at least one workload_id is required")
        if not self.observation.strip():
            raise OpportunitySchemaError("observation must not be empty")
        if not self.hypothesis.strip():
            raise OpportunitySchemaError("hypothesis must not be empty")
        if not isinstance(self.bottleneck, BottleneckType):
            object.__setattr__(self, "bottleneck", BottleneckType(str(self.bottleneck)))
        if not isinstance(self.evidence_level, ConfidenceLevel):
            object.__setattr__(
                self, "evidence_level", ConfidenceLevel(str(self.evidence_level))
            )
        if not isinstance(self.status, OpportunityStatus):
            object.__setattr__(self, "status", OpportunityStatus(str(self.status)))
        if self.instance_id is None:
            object.__setattr__(self, "instance_id", uuid.uuid4().hex)
        elif not isinstance(self.instance_id, str):
            raise OpportunitySchemaError("instance_id must be a string")
        else:
            object.__setattr__(self, "instance_id", self.instance_id.strip())
        if self.is_synthetic is not None and not isinstance(self.is_synthetic, bool):
            raise OpportunitySchemaError("is_synthetic must be a boolean or null")
        if not self.validation_plan:
            raise OpportunitySchemaError("validation_plan is required")
        needs_gap = self.evidence_level in (
            ConfidenceLevel.HYPOTHESIS,
            ConfidenceLevel.INSUFFICIENT_EVIDENCE,
        )
        if needs_gap and not self.missing_evidence:
            raise OpportunitySchemaError(
                f"evidence_level={self.evidence_level.value} requires missing_evidence "
                "so the gap is explicit"
            )
        if not self.created_at:
            object.__setattr__(
                self, "created_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )

    # -- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "opportunity_id": self.opportunity_id,
            "operator": self.operator,
            "workload_ids": list(self.workload_ids),
            "bottleneck": self.bottleneck.value,
            "evidence_level": self.evidence_level.value,
            "observation": self.observation,
            "hypothesis": self.hypothesis,
            "evidence": [e.to_dict() for e in self.evidence],
            "missing_evidence": [m.to_dict() for m in self.missing_evidence],
            "suggested_actions": list(self.suggested_actions),
            "validation_plan": self.validation_plan.to_dict() if self.validation_plan else None,
            "source_refs": list(self.source_refs),
            "status": self.status.value,
            "scores": self.scores.to_dict() if self.scores else None,
            "score_basis": dict(self.score_basis),
            "diagnosis_refs": list(self.diagnosis_refs),
            "caveats": list(self.caveats),
            "created_at": self.created_at,
            "instance_id": self.instance_id or "",
            "is_synthetic": self.is_synthetic,
        }
        # ``extra`` is for forward-compatible extension only.  It must never
        # shadow a retained field (e.g. an ``extra={"evidence_level": ...}``
        # expansion would otherwise rewrite the serialized evidence level).
        known = set(self.__dataclass_fields__)
        for key, value in self.extra.items():
            if key not in known and key not in payload:
                payload[key] = value
        return payload

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, verify_trust: bool = True
    ) -> "Opportunity":
        known = set(cls.__dataclass_fields__)
        plan = data.get("validation_plan")
        scores = data.get("scores")
        # Missing provenance stays unknown: old synthetic output must not be
        # silently re-labelled as a known real measurement.
        is_synthetic = data.get("is_synthetic", None)
        if is_synthetic is not None and not isinstance(is_synthetic, bool):
            raise OpportunitySchemaError("is_synthetic must be a boolean or null")
        raw_instance_id = data.get("instance_id")
        if raw_instance_id is None:
            # Historical record: readable, but not a promotable target.
            instance_id = ""
        elif isinstance(raw_instance_id, str):
            instance_id = raw_instance_id
        else:
            raise OpportunitySchemaError("instance_id must be a string")
        opportunity = cls(
            opportunity_id=data["opportunity_id"],
            operator=data["operator"],
            workload_ids=tuple(data.get("workload_ids") or ()),
            observation=data.get("observation", ""),
            hypothesis=data.get("hypothesis", ""),
            bottleneck=BottleneckType(str(data.get("bottleneck", "unknown"))),
            evidence_level=ConfidenceLevel(
                str(data.get("evidence_level", ConfidenceLevel.HYPOTHESIS.value))
            ),
            evidence=[EvidenceItem(**e) for e in data.get("evidence") or []],
            missing_evidence=[MissingEvidence(**m) for m in data.get("missing_evidence") or []],
            suggested_actions=tuple(data.get("suggested_actions") or ()),
            validation_plan=ValidationPlan(
                kernel=tuple(plan.get("kernel") or ()),
                model=tuple(plan.get("model") or ()),
                serving=tuple(plan.get("serving") or ()),
                correctness=tuple(plan.get("correctness") or ()),
            )
            if plan
            else None,
            source_refs=tuple(data.get("source_refs") or ()),
            status=OpportunityStatus(str(data.get("status", "discovered"))),
            scores=PriorityScore(**{k: scores[k] for k in ("impact", "feasibility", "confidence", "cost")})
            if scores
            else None,
            score_basis=dict(data.get("score_basis") or {}),
            diagnosis_refs=tuple(data.get("diagnosis_refs") or ()),
            caveats=tuple(data.get("caveats") or ()),
            created_at=data.get("created_at", ""),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            instance_id=instance_id,
            is_synthetic=is_synthetic,
            extra={k: v for k, v in data.items() if k not in known and k != "scores"},
        )
        if verify_trust and opportunity.evidence_level is ConfidenceLevel.CONFIRMED:
            # Late import keeps schemas.opportunity -> schemas.evidence_gate
            # one-directional (the gate imports Opportunity, not vice versa).
            from .evidence_gate import trusted_evidence_level

            trusted_evidence_level(opportunity, verify_artifacts=True)
        return opportunity


def sort_opportunities(items: Iterable[Opportunity]) -> list[Opportunity]:
    """Deterministic ordering: score desc, then impact desc, then id."""
    def key(opp: Opportunity) -> tuple[float, float, str]:
        score = opp.scores.value if opp.scores else 0.0
        impact = opp.scores.impact if opp.scores else 0.0
        return (-score, -impact, opp.opportunity_id)

    return sorted(items, key=key)


def write_opportunities(items: Sequence[Opportunity], path: str | Path) -> Path:
    from .evidence_gate import trusted_evidence_level

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A trusted writer never serialises an unverified `confirmed` as if it were
    # an ordinary level: the same schema gate downgrades and marks it first.
    for item in items:
        if item.evidence_level is ConfidenceLevel.CONFIRMED:
            trusted_evidence_level(item, verify_artifacts=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items),
        "opportunities": [o.to_dict() for o in items],
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def read_opportunities(
    path: str | Path, *, verify_trust: bool = True
) -> list[Opportunity]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = payload.get("opportunities") if isinstance(payload, dict) else payload
    return [
        Opportunity.from_dict(item, verify_trust=verify_trust) for item in raw or []
    ]
