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

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from ..schemas.workload import WorkloadSpec
from .workload_log import LogParseError, RunLog

__all__ = [
    "ExportError",
    "RankExport",
    "ProfileBundle",
    "discover_bundle",
    "discover_all",
    "inspect_ranks",
    "EXPORT_DIR_LABEL",
]

#: Workload YAML 中用这个 label 声明对应的导出目录名
EXPORT_DIR_LABEL = "export_dir"
COLLECTION_RUN_LABEL = "collection_run_id"
COLLECTION_MANIFEST = "collection_manifest.json"

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
    msprof_dbs: tuple[Path, ...] = ()

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
    collection: dict | None = None

    @property
    def workload_id(self) -> str:
        return self.workload.workload_id

    @property
    def window_rounds(self) -> tuple[int, ...] | None:
        if self.collection is None:
            return None
        rounds = self.collection["fixed"].get("window_rounds")
        return tuple(rounds) if rounds is not None else None

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


def inspect_ranks(export_dir: Path) -> tuple[tuple[RankExport, ...], dict[str, str]]:
    """Select usable sessions per device for both collection checks and analysis."""
    if not export_dir.is_dir():
        raise ExportError(f"{export_dir}: 目录不存在")
    prof_dirs = sorted(p for p in export_dir.glob("PROF_*") if p.is_dir())
    nested = sorted(p for p in (export_dir / "PROF").glob("PROF_*") if p.is_dir())
    if prof_dirs and nested:
        raise ExportError(f"{export_dir}: 同时存在 flat 与 PROF/ 嵌套导出，请明确选择本轮数据")
    prof_dirs = prof_dirs or nested
    if not prof_dirs:
        raise ExportError(f"{export_dir}: 未找到 PROF_* 导出目录")

    by_rank: dict[int, RankExport] = {}
    excluded: dict[str, str] = {}
    for position, prof_dir in enumerate(prof_dirs):
        output = prof_dir / "mindstudio_profiler_output"
        device_dirs = sorted(path for path in prof_dir.glob("device_*") if path.is_dir())
        device = device_dirs[0].name if device_dirs else f"device_{position}"
        rank_match = _NUM_RE.search(device)
        rank = int(rank_match.group(1)) if rank_match else position

        summary_files = tuple(sorted(output.glob("op_summary*.csv"), key=_natural_key))
        op_report_db = next(iter(sorted(prof_dir.glob("device_*/sqlite/op_counter.db"))), None)
        if not summary_files and op_report_db is None:
            excluded[prof_dir.name] = "no_operator_export"
            continue
        statistic = sorted(output.glob("op_statistic*.csv"), key=_natural_key)
        api = sorted(output.glob("api_statistic*.csv"), key=_natural_key)
        candidate = RankExport(
            rank=rank,
            directory=prof_dir,
            device=device,
            op_summary_files=summary_files,
            op_statistic_file=statistic[0] if statistic else None,
            api_statistic_file=api[0] if api else None,
            op_report_db=op_report_db,
            msprof_dbs=tuple(sorted(prof_dir.glob("msprof_*.db"))),
        )
        previous = by_rank.get(rank)
        if previous is None:
            by_rank[rank] = candidate
        elif _rank_priority(candidate) > _rank_priority(previous):
            excluded[previous.directory.name] = "duplicate_device"
            by_rank[rank] = candidate
        else:
            excluded[prof_dir.name] = "duplicate_device"
    return tuple(by_rank[key] for key in sorted(by_rank)), excluded


def _rank_priority(rank: RankExport) -> tuple[bool, int, int]:
    return (
        bool(rank.op_summary_files),
        max((path.stat().st_size for path in rank.msprof_dbs), default=0),
        sum(path.stat().st_size for path in rank.op_summary_files),
    )


def _read_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"{path}: 读取采集清单失败") from exc
    if not isinstance(manifest, dict):
        raise ExportError(f"{path}: 采集清单必须为对象")
    return manifest


def _build(
    workload: WorkloadSpec, capture_dir: Path, manifest: dict | None = None
) -> ProfileBundle:
    export_dir = capture_dir
    manifest_path = capture_dir / COLLECTION_MANIFEST
    if manifest is None and manifest_path.is_file():
        manifest = _read_manifest(manifest_path)
    if manifest is not None:
        relative = manifest.get("export_dir", ".")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or not (capture_dir / relative).resolve().is_relative_to(capture_dir.resolve())
        ):
            raise ExportError(f"{manifest_path}: export_dir 必须指向本次采集目录内的路径")
        export_dir = capture_dir / relative
    ranks, excluded = inspect_ranks(export_dir)
    if not ranks:
        raise ExportError(f"{export_dir}: 未找到可用的 op_summary*.csv")

    notes = [f"{name}: {reason}，已跳过" for name, reason in excluded.items()]
    for rank in ranks:
        num_slices = len(rank.op_summary_files)
        if not num_slices:
            notes.append(f"rank {rank.rank}: 无 op_summary，可用 sqlite op_report 回填")
        elif num_slices > 1:
            notes.append(f"rank {rank.rank}: op_summary 分为 {num_slices} 个切片")

    run_log: RunLog | None = None
    log_path = capture_dir / "workload.log"
    if log_path.is_file():
        try:
            run_log = RunLog.parse(log_path)
        except LogParseError as exc:
            notes.append(f"workload.log 解析失败，无法按轮次法切窗: {exc}")
    else:
        notes.append("缺少 workload.log，无法获得轮次边界")
    if manifest is not None:
        workload = _bind_capture(workload, manifest_path, manifest, run_log, ranks)

    return ProfileBundle(
        export_dir=export_dir,
        workload=workload,
        ranks=ranks,
        run_log=run_log,
        notes=tuple(notes),
        collection=manifest,
    )


