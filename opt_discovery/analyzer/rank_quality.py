"""rank 合并质量评估（AE-03）。

历史问题（README 已知约束 5/10）：decode 的 rank1 曾缺 CSV（只能 sqlite 回填），
且长 kernel 时长被截断，导致「任务数接近但累计时长差数倍」。这类 rank：

* 不能当作可直接合并的可信观测（R05）；
* 相关对比必须受限（R10）。

本模块把判定从 CLI 文案里抽出来，产出 ``RankMergeAssessment`` 并转换为
:class:`~opt_discovery.schemas.quality.QualityFlag`，随表落盘供 ``compare`` 消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..schemas.operator import OperatorTable
from ..schemas.quality import (
    BLOCKING,
    DEGRADED,
    RANK_MERGE_INCONSISTENT,
    RANK_MERGE_UNVERIFIED,
    RANK_SCOPE_MISMATCH,
    QualityFlag,
)

CALLS_RATIO_RANGE = (0.90, 1.10)
TOTAL_RATIO_RANGE = (0.80, 1.25)
MEDIAN_AVG_RATIO_RANGE = (0.70, 1.40)
MEDIAN_TOP_N = 8


@dataclass(frozen=True)
class RankDigest:
    rank: int
    source: str
    scope: str
    records: int
    calls: int
    total_us: float

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "source": self.source,
            "scope": self.scope,
            "records": self.records,
            "calls": self.calls,
            "total_us": round(self.total_us, 6),
        }


@dataclass(frozen=True)
class RankRow:
    operator: str
    share_base: float
    share_peer: float
    avg_base: float
    avg_peer: float
    avg_ratio: float | None


@dataclass(frozen=True)
class RankMergeAssessment:
    ranks: tuple[int, int]
    digests: tuple[RankDigest, ...]
    same_scope: bool
    calls_ratio: float | None
    total_ratio: float | None
    median_avg_ratio: float | None
    consistent: bool
    reasons: tuple[str, ...]
    rows: tuple[RankRow, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "ranks": list(self.ranks),
            "same_scope": self.same_scope,
            "calls_ratio": None if self.calls_ratio is None else round(self.calls_ratio, 6),
            "total_ratio": None if self.total_ratio is None else round(self.total_ratio, 6),
            "median_avg_ratio": (
                None if self.median_avg_ratio is None else round(self.median_avg_ratio, 6)
            ),
            "consistent": self.consistent,
            "reasons": list(self.reasons),
            "digests": [digest.to_dict() for digest in self.digests],
        }

    def quality_flags(self) -> tuple[QualityFlag, ...]:
        flags: list[QualityFlag] = []
        details = self.to_dict()
        if not self.consistent:
            flags.append(
                QualityFlag(
                    code=RANK_MERGE_INCONSISTENT,
                    severity=BLOCKING,
                    message="rank 之间不可合并（" + "；".join(self.reasons or ("未知原因",)) + "）",
                    source="rank_quality",
                    details=details,
                )
            )
        if not self.same_scope:
            flags.append(
                QualityFlag(
                    code=RANK_SCOPE_MISMATCH,
                    severity=DEGRADED,
                    message="两个 rank 的统计口径不同（窗口 vs 全量），累计时间与 calls 不可直接比较",
                    source="rank_quality",
                    details={"scopes": [digest.scope for digest in self.digests]},
                )
            )
        return tuple(flags)


def unverified_flags(
    reason: str, *, details: Mapping[str, object] | None = None
) -> tuple[QualityFlag, ...]:
    return (
        QualityFlag(
            code=RANK_MERGE_UNVERIFIED,
            severity=BLOCKING,
            message=f"rank 合并质量未验证：{reason}",
            source="rank_quality",
            details=dict(details or {}),
        ),
    )


def assess_rank_tables(
    tables: Mapping[int, OperatorTable],
    sources: Mapping[int, str],
    scopes: Mapping[int, str],
) -> RankMergeAssessment:
    """按历史 CLI 口径比较前两个 rank（同口径比 calls/total，跨口径比单次耗时）。"""
    ranks = tuple(sorted(tables))[:2]
    base, peer = tables[ranks[0]], tables[ranks[1]]
    total_base = base.total_operator_time_us() or 0.0
    total_peer = peer.total_operator_time_us() or 0.0
    calls_base = sum(record.calls for record in base.records)
    calls_peer = sum(record.calls for record in peer.records)
    same_scope = scopes[ranks[0]] == scopes[ranks[1]]

    digests = tuple(
        RankDigest(
            rank=index,
            source=sources[index],
            scope=scopes[index],
            records=len(tables[index].records),
            calls=sum(record.calls for record in tables[index].records),
            total_us=tables[index].total_operator_time_us() or 0.0,
        )
        for index in sorted(tables)
    )

    base_map = {record.operator: record for record in base.records}
    rows: list[RankRow] = []
    for record in peer.records:
        reference = base_map.get(record.operator)
        if reference is None or not reference.total_time_us or not record.total_time_us:
            continue
        average_base = reference.total_time_us.value / reference.calls if reference.calls else 0.0
        average_peer = record.total_time_us.value / record.calls if record.calls else 0.0
        rows.append(
            RankRow(
                operator=record.operator,
                share_base=(reference.total_time_us.value / total_base if total_base else 0.0),
                share_peer=(record.total_time_us.value / total_peer if total_peer else 0.0),
                avg_base=average_base,
                avg_peer=average_peer,
                avg_ratio=(average_peer / average_base) if average_base else None,
            )
        )
    rows.sort(key=lambda item: -item.share_base)

    calls_ratio = calls_peer / calls_base if calls_base else None
    total_ratio = total_peer / total_base if total_base else None
    median_avg_ratio: float | None = None
    reasons: list[str] = []
    if same_scope:
        consistent = (
            calls_ratio is not None
            and total_ratio is not None
            and CALLS_RATIO_RANGE[0] <= calls_ratio <= CALLS_RATIO_RANGE[1]
            and TOTAL_RATIO_RANGE[0] <= total_ratio <= TOTAL_RATIO_RANGE[1]
        )
        if not consistent:
            reasons.append(
                f"calls_ratio={calls_ratio if calls_ratio is not None else 'NA'}、"
                f"total_ratio={total_ratio if total_ratio is not None else 'NA'} "
                f"超出允许范围 {CALLS_RATIO_RANGE}/{TOTAL_RATIO_RANGE}"
            )
    else:
        ratios = [row.avg_ratio for row in rows[:MEDIAN_TOP_N] if row.avg_ratio is not None]
        median_avg_ratio = sorted(ratios)[len(ratios) // 2] if ratios else None
        consistent = bool(
            median_avg_ratio is not None
            and MEDIAN_AVG_RATIO_RANGE[0] <= median_avg_ratio <= MEDIAN_AVG_RATIO_RANGE[1]
        )
        if not consistent:
            reasons.append(
                "单次耗时比中位数="
                f"{median_avg_ratio if median_avg_ratio is not None else 'NA'} "
                f"超出允许范围 {MEDIAN_AVG_RATIO_RANGE}"
            )

    return RankMergeAssessment(
        ranks=(ranks[0], ranks[1]),
        digests=digests,
        same_scope=same_scope,
        calls_ratio=calls_ratio,
        total_ratio=total_ratio,
        median_avg_ratio=median_avg_ratio,
        consistent=consistent,
        reasons=tuple(reasons),
        rows=tuple(rows),
    )


def load_rank_tables(
    bundle,
    parser,
    sqlite_parser,
    window,
) -> tuple[dict[int, OperatorTable], dict[int, str], dict[int, str], tuple[str, ...]]:
    """解析一个 bundle 的全部 rank；返回 ``tables/sources/scopes/problems``。

    CSV 缺失时按历史约定回退到 ``device_*/sqlite/op_counter.db``（全量口径）。
    """
    tables: dict[int, OperatorTable] = {}
    sources: dict[int, str] = {}
    scopes: dict[int, str] = {}
    problems: list[str] = []
    for rank in bundle.ranks:
        if rank.op_summary_files:
            tables[rank.rank] = parser.parse(
                rank.op_summary_files, bundle.workload, window=window
            )
            sources[rank.rank] = "op_summary(窗口)"
            scopes[rank.rank] = "window"
            continue
        target = rank.op_report_db or rank.directory
        try:
            tables[rank.rank] = sqlite_parser.parse(target, bundle.workload)
            sources[rank.rank] = "sqlite op_report(全量)"
            scopes[rank.rank] = "full"
        except Exception as exc:  # noqa: BLE001 - 单 rank 失败要记录，不能中断整批
            problems.append(f"rank{rank.rank}: 无法解析（{exc}）")
    return tables, sources, scopes, tuple(problems)


def assess_bundle(
    bundle,
    parser,
    sqlite_parser,
    window,
) -> tuple[RankMergeAssessment | None, tuple[QualityFlag, ...], dict[int, OperatorTable]]:
    """端到端评估一个 bundle 的 rank 合并质量。

    * 只声明 1 个 rank：没有可合并对象，返回 ``(None, (), tables)``；
    * 声明多个 rank 但可解析不足 2 个：``rank_merge_unverified``（blocking）；
    * 两个 rank 都可用：返回评估结果 + 相应标记（不一致为 blocking）。
    """
    tables, sources, scopes, problems = load_rank_tables(
        bundle, parser, sqlite_parser, window
    )
    declared = len(bundle.ranks)
    if len(tables) < 2:
        if declared <= 1:
            return None, (), tables
        detail = {
            "declared_ranks": declared,
            "parsed_ranks": sorted(tables),
            "problems": list(problems),
        }
        reason = (
            f"声明 {declared} 个 rank，仅 {len(tables)} 个可解析"
            + (f"（{problems[0]}）" if problems else "")
        )
        return None, unverified_flags(reason, details=detail), tables

    assessment = assess_rank_tables(tables, sources, scopes)
    return assessment, assessment.quality_flags(), tables


def rank_quality_from_timeline(summary) -> tuple[QualityFlag, ...]:
    """把 timeline 的时长截断标记转成阻断型质量标记（R10）。"""
    if not getattr(summary, "rank_duration_inconsistent", False):
        return ()
    per_rank = [
        {
            "index": index,
            "tasks": item.get("tasks"),
            "duration_sum_us": item.get("duration_sum_us"),
            "busy_ratio": item.get("busy_ratio"),
        }
        for index, item in enumerate(getattr(summary, "per_rank", ()) or ())
    ]
    return (
        QualityFlag(
            code="rank_duration_inconsistent",
            severity=BLOCKING,
            message="rank 之间任务数接近但累计时长相差数倍：长 kernel 时长疑似被截断，相关对比不可用",
            source="timeline",
            details={"per_rank": per_rank, "notes": list(getattr(summary, "notes", ()))},
        ),
    )


__all__ = [
    "CALLS_RATIO_RANGE",
    "MEDIAN_AVG_RATIO_RANGE",
    "MEDIAN_TOP_N",
    "RankDigest",
    "RankMergeAssessment",
    "RankRow",
    "TOTAL_RATIO_RANGE",
    "assess_bundle",
    "assess_rank_tables",
    "load_rank_tables",
    "rank_quality_from_timeline",
    "unverified_flags",
]
