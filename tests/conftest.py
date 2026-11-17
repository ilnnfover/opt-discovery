from __future__ import annotations

from pathlib import Path

import pytest

from opt_discovery.diagnosis.rules import (
    AppliesTo,
    Condition,
    Rule,
    RuleSet,
    Threshold,
)
from opt_discovery.schemas.bottleneck import BottleneckType
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, TaskType
from opt_discovery.schemas.workload import (
    ExecutionMode,
    ExecutionSpec,
    HardwareSpec,
    InputSpec,
    ModelSpec,
    Phase,
    WorkloadSpec,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workload() -> WorkloadSpec:
    return WorkloadSpec(
        workload_id="qwen35-prefill-graph",
        model=ModelSpec(name="Qwen3.5-35B-A3B", dtype="bfloat16", is_moe=True),
        hardware=HardwareSpec(device="Ascend 910B", device_count=1, device_profile_id="ascend_910b"),
        execution=ExecutionSpec(phase=Phase.PREFILL, mode=ExecutionMode.GRAPH),
        input=InputSpec(batch_size=None, input_length=None, output_length=None),
    )


@pytest.fixture
def matmul_record() -> OperatorRecord:
    """Synthetic record: compute pipe dominant, small memory pipe."""
    return OperatorRecord(
        workload_id="qwen35-prefill-graph",
        operator="MatMul",
        calls=64,
        total_time_us=Metric(24000, "us"),
        device_time_us=Metric(24000, "us"),
        shape=(64, 4096, 4096),
        dtype="bfloat16",
        task_type=TaskType.KERNEL,
        extra={
            "aicore_time_us": Metric(22000, "us"),
            "mac_time_us": Metric(19000, "us"),
            "mte2_time_us": Metric(1000, "us"),
        },
    )


def make_threshold(name: str, value: float, unit: str, provenance: Provenance) -> Threshold:
    return Threshold(
        name=name,
        value=value,
        unit=unit,
        provenance=provenance,
        basis="test fixture",
    )


def compute_rule() -> Rule:
    return Rule(
        id="compute_bound.pipe_dominant",
        bottleneck=BottleneckType.COMPUTE_BOUND,
        description="test rule",
        required=(Condition(fact="compute_pipe_time_fraction", op=">=", threshold="pipe_saturated"),),
        supporting=(
            Condition(fact="memory_pipe_time_fraction", op="<=", threshold="pipe_ceiling"),
        ),
        suggested_actions=("check tiling",),
        caveats=("time fraction is not utilization",),
        applies_to=AppliesTo(task_types=(TaskType.KERNEL,)),
    )


def build_ruleset(provenance: Provenance) -> RuleSet:
    thresholds = {
        "pipe_saturated": make_threshold("pipe_saturated", 0.85, "ratio", provenance),
        "pipe_ceiling": make_threshold("pipe_ceiling", 0.50, "ratio", provenance),
    }
    ruleset = RuleSet(
        version=1,
        device_profile_id="test_device",
        rules=(compute_rule(),),
        thresholds=thresholds,
        status="calibrated" if provenance is not Provenance.ASSUMED else "uncalibrated",
    )
    ruleset.validate()
    return ruleset
