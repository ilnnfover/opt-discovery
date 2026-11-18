"""R04：流水时间占比可以 >1，不得 clamp。

历史错误（README 已知约束 4）：跨实例/跨流水重叠会让 pipe 时间大于任务时长，
把占比截断到 1 会掩盖重叠事实。不变量 INV-06/INV-07：保留真实值并标注语义。
"""

from __future__ import annotations

import pytest

from opt_discovery.diagnosis.facts import build_facts
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, TaskType


def _record(**extra: float) -> OperatorRecord:
    return OperatorRecord(
        workload_id="qwen35-prefill-eager",
        operator="MatMul",
        calls=1,
        total_time_us=Metric(100.0, "us", Provenance.MEASURED, "fixture"),
        task_type=TaskType.KERNEL,
        extra={
            name: Metric(value, "us", Provenance.MEASURED, f"fixture:{name}")
            for name, value in extra.items()
        },
    )


def test_pipe_fraction_above_one_is_preserved() -> None:
    facts = build_facts(_record(aicore_time_us=150.0, vec_time_us=20.0)).facts

    assert facts["aicore_time_fraction"].value == pytest.approx(1.5)
    assert facts["compute_pipe_time_fraction"].value == pytest.approx(1.5)
    # 明确不是被 clamp 过的 1.0
    assert facts["compute_pipe_time_fraction"].value != pytest.approx(1.0)
    assert facts["compute_pipe_time_fraction"].source is not None


def test_single_pipe_fraction_cannot_be_clamped_either() -> None:
    facts = build_facts(_record(mte2_time_us=260.0)).facts
    assert facts["mte2_time_fraction"].value == pytest.approx(2.6)
