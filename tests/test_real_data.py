"""Integration tests against the real msprof export.

These are skipped when the export is not present, so the suite stays portable.
They assert *口径* (semantics) that were verified manually, not exact timings:
machine-specific numbers are compared relatively or with a tolerance.

Data root: ``$OPT_DISCOVERY_DATA_ROOT``（未设置时用相对路径，目录不存在则整组 skip）。
期望布局: ``<root>/qwen35-prefill/PROF_*/mindstudio_profiler_output``。
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.discovery.generator import opportunities_from_diagnosis
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
from opt_discovery.schemas.evidence import ConfidenceLevel
from opt_discovery.schemas.operator import TaskType
from opt_discovery.schemas.workload import WorkloadSpec

_DATA_ROOT = Path(os.environ.get("OPT_DISCOVERY_DATA_ROOT", "."))
PROF_ROOT = _DATA_ROOT / "qwen35-prefill"
REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not PROF_ROOT.is_dir(), reason="real profiling export not available on this machine"
)

SUMMARY_MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_summary.yaml"
STATISTIC_MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_statistic.yaml"
RULES = REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"
WORKLOAD = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b" / "prefill_eager.yaml"


def _all_prof_dirs() -> list[Path]:
    """磁盘上的全部 PROF_* 目录，包括没有任何输出的「空会话」。"""
    return sorted(p for p in PROF_ROOT.glob("PROF_*") if p.is_dir())


def _rank_dirs() -> list[Path]:
    """有可解析 CSV 的 rank 目录。

    空 PROF 会话（无任何 CSV）不参与解析，也不能被当成一个 rank；它是否被正确
    识别由 ``test_export_has_two_ranks`` 单独核对。若这里不过滤，所有依赖
    ``_output`` 的用例会一起 skip，真实覆盖会被静默丢掉。
    """
    usable: list[Path] = []
    for path in _all_prof_dirs():
        output = path / "mindstudio_profiler_output"
        has_operator_csv = bool(list(output.glob("op_summary*.csv"))) or bool(
            list(output.glob("op_statistic*.csv"))
        )
        if has_operator_csv:
            usable.append(path)
    return usable


def _output(rank: Path, prefix: str) -> Path:
    matches = sorted((rank / "mindstudio_profiler_output").glob(f"{prefix}_*.csv"))
    if not matches:
        pytest.skip(f"{prefix} not found under {rank}")
    return matches[0]


@pytest.fixture(scope="module")
def workload() -> WorkloadSpec:
    return WorkloadSpec.from_yaml(WORKLOAD)


@pytest.fixture(scope="module")
def statistic_tables(workload):
    parser = MappingCsvParser(CsvMapping.load(STATISTIC_MAPPING))
    return [(rank, parser.parse(_output(rank, "op_statistic"), workload)) for rank in _rank_dirs()]


@pytest.fixture(scope="module")
def summary_tables(workload):
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    return [(rank, parser.parse(_output(rank, "op_summary"), workload)) for rank in _rank_dirs()]


def test_export_has_two_ranks():
    """TP=2：导出里必须有 device_0/device_1 两个完整 rank。

    2026-11-15 统一补采的目录里还会残留一次「空 PROF 目录」（无 CSV、无 sqlite）。
    它必须被跳过，不能被当成第三个 rank 计入（否则 rank 合并口径会被污染）。
    """
    ranks = _all_prof_dirs()
    complete = _rank_dirs()
    devices = sorted((next(iter(r.glob("device_*")), r)).name for r in complete)
    assert devices == ["device_0", "device_1"]
    for empty in (r for r in ranks if r not in complete):
        assert not list(empty.glob("device_*/sqlite/op_counter.db")), "空导出不应含可用 sqlite"


def test_op_statistic_total_matches_profiler_sum(statistic_tables):
    for rank, table in statistic_tables:
        with _output(rank, "op_statistic").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        profiler_total = sum(float(row["Total Time(us)"]) for row in rows)
        assert table.total_operator_time_us() == pytest.approx(profiler_total, rel=1e-9)
        assert table.meta.skipped_rows == 0
        # 导出规模随补采变化：与磁盘行数独立核对，而不是写死历史数字
        assert table.meta.row_count == len(rows)


def test_operator_variants_are_split_by_core_type(statistic_tables):
    """MatMulV3 在导出里按 Core Type 拆成两行，聚合后必须是两条记录而不是一条。"""
    rank, table = statistic_tables[0]
    variants = [r for r in table.records if r.operator == "MatMulV3"]
    assert len(variants) == 2
    assert {r.attributes["core_type"] for r in variants} == {"AI_CORE", "MIX_AIC"}
    # 期望值来自源文件 Count 列的独立求和（不是复用解析器的聚合结果）
    with _output(rank, "op_statistic").open(newline="") as handle:
        expected = sum(
            int(row["Count"]) for row in csv.DictReader(handle) if row["OP Type"] == "MatMulV3"
        )
    assert sum(r.calls for r in variants) == expected


def test_calls_consistent_between_statistic_and_summary(statistic_tables, summary_tables):
    """两个文件对同一 (operator, core/task type) 的调用次数必须一致。"""
    (_, stat), (_, summ) = statistic_tables[0], summary_tables[0]
    stat_calls = {
        (r.operator, r.attributes.get("core_type", "")): r.calls for r in stat.records
    }
    summ_calls = {
        (r.operator, r.attributes.get("task_type_raw", "")): r.calls for r in summ.records
    }
    shared = set(stat_calls) & set(summ_calls)
    assert len(shared) >= 30, "两个文件应覆盖大量相同算子"
    mismatches = {
        key: (stat_calls[key], summ_calls[key])
        for key in shared
        if stat_calls[key] != summ_calls[key]
    }
    assert mismatches == {}


def test_communication_only_exists_in_op_summary(statistic_tables, summary_tables):
    _, stat = statistic_tables[0]
    _, summ = summary_tables[0]
    assert not [r for r in stat.records if r.operator.startswith("hcom_")]

    comm = [r for r in summ.records if r.task_type is TaskType.COMMUNICATION]
    assert comm, "op_summary 应包含 HCCL 通信算子"
    total = summ.total_operator_time_us()
    share = sum(r.total_time_us.value for r in comm) / total
    # COMMUNICATION 行的 Task Duration 在源文件里是 ns（其余类型是 us），
    # 解析层已换算，因此占比从"看起来 20~50%"修正到千分级。
    assert 1e-5 <= share <= 0.05, f"通信占比异常: {share:.6f}"
    assert all(r.extra.get("communication_time_us") is not None for r in comm)


def test_communication_time_matches_record_total(summary_tables):
    """通信行的 ``communication_time_us`` 必须与 ``total_time_us`` 同口径。

    两者都来自 ``Task Duration``（COMMUNICATION 行是 ns），必须只做一次换算。
    历史缺陷（D3）：``total_time_us`` 换算、额外指标没换算 -> 相差 1000 倍。
    这里用真实导出锁定，避免合成 fixture 掩盖真实列名的差异。
    """
    seen = 0
    for _, table in summary_tables:
        for record in table.records:
            if record.task_type is not TaskType.COMMUNICATION:
                continue
            metric = record.metric("communication_time_us")
            assert metric is not None, record.operator
            assert metric.unit == "us"
            assert record.total_time_us is not None
            assert metric.value == pytest.approx(record.total_time_us.value, rel=1e-9), (
                record.operator
            )
            seen += 1
    assert seen > 0, "op_summary 中应存在 COMMUNICATION 行"


def test_time_share_matches_profiler_ratio_column(statistic_tables):
    """time_share 是派生事实，必须与 Profiler 自算的 Ratio(%) 对齐。"""
    rank, table = statistic_tables[0]
    with _output(rank, "op_statistic").open(newline="") as handle:
        profiler_ratio = {
            (row["OP Type"], row["Core Type"]): float(row["Ratio(%)"])
            for row in csv.DictReader(handle)
        }
    total = table.total_operator_time_us()
    for record in table.records:
        key = (record.operator, record.attributes.get("core_type", ""))
        expected = profiler_ratio[key]
        actual = record.total_time_us.value / total * 100
        assert actual == pytest.approx(expected, abs=0.001), key


def test_pipe_fractions_follow_export_columns(summary_tables):
    """46 列导出有 mac/mte/cube_utilization；33 列精简导出没有这些分列。

    两种导出都必须可解析：缺列时指标缺席并被记录，绝不能补 0 或估值。
    """
    _, table = summary_tables[0]
    fused = next(r for r in table.records if r.operator == "FusedInferAttentionScore")
    missing = set(table.meta.extra.get("missing_extra_metrics") or ())
    if "mac_time_us" in missing:
        # 33 列：无 mac / mte / cube_utilization 分列
        assert "mac_time_us" not in fused.extra
        assert "cube_utilization" not in fused.extra
        assert "mte2_time_us" not in fused.extra
        # 但 aicore/aiv 流水时间仍可用于诊断，且必须 > 0
        assert fused.extra["aicore_time_us"].value > 0
        assert any(w.code == "missing_extra_metric" for w in table.meta.warnings)
    else:
        assert fused.extra["mac_time_us"].value > 0
        assert fused.extra["cube_utilization"].unit == "%"
        # aic_ 与 aiv_ 两条 MTE2 流水必须合并
        if "mte2_time_us" in fused.extra:
            assert fused.extra["mte2_time_us"].value > 0


def test_diagnosis_on_real_data_stays_hypothesis(summary_tables):
    ruleset = RuleSet.load(RULES)
    engine = DiagnosisEngine(ruleset)
    _, table = summary_tables[0]
    report = engine.diagnose_table(table)
    summary = report.summary()

    assert summary["operators_analyzed"] == len(table.records)
    assert summary["candidates_total"] > 0
    # 阈值未校准 -> 不允许出现 confirmed
    assert summary["candidates_by_level"].get("confirmed", 0) == 0
    assert all(c.level is ConfidenceLevel.HYPOTHESIS for c in report.candidates())
    # 缺指标必须被显式报告
    assert summary["blocked_rules"]
    assert summary["missing_facts_top"]

    bottlenecks = set(summary["candidates_by_bottleneck"])
    # 通信时长按 ns 修正后不再必然触发 communication_bound（此前是单位错误导致的误判）
    assert bottlenecks
    assert "compute_bound" in bottlenecks

    opportunities = opportunities_from_diagnosis(report)
    assert opportunities
    assert all(o.missing_evidence for o in opportunities if o.evidence_level is ConfidenceLevel.HYPOTHESIS)


def test_no_rows_are_silently_dropped(summary_tables):
    for rank, table in summary_tables:
        path = _output(rank, "op_summary")
        disk_rows = sum(1 for _ in path.open(encoding="utf-8", errors="replace")) - 1
        assert table.meta.row_count == disk_rows
        assert table.meta.skipped_rows == 0
        assert not [w for w in table.meta.warnings if w.code == "row_skipped"]


# ---------------------------------------------------------------------------
# 增量 2：轮次稳态窗口 / 多 Workload 对比
# ---------------------------------------------------------------------------
from opt_discovery.analyzer.compare import compare_tables  # noqa: E402
from opt_discovery.analyzer.hotspot import hotspot_matrix  # noqa: E402
from opt_discovery.analyzer.window import select_rounds_window  # noqa: E402
from opt_discovery.collectors import discover_all  # noqa: E402

EXPORTS_ROOT = _DATA_ROOT
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"


@pytest.fixture(scope="module")
def bundles():
    items = {b.workload_id: b for b in discover_all(WORKLOAD_DIR, EXPORTS_ROOT)}
    if len(items) < 4:
        pytest.skip(f"需要 4 组导出，实际发现 {sorted(items)}")
    return items


def test_all_four_exports_are_discovered(bundles):
    assert len(bundles) == 4
    for workload_id, bundle in bundles.items():
        assert bundle.run_log is not None, workload_id
        assert bundle.run_log.round_indices == (0, 1, 2, 3, 4)
        assert len(bundle.ranks) >= 1
        # 缺失 rank 的导出必须被记录，不能静默当作只有一个 rank
        for note in bundle.notes:
            assert any(key in note for key in ("跳过", "切片", "回填"))

    # 两个 rank 必须都在；某个 rank 缺 CSV 时必须保留并标注可回填，不能静默丢弃。
    # 2026-11-15 补采已修复 decode rank1 的 CSV 缺失，因此该场景只在仍然缺 CSV 时要求「回填」标记。
    for workload_id, bundle in bundles.items():
        assert {r.device for r in bundle.ranks} == {"device_0", "device_1"}, workload_id
        for rank in bundle.ranks:
            assert rank.op_summary_files or rank.op_report_db is not None, (workload_id, rank.rank)
        missing_csv = [r for r in bundle.ranks if not r.op_summary_files]
        if missing_csv:
            assert any("回填" in note for note in bundle.notes), workload_id
            assert all(r.op_report_db is not None for r in missing_csv), workload_id


def test_round_window_shrinks_prefill_to_steady_state(bundles):
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    full = parser.parse(bundles["qwen35-35b-a3b-prefill-eager"].rank(0).op_summary_files,
                        bundles["qwen35-35b-a3b-prefill-eager"].workload)
    bundle = bundles["qwen35-35b-a3b-prefill-eager"]
    window = select_rounds_window(bundle.run_log, (1, 2, 3, 4))
    steady = parser.parse(bundle.rank(0).op_summary_files, bundle.workload, window=window)

    assert steady.meta.row_count == full.meta.row_count
    assert 0 < steady.meta.extra["rows_outside_window"] < steady.meta.row_count
    assert steady.total_operator_time_us() < full.total_operator_time_us()
    quality = steady.meta.extra["window_quality"]
    # COMMUNICATION 行的 Duration 修正为 ns 后，kernel 忙碌占比回落到 ~0.17
    assert quality["busy_ratio"] > 0.1
    assert quality["max_gap_us"] < 100_000  # 窗口内没有 >100ms 的空洞
    assert steady.meta.extra["window"]["provenance"] == "measured"


def test_decode_window_on_sliced_export(bundles):
    """decode 的 op_summary 可能按 100 万行切片，窗口解析必须跨全部切片生效。"""
    bundle = bundles["qwen35-35b-a3b-decode-graph"]
    rank = bundle.rank(0)
    on_disk = sorted(
        (rank.directory / "mindstudio_profiler_output").glob("op_summary*.csv")
    )
    assert on_disk, "decode 应至少有一个 op_summary 分片"
    # 不能被字典序或只取第一个文件的影响漏读分片
    assert list(rank.op_summary_files) == on_disk
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    window = select_rounds_window(bundle.run_log, (1, 2))
    table = parser.parse(rank.op_summary_files, bundle.workload, window=window)

    # row_count 是「读到的总行数」，可与磁盘行数独立核对（漏读/重复读都会不等）
    disk_rows = sum(
        sum(1 for _ in path.open(encoding="utf-8", errors="replace")) - 1 for path in on_disk
    )
    assert table.meta.row_count == disk_rows
    assert table.meta.row_count > 100_000
    assert 0 < table.meta.extra["rows_outside_window"] < table.meta.row_count
    assert table.meta.skipped_rows == 0
    assert table.meta.extra["window"]["rounds"] == [1, 2]
    # 窗口只覆盖 round1-2，累计时间必然小于窗口外+窗口内全部
    assert table.total_operator_time_us() is not None


def test_hotspot_shifts_between_eager_and_graph(bundles):
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    tables = {}
    for workload_id, bundle in bundles.items():
        if "prefill" not in workload_id:
            continue
        window = select_rounds_window(bundle.run_log, (1, 2, 3, 4))
        tables[workload_id] = parser.parse(
            bundle.rank(0).op_summary_files, bundle.workload, window=window
        )
    matrix = hotspot_matrix(tables, top=10)
    assert len(matrix["workloads"]) == 2
    rows = {row["operator"]: row for row in matrix["rows"]}
    # 矩阵每行都必须覆盖全部 Workload 列（缺失算子显式为 None，不能被悄悄省略）
    assert all(set(row["workloads"]) == set(matrix["workloads"]) for row in rows.values())
    # 通信时长按 ns 修正后不再主导 top，但仍应存在于算子表中
    assert any(
        record.operator.startswith("hcom_")
        for table in tables.values()
        for record in table.records
    )

    eager = "qwen35-35b-a3b-prefill-eager"
    graph = "qwen35-35b-a3b-prefill-graph"
    report = compare_tables(tables[eager], tables[graph], top=10)
    assert report.comparable is True
    assert report.same_window is True
    # Graph 模式下 op_summary 暴露的算子更少（部分被捕获/融合）
    assert len(tables[graph].records) <= len(tables[eager].records)
    assert any("执行模式不同" in warning for warning in report.warnings)


# ---------------------------------------------------------------------------
# CSV 导出缺失时的 sqlite 回填路径
# ---------------------------------------------------------------------------
from opt_discovery.parsers.sqlite_op_report import SqliteOpReportParser  # noqa: E402


def test_sqlite_fallback_equals_exported_op_statistic():
    """rank0 同时有 CSV 和 sqlite：回填结果必须与导出完全一致，否则不能用它补 rank1。"""
    from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser

    decode_root = _DATA_ROOT / "qwen35-decode"
    if not decode_root.is_dir():
        pytest.skip("qwen35-decode 导出不存在")
    rank0 = sorted(decode_root.glob("PROF_*"))[0]

    workload = WorkloadSpec.from_yaml(WORKLOAD)
    parser = MappingCsvParser(CsvMapping.load(STATISTIC_MAPPING))
    csv_table = parser.parse(
        sorted((rank0 / "mindstudio_profiler_output").glob("op_statistic*.csv"))[0], workload
    )
    sqlite_table = SqliteOpReportParser().parse(rank0, workload)

    csv_map = {
        (r.operator, r.attributes.get("core_type", "")): (r.calls, r.total_time_us.value)
        for r in csv_table.records
    }
    sql_map = {
        (r.operator, r.attributes.get("core_type", "")): (r.calls, r.total_time_us.value)
        for r in sqlite_table.records
    }
    assert set(csv_map) == set(sql_map)
    for key in csv_map:
        assert sql_map[key][0] == csv_map[key][0], key
        assert sql_map[key][1] == pytest.approx(csv_map[key][1], rel=1e-6), key


def test_sqlite_fallback_fills_rank1_without_csv():
    """缺 CSV 的 rank 必须能用 device_*/sqlite 回填出同一口径的表。

    2026-11-15 统一补采后两个 rank 都有 CSV，历史场景无法在该数据上复现；
    此时显式 skip 并指向 CPU 回归（tests/regression/test_r05_rank_merge.py），
    不允许用「有 CSV 的 rank」冒充回填路径。
    """
    decode_root = _DATA_ROOT / "qwen35-decode"
    if not decode_root.is_dir():
        pytest.skip("qwen35-decode 导出不存在")
    candidates = [
        prof
        for prof in sorted(decode_root.glob("PROF_*"))
        if prof.is_dir()
        and not list((prof / "mindstudio_profiler_output").glob("op_statistic*.csv"))
        and list(prof.glob("device_*/sqlite/op_counter.db"))
    ]
    if not candidates:
        # 本次导出所有 rank 都有 CSV：历史场景不适用。这不等于「已验证回填可用」，
        # 因此改为断言回填路径的前提——有 CSV 的 rank 也必须带 sqlite。
        # 无 CSV 的完整回填场景由 CPU 回归 tests/regression/test_r05_rank_merge.py 覆盖。
        for prof in sorted(decode_root.glob("PROF_*")):
            csvs = list((prof / "mindstudio_profiler_output").glob("op_statistic*.csv"))
            if not csvs:
                continue
            assert list(prof.glob("device_*/sqlite/op_counter.db")), (
                f"{prof}: 有 CSV 但无 sqlite，回填路径不可用"
            )
        return
    rank1 = candidates[-1]

    workload = WorkloadSpec.from_yaml(WORKLOAD)
    table = SqliteOpReportParser().parse(rank1, workload)
    assert len(table.records) == 70
    assert table.meta.parser == "ascend_op_report_sqlite"
    codes = {w.code for w in table.meta.warnings}
    assert {"sqlite_fallback", "no_pipe_metrics", "no_timestamps"} <= codes
    assert table.total_operator_time_us() is not None