def _bind_capture(
    workload: WorkloadSpec, path: Path, manifest: dict, log: RunLog | None,
    ranks: tuple[RankExport, ...],
) -> WorkloadSpec:
    run_id = workload.labels.get(COLLECTION_RUN_LABEL)
    fixed = manifest.get("fixed") or {}
    variables = manifest.get("variables") or {}
    checks = manifest.get("checks") or {}
    if not all(isinstance(item, dict) for item in (fixed, variables, checks)):
        raise ExportError(f"{path}: fixed、variables 和 checks 必须为对象")
    if run_id and manifest.get("run_id") != run_id:
        raise ExportError(f"{path}: run_id 与 {run_id} 不一致")
    if (
        variables.get("mode") != workload.execution.mode.value
        or variables.get("graph_mode") != workload.execution.graph_mode.value
        or fixed.get("tensor_parallel_size") != workload.hardware.device_count
    ):
        raise ExportError(f"{path}: 执行模式、图模式或 TP 与 workload 不一致")
    active = fixed.get("active_rounds")
    if (
        manifest.get("exit_code") != 0
        or checks.get("ok") is not True
        or type(active) is not int
        or active < 1
        or log is None
        or log.round_indices != tuple(range(active))
        or any(round_.duration_us <= 0 for round_ in log.rounds)
        or len(ranks) != workload.hardware.device_count
        or not all(rank.op_summary_files and rank.msprof_dbs for rank in ranks)
    ):
        raise ExportError(f"{path}: 采集未通过退出码、轮次或 rank 自检")
    declared = checks.get("ranks")
    if declared is not None:
        actual = {(rank.device, rank.directory.name) for rank in ranks}
        if (
            not isinstance(declared, list)
            or len(declared) != len(ranks)
            or not all(isinstance(item, dict) for item in declared)
            or actual != {(str(item.get("device")), str(item.get("dir"))) for item in declared}
        ):
            raise ExportError(f"{path}: 实际 rank 目录与清单声明不一致")
    window = fixed.get("window_rounds")
    if window is not None and (
        not isinstance(window, list)
        or not window
        or any(type(index) is not int or index not in range(active) for index in window)
        or window != sorted(set(window))
    ):
        raise ExportError(f"{path}: window_rounds 必须为有效、递增的实采轮次")
    output = variables.get("output_length")
    observed = {item.output_tokens for item in log.rounds}
    if type(output) is not int or output < 0 or observed - {None, output} or (
        workload.input.output_length is not None and output != workload.input.output_length
    ):
        raise ExportError(f"{path}: 输出长度与 workload 或实际日志不一致")
    if observed == {output}:
        return replace(workload, input=replace(workload.input, output_length=output))
    return workload


def discover_all(workload_dir: str | Path, exports_root: str | Path) -> list[ProfileBundle]:
    """Bind explicit collection run IDs first, then fall back to legacy aliases."""
    root = Path(exports_root)
    workloads = [
        WorkloadSpec.from_yaml(path) for path in sorted(Path(workload_dir).glob("*.yaml"))
    ]
    run_ids = {workload.labels.get(COLLECTION_RUN_LABEL) for workload in workloads} - {None}
    captures: dict[str, list[tuple[Path, dict]]] = {}
    for path in sorted(root.glob(f"*/{COLLECTION_MANIFEST}")) if run_ids else ():
        manifest = _read_manifest(path)
        run_id = manifest.get("run_id")
        if isinstance(run_id, str) and run_id in run_ids:
            options = captures.setdefault(run_id, [])
            if all(directory.resolve() != path.parent.resolve() for directory, _ in options):
                options.append((path.parent, manifest))

    bundles: list[ProfileBundle] = []
    for workload in workloads:
        options = captures.get(workload.labels.get(COLLECTION_RUN_LABEL, ""), [])
        if len(options) > 1:
            paths = ", ".join(str(path) for path, _ in options)
            raise ExportError(f"{workload.workload_id}: 多个匹配的采集目录，请指定批次: {paths}")
        if options:
            capture_dir, manifest = options[0]
            bundles.append(_build(workload, capture_dir, manifest))
            continue
        label = workload.labels.get(EXPORT_DIR_LABEL)
        if not label:
            continue
        export_dir = root / label
        if not export_dir.is_dir():
            continue
        bundles.append(_build(workload, export_dir))
    return bundles
