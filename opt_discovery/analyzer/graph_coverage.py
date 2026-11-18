"""Graph 覆盖率（P1-2）。

FULL_AND_PIECEWISE 不等于"全部图化"：图外还有 fallback（首次执行、动态 shape、
不支持图捕获的算子）。本模块量化"图覆盖到哪一步"，因为**只有图内的任务**才享受
Graph 带来的下发收益。

数据来源：``op_summary`` 的 ``Model ID`` 列（有效值 = 已捕获图 id，
``4294967295`` = 图外）。解析时由 ``row_flags`` 汇总为
``in_graph_tasks`` / ``in_graph_time_us``，本模块只做聚合与解释。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas.operator import OperatorTable

__all__ = ["OpCoverage", "GraphCoverage", "summarize_coverage"]


@dataclass(frozen=True)
class OpCoverage:
    operator: str
    calls: int
    in_graph_tasks: int
    total_us: float
    in_graph_us: float

    @property
    def task_ratio(self) -> float:
        return self.in_graph_tasks / self.calls if self.calls else 0.0

    @property
    def time_ratio(self) -> float:
        return self.in_graph_us / self.total_us if self.total_us else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "calls": self.calls,
            "in_graph_tasks": self.in_graph_tasks,
            "task_ratio": round(self.task_ratio, 4),
            "total_us": round(self.total_us, 3),
            "in_graph_us": round(self.in_graph_us, 3),
            "time_ratio": round(self.time_ratio, 4),
        }


@dataclass
class GraphCoverage:
    workload_id: str
    mode: str
    graph_mode: str
    total_calls: int = 0
    in_graph_calls: int = 0
    total_us: float = 0.0
    in_graph_us: float = 0.0
    top_fallback: tuple[OpCoverage, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return self.mode == "graph" and self.total_calls > 0

    @property
    def task_ratio(self) -> float:
        return self.in_graph_calls / self.total_calls if self.total_calls else 0.0

    @property
    def time_ratio(self) -> float:
        return self.in_graph_us / self.total_us if self.total_us else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "workload_id": self.workload_id,
            "mode": self.mode,
            "graph_mode": self.graph_mode,
            "available": self.available,
            "total_calls": self.total_calls,
            "in_graph_calls": self.in_graph_calls,
            "task_ratio": round(self.task_ratio, 4),
            "total_us": round(self.total_us, 3),
            "in_graph_us": round(self.in_graph_us, 3),
            "time_ratio": round(self.time_ratio, 4),
            "top_fallback": [item.to_dict() for item in self.top_fallback],
            "notes": list(self.notes),
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def summarize_coverage(table: OperatorTable, *, top: int = 8) -> GraphCoverage:
    mode = table.workload.execution.mode.value
    graph_mode = table.workload.execution.graph_mode.value

    per_op: list[OpCoverage] = []
    total_calls = in_graph = 0
    total_us = in_graph_us = 0.0
    for record in table.records:
        flag = record.extra.get("in_graph_tasks")
        if flag is None:
            continue  # 该表不是从 op_summary 解析的，没有图标记
        calls = record.calls
        tasks_in = int(flag.value)
        in_graph_time = record.extra.get("in_graph_time_us")
        time_in = in_graph_time.value if in_graph_time is not None else 0.0
        duration = record.total_time_us.value if record.total_time_us else 0.0
        total_calls += calls
        in_graph += tasks_in
        total_us += duration
        in_graph_us += time_in
        per_op.append(OpCoverage(record.operator, calls, tasks_in, duration, time_in))

    coverage = GraphCoverage(
        workload_id=table.workload_id,
        mode=mode,
        graph_mode=graph_mode,
        total_calls=total_calls,
        in_graph_calls=in_graph,
        total_us=total_us,
        in_graph_us=in_graph_us,
    )

    if total_calls == 0:
        coverage.notes = ("没有 in_graph 标记：该表不是从含 Model ID 的 op_summary 解析的",)
        return coverage

    if mode != "graph":
        coverage.notes = (
            "eager 模式：Model ID 恒为无效值，图覆盖率为 0（符合预期，不是缺失数据）",
        )
        return coverage

    # 图外任务里耗时最高的算子 = fallback 的主要来源
    fallback = [item for item in per_op if item.in_graph_tasks < item.calls]
    fallback.sort(key=lambda item: (item.total_us - item.in_graph_us), reverse=True)
    coverage.top_fallback = tuple(fallback[:top])

    task_ratio = coverage.task_ratio
    time_ratio = coverage.time_ratio
    notes: list[str] = [
        f"口径：op_summary 的 Model ID；图内 {in_graph} / {total_calls} 次任务（{task_ratio:.1%}）",
        f"按时间加权仅 {time_ratio:.1%}：图外任务更“长”，不能用任务占比代替时间占比",
    ]
    if task_ratio < 0.9:
        notes.append(
            f"仍有 {1 - task_ratio:.1%} 任务在图外执行（fallback / 未捕获），"
            "这部分不享受图收益；对比 Eager/Graph 时需按图内部分单独看"
        )
    if time_ratio + 0.05 < task_ratio:
        notes.append(
            "任务占比显著高于时间占比：图外任务单次更慢，说明 fallback 集中在长耗时算子"
        )
    coverage.notes = tuple(notes)
    return coverage
