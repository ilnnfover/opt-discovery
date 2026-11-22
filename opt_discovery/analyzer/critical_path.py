"""CP-02: evidence-constrained task-stream DAG and longest-path solver.

CP-02A covers one capture/rank/device/stream. A stream-order edge is emitted
only when every local precondition is observed:

* the rows are adjacent records in the selected stream window;
* both are known serial task types (:data:`~opt_discovery.events.TASK_SERIAL_TYPES`);
* capture/source/device/process/context/stream match;
* ``taskId`` increases by exactly one and is not reused in the window;
* ``modelId`` is the known non-graph sentinel (normalized to ``None``);
* intervals are disjoint and starts are distinct.

The official CANN FIFO contract is the ordering basis; timestamps only check
consistency.  A gap is not a dependency and is never given weight.  There is no
trusted denominator for dependency coverage, so it is always ``None``.

CP-02B adds explicit cross-stream ``EVENT_RECORD``/``EVENT_WAIT`` edges.  Such
an edge exists only when a caller supplies an independently observed
:class:`SyncBinding` for both TASK rows with the same explicit handle and
generation.  Time, names, connection IDs and binding absence never create one.
"""

from __future__ import annotations

import posixpath
from collections import Counter, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from ..events import (
    TASK_SERIAL_TYPES,
    EventReadError,
    SourceReference,
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
    "SyncBinding",
    "TaskDag",
    "analyze_task_stream",
    "analyze_task_streams",
    "build_synchronized_task_dag",
    "build_task_dag",
    "solve_task_dag",
]


class ScopeError(ValueError):
    """Raised when a scope is not a single rank/stream or lacks a window."""


#: TASK types that can act as sync endpoints once an explicit binding proves it.
_SYNC_TYPES = frozenset({"EVENT_RECORD", "EVENT_WAIT"})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _capture_relative(path: str, field: str) -> str:
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/") or (len(text) >= 2 and text[0].isalpha() and text[1] == ":"):
        raise ValueError(f"{field} must be capture-relative, not absolute")
    logical = posixpath.normpath(text)
    if logical in ("", "."):
        raise ValueError(f"{field} must identify a record, not the current directory")
    if logical == ".." or logical.startswith("../"):
        raise ValueError(f"{field} must not escape the capture root")
    return logical


def _source_key(source: SourceReference) -> tuple[str, str, int | None]:
    """Index a source by location only; ``note`` is descriptive, not identity."""

    return (source.kind, source.path, source.row)


def _binding_source_key(source: object, field: str) -> tuple[str, str, int]:
    if not isinstance(source, SourceReference):
        raise TypeError(f"{field} must be SourceReference, got {type(source).__name__}")
    kind = _text(source.kind, f"{field}.kind")
    path = _text(source.path, f"{field}.path")
    row = source.row
    if row is None:
        raise ValueError(f"{field}.row is required to locate a binding record")
    if isinstance(row, bool) or not isinstance(row, int):
        raise TypeError(f"{field}.row must be a positive integer")
    if row <= 0:
        raise ValueError(f"{field}.row must be a positive integer, got {row}")
    return (kind, _capture_relative(path, f"{field}.path"), row)


@dataclass(frozen=True)
class SyncBinding:
    """One independently observed TASK-row to event-handle binding.

    The caller supplies and verifies the underlying rows.  This record only
    checks structure: both sources are locatable rows, the evidence row is not
    the TASK row it claims to bind, and an explicit handle/generation is
    carried verbatim.  The TASK type of the row named by ``task_source``
    decides whether it acts as ``EVENT_RECORD`` or ``EVENT_WAIT``.
    """

    capture_id: str
    task_source: SourceReference
    event_handle: str | None
    generation: int | None
    evidence_source: SourceReference

    def __post_init__(self) -> None:
        capture_id = _text(self.capture_id, "capture_id")
        task_key = _binding_source_key(self.task_source, "task_source")
        evidence_key = _binding_source_key(self.evidence_source, "evidence_source")
        if task_key == evidence_key:
            raise ValueError("evidence_source must not be the TASK row being bound")

        handle = self.event_handle
        if handle is not None:
            if not isinstance(handle, str):
                raise TypeError("event_handle must be a string or None")
            handle = handle.strip()
            if not handle:
                raise ValueError("event_handle must be non-empty or None")

        generation = self.generation
        if generation is not None:
            if isinstance(generation, bool) or not isinstance(generation, int):
                raise TypeError("generation must be an integer or None")
            if generation < 0:
                raise ValueError("generation must be non-negative")

        object.__setattr__(self, "capture_id", capture_id)
        object.__setattr__(
            self, "task_source", replace(self.task_source, kind=task_key[0], path=task_key[1])
        )
        object.__setattr__(
            self, "evidence_source", replace(self.evidence_source, kind=evidence_key[0], path=evidence_key[1])
        )
        object.__setattr__(self, "event_handle", handle)
        object.__setattr__(self, "generation", generation)


