"""Operator hotspots and the cross-workload hotspot matrix.

Both operate on :class:`OperatorTable` and only use measured fields; an operator
whose total time is unknown is reported with ``share=None`` instead of 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..schemas.operator import OperatorTable

__all__ = ["OperatorHotspot", "operator_hotspots", "hotspot_matrix", "matrix_to_text"]


@dataclass(frozen=True)
class OperatorHotspot:
    operator: str
    task_type: str
    calls: int
    total_us: float
    share: float | None
    avg_us: float
    max_us: float | None
    variants: int = 1

    def to_dict(self) -> dict:
        return {
            "operator": self.operator,
            "task_type": self.task_type,
            "calls": self.calls,
            "total_us": self.total_us,
            "share": self.share,
            "avg_us": self.avg_us,
            "max_us": self.max_us,
            "variants": self.variants,
        }


def operator_hotspots(
    table: OperatorTable, top: int | None = None, *, merge_variants: bool = True
) -> list[OperatorHotspot]:
    """Rank operators by accumulated device time within one workload.

    Aggregation here is by **operator name**, i.e. the same name split by
    core/task type is merged.  That is the right unit for cross-workload
    comparison; per-variant detail stays available in the operator table.
    """
    total = table.total_operator_time_us()
    buckets: dict[tuple[str, str], dict] = {}

    for record in table.records:
        if record.total_time_us is None:
            continue
        key = (record.operator, record.task_type.value if not merge_variants else "*")
        bucket = buckets.setdefault(
            key,
            {"total": 0.0, "calls": 0, "max": None, "variants": 0, "types": set()},
        )
        bucket["total"] += record.total_time_us.value
        bucket["calls"] += record.calls
        bucket["variants"] += 1
        bucket["types"].add(record.task_type.value)
        if record.max_time_us is not None:
            bucket["max"] = (
                record.max_time_us.value
                if bucket["max"] is None
                else max(bucket["max"], record.max_time_us.value)
            )

    hotspots: list[OperatorHotspot] = []
    for (operator, _), bucket in buckets.items():
        calls = bucket["calls"]
        types = bucket["types"]
        hotspots.append(
            OperatorHotspot(
                operator=operator,
                task_type="+".join(sorted(types)) if len(types) > 1 else next(iter(types)),
                calls=calls,
                total_us=bucket["total"],
                share=(bucket["total"] / total) if total else None,
                avg_us=bucket["total"] / calls if calls else 0.0,
                max_us=bucket["max"],
                variants=bucket["variants"],
            )
        )
    hotspots.sort(key=lambda item: item.total_us, reverse=True)
    return hotspots[:top] if top else hotspots


def hotspot_matrix(
    tables: Mapping[str, OperatorTable],
    *,
    top: int = 20,
    operators: Iterable[str] | None = None,
) -> dict:
    """Build ``operator -> {workload: share}`` for the union of top operators.

    ``workloads`` order follows the input mapping so the table can be printed
    deterministically.
    """
    workload_ids = list(tables)
    per_workload = {
        workload_id: {h.operator: h for h in operator_hotspots(table)}
        for workload_id, table in tables.items()
    }

    if operators is not None:
        selected = list(operators)
    else:
        selected = []
        seen: set[str] = set()
        for workload_id in workload_ids:
            for hotspot in list(per_workload[workload_id].values())[:top]:
                if hotspot.operator not in seen:
                    seen.add(hotspot.operator)
                    selected.append(hotspot.operator)

    rows = []
    for operator in selected:
        row = {"operator": operator, "workloads": {}}
        for workload_id in workload_ids:
            hotspot = per_workload[workload_id].get(operator)
            row["workloads"][workload_id] = (
                {
                    "share": hotspot.share,
                    "calls": hotspot.calls,
                    "avg_us": hotspot.avg_us,
                }
                if hotspot
                else None
            )
        rows.append(row)
    return {
        "workloads": workload_ids,
        "rows": rows,
        "note": "share 为算子累计时间占该 Workload 内已解析记录累计时间的比例；"
        "op_statistic 口径不含 HCCL，op_summary 口径含 HCCL，跨口径不可直接比较",
    }


def matrix_to_text(matrix: dict, top: int = 15) -> str:
    workloads = matrix["workloads"]
    short = {wid: _short(wid) for wid in workloads}
    header = f"{'operator':<26}" + "".join(f"{short[w]:>14}" for w in workloads)
    lines = [header]
    for row in matrix["rows"][:top]:
        cells = []
        for workload_id in workloads:
            cell = row["workloads"][workload_id]
            cells.append(f"{cell['share'] * 100:13.2f}%" if cell else f"{'-':>14}")
        lines.append(f"{row['operator']:<26}" + "".join(cells))
    return "\n".join(lines)


def _short(workload_id: str) -> str:
    """``qwen35-35b-a3b-prefill-graph`` -> ``prefill-graph``."""
    for marker in ("prefill", "decode"):
        index = workload_id.rfind(marker)
        if index >= 0:
            return workload_id[index:]
    return workload_id[:14]
