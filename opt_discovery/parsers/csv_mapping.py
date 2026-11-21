"""Mapping-driven CSV parser for Ascend/msprof exports.

Design points that come from the real data (see configs/parsers/*.yaml):

* rows are **streamed**, never materialized: a decode rank has >=1.27M rows and
  4 slices in one run;
* one rank's data may span several slice files, parsed as one table in order;
* ``row_semantics`` decides how a row maps to a call bucket: ``aggregate``
  (op_statistic: one row already summarizes a variant) or ``instance``
  (op_summary: one row is one execution, ``calls`` = row count);
* an optional :class:`~opt_discovery.analyzer.window.Window` filters rows by
  ``row_start_time`` *before* any metric work, and accumulates busy/gap
  statistics for that window on the fly.

In both semantics the output is one record per aggregation key, with
``avg_time_us`` recomputed as ``total / calls`` (never an average of averages).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import yaml

from ..schemas.metrics import Metric, Provenance
from ..schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from ..schemas.workload import WorkloadSpec
from .base import ParserCapabilities, ParserError

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..primitives import Window

from ..events import normalize_instance_row
from ..primitives import COMMUNICATION_DURATION_SCALE, normalize_duration, window_span

__all__ = [
    "COMMUNICATION_DURATION_SCALE",
    "ColumnSpec",
    "ExtraMetricSpec",
    "AggregateSpec",
    "CsvMapping",
    "MappingCsvParser",
    "KNOWN_FIELDS",
    "ROW_SEMANTICS",
    "AGG_FUNCTIONS",
]

KNOWN_FIELDS = {
    "operator",
    "kernel",
    "calls",
    "total_time_us",
    "avg_time_us",
    "min_time_us",
    "max_time_us",
    "device_time_us",
    "task_type",
    "dtype",
    "shape",
    # 仅用于窗口筛选，不进入记录
    "row_start_time",
}

_FILTER_ONLY_FIELDS = {"row_start_time"}
ROW_SEMANTICS = {"aggregate", "instance"}
AGG_FUNCTIONS = {"sum", "mean", "max", "min"}
COMBINE_MODES = {"first", "sum"}

_NULL_TOKENS = {"", "-", "--", "n/a", "na", "none", "null", "nan"}

#: 支持的派生指标计算（逐实例，从非数值列推导）
_COMPUTED_METRICS = {"bytes_from_shapes", "bytes_time_for"}
_INSTRUMENT_METRICS = {"cube_utilization", "flops", "vector_flops"}

#: dtype 名 -> 字节数（键全部小写；昇腾导出常见 DT_ 前缀与裸名都收录）
_DTYPE_SIZES: dict[str, float] = {
    "bf16": 2, "dt_bf16": 2, "bfloat16": 2,
    "fp16": 2, "dt_fp16": 2, "float16": 2, "half": 2, "dt_half": 2,
    "fp32": 4, "dt_fp32": 4, "float32": 4, "float": 4, "dt_float": 4,
    "int8": 1, "dt_int8": 1, "uint8": 1, "dt_uint8": 1,
    "bool": 1, "dt_bool": 1,
    "int16": 2, "dt_int16": 2, "uint16": 2, "dt_uint16": 2,
    "int32": 4, "dt_int32": 4, "uint32": 4, "dt_uint32": 4,
    "int64": 8, "dt_int64": 8, "uint64": 8, "dt_uint64": 8,
    "double": 8, "dt_double": 8, "float64": 8, "dt_float64": 8,
}


def _shape_group_numel(raw: str) -> float | None:
    """解析一组 shape（如 ``1,2048``）为元素数；动态维/不可解析返回 None。"""
    text = raw.strip().strip('"').strip()
    if not text or text.lower() in _NULL_TOKENS:
        return None
    total = 1.0
    for dim in text.split(","):
        dim = dim.strip()
        if not dim:
            continue
        try:
            value = float(dim)
        except ValueError:
            return None
        if value <= 0:
            return None  # -1 等动态维，无法估算
        total *= value
    return total


def _dtype_size(raw: str) -> float | None:
    text = raw.strip().strip('"').lower()
    if not text or text.lower() in _NULL_TOKENS:
        return None
    return _DTYPE_SIZES.get(text)


def _estimate_row_bytes(row: list[str], positions: Sequence[int]) -> float | None:
    """按 (in_shapes, in_dtypes, out_shapes, out_dtypes) 列估算单实例搬运字节。

    约定：读(input numel×dtype) + 写(output numel×dtype)，即读写合计口径，
    与设备档案 peak_hbm_gbps 的微基准口径一致。某组 shape/dtype 不可解析
    （动态维、DT_UNDEFINED）时跳过该组；全部不可解析返回 None。
    估算忽略 L2/片上缓存复用，结果是 HBM 流量的上界近似。
    """
    if len(positions) < 4:
        return None
    total = 0.0
    parsed = 0
    # (shapes_idx, dtypes_idx) 成对：先输入后输出
    for shapes_idx, dtypes_idx in ((positions[0], positions[1]), (positions[2], positions[3])):
        if shapes_idx >= len(row) or dtypes_idx >= len(row):
            continue
        shapes_raw = (row[shapes_idx] or "").strip().strip('"')
        dtypes_raw = (row[dtypes_idx] or "").strip().strip('"')
        if not shapes_raw or shapes_raw.lower() in _NULL_TOKENS:
            continue
        shape_groups = shapes_raw.split(";")
        dtype_groups = dtypes_raw.split(";") if dtypes_raw else []
        for i, shape_group in enumerate(shape_groups):
            numel = _shape_group_numel(shape_group)
            if numel is None:
                continue
            size = (
                _dtype_size(dtype_groups[i])
                if i < len(dtype_groups)
                else None
            )
            if size is None:
                continue
            total += numel * size
            parsed += 1
    return total if parsed else None


@dataclass(frozen=True)
class ColumnSpec:
    names: tuple[str, ...]
    unit: str | None = None
    cast: str = "str"
    required: bool = False


@dataclass(frozen=True)
class ExtraMetricSpec:
    name: str
    columns: tuple[str, ...]
    unit: str
    agg: str = "sum"
    combine: str = "first"
    when_task_types: tuple[TaskType, ...] = ()
    #: 该列在**本任务类型下**的单位修正系数（例如 COMMUNICATION 的 Task Duration 是 ns）
    scale: float = 1.0
    #: 非数值列的派生计算（当前支持 "bytes_from_shapes"：shape×dtype 估算搬运字节）
    computed: str = ""


@dataclass(frozen=True)
class RowFlagSpec:
    """按行判定一个布尔标记（例如「该任务是否在图内执行」）。

    命中条件：该列取值非空且不在 ``exclude_values`` 中。
    """

    name: str
    columns: tuple[str, ...]
    exclude_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class AggregateSpec:
    by: tuple[str, ...] = ("operator",)


@dataclass(frozen=True)
class CsvMapping:
    parser: str
    version: str = "0"
    needs_verification: bool = True
    row_semantics: str = "aggregate"
    aggregate: AggregateSpec = field(default_factory=AggregateSpec)
    delimiter: str = ","
    encoding: str = "utf-8"
    fields: Mapping[str, ColumnSpec] = field(default_factory=dict)
    labels: Mapping[str, ColumnSpec] = field(default_factory=dict)
    extra_metrics: tuple[ExtraMetricSpec, ...] = ()
    row_flags: tuple[RowFlagSpec, ...] = ()
    task_type_map: Mapping[str, str] = field(default_factory=dict)
    name_aliases: Mapping[str, str] = field(default_factory=dict)
    defaults: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""
    verified_against: Mapping[str, str] = field(default_factory=dict)

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "CsvMapping":
        source = Path(path)
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ParserError(f"{path}: expected a mapping at top level")

        row_semantics = str(raw.get("row_semantics", "aggregate")).strip().lower()
        if row_semantics not in ROW_SEMANTICS:
            raise ParserError(f"{path}: row_semantics must be one of {sorted(ROW_SEMANTICS)}")

        fields_raw = raw.get("columns") or {}
        if not isinstance(fields_raw, Mapping):
            raise ParserError(f"{path}: 'columns' must be a mapping")
        fields: dict[str, ColumnSpec] = {}
        for key, spec in fields_raw.items():
            if key not in KNOWN_FIELDS:
                raise ParserError(
                    f"{path}: unknown canonical field {key!r}; allowed: {sorted(KNOWN_FIELDS)}"
                )
            fields[key] = _parse_column_spec(spec, key, source)
            if key.endswith("_us") and not fields[key].unit:
                raise ParserError(f"{path}: time column {key!r} must declare 'unit'")

        labels_raw = raw.get("labels") or {}
        if not isinstance(labels_raw, Mapping):
            raise ParserError(f"{path}: 'labels' must be a mapping")
        labels = {str(k): _parse_column_spec(v, str(k), source) for k, v in labels_raw.items()}

        aggregate_raw = raw.get("aggregate") or {}
        if not isinstance(aggregate_raw, Mapping):
            raise ParserError(f"{path}: 'aggregate' must be a mapping")
        by = tuple(str(item) for item in (aggregate_raw.get("by") or ("operator",)))
        if "operator" not in by:
            raise ParserError(f"{path}: aggregate.by must contain 'operator'")
        for key in by:
            if key != "operator" and key not in labels:
                raise ParserError(
                    f"{path}: aggregate.by entry {key!r} must be declared under 'labels'"
                )

        extras = tuple(_parse_extra(entry, source) for entry in raw.get("extra_metrics") or ())
        flags = tuple(_parse_row_flag(entry, source) for entry in raw.get("row_flags") or ())

        csv_cfg = raw.get("csv") or {}
        mapping = cls(
            parser=str(raw.get("parser", source.stem)),
            version=str(raw.get("parser_version", raw.get("version", "0"))),
            needs_verification=bool(raw.get("needs_verification", True)),
            row_semantics=row_semantics,
            aggregate=AggregateSpec(by=by),
            delimiter=str(csv_cfg.get("delimiter", ",")),
            encoding=str(csv_cfg.get("encoding", "utf-8")),
            fields=fields,
            labels=labels,
            extra_metrics=extras,
            row_flags=flags,
            task_type_map={_norm(k): str(v) for k, v in (raw.get("task_type_map") or {}).items()},
            name_aliases={str(k): str(v) for k, v in (raw.get("name_aliases") or {}).items()},
            defaults={str(k): str(v) for k, v in (raw.get("defaults") or {}).items()},
            notes=str(raw.get("notes", "")),
            verified_against={str(k): str(v) for k, v in (raw.get("verified_against") or {}).items()},
        )
        mapping._validate_required_columns(source)
        return mapping

    def _validate_required_columns(self, source: Path) -> None:
        if self.row_semantics == "instance":
            if "calls" in self.fields and self.fields["calls"].required:
                raise ParserError(
                    f"{source}: row_semantics=instance 时 calls 由行数得到，不能再要求 calls 列"
                )
            if "total_time_us" not in self.fields:
                raise ParserError(
                    f"{source}: row_semantics=instance 需要声明 total_time_us（逐实例时长）"
                )
        elif "total_time_us" not in self.fields and "avg_time_us" not in self.fields:
            raise ParserError(f"{source}: 需要 total_time_us 或 avg_time_us 之一")

    @property
    def missing_required_fields(self) -> tuple[str, ...]:
        names = [name for name, spec in self.fields.items() if spec.required]
        if self.row_semantics == "instance":
            names = [name for name in names if name != "calls"]
        return tuple(names)


@dataclass(frozen=True)
class _Columns:
    """Column positions resolved from one file header."""

    fields: Mapping[str, int]
    labels: Mapping[str, int]
    extras: Mapping[str, tuple[int, ...]]
    header: tuple[str, ...]
    flags: Mapping[str, int] = field(default_factory=dict)

    def index(self, name: str) -> int | None:
        return self.fields.get(name)


class MappingCsvParser:
    """Parses one or more operator CSV files into a single :class:`OperatorTable`."""

    capabilities = ParserCapabilities(
        provides_timeline=False, provides_shape=True, provides_hardware_metrics=True
    )

    def __init__(self, mapping: CsvMapping) -> None:
        self.mapping = mapping
        self.name = mapping.parser
        self.version = mapping.version

    # -- sniffing --------------------------------------------------------
    def can_parse(self, path: Path) -> bool:
        path = Path(path)
        if not path.is_file() or path.suffix.lower() not in (".csv", ".txt"):
            return False
        try:
            header = self._read_header(path)
        except OSError:
            return False
        columns = self._resolve_columns(list(header))
        if not self._has_time_column(columns):
            return False
        return all(name in columns.fields for name in self.mapping.missing_required_fields)

    def _read_header(self, path: Path) -> list[str]:
        with path.open(newline="", encoding=self.mapping.encoding) as handle:
            reader = csv.reader(handle, delimiter=self.mapping.delimiter)
            return next(reader, [])

    # -- parsing ---------------------------------------------------------
    def parse(
        self,
        paths: Path | Sequence[Path],
        workload: WorkloadSpec,
        *,
        window: Window | None = None,
    ) -> OperatorTable:
        files = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(p) for p in paths]
        if not files:
            raise ParserError("没有输入文件")
        for file in files:
            if not file.is_file():
                raise ParserError(f"{file}: file not found")

        meta = ParseMeta(
            parser=self.name,
            parser_version=self.version,
            source_files=tuple(str(p) for p in files),
            extra={
                "needs_verification": self.mapping.needs_verification,
                "row_semantics": self.mapping.row_semantics,
                "aggregate_by": list(self.mapping.aggregate.by),
                "verified_against": dict(self.mapping.verified_against),
                "window": window.to_dict() if window else None,
                "files": [],
                "rows_outside_window": 0,
            },
        )
        if self.mapping.needs_verification:
            meta.add_warning(
                "mapping_unverified",
                "列映射尚未与真实 Profiler 导出核对，字段解析结果需人工确认",
                str(files[0]),
            )
        if window is not None and "row_start_time" not in self.mapping.fields:
            raise ParserError(
                "窗口筛选需要 columns.row_start_time（逐实例时间戳），当前 mapping 未声明"
            )

        groups: dict[tuple[str, ...], _Group] = {}
        columns: _Columns | None = None
        union = _busy_union()
        rows_outside = 0
        round_counts: dict[int, int] = {}

        for file in files:
            with file.open(newline="", encoding=self.mapping.encoding) as handle:
                reader = csv.reader(handle, delimiter=self.mapping.delimiter)
                header = next(reader, [])
                file_columns = self._resolve_columns(header)
                if columns is None:
                    columns = file_columns
                    self._report_column_issues(columns, meta, file)
                elif file_columns.header != columns.header:
                    meta.add_warning(
                        "header_mismatch",
                        f"{file.name} 表头与其他切片不同，按本文件表头重新解析",
                        str(file),
                    )
                if not self._has_time_column(file_columns):
                    raise ParserError(
                        f"{file}: total_time_us 与 avg_time_us 均未命中，无法得到累计时间；"
                        f"实际表头为 {header}。请修正 mapping。"
                    )
                rows_in_file = 0
                for line_no, row in enumerate(reader, start=2):
                    meta.row_count += 1
                    rows_in_file += 1
                    row_metrics = None
                    timestamp = None
                    if self.mapping.row_semantics == "instance":
                        start_pos = file_columns.fields.get("row_start_time")
                        duration_pos = file_columns.fields.get("total_time_us")
                        if duration_pos is not None:
                            start_scale = (
                                _to_us(
                                    1.0,
                                    self.mapping.fields["row_start_time"].unit,
                                )
                                if start_pos is not None
                                and "row_start_time" in self.mapping.fields
                                else 1.0
                            )
                            duration_scale = _to_us(
                                1.0, self.mapping.fields["total_time_us"].unit
                            )
                            if window is not None:
                                # 保持旧语义：先按有效起点筛窗口/轮次，再处理时长
                                timestamp = _row_start(file_columns, row, self.mapping)
                                if timestamp is None:
                                    meta.skipped_rows += 1
                                    meta.add_warning(
                                        "row_without_start",
                                        "缺少可解析的时间戳，无法判断是否在窗口内，已跳过",
                                        f"{file.name}:{line_no}",
                                    )
                                    continue
                                span = window_span(
                                    timestamp, 0.0, window, policy="start"
                                )
                                if span is None:
                                    rows_outside += 1
                                    continue
                                timestamp = span[0]
                                for (
                                    round_index,
                                    round_start,
                                    round_end,
                                ) in window.round_spans:
                                    if round_start <= timestamp <= round_end:
                                        round_counts[round_index] = (
                                            round_counts.get(round_index, 0) + 1
                                        )
                                        break
                                type_pos = file_columns.fields.get("task_type")
                                row_metrics = normalize_instance_row(
                                    row,
                                    start_idx=start_pos,
                                    duration_idx=duration_pos,
                                    task_type_override=self._resolve_task_type(
                                        row[type_pos]
                                        if type_pos is not None and type_pos < len(row)
                                        else None
                                    ),
                                    operator_idx=file_columns.fields.get("operator"),
                                    start_scale=start_scale,
                                    duration_scale=duration_scale,
                                    window=None,
                                    number_parser=_to_float,
                                    allow_zero_duration=True,
                                )
                            else:
                                type_pos = file_columns.fields.get("task_type")
                                row_metrics = normalize_instance_row(
                                    row,
                                    start_idx=start_pos,
                                    duration_idx=duration_pos,
                                    task_type_override=self._resolve_task_type(
                                        row[type_pos]
                                        if type_pos is not None and type_pos < len(row)
                                        else None
                                    ),
                                    operator_idx=file_columns.fields.get("operator"),
                                    start_scale=start_scale,
                                    duration_scale=duration_scale,
                                    window=None,
                                    number_parser=_to_float,
                                    allow_zero_duration=True,
                                )
                    elif window is not None:
                        timestamp = _row_start(file_columns, row, self.mapping)
                        if timestamp is None:
                            meta.skipped_rows += 1
                            meta.add_warning(
                                "row_without_start",
                                "缺少可解析的时间戳，无法判断是否在窗口内，已跳过",
                                f"{file.name}:{line_no}",
                            )
                            continue
                        if window_span(timestamp, 0.0, window, policy="start") is None:
                            rows_outside += 1
                            continue
                        for round_index, round_start, round_end in window.round_spans:
                            if round_start <= timestamp <= round_end:
                                round_counts[round_index] = (
                                    round_counts.get(round_index, 0) + 1
                                )
                                break
                    duration = self._accumulate(
                        row,
                        file_columns,
                        groups,
                        meta,
                        file,
                        line_no,
                        task_type_override=row_metrics.task_type if row_metrics else None,
                        duration_override=row_metrics.duration_us if row_metrics else None,
                    )
                    if timestamp is not None and duration is not None:
                        # 窗口内 busy 用相对坐标累计，避免 epoch 加法吞掉亚微秒时长
                        origin = window.start_us if window is not None else 0.0
                        relative = timestamp - origin
                        union.add(relative, relative + duration)
                meta.extra["files"].append({"path": str(file), "rows": rows_in_file})

        records = [
            group.to_record(self.mapping, workload.workload_id) for group in groups.values()
        ]
        tracked = [spec.name for spec in self.mapping.extra_metrics if spec.name in _INSTRUMENT_METRICS]
        if tracked:
            meta.extra["extra_metric_coverage"] = [
                {
                    "operator": group.operator,
                    "labels": dict(group.labels),
                    "metrics": {name: group.extra_coverage(name) for name in tracked},
                }
                for group in groups.values()
                if group.has_time
            ]
        meta.extra["groups"] = len(records)
        meta.extra["rows_outside_window"] = rows_outside
        if window is not None:
            meta.extra["window_quality"] = union.finalize(window.duration_us).to_dict()
            if window.round_spans:
                counts = {
                    index: round_counts.get(index, 0) for index, _, _ in window.round_spans
                }
                meta.extra["round_row_counts"] = counts
                missing = sorted(index for index, count in counts.items() if count == 0)
                # 非零但远低于同组中位数的轮次，同样说明数据被截断
                present = sorted(count for count in counts.values() if count > 0)
                median = present[len(present) // 2] if present else 0
                low = sorted(
                    index
                    for index, count in counts.items()
                    if 0 < count < median * 0.5
                )
                if low:
                    quality = meta.extra["window_quality"]
                    quality["degraded"] = True
                    quality["low_rounds"] = low
                    quality["reasons"] = list(quality.get("reasons") or ()) + [
                        f"round {low} 的行数不足同组中位数的一半（{median}），疑似数据被截断"
                    ]
                    meta.add_warning(
                        "low_rounds",
                        f"round {low} 行数异常偏低（中位数 {median}），"
                        "calls 与累计时间不可与他组直接比较",
                        str(files[0]),
                    )
                if missing:
                    # 某些轮在 op_summary 里没有任何行 = profiling 丢数据，
                    # calls / 累计时间都会偏低，必须显式标出来而不是静默通过
                    quality = meta.extra["window_quality"]
                    quality["degraded"] = True
                    quality["missing_rounds"] = missing
                    quality["reasons"] = list(quality.get("reasons") or ()) + [
                        f"round {missing} 在窗口内没有任何任务行（profiling 丢数据，"
                        "calls 与累计时间偏低）"
                    ]
                    meta.add_warning(
                        "missing_rounds",
                        f"round {missing} 无数据：窗口内只覆盖 "
                        f"{len(counts) - len(missing)}/{len(counts)} 轮，"
                        "calls 与累计时间不可与他组直接比较",
                        str(files[0]),
                    )
            if rows_outside == 0 and meta.row_count > 0:
                meta.add_warning(
                    "window_not_applied",
                    "未过滤掉任何行，窗口可能覆盖了整段采集",
                    str(files[0]),
                )
        return OperatorTable(workload=workload, records=records, meta=meta)

    def _report_column_issues(self, columns: _Columns, meta: ParseMeta, file: Path) -> None:
        unmapped = [
            name for name in columns.header if name and name not in _used_headers(columns)
        ]
        meta.unmapped_columns = tuple(unmapped)
        for name in unmapped:
            meta.add_warning("unmapped_column", f"列 {name!r} 未映射，已忽略", str(file))
        missing = tuple(name for name in self.mapping.fields if name not in columns.fields)
        meta.missing_columns = missing
        for name in missing:
            spec = self.mapping.fields[name]
            if spec.required:
                raise ParserError(
                    f"{file}: 必需列缺失 {name!r}（候选列名 {list(spec.names)}）；"
                    f"实际表头为 {columns.header}。请修正 mapping 后再解析。"
                )
            meta.add_warning("missing_column", f"可选列 {name!r} 不存在，对应指标视为未采集")

        # 33 列 / 46 列导出差异：extra_metrics 的列缺失时不能静默消失，
        # 必须在 meta 里留下可机读清单（R09）。缺失只影响该指标，不阻塞解析。
        normalized_header = {_norm(name) for name in columns.header}
        missing_extras = tuple(
            extra.name
            for extra in self.mapping.extra_metrics
            if not any(_norm(candidate) in normalized_header for candidate in extra.columns)
        )
        meta.extra["missing_extra_metrics"] = list(missing_extras)
        for name in missing_extras:
            meta.add_warning(
                "missing_extra_metric",
                f"可选指标 {name!r} 的列在该导出中不存在，视为未采集（不填 0）",
                str(file),
            )

    def _resolve_columns(self, header: list[str]) -> _Columns:
        index: dict[str, int] = {}
        for position, name in enumerate(header):
            index.setdefault(_norm(name), position)

        fields: dict[str, int] = {}
        for canonical, field_spec in self.mapping.fields.items():
            for candidate in field_spec.names:
                field_pos = index.get(_norm(candidate))
                if field_pos is not None:
                    fields[canonical] = field_pos
                    break
        labels: dict[str, int] = {}
        for label_name, label_spec in self.mapping.labels.items():
            for candidate in label_spec.names:
                label_pos = index.get(_norm(candidate))
                if label_pos is not None:
                    labels[label_name] = label_pos
                    break
        extras: dict[str, tuple[int, ...]] = {}
        for extra in self.mapping.extra_metrics:
            positions = tuple(
                index[_norm(candidate)] for candidate in extra.columns if _norm(candidate) in index
            )
            if positions:
                extras[extra.name] = positions
        flags: dict[str, int] = {}
        for flag_spec in self.mapping.row_flags:
            for candidate in flag_spec.columns:
                flag_pos = index.get(_norm(candidate))
                if flag_pos is not None:
                    flags[flag_spec.name] = flag_pos
                    break
        return _Columns(
            fields=fields, labels=labels, extras=extras, header=tuple(header), flags=flags
        )

    def _has_time_column(self, columns: _Columns) -> bool:
        return "total_time_us" in columns.fields or "avg_time_us" in columns.fields

    def _accumulate(
        self,
        row: list[str],
        columns: _Columns,
        groups: dict[tuple[str, ...], _Group],
        meta: ParseMeta,
        file: Path,
        line_no: int,
        *,
        task_type_override: TaskType | None = None,
        duration_override: float | None = None,
    ) -> float | None:
        """Fold one row into its group; returns the row duration in us if known."""
        operator_index = columns.fields.get("operator")
        operator_raw = (
            row[operator_index].strip()
            if operator_index is not None and operator_index < len(row)
            else ""
        )
        if not operator_raw:
            meta.skipped_rows += 1
            meta.add_warning("row_skipped", "operator 为空，跳过该行", f"{file.name}:{line_no}")
            return None

        task_type_index = columns.fields.get("task_type")
        task_type = task_type_override or self._resolve_task_type(
            row[task_type_index]
            if task_type_index is not None and task_type_index < len(row)
            else None
        )

        key = [self.mapping.name_aliases.get(operator_raw, operator_raw)]
        for label_name in self.mapping.aggregate.by:
            if label_name == "operator":
                continue
            position = columns.labels.get(label_name)
            key.append(row[position].strip() if position is not None and position < len(row) else "")
        group_key = tuple(key)
        group = groups.get(group_key)
        if group is None:
            group = _Group(operator=group_key[0], raw_operator=operator_raw, source=file.name)
            groups[group_key] = group
            if len(self.mapping.aggregate.by) > 1:
                group.labels = {
                    name: value
                    for name, value in zip(
                        self.mapping.aggregate.by[1:], group_key[1:], strict=True
                    )
                    if value
                }

        group.task_types[task_type] = group.task_types.get(task_type, 0) + 1

        duration = duration_override
        total_index = columns.fields.get("total_time_us")
        if duration is None and total_index is not None:
            value = _to_float(row[total_index] if total_index < len(row) else None)
            if value is not None:
                # 单位换算 + COMMUNICATION ns 缩放由 primitives 统一处理，
                # 与 events.py 的实例读取器共用同一入口。
                duration = normalize_duration(
                    _to_us(value, self.mapping.fields["total_time_us"].unit),
                    task_type=task_type,
                )

        if self.mapping.row_semantics == "instance":
            if duration is None:
                meta.skipped_rows += 1
                meta.add_warning(
                    "row_skipped", "实例行缺少可解析的时长，跳过", f"{file.name}:{line_no}"
                )
                return None
            group.add_instance(duration)
        else:
            calls_index = columns.fields.get("calls")
            calls = _to_int(row[calls_index] if calls_index is not None else None)
            if calls is None:
                meta.skipped_rows += 1
                meta.add_warning("row_skipped", "缺少可解析的 Count，跳过该行", f"{file.name}:{line_no}")
                return None
            if duration is None:
                avg_index = columns.fields.get("avg_time_us")
                avg = _to_float(row[avg_index] if avg_index is not None else None)
                if avg is None or calls <= 0:
                    meta.skipped_rows += 1
                    meta.add_warning("row_skipped", "缺少可用的时间列，跳过该行", f"{file.name}:{line_no}")
                    return None
                duration = _to_us(avg, self.mapping.fields["avg_time_us"].unit) * calls
                meta.add_warning(
                    "derived_total_time",
                    "缺少累计时间列，已由 avg_time * calls 推导",
                    f"{file.name}:{line_no}",
                )
            group.add_bucket(calls, duration)

        for field_name in ("min_time_us", "max_time_us", "device_time_us"):
            position = columns.fields.get(field_name)
            if position is None:
                continue
            value = _to_float(row[position] if position < len(row) else None)
            if value is not None:
                group.observe_extent(field_name, _to_us(value, self.mapping.fields[field_name].unit))

        # 逐实例字节估算只算一次；bytes 与配对时长（同批实例）共享该结果，
        # 保证带宽 = bytes/时长 的分子分母来自同一组可解析实例
        row_bytes = None
        if any(extra.computed == "bytes_from_shapes" for extra in self.mapping.extra_metrics):
            bytes_positions = columns.extras.get("bytes_moved")
            if bytes_positions:
                row_bytes = _estimate_row_bytes(row, bytes_positions)

        for extra in self.mapping.extra_metrics:
            positions = columns.extras.get(extra.name)
            if extra.when_task_types and task_type not in extra.when_task_types:
                continue
            if extra.name in _INSTRUMENT_METRICS:
                group.extra_eligible[extra.name] = group.extra_eligible.get(extra.name, 0) + 1
                if positions:
                    group.extra_columns[extra.name] = group.extra_columns.get(extra.name, 0) + 1
            if not positions:
                continue
            if extra.computed == "bytes_from_shapes":
                if row_bytes is not None:
                    group.observe_extra(extra, row_bytes)
                continue
            if extra.computed == "bytes_time_for":
                if row_bytes is not None and duration is not None:
                    group.observe_extra(extra, duration)
                continue
            value = None
            for position in positions:
                if position >= len(row):
                    continue
                part = _to_float(row[position])
                if part is None:
                    continue
                value = part if value is None else value + part
            if value is not None:
                group.observe_extra(extra, value * extra.scale)

        for spec in self.mapping.row_flags:
            position = columns.flags.get(spec.name)
            if position is None or position >= len(row):
                continue
            raw_value = (row[position] or "").strip()
            if not raw_value or raw_value in spec.exclude_values:
                continue
            group.observe_flag(spec.name, duration)

        if "shape" in columns.fields and columns.fields["shape"] < len(row):
            raw_shape = (row[columns.fields["shape"]] or "").strip()
            if raw_shape and raw_shape.lower() not in _NULL_TOKENS:
                group.shapes.add(raw_shape)
                first = _first_dim(raw_shape)
                if first is not None:
                    group.first_dim_min = (
                        first if group.first_dim_min is None else min(group.first_dim_min, first)
                    )
                    group.first_dim_max = (
                        first if group.first_dim_max is None else max(group.first_dim_max, first)
                    )
        if "dtype" in columns.fields and columns.fields["dtype"] < len(row):
            raw_dtype = (row[columns.fields["dtype"]] or "").strip()
            if raw_dtype and raw_dtype.lower() not in _NULL_TOKENS:
                group.dtypes.add(raw_dtype)
        if "kernel" in columns.fields and columns.fields["kernel"] < len(row):
            kernel = (row[columns.fields["kernel"]] or "").strip()
            if kernel and kernel.lower() not in _NULL_TOKENS:
                group.kernels.add(kernel)
        return duration

    def _resolve_task_type(self, raw: str | None) -> TaskType:
        default = self.mapping.defaults.get("task_type", TaskType.KERNEL.value)
        if not raw or raw.strip().lower() in _NULL_TOKENS:
            raw = default
        mapped = self.mapping.task_type_map.get(_norm(str(raw)))
        candidate = mapped if mapped is not None else str(raw).strip().lower()
        try:
            return TaskType(candidate)
        except ValueError:
            return TaskType.OTHER


@dataclass
class _Group:
    """Accumulator for one aggregation key (see csv_mapping docstring)."""

    operator: str
    raw_operator: str
    source: str
    calls: int = 0
    total_us: float = 0.0
    has_time: bool = False
    min_us: float | None = None
    max_us: float | None = None
    device_us: float | None = None
    extra_sum: dict[str, float] = field(default_factory=dict)
    extra_count: dict[str, int] = field(default_factory=dict)
    extra_eligible: dict[str, int] = field(default_factory=dict)
    extra_columns: dict[str, int] = field(default_factory=dict)
    extra_max: dict[str, float] = field(default_factory=dict)
    extra_min: dict[str, float] = field(default_factory=dict)
    flag_tasks: dict[str, int] = field(default_factory=dict)
    flag_time: dict[str, float] = field(default_factory=dict)
    shapes: set[str] = field(default_factory=set)
    first_dim_min: int | None = None
    first_dim_max: int | None = None
    dtypes: set[str] = field(default_factory=set)
    kernels: set[str] = field(default_factory=set)
    task_types: dict[TaskType, int] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def add_instance(self, duration_us: float) -> None:
        self.calls += 1
        self.total_us += duration_us
        self.has_time = True
        self.min_us = duration_us if self.min_us is None else min(self.min_us, duration_us)
        self.max_us = duration_us if self.max_us is None else max(self.max_us, duration_us)

    def add_bucket(self, calls: int, total_us: float) -> None:
        self.calls += calls
        self.total_us += total_us
        self.has_time = True

    def observe_extent(self, field_name: str, value_us: float) -> None:
        if field_name == "min_time_us":
            self.min_us = value_us if self.min_us is None else min(self.min_us, value_us)
        elif field_name == "max_time_us":
            self.max_us = value_us if self.max_us is None else max(self.max_us, value_us)
        elif field_name == "device_time_us":
            self.device_us = value_us if self.device_us is None else self.device_us + value_us

    def observe_flag(self, name: str, duration_us: float | None) -> None:
        self.flag_tasks[name] = self.flag_tasks.get(name, 0) + 1
        if duration_us is not None:
            self.flag_time[name] = self.flag_time.get(name, 0.0) + duration_us

    def observe_extra(self, spec: ExtraMetricSpec, value: float) -> None:
        self.extra_sum[spec.name] = self.extra_sum.get(spec.name, 0.0) + value
        self.extra_count[spec.name] = self.extra_count.get(spec.name, 0) + 1
        self.extra_max[spec.name] = max(self.extra_max.get(spec.name, value), value)
        self.extra_min[spec.name] = min(self.extra_min.get(spec.name, value), value)

    def extra_coverage(self, name: str) -> dict:
        eligible = self.extra_eligible.get(name, 0)
        column_rows = self.extra_columns.get(name, 0)
        samples = self.extra_count.get(name, 0)
        if eligible == 0:
            status = "not_applicable"
        elif column_rows == 0:
            status = "missing_column"
        elif samples == 0:
            status = "empty"
        elif samples < eligible:
            status = "partial"
        else:
            status = "complete"
        return {"status": status, "eligible_rows": eligible, "column_rows": column_rows, "samples": samples}

    def to_record(self, mapping: CsvMapping, workload_id: str) -> OperatorRecord:
        extras: dict[str, Metric] = {}
        for spec in mapping.extra_metrics:
            count = self.extra_count.get(spec.name, 0)
            if count == 0:
                continue
            if spec.agg == "sum":
                value = self.extra_sum[spec.name]
            elif spec.agg == "mean":
                value = self.extra_sum[spec.name] / count
            elif spec.agg == "max":
                value = self.extra_max[spec.name]
            else:
                value = self.extra_min[spec.name]
            extras[spec.name] = Metric(
                value,
                spec.unit,
                Provenance.MEASURED,
                f"{self.source}:{spec.name} ({spec.agg} over {count} rows)",
            )

        total = (
            Metric(self.total_us, "us", Provenance.MEASURED, f"{self.source}:sum")
            if self.has_time
            else None
        )
        avg = (
            Metric(self.total_us / self.calls, "us", Provenance.DERIVED, "total / calls")
            if self.has_time and self.calls > 0
            else None
        )
        task_type = _dominant_task_type(self.task_types)

        attributes = {"raw_operator": self.raw_operator}
        attributes.update(self.labels)
        if mapping.row_semantics == "instance":
            partial = [
                name for name in ("flops", "aicore_time_us", "bytes_moved")
                if 0 < self.extra_count.get(name, 0) < self.calls
            ]
            if partial:
                attributes["partial_metric_inputs"] = ",".join(partial)
        for flag_spec in mapping.row_flags:
            count = self.flag_tasks.get(flag_spec.name, 0)
            extras[f"{flag_spec.name}_tasks"] = Metric(
                float(count), "count", Provenance.MEASURED, f"{self.source}:{flag_spec.name} rows"
            )
            extras[f"{flag_spec.name}_time_us"] = Metric(
                self.flag_time.get(flag_spec.name, 0.0),
                "us",
                Provenance.MEASURED,
                f"{self.source}:{flag_spec.name} duration sum",
            )
            if self.calls:
                attributes[f"{flag_spec.name}_ratio"] = str(round(count / self.calls, 4))


        if len(self.task_types) > 1:
            attributes["task_types"] = ",".join(sorted(t.value for t in self.task_types))

        kernel = None
        if len(self.kernels) == 1:
            kernel = next(iter(self.kernels))
        elif self.kernels:
            attributes["kernel_count"] = str(len(self.kernels))

        notes: list[str] = []
        if mapping.row_semantics == "instance":
            notes.append(f"aggregated from {self.calls} instances")
        if len(self.shapes) > 1:
            attributes["shape_count"] = str(len(self.shapes))
            notes.append(f"{len(self.shapes)} distinct shapes aggregated; shape left null")
        if len(self.dtypes) > 1:
            attributes["dtype_count"] = str(len(self.dtypes))
        if self.first_dim_max is not None:
            # 输入形状第一组的首维（仅作指纹；不同算子的语义不同，不能直接当 batch）
            attributes["shape_dim0_min"] = str(self.first_dim_min)
            attributes["shape_dim0_max"] = str(self.first_dim_max)

        shape = None
        if len(self.shapes) == 1:
            attributes["raw_shape"] = next(iter(self.shapes))
            shape = _parse_shape(next(iter(self.shapes)))
        dtype = next(iter(self.dtypes)) if len(self.dtypes) == 1 else None

        return OperatorRecord(
            workload_id=workload_id,
            operator=self.operator,
            calls=self.calls,
            total_time_us=total,
            avg_time_us=avg,
            min_time_us=Metric(self.min_us, "us", Provenance.MEASURED, f"{self.source}:min")
            if self.min_us is not None
            else None,
            max_time_us=Metric(self.max_us, "us", Provenance.MEASURED, f"{self.source}:max")
            if self.max_us is not None
            else None,
            device_time_us=Metric(self.device_us, "us", Provenance.MEASURED, f"{self.source}:device")
            if self.device_us is not None
            else None,
            kernel=kernel,
            shape=shape,
            dtype=dtype,
            task_type=task_type,
            extra=extras,
            attributes=attributes,
            notes=tuple(notes),
        )


def _busy_union():
    from ..primitives import IntervalUnion

    return IntervalUnion()


def _row_start(columns: _Columns, row: list[str], mapping: CsvMapping) -> float | None:
    position = columns.fields.get("row_start_time")
    if position is None or position >= len(row):
        return None
    value = _to_float(row[position])
    if value is None:
        return None
    return _to_us(value, mapping.fields["row_start_time"].unit)


def _used_headers(columns: _Columns) -> set[str]:
    used = {columns.header[i] for i in list(columns.fields.values()) + list(columns.labels.values())}
    for positions in columns.extras.values():
        used.update(columns.header[i] for i in positions)
    return used


def _dominant_task_type(counts: dict[TaskType, int]) -> TaskType:
    if not counts:
        return TaskType.UNKNOWN
    return max(counts.items(), key=lambda item: (item[1], item[0].value))[0]


def _parse_column_spec(raw: Any, key: str, path: Path) -> ColumnSpec:
    if isinstance(raw, str):
        raw = {"names": [raw]}
    if not isinstance(raw, Mapping) or not raw.get("names"):
        raise ParserError(f"{path}: column {key!r} needs 'names'")
    default_cast = "int" if key == "calls" else "float" if key.endswith("_us") else "str"
    return ColumnSpec(
        names=tuple(str(n) for n in raw["names"]),
        unit=raw.get("unit"),
        cast=str(raw.get("cast", default_cast)),
        required=bool(raw.get("required", False)),
    )


def _parse_row_flag(raw: Any, path: Path) -> RowFlagSpec:
    if not isinstance(raw, Mapping) or not raw.get("columns"):
        raise ParserError(f"{path}: each row_flags entry needs 'columns'")
    columns = raw["columns"]
    if isinstance(columns, str):
        columns = [columns]
    return RowFlagSpec(
        name=str(raw.get("name") or columns[0]),
        columns=tuple(str(item) for item in columns),
        exclude_values=tuple(str(item) for item in (raw.get("exclude_values") or ())),
    )


def _parse_extra(raw: Any, path: Path) -> ExtraMetricSpec:
    if not isinstance(raw, Mapping) or not raw.get("columns"):
        raise ParserError(f"{path}: each extra_metrics entry needs 'columns'")
    if not raw.get("unit"):
        raise ParserError(f"{path}: extra metric {raw.get('name')!r} needs 'unit'")
    agg = str(raw.get("agg", "sum")).strip().lower()
    if agg not in AGG_FUNCTIONS:
        raise ParserError(f"{path}: extra metric agg must be one of {sorted(AGG_FUNCTIONS)}")
    combine = str(raw.get("combine", "first")).strip().lower()
    if combine not in COMBINE_MODES:
        raise ParserError(f"{path}: extra metric combine must be one of {sorted(COMBINE_MODES)}")
    when = raw.get("when") or {}
    when_types: tuple[TaskType, ...] = ()
    if when:
        if not isinstance(when, Mapping):
            raise ParserError(f"{path}: extra metric 'when' must be a mapping")
        values = when.get("task_type") or ()
        if isinstance(values, str):
            values = [values]
        resolved = []
        for value in values:
            try:
                resolved.append(TaskType(str(value).strip().lower()))
            except ValueError as exc:
                raise ParserError(f"{path}: unknown task_type {value!r} in 'when'") from exc
        when_types = tuple(resolved)
    scale_raw = raw.get("scale", 1.0)
    try:
        scale = float(scale_raw)
    except (TypeError, ValueError) as exc:
        raise ParserError(f"{path}: extra metric scale must be numeric, got {scale_raw!r}") from exc
    if not math.isfinite(scale) or scale <= 0:
        raise ParserError(f"{path}: extra metric scale must be finite and > 0, got {scale_raw!r}")

    computed = str(raw.get("computed", "")).strip().lower()
    if computed and computed not in _COMPUTED_METRICS:
        raise ParserError(
            f"{path}: extra metric {raw.get('name')!r} computed must be one of {sorted(_COMPUTED_METRICS)}"
        )
    return ExtraMetricSpec(
        name=str(raw.get("name") or raw["columns"][0]),
        columns=tuple(str(c) for c in raw["columns"]),
        unit=str(raw["unit"]),
        agg=agg,
        combine=combine,
        when_task_types=when_types,
        scale=scale,
        computed=computed,
    )


def _norm(value: str) -> str:
    return " ".join(str(value).replace("\u00a0", " ").split()).lower()


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text.lower() in _NULL_TOKENS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(raw: str | None) -> int | None:
    value = _to_float(raw)
    if value is None:
        return None
    return int(round(value))


def _to_us(value: float, unit: str | None) -> float:
    from ..schemas.units import to_us

    return to_us(value, unit or "us")


def _first_dim(raw: str) -> int | None:
    """输入形状第一组的首维；仅作指纹使用。"""
    text = str(raw).strip().strip('"').strip()
    for separator in (";", "\u00d7"):
        index = text.find(separator)
        if index > 0:
            text = text[:index]
            break
    token = text.strip().strip("[](){} ").split(",")[0].strip()
    try:
        return int(float(token))
    except ValueError:
        return None


def _parse_shape(raw: str) -> tuple[int, ...] | None:
    text = str(raw).strip().strip("[](){} ")
    if not text:
        return None
    for separator in (";", "x", "*", "\u00d7"):
        text = text.replace(separator, ",")
    parts = [part.strip().strip("[](){} ") for part in text.split(",")]
    dims: list[int] = []
    for part in parts:
        if not part:
            continue
        try:
            dims.append(int(float(part)))
        except ValueError:
            return None
    return tuple(dims) if dims else None
