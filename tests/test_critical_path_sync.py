"""CP-02B contract tests: explicit event-handle synchronization.

Fixtures are synthetic SQLite databases using the real ``TASK`` columns.
No network, NPU or real profiler export is involved.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from opt_discovery.analyzer.critical_path import (
    ScopeError,
    SyncBinding,
    TaskDag,
    analyze_task_stream,
    analyze_task_streams,
    build_synchronized_task_dag,
    build_task_dag,
    solve_task_dag,
)
from opt_discovery.events import (
    SourceReference,
    TaskExecutionEvent,
    iter_task_execution_events,
    task_relative_window,
)
from opt_discovery.primitives import Window
from opt_discovery.schemas.critical_path import (
    ActivityKind,
    AnalysisScope,
    CriticalPathStatus,
    DependencyEdge,
    DependencyEvidence,
    DependencyType,
    EvidenceBasis,
    ExecutionNode,
)
from opt_discovery.schemas.metrics import Provenance
from opt_discovery.schemas.operator import TaskType
from opt_discovery.schemas.quality import BLOCKING, EMPTY_WINDOW, QualityFlag

UINT32_MAX = 4_294_967_295
DB_LOGICAL = "PROF_000000/msprof_0.db"
CAPTURE = "capture-cp02b"
RANK = 0
DEVICE = 0
PID = 100
CONTEXT = 7
PRODUCER = 47
CONSUMER = 48
CONSUMER_B = 49

STRING_IDS = {
    1: "KERNEL_AICORE",
    2: "KERNEL_AIVEC",
    3: "MEMCPY_ASYNC",
    4: "EVENT_RECORD",
    5: "EVENT_WAIT",
    6: "COMMUNICATION",
    7: "EVENT_RESET",
}

TASK_COLUMNS = (
    ("startNs", "integer"),
    ("endNs", "integer"),
    ("deviceId", "integer"),
    ("globalPid", "integer"),
    ("contextId", "integer"),
    ("streamId", "integer"),
    ("taskId", "integer"),
    ("globalTaskId", "integer"),
    ("modelId", "integer"),
    ("taskType", "integer"),
)


def _ns(us: float) -> int:
    return int(us * 1000)


def _window() -> Window:
    return Window(0.0, 100.0, rounds=(1,))


def _row(
    task_id: int | None,
    start_us: float | None,
    end_us: float | None,
    *,
    stream: int | None = PRODUCER,
    type_id: int = 1,
    gid: int | None = None,
    model_id: int | None = UINT32_MAX,
    context: int | None = CONTEXT,
    pid: int | None = PID,
    device: int | None = DEVICE,
) -> dict[str, int | None]:
    return {
        "startNs": None if start_us is None else _ns(start_us),
        "endNs": None if end_us is None else _ns(end_us),
        "deviceId": device,
        "globalPid": pid,
        "contextId": context,
        "streamId": stream,
        "taskId": task_id,
        "globalTaskId": (
            (stream or 0) * 1000 + task_id if gid is None and task_id is not None else gid
        ),
        "modelId": model_id,
        "taskType": type_id,
    }


def _write_db(path: Path, rows: list[dict[str, int | None]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        definition = ", ".join(f"{name} {kind}" for name, kind in TASK_COLUMNS)
        connection.execute(f"create table TASK ({definition})")
        names = tuple(name for name, _ in TASK_COLUMNS)
        placeholders = ",".join("?" for _ in names)
        for row in rows:
            connection.execute(
                f"insert into TASK ({','.join(names)}) values ({placeholders})",
                tuple(row[name] for name in names),
            )
        connection.execute("create table STRING_IDS (id integer, value text)")
        for type_id, value in STRING_IDS.items():
            connection.execute("insert into STRING_IDS values (?, ?)", (type_id, value))
        connection.commit()
    finally:
        connection.close()
    return path


def _scope(
    *,
    streams: tuple[int, ...] = (PRODUCER, CONSUMER),
    ranks: tuple[int, ...] = (RANK,),
    window: Window | None = None,
) -> AnalysisScope:
    return AnalysisScope(
        capture_id=CAPTURE,
        workload_id="wl-synthetic",
        is_synthetic=True,
        window=_window() if window is None else window,
        ranks=ranks,
        stream_ids=streams,
        clock_domain=None,
    )


def _read(db: Path, stream: int, window: Window | None = None) -> list[TaskExecutionEvent]:
    return list(
        iter_task_execution_events(
            db,
            capture_id=CAPTURE,
            source_path=DB_LOGICAL,
            rank=RANK,
            device_id=DEVICE,
            stream_id=stream,
            window=_window() if window is None else window,
        )
    )


def _events(db: Path, streams: tuple[int, ...] = (PRODUCER, CONSUMER)) -> list[TaskExecutionEvent]:
    result: list[TaskExecutionEvent] = []
    seen: set[tuple[str, str, int | None]] = set()
    for stream in streams:
        for event in _read(db, stream):
            key = (event.source.kind, event.source.path, event.source.row)
            if key in seen:
                continue
            seen.add(key)
            result.append(event)
    return result


def _by_role(
    events: list[TaskExecutionEvent], role: str, *, stream: int | None = None
) -> TaskExecutionEvent:
    for event in events:
        if (event.task_type_name or "").upper() != role:
            continue
        if stream is not None and event.stream_id != stream:
            continue
        return event
    raise AssertionError(f"no {role} event on stream {stream!r}")


def _binding(
    event: TaskExecutionEvent,
    *,
    handle: str | None = "h1",
    generation: int | None = 0,
    evidence_row: int = 900,
    evidence_path: str = "bindings/events.db",
) -> SyncBinding:
    return SyncBinding(
        capture_id=event.capture_id,
        task_source=event.source,
        event_handle=handle,
        generation=generation,
        evidence_source=SourceReference(
            kind="event_binding", path=evidence_path, row=evidence_row
        ),
    )


def _explicit_types(dag: TaskDag) -> list[DependencyEdge]:
    return [
        edge
        for edge in dag.edges
        if edge.dependency_type is DependencyType.EXPLICIT_EVENT_WAIT
    ]


def _reasons(dag: TaskDag) -> set[str]:
    return {item.reason_code for item in dag.unresolved}


def _record_wait_db(tmp_path: Path, wait_end_us: float = 20.0) -> tuple[Path, list[TaskExecutionEvent]]:
    db = _write_db(
        tmp_path / "sync.db",
        [
            _row(1, 10.0, 12.0, stream=PRODUCER, type_id=4),
            _row(1, 5.0, wait_end_us, stream=CONSUMER, type_id=5),
        ],
    )
    return db, _events(db)


def test_sync_binding_rejects_file_level_and_same_row_evidence() -> None:
    task = SourceReference(kind="task_table", path=DB_LOGICAL, row=1)
    good = SourceReference(kind="event_binding", path="bindings/events.db", row=2)

    with pytest.raises(ValueError):
        SyncBinding(CAPTURE, task, "h1", 0, task)
    with pytest.raises(ValueError):
        SyncBinding(
            CAPTURE,
            task,
            "h1",
            0,
            SourceReference(kind="event_binding", path="bindings/events.db"),
        )
    with pytest.raises(ValueError):
        SyncBinding(CAPTURE, task, "h1", -1, good)
    with pytest.raises(ValueError):
        SyncBinding(CAPTURE, task, "", 0, good)

    binding = SyncBinding(CAPTURE, task, None, None, good)
    assert binding.event_handle is None and binding.generation is None


def test_explicit_record_and_wait_form_one_cross_stream_edge(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "chain.db",
        [
            _row(1, 0.0, 1.0, stream=PRODUCER),
            _row(2, 10.0, 12.0, stream=PRODUCER, type_id=4),
            _row(3, 20.0, 30.0, stream=PRODUCER),
            _row(1, 0.0, 0.5, stream=CONSUMER),
            _row(2, 5.0, 25.0, stream=CONSUMER, type_id=5),
            _row(3, 25.0, 40.0, stream=CONSUMER),
        ],
    )
    events = _events(db)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events, _scope(), bindings=[_binding(record), _binding(wait, evidence_row=901)]
    )

    edges = _explicit_types(dag)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.evidence.basis is EvidenceBasis.EVENT_HANDLE
    assert edge.evidence.provenance is Provenance.DERIVED
    assert edge.evidence.fields == ("eventHandle", "generation")
    assert len(edge.evidence.source_refs) == 4
    assert edge.source_node_id == record.node.node_id  # type: ignore[union-attr]
    assert edge.target_node_id == wait.node.node_id  # type: ignore[union-attr]

    assert wait.node is not None
    wait_node = next(node for node in dag.nodes if node.node_id == wait.node.node_id)
    assert wait_node.duration_us == 0.0
    assert wait_node.start_us == pytest.approx(25.0)
    assert wait_node.source == wait.source

    result = solve_task_dag(dag, _scope())
    assert result.status is CriticalPathStatus.PARTIAL
    assert result.dependency_coverage is None
    assert result.observed_window_duration_us == pytest.approx(100.0)
    # producer kernel (1) + record (2) + milestone (0) + consumer kernel (15)
    assert result.critical_path_duration_us == pytest.approx(18.0)
    assert [node.source.row for node in result.path_nodes] == [1, 2, 5, 6]
    assert [edge.dependency_type for edge in result.path_edges] == [
        DependencyType.STREAM_ORDER,
        DependencyType.EXPLICIT_EVENT_WAIT,
        DependencyType.STREAM_ORDER,
    ]


def test_record_forks_to_two_waits_and_longest_branch_wins(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "fork.db",
        [
            _row(1, 0.0, 1.0, stream=PRODUCER),
            _row(2, 10.0, 12.0, stream=PRODUCER, type_id=4),
            _row(3, 20.0, 30.0, stream=PRODUCER),
            _row(1, 0.0, 0.5, stream=CONSUMER),
            _row(2, 5.0, 25.0, stream=CONSUMER, type_id=5),
            _row(3, 25.0, 40.0, stream=CONSUMER),
            _row(1, 0.0, 0.5, stream=CONSUMER_B),
            _row(2, 5.0, 35.0, stream=CONSUMER_B, type_id=5),
            _row(3, 35.0, 70.0, stream=CONSUMER_B),
        ],
    )
    events = _events(db, (PRODUCER, CONSUMER, CONSUMER_B))
    record = _by_role(events, "EVENT_RECORD")
    wait_a = _by_role(events, "EVENT_WAIT", stream=CONSUMER)
    wait_b = _by_role(events, "EVENT_WAIT", stream=CONSUMER_B)

    dag = build_synchronized_task_dag(
        events,
        _scope(streams=(PRODUCER, CONSUMER, CONSUMER_B)),
        bindings=[
            _binding(record),
            _binding(wait_a, evidence_row=901),
            _binding(wait_b, evidence_row=902),
        ],
    )

    edges = _explicit_types(dag)
    assert {edge.target_node_id for edge in edges} == {
        wait_a.node.node_id,  # type: ignore[union-attr]
        wait_b.node.node_id,  # type: ignore[union-attr]
    }
    assert len(edges) == 2

    result = solve_task_dag(dag, _scope(streams=(PRODUCER, CONSUMER, CONSUMER_B)))
    assert result.status is CriticalPathStatus.PARTIAL
    # Producer 1 + record 2 + milestone 0 + longest consumer (70 - 35).
    assert result.critical_path_duration_us == pytest.approx(38.0)
    waits = [
        node for node in result.path_nodes if node.source == wait_b.source
    ]
    assert len(waits) == 1
    assert all(node.duration_us is not None for node in result.path_nodes)
    assert sum(
        node.duration_us for node in result.path_nodes if node.duration_us is not None
    ) == pytest.approx(38.0)


def test_no_binding_means_no_cross_edge_even_with_unique_times(tmp_path: Path) -> None:
    _, events = _record_wait_db(tmp_path, wait_end_us=40.0)

    dag = build_synchronized_task_dag(events, _scope())

    assert _explicit_types(dag) == []
    missing = [
        item
        for item in dag.unresolved
        if item.dependency_type is DependencyType.EXPLICIT_EVENT_WAIT
        and item.reason_code == "missing_sync_binding"
    ]
    assert len(missing) == 2  # one EVENT_RECORD, one EVENT_WAIT


def test_duplicate_binding_is_unresolved(tmp_path: Path) -> None:
    _, events = _record_wait_db(tmp_path)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events,
        _scope(),
        bindings=[
            _binding(record),
            _binding(wait, evidence_row=901),
            _binding(wait, evidence_row=902),
        ],
    )

    assert _explicit_types(dag) == []
    assert "duplicate_task_binding" in _reasons(dag)


def test_ambiguous_record_key_blocks_every_wait(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "ambiguous.db",
        [
            _row(1, 10.0, 12.0, stream=PRODUCER, type_id=4),
            _row(1, 10.0, 12.0, stream=CONSUMER_B, type_id=4),
            _row(1, 5.0, 20.0, stream=CONSUMER, type_id=5),
        ],
    )
    events = _events(db, (PRODUCER, CONSUMER, CONSUMER_B))
    records = [
        event
        for event in events
        if (event.task_type_name or "").upper() == "EVENT_RECORD"
    ]
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events,
        _scope(streams=(PRODUCER, CONSUMER, CONSUMER_B)),
        bindings=[_binding(event, evidence_row=900 + index) for index, event in enumerate(records)]
        + [_binding(wait, evidence_row=910)],
    )

    assert _explicit_types(dag) == []
    assert "ambiguous_record_key" in _reasons(dag)


def test_ineligible_record_still_makes_its_generation_ambiguous(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "ambiguous-ineligible.db",
        [
            _row(1, 10.0, 12.0, stream=PRODUCER, type_id=4),
            {**_row(1, 10.0, 12.0, stream=CONSUMER_B, type_id=4), "globalTaskId": None},
            _row(1, 5.0, 20.0, stream=CONSUMER, type_id=5),
        ],
    )
    streams = (PRODUCER, CONSUMER, CONSUMER_B)
    events = _events(db, streams)
    dag = build_synchronized_task_dag(
        events, _scope(streams=streams),
        bindings=[_binding(event, evidence_row=900 + index) for index, event in enumerate(events)],
    )

    assert _explicit_types(dag) == []
    assert "ambiguous_record_key" in _reasons(dag)


def test_conflicting_task_locator_prevents_sync_assembly(tmp_path: Path) -> None:
    _, events = _record_wait_db(tmp_path)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")
    dag = build_synchronized_task_dag(
        [*events, replace(record, raw_end_ns=record.raw_end_ns + 1)],
        _scope(),
        bindings=[_binding(record), _binding(wait, evidence_row=901)],
    )

    assert dag.nodes == () and dag.edges == ()
    assert "conflicting_task_observations" in _reasons(dag)


def test_generation_selects_record_not_timestamp(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "generation.db",
        [
            _row(1, 10.0, 30.0, stream=PRODUCER, type_id=4),
            _row(1, 10.0, 12.0, stream=CONSUMER_B, type_id=4),
            _row(1, 5.0, 40.0, stream=CONSUMER, type_id=5),
        ],
    )
    events = _events(db, (PRODUCER, CONSUMER, CONSUMER_B))
    records = [
        event
        for event in events
        if (event.task_type_name or "").upper() == "EVENT_RECORD"
    ]
    older = next(event for event in records if event.stream_id == PRODUCER)
    newer = next(event for event in records if event.stream_id == CONSUMER_B)
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events,
        _scope(streams=(PRODUCER, CONSUMER, CONSUMER_B)),
        bindings=[
            _binding(older, generation=0, evidence_row=900),
            _binding(newer, generation=1, evidence_row=901),
            _binding(wait, generation=1, evidence_row=902),
        ],
    )

    edges = _explicit_types(dag)
    assert len(edges) == 1
    assert edges[0].source_node_id == newer.node.node_id  # type: ignore[union-attr]
    assert "unmatched_record" in _reasons(dag)


def test_missing_generation_is_unresolved(tmp_path: Path) -> None:
    _, events = _record_wait_db(tmp_path)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events,
        _scope(),
        bindings=[
            _binding(record, generation=None),
            _binding(wait, generation=None, evidence_row=901),
        ],
    )

    assert _explicit_types(dag) == []
    assert "missing_sync_identity" in _reasons(dag)


def test_cross_process_pid_guard_emits_unresolved(tmp_path: Path) -> None:
    _, events = _record_wait_db(tmp_path)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")
    foreign_wait = replace(wait, global_pid=PID + 1)

    dag = build_synchronized_task_dag(
        [foreign_wait if event is wait else event for event in events],
        _scope(),
        bindings=[_binding(record), _binding(foreign_wait, evidence_row=901)],
    )

    assert _explicit_types(dag) == []
    assert "global_pid_mismatch" in _reasons(dag)


def test_cross_rank_and_mixed_source_fail_closed(tmp_path: Path) -> None:
    _, events = _record_wait_db(tmp_path)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    mixed_rank = [record, replace(wait, rank=RANK + 1)]
    dag = build_synchronized_task_dag(
        mixed_rank,
        _scope(),
        bindings=[
            _binding(record),
            _binding(replace(wait, rank=RANK + 1), evidence_row=901),
        ],
    )
    assert dag.nodes == () and dag.edges == ()
    assert "mixed_sync_scope" in _reasons(dag)

    moved = replace(record, source=replace(record.source, path="OTHER/msprof.db"))
    dag = build_synchronized_task_dag(
        [moved, wait],
        _scope(),
        bindings=[_binding(moved), _binding(wait, evidence_row=901)],
    )
    assert dag.nodes == () and dag.edges == ()
    assert "mixed_sync_scope" in _reasons(dag)


def test_record_after_wait_completion_is_not_an_edge(tmp_path: Path) -> None:
    _, events = _record_wait_db(tmp_path, wait_end_us=11.0)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events,
        _scope(),
        bindings=[_binding(record), _binding(wait, evidence_row=901)],
    )

    assert _explicit_types(dag) == []
    assert "record_after_wait_completion" in _reasons(dag)


def test_invalid_wait_timestamp_never_forms_an_edge(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "invalid-time.db",
        [
            _row(1, 10.0, 12.0, stream=PRODUCER, type_id=4),
            _row(1, 20.0, 5.0, stream=CONSUMER, type_id=5),
        ],
    )
    events = _events(db)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events,
        _scope(),
        bindings=[_binding(record), _binding(wait, evidence_row=901)],
    )

    assert _explicit_types(dag) == []
    assert "sync_endpoint_ineligible" in _reasons(dag)


def test_same_stream_is_never_paired(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "same-stream.db",
        [
            _row(1, 10.0, 12.0, stream=PRODUCER, type_id=4),
            _row(2, 5.0, 20.0, stream=PRODUCER, type_id=5),
        ],
    )
    events = _events(db, (PRODUCER,))
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    dag = build_synchronized_task_dag(
        events,
        _scope(streams=(PRODUCER,)),
        bindings=[_binding(record), _binding(wait, evidence_row=901)],
    )

    assert _explicit_types(dag) == []
    assert "same_stream" in _reasons(dag)


def test_sync_rows_without_binding_are_barriers_even_for_reset(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "barrier.db",
        [
            _row(1, 0.0, 1.0, stream=PRODUCER),
            _row(2, 1.0, 2.0, stream=PRODUCER, type_id=4),
            _row(3, 3.0, 4.0, stream=PRODUCER),
        ],
    )
    events = _read(db, PRODUCER)
    dag = build_task_dag(events, _scope(streams=(PRODUCER,)))

    assert dag.edges == ()
    assert "task_type_not_serial" in _reasons(dag)

    reset_db = _write_db(
        tmp_path / "reset.db",
        [
            _row(1, 0.0, 1.0, stream=PRODUCER),
            _row(2, 1.0, 2.0, stream=PRODUCER, type_id=7),
            _row(3, 3.0, 4.0, stream=PRODUCER),
        ],
    )
    reset_dag = build_task_dag(_read(reset_db, PRODUCER), _scope(streams=(PRODUCER,)))
    assert reset_dag.edges == ()
    assert "task_type_not_serial" in _reasons(reset_dag)


def test_analyze_task_streams_builds_one_relative_origin(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "analyze.db",
        [
            _row(1, 0.0, 1.0, stream=PRODUCER),
            _row(2, 10.0, 12.0, stream=PRODUCER, type_id=4),
            _row(3, 20.0, 30.0, stream=PRODUCER),
            _row(1, 0.0, 0.5, stream=CONSUMER),
            _row(2, 5.0, 25.0, stream=CONSUMER, type_id=5),
            _row(3, 25.0, 40.0, stream=CONSUMER),
        ],
    )
    events = _events(db)
    record = _by_role(events, "EVENT_RECORD")
    wait = _by_role(events, "EVENT_WAIT")

    result = analyze_task_streams(
        db,
        _scope(),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
        bindings=[_binding(record), _binding(wait, evidence_row=901)],
    )

    assert result.status is CriticalPathStatus.PARTIAL
    assert result.scope.window is not None
    assert result.scope.window.start_us == 0.0
    assert result.scope.window.end_us == pytest.approx(100.0)
    assert result.scope.clock_domain is not None and "origin_ns" in result.scope.clock_domain
    assert result.dependency_coverage is None
    assert result.status is not CriticalPathStatus.COMPLETE
    assert any(
        edge.dependency_type is DependencyType.EXPLICIT_EVENT_WAIT
        for edge in result.path_edges
    )


def test_analyze_task_streams_scope_and_source_guards(tmp_path: Path) -> None:
    db = _write_db(tmp_path / "plain.db", [_row(1, 0.0, 1.0, stream=PRODUCER)])

    multi_rank = analyze_task_streams(
        db,
        _scope(ranks=(0, 1)),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
    )
    assert multi_rank.status is CriticalPathStatus.UNAVAILABLE
    assert "scope_not_single_rank" in _reasons(
        TaskDag((), (), multi_rank.unresolved_dependencies)
    )

    duplicate_stream = analyze_task_streams(
        db,
        _scope(streams=(PRODUCER, PRODUCER)),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
    )
    assert duplicate_stream.status is CriticalPathStatus.UNAVAILABLE
    assert "scope_stream_not_distinct" in {
        item.reason_code for item in duplicate_stream.unresolved_dependencies
    }

    missing = analyze_task_streams(
        tmp_path / "missing.db",
        _scope(),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
    )
    assert missing.status is CriticalPathStatus.UNAVAILABLE
    assert "source_unavailable" in {
        item.reason_code for item in missing.unresolved_dependencies
    }

    blocked = analyze_task_streams(
        db,
        _scope(streams=(PRODUCER,)),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
        quality_flags=(
            QualityFlag(code=EMPTY_WINDOW, message="empty", severity=BLOCKING),
        ),
    )
    assert blocked.status is CriticalPathStatus.UNAVAILABLE


def test_multi_stream_builder_requires_explicit_distinct_streams() -> None:
    with pytest.raises(ScopeError):
        build_synchronized_task_dag((), _scope(streams=()))
    with pytest.raises(ScopeError):
        build_synchronized_task_dag((), _scope(streams=(PRODUCER, PRODUCER)))
    with pytest.raises(ScopeError):
        build_synchronized_task_dag((), _scope(ranks=(0, 1)))


def _manual_node(row: int, start_us: float, duration_us: float) -> ExecutionNode:
    return ExecutionNode(
        capture_id=CAPTURE,
        source=SourceReference(kind="task_table", path=DB_LOGICAL, row=row),
        activity=ActivityKind.DEVICE_TASK,
        start_us=start_us,
        duration_us=duration_us,
        rank=RANK,
        stream_id=PRODUCER,
        task_type=TaskType.KERNEL,
    )


def _explicit_edge(source: ExecutionNode, target: ExecutionNode) -> DependencyEdge:
    return DependencyEdge(
        source_node_id=source.node_id,
        target_node_id=target.node_id,
        dependency_type=DependencyType.EXPLICIT_EVENT_WAIT,
        evidence=DependencyEvidence(
            source_refs=(source.source, target.source),
            basis=EvidenceBasis.EVENT_HANDLE,
            fields=("eventHandle", "generation"),
            provenance=Provenance.DERIVED,
        ),
    )


def test_existing_solver_reuses_cycle_detection_for_explicit_edges() -> None:
    first = _manual_node(1, 0.0, 10.0)
    second = _manual_node(2, 20.0, 10.0)
    dag = TaskDag(
        nodes=(first, second),
        edges=(_explicit_edge(first, second), _explicit_edge(second, first)),
        unresolved=(),
    )

    result = solve_task_dag(dag, _scope())

    assert result.status is CriticalPathStatus.UNAVAILABLE
    assert result.path_nodes == ()
    assert "dependency_cycle" in {
        item.reason_code for item in result.unresolved_dependencies
    }


def test_single_stream_api_is_unchanged(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "single.db",
        [_row(1, 0.0, 10.0), _row(2, 10.0, 20.0)],
    )
    result = analyze_task_stream(
        db, _scope(streams=(PRODUCER,)), source_path=DB_LOGICAL, device_id=DEVICE
    )

    assert result.status is CriticalPathStatus.PARTIAL
    assert result.critical_path_duration_us == pytest.approx(20.0)
    relative, origin_ns = task_relative_window(_window())
    assert relative.end_us == pytest.approx(100.0)
    assert origin_ns == 0
