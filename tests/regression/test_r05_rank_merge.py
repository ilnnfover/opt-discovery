"""R05：缺失 rank / 不可合并 rank 不能被当成可信观测。

历史错误（README 已知约束 5）：decode 的 rank1 没有 CSV（只能 sqlite 回填），
且该 rank 与 rank0 的累计时间差 10 倍，不能直接按 rank 合并。
不变量 INV-04/INV-05：证据缺口与不可比必须显式判定，不能静默合并。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _helpers import (
    WORKLOADS,
    make_op_report_db,
    make_rank_export,
    summary_row,
    write_op_summary,
)
from opt_discovery.cli import main
from opt_discovery.collectors import discover_bundle
from opt_discovery.parsers.sqlite_op_report import SqliteOpReportParser

WORKLOAD_YAML = WORKLOADS / "decode_eager.yaml"


def _two_ranks(root: Path, *, rank1_scale: float, rows_per_op: int = 2) -> Path:
    for rank in (0, 1):
        prof = make_rank_export(root, rank)
        rows = [
            summary_row("MatMul", 100.0 * rank1_scale if rank == 1 else 100.0, start_us=index * 200)
            for index in range(rows_per_op)
        ] + [
            summary_row("Add", 50.0 * rank1_scale if rank == 1 else 50.0, start_us=1000 + index * 100)
            for index in range(rows_per_op)
        ]
        write_op_summary(prof / "mindstudio_profiler_output" / f"op_summary_{rank}.csv", rows)
    return root


def test_check_ranks_reports_inconsistency(tmp_path: Path, capsys) -> None:
    """任务数相同、累计时长 4x：判定不可合并，而不是给出「某卡更忙」的结论。"""
    export = _two_ranks(tmp_path / "export", rank1_scale=4.0)
    code = main(["check-ranks", "--export", str(export), "--workload", str(WORKLOAD_YAML)])
    out = capsys.readouterr().out

    assert code == 0
    assert "口径相同" in out
    assert "不一致（不可合并）" in out
    assert "单次耗时整体偏离" in out
    assert "一致（可合并/可代表）" not in out


def test_check_ranks_strict_returns_nonzero_for_inconsistent_ranks(
    tmp_path: Path, capsys
) -> None:
    """--strict：rank 不一致必须非零退出（默认模式保持 0 兼容既有脚本）。"""
    export = _two_ranks(tmp_path / "export", rank1_scale=4.0)

    code = main(
        ["check-ranks", "--export", str(export), "--workload", str(WORKLOAD_YAML), "--strict"]
    )
    out = capsys.readouterr()

    assert code == 3
    assert "不一致（不可合并）" in out.out
    assert "[strict]" in out.err


def test_missing_rank_is_not_silently_merged(tmp_path: Path, capsys) -> None:
    """只有一个 rank 可用时必须非零退出并说明缺什么。"""
    export = _two_ranks(tmp_path / "export", rank1_scale=1.0)
    for path in export.glob("PROF_*"):
        if "fixture" in path.name and path.name.endswith("1_fixture"):
            for item in path.rglob("*"):
                if item.is_file():
                    item.unlink()

    code = main(["check-ranks", "--export", str(export), "--workload", str(WORKLOAD_YAML)])
    captured = capsys.readouterr()

    assert code == 2
    assert "只有 1 个 rank 可解析" in captured.err


def test_sqlite_fallback_rank_is_reported_as_different_scope(tmp_path: Path, capsys) -> None:
    """缺 CSV 的 rank 用 sqlite 回填后，calls/total 不可与窗口口径直接比较。"""
    export = _two_ranks(tmp_path / "export", rank1_scale=1.0)
    # 删掉 rank1 的 CSV，换成 device_1/sqlite/op_counter.db（ns 单位）
    rank1_prof = next(p for p in export.glob("PROF_*") if p.name.endswith("1_fixture"))
    for item in rank1_prof.rglob("*.csv"):
        item.unlink()
    make_op_report_db(
        rank1_prof / "device_1" / "sqlite" / "op_counter.db",
        [
            ("MatMul", "AI_CORE", 2, 200_000, 100_000, 100_000, 100_000, 66.0),
            ("Add", "AI_CORE", 2, 100_000, 50_000, 50_000, 50_000, 34.0),
        ],
    )

    bundle = discover_bundle(WORKLOAD_YAML, export)
    assert any("回填" in note or "sqlite" in note for note in bundle.notes)

    code = main(["check-ranks", "--export", str(export), "--workload", str(WORKLOAD_YAML)])
    out = capsys.readouterr().out

    assert code == 0
    assert "口径不同" in out
    assert "calls/total 不可直接比较" in out


def test_sqlite_table_carries_missing_capability_flags(tmp_path: Path) -> None:
    """回填数据必须声明缺流水指标 / 缺时间戳，不能冒充完整 CSV 观测。"""
    db = make_op_report_db(
        tmp_path / "PROF_x" / "device_1" / "sqlite" / "op_counter.db",
        [("MatMul", "AI_CORE", 2, 200_000, 100_000, 100_000, 100_000, 100.0)],
    )
    table = SqliteOpReportParser().parse(db, discover_bundle(
        WORKLOAD_YAML, _two_ranks(tmp_path / "export", rank1_scale=1.0)
    ).workload)

    codes = {warning.code for warning in table.meta.warnings}
    assert {"sqlite_fallback", "no_pipe_metrics", "no_timestamps"} <= codes
    record = next(r for r in table.records if r.operator == "MatMul")
    assert record.total_time_us.value == pytest.approx(200.0)  # ns -> us
    assert record.metric("aicore_time_us") is None
