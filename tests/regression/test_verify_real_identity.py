"""AE-06：verify-real 的采集身份反例与数据缺口分类（纯 CPU）。

* 目录存在但 ``collection_manifest.json`` 身份/契约不对 -> 真实测试 FAILED；
* 缺目录/缺 workload.log/缺 manifest/缺 op_summary -> verify-real BLOCKED(2)，
  且不得误跑到 pytest。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from opt_discovery.schemas.workload import WorkloadSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_real  # noqa: E402

WORKLOAD_ID = "qwen35-35b-a3b-prefill-eager"


@pytest.fixture(scope="module")
def fixture() -> dict:
    return verify_real.load_manifest_fixture()


def _valid_manifest(fixture: dict) -> dict:
    spec = fixture["runs"][WORKLOAD_ID]
    return {
        "run_id": spec["run_id"],
        "started_at": spec["started_at"],
        "dataset_id": fixture["dataset_id"],
        "contract_id": fixture["contract_id"],
        "contract_sha256": fixture["collection_contract_sha256"],
        "fixed": dict(fixture["fixed"]),
        "profiling": dict(fixture["profiling"]),
        "checks": {
            "ok": True,
            "active_rounds": len(fixture["active_rounds"]),
            "expected_rounds": len(fixture["active_rounds"]),
            "ranks": [
                {
                    "device": device,
                    "ok": True,
                    "dir": f"PROF_{device}",
                    "db": True,
                }
                for device in spec["ranks"]
            ],
        },
        "variables": {
            "mode": spec["mode"],
            "graph_mode": spec["graph_mode"],
            "output_length": spec["output_length"],
        },
    }


def test_valid_manifest_has_no_identity_problems(fixture: dict) -> None:
    assert verify_real.manifest_identity_problems(
        _valid_manifest(fixture), WORKLOAD_ID, fixture=fixture
    ) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (("run_id", "decode-eager"), "run_id"),
        (("started_at", "2026-11-21T06:15:43+00:00"), "started_at"),
        (("contract_id", "other-contract"), "contract_id"),
        (("contract_sha256", "0000000000000000"), "contract_sha256"),
        (("checks", {"ok": False, "active_rounds": 5}), "checks.ok"),
        (("checks", {"ok": True, "active_rounds": 4}), "active_rounds"),
        (("variables", {"mode": "graph"}), "variables.mode"),
    ],
)
def test_identity_mismatches_are_reported(
    fixture: dict, mutation: tuple[str, object], expected: str
) -> None:
    manifest = _valid_manifest(fixture)
    key, value = mutation
    manifest[key] = value
    problems = verify_real.manifest_identity_problems(
        manifest, WORKLOAD_ID, fixture=fixture
    )
    assert any(expected in problem for problem in problems), problems


def test_nested_fixed_and_profiling_changes_fail_even_with_declared_hash(
    fixture: dict,
) -> None:
    """反例：哈希未变，但固定参数/采集开关被改，不能仍判为同一采集。"""
    manifest = json.loads(json.dumps(_valid_manifest(fixture)))
    manifest["fixed"]["max_num_seqs"] = 1
    manifest["profiling"]["switches"] = []

    problems = verify_real.manifest_identity_problems(
        manifest, WORKLOAD_ID, fixture=fixture
    )

    assert any("fixed.max_num_seqs" in problem for problem in problems), problems
    assert any("profiling.switches" in problem for problem in problems), problems
    # 哈希保持 fixture 声明值，问题必须来自固定参数/开关而不是 contract_sha256
    assert not any("contract_sha256" in problem for problem in problems), problems


def _make_complete_root(root: Path) -> None:
    for label in verify_real.expected_exports().values():
        export = root / label
        export.mkdir(parents=True)
        (export / "workload.log").write_text(
            "ACTIVE_ROUND 0 start 1.0 end 2.0\n", encoding="utf-8"
        )
        (export / "collection_manifest.json").write_text("{}", encoding="utf-8")
        output = export / "PROF_1" / "mindstudio_profiler_output"
        output.mkdir(parents=True)
        (output / "op_summary_0.csv").write_text(
            "OP Type,Task Start Time(us),Task Duration(us)\n", encoding="utf-8"
        )


def test_complete_layout_has_no_gaps(tmp_path: Path) -> None:
    _make_complete_root(tmp_path)
    assert verify_real.data_gaps(tmp_path) == []


def _make_capture(tmp_path: Path, *, layout: str = "PROF") -> tuple[Path, dict[str, str]]:
    """临时采集根：日志和两个实采 rank 源目录。"""
    capture = tmp_path / "capture"
    (capture / layout / "PROF_a").mkdir(parents=True)
    (capture / layout / "PROF_b").mkdir(parents=True)
    (capture / "workload.log").write_text(
        "ACTIVE_ROUND 0 start 1.0 end 2.0\n", encoding="utf-8"
    )
    return capture, {"device_0": "PROF_a", "device_1": "PROF_b"}


def _rank_manifest(dirs: dict[str, str]) -> dict:
    return {
        "checks": {
            "ranks": [
                {"device": device, "ok": True, "dir": dir_name, "db": True}
                for device, dir_name in dirs.items()
            ]
        }
    }


@pytest.mark.parametrize("alias_log", [False, True])
@pytest.mark.parametrize("layout", [".", "PROF"])
def test_rank_layout_accepts_original_and_alias_capture(
    tmp_path: Path, alias_log: bool, layout: str
) -> None:
    """原目录和 alias（workload.log 指向临时采集根）都必须能绑定 rank 源。"""
    capture, dirs = _make_capture(tmp_path, layout=layout)
    if alias_log:
        export = tmp_path / "export"
        export.mkdir()
        (export / "workload.log").symlink_to(capture / "workload.log")
    else:
        export = capture

    sources = {
        device: capture / layout / dir_name for device, dir_name in dirs.items()
    }
    problems = verify_real.rank_layout_problems(export, _rank_manifest(dirs), sources)

    assert problems == [], problems


@pytest.mark.parametrize("layout", [".", "PROF"])
def test_preflight_requires_csv_for_each_declared_rank(tmp_path: Path, layout: str) -> None:
    capture, dirs = _make_capture(tmp_path, layout=layout)
    (capture / "collection_manifest.json").write_text(
        json.dumps(_rank_manifest(dirs)), encoding="utf-8"
    )
    files = []
    for directory in dirs.values():
        csv = capture / layout / directory / "mindstudio_profiler_output" / "op_summary.csv"
        csv.parent.mkdir()
        csv.touch()
        files.append(csv)
    export = tmp_path / "prefill-eager"
    export.symlink_to(capture, target_is_directory=True)
    expected = {WORKLOAD_ID: export.name}
    assert verify_real.data_gaps(tmp_path, expected=expected) == []

    files[1].unlink()
    assert any(
        "PROF_b" in gap and "op_summary" in gap
        for gap in verify_real.data_gaps(tmp_path, expected=expected)
    )

    files[1].touch()
    manifest = _rank_manifest(dirs)
    manifest["checks"]["ranks"][1]["op_summary_csv"] = 2
    (capture / "collection_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert any(
        "PROF_b" in gap and "期望 2" in gap
        for gap in verify_real.data_gaps(tmp_path, expected=expected)
    )


def test_rank_layout_rejects_rank_symlink_to_another_group(tmp_path: Path) -> None:
    """反例：alias 文件名不变，但 rank 1 实际指向另一组的 PROF 目录。"""
    capture, dirs = _make_capture(tmp_path)
    export = tmp_path / "export"
    export.mkdir()
    (export / "workload.log").symlink_to(capture / "workload.log")

    wrong = tmp_path / "prefill-graph-rank1"
    wrong.symlink_to(capture / "PROF" / "PROF_a", target_is_directory=True)
    sources = {
        "device_0": capture / "PROF" / "PROF_a",
        "device_1": wrong,
    }

    problems = verify_real.rank_layout_problems(export, _rank_manifest(dirs), sources)

    assert any(
        "device_1" in problem and "不一致" in problem for problem in problems
    ), problems


def test_missing_manifest_is_a_data_gap(tmp_path: Path) -> None:
    _make_complete_root(tmp_path)
    label = next(iter(verify_real.expected_exports().values()))
    (tmp_path / label / "collection_manifest.json").unlink()

    gaps = verify_real.data_gaps(tmp_path)
    assert any("collection_manifest.json" in gap for gap in gaps), gaps


def test_missing_export_is_blocked_before_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _make_complete_root(tmp_path)
    label = next(iter(verify_real.expected_exports().values()))
    (tmp_path / label / "collection_manifest.json").unlink()
    monkeypatch.setenv("OPT_DISCOVERY_DATA_ROOT", str(tmp_path))

    def _must_not_run() -> None:
        raise AssertionError("数据不完整时不得启动真实 pytest")

    monkeypatch.setattr(verify_real, "_run_real_test", _must_not_run)

    assert verify_real.main([]) == verify_real.EXIT_CODES[verify_real.Outcome.BLOCKED]
    output = capsys.readouterr().out
    assert "BLOCKED" in output
    assert "collection_manifest.json" in output


def test_fixture_contains_only_logical_ids() -> None:
    """仓库内 fixture 不能携带机器路径/原始导出目录。"""
    text = verify_real.MANIFEST_FIXTURE.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert not any(token in text for token in ("/root/", "PROF_", "\\", ":\\\\"))
    assert payload["collection_contract_sha256"] == "0f972be0b536df91"
    workload_dir = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"
    workloads = [WorkloadSpec.from_yaml(path) for path in sorted(workload_dir.glob("*.yaml"))]
    assert {w.workload_id: w.labels["collection_run_id"] for w in workloads} == {
        key: spec["run_id"] for key, spec in payload["runs"].items()
    }
