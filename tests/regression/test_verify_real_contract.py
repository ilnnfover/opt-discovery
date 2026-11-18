"""AE-01：``verify-real`` 的退出契约必须可判定（结构化结果，不解析文本摘要）。

覆盖审查提出的两个场景：
* 环境里多一个 ``-q``（``PYTEST_ADDOPTS``）时，「全部 skip」不得变成 PASS；
* pytest 因中断/收集错误返回 2 时，不得被当成缺数据 BLOCKED。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_real  # noqa: E402


def _junit(tmp_path: Path, *, passed: int = 0, skipped: int = 0, failed: int = 0,
           errors: int = 0) -> Path:
    """写一个最小 junit xml（结构同 pytest --junitxml）。"""
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<testsuites><testsuite tests="{passed + skipped + failed + errors}">',
    ]
    for index in range(passed):
        parts.append(f'<testcase classname="t" name="pass_{index}" time="0.01"/>')
    for index in range(skipped):
        parts.append(
            f'<testcase classname="t" name="skip_{index}" time="0.01"><skipped/></testcase>'
        )
    for index in range(failed):
        parts.append(
            f'<testcase classname="t" name="fail_{index}" time="0.01">'
            f'<failure message="boom"/></testcase>'
        )
    for index in range(errors):
        parts.append(
            f'<testcase classname="t" name="err_{index}" time="0.01">'
            f'<error message="boom"/></testcase>'
        )
    parts.append("</testsuite></testsuites>")
    path = tmp_path / "junit.xml"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def test_parse_junit_counts_each_kind(tmp_path: Path) -> None:
    run = verify_real.parse_junit(
        _junit(tmp_path, passed=3, skipped=2, failed=1, errors=1)
    )
    assert (run.tests, run.skipped, run.failed, run.errors) == (7, 2, 1, 1)
    assert run.executed == 5


def test_parse_junit_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        verify_real.parse_junit(tmp_path / "absent.xml")


@pytest.mark.parametrize(
    ("returncode", "counts", "expected"),
    [
        (0, {"passed": 16}, verify_real.Outcome.PASS),
        (0, {"passed": 16, "skipped": 1}, verify_real.Outcome.SKIPPED),
        (0, {"skipped": 16}, verify_real.Outcome.SKIPPED),          # 全跳过
        (0, {}, verify_real.Outcome.FAILED),                        # 未收集到用例
        (1, {"passed": 10, "failed": 1}, verify_real.Outcome.FAILED),
        (2, {"skipped": 16}, verify_real.Outcome.FAILED),           # pytest 中断，不能算 BLOCKED
        (0, {"passed": 1, "errors": 1}, verify_real.Outcome.FAILED),
    ],
)
def test_classify(returncode: int, counts: dict[str, int], expected) -> None:
    run = verify_real.TestRun(
        tests=sum(counts.values()),
        skipped=counts.get("skipped", 0),
        failed=counts.get("failed", 0),
        errors=counts.get("errors", 0),
    )
    assert verify_real.classify(returncode, run) is expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (verify_real.Outcome.BLOCKED, 0),
        (verify_real.Outcome.PASS, verify_real.EXIT_GATE_FAILED),
        (verify_real.Outcome.SKIPPED, verify_real.EXIT_GATE_FAILED),
        (verify_real.Outcome.FAILED, verify_real.EXIT_GATE_FAILED),
    ],
)
def test_gate_accepts_only_blocked_outcome(outcome, expected: int) -> None:
    assert verify_real.gate_exit(outcome, expect_blocked=True) == expected


def test_gate_is_transparent_without_flag() -> None:
    for outcome in verify_real.Outcome:
        assert verify_real.gate_exit(outcome, expect_blocked=False) == (
            verify_real.EXIT_CODES[outcome]
        )


def test_pytest_interrupt_code_two_is_not_blocked() -> None:
    """P2-1 回归：pytest 退出码 2（中断/收集错误）不得被当作缺数据。"""
    run = verify_real.TestRun(tests=16, skipped=16, failed=0, errors=0)
    outcome = verify_real.classify(2, run)

    assert outcome is verify_real.Outcome.FAILED
    assert verify_real.gate_exit(outcome, expect_blocked=True) == verify_real.EXIT_GATE_FAILED


def test_missing_data_root_is_blocked(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPT_DISCOVERY_DATA_ROOT", raising=False)
    code = verify_real.main([])

    assert code == verify_real.EXIT_CODES[verify_real.Outcome.BLOCKED] == 2
    assert "BLOCKED" in capsys.readouterr().out


def test_expect_blocked_mode_passes_when_data_is_absent(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPT_DISCOVERY_DATA_ROOT", raising=False)
    code = verify_real.main(["--expect-blocked"])

    assert code == 0
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "OK: 缺数据时按预期 BLOCKED" in out


def test_expect_blocked_rejects_false_pass(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        verify_real, "_run", lambda: (verify_real.Outcome.PASS, "PASS: fake", None)
    )
    code = verify_real.main(["--expect-blocked"])

    assert code == verify_real.EXIT_GATE_FAILED
    assert "GATE FAILED" in capsys.readouterr().out


def test_expect_blocked_rejects_skipped(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        verify_real, "_run", lambda: (verify_real.Outcome.SKIPPED, "SKIPPED: fake", None)
    )
    code = verify_real.main(["--expect-blocked"])

    assert code == verify_real.EXIT_GATE_FAILED
    assert "GATE FAILED" in capsys.readouterr().out


def test_all_skipped_with_extra_quiet_is_not_pass(monkeypatch) -> None:
    """P1-1 回归：即使环境里有 ``PYTEST_ADDOPTS=-q``，全跳过也必须是 SKIPPED。

    无数据根时本仓库的真实测试整组 skip；junit 计数不依赖 pytest 的文本摘要。
    """
    monkeypatch.delenv("OPT_DISCOVERY_DATA_ROOT", raising=False)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q --no-header")

    outcome, message, run = verify_real._run_real_test()

    assert run is not None and run.tests > 0
    assert run.executed == 0
    assert outcome is verify_real.Outcome.SKIPPED
    assert message.startswith("SKIPPED")
    assert not message.startswith("PASS")
