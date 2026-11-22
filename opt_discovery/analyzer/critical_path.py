"""CP-02A: evidence-constrained task-stream DAG and longest-path solver.

Scope is deliberately narrow: one capture, one rank, one device, one stream.
A stream-order edge is emitted only when every local precondition is observed:

* the rows are adjacent records in the selected stream window;
* both are known serial task types (:data:`~opt_discovery.events.TASK_SERIAL_TYPES`);
* capture/source/device/process/context/stream match;
* ``taskId`` increases by exactly one and is not reused in the window;
* ``modelId`` is the known non-graph sentinel (normalized to ``None``);
* intervals are disjoint and starts are distinct.

The official CANN FIFO contract is the ordering basis; timestamps only check
consistency.  A gap is not a dependency and is never given weight.  There is no
trusted denominator for dependency coverage, so it is always ``None``.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from ..events import (
    TASK_SERIAL_TYPES,
    EventReadError,
    TaskExecutionEvent,
    iter_task_execution_events,
    task_relative_window,
)
from ..schemas.critical_path import (
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
from ..schemas.metrics import Provenance
from ..schemas.quality import QualityFlag, blocking

__all__ = [
    "ScopeError",
    "TaskDag",
    "analyze_task_stream",
    "build_task_dag",
    "solve_task_dag",
]


class ScopeError(ValueError):
    """Raised when a scope is not a single rank/stream or lacks a window."""


@dataclass(frozen=True)
class TaskDag:
    """Small, auditable DAG fragment for one stream."""

    nodes: tuple[ExecutionNode, ...]
    edges: tuple[DependencyEdge, ...]
    unresolved: tuple[UnresolvedDependency, ...]


def _serial(event: TaskExecutionEvent) -> bool:
    name = (event.task_type_name or "").strip().upper()
    return name in TASK_SERIAL_TYPES


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _require_single_scope(scope: AnalysisScope) -> tuple[int, int]:
    if not isinstance(scope, AnalysisScope):
        raise TypeError("scope must be AnalysisScope")
    if len(scope.ranks) != 1:
        raise ScopeError("scope_not_single_rank")
    if len(scope.stream_ids) != 1:
        raise ScopeError("scope_not_single_stream")
    return scope.ranks[0], scope.stream_ids[0]


def _unresolved(
    reason_code: str,
    *,
    detail: str | None = None,
    events: Sequence[TaskExecutionEvent] = (),
    source_node_id: str | None = None,
    target_node_id: str | None = None,
) -> UnresolvedDependency:
    return UnresolvedDependency(
        dependency_type=DependencyType.STREAM_ORDER,
        reason_code=reason_code,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        source_refs=tuple(event.source for event in events),
        detail=detail,
    )


def _order_key(event: TaskExecutionEvent) -> tuple[int, int, int]:
    if event.raw_start_ns is None:
        return (1, 0, event.source.row or 0)
    return (0, event.raw_start_ns, event.source.row or 0)


def _eligibility_reasons(
    event: TaskExecutionEvent,
    scope: AnalysisScope,
    rank: int,
    stream_id: int,
    task_counts: Counter[int],
    gid_counts: Counter[int],
) -> list[str]:
    reasons: list[str] = list(event.reasons)
    if event.node is None:
        reasons.append("missing_node")
    elif event.node.duration_us is None:
        reasons.append("missing_duration")
    if event.capture_id != scope.capture_id:
        reasons.append("capture_mismatch")
    if event.rank != rank:
        reasons.append("rank_mismatch")
    if event.source.kind != "task_table":
        reasons.append("source_mismatch")
    if event.device_id is None or event.device_id < 0:
        reasons.append("missing_device_id")
    if event.stream_id is None or event.stream_id != stream_id:
        reasons.append("stream_mismatch")
    if event.global_pid is None:
        reasons.append("missing_global_pid")
    if event.context_id is None:
        reasons.append("missing_context_id")
    if event.task_id is None:
        reasons.append("missing_task_id")
    if event.global_task_id is None:
        reasons.append("missing_global_task_id")
    if event.raw_start_ns is None:
        reasons.append("missing_start")
    if event.raw_end_ns is None:
        reasons.append("missing_end")
    if event.crosses_boundary:
        reasons.append("window_crossing")
    if not _serial(event):
        reasons.append("task_type_not_serial")
    if event.model_id_state == "graph":
        reasons.append("model_is_graph")
    elif event.model_id_state != "non_graph":
        reasons.append("missing_model_id")
    if event.task_id is not None and task_counts[event.task_id] > 1:
        reasons.append("task_id_reused")
    if event.global_task_id is not None and gid_counts[event.global_task_id] > 1:
        reasons.append("global_task_id_reused")
    return _unique(reasons)


def _pair_reason(
    previous: TaskExecutionEvent,
    current: TaskExecutionEvent,
    task_counts: Counter[int],
    gid_counts: Counter[int],
) -> str | None:
    previous_node = previous.node
    current_node = current.node
    if previous_node is None or previous_node.duration_us is None:
        return "unusable_predecessor"
    if current_node is None or current_node.duration_us is None:
        return "unusable_current"
    if previous.capture_id != current.capture_id:
        return "capture_mismatch"
    if (
        previous.source.kind != current.source.kind
        or previous.source.path != current.source.path
    ):
        return "source_mismatch"
    if previous.rank != current.rank:
        return "rank_mismatch"
    if (
        previous.device_id is None
        or current.device_id is None
        or previous.device_id != current.device_id
    ):
        return "device_mismatch"
    if (
        previous.stream_id is None
        or current.stream_id is None
        or previous.stream_id != current.stream_id
    ):
        return "stream_mismatch"
    if (
        previous.global_pid is None
        or current.global_pid is None
        or previous.global_pid != current.global_pid
    ):
        return "global_pid_mismatch"
    if (
        previous.context_id is None
        or current.context_id is None
        or previous.context_id != current.context_id
    ):
        return "context_mismatch"
    if not _serial(previous) or not _serial(current):
        return "task_type_not_serial"
    if previous.model_id_state == "graph" or current.model_id_state == "graph":
        return "model_is_graph"
    if (
        previous.model_id_state != "non_graph"
        or current.model_id_state != "non_graph"
    ):
        return "missing_model_id"
    if previous.task_id is None or current.task_id is None:
        return "missing_task_id"
    if previous.global_task_id is None or current.global_task_id is None:
        return "missing_global_task_id"
    if task_counts[previous.task_id] > 1 or task_counts[current.task_id] > 1:
        return "task_id_reused"
    if previous.global_task_id is not None and gid_counts[previous.global_task_id] > 1:
        return "global_task_id_reused"
    if current.global_task_id is not None and gid_counts[current.global_task_id] > 1:
        return "global_task_id_reused"
    if current.task_id != previous.task_id + 1:
        return "task_id_not_adjacent"
    if (
        previous.raw_start_ns is None
        or previous.raw_end_ns is None
        or current.raw_start_ns is None
        or current.raw_end_ns is None
    ):
        return "unusable_predecessor"
    if current.raw_start_ns <= previous.raw_start_ns:
        return "equal_start"
    if previous.raw_end_ns > current.raw_start_ns:
        return "overlapping_intervals"
    if previous_node.duration_us > current_node.start_us - previous_node.start_us:
        # CP-01 also checks the serialized coordinates. Preserve the original
        # duration even when float subtraction makes an exact raw touch fail.
        return "coordinate_precision"
    return None


def _edge(previous: TaskExecutionEvent, current: TaskExecutionEvent) -> DependencyEdge:
    assert previous.node is not None
    assert current.node is not None
    return DependencyEdge(
        source_node_id=previous.node.node_id,
        target_node_id=current.node.node_id,
        dependency_type=DependencyType.STREAM_ORDER,
        evidence=DependencyEvidence(
            source_refs=(previous.source, current.source),
            basis=EvidenceBasis.STREAM_SEQUENCE,
            fields=(
                "taskId",
                "globalTaskId",
                "streamId",
                "deviceId",
                "globalPid",
                "contextId",
                "modelId",
                "taskType",
            ),
            provenance=Provenance.DERIVED,
            detail=(
                "same capture/source/device/process/stream and subgraph tag. "
                "Consecutive unique taskId under CANN stream FIFO. "
                "Timestamps checked for disjointness."
            ),
        ),
    )


def build_task_dag(
    events: Iterable[TaskExecutionEvent], scope: AnalysisScope
) -> TaskDag:
    """Build the local stream DAG.  Cross-scope and weak relations stay unresolved."""
    rank, stream_id = _require_single_scope(scope)
    items = sorted(events, key=_order_key)
    if not items:
        return TaskDag(nodes=(), edges=(), unresolved=())

    task_counts = Counter(
        event.task_id for event in items if event.task_id is not None
    )
    gid_counts = Counter(
        event.global_task_id
        for event in items
        if event.global_task_id is not None
    )

    nodes: list[ExecutionNode] = []
    edges: list[DependencyEdge] = []
    unresolved: list[UnresolvedDependency] = []

    # Without a usable stream position no ordering can be proved at all.
    unknown_position = any(event.raw_start_ns is None for event in items)
    if unknown_position:
        unresolved.append(
            _unresolved(
                "unknown_stream_position",
                detail="at least one TASK row has no startNs; stream order is unprovable",
                events=items,
            )
        )

    previous: TaskExecutionEvent | None = None
    previous_eligible = False
    previous_overlapped = False
    prefix_max_end_ns: int | float = float("-inf")

    for index, event in enumerate(items):
        reasons = _eligibility_reasons(
            event, scope, rank, stream_id, task_counts, gid_counts
        )
        eligible = not reasons
        for row_reason in reasons:
            unresolved.append(
                _unresolved(
                    row_reason,
                    detail=f"task_id={event.task_id!r} row={event.source.row!r}",
                    events=(event,),
                )
            )
        if eligible:
            assert event.node is not None
            nodes.append(event.node)

        if event.raw_start_ns is None:
            current_overlapped = True
        else:
            current_overlapped = prefix_max_end_ns > event.raw_start_ns or (
                previous is not None and previous.raw_start_ns == event.raw_start_ns
            )

        if previous is not None and not unknown_position:
            reason: str | None
            if not previous_eligible or not eligible:
                reason = (
                    "unusable_predecessor" if not previous_eligible else "unusable_current"
                )
            else:
                reason = _pair_reason(previous, event, task_counts, gid_counts)
            if reason is None and (previous_overlapped or current_overlapped):
                reason = "earlier_interval_overlaps"
            if reason is None and index + 1 < len(items):
                next_start = items[index + 1].raw_start_ns
                assert event.raw_end_ns is not None
                if next_start is not None and (
                    next_start < event.raw_end_ns or next_start == event.raw_start_ns
                ):
                    reason = "later_interval_overlaps"
            if reason is None:
                edges.append(_edge(previous, event))
            else:
                unresolved.append(
                    _unresolved(
                        reason,
                        detail=(
                            "adjacent stream records cannot be connected; "
                            "no time-only or skipped-record edge is allowed"
                        ),
                        events=(previous, event),
                        source_node_id=(
                            previous.node.node_id if previous.node is not None else None
                        ),
                        target_node_id=(
                            event.node.node_id if event.node is not None else None
                        ),
                    )
                )

        previous = event
        previous_eligible = eligible
        previous_overlapped = current_overlapped
        if event.raw_start_ns is None or event.raw_end_ns is None:
            prefix_max_end_ns = float("inf")
        elif event.raw_end_ns < event.raw_start_ns:
            prefix_max_end_ns = float("inf")
        else:
            prefix_max_end_ns = max(prefix_max_end_ns, event.raw_end_ns)

    return TaskDag(
        nodes=tuple(nodes),
        edges=tuple(edges),
        unresolved=tuple(unresolved),
    )


def _unavailable(
    scope: AnalysisScope,
    reason_code: str,
    *,
    detail: str | None = None,
    quality_flags: tuple[QualityFlag, ...] = (),
) -> CriticalPathResult:
    return CriticalPathResult(
        scope=scope,
        status=CriticalPathStatus.UNAVAILABLE,
        quality_flags=quality_flags,
        unresolved_dependencies=(
            UnresolvedDependency(
                dependency_type=DependencyType.STREAM_ORDER,
                reason_code=reason_code,
                detail=detail,
            ),
        ),
    )


def solve_task_dag(
    dag: TaskDag,
    scope: AnalysisScope,
    *,
    quality_flags: Iterable[QualityFlag] = (),
) -> CriticalPathResult:
    """Kahn topological order + O(V+E) longest node-duration path."""
    flags = tuple(quality_flags)
    unresolved = list(dag.unresolved)

    if blocking(flags):
        unresolved.append(_unresolved("blocking_quality_flag"))
        return CriticalPathResult(
            scope=scope,
            status=CriticalPathStatus.UNAVAILABLE,
            quality_flags=flags,
            unresolved_dependencies=tuple(unresolved),
        )

    if any(node.duration_us is None for node in dag.nodes):
        unresolved.append(_unresolved("missing_duration"))
        return CriticalPathResult(
            scope=scope,
            status=CriticalPathStatus.UNAVAILABLE,
            quality_flags=flags,
            unresolved_dependencies=tuple(unresolved),
        )

    if not dag.nodes:
        unresolved.append(_unresolved("no_usable_nodes"))
        return CriticalPathResult(
            scope=scope,
            status=CriticalPathStatus.UNDETERMINED,
            quality_flags=flags,
            unresolved_dependencies=tuple(unresolved),
        )

    nodes = dag.nodes
    index = {node.node_id: position for position, node in enumerate(nodes)}
    if len(index) != len(nodes):
        raise ValueError("TaskDag contains duplicate node_id values")

    edge_map: dict[tuple[int, int], DependencyEdge] = {}
    for edge in dag.edges:
        if edge.source_node_id not in index or edge.target_node_id not in index:
            raise ValueError("TaskDag edge references a node outside the graph")
        source = index[edge.source_node_id]
        target = index[edge.target_node_id]
        edge_map.setdefault((source, target), edge)

    adjacency: list[list[int]] = [[] for _ in nodes]
    indegree = [0 for _ in nodes]
    for source, target in edge_map:
        adjacency[source].append(target)
        indegree[target] += 1

    ready = deque(
        position for position, degree in enumerate(indegree) if degree == 0
    )
    order: list[int] = []
    while ready:
        source = ready.popleft()
        order.append(source)
        for target in adjacency[source]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)

    if len(order) != len(nodes):
        unresolved.append(_unresolved("dependency_cycle"))
        return CriticalPathResult(
            scope=scope,
            status=CriticalPathStatus.UNAVAILABLE,
            quality_flags=flags,
            unresolved_dependencies=tuple(unresolved),
        )

    durations: list[float] = []
    for node in nodes:
        duration = node.duration_us
        assert duration is not None
        durations.append(float(duration))
    distance = list(durations)
    predecessor = [-1 for _ in nodes]
    for source in order:
        for target in adjacency[source]:
            candidate = distance[source] + durations[target]
            if candidate > distance[target]:
                distance[target] = candidate
                predecessor[target] = source

    # Ties keep the earliest node in the declared graph input order.
    end = max(range(len(nodes)), key=lambda position: (distance[position], -position))
    chain: list[int] = []
    cursor = end
    while cursor != -1:
        chain.append(cursor)
        cursor = predecessor[cursor]
    chain.reverse()

    path_nodes = tuple(nodes[position] for position in chain)
    path_edges: list[DependencyEdge] = []
    for source, target in zip(chain, chain[1:], strict=False):
        path_edges.append(edge_map[source, target])

    return CriticalPathResult(
        scope=scope,
        status=CriticalPathStatus.PARTIAL,
        path_nodes=path_nodes,
        path_edges=tuple(path_edges),
        critical_path_duration_us=distance[end],
        dependency_coverage=None,
        unresolved_dependencies=tuple(unresolved),
        quality_flags=flags,
    )


def analyze_task_stream(
    db_path: str | Path,
    scope: AnalysisScope,
    *,
    source_path: str,
    device_id: int,
    quality_flags: Iterable[QualityFlag] = (),
) -> CriticalPathResult:
    """Read, build and solve one device stream without materializing the batch."""
    flags = tuple(quality_flags)
    try:
        rank, stream_id = _require_single_scope(scope)
    except ScopeError as exc:
        return _unavailable(scope, str(exc), quality_flags=flags)

    if scope.window is None:
        return _unavailable(scope, "scope_window_missing", quality_flags=flags)

    relative_window, origin_ns = task_relative_window(scope.window)
    relative_scope = replace(
        scope,
        window=relative_window,
        clock_domain=(
            f"{scope.clock_domain or f'device-{device_id}'}:origin_ns={origin_ns}"
        ),
    )

    if blocking(flags):
        return solve_task_dag(
            TaskDag(nodes=(), edges=(), unresolved=()),
            relative_scope,
            quality_flags=flags,
        )

    try:
        events = list(
            iter_task_execution_events(
                db_path,
                capture_id=scope.capture_id,
                source_path=source_path,
                rank=rank,
                device_id=device_id,
                stream_id=stream_id,
                window=scope.window,
            )
        )
    except EventReadError as exc:
        return _unavailable(
            relative_scope,
            "source_unavailable",
            detail=str(exc),
            quality_flags=flags,
        )

    dag = build_task_dag(events, relative_scope)
    return solve_task_dag(dag, relative_scope, quality_flags=flags)
