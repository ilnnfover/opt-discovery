"""采集验收通过 CLI 核对磁盘产物、轮次和比较来源。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from opt_discovery.analyzer.acceptance import build_acceptance
from opt_discovery.cli import main
from opt_discovery.schemas.metrics import Metric
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta
from opt_discovery.schemas.workload import WorkloadSpec

OP_HEADER = "Device_id,Task Type,aic_mac_time(us),aic_cube_fops,cube_utilization(%)"
RUN_IDS = ("decode-eager", "decode-graph", "prefill-eager", "prefill-graph")


def _write_run(root, run_id, *, nested=False):
    index = RUN_IDS.index(run_id)
    directory = root / run_id
    export = directory / "export"
    sessions = export / "PROF" if nested else export
    mode = run_id.split("-")[1]
    output_length = 64 if run_id.startswith("decode") else 1
    ranks = []
    for rank in range(2):
        session = sessions / f"PROF_{rank}"
        (session / f"device_{rank}").mkdir(parents=True)
        output = session / "mindstudio_profiler_output"
        output.mkdir()
        slices = 2 if run_id == "decode-graph" else 1
        for index in range(slices):
            (output / f"op_summary_{index:04d}.csv").write_text(
                f"{OP_HEADER}\n{rank},AI_CORE,1,2,3\n", encoding="utf-8",
            )
        with sqlite3.connect(session / f"msprof_{rank}.db") as db:
            db.execute("CREATE TABLE test_export (id INTEGER)")
        ranks.append({
            "device": f"device_{rank}", "dir": session.name, "db": True,
            "db_size_mb": 1, "op_summary_csv": slices, "ok": True,
        })
    manifest = {
        "run_id": run_id,
        "started_at": f"2026-01-01T00:00:0{index}+00:00",
        "contract_sha256": "same-contract",
        "export_dir": "export",
        "exit_code": 0,
        "fixed": {"active_rounds": 2, "window_rounds": [0, 1], "tensor_parallel_size": 2},
        "variables": {
            "mode": mode, "graph_mode": "none" if mode == "eager" else "full_and_piecewise",
            "output_length": output_length,
        },
        "checks": {
            "ok": True, "active_rounds": 2, "expected_rounds": 2,
            "out_tokens_per_round": [64, 64] if output_length == 64 else [None, None],
            "ranks": ranks,
        },
    }
    (directory / "collection_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    suffix = "out_tokens 64" if output_length == 64 else "sample ' of'"
    (directory / "workload.log").write_text(
        f"ACTIVE_ROUND 0 start 1.0 end 2.0 {suffix}\n"
        f"ACTIVE_ROUND 1 start 3.0 end 4.0 {suffix}\n",
        encoding="utf-8",
    )
    return directory, manifest


@pytest.fixture
def collection(tmp_path):
    root = tmp_path / "exports"
    manifests = {}
    for run_id in RUN_IDS:
        _, manifests[run_id] = _write_run(root, run_id, nested=run_id.endswith("graph"))
    comparison = {
        "a": "qwen-test-decode-eager", "b": "qwen-test-decode-graph",
        "comparable": True, "same_window": True, "total_ratio": 1.319,
        "window": {"policy": "rounds", "provenance": "measured", "rounds": [0, 1]},
        "collections": {
            side: {
                key: manifests[run_id][key] for key in ("run_id", "contract_sha256", "started_at")
            }
            for side, run_id in zip(("a", "b"), RUN_IDS[:2], strict=True)
        },
    }
    return root, comparison


def _check(tmp_path, collection, *, comparison=True):
    root, payload = collection
    report = tmp_path / "acceptance.json"
    args = ["check-collection", "--dir", str(root), "--acceptance", str(report)]
    if comparison:
        path = tmp_path / "compare.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        args.extend(["--comparison", str(path)])
    code = main(args)
    return code, json.loads(report.read_text(encoding="utf-8"))


def test_acceptance_pass(tmp_path, collection):
    code, report = _check(tmp_path, collection)
    assert code == 0
    assert report["verdict"] == "pass"
    runs = {run["run_id"]: run for run in report["runs"]}
    assert set(runs) == set(RUN_IDS)
    for run in runs.values():
        assert run["rank_count"] == 2
        assert run["active_rounds"] == run["expected_rounds"] == 2
        assert all(run["metrics"][flag] for flag in ("pipe_time", "flops", "cube_utilization"))
    assert runs["decode-graph"]["tokens"] == [64, 64]
    assert runs["prefill-graph"]["tokens"] == []
    assert report["comparison"]["status"] == "pass"
    assert report["comparison"]["total_ratio"] == 1.319


def test_acceptance_accepts_report_from_compare_cli(tmp_path, collection):
    root, comparison = collection
    args = ["compare", "--strict", "--out-dir", str(tmp_path / "compare")]
    for side, mode in (("a", "eager"), ("b", "graph")):
        workload = WorkloadSpec.from_dict({
            "workload_id": comparison[side],
            "model": {"name": "qwen-test", "dtype": "bfloat16"},
            "hardware": {"device": "Ascend 910B", "device_count": 2},
            "execution": {"phase": "decode", "mode": mode},
            "input": {"batch_size": 1, "input_length": 16, "output_length": 64},
            "verified": True,
        })
        table = OperatorTable(
            workload=workload,
            records=[OperatorRecord(workload.workload_id, "MatMul", 1, Metric(1, "us"))],
            meta=ParseMeta(parser="test", extra={
                "collection": comparison["collections"][side], "window": comparison["window"],
            }),
        )
        args.extend([f"--{side}", str(table.write_json(tmp_path / f"{side}.json"))])
    assert main(args) == 0
    (report_path,) = (tmp_path / "compare").glob("compare_*.json")
    report = build_acceptance(root, report_path)
    assert report.verdict == "pass"
    assert report.comparison["status"] == "pass"
    assert report.comparison["total_ratio"] == 1


def test_acceptance_without_comparison_is_incomplete(tmp_path, collection):
    code, report = _check(tmp_path, collection, comparison=False)
    assert code == 2
    assert report["verdict"] == "incomplete"
    assert report["comparison"]["status"] == "not_run"
    assert main(["check-collection", "--dir", str(collection[0])]) == 0


@pytest.mark.parametrize(("suffix", "reason"), [
    ("*.db", "缺 msprof db"),
    ("*.csv", "缺 op_summary csv"),
])
def test_manifest_cannot_replace_missing_artifacts(tmp_path, collection, suffix, reason):
    graph = collection[0] / "decode-graph"
    for path in (graph / "export" / "PROF" / "PROF_0").rglob(suffix):
        path.unlink()
    code, report = _check(tmp_path, collection)
    assert code == 1
    assert report["verdict"] == "fail"
    run = next(run for run in report["runs"] if run["run_id"] == "decode-graph")
    assert any(reason in problem for problem in run["problems"])
    assert main(["check-collection", "--dir", str(collection[0])]) == 1


@pytest.mark.parametrize("header_only", [False, True])
def test_acceptance_rejects_partial_summary(tmp_path, collection, header_only):
    graph = collection[0] / "decode-graph"
    path = graph / "export/PROF/PROF_0/mindstudio_profiler_output/op_summary_0001.csv"
    if header_only:
        path.write_text(f"{OP_HEADER}\n", encoding="utf-8")
    else:
        path.unlink()

    code, report = _check(tmp_path, collection)
    assert code == 1 and report["verdict"] == "fail"
    run = next(run for run in report["runs"] if run["run_id"] == "decode-graph")
    rank = next(rank for rank in run["ranks"] if rank["device"] == "device_0")
    assert rank["op_summary_csv"] == 1 and rank["ok"] is False
    assert any("期望 2" in problem for problem in run["problems"])


@pytest.mark.parametrize(("rounds", "reason"), [
    ("", "ACTIVE_ROUND"),
    ("ACTIVE_ROUND 0 start 1 end 2 out_tokens 64\n"
     "ACTIVE_ROUND 0 start 3 end 4 out_tokens 64\n", "轮次索引"),
    ("ACTIVE_ROUND 0 start 1 end 2 out_tokens 32\n"
     "ACTIVE_ROUND 1 start 3 end 4 out_tokens 32\n", "out_tokens"),
    ("ACTIVE_ROUND 0 start 1 end 2 out_tokens 64\n"
     "ACTIVE_ROUND 1 start 3 end 4\n", "out_tokens"),
    ("ACTIVE_ROUND 0 start 1 end 1 out_tokens 64\n"
     "ACTIVE_ROUND 1 start 3 end 4 out_tokens 64\n", "轮次时长"),
])
def test_acceptance_checks_actual_rounds_and_tokens(tmp_path, collection, rounds, reason):
    (collection[0] / "decode-graph" / "workload.log").write_text(rounds, encoding="utf-8")
    code, report = _check(tmp_path, collection)
    assert code == 1
    assert report["verdict"] == "fail"
    run = next(run for run in report["runs"] if run["run_id"] == "decode-graph")
    assert any(reason in problem for problem in run["problems"])


@pytest.mark.parametrize("case", ["unbound", "stale", "same_run", "different_window", "incomparable"])
def test_acceptance_rejects_nonmatching_comparison(tmp_path, collection, case):
    _, comparison = collection
    if case == "unbound":
        del comparison["collections"]
    elif case == "stale":
        comparison["collections"]["b"]["started_at"] = "2025-01-01T00:00:00+00:00"
    elif case == "same_run":
        comparison["collections"]["b"] = comparison["collections"]["a"]
    elif case == "different_window":
        comparison["window"]["rounds"] = [0, 2]
    else:
        comparison["comparable"] = False
    code, report = _check(tmp_path, collection)
    assert code == 1
    assert report["verdict"] == "fail"
    assert report["comparison"]["status"] == "fail"
    assert report["comparison"]["total_ratio"] is None


def test_acceptance_reports_missing_comparison(collection, tmp_path):
    report = build_acceptance(collection[0], tmp_path / "missing.json")
    assert report.verdict == "fail"
    assert report.comparison["status"] == "missing"
