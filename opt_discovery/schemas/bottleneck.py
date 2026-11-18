"""Bottleneck taxonomy shared by the rule engine, opportunity schema and reports.

The library mirrors the six categories agreed in ``plan.md`` and deliberately
records the *counter checks* that must accompany each category, because a single
metric is never enough to declare a bottleneck.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["BottleneckType", "BottleneckInfo", "BOTTLENECK_LIBRARY", "get_bottleneck"]


class BottleneckType(str, Enum):
    COMPUTE_BOUND = "compute_bound"
    MEMORY_BOUND = "memory_bound"
    LAUNCH_BOUND = "launch_bound"
    SYNCHRONIZATION_BOUND = "synchronization_bound"
    PARALLELISM_BOUND = "parallelism_bound"
    COMMUNICATION_BOUND = "communication_bound"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BottleneckInfo:
    type: BottleneckType
    label: str
    definition: str
    typical_evidence: tuple[str, ...]
    counter_checks: tuple[str, ...]
    candidate_directions: tuple[str, ...]


BOTTLENECK_LIBRARY: dict[BottleneckType, BottleneckInfo] = {
    BottleneckType.COMPUTE_BOUND: BottleneckInfo(
        type=BottleneckType.COMPUTE_BOUND,
        label="计算受限",
        definition="算子受限于 AI Core 计算流水，计算单元占用明显高于其他流水。",
        typical_evidence=(
            "aicore/mac 时间占算子任务时长的比例高",
            "与设备峰值对比的计算利用率接近饱和（需要校准数据）",
        ),
        counter_checks=(
            "算子累计耗时长不等于计算受限：可能只是被调度或等待占用",
            "时间占比是占自身任务时长的比例，不是占设备峰值的利用率",
            "需要确认是否被 tiling/搬运限制，而不是计算指令本身",
        ),
        candidate_directions=("Tiling 与切分调整", "计算指令/数据类型优化", "算子融合减少重复计算"),
    ),
    BottleneckType.MEMORY_BOUND: BottleneckInfo(
        type=BottleneckType.MEMORY_BOUND,
        label="访存受限",
        definition="算子受限于数据搬运（MTE2/MTE3）或带宽，计算流水未饱和。",
        typical_evidence=(
            "搬运流水时间占比高而计算流水时间占比低",
            "实测带宽接近设备可达带宽（需要校准数据）",
            "算术强度低",
        ),
        counter_checks=(
            "带宽利用率低也可能是访存不连续或并行度不足，不能据此排除访存受限",
            "搬运流水繁忙不等于带宽饱和，可能是小粒度搬运导致指令开销",
        ),
        candidate_directions=("数据布局与合并访存", "减少冗余读写", "提高算术强度/算子融合"),
    ),
    BottleneckType.LAUNCH_BOUND: BottleneckInfo(
        type=BottleneckType.LAUNCH_BOUND,
        label="下发受限",
        definition="Host 侧提交与调度开销在整体耗时中占比明显，Device 执行不是主因。",
        typical_evidence=("Host 提交时间与 Device 执行时间的比值高", "提交到启动的间隙大"),
        counter_checks=(
            "Kernel 很短不等于下发受限",
            "需要 Timeline 证据（提交/启动/结束事件）才能确认，算子统计表不足以判定",
        ),
        candidate_directions=("Graph 执行路径", "算子融合", "减少 Python 侧调度与同步点"),
    ),
    BottleneckType.SYNCHRONIZATION_BOUND: BottleneckInfo(
        type=BottleneckType.SYNCHRONIZATION_BOUND,
        label="同步受限",
        definition="Event/Barrier/流同步的等待时间在整体耗时中占比明显。",
        typical_evidence=("同步等待时间与 Device 执行时间比值高", "Timeline 中存在明显空洞"),
        counter_checks=("等待可能来自上下游依赖而非同步原语本身", "需要区分设备空闲与流等待"),
        candidate_directions=("减少同步点", "调整 Stream/Event 依赖", "重叠 Host 与 Device 工作"),
    ),
    BottleneckType.PARALLELISM_BOUND: BottleneckInfo(
        type=BottleneckType.PARALLELISM_BOUND,
        label="并行度不足",
        definition="任务划分未能占满可用核，设备利用不充分。",
        typical_evidence=("活跃核比例低", "相同算子在不同 Shape 下耗时差异异常"),
        counter_checks=("小 Shape 下活跃核低可能是合理现象", "需要核数与 Tile Size 的实际配置"),
        candidate_directions=("调整 Tiling 与核数", "任务切分", "批处理合并小请求"),
    ),
    BottleneckType.COMMUNICATION_BOUND: BottleneckInfo(
        type=BottleneckType.COMMUNICATION_BOUND,
        label="通信受限",
        definition="集合通信或 H2D/D2H 传输时间/等待占比明显，且未与计算重叠。",
        typical_evidence=("通信时间与计算时间比值高", "通信与计算串行"),
        counter_checks=("单卡场景下通信应接近 0，出现通信时间需先确认采集口径", "需确认是否可重叠"),
        candidate_directions=("通信计算重叠", "通信算法与分桶策略", "减少传输数据量"),
    ),
    BottleneckType.UNKNOWN: BottleneckInfo(
        type=BottleneckType.UNKNOWN,
        label="证据不足",
        definition="现有数据无法归类，需要补充 Profiling 指标。",
        typical_evidence=(),
        counter_checks=("不要用默认值填充缺失指标",),
        candidate_directions=("补充 Timeline 或硬件指标采集",),
    ),
}


def get_bottleneck(value: str | BottleneckType) -> BottleneckInfo:
    """Resolve a bottleneck type; unknown strings map to :attr:`UNKNOWN`."""
    if isinstance(value, BottleneckType):
        return BOTTLENECK_LIBRARY[value]
    try:
        return BOTTLENECK_LIBRARY[BottleneckType(str(value).strip().lower())]
    except (KeyError, ValueError):
        return BOTTLENECK_LIBRARY[BottleneckType.UNKNOWN]