@dataclass(frozen=True)
class TaskDag:
    """Auditable DAG fragment for the selected streams of one rank."""

    nodes: tuple[ExecutionNode, ...]
    edges: tuple[DependencyEdge, ...]
    unresolved: tuple[UnresolvedDependency, ...]


def _serial(event: TaskExecutionEvent, *, promoted: bool = False) -> bool:
    name = (event.task_type_name or "").strip().upper()
    if name in TASK_SERIAL_TYPES:
        return True
    return promoted and name in _SYNC_TYPES


def _event_key(event: TaskExecutionEvent) -> tuple[str, str, int | None]:
    return _source_key(event.source)


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
    *,
    promoted: bool = False,
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
    if not _serial(event, promoted=promoted):
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


def _require_sync_scope(scope: AnalysisScope) -> int:
    if not isinstance(scope, AnalysisScope):
        raise TypeError("scope must be AnalysisScope")
    if len(scope.ranks) != 1:
        raise ScopeError("scope_not_single_rank")
    if not scope.stream_ids:
        raise ScopeError("scope_no_stream")
    if len(set(scope.stream_ids)) != len(scope.stream_ids):
        raise ScopeError("scope_stream_not_distinct")
    return scope.ranks[0]


def _dedup_events(
    events: Iterable[TaskExecutionEvent],
) -> tuple[list[TaskExecutionEvent], tuple[TaskExecutionEvent, ...]]:
    seen: dict[tuple[str, str, int | None], TaskExecutionEvent] = {}
    result: list[TaskExecutionEvent] = []
    for event in events:
        key = _event_key(event)
        if event.source.row is not None:
            prior = seen.get(key)
            if prior is not None:
                if prior != event:
                    return [], (prior, event)
                continue
            seen[key] = event
        result.append(event)
    return result, ()


def _sync_role(event: TaskExecutionEvent) -> str:
    return (event.task_type_name or "").strip().upper()


def _sync_unresolved(
    reason_code: str,
    *,
    detail: str | None = None,
    events: Sequence[TaskExecutionEvent] = (),
    source_node_id: str | None = None,
    target_node_id: str | None = None,
) -> UnresolvedDependency:
    return UnresolvedDependency(
        dependency_type=DependencyType.EXPLICIT_EVENT_WAIT,
        reason_code=reason_code,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        source_refs=tuple(event.source for event in events),
        detail=detail,
    )


def _binding_unresolved(
    reason_code: str, binding: SyncBinding, *, detail: str | None = None
) -> UnresolvedDependency:
    return UnresolvedDependency(
        dependency_type=DependencyType.EXPLICIT_EVENT_WAIT,
        reason_code=reason_code,
        source_refs=(binding.task_source, binding.evidence_source),
        detail=detail,
    )


