"""P0 交付物：CSV 汇总、benchmark 闭环、采集契约闸门。"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

from opt_discovery.analyzer.contract import compare_contracts
from opt_discovery.experiment.analyzer import compare_benchmarks
from opt_discovery.experiment.runner import parse_rounds, run_benchmark
from opt_discovery.report.summary_csv import write_summaries

STUB = """\
import time
print("PROCESS_START_ABS", time.time(), flush=True)
print("PROMPT_TOKENS 1 LOAD_SECONDS 3.0", flush=True)
print("PROFILE_START_ABS", time.time(), flush=True)
for i, d in enumerate([0.02, 0.02, 0.02]):
    t0 = time.time(); time.sleep(d)
    print("ACTIVE_ROUND", i, "start", t0, "end", time.time(), "out_tokens", 64, flush=True)
print("PROFILE_STOP_ABS", time.time(), flush=True)
"""


# ---------------------------------------------------------------------------
# P0-1: CSV 汇总
# ---------------------------------------------------------------------------
def _write_workload(root: Path, workload_id: str, *, total_us: float, candidates: int = 1) -> None:
    directory = root / workload_id
    directory.mkdir(parents=True)
    table = {
        "workload": {
            "workload_id": workload_id,
            "execution": {"phase": "decode", "mode": "eager", "graph_mode": "none"},
        },
        "records": [
            {
                "operator": "MatMulV2",
                "task_type": "kernel",
                "calls": 10,
                "total_time_us": {"value": total_us, "unit": "us"},
                "avg_time_us": {"value": total_us / 10, "unit": "us"},
                "max_time_us": {"value": total_us, "unit": "us"},
            }
        ],
        "meta": {
            "row_count": 10,
            "is_synthetic": False,
            "extra": {
                "window": {"rounds": [1, 2, 3, 4], "duration_us": 1_000_000.0},
                "window_quality": {"busy_ratio": 0.5, "degraded": False, "max_gap_us": 2000.0},
            },
        },
    }
    (directory / "operator_table.json").write_text(json.dumps(table), encoding="utf-8")
    (directory / "comm.json").write_text(
        json.dumps({"available": True, "mib_total": 100.0, "unique_tasks": 7}), encoding="utf-8"
    )
    (directory / "diagnosis.json").write_text(
        json.dumps(
            {
                "summary": {"candidates_total": candidates},
                "operators": [
                    {
                        "operator": "MatMulV2",
                        "candidates": [
                            {
                                "bottleneck": "compute_bound",
                                "level": "hypothesis",
                                "confidence": 0.4,
                                "rule_id": "compute_bound.pipe_dominant",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (directory / "opportunities.json").write_text(json.dumps({"count": 1}), encoding="utf-8")


def test_summary_csv_matches_artifacts(tmp_path):
    _write_workload(tmp_path, "wl-a", total_us=1000.0, candidates=3)
    workload_csv, operator_csv = write_summaries(tmp_path)

    rows = list(csv.DictReader(workload_csv.open(encoding="utf-8-sig")))
    assert len(rows) == 1
    assert rows[0]["workload_id"] == "wl-a"
    assert rows[0]["total_us"] == "1000.0"
    assert rows[0]["comm_mib"] == "100.0"
    assert rows[0]["candidates_total"] == "3"

    op_rows = list(csv.DictReader(operator_csv.open(encoding="utf-8-sig")))
    assert op_rows[0]["operator"] == "MatMulV2"
    assert op_rows[0]["share"] == "1.0"
    assert op_rows[0]["top_bottleneck"] == "compute_bound"


def test_summary_csv_leaves_missing_files_blank(tmp_path):
    _write_workload(tmp_path, "wl-a", total_us=1000.0)
    (tmp_path / "wl-a" / "comm.json").unlink()
    _, operator_csv = write_summaries(tmp_path)
    rows = list(csv.DictReader(operator_csv.open(encoding="utf-8-sig")))
    assert rows[0]["top_level"] == "hypothesis"
    workload_rows = list(
        csv.DictReader((tmp_path / "workload_summary.csv").open(encoding="utf-8-sig"))
    )
    assert workload_rows[0]["comm_mib"] == ""


# ---------------------------------------------------------------------------
# P0-2: benchmark 闭环
# ---------------------------------------------------------------------------
def test_parse_rounds_reads_harness_markers():
    text = (
        "PROMPT_TOKENS 1 LOAD_SECONDS 77.5\n"
        "ACTIVE_ROUND 0 start 1000.0 end 1017.4 out_tokens 64\n"
        "ACTIVE_ROUND 1 start 1017.4 end 1034.9 out_tokens 64\n"
    )
    rounds, prompt_tokens, load_s = parse_rounds(text)
    assert [item.index for item in rounds] == [0, 1]
    assert rounds[0].duration_s == pytest.approx(17.4)
    assert rounds[0].tokens_per_s == pytest.approx(64 / 17.4)
    assert prompt_tokens == 1
    assert load_s == pytest.approx(77.5)


def test_run_benchmark_executes_stub_and_writes_json(tmp_path):
    script = tmp_path / "stub.py"
    script.write_text(STUB, encoding="utf-8")
    run = run_benchmark(script, tmp_path / "out", label="stub", python=sys.executable)
    assert run.exit_code == 0
    assert len(run.rounds) == 3
    assert run.prompt_tokens == 1
    target = run.write_json(tmp_path / "out" / "benchmark.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["summary"]["rounds"] == 3
    assert payload["summary"]["latency_s"]["median"] == pytest.approx(0.02, abs=0.01)


def _bench(label: str, median: float, rounds: int = 5) -> dict:
    return {
        "label": label,
        "harness": "h.py",
        "prompt_tokens": 1,
        "profiled": False,
        "summary": {
            "rounds": rounds,
            "latency_s": {"median": median, "p90": median * 1.1, "max": median * 1.2},
            "tokens_per_s": {"median": 64 / median},
        },
    }


def test_compare_benchmarks_verdicts():
    faster = compare_benchmarks(_bench("a", 1.0), _bench("b", 0.5))
    assert faster.verdict == "b_faster"
    slower = compare_benchmarks(_bench("a", 1.0), _bench("b", 1.6))
    assert slower.verdict == "b_slower"
    same = compare_benchmarks(_bench("a", 1.0), _bench("b", 1.01))
    assert same.verdict == "no_significant_difference"
    few = compare_benchmarks(_bench("a", 1.0, rounds=2), _bench("b", 0.5))
    assert few.verdict == "insufficient_samples"


# ---------------------------------------------------------------------------
# P0-3: 采集契约闸门
# ---------------------------------------------------------------------------
def _table(workload_id: str, *, mode="eager", rounds=(1, 2, 3, 4), extras=None, bytes_total=None):
    input_spec = {"batch_size": 1, "input_length": 1, "output_length": 1}
    input_spec.update(extras or {})
    extra = {"window": {"rounds": list(rounds)}}
    if bytes_total is not None:
        extra["comm_volume"] = {"bytes_total": bytes_total, "unique_tasks": 10}
    return {
        "workload": {
            "workload_id": workload_id,
            "execution": {"phase": "decode", "mode": mode, "graph_mode": "none"},
            "hardware": {"device": "Ascend 910B", "device_count": 2},
            "input": input_spec,
        },
        "records": [{"operator": "MatMulV2", "calls": 10, "attributes": {"shape_count": "3"}}],
        "meta": {"extra": extra},
    }


def test_contract_passes_for_aligned_runs():
    report = compare_contracts(_table("a", bytes_total=1000.0), _table("b", mode="graph", bytes_total=1000.0))
    assert report.verdict in ("pass", "pass_with_warnings")


def test_contract_fails_on_phase_mismatch():
    a = _table("a")
    b = _table("b")
    b["workload"]["execution"]["phase"] = "prefill"
    report = compare_contracts(a, b)
    assert report.verdict == "incomparable"
    assert "execution.phase" in {item.name for item in report.failures}


def test_contract_fails_on_window_mismatch():
    report = compare_contracts(_table("a"), _table("b", rounds=(2, 3)))
    assert report.verdict == "incomparable"
    assert "window.rounds" in {item.name for item in report.failures}


def test_contract_warns_on_capture_sizes_and_comm_mismatch():
    a = _table("a", bytes_total=1000.0)
    b = _table("b", mode="graph", extras={"max_num_seqs": 32}, bytes_total=250.0)
    report = compare_contracts(a, b)
    names = {item.name for item in report.checks}
    assert "input.max_num_seqs" in names
    assert report.verdict == "incomparable"  # 字节量差 2 倍
    assert "comm.bytes" in {item.name for item in report.failures}
