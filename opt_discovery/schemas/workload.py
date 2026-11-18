"""Workload description schema.

A workload is the *context* of a profiling run.  It is deliberately separate
from operator records: request-level input/output lengths (e.g. ``1024 -> 1``)
must never be confused with per-operator shapes, which come from the profiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, TypeVar

import yaml

__all__ = [
    "Phase",
    "ExecutionMode",
    "GraphMode",
    "ModelSpec",
    "HardwareSpec",
    "ExecutionSpec",
    "InputSpec",
    "EnvironmentSpec",
    "WorkloadSpec",
    "WorkloadError",
]

_WORKLOAD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SCHEMA_VERSION = 1


class WorkloadError(ValueError):
    """Raised when a workload description is inconsistent."""


class Phase(str, Enum):
    PREFILL = "prefill"
    DECODE = "decode"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ExecutionMode(str, Enum):
    EAGER = "eager"
    GRAPH = "graph"
    UNKNOWN = "unknown"


class GraphMode(str, Enum):
    FULL = "full"
    PIECEWISE = "piecewise"
    FULL_AND_PIECEWISE = "full_and_piecewise"
    NONE = "none"
    UNKNOWN = "unknown"


_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum_or_unknown(enum_cls: type[_EnumT], raw: Any) -> _EnumT:
    """枚举解析失败的显式回退：所有 workload 枚举都带 ``unknown`` 成员。"""
    if raw is None:
        return enum_cls("unknown")
    try:
        return enum_cls(str(raw).strip().lower())
    except ValueError:
        return enum_cls("unknown")


def _optional_int(raw: Any, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise WorkloadError(f"{name} must be an integer or null, got {raw!r}")
    if raw < 0:
        raise WorkloadError(f"{name} must be non-negative, got {raw!r}")
    return raw


@dataclass(frozen=True)
class ModelSpec:
    name: str
    dtype: str | None = None
    is_moe: bool | None = None
    architecture: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ModelSpec":
        data = dict(data or {})
        if not data.get("name"):
            raise WorkloadError("model.name is required")
        known = {"name", "dtype", "is_moe", "architecture"}
        return cls(
            name=str(data["name"]),
            dtype=data.get("dtype"),
            is_moe=data.get("is_moe"),
            architecture=data.get("architecture"),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass(frozen=True)
class HardwareSpec:
    device: str
    device_count: int = 1
    device_profile_id: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.device:
            raise WorkloadError("hardware.device is required")
        if self.device_count < 1:
            raise WorkloadError("hardware.device_count must be >= 1")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "HardwareSpec":
        data = dict(data or {})
        if not data.get("device"):
            raise WorkloadError("hardware.device is required")
        known = {"device", "device_count", "device_profile_id"}
        return cls(
            device=str(data["device"]),
            device_count=int(data.get("device_count", 1)),
            device_profile_id=data.get("device_profile_id"),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass(frozen=True)
class ExecutionSpec:
    phase: Phase = Phase.UNKNOWN
    mode: ExecutionMode = ExecutionMode.UNKNOWN
    graph_mode: GraphMode = GraphMode.UNKNOWN
    npugraph_ex: bool | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ExecutionSpec":
        data = dict(data or {})
        known = {"phase", "mode", "graph_mode", "npugraph_ex"}
        return cls(
            phase=_enum_or_unknown(Phase, data.get("phase")),
            mode=_enum_or_unknown(ExecutionMode, data.get("mode")),
            graph_mode=_enum_or_unknown(GraphMode, data.get("graph_mode")),
            npugraph_ex=data.get("npugraph_ex"),
            extra={k: v for k, v in data.items() if k not in known},
        )

    @property
    def label(self) -> str:
        return f"{self.phase.value}/{self.mode.value}"


@dataclass(frozen=True)
class InputSpec:
    """Request-level input description.

    ``None`` means "not verified from the profiling launcher"; it does not mean
    zero.  These values are for cross-workload comparability, not for operator
    shape reconstruction.
    """

    batch_size: int | None = None
    input_length: int | None = None
    output_length: int | None = None
    num_requests: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("batch_size", "input_length", "output_length", "num_requests"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise WorkloadError(f"input.{name} must be non-negative")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "InputSpec":
        data = dict(data or {})
        known = {"batch_size", "input_length", "output_length", "num_requests"}
        return cls(
            batch_size=_optional_int(data.get("batch_size"), "input.batch_size"),
            input_length=_optional_int(data.get("input_length"), "input.input_length"),
            output_length=_optional_int(data.get("output_length"), "input.output_length"),
            num_requests=_optional_int(data.get("num_requests"), "input.num_requests"),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass(frozen=True)
class EnvironmentSpec:
    vllm_version: str | None = None
    vllm_ascend_commit: str | None = None
    torch_version: str | None = None
    cann_version: str | None = None
    profiler_version: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "EnvironmentSpec":
        data = dict(data or {})
        known = {
            "vllm_version",
            "vllm_ascend_commit",
            "torch_version",
            "cann_version",
            "profiler_version",
        }
        return cls(
            **{k: data.get(k) for k in known},
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass(frozen=True)
class WorkloadSpec:
    workload_id: str
    model: ModelSpec
    hardware: HardwareSpec
    execution: ExecutionSpec = field(default_factory=ExecutionSpec)
    input: InputSpec = field(default_factory=InputSpec)
    environment: EnvironmentSpec = field(default_factory=EnvironmentSpec)
    labels: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    verified: bool = False
    schema_version: int = SCHEMA_VERSION
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _WORKLOAD_ID_RE.match(self.workload_id or ""):
            raise WorkloadError(
                f"workload_id {self.workload_id!r} must match {_WORKLOAD_ID_RE.pattern} "
                "(lowercase, digits, '.', '_', '-')"
            )

    # -- conveniences ----------------------------------------------------
    @property
    def comparison_key(self) -> str:
        return f"{self.execution.phase.value}-{self.execution.mode.value}"

    @property
    def unverified_fields(self) -> tuple[str, ...]:
        """Fields that are ``None`` and therefore unconfirmed."""
        missing: list[str] = []
        for name in ("batch_size", "input_length", "output_length"):
            if getattr(self.input, name) is None:
                missing.append(f"input.{name}")
        for name in ("vllm_version", "vllm_ascend_commit", "torch_version"):
            if getattr(self.environment, name) is None:
                missing.append(f"environment.{name}")
        return tuple(missing)

    def comparable_with(self, other: "WorkloadSpec") -> tuple[bool, list[str]]:
        """Check whether two workloads differ only by execution mode/phase.

        Phase and mode are intentionally excluded: they are the comparison axis.
        """
        diffs: list[str] = []
        pairs = (
            ("model.name", self.model.name, other.model.name),
            ("model.dtype", self.model.dtype, other.model.dtype),
            ("hardware.device", self.hardware.device, other.hardware.device),
            ("hardware.device_count", self.hardware.device_count, other.hardware.device_count),
            ("input.batch_size", self.input.batch_size, other.input.batch_size),
            ("input.input_length", self.input.input_length, other.input.input_length),
        )
        for name, left, right in pairs:
            if left is None or right is None:
                if left != right:
                    diffs.append(f"{name}: {left!r} vs {right!r} (unverified)")
            elif left != right:
                diffs.append(f"{name}: {left!r} vs {right!r}")
        return (not diffs, diffs)

    # -- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workload_id": self.workload_id,
            "model": {"name": self.model.name, **{k: v for k, v in (
                ("dtype", self.model.dtype),
                ("is_moe", self.model.is_moe),
                ("architecture", self.model.architecture),
            ) if v is not None}, **dict(self.model.extra)},
            "hardware": {
                "device": self.hardware.device,
                "device_count": self.hardware.device_count,
                "device_profile_id": self.hardware.device_profile_id,
                **dict(self.hardware.extra),
            },
            "execution": {
                "phase": self.execution.phase.value,
                "mode": self.execution.mode.value,
                "graph_mode": self.execution.graph_mode.value,
                "npugraph_ex": self.execution.npugraph_ex,
                **dict(self.execution.extra),
            },
            "input": {
                "batch_size": self.input.batch_size,
                "input_length": self.input.input_length,
                "output_length": self.input.output_length,
                "num_requests": self.input.num_requests,
                **dict(self.input.extra),
            },
            "environment": {
                **{
                    k: getattr(self.environment, k)
                    for k in (
                        "vllm_version",
                        "vllm_ascend_commit",
                        "torch_version",
                        "cann_version",
                        "profiler_version",
                    )
                },
                **dict(self.environment.extra),
            },
            "labels": dict(self.labels),
            "notes": list(self.notes),
            "verified": self.verified,
            "unverified_fields": list(self.unverified_fields),
            **dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkloadSpec":
        data = dict(data)
        known = {
            "schema_version",
            "workload_id",
            "model",
            "hardware",
            "execution",
            "input",
            "environment",
            "labels",
            "notes",
            "verified",
        }
        notes = data.get("notes") or ()
        if isinstance(notes, str):
            notes = (notes,)
        return cls(
            workload_id=str(data.get("workload_id", "")),
            model=ModelSpec.from_dict(data.get("model")),
            hardware=HardwareSpec.from_dict(data.get("hardware")),
            execution=ExecutionSpec.from_dict(data.get("execution")),
            input=InputSpec.from_dict(data.get("input")),
            environment=EnvironmentSpec.from_dict(data.get("environment")),
            labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
            notes=tuple(str(n) for n in notes),
            verified=bool(data.get("verified", False)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            extra={k: v for k, v in data.items() if k not in known},
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "WorkloadSpec":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise WorkloadError(f"{path}: expected a mapping at top level")
        return cls.from_dict(payload)
