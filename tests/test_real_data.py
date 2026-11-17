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


def _rank_dirs() -> list[Path]:
    return sorted(p for p in PROF_ROOT.glob("PROF_*") if p.is_dir())


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
    ranks = _rank_dirs()
    assert len(ranks) == 2, "TP=2 时应有两个 rank 目录"


def test_op_statistic_total_matches_profiler_sum(statistic_tables):
    for rank, table in statistic_tables:
        with _output(rank, "op_statistic").open(newline="") as handle:
            profiler_total = sum(
                float(row["Total Time(us)"]) for row in csv.DictReader(handle)
            )
        assert table.total_operator_time_us() == pytest.approx(profiler_total, rel=1e-9)
        assert table.meta.skipped_rows == 0
        assert table.meta.row_count == 69


def test_operator_variants_are_split_by_core_type(statistic_tables):
    """MatMulV3 在导出里按 Core Type 拆成两行，聚合后必须是两条记录而不是一条。"""
    _, table = statistic_tables[0]
    variants = [r for r in table.records if r.operator == "MatMulV3"]
    assert len(variants) == 2
    assert {r.attributes["core_type"] for r in variants} == {"AI_CORE", "MIX_AIC"}
    assert sum(r.calls for r in variants) == 771


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


def test_pipe_fractions_are_available_from_summary(summary_tables):
    _, table = summary_tables[0]
    fused = next(r for r in table.records if r.operator == "FusedInferAttentionScore")
    assert fused.extra["mac_time_us"].value > 0
    # aic_ 与 aiv_ 两条 MTE2 流水必须合并
    assert fused.extra["mte2_time_us"].value > 0
    assert fused.extra["cube_utilization"].unit == "%"


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
    for _, table in summary_tables:
        assert table.meta.row_count > 30000
        assert table.meta.skipped_rows == 0
        assert not [w for w in table.meta.warnings if w.code == "row_skipped"]


# ---------------------------------------------------------------------------
# 增量 2：轮次稳态窗口 / 多 Workload 对比
# ---------------------------------------------------------------------------
from opt_discovery.analyzer.compare import compare_tables  # noqa: E402
from opt_discovery.analyzer.hotspot import hotspot_matrix, operator_hotspots  # noqa: E402
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

    # rank1 的 CSV 缺失时应保留该 rank 并标记可回填，而不是静默丢弃
    decode = bundles["qwen35-35b-a3b-decode-eager"]
    assert len(decode.ranks) == 2
    assert any("回填" in note for note in decode.notes)
    missing_csv = [r for r in decode.ranks if not r.op_summary_files]
    assert len(missing_csv) == 1 and missing_csv[0].op_report_db is not None
    assert len(bundles["qwen35-35b-a3b-prefill-eager"].ranks) == 2


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
    """decode 的 op_summary 被切成两个文件，窗口解析必须跨切片生效。"""
    bundle = bundles["qwen35-35b-a3b-decode-graph"]
    rank = bundle.rank(0)
    assert len(rank.op_summary_files) == 2
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    window = select_rounds_window(bundle.run_log, (1, 2))
    table = parser.parse(rank.op_summary_files, bundle.workload, window=window)

    assert table.meta.row_count > 1_000_000
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
    """decode 的 rank1 没有 CSV，但 device_1/sqlite 完整，可回填出同一口径的表。"""
    decode_root = _DATA_ROOT / "qwen35-decode"
    if not decode_root.is_dir():
        pytest.skip("qwen35-decode 导出不存在")
    rank1 = sorted(decode_root.glob("PROF_*"))[-1]
    assert not list((rank1 / "mindstudio_profiler_output").glob("op_statistic*.csv"))

    workload = WorkloadSpec.from_yaml(WORKLOAD)
    table = SqliteOpReportParser().parse(rank1, workload)
    assert len(table.records) == 70
    assert table.meta.parser == "ascend_op_report_sqlite"
    codes = {w.code for w in table.meta.warnings}
    assert {"sqlite_fallback", "no_pipe_metrics", "no_timestamps"} <= codes
    assert table.total_operator_time_us() is not None
