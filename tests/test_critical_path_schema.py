"""CP-01 contract tests for the critical-path schema.

All fixtures below are synthetic: they exercise the schema boundary only.
They do not read profiler exports, build a DAG, solve a path or estimate weights.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from opt_discovery import events, primitives
from opt_discovery.events import (
    HostCall,
    KernelEvent,
    SourceReference,
    TaskIntervalEvent,
    iter_op_summary_events,
)
from opt_discovery.primitives import Window
from opt_discovery.schemas import OperatorRecord, ParseMeta, TaskType
from opt_discovery.schemas.critical_path import (
    ActivityKind,
    AnalysisScope,
    CriticalPathResult,
    CriticalPathStatus,
    DependencyEdge,
    DependencyEvidence,
    DependencyType,
    EvidenceBasis,
    ExecutionNode,
    UnresolvedDependency,
)
from opt_discovery.schemas.metrics import Provenance
from opt_discovery.schemas.quality import (
    BLOCKING,
    EMPTY_TABLE,
    WINDOW_DEGRADED,
    QualityFlag,
    blocking,
)

CAPTURE = "capture-1"
DB_PATH = "PROF_000000/msprof_0.db"
CSV_PATH = "PROF_000000/op_summary_1.csv"


def _ref(
    row: int | None = 2,
    *,
    kind: str = "task_table",
    path: str = DB_PATH,
    note: str | None = None,
) -> SourceReference:
    return SourceReference(kind=kind, path=path, row=row, note=note)


def _node(row: int = 2, **overrides: object) -> ExecutionNode:
    values: dict[str, object] = {
        "capture_id": CAPTURE,
        "source": _ref(row),
        "activity": ActivityKind.DEVICE_TASK,
        "start_us": 1100.0,
        "duration_us": 100.0,
        "rank": 0,
        "stream_id": 47,
        "operator": "MatMul",
        "task_type": TaskType.KERNEL,
    }
    values.update(overrides)
    return ExecutionNode(**values)  # type: ignore[arg-type]


def _scope(**overrides: object) -> AnalysisScope:
    values: dict[str, object] = {
        "capture_id": CAPTURE,
        "workload_id": "wl-synthetic",
        "is_synthetic": True,
        "window": Window(1000.0, 2000.0, rounds=(1,)),
        "ranks": (0,),
        "stream_ids": (47,),
        "clock_domain": "device-0",
    }
    values.update(overrides)
    return AnalysisScope(**values)  # type: ignore[arg-type]


def _evidence(
    basis: EvidenceBasis = EvidenceBasis.STREAM_SEQUENCE,
    provenance: Provenance = Provenance.MEASURED,
    *,
    fields: tuple[str, ...] = ("stream_id", "task_id"),
    refs: tuple[SourceReference, ...] | None = None,
) -> DependencyEvidence:
    return DependencyEvidence(
        source_refs=refs if refs is not None else (_ref(2), _ref(3)),
        basis=basis,
        fields=fields,
        provenance=provenance,
        detail="synthetic fixture",
    )


def _edge(
    source: ExecutionNode,
    target: ExecutionNode,
    *,
    dependency_type: DependencyType = DependencyType.STREAM_ORDER,
    basis: EvidenceBasis = EvidenceBasis.STREAM_SEQUENCE,
    provenance: Provenance = Provenance.MEASURED,
) -> DependencyEdge:
    return DependencyEdge(
        source_node_id=source.node_id,
        target_node_id=target.node_id,
        dependency_type=dependency_type,
        evidence=_evidence(basis, provenance),
    )


def _complete_result() -> CriticalPathResult:
    first = _node(2, start_us=1100.0, duration_us=100.0)
    second = _node(3, start_us=1200.0, duration_us=50.0)
    return CriticalPathResult(
        scope=_scope(),
        status=CriticalPathStatus.COMPLETE,
        path_nodes=(first, second),
        path_edges=(_edge(first, second),),
        critical_path_duration_us=150.0,
        dependency_coverage=1.0,
    )


def test_json_roundtrip_preserves_nodes_edges_evidence_and_scope() -> None:
    first = _node(2, operator="MatMul")
    second = _node(3, operator="Add", start_us=1200.0)
    edge = _edge(first, second)
    unresolved = UnresolvedDependency(
        dependency_type=DependencyType.EXPLICIT_EVENT_WAIT,
        reason_code="missing_event_handle",
        source_node_id=second.node_id,
        source_refs=(_ref(3, kind="cann_api", path=DB_PATH),),
        detail="event handle not collected",
    )
    result = CriticalPathResult(
        scope=_scope(),
        status=CriticalPathStatus.PARTIAL,
        path_nodes=(first, second),
        path_edges=(edge,),
        critical_path_duration_us=200.0,
        dependency_coverage=0.5,
        unresolved_dependencies=(unresolved,),
        quality_flags=(
            QualityFlag(code=WINDOW_DEGRADED, message="synthetic window", severity="degraded"),
        ),
    )

    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    restored = CriticalPathResult.from_dict(payload)

    assert restored == result
    assert restored.scope.window == Window(1000.0, 2000.0, rounds=(1,))
    assert restored.scope.ranks == (0,)
    assert restored.scope.stream_ids == (47,)
    assert restored.scope.clock_domain == "device-0"
    assert restored.scope.is_synthetic is True
    assert restored.status is CriticalPathStatus.PARTIAL
    assert restored.path_nodes[0].node_id == first.node_id
    assert restored.path_nodes[0].end_us == 1200.0
    assert restored.path_edges[0].evidence.basis is EvidenceBasis.STREAM_SEQUENCE
    assert restored.path_edges[0].evidence.provenance is Provenance.MEASURED
    assert restored.unresolved_dependencies[0].reason_code == "missing_event_handle"
    assert restored.quality_flags[0].code == WINDOW_DEGRADED


def test_node_id_is_stable_for_the_same_identifying_source() -> None:
    base = _node(7)
    same = _node(
        7,
        start_us=999.0,
        duration_us=None,
        operator="OtherOperator",
        rank=None,
        task_type=None,
    )
    assert base.node_id == same.node_id
    assert ExecutionNode.from_dict(base.to_dict()).node_id == base.node_id

    without_id = base.to_dict()
    del without_id["node_id"]
    assert ExecutionNode.from_dict(without_id).node_id == base.node_id


def test_node_id_isolates_capture_shard_table_and_row() -> None:
    variants = (
        _node(2),
        _node(2, capture_id="capture-2"),
        _node(2, source=_ref(2, path=CSV_PATH)),
        _node(2, source=_ref(2, kind="cann_api")),
        _node(3),
        _node(2, source=_ref(2, path="PROF_000001/msprof_0.db")),
    )
    assert len({node.node_id for node in variants}) == len(variants)


def test_node_requires_positive_source_row_and_rejects_tampered_id() -> None:
    with pytest.raises(ValueError):
        _node(source=_ref(None))
    with pytest.raises(ValueError):
        _node(source=_ref(0))

    node = _node()

    payload = node.to_dict()
    payload["node_id"] = "f" * 64
    with pytest.raises(ValueError):
        ExecutionNode.from_dict(payload)

    payload = node.to_dict()
    payload["node_id"] = None
    with pytest.raises(ValueError):
        ExecutionNode.from_dict(payload)


def test_reader_communication_scaling_is_applied_once_and_short_duration_survives(
    tmp_path,
) -> None:
    epoch = 1_790_000_000_000_000.0
    path = tmp_path / "op_summary_1.csv"
    path.write_text(
        "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us)\n"
        f"0,hcom_allReduce_,COMMUNICATION,{epoch},100\n",
        encoding="utf-8",
    )
    event = next(iter_op_summary_events(path, rank=0))
    assert event.duration_us == pytest.approx(0.1)
    assert event.task_type is TaskType.COMMUNICATION

    node = ExecutionNode(
        capture_id=CAPTURE,
        source=_ref(2, kind="op_summary", path=CSV_PATH),
        activity=ActivityKind.DEVICE_TASK,
        start_us=event.start_us,
        duration_us=event.duration_us,
        rank=0,
        stream_id=event.stream_id,
        operator=event.operator,
        task_type=event.task_type,
    )
    restored = ExecutionNode.from_dict(json.loads(json.dumps(node.to_dict())))
    assert restored.duration_us == pytest.approx(0.1)
    assert restored.duration_us == node.duration_us
    assert restored.end_us is not None


def test_missing_end_zero_marker_and_sync_or_communication_activity() -> None:
    open_node = _node(duration_us=None)
    assert open_node.end_us is None
    assert ExecutionNode.from_dict(open_node.to_dict()).duration_us is None

    marker = _node(
        activity=ActivityKind.MARKER,
        task_type=TaskType.SYNC,
        start_us=-5.0,
        duration_us=0.0,
        rank=None,
        stream_id=None,
        operator=None,
    )
    assert marker.start_us == -5.0
    assert marker.end_us == -5.0
    assert marker.to_dict()["duration_us"] == 0.0
    assert marker.to_dict()["rank"] is None
    assert marker.task_type is TaskType.SYNC

    communication = _node(3, task_type=TaskType.COMMUNICATION)
    assert communication.task_type is TaskType.COMMUNICATION


@pytest.mark.parametrize("duration", [-1.0, float("nan"), float("inf"), float("-inf")])
def test_node_rejects_negative_or_non_finite_duration(duration: float) -> None:
    with pytest.raises(ValueError):
        _node(duration_us=duration)


@pytest.mark.parametrize("start", [float("nan"), float("inf"), float("-inf")])
def test_node_rejects_non_finite_start(start: float) -> None:
    with pytest.raises(ValueError):
        _node(start_us=start)


def test_node_rejects_end_overflow_but_allows_negative_relative_start() -> None:
    with pytest.raises(ValueError):
        _node(start_us=1e308, duration_us=1e308)

    relative = _node(start_us=-10.0, duration_us=1.0)
    assert relative.start_us == -10.0
    assert relative.end_us == -9.0


@pytest.mark.parametrize(
    ("start", "end"),
    [(float("nan"), 1.0), (0.0, float("inf")), (float("-inf"), 1.0)],
)
def test_scope_rejects_non_finite_window_endpoints(start: float, end: float) -> None:
    with pytest.raises(ValueError):
        AnalysisScope(
            capture_id=CAPTURE,
            workload_id="wl-synthetic",
            is_synthetic=True,
            window=Window(start, end, rounds=(1,)),
        )


def test_scope_rejects_overflowing_window_duration() -> None:
    with pytest.raises(ValueError):
        AnalysisScope(
            capture_id=CAPTURE,
            workload_id="wl-synthetic",
            is_synthetic=True,
            window=Window(-1e308, 1e308, rounds=(1,)),
        )


@pytest.mark.parametrize("start", (True, "1000.0"))
def test_scope_rejects_non_numeric_serialized_window_boundary(start) -> None:
    payload = _scope().to_dict()
    payload["window"]["start_us"] = start
    with pytest.raises(TypeError, match="window.start_us"):
        AnalysisScope.from_dict(payload)


def test_dependency_rejects_empty_evidence_or_field_names() -> None:
    with pytest.raises(ValueError):
        DependencyEvidence(
            source_refs=(),
            basis=EvidenceBasis.STREAM_SEQUENCE,
            fields=("stream_id",),
            provenance=Provenance.MEASURED,
        )
    with pytest.raises(ValueError):
        DependencyEvidence(
            source_refs=(_ref(2),),
            basis=EvidenceBasis.STREAM_SEQUENCE,
            fields=(),
            provenance=Provenance.MEASURED,
        )


def test_edge_rejects_self_edge_empty_endpoint_and_mismatched_basis() -> None:
    first = _node(2)
    second = _node(3)

    with pytest.raises(ValueError):
        DependencyEdge(
            first.node_id,
            first.node_id,
            DependencyType.STREAM_ORDER,
            _evidence(EvidenceBasis.STREAM_SEQUENCE),
        )
    with pytest.raises(ValueError):
        DependencyEdge(
            "",
            second.node_id,
            DependencyType.STREAM_ORDER,
            _evidence(EvidenceBasis.STREAM_SEQUENCE),
        )
    with pytest.raises(ValueError):
        DependencyEdge(
            first.node_id,
            second.node_id,
            DependencyType.STREAM_ORDER,
            _evidence(EvidenceBasis.CONNECTION_ID),
        )


@pytest.mark.parametrize("provenance", [Provenance.DECLARED, Provenance.ASSUMED])
def test_edge_requires_measured_or_derived_evidence(provenance: Provenance) -> None:
    first = _node(2)
    second = _node(3)
    with pytest.raises(ValueError):
        _edge(first, second, provenance=provenance)

    unresolved = UnresolvedDependency(
        DependencyType.STREAM_ORDER,
        "relation_not_provable",
        first.node_id,
        second.node_id,
        (_ref(2), _ref(3)),
    )
    assert unresolved.reason_code == "relation_not_provable"


def test_derived_evidence_is_accepted_and_edges_have_no_implicit_weight() -> None:
    first = _node(2, activity=ActivityKind.HOST_CALL, task_type=None, stream_id=None)
    second = _node(3)
    edge = DependencyEdge(
        first.node_id,
        second.node_id,
        DependencyType.HOST_DEVICE_SUBMISSION,
        _evidence(EvidenceBasis.CONNECTION_ID, Provenance.DERIVED),
    )
    assert edge.dependency_type is DependencyType.HOST_DEVICE_SUBMISSION
    assert edge.evidence.provenance is Provenance.DERIVED
    assert "weight" not in edge.to_dict()
    assert "weight_us" not in edge.to_dict()


@pytest.mark.parametrize("reverse", (False, True))
def test_submission_path_checks_direction_and_accepts_overlapping_host_call(reverse) -> None:
    host = _node(2, activity=ActivityKind.HOST_CALL, stream_id=None, start_us=1100.0)
    device = _node(3, start_us=1150.0)
    source, target = (device, host) if reverse else (host, device)
    edge = DependencyEdge(
        source.node_id,
        target.node_id,
        DependencyType.HOST_DEVICE_SUBMISSION,
        _evidence(EvidenceBasis.CONNECTION_ID, Provenance.DERIVED, fields=("connectionId",)),
    )
    kwargs = {
        "scope": _scope(stream_ids=() if reverse else (47,)),
        "status": CriticalPathStatus.PARTIAL,
        "path_nodes": (source, target),
        "path_edges": (edge,),
    }
    if reverse:
        with pytest.raises(ValueError, match="host_call to device_task"):
            CriticalPathResult(**kwargs)
    else:
        assert CriticalPathResult(**kwargs).path_edges == (edge,)


def test_unresolved_rejects_empty_reason_and_empty_ids() -> None:
    with pytest.raises(ValueError):
        UnresolvedDependency(DependencyType.EXPLICIT_EVENT_WAIT, "")
    with pytest.raises(ValueError):
        UnresolvedDependency(
            DependencyType.EXPLICIT_EVENT_WAIT,
            "missing_event_handle",
            source_node_id="",
        )


def test_complete_requires_full_adjudicated_coverage_and_known_scope() -> None:
    result = _complete_result()
    assert result.observed_window_duration_us == pytest.approx(1000.0)
    assert result.dependency_coverage == 1.0

    with pytest.raises(ValueError):
        replace(result, dependency_coverage=None)
    with pytest.raises(ValueError):
        replace(result, dependency_coverage=0.5)
    with pytest.raises(ValueError):
        replace(
            result,
            scope=_scope(window=None, ranks=(), stream_ids=(), clock_domain=None),
            observed_window_duration_us=None,
        )
    with pytest.raises(ValueError):
        replace(
            result,
            scope=_scope(clock_domain=None),
        )
    with pytest.raises(ValueError):
        replace(
            result,
            unresolved_dependencies=(
                UnresolvedDependency(DependencyType.COLLECTIVE_DEPENDENCY, "missing_collective_id"),
            ),
        )


def test_complete_requires_total_duration() -> None:
    with pytest.raises(ValueError, match="requires a path duration"):
        replace(_complete_result(), critical_path_duration_us=None)


def test_complete_rejects_nodes_without_duration() -> None:
    first = _node(2, duration_us=None)
    second = _node(3)
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(),
            status=CriticalPathStatus.COMPLETE,
            path_nodes=(first, second),
            path_edges=(_edge(first, second),),
            critical_path_duration_us=50.0,
            dependency_coverage=1.0,
        )


def test_blocking_quality_can_only_be_unavailable() -> None:
    flag = QualityFlag(code=EMPTY_TABLE, message="empty export", severity=BLOCKING)
    with pytest.raises(ValueError):
        replace(_complete_result(), quality_flags=(flag,))

    unavailable = CriticalPathResult(
        scope=_scope(),
        status=CriticalPathStatus.UNAVAILABLE,
        quality_flags=(flag,),
    )
    assert unavailable.status is CriticalPathStatus.UNAVAILABLE
    assert blocking(unavailable.quality_flags) == (flag,)

    with pytest.raises(ValueError):
        CriticalPathResult(scope=_scope(), status=CriticalPathStatus.UNAVAILABLE)


def test_unavailable_accepts_an_explicit_unresolved_reason() -> None:
    unresolved = UnresolvedDependency(
        DependencyType.EXPLICIT_EVENT_WAIT,
        "missing_event_handle",
        detail="sync endpoints are not observable",
    )
    result = CriticalPathResult(
        scope=_scope(),
        status=CriticalPathStatus.UNAVAILABLE,
        unresolved_dependencies=(unresolved,),
    )
    assert result.path_nodes == ()
    assert result.path_edges == ()
    assert result.critical_path_duration_us is None


def test_no_path_status_rejects_nodes_edges_or_duration() -> None:
    with pytest.raises(ValueError):
        replace(_complete_result(), status=CriticalPathStatus.UNDETERMINED)
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(),
            status=CriticalPathStatus.UNDETERMINED,
            critical_path_duration_us=1.0,
        )


def test_path_requires_adjacent_unique_in_scope_nodes() -> None:
    first = _node(2)
    second = _node(3, start_us=1200.0)
    third = _node(4, start_us=1300.0)
    first_edge = _edge(first, second)
    second_edge = _edge(second, third)

    valid = CriticalPathResult(
        scope=_scope(),
        status=CriticalPathStatus.PARTIAL,
        path_nodes=(first, second, third),
        path_edges=(first_edge, second_edge),
        critical_path_duration_us=300.0,
        dependency_coverage=0.5,
    )
    assert [node.node_id for node in valid.path_nodes] == [
        first.node_id,
        second.node_id,
        third.node_id,
    ]

    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(first, second),
            path_edges=(second_edge,),
            critical_path_duration_us=300.0,
        )
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(first, second),
            path_edges=(first_edge, first_edge),
            critical_path_duration_us=300.0,
        )
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(first, second, first),
            path_edges=(first_edge, _edge(second, first)),
            critical_path_duration_us=300.0,
        )
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(ranks=(1,)),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(first,),
            path_edges=(),
            critical_path_duration_us=100.0,
        )
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(stream_ids=(99,)),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(first,),
            path_edges=(),
            critical_path_duration_us=100.0,
        )
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(_node(2, capture_id="other-capture"),),
            path_edges=(),
            critical_path_duration_us=100.0,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"rank": 1}, "matching rank"),
        ({"stream_id": 48}, "identical stream_id"),
        ({"start_us": 1199.0}, "finish before"),
    ],
)
def test_stream_order_checks_the_path_endpoints(changes, message) -> None:
    first = _node(2)
    target = replace(_node(3, start_us=1200.0), **changes)
    with pytest.raises(ValueError, match=message):
        CriticalPathResult(
            scope=_scope(ranks=(0, 1), stream_ids=(47, 48)),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(first, target),
            path_edges=(_edge(first, target),),
        )


def test_edge_evidence_requires_a_locatable_record_and_field_names() -> None:
    with pytest.raises(ValueError, match="source.row"):
        _evidence(refs=(_ref(None),))
    payload = _evidence().to_dict()
    with pytest.raises((ValueError, TypeError)):
        DependencyEvidence.from_dict({**payload, "fields": "taskId"})
    payload["source_refs"][0]["kind"] = None
    with pytest.raises(TypeError, match="source.kind"):
        DependencyEvidence.from_dict(payload)


@pytest.mark.parametrize("path", ("/export/task.db", "./C:/export/task.db", "../task.db"))
def test_node_requires_capture_relative_source(path) -> None:
    with pytest.raises(ValueError, match="source.path"):
        _node(source=_ref(path=path))


@pytest.mark.parametrize("field", ("quality_flags", "unresolved_dependencies"))
def test_result_rejects_malformed_audit_arrays(field) -> None:
    with pytest.raises(ValueError, match="JSON array"):
        CriticalPathResult.from_dict({**_complete_result().to_dict(), field: {}})


def test_partial_path_still_requires_known_node_duration() -> None:
    first = _node(2, duration_us=None)
    second = _node(3)
    with pytest.raises(ValueError):
        CriticalPathResult(
            scope=_scope(),
            status=CriticalPathStatus.PARTIAL,
            path_nodes=(first, second),
            path_edges=(_edge(first, second),),
            critical_path_duration_us=10.0,
        )


def test_result_rejects_negative_duration_bad_coverage_and_window_mismatch() -> None:
    result = _complete_result()
    with pytest.raises(ValueError):
        replace(result, critical_path_duration_us=-1.0)
    with pytest.raises(ValueError):
        replace(result, dependency_coverage=1.5)
    with pytest.raises(ValueError):
        replace(result, observed_window_duration_us=999.0)
    with pytest.raises(ValueError):
        replace(
            result,
            scope=_scope(window=None, ranks=(), stream_ids=(), clock_domain=None),
        )


def test_existing_event_and_schema_api_still_imports_and_constructs() -> None:
    event = KernelEvent(
        operator="MatMul",
        rank=0,
        start_us=1.0,
        duration_us=2.0,
        task_type=TaskType.KERNEL,
        source=_ref(2, kind="op_summary", path=CSV_PATH),
    )
    assert event.end_us == 3.0
    assert isinstance(event.source, events.SourceReference)
    assert isinstance(HostCall("launch", 1.0, 2.0, _ref(2)), HostCall)
    assert isinstance(
        TaskIntervalEvent(rank=0, start_us=1.0, duration_us=2.0, source=_ref(2)),
        TaskIntervalEvent,
    )
    assert primitives.Window is Window
    assert OperatorRecord(workload_id="wl-synthetic", operator="MatMul", calls=1).operator == "MatMul"
    assert ParseMeta(parser="synthetic").is_synthetic is False
