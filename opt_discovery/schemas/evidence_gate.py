"""Single evidence-promotion contract (AE-07).

The rule-based diagnosis pipeline can at most produce ``probable`` evidence.
``confirmed`` is only reachable through :func:`promote_evidence` when a
structured :class:`ValidationBundle` links two independent, valid measurements
(kernel + end-to-end) to the *same* opportunity, change, baseline/candidate and
input.

This module is deliberately part of :mod:`opt_discovery.schemas`: it depends on
the opportunity/evidence schemas only, so both the CLI, the experiment package
and the read-only report path can apply the same trust rules without importing
the mutable discovery/experiment layers.

The contract is intentionally local: a user-run benchmark or measurement tool
does not need a signature, an external validation service, a database or a
complex proof system.  It only needs to emit a structured bundle with explicit
binding fields and artifact references.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .evidence import LEVEL_CAP, ConfidenceLevel, MissingEvidence
from .opportunity import Opportunity, OpportunitySchemaError, PriorityScore

__all__ = [
    "AUDIT_KEY",
    "EvidenceGateError",
    "EvidenceSource",
    "ValidationScope",
    "ArtifactReference",
    "ValidationResult",
    "ValidationBundle",
    "PromotionDecision",
    "MIN_EFFECT",
    "MIN_SAMPLES",
    "evaluate_validation",
    "promote_evidence",
    "withdraw_evidence",
    "trusted_evidence_level",
    "trusted_level_for_mapping",
    "bundle_digest",
]

#: existing comparison policy: at least three rounds, at least 5% effect
MIN_SAMPLES = 3
MIN_EFFECT = 0.05

#: opportunity.extra key holding the machine-readable audit trail
AUDIT_KEY = "evidence_gate"
_SCHEMA_VERSION = 1
_LEVEL_ORDER: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.INSUFFICIENT_EVIDENCE: 0,
    ConfidenceLevel.HYPOTHESIS: 1,
    ConfidenceLevel.PROBABLE: 2,
    ConfidenceLevel.CONFIRMED: 3,
}
#: End-to-end scopes.  ``operator`` is a single-operator local experiment and
#: is deliberately *not* end-to-end: kernel + operator must stay at probable.
_E2E_SCOPES = frozenset(
    {
        "model",
        "serving",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceGateError(ValueError):
    """Raised when structured validation input violates the contract."""


class EvidenceSource(str, Enum):
    """Where a validation result came from.

    ``measured`` and ``manual`` are accepted for promotion: a human-run local
    tool is enough, no signature or external service is required.  ``synthetic``
    is for self-tests only; ``benchmark`` is a bare observation record;
    ``unknown`` is a missing declaration.  The last three never promote.
    """

    MEASURED = "measured"
    MANUAL = "manual"
    SYNTHETIC = "synthetic"
    BENCHMARK = "benchmark"
    UNKNOWN = "unknown"


class ValidationScope(str, Enum):
    """What one validation result actually measured."""

    KERNEL = "kernel"
    OPERATOR = "operator"
    MODEL = "model"
    SERVING = "serving"
    WORKLOAD = "workload"


def _text(value: Any, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise EvidenceGateError(f"{name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise EvidenceGateError(f"{name} must be a non-empty string")
    return text


def _bool(value: Any, name: str, *, optional: bool = False) -> bool | None:
    if value is None and optional:
        return None
    if not isinstance(value, bool):
        raise EvidenceGateError(f"{name} must be a boolean")
    return value


def _int(value: Any, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceGateError(f"{name} must be an integer")
    return value


def _number(value: Any, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceGateError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise EvidenceGateError(f"{name} must be a finite number")
    return number


def _min_effect(value: Any) -> float:
    """Validate the promotion threshold once, for evaluation and replay."""
    number = _number(value, "min_effect")
    assert number is not None
    if not 0.0 <= number <= 1.0:
        raise EvidenceGateError("min_effect must be within [0, 1]")
    return number


def _enum(value: Any, enum_cls: type[Enum], name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise EvidenceGateError(f"{name} must be one of {[item.value for item in enum_cls]}")
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise EvidenceGateError(
            f"{name} must be one of {[item.value for item in enum_cls]}, got {value!r}"
        ) from exc


def _mapping(value: Any, name: str, *, allow_empty: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceGateError(f"{name} must be a mapping")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise EvidenceGateError(f"{name} keys must be non-empty strings")
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (int, float)):
            if not math.isfinite(float(item)):
                raise EvidenceGateError(f"{name}.{key} must be finite")
            result[key] = item
        elif isinstance(item, str):
            result[key] = item
        elif item is None:
            result[key] = None
        else:
            raise EvidenceGateError(
                f"{name}.{key} must be a scalar value (str/int/float/bool/null)"
            )
    if not result and not allow_empty:
        raise EvidenceGateError(f"{name} must not be empty")
    return result


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise EvidenceGateError(f"{name} has unknown field(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class ArtifactReference:
    """A local artifact whose content must match ``sha256``.

    No signatures or external service are involved: the digest only proves that
    the promotion-time file is the same file a later reader sees.
    """

    name: str
    path: str
    sha256: str
    kind: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "artifact.name"))
        object.__setattr__(self, "path", _text(self.path, "artifact.path"))
        digest = _text(self.sha256, "artifact.sha256").lower()
        if not _SHA256_RE.match(digest):
            raise EvidenceGateError("artifact.sha256 must be a 64-character hex digest")
        object.__setattr__(self, "sha256", digest)
        if self.kind is not None:
            object.__setattr__(self, "kind", _text(self.kind, "artifact.kind"))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
        }
        if self.kind is not None:
            payload["kind"] = self.kind
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactReference":
        if not isinstance(data, Mapping):
            raise EvidenceGateError("artifact must be a mapping")
        _reject_unknown(data, {"name", "path", "sha256", "kind"}, "artifact")
        return cls(
            name=_text(data.get("name"), "artifact.name"),
            path=_text(data.get("path"), "artifact.path"),
            sha256=_text(data.get("sha256"), "artifact.sha256"),
            kind=_text(data["kind"], "artifact.kind") if data.get("kind") is not None else None,
        )

    def verify(self, root: str | Path | None = None) -> str | None:
        target = Path(self.path)
        if not target.is_absolute():
            target = Path(root or ".") / target
        try:
            if not target.is_file():
                return f"artifact_missing:{self.name}"
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            return f"artifact_unreadable:{self.name}"
        if digest.hexdigest() != self.sha256:
            return f"artifact_digest_mismatch:{self.name}"
        return None


@dataclass(frozen=True)
class ValidationResult:
    """One measured comparison inside a validation bundle."""

    scope: ValidationScope
    baseline: str
    candidate: str
    input: Mapping[str, Any]
    comparable: bool | None
    correctness: bool | None
    performance_passed: bool | None
    effect: float | None
    samples: int | None
    uncertainty: float | None
    artifacts: tuple[str, ...]
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _enum(self.scope, ValidationScope, "result.scope"))
        object.__setattr__(self, "baseline", _text(self.baseline, "result.baseline"))
        object.__setattr__(self, "candidate", _text(self.candidate, "result.candidate"))
        object.__setattr__(self, "input", _mapping(self.input, "result.input", allow_empty=False))
        object.__setattr__(
            self, "comparable", _bool(self.comparable, "result.comparable", optional=True)
        )
        object.__setattr__(
            self, "correctness", _bool(self.correctness, "result.correctness", optional=True)
        )
        object.__setattr__(
            self,
            "performance_passed",
            _bool(self.performance_passed, "result.performance_passed", optional=True),
        )
        object.__setattr__(
            self, "effect", _number(self.effect, "result.effect", optional=True)
        )
        object.__setattr__(
            self, "samples", _int(self.samples, "result.samples", optional=True)
        )
        object.__setattr__(
            self,
            "uncertainty",
            _number(self.uncertainty, "result.uncertainty", optional=True),
        )
        if self.uncertainty is not None and self.uncertainty < 0:
            raise EvidenceGateError("result.uncertainty must be non-negative")
        names = self.artifacts
        if isinstance(names, (str, bytes)) or not isinstance(names, (tuple, list)):
            raise EvidenceGateError("result.artifacts must be a list of artifact names")
        cleaned: list[str] = []
        for item in names:
            cleaned.append(_text(item, "result.artifacts[]"))
        if not cleaned:
            raise EvidenceGateError("result.artifacts must not be empty")
        object.__setattr__(self, "artifacts", tuple(cleaned))
        if self.notes:
            object.__setattr__(self, "notes", _text(self.notes, "result.notes"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "input": dict(self.input),
            "comparable": self.comparable,
            "correctness": self.correctness,
            "performance_passed": self.performance_passed,
            "effect": self.effect,
            "samples": self.samples,
            "uncertainty": self.uncertainty,
            "artifacts": list(self.artifacts),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValidationResult":
        if not isinstance(data, Mapping):
            raise EvidenceGateError("result must be a mapping")
        _reject_unknown(
            data,
            {
                "scope",
                "baseline",
                "candidate",
                "input",
                "comparable",
                "correctness",
                "performance_passed",
                "effect",
                "samples",
                "uncertainty",
                "artifacts",
                "notes",
            },
            "result",
        )
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, (list, tuple)):
            raise EvidenceGateError("result.artifacts must be a list")
        return cls(
            scope=_enum(data.get("scope"), ValidationScope, "result.scope"),
            baseline=_text(data.get("baseline"), "result.baseline"),
            candidate=_text(data.get("candidate"), "result.candidate"),
            input=_mapping(data.get("input"), "result.input", allow_empty=False),
            comparable=_bool(data.get("comparable"), "result.comparable", optional=True),
            correctness=_bool(data.get("correctness"), "result.correctness", optional=True),
            performance_passed=_bool(
                data.get("performance_passed"), "result.performance_passed", optional=True
            ),
            effect=_number(data.get("effect"), "result.effect", optional=True),
            samples=_int(data.get("samples"), "result.samples", optional=True),
            uncertainty=_number(data.get("uncertainty"), "result.uncertainty", optional=True),
            artifacts=tuple(artifacts),
            notes=_text(data.get("notes", ""), "result.notes") if data.get("notes") else "",
        )


@dataclass(frozen=True)
class ValidationBundle:
    """Structured, machine-readable experiment validation input.

    Every binding field is explicit.  Nothing is inferred from a benchmark
    payload, a CLI path, ``--operator`` or ``--change``.
    """

    opportunity_id: str
    opportunity_instance_id: str
    operator: str
    workload_id: str
    change_id: str
    source: EvidenceSource
    baseline: str
    candidate: str
    input: Mapping[str, Any]
    results: tuple[ValidationResult, ...]
    artifacts: tuple[ArtifactReference, ...]
    notes: tuple[str, ...] = ()
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "opportunity_id", _text(self.opportunity_id, "opportunity_id")
        )
        object.__setattr__(
            self,
            "opportunity_instance_id",
            _text(self.opportunity_instance_id, "opportunity_instance_id"),
        )
        object.__setattr__(self, "operator", _text(self.operator, "operator"))
        object.__setattr__(self, "workload_id", _text(self.workload_id, "workload_id"))
        object.__setattr__(self, "change_id", _text(self.change_id, "change_id"))
        object.__setattr__(self, "source", _enum(self.source, EvidenceSource, "source"))
        object.__setattr__(self, "baseline", _text(self.baseline, "baseline"))
        object.__setattr__(self, "candidate", _text(self.candidate, "candidate"))
        if self.baseline == self.candidate:
            raise EvidenceGateError("baseline and candidate must be different")
        object.__setattr__(
            self, "input", _mapping(self.input, "input", allow_empty=False)
        )
        results = self.results
        if isinstance(results, (str, bytes)) or not isinstance(results, (tuple, list)):
            raise EvidenceGateError("results must be a list")
        parsed_results = tuple(
            item if isinstance(item, ValidationResult) else ValidationResult.from_dict(item)
            for item in results
        )
        if not parsed_results:
            raise EvidenceGateError("results must not be empty")
        object.__setattr__(self, "results", parsed_results)
        artifacts = self.artifacts
        if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, (tuple, list)):
            raise EvidenceGateError("artifacts must be a list")
        parsed_artifacts = tuple(
            item if isinstance(item, ArtifactReference) else ArtifactReference.from_dict(item)
            for item in artifacts
        )
        names = [item.name for item in parsed_artifacts]
        if len(names) != len(set(names)):
            raise EvidenceGateError("artifact names must be unique")
        object.__setattr__(self, "artifacts", parsed_artifacts)
        notes = self.notes
        if isinstance(notes, str):
            notes = (notes,)
        object.__setattr__(self, "notes", tuple(_text(item, "notes[]") for item in notes))
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise EvidenceGateError("schema_version must be an integer")
        if self.schema_version != _SCHEMA_VERSION:
            raise EvidenceGateError(
                f"unsupported validation schema_version {self.schema_version!r}"
            )

    @property
    def artifact_index(self) -> dict[str, ArtifactReference]:
        return {item.name: item for item in self.artifacts}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "opportunity_id": self.opportunity_id,
            "opportunity_instance_id": self.opportunity_instance_id,
            "operator": self.operator,
            "workload_id": self.workload_id,
            "change_id": self.change_id,
            "source": self.source.value,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "input": dict(self.input),
            "results": [item.to_dict() for item in self.results],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValidationBundle":
        if not isinstance(data, Mapping):
            raise EvidenceGateError("validation bundle must be a mapping")
        _reject_unknown(
            data,
            {
                "schema_version",
                "opportunity_id",
                "opportunity_instance_id",
                "operator",
                "workload_id",
                "change_id",
                "source",
                "baseline",
                "candidate",
                "input",
                "results",
                "artifacts",
                "notes",
            },
            "validation bundle",
        )
        results = data.get("results")
        if not isinstance(results, (list, tuple)):
            raise EvidenceGateError("results must be a list")
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, (list, tuple)):
            raise EvidenceGateError("artifacts must be a list")
        notes = data.get("notes") or ()
        if isinstance(notes, str):
            notes = (notes,)
        return cls(
            schema_version=_int(data.get("schema_version", _SCHEMA_VERSION), "schema_version")
            or _SCHEMA_VERSION,
            opportunity_id=_text(data.get("opportunity_id"), "opportunity_id"),
            opportunity_instance_id=_text(
                data.get("opportunity_instance_id"), "opportunity_instance_id"
            ),
            operator=_text(data.get("operator"), "operator"),
            workload_id=_text(data.get("workload_id"), "workload_id"),
            change_id=_text(data.get("change_id"), "change_id"),
            source=_enum(data.get("source"), EvidenceSource, "source"),
            baseline=_text(data.get("baseline"), "baseline"),
            candidate=_text(data.get("candidate"), "candidate"),
            input=_mapping(data.get("input"), "input", allow_empty=False),
            results=tuple(
                item if isinstance(item, ValidationResult) else ValidationResult.from_dict(item)
                for item in results
            ),
            artifacts=tuple(
                item if isinstance(item, ArtifactReference) else ArtifactReference.from_dict(item)
                for item in artifacts
            ),
            notes=tuple(str(item) for item in notes),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "ValidationBundle":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EvidenceGateError(f"{path}: invalid JSON ({exc})") from exc
        return cls.from_dict(payload)


@dataclass(frozen=True)
class PromotionDecision:
    """Machine-readable outcome of one :func:`evaluate_validation` call."""

    allowed: bool
    promoted: bool
    current_level: ConfidenceLevel
    target_level: ConfidenceLevel
    change_id: str
    source: str
    reasons: tuple[str, ...] = ()
    valid_scopes: tuple[str, ...] = ()
    confirmed_ready: bool = False
    min_effect: float = MIN_EFFECT
    opportunity_id: str = ""
    opportunity_instance_id: str = ""
    bundle_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "promoted": self.promoted,
            "current_level": self.current_level.value,
            "target_level": self.target_level.value,
            "change_id": self.change_id,
            "source": self.source,
            "reasons": list(self.reasons),
            "valid_scopes": list(self.valid_scopes),
            "confirmed_ready": self.confirmed_ready,
            "min_effect": self.min_effect,
            "opportunity_id": self.opportunity_id,
            "opportunity_instance_id": self.opportunity_instance_id,
            "bundle_digest": self.bundle_digest,
        }


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bundle_digest(bundle: ValidationBundle) -> str:
    """Stable digest of the complete structured input (tamper evidence)."""
    return hashlib.sha256(_canonical(bundle.to_dict()).encode("utf-8")).hexdigest()


def _logical_digest(bundle: ValidationBundle) -> str:
    """Digest for duplicate detection.

    Artifact paths may be resolved to absolute paths when an audit record is
    written, so the same logical input can have a different :func:`bundle_digest`
    on a second call.  Duplicate promotion therefore compares name/digest/kind.
    """
    payload = bundle.to_dict()
    payload["artifacts"] = [
        {key: item[key] for key in ("name", "sha256", "kind") if key in item}
        for item in payload["artifacts"]
    ]
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _resolve_bundle_artifacts(
    bundle: ValidationBundle, root: str | Path | None
) -> ValidationBundle:
    """Record absolute artifact paths when the file exists.

    Keeping relative paths would make the audit depend on the reader's cwd; the
    first successful promotion resolves and records them instead.
    """
    base = Path(root) if root is not None else Path.cwd()
    resolved: list[ArtifactReference] = []
    changed = False
    for artifact in bundle.artifacts:
        target = Path(artifact.path)
        if not target.is_absolute():
            candidate = base / target
            if candidate.is_file():
                target = candidate.resolve()
        if str(target) != artifact.path:
            changed = True
            resolved.append(replace(artifact, path=str(target)))
        else:
            resolved.append(artifact)
    if not changed:
        return bundle
    return replace(bundle, artifacts=tuple(resolved))


def _audit(opportunity: Opportunity) -> dict[str, Any]:
    raw = opportunity.extra.get(AUDIT_KEY) if isinstance(opportunity.extra, Mapping) else None
    if not isinstance(raw, Mapping):
        return {"schema_version": _SCHEMA_VERSION, "validations": [], "withdrawals": []}
    audit = dict(raw)
    validations = audit.get("validations")
    withdrawals = audit.get("withdrawals")
    audit["schema_version"] = _SCHEMA_VERSION
    audit["validations"] = list(validations) if isinstance(validations, list) else []
    audit["withdrawals"] = list(withdrawals) if isinstance(withdrawals, list) else []
    return audit


def _set_audit(opportunity: Opportunity, audit: Mapping[str, Any]) -> None:
    opportunity.extra = {**dict(opportunity.extra), AUDIT_KEY: dict(audit)}


def _append_caveat(opportunity: Opportunity, text: str) -> None:
    if text in opportunity.caveats:
        return
    opportunity.caveats = tuple(opportunity.caveats) + (text,)


def _verification_reason(artifact: ArtifactReference, root: str | Path | None) -> str | None:
    return artifact.verify(root)


def _withdrawal_reasons(
    opportunity: Opportunity, bundle: ValidationBundle
) -> list[str]:
    """Reject reuse of a withdrawn change or withdrawn artifacts."""
    audit = _audit(opportunity)
    reasons: list[str] = []
    bundle_digests = {item.sha256 for item in bundle.artifacts}
    for entry in audit["withdrawals"]:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("change_id") == bundle.change_id:
            reasons.append("withdrawn_change")
        recorded = entry.get("artifact_digests")
        if isinstance(recorded, (list, tuple)):
            if bundle_digests.intersection(str(item) for item in recorded):
                reasons.append("withdrawn_artifact_reuse")
    for entry in audit["validations"]:
        if not isinstance(entry, Mapping) or not entry.get("withdrawn"):
            continue
        recorded = entry.get("artifact_digests")
        if isinstance(recorded, (list, tuple)):
            if bundle_digests.intersection(str(item) for item in recorded):
                reasons.append("withdrawn_artifact_reuse")
    # keep the reason readable but deterministic
    return sorted(set(reasons))


def _check_result(
    result: ValidationResult,
    bundle: ValidationBundle,
    artifact_index: Mapping[str, ArtifactReference],
    artifact_root: str | Path | None,
    *,
    verify_artifacts: bool,
    min_effect: float,
) -> tuple[list[str], list[str]]:
    """Return ``(structural_problems, non_upgrading_reasons)``.

    Structural problems mean the bundle as a whole must fail closed.  Negative
    performance is a valid experiment outcome that simply does not promote.
    """
    structural: list[str] = []
    negative: list[str] = []

    if result.baseline != bundle.baseline or result.candidate != bundle.candidate:
        structural.append("result_baseline_candidate_mismatch")
    # Typed equality: canonical JSON keeps ``true`` distinct from ``1`` and
    # ``1.0``, unlike Python dict equality.
    if _canonical(dict(result.input)) != _canonical(dict(bundle.input)):
        structural.append("result_input_mismatch")
    if result.comparable is not True:
        structural.append(
            "missing_comparability" if result.comparable is None else "not_comparable"
        )
    if result.correctness is not True:
        structural.append(
            "missing_correctness" if result.correctness is None else "correctness_failed"
        )
    if result.samples is None:
        structural.append("missing_samples")
    elif result.samples < MIN_SAMPLES:
        structural.append("insufficient_samples")
    if result.uncertainty is None:
        structural.append("missing_uncertainty")
    if result.effect is None:
        structural.append("missing_effect")

    for name in result.artifacts:
        artifact = artifact_index.get(name)
        if artifact is None:
            structural.append(f"unlinked_artifact:{name}")
            continue
        if verify_artifacts:
            reason = _verification_reason(artifact, artifact_root)
            if reason:
                structural.append(reason)

    if structural:
        return structural, negative

    if result.performance_passed is None:
        structural.append("missing_performance_result")
        return structural, negative

    if result.performance_passed is False:
        negative.append(f"{result.scope.value}_performance_not_improved")
        return structural, negative

    # performance_passed is True: the declared effect must be a strictly
    # positive improvement and clear the (validated) policy threshold.  A zero
    # threshold still never turns a zero gain into a benefit.
    assert result.effect is not None
    if result.effect <= 0:
        negative.append(f"{result.scope.value}_effect_not_positive")
        return structural, negative
    if result.effect < min_effect:
        structural.append("effect_below_threshold")
        return structural, negative

    if result.scope is ValidationScope.WORKLOAD:
        negative.append("workload_scope_not_attributable")

    return structural, negative


def _has_promoted_record(opportunity: Opportunity, digest: str) -> bool:
    audit = _audit(opportunity)
    for entry in audit["validations"]:
        if not isinstance(entry, Mapping) or entry.get("withdrawn"):
            continue
        if entry.get("logical_digest") != digest and entry.get("bundle_digest") != digest:
            continue
        decision = entry.get("decision")
        if isinstance(decision, Mapping) and decision.get("promoted") is True:
            return True
    return False


def evaluate_validation(
    opportunity: Opportunity,
    bundle: ValidationBundle,
    *,
    artifact_root: str | Path | None = None,
    min_effect: float = MIN_EFFECT,
    verify_artifacts: bool = True,
    for_replay: bool = False,
) -> PromotionDecision:
    """Apply the single evidence contract without mutating the opportunity."""
    policy = _min_effect(min_effect)
    current = opportunity.evidence_level
    reasons: list[str] = []

    if not isinstance(current, ConfidenceLevel):
        current = ConfidenceLevel(str(current))
    if opportunity.opportunity_id != bundle.opportunity_id:
        reasons.append("opportunity_id_mismatch")
    if not opportunity.instance_id:
        # Historical record: readable, but its origin is unknown, so no bundle
        # can legitimately claim to have measured this instance.
        reasons.append("historical_opportunity_source_unknown")
    elif opportunity.instance_id != bundle.opportunity_instance_id:
        reasons.append("opportunity_instance_mismatch")
    if opportunity.operator != bundle.operator:
        reasons.append("operator_mismatch")
    if bundle.workload_id not in opportunity.workload_ids:
        reasons.append("workload_mismatch")
    if opportunity.is_synthetic is None:
        reasons.append("unknown_opportunity_source")
    elif opportunity.is_synthetic is True:
        reasons.append("synthetic_opportunity")
    if bundle.source not in (EvidenceSource.MEASURED, EvidenceSource.MANUAL):
        reasons.append(f"source_not_verified:{bundle.source.value}")
    if not bundle.artifacts:
        reasons.append("missing_artifacts")

    reasons.extend(_withdrawal_reasons(opportunity, bundle))

    if reasons:
        return PromotionDecision(
            allowed=False,
            promoted=False,
            current_level=current,
            target_level=current,
            change_id=bundle.change_id,
            source=bundle.source.value,
            reasons=tuple(sorted(set(reasons))),
            min_effect=policy,
            opportunity_id=opportunity.opportunity_id,
            opportunity_instance_id=opportunity.instance_id or "",
            bundle_digest=_logical_digest(bundle),
        )

    artifact_index = bundle.artifact_index
    structural: list[str] = []
    negative: list[str] = []
    valid_results: list[ValidationResult] = []
    for index, result in enumerate(bundle.results):
        result_structural, result_negative = _check_result(
            result,
            bundle,
            artifact_index,
            artifact_root,
            verify_artifacts=verify_artifacts,
            min_effect=policy,
        )
        if result_structural:
            structural.extend(f"result[{index}]:{item}" for item in result_structural)
            continue
        negative.extend(f"result[{index}]:{item}" for item in result_negative)
        if (
            not result_negative
            and result.performance_passed is True
            and result.scope is not ValidationScope.WORKLOAD
        ):
            valid_results.append(result)

    if structural:
        return PromotionDecision(
            allowed=False,
            promoted=False,
            current_level=current,
            target_level=current,
            change_id=bundle.change_id,
            source=bundle.source.value,
            reasons=tuple(sorted(set(reasons + structural + negative))),
            min_effect=policy,
            opportunity_id=opportunity.opportunity_id,
            opportunity_instance_id=opportunity.instance_id or "",
            bundle_digest=_logical_digest(bundle),
        )

    if not valid_results:
        reasons.extend(negative or ["no_performance_gain"])
        return PromotionDecision(
            allowed=False,
            promoted=False,
            current_level=current,
            target_level=current,
            change_id=bundle.change_id,
            source=bundle.source.value,
            reasons=tuple(sorted(set(reasons))),
            min_effect=policy,
            opportunity_id=opportunity.opportunity_id,
            opportunity_instance_id=opportunity.instance_id or "",
            bundle_digest=_logical_digest(bundle),
        )

    scopes = {result.scope for result in valid_results}
    kernel_results = [result for result in valid_results if result.scope is ValidationScope.KERNEL]
    e2e_results = [
        result for result in valid_results if result.scope.value in _E2E_SCOPES
    ]

    kernel_artifacts: set[str] = set()
    for result in kernel_results:
        kernel_artifacts.update(
            artifact_index[name].sha256 for name in result.artifacts if name in artifact_index
        )
    e2e_artifacts: set[str] = set()
    for result in e2e_results:
        e2e_artifacts.update(
            artifact_index[name].sha256 for name in result.artifacts if name in artifact_index
        )

    independent = bool(kernel_artifacts) and bool(e2e_artifacts) and not (
        kernel_artifacts & e2e_artifacts
    )
    if kernel_artifacts and e2e_artifacts and not independent:
        negative.append("shared_evidence_not_independent")

    confirmed_ready = bool(kernel_results) and bool(e2e_results) and independent
    if confirmed_ready:
        # Two independent scopes are not a "single experiment"; once the
        # contract is satisfied the opportunity may reach confirmed directly.
        target = ConfidenceLevel.CONFIRMED
    elif _LEVEL_ORDER[current] < _LEVEL_ORDER[ConfidenceLevel.PROBABLE]:
        target_order = min(
            _LEVEL_ORDER[current] + 1, _LEVEL_ORDER[ConfidenceLevel.PROBABLE]
        )
        target = next(
            level for level, order in _LEVEL_ORDER.items() if order == target_order
        )
    else:
        target = current
    if not confirmed_ready and bool(kernel_results) != bool(e2e_results):
        negative.append("single_scope_capped_at_probable")

    # Re-submitting the same single-scope evidence must not climb one level per
    # call (insufficient -> hypothesis -> probable).  A genuine two-scope bundle
    # is unaffected and may confirm in one call.
    if (
        not for_replay
        and not confirmed_ready
        and _LEVEL_ORDER[target] > _LEVEL_ORDER[current]
        and _has_promoted_record(opportunity, _logical_digest(bundle))
    ):
        target = current
        negative.append("duplicate_validation_no_repeat_promotion")

    return PromotionDecision(
        allowed=True,
        promoted=_LEVEL_ORDER[target] > _LEVEL_ORDER[current],
        current_level=current,
        target_level=target,
        change_id=bundle.change_id,
        source=bundle.source.value,
        reasons=tuple(sorted(set(negative))),
        valid_scopes=tuple(sorted(scope.value for scope in scopes)),
        confirmed_ready=confirmed_ready,
        min_effect=policy,
        opportunity_id=opportunity.opportunity_id,
        opportunity_instance_id=opportunity.instance_id or "",
        bundle_digest=_logical_digest(bundle),
    )


def _apply_promotion(
    opportunity: Opportunity,
    validation: ValidationBundle,
    decision: PromotionDecision,
    *,
    artifact_root: str | Path | None,
    policy: float,
) -> Opportunity:
    """Record and apply a decision produced by :func:`evaluate_validation`.

    This is private on purpose: the public write entry must generate its own
    decision, so a caller cannot pair a valid decision with a different bundle
    or opportunity just because ``change_id`` happens to match.
    """
    # Strict binding only matters for a decision that takes effect.  A rejected
    # decision (wrong instance, historical origin, ...) is a legitimate outcome
    # for this opportunity and must still record why it did not promote; the
    # mismatch is already carried in ``decision.reasons``.
    if decision.allowed and (
        decision.opportunity_id != validation.opportunity_id
        or decision.opportunity_instance_id != validation.opportunity_instance_id
        or decision.source != validation.source.value
        or decision.bundle_digest != _logical_digest(validation)
        or decision.change_id != validation.change_id
    ):
        raise EvidenceGateError("decision does not belong to this validation bundle")
    policy = _min_effect(policy)

    recorded = _resolve_bundle_artifacts(validation, artifact_root)
    audit = _audit(opportunity)
    audit["validations"].append(
        {
            "bundle": recorded.to_dict(),
            "bundle_digest": bundle_digest(recorded),
            "logical_digest": _logical_digest(validation),
            "artifact_digests": sorted({item.sha256 for item in recorded.artifacts}),
            "policy": {"min_effect": policy},
            "decision": decision.to_dict(),
            "withdrawn": False,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _set_audit(opportunity, audit)
    if decision.allowed:
        # A fresh trusted validation supersedes an older downgrade marker.
        audit = _audit(opportunity)
        audit.pop("trust", None)
        _set_audit(opportunity, audit)

    if not decision.allowed:
        _append_caveat(
            opportunity,
            "证据未升级：" + (", ".join(decision.reasons) or "validation_rejected"),
        )
        return opportunity

    if decision.promoted:
        target_level = decision.target_level
        opportunity.evidence_level = target_level
        if opportunity.scores is not None:
            opportunity.scores = PriorityScore(
                impact=opportunity.scores.impact,
                feasibility=opportunity.scores.feasibility,
                confidence=round(LEVEL_CAP[target_level] * 0.9, 3),
                cost=opportunity.scores.cost,
            )
            opportunity.score_basis = {
                **dict(opportunity.score_basis),
                "confidence": f"结构化验证通过，提升到 {target_level.value}",
            }
        _append_caveat(
            opportunity,
            f"结构化验证通过（scope={','.join(decision.valid_scopes)}），"
            f"证据提升到 {target_level.value}",
        )
    return opportunity


def promote_evidence(
    opportunity: Opportunity,
    validation: ValidationBundle,
    *,
    artifact_root: str | Path | None = None,
    min_effect: float = MIN_EFFECT,
) -> Opportunity:
    """Promote evidence through the single contract and keep an audit trail.

    Direct Python calls and the CLI feedback path both end up here.  A blocked
    validation never lowers an existing conclusion; it records why it did not
    promote.  Use :func:`withdraw_evidence` for an explicit downgrade.
    """
    policy = _min_effect(min_effect)
    # Never build on an untrusted ``confirmed``: if the field was edited or the
    # audit no longer matches, consume it as ``probable`` first.
    trusted_evidence_level(opportunity, artifact_root=artifact_root)
    decision = evaluate_validation(
        opportunity,
        validation,
        artifact_root=artifact_root,
        min_effect=policy,
    )
    return _apply_promotion(
        opportunity,
        validation,
        decision,
        artifact_root=artifact_root,
        policy=policy,
    )


def withdraw_evidence(
    opportunity: Opportunity,
    *,
    reason: str,
    source: EvidenceSource | str,
    change_id: str | None = None,
    to_level: ConfidenceLevel | str | None = None,
) -> Opportunity:
    """Explicitly downgrade evidence while preserving the original record."""
    reason_text = _text(reason, "withdrawal.reason")
    source_value = _enum(source, EvidenceSource, "withdrawal.source")
    if source_value not in (EvidenceSource.MEASURED, EvidenceSource.MANUAL):
        raise EvidenceGateError("withdrawal.source must be measured or manual")
    if change_id is not None:
        change_id = _text(change_id, "withdrawal.change_id")

    current = opportunity.evidence_level
    if not isinstance(current, ConfidenceLevel):
        current = ConfidenceLevel(str(current))
    if to_level is None:
        target = (
            ConfidenceLevel.PROBABLE
            if current is ConfidenceLevel.CONFIRMED
            else ConfidenceLevel.HYPOTHESIS
        )
    else:
        target = _enum(to_level, ConfidenceLevel, "withdrawal.to_level")
    if _LEVEL_ORDER[target] >= _LEVEL_ORDER[current]:
        raise EvidenceGateError("withdrawal must lower the evidence level")

    audit = _audit(opportunity)
    matching: list[dict[str, Any]] = []
    for entry in audit["validations"]:
        if not isinstance(entry, dict):
            continue
        bundle = entry.get("bundle")
        if not isinstance(bundle, Mapping):
            continue
        if change_id is not None and bundle.get("change_id") != change_id:
            continue
        matching.append(entry)
        entry["withdrawn"] = True

    artifact_digests: set[str] = set()
    for entry in matching:
        recorded = entry.get("artifact_digests")
        if isinstance(recorded, (list, tuple)):
            artifact_digests.update(str(item) for item in recorded)

    audit["withdrawals"].append(
        {
            "previous_level": current.value,
            "target_level": target.value,
            "reason": reason_text,
            "source": source_value.value,
            "change_id": change_id,
            "artifact_digests": sorted(artifact_digests),
            "original_records": [
                {
                    "bundle": entry.get("bundle"),
                    "bundle_digest": entry.get("bundle_digest"),
                    "decision": entry.get("decision"),
                }
                for entry in matching
            ],
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _set_audit(opportunity, audit)
    target_level = target
    opportunity.evidence_level = target_level
    if target in (ConfidenceLevel.HYPOTHESIS, ConfidenceLevel.INSUFFICIENT_EVIDENCE):
        if not any(item.name == "evidence_withdrawn" for item in opportunity.missing_evidence):
            opportunity.missing_evidence = list(opportunity.missing_evidence) + [
                MissingEvidence(
                    name="evidence_withdrawn",
                    reason=reason_text,
                    needed_for="explicit evidence withdrawal",
                )
            ]
    _append_caveat(opportunity, f"证据已显式撤回至 {target.value}：{reason_text}")
    return opportunity


def _trusted_record(
    opportunity: Opportunity, *, verify_artifacts: bool, artifact_root: str | Path | None
) -> tuple[bool, tuple[str, ...]]:
    audit = _audit(opportunity)
    reasons: list[str] = []
    if audit["withdrawals"]:
        reasons.append("withdrawn_record_present")
    for entry in audit["validations"]:
        if not isinstance(entry, Mapping) or entry.get("withdrawn"):
            continue
        stored = entry.get("decision")
        if not isinstance(stored, Mapping):
            reasons.append("invalid_audit_decision")
            continue
        if stored.get("allowed") is not True or stored.get("target_level") != (
            ConfidenceLevel.CONFIRMED.value
        ):
            # A partial/probable validation cannot justify a confirmed level,
            # even if the opportunity's field was edited to say otherwise.
            reasons.append("audit_decision_not_confirmed")
            continue
        raw_bundle = entry.get("bundle")
        if not isinstance(raw_bundle, Mapping):
            reasons.append("invalid_audit_bundle")
            continue
        try:
            bundle = ValidationBundle.from_dict(raw_bundle)
        except (EvidenceGateError, TypeError, ValueError):
            reasons.append("invalid_audit_bundle")
            continue
        if entry.get("bundle_digest") != bundle_digest(bundle):
            reasons.append("audit_digest_mismatch")
            continue
        policy = MIN_EFFECT
        raw_policy = entry.get("policy")
        candidate_policy = None
        if isinstance(raw_policy, Mapping) and "min_effect" in raw_policy:
            candidate_policy = raw_policy["min_effect"]
        elif "min_effect" in stored:
            candidate_policy = stored["min_effect"]
        if candidate_policy is not None:
            try:
                policy = _min_effect(candidate_policy)
            except EvidenceGateError:
                reasons.append("invalid_audit_policy")
                continue
        decision = evaluate_validation(
            opportunity,
            bundle,
            artifact_root=artifact_root,
            verify_artifacts=verify_artifacts,
            min_effect=policy,
            for_replay=True,
        )
        if decision.allowed and decision.confirmed_ready:
            return True, ()
        reasons.extend(decision.reasons or ("validation_no_longer_confirms",))
    return False, tuple(sorted(set(reasons or {"missing_validation_audit"})))


def trusted_evidence_level(
    opportunity: Opportunity,
    *,
    artifact_root: str | Path | None = None,
    verify_artifacts: bool = True,
    mark: bool = True,
) -> tuple[ConfidenceLevel, tuple[str, ...]]:
    """Return the level a consumer may trust.

    Only ``confirmed`` is gated.  A historical ``confirmed`` with no matching,
    non-withdrawn validation audit is conservatively downgraded to ``probable``
    and the untrusted state is kept in ``extra['evidence_gate']['trust']``.
    Artifacts referenced by the audit are verified by default: a missing or
    changed file makes the record untrusted rather than silently confirmed.
    """
    current = opportunity.evidence_level
    if not isinstance(current, ConfidenceLevel):
        current = ConfidenceLevel(str(current))
    if current is not ConfidenceLevel.CONFIRMED:
        return current, ()
    reasons: tuple[str, ...]
    if opportunity.is_synthetic is True:
        reasons = ("synthetic_opportunity",)
        trusted = False
    elif opportunity.is_synthetic is None:
        reasons = ("unknown_opportunity_source",)
        trusted = False
    else:
        trusted, reasons = _trusted_record(
            opportunity, verify_artifacts=verify_artifacts, artifact_root=artifact_root
        )
    if trusted:
        if mark:
            audit = _audit(opportunity)
            if audit.pop("trust", None) is not None:
                _set_audit(opportunity, audit)
        return ConfidenceLevel.CONFIRMED, ()
    if mark:
        audit = _audit(opportunity)
        audit["trust"] = {
            "trusted": False,
            "claimed_level": ConfidenceLevel.CONFIRMED.value,
            "effective_level": ConfidenceLevel.PROBABLE.value,
            "reasons": list(reasons),
        }
        _set_audit(opportunity, audit)
        target_level = ConfidenceLevel.PROBABLE
        opportunity.evidence_level = target_level
        _append_caveat(
            opportunity,
            "confirmed 缺少可核验的验证记录或证据已被撤回，已保守降级为 probable（不可信）："
            + ", ".join(reasons),
        )
    return ConfidenceLevel.PROBABLE, reasons


def trusted_level_for_mapping(item: Mapping[str, Any]) -> tuple[str, str | None]:
    """Read-only trust check for the report/raw-JSON path.

    The report must not import the mutable feedback module, so it uses this
    schema-level check.  It never raises for a malformed historical record; it
    returns a conservative level and an explanation instead.
    """
    level = item.get("evidence_level")
    if level != ConfidenceLevel.CONFIRMED.value:
        gate = item.get("evidence_gate")
        trust = gate.get("trust") if isinstance(gate, Mapping) else None
        if isinstance(trust, Mapping) and trust.get("trusted") is False:
            reasons = trust.get("reasons")
            detail = ", ".join(str(reason) for reason in reasons) if isinstance(reasons, list) else ""
            return (
                str(level),
                "该记录曾被判定为不可信，尚未重新验证"
                + (f"：{detail}" if detail else ""),
            )
        return str(level), None
    try:
        opportunity = Opportunity.from_dict(dict(item), verify_trust=False)
        trusted, reasons = trusted_evidence_level(
            opportunity, verify_artifacts=True, mark=False
        )
    except (KeyError, TypeError, ValueError, OpportunitySchemaError) as exc:
        return ConfidenceLevel.PROBABLE.value, f"confirmed 记录无法解析：{exc}"
    if trusted is ConfidenceLevel.CONFIRMED:
        return ConfidenceLevel.CONFIRMED.value, None
    return (
        ConfidenceLevel.PROBABLE.value,
        "confirmed 缺少可核验的验证记录，已保守降级：" + ", ".join(reasons),
    )
