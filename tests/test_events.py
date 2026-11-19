"""AE-04: shared KernelEvent/EventReader contract and timeline migration."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from opt_discovery.analyzer import timeline
from opt_discovery.analyzer.window import Window as AnalyzerWindow
from opt_discovery.events import (
    EventReadError,
    HostCall,
    TaskIntervalEvent,
    iter_host_calls,
    iter_host_spans,
    iter_op_summary_events,
    iter_op_summary_intervals,
    iter_task_intervals,
    iter_task_span_rows,
)
from opt_discovery.primitives import (
    COMMUNICATION_DURATION_SCALE,
    NS_PER_US,
    Interval,
    Window,
)
from opt_discovery.schemas.operator import TaskType

HEADER = "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"


def _write_csv(path: Path, rows: list[list[str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(",".join(row) + "\n" for row in rows)
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def _window(start_us: float = 1050.0, end_us: float = 1250.0) -> Window:
    return Window(start_us=start_us, end_us=end_us, rounds=(1,))


def _db(path: Path, *, host: bool = False) -> Path:
    connection = sqlite3.connect(path)
    if host:
        connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
        connection.execute("create table STRING_IDS (id integer, value text)")
        connection.execute("insert into STRING_IDS values (1, 'launch')")
        connection.execute("insert into CANN_API values (1, 1000000, 1100000)")
        connection.execute("insert into CANN_API values (2, 1300000, 1500000)")
    else:
        connection.execute("create table TASK (startNs integer, endNs integer)")
        connection.execute("insert into TASK values (1000000, 1100000)")
        connection.execute("insert into TASK values (1300000, 1500000)")
    connection.commit()
    connection.close()
    return path


def test_op_summary_reader_filter_and_clip_semantics(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "op_summary_1.csv",
        [
            ["0", "MatMul", "AI_CORE", "1000", "100"],
            ["0", "hcom_allReduce_", "COMMUNICATION", "1100", "50000"],
            ["0", "Add", "AI_CORE", "1300", "200"],
        ],
    )
    window = _window()

    filtered = list(iter_op_summary_events(csv_path, window=window, rank=1))
    assert [event.operator for event in filtered] == ["MatMul", "hcom_allReduce_"]
    assert [event.duration_us for event in filtered] == [100.0, 50.0]
    assert filtered[0].task_type is TaskType.KERNEL
    assert filtered[1].task_type is TaskType.COMMUNICATION
    assert filtered[0].source.kind == "op_summary"
    assert filtered[0].source.row == 2
    assert filtered[0].rank == 1
    assert filtered[0].interval() == Interval(1000.0, 1100.0)

    clipped = list(iter_op_summary_events(csv_path, window=window, clip=True))
    assert [(event.start_us, event.end_us) for event in clipped] == [
        (1050.0, 1100.0),
        (1100.0, 1150.0),
    ]


def test_op_summary_reader_skips_bad_rows_and_is_lazy(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "op_summary_1.csv",
        [
            ["0", "MatMul", "AI_CORE", "1000", "100"],
            ["0", "Bad", "AI_CORE", "not-a-number", "100"],
            ["0", "Zero", "AI_CORE", "1200", "0"],
        ],
    )
    stream = iter_op_summary_events(csv_path)
    assert inspect.isgenerator(stream)
    assert [event.operator for event in stream] == ["MatMul"]


def test_op_summary_missing_time_columns_raises(tmp_path: Path) -> None:
    path = tmp_path / "op_summary_1.csv"
    path.write_text("Device_id,OP Type\n0,MatMul\n", encoding="utf-8")
    with pytest.raises(EventReadError, match="Task Start Time"):
        list(iter_op_summary_events(path))


def test_task_interval_reader_units_and_window(tmp_path: Path) -> None:
    db = _db(tmp_path / "msprof_0.db")
    events = list(iter_task_intervals(db, window=_window(), rank=1))
    assert isinstance(events[0], TaskIntervalEvent)
    assert [(event.start_us, event.end_us) for event in events] == [(1000.0, 1100.0)]
    assert events[0].rank == 1
    assert events[0].source.kind == "task_table"


def test_task_interval_reader_missing_table(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(EventReadError, match="TASK"):
        list(iter_task_intervals(path))


def test_host_call_reader_resolves_names_and_units(tmp_path: Path) -> None:
    db = _db(tmp_path / "msprof_0.db", host=True)
    events = list(iter_host_calls(db, window=_window()))
    assert isinstance(events[0], HostCall)
    assert [(event.name, event.start_us, event.end_us) for event in events] == [
        ("launch", 1000.0, 1100.0)
    ]


def test_timeline_wrappers_share_the_same_semantics(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "op_summary_1.csv",
        [
            ["0", "MatMul", "AI_CORE", "1000", "100"],
            ["0", "hcom_allReduce_", "COMMUNICATION", "1100", "50000"],
        ],
    )
    expected = [
        event.interval() for event in iter_op_summary_events(csv_path, window=_window())
    ]
    assert timeline.load_op_intervals(csv_path, 1050.0, 1250.0) == expected

    db = _db(tmp_path / "host.db", host=True)
    host_expected = [
        (event.interval(), event.name)
        for event in iter_host_calls(db, window=_window())
    ]
    assert timeline.load_host_intervals(db, 1050.0, 1250.0) == host_expected


def test_primitives_are_shared_without_parser_to_analyzer_import() -> None:
    from opt_discovery import primitives

    assert AnalyzerWindow is primitives.Window
    assert primitives.COMMUNICATION_DURATION_SCALE == pytest.approx(0.001)
    assert COMMUNICATION_DURATION_SCALE == primitives.COMMUNICATION_DURATION_SCALE

    import ast

    source_path = (
        Path(__file__).resolve().parent.parent
        / "opt_discovery"
        / "parsers"
        / "csv_mapping.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.startswith("opt_discovery.analyzer") for module in imports)
    assert not any(module == "..analyzer.window" for module in imports)


def test_op_reader_preserves_stream_id_and_unknown_rank(tmp_path: Path) -> None:
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us),Stream ID\n"
        "0,MatMul,AI_CORE,1000,100,17\n"
        "0,Add,AI_CORE,1200,100,18\n",
        encoding="utf-8",
    )
    default = list(iter_op_summary_events(path))
    assert [event.rank for event in default] == [None, None]
    assert [event.stream_id for event in default] == [17, 18]

    explicit = list(iter_op_summary_events(path, rank=1))
    assert [event.rank for event in explicit] == [1, 1]


def test_rows_without_operator_are_not_kernel_events(tmp_path: Path) -> None:
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        "0,,AI_CORE,1000,100\n",
        encoding="utf-8",
    )
    assert list(iter_op_summary_events(path)) == []
    intervals = list(iter_op_summary_intervals(path))
    assert [(item.start_us, item.end_us) for item in intervals] == [(1000.0, 1100.0)]


def test_epoch_short_durations_survive_all_readers(tmp_path: Path) -> None:
    """P1-3：1.79e15 µs epoch 上的 0.1 µs 事件不能被浮点加法吃掉。"""
    epoch = 1_790_000_000_000_000.0
    csv_path = tmp_path / "op_summary_1.csv"
    csv_path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        f"0,MatMul,AI_CORE,{epoch},0.1\n"
        f"0,hcom_allReduce_,COMMUNICATION,{epoch},100\n",
        encoding="utf-8",
    )
    events = list(iter_op_summary_events(csv_path))
    assert [event.duration_us for event in events] == pytest.approx([0.1, 0.1])

    db = tmp_path / "msprof_0.db"
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute(
        "insert into TASK values (?, ?)", (int(epoch * 1000), int(epoch * 1000) + 100)
    )
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.execute("insert into STRING_IDS values (1, 'launch')")
    connection.execute(
        "insert into CANN_API values (1, ?, ?)",
        (int(epoch * 1000), int(epoch * 1000) + 100),
    )
    connection.commit()
    connection.close()

    task = list(iter_task_intervals(db))[0]
    host = list(iter_host_calls(db))[0]
    assert task.duration_us == pytest.approx(0.1)
    assert host.duration_us == pytest.approx(0.1)


def test_timeline_reads_all_op_summary_slices(tmp_path: Path) -> None:
    """P1-2：slice_1/2/10 必须全部读取，而不是只取最大分片。"""
    from opt_discovery import events

    prof = tmp_path / "PROF_000000_test"
    output = prof / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    for name, rows in (
        ("op_summary_slice_1.csv", 2),
        ("op_summary_slice_2.csv", 1),
        ("op_summary_slice_10.csv", 1),
    ):
        body = "".join(
            f"0,op_{index},AI_CORE,{1000 + index * 100},10\n"
            for index in range(rows)
        )
        (output / name).write_text(
            "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n" + body,
            encoding="utf-8",
        )
    (prof / "msprof_0.db").write_bytes(b"")
    window = Window(1000.0, 2000.0, rounds=(1,))
    intervals, source, _csv = timeline._rank_intervals(prof / "msprof_0.db", window)
    assert source == "op_summary"
    assert len(intervals) == 4
    assert sum(item.duration_us for item in intervals) == pytest.approx(40.0)
    assert [path.name for path in events.op_summary_paths(prof / "msprof_0.db")] == [
        "op_summary_slice_1.csv",
        "op_summary_slice_2.csv",
        "op_summary_slice_10.csv",
    ]


def test_point_window_wrappers_keep_historical_filter(tmp_path: Path) -> None:
    """P2-3：load_task_intervals(db, 1, 1) 仍返回覆盖该点的区间。"""
    db = tmp_path / "point.db"
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("insert into TASK values (1000, 1100)")
    connection.commit()
    connection.close()
    intervals = timeline.load_task_intervals(db, 1.0, 1.0)
    assert [(item.start_us, item.end_us) for item in intervals] == [(1.0, 1.1)]


def _prof_with_csv(tmp_path: Path, rows: list[str], name: str = "op_summary_1.csv") -> Path:
    prof = tmp_path / "PROF_000000_test"
    output = prof / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    (output / name).write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    db = prof / "msprof_0.db"
    connection = sqlite3.connect(db)
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.commit()
    connection.close()
    return db


def test_summarize_epoch_short_event_has_busy(tmp_path: Path) -> None:
    """P1-3：epoch 上的 0.1µs 事件在 summarize 里必须产生正的 busy。"""
    epoch = 1_790_000_000_000_000.0
    db = _prof_with_csv(tmp_path, [f"0,MatMul,AI_CORE,{epoch},0.1"])
    window = Window(epoch, epoch + 1.0, rounds=(1,))

    summary = timeline.summarize(db, window)

    assert summary.task_count == 1
    assert summary.device_busy_us == pytest.approx(0.1)
    assert summary.device_idle_us == pytest.approx(0.9)


def test_window_clip_drops_event_at_right_boundary(tmp_path: Path) -> None:
    epoch = 1_790_000_000_000_000.0
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        f"0,MatMul,AI_CORE,{epoch + 2},0.1\n",
        encoding="utf-8",
    )
    window = Window(epoch, epoch + 2.0, rounds=(1,))
    assert list(iter_op_summary_events(path, window=window, clip=True)) == []


def test_summarize_handles_unordered_slices(tmp_path: Path) -> None:
    """P1 · 乱序分片：并集前必须全局按起点排序。"""
    db = _prof_with_csv(
        tmp_path,
        ["0,op_a,AI_CORE,30,10"],
        name="op_summary_slice_1.csv",
    )
    output = db.parent / "mindstudio_profiler_output"
    (output / "op_summary_slice_2.csv").write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        "0,op_b,AI_CORE,10,10\n",
        encoding="utf-8",
    )
    (output / "op_summary_slice_10.csv").write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        "0,op_c,AI_CORE,20,10\n",
        encoding="utf-8",
    )
    window = Window(0.0, 100.0, rounds=(1,))

    summary = timeline.summarize(db, window)

    assert summary.task_count == 3
    assert summary.device_busy_us == pytest.approx(30.0)
    assert summary.gap_count == 0


def test_parser_and_reader_share_epoch_normalization(tmp_path: Path) -> None:
    """P1-1：Parser 与 Reader 对同一 CSV 的实例时长一致。"""
    from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
    from opt_discovery.schemas.workload import WorkloadSpec

    epoch = 1_790_000_000_000_000.0
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        f"0,MatMul,AI_CORE,{epoch},0.1\n"
        f"0,hcom_allReduce_,COMMUNICATION,{epoch},100\n",
        encoding="utf-8",
    )
    workload = WorkloadSpec.from_dict(
        {
            "workload_id": "wl",
            "model": {"name": "m", "dtype": "bfloat16"},
            "hardware": {"device": "Ascend 910B", "device_count": 1},
            "execution": {"phase": "prefill", "mode": "eager"},
            "input": {"batch_size": 1, "input_length": 1024},
            "verified": True,
        }
    )
    parser = MappingCsvParser(
        CsvMapping.load(
            Path(__file__).resolve().parent.parent
            / "configs"
            / "parsers"
            / "ascend_op_summary.yaml"
        )
    )
    table = parser.parse(path, workload)
    parser_total = sum(
        record.total_time_us.value
        for record in table.records
        if record.total_time_us is not None
    )
    reader_total = sum(
        event.duration_us for event in iter_op_summary_events(path)
    )
    assert parser_total == pytest.approx(0.2)
    assert reader_total == pytest.approx(0.2)


def _summary_parser(tmp_path: Path):
    from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
    from opt_discovery.schemas.workload import WorkloadSpec

    parser = MappingCsvParser(
        CsvMapping.load(
            Path(__file__).resolve().parent.parent
            / "configs"
            / "parsers"
            / "ascend_op_summary.yaml"
        )
    )
    workload = WorkloadSpec.from_dict(
        {
            "workload_id": "wl",
            "model": {"name": "m", "dtype": "bfloat16"},
            "hardware": {"device": "Ascend 910B", "device_count": 1},
            "execution": {"phase": "prefill", "mode": "eager"},
            "input": {"batch_size": 1, "input_length": 1024},
            "verified": True,
        }
    )
    return parser, workload


def test_parser_keeps_number_format_zero_and_missing_timestamp(tmp_path: Path) -> None:
    """P1 · Parser 回归：千分位、零时长、N/A 起点的旧语义必须保留。"""
    parser, workload = _summary_parser(tmp_path)
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        '0,MatMul,AI_CORE,"1,020","1,000"\n'
        "0,MatMul,AI_CORE,1500,0\n"
        "0,MatMul,AI_CORE,N/A,100\n",
        encoding="utf-8",
    )
    window = Window(1000.0, 3000.0, rounds=(1,), round_spans=((1, 1000.0, 3000.0),))
    table = parser.parse(path, workload, window=window)

    assert table.records[0].calls == 2
    assert table.meta.skipped_rows == 1
    assert table.meta.extra["rows_outside_window"] == 0
    assert table.meta.extra["round_row_counts"] == {1: 2}

    # 表头缺起点列：保持“跳过 + 该轮 0 行”，不得用合成起点 0 计入窗口
    missing = tmp_path / "op_summary_2.csv"
    missing.write_text(
        "Device_id,OP Type,Task Type,Task Duration(us)\n"
        "0,MatMul,AI_CORE,100\n",
        encoding="utf-8",
    )
    table2 = parser.parse(missing, workload, window=window)
    assert table2.meta.skipped_rows == 1
    assert table2.meta.extra["rows_outside_window"] == 0
    assert table2.meta.extra["round_row_counts"] == {1: 0}


def test_host_union_treats_zero_relative_end_as_valid(tmp_path: Path) -> None:
    """P1 · Host 并集：相对坐标 0 是合法端点，不能当作未初始化。"""
    db = _prof_with_csv(tmp_path, ["0,MatMul,AI_CORE,150,10"])
    connection = sqlite3.connect(db)
    connection.execute(
        "insert into CANN_API values (1, ?, ?)", (90_000, 100_000)
    )
    connection.execute(
        "insert into CANN_API values (1, ?, ?)", (130_000, 140_000)
    )
    connection.commit()
    connection.close()
    window = Window(100.0, 200.0, rounds=(1,))

    summary = timeline.summarize(db, window)

    assert summary.host_acl_calls == 2
    assert summary.host_acl_busy_us == pytest.approx(20.0)


def test_epoch_clip_is_bounded_by_window_end(tmp_path: Path) -> None:
    epoch = 1_790_000_000_000_000.0
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "DeviceId,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        f"0,MatMul,AI_CORE,{epoch},0.3\n".replace("DeviceId", "Device_id"),
        encoding="utf-8",
    )
    window = Window(epoch, epoch + 0.25, rounds=(1,))
    events = list(iter_op_summary_events(path, window=window, clip=True))
    assert [event.duration_us for event in events] == pytest.approx([0.25])


def test_epoch_adjacent_ns_events_keep_both_durations(tmp_path: Path) -> None:
    epoch = 1_790_000_000_000_000.0
    ns = int(epoch * NS_PER_US)
    db = tmp_path / "PROF_000000_test" / "msprof_0.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.execute("insert into STRING_IDS values (1, 'launch')")
    for offset in (0, 100):
        connection.execute(
            "insert into TASK values (?, ?)", (ns + offset, ns + offset + 100)
        )
        connection.execute(
            "insert into CANN_API values (1, ?, ?)",
            (ns + offset, ns + offset + 100),
        )
    connection.commit()
    connection.close()
    window = Window(epoch, epoch + 1.0, rounds=(1,))

    summary = timeline.summarize(db, window)

    assert summary.task_count == 2
    assert summary.device_busy_us == pytest.approx(0.2)
    assert summary.host_acl_busy_us == pytest.approx(0.2)


def test_no_window_single_ns_event_has_busy(tmp_path: Path) -> None:
    epoch = 1_790_000_000_000_000.0
    ns = int(epoch * NS_PER_US)
    db = tmp_path / "PROF_000000_test" / "msprof_0.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.execute("insert into STRING_IDS values (1, 'launch')")
    connection.execute("insert into TASK values (?, ?)", (ns, ns + 100))
    connection.execute("insert into CANN_API values (1, ?, ?)", (ns, ns + 100))
    connection.commit()
    connection.close()

    summary = timeline.summarize(db, None)

    assert summary.task_count == 1
    assert summary.device_busy_us == pytest.approx(0.1)
    assert summary.host_acl_busy_us == pytest.approx(0.1)


def test_parser_window_quality_uses_relative_busy(tmp_path: Path) -> None:
    epoch = 1_790_000_000_000_000.0
    parser, workload = _summary_parser(tmp_path)
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        f"0,MatMul,AI_CORE,{epoch},0.1\n",
        encoding="utf-8",
    )
    window = Window(epoch, epoch + 0.5, rounds=(1,), round_spans=((1, epoch, epoch + 0.5),))
    table = parser.parse(path, workload, window=window)

    quality = table.meta.extra["window_quality"]
    assert quality["busy_ratio"] == pytest.approx(0.2)
    assert quality["degraded"] is False


def test_parser_uses_configured_task_type_and_scaling(tmp_path: Path) -> None:
    """P1-1：空类型用 kernel 默认值，自定义通信别名按 0.001 缩放。"""
    from dataclasses import replace

    from opt_discovery.parsers.csv_mapping import MappingCsvParser

    parser, workload = _summary_parser(tmp_path)
    mapping = replace(
        parser.mapping,
        task_type_map={**parser.mapping.task_type_map, "custom_comm": "communication"},
    )
    parser = MappingCsvParser(mapping)
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        "0,MatMul,,100,100\n"
        "0,hcom_custom,custom_comm,200,10000\n",
        encoding="utf-8",
    )
    table = parser.parse(path, workload)
    records = {record.operator: record for record in table.records}

    assert records["MatMul"].task_type is TaskType.KERNEL
    assert records["MatMul"].total_time_us.value == pytest.approx(100.0)
    assert records["hcom_custom"].task_type is TaskType.COMMUNICATION
    assert records["hcom_custom"].total_time_us.value == pytest.approx(10.0)


def test_parser_filters_window_before_missing_duration(tmp_path: Path) -> None:
    """P1-2：合法起点先计窗口/轮次，缺时长再跳过。"""
    parser, workload = _summary_parser(tmp_path)
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        "0,outside,AI_CORE,10,N/A\n"
        "0,inside_missing,AI_CORE,120,N/A\n"
        "0,inside_ok,AI_CORE,160,10\n",
        encoding="utf-8",
    )
    window = Window(
        100.0,
        200.0,
        rounds=(1, 2),
        round_spans=((1, 100.0, 150.0), (2, 150.0, 200.0)),
    )
    table = parser.parse(path, workload, window=window)

    assert table.meta.extra["rows_outside_window"] == 1
    assert table.meta.skipped_rows == 1
    assert table.meta.extra["round_row_counts"] == {1: 1, 2: 1}
    operators = {record.operator: record.calls for record in table.records}
    assert "outside" not in operators
    assert operators["inside_ok"] == 1


def _gap_db(tmp_path: Path) -> Path:
    db = tmp_path / "PROF_000000_test" / "msprof_0.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.execute("insert into STRING_IDS values (1, 'launch')")
    for start_us, end_us in ((1000, 1250), (1450, 1600)):
        connection.execute(
            "insert into TASK values (?, ?)",
            (int(start_us * 1000), int(end_us * 1000)),
        )
    connection.execute("insert into CANN_API values (1, ?, ?)", (1_250_000, 1_450_000))
    connection.commit()
    connection.close()
    return db


def _mstx():
    from types import SimpleNamespace

    return SimpleNamespace(
        available=True,
        ranges=(SimpleNamespace(start_us=1250.0, end_us=1450.0, name="x"),),
        sync_us=0.0,
    )


def test_gap_attribution_uses_common_origin(tmp_path: Path) -> None:
    """P1-3：单 rank/多 rank/无窗与 MSTX 使用同一原点，覆盖 200µs。"""
    db = _gap_db(tmp_path)
    window = Window(1000.0, 1600.0, rounds=(1,))

    single = timeline.summarize(db, window)
    assert single.idle_covered_by_acl_us == pytest.approx(200.0)
    assert single.gaps_by_class.get("acl_busy") == 1

    multi = timeline.summarize(db, window, extra_db_paths=(db,))
    assert multi.idle_covered_by_acl_us == pytest.approx(200.0)
    assert multi.gaps_by_class.get("acl_busy") == 1

    no_window = timeline.summarize(db, None)
    assert no_window.idle_covered_by_acl_us == pytest.approx(200.0)

    connection = sqlite3.connect(db)
    connection.execute("delete from CANN_API")
    connection.commit()
    connection.close()
    mstx = timeline.summarize(db, None, mstx=_mstx())
    assert mstx.silent_covered_by_mstx_us == pytest.approx(200.0)


def test_ns_boundaries_and_csv_intersection_precision(tmp_path: Path) -> None:
    """P1-4：SQLite 用精确整数 ns，CSV 相交不先加 epoch。"""
    from opt_discovery.events import ns_from_us

    epoch = 1_790_000_000_000_000.0
    assert ns_from_us(epoch + 0.25) == int(epoch) * 1000 + 250

    boundary = int(epoch) * 1000 + 250
    db = tmp_path / "PROF_000000_test" / "msprof_0.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.execute("insert into STRING_IDS values (1, 'launch')")
    for start, end in ((boundary - 50, boundary + 2), (boundary + 100, boundary + 200)):
        connection.execute("insert into TASK values (?, ?)", (start, end))
        connection.execute("insert into CANN_API values (1, ?, ?)", (start, end))
    connection.commit()
    connection.close()
    window = Window(epoch + 0.25, epoch + 1.0, rounds=(1,))

    assert len(list(iter_task_span_rows(db, window=window))) == 2
    assert len(list(iter_host_calls(db, window=window))) == 2

    csv_path = tmp_path / "op_summary_1.csv"
    csv_path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        f"0,MatMul,AI_CORE,{epoch},0.2\n",
        encoding="utf-8",
    )
    assert list(iter_op_summary_events(csv_path, window=window)) == []


def test_reader_origin_none_uses_first_row(tmp_path: Path) -> None:
    """P1：origin_us=None 保留“首行原点”语义；默认仍是绝对坐标。"""
    db = tmp_path / "PROF_000000_test" / "msprof_0.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.execute("insert into STRING_IDS values (1, 'launch')")
    for start in (1_000_000, 1_100_000):
        connection.execute("insert into TASK values (?, ?)", (start, start + 100_000))
        connection.execute("insert into CANN_API values (1, ?, ?)", (start, start + 100_000))
    connection.commit()
    connection.close()

    relative_tasks = list(iter_task_span_rows(db, origin_us=None))
    assert [start for start, _ in relative_tasks] == pytest.approx([0.0, 100.0])
    relative_host = list(iter_host_spans(db, origin_us=None))
    assert [start for start, _, _ in relative_host] == pytest.approx([0.0, 100.0])

    absolute_tasks = list(iter_task_span_rows(db))
    assert [start for start, _ in absolute_tasks][0] == pytest.approx(1000.0)
    assert list(iter_task_intervals(db))[0].start_us == pytest.approx(1000.0)


def test_missing_task_db_raises_timeline_error_without_creating(tmp_path: Path) -> None:
    """P2：缺 TASK/文件时保持 TimelineError，不创建空库、不泄露 sqlite 异常。"""
    from opt_discovery.analyzer.timeline import TimelineError
    from opt_discovery.events import EventReadError, task_origin_ns

    missing = tmp_path / "not-there.db"
    with pytest.raises(TimelineError):
        timeline.summarize(missing, None)
    assert not missing.exists()

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(TimelineError):
        timeline.summarize(empty, None)
    with pytest.raises(EventReadError):
        task_origin_ns(empty)
