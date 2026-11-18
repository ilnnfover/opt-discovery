"""AE-01：``verify-real`` 的退出契约必须可判定。

要点：
* pytest 全 skip 时退出码仍是 0，不能算 PASS；
* 失败与跳过并存时必须报 FAIL（否则自动化无法区分「回归失败」与「缺数据」）；
* CI 的 ``--expect-blocked`` 必须断言「恰为 2」，假 PASS(0)/3/其他错误都要判失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_real  # noqa: E402


def test_pass_requires_executed_cases() -> None:
    code, message = verify_real.classify(0, "15 passed in 3.16s")
    assert code == 0
    assert message.startswith("PASS")
    assert "15 passed" in message


def test_any_skip_is_not_a_pass() -> None:
    code, message = verify_real.classify(0, "12 passed, 3 skipped in 1.02s")
    assert code == verify_real.EXIT_SKIPPED
    assert message.startswith("SKIPPED")


def test_failure_propagates_exit_code() -> None:
    code, message = verify_real.classify(1, "4 failed, 6 passed in 0.4s")
    assert code == 1
    assert message.startswith("FAIL")


def test_failure_with_skips_is_failure_not_skipped() -> None:
    """失败 + 跳过并存：必须先报 FAIL，不能被 skip 分类掩盖。"""
    code, message = verify_real.classify(1, "2 failed, 3 skipped in 0.9s")
    assert code == 1
    assert message.startswith("FAIL")

    code, message = verify_real.classify(2, "1 error, 5 skipped in 0.2s")
    assert code == 2
    assert message.startswith("FAIL")


def test_missing_data_root_is_blocked(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPT_DISCOVERY_DATA_ROOT", raising=False)
    code = verify_real.main([])

    assert code == verify_real.EXIT_BLOCKED
    assert "BLOCKED" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (verify_real.EXIT_BLOCKED, 0),   # 唯一允许：确实是 BLOCKED
        (0, verify_real.EXIT_GATE_FAILED),   # 假 PASS
        (1, verify_real.EXIT_GATE_FAILED),   # 回归失败不等于「缺数据」
        (verify_real.EXIT_SKIPPED, verify_real.EXIT_GATE_FAILED),  # skip 不等于 BLOCKED
        (5, verify_real.EXIT_GATE_FAILED),   # 其他错误
    ],
)
def test_expect_blocked_gate_accepts_only_two(actual: int, expected: int) -> None:
    assert verify_real.gate_exit(actual, expect_blocked=True) == expected


def test_gate_is_transparent_without_flag() -> None:
    for code in (0, 1, 2, 3, 5):
        assert verify_real.gate_exit(code, expect_blocked=False) == code


def test_expect_blocked_mode_passes_when_data_is_absent(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPT_DISCOVERY_DATA_ROOT", raising=False)
    code = verify_real.main(["--expect-blocked"])

    assert code == 0
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "OK: 缺数据时按预期 BLOCKED" in out


def test_expect_blocked_rejects_false_pass(monkeypatch, capsys) -> None:
    """负向：脚本错误返回 0（假 PASS）时，CI 闸门必须失败。"""
    monkeypatch.setattr(verify_real, "_run", lambda: 0)
    code = verify_real.main(["--expect-blocked"])

    assert code == verify_real.EXIT_GATE_FAILED
    assert "GATE FAILED" in capsys.readouterr().out


def test_expect_blocked_rejects_skip(monkeypatch, capsys) -> None:
    monkeypatch.setattr(verify_real, "_run", lambda: verify_real.EXIT_SKIPPED)
    code = verify_real.main(["--expect-blocked"])

    assert code == verify_real.EXIT_GATE_FAILED
    assert "GATE FAILED" in capsys.readouterr().out
