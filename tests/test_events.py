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

    filtered = list(iter_op_summary_events(csv_path, window=window))
    assert [event.operator for event in filtered] == ["MatMul", "hcom_allReduce_"]
    assert [event.duration_us for event in filtered] == [100.0, 50.0]
    assert filtered[0].task_type is TaskType.KERNEL
    assert filtered[1].task_type is TaskType.COMMUNICATION
    assert filtered[0].source.kind == "op_summary"
    assert filtered[0].source.row == 2
    assert filtered[0].rank == 0
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
