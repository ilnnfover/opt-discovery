"""采集验收报告：把人工核对项固化成一份机器可读文件。

覆盖的是 ``collection_manifest.json`` 之外、每批数据都要重新确认的五件事：

* **双卡**：rank 数、device 名、每 rank 的 db 与 op_summary；
* **轮次**：``workload.log`` 里的 ``ACTIVE_ROUND`` 索引、时长与契约期望值；
* **token 数**：Decode 每轮 ``out_tokens`` 必须匹配契约，Prefill 可只记录 sample；
* **指标覆盖**：``op_summary`` 表头里有哪些指标列（决定能算哪些事实）；
* **严格比较状态**：可选读入 ``compare`` 的产物，记录是否可比、是否同窗口。

与 :mod:`opt_discovery.analyzer.collection_audit` 的分工：那边只审"组间参数是否
一致"，这里审"每一批数据本身是否可用"。两者都通过才建议进入人工评审。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..collectors.profiler_dir import ExportError, inspect_ranks, resolve_export_dir
from ..collectors.workload_log import LogParseError, RunLog
from .collection_audit import load_collection_runs
from .contract import collection_identity

__all__ = [
    "METRIC_FLAGS",
    "RunAcceptance",
    "AcceptanceReport",
    "build_acceptance",
]

#: 指标覆盖标志 -> op_summary 里任一命中即视为具备
METRIC_FLAGS: dict[str, tuple[str, ...]] = {
    "pipe_time": ("aic_mac_time(us)", "aiv_vec_time(us)", "aic_mte2_time(us)"),
    "flops": ("aic_cube_fops", "aiv_vector_fops"),
    "cube_utilization": ("cube_utilization(%)",),
    "block_num": ("Block Num",),
    "task_wait": ("Task Wait Time(us)",),
    "shapes": ("Input Shapes",),
}

@dataclass
class RunAcceptance:
    run_id: str
    directory: str
    exit_code: int | None
    checks_ok: bool
    ranks: list[dict[str, Any]] = field(default_factory=list)
    active_rounds: int = 0
    expected_rounds: int | None = None
    tokens: list[int] = field(default_factory=list)
    metrics: dict[str, bool] = field(default_factory=dict)
    output_length: int | None = None
    problems: list[str] = field(default_factory=list)
    identity: dict[str, str] | None = None
    round_indices: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "directory": self.directory,
            "exit_code": self.exit_code,
            "checks_ok": self.checks_ok,
            "ranks": self.ranks,
            "rank_count": len(self.ranks),
            "active_rounds": self.active_rounds,
            "expected_rounds": self.expected_rounds,
            "tokens": self.tokens,
            "output_length": self.output_length,
            "metrics": self.metrics,
            "problems": self.problems,
            "ok": self.ok,
        }


@dataclass
class AcceptanceReport:
    root: str
    runs: list[RunAcceptance]
    comparison: dict[str, Any]
    generated_at: str
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "root": self.root,
            "generated_at": self.generated_at,
            "verdict": self.verdict,
            "comparison": self.comparison,
            "runs": [run.to_dict() for run in self.runs],
        }


def _check_exports(directory: Path, manifest: dict[str, Any], run: RunAcceptance) -> None:
    columns: set[str] = set()
    declared = (manifest.get("checks") or {}).get("ranks") or []
    expected = {
        (item.get("device"), item.get("dir")): item.get("op_summary_csv")
        for item in declared
    }
    try:
        ranks, _ = inspect_ranks(resolve_export_dir(directory, manifest))
    except (ExportError, OSError) as exc:
        run.problems.append(str(exc))
        ranks = ()

    for rank in ranks:
        sizes = []
        for db in rank.msprof_dbs:
            try:
                with db.open("rb") as handle:
                    if handle.read(16) == b"SQLite format 3\x00":
                        sizes.append(db.stat().st_size)
            except OSError as exc:
                run.problems.append(f"{rank.device}: {exc}")
        if not sizes:
            run.problems.append(f"{rank.device}: 缺 msprof db 或文件不可读")

        summaries = 0
        for path in rank.op_summary_files:
            try:
                with path.open(newline="", encoding="utf-8-sig") as handle:
                    reader = csv.reader(handle)
                    header = next(reader, [])
                    has_rows = any(row for row in reader)
            except (OSError, UnicodeError, csv.Error) as exc:
                run.problems.append(f"{rank.device}: {path.name}: {exc}")
                continue
            if header and has_rows:
                columns.update(name.strip() for name in header)
                summaries += 1
        if not summaries:
            run.problems.append(f"{rank.device}: 缺 op_summary csv 或文件无数据行")
        valid_summaries = summaries > 0
        expected_count = expected.get((rank.device, rank.directory.name))
        if type(expected_count) is int and summaries != expected_count:
            run.problems.append(
                f"{rank.device}: 有效 op_summary csv 分片 {summaries}，期望 {expected_count}"
            )
            valid_summaries = False

        run.ranks.append({
            "device": rank.device,
            "dir": rank.directory.name,
            "db": bool(sizes),
            "db_size_mb": round(max(sizes) / 1e6, 1) if sizes else None,
            "op_summary_csv": summaries,
            "ok": bool(sizes) and valid_summaries,
        })

    run.metrics = {
        flag: any(column in columns for column in candidates)
        for flag, candidates in METRIC_FLAGS.items()
    }
    if len(ranks) != 2:
        run.problems.append(f"rank 数 {len(ranks)}（期望 2）")
    actual = {(rank.device, rank.directory.name) for rank in ranks}
    if len(declared) != len(actual) or expected.keys() != actual:
        run.problems.append(
            f"实际 rank 与清单不一致：声明的 rank 缺 op_summary csv 或可用目录，"
            f"期望 {sorted(expected, key=str)}，实际 {sorted(actual)}"
        )


def _check_run(directory: Path, manifest: dict[str, Any]) -> RunAcceptance:
    checks = manifest.get("checks") or {}
    fixed = manifest.get("fixed") or {}
    expected = fixed.get("active_rounds", checks.get("expected_rounds"))
    output_length = (manifest.get("variables") or {}).get("output_length")
    run = RunAcceptance(
        run_id=str(manifest.get("run_id") or directory.name),
        directory=str(directory),
        exit_code=manifest.get("exit_code"),
        checks_ok=checks.get("ok") is True,
        expected_rounds=expected if type(expected) is int else None,
        output_length=output_length if type(output_length) is int else None,
        identity=collection_identity(manifest),
    )
    if type(run.exit_code) is not int or run.exit_code != 0:
        run.problems.append(f"收集器退出码 {run.exit_code}")
    if not run.checks_ok:
        run.problems.append("collection_manifest.checks.ok=false")
    _check_exports(directory, manifest, run)
    try:
        log = RunLog.parse(directory / "workload.log")
    except (LogParseError, OSError) as exc:
        run.problems.append(f"读取 ACTIVE_ROUND 失败：{exc}")
        return run
    run.round_indices = log.round_indices
    run.active_rounds = len(log.rounds)
    run.tokens = [item.output_tokens for item in log.rounds if item.output_tokens is not None]
    if run.expected_rounds is None or run.expected_rounds < 1:
        run.problems.append("清单缺有效的 active_rounds")
    elif run.round_indices != tuple(range(run.expected_rounds)):
        run.problems.append(f"轮次索引 {run.round_indices} 与 active_rounds={run.expected_rounds} 不一致")
    if any(not item.duration_us > 0 for item in log.rounds):
        run.problems.append("存在非正轮次时长")
    if run.output_length is None or run.output_length < 0:
        run.problems.append("清单缺有效的 output_length")
    else:
        # Prefill harness 只打印 sample；如果它提供了 out_tokens，也要匹配契约。
        allowed = {run.output_length, None}
        if run.run_id.startswith("decode-") or run.output_length > 1:
            allowed.remove(None)
        mismatches = [
            (item.index, item.output_tokens) for item in log.rounds if item.output_tokens not in allowed
        ]
        if mismatches:
            run.problems.append(f"out_tokens 与期望 {run.output_length} 不一致：{mismatches}")
    return run


def _read_comparison(path: str | Path | None, runs: list[RunAcceptance]) -> dict[str, Any]:
    if path is None:
        return {"status": "not_run", "source": None}
    target = Path(path)
    if not target.is_file():
        return {"status": "missing", "source": str(target)}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("比较产物必须为 JSON 对象")
    except (OSError, ValueError) as exc:
        return {"status": "invalid", "source": str(target), "error": str(exc)}
    comparable = payload.get("comparable")
    same_window = payload.get("same_window")
    window = payload.get("window")
    rounds = window.get("rounds") if isinstance(window, dict) else None
    collections = payload.get("collections")
    matched = []
    if isinstance(collections, dict):
        for side in ("a", "b"):
            identity = collection_identity(collections.get(side))
            matched.append(next(
                (run for run in runs if run.ok and identity is not None and run.identity == identity),
                None,
            ))
    error = None
    if comparable is not True or same_window is not True:
        error = "比较不可比或窗口不同"
    elif len(matched) != 2 or None in matched or matched[0] is matched[1]:
        error = "比较产物未绑定本批次的两个有效采集；请用本批次的算子表重新执行 compare"
    elif (
        not isinstance(rounds, list)
        or not rounds
        or any(type(index) is not int for index in rounds)
        or rounds != sorted(set(rounds))
        or any(not set(rounds).issubset(run.round_indices) for run in matched if run is not None)
    ):
        error = "比较窗口与本批次的实采轮次不符"
    return {
        "status": "pass" if error is None else "fail",
        "source": str(target),
        "a": payload.get("a"),
        "b": payload.get("b"),
        "comparable": comparable,
        "same_window": same_window,
        "total_ratio": payload.get("total_ratio") if error is None else None,
        "window_rounds": rounds,
        **({"error": error} if error else {}),
    }


def build_acceptance(
    root: str | Path, comparison: str | Path | None = None
) -> AcceptanceReport:
    """扫描一批导出，产出可落盘的验收结论。"""
    path = Path(root)
    runs = [_check_run(directory, manifest) for directory, manifest in load_collection_runs(path)]
    comparison_info = _read_comparison(comparison, runs)

    if not runs:
        verdict = "no_data"
    elif any(not run.ok for run in runs):
        verdict = "fail"
    elif comparison_info["status"] == "not_run":
        verdict = "incomplete"
    elif comparison_info["status"] != "pass":
        verdict = "fail"
    else:
        verdict = "pass"

    return AcceptanceReport(
        root=str(path),
        runs=runs,
        comparison=comparison_info,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        verdict=verdict,
    )
