"""阈值覆盖层：把标定结果叠加到规则集上（P2）。

规则 YAML 里的阈值是"基线"（多为 ``assumed``）。标定产物是一份**设备级覆盖文件**：

    device_profile_id: ascend_910b
    thresholds:
      compute_pipe_time_fraction_saturated:
        value: 0.9
        unit: ratio
        provenance: measured
        basis: "p75 of compute-class operators (n=14)"

``apply_overlay`` 只替换同名阈值，其余保持原样，并记录来源，便于审计
"这条结论用的是哪版阈值"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..schemas.metrics import Provenance
from .rules import RuleSet, Threshold

__all__ = [
    "ThresholdOverlay",
    "load_overlay",
    "apply_overlay",
    "write_overlay",
    "overlay_summary",
]


@dataclass(frozen=True)
class ThresholdOverlay:
    path: str
    device_profile_id: str
    thresholds: Mapping[str, Threshold]
    generated_at: str = ""
    source: str = ""
    notes: tuple[str, ...] = ()

    @property
    def calibrated(self) -> int:
        return sum(1 for item in self.thresholds.values() if item.provenance.value != "assumed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_profile_id": self.device_profile_id,
            "generated_at": self.generated_at,
            "source": self.source,
            "threshold_count": len(self.thresholds),
            "calibrated": self.calibrated,
            "thresholds": {
                name: {
                    "value": item.value,
                    "unit": item.unit,
                    "provenance": item.provenance.value,
                    "basis": item.basis,
                }
                for name, item in sorted(self.thresholds.items())
            },
            "notes": list(self.notes),
        }


def load_overlay(path: str | Path) -> ThresholdOverlay:
    source = Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: 顶层必须是 mapping")
    items: dict[str, Threshold] = {}
    for name, spec in (raw.get("thresholds") or {}).items():
        if not isinstance(spec, Mapping) or "value" not in spec:
            raise ValueError(f"{path}: 阈值 {name!r} 缺少 value")
        items[str(name)] = Threshold(
            name=str(name),
            value=float(spec["value"]),
            unit=str(spec.get("unit", "ratio")),
            provenance=Provenance(str(spec.get("provenance", "assumed"))),
            basis=str(spec.get("basis", "")),
        )
    return ThresholdOverlay(
        path=str(source),
        device_profile_id=str(raw.get("device_profile_id", "")),
        thresholds=items,
        generated_at=str(raw.get("generated_at", "")),
        source=str(raw.get("source", "")),
        notes=tuple(str(item) for item in (raw.get("notes") or ())),
    )


def apply_overlay(ruleset: RuleSet, overlay: ThresholdOverlay) -> RuleSet:
    merged = dict(ruleset.thresholds)
    applied = 0
    for name, item in overlay.thresholds.items():
        if name in merged:
            merged[name] = item
            applied += 1
    note = f"阈值来源: {Path(overlay.path).name}（覆盖 {applied} 项）"
    return replace(
        ruleset,
        thresholds=merged,
        status="calibrated" if applied else ruleset.status,
        provenance_notes=(ruleset.provenance_notes + "; " + note).strip("; "),
    )


def write_overlay(
    path: str | Path,
    thresholds: Mapping[str, Threshold],
    *,
    device_profile_id: str,
    source: str = "",
    notes: tuple[str, ...] = (),
) -> Path:
    payload = {
        "device_profile_id": device_profile_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "notes": list(notes),
        "thresholds": {
            name: {
                "value": item.value,
                "unit": item.unit,
                "provenance": item.provenance.value,
                "basis": item.basis,
            }
            for name, item in sorted(thresholds.items())
        },
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def overlay_summary(ruleset: RuleSet) -> str:
    total = len(ruleset.thresholds)
    measured = sum(1 for item in ruleset.thresholds.values() if item.provenance.value == "measured")
    declared = sum(1 for item in ruleset.thresholds.values() if item.provenance.value == "declared")
    return f"阈值 {total} 项：measured={measured}, declared={declared}, assumed={total - measured - declared}"
