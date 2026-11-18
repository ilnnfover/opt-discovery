"""R11：Task Duration 可能小于 aicore_time，利用率分母必须用流水活跃时间。

历史错误（README 已知约束 11）：小算子 Task Duration 2.3 µs < aicore_time 19.7 µs，
用 Task Duration 作分母会得到 >100% 的利用率。
不变量 INV-01/INV-03：缺分母时不得伪造利用率，单位与分母语义必须一致。
"""

from __future__ import annotations

import json

import pytest

from _helpers import DEVICE_PROFILE
from opt_discovery.diagnosis.facts import build_facts
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, TaskType


def _profile() -> dict:
    return json.loads(DEVICE_PROFILE.read_text(encoding="utf-8"))


def _record(*, aicore_us: float | None) -> OperatorRecord:
    extra = {"flops": Metric(1e9, "flop", Provenance.MEASURED, "fixture:flops")}
    if aicore_us is not None:
        extra["aicore_time_us"] = Metric(aicore_us, "us", Provenance.MEASURED, "fixture")
    return OperatorRecord(
        workload_id="qwen35-prefill-eager",
        operator="MatMul",
        calls=1,
        total_time_us=Metric(2.3, "us", Provenance.MEASURED, "fixture"),
        task_type=TaskType.KERNEL,
        extra=extra,
    )


def test_utilization_uses_aicore_time_not_task_duration() -> None:
    profile = _profile()
    peak = float(profile["peak_bf16_tflops"])
    aicore_us = 19.7

    facts = build_facts(_record(aicore_us=aicore_us), device_profile=profile).facts
    utilization = facts["aicore_utilization"].value

    expected = 1e9 / (aicore_us * 1e-6) / (peak * 1e12)
    with_task_duration = 1e9 / (2.3 * 1e-6) / (peak * 1e12)

    assert utilization == pytest.approx(expected, rel=1e-9)
    assert 0.0 < utilization < 1.0
    # 负向：用 Task Duration 作分母会得到 >100% 的物理不可能值
    assert with_task_duration > 1.0
    assert utilization != pytest.approx(with_task_duration)


def test_missing_denominator_does_not_fabricate_utilization() -> None:
    facts = build_facts(_record(aicore_us=None), device_profile=_profile()).facts
    assert "aicore_utilization" not in facts
