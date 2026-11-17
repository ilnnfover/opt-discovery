"""Fallback parser: read per-device msprof sqlite instead of exported CSV.

Use case (real incident, ``$PROF_ROOT/qwen35-decode``): for TP rank 1 the msprof
*merge/export* stage failed, so ``mindstudio_profiler_output`` only contains the
raw ``msprof_*.json`` -- no ``op_statistic``/``op_summary`` CSV.  The raw
per-device sqlite, however, is intact:

    device_<N>/sqlite/op_counter.db        -> op_report(op_type, core_type,
                                              occurrences, total_time, min, avg,
                                              max, ratio)
    device_<N>/sqlite/ai_core_op_summary.db-> ge_summary / ai_core_metrics /
                                              task_time   (per-instance detail)
    device_<N>/sqlite/ascend_task.db       -> AscendTask

This module reads ``op_report`` only, because it was verified to be **identical**
to the exported ``op_statistic`` CSV (rank 0: 70/70 keys, calls and totals equal,
time unit = ns).  It produces an ``operator + core_type`` table, i.e. exactly the
aggregate semantics of ``ascend_op_statistic.yaml``.

Limits that are reported in ``meta`` instead of being hidden:

* no per-instance pipe metrics (``aic_*``/``aiv_*``) -> only duration is known;
* no row timestamps -> a steady-state window cannot be applied here;
* per-instance joins by ``task_id`` are **not** used: ``task_id`` wraps around
  (65535 distinct values for 1.36M rows), so a naive join multiplies rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..schemas.metrics import Metric, Provenance
from ..schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from ..schemas.workload import WorkloadSpec
from .base import ParserCapabilities, ParserError

__all__ = ["SqliteOpReportParser", "find_op_counter_db", "CORE_TYPE_TO_TASK"]

#: Core Type -> schema TaskType（与 ascend_op_statistic.yaml 的映射保持一致）
CORE_TYPE_TO_TASK = {
    "ai_core": TaskType.KERNEL,
    "ai_vector_core": TaskType.KERNEL,
    "mix_aic": TaskType.KERNEL,
    "mix_aiv": TaskType.KERNEL,
    "dsa_sqe": TaskType.OTHER,
    "communication": TaskType.COMMUNICATION,
    "hccl": TaskType.COMMUNICATION,
}

_NS_PER_US = 1000.0


@dataclass
class SqliteOpReportParser:
    """Reads ``op_counter.db::op_report`` for one rank directory."""

    name = "ascend_op_report_sqlite"
    version = "0.1"
    capabilities = ParserCapabilities(
        provides_timeline=False,
        provides_shape=False,
        provides_hardware_metrics=False,
        provides_source_location=False,
        requires_column_mapping=False,
    )

    def can_parse(self, path: Path) -> bool:
        path = Path(path)
        return path.is_file() and path.name == "op_counter.db"

    def parse(self, paths: Path | Sequence[Path], workload: WorkloadSpec) -> OperatorTable:
        db_path = find_op_counter_db(paths)
        meta = ParseMeta(
            parser=self.name,
            parser_version=self.version,
            source_files=(str(db_path),),
            extra={
                "row_semantics": "aggregate",
                "aggregate_by": ["operator", "core_type"],
                "window": None,
                "time_unit_source": "ns",
                "needs_verification": False,
                "verified_against": {
                    "method": "rank0 op_report == exported op_statistic CSV（calls/total 完全一致）",
                    "checked_on": "2026-11-14",
                },
            },
        )
        meta.add_warning(
            "sqlite_fallback",
            "数据来自 device_*/sqlite/op_counter.db::op_report（CSV 导出缺失时的回填路径）",
            str(db_path),
        )
        meta.add_warning(
            "no_pipe_metrics",
            "该路径没有 aic_*/aiv_* 流水时间，流水占比类规则将报告为 blocked",
            str(db_path),
        )
        meta.add_warning(
            "no_timestamps",
            "该路径没有逐实例时间戳，无法应用稳态轮次窗口",
            str(db_path),
        )

        records: list[OperatorRecord] = []
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise ParserError(f"{db_path}: 无法打开 sqlite: {exc}") from exc

        with connection:
            try:
                rows = list(
                    connection.execute(
                        "select op_type, core_type, occurrences, total_time, min, avg, max, ratio"
                        " from op_report"
                    )
                )
            except sqlite3.Error as exc:
                raise ParserError(f"{db_path}: 读取 op_report 失败: {exc}") from exc

        for op_type, core_type, occurrences, total_ns, min_ns, avg_ns, max_ns, ratio in rows:
            calls = _to_int(occurrences)
            if not op_type or not calls:
                meta.skipped_rows += 1
                continue
            task_type = CORE_TYPE_TO_TASK.get(str(core_type).strip().lower(), TaskType.OTHER)
            extra = {}
            ratio_value = _to_float(ratio)
            if ratio_value is not None:
                extra["profiler_ratio"] = Metric(
                    ratio_value, "%", Provenance.MEASURED, f"{db_path.name}:op_report.ratio"
                )
            records.append(
                OperatorRecord(
                    workload_id=workload.workload_id,
                    operator=str(op_type),
                    calls=calls,
                    total_time_us=_us(total_ns, f"{db_path.name}:op_report.total_time"),
                    avg_time_us=_us(avg_ns, f"{db_path.name}:op_report.avg"),
                    min_time_us=_us(min_ns, f"{db_path.name}:op_report.min"),
                    max_time_us=_us(max_ns, f"{db_path.name}:op_report.max"),
                    task_type=task_type,
                    extra=extra,
                    attributes={"raw_operator": str(op_type), "core_type": str(core_type)},
                )
            )
            meta.row_count += 1

        return OperatorTable(workload=workload, records=records, meta=meta)


def find_op_counter_db(paths: Path | Sequence[Path]) -> Path:
    """Accept a db file, a ``sqlite/`` dir, a ``device_N/`` dir, or a PROF dir."""
    candidates = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(p) for p in paths]
    for candidate in candidates:
        if candidate.is_file():
            if candidate.name == "op_counter.db":
                return candidate
            raise ParserError(f"{candidate}: 不是 op_counter.db")
        matches = sorted(candidate.glob("**/sqlite/op_counter.db"))
        if not matches:
            matches = sorted(candidate.glob("op_counter.db"))
        if matches:
            if len(matches) > 1:
                raise ParserError(
                    f"{candidate}: 发现多个 op_counter.db {[str(m) for m in matches]}，"
                    "请显式指定其中一个（当前不支持跨 rank 合并）"
                )
            return matches[0]
    raise ParserError(f"未找到 op_counter.db：{candidates}")


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    number = _to_float(value)
    return None if number is None else int(round(number))


def _us(value_ns: object, source: str) -> Metric | None:
    number = _to_float(value_ns)
    if number is None:
        return None
    return Metric(number / _NS_PER_US, "us", Provenance.MEASURED, source)
