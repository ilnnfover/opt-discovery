"""按统一契约串行补采四组（Prefill/Decode × Eager/Graph）。

历史四组的问题：除执行模式这个自变量外，`max_num_seqs` 只有 Graph 设了 32，
Eager 用默认；decode 的 rank1 又没有导出 CSV；采集命令也没有留痕。
本脚本把三件事一次解决：

1. 从 `configs/collection_contract.yaml` 生成 4 个**参数统一**的 harness 副本；
2. 用同一套 msprof 开关、同一个解释器、清空遗留环境变量，串行采集；
3. 每组写 `collection_manifest.json`（实际命令 / 契约 hash / 两 rank 自检结果）。

用法：
    python scripts/recollect_contract.py --dry-run            # 只打印将要执行的命令
    python scripts/recollect_contract.py --only decode-eager  # 单组
    python scripts/recollect_contract.py --out-root $PROF_ROOT/unified
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from opt_discovery.collectors.profiler_dir import ExportError, inspect_ranks  # noqa: E402
from opt_discovery.collectors.workload_log import LogParseError, RunLog  # noqa: E402

CONTRACT_PATH = REPO / "configs" / "collection_contract.yaml"
#: 原始 harness 目录：可选（$NPU_STACK）；未设置时直接用仓库内模板
SOURCE_HARNESS_DIR = (
    Path(os.environ["NPU_STACK"]) if os.environ.get("NPU_STACK") else None
)
HARNESS_DIR = REPO / "scripts" / "harness"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_harnesses(contract: dict, *, write: bool = True) -> dict[str, Path]:
    """复制 harness 并注入契约里固定的参数，不改动 $NPU_STACK 原文件。"""
    if write:
        HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    fixed = contract["fixed"]
    result: dict[str, Path] = {}
    for run in contract["runs"]:
        source = (
            SOURCE_HARNESS_DIR / run["harness"]
            if SOURCE_HARNESS_DIR is not None
            else HARNESS_DIR / run["harness"]
        )
        if not source.is_file():
            raise FileNotFoundError(
                f"{source}: harness 不存在（可用 $NPU_STACK 指定原始模板目录）"
            )
        text = source.read_text(encoding="utf-8")

        # 1) max_num_seqs：历史只有 Graph 两组显式设了 32
        if "max_num_seqs" not in text:
            text2 = text.replace(
                "LLM(model=", f"LLM(max_num_seqs={fixed['max_num_seqs']}, model=", 1
            )
            if text2 == text:
                raise RuntimeError(f"{source}: 无法注入 max_num_seqs")
            text = text2

        # 2) 模型路径：harness 从 $MODEL_PATH 读取，构建时无需替换
        #    （历史模板里写死的本机路径已移除）

        # 3) 收尾等待：应用退出太快会让 msprof 丢掉最后 1~2 轮数据
        #    （历史四组都有这个现象：r4 偏低或 r3/r4 为 0）
        if "PROFILE_STOP_ABS" in text and "MSPROF_FLUSH_WAIT" not in text:
            text = text.replace(
                'print("TOTAL_SECONDS"',
                "try:\n"
                "    import torch, torch_npu  # noqa: F401\n"
                "    torch.npu.synchronize()\n"
                "except Exception:\n"
                "    pass\n"
                "import time as _mstx_wait  # MSPROF_FLUSH_WAIT\n"
                "_mstx_wait.sleep(3)\n"
                'print("TOTAL_SECONDS"',
                1,
            )

        # 4) 按契约写入轮数（契约是唯一真相源，不再要求源脚本恰好一致）
        matches = list(re.finditer(r"for i in range\((\d+)\):", text))
        if len(matches) < 2:
            raise RuntimeError(f"{source}: 找不到 warmup/active 两个循环")
        for match, value in zip(
            (matches[1], matches[0]),
            (fixed["active_rounds"], fixed["warmup_rounds"]),
            strict=True,
        ):
            text = (
                text[: match.start()]
                + f"for i in range({value}):"
                + text[match.end():]
            )
        # 从后往前替换，避免偏移；上面 zip 顺序即 (后, 前)

        target = HARNESS_DIR / run["harness"]
        if write:
            target.write_text(text, encoding="utf-8")
        result[run["id"]] = target
    return result


def _command(contract: dict, run: dict, harness: Path, dest: Path) -> list[str]:
    profiling = contract["profiling"]
    command = [
        profiling["msprof"],
        *profiling["switches"],
        f"--msproftx={profiling['msproftx']}",
        f"--summary-format={profiling['summary_format']}",
        f"--output={dest}",
        f"--application={contract['fixed']['python']} {harness}",
    ]
    return command


def _check(dest: Path, contract: dict, run: dict) -> dict:
    """返回两 rank 的自检结果；不完整时标记 ok=False 而不是静默通过。"""
    errors: list[str] = []
    ranks = []
    excluded: dict[str, str] = {}
    try:
        exports, excluded = inspect_ranks(dest)
        for rank in exports:
            size = max((db.stat().st_size for db in rank.msprof_dbs), default=0)
            ranks.append({
                "dir": rank.directory.name,
                "device": rank.device,
                "db": bool(rank.msprof_dbs),
                "db_size_mb": round(size / 1e6, 1),
                "op_summary_csv": len(rank.op_summary_files),
                "ok": bool(rank.msprof_dbs and rank.op_summary_files),
            })
    except (ExportError, OSError) as exc:
        errors.append(str(exc))

    rounds = ()
    try:
        rounds = RunLog.parse(dest / "workload.log").rounds
    except (LogParseError, OSError) as exc:
        errors.append(str(exc))
    expected = contract["fixed"]["active_rounds"]
    output_tokens = [item.output_tokens for item in rounds]
    return {
        "ranks": ranks,
        "duplicate_prof_dirs": len(excluded),
        "excluded_prof_dirs": excluded,
        "active_rounds": len(rounds),
        "expected_rounds": expected,
        "out_tokens_per_round": output_tokens,
        "errors": errors,
        "ok": not errors
        and len(ranks) == contract["fixed"]["tensor_parallel_size"]
        and all(item["ok"] for item in ranks)
        and tuple(item.index for item in rounds) == tuple(range(expected))
        and all(item.duration_us > 0 for item in rounds)
        and all(value in (None, run["output_length"]) for value in output_tokens),
    }


def _preflight_memory(python: str, utilization: float) -> tuple[bool, str]:
    """启动前检查每张卡的可用显存，避免跑出一次"rounds=0"的失败记录。

    上次教训：卡被其它实验占着时启动，harness 直接 ValueError 退出，
    msprof 却仍然生成一个无数据的 PROF 目录。
    """
    code = (
        "import torch, torch_npu\n"
        "n = torch.npu.device_count()\n"
        "for i in range(n):\n"
        "    free, total = torch.npu.mem_get_info(i)\n"
        "    print(i, free, total)\n"
    )
    try:
        result = subprocess.run(
            [python, "-c", code], capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        return False, "显存检查超时（NPU 可能不可用）"
    if result.returncode != 0:
        return False, f"显存检查失败：{(result.stderr or '').strip().splitlines()[-1:]}"
    problems = []
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        index, free, total = int(parts[0]), int(parts[1]), int(parts[2])
        need = total * utilization
        if free < need:
            problems.append(
                f"device{index}: 可用 {free / 2**30:.1f} GiB < 需要 {need / 2**30:.1f} GiB"
            )
    if problems:
        return False, "; ".join(problems)
    return True, "显存充足"


def _wait_memory(python: str, utilization: float, timeout_s: int = 600) -> tuple[bool, str]:
    """每组采集前等待显存释放。

    教训：一组 vLLM 退出后显存不会立刻归还，紧接着跑下一组会直接 ValueError
    退出（rounds=0），甚至被 SIGKILL。
    """
    deadline = time.time() + timeout_s
    detail = ""
    while time.time() < deadline:
        ok, detail = _preflight_memory(python, utilization)
        if ok:
            return True, detail
        time.sleep(15)
    return False, detail


def _wait_profiler_idle(timeout_s: int = 1800) -> bool:
    """等 msprof 后处理结束；否则 CPU 争用会污染 Host 侧时间测量。"""
    pattern = "msprof --msproftx|profiler_tool/analysis/msprof"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, check=False
        ).returncode
        if rc != 0:  # pgrep 无匹配返回 1
            return True
        time.sleep(20)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-root", default=None,
                        help="输出根目录，默认 $PROF_ROOT/unified（未设置则为 ./exports/unified）")
    parser.add_argument("--only", default=None,
                        help="只采指定组，逗号分隔（如 decode-eager,decode-graph）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--memory-wait", type=int, default=600,
                        help="每组前等待显存释放的最长秒数")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="跳过显存前置检查（默认会检查，避免卡被占用时白跑）")
    parser.add_argument("--wait-idle", action="store_true",
                        help="等已有 msprof 后处理结束再开始（避免 CPU 争用）")
    args = parser.parse_args()

    if args.wait_idle and not args.dry_run:
        print("等待已有 msprof 后处理结束...", flush=True)
        if not _wait_profiler_idle():
            print("[WARN] 等待超时，仍继续采集（Host 侧测量可能受影响）", file=sys.stderr)

    contract = yaml.safe_load(
        os.path.expandvars(CONTRACT_PATH.read_text(encoding="utf-8"))
    )

    def ensure_memory(run_id: str) -> int:
        """每组开始前确认显存，不足则等待（最多 --memory-wait 秒）。"""
        if args.dry_run or args.skip_preflight:
            return 0
        ok, detail = _wait_memory(
            contract["fixed"]["python"],
            float(contract["fixed"]["gpu_memory_utilization"]),
            timeout_s=args.memory_wait,
        )
        if not ok:
            print(f"[ABORT] {run_id}: 显存等待超时 —— {detail}", file=sys.stderr)
            print("        等设备空闲后重试，或加 --skip-preflight 强制继续", file=sys.stderr)
            return 3
        print(f"[preflight] {run_id}: {detail}", flush=True)
        return 0
    contract_sha = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()[:16]
    harnesses = build_harnesses(contract, write=not args.dry_run)
    if args.out_root is None:
        args.out_root = str(Path(os.environ.get("PROF_ROOT", "exports")) / "unified")
    out_root = Path(args.out_root)
    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)

    env = {k: v for k, v in os.environ.items()}
    for key in contract.get("unset_environment") or ():
        env.pop(key, None)

    only_ids = {x.strip() for x in args.only.split(",")} if args.only else None
    runs = [r for r in contract["runs"] if only_ids is None or r["id"] in only_ids]
    if not runs:
        print(f"没有匹配的组: {args.only}", file=sys.stderr)
        return 2

    manifest_all = []
    for index, run in enumerate(runs, start=1):
        code = ensure_memory(run["id"])
        if code:
            return code
        dest = out_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{run['id']}"
        command = _command(contract, run, harnesses[run["id"]], dest)
        print(f"\n=== [{index}/{len(runs)}] {run['id']}  dest={dest}", flush=True)
        print("   " + " ".join(command), flush=True)
        if args.dry_run:
            continue

        dest.mkdir()
        started = _now()
        with (dest / "workload.log").open("w", encoding="utf-8") as handle:
            rc = subprocess.call(command, stdout=handle, stderr=subprocess.STDOUT, env=env)
        checks = _check(dest, contract, run)
        manifest = {
            "contract_id": contract["contract_id"],
            "contract_sha256": contract_sha,
            "run_id": run["id"],
            "export_dir": ".",
            "fixed": contract["fixed"],
            "profiling": contract["profiling"],
            "variables": {k: run[k] for k in ("mode", "graph_mode", "output_length")},
            "command": command,
            "env_removed": contract.get("unset_environment") or [],
            "harness": str(harnesses[run["id"]]),
            "started_at": started,
            "finished_at": _now(),
            "exit_code": rc,
            "checks": checks,
        }
        (dest / "collection_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        manifest_all.append(manifest)
        print(
            f"   exit={rc} ranks={len(checks['ranks'])} rounds={checks['active_rounds']} "
            f"op_summary={[r['op_summary_csv'] for r in checks['ranks']]} ok={checks['ok']}",
            flush=True,
        )
        if rc != 0 or not checks["ok"]:
            print(f"   [WARN] {run['id']} 自检未通过，详情见 collection_manifest.json", file=sys.stderr)

    if args.dry_run:
        return 0

    summary = out_root / "collection_summary.json"
    summary.write_text(
        json.dumps(
            {
                "contract_id": contract["contract_id"],
                "contract_sha256": contract_sha,
                "generated_at": _now(),
                "runs": [
                    {
                        "run_id": m["run_id"],
                        "ok": m["exit_code"] == 0 and m["checks"]["ok"],
                        "ranks": len(m["checks"]["ranks"]),
                        "exit_code": m["exit_code"],
                    }
                    for m in manifest_all
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\n-> {summary}", flush=True)
    return 0 if all(m["exit_code"] == 0 and m["checks"]["ok"] for m in manifest_all) else 1


if __name__ == "__main__":
    raise SystemExit(main())
