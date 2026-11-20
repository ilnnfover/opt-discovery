"""Machine-readable verification summary (AE-08).

Design rules
------------
* Reuse the existing entry points: ``make check``, ``make verify`` and
  ``scripts/verify_real.py``.  Do not re-run individual linters or tests here.
* Five states only: ``PASS`` / ``FAIL`` / ``BLOCKED`` / ``SKIPPED`` / ``NOT_RUN``.
* ``verify_real.py`` is executed *directly* so its 0/1/2/3 contract survives.
  GNU Make folds a failing sub-command into exit 2; ``make check`` / ``make verify``
  therefore map every non-zero exit to ``FAIL`` (never ``BLOCKED``).
* A summary is written even when a step fails.  The tool's own exit code stays
  non-zero in that case.
* Real-data verification is opt-in.  NPU and full real analysis are always
  reported as ``NOT_RUN`` unless someone actually executes them outside this tool.

Typical use::

    python scripts/verify_summary.py --out /tmp/verify-run
    python scripts/verify_summary.py --out /tmp/verify-run-real --real
    python scripts/verify_summary.py --out /tmp/verify-run-diff \
        --artifacts-a /tmp/before --artifacts-b /tmp/after
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .artifact_diff import compare_dirs
from .artifact_diff import write_report as write_diff_report

SCHEMA_VERSION = "1.0"

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
SKIPPED = "SKIPPED"
NOT_RUN = "NOT_RUN"
STATES = (PASS, FAIL, BLOCKED, SKIPPED, NOT_RUN)
STATE_EXIT = {PASS: 0, FAIL: 1, BLOCKED: 2, SKIPPED: 3, NOT_RUN: 4}

_KIND_BY_STEP = {
    "check": "command",
    "verify": "command",
    "real": "verify_real",
}
_REAL_STATE_BY_EXIT = {
    0: PASS,
    1: FAIL,
    2: BLOCKED,
    3: SKIPPED,
}

ProcessRunner = Callable[[Sequence[str], Path, Mapping[str, str]], Any]


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def state_for_exit(kind: str, exit_code: int | None) -> str:
    """Map an exit code to one of the five states.

    ``kind`` is ``"command"`` for make targets and ``"verify_real"`` for the
    real-data entry point.  A timeout/exception is represented by ``None`` and
    is always ``FAIL`` (it did execute, but did not produce a usable result).
    """
    if exit_code is None:
        return FAIL
    if kind == "verify_real":
        return _REAL_STATE_BY_EXIT.get(int(exit_code), FAIL)
    return PASS if int(exit_code) == 0 else FAIL


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_process(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> _ProcessResult:
    proc = subprocess.run(
        list(argv),
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
    )
    return _ProcessResult(proc.returncode, proc.stdout, proc.stderr)


_PYTEST_PATTERNS = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "errors": re.compile(r"(\d+)\s+error"),
}


def _extract_pytest_counts(text: str) -> dict[str, Any] | None:
    """Optional counts for humans.  Never used to decide a state."""
    counts: dict[str, int] = {}
    for name, pattern in _PYTEST_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            counts[name] = int(matches[-1])
    if not counts:
        return None
    counts["source"] = "optional_human_readable_summary"
    return counts


def _write_log(
    path: Path,
    *,
    argv: Sequence[str],
    exit_code: int | None,
    state: str,
    stdout: str,
    stderr: str,
) -> None:
    lines = [
        f"$ {' '.join(str(part) for part in argv)}",
        f"exit_code={exit_code}",
        f"state={state}",
        "",
        "--- stdout ---",
        stdout,
        "--- stderr ---",
        stderr,
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _not_run_step(name: str, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "argv": None,
        "exit_code": None,
        "state": NOT_RUN,
        "reason": reason,
        "log": None,
        "duration_s": None,
    }


def _execute_step(
    *,
    name: str,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    process_runner: ProcessRunner,
) -> dict[str, Any]:
    started = time.monotonic()
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    try:
        result = process_runner(argv, cwd, env)
        exit_code = int(getattr(result, "returncode", 1))
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        state = state_for_exit(_KIND_BY_STEP[name], exit_code)
        reason = None
        if state == FAIL:
            reason = "command_failed"
        elif state == BLOCKED:
            reason = "missing_real_data"
        elif state == SKIPPED:
            reason = "real_regression_skipped"
    except FileNotFoundError as exc:
        state = FAIL
        reason = f"command_not_found: {exc}"
        stderr = traceback.format_exc()
    except Exception as exc:  # noqa: BLE001 - every step must settle into the summary
        state = FAIL
        reason = f"{type(exc).__name__}: {exc}"
        stderr = traceback.format_exc()
    duration = round(time.monotonic() - started, 3)
    _write_log(
        log_path,
        argv=argv,
        exit_code=exit_code,
        state=state,
        stdout=stdout,
        stderr=stderr,
    )
    step: dict[str, Any] = {
        "name": name,
        "argv": list(argv),
        "exit_code": exit_code,
        "state": state,
        "reason": reason,
        "log": str(log_path.relative_to(log_path.parent.parent)),
        "duration_s": duration,
    }
    if name == "verify":
        counts = _extract_pytest_counts(stdout + "\n" + stderr)
        if counts:
            step["pytest"] = counts
    return step


def _git_identity(repo_root: Path) -> dict[str, str | None]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None

    return {"commit": run("rev-parse", "--short", "HEAD"), "branch": run("branch", "--show-current")}


def _combined_state(states: Sequence[str]) -> str:
    if FAIL in states:
        return FAIL
    if BLOCKED in states:
        return BLOCKED
    if SKIPPED in states:
        return SKIPPED
    if states and all(state == PASS for state in states):
        return PASS
    return NOT_RUN


def _claims(steps: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    by_name = {str(step["name"]): str(step["state"]) for step in steps}
    return {
        "cpu_checks": _combined_state(
            [by_name.get("check", NOT_RUN), by_name.get("verify", NOT_RUN)]
        ),
        "real_data_regression": by_name.get("real", NOT_RUN),
        "npu_experiments": NOT_RUN,
        "full_analysis": NOT_RUN,
    }


def _not_executed(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for step in steps:
        if step["state"] != PASS:
            items.append(
                {
                    "name": str(step["name"]),
                    "state": str(step["state"]),
                    "reason": str(step.get("reason") or "not executed"),
                }
            )
    items.append(
        {
            "name": "npu_experiments",
            "state": NOT_RUN,
            "reason": "this tool never runs NPU workloads; use an authorized experiment separately",
        }
    )
    items.append(
        {
            "name": "full_analysis",
            "state": NOT_RUN,
            "reason": "this tool does not run analyze over real exports; run it explicitly and review its run_manifest",
        }
    )
    return items


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> Path:
    try:
        import tempfile

        handle, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, indent=2, ensure_ascii=False))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        raise
    return path


def _execute_artifact_diff(
    *,
    before: Path,
    after: Path,
    out_dir: Path,
    float_tolerance: float,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run the requested before/after diff as a first-class summary step.

    The comparison is an *execution item*: an unreadable input, a malformed
    artifact or a failed write is a non-zero result and cannot be hidden behind
    an otherwise green CPU run.  A legal comparison of differing artifacts is a
    successful comparison (``PASS``); it is still evidence, not a quality gate.
    """
    started = time.monotonic()
    target = out_dir / "artifact_diff.json"
    log_path = out_dir / "logs" / "artifact_diff.log"
    # This is an in-process function call, not a subprocess.  Record it as such
    # instead of inventing an argv/exit code that never existed.
    invocation = {
        "kind": "python_callable",
        "callable": "tools.artifact_diff.compare_dirs",
        "arguments": {
            "before": str(before),
            "after": str(after),
            "before_label": before.name,
            "after_label": after.name,
            "float_tolerance": float_tolerance,
        },
    }
    # Human-readable hint for the log; the summary keeps ``argv``/``exit_code`` null.
    log_argv = [
        "<python-callable>",
        invocation["callable"],
        json.dumps(invocation["arguments"], ensure_ascii=False),
    ]
    errors: list[str] = []
    artifact_diff: dict[str, Any]
    state = FAIL
    reason: str | None = None
    try:
        report = compare_dirs(
            before,
            after,
            before_label=before.name,
            after_label=after.name,
            float_tolerance=float_tolerance,
        )
    except Exception as exc:  # noqa: BLE001 - settle every requested check
        errors = [f"{type(exc).__name__}: {exc}"]
        artifact_diff = {
            "path": None,
            "status": "error",
            "summary": {"changed": None, "change_count": None},
            "errors": errors,
        }
        state = FAIL
        reason = "artifact_diff_error"
    else:
        errors = list(report["errors"])
        try:
            write_diff_report(report, target)
        except OSError as exc:
            errors.append(f"写差异报告失败: {exc}")
            artifact_diff = {
                "path": None,
                "status": "write_error",
                "summary": report["summary"],
                "errors": errors,
            }
            state = FAIL
            reason = "artifact_diff_write_error"
        else:
            artifact_diff = {
                "path": target.name,
                "status": report["status"],
                "summary": report["summary"],
                "errors": errors,
            }
            if report["status"] == "compared":
                state = PASS
                reason = None
            else:
                state = FAIL
                reason = f"artifact_diff_{report['status']}"
    _write_log(
        log_path,
        argv=log_argv,
        exit_code=None,
        state=state,
        stdout=json.dumps(artifact_diff, ensure_ascii=False),
        stderr="\n".join(errors),
    )
    steps.append(
        {
            "name": "artifact_diff",
            "argv": None,
            "exit_code": None,
            "invocation": invocation,
            "state": state,
            "reason": reason,
            "log": str(log_path.relative_to(out_dir)),
            "duration_s": round(time.monotonic() - started, 3),
        }
    )
    return artifact_diff


