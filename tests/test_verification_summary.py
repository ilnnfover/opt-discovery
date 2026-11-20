"""AE-08: verification summary tests.

State mapping is tested directly; integration covers the real ``verify_real.py``
entry with no data (must be BLOCKED, not PASS), NOT_RUN for unselected steps,
fresh output directories, logs and artifact-freshness fields.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _verification_helpers import make_analysis_dir
from conftest import REPO_ROOT

from tools.verification import (
    BLOCKED,
    FAIL,
    NOT_RUN,
    PASS,
    SKIPPED,
    STATE_EXIT,
    run_verification,
    state_for_exit,
)

SCRIPT = REPO_ROOT / "scripts" / "verify_summary.py"


@dataclass
class _Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_five_state_exit_mapping() -> None:
    assert state_for_exit("command", 0) == PASS
    # GNU Make folds a failing sub-command into 2; that is FAIL, not BLOCKED.
    assert state_for_exit("command", 2) == FAIL
    assert state_for_exit("command", 1) == FAIL

    assert state_for_exit("verify_real", 0) == PASS
    assert state_for_exit("verify_real", 1) == FAIL
    assert state_for_exit("verify_real", 2) == BLOCKED
    assert state_for_exit("verify_real", 3) == SKIPPED
    assert state_for_exit("verify_real", 9) == FAIL
    assert state_for_exit("verify_real", None) == FAIL

    assert STATE_EXIT == {PASS: 0, FAIL: 1, BLOCKED: 2, SKIPPED: 3, NOT_RUN: 4}


def test_failure_still_writes_summary_and_logs(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, cwd, env):
        calls.append(list(argv))
        if "check" in argv:
            return _Result(2, "ruff failed\n", "make: *** [check] Error 1\n")
        return _Result(0, "485 passed, 29 skipped in 12.3s\n", "")

    out = tmp_path / "run"
    summary, code = run_verification(out=out, python="python", process_runner=runner)

    assert code == 1
    assert summary["overall_state"] == FAIL
    steps = {step["name"]: step for step in summary["steps"]}
    assert steps["check"]["state"] == FAIL
    assert steps["check"]["exit_code"] == 2  # preserved exactly; not folded into BLOCKED
    assert steps["verify"]["state"] == PASS
    assert steps["verify"]["pytest"]["passed"] == 485
    assert steps["real"]["state"] == NOT_RUN
    assert (out / "verification_summary.json").is_file()
    check_log = out / steps["check"]["log"]
    assert check_log.is_file()
    text = check_log.read_text(encoding="utf-8")
    assert "exit_code=2" in text
    assert "ruff failed" in text
    assert calls[0][:2] == ["make", "check"]


def test_pytest_counts_do_not_override_a_failure(tmp_path: Path) -> None:
    def runner(argv, cwd, env):
        if "verify" in argv:
            return _Result(1, "485 passed, 1 failed in 3s\n", "")
        return _Result(0, "", "")

    summary, code = run_verification(
        out=tmp_path / "run", python="python", process_runner=runner
    )
    verify = {step["name"]: step for step in summary["steps"]}["verify"]
    assert verify["state"] == FAIL
    assert verify["pytest"]["passed"] == 485
    assert verify["pytest"]["failed"] == 1
    assert code == 1


def test_unselected_steps_are_not_run(tmp_path: Path) -> None:
    out = tmp_path / "run"
    summary, code = run_verification(
        out=out,
        python="python",
        check=False,
        verify=False,
        real=False,
    )
    assert code == 4
    assert summary["overall_state"] == NOT_RUN
    assert {step["state"] for step in summary["steps"]} == {NOT_RUN}
    assert summary["claims"]["real_data_regression"] == NOT_RUN
    assert summary["claims"]["npu_experiments"] == NOT_RUN
    assert summary["claims"]["full_analysis"] == NOT_RUN
    names = {item["name"] for item in summary["not_executed"]}
    assert {"real", "npu_experiments", "full_analysis"} <= names


def test_real_entry_without_data_is_blocked(tmp_path: Path) -> None:
    out = tmp_path / "real-run"
    env = {
        key: value
        for key, value in __import__("os").environ.items()
        if key != "OPT_DISCOVERY_DATA_ROOT"
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--out",
            str(out),
            "--no-check",
            "--no-verify",
            "--real",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2, proc.stderr
    payload = json.loads((out / "verification_summary.json").read_text(encoding="utf-8"))
    real = {step["name"]: step for step in payload["steps"]}["real"]
    assert real["state"] == BLOCKED
    assert real["exit_code"] == 2
    assert real["reason"] == "missing_real_data"
    assert (out / real["log"]).is_file()
    assert payload["overall_state"] == BLOCKED
    assert payload["claims"]["real_data_regression"] == BLOCKED


def test_existing_output_dir_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    (out / "old.txt").write_text("old", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--out",
            str(out),
            "--no-check",
            "--no-verify",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "拒绝复用" in proc.stderr
    assert not (out / "verification_summary.json").exists()


def test_stop_on_fail_marks_dependent_steps_not_run(tmp_path: Path) -> None:
    def runner(argv, cwd, env):
        if "check" in argv:
            return _Result(2, "lint failed", "")
        raise AssertionError("later steps must not execute")

    summary, code = run_verification(
        out=tmp_path / "run",
        python="python",
        real=True,
        stop_on_fail=True,
        process_runner=runner,
    )
    steps = {step["name"]: step for step in summary["steps"]}
    assert steps["check"]["state"] == FAIL
    assert steps["verify"]["state"] == NOT_RUN
    assert steps["real"]["state"] == NOT_RUN
    assert "after check failed" in steps["real"]["reason"]
    assert code == 1


def test_artifact_diff_is_a_summary_step(tmp_path: Path) -> None:
    before = make_analysis_dir(tmp_path / "before")
    after = make_analysis_dir(tmp_path / "after")
    table = after / "w1" / "operator_table.json"
    payload = json.loads(table.read_text(encoding="utf-8"))
    payload["records"][0]["calls"] = 11
    table.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    summary, code = run_verification(
        out=tmp_path / "run",
        python="python",
        check=False,
        verify=False,
        artifacts_a=before,
        artifacts_b=after,
    )
    diff = summary["artifacts"]["artifact_diff"]
    assert diff["status"] == "compared"
    assert diff["summary"]["changed"] is True
    assert diff["path"] == "artifact_diff.json"
    assert (tmp_path / "run" / "artifact_diff.json").is_file()
    step = {item["name"]: item for item in summary["steps"]}["artifact_diff"]
    assert step["state"] == PASS
    # In-process comparison: no fake external argv/exit code.
    assert step["argv"] is None
    assert step["exit_code"] is None
    assert step["invocation"]["callable"] == "tools.artifact_diff.compare_dirs"
    assert step["invocation"]["arguments"]["before"] == str(before)
    assert (tmp_path / "run" / step["log"]).is_file()
    # Comparing alone is a real selected execution item.
    assert code == 0
    assert summary["overall_state"] == PASS


def test_artifact_diff_invalid_input_fails_the_summary(tmp_path: Path) -> None:
    before = make_analysis_dir(tmp_path / "before")
    empty = tmp_path / "empty"
    empty.mkdir()

    summary, code = run_verification(
        out=tmp_path / "run",
        python="python",
        check=False,
        verify=False,
        artifacts_a=before,
        artifacts_b=empty,
    )
    step = {item["name"]: item for item in summary["steps"]}["artifact_diff"]
    assert step["state"] == FAIL
    assert step["reason"] == "artifact_diff_invalid_input"
    assert code == 1
    assert summary["overall_state"] == FAIL
    assert summary["artifacts"]["artifact_diff"]["status"] == "invalid_input"


def test_artifact_read_error_is_not_a_green_run(tmp_path: Path) -> None:
    before = make_analysis_dir(tmp_path / "before")
    after = make_analysis_dir(tmp_path / "after")
    (after / "w1" / "diagnosis.json").write_text("{broken", encoding="utf-8")

    summary, code = run_verification(
        out=tmp_path / "run",
        python="python",
        check=False,
        verify=False,
        artifacts_a=before,
        artifacts_b=after,
    )
    step = {item["name"]: item for item in summary["steps"]}["artifact_diff"]
    assert step["state"] == FAIL
    assert step["reason"] == "artifact_diff_read_error"
    assert code == 1


def test_cpu_checks_strip_pytest_addopts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    seen: list[dict[str, str]] = []

    def runner(argv, cwd, env):
        seen.append(dict(env))
        return _Result(0, "533 tests collected\n", "")

    run_verification(out=tmp_path / "run", python="python", process_runner=runner)

    assert seen
    assert all("PYTEST_ADDOPTS" not in env for env in seen)


def test_real_entry_path_is_fixed(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(argv, cwd, env):
        seen.append(list(argv))
        return _Result(2, "", "")

    summary, code = run_verification(
        out=tmp_path / "run",
        python="custom-python",
        check=False,
        verify=False,
        real=True,
        process_runner=runner,
    )
    assert seen[0] == ["custom-python", str(REPO_ROOT / "scripts" / "verify_real.py")]
    # The configured interpreter argv is recorded; the wrapper's own version is not.
    assert summary["python"] == "custom-python"
    assert code == 2


def test_real_script_override_is_rejected(tmp_path: Path) -> None:
    fake = tmp_path / "pass.py"
    fake.write_text("print('pass')\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--out",
            str(tmp_path / "run"),
            "--no-check",
            "--no-verify",
            "--real",
            "--real-script",
            str(fake),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "unrecognized arguments" in proc.stderr
    assert not (tmp_path / "run").exists()


def test_python_override_is_rejected(tmp_path: Path) -> None:
    # The CLI must reuse the interpreter that launched it; a trivial /bin/true
    # stand-in would otherwise turn --real into a false green.
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--out",
            str(tmp_path / "run"),
            "--no-check",
            "--no-verify",
            "--real",
            "--python",
            "/bin/true",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "unrecognized arguments" in proc.stderr
    assert not (tmp_path / "run").exists()


def test_relative_out_is_resolved_against_caller_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    summary, code = run_verification(
        out="relrun",
        python="python",
        check=False,
        verify=False,
    )
    assert (tmp_path / "relrun" / "verification_summary.json").is_file()
    assert summary["overall_state"] == NOT_RUN
    assert code == 4


def test_custom_output_overrides_are_rejected(tmp_path: Path) -> None:
    for option in ("--demo-out", "--artifact-out"):
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--out",
                str(tmp_path / option.strip("-")),
                "--no-check",
                "--no-verify",
                option,
                str(tmp_path / "somewhere.json"),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
        assert "unrecognized arguments" in proc.stderr


def test_output_directory_inside_artifact_input_is_rejected(tmp_path: Path) -> None:
    before = make_analysis_dir(tmp_path / "before")
    after = make_analysis_dir(tmp_path / "after")
    try:
        run_verification(
            out=before / "summary",
            python="python",
            check=False,
            verify=False,
            artifacts_a=before,
            artifacts_b=after,
        )
    except ValueError as exc:
        assert "before/after" in str(exc)
    else:  # pragma: no cover - explicit failure if the guard disappears
        raise AssertionError("expected the output-directory guard to reject the path")
    assert not (before / "summary").exists()
