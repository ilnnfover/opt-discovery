"""验收报告：双卡、轮次、token、指标覆盖与比较状态。"""

from __future__ import annotations

import json

from opt_discovery.analyzer.acceptance import build_acceptance

OP_HEADER = [
    "Device_id", "Task Type", "Task Start Time(us)", "Task Duration(us)",
    "Task Wait Time(us)", "Block Num", "Input Shapes",
    "aicore_time(us)", "aic_mac_time(us)", "aiv_vec_time(us)", "aic_cube_fops",
    "cube_utilization(%)",
]


def _write_run(root, *, run_id="decode-graph", rounds=2, tokens=(64, 64), with_csv=True,
               ranks=2, ok=True):
    directory = root / f"20260101-000000-{run_id}"
    output = directory / "PROF_000001_x" / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    if with_csv:
        (output / "op_summary_0001.csv").write_text(",".join(OP_HEADER) + "\n", encoding="utf-8")
    lines = [f"ACTIVE_ROUND {i} start 1.0 end 2.0 out_tokens {tokens[i]}" for i in range(rounds)]
    (directory / "workload.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "exit_code": 0,
        "checks": {
            "ok": ok,
            "active_rounds": rounds,
            "expected_rounds": rounds,
            "out_tokens_per_round": list(tokens),
            "ranks": [
                {"device": f"device_{i}", "dir": f"PROF_{i}", "db": True,
                 "db_size_mb": 900.0, "op_summary_csv": 1 if with_csv else 0, "ok": ok}
                for i in range(ranks)
            ],
        },
    }
    (directory / "collection_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _write_comparison(path, *, comparable=True, same_window=True):
    path.write_text(json.dumps({
        "a": "qwen35-35b-a3b-decode-eager", "b": "qwen35-35b-a3b-decode-graph",
        "comparable": comparable, "same_window": same_window, "total_ratio": 1.319,
        "window": {"rounds": [0, 1, 2, 3]},
    }), encoding="utf-8")
    return path


def test_acceptance_pass(tmp_path):
    _write_run(tmp_path)
    comparison = _write_comparison(tmp_path / "compare.json")
    report = build_acceptance(tmp_path, comparison)
    assert report.verdict == "pass"
    run = report.runs[0]
    assert len(run.ranks) == 2
    assert run.active_rounds == 2 and run.expected_rounds == 2
    assert run.tokens == [64, 64]
    assert run.metrics["pipe_time"] and run.metrics["flops"] and run.metrics["cube_utilization"]
    assert report.comparison["status"] == "pass"
    assert report.comparison["total_ratio"] == 1.319


def test_acceptance_without_comparison_is_incomplete(tmp_path):
    _write_run(tmp_path)
    report = build_acceptance(tmp_path)
    assert report.verdict == "incomplete"
    assert report.comparison["status"] == "not_run"


def test_acceptance_flags_single_rank_and_bad_tokens(tmp_path):
    _write_run(tmp_path, ranks=1, tokens=(64, 63))
    report = build_acceptance(tmp_path)
    run = report.runs[0]
    assert report.verdict == "fail"
    assert any("rank 数 1" in problem for problem in run.problems)
    assert any("token 数不一致" in problem for problem in run.problems)


def test_acceptance_flags_missing_csv(tmp_path):
    _write_run(tmp_path, with_csv=False, ok=False)
    report = build_acceptance(tmp_path)
    run = report.runs[0]
    assert report.verdict == "fail"
    assert any("缺 op_summary csv" in problem for problem in run.problems)
    assert run.metrics["pipe_time"] is False


def test_acceptance_failed_comparison(tmp_path):
    _write_run(tmp_path)
    comparison = _write_comparison(tmp_path / "compare.json", comparable=False)
    report = build_acceptance(tmp_path, comparison)
    assert report.verdict == "incomplete"
    assert report.comparison["status"] == "fail"