def run_verification(
    *,
    out: str | Path,
    python: str,
    check: bool = True,
    verify: bool = True,
    real: bool = False,
    artifacts_a: str | Path | None = None,
    artifacts_b: str | Path | None = None,
    stop_on_fail: bool = False,
    process_runner: ProcessRunner | None = None,
    repo_root: str | Path | None = None,
    float_tolerance: float = 1e-9,
) -> tuple[dict[str, Any], int]:
    """Run the selected checks and write ``verification_summary.json``.

    Raises ``FileExistsError`` when ``out`` already exists (fresh directory rule).
    """
    if not math.isfinite(float_tolerance) or float_tolerance < 0:
        raise ValueError("--float-tolerance 必须是有限非负数")
    if (artifacts_a is None) != (artifacts_b is None):
        raise ValueError("--artifacts-a 与 --artifacts-b 必须同时提供")
    root = Path(repo_root or Path(__file__).resolve().parent.parent)
    # A relative ``--out`` is anchored at the caller's working directory, not at
    # the repository root used as the subprocess cwd.
    out_dir = Path(out).resolve()
    if artifacts_a is not None and artifacts_b is not None:
        for candidate in (Path(artifacts_a), Path(artifacts_b)):
            if _is_within(out_dir, candidate):
                raise ValueError("输出目录不得位于 before/after 产物目录内")
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "logs").mkdir()
    runner = process_runner or _run_process
    started_at = _utc_now()
    start = time.monotonic()

    # Outputs always live inside the freshly created ``out`` directory; there is
    # no option to redirect them into an existing tree or onto an input file.
    demo = out_dir / "demo"
    # Fixed repository entry point: callers cannot substitute a trivial script.
    real_entry = root / "scripts" / "verify_real.py"

    base_env = dict(os.environ)
    # A developer's pytest addopts (e.g. --collect-only) must not change what
    # "executed" means, and CPU checks must not depend on a private data root.
    base_env.pop("PYTEST_ADDOPTS", None)
    cpu_env = dict(base_env)
    cpu_env.pop("OPT_DISCOVERY_DATA_ROOT", None)

    planned: list[tuple[str, bool, Sequence[str], Mapping[str, str], str]] = []
    planned.append(
        (
            "check",
            check,
            ["make", "check", f"PYTHON={python}"],
            cpu_env,
            "make check",
        )
    )
    planned.append(
        (
            "verify",
            verify,
            ["make", "verify", f"PYTHON={python}", f"DEMO_OUT={demo}"],
            cpu_env,
            "make verify",
        )
    )
    planned.append(
        (
            "real",
            real,
            [python, str(real_entry)],
            base_env,
            "scripts/verify_real.py",
        )
    )

    steps: list[dict[str, Any]] = []
    previous_failure: str | None = None
    for name, selected, argv, env, _label in planned:
        if not selected:
            reason = "not requested"
            if name == "real":
                reason = "not requested (CPU-only default; pass --real to run real-data regression)"
            steps.append(_not_run_step(name, reason))
            continue
        if stop_on_fail and previous_failure is not None:
            steps.append(
                _not_run_step(name, f"not run after {previous_failure} failed (--stop-on-fail)")
            )
            continue
        step = _execute_step(
            name=name,
            argv=argv,
            cwd=root,
            env=env,
            log_path=out_dir / "logs" / f"{name}.log",
            process_runner=runner,
        )
        steps.append(step)
        if step["state"] in (FAIL, BLOCKED):
            previous_failure = name

    artifact_diff: dict[str, Any] | None = None
    diff_selected = artifacts_a is not None and artifacts_b is not None
    if diff_selected:
        if stop_on_fail and previous_failure is not None:
            steps.append(
                _not_run_step(
                    "artifact_diff",
                    f"not run after {previous_failure} failed (--stop-on-fail)",
                )
            )
        else:
            artifact_diff = _execute_artifact_diff(
                before=Path(artifacts_a),
                after=Path(artifacts_b),
                out_dir=out_dir,
                float_tolerance=float_tolerance,
                steps=steps,
            )

    selected_names = {name for name, selected, _argv, _env, _label in planned if selected}
    if diff_selected:
        selected_names.add("artifact_diff")
    selected_states = [
        str(step["state"]) for step in steps if str(step["name"]) in selected_names
    ]
    if any(state == FAIL for state in selected_states):
        overall = FAIL
    elif any(state == BLOCKED for state in selected_states):
        overall = BLOCKED
    elif any(state == SKIPPED for state in selected_states):
        overall = SKIPPED
    elif selected_states and all(state == PASS for state in selected_states):
        overall = PASS
    else:
        overall = NOT_RUN

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": started_at,
        "finished_at": _utc_now(),
        "duration_s": round(time.monotonic() - start, 3),
        "repo": {"root_label": ".", **_git_identity(root)},
        "python": str(python),
        "scope": "cpu+real" if real else "cpu-only",
        "overall_state": overall,
        "selected_steps": sorted(selected_names),
        "overall_statement": (
            "PASS covers only the selected steps. See claims and not_executed: "
            "real data, NPU and full analysis are NOT_RUN unless explicitly executed."
        ),
        "states": list(STATES),
        "state_definitions": {
            PASS: "selected command executed and exited 0 (verify-real also requires exit 0)",
            FAIL: "selected command executed and failed, or did not produce a usable result",
            BLOCKED: "verify-real could not run because required real data is missing",
            SKIPPED: "verify-real ran but pytest skipped cases, so it cannot be PASS",
            NOT_RUN: "the step was not selected or was not reached; never counts as PASS",
        },
        "steps": steps,
        "claims": _claims(steps),
        "not_executed": _not_executed(steps),
        "artifacts": {
            "summary": "verification_summary.json",
            "logs": "logs/",
            "demo": "demo/" if verify else None,
            "artifact_diff": artifact_diff,
        },
    }
    summary_path = _write_json_atomic(out_dir / "verification_summary.json", summary)
    summary["artifacts"]["summary"] = str(summary_path.name)
    return summary, STATE_EXIT[overall]


