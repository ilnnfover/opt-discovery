"""CP-02A tests: TASK adapter, evidence-constrained stream DAG and solver.

Every fixture is a synthetic SQLite database with the real ``TASK`` columns.
No network, NPU or real profiler export is used.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from opt_discovery.analyzer.critical_path import (
    ScopeError,
    TaskDag,
    analyze_task_stream,
    build_task_dag,
    solve_task_dag,
)
from opt_discovery.events import (
    EventReadError,
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
CAPTURE = "capture-cp02a"
STREAM = 47
DEVICE = 0
PID = 100
CONTEXT = 7

STRING_IDS = {
    1: "KERNEL_AICORE",
    2: "KERNEL_AIVEC",
    3: "MEMCPY_ASYNC",
    4: "EVENT_RECORD",
    5: "EVENT_WAIT",
    6: "COMMUNICATION",
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


def _window(start_us: float = 1000.0, end_us: float = 2000.0) -> Window:
    return Window(start_us=start_us, end_us=end_us, rounds=(1,))


def _epoch_window() -> Window:
    epoch = 1_790_000_000_000_000.0
    return Window(epoch, epoch + 1.0, rounds=(1,))


def _row(
    task_id: int | None,
    start_ns: int | None,
    end_ns: int | None,
    *,
    type_id: int = 1,
    gid: int | None = None,
    model_id: int | None = UINT32_MAX,
    context: int | None = CONTEXT,
    pid: int | None = PID,
    stream: int | None = STREAM,
    device: int | None = DEVICE,
) -> dict[str, int | None]:
    return {
        "startNs": start_ns,
        "endNs": end_ns,
        "deviceId": device,
        "globalPid": pid,
        "contextId": context,
        "streamId": stream,
        "taskId": task_id,
        "globalTaskId": (task_id * 10 + 1) if gid is None and task_id is not None else gid,
        "modelId": model_id,
        "taskType": type_id,
    }


def _write_db(    path: Path,
    rows: list[dict[str, int | None]],
    *,
    string_ids: bool = True,
    task_columns: tuple[tuple[str, str], ...] = TASK_COLUMNS,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        definition = ", ".join(f"{name} {kind}" for name, kind in task_columns)
        connection.execute(f"create table TASK ({definition})")
        names = tuple(name for name, _ in task_columns)
        placeholders = ",".join("?" for _ in names)
        for row in rows:
            connection.execute(
                f"insert into TASK ({','.join(names)}) values ({placeholders})",
                tuple(row[name] for name in names),
            )
        if string_ids:
            connection.execute("create table STRING_IDS (id integer, value text)")
            for type_id, value in STRING_IDS.items():
                connection.execute("insert into STRING_IDS values (?, ?)", (type_id, value))
        connection.commit()
    finally:
        connection.close()
    return path


def _scope(
    window: Window | None = None,
    *,
    ranks: tuple[int, ...] = (0,),
    streams: tuple[int, ...] = (STREAM,),
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


def _relative_scope(window: Window | None = None) -> AnalysisScope:
    base = _scope(window)
    relative, origin_ns = task_relative_window(base.window)  # type: ignore[arg-type]
    return replace(
        base,
        window=relative,
        clock_domain=f"device-{DEVICE}:origin_ns={origin_ns}",
    )


def _events(db: Path, window: Window | None = None) -> list[TaskExecutionEvent]:
    return list(
        iter_task_execution_events(
            db,
            capture_id=CAPTURE,
            source_path=DB_LOGICAL,
            rank=0,
            device_id=DEVICE,
            stream_id=STREAM,
            window=_window() if window is None else window,
        )
    )


def _manual_node(row: int, start_us: float, duration_us: float) -> ExecutionNode:
    return ExecutionNode(
        capture_id=CAPTURE,
        source=_ref(row),
        activity=ActivityKind.DEVICE_TASK,
        start_us=start_us,
        duration_us=duration_us,
        rank=0,
        stream_id=STREAM,
        task_type=TaskType.KERNEL,
    )


def _ref(row: int):
    from opt_discovery.events import SourceReference

    return SourceReference(kind="task_table", path=DB_LOGICAL, row=row)


def _manual_edge(source: ExecutionNode, target: ExecutionNode) -> DependencyEdge:
    return DependencyEdge(
        source_node_id=source.node_id,
        target_node_id=target.node_id,
        dependency_type=DependencyType.STREAM_ORDER,
        evidence=DependencyEvidence(
            source_refs=(source.source, target.source),
            basis=EvidenceBasis.STREAM_SEQUENCE,
            fields=("taskId", "streamId"),
            provenance=Provenance.DERIVED,
            detail="synthetic stream order",
        ),
    )


def _forced_edge(source: ExecutionNode, target: ExecutionNode) -> DependencyEdge:
    """Only used to construct an invalid cycle that the schemas would reject."""

    edge = object.__new__(DependencyEdge)
    object.__setattr__(edge, "source_node_id", source.node_id)
    object.__setattr__(edge, "target_node_id", target.node_id)
    object.__setattr__(edge, "dependency_type", DependencyType.STREAM_ORDER)
    object.__setattr__(
        edge,
        "evidence",
        DependencyEvidence(
            source_refs=(source.source, target.source),
            basis=EvidenceBasis.STREAM_SEQUENCE,
            fields=("taskId",),
            provenance=Provenance.DERIVED,
            detail="forced cycle fixture",
        ),
    )
    return edge


def test_reader_is_read_only_and_does_not_create_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(EventReadError, match="文件不存在"):
        _events(missing)
    assert not missing.exists()


def test_reader_requires_task_table_and_columns(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(EventReadError, match="TASK"):
        _events(empty)

    incomplete = _write_db(
        tmp_path / "incomplete.db",
        [],
        task_columns=(("startNs", "integer"), ("endNs", "integer"), ("streamId", "integer")),
    )
    with pytest.raises(EventReadError, match="缺少"):
        _events(incomplete)


def test_reader_preserves_identity_row_and_sub_microsecond_time(tmp_path: Path) -> None:
    epoch = 1_790_000_000_000_000.0
    base_ns = int(epoch) * 1000
    db = _write_db(
        tmp_path / "epoch.db",
        [_row(1, base_ns + 250, base_ns + 500)],
    )
    window = _epoch_window()

    first = _events(db, window)[0]
    second = _events(db, window)[0]

    assert first.start_us == pytest.approx(0.25)
    assert first.duration_us == pytest.approx(0.25)
    assert first.raw_start_ns == base_ns + 250
    assert first.raw_end_ns == base_ns + 500
    assert first.source.kind == "task_table"
    assert first.source.path == DB_LOGICAL
    assert first.source.row == 1
    assert first.model_id is None  # UINT32_MAX is a sentinel, not an identity
    assert first.context_id == CONTEXT
    assert first.node is not None
    assert first.node.node_id == second.node.node_id
    assert first.node.duration_us == pytest.approx(0.25)


def test_task_relative_window_preserves_round_spans_and_origin(tmp_path: Path) -> None:
    epoch = 1_790_000_000_000_000.0
    window = Window(
        epoch,
        epoch + 2.0,
        rounds=(1, 2),
        round_spans=((1, epoch, epoch + 1.0), (2, epoch + 1.0, epoch + 2.0)),
    )
    relative, origin_ns = task_relative_window(window)

    assert origin_ns == 1_790_000_000_000_000_000
    assert relative.start_us == 0.0
    assert relative.end_us == pytest.approx(2.0)
    assert relative.rounds == (1, 2)
    assert relative.round_spans[0] == pytest.approx((1, 0.0, 1.0))
    assert relative.round_spans[1] == pytest.approx((2, 1.0, 2.0))


def test_window_is_half_open_and_boundary_crossers_are_barriers(tmp_path: Path) -> None:
    window = _window()
    start_ns = 1_000_000
    end_ns = 2_000_000
    db = _write_db(
        tmp_path / "bounds.db",
        [
            _row(1, start_ns - 50_000, start_ns + 50_000),  # crosses left edge
            _row(2, 1_100_000, 1_200_000),  # fully inside
            _row(3, end_ns, end_ns + 100_000),  # starts at the open right edge
            _row(4, 1_950_000, end_ns + 50_000),  # crosses right edge
        ],
    )

    events = _events(db, window)
    assert [event.task_id for event in events] == [1, 2, 4]
    assert events[0].in_window is False
    assert events[0].crosses_boundary is True
    assert events[0].duration_us is None
    assert "window_crossing" in events[0].reasons
    assert events[1].in_window is True
    assert events[2].duration_us is None

    dag = build_task_dag(events, _relative_scope(window))
    assert [node.source.row for node in dag.nodes] == [2]
    assert dag.edges == ()
    assert any(item.reason_code == "window_crossing" for item in dag.unresolved)

    result = solve_task_dag(dag, _relative_scope(window))
    assert result.status is CriticalPathStatus.PARTIAL
    assert [node.source.row for node in result.path_nodes] == [2]
    assert result.critical_path_duration_us == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (
            [_row(1, 1_000_000, 1_100_000), _row(3, 1_100_000, 1_200_000)],
            "task_id_not_adjacent",
        ),
        (
            [_row(1, 1_000_000, 1_100_000), _row(1, 1_100_000, 1_200_000)],
            "task_id_reused",
        ),
        (
            [_row(1, 1_000_000, 1_300_000), _row(2, 1_100_000, 1_200_000)],
            "overlapping_intervals",
        ),
        (
            [_row(1, 1_000_000, 1_200_000), _row(2, 1_000_000, 1_100_000)],
            "equal_start",
        ),
        (
            [_row(1, 1_000_000, 1_100_000, type_id=4), _row(2, 1_100_000, 1_200_000)],
            "task_type_not_serial",
        ),
        (
            [_row(1, 1_000_000, 1_100_000), _row(2, 1_100_000, 1_200_000, model_id=5)],
            "model_is_graph",
        ),
        (
            [_row(None, 1_000_000, 1_100_000), _row(2, 1_100_000, 1_200_000)],
            "missing_task_id",
        ),
        (
            [
                _row(1, 1_000_000, 1_100_000),
                _row(2, 1_100_000, 1_200_000, context=UINT32_MAX),
            ],
            "context_mismatch",
        ),
        (
            [
                _row(1, 1_000_000, 1_100_000),
                _row(2, 1_100_000, 1_200_000, context=None),
            ],
            "missing_context_id",
        ),
        (
            [
                _row(1, 1_000_000, 1_100_000),
                {**_row(2, 1_100_000, 1_200_000), "globalTaskId": None},
            ],
            "missing_global_task_id",
        ),
    ],
)
def test_builder_never_fabricates_stream_edges(
    tmp_path: Path, rows: list[dict[str, int | None]], reason: str
) -> None:
    db = _write_db(tmp_path / "no-edge.db", rows)
    dag = build_task_dag(_events(db), _relative_scope())

    assert dag.edges == ()
    assert reason in {item.reason_code for item in dag.unresolved}


def test_builder_allows_only_verified_serial_types_and_records_two_row_references(
    tmp_path: Path,
) -> None:
    db = _write_db(
        tmp_path / "chain.db",
        [
            _row(1, 1_000_000, 1_100_000, type_id=1),
            _row(2, 1_100_000, 1_250_000, type_id=3),
            _row(3, 1_300_000, 1_400_000, type_id=2),
        ],
    )
    scope = _relative_scope()
    dag = build_task_dag(_events(db), scope)

    assert [node.source.row for node in dag.nodes] == [1, 2, 3]
    assert len(dag.edges) == 2
    assert dag.unresolved == ()
    for edge in dag.edges:
        assert edge.dependency_type is DependencyType.STREAM_ORDER
        assert edge.evidence.basis is EvidenceBasis.STREAM_SEQUENCE
        assert edge.evidence.provenance is Provenance.DERIVED
        assert "taskId" in edge.evidence.fields
        assert len(edge.evidence.source_refs) == 2
        assert {ref.row for ref in edge.evidence.source_refs} <= {1, 2, 3}

    result = solve_task_dag(dag, scope)
    assert result.status is CriticalPathStatus.PARTIAL
    assert result.critical_path_duration_us == pytest.approx(350.0)
    assert result.dependency_coverage is None
    assert [node.source.row for node in result.path_nodes] == [1, 2, 3]


def test_both_subgraph_context_sentinels_can_form_a_stream_edge(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "sentinel-context.db",
        [
            _row(1, 1_000_000, 1_100_000, context=UINT32_MAX),
            _row(2, 1_100_000, 1_200_000, context=UINT32_MAX),
        ],
    )
    events = _events(db)

    assert [event.context_id for event in events] == [UINT32_MAX, UINT32_MAX]
    dag = build_task_dag(events, _relative_scope())
    assert len(dag.edges) == 1


def test_long_covering_task_taints_its_inner_endpoint(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "long-cover.db",
        [
            _row(1, 1_000_000, 1_300_000),
            _row(2, 1_100_000, 1_200_000),
            _row(3, 1_500_000, 1_600_000),
        ],
    )
    dag = build_task_dag(_events(db), _relative_scope())

    assert dag.edges == ()
    assert "earlier_interval_overlaps" in {
        item.reason_code for item in dag.unresolved
    }


def test_later_overlap_invalidates_an_incoming_edge(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "overlap-incoming.db",
        [
            _row(1, 1_000_000, 1_100_000),
            _row(2, 1_200_000, 1_400_000),
            _row(3, 1_300_000, 1_350_000),
        ],
    )
    dag = build_task_dag(_events(db), _relative_scope())

    assert dag.edges == ()
    assert "later_interval_overlaps" in {item.reason_code for item in dag.unresolved}


def test_unknown_stream_row_cannot_be_silently_skipped(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "unknown-stream.db",
        [
            _row(1, 1_000_000, 1_100_000),
            _row(9, 1_200_000, 1_250_000, stream=None),
            _row(2, 1_300_000, 1_400_000),
        ],
    )
    events = _events(db)
    dag = build_task_dag(events, _relative_scope())

    assert events[1].node is not None and events[1].node.stream_id is None
    assert dag.edges == ()
    assert "invalid_stream_id" in {item.reason_code for item in dag.unresolved}


def test_exact_raw_touch_reports_coordinate_precision_and_keeps_duration(
    tmp_path: Path,
) -> None:
    window = Window(0.0, 1.0, rounds=(1,))
    db = _write_db(
        tmp_path / "precision.db",
        [
            _row(1, 8, 9),
            _row(2, 9, 10),
        ],
    )
    events = _events(db, window)

    assert [event.duration_us for event in events] == pytest.approx([0.001, 0.001])
    dag = build_task_dag(events, _relative_scope(window))
    assert dag.edges == ()
    assert "coordinate_precision" in {
        item.reason_code for item in dag.unresolved
    }
    assert [node.duration_us for node in dag.nodes] == pytest.approx([0.001, 0.001])


def test_solver_takes_longest_weighted_chain_and_ignores_gaps(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "gap.db",
        [
            _row(1, 1_000_000, 1_010_000),
            _row(2, 1_010_000, 1_020_000),
            _row(5, 1_500_000, 1_560_000),
            _row(6, 1_560_000, 1_570_000),
        ],
    )
    scope = _relative_scope()
    dag = build_task_dag(_events(db), scope)

    assert len(dag.edges) == 2
    result = solve_task_dag(dag, scope)

    assert result.status is CriticalPathStatus.PARTIAL
    assert [node.source.row for node in result.path_nodes] == [3, 4]
    assert result.critical_path_duration_us == pytest.approx(70.0)
    assert result.observed_window_duration_us == pytest.approx(1000.0)


def test_solver_handles_topological_branch_without_double_counting() -> None:
    scope = _relative_scope()
    first = _manual_node(1, 0.0, 10.0)
    left = _manual_node(2, 10.0, 5.0)
    right = _manual_node(3, 15.0, 7.0)
    last = _manual_node(4, 22.0, 1.0)
    dag = TaskDag(
        nodes=(first, left, right, last),
        edges=(
            _manual_edge(first, left),
            _manual_edge(first, right),
            _manual_edge(left, last),
            _manual_edge(right, last),
        ),
        unresolved=(),
    )

    result = solve_task_dag(dag, scope)

    assert result.status is CriticalPathStatus.PARTIAL
    assert [node.source.row for node in result.path_nodes] == [1, 3, 4]
    assert result.critical_path_duration_us == pytest.approx(18.0)


def test_solver_tie_uses_the_declared_graph_input_order() -> None:
    scope = _relative_scope()
    first = _manual_node(1, 0.0, 10.0)
    second = _manual_node(2, 20.0, 10.0)
    dag = TaskDag(nodes=(first, second), edges=(), unresolved=())

    result = solve_task_dag(dag, scope)

    assert [node.source.row for node in result.path_nodes] == [1]
    assert result.critical_path_duration_us == pytest.approx(10.0)


def test_solver_reports_cycle_as_unavailable_without_a_path() -> None:
    scope = _relative_scope()
    first = _manual_node(1, 0.0, 10.0)
    second = _manual_node(2, 5.0, 5.0)
    dag = TaskDag(
        nodes=(first, second),
        edges=(_forced_edge(first, second), _forced_edge(second, first)),
        unresolved=(),
    )

    result = solve_task_dag(dag, scope)

    assert result.status is CriticalPathStatus.UNAVAILABLE
    assert result.path_nodes == ()
    assert result.path_edges == ()
    assert result.critical_path_duration_us is None
    assert any(item.reason_code == "dependency_cycle" for item in result.unresolved_dependencies)


def test_blocking_quality_flag_makes_result_unavailable(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "blocked.db",
        [_row(1, 1_000_000, 1_100_000), _row(2, 1_100_000, 1_200_000)],
    )
    scope = _relative_scope()
    dag = build_task_dag(_events(db), scope)
    flag = QualityFlag(code=EMPTY_WINDOW, message="empty", severity=BLOCKING)

    result = solve_task_dag(dag, scope, quality_flags=(flag,))

    assert result.status is CriticalPathStatus.UNAVAILABLE
    assert result.path_nodes == ()
    assert result.critical_path_duration_us is None
    assert result.quality_flags == (flag,)


def test_empty_dag_is_undetermined() -> None:
    result = solve_task_dag(TaskDag((), (), ()), _relative_scope())

    assert result.status is CriticalPathStatus.UNDETERMINED
    assert result.path_nodes == ()


@pytest.mark.parametrize(
    "scope",
    [
        _scope(ranks=(0, 1)),
        _scope(streams=(STREAM, 48)),
    ],
)
def test_builder_requires_a_single_rank_and_stream(scope: AnalysisScope, tmp_path: Path) -> None:
    db = _write_db(tmp_path / "scope.db", [_row(1, 1_000_000, 1_100_000)])
    with pytest.raises(ScopeError):
        build_task_dag(_events(db), scope)


def test_analyze_returns_unavailable_for_missing_source(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    result = analyze_task_stream(
        missing,
        _scope(),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
    )

    assert result.status is CriticalPathStatus.UNAVAILABLE
    assert result.path_nodes == ()
    assert "source_unavailable" in {
        item.reason_code for item in result.unresolved_dependencies
    }
    assert not missing.exists()


def test_analyze_returns_unavailable_for_unsupported_scope(tmp_path: Path) -> None:
    db = _write_db(tmp_path / "scope.db", [_row(1, 1_000_000, 1_100_000)])
    result = analyze_task_stream(
        db,
        _scope(ranks=(0, 1)),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
    )

    assert result.status is CriticalPathStatus.UNAVAILABLE
    assert "scope_not_single_rank" in {
        item.reason_code for item in result.unresolved_dependencies
    }


def test_analyze_builds_same_origin_relative_scope(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "analyze.db",
        [
            _row(1, 1_000_000, 1_100_000),
            _row(2, 1_100_000, 1_250_000),
            _row(3, 1_300_000, 1_400_000),
        ],
    )
    result = analyze_task_stream(
        db,
        _scope(),
        source_path=DB_LOGICAL,
        device_id=DEVICE,
    )

    assert result.status is CriticalPathStatus.PARTIAL
    assert result.scope.window is not None
    assert result.scope.window.start_us == 0.0
    assert result.scope.window.end_us == pytest.approx(1000.0)
    assert result.scope.clock_domain is not None
    assert "origin_ns" in result.scope.clock_domain
    assert [node.start_us for node in result.path_nodes] == pytest.approx([0.0, 100.0, 300.0])
    assert result.critical_path_duration_us == pytest.approx(350.0)


def test_missing_string_ids_keeps_types_unknown_and_blocks_edges(tmp_path: Path) -> None:
    db = _write_db(
        tmp_path / "no-strings.db",
        [_row(1, 1_000_000, 1_100_000), _row(2, 1_100_000, 1_200_000)],
        string_ids=False,
    )
    events = _events(db)

    assert all(event.task_type_name is None for event in events)
    assert all(event.task_type is TaskType.UNKNOWN for event in events)
    dag = build_task_dag(events, _relative_scope())
    assert dag.edges == ()
    assert "task_type_not_serial" in {item.reason_code for item in dag.unresolved}
