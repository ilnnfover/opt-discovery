"""AE-06 真实数据回归：统一补采身份、窗口与 rank 可用性。

真实数据只由独立运行的 Codex 执行；本文件不在开发环境中读取大导出。
数据根目录来自 ``OPT_DISCOVERY_DATA_ROOT``，仓库内只保留独立核对的逻辑
manifest（``configs/collection_fixtures/``），不保存机器路径。

读取策略：四组身份/窗口/rank 只读小文件（workload.log、manifest、目录布局）；
另外只对 prefill 的代表窗口解析 op_summary，并对少量 rank 0 的 sqlite 读
Host/通信表；不对 decode 或全部四组跑完整 analyze。
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from opt_discovery.analyzer.comm import summarize_comm
from opt_discovery.analyzer.graph_coverage import summarize_coverage
from opt_discovery.analyzer.timeline import summarize
from opt_discovery.analyzer.window import select_rounds_window
from opt_discovery.collectors import discover_all
from opt_discovery.collectors.mstx import load_mstx
from opt_discovery.events import ns_from_us
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_real  # noqa: E402

DATA_ROOT = Path(os.environ.get("OPT_DISCOVERY_DATA_ROOT", "."))
FIXTURE = verify_real.load_manifest_fixture()
RUNS: dict[str, dict] = dict(FIXTURE["runs"])
EXPORTS = {workload_id: spec["export_dir"] for workload_id, spec in RUNS.items()}

WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"
SUMMARY_MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_summary.yaml"

pytestmark = pytest.mark.skipif(
    not all((DATA_ROOT / label).is_dir() for label in EXPORTS.values()),
    reason="四组统一补采导出未全部就绪（应通过 verify-real / OPT_DISCOVERY_DATA_ROOT 提供）",
)


@pytest.fixture(scope="module")
def bundles():
    items = {bundle.workload_id: bundle for bundle in discover_all(WORKLOAD_DIR, DATA_ROOT)}
    assert set(items) == set(EXPORTS), f"发现的 Workload 不完整: {sorted(items)}"
    return items


def _manifest_for(export_dir: Path) -> dict:
    log_path = export_dir / "workload.log"
    assert log_path.is_file(), f"{export_dir}: 缺 workload.log"
    manifest_path = log_path.resolve().parent / "collection_manifest.json"
    assert manifest_path.is_file(), (
        f"{export_dir}: workload.log 指向的采集目录缺 collection_manifest.json"
    )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


_MODEL_ID_EXCLUDED = {"4294967295", "-1", "N/A"}


def _db_for(bundle) -> Path:
    """rank 0 目录直属的合并库；不是 ``device_0/sqlite`` 下的库。"""
    dbs = sorted(bundle.rank(0).directory.glob("msprof_*.db"))
    assert dbs, f"{bundle.export_dir}: rank0 目录下缺 msprof_*.db"
    return dbs[0]


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)


def _number(raw: str | None) -> float | None:
    text = (raw or "").strip().replace(",", "")
    if not text or text.lower() in {"-", "--", "n/a", "na", "none", "null", "nan"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _graph_oracle(
    files, window
) -> tuple[int, int, Decimal, Decimal]:
    """独立读原始 CSV：Model ID 有效行数/总行数与时长占比。

    与 Parser 使用同一窗口口径（按 ``Task Start Time(us)`` 起点筛选）；
    COMMUNICATION 行的 ``Task Duration(us)`` 源单位是 ns，需 /1000。
    """
    total_calls = in_graph_calls = 0
    total_us = Decimal(0)
    in_graph_us = Decimal(0)
    for path in files:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                operator = (row.get("OP Type") or "").strip()
                if not operator:
                    continue
                start = _number(row.get("Task Start Time(us)"))
                duration = _number(row.get("Task Duration(us)"))
                if start is None or duration is None or duration < 0.0:
                    continue
                if not (window.start_us <= start <= window.end_us):
                    continue
                if (row.get("Task Type") or "").strip().upper() == "COMMUNICATION":
                    duration /= 1000.0
                duration_us = Decimal(str(duration))
                total_calls += 1
                total_us += duration_us
                model_id = (row.get("Model ID") or "").strip()
                if model_id and model_id not in _MODEL_ID_EXCLUDED:
                    in_graph_calls += 1
                    in_graph_us += duration_us
    return total_calls, in_graph_calls, total_us, in_graph_us


@pytest.mark.parametrize("workload_id", sorted(RUNS))
def test_manifest_identity_matches_checked_in_fixture(workload_id: str, bundles) -> None:
    """每组必须能由 workload.log 找到采集 manifest，且身份/契约与 fixture 一致。"""
    spec = RUNS[workload_id]
    manifest = _manifest_for(DATA_ROOT / spec["export_dir"])
    problems = verify_real.manifest_identity_problems(manifest, workload_id, fixture=FIXTURE)
    assert problems == [], f"{workload_id}: " + "; ".join(problems)

    bundle = bundles[workload_id]
    assert bundle.workload.verified is True
    assert bundle.workload.labels.get("collection_run_id") == spec["run_id"]
    assert bundle.run_log is not None
    assert bundle.run_log.round_indices == tuple(FIXTURE["active_rounds"])


@pytest.mark.parametrize("workload_id", sorted(RUNS))
def test_four_exports_have_two_complete_ranks(workload_id: str, bundles) -> None:
    """rank 可用性：每组必须有两个可解析 rank，不能把空 PROF 目录当第三张卡。"""
    spec = RUNS[workload_id]
    bundle = bundles[workload_id]
    assert {rank.device for rank in bundle.ranks} == set(spec["ranks"])
    for rank in bundle.ranks:
        assert rank.op_summary_files or rank.op_report_db is not None, (
            workload_id,
            rank.rank,
        )
    # 统一补采后两个 rank 都有 op_summary CSV；若只剩 sqlite 回填，会在
    # tests/test_real_data.py 的 SKIPPED 场景里显式报告，而不是静默通过。
    for rank in bundle.ranks:
        assert rank.op_summary_files, f"{workload_id} rank{rank.rank}: 缺 op_summary CSV"

    manifest = _manifest_for(DATA_ROOT / spec["export_dir"])
    rank_sources = {rank.device: rank.directory for rank in bundle.ranks}
    problems = verify_real.rank_layout_problems(
        DATA_ROOT / spec["export_dir"], manifest, rank_sources
    )
    assert problems == [], f"{workload_id}: " + "; ".join(problems)


@pytest.fixture(scope="module")
def prefill_eager_window(bundles):
    bundle = bundles["qwen35-35b-a3b-prefill-eager"]
    window = select_rounds_window(bundle.run_log, FIXTURE["window_rounds"])
    return bundle, window


def test_representative_prefill_window_is_measured_and_bounded(
    prefill_eager_window,
) -> None:
    """代表性真实窗口：busy+idle=window，轮次来自日志实测，不混用历史口径。"""
    bundle, window = prefill_eager_window
    assert window.rounds == tuple(FIXTURE["window_rounds"])
    assert window.provenance == "measured"
    assert window.duration_us > 0.0
    assert window.start_us == min(start for _, start, _ in window.round_spans)
    assert window.end_us == max(end for _, _, end in window.round_spans)
    by_index = {round_.index: round_ for round_ in bundle.run_log.rounds}
    assert window.round_spans == tuple(
        (index, by_index[index].start_us, by_index[index].end_us)
        for index in window.rounds
    )

    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    table = parser.parse(
        bundle.rank(0).op_summary_files, bundle.workload, window=window
    )

    assert table.total_operator_time_us() > 0.0
    assert table.meta.extra["window"]["rounds"] == list(window.rounds)
    assert table.meta.extra["rows_outside_window"] > 0
    quality = table.meta.extra["window_quality"]
    span = window.duration_us
    # 浮点容差：busy/idle 由同一批逐行时长累计，允许 1e-6 相对误差
    assert quality["busy_us"] + quality["underfeed_ratio"] * span == pytest.approx(span, rel=1e-6)
    assert 0.0 <= quality["busy_us"] <= span
    counts = table.meta.extra["round_row_counts"]
    assert set(counts) == set(window.rounds)
    assert all(count > 0 for count in counts.values()), counts


def test_representative_graph_coverage_reports_task_and_time_separately(
    bundles,
) -> None:
    """Graph 覆盖率必须同时给出任务占比与时间占比，不能互相替代。"""
    bundle = bundles["qwen35-35b-a3b-prefill-graph"]
    window = select_rounds_window(bundle.run_log, FIXTURE["window_rounds"])
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    table = parser.parse(
        bundle.rank(0).op_summary_files, bundle.workload, window=window
    )

    coverage = summarize_coverage(table)
    payload = coverage.to_dict()
    assert coverage.available is True
    assert payload["total_calls"] > 0
    assert payload["total_us"] > 0
    assert 0.0 <= payload["task_ratio"] <= 1.0
    assert 0.0 <= payload["time_ratio"] <= 1.0

    # 独立 oracle：直接读原始 CSV 行，按同一窗口起点筛选并修正 COMMUNICATION 单位。
    oracle_calls, oracle_in_graph, oracle_us, oracle_in_graph_us = _graph_oracle(
        bundle.rank(0).op_summary_files, window
    )
    assert oracle_calls > 0
    assert 0 < oracle_in_graph < oracle_calls

    assert coverage.total_calls == oracle_calls
    assert coverage.in_graph_calls == oracle_in_graph
    assert coverage.total_us == pytest.approx(
        float(oracle_us), rel=1e-6, abs=1e-3
    )
    assert coverage.in_graph_us == pytest.approx(
        float(oracle_in_graph_us), rel=1e-6, abs=1e-3
    )
    assert coverage.task_ratio == pytest.approx(
        oracle_in_graph / oracle_calls, rel=1e-12
    )
    assert coverage.time_ratio == pytest.approx(
        float(oracle_in_graph_us / oracle_us), rel=1e-6
    )
    # 两项口径必须独立：该样本任务占比约 47%、时间占比约 57%
    assert abs(coverage.time_ratio - coverage.task_ratio) > 0.01


def test_host_acl_busy_union_matches_cann_api_window_scan(bundles) -> None:
    """prefill eager rank 0：CANN_API 完整区间端点扫描的并集 == Timeline busy。"""
    bundle = bundles["qwen35-35b-a3b-prefill-eager"]
    assert bundle.run_log is not None
    window = select_rounds_window(bundle.run_log, FIXTURE["window_rounds"])
    db = _db_for(bundle)

    low = ns_from_us(window.start_us)
    high = ns_from_us(window.end_us)
    events: dict[int, int] = defaultdict(int)
    calls = 0
    naive_ns = 0
    with _connect_ro(db) as connection:
        rows = connection.execute(
            "SELECT startNs, endNs FROM CANN_API WHERE endNs >= ? AND startNs <= ?",
            (low, high),
        )
        for start_ns, end_ns in rows:
            calls += 1
            naive_ns += end_ns - start_ns
            events[start_ns] += 1
            events[end_ns] -= 1

    union_ns = 0
    active = 0
    previous = None
    for point, delta in sorted(events.items()):
        if previous is not None and active > 0:
            union_ns += point - previous
        previous = point
        active += delta

    # 必须真的与窗口相交，而不是只验证全部调用
    assert calls > 0
    assert union_ns > 0
    assert union_ns < naive_ns, "并集应小于逐条累加（调用区间存在重叠）"

    summary = summarize(db, window, mstx=load_mstx(db, window))
    assert summary.host_acl_calls == calls
    assert summary.host_acl_busy_us == pytest.approx(union_ns / 1000.0, abs=0.001)


def test_decode_task_counts_differ_but_deduped_bytes_are_comparable(bundles) -> None:
    """验证新基线的重复 plane、图任务合并和两侧去重字节量。"""
    totals = {}
    for suffix in ("decode-eager", "decode-graph"):
        bundle = bundles[f"qwen35-35b-a3b-{suffix}"]
        db = _db_for(bundle)

        caps: dict[int, int] = {}
        records = 0
        with _connect_ro(db) as connection:
            rows = connection.execute(
                "SELECT globalTaskId, size FROM COMMUNICATION_TASK_INFO"
            )
            for global_task_id, size in rows:
                records += 1
                if global_task_id not in caps or size > caps[global_task_id]:
                    caps[global_task_id] = size

        summary = summarize_comm(db)
        assert summary.available is True
        assert (summary.records, summary.unique_tasks, summary.bytes_total) == (
            records,
            len(caps),
            float(sum(caps.values())),
        )
        totals[suffix] = summary

    eager = totals["decode-eager"]
    graph = totals["decode-graph"]
    assert eager.records == eager.unique_tasks
    assert graph.records > graph.unique_tasks * 2
    assert eager.unique_tasks > graph.unique_tasks * 2
    assert eager.bytes_total == pytest.approx(graph.bytes_total, rel=0.15)
