"""Unit handling for profiling data.

Design rule: never guess a unit.  Every conversion must be explicit; when a
unit is unknown we raise instead of silently treating a value as microseconds.
"""

from __future__ import annotations

import math

__all__ = [
    "UnitError",
    "normalize_time_unit",
    "to_us",
    "to_bytes",
    "to_ratio",
]


class UnitError(ValueError):
    """Raised when a unit is unknown or a value cannot be converted."""


# micro sign U+00B5 and greek mu U+03BC both appear in profiler exports
_TIME_TO_US: dict[str, float] = {
    "ns": 1e-3,
    "us": 1.0,
    "\u00b5s": 1.0,
    "\u03bcs": 1.0,
    "ms": 1e3,
    "s": 1e6,
}

_BYTE_TO_B: dict[str, float] = {
    "b": 1.0,
    "kb": 1024.0,
    "mb": 1024.0**2,
    "gb": 1024.0**3,
}

_RATIO_UNITS = {"ratio", "fraction", ""}
_PERCENT_UNITS = {"%", "percent", "pct"}


def _clean(unit: str) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise UnitError("unit must be a non-empty string")
    return unit.strip()


def normalize_time_unit(unit: str) -> str:
    """Return the canonical time unit (``ns``/``us``/``ms``/``s``)."""
    raw = _clean(unit).lower()
    if raw in _TIME_TO_US:
        return "us" if raw in ("us", "\u00b5s", "\u03bcs") else raw
    raise UnitError(f"unknown time unit: {unit!r}")


def to_us(value: float, unit: str = "us") -> float:
    """Convert ``value`` expressed in ``unit`` to microseconds."""
    raw = _clean(unit).lower()
    if raw not in _TIME_TO_US:
        raise UnitError(f"unknown time unit: {unit!r} (expected one of {sorted(_TIME_TO_US)})")
    number = _as_finite(value)
    return number * _TIME_TO_US[raw]


def to_bytes(value: float, unit: str = "B") -> float:
    """Convert a byte quantity to bytes (binary multiples)."""
    raw = _clean(unit).lower()
    if raw not in _BYTE_TO_B:
        raise UnitError(f"unknown byte unit: {unit!r} (expected one of {sorted(_BYTE_TO_B)})")
    return _as_finite(value) * _BYTE_TO_B[raw]


def to_ratio(value: float, unit: str = "ratio") -> float:
    """Normalize a utilization/ratio-like value to the ``[0, 1]`` interval.

    ``percent``/``%`` inputs are divided by 100.  Values outside ``[0, 1]`` are
    rejected because a ratio column with ``1.4`` almost always means the column
    was misidentified (e.g. a duration) and silently clamping would hide it.
    """
    raw = _clean(unit).lower()
    number = _as_finite(value)
    if raw in _RATIO_UNITS:
        result = number
    elif raw in _PERCENT_UNITS:
        result = number / 100.0
    else:
        raise UnitError(f"unknown ratio unit: {unit!r}")
    if not 0.0 <= result <= 1.0:
        raise UnitError(f"ratio out of [0, 1]: {value!r} with unit {unit!r}")
    return result


def _as_finite(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnitError(f"expected a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise UnitError(f"expected a finite number, got {value!r}")
    return number
