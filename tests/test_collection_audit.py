"""统一补采契约的审计。"""

from __future__ import annotations

import json

import yaml

from opt_discovery.analyzer.collection_audit import audit_collection


def _manifest(root, run_id, *, fixed=None, profiling=None, sha="abc123", variables=None, ok=True):
    d = root / f"20261115-{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract_sha256": sha,
        "run_id": run_id,
        "fixed": fixed or {"max_num_seqs": 32, "seed": 42},
        "profiling": profiling or {"switches": ["--aic-metrics=ArithmeticUtilization"]},
        "variables": variables or {"mode": "eager", "graph_mode": "none", "output_length": 1},
        "checks": {"ok": ok},
    }
    (d / "collection_manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _contract(root, ids):
    """写一份只包含指定组的临时契约，避免测试被默认四组契约影响。"""
    path = root / "contract.yaml"
    path.write_text(
        yaml.safe_dump({"runs": [{"id": item} for item in ids]}), encoding="utf-8"
    )
    return path


def test_consistent_runs_pass(tmp_path):
    _manifest(tmp_path, "prefill-eager")
    _manifest(
        tmp_path,
        "decode-graph",
        variables={"mode": "graph", "graph_mode": "full_and_piecewise", "output_length": 64},
    )
    contract = _contract(tmp_path, ["prefill-eager", "decode-graph"])
    report = audit_collection(tmp_path, contract)
    assert report.verdict == "pass"
    assert len(report.runs) == 2


def test_parameter_drift_is_caught(tmp_path):
    _manifest(tmp_path, "prefill-eager")
    _manifest(tmp_path, "decode-eager", fixed={"max_num_seqs": 16, "seed": 42})
    report = audit_collection(tmp_path, _contract(tmp_path, ["prefill-eager", "decode-eager"]))
    assert report.verdict == "fail"
    failed = {item.name for item in report.items if not item.ok}
    assert "fixed" in failed


def test_different_contract_is_caught(tmp_path):
    _manifest(tmp_path, "a", sha="v1")
    _manifest(tmp_path, "b", sha="v2")
    report = audit_collection(tmp_path, _contract(tmp_path, ["a", "b"]))
    assert "contract_sha256" in {item.name for item in report.items if not item.ok}


def test_unexpected_variable_is_caught(tmp_path):
    _manifest(tmp_path, "a", variables={"mode": "eager", "concurrency": 16})
    report = audit_collection(tmp_path, _contract(tmp_path, ["a"]))
    assert "variables" in {item.name for item in report.items if not item.ok}


def test_missing_data_is_reported(tmp_path):
    report = audit_collection(tmp_path, _contract(tmp_path, ["a"]))
    assert report.verdict == "no_data"


def test_missing_run_is_incomplete_not_fail(tmp_path):
    _manifest(tmp_path, "prefill-eager")
    _manifest(tmp_path, "prefill-graph")
    contract = _contract(tmp_path, ["prefill-eager", "prefill-graph", "decode-eager"])
    report = audit_collection(tmp_path, contract)
    assert report.verdict == "incomplete"
    assert {item.name for item in report.items if not item.ok} == {"runs_complete"}


def test_all_runs_present_is_pass(tmp_path):
    _manifest(tmp_path, "a")
    _manifest(tmp_path, "b")
    assert audit_collection(tmp_path, _contract(tmp_path, ["a", "b"])).verdict == "pass"
