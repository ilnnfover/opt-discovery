"""Fact extraction: from one operator record to a flat, unit-normalized fact set.

Rules only ever see facts, never raw records.  Every fact keeps its provenance
and its derivation, so a rule firing on ``aicore_time_fraction`` can be traced
back to the column it came from.

Naming discipline matters here:

* ``*_time_fraction`` = pipe time / task duration (measured ratio, *not* a
  utilization against device peak);
* ``*_utilization``   = only present when a calibrated source provides it;
* ``*_over_device``   = ratio between two measured durations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..schemas.metrics import Metric, Provenance, weakest_provenance
from ..schemas.operator import OperatorRecord, TaskType
from ..schemas.units import UnitError

__all__ = ["Fact", "FactSet", "FactSpec", "FACT_CATALOG", "build_facts", "FACT_GROUPS"]


@dataclass(frozen=True)
class FactSpec:
    name: str
    unit: str
    description: str
    kind: str  # "measured" | "derived" | "calibrated"
    requires: tuple[str, ...] = ()


FACT_CATALOG: dict[str, FactSpec] = {
    # -- volume ---------------------------------------------------------
    "calls": FactSpec("calls", "count", "算子调用次数", "measured"),
    "total_time_us": FactSpec("total_time_us", "us", "算子累计执行时间", "measured"),
    "avg_time_us": FactSpec("avg_time_us", "us", "单次平均执行时间", "measured"),
    "min_time_us": FactSpec("min_time_us", "us", "单次最小执行时间", "measured"),
    "max_time_us": FactSpec("max_time_us", "us", "单次最大执行时间", "measured"),
    "device_time_us": FactSpec("device_time_us", "us", "Device 侧执行时间", "measured"),
    "avg_over_min": FactSpec("avg_over_min", "ratio", "平均/最小耗时，衡量抖动", "derived",
                             ("avg_time_us", "min_time_us")),
    "time_share": FactSpec(
        "time_share", "ratio",
        "算子累计时间 / 当前 Workload 所有算子累计时间之和（并行执行时会高估端到端占比）",
        "derived", ("total_time_us", "workload_total_time_us"),
    ),
    "end_to_end_share": FactSpec(
        "end_to_end_share", "ratio", "算子累计时间 / 端到端延迟", "derived",
        ("total_time_us", "end_to_end_us"),
    ),
    "device_over_total": FactSpec("device_over_total", "ratio", "Device 时间/累计时间", "derived",
                                  ("device_time_us", "total_time_us")),
    # -- host / launch ---------------------------------------------------
    "cpu_submit_time_us": FactSpec("cpu_submit_time_us", "us", "Host 侧提交时间", "measured"),
    "host_gap_us": FactSpec("host_gap_us", "us", "提交到 Device 启动之间的间隙", "measured"),
    "cpu_submit_over_device": FactSpec(
        "cpu_submit_over_device", "ratio", "Host 提交时间 / Device 执行时间", "derived",
        ("cpu_submit_time_us", "device_time_us"),
    ),
    "host_gap_over_device": FactSpec(
        "host_gap_over_device", "ratio", "Host 间隙 / Device 执行时间", "derived",
        ("host_gap_us", "device_time_us"),
    ),
    # -- sync / communication -------------------------------------------
    "sync_wait_us": FactSpec("sync_wait_us", "us", "同步等待时间", "measured"),
    "wait_time_us": FactSpec("wait_time_us", "us", "通用等待时间", "measured"),
    "task_wait_time_us": FactSpec(
        "task_wait_time_us",
        "us",
        "Task Wait Time 逐实例求和（排队/等待时长；跨流会重叠，可能大于总时长，不能单独作为瓶颈结论）",
        "measured",
    ),
    "communication_time_us": FactSpec("communication_time_us", "us", "通信时间", "measured"),
    "sync_wait_over_device": FactSpec(
        "sync_wait_over_device", "ratio", "同步等待 / Device 执行时间", "derived",
        ("sync_wait_us", "device_time_us"),
    ),
    "wait_over_device": FactSpec(
        "wait_over_device", "ratio", "等待时间 / Device 执行时间", "derived",
        ("wait_time_us", "device_time_us"),
    ),
    "task_wait_over_device": FactSpec(
        "task_wait_over_device", "ratio",
        "Task Wait 求和 / Device 执行时间（>1 表示等待跨流重叠，属正常现象）", "derived",
        ("task_wait_time_us", "device_time_us"),
    ),
    "communication_over_device": FactSpec(
        "communication_over_device", "ratio", "通信时间 / Device 执行时间", "derived",
        ("communication_time_us", "device_time_us"),
    ),
    # -- hardware (calibrated) ------------------------------------------
    "aicore_utilization": FactSpec(
        "aicore_utilization",
        "ratio",
        "Cube 实测 FLOPS / 设备 bf16 峰值（flops / aicore_time / peak，需设备档案）",
        "calibrated",
    ),
    "memory_bandwidth_utilization": FactSpec(
        "memory_bandwidth_utilization",
        "ratio",
        "估算搬运字节 / 任务时长 / peak_hbm_gbps（shape×dtype 估算，读写合计口径，需设备档案）",
        "calibrated",
    ),
    "active_core_ratio": FactSpec(
        "active_core_ratio",
        "ratio",
        "活跃核比例 = Block Num / 设备核数（需设备档案提供核数）",
        "derived",
        ("block_num",),
    ),
    "cube_utilization": FactSpec(
        "cube_utilization", "ratio",
        "Profiler 报告的 cube 利用率（%）归一化；口径需确认是相对峰值还是相对任务时长",
        "measured",
    ),
    "arithmetic_intensity": FactSpec(
        "arithmetic_intensity", "flop/byte", "算术强度", "derived", ("flops", "bytes_moved")
    ),
    "bytes_moved": FactSpec("bytes_moved", "bytes", "搬运字节数", "measured"),
    "flops": FactSpec("flops", "count", "浮点运算次数", "measured"),
    "block_num": FactSpec(
        "block_num", "count", "每实例平均使用的 AI Core 块数（Block Num）", "measured"
    ),
    # -- combined pipes (single fact instead of binding a rule to one column) --
    "compute_pipe_time_fraction": FactSpec(
        "compute_pipe_time_fraction",
        "ratio",
        "计算类流水（AI Core / Vector 取较大者）/ 算子任务时长",
        "derived",
        ("aicore_time_fraction", "vec_time_fraction"),
    ),
    "memory_pipe_time_fraction": FactSpec(
        "memory_pipe_time_fraction",
        "ratio",
        "搬运类流水（MTE1/MTE2/MTE3 取较大者）/ 算子任务时长",
        "derived",
        ("mte1_time_fraction", "mte2_time_fraction", "mte3_time_fraction"),
    ),
    # -- structure -------------------------------------------------------
    "is_kernel": FactSpec("is_kernel", "bool", "是否为 Kernel 任务", "measured"),
    "has_shape": FactSpec("has_shape", "bool", "是否解析出算子 Shape", "measured"),
}

#: facts represented as ``<pipe>_time_fraction``; keys are the extra metric names
_PIPE_TIME_METRICS: tuple[str, ...] = (
    "aicore_time_us",
    "mac_time_us",
    "vec_time_us",
    "mte1_time_us",
    "mte2_time_us",
    "mte3_time_us",
    "fixpipe_time_us",
    "scalar_time_us",
)

for _pipe in _PIPE_TIME_METRICS:
    _stem = _pipe[: -len("_time_us")]
    FACT_CATALOG[f"{_pipe}"] = FactSpec(_pipe, "us", f"{_stem} 流水时间", "measured")
    FACT_CATALOG[f"{_stem}_time_fraction"] = FactSpec(
        f"{_stem}_time_fraction",
        "ratio",
        f"{_stem} 流水时间 / 算子任务时长（占自身时长的比例，不是设备利用率）",
        "derived",
        (_pipe, "total_time_us"),
    )

FACT_GROUPS: dict[str, tuple[str, ...]] = {
    "volume": ("calls", "total_time_us", "avg_time_us", "time_share"),
    "host": ("cpu_submit_time_us", "cpu_submit_over_device", "host_gap_over_device"),
    "sync": ("sync_wait_us", "sync_wait_over_device", "wait_over_device"),
    "communication": ("communication_time_us", "communication_over_device"),
    "calibrated": ("aicore_utilization", "memory_bandwidth_utilization", "active_core_ratio"),
    "structure": ("is_kernel", "has_shape"),
}

_RATIO_METRICS = frozenset(
    {
        "aicore_utilization",
        "memory_bandwidth_utilization",
        "active_core_ratio",
        "vector_utilization",
        "cube_utilization",
    }
)
_BYTE_METRICS = frozenset({"bytes_moved", "bytes_read", "bytes_write"})
_TIME_METRICS = frozenset(
    {
        "cpu_submit_time_us",
        "host_gap_us",
        "sync_wait_us",
        "wait_time_us",
        "communication_time_us",
    }
) | frozenset(_PIPE_TIME_METRICS)

_CORE_TIME_FIELDS = (
    "total_time_us",
    "avg_time_us",
    "min_time_us",
    "max_time_us",
    "device_time_us",
)

_DERIVED_RATIOS: tuple[tuple[str, str, str], ...] = (
    ("device_over_total", "device_time_us", "total_time_us"),
    ("cpu_submit_over_device", "cpu_submit_time_us", "device_time_us"),
    ("host_gap_over_device", "host_gap_us", "device_time_us"),
    ("sync_wait_over_device", "sync_wait_us", "device_time_us"),
    ("wait_over_device", "wait_time_us", "device_time_us"),
    ("task_wait_over_device", "task_wait_time_us", "device_time_us"),
    ("communication_over_device", "communication_time_us", "device_time_us"),
)


@dataclass(frozen=True)
class Fact:
    name: str
    value: float
    unit: str
    provenance: Provenance
    source: str | None = None

    @property
    def weight(self) -> float:
        return Metric(self.value, self.unit, self.provenance, self.source).weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "provenance": self.provenance.value,
            "source": self.source,
        }


@dataclass
class FactSet:
    workload_id: str
    operator: str
    facts: dict[str, Fact] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    def get(self, name: str) -> Fact | None:
        return self.facts.get(name)

    def has(self, name: str) -> bool:
        return name in self.facts

    def missing(self, names: Iterable[str]) -> list[str]:
        return [n for n in names if n not in self.facts]

    def value(self, name: str) -> float | None:
        fact = self.facts.get(name)
        return fact.value if fact else None

    def describe(self, name: str) -> str:
        fact = self.facts.get(name)
        if fact is None:
            return f"{name}=<未采集>"
        if fact.unit == "ratio":
            return f"{name}={fact.value:.3f}"
        return f"{name}={fact.value:g}{(' ' + fact.unit) if fact.unit not in ('bool', 'count') else ''}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload_id": self.workload_id,
            "operator": self.operator,
            "facts": {k: v.to_dict() for k, v in sorted(self.facts.items())},
            "issues": list(self.issues),
        }


def _put_max_fact(put, facts: dict[str, Fact], name: str, members: tuple[str, ...], unit: str) -> None:
    """Create ``name`` as the maximum of the available ``members``.

    Provenance is the weakest among the inputs, because the maximum is only as
    trustworthy as the least trustworthy column that could have produced it.
    """
    present = [facts[m] for m in members if m in facts]
    if not present:
        return
    best = max(present, key=lambda fact: fact.value)
    put(
        name,
        best.value,
        unit,
        weakest_provenance([fact.provenance for fact in present]),
        f"max({', '.join(members)}) -> {best.name}",
    )


def build_facts(
    record: OperatorRecord,
    *,
    workload_total_time_us: float | None = None,
    end_to_end_us: float | None = None,
    device_profile: Mapping[str, Any] | None = None,
) -> FactSet:
    """Build the fact set of a single operator record.

    ``workload_total_time_us`` and ``end_to_end_us`` are optional context; when
    absent the corresponding share facts are simply not produced.

    ``device_profile``（来自 micro-benchmark 设备档案）启用两个校准事实：

    * ``aicore_utilization`` = flops / aicore_time / peak_bf16 —— 只有
      aicore_time 是 Cube 实际活跃时间时才是真利用率；实测发现小算子的
      Task Duration 可能小于 aicore_time（逐实例下发口径差异），
      所以分母必须用流水时间；
    * ``active_core_ratio`` = block_num / ai_core_count。
    """
    result = FactSet(workload_id=record.workload_id, operator=record.operator)
    facts: dict[str, Fact] = result.facts

    def put(name: str, value: float, unit: str, prov: Provenance, source: str | None) -> None:
        facts[name] = Fact(name=name, value=float(value), unit=unit, provenance=prov, source=source)

    def put_metric(name: str, metric: Metric) -> None:
        put(name, metric.value, metric.unit, metric.provenance, metric.source)

    put("calls", record.calls, "count", Provenance.MEASURED, "record.calls")

    for field_name in _CORE_TIME_FIELDS:
        metric: Metric | None = getattr(record, field_name)
        if metric is not None:
            put_metric(field_name, metric.as_time_us())

    average = record.effective_avg_time_us
    if average is not None:
        put_metric("avg_time_us", average.as_time_us())

    # -- extra metrics, unit-normalized where the contract says so --------
    for name, metric in record.extra.items():
        try:
            if name in _RATIO_METRICS:
                put_metric(name, metric.as_ratio())
            elif name in _BYTE_METRICS:
                put_metric(name, metric.as_bytes())
            elif name in _TIME_METRICS or name.endswith("_time_us"):
                put_metric(name, metric.as_time_us())
            else:
                put_metric(name, metric)
        except UnitError as exc:
            result.issues.append(f"extra metric {name!r} ignored: {exc}")

    # -- derived ratios --------------------------------------------------
    for fact_name, numerator, denominator in _DERIVED_RATIOS:
        num, den = facts.get(numerator), facts.get(denominator)
        if num and den and den.value > 0:
            put(
                fact_name,
                num.value / den.value,
                "ratio",
                weakest_provenance([num.provenance, den.provenance]),
                f"{numerator} / {denominator}",
            )

    average_fact, min_fact = facts.get("avg_time_us"), facts.get("min_time_us")
    if average_fact and min_fact and min_fact.value > 0:
        put(
            "avg_over_min",
            average_fact.value / min_fact.value,
            "ratio",
            weakest_provenance([average_fact.provenance, min_fact.provenance]),
            "avg_time_us / min_time_us",
        )

    total_fact = facts.get("total_time_us")
    if total_fact and workload_total_time_us and workload_total_time_us > 0:
        put(
            "time_share",
            total_fact.value / workload_total_time_us,
            "ratio",
            Provenance.DERIVED,
            "total_time_us / sum(total_time_us)",
        )
    if total_fact and end_to_end_us and end_to_end_us > 0:
        put(
            "end_to_end_share",
            total_fact.value / end_to_end_us,
            "ratio",
            Provenance.DERIVED,
            "total_time_us / end_to_end_us",
        )

    # -- pipe busy fractions (share of the task's own duration) ----------
    denominator_fact = facts.get("total_time_us") or facts.get("device_time_us")
    if denominator_fact and denominator_fact.value > 0:
        # 只对「流水时间」求占比；等待/提交/通信等时长不是流水，不能算 fraction
        for name in _PIPE_TIME_METRICS:
            pipe = facts.get(name)
            if pipe is None:
                continue
            put(
                f"{name[: -len('_time_us')]}_time_fraction",
                pipe.value / denominator_fact.value,
                "ratio",
                weakest_provenance([pipe.provenance, denominator_fact.provenance]),
                f"{name} / {denominator_fact.name}",
            )

    # -- combined pipe facts ---------------------------------------------
    # 单个算子可能同时跑在 AI Core 与 Vector 上，规则不应绑定具体某一条流水名。
    _put_max_fact(
        put,
        facts,
        "compute_pipe_time_fraction",
        ("aicore_time_fraction", "vec_time_fraction"),
        "ratio",
    )
    _put_max_fact(
        put,
        facts,
        "memory_pipe_time_fraction",
        ("mte1_time_fraction", "mte2_time_fraction", "mte3_time_fraction"),
        "ratio",
    )

    # -- arithmetic intensity, if both inputs exist ----------------------
    flops, bytes_moved = facts.get("flops"), facts.get("bytes_moved")
    if flops and bytes_moved and bytes_moved.value > 0:
        put(
            "arithmetic_intensity",
            flops.value / bytes_moved.value,
            "flop/byte",
            weakest_provenance([flops.provenance, bytes_moved.provenance]),
            "flops / bytes_moved",
        )

    # -- calibrated against the device profile ---------------------------
    if device_profile:
        peak_tflops = device_profile.get("peak_bf16_tflops")
        flops_fact, aicore_fact = facts.get("flops"), facts.get("aicore_time_us")
        if (
            peak_tflops
            and flops_fact
            and aicore_fact
            and aicore_fact.value > 0
            and flops_fact.value > 0
        ):
            achieved = flops_fact.value / (aicore_fact.value * 1e-6)
            put(
                "aicore_utilization",
                achieved / (float(peak_tflops) * 1e12),
                "ratio",
                Provenance.MEASURED,
                "flops / aicore_time / peak_bf16_tflops（设备档案实测峰值）",
            )
        core_count = device_profile.get("ai_core_count")
        block_fact = facts.get("block_num")
        if core_count and block_fact and block_fact.value > 0:
            put(
                "active_core_ratio",
                block_fact.value / float(core_count),
                "ratio",
                Provenance.MEASURED,
                f"block_num / ai_core_count（{int(core_count)} 核，设备档案实测）",
            )
        peak_gbps = device_profile.get("peak_hbm_gbps")
        bytes_fact = facts.get("bytes_moved")
        if (
            peak_gbps
            and bytes_fact
            and bytes_fact.value > 0
            and record.task_type is not TaskType.COMMUNICATION
        ):
            # 分母优先用与 bytes 同一批实例的配对时长；缺失时回退总时长（口径略宽）
            duration_fact = facts.get("bytes_moved_time_us") or facts.get("total_time_us")
            if duration_fact and duration_fact.value > 0:
                put(
                    "memory_bandwidth_utilization",
                    bytes_fact.value / (duration_fact.value * 1e-6) / (float(peak_gbps) * 1e9),
                    "ratio",
                    Provenance.MEASURED,
                    "bytes_moved(shape 估算，读写合计) / 配对时长 / peak_hbm_gbps",
                )

    # -- structural ------------------------------------------------------
    put(
        "is_kernel",
        1.0 if record.task_type is TaskType.KERNEL else 0.0,
        "bool",
        Provenance.MEASURED,
        "record.task_type",
    )
    put("has_shape", 1.0 if record.shape else 0.0, "bool", Provenance.MEASURED, "record.shape")

    return result
