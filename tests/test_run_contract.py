"""AE-03：CLI 运行契约（strict 退出码 + run_manifest）。

覆盖：正常、缺输入、缺 workload 日志、缺导出目录、必需步骤失败、TP 缺 rank、
不可比、可选步骤失败、旧输出复用、配置失败结算、自定义清单路径。
"""

from __future__ import annotations

import json
import sqlite3
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


def _write_db(prof: Path) -> Path:
    db = prof / "msprof_0.db"
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.commit()
    connection.close()
    return db


def _write_export(
    root: Path,
    export_dir: str,
    *,
    ranks: int = 1,
    rows=ROWS,
    log: bool = True,
    db: bool = True,
) -> Path:
    export = root / export_dir
    export.mkdir(parents=True)
    if log:
        (export / "workload.log").write_text(LOG_TEXT, encoding="utf-8")
    for rank in range(ranks):
        prof = export / f"PROF_{rank:06d}_test"
        output = prof / "mindstudio_profiler_output"
        output.mkdir(parents=True)
        (prof / f"device_{rank}").mkdir()
        body = "".join(
            f'0,{operator},AI_CORE,{start},{duration},"8,16"\n'
            for operator, start, duration in rows
        )
        (output / "op_summary_1.csv").write_text(HEADER + body, encoding="utf-8")
        if db:
            _write_db(prof)
    return export


def _workloads(tmp_path: Path, overrides: dict[str, dict] | None = None) -> Path:
    target = tmp_path / "workloads"
    target.mkdir()
    overrides = overrides or {"prefill_eager": {"hardware.device_count": 1}}
    for name, values in overrides.items():
        raw = yaml.safe_load((WORKLOAD_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        for dotted, value in values.items():
            section, field = dotted.split(".")
            raw[section][field] = value
        (target / f"{name}.yaml").write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return target


def _args(exports: Path, workloads: Path, out: Path, *extra: str) -> list[str]:
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


def _manifest(out: Path) -> dict:
    return json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))


def test_normal_run_is_success_and_manifest_is_verifiable(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 0

    manifest = _manifest(out)
    assert manifest["status"] == "success"
    assert manifest["strict"] is True
    assert manifest["is_synthetic"] is False
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["discover"]["status"] == "passed"
    assert stages["parse"]["status"] == "passed"
    assert stages["timeline"]["status"] == "passed"
    assert stages["diagnosis"]["status"] == "passed"
    assert stages["source_map"]["status"] == "not_requested"
    workload = manifest["workloads"][0]
    assert workload["workload_id"] == "qwen35-35b-a3b-prefill-eager"
    assert workload["quality_flags"] == []
    paths = {item["path"] for item in manifest["artifacts"]}
    assert "qwen35-35b-a3b-prefill-eager/timeline.json" in paths
    assert verify_manifest(out / "run_manifest.json") == []
    # 配置指纹与逻辑标签：不泄露绝对路径
    config_names = {item["name"] for item in manifest["inputs"]["configs"]}
    assert {"prefill_eager.yaml", "bottleneck_rules.v1.yaml"} <= config_names
    assert all(item.get("sha256") for item in manifest["inputs"]["configs"])
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)


def test_missing_input_is_blocked_with_manifest(tmp_path: Path) -> None:
    exports = tmp_path / "empty-exports"
    exports.mkdir()
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 2

    manifest = _manifest(out)
    assert manifest["status"] == "failed"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["discover"]["status"] == "failed"
    assert stages["discover"]["reason_code"] == "no_export"
    assert stages["parse"]["status"] == "not_requested"  # 无导出就没有 parse 可执行


