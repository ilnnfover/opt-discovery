"""Discover an msprof export directory and bind it to a workload description.

Layout assumptions were verified against ``$PROF_ROOT``:

    <export_dir>/
      workload.log                       # 轮次边界与采集参数（外部事实）
      PROF_*/device_<N>/                 # 每个 rank 一个目录
      PROF_*/mindstudio_profiler_output/
          op_statistic_*.csv             # 按算子聚合（不含 HCCL）
          op_summary*.csv                # 逐实例明细，可能按 1e6 行切片
          api_statistic_*.csv            # Host API 聚合（无时间戳）
          task_time*.csv

Slice handling matters: ``qwen35-decode`` has ``op_summary_slice_0`` +
``op_summary_slice_1`` per rank (>=1.27M instances), so all slices of a rank
must be parsed as one table in slice order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..schemas.workload import WorkloadSpec
from .workload_log import LogParseError, RunLog

__all__ = [
    "ExportError",
    "RankExport",
    "ProfileBundle",
    "discover_bundle",
    "discover_all",
    "EXPORT_DIR_LABEL",
]

#: Workload YAML 中用这个 label 声明对应的导出目录名
EXPORT_DIR_LABEL = "export_dir"

_NUM_RE = re.compile(r"(\d+)")


class ExportError(RuntimeError):
    """Raised when an export directory does not have the expected layout."""


def _natural_key(path: Path) -> tuple:
    parts = _NUM_RE.split(path.name)
    return tuple(int(part) if part.isdigit() else part for part in parts)


@dataclass(frozen=True)
class RankExport:
    rank: int
    directory: Path
    device: str
    op_summary_files: tuple[Path, ...]
    op_statistic_file: Path | None
    api_statistic_file: Path | None
    op_report_db: Path | None = None

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "directory": str(self.directory),
            "device": self.device,
            "op_summary_files": [str(p) for p in self.op_summary_files],
            "op_statistic_file": str(self.op_statistic_file) if self.op_statistic_file else None,
            "api_statistic_file": str(self.api_statistic_file) if self.api_statistic_file else None,
            "op_report_db": str(self.op_report_db) if self.op_report_db else None,
        }


@dataclass(frozen=True)
class ProfileBundle:
    export_dir: Path
    workload: WorkloadSpec
    ranks: tuple[RankExport, ...]
    run_log: RunLog | None
    notes: tuple[str, ...] = ()

    @property
    def workload_id(self) -> str:
        return self.workload.workload_id

    def rank(self, index: int = 0) -> RankExport:
        for item in self.ranks:
            if item.rank == index:
                return item
        available = [item.rank for item in self.ranks]
        raise ExportError(f"{self.export_dir}: rank {index} 不存在（可用 {available}）")

    def to_dict(self) -> dict:
        return {
            "export_dir": str(self.export_dir),
            "workload_id": self.workload.workload_id,
            "execution": self.workload.execution.label,
            "verified": self.workload.verified,
            "run_log": self.run_log.to_dict() if self.run_log else None,
            "ranks": [r.to_dict() for r in self.ranks],
            "notes": list(self.notes),
        }


def discover_bundle(workload_path: str | Path, export_dir: str | Path | None = None) -> ProfileBundle:
    workload = WorkloadSpec.from_yaml(workload_path)
    if export_dir is None:
        label = workload.labels.get(EXPORT_DIR_LABEL)
        if not label:
            raise ExportError(
                f"{workload_path}: 未指定导出目录；请在 labels.{EXPORT_DIR_LABEL} 中声明，"
                "或显式传入 export_dir"
            )
        raise ExportError(
            f"{workload_path}: labels.{EXPORT_DIR_LABEL}={label!r} 需要与导出根目录结合使用，"
            "请调用 discover_all() 或显式传入 export_dir"
        )
    return _build(workload, Path(export_dir))


def _build(workload: WorkloadSpec, export_dir: Path) -> ProfileBundle:
    if not export_dir.is_dir():
        raise ExportError(f"{export_dir}: 目录不存在")

    prof_dirs = sorted(p for p in export_dir.glob("PROF_*") if p.is_dir())
    if not prof_dirs:
        raise ExportError(f"{export_dir}: 未找到 PROF_* 导出目录")

    ranks: list[RankExport] = []
    notes: list[str] = []
    for position, prof_dir in enumerate(prof_dirs):
        output = prof_dir / "mindstudio_profiler_output"
        if not output.is_dir():
            notes.append(f"{prof_dir.name}: 缺少 mindstudio_profiler_output，已跳过")
            continue
        device_dirs = sorted(prof_dir.glob("device_*"))
        device = device_dirs[0].name if device_dirs else f"device_{position}"
        rank_match = _NUM_RE.search(device)
        rank = int(rank_match.group(1)) if rank_match else position

        summary_files = tuple(sorted(output.glob("op_summary*.csv"), key=_natural_key))
        op_report_db = next(iter(sorted(prof_dir.glob("device_*/sqlite/op_counter.db"))), None)
        if not summary_files and op_report_db is None:
            notes.append(f"{prof_dir.name}: 缺少 op_summary*.csv 与 sqlite，已跳过")
            continue
        if not summary_files:
            notes.append(
                f"rank {rank}: 无 op_summary（聚合失败），可用 sqlite op_report 回填"
            )
        statistic = sorted(output.glob("op_statistic*.csv"), key=_natural_key)
        api = sorted(output.glob("api_statistic*.csv"), key=_natural_key)
        if len(summary_files) > 1:
            notes.append(f"rank {rank}: op_summary 分为 {len(summary_files)} 个切片")

        ranks.append(
            RankExport(
                rank=rank,
                directory=prof_dir,
                device=device,
                op_summary_files=summary_files,
                op_statistic_file=statistic[0] if statistic else None,
                api_statistic_file=api[0] if api else None,
                op_report_db=op_report_db,
            )
        )

    if not ranks:
        raise ExportError(f"{export_dir}: 未找到可用的 op_summary*.csv")

    run_log: RunLog | None = None
    log_path = export_dir / "workload.log"
    if log_path.is_file():
        try:
            run_log = RunLog.parse(log_path)
        except LogParseError as exc:
            notes.append(f"workload.log 解析失败，无法按轮次法切窗: {exc}")
    else:
        notes.append("缺少 workload.log，无法获得轮次边界")

    return ProfileBundle(
        export_dir=export_dir,
        workload=workload,
        ranks=tuple(sorted(ranks, key=lambda item: item.rank)),
        run_log=run_log,
        notes=tuple(notes),
    )


def discover_all(workload_dir: str | Path, exports_root: str | Path) -> list[ProfileBundle]:
    """Load every workload YAML that declares ``labels.export_dir``."""
    bundles: list[ProfileBundle] = []
    for path in sorted(Path(workload_dir).glob("*.yaml")):
        workload = WorkloadSpec.from_yaml(path)
        label = workload.labels.get(EXPORT_DIR_LABEL)
        if not label:
            continue
        export_dir = Path(exports_root) / label
        if not export_dir.is_dir():
            continue
        bundles.append(_build(workload, export_dir))
    return bundles
