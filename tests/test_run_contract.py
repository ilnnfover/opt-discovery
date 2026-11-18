"""AE-03：CLI 运行契约（strict 退出码 + run_manifest）。

覆盖任务书要求的三类场景：正常、缺输入、子步骤异常、不可比、可选步骤缺失、旧输出残留。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from conftest import REPO_ROOT

from opt_discovery.cli import main
from opt_discovery.experiment.run_manifest import verify_manifest

CONFIGS = REPO_ROOT / "configs"
WORKLOAD_DIR = CONFIGS / "workloads" / "qwen35-35b-a3b-910b"

LOG_TEXT = """PROMPT_TOKENS 8 LOAD_SECONDS 1.5
PROFILE_START_ABS 1000.0
WARMUP_DONE_ABS 999.5
ACTIVE_ROUND 0 start 1000.0 end 1001.0 sample 'x'
ACTIVE_ROUND 1 start 1001.0 end 1002.0 sample 'x'
ACTIVE_ROUND 2 start 1002.0 end 1004.0 sample 'x'
"""

HEADER = "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us),Input Shapes\n"
ROWS = [
    ("MatMul", 1001_100_000, 1000.0),
    ("Add", 1001_102_000, 500.0),
    ("MatMul", 1003_100_000, 1200.0),
]


def _write_export(root: Path, export_dir: str, rows=ROWS) -> Path:
    export = root / export_dir
    export.mkdir(parents=True)
    (export / "workload.log").write_text(LOG_TEXT, encoding="utf-8")
    prof = export / "PROF_000001_test"
    output = prof / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    (prof / "device_0").mkdir()
    body = "".join(
        f'0,{operator},AI_CORE,{start},{duration},"8,16"\n'
        for operator, start, duration in rows
    )
    (output / "op_summary_1.csv").write_text(HEADER + body, encoding="utf-8")
    return export


def _workloads_dir(tmp_path: Path, names: list[str] | None = None, **overrides) -> Path:
    target = tmp_path / "workloads"
    target.mkdir()
    for name in names or ["prefill_eager"]:
        source = WORKLOAD_DIR / f"{name}.yaml"
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        for key, value in overrides.items():
            section, field = key.split("__")
            raw[section][field] = value
        (target / f"{name}.yaml").write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return target


def _analyze_args(tmp_path: Path, exports: Path, workloads: Path, out: Path, *extra: str):
    return [
        "analyze",
        "--exports-root",
        str(exports),
        "--workloads-dir",
        str(workloads),
        "--out",
        str(out),
        "--rounds",
        "1-2",
        *extra,
    ]


def test_normal_run_is_success_and_manifest_is_verifiable(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads_dir(tmp_path)
    out = tmp_path / "out"

    code = main(_analyze_args(tmp_path, exports, workloads, out, "--strict"))
    assert code == 0

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["strict"] is True
    assert manifest["is_synthetic"] is False
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["discover"]["status"] == "passed"
    assert stages["parse"]["status"] == "passed"
    assert stages["diagnosis"]["status"] == "passed"
    assert stages["timeline"]["status"] == "skipped"  # 没有 msprof db
    assert stages["source_map"]["status"] == "not_requested"
    assert manifest["workloads"][0]["workload_id"] == "qwen35-35b-a3b-prefill-eager"
    assert verify_manifest(out / "run_manifest.json") == []
    # 不泄露绝对路径
    dumped = json.dumps(manifest, ensure_ascii=False)
    assert str(tmp_path) not in dumped


def test_missing_input_is_blocked_with_manifest(tmp_path: Path) -> None:
    exports = tmp_path / "empty-exports"
    exports.mkdir()
    workloads = _workloads_dir(tmp_path)
    out = tmp_path / "out"

    code = main(_analyze_args(tmp_path, exports, workloads, out, "--strict"))
    assert code == 2  # 无可用导出：invalid/blocked，而不是 success

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["discover"]["status"] == "failed"
    assert stages["discover"]["reason_code"] == "no_export"


def test_failed_timeline_is_partial_not_success(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    export = _write_export(exports, "qwen35-prefill")
    # 放一个损坏的 msprof db：timeline 必需步骤失败，但表/诊断产物仍可用
    (export / "PROF_000001_test" / "msprof_0.db").write_bytes(b"not a sqlite database")
    workloads = _workloads_dir(tmp_path)
    out = tmp_path / "out"

    code = main(_analyze_args(tmp_path, exports, workloads, out, "--strict"))
    assert code == 3  # partial：部分产物可用，但必需步骤未完成

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "partial"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["timeline"]["status"] == "failed"
    assert stages["parse"]["status"] == "passed"
    assert manifest["workloads"][0]["status"] == "partial"
    assert manifest["workloads"][0]["details"]["timeline_failed"] is True
    assert verify_manifest(out / "run_manifest.json") == []


def test_incomparable_analysis_returns_4(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    _write_export(exports, "qwen35-prefill-graph-noex")
    # 两侧 device_count 不同 -> WorkloadSpec.comparable_with 失败
    workloads = _workloads_dir(tmp_path, ["prefill_eager", "prefill_graph"])
    graph_yaml = workloads / "prefill_graph.yaml"
    raw = yaml.safe_load(graph_yaml.read_text(encoding="utf-8"))
    raw["hardware"]["device_count"] = 4
    graph_yaml.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    out = tmp_path / "out"

    code = main(_analyze_args(tmp_path, exports, workloads, out, "--strict"))
    assert code == 4

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomparable"
    assert manifest["comparison_status"] == "incomparable"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["compare"]["status"] == "failed"
    assert stages["compare"]["reason_code"] == "incomparable"


def test_optional_source_map_failure_keeps_success(tmp_path: Path, monkeypatch, capsys) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads_dir(tmp_path)
    out = tmp_path / "out"

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic source-map failure")

    monkeypatch.setattr("opt_discovery.cli.build_source_map", _boom)
    code = main(_analyze_args(tmp_path, exports, workloads, out, "--strict", "--source-map"))
    assert code == 0  # 可选步骤失败不能宣告核心分析失败

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["source_map"]["required"] is False
    assert stages["source_map"]["status"] == "failed"
    assert stages["source_map"]["reason_code"] == "source_map_failed"
    assert "source-map 失败" in capsys.readouterr().err


def test_reused_output_dir_does_not_masquerade_old_artifacts(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.json").write_text("{}", encoding="utf-8")
    (out / "run_manifest.json").write_text(
        json.dumps({"run_id": "old-run", "artifacts": []}), encoding="utf-8"
    )

    code = main(_analyze_args(tmp_path, exports, workloads, out, "--strict"))
    assert code == 0

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["previous_run_id"] == "old-run"
    paths = {item["path"] for item in manifest["artifacts"]}
    assert "stale.json" not in paths
    assert "hotspots.json" in paths
    assert verify_manifest(out / "run_manifest.json") == []
    assert main(["check-manifest", "--path", str(out / "run_manifest.json")]) == 0

    # 产物被篡改或缺失时，校验必须发现（避免旧/坏产物冒充本次输出）
    target = out / "hotspots.json"
    target.write_text("{}", encoding="utf-8")
    problems = verify_manifest(out / "run_manifest.json")
    assert any("修改" in item for item in problems)
    assert main(["check-manifest", "--path", str(out / "run_manifest.json")]) == 1

    target.unlink()
    problems = verify_manifest(out / "run_manifest.json")
    assert any("缺少" in item for item in problems)