def test_failed_timeline_is_partial_not_success(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    export = _write_export(exports, "qwen35-prefill")
    (export / "PROF_000000_test" / "msprof_0.db").write_bytes(b"not a sqlite database")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 3

    manifest = _manifest(out)
    assert manifest["status"] == "partial"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["timeline"]["status"] == "failed"
    assert manifest["workloads"][0]["status"] == "partial"
    assert manifest["workloads"][0]["details"]["timeline_failed"] is True
    assert verify_manifest(out / "run_manifest.json") == []


def test_missing_workload_log_is_partial_not_success(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    _write_export(exports, "qwen35-prefill-graph-noex")
    (exports / "qwen35-prefill" / "workload.log").unlink()  # eager 缺日志
    workloads = _workloads(
        tmp_path,
        {
            "prefill_eager": {"hardware.device_count": 1},
            "prefill_graph": {"hardware.device_count": 1},
        },
    )
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 3  # 部分产物可用，但必需输入未完成

    manifest = _manifest(out)
    assert manifest["status"] == "partial"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["parse"]["status"] == "failed"
    by_id = {item["workload_id"]: item for item in manifest["workloads"]}
    eager = by_id["qwen35-35b-a3b-prefill-eager"]
    graph = by_id["qwen35-35b-a3b-prefill-graph"]
    assert eager["status"] == "failed"
    assert eager["reason_code"] == "missing_run_log"
    assert graph["status"] == "success"


def test_missing_export_dir_is_recorded_and_partial(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(
        tmp_path,
        {
            "prefill_eager": {"hardware.device_count": 1},
            "prefill_graph": {"hardware.device_count": 1},
        },
    )
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 3

    manifest = _manifest(out)
    assert manifest["status"] == "partial"
    by_id = {item["workload_id"]: item for item in manifest["workloads"]}
    missing = by_id["qwen35-35b-a3b-prefill-graph"]
    assert missing["status"] == "failed"
    assert missing["reason_code"] == "missing_export"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["discover"]["status"] == "failed"
    assert stages["discover"]["reason_code"] == "missing_export"


def test_declared_tp_rank_missing_blocks_quality(tmp_path: Path) -> None:
    """采集契约声明 device_count=2，但只交付 rank0：必须显式阻断（R05）。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill", ranks=1)
    workloads = _workloads(
        tmp_path, {"prefill_eager": {"hardware.device_count": 2}}
    )
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 3

    manifest = _manifest(out)
    assert manifest["status"] == "partial"
    assert "rank_merge_unverified" in manifest["quality_flags"]
    assert "rank_merge_unverified" in manifest["workloads"][0]["quality_flags"]
    table = json.loads(
        (
            out / "qwen35-35b-a3b-prefill-eager" / "operator_table.json"
        ).read_text(encoding="utf-8")
    )
    codes = {flag["code"] for flag in table["meta"]["extra"]["quality_flags"]}
    assert "rank_merge_unverified" in codes


def test_incomparable_analysis_returns_4(tmp_path: Path, capsys) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    _write_export(exports, "qwen35-prefill-graph-noex")
    # 输入长度不同 -> WorkloadSpec.comparable_with 失败（不引入其他 partial 因素）
    workloads = _workloads(
        tmp_path,
        {
            "prefill_eager": {"hardware.device_count": 1},
            "prefill_graph": {
                "hardware.device_count": 1,
                "input.input_length": 2048,
            },
        },
    )
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 4

    manifest = _manifest(out)
    assert manifest["status"] == "incomparable"
    assert manifest["comparison_status"] == "incomparable"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["compare"]["status"] == "failed"
    assert stages["compare"]["reason_code"] == "incomparable"
    compare_files = list(out.glob("compare_*.json"))
    assert compare_files
    payload = json.loads(compare_files[0].read_text(encoding="utf-8"))
    assert payload["comparable"] is False
    assert payload["deltas"] == []
    assert payload["total_ratio"] is None  # 不可比不得提供速度比
    assert "total_ratio=-" in capsys.readouterr().out  # 终端同样省略


def test_optional_source_map_failure_keeps_success(tmp_path: Path, monkeypatch, capsys) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic source-map failure")

    monkeypatch.setattr("opt_discovery.cli.build_source_map", _boom)
    code = main(_args(exports, workloads, out, "--strict", "--source-map"))
    assert code == 0

    manifest = _manifest(out)
    assert manifest["status"] == "success"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["source_map"]["required"] is False
    assert stages["source_map"]["status"] == "failed"
    assert stages["source_map"]["reason_code"] == "source_map_failed"
    assert "source-map 失败" in capsys.readouterr().err


def test_reused_output_dir_does_not_masquerade_old_artifacts(tmp_path: Path) -> None:
    """复用目录：旧 timeline/旧 workload 行不得进入本轮清单与汇总。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    _write_export(exports, "qwen35-prefill-graph-noex")
    workloads = _workloads(
        tmp_path,
        {
            "prefill_eager": {"hardware.device_count": 1},
            "prefill_graph": {"hardware.device_count": 1},
        },
    )
    out = tmp_path / "out"

    assert main(_args(exports, workloads, out, "--strict")) == 0
    first = _manifest(out)
    assert first["status"] == "success"
    eager_timeline = out / "qwen35-35b-a3b-prefill-eager" / "timeline.json"
    assert eager_timeline.is_file()

    # 第二轮：eager 缺日志 + 显式跳过 timeline；graph 仍成功
    (exports / "qwen35-prefill" / "workload.log").unlink()
    code = main(_args(exports, workloads, out, "--strict", "--skip-timeline"))
    assert code == 3

    manifest = _manifest(out)
    assert manifest["previous_run_id"] == first["run_id"]
    paths = {item["path"] for item in manifest["artifacts"]}
    assert not any(path.endswith("timeline.json") for path in paths)
    assert not any(path.startswith("qwen35-35b-a3b-prefill-eager/") for path in paths)
    assert "qwen35-35b-a3b-prefill-graph/operator_table.json" in paths
    assert verify_manifest(out / "run_manifest.json") == []

    # 汇总只包含本轮选中的 workload（旧 eager 行不得混入）
    rows = (out / "workload_summary.csv").read_text(encoding="utf-8-sig").splitlines()
    assert any("qwen35-35b-a3b-prefill-graph" in row for row in rows)
    assert not any("qwen35-35b-a3b-prefill-eager" in row for row in rows)

    # check-manifest 只校验清单内容；篡改/缺失仍要能发现
    assert main(["check-manifest", "--path", str(out / "run_manifest.json")]) == 0
    target = out / "hotspots.json"
    target.write_text("{}", encoding="utf-8")
    assert main(["check-manifest", "--path", str(out / "run_manifest.json")]) == 1
    target.unlink()
    assert main(["check-manifest", "--path", str(out / "run_manifest.json")]) == 1


def test_config_failure_replaces_previous_success_manifest(tmp_path: Path, capsys) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"
    assert main(_args(exports, workloads, out, "--strict")) == 0
    old_run_id = _manifest(out)["run_id"]

    missing_mapping = tmp_path / "missing-mapping.yaml"
    code = main(
        _args(
            exports,
            workloads,
            out,
            "--strict",
            "--summary-mapping",
            str(missing_mapping),
        )
    )
    assert code == 1

    manifest = _manifest(out)
    assert manifest["status"] == "failed"
    assert manifest["run_id"] != old_run_id
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["discover"]["status"] == "passed"
    assert stages["parse"]["status"] == "not_requested"
    assert "配置加载失败" in capsys.readouterr().err


def test_custom_manifest_path_outside_out_dir_is_verifiable(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"
    manifest_path = tmp_path / "manifests" / "run.json"
    assert (
        main(_args(exports, workloads, out, "--strict", "--manifest", str(manifest_path))) == 0
    )
    assert manifest_path.is_file()
    assert verify_manifest(manifest_path) == []
    assert main(["check-manifest", "--path", str(manifest_path)]) == 0


def test_empty_manifest_is_rejected(tmp_path: Path, capsys) -> None:
    path = tmp_path / "run_manifest.json"
    path.write_text("{}", encoding="utf-8")

    assert main(["check-manifest", "--path", str(path)]) == 1
    assert "schema_version" in capsys.readouterr().out


def test_artifact_write_failure_is_settled(tmp_path: Path, monkeypatch, capsys) -> None:
    """P1-6：产物写出失败必须落到 workload/stage，不能留下旧成功假象。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    def _boom(self, path):
        raise OSError("disk full (synthetic)")

    monkeypatch.setattr("opt_discovery.schemas.operator.OperatorTable.write_json", _boom)
    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 3

    manifest = _manifest(out)
    assert manifest["status"] == "partial"
    workloads_out = manifest["workloads"]
    assert workloads_out[0]["status"] == "failed"
    assert workloads_out[0]["reason_code"] == "artifact_write_error"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["opportunities"]["status"] == "failed"
    assert stages["opportunities"]["reason_code"] == "artifact_write_failed"
    paths = {item["path"] for item in manifest["artifacts"]}
    assert not any(path.endswith("operator_table.json") for path in paths)


def test_parse_error_details_are_redacted(tmp_path: Path) -> None:
    """P2-2：错误详情进入清单前必须去掉绝对路径。"""
    exports = tmp_path / "exports"
    export = exports / "qwen35-prefill"
    prof = export / "PROF_000000_test"
    output = prof / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    (prof / "device_0").mkdir()
    (export / "workload.log").write_text(LOG_TEXT, encoding="utf-8")
    (output / "op_summary_1.csv").write_text(
        "Device_id,Task Type,Task Start Time(us),Task Duration(us)\n0,AI_CORE,1001100000,1000\n",
        encoding="utf-8",
    )
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 1  # 唯一 workload 解析失败 -> failed（不是 partial）

    manifest = _manifest(out)
    assert manifest["status"] == "failed"
    workload = manifest["workloads"][0]
    assert workload["status"] == "failed"
    assert workload["reason_code"] == "parse_error"
    assert "<path>" in workload["details"]["error"]
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)


def test_partial_timeline_db_blocks_quality(tmp_path: Path) -> None:
    """P1-2：TP=2 但只有部分 timeline DB 时必须显式降级，不能当作完整分析。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill", ranks=2)
    (exports / "qwen35-prefill" / "PROF_000001_test" / "msprof_0.db").unlink()
    workloads = _workloads(tmp_path, {"prefill_eager": {"hardware.device_count": 2}})
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 3

    manifest = _manifest(out)
    assert manifest["status"] == "partial"
    assert "rank_timeline_incomplete" in manifest["quality_flags"]
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["timeline"]["status"] == "failed"
    assert any("partial_db" in item for item in stages["timeline"]["details"]["failed"])


def test_timeline_failure_makes_table_uncomparable(tmp_path: Path) -> None:
    """F1-2：必需 timeline 失败必须随表落盘，独立 compare 也要拒绝。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill", ranks=2)
    _write_export(exports, "qwen35-prefill-graph-noex", ranks=2)
    (exports / "qwen35-prefill" / "PROF_000000_test" / "msprof_0.db").write_bytes(
        b"not a sqlite database"
    )
    workloads = _workloads(
        tmp_path,
        {
            "prefill_eager": {"hardware.device_count": 2},
            "prefill_graph": {"hardware.device_count": 2},
        },
    )
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 3

    manifest = _manifest(out)
    assert manifest["status"] == "partial"
    assert manifest["comparison_status"] == "incomparable"
    eager_table = out / "qwen35-35b-a3b-prefill-eager" / "operator_table.json"
    table = json.loads(eager_table.read_text(encoding="utf-8"))
    assert "timeline_unavailable" in {
        flag["code"] for flag in table["meta"]["extra"]["quality_flags"]
    }

    graph_table = out / "qwen35-35b-a3b-prefill-graph" / "operator_table.json"
    compare_dir = tmp_path / "cmp"
    assert (
        main(
            [
                "compare",
                "--a",
                str(eager_table),
                "--b",
                str(graph_table),
                "--out-dir",
                str(compare_dir),
                "--strict",
            ]
        )
        == 4
    )


def test_stale_comm_is_not_used_in_summary(tmp_path: Path) -> None:
    """F1-3：同一 workload 的旧 comm.json 不得进入本轮 CSV。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    assert main(_args(exports, workloads, out, "--strict")) == 0
    stale = out / "qwen35-35b-a3b-prefill-eager" / "comm.json"
    stale.write_text(
        json.dumps({"mib_total": 4.0, "unique_tasks": 1}), encoding="utf-8"
    )

    (exports / "qwen35-prefill" / "PROF_000000_test" / "msprof_0.db").unlink()
    assert main(_args(exports, workloads, out, "--strict", "--skip-timeline")) == 0

    manifest = _manifest(out)
    assert "qwen35-35b-a3b-prefill-eager/comm.json" not in {
        item["path"] for item in manifest["artifacts"]
    }
    row = [
        line
        for line in (out / "workload_summary.csv").read_text(encoding="utf-8-sig").splitlines()
        if "qwen35-35b-a3b-prefill-eager" in line
    ]
    assert row
    assert "4.0" not in row[0]


def test_discover_error_settles_manifest(tmp_path: Path, capsys) -> None:
    """F1-4：导出目录存在但无可解析 CSV 时，不得保留旧 success 清单。"""
    exports = tmp_path / "exports"
    export = exports / "qwen35-prefill"
    output = export / "PROF_000000_test" / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    (export / "workload.log").write_text(LOG_TEXT, encoding="utf-8")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "run_manifest.json").write_text(
        json.dumps({"run_id": "old-run", "status": "success"}), encoding="utf-8"
    )

    code = main(_args(exports, workloads, out, "--strict"))
    assert code == 1

    manifest = _manifest(out)
    assert manifest["run_id"] != "old-run"
    assert manifest["status"] == "failed"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["discover"]["status"] == "failed"
    assert stages["discover"]["reason_code"] == "discover_error"
    assert stages["parse"]["status"] == "not_requested"


def test_compare_input_directory_settles_manifest(tmp_path: Path, capsys) -> None:
    """F1-4：compare 输入是目录时返回 1、写失败清单且错误详情不含绝对路径。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"
    assert main(_args(exports, workloads, out, "--strict")) == 0
    table = out / "qwen35-35b-a3b-prefill-eager" / "operator_table.json"

    compare_out = tmp_path / "cmp"
    compare_out.mkdir()
    (compare_out / "run_manifest.json").write_text(
        json.dumps({"run_id": "old-run", "status": "success"}), encoding="utf-8"
    )
    code = main(
        ["compare", "--a", str(tmp_path), "--b", str(table), "--out-dir", str(compare_out)]
    )
    assert code == 1

    manifest = json.loads((compare_out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] != "old-run"
    assert manifest["status"] == "failed"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["load"]["reason_code"] == "load_error"
    assert str(tmp_path) not in manifest["stages"][0]["details"]["error"]
    assert "<path>" in manifest["stages"][0]["details"]["error"]


def test_compare_custom_manifest_and_missing_sha(tmp_path: Path) -> None:
    """F2-1：compare 的清单路径也相对清单目录；缺 sha256 必须校验失败。"""
    exports = tmp_path / "exports"
    _write_export(exports, "qwen35-prefill")
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"
    assert main(_args(exports, workloads, out, "--strict")) == 0
    table = out / "qwen35-35b-a3b-prefill-eager" / "operator_table.json"

    compare_out = tmp_path / "cmp"
    manifest_path = tmp_path / "manifests" / "compare.json"
    code = main(
        [
            "compare",
            "--a",
            str(table),
            "--b",
            str(table),
            "--out-dir",
            str(compare_out),
            "--manifest",
            str(manifest_path),
            "--strict",
        ]
    )
    assert code == 0
    assert main(["check-manifest", "--path", str(manifest_path)]) == 0

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in payload["artifacts"]:
        item.pop("sha256", None)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["check-manifest", "--path", str(manifest_path)]) == 1


def test_skip_timeline_with_tp2_is_not_failure(tmp_path: Path) -> None:
    """F2-3：显式 --skip-timeline 时，缺 DB 不是失败，而是降级提示。"""
    exports = tmp_path / "exports"
    export = _write_export(exports, "qwen35-prefill", ranks=2)
    for rank in range(2):
        (export / f"PROF_{rank:06d}_test" / "msprof_0.db").unlink()
    workloads = _workloads(tmp_path, {"prefill_eager": {"hardware.device_count": 2}})
    out = tmp_path / "out"

    code = main(_args(exports, workloads, out, "--strict", "--skip-timeline"))
    assert code == 0

    manifest = _manifest(out)
    assert manifest["status"] == "success"
    stages = {item["name"]: item for item in manifest["stages"]}
    assert stages["timeline"]["required"] is False
    assert stages["timeline"]["status"] == "not_requested"
    assert "timeline_skipped" in manifest["quality_flags"]
    assert {"rank_merge_inconsistent", "timeline_unavailable"}.isdisjoint(
        manifest["quality_flags"]
    )
