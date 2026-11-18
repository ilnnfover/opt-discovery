#!/usr/bin/env python3
"""真实数据回归入口（``make verify-real``）。

契约：
* 缺数据 / 目录不完整 -> 打印 BLOCKED，退出码 2，**不是** PASS；
* 真实回归中有用例被 skip -> 打印 SKIPPED，退出码 3（未执行完不算通过）；
* 全部通过 -> 打印 PASS，退出码 0；失败 -> 透传 pytest 的非零退出码。
* ``--expect-blocked``：供 CI 使用，断言「缺数据时确实返回 BLOCKED」，
  只有实际退出码为 2 才算通过；返回 0（假 PASS）、3、其他错误一律判失败。

失败优先于 skip：pytest 同时出现 failed 与 skipped 时退出码非零，必须先报 FAIL，
否则自动化调用者无法区分「回归失败」与「数据缺失」。

数据根目录来自 ``OPT_DISCOVERY_DATA_ROOT``，仓库内不保存任何机器路径。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"
REAL_TEST = REPO_ROOT / "tests" / "test_real_data.py"

EXIT_BLOCKED = 2
EXIT_SKIPPED = 3
EXIT_GATE_FAILED = 1


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


def classify(returncode: int, output: str) -> tuple[int, str]:
    """把 pytest 结果映射为 verify-real 的四态。

    顺序（不可交换）：非零退出码 -> FAIL；然后 skip -> SKIPPED；最后 PASS。
    pytest 全部 skip 时退出码仍是 0，不能算 PASS；失败+跳过时必须报 FAIL。
    """
    if returncode != 0:
        return returncode, "FAIL: 真实数据回归失败，见上方 pytest 输出。"
    skipped = re.search(r"(\d+) skipped", output)
    if skipped and int(skipped.group(1)) > 0:
        return EXIT_SKIPPED, (
            f"SKIPPED: 有 {skipped.group(1)} 个真实回归用例未执行"
            "（缺数据或场景不可复现），不能记为 PASS。"
        )
    passed = re.search(r"(\d+) passed", output)
    count = passed.group(1) if passed else "?"
    return 0, f"PASS: 真实数据回归全部通过（{count} passed，0 skipped）。"


def gate_exit(actual: int, *, expect_blocked: bool) -> int:
    """``--expect-blocked`` 的判定：只有实际 BLOCKED(2) 才算通过。

    等价于 CI 里「捕获退出码并断言恰为 2」。不要用 ``cmd || [ $? -eq 2 ]``：
    脚本错误返回 0 时该写法会直接通过，把假 PASS 带进流水线。
    """
    if not expect_blocked:
        return actual
    if actual == EXIT_BLOCKED:
        print("OK: 缺数据时按预期 BLOCKED（退出码 2），未把缺数据当成 PASS。")
        return 0
    print(
        f"GATE FAILED: --expect-blocked 要求退出码 {EXIT_BLOCKED}，实际 {actual}；"
        "缺数据保护可能失效（例如错误地返回 0=PASS）。"
    )
    return EXIT_GATE_FAILED


def _run_real_test() -> int:
    # 不要额外传 -q：pyproject 的 addopts 已含 -q，再传会吞掉 "N passed / N skipped"
    # 摘要行，导致 skip 检测失效（静默把 skip 当 PASS）。
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(REAL_TEST)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    code, message = classify(proc.returncode, proc.stdout + proc.stderr)
    print(message)
    return code


def _run() -> int:
    root_raw = os.environ.get("OPT_DISCOVERY_DATA_ROOT", "").strip()
    expected = expected_exports()

    if not root_raw:
        print("BLOCKED: OPT_DISCOVERY_DATA_ROOT 未设置，真实数据回归未执行。")
        print(f"  需要四组导出：{', '.join(sorted(expected.values()))}")
        print("  解除条件：export OPT_DISCOVERY_DATA_ROOT=<统一补采根目录>")
        return EXIT_BLOCKED

    root = Path(root_raw)
    if not root.is_dir():
        print(f"BLOCKED: 数据根目录不存在或不可访问（OPT_DISCOVERY_DATA_ROOT={root_raw!r}）。")
        return EXIT_BLOCKED

    missing = [f"{workload} -> {label}" for workload, label in expected.items()
               if not (root / label).is_dir()]
    if missing:
        print("BLOCKED: 缺少以下导出目录，真实回归未执行（不是 PASS）：")
        for item in missing:
            print(f"  - {item}")
        return EXIT_BLOCKED

    print(f"[verify-real] 数据根目录: {root}")
    print(f"[verify-real] 四组导出均已发现: {', '.join(sorted(expected.values()))}")
    return _run_real_test()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实数据回归入口（缺数据必须 BLOCKED）")
    parser.add_argument(
        "--expect-blocked",
        action="store_true",
        help="断言本次运行确实因缺数据而 BLOCKED（CI 用；返回 0/3 视为失败）",
    )
    args = parser.parse_args(argv)
    actual = _run()
    return gate_exit(actual, expect_blocked=args.expect_blocked)


if __name__ == "__main__":
    raise SystemExit(main())
