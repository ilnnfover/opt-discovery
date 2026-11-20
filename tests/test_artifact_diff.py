"""AE-08: semantic before/after artifact diff tests.

These use the real artifact shape written by ``analyze`` (per-workload JSON,
summary CSV, compare JSON, ``run_manifest.json``) and check that the diff
reports semantic changes while ignoring timestamps, run ids and random
opportunity instance ids.  Fail-closed cases are covered explicitly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _verification_helpers import make_analysis_dir, opportunity, write_json
from conftest import REPO_ROOT

from tools.artifact_diff import _trusted_level, compare_dirs

SCRIPT = REPO_ROOT / "scripts" / "compare_artifacts.py"


def _copy_pair(tmp_path: Path) -> tuple[Path, Path]:
    before = make_analysis_dir(tmp_path / "before")
    after = tmp_path / "after"
    shutil.copytree(before, after)
    return before, after


def _edit_json(path: Path, mutator) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutator(payload)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _paths(report: dict) -> set[str]:
    return {
        str(change.get("path"))
        for change in report["changes"]
        if change.get("path")
    }


def test_noise_only_is_not_reported(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    wdir = after / "w1"
    _edit_json(
        wdir / "opportunities.json",
        lambda data: (
            data.update({"generated_at": "2026-02-02T00:00:00+00:00"}),
            data["opportunities"][0].update(
                {
                    "instance_id": "another-random-instance",
                    "created_at": "2026-02-02T00:00:00+00:00",
                }
            ),
        ),
    )
    _edit_json(
        wdir / "timeline.json",
        lambda data: data.update({"db_path": "/other/machine/msprof_0.db"}),
    )
    _edit_json(
        after / "run_manifest.json",
        lambda data: data.update(
            {"run_id": "run-2", "created_at": "2026-02-02T00:00:00+00:00"}
        ),
    )

    report = compare_dirs(before, after)

    assert report["status"] == "compared"
    assert report["summary"]["changed"] is False
    assert report["changes"] == []
    assert "instance_id" in report["summary"]["ignored_noise"]


def test_quality_flag_and_evidence_level_changes_are_reported(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    wdir = after / "w1"

    def add_flag(data: dict) -> None:
        data["meta"]["extra"]["quality_flags"] = [
            {
                "code": "rank_merge_inconsistent",
                "severity": "blocking",
                "message": "两个 rank 不一致",
                "source": "test",
            }
        ]

    _edit_json(wdir / "operator_table.json", add_flag)
    _edit_json(
        wdir / "opportunities.json",
        lambda data: data["opportunities"][0].update({"evidence_level": "probable"}),
    )

    report = compare_dirs(before, after)
    changes = report["changes"]

    assert report["status"] == "compared"
    assert any(
        item["category"] == "quality_flag"
        and item["path"] == "meta.extra.quality_flags[rank_merge_inconsistent]"
        and item["change"] == "added"
        for item in changes
    )
    assert any(
        item["category"] == "opportunity"
        and item["path"].endswith(".evidence_level")
        and item["before"] == "hypothesis"
        and item["after"] == "probable"
        for item in changes
    )
    # Trusted level is tracked too: a raw field change is not the only signal.
    assert any(
        item["path"].endswith(".trusted_evidence_level") for item in changes
    )


def test_raw_confirmed_is_not_treated_as_a_trusted_promotion(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    wdir = after / "w1"
    _edit_json(
        wdir / "opportunities.json",
        lambda data: data["opportunities"][0].update({"evidence_level": "confirmed"}),
    )

    report = compare_dirs(before, after)
    changes = report["changes"]
    trusted = [
        item
        for item in changes
        if item.get("path", "").endswith(".trusted_evidence_level")
    ]

    assert trusted, "trusted level must be compared separately from the raw string"
    assert trusted[0]["after"] == "probable"
    assert trusted[0]["after"] != "confirmed"
    assert any(
        item.get("path", "").endswith(".trusted_level_warning")
        and item.get("after")
        for item in changes
    )


def test_opportunity_reordering_is_not_reported_as_a_level_change(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    path = after / "w1" / "opportunities.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    second = opportunity("w1", operator="Add")
    second["opportunity_id"] = "OPP-002"
    payload["opportunities"].append(second)
    payload["count"] = 2
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Adding a genuinely new candidate is reported; reordering alone is not.
    report_added = compare_dirs(before, after)
    assert any(
        item["category"] == "opportunity" and item["change"] == "added"
        for item in report_added["changes"]
    )

    reordered = tmp_path / "reordered"
    shutil.copytree(after, reordered)
    path = reordered / "w1" / "opportunities.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["opportunities"] = list(reversed(payload["opportunities"]))
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report_reordered = compare_dirs(after, reordered)
    assert report_reordered["summary"]["changed"] is False


def test_csv_zero_vs_missing_and_row_content_changes(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    path = after / "operator_summary.csv"
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header = lines[0].split(",")
    values = lines[1].split(",")
    calls_index = header.index("calls")
    total_index = header.index("total_us")
    values[calls_index] = "0"  # 10 -> 0 is a real change
    values[total_index] = ""  # measured value -> missing is not "no change"
    lines[1] = ",".join(values)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    report = compare_dirs(before, after)
    paths = _paths(report)

    assert any(
        item["category"] == "csv"
        and item["artifact"] == "operator_summary.csv"
        and item["path"].endswith(".calls")
        and item["after"] == "0"
        for item in report["changes"]
    )
    assert any(
        item["category"] == "csv"
        and item["artifact"] == "operator_summary.csv"
        and item["path"].endswith(".total_us")
        and item["after"] == ""
        for item in report["changes"]
    )
    assert paths  # non-empty means the diff did not silently pass


def test_compare_status_and_manifest_status_changes(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    compare_name = "compare_w1_vs_w2.json"
    _edit_json(
        after / compare_name,
        lambda data: data.update(
            {"comparable": False, "reasons": ["window mismatch"], "total_ratio": None}
        ),
    )
    _edit_json(
        after / "run_manifest.json",
        lambda data: (
            data.update({"status": "partial", "comparison_status": "incomparable"}),
            data["stages"][2].update({"status": "failed", "reason_code": "incomparable"}),
            data["workloads"][0].update({"status": "partial", "quality_flags": ["empty_window"]}),
        ),
    )

    report = compare_dirs(before, after)
    changes = report["changes"]

    assert any(
        item["category"] == "compare"
        and item["path"] == "comparable"
        and item["after"] is False
        for item in changes
    )
    assert any(
        item["category"] == "compare"
        and item["path"] == "reasons"
        and item["after"] == ["window mismatch"]
        for item in changes
    )
    assert any(
        item["category"] == "run_manifest"
        and item["path"] == "status"
        and item["after"] == "partial"
        for item in changes
    )
    assert any(
        item["category"] == "run_manifest"
        and item["path"] == "row:compare.status"
        and item["after"] == "failed"
        for item in changes
    )


def test_added_and_removed_artifacts_are_recorded(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    (after / "w1" / "timeline.json").unlink()
    write_json(after / "source_map.json", {"repos": ["repo"], "operators": {}})

    report = compare_dirs(before, after)
    changes = report["changes"]

    assert any(
        item["category"] == "artifact"
        and item["artifact"] == "w1/timeline.json"
        and item["change"] == "removed"
        for item in changes
    )
    assert any(
        item["category"] == "artifact"
        and item["artifact"] == "source_map.json"
        and item["change"] == "added"
        for item in changes
    )


def test_empty_directory_is_invalid_not_clean(tmp_path: Path) -> None:
    before = make_analysis_dir(tmp_path / "before")
    empty = tmp_path / "empty"
    empty.mkdir()

    report = compare_dirs(before, empty)
    assert report["status"] == "invalid_input"
    assert report["errors"]
    assert "空目录" in report["errors"][0]


def test_invalid_json_fails_closed(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    (after / "w1" / "diagnosis.json").write_text("{broken json", encoding="utf-8")

    report = compare_dirs(before, after)
    assert report["status"] == "read_error"
    assert any("diagnosis.json" in error for error in report["errors"])


def test_cli_rejects_output_inside_input(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    target = before / "diff.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--before",
            str(before),
            "--after",
            str(after),
            "--out",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert not target.exists()


def test_cli_writes_diff_and_fails_closed_for_empty_dir(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    _edit_json(
        after / "w1" / "operator_table.json",
        lambda data: data["records"][0].update({"calls": 11}),
    )
    target = tmp_path / "diff.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--before",
            str(before),
            "--after",
            str(after),
            "--out",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "compared"
    assert payload["summary"]["changed"] is True
    assert any(item["path"].endswith(".calls") for item in payload["changes"])

    empty = tmp_path / "empty"
    empty.mkdir()
    broken_target = tmp_path / "broken.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--before",
            str(before),
            "--after",
            str(empty),
            "--out",
            str(broken_target),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert json.loads(broken_target.read_text(encoding="utf-8"))["status"] == "invalid_input"


def test_non_object_json_is_rejected_not_compared(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    for root in (before, after):
        root.mkdir()
        (root / "run_manifest.json").write_text("[]", encoding="utf-8")

    report = compare_dirs(before, after)

    assert report["status"] == "read_error"
    assert report["summary"]["change_count"] == 0
    assert any("顶层应为 JSON 对象" in error for error in report["errors"])


def test_unclosed_quote_in_csv_fails_closed(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    path = after / "operator_summary.csv"
    path.write_text(
        path.read_text(encoding="utf-8-sig") + '"unclosed\n',
        encoding="utf-8",
    )

    report = compare_dirs(before, after)

    assert report["status"] == "read_error"
    assert any("operator_summary.csv" in error for error in report["errors"])


def test_short_or_long_csv_rows_fail_closed(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    path = after / "operator_summary.csv"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    lines[1] = lines[1] + ",extra"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    report = compare_dirs(before, after)

    assert report["status"] == "read_error"
    assert any("短行/长行" in error for error in report["errors"])


def test_duplicate_opportunity_keys_are_both_kept(tmp_path: Path) -> None:
    before = make_analysis_dir(tmp_path / "before")
    after = tmp_path / "after"
    shutil.copytree(before, after)

    payload = json.loads((before / "w1" / "opportunities.json").read_text(encoding="utf-8"))
    first = opportunity("w1")
    second = opportunity("w1")
    second["opportunity_id"] = "OPP-002"
    second["hypothesis"] = "另一条同键机会"
    payload["opportunities"] = [first, second]
    payload["count"] = 2
    (before / "w1" / "opportunities.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (after / "w1" / "opportunities.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    changed = json.loads((after / "w1" / "opportunities.json").read_text(encoding="utf-8"))
    changed["opportunities"][0]["evidence_level"] = "probable"
    (after / "w1" / "opportunities.json").write_text(
        json.dumps(changed, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    report = compare_dirs(before, after)

    assert report["status"] == "compared"
    assert any(
        item["path"].endswith(".evidence_level")
        and item["after"] == "probable"
        for item in report["changes"]
    ), report["changes"]
    # The second, unchanged same-key opportunity must not be dropped silently.
    assert not any(
        item["change"] in ("added", "removed") and item["category"] == "opportunity"
        for item in report["changes"]
    )


def test_trusted_level_failure_is_unknown_not_raw_confirmed(monkeypatch) -> None:
    import opt_discovery.schemas.evidence_gate as gate

    def explode(item):
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(gate, "trusted_level_for_mapping", explode)

    level, reason = _trusted_level({"evidence_level": "confirmed"})

    assert level == "unknown"
    assert "confirmed" in (reason or "")
    assert "trust_check_failed" in (reason or "")


def test_float_tolerance_must_be_finite_and_non_negative(tmp_path: Path) -> None:
    before = make_analysis_dir(tmp_path / "before")
    after = make_analysis_dir(tmp_path / "after")

    with pytest.raises(ValueError):
        compare_dirs(before, after, float_tolerance=float("nan"))
    with pytest.raises(ValueError):
        compare_dirs(before, after, float_tolerance=-1.0)


def test_zero_and_missing_are_not_the_same_value(tmp_path: Path) -> None:
    before, after = _copy_pair(tmp_path)
    path = after / "w1" / "operator_table.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["calls"] = 0
    payload["records"][0].pop("total_time_us")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    report = compare_dirs(before, after)

    assert report["status"] == "compared"
    changes = {item["path"]: item for item in report["changes"]}
    assert changes["row:MatMul|kernel.calls"]["before"] == 10
    assert changes["row:MatMul|kernel.calls"]["after"] == 0
    missing = changes["row:MatMul|kernel.total_time_us"]
    assert "after" not in missing
    assert missing["before"] == 100.0
