"""P2：阈值标定与覆盖层。"""

from __future__ import annotations

from pathlib import Path

import pytest

from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.diagnosis.thresholds import apply_overlay, load_overlay, write_overlay
from opt_discovery.experiment.calibration import (
    STATUS_CALIBRATED,
    STATUS_INSUFFICIENT,
    STATUS_NEEDS_BASELINE,
    STATUS_POLICY,
    calibrate,
)
from opt_discovery.schemas.evidence import ConfidenceLevel
from opt_discovery.schemas.metrics import Metric
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from opt_discovery.schemas.workload import WorkloadSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"


def _table(workload_id: str, records) -> OperatorTable:
    workload = WorkloadSpec.from_dict(
        {"workload_id": workload_id, "model": {"name": "m"}, "hardware": {"device": "d"}}
    )
    items = []
    for operator, total_us, extras in records:
        items.append(
            OperatorRecord(
                workload_id=workload_id,
                operator=operator,
                calls=10,
                total_time_us=Metric(total_us, "us"),
                task_type=TaskType.KERNEL,
                extra={name: Metric(value, "us") for name, value in extras.items()},
            )
        )
    return OperatorTable(workload=workload, records=items, meta=ParseMeta(parser="t"))


@pytest.fixture()
def tables():
    # 计算类：计算流水占比 0.9；搬运类：搬运占比 0.8（各给足够样本）
    compute = [
        _table(
            f"wl-c{i}",
            [("MatMulV2", 1000.0, {"aicore_time_us": 900.0, "mte2_time_us": 100.0, "mac_time_us": 700.0})],
        )
        for i in range(3)
    ]
    memory = [
        _table(
            f"wl-m{i}",
            [("Cast", 1000.0, {"mte2_time_us": 800.0, "aicore_time_us": 50.0})],
        )
        for i in range(3)
    ]
    merged = {}
    for table in compute + memory:
        merged[table.workload_id] = table
    return merged


def test_calibrated_thresholds_are_derived_from_samples(tables):
    report = calibrate(tables, RuleSet.load(RULES), min_samples=3)
    by_name = {item.name: item for item in report.proposals}

    saturated = by_name["compute_pipe_time_fraction_saturated"]
    assert saturated.status == STATUS_CALIBRATED
    assert saturated.proposed_value == pytest.approx(0.9)
    assert saturated.samples == 3

    mac = by_name["mac_pipe_time_fraction_high"]
    assert mac.status == STATUS_CALIBRATED
    assert mac.proposed_value == pytest.approx(0.7)

    memory_sat = by_name["memory_pipe_time_fraction_saturated"]
    assert memory_sat.status == STATUS_CALIBRATED
    assert memory_sat.proposed_value == pytest.approx(0.8)


def test_unmeasurable_thresholds_are_marked(tables):
    report = calibrate(tables, RuleSet.load(RULES), min_samples=3)
    by_name = {item.name: item for item in report.proposals}
    assert by_name["aicore_utilization_saturated"].status == STATUS_NEEDS_BASELINE
    assert by_name["bandwidth_utilization_saturated"].status == STATUS_NEEDS_BASELINE
    assert by_name["high_call_count"].status == STATUS_POLICY
    assert by_name["communication_share_threshold"].status == STATUS_POLICY


def test_insufficient_samples_are_reported_not_guessed():
    table = _table("wl-1", [("MatMulV2", 1000.0, {"aicore_time_us": 900.0})])
    report = calibrate({"wl-1": table}, RuleSet.load(RULES), min_samples=3)
    by_name = {item.name: item for item in report.proposals}
    item = by_name["compute_pipe_time_fraction_saturated"]
    assert item.status == STATUS_INSUFFICIENT
    assert item.proposed_value is None
    assert report.to_thresholds() == {}


def test_overlay_round_trip_and_precedence(tmp_path, tables):
    ruleset = RuleSet.load(RULES)
    report = calibrate(tables, ruleset, min_samples=3)
    path = write_overlay(
        tmp_path / "cal.yaml", report.to_thresholds(), device_profile_id="ascend_910b"
    )
    overlay = load_overlay(path)
    assert overlay.device_profile_id == "ascend_910b"

    merged = apply_overlay(ruleset, overlay)
    target = merged.thresholds["compute_pipe_time_fraction_saturated"]
    assert target.provenance.value == "measured"
    assert target.value == pytest.approx(0.9)
    # 未在覆盖文件里的阈值保持原样
    assert merged.thresholds["aicore_utilization_saturated"].provenance.value == "assumed"


def test_measured_threshold_lifts_rule_to_probable(tables):
    ruleset = apply_overlay(RuleSet.load(RULES), load_overlay(
        write_overlay("/tmp/_cal_test.yaml", calibrate(tables, RuleSet.load(RULES)).to_thresholds(),
                      device_profile_id="ascend_910b")
    ))
    table = _table("wl-x", [("MatMulV2", 1000.0, {"aicore_time_us": 950.0, "mte2_time_us": 10.0})])
    report = DiagnosisEngine(ruleset).diagnose_table(table)
    candidate = report.candidates()[0]
    assert candidate.level is ConfidenceLevel.PROBABLE
