"""从已有 Profiling 导出推导阈值（P2）。

原则：**能测的才标定，测不了的显式标成 needs_device_baseline**，不留“看起来像测过”的值。

可直接从四组导出得到的：

* compute_pipe_time_fraction_saturated ← 计算类算子计算流水占比 p75
* mac_pipe_time_fraction_high          ← 计算类算子 mac 占比 p50
* memory_pipe_time_fraction_saturated  ← 搬运类算子搬运占比 p75
* compute_pipe_time_fraction_ceiling   ← 搬运类算子计算占比 p75
* memory_pipe_time_fraction_ceiling    ← 计算类算子搬运占比 p75
* communication_share_threshold        ← 各 Workload 通信占比中位数

需设备基线或 Timeline 的：利用率、算术强度、host/sync 比值、活跃核比例。
small_kernel_time_us / high_call_count 属策略值，不参与标定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..diagnosis.rules import RuleSet, Threshold
from ..schemas.metrics import Provenance
from ..schemas.operator import OperatorTable, TaskType

__all__ = [
    "COMPUTE_OPS", "MEMORY_OPS",
    "ThresholdProposal", "CalibrationReport", "calibrate",
]

COMPUTE_OPS = frozenset(
    {
        "MatMulV2", "MatMulV3", "MatMul", "BatchMatMulV2", "BatchMatMul",
        "GroupedMatmul", "FusedInferAttentionScore", "Conv2D",
    }
)
MEMORY_OPS = frozenset(
    {
        "Cast", "Slice", "Transpose", "BroadcastTo", "ZerosLike", "Fill",
        "ConcatD", "TensorMove", "IndexPutV2", "GatherV2", "PadV3", "MemSet",
    }
)

NEEDS_BASELINE = {
    "aicore_utilization_saturated": "需要设备档案（peak_bf16_tflops）+ op_summary 的 flops/aicore_time 列",
    "aicore_utilization_ceiling": "同上",
    "bandwidth_utilization_saturated": "需要设备档案（peak_hbm_gbps）+ shape×dtype 字节估算（见 bytes_moved 指标）",
    "arithmetic_intensity_low": "需要设备档案的 roofline_flop_per_byte",

    "sync_wait_ratio": "需 Event/Barrier 区间；当前导出没有，需 mstx 或其他采集",
    "low_core_ratio": "需要设备档案（ai_core_count）+ op_summary 的 Block Num 列",
}
POLICY = {"small_kernel_time_us", "high_call_count", "communication_share_threshold"}

STATUS_CALIBRATED = "calibrated"
STATUS_INSUFFICIENT = "insufficient_samples"
STATUS_NEEDS_BASELINE = "needs_device_baseline"
STATUS_POLICY = "policy"

#: 分位数标定的最小样本数。p75/p90 在 n<12 时基本是"某一次观测"，不能当设备常数。
DEFAULT_MIN_SAMPLES = 12


@dataclass
class ThresholdProposal:
    name: str
    current_value: float
    current_provenance: str
    proposed_value: float | None
    status: str
    method: str
    samples: int = 0
    stats: Mapping[str, float] = field(default_factory=dict)
    note: str = ""
    raw_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current_value": self.current_value,
            "current_provenance": self.current_provenance,
            "proposed_value": self.proposed_value,
            "status": self.status,
            "method": self.method,
            "samples": self.samples,
            "stats": {key: round(value, 4) for key, value in self.stats.items()},
            "note": self.note,
            "raw_value": None if self.raw_value is None else round(self.raw_value, 4),
        }


@dataclass
class CalibrationReport:
    generated_at: str
    source: str
    device_profile_id: str
    workloads: tuple[str, ...]
    proposals: tuple[ThresholdProposal, ...]
    notes: tuple[str, ...] = ()

    @property
    def calibrated(self) -> int:
        return sum(1 for item in self.proposals if item.status == STATUS_CALIBRATED)

    def to_thresholds(self) -> dict[str, Threshold]:
        items: dict[str, Threshold] = {}
        for proposal in self.proposals:
            if proposal.status != STATUS_CALIBRATED or proposal.proposed_value is None:
                continue
            stats = ", ".join(f"{key}={value:.2f}" for key, value in proposal.stats.items())
            items[proposal.name] = Threshold(
                name=proposal.name,
                value=proposal.proposed_value,
                unit="ratio",
                provenance=Provenance.MEASURED,
                basis=f"{proposal.method}; {stats}; n={proposal.samples}",
            )
        return items

    def to_thresholds_with_validation(
        self, validation: Mapping[str, Any] | None
    ) -> dict[str, Threshold]:
        """把标定值转成阈值；不稳的项降级为 ``declared``。

        * 留一验证判为 ``stable``/``drift`` → ``measured``；
        * 判为 ``unstable``（分位数对样本敏感）或验证缺失 → ``declared``，
          值沿用全量标定值，但 basis 里写明"为什么不算实测"。
        """
        status_by_name = {
            item["name"]: item for item in (validation or {}).get("drifts", [])
        }
        items: dict[str, Threshold] = {}
        for proposal in self.proposals:
            if proposal.status != STATUS_CALIBRATED or proposal.proposed_value is None:
                continue
            stats = ", ".join(f"{key}={value:.2f}" for key, value in proposal.stats.items())
            drift = status_by_name.get(proposal.name, {})
            verdict = drift.get("status")
            if verdict in ("stable", "drift"):
                items[proposal.name] = Threshold(
                    name=proposal.name,
                    value=proposal.proposed_value,
                    unit="ratio",
                    provenance=Provenance.MEASURED,
                    basis=f"{proposal.method}; {stats}; n={proposal.samples}; 留一验证 {verdict}",
                )
                continue
            detail = (
                f"留一验证 {verdict}（最大漂移 {drift.get('rel_drift'):.0%}）"
                if verdict and drift.get("rel_drift") is not None
                else (f"留一验证 {verdict}" if verdict else "缺少留出验证")
            )
            items[proposal.name] = Threshold(
                name=proposal.name,
                value=proposal.proposed_value,
                unit="ratio",
                provenance=Provenance.DECLARED,
                basis=(
                    f"人工冻结为固定值：{proposal.method} 得 {proposal.proposed_value:.2f}"
                    f"（{stats}; n={proposal.samples}），{detail}，"
                    "不作为实测值声称；Workload 数量增加后重标"
                ),
            )
        return items

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "source": self.source,
            "device_profile_id": self.device_profile_id,
            "workloads": list(self.workloads),
            "calibrated": self.calibrated,
            "total": len(self.proposals),
            "proposals": [item.to_dict() for item in self.proposals],
            "notes": list(self.notes),
        }


def _percentile(values: Sequence[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def _round_to_step(value: float, step: float = 0.05) -> float:
    return round(round(value / step) * step, 3)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fraction(record, names: Iterable[str]) -> float | None:
    total = record.total_time_us.value if record.total_time_us else 0.0
    if total <= 0:
        return None
    parts = [record.extra[name].value for name in names if name in record.extra and record.extra[name]]
    if not parts:
        return None
    return max(parts) / total


def _collect(tables: Mapping[str, OperatorTable], operators: frozenset, kind: str) -> list[float]:
    values: list[float] = []
    for table in tables.values():
        for record in table.records:
            if record.operator not in operators:
                continue
            if kind == "compute":
                value = _fraction(record, ("aicore_time_us", "vec_time_us"))
            elif kind == "memory":
                value = _fraction(record, ("mte1_time_us", "mte2_time_us", "mte3_time_us"))
            else:
                value = _fraction(record, ("mac_time_us",))
            if value is not None:
                values.append(value)
    return values


def _sample_proposal(
    name: str,
    current: Threshold,
    sample: Sequence[float],
    method: str,
    clamp: tuple[float, float],
    percentile: float,
) -> ThresholdProposal:
    """按「分布分位数 + 夹取」生成一条标定提案。"""
    if len(sample) < DEFAULT_MIN_SAMPLES:
        return ThresholdProposal(
            name,
            current.value,
            current.provenance.value,
            None,
            STATUS_INSUFFICIENT,
            method,
            samples=len(sample),
            note=f"样本 {len(sample)} < {DEFAULT_MIN_SAMPLES}，不足以标定",
        )
    raw = _percentile(sample, percentile) or 0.0
    value = _round_to_step(_clamp(raw, *clamp))
    return ThresholdProposal(
        name,
        current.value,
        current.provenance.value,
        value,
        STATUS_CALIBRATED,
        method,
        samples=len(sample),
        stats={
            "p50": _percentile(sample, 0.5) or 0.0,
            "p75": _percentile(sample, 0.75) or 0.0,
            "max": max(sample),
        },
        note=f"取 p{int(percentile * 100)}（原始 {raw:.3f}），夹在 [{clamp[0]}, {clamp[1]}]",
        raw_value=raw,
    )


def _recipe(
    name, compute, compute_mem, mac, memory, memory_compute, comm_shares,
    idle_ratios, host_overheads,
):
    """(样本, 方法, 夹取区间, 分位数)；样本为 None 表示无法从现有数据标定。"""
    if name == "compute_pipe_time_fraction_saturated":
        return compute, "计算类算子的计算流水占比", (0.5, 0.95), 0.90
    if name == "mac_pipe_time_fraction_high":
        return mac, "计算类算子的 mac 流水占比", (0.05, 0.9), 0.75
    if name == "memory_pipe_time_fraction_saturated":
        return memory, "搬运类算子的搬运流水占比", (0.3, 0.95), 0.90
    if name == "memory_pipe_time_fraction_ceiling":
        return compute_mem, "计算类算子的搬运流水占比（未饱和上界）", (0.05, 0.8), 0.75
    if name == "compute_pipe_time_fraction_ceiling":
        return memory_compute, "搬运类算子的计算流水占比（未饱和上界）", (0.05, 0.8), 0.75
    if name == "communication_share_threshold":
        return comm_shares, "各 Workload 通信占比", (0.05, 0.5), 0.5
    if name == "host_gap_ratio":
        return idle_ratios, "各 Workload 设备空闲占比（timeline.json）", (0.05, 0.8), 0.75
    if name == "host_overhead_ratio":
        return host_overheads, "各 Workload ACL 提交时长 / 设备忙碌时长", (0.05, 2.0), 0.75
    return None, "", (0.0, 1.0), 0.5


def _extra_value(record, name: str) -> float | None:
    metric = record.extra.get(name) if record.extra else None
    if metric is None or not metric.value:
        return None
    return float(metric.value)


def _utilization_sample(
    tables: Mapping[str, OperatorTable],
    operators: frozenset,
    peak_flops: float,
) -> list[float]:
    """逐算子的 Cube 利用率 = flops / aicore_time / peak。

    分母用 aicore_time（流水活跃时间）而不是 Task Duration：实测发现小算子
    的 Task Duration 可能小于 aicore_time，用后者会把利用率算到 >1。
    """
    values: list[float] = []
    for table in tables.values():
        for record in table.records:
            if record.operator not in operators:
                continue
            flops = _extra_value(record, "flops")
            aicore_us = _extra_value(record, "aicore_time_us")
            if not flops or not aicore_us or aicore_us <= 0:
                continue
            achieved = flops / (aicore_us * 1e-6)
            values.append(achieved / peak_flops)
    return values


def _core_ratio_sample(
    tables: Mapping[str, OperatorTable], core_count: int
) -> list[float]:
    values: list[float] = []
    for table in tables.values():
        for record in table.records:
            if record.task_type is not TaskType.KERNEL:
                continue
            block = _extra_value(record, "block_num")
            if not block or block <= 0:
                continue
            values.append(block / core_count)
    return values


def _bandwidth_sample(
    tables: Mapping[str, OperatorTable],
    operators: frozenset,
    peak_gbps: float,
) -> list[float]:
    """逐算子的估算带宽利用率 = bytes_moved / bytes_moved_time_us / peak_hbm_gbps。

    bytes 来自 shape×dtype 估算（读写合计口径，忽略缓存复用，上界近似）；
    分母是与 bytes 同一批实例的配对时长（解析器保证分子分母对齐）。
    """
    values: list[float] = []
    for table in tables.values():
        for record in table.records:
            if record.operator not in operators:
                continue
            if record.task_type is TaskType.COMMUNICATION:
                continue
            moved = _extra_value(record, "bytes_moved")
            paired_time = _extra_value(record, "bytes_moved_time_us")
            if not moved or not paired_time or paired_time <= 0:
                continue
            values.append(moved / (paired_time * 1e-6) / (peak_gbps * 1e9))
    return values


def calibrate(
    tables: Mapping[str, OperatorTable],
    ruleset: RuleSet,
    *,
    source: str = "",
    min_samples: int = 3,
    timelines: Mapping[str, Mapping[str, Any]] | None = None,
    device_profile: Mapping[str, Any] | None = None,
) -> CalibrationReport:
    """``timelines``: workload_id -> timeline.json 内容，用于标定 host_* 两项。

    ``device_profile``: micro-benchmark 设备档案（peak_bf16_tflops /
    ai_core_count / roofline_flop_per_byte），启用 aicore_utilization_* /
    low_core_ratio / arithmetic_intensity_low 四项的实测标定。
    """
    compute = _collect(tables, COMPUTE_OPS, "compute")
    compute_mem = _collect(tables, COMPUTE_OPS, "memory")
    mac = _collect(tables, COMPUTE_OPS, "mac")
    memory = _collect(tables, MEMORY_OPS, "memory")
    memory_compute = _collect(tables, MEMORY_OPS, "compute")

    comm_shares: list[float] = []
    for table in tables.values():
        total = table.total_operator_time_us() or 0.0
        if total <= 0:
            continue
        comm = sum(
            (record.total_time_us.value if record.total_time_us else 0.0)
            for record in table.records
            if record.operator.startswith(("hcom_", "hccl_"))
        )
        if comm > 0:
            comm_shares.append(comm / total)

    comm_note = ""
    if comm_shares:
        comm_note = (
            f"观测通信占比 {min(comm_shares):.1%}~{max(comm_shares):.1%}"
            f"（{len(comm_shares)} 个 Workload），建议按需在 0.20~0.30 间取值"
        )

    idle_ratios: list[float] = []
    host_overheads: list[float] = []
    for payload in (timelines or {}).values():
        idle = payload.get("device_idle_ratio")
        if idle is not None:
            idle_ratios.append(float(idle))
        submit = payload.get("submit_time_us") or 0.0
        busy = payload.get("device_busy_us") or 0.0
        if busy > 0:
            host_overheads.append(float(submit) / float(busy))

    proposals: list[ThresholdProposal] = []
    peak_tflops = (device_profile or {}).get("peak_bf16_tflops")
    core_count = (device_profile or {}).get("ai_core_count")
    roofline = (device_profile or {}).get("roofline_flop_per_byte")
    peak_gbps = (device_profile or {}).get("peak_hbm_gbps")
    bw_saturated = (
        _bandwidth_sample(tables, MEMORY_OPS, float(peak_gbps)) if peak_gbps else []
    )
    util_saturated = (
        _utilization_sample(tables, COMPUTE_OPS, float(peak_tflops) * 1e12)
        if peak_tflops
        else []
    )
    util_ceiling = (
        _utilization_sample(tables, MEMORY_OPS, float(peak_tflops) * 1e12)
        if peak_tflops
        else []
    )
    core_ratios = (
        _core_ratio_sample(tables, int(core_count)) if core_count else []
    )
    for name, current in sorted(ruleset.thresholds.items()):
        if name == "arithmetic_intensity_low" and roofline:
            # roofline 拐点是设备常数（peak_flops/peak_bw，micro-benchmark 实测），
            # 不依赖逐算子样本；逐算子强度需要 bytes 列，当前导出没有。
            proposals.append(
                ThresholdProposal(
                    name,
                    current.value,
                    current.provenance.value,
                    float(roofline),
                    STATUS_CALIBRATED,
                    "设备 roofline 拐点（micro-benchmark 实测 peak_flops / peak_bw）",
                    samples=0,
                    stats={"peak_bf16_tflops": float(peak_tflops or 0.0)},
                    note=f"roofline_flop_per_byte={roofline}（设备档案实测），"
                    "逐算子强度需 bytes_moved 列，当前导出未采集",
                    raw_value=float(roofline),
                )
            )
            continue
        if name == "aicore_utilization_saturated" and peak_tflops:
            proposals.append(
                _sample_proposal(
                    name, current, util_saturated,
                    "计算类算子 Cube 利用率（flops/aicore_time/实测峰值）",
                    (0.3, 1.0), 0.90,
                )
            )
            continue
        if name == "aicore_utilization_ceiling" and peak_tflops:
            proposals.append(
                _sample_proposal(
                    name, current, util_ceiling,
                    "搬运类算子 Cube 利用率（未饱和上界）",
                    (0.05, 0.8), 0.75,
                )
            )
            continue
        if name == "low_core_ratio" and core_count:
            proposals.append(
                _sample_proposal(
                    name, current, core_ratios,
                    f"kernel 算子活跃核占比（Block Num/{int(core_count)}）",
                    (0.1, 1.0), 0.50,
                )
            )
            continue
        if name == "bandwidth_utilization_saturated" and peak_gbps:
            proposal = _sample_proposal(
                name, current, bw_saturated,
                "搬运类算子估算带宽利用率（shape 估算字节/时长/实测峰值，读写合计口径）",
                (0.3, 1.0), 0.90,
            )
            if proposal.status == STATUS_CALIBRATED:
                proposal.note += "；bytes 为 shape×dtype 估算（忽略缓存复用，上界近似），"
                "待采集逐算子真实字节后复核"
            proposals.append(proposal)
            continue
        if name in POLICY:
            note = comm_note if name == "communication_share_threshold" else (
                "策略值，不参与标定（与工作负载相关，不是设备能力）"
            )
            proposals.append(
                ThresholdProposal(
                    name, current.value, current.provenance.value, None, STATUS_POLICY,
                    "策略值，不参与标定",
                    samples=len(comm_shares) if name == "communication_share_threshold" else 0,
                    note=note,
                )
            )
            continue
        sample, method, clamp, percentile = _recipe(
            name, compute, compute_mem, mac, memory, memory_compute, comm_shares,
            idle_ratios, host_overheads,
        )
        if sample is None:
            proposals.append(
                ThresholdProposal(
                    name, current.value, current.provenance.value, None,
                    STATUS_NEEDS_BASELINE, NEEDS_BASELINE.get(name, "缺少可用的实测数据来源"),
                )
            )
            continue
        if len(sample) < min_samples:
            proposals.append(
                ThresholdProposal(
                    name, current.value, current.provenance.value, None, STATUS_INSUFFICIENT,
                    method, samples=len(sample),
                    note=f"样本 {len(sample)} < {min_samples}，不足以标定",
                )
            )
            continue
        raw = _percentile(sample, percentile) or 0.0
        value = _round_to_step(_clamp(raw, *clamp))
        proposals.append(
            ThresholdProposal(
                name, current.value, current.provenance.value, value, STATUS_CALIBRATED,
                method, samples=len(sample),
                stats={
                    "p50": _percentile(sample, 0.5) or 0.0,
                    "p75": _percentile(sample, 0.75) or 0.0,
                    "max": max(sample),
                },
                note=f"取 p{int(percentile * 100)}（原始 {raw:.3f}），夹在 [{clamp[0]}, {clamp[1]}]",
                raw_value=raw,
            )
        )

    notes = [
        f"从 {len(tables)} 个 Workload 推导；计算类样本 {len(compute)}，搬运类样本 {len(memory)}",
        "标定只改阈值不改规则；CONFIRMED 仍需端到端验证（见 feedback）",
        "分位数取自同一批诊断数据（in-sample）：余量 <10% 的命中应视为 provisional，"
        "建议用留出数据或复采数据复核",
    ]
    if not comm_shares:
        notes.append("没有通信样本，communication_share_threshold 未标定")
    if timelines:
        notes.append(
            f"host_* 两项来自 {len(timelines)} 份 timeline.json"
            f"（idle 样本 {len(idle_ratios)}，submit/busy 样本 {len(host_overheads)}）"
        )
    if device_profile:
        notes.append(
            f"设备档案：peak_bf16={peak_tflops} TFLOPS、ai_core_count={core_count}、"
            f"roofline={roofline} FLOP/Byte（micro-benchmark 实测）；"
            "aicore_utilization_* / low_core_ratio / arithmetic_intensity_low 由其标定"
        )
    if peak_gbps:
        notes.append(
            f"带宽利用率基于 shape×dtype 字节估算（读写合计口径，忽略缓存复用，上界近似），"
            f"峰值 {peak_gbps} GB/s 来自 1GiB copy 微基准；待采集逐算子真实字节后复核"
        )
    return CalibrationReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=source,
        device_profile_id=ruleset.device_profile_id,
        workloads=tuple(sorted(tables)),
        proposals=tuple(proposals),
        notes=tuple(notes),
    )
