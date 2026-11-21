"""Real acquisition layout contracts exercised with small CPU exports."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from _helpers import make_timeline_db, make_workload, summary_row, write_rank_summary
from opt_discovery.analyzer.contract import compare_contracts
from opt_discovery.cli import main
from opt_discovery.collectors.profiler_dir import ExportError, discover_all, discover_bundle
from scripts.recollect_contract import _check, _command


@pytest.fixture
def capture(tmp_path: Path):
    root = tmp_path / "exports"
    dest = root / "20261121-prefill-eager"
    workloads = tmp_path / "workloads"
    workloads.mkdir()
    spec = replace(
        make_workload(output_length=None),
        labels={"export_dir": "legacy-alias", "collection_run_id": "prefill-eager"},
    )
    workload = workloads / "eager.yaml"
    workload.write_text(yaml.safe_dump(spec.to_dict()))
    summary = write_rank_summary(
        dest / "PROF", 0,
        [summary_row("MatMul", 10.0, start_us=1_000_010.0)],
    )
    make_timeline_db(summary.parents[1] / "msprof_0.db", tasks=[(1_000_010, 1_000_020)])
    empty = dest / "PROF" / "PROF_reattach"
    empty.mkdir()
    (empty / "msprof_empty.db").write_bytes(b"db only")
    (dest / "workload.log").write_text("ACTIVE_ROUND 0 start 1 end 2 out_tokens 1\n")
    fixed = {"tensor_parallel_size": 1, "active_rounds": 1, "window_rounds": [0], "python": "python3"}
    run = {"id": "prefill-eager", "mode": "eager", "graph_mode": "none", "output_length": 1}
    profiling = {
        "msprof": "msprof", "switches": [], "msproftx": "off", "summary_format": "csv",
    }
    manifest = {
        "run_id": run["id"], "exit_code": 0, "checks": {"ok": True},
        "contract_sha256": "fixture", "fixed": fixed, "profiling": profiling,
        "variables": {k: v for k, v in run.items() if k != "id"},
    }
    path = dest / "collection_manifest.json"
    path.write_text(json.dumps(manifest))
    return dest, workload, path, manifest, run


def test_discovery_and_collection_check_share_layout(capture, tmp_path):
    dest, workload, _, manifest, run = capture
    bundle, = discover_all(workload.parent, dest.parent)
    assert bundle.export_dir == dest
    assert bundle.workload.input.output_length == 1
    assert bundle.window_rounds == (0,)
    assert bundle.rank(0).directory.parent == dest / "PROF"
    checks = _check(dest, manifest, run)
    assert checks["ok"]
    assert checks["excluded_prof_dirs"] == {"PROF_reattach": "no_operator_export"}
    assert checks["active_rounds"] == 1
    assert f"--output={dest}" in _command(manifest, run, tmp_path / "harness.py", dest)

    # timeline uses the actual collection window; historically its default was 1-4.
    out = tmp_path / "timeline.json"
    assert main(["timeline", "--export", str(dest), "--workload", str(workload),
                 "--out", str(out)]) == 0
    assert json.loads(out.read_text())["window"]["rounds"] == [0]

    for prof in (dest / "PROF").iterdir():
        prof.rename(dest / prof.name)
    assert discover_bundle(workload, dest).rank(0).directory.parent == dest
    assert _check(dest, manifest, run)["ok"]


def test_manifest_does_not_bind_failed_or_ambiguous_runs(capture):
    dest, workload, path, manifest, _ = capture
    manifest["exit_code"] = 5
    path.write_text(json.dumps(manifest))
    with pytest.raises(ExportError, match="自检"):
        discover_all(workload.parent, dest.parent)
    manifest["exit_code"] = 0
    path.write_text(json.dumps(manifest))
    alternate = dest.parent / "20261122-full-graph"
    alternate.mkdir()
    other = alternate / path.name
    other.write_text(json.dumps({**manifest, "run_id": "prefill-full-graph"}))
    assert discover_all(workload.parent, dest.parent)[0].export_dir == dest
    other.write_text(json.dumps(manifest))
    with pytest.raises(ExportError, match="多个匹配"):
        discover_all(workload.parent, dest.parent)


def test_compare_contract_rejects_instrument_changes(capture):
    dest, workload, _, _, _ = capture
    spec = discover_bundle(workload, dest).workload

    def payload(switch):
        return {"workload": spec.to_dict(), "meta": {"extra": {
            "collection": {"contract_sha256": "same", "profiling": {"switches": [switch]}}
        }}}

    report = compare_contracts(payload("--aic-metrics=Memory"), payload("--aic-metrics=ArithmeticUtilization"))
    assert report.verdict == "incomparable"
    assert any(check.name == "collection.profiling" and check.status == "fail" for check in report.checks)


def test_timeline_requires_aligned_external_mstx(capture, tmp_path):
    dest, workload, _, _, _ = capture
    trace = tmp_path / "external.db"
    with sqlite3.connect(trace) as connection:
        connection.execute("CREATE TABLE MSTX_EVENTS (startNs, endNs, message)")
        connection.execute("INSERT INTO MSTX_EVENTS VALUES (10, 20, 'step')")
    out = tmp_path / "timeline.json"
    args = ["timeline", "--export", str(dest), "--workload", str(workload),
            "--mstx-db", str(trace), "--out", str(out)]
    assert main(args) == 2
    assert not out.exists()
    with sqlite3.connect(trace) as connection:
        connection.execute("UPDATE MSTX_EVENTS SET startNs=1100000000, endNs=1200000000")
    assert main(args) == 0
    payload = json.loads(out.read_text())
    assert payload["mstx_db"] == str(trace)
    assert payload["mstx_ranges"] == 1
