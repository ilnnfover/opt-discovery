from __future__ import annotations

import pytest

from opt_discovery.schemas.metrics import Metric, Provenance, weakest_provenance
from opt_discovery.schemas.operator import (
    OperatorRecord,
    OperatorSchemaError,
    OperatorTable,
    ParseMeta,
    TaskType,
)
from opt_discovery.schemas.units import UnitError, to_bytes, to_ratio, to_us
from opt_discovery.schemas.workload import WorkloadError, WorkloadSpec


def test_time_unit_conversion():
    assert to_us(1500, "ns") == pytest.approx(1.5)
    assert to_us(2, "ms") == pytest.approx(2000)
    assert to_us(1, "\u00b5s") == pytest.approx(1.0)
    with pytest.raises(UnitError):
        to_us(1, "furlong")


def test_ratio_and_bytes_conversion():
    assert to_ratio(35, "%") == pytest.approx(0.35)
    assert to_ratio(0.4, "ratio") == pytest.approx(0.4)
    with pytest.raises(UnitError):
        to_ratio(1.4, "ratio")  # 明显不是比例列
    with pytest.raises(UnitError):
        to_ratio(0.5, "us")
    assert to_bytes(2, "MB") == 2 * 1024 * 1024


def test_metric_rejects_negative_and_non_finite():
    with pytest.raises(ValueError):
        Metric(-1, "us")
    with pytest.raises(ValueError):
        Metric(float("nan"), "us")


def test_weakest_provenance():
    assert (
        weakest_provenance([Provenance.MEASURED, Provenance.ASSUMED, Provenance.DERIVED])
        is Provenance.ASSUMED
    )


def test_operator_record_derives_avg_and_normalizes_unit():
    record = OperatorRecord(
        workload_id="wl",
        operator="Add",
        calls=4,
        total_time_us=Metric(2000, "ns"),
    )
    assert record.total_time_us is not None
    assert record.total_time_us.value == pytest.approx(2.0)
    avg = record.effective_avg_time_us
    assert avg is not None
    assert avg.value == pytest.approx(0.5)
    assert avg.provenance is Provenance.DERIVED


def test_operator_record_rejects_bad_calls():
    with pytest.raises(OperatorSchemaError):
        OperatorRecord(workload_id="wl", operator="Add", calls=-1)


def test_operator_table_rejects_workload_mismatch(workload):
    record = OperatorRecord(workload_id="other", operator="Add", calls=1)
    with pytest.raises(OperatorSchemaError):
        OperatorTable(workload=workload, records=[record], meta=ParseMeta(parser="t"))


def test_operator_table_total_time_returns_none_without_data(workload):
    table = OperatorTable(
        workload=workload,
        records=[OperatorRecord(workload_id=workload.workload_id, operator="Add", calls=3)],
        meta=ParseMeta(parser="t"),
    )
    assert table.total_operator_time_us() is None


def test_operator_table_json_roundtrip(workload, tmp_path):
    table = OperatorTable(
        workload=workload,
        records=[
            OperatorRecord(
                workload_id=workload.workload_id,
                operator="MatMul",
                calls=2,
                total_time_us=Metric(10, "us"),
                task_type=TaskType.KERNEL,
                shape=(2, 3),
            )
        ],
        meta=ParseMeta(parser="t", is_synthetic=True),
    )
    path = table.write_json(tmp_path / "table.json")
    restored = OperatorTable.read_json(path)
    assert restored.records[0].operator == "MatMul"
    assert restored.records[0].shape == (2, 3)
    assert restored.meta.is_synthetic is True
    assert restored.workload.workload_id == workload.workload_id


def test_workload_id_validation():
    with pytest.raises(WorkloadError):
        WorkloadSpec.from_dict(
            {
                "workload_id": "Invalid ID",
                "model": {"name": "m"},
                "hardware": {"device": "d"},
            }
        )


def test_workload_unverified_fields_and_comparability():
    base = {
        "workload_id": "wl-eager",
        "model": {"name": "Qwen3.5-35B-A3B", "dtype": "bfloat16"},
        "hardware": {"device": "Ascend 910B", "device_count": 1},
        "execution": {"phase": "prefill", "mode": "eager"},
        "input": {"batch_size": 1, "input_length": 1024, "output_length": 1},
    }
    eager = WorkloadSpec.from_dict(base)
    graph = WorkloadSpec.from_dict(
        {**base, "workload_id": "wl-graph", "execution": {"phase": "prefill", "mode": "graph"}}
    )
    ok, diffs = eager.comparable_with(graph)
    assert ok is True
    assert diffs == []
    assert eager.comparison_key == "prefill-eager"

    other = WorkloadSpec.from_dict({**base, "input": {"batch_size": 8, "input_length": 1024}})
    ok, diffs = eager.comparable_with(other)
    assert ok is False
    assert any("batch_size" in d for d in diffs)

    assert "environment.vllm_version" in eager.unverified_fields


def test_workload_yaml_roundtrip(tmp_path):
    payload = {
        "workload_id": "wl",
        "model": {"name": "m", "dtype": "bfloat16"},
        "hardware": {"device": "Ascend 910B"},
        "execution": {"phase": "decode", "mode": "graph", "graph_mode": "full_and_piecewise"},
        "input": {"batch_size": None},
    }
    path = tmp_path / "wl.yaml"
    import yaml

    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    spec = WorkloadSpec.from_yaml(path)
    restored = WorkloadSpec.from_dict(spec.to_dict())
    assert restored.workload_id == spec.workload_id
    assert restored.execution.graph_mode.value == "full_and_piecewise"
    assert restored.input.batch_size is None
