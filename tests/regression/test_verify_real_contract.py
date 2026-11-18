"""AE-01：``verify-real`` 的三态退出契约必须可判定。

pytest 在全 skip 时退出码仍是 0；若脚本只看退出码就会把「没跑」显示成 PASS。
"""

from __future__ import annotations

import sys
from pathlib import Path

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


def test_missing_data_root_is_blocked(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPT_DISCOVERY_DATA_ROOT", raising=False)
    code = verify_real.main()

    assert code == verify_real.EXIT_BLOCKED
    assert "BLOCKED" in capsys.readouterr().out
