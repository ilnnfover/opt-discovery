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
        return cls(
            code=str(data["code"]),
            message=str(data.get("message") or ""),
            severity=str(data.get("severity") or DEGRADED),
            source=data.get("source"),
            details=dict(data.get("details") or {}),
        )


def blocking(flags: Iterable[QualityFlag]) -> tuple[QualityFlag, ...]:
    return tuple(flag for flag in flags if flag.severity == BLOCKING)


def degraded(flags: Iterable[QualityFlag]) -> tuple[QualityFlag, ...]:
    return tuple(flag for flag in flags if flag.severity == DEGRADED)


def flags_from_meta(meta: Any) -> tuple[QualityFlag, ...]:
    """从 ``ParseMeta.extra['quality_flags']`` 读取标记；坏数据不静默丢弃。"""
    raw = (getattr(meta, "extra", None) or {}).get("quality_flags") or ()
    flags: list[QualityFlag] = []
    for item in raw:
        if not isinstance(item, Mapping) or "code" not in item:
            continue
        try:
            flags.append(QualityFlag.from_dict(item))
        except (ValueError, TypeError):
            continue
    return tuple(flags)


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
    "RANK_SCOPE_MISMATCH",
    "SQLITE_FALLBACK",
    "SCHEMA_VERSION",
    "WINDOW_DEGRADED",
    "QualityFlag",
    "attach",
    "blocking",
    "degraded",
    "flags_from_meta",
]
