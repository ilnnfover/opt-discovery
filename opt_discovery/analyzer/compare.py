"""Cross-workload comparison (Eager vs Graph, Prefill vs Decode).

Guard rails:

* a comparison refuses to produce numbers when the two workloads are not
  comparable (model/hardware/batch/input length differ or are unknown);
* both sides must use the same steady-state window policy, otherwise the
  difference would partly reflect window choice;
* conclusions stay directional ("更像 Host 侧开销下降"), because operator
  statistics alone cannot prove a critical-path effect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..schemas.operator import OperatorTable
from ..schemas.workload import WorkloadSpec
from .contract import GRAPH_ONLY_PARAMS

__all__ = ["OperatorDelta", "ComparisonReport", "compare_tables"]


@dataclass(frozen=True)
class OperatorDelta:
    operator: str
    presence: str  # both | only_a | only_b
    share_a: float | None
    share_b: float | None
    delta_share: float | None  # b - a, in fraction of total
    calls_a: int | None
    calls_b: int | None
    calls_ratio: float | None  # b / a
    avg_a: float | None
    avg_b: float | None
    avg_ratio: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "presence": self.presence,
            "share_a": self.share_a,
            "share_b": self.share_b,
            "delta_share": self.delta_share,
            "calls_a": self.calls_a,
            "calls_b": self.calls_b,
            "calls_ratio": self.calls_ratio,
            "avg_a": self.avg_a,
            "avg_b": self.avg_b,
            "avg_ratio": self.avg_ratio,
        }


@dataclass
class ComparisonReport:
    a_id: str
    b_id: str
    comparable: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    same_window: bool | None = None
    window: dict[str, Any] | None = None
    total_a_us: float | None = None
    total_b_us: float | None = None
    deltas: list[OperatorDelta] = field(default_factory=list)
    observations: tuple[str, ...] = ()
    generated_at: str = ""

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "a": self.a_id,
            "b": self.b_id,
            "comparable": self.comparable,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "same_window": self.same_window,
            "window": self.window,
            "total_a_us": self.total_a_us,
            "total_b_us": self.total_b_us,
            "total_ratio": (
                self.total_b_us / self.total_a_us
                if self.total_a_us and self.total_b_us is not None
                else None
            ),
            "observations": list(self.observations),
            "deltas": [d.to_dict() for d in self.deltas],
            "generated_at": self.generated_at,
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def _summarize(table: OperatorTable) -> dict[str, dict[str, float]]:
    """operator -> {share, calls, avg} merged across variants."""
    total = table.total_operator_time_us()
    summary: dict[str, dict[str, float]] = {}
    for record in table.records:
        if record.total_time_us is None:
            continue
        bucket = summary.setdefault(record.operator, {"total": 0.0, "calls": 0})
        bucket["total"] += record.total_time_us.value
        bucket["calls"] += record.calls
    for bucket in summary.values():
        bucket["share"] = bucket["total"] / total if total else None
        bucket["avg"] = bucket["total"] / bucket["calls"] if bucket["calls"] else None
    return summary


def _window_of(table: OperatorTable) -> dict | None:
    return (table.meta.extra or {}).get("window")


def _hcom_calls(table: OperatorTable) -> int:
    return sum(record.calls for record in table.records if record.operator.startswith("hcom"))


def _collection_param_diffs(a: WorkloadSpec, b: WorkloadSpec) -> list[str]:
    diffs: list[str] = []
    keys = set(a.input.extra) | set(b.input.extra)
    eager_side = "eager" in (a.execution.mode.value, b.execution.mode.value)
    for key in sorted(keys):
        if key in GRAPH_ONLY_PARAMS and eager_side:
            continue
        left, right = a.input.extra.get(key), b.input.extra.get(key)
        if left != right:
            diffs.append(f"input.{key}: {left!r} vs {right!r}")
    # graph_mode 与 execution.mode 是同一个自变量（图捕获策略），不算"采集参数差异"
    return diffs


def compare_tables(
    a: OperatorTable,
    b: OperatorTable,
    *,
    top: int = 20,
    min_share: float = 0.0,
) -> ComparisonReport:
    """Compare ``b`` against ``a`` (``delta_share = share_b - share_a``)."""
    comparable, reasons = a.workload.comparable_with(b.workload)
    warnings: list[str] = []

    if a.workload.execution.phase is not b.workload.execution.phase:
        warnings.append(
            "跨阶段对比（prefill vs decode）：只比较占比结构，绝对时长与 calls 不可直接比较"
        )
    window_a, window_b = _window_of(a), _window_of(b)
    same_window: bool | None = None
    if window_a is not None or window_b is not None:
        rounds_a = tuple((window_a or {}).get("rounds") or ())
        rounds_b = tuple((window_b or {}).get("rounds") or ())
        same_window = rounds_a == rounds_b
        if not same_window:
            warnings.append(
                f"两侧轮次窗口不同（{rounds_a} vs {rounds_b}），差异会混入窗口口径"
            )
    if not a.meta.is_synthetic and b.meta.is_synthetic:
        warnings.append("一侧为合成数据")

    if a.workload.execution.mode is not b.workload.execution.mode:
        warnings.append(
            "两侧执行模式不同（eager vs graph）：op_summary 中暴露的算子集合与调用次数口径不同"
            "（Graph 会把部分算子捕获/融合进图），Δshare 与“消失的算子”只能作为线索"
        )
        volume_a = (a.meta.extra or {}).get("comm_volume") or {}
        volume_b = (b.meta.extra or {}).get("comm_volume") or {}
        bytes_a, bytes_b = volume_a.get("bytes_total"), volume_b.get("bytes_total")
        if bytes_a and bytes_b:
            ratio = bytes_b / bytes_a
            if 0.8 <= ratio <= 1.25:
                warnings.append(
                    f"通信字节量基本一致（{bytes_a / 1024**2:.1f} MiB vs "
                    f"{bytes_b / 1024**2:.1f} MiB，{ratio:.3f}x），说明通信工作量没有变化；"
                    f"但 HCCL 记录数 {volume_a.get('unique_tasks')} vs "
                    f"{volume_b.get('unique_tasks')}，Graph 下记录被合并，"
                    "因此「通信占比/调用次数」在两侧不可比，只能比字节量"
                )
            else:
                warnings.append(
                    f"通信字节量差异 {ratio:.2f}x（{bytes_a / 1024**2:.1f} MiB vs "
                    f"{bytes_b / 1024**2:.1f} MiB），先确认 token 数/层数/TP 是否一致再解读"
                )
        else:
            hcom_a = _hcom_calls(a)
            hcom_b = _hcom_calls(b)
            if hcom_a and hcom_b:
                ratio = hcom_b / hcom_a
                if ratio < 0.2 or ratio > 5:
                    warnings.append(
                        f"HCCL 调用次数差异 {ratio:.3f}x（{hcom_a} -> {hcom_b}）："
                        "Graph 下通信记录可能被合并，通信占比不可直接比较；"
                        "建议补采 COMMUNICATION_TASK_INFO 以比较字节量"
                    )

    for table in (a, b):
        if table.meta.parser == "ascend_op_report_sqlite":
            warnings.append(
                f"{table.workload_id} 的数据来自 sqlite 回填（op_report）：没有时间戳与流水指标，"
                "轮次窗口/流水占比不可比，仅能比较算子级 calls 与累计时间"
            )
    param_diffs = _collection_param_diffs(a.workload, b.workload)
    if param_diffs:
        warnings.append("采集参数差异（不比不可比判定，但会影响结论）: " + "; ".join(param_diffs))

    report = ComparisonReport(
        a_id=a.workload_id,
        b_id=b.workload_id,
        comparable=comparable,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        same_window=same_window,
        window=window_a if same_window else None,
        total_a_us=a.total_operator_time_us(),
        total_b_us=b.total_operator_time_us(),
    )
    if not comparable:
        report.observations = ("不可比：先统一采集条件再对比，否则差异无法归因",)
        return report

    left, right = _summarize(a), _summarize(b)
    deltas: list[OperatorDelta] = []
    for operator in set(left) | set(right):
        item_a, item_b = left.get(operator), right.get(operator)
        share_a = item_a["share"] if item_a else None
        share_b = item_b["share"] if item_b else None
        if max(share_a or 0.0, share_b or 0.0) < min_share:
            continue
        calls_a = int(item_a["calls"]) if item_a else None
        calls_b = int(item_b["calls"]) if item_b else None
        deltas.append(
            OperatorDelta(
                operator=operator,
                presence="both" if item_a and item_b else ("only_a" if item_a else "only_b"),
                share_a=share_a,
                share_b=share_b,
                delta_share=(share_b - share_a) if share_a is not None and share_b is not None else None,
                calls_a=calls_a,
                calls_b=calls_b,
                calls_ratio=(calls_b / calls_a) if calls_a and calls_b else None,
                avg_a=item_a["avg"] if item_a else None,
                avg_b=item_b["avg"] if item_b else None,
                avg_ratio=(
                    item_b["avg"] / item_a["avg"]
                    if item_a and item_b and item_a["avg"] and item_b["avg"]
                    else None
                ),
            )
        )
    deltas.sort(
        key=lambda d: abs(d.delta_share) if d.delta_share is not None else (d.share_b or d.share_a or 0),
        reverse=True,
    )
    report.deltas = deltas[:top]
    report.observations = _observations(a, b, left, right, deltas)
    return report


def _observations(
    a: OperatorTable,
    b: OperatorTable,
    left: dict[str, dict[str, float]],
    right: dict[str, dict[str, float]],
    deltas: list[OperatorDelta],
) -> tuple[str, ...]:
    notes: list[str] = []
    total_a, total_b = a.total_operator_time_us(), b.total_operator_time_us()
    if total_a and total_b:
        notes.append(
            f"设备侧累计时间 {a.workload_id}={total_a / 1e6:.3f}s，"
            f"{b.workload_id}={total_b / 1e6:.3f}s，比值 {total_b / total_a:.3f}"
            "（累计时间不是端到端延迟）"
        )

    def total_calls(summary: dict[str, dict[str, float]]) -> int:
        return int(sum(item["calls"] for item in summary.values()))

    calls_a, calls_b = total_calls(left), total_calls(right)
    if calls_a and calls_b:
        notes.append(
            f"算子调用总数 {calls_a} -> {calls_b}（{calls_b / calls_a:.3f}x）"
        )

    disappeared = [d.operator for d in deltas if d.presence == "only_a"][:5]
    appeared = [d.operator for d in deltas if d.presence == "only_b"][:5]
    if disappeared:
        notes.append("仅在 " + a.workload_id + " 出现: " + ", ".join(disappeared))
    if appeared:
        notes.append("仅在 " + b.workload_id + " 出现: " + ", ".join(appeared))

    # 只对占比 >=1% 的共享算子做单次耗时对比，避免被极小算子的上报差异主导
    shared = [
        d
        for d in deltas
        if d.presence == "both"
        and d.avg_ratio is not None
        and max(d.share_a or 0.0, d.share_b or 0.0) >= 0.01
    ]
    if shared:
        by_share = sorted(shared, key=lambda d: max(d.share_a or 0.0, d.share_b or 0.0), reverse=True)[:5]
        notes.append(
            "占比最高且两侧都存在的前 5 个算子，单次耗时比 (b/a): "
            + ", ".join(f"{d.operator}({d.avg_ratio:.2f}x)" for d in by_share)
        )
        shrink_calls = [d for d in shared if d.calls_ratio and d.calls_ratio < 0.8]
        if shrink_calls:
            notes.append(
                f"{len(shrink_calls)} 个占比 >=1% 的算子调用次数下降，"
                "若单次耗时基本不变，更像下发/调度开销下降而非计算效率提升（需 Timeline 确认）"
            )
    return tuple(notes)
