"""Synthetic fixture generator -- pipeline self-test only, NOT profiling data.

The numbers below are invented placeholders chosen to exercise specific rules
(launch-bound ratio, communication-bound, pipe-fraction).  Every table produced
here carries ``meta.is_synthetic = True`` and a warning, and reports built from
it must state that no real measurement is involved.
"""

from __future__ import annotations

from typing import Any

from ..schemas.metrics import Metric, Provenance
from ..schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from ..schemas.workload import WorkloadSpec
from .base import ParserCapabilities

__all__ = ["SCENARIOS", "build_synthetic_table", "SyntheticOperatorParser"]


#: scenario -> list of record templates.  Values are placeholders.
SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "rich": [
        {
            "operator": "MatMul",
            "kernel": "MatMulV2Kernel",
            "calls": 64,
            "total_time_us": 24000.0,
            "device_time_us": 24000.0,
            "shape": (64, 4096, 4096),
            "extra": {
                "aicore_time_us": 22000.0,
                "mac_time_us": 19000.0,
                "mte2_time_us": 3000.0,
            },
        },
        {
            "operator": "RmsNorm",
            "kernel": "RmsNormKernel",
            "calls": 128,
            "total_time_us": 1800.0,
            "device_time_us": 1800.0,
            "shape": (64, 1, 4096),
            "extra": {"vec_time_us": 1500.0, "mte2_time_us": 1100.0},
        },
        {
            # 占比刻意设成超过 communication_share_threshold，用于验证通信规则
            "operator": "AllReduce",
            "kernel": None,
            "task_type": TaskType.COMMUNICATION,
            "calls": 8,
            "total_time_us": 12000.0,
            "device_time_us": 12000.0,
            "extra": {"communication_time_us": 12000.0},
        },
        {
            "operator": "Add",
            "kernel": "AddKernel",
            "calls": 512,
            "total_time_us": 512.0,
            "device_time_us": 400.0,
            "shape": (64, 1, 4096),
            "extra": {"cpu_submit_time_us": 900.0},
        },
        {
            "operator": "DynamicQuant",
            "kernel": "DynamicQuantKernel",
            "calls": 256,
            "total_time_us": 3000.0,
            "device_time_us": 2500.0,
            "extra": {"sync_wait_us": 1200.0},
        },
    ],
    "sparse": [
        {
            "operator": "MatMul",
            "kernel": "MatMulV2Kernel",
            "calls": 64,
            "total_time_us": 24000.0,
            "shape": (64, 4096, 4096),
            "extra": {},
        },
        {
            "operator": "RmsNorm",
            "kernel": "RmsNormKernel",
            "calls": 128,
            "total_time_us": 1800.0,
            "shape": (64, 1, 4096),
            "extra": {},
        },
        {
            "operator": "Add",
            "kernel": "AddKernel",
            "calls": 512,
            "total_time_us": 512.0,
            "shape": (64, 1, 4096),
            "extra": {},
        },
    ],
}


def build_synthetic_table(scenario: str, workload: WorkloadSpec) -> OperatorTable:
    if scenario not in SCENARIOS:
        raise KeyError(f"unknown scenario {scenario!r}; available: {sorted(SCENARIOS)}")
    meta = ParseMeta(
        parser="synthetic",
        parser_version="0",
        source_files=(),
        is_synthetic=True,
        extra={"scenario": scenario},
    )
    meta.add_warning(
        "synthetic_data",
        "合成数据：仅用于验证 schema 与规则流水线，不代表任何真实 Profiling 指标",
    )
    records: list[OperatorRecord] = []
    for template in SCENARIOS[scenario]:
        calls = int(template["calls"])
        total = float(template["total_time_us"])
        device = template.get("device_time_us")
        extra = {
            name: Metric(float(value), _unit_of(name), Provenance.MEASURED, f"synthetic:{name}")
            for name, value in dict(template.get("extra") or {}).items()
        }
        records.append(
            OperatorRecord(
                workload_id=workload.workload_id,
                operator=str(template["operator"]),
                calls=calls,
                total_time_us=Metric(total, "us", Provenance.MEASURED, "synthetic:total"),
                avg_time_us=Metric(
                    total / max(calls, 1), "us", Provenance.DERIVED, "synthetic:total/calls"
                ),
                device_time_us=(
                    Metric(float(device), "us", Provenance.MEASURED, "synthetic:device")
                    if device is not None
                    else None
                ),
                kernel=template.get("kernel"),
                shape=template.get("shape"),
                dtype="bfloat16",
                task_type=template.get("task_type", TaskType.KERNEL),
                extra=extra,
                attributes={"raw_operator": str(template["operator"])},
                notes=("synthetic placeholder values",),
            )
        )
    return OperatorTable(workload=workload, records=records, meta=meta)


def _unit_of(name: str) -> str:
    if name.endswith("_us"):
        return "us"
    if name in ("aicore_utilization", "memory_bandwidth_utilization", "active_core_ratio"):
        return "ratio"
    if name.startswith("bytes"):
        return "bytes"
    return "count"


class SyntheticOperatorParser:
    """Same interface as real parsers, so the CLI can swap data sources."""

    name = "synthetic"
    version = "0"
    capabilities = ParserCapabilities(
        provides_timeline=False,
        provides_shape=True,
        provides_hardware_metrics=True,
        provides_source_location=False,
        requires_column_mapping=False,
    )

    def __init__(self, scenario: str = "rich") -> None:
        self.scenario = scenario

    def can_parse(self, path) -> bool:  # noqa: ANN001 - protocol signature
        return False

    def parse(self, path, workload: WorkloadSpec) -> OperatorTable:  # noqa: ANN001
        return build_synthetic_table(self.scenario, workload)
