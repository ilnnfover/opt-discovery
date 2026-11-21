"""采集验收报告：把人工核对项固化成一份机器可读文件。

覆盖的是 ``collection_manifest.json`` 之外、每批数据都要重新确认的五件事：

* **双卡**：rank 数、device 名、每 rank 的 db 与 op_summary；
* **轮次**：``workload.log`` 里的 ``ACTIVE_ROUND`` 条数与契约期望值；
* **token 数**：每轮 ``out_tokens``，缺失或不一致都算问题；
* **指标覆盖**：``op_summary`` 表头里有哪些指标列（决定能算哪些事实）；
* **严格比较状态**：可选读入 ``compare`` 的产物，记录是否可比、是否同窗口。

与 :mod:`opt_discovery.analyzer.collection_audit` 的分工：那边只审"组间参数是否
一致"，这里审"每一批数据本身是否可用"。两者都通过才建议进入人工评审。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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

_ROUND_RE = re.compile(r"ACTIVE_ROUND (\d+).*out_tokens (\d+)")


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


def _manifest_paths(root: Path) -> list[Path]:
    direct = root / "collection_manifest.json"
    if direct.is_file():
        return [direct]
    return sorted(root.glob("*/collection_manifest.json"))


def _read_rounds(directory: Path) -> tuple[int, list[int]]:
    log = directory / "workload.log"
    if not log.is_file():
        return 0, []
    matches = _ROUND_RE.findall(log.read_text(errors="replace"))
    return len(matches), [int(tokens) for _, tokens in matches]


def _op_summary_headers(directory: Path) -> list[str]:
    columns: set[str] = set()
    for path in sorted(directory.glob("PROF_*/mindstudio_profiler_output/op_summary*.csv")):
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as handle:
                header = next(csv.reader(handle), [])
        except OSError:
            continue
        columns.update(name.strip() for name in header if name.strip())
    return sorted(columns)


def _metric_flags(columns: Iterable[str]) -> dict[str, bool]:
    present = set(columns)
    return {
        flag: any(candidate in present for candidate in candidates)
        for flag, candidates in METRIC_FLAGS.items()
    }


def _check_run(directory: Path, manifest: dict[str, Any]) -> RunAcceptance:
    checks = manifest.get("checks") or {}
    ranks = list(checks.get("ranks") or [])
    rounds, tokens = _read_rounds(directory)
    expected = checks.get("expected_rounds")
    run = RunAcceptance(
        run_id=str(manifest.get("run_id") or directory.name),
        directory=str(directory),
        exit_code=manifest.get("exit_code"),
        checks_ok=bool(checks.get("ok")),
        ranks=[
            {
                "device": item.get("device"),
                "dir": item.get("dir"),
                "db": bool(item.get("db")),
                "db_size_mb": item.get("db_size_mb"),
                "op_summary_csv": item.get("op_summary_csv"),
                "ok": bool(item.get("ok")),
            }
            for item in ranks
        ],
        active_rounds=rounds or int(checks.get("active_rounds") or 0),
        expected_rounds=int(expected) if expected is not None else None,
        tokens=tokens
        or [
            int(value)
            for value in checks.get("out_tokens_per_round") or []
            if value is not None
        ],
        metrics=_metric_flags(_op_summary_headers(directory)),
        output_length=(manifest.get("variables") or {}).get("output_length"),
    )

    if run.exit_code not in (0, None):
        run.problems.append(f"收集器退出码 {run.exit_code}")
    if not run.checks_ok:
        run.problems.append("collection_manifest.checks.ok=false")
    if len(run.ranks) != 2:
        run.problems.append(f"rank 数 {len(run.ranks)}（期望 2）")
    for item in run.ranks:
        if not item["db"]:
            run.problems.append(f"{item['device']}: 缺 msprof db")
        if not item["op_summary_csv"]:
            run.problems.append(f"{item['device']}: 缺 op_summary csv")
    if run.expected_rounds is not None and run.active_rounds != run.expected_rounds:
        run.problems.append(f"active_rounds {run.active_rounds} != 期望 {run.expected_rounds}")
    # prefill 的 output_length=1，harness 打印的是 sample 而不是 out_tokens，
    # 因此只在 decode（output_length>1）时强制要求每轮 token 数。
    expects_tokens = run.output_length is None or run.output_length > 1
    if expects_tokens:
        if not run.tokens:
            run.problems.append("没有解析到 out_tokens")
        elif len(set(run.tokens)) != 1:
            run.problems.append(f"每轮 token 数不一致: {sorted(set(run.tokens))}")
    return run


def _read_comparison(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "not_run", "source": None}
    target = Path(path)
    if not target.is_file():
        return {"status": "missing", "source": str(target)}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "invalid", "source": str(target), "error": str(exc)}
    comparable = payload.get("comparable")
    same_window = payload.get("same_window")
    status = "pass" if (comparable and same_window) else "fail"
    return {
        "status": status,
        "source": str(target),
        "a": payload.get("a"),
        "b": payload.get("b"),
        "comparable": comparable,
        "same_window": same_window,
        "total_ratio": payload.get("total_ratio"),
        "window_rounds": (payload.get("window") or {}).get("rounds"),
    }


def build_acceptance(
    root: str | Path, comparison: str | Path | None = None
) -> AcceptanceReport:
    """扫描一批导出，产出可落盘的验收结论。"""
    path = Path(root)
    manifests = _manifest_paths(path)
    runs = [
        _check_run(manifest.parent, json.loads(manifest.read_text(encoding="utf-8")))
        for manifest in manifests
    ]
    comparison_info = _read_comparison(comparison)

    if not runs:
        verdict = "no_data"
    elif any(not run.ok for run in runs):
        verdict = "fail"
    elif comparison_info["status"] != "pass":
        verdict = "incomplete"
    else:
        verdict = "pass"

    return AcceptanceReport(
        root=str(path),
        runs=runs,
        comparison=comparison_info,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        verdict=verdict,
    )
