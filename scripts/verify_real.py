#!/usr/bin/env python3
"""真实数据回归入口（``make verify-real``）。

契约（结构化判定，不解析 pytest 的人类可读摘要）：

* 缺数据 / 目录不完整 / 缺 ``workload.log`` 或采集 manifest -> BLOCKED，退出码 2，
  **不是** PASS；
* 目录存在但采集身份/契约不匹配 -> 由真实测试判 FAILED（不会伪装成 BLOCKED）；
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
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opt_discovery.collectors.profiler_dir import (  # noqa: E402
    ExportError,
    profiler_sessions,
    resolve_export_dir,
)

MANIFEST_FIXTURE = (
    REPO_ROOT / "configs" / "collection_fixtures" / "qwen35-35b-a3b-910b-unified-20261121.json"
)
REAL_TESTS = (
    REPO_ROOT / "tests" / "test_real_data.py",
    REPO_ROOT / "tests" / "test_real_unified_manifest.py",
)


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

    @property
    def passed(self) -> int:
        """实际通过数（失败/错误/跳过早已从总数中扣除）。"""
        return max(0, self.tests - self.skipped - self.failed - self.errors)


def expected_exports() -> dict[str, str]:
    """Map workload IDs to the four entry names used by the active fixture."""
    return {key: spec["export_dir"] for key, spec in load_manifest_fixture()["runs"].items()}


def load_manifest_fixture(path: str | Path | None = None) -> dict:
    """读取独立核对的采集身份 fixture（仓库内不带机器路径/原始数据）。"""
    return json.loads(Path(path or MANIFEST_FIXTURE).read_text(encoding="utf-8"))


def manifest_identity_problems(
    manifest: Mapping,
    workload_id: str,
    *,
    fixture: Mapping | None = None,
) -> list[str]:
    """核对一份 ``collection_manifest.json`` 与 fixture 的采集身份。

    缺数据/目录不完整由 :func:`data_gaps` 判 BLOCKED；这里只处理「文件存在但
    身份或契约不对」，因此返回问题清单，由真实测试判失败，而不是当成缺数据。
    """
    data = fixture or load_manifest_fixture()
    spec = (data.get("runs") or {}).get(workload_id)
    if spec is None:
        return [f"fixture 未定义 workload {workload_id!r}"]

    problems: list[str] = []
    if manifest.get("run_id") != spec.get("run_id"):
        problems.append(
            f"run_id 期望 {spec.get('run_id')!r}，实际 {manifest.get('run_id')!r}"
        )
    if "started_at" in spec and manifest.get("started_at") != spec["started_at"]:
        problems.append(
            f"started_at 期望 {spec['started_at']!r}，实际 {manifest.get('started_at')!r}"
        )
    if manifest.get("contract_id") != data.get("contract_id"):
        problems.append(
            f"contract_id 期望 {data.get('contract_id')!r}，"
            f"实际 {manifest.get('contract_id')!r}"
        )
    if manifest.get("contract_sha256") != data.get("collection_contract_sha256"):
        problems.append(
            f"contract_sha256 期望 {data.get('collection_contract_sha256')!r}，"
            f"实际 {manifest.get('contract_sha256')!r}"
        )

    fixed = manifest.get("fixed")
    if not isinstance(fixed, Mapping):
        problems.append("fixed 不是对象")
    else:
        for key, expected in (data.get("fixed") or {}).items():
            if fixed.get(key) != expected:
                problems.append(f"fixed.{key} 期望 {expected!r}，实际 {fixed.get(key)!r}")

    profiling = manifest.get("profiling")
    expected_profiling = data.get("profiling") or {}
    if not isinstance(profiling, Mapping):
        problems.append("profiling 不是对象")
    else:
        expected_switches = list(expected_profiling.get("switches") or ())
        if list(profiling.get("switches") or ()) != expected_switches:
            problems.append(
                f"profiling.switches 期望 {expected_switches!r}，"
                f"实际 {profiling.get('switches')!r}"
            )
        for key in ("msproftx", "summary_format"):
            if key in expected_profiling and profiling.get(key) != expected_profiling[key]:
                problems.append(
                    f"profiling.{key} 期望 {expected_profiling[key]!r}，"
                    f"实际 {profiling.get(key)!r}"
                )

    checks = manifest.get("checks") or {}
    if checks.get("ok") is not True:
        problems.append(f"checks.ok 不是 true：{checks.get('ok')!r}")
    expected_rounds = data.get("active_rounds") or []
    if checks.get("active_rounds") != len(expected_rounds):
        problems.append(
            f"active_rounds 期望 {len(expected_rounds)}，实际 {checks.get('active_rounds')!r}"
        )
    if "expected_rounds" in checks and checks.get("expected_rounds") != len(expected_rounds):
        problems.append(
            f"expected_rounds 期望 {len(expected_rounds)}，"
            f"实际 {checks.get('expected_rounds')!r}"
        )

    expected_ranks = list(spec.get("ranks") or ())
    ranks = checks.get("ranks")
    if not isinstance(ranks, list):
        problems.append("checks.ranks 不是列表")
    else:
        devices = [str(item.get("device")) for item in ranks if isinstance(item, Mapping)]
        if sorted(devices) != sorted(expected_ranks):
            problems.append(f"rank 设备期望 {expected_ranks}，实际 {devices}")
        for item in ranks:
            if not isinstance(item, Mapping):
                problems.append(f"rank 记录不是对象：{item!r}")
                continue
            if item.get("ok") is not True:
                problems.append(f"rank 检查未通过：{item!r}")
            if not isinstance(item.get("device"), str) or not item.get("device"):
                problems.append(f"rank.device 不是非空字符串：{item.get('device')!r}")
            if not isinstance(item.get("dir"), str) or not item.get("dir"):
                problems.append(f"rank.dir 不是非空字符串：{item.get('dir')!r}")
            if not isinstance(item.get("db"), bool):
                problems.append(f"rank.db 不是 bool：{item.get('db')!r}")

    variables = manifest.get("variables") or {}
    for key in ("mode", "graph_mode", "output_length"):
        if variables.get(key) != spec.get(key):
            problems.append(
                f"variables.{key} 期望 {spec.get(key)!r}，实际 {variables.get(key)!r}"
            )
    return problems


def rank_layout_problems(
    export_dir: str | Path,
    manifest: dict,
    rank_sources: Mapping[str, str | Path],
) -> list[str]:
    """核对「发现的 rank 目录」与采集 manifest 声明的同 rank 源目录。

    采集可以整体搬迁：源目录由 ``workload.log.resolve().parent``（真实采集根）
    加 manifest 里的相对 ``dir`` 组成，不写死任何机器路径，也不靠 alias 名/设备名
    单独判定。``rank_sources`` 是 ``device -> 发现的 rank 目录``（通常来自
    :class:`~opt_discovery.collectors.RankExport.directory`）。
    """
    export = Path(export_dir)
    log_path = export / "workload.log"
    if not log_path.is_file():
        return [f"{export}: 缺 workload.log，无法定位真实采集目录"]
    try:
        capture = resolve_export_dir(log_path.resolve().parent, manifest)
        sessions = {path.name: path.resolve() for path in profiler_sessions(capture)}
    except (ExportError, OSError) as exc:
        return [str(exc)]

    checks = manifest.get("checks") or {}
    declared = checks.get("ranks")
    if not isinstance(declared, list):
        return ["checks.ranks 不是列表，无法绑定 rank 源目录"]

    problems: list[str] = []
    expected: dict[str, list[Path]] = {}
    for item in declared:
        if not isinstance(item, Mapping):
            problems.append(f"rank 记录不是对象：{item!r}")
            continue
        device = str(item.get("device") or "")
        dir_name = item.get("dir")
        if not device or not isinstance(dir_name, str) or not dir_name:
            problems.append(f"rank 记录缺少 device/dir：{item!r}")
            continue
        source = sessions.get(dir_name)
        if source is None:
            problems.append(f"rank {device}: manifest 声明的源目录不存在 {dir_name}")
            continue
        expected.setdefault(device, []).append(source)

    for device, directory in sorted(rank_sources.items()):
        actual = Path(directory).resolve()
        candidates = expected.get(str(device))
        if not candidates:
            problems.append(f"rank {device}: manifest 没有对应 rank 记录")
        elif actual not in candidates:
            problems.append(
                f"rank {device}: 实际目录 {actual} 与 manifest 声明 "
                f"{[str(path) for path in candidates]} 不一致"
            )

    for device in sorted(set(expected) - {str(item) for item in rank_sources}):
        problems.append(f"rank {device}: manifest 声明了该 rank，但导出中没有发现")
    return problems


def data_gaps(root: str | Path, *, expected: Mapping[str, str] | None = None) -> list[str]:
    """返回「数据缺失/不完整」清单；非空时 verify-real 必须 BLOCKED。

    只做目录/小文件存在性检查，不读取 Profiling 大文件。manifest 必须位于
    ``workload.log.resolve().parent``（alias 目录可能只是指向统一补采的软链接）。
    """
    base = Path(root)
    wanted = dict(expected or expected_exports())
    gaps: list[str] = []
    for workload, label in sorted(wanted.items()):
        export = base / label
        if not export.is_dir():
            gaps.append(f"{workload}: 缺导出目录 {label}")
            continue
        log_path = export / "workload.log"
        if not log_path.is_file():
            gaps.append(f"{workload}: 缺 workload.log")
            continue
        capture = log_path.resolve().parent
        manifest_path = capture / "collection_manifest.json"
        if not manifest_path.is_file():
            gaps.append(f"{workload}: workload.log 同目录缺 collection_manifest.json")
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        manifest = payload if isinstance(payload, dict) else {}
        try:
            sessions = {
                path.name: path for path in profiler_sessions(resolve_export_dir(capture, manifest))
            }
        except (ExportError, OSError) as exc:
            gaps.append(f"{workload}: {exc}")
            continue
        ranks = (manifest.get("checks") or {}).get("ranks")
        minimums: dict[str, int] = {}
        if isinstance(ranks, list):
            for item in ranks:
                if not isinstance(item, Mapping):
                    continue
                name = item.get("dir")
                if isinstance(name, str) and name:
                    count = item.get("op_summary_csv")
                    minimums[name] = max(count, 1) if type(count) is int else 1
        summary_counts = {
            name: sum(item.is_file() for item in path.glob("mindstudio_profiler_output/op_summary*.csv"))
            for name, path in sessions.items()
        }
        for name, minimum in sorted(minimums.items()):
            count = summary_counts.get(name)
            if count is None:
                gaps.append(f"{workload}: manifest 声明的 rank 源目录缺失 {name}")
            elif count < minimum:
                gaps.append(f"{workload}: {name} 缺 op_summary*.csv，实际 {count}，期望 {minimum}")
        if not minimums and not any(summary_counts.values()):
            gaps.append(f"{workload}: 无 op_summary*.csv（导出不完整）")
    return gaps


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
    detail = f"{run.passed} passed, {run.skipped} skipped, {run.failed} failed, {run.errors} errors"
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
                *(str(path) for path in REAL_TESTS),
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

    gaps = data_gaps(root, expected=expected)
    if gaps:
        print("BLOCKED: 数据不完整，真实回归未执行（不是 PASS）：")
        for item in gaps:
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
