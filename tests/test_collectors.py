"""Harness-log parsing, export discovery, and round-window parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO_ROOT

from opt_discovery.analyzer.window import (
    IntervalUnion,
    WindowError,
    select_rounds_window,
)
from opt_discovery.collectors.profiler_dir import ExportError, discover_bundle
from opt_discovery.collectors.workload_log import LogParseError, RunLog
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser

SUMMARY_MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_summary.yaml"

LOG_TEXT = """\
PROMPT_TOKENS 8 LOAD_SECONDS 1.5
PROFILE_START_ABS 1000.0
WARMUP_DONE_ABS 999.5
ACTIVE_ROUND 0 start 1000.0 end 1001.0 sample 'x'
ACTIVE_ROUND 1 start 1001.0 end 1002.0 sample 'x'
ACTIVE_ROUND 2 start 1002.0 end 1004.0 sample 'x'
"""

HEADER = "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us),Input Shapes\n"
# (start us, duration us)
ROWS = [
    (1000_100_000, 1000.0),  # round 0 (excluded by default window)
    (1001_100_000, 2000.0),  # round 1
    (1001_102_000, 2000.0),  # round 1 (touching)
    (1003_100_000, 4000.0),  # round 2 after a gap
]


def _write_export(tmp_path: Path, *, rows=ROWS, slices: int = 1, log: str = LOG_TEXT) -> Path:
    export = tmp_path / "export"
    export.mkdir()
    if log:
        (export / "workload.log").write_text(log, encoding="utf-8")
    prof = export / "PROF_000001_test"
    output = prof / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    (prof / "device_0").mkdir()
    (output / "op_statistic_1.csv").write_text(
        "Device_id,OP Type,Core Type,Count,Total Time(us)\n0,Add,AI_CORE,2,3000.0\n",
        encoding="utf-8",
    )
    per_slice = max(1, len(rows) // slices) if slices > 1 else len(rows)
    for index in range(slices):
        chunk = rows[index * per_slice : (index + 1) * per_slice] if slices > 1 else rows
        name = f"op_summary_slice_{index}_1.csv" if slices > 1 else "op_summary_1.csv"
        body = "".join(
            f'0,Op{index},AI_CORE,{start},{duration},"8,16"\n'
            for start, duration in chunk
        )
        (output / name).write_text(HEADER + body, encoding="utf-8")
    return export


# ---------------------------------------------------------------------------
# workload.log
# ---------------------------------------------------------------------------
def test_run_log_parses_rounds_and_params(tmp_path):
    path = tmp_path / "workload.log"
    path.write_text(LOG_TEXT, encoding="utf-8")
    run_log = RunLog.parse(path)
    assert run_log.prompt_tokens == 8
    assert run_log.load_seconds == pytest.approx(1.5)
    assert run_log.round_indices == (0, 1, 2)
    assert run_log.rounds[0].start_us == pytest.approx(1000.0 * 1e6)
    assert run_log.rounds[2].duration_us == pytest.approx(2.0 * 1e6)


def test_run_log_without_rounds_is_rejected(tmp_path):
    path = tmp_path / "workload.log"
    path.write_text("PROMPT_TOKENS 8\n", encoding="utf-8")
    with pytest.raises(LogParseError):
        RunLog.parse(path)


def test_missing_round_is_reported_not_approximated(tmp_path):
    path = tmp_path / "workload.log"
    path.write_text(LOG_TEXT, encoding="utf-8")
    run_log = RunLog.parse(path)
    with pytest.raises(WindowError) as exc:
        select_rounds_window(run_log, (3, 4))
    assert "不存在" in str(exc.value)


def test_round_window_uses_harness_timestamps(tmp_path):
    path = tmp_path / "workload.log"
    path.write_text(LOG_TEXT, encoding="utf-8")
    window = select_rounds_window(RunLog.parse(path), (1, 2))
    assert window.rounds == (1, 2)
    assert window.start_us == pytest.approx(1001.0 * 1e6)
    assert window.end_us == pytest.approx(1004.0 * 1e6)
    assert window.policy == "rounds"
    assert window.provenance == "measured"
    assert window.contains(1002_500_000)
    assert not window.contains(1000_500_000)


# ---------------------------------------------------------------------------
# 导出发现
# ---------------------------------------------------------------------------
def test_discover_bundle_finds_rank_and_files(tmp_path):
    export = _write_export(tmp_path, slices=2)
    workload = WorkloadSpecFactory()
    (tmp_path / "wl.yaml").write_text(workload, encoding="utf-8")
    bundle = discover_bundle(tmp_path / "wl.yaml", export)
    assert len(bundle.ranks) == 1
    rank = bundle.rank(0)
    assert len(rank.op_summary_files) == 2  # 切片按序合并
    assert rank.op_summary_files[0].name.endswith("slice_0_1.csv")
    assert rank.op_statistic_file is not None
    assert bundle.run_log is not None
    assert any("切片" in note for note in bundle.notes)


def test_discover_rejects_directory_without_prof(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (tmp_path / "wl.yaml").write_text(WorkloadSpecFactory(), encoding="utf-8")
    with pytest.raises(ExportError):
        discover_bundle(tmp_path / "wl.yaml", empty)


def WorkloadSpecFactory() -> str:  # noqa: N802 - test helper
    return (
        "workload_id: wl-test\n"
        "model: {name: m}\n"
        "hardware: {device: d}\n"
        "execution: {phase: prefill, mode: eager}\n"
    )


# ---------------------------------------------------------------------------
# 窗口解析
# ---------------------------------------------------------------------------
def test_parse_with_window_filters_before_aggregation(tmp_path):
    export = _write_export(tmp_path)
    (tmp_path / "wl.yaml").write_text(WorkloadSpecFactory(), encoding="utf-8")
    bundle = discover_bundle(tmp_path / "wl.yaml", export)
    window = select_rounds_window(bundle.run_log, (1, 2))

    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    table = parser.parse(bundle.rank(0).op_summary_files, bundle.workload, window=window)

    assert table.meta.row_count == 4
    assert table.meta.extra["rows_outside_window"] == 1
    assert table.meta.skipped_rows == 0
    # 窗口内只有 Op0（round1 的两行 + round2 的一行），共 3 次调用
    assert len(table.records) == 1
    assert table.records[0].calls == 3
    assert table.total_operator_time_us() == pytest.approx(8000.0)

    quality = table.meta.extra["window_quality"]
    assert quality["span_us"] == pytest.approx(3.0 * 1e6)
    assert quality["busy_us"] == pytest.approx(8000.0)
    assert quality["gap_count"] == 1
    # round1 的两行首尾相接会被合并，round2 与之间隔 1.996s
    assert quality["max_gap_us"] == pytest.approx(1_996_000.0)
    assert quality["degraded"] is True
    assert table.meta.extra["window"]["rounds"] == [1, 2]


def test_parse_multiple_slices_are_merged(tmp_path):
    export = _write_export(tmp_path, slices=2)
    (tmp_path / "wl.yaml").write_text(WorkloadSpecFactory(), encoding="utf-8")
    bundle = discover_bundle(tmp_path / "wl.yaml", export)
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    table = parser.parse(bundle.rank(0).op_summary_files, bundle.workload)
    assert table.meta.row_count == 4
    assert len(table.meta.source_files) == 2
    assert sum(r.calls for r in table.records) == 4


def test_window_requires_start_time_column(tmp_path):
    export = _write_export(tmp_path)
    (tmp_path / "wl.yaml").write_text(WorkloadSpecFactory(), encoding="utf-8")
    bundle = discover_bundle(tmp_path / "wl.yaml", export)
    window = select_rounds_window(bundle.run_log, (1, 2))

    mapping_data = {
        "parser": "no_start",
        "row_semantics": "instance",
        "columns": {
            "operator": {"names": ["OP Type"], "required": True},
            "total_time_us": {"names": ["Task Duration(us)"], "unit": "us", "required": True},
        },
    }
    import yaml

    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(mapping_data), encoding="utf-8")
    parser = MappingCsvParser(CsvMapping.load(path))
    with pytest.raises(Exception) as exc:
        parser.parse(bundle.rank(0).op_summary_files, bundle.workload, window=window)
    assert "row_start_time" in str(exc.value)


def test_interval_union_flags_degraded_window():
    union = IntervalUnion()
    union.add(0.0, 1000.0)
    union.add(500_000.0, 501_000.0)  # 499ms 空洞
    quality = union.finalize(1_000_000.0, max_gap_us=100_000.0)
    assert quality.gap_count == 1
    assert quality.max_gap_us == pytest.approx(499_000.0)
    assert quality.degraded is True
    assert any("空洞" in reason for reason in quality.reasons)


def test_interval_union_detects_out_of_order():
    union = IntervalUnion()
    union.add(2000.0, 3000.0)
    union.add(1000.0, 1500.0)
    quality = union.finalize(5000.0)
    assert quality.degraded
    assert any("乱序" in reason for reason in quality.reasons)
