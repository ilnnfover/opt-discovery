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
    iter_op_summary_events,
    iter_op_summary_intervals,
    iter_task_intervals,
)
from opt_discovery.primitives import (
    COMMUNICATION_DURATION_SCALE,
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
