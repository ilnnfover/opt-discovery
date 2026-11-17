"""Measured-value wrapper with explicit provenance.

Every number that enters the discovery pipeline carries
``(value, unit, provenance, source)``.  ``None`` is used whenever a metric was
not observed -- there is no placeholder numeric default, so downstream rules
can distinguish "not measured" from "measured as 0".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .units import to_bytes, to_ratio, to_us

__all__ = ["Provenance", "Metric", "PROVENANCE_WEIGHT", "weakest_provenance"]


class Provenance(str, Enum):
    """Where a number comes from.  Ordered from strongest to weakest."""

    MEASURED = "measured"  # read directly from a profiler/timeline record
    DERIVED = "derived"  # computed from measured fields (e.g. total / calls)
    DECLARED = "declared"  # from a config file written by a human
    ASSUMED = "assumed"  # heuristic placeholder, must be calibrated


PROVENANCE_WEIGHT: dict[Provenance, float] = {
    Provenance.MEASURED: 1.0,
    Provenance.DERIVED: 0.8,
    Provenance.DECLARED: 0.4,
    Provenance.ASSUMED: 0.15,
}

_ORDER: tuple[Provenance, ...] = (
    Provenance.MEASURED,
    Provenance.DERIVED,
    Provenance.DECLARED,
    Provenance.ASSUMED,
)


def weakest_provenance(items: Any) -> Provenance:
    """Return the weakest provenance among ``items`` (Metric or Provenance)."""
    resolved = [i.provenance if isinstance(i, Metric) else i for i in items]
    worst = Provenance.MEASURED
    for prov in resolved:
        if _ORDER.index(prov) > _ORDER.index(worst):
            worst = prov
    return worst


@dataclass(frozen=True)
class Metric:
    """A single observed value.

    Attributes:
        value: non-negative finite number.
        unit: canonical unit string (``us``, ``bytes``, ``ratio``, ``count``).
        provenance: how the value was obtained.
        source: free-form origin, e.g. ``op_statistic.csv:Total Time(us)``.
    """

    value: float
    unit: str
    provenance: Provenance = Provenance.MEASURED
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            object.__setattr__(self, "provenance", Provenance(str(self.provenance)))
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError(f"Metric.value must be a number, got {self.value!r}")
        number = float(self.value)
        if not math.isfinite(number):
            raise ValueError(f"Metric.value must be finite, got {self.value!r}")
        if number < 0:
            raise ValueError(f"Metric.value must be non-negative, got {number!r}")
        object.__setattr__(self, "value", number)
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("Metric.unit must be a non-empty string")
        object.__setattr__(self, "unit", self.unit.strip())

    # -- conversions -----------------------------------------------------
    def as_time_us(self) -> "Metric":
        return Metric(to_us(self.value, self.unit), "us", self.provenance, self.source)

    def as_bytes(self) -> "Metric":
        return Metric(to_bytes(self.value, self.unit), "bytes", self.provenance, self.source)

    def as_ratio(self) -> "Metric":
        return Metric(to_ratio(self.value, self.unit), "ratio", self.provenance, self.source)

    @property
    def weight(self) -> float:
        return PROVENANCE_WEIGHT[self.provenance]

    # -- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "provenance": self.provenance.value,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Metric":
        return cls(
            value=data["value"],
            unit=data["unit"],
            provenance=Provenance(data.get("provenance", Provenance.MEASURED.value)),
            source=data.get("source"),
        )

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return f"{self.value:g} {self.unit} [{self.provenance.value}]"
