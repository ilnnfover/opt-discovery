"""Export the two flat tables promised by the plan (P0-1).

``workload_summary.csv`` 一 Workload 一行：采集条件、窗口、设备忙碌率、通信量、
候选/机会数量。
``operator_summary.csv`` 一算子一行：calls、累计/单次耗时、占比、命中的瓶颈。

只读取 ``analyze`` 已产出的 JSON，不重算任何指标；缺文件时该列留空而不是填 0。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

__all__ = ["write_summaries", "WORKLOAD_COLUMNS", "OPERATOR_COLUMNS"]

WORKLOAD_COLUMNS = [
    "workload_id",
    "phase",
    "mode",
    "graph_mode",
    "record_count",
    "operator_count",
    "calls",
    "total_us",
    "window_rounds",
    "window_duration_s",
    "device_busy_ratio",
    "window_degraded",
    "max_gap_ms",
    "comm_mib",
    "comm_tasks",
    "candidates_total",
    "opportunities_total",
    "synthetic",
]

OPERATOR_COLUMNS = [
    "workload_id",
    "operator",
    "task_type",
    "calls",
    "total_us",
    "avg_us",
    "max_us",
    "share",
    "top_bottleneck",
    "top_level",
    "top_confidence",
    "rule_id",
]


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_artifact(
    workload_dir: Path, name: str, allowed: set[str] | None
) -> dict[str, Any] | None:
    """只读取本轮清单登记过的产物，避免复用目录时读到旧的可选文件。"""
    relative = f"{workload_dir.name}/{name}"
    if allowed is not None and relative not in allowed:
        return None
    return _load(workload_dir / name)


def _workload_row(
    workload_dir: Path, allowed: set[str] | None = None
) -> dict[str, Any] | None:
    table = _read_artifact(workload_dir, "operator_table.json", allowed)
    if table is None:
        return None
    meta = table.get("meta", {})
    extra = meta.get("extra", {})
    window = extra.get("window") or {}
    quality = extra.get("window_quality") or {}
    execution = table.get("workload", {}).get("execution", {})
    comm = _read_artifact(workload_dir, "comm.json", allowed) or {}
    diagnosis = _read_artifact(workload_dir, "diagnosis.json", allowed) or {}
    opportunities = _read_artifact(workload_dir, "opportunities.json", allowed) or {}

    records = table.get("records", [])
    total = sum(
        (record.get("total_time_us") or {}).get("value") or 0.0 for record in records
    )
    calls = sum(int(record.get("calls") or 0) for record in records)
    duration_us = window.get("duration_us")

    return {
        "workload_id": table.get("workload", {}).get("workload_id") or workload_dir.name,
        "phase": execution.get("phase", ""),
        "mode": execution.get("mode", ""),
        "graph_mode": execution.get("graph_mode", ""),
        "record_count": meta.get("row_count", ""),
        "operator_count": len(records),
        "calls": calls,
        "total_us": round(total, 3),
        "window_rounds": "|".join(str(r) for r in window.get("rounds", [])),
        "window_duration_s": round(duration_us / 1e6, 6) if duration_us else "",
        "device_busy_ratio": quality.get("busy_ratio", ""),
        "window_degraded": quality.get("degraded", ""),
        "max_gap_ms": round(quality.get("max_gap_us", 0.0) / 1000, 3)
        if quality.get("max_gap_us") is not None
        else "",
        "comm_mib": comm.get("mib_total", ""),
        "comm_tasks": comm.get("unique_tasks", ""),
        "candidates_total": diagnosis.get("summary", {}).get("candidates_total", ""),
        "opportunities_total": opportunities.get("count", ""),
        "synthetic": meta.get("is_synthetic", False),
    }


def _operator_rows(
    workload_dir: Path, allowed: set[str] | None = None
) -> list[dict[str, Any]]:
    table = _read_artifact(workload_dir, "operator_table.json", allowed)
    if table is None:
        return []
    diagnosis = _read_artifact(workload_dir, "diagnosis.json", allowed) or {}
    top_by_operator: dict[str, dict[str, Any]] = {}
    for item in diagnosis.get("operators", []):
        candidates = item.get("candidates") or []
        if candidates:
            top_by_operator[item["operator"]] = candidates[0]

    records = table.get("records", [])
    total = sum(
        (record.get("total_time_us") or {}).get("value") or 0.0 for record in records
    )
    rows: list[dict[str, Any]] = []
    for record in records:
        value = (record.get("total_time_us") or {}).get("value")
        top = top_by_operator.get(record["operator"]) or {}
        rows.append(
            {
                "workload_id": table.get("workload", {}).get("workload_id") or workload_dir.name,
                "operator": record["operator"],
                "task_type": record.get("task_type", ""),
                "calls": record.get("calls", ""),
                "total_us": round(value, 3) if value is not None else "",
                "avg_us": round((record.get("avg_time_us") or {}).get("value") or 0.0, 3)
                if record.get("avg_time_us")
                else "",
                "max_us": round((record.get("max_time_us") or {}).get("value") or 0.0, 3)
                if record.get("max_time_us")
                else "",
                "share": round(value / total, 6) if value and total else "",
                "top_bottleneck": top.get("bottleneck", ""),
                "top_level": top.get("level", ""),
                "top_confidence": top.get("confidence", ""),
                "rule_id": top.get("rule_id", ""),
            }
        )
    rows.sort(key=lambda row: (row["workload_id"], -(row["total_us"] or 0.0)))
    return rows


def write_summaries(
    out_dir: str | Path,
    *,
    include: set[str] | None = None,
    artifacts: set[str] | None = None,
) -> tuple[Path, Path]:
    """Read an ``analyze`` output directory and write both CSV files.

    ``include``：只统计本次运行成功写出的 workload 目录；``artifacts``：只读取本轮
    manifest 登记过的文件（避免同一 workload 的旧 ``comm.json`` 等混进本轮 CSV）。
    """
    root = Path(out_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"{root}: 目录不存在")

    workload_rows: list[dict[str, Any]] = []
    operator_rows: list[dict[str, Any]] = []
    for workload_dir in sorted(
        p
        for p in root.iterdir()
        if p.is_dir() and (include is None or p.name in include)
    ):
        row = _workload_row(workload_dir, artifacts)
        if row is not None:
            workload_rows.append(row)
        operator_rows.extend(_operator_rows(workload_dir, artifacts))

    workload_path = root / "workload_summary.csv"
    operator_path = root / "operator_summary.csv"
    with workload_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKLOAD_COLUMNS)
        writer.writeheader()
        writer.writerows(workload_rows)
    with operator_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OPERATOR_COLUMNS)
        writer.writeheader()
        writer.writerows(operator_rows)
    return workload_path, operator_path
