"""R01：``op_summary`` 百万行分片必须按自然序全部读取。

历史错误（README 已知约束 1）：decode 每 rank 2 片，按字典序或只读第一片会漏掉一半数据。
不变量 INV-12：大导出必须流式/分片处理，但不能因此漏读或重复读。
"""

from __future__ import annotations

from pathlib import Path

from _helpers import SUMMARY_MAPPING, WORKLOADS, make_workload, summary_row, write_op_summary
from opt_discovery.collectors import discover_bundle
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser

WORKLOAD_YAML = WORKLOADS / "prefill_eager.yaml"


def _export(tmp_path: Path, slices: dict[str, list[list[object]]]) -> Path:
    output = tmp_path / "export" / "PROF_000001_fixture" / "mindstudio_profiler_output"
    for name, rows in slices.items():
        write_op_summary(output / name, rows)
    (output.parent / "device_0").mkdir(parents=True, exist_ok=True)
    return tmp_path / "export"


def _totals(table) -> dict[str, tuple[int, float]]:
    return {
        record.operator: (record.calls, record.total_time_us.value)
        for record in table.records
        if record.total_time_us is not None
    }


def test_natural_order_keeps_slice_10_after_slice_2(tmp_path: Path) -> None:
    """字典序会把 slice_10 排到 slice_2 前面；自然序必须按数值。"""
    export = _export(
        tmp_path,
        {
            "op_summary_slice_1.csv": [summary_row("MatMul", 10.0, start_us=0)],
            "op_summary_slice_2.csv": [summary_row("MatMul", 10.0, start_us=100)],
            "op_summary_slice_10.csv": [summary_row("MatMul", 10.0, start_us=200)],
        },
    )
    bundle = discover_bundle(WORKLOAD_YAML, export)
    names = [path.name for path in bundle.rank(0).op_summary_files]

    assert names == [
        "op_summary_slice_1.csv",
        "op_summary_slice_2.csv",
        "op_summary_slice_10.csv",
    ]
    # 反例：字典序会得到 1, 10, 2
    assert names != sorted(names)


def test_sliced_aggregation_equals_single_file(tmp_path: Path) -> None:
    """分片聚合必须与「同样的行放在一个文件里」逐键等价。"""
    rows: list[list[object]] = [
        summary_row("MatMul", 100.0, start_us=0),
        summary_row("MatMul", 100.0, start_us=100),
        summary_row("Add", 50.0, start_us=200),
        summary_row("MatMul", 100.0, start_us=300),
        summary_row("Add", 60.0, start_us=400),
        summary_row("Add", 70.0, start_us=500),
    ]
    sliced = _export(
        tmp_path,
        {
            "op_summary_slice_1.csv": rows[:2],
            "op_summary_slice_2.csv": rows[2:4],
            "op_summary_slice_3.csv": rows[4:],
        },
    )
    single = write_op_summary(tmp_path / "single" / "op_summary_slice_1.csv", rows)

    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    workload = make_workload()
    bundle = discover_bundle(WORKLOAD_YAML, sliced)
    merged = parser.parse(bundle.rank(0).op_summary_files, workload)
    whole = parser.parse(single, workload)

    # 期望值来自独立算术，不来自被测聚合
    assert _totals(merged) == {
        "MatMul": (3, 300.0),
        "Add": (3, 180.0),
    }
    assert _totals(merged) == _totals(whole)
    assert merged.meta.row_count == 6
    assert whole.meta.row_count == 6
    assert len(merged.meta.source_files) == 3
    assert merged.meta.skipped_rows == 0


def test_reading_only_first_slice_would_be_caught(tmp_path: Path) -> None:
    """负向：只读第一片（历史错误的形态）时，调用数与累计时间必然对不上。"""
    rows = [
        summary_row("MatMul", 100.0, start_us=0),
        summary_row("MatMul", 100.0, start_us=100),
        summary_row("MatMul", 100.0, start_us=200),
        summary_row("MatMul", 100.0, start_us=300),
    ]
    export = _export(
        tmp_path,
        {
            "op_summary_slice_1.csv": rows[:2],
            "op_summary_slice_2.csv": rows[2:],
        },
    )
    parser = MappingCsvParser(CsvMapping.load(SUMMARY_MAPPING))
    workload = make_workload()
    bundle = discover_bundle(WORKLOAD_YAML, export)
    first_only = parser.parse(bundle.rank(0).op_summary_files[:1], workload)

    assert _totals(first_only) == {"MatMul": (2, 200.0)}
    assert _totals(first_only) != {"MatMul": (4, 400.0)}
