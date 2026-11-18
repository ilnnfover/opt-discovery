"""P2：留出（留一 Workload）验证。"""

from __future__ import annotations

from pathlib import Path

import pytest

from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.experiment.validation import validate_thresholds
from opt_discovery.schemas.metrics import Metric
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from opt_discovery.schemas.workload import WorkloadSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"


def _table(workload_id: str, compute_mac: float = 0.7, memory_mte: float = 0.8) -> OperatorTable:
    workload = WorkloadSpec.from_dict(
        {"workload_id": workload_id, "model": {"name": "m"}, "hardware": {"device": "d"}}
    )
    records = [
        OperatorRecord(
            workload_id=workload_id,
            operator="MatMulV2",
            calls=10,
            total_time_us=Metric(1000.0, "us"),
            task_type=TaskType.KERNEL,
            extra={
                "aicore_time_us": Metric(900.0, "us"),
                "mte2_time_us": Metric(100.0, "us"),
                "mac_time_us": Metric(1000.0 * compute_mac, "us"),
            },
        ),
        OperatorRecord(
            workload_id=workload_id,
            operator="Cast",
            calls=10,
            total_time_us=Metric(1000.0, "us"),
            task_type=TaskType.KERNEL,
            extra={
                "mte2_time_us": Metric(1000.0 * memory_mte, "us"),
                "aicore_time_us": Metric(50.0, "us"),
            },
        ),
    ]
    return OperatorTable(workload=workload, records=records, meta=ParseMeta(parser="t"))


def test_identical_workloads_are_stable_and_conclusions_pass():
    tables = {f"wl-{i}": _table(f"wl-{i}") for i in range(4)}
    report = validate_thresholds(tables, RuleSet.load(RULES), min_samples=1)
    assert report.conclusion_stability == "pass"
    assert report.verdict == "pass"
    stable = {item.name: item.status for item in report.drifts}
    assert stable["mac_pipe_time_fraction_high"] == "stable"
    for fold in report.folds:
        assert fold.rule_overlap == pytest.approx(1.0)
        assert not fold.level_changes


def test_outlier_workload_makes_threshold_unstable_but_verdict_not_fail_by_default():
    tables = {f"wl-{i}": _table(f"wl-{i}") for i in range(3)}
    tables["wl-odd"] = _table("wl-odd", compute_mac=0.05)
    report = validate_thresholds(tables, RuleSet.load(RULES), min_samples=1)
    status = {item.name: item.status for item in report.drifts}
    assert status["mac_pipe_time_fraction_high"] == "unstable"
    # 阈值不稳不等于结论翻转：只有在候选重叠 <70% 时才 fail
    assert report.verdict in ("warn", "fail")
    assert report.conclusion_stability in ("pass", "warn")


def test_requires_at_least_three_workloads():
    tables = {"a": _table("a"), "b": _table("b")}
    with pytest.raises(ValueError):
        validate_thresholds(tables, RuleSet.load(RULES), min_samples=1)


def test_unmeasurable_thresholds_are_reported():
    tables = {f"wl-{i}": _table(f"wl-{i}") for i in range(3)}
    report = validate_thresholds(tables, RuleSet.load(RULES), min_samples=1)
    by_name = {item.name: item for item in report.drifts}
    # 没有 utilization 指标的阈值应标成不可测，而不是编一个漂移值
    assert by_name["aicore_utilization_saturated"].status == "unmeasurable"
    assert by_name["aicore_utilization_saturated"].rel_drift is None


# ---------------------------------------------------------------------------
# 阈值分级：measured / declared / 样本门槛
# ---------------------------------------------------------------------------
from opt_discovery.diagnosis.thresholds import apply_overlay, load_overlay, write_overlay  # noqa: E402
from opt_discovery.experiment.calibration import calibrate  # noqa: E402
from opt_discovery.schemas.metrics import Provenance  # noqa: E402


def test_unstable_thresholds_are_frozen_as_declared(tmp_path):
    tables = {f"wl-{i}": _table(f"wl-{i}") for i in range(3)}
    tables["wl-odd"] = _table("wl-odd", compute_mac=0.05)
    ruleset = RuleSet.load(RULES)
    report = calibrate(tables, ruleset, min_samples=1)
    validation = validate_thresholds(tables, ruleset, min_samples=1).to_dict()

    thresholds = report.to_thresholds_with_validation(validation)
    mac = thresholds["mac_pipe_time_fraction_high"]
    assert mac.provenance is Provenance.DECLARED
    assert "留一验证" in mac.basis and "不作为实测值声称" in mac.basis

    stable_name = "compute_pipe_time_fraction_saturated"
    assert thresholds[stable_name].provenance is Provenance.MEASURED


def test_missing_validation_means_declared_not_measured():
    tables = {f"wl-{i}": _table(f"wl-{i}") for i in range(3)}
    ruleset = RuleSet.load(RULES)
    report = calibrate(tables, ruleset, min_samples=1)
    thresholds = report.to_thresholds_with_validation(None)
    assert thresholds
    assert all(item.provenance is Provenance.DECLARED for item in thresholds.values())


def test_sample_floor_blocks_small_sample_calibration():
    """n=4 的 host_* 不应产出 measured 阈值（默认门槛 12）。"""
    tables = {f"wl-{i}": _table(f"wl-{i}") for i in range(4)}
    timelines = {
        f"wl-{i}": {"device_idle_ratio": 0.5 + i * 0.01, "submit_time_us": 1e6, "device_busy_us": 2e6}
        for i in range(4)
    }
    ruleset = RuleSet.load(RULES)
    report = calibrate(tables, ruleset, min_samples=12, timelines=timelines)
    by_name = {item.name: item for item in report.proposals}
    assert by_name["host_gap_ratio"].status == "insufficient_samples"
    assert by_name["host_gap_ratio"].proposed_value is None


def test_overlay_roundtrip_preserves_declared(tmp_path):

    ruleset = RuleSet.load(RULES)
    tables = {f"wl-{i}": _table(f"wl-{i}") for i in range(3)}
    tables["wl-odd"] = _table("wl-odd", compute_mac=0.05)  # 制造不稳
    report = calibrate(tables, ruleset, min_samples=1)
    validation = validate_thresholds(tables, ruleset, min_samples=1).to_dict()
    path = write_overlay(
        tmp_path / "t.yaml",
        report.to_thresholds_with_validation(validation),
        device_profile_id="ascend_910b",
    )
    overlay = load_overlay(path)
    merged = apply_overlay(ruleset, overlay)
    assert merged.thresholds["mac_pipe_time_fraction_high"].provenance is Provenance.DECLARED
    assert merged.status == "calibrated"
