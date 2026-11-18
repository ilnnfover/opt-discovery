#!/usr/bin/env python3
"""真实数据回归入口（``make verify-real``）。

契约（结构化判定，不解析 pytest 的人类可读摘要）：

* 缺数据 / 目录不完整 -> BLOCKED，退出码 2，**不是** PASS；
* 有用例被 skip（含全部 skip）-> SKIPPED，退出码 3，不能记为通过；
* 全部执行且通过 -> PASS，退出码 0；失败 -> 1（与 pytest 的非零结果解耦）。
* ``--expect-blocked``：供 CI 使用，只有**内部状态**为 BLOCKED 才算通过；
  任何非 BLOCKED（含 pytest 因中断/收集错误返回 2 的情形）都判失败。

判定依据是 ``--junitxml`` 产出的结构化结果（testcase/skipped/failure/error 计数），
因此 ``PYTEST_ADDOPTS=-q`` 之类的环境变量不会让「全部跳过」被误判为 PASS。
结果为缺失/无法解析时按失败关闭（fail closed）。

数据根目录来自 ``OPT_DISCOVERY_DATA_ROOT``，仓库内不保存任何机器路径。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"
REAL_TEST = REPO_ROOT / "tests" / "test_real_data.py"


class Outcome(Enum):
    """verify-real 的内部状态。退出码由状态映射，避免与 pytest 退出码混用。"""

    PASS = "pass"
    SKIPPED = "skipped"
    FAILED = "failed"
    BLOCKED = "blocked"


EXIT_CODES = {
    Outcome.PASS: 0,
    Outcome.SKIPPED: 3,
    Outcome.FAILED: 1,
    Outcome.BLOCKED: 2,
}
EXIT_GATE_FAILED = 1


@dataclass(frozen=True)
class TestRun:
    """junit 里的用例计数。"""

    tests: int
    skipped: int
    failed: int
    errors: int

    @property
    def executed(self) -> int:
        return self.tests - self.skipped


def expected_exports() -> dict[str, str]:
    """workload_id -> labels.export_dir（四组统一补采的目录名）。"""
    mapping: dict[str, str] = {}
    for path in sorted(WORKLOAD_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        label = (raw.get("labels") or {}).get("export_dir")
        if not label:
            continue
        mapping[str(raw.get("workload_id") or path.stem)] = str(label)
    return mapping


def parse_junit(path: str | Path) -> TestRun:
    """读取 junit xml 的用例计数；文件缺失或格式错误时抛异常（上层按失败关闭）。"""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    skipped = sum(1 for case in cases if case.find("skipped") is not None)
    failed = sum(1 for case in cases if case.find("failure") is not None)
    errors = sum(1 for case in cases if case.find("error") is not None)
    return TestRun(tests=len(cases), skipped=skipped, failed=failed, errors=errors)


def classify(returncode: int, run: TestRun) -> Outcome:
    """把 pytest 退出码 + 结构化计数映射为内部状态。

    顺序（不可交换）：非零结果/失败计数 -> FAILED；未收集到用例 -> FAILED；
    有跳过（含全跳过）-> SKIPPED；否则 PASS。不能把「全部跳过」当通过。
    """
    if returncode != 0 or run.failed or run.errors:
        return Outcome.FAILED
    if run.tests == 0:
        return Outcome.FAILED
    if run.executed <= 0 or run.skipped > 0:
        return Outcome.SKIPPED
    return Outcome.PASS


def describe(outcome: Outcome, run: TestRun | None = None) -> str:
    if outcome is Outcome.BLOCKED:
        return "BLOCKED: 真实数据回归未执行（缺数据），不是 PASS。"
    if run is None:
        return f"{outcome.value.upper()}: 未获得结构化结果。"
    detail = f"{run.executed} passed, {run.skipped} skipped, {run.failed} failed, {run.errors} errors"
    if outcome is Outcome.PASS:
        return f"PASS: 真实数据回归全部执行通过（{detail}）。"
    if outcome is Outcome.SKIPPED:
        return f"SKIPPED: 有 {run.skipped} 个真实回归用例未执行，不能记为 PASS（{detail}）。"
    return f"FAIL: 真实数据回归失败（{detail}），见上方 pytest 输出。"


def gate_exit(outcome: Outcome, *, expect_blocked: bool) -> int:
    """``--expect-blocked`` 的判定：只有内部状态 BLOCKED 才算通过。

    不能比较退出码整数：pytest 因中断/收集错误也可能返回 2，会与 BLOCKED 撞码。
    """
    if not expect_blocked:
        return EXIT_CODES[outcome]
    if outcome is Outcome.BLOCKED:
        print("OK: 缺数据时按预期 BLOCKED，未把缺数据当成 PASS。")
        return 0
    print(
        f"GATE FAILED: --expect-blocked 要求 BLOCKED，实际 {outcome.value}；"
        "缺数据保护可能失效（例如错误地返回 PASS 或与 pytest 退出码撞码）。"
    )
    return EXIT_GATE_FAILED


def _run_real_test() -> tuple[Outcome, str, TestRun | None]:
    with tempfile.TemporaryDirectory(prefix="opt-verify-real-") as tmp:
        junit = Path(tmp) / "junit.xml"
        env = dict(os.environ)
        # 不解析人类可读摘要，但仍清除可能改变收集/输出行为的 pytest 环境变量
        env.pop("PYTEST_ADDOPTS", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(REAL_TEST),
                f"--junitxml={junit}",
                "-p",
                "no:cacheprovider",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)

        if not junit.is_file():
            return (
                Outcome.FAILED,
                "FAIL: pytest 未产出 junit 结果文件，无法判定执行情况（按失败关闭）。",
                None,
            )
        try:
            run = parse_junit(junit)
        except (ET.ParseError, OSError) as exc:
            return Outcome.FAILED, f"FAIL: junit 结果无法解析（{exc}），按失败关闭。", None

        outcome = classify(proc.returncode, run)
        return outcome, describe(outcome, run), run


def _run() -> tuple[Outcome, str, TestRun | None]:
    root_raw = os.environ.get("OPT_DISCOVERY_DATA_ROOT", "").strip()
    expected = expected_exports()

    if not root_raw:
        print("BLOCKED: OPT_DISCOVERY_DATA_ROOT 未设置，真实数据回归未执行。")
        print(f"  需要四组导出：{', '.join(sorted(expected.values()))}")
        print("  解除条件：export OPT_DISCOVERY_DATA_ROOT=<统一补采根目录>")
        return Outcome.BLOCKED, describe(Outcome.BLOCKED), None

    root = Path(root_raw)
    if not root.is_dir():
        print(f"BLOCKED: 数据根目录不存在或不可访问（OPT_DISCOVERY_DATA_ROOT={root_raw!r}）。")
        return Outcome.BLOCKED, describe(Outcome.BLOCKED), None

    missing = [f"{workload} -> {label}" for workload, label in expected.items()
               if not (root / label).is_dir()]
    if missing:
        print("BLOCKED: 缺少以下导出目录，真实回归未执行（不是 PASS）：")
        for item in missing:
            print(f"  - {item}")
        return Outcome.BLOCKED, describe(Outcome.BLOCKED), None

    print(f"[verify-real] 数据根目录: {root}")
    print(f"[verify-real] 四组导出均已发现: {', '.join(sorted(expected.values()))}")
    return _run_real_test()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实数据回归入口（缺数据必须 BLOCKED）")
    parser.add_argument(
        "--expect-blocked",
        action="store_true",
        help="断言本次运行确实因缺数据而 BLOCKED（CI 用；PASS/SKIPPED/FAILED 均判失败）",
    )
    args = parser.parse_args(argv)
    outcome, message, _ = _run()
    print(message)
    return gate_exit(outcome, expect_blocked=args.expect_blocked)


if __name__ == "__main__":
    raise SystemExit(main())