def _print_summary(summary: Mapping[str, Any]) -> None:
    print(
        f"[verify-summary] overall={summary['overall_state']} "
        f"scope={summary['scope']} -> {summary['artifacts']['summary']}"
    )
    for step in summary["steps"]:
        exit_code = step.get("exit_code")
        detail = "" if exit_code is None else f" exit={exit_code}"
        reason = f" reason={step['reason']}" if step.get("reason") else ""
        print(f"  {step['name']:<13} {step['state']:<7}{detail}{reason}")
    claims = summary["claims"]
    print(
        "  claims: "
        + ", ".join(f"{name}={state}" for name, state in claims.items())
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行 CPU 检查并生成机器可读验证摘要（真实数据需显式 --real）"
    )
    parser.add_argument(
        "--out",
        required=True,
        help="新的输出目录；已存在则拒绝（避免旧日志/旧摘要冒充本次结果）",
    )
    parser.add_argument(
        "--check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="运行 make check（默认开启）",
    )
    parser.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="运行 make verify（默认开启）",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="显式运行 scripts/verify_real.py（缺数据会得到 BLOCKED，不是 PASS）",
    )
    parser.add_argument("--artifacts-a", default=None, help="改动前 analyze 产物目录")
    parser.add_argument("--artifacts-b", default=None, help="改动后 analyze 产物目录")
    parser.add_argument(
        "--stop-on-fail",
        action="store_true",
        help="某步骤失败后不再执行后续步骤（未执行项明确记为 NOT_RUN）",
    )
    parser.add_argument("--float-tolerance", type=float, default=1e-9)
    args = parser.parse_args(argv)

    if bool(args.artifacts_a) != bool(args.artifacts_b):
        print("[verify-summary] --artifacts-a 与 --artifacts-b 必须同时提供", file=sys.stderr)
        return 2

    try:
        summary, code = run_verification(
            out=args.out,
            # The public CLI always reuses the interpreter that launched it, so
            # choosing a venv also chooses the interpreter for the child scripts.
            python=sys.executable,
            check=args.check,
            verify=args.verify,
            real=args.real,
            artifacts_a=args.artifacts_a,
            artifacts_b=args.artifacts_b,
            stop_on_fail=args.stop_on_fail,
            float_tolerance=args.float_tolerance,
        )
    except FileExistsError:
        print(
            f"[verify-summary] 输出目录已存在，拒绝复用: {args.out}",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(f"[verify-summary] 参数不合法: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - still emit a readable summary if possible
        fallback = {
            "schema_version": SCHEMA_VERSION,
            "overall_state": FAIL,
            "error": f"{type(exc).__name__}: {exc}",
            "steps": [],
        }
        print(json.dumps(fallback, indent=2, ensure_ascii=False))
        print(f"[verify-summary] 无法写入摘要: {exc}", file=sys.stderr)
        return 1

    _print_summary(summary)
    if summary["artifacts"].get("artifact_diff"):
        diff = summary["artifacts"]["artifact_diff"]
        print(
            f"  artifact-diff: status={diff['status']} "
            f"changed={diff['summary'].get('changed')} path={diff['path']}"
        )
    return code


__all__ = [
    "BLOCKED",
    "FAIL",
    "NOT_RUN",
    "PASS",
    "SKIPPED",
    "STATES",
    "STATE_EXIT",
    "main",
    "run_verification",
    "state_for_exit",
]