def _sync_pair_reason(
    record: TaskExecutionEvent, wait: TaskExecutionEvent, rank: int
) -> str | None:
    """Cross-stream guard.  Time never selects a partner or a generation."""

    if record.capture_id != wait.capture_id:
        return "capture_mismatch"
    if record.rank != rank or wait.rank != rank:
        return "rank_mismatch"
    if record.source.kind != "task_table" or wait.source.kind != "task_table":
        return "source_mismatch"
    if record.source.path != wait.source.path:
        return "source_mismatch"
    if (
        record.device_id is None
        or wait.device_id is None
        or record.device_id != wait.device_id
    ):
        return "device_mismatch"
    if (
        record.global_pid is None
        or wait.global_pid is None
        or record.global_pid != wait.global_pid
    ):
        return "global_pid_mismatch"
    if record.stream_id is None or wait.stream_id is None:
        return "stream_mismatch"
    if record.stream_id == wait.stream_id:
        return "same_stream"
    if record.raw_end_ns is None or wait.raw_end_ns is None:
        return "invalid_sync_time"
    if record.raw_end_ns > wait.raw_end_ns:
        return "record_after_wait_completion"
    if record.node is None or record.node.duration_us is None or wait.raw_end_us is None:
        return "invalid_sync_time"
    if record.node.duration_us > wait.raw_end_us - record.node.start_us:
        return "sync_coordinate_precision"
    return None


def _sync_edge(
    record: TaskExecutionEvent,
    record_binding: SyncBinding,
    wait: TaskExecutionEvent,
    wait_binding: SyncBinding,
) -> DependencyEdge:
    assert record.node is not None
    assert wait.node is not None
    detail = (
        f"capture={record.capture_id} rank={record.rank} "
        f"device={record.device_id} globalPid={record.global_pid} "
        f"streams={record.stream_id}->{wait.stream_id} "
        f"recordTaskId={record.task_id} waitTaskId={wait.task_id} "
        f"generation={record_binding.generation} waitStartNs={wait.raw_start_ns} "
        f"waitEndNs={wait.raw_end_ns} waitWeightUs=0"
    )
    return DependencyEdge(
        source_node_id=record.node.node_id,
        target_node_id=wait.node.node_id,
        dependency_type=DependencyType.EXPLICIT_EVENT_WAIT,
        evidence=DependencyEvidence(
            source_refs=(
                record.source,
                record_binding.evidence_source,
                wait.source,
                wait_binding.evidence_source,
            ),
            basis=EvidenceBasis.EVENT_HANDLE,
            fields=("eventHandle", "generation"),
            provenance=Provenance.DERIVED,
            detail=detail,
        ),
    )


def _wait_milestone(event: TaskExecutionEvent) -> ExecutionNode:
    assert event.node is not None
    assert event.raw_end_us is not None
    return ExecutionNode(
        capture_id=event.capture_id,
        source=event.source,
        activity=event.node.activity,
        start_us=event.raw_end_us,
        duration_us=0.0,
        rank=event.node.rank,
        stream_id=event.node.stream_id,
        operator=event.node.operator,
        task_type=event.node.task_type,
    )


