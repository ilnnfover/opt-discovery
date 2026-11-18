"""补充闸门：样本门槛（默认 12）与采集审计在缺字段时必须失败而非崩溃。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from _helpers import RULES
from opt_discovery.analyzer.collection_audit import audit_collection
from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.experiment.calibration import (
    STATUS_INSUFFICIENT,
    calibrate,
)
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from opt_discovery.schemas.workload import WorkloadSpec

DEVICE = {"peak_bf16_tflops": 282.5, "ai_core_count": 24, "roofline_flop_per_byte": 230.3}


def _table(workload_id: str) -> OperatorTable:
    workload = WorkloadSpec.from_dict(
        {"workload_id": workload_id, "model": {"name": "m"}, "hardware": {"device": "d"}}
    )
    def _record(operator: str, aicore_us: float) -> OperatorRecord:
        return OperatorRecord(
            workload_id=workload_id,
            operator=operator,
            calls=10,
            total_time_us=Metric(1000.0, "us", Provenance.MEASURED, "fixture"),
            task_type=TaskType.KERNEL,
            extra={
                "aicore_time_us": Metric(aicore_us, "us", Provenance.MEASURED, "fixture"),
                "mte2_time_us": Metric(100.0, "us", Provenance.MEASURED, "fixture"),
                "flops": Metric(1e9, "flop", Provenance.MEASURED, "fixture"),
                "block_num": Metric(24.0, "count", Provenance.MEASURED, "fixture"),
            },
        )

    # 计算类 + 搬运类各一条，覆盖 aicore_utilization_saturated / _ceiling / low_core_ratio
    records = [_record("MatMulV2", 900.0), _record("Cast", 50.0)]
    return OperatorTable(
        workload=workload, records=records, meta=ParseMeta(parser="fixture")
    )


def test_device_calibration_floor_is_twelve_samples() -> None:
    """设备档案派生的阈值必须走 12 样本门槛，4 个样本只能报 insufficient。"""
    tables = {f"wl-{index}": _table(f"wl-{index}") for index in range(4)}
    report = calibrate(tables, RuleSet.load(RULES), device_profile=DEVICE)
    by_name = {item.name: item for item in report.proposals}

    # 利用率：每表 1 条同类算子 × 4 表 = 4 个样本
    for name in ("aicore_utilization_saturated", "aicore_utilization_ceiling"):
        proposal = by_name[name]
        assert proposal.status == STATUS_INSUFFICIENT, name
        assert proposal.proposed_value is None, name
        assert proposal.samples == 4, name
        assert "12" in proposal.note, name
        assert name not in report.to_thresholds(), name

    # 活跃核占比：每表 2 条 kernel 记录 × 4 表 = 8 个样本
    core = by_name["low_core_ratio"]
    assert core.status == STATUS_INSUFFICIENT
    assert core.proposed_value is None
    assert core.samples == 8
    assert "12" in core.note
    assert "low_core_ratio" not in report.to_thresholds()


def _manifest(root: Path, run_id: str, *, sha: str | None) -> None:
    directory = root / f"20261115-{run_id}"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "fixed": {"max_num_seqs": 32},
        "profiling": {"switches": ["--aic-metrics=ArithmeticUtilization"]},
        "variables": {"mode": "eager"},
    }
    if sha:
        payload["contract_sha256"] = sha
    (directory / "collection_manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_missing_contract_sha_fails_instead_of_crashing(tmp_path: Path) -> None:
    """缺 contract_sha256 时曾会 sorted({None, str}) 抛 TypeError（基线审计发现）。"""
    _manifest(tmp_path, "prefill-eager", sha=None)
    _manifest(tmp_path, "decode-eager", sha="abc")
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        yaml.safe_dump({"runs": [{"id": "prefill-eager"}, {"id": "decode-eager"}]}),
        encoding="utf-8",
    )

    audit = audit_collection(tmp_path, contract_path=contract)
    item = next(entry for entry in audit.items if entry.name == "contract_sha256")

    assert item.ok is False
    assert "<missing>" in item.detail
    assert audit.verdict == "fail"
