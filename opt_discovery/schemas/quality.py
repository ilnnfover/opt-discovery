"""机器可读的质量标记（AE-03）。

标记分两级：

* ``blocking``：质量闸门未通过，**不得**输出有效比较结论（速度差、因果判断）；
* ``degraded``：允许产出，但受影响结论必须被限制或附带说明。

标记随 ``ParseMeta.extra["quality_flags"]`` 落盘，因此 ``analyze`` 写出的表被
``compare`` 读取时仍能保留「这个 rank 不可合并 / 时长被截断」的事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1

BLOCKING = "blocking"
DEGRADED = "degraded"
SEVERITIES = (BLOCKING, DEGRADED)

#: 汇总口径无法验证（rank 解析失败 / 只有一个 rank 可读但采集声明 TP>1）
RANK_MERGE_UNVERIFIED = "rank_merge_unverified"
#: 已比较两个 rank，调用数/累计时间或单次耗时不一致
RANK_MERGE_INCONSISTENT = "rank_merge_inconsistent"
#: 两个 rank 的统计口径不同（窗口 vs 全量），只能比单次耗时
RANK_SCOPE_MISMATCH = "rank_scope_mismatch"
#: 任务数接近但累计时长差数倍：长 kernel 时长疑似被截断
RANK_DURATION_INCONSISTENT = "rank_duration_inconsistent"
#: 稳态窗口质量下降（空洞过大/忙碌占比过低）
WINDOW_DEGRADED = "window_degraded"
#: 数据来自 sqlite 回填，缺时间戳与流水指标
SQLITE_FALLBACK = "sqlite_fallback"
#: 部分 rank 缺少 timeline 数据库，busy/idle 与通信重叠结论不完整
RANK_TIMELINE_INCOMPLETE = "rank_timeline_incomplete"
#: 标记本身无法解析（未知 severity/缺字段）：必须按未验证处理，不能静默丢弃
QUALITY_FLAG_INVALID = "quality_flag_invalid"
#: 必需 timeline 未执行（缺 DB 或执行异常）：busy/idle、通信重叠等结论不可用
TIMELINE_UNAVAILABLE = "timeline_unavailable"
#: 用户显式 `--skip-timeline`：降级提示，不视为失败
TIMELINE_SKIPPED = "timeline_skipped"
#: 窗口内零行：无法作为任何比较输入
EMPTY_WINDOW = "empty_window"
#: 请求的轮次在 op_summary 中没有任何行（profiling 丢数据）
MISSING_ROUNDS = "missing_rounds"
#: 某轮行数远低于同组中位数（疑似截断）
LOW_ROUND_COVERAGE = "low_round_coverage"
#: 合成数据与实测数据被放在一起比较
SYNTHETIC_MISMATCH = "synthetic_mismatch"
#: 采集条件未核对（verified=false）
VERIFIED_FALSE = "verified_false"
#: 表没有任何算子记录（手工/空导出）
EMPTY_TABLE = "empty_table"

#: 这些 code 一旦出现就必须阻断比较（即使 severity 字段缺失或被写成字符串）
KNOWN_BLOCKING_CODES = frozenset(
    {
        RANK_MERGE_UNVERIFIED,
        RANK_MERGE_INCONSISTENT,
        RANK_DURATION_INCONSISTENT,
        RANK_TIMELINE_INCOMPLETE,
        TIMELINE_UNAVAILABLE,
        QUALITY_FLAG_INVALID,
        EMPTY_WINDOW,
        MISSING_ROUNDS,
        LOW_ROUND_COVERAGE,
        SYNTHETIC_MISMATCH,
        VERIFIED_FALSE,
        EMPTY_TABLE,
    }
)


@dataclass(frozen=True)
class QualityFlag:
    """一个质量事实；``code`` 稳定，``message`` 给人看，``details`` 给机器用。"""

    code: str
    message: str
    severity: str = DEGRADED
    source: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("quality flag code must not be empty")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}; allowed: {SEVERITIES}")
        # code 决定最低严重性：已知阻断码不能被调用方“降级”为 degraded
        if self.code in KNOWN_BLOCKING_CODES and self.severity != BLOCKING:
            raise ValueError(
                f"{self.code} 是已知阻断码，severity 不能为 {self.severity!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.source:
            payload["source"] = self.source
        if self.details:
            payload["details"] = dict(self.details)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QualityFlag":
        code = str(data["code"])
        severity = data.get("severity")
        if severity is None:
            # 旧表没有 severity：已知阻断码按阻断处理，其余按提示处理
            severity = BLOCKING if code in KNOWN_BLOCKING_CODES else DEGRADED
        return cls(
            code=code,
            message=str(data.get("message") or ""),
            severity=str(severity),
            source=data.get("source"),
            details=dict(data.get("details") or {}),
        )


def blocking(flags: Iterable[QualityFlag]) -> tuple[QualityFlag, ...]:
    return tuple(flag for flag in flags if flag.severity == BLOCKING)


def degraded(flags: Iterable[QualityFlag]) -> tuple[QualityFlag, ...]:
    return tuple(flag for flag in flags if flag.severity == DEGRADED)


def parse_meta_flags(meta: Any) -> tuple[tuple[QualityFlag, ...], tuple[str, ...]]:
    """读取标记并返回无法解析的条目（不能静默丢弃，否则阻断标记会消失）。"""
    extra = getattr(meta, "extra", None) or {}
    raw = extra.get("quality_flags")
    if raw is None:
        return (), ()
    if not isinstance(raw, (list, tuple)):
        # `{}`、字符串、数字都是“存在但格式错误”，不能当成缺字段静默放过
        return (), (f"quality_flags 不是列表: {type(raw).__name__}",)
    flags: list[QualityFlag] = []
    problems: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or "code" not in item:
            problems.append(f"quality_flags[{index}] 缺少 code")
            continue
        try:
            flags.append(QualityFlag.from_dict(item))
        except (ValueError, TypeError) as exc:
            problems.append(f"quality_flags[{index}] 无法解析: {exc}")
    return tuple(flags), tuple(problems)


def invalid_flag(problems: Iterable[str], *, source: str = "unknown") -> QualityFlag | None:
    """把无法解析的标记转成一个阻断型标记；没有问题时返回 None。"""
    items = tuple(problems)
    if not items:
        return None
    return QualityFlag(
        code=QUALITY_FLAG_INVALID,
        severity=BLOCKING,
        message="质量标记存在但无法解析，按未验证处理（不允许视为可比）",
        source=source,
        details={"problems": list(items)},
    )


def flags_from_meta(meta: Any) -> tuple[QualityFlag, ...]:
    """兼容包装：只返回可解析标记；需要知道坏条目时用 :func:`parse_meta_flags`。"""
    flags, _problems = parse_meta_flags(meta)
    return flags


def attach(meta: Any, flags: Iterable[QualityFlag]) -> tuple[QualityFlag, ...]:
    """合并写入 ``meta.extra['quality_flags']``，按 ``code`` 去重（先到先得）。"""
    if not hasattr(meta, "extra"):
        raise TypeError("meta must expose an `extra` mapping (ParseMeta)")
    merged = {flag.code: flag for flag in flags_from_meta(meta)}
    for flag in flags:
        merged.setdefault(flag.code, flag)
    meta.extra["quality_flags"] = [flag.to_dict() for flag in merged.values()]
    return tuple(merged.values())


__all__ = [
    "BLOCKING",
    "DEGRADED",
    "RANK_DURATION_INCONSISTENT",
    "RANK_MERGE_INCONSISTENT",
    "RANK_MERGE_UNVERIFIED",
    "EMPTY_TABLE",
    "EMPTY_WINDOW",
    "LOW_ROUND_COVERAGE",
    "MISSING_ROUNDS",
    "RANK_SCOPE_MISMATCH",
    "RANK_TIMELINE_INCOMPLETE",
    "SYNTHETIC_MISMATCH",
    "VERIFIED_FALSE",
    "SQLITE_FALLBACK",
    "TIMELINE_SKIPPED",
    "TIMELINE_UNAVAILABLE",
    "SCHEMA_VERSION",
    "QUALITY_FLAG_INVALID",
    "KNOWN_BLOCKING_CODES",
    "invalid_flag",
    "parse_meta_flags",
    "WINDOW_DEGRADED",
    "QualityFlag",
    "attach",
    "blocking",
    "degraded",
    "flags_from_meta",
]