def _dedup_unresolved(
    items: Sequence[UnresolvedDependency],
) -> tuple[UnresolvedDependency, ...]:
    seen: set[tuple[object, ...]] = set()
    result: list[UnresolvedDependency] = []
    for item in items:
        key = (
            item.dependency_type,
            item.reason_code,
            item.source_node_id,
            item.target_node_id,
            tuple(_source_key(ref) for ref in item.source_refs),
            item.detail,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def build_synchronized_task_dag(
    events: Iterable[TaskExecutionEvent],
    scope: AnalysisScope,
    *,
    bindings: Iterable[SyncBinding] = (),
) -> TaskDag:
    """Compose explicit cross-stream waits over disjoint CP-02A local DAGs.

    Only a unique ``EVENT_RECORD`` matched to an ``EVENT_WAIT`` through the same
    explicit handle/generation, capture, TASK DB, rank, device and process can
    form a cross-stream edge.  Every other sync row is an unresolved barrier.
    """
    rank = _require_sync_scope(scope)
    items, conflict = _dedup_events(events)
    if conflict:
        return TaskDag((), (), (_sync_unresolved("conflicting_task_observations", events=conflict),))
    binding_items = tuple(bindings)
    for binding in binding_items:
        if not isinstance(binding, SyncBinding):
            raise TypeError("bindings must contain SyncBinding values")

    if not items:
        return TaskDag(
            nodes=(),
            edges=(),
            unresolved=tuple(
                _binding_unresolved(
                    "binding_task_not_observed",
                    binding,
                    detail="binding names a TASK row that is not in the supplied events",
                )
                for binding in binding_items
            ),
        )

    identities = {
        (
            event.capture_id,
            event.rank,
            event.origin_ns,
            event.source.kind,
            event.source.path,
        )
        for event in items
    }
    devices = {event.device_id for event in items if event.device_id is not None}
    if len(identities) != 1 or len(devices) > 1:
        return TaskDag(
            nodes=(),
            edges=(),
            unresolved=(
                _sync_unresolved(
                    "mixed_sync_scope",
                    detail="events mix capture/rank/device/origin/source",
                    events=items[:1],
                ),
            ),
        )
    if any(event.source.kind != "task_table" for event in items):
        return TaskDag(
            nodes=(),
            edges=(),
            unresolved=(
                _sync_unresolved(
                    "source_mismatch",
                    detail="synchronized task rows must come from a TASK table",
                    events=items[:1],
                ),
            ),
        )

    by_key = {_event_key(event): event for event in items}
    unknown_stream = [event for event in items if event.stream_id is None]
    stream_items = {stream: list(unknown_stream) for stream in scope.stream_ids}
    for event in items:
        if event.stream_id is not None and event.stream_id in stream_items:
            stream_items[event.stream_id].append(event)
    task_counts = {
        stream: Counter(
            event.task_id for event in rows if event.task_id is not None
        )
        for stream, rows in stream_items.items()
    }
    gid_counts = {
        stream: Counter(
            event.global_task_id
            for event in rows
            if event.global_task_id is not None
        )
        for stream, rows in stream_items.items()
    }

    def endpoint_reasons(event: TaskExecutionEvent) -> list[str]:
        stream = event.stream_id
        if stream is None or stream not in stream_items:
            return ["stream_mismatch"]
        return _eligibility_reasons(
            event,
            replace(scope, stream_ids=(stream,)),
            rank,
            stream,
            task_counts[stream],
            gid_counts[stream],
            promoted=True,
        )

    unresolved: list[UnresolvedDependency] = []

    bindings_by_task: dict[
        tuple[str, str, int | None], list[SyncBinding]
    ] = {}
    for binding in binding_items:
        bindings_by_task.setdefault(
            _source_key(binding.task_source), []
        ).append(binding)

    binding_for_task: dict[tuple[str, str, int | None], SyncBinding] = {}
    ambiguous_keys: set[tuple[str, int]] = set()
    for key, binding_group in bindings_by_task.items():
        usable = [binding for binding in binding_group if binding.capture_id == scope.capture_id]
        for binding in binding_group:
            if binding.capture_id != scope.capture_id:
                unresolved.append(
                    _binding_unresolved(
                        "binding_capture_mismatch",
                        binding,
                        detail=(
                            f"binding capture {binding.capture_id!r} != "
                            f"scope capture {scope.capture_id!r}"
                        ),
                    )
                )
        observed_event = by_key.get(key)
        if len(usable) > 1:
            for binding in usable:
                if (
                    observed_event is not None and _sync_role(observed_event) == "EVENT_RECORD"
                    and binding.event_handle is not None and binding.generation is not None
                ):
                    ambiguous_keys.add((binding.event_handle, binding.generation))
                unresolved.append(
                    _binding_unresolved(
                        "duplicate_task_binding",
                        binding,
                        detail="more than one binding observes the same TASK row",
                    )
                )
            continue
        if not usable:
            continue
        binding = usable[0]
        if observed_event is None:
            unresolved.append(
                _binding_unresolved(
                    "binding_task_not_observed",
                    binding,
                    detail="binding names a TASK row that is not in the supplied events",
                )
            )
        elif observed_event.capture_id != binding.capture_id:
            unresolved.append(
                _binding_unresolved(
                    "binding_capture_mismatch",
                    binding,
                    detail="binding capture does not match the TASK row",
                )
            )
        else:
            binding_for_task[key] = binding

    records_by_key: dict[
        tuple[str, int], list[tuple[TaskExecutionEvent, SyncBinding]]
    ] = {}
    ineligible_records: set[tuple[str, str, int | None]] = set()
    for event in items:
        if _sync_role(event) != "EVENT_RECORD":
            continue
        key = _event_key(event)
        record_observation = binding_for_task.get(key)
        if record_observation is None:
            unresolved.append(
                _sync_unresolved(
                    "missing_sync_binding",
                    detail="EVENT_RECORD has no independently observed sync binding",
                    events=(event,),
                )
            )
            continue
        if record_observation.event_handle is None or record_observation.generation is None:
            unresolved.append(
                _sync_unresolved(
                    "missing_sync_identity",
                    detail="binding lacks an explicit event handle or generation",
                    events=(event,),
                )
            )
            continue
        composite = (record_observation.event_handle, record_observation.generation)
        records_by_key.setdefault(composite, []).append((event, record_observation))
        reasons = endpoint_reasons(event)
        if reasons:
            ineligible_records.add(key)
            unresolved.append(
                _sync_unresolved(
                    "sync_endpoint_ineligible",
                    detail=",".join(reasons),
                    events=(event,),
                )
            )
    pending_records: set[tuple[str, int]] = set()
    for composite, group in records_by_key.items():
        if len(group) > 1:
            ambiguous_keys.add(composite)
            for event, _ in group:
                unresolved.append(
                    _sync_unresolved(
                        "ambiguous_record_key",
                        detail=(
                            f"handle={composite[0]!r} generation={composite[1]!r} "
                            f"matches {len(group)} TASK records"
                        ),
                        events=(event,),
                    )
                )
        elif composite not in ambiguous_keys and _event_key(group[0][0]) not in ineligible_records:
            pending_records.add(composite)

    verified: set[tuple[str, str, int | None]] = set()
    verified_records: set[tuple[str, int]] = set()
    milestones: dict[str, ExecutionNode] = {}
    cross_edges: list[DependencyEdge] = []

    for event in items:
        if _sync_role(event) != "EVENT_WAIT":
            continue
        key = _event_key(event)
        wait_binding = binding_for_task.get(key)
        if wait_binding is None:
            unresolved.append(
                _sync_unresolved(
                    "missing_sync_binding",
                    detail="EVENT_WAIT has no independently observed sync binding",
                    events=(event,),
                )
            )
            continue
        if wait_binding.event_handle is None or wait_binding.generation is None:
            unresolved.append(
                _sync_unresolved(
                    "missing_sync_identity",
                    detail="binding lacks an explicit event handle or generation",
                    events=(event,),
                )
            )
            continue
        reasons = endpoint_reasons(event)
        if reasons:
            unresolved.append(
                _sync_unresolved(
                    "sync_endpoint_ineligible",
                    detail=",".join(reasons),
                    events=(event,),
                )
            )
            continue
        composite = (wait_binding.event_handle, wait_binding.generation)
        group = records_by_key.get(composite, [])
        if composite in ambiguous_keys:
            unresolved.append(
                _sync_unresolved(
                    "ambiguous_record_key",
                    detail=(
                        f"handle={composite[0]!r} generation={composite[1]!r} "
                        "has conflicting record observations"
                    ),
                    events=(event,),
                )
            )
            continue
        if not group:
            unresolved.append(
                _sync_unresolved(
                    "missing_record",
                    detail=(
                        f"handle={composite[0]!r} generation={composite[1]!r} "
                        "has no eligible EVENT_RECORD"
                    ),
                    events=(event,),
                )
            )
            continue
        record, record_binding = group[0]
        pair_reason = (
            "sync_record_ineligible" if _event_key(record) in ineligible_records
            else _sync_pair_reason(record, event, rank)
        )
        if pair_reason is not None:
            unresolved.append(
                _sync_unresolved(
                    pair_reason,
                    detail="explicit handle/generation matched but the cross-stream guard failed",
                    events=(record, event),
                )
            )
            continue
        cross_edges.append(
            _sync_edge(record, record_binding, event, wait_binding)
        )
        verified_records.add(composite)
        verified.add(_event_key(record))
        verified.add(key)
        assert event.node is not None
        milestones[event.node.node_id] = _wait_milestone(event)

    for composite in records_by_key:
        if composite not in pending_records or composite in verified_records:
            continue
        event, _ = records_by_key[composite][0]
        unresolved.append(
            _sync_unresolved(
                "unmatched_record",
                detail=(
                    f"handle={composite[0]!r} generation={composite[1]!r} "
                    "has no eligible EVENT_WAIT"
                ),
                events=(event,),
            )
        )

    nodes: list[ExecutionNode] = []
    edges: list[DependencyEdge] = []
    local_unresolved: list[UnresolvedDependency] = []
    for stream in scope.stream_ids:
        local = _build_task_dag(
            stream_items[stream],
            replace(scope, stream_ids=(stream,)),
            verified_sync_ids=frozenset(verified),
        )
        nodes.extend(local.nodes)
        edges.extend(local.edges)
        local_unresolved.extend(local.unresolved)

    final_nodes = tuple(milestones.get(node.node_id, node) for node in nodes)
    node_ids = [node.node_id for node in final_nodes]
    if len(set(node_ids)) != len(node_ids):
        return TaskDag(
            nodes=(),
            edges=(),
            unresolved=(
                _sync_unresolved(
                    "duplicate_node_identity",
                    detail="synchronized composition produced duplicate node identity",
                ),
            ),
        )

    all_edges = tuple(edges) + tuple(cross_edges)
    index = set(node_ids)
    if any(
        edge.source_node_id not in index or edge.target_node_id not in index
        for edge in all_edges
    ):
        return TaskDag(
            nodes=(),
            edges=(),
            unresolved=(
                _sync_unresolved(
                    "sync_endpoint_not_admitted",
                    detail="an explicit wait edge references a row that failed admission",
                ),
            ),
        )

    return TaskDag(
        nodes=final_nodes,
        edges=all_edges,
        unresolved=_dedup_unresolved([*local_unresolved, *unresolved]),
    )


def build_task_dag(
    events: Iterable[TaskExecutionEvent], scope: AnalysisScope
) -> TaskDag:
    """Build the local stream DAG.  Cross-scope and weak relations stay unresolved."""
    return _build_task_dag(events, scope)


def _build_task_dag(
    events: Iterable[TaskExecutionEvent],
    scope: AnalysisScope,
    *,
    verified_sync_ids: frozenset[tuple[str, str, int | None]] = frozenset(),
) -> TaskDag:
    """Local stream DAG, optionally admitting explicitly verified sync rows.

    The allow set only lifts the serial-type gate.  Every other eligibility
    rule (identity, scope, graph tag, times, reuse) still applies, so a sync
    row that fails admission remains an ordinary barrier.
    """
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
        promoted = _event_key(event) in verified_sync_ids
        reasons = _eligibility_reasons(
            event,
            scope,
            rank,
            stream_id,
            task_counts,
            gid_counts,
            promoted=promoted,
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


def analyze_task_streams(
    db_path: str | Path,
    scope: AnalysisScope,
    *,
    source_path: str,
    device_id: int,
    bindings: Iterable[SyncBinding] = (),
    quality_flags: Iterable[QualityFlag] = (),
) -> CriticalPathResult:
    """Read several explicitly selected streams on one rank and solve one DAG.

    Every stream is read through the existing read-only TASK iterator.  Rows
    with an unknown stream are yielded once per request and deduplicated by
    TASK reference, but still act as barriers in every requested stream.
    """
    flags = tuple(quality_flags)
    try:
        rank = _require_sync_scope(scope)
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

    events: list[TaskExecutionEvent] = []
    try:
        for stream_id in scope.stream_ids:
            for event in iter_task_execution_events(
                db_path,
                capture_id=scope.capture_id,
                source_path=source_path,
                rank=rank,
                device_id=device_id,
                stream_id=stream_id,
                window=scope.window,
            ):
                events.append(event)
    except EventReadError as exc:
        return _unavailable(
            relative_scope,
            "source_unavailable",
            detail=str(exc),
            quality_flags=flags,
        )

    dag = build_synchronized_task_dag(events, relative_scope, bindings=bindings)
    return solve_task_dag(dag, relative_scope, quality_flags=flags)
