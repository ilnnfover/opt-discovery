#!/usr/bin/env python3
"""真实数据回归入口（``make verify-real``）。

契约：
* 缺数据 / 目录不完整 -> 打印 BLOCKED，退出码 2，**不是** PASS；
* 真实回归中有用例被 skip -> 打印 SKIPPED，退出码 3（未执行完不算通过）；
* 全部通过 -> 打印 PASS，退出码 0；失败 -> 透传 pytest 的非零退出码。

数据根目录来自 ``OPT_DISCOVERY_DATA_ROOT``，仓库内不保存任何机器路径。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"
REAL_TEST = REPO_ROOT / "tests" / "test_real_data.py"

EXIT_BLOCKED = 2
EXIT_SKIPPED = 3


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
    """把 pytest 结果映射为 verify-real 的三态，供测试直接校验。

    skip 优先于 returncode：pytest 全部 skip 时退出码仍是 0，不能算 PASS。
    """
    skipped = re.search(r"(\d+) skipped", output)
    if skipped and int(skipped.group(1)) > 0:
        return EXIT_SKIPPED, (
            f"SKIPPED: 有 {skipped.group(1)} 个真实回归用例未执行"
            "（缺数据或场景不可复现），不能记为 PASS。"
        )
    if returncode != 0:
        return returncode, "FAIL: 真实数据回归失败，见上方 pytest 输出。"
    passed = re.search(r"(\d+) passed", output)
    count = passed.group(1) if passed else "?"
    return 0, f"PASS: 真实数据回归全部通过（{count} passed，0 skipped）。"


def main() -> int:
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

    # 不要额外传 -q：pyproject 的 addopts 已含 -q，再传会吞掉 "N passed / N skipped"
    # 摘要行，导致下面的 skip 检测失效（静默把 skip 当 PASS）。
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


if __name__ == "__main__":
    raise SystemExit(main())
