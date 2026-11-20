"""Before/after semantic diff for ``analyze`` artifacts (AE-08).

The diff is intentionally narrow: it reads the artifacts this repository actually
writes, ignores non-semantic noise (timestamps, run ids, randomized opportunity
instance ids and machine paths), and reports real content changes including
"zero vs missing".

It is **not** a quality gate.  It is an evidence report attached to a PR or a
verification summary.  A missing/unreadable input fails closed with a non-zero
exit code instead of claiming "no differences".

Typical use::

    python scripts/compare_artifacts.py \
        --before /tmp/run-before --after /tmp/run-after \
        --out /tmp/artifact-diff.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "1.0"

STATUS_COMPARED = "compared"
STATUS_INVALID_INPUT = "invalid_input"
STATUS_READ_ERROR = "read_error"

WORKLOAD_ARTIFACTS = (
    "operator_table.json",
    "diagnosis.json",
    "opportunities.json",
    "timeline.json",
    "comm.json",
    "graph_coverage.json",
)
ROOT_ARTIFACTS = (
    "hotspots.json",
    "workload_summary.csv",
    "operator_summary.csv",
    "source_map.json",
    "run_manifest.json",
)
COMPARE_GLOB = "compare_*.json"

#: Fields that are randomized or machine-specific and never semantic.
NOISE_KEYS = frozenset(
    {
        "created_at",
        "generated_at",
        "instance_id",
        "run_id",
        "previous_run_id",
        "db_path",
    }
)

_MISSING = object()


def _jsonable(value: Any) -> Any:
    if value is _MISSING:
        return {"state": "missing"}
    return value


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _read_csv(
    path: Path,
) -> tuple[list[dict[str, str | None]], list[str] | None, str | None]:
    """Strictly read a CSV artifact.

    ``csv.DictReader`` silently accepts a truncated quoted field (the last row
    becomes one long value with ``None`` in the remaining columns) and short /
    long rows.  Both are unreadable artifacts, not "no differences", so the
    reader is deliberately strict and every row must match the header width.
    """
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                return [], None, "CSV 没有表头"
            fieldnames = [name.strip() for name in header]
            if not fieldnames or not any(fieldnames):
                return [], None, "CSV 没有表头"
            rows: list[dict[str, str | None]] = []
            for number, values in enumerate(reader, start=2):
                if not values:
                    # A blank separator line carries no row keys; reject it
                    # rather than silently skipping unreadable content.
                    return [], None, f"CSV 第 {number} 行为空"
                if len(values) != len(fieldnames):
                    return (
                        [],
                        None,
                        f"CSV 第 {number} 行有 {len(values)} 列，表头有 "
                        f"{len(fieldnames)} 列（短行/长行）",
                    )
                rows.append(
                    {
                        name: (values[index] if index < len(values) else None)
                        for index, name in enumerate(fieldnames)
                    }
                )
            return rows, fieldnames, None
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return [], None, f"{type(exc).__name__}: {exc}"


def _get(data: Any, path: Sequence[str]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _values_equal(left: Any, right: Any, *, float_tolerance: float) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if left == right:
            return True
        scale = max(1.0, abs(float(left)), abs(float(right)))
        return abs(float(left) - float(right)) <= float_tolerance * scale
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(
            _values_equal(left[key], right[key], float_tolerance=float_tolerance)
            for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _values_equal(a, b, float_tolerance=float_tolerance)
            for a, b in zip(left, right, strict=False)
        )
    return left == right


def _add_change(
    changes: list[dict[str, Any]],
    *,
    category: str,
    artifact: str,
    change: str,
    path: str | None = None,
    workload: str | None = None,
    before: Any = _MISSING,
    after: Any = _MISSING,
) -> None:
    item: dict[str, Any] = {
        "category": category,
        "artifact": artifact,
        "change": change,
    }
    if workload:
        item["workload"] = workload
    if path:
        item["path"] = path
    if before is not _MISSING:
        item["before"] = _jsonable(before)
    if after is not _MISSING:
        item["after"] = _jsonable(after)
    changes.append(item)


def _diff_fields(
    changes: list[dict[str, Any]],
    *,
    category: str,
    artifact: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fields: Mapping[str, tuple[str, ...]],
    workload: str | None = None,
    float_tolerance: float,
) -> None:
    for label, path in fields.items():
        left = _get(before, path)
        right = _get(after, path)
        if not _values_equal(left, right, float_tolerance=float_tolerance):
            _add_change(
                changes,
                category=category,
                artifact=artifact,
                workload=workload,
                path=label,
                change="modified",
                before=left,
                after=right,
            )


def _keyed(items: Iterable[Mapping[str, Any]], key_fields: Sequence[str]) -> dict[tuple, Mapping[str, Any]]:
    result: dict[tuple, Mapping[str, Any]] = {}
    seen: dict[tuple, int] = {}
    for item in items:
        key = tuple(item.get(field) for field in key_fields)
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count > 1:
            key = key + (f"#{count}",)
        result[key] = item
    return result


def _diff_records(
    changes: list[dict[str, Any]],
    *,
    category: str,
    artifact: str,
    workload: str,
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    key_fields: Sequence[str],
    value_fields: Mapping[str, tuple[str, ...]],
    float_tolerance: float,
) -> None:
    left = _keyed(before, key_fields)
    right = _keyed(after, key_fields)
    for key in sorted(set(left) - set(right), key=str):
        _add_change(
            changes,
            category=category,
            artifact=artifact,
            workload=workload,
            path="row:" + "|".join(str(part) for part in key),
            change="removed",
            before=dict(left[key]),
        )
    for key in sorted(set(right) - set(left), key=str):
        _add_change(
            changes,
            category=category,
            artifact=artifact,
            workload=workload,
            path="row:" + "|".join(str(part) for part in key),
            change="added",
            after=dict(right[key]),
        )
    for key in sorted(set(left) & set(right), key=str):
        row_label = "row:" + "|".join(str(part) for part in key)
        for label, path in value_fields.items():
            left_value = _get(left[key], path)
            right_value = _get(right[key], path)
            if not _values_equal(left_value, right_value, float_tolerance=float_tolerance):
                _add_change(
                    changes,
                    category=category,
                    artifact=artifact,
                    workload=workload,
                    path=f"{row_label}.{label}",
                    change="modified",
                    before=left_value,
                    after=right_value,
                )


def _known_artifacts(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for name in ROOT_ARTIFACTS:
        path = root / name
        if path.is_file():
            found[name] = path
    for path in sorted(root.glob(COMPARE_GLOB)):
        if path.is_file():
            found[path.name] = path
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        for name in WORKLOAD_ARTIFACTS:
            path = child / name
            if path.is_file():
                found[f"{child.name}/{name}"] = path
    return found


def _workload_names(root: Path) -> set[str]:
    names: set[str] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if any((child / name).is_file() for name in WORKLOAD_ARTIFACTS):
            names.add(child.name)
    return names


def _check_sequence(
    diff: "_Diff", artifact: str, payload: Mapping[str, Any], field: str
) -> bool:
    """Reject a key container whose type cannot be diffed meaningfully."""
    if field not in payload:
        return True
    value = payload[field]
    if not isinstance(value, list):
        diff.errors.append(
            f"{artifact}: {field} 应为列表，实际 {type(value).__name__}"
        )
        return False
    if any(not isinstance(item, Mapping) for item in value):
        diff.errors.append(f"{artifact}: {field} 含非对象条目")
        return False
    return True


def _check_mapping(
    diff: "_Diff", artifact: str, payload: Mapping[str, Any], field: str
) -> bool:
    if field not in payload:
        return True
    value = payload[field]
    if not isinstance(value, Mapping):
        diff.errors.append(
            f"{artifact}: {field} 应为对象，实际 {type(value).__name__}"
        )
        return False
    return True


class _Diff:
    def __init__(self, *, float_tolerance: float) -> None:
        self.float_tolerance = float_tolerance
        self.changes: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.ignored_noise: set[str] = set(NOISE_KEYS)

    def read_json(self, path: Path) -> Any:
        data, error = _read_json(path)
        if error is not None:
            self.errors.append(f"{path.name}: {error}")
            return None
        if not isinstance(data, Mapping):
            self.errors.append(
                f"{path.name}: 顶层应为 JSON 对象，实际 {type(data).__name__}"
            )
            return None
        return data

    def add_field_changes(
        self,
        *,
        category: str,
        artifact: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        fields: Mapping[str, tuple[str, ...]],
        workload: str | None = None,
    ) -> None:
        _diff_fields(
            self.changes,
            category=category,
            artifact=artifact,
            before=before,
            after=after,
            fields=fields,
            workload=workload,
            float_tolerance=self.float_tolerance,
        )

    def add_rows(
        self,
        *,
        category: str,
        artifact: str,
        workload: str,
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]],
        key_fields: Sequence[str],
        value_fields: Mapping[str, tuple[str, ...]],
    ) -> None:
        _diff_records(
            self.changes,
            category=category,
            artifact=artifact,
            workload=workload,
            before=before,
            after=after,
            key_fields=key_fields,
            value_fields=value_fields,
            float_tolerance=self.float_tolerance,
        )

    def add(self, **kwargs: Any) -> None:
        _add_change(self.changes, **kwargs)


def _compare_quality_flags(diff: _Diff, workload: str, before: Any, after: Any) -> None:
    def flags(payload: Any) -> dict[str, Mapping[str, Any]] | Any:
        result: dict[str, Mapping[str, Any]] = {}
        if not isinstance(payload, list):
            return payload
        for item in payload:
            if isinstance(item, Mapping) and isinstance(item.get("code"), str):
                result[item["code"]] = item
            else:
                result[f"<invalid:{len(result)}>"] = {"value": item}
        return result

    left = flags(before)
    right = flags(after)
    artifact = "operator_table.json"
    path = "meta.extra.quality_flags"
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        if not _values_equal(left, right, float_tolerance=diff.float_tolerance):
            diff.add(
                category="quality_flag",
                artifact=artifact,
                workload=workload,
                path=path,
                change="modified",
                before=left,
                after=right,
            )
        return
    for code in sorted(set(left) - set(right)):
        diff.add(
            category="quality_flag",
            artifact=artifact,
            workload=workload,
            path=f"{path}[{code}]",
            change="removed",
            before=dict(left[code]),
        )
    for code in sorted(set(right) - set(left)):
        diff.add(
            category="quality_flag",
            artifact=artifact,
            workload=workload,
            path=f"{path}[{code}]",
            change="added",
            after=dict(right[code]),
        )
    for code in sorted(set(left) & set(right)):
        if not _values_equal(left[code], right[code], float_tolerance=diff.float_tolerance):
            diff.add(
                category="quality_flag",
                artifact=artifact,
                workload=workload,
                path=f"{path}[{code}]",
                change="modified",
                before=dict(left[code]),
                after=dict(right[code]),
            )


def _compare_operator_table(diff: _Diff, workload: str, before: Mapping, after: Mapping) -> None:
    artifact = "operator_table.json"
    _check_sequence(diff, artifact, before, "records")
    _check_sequence(diff, artifact, after, "records")
    diff.add_field_changes(
        category="operator_table",
        artifact=artifact,
        workload=workload,
        before=before,
        after=after,
        fields={
            "meta.row_count": ("meta", "row_count"),
            "meta.skipped_rows": ("meta", "skipped_rows"),
            "meta.is_synthetic": ("meta", "is_synthetic"),
            "meta.missing_columns": ("meta", "missing_columns"),
            "meta.extra.missing_extra_metrics": ("meta", "extra", "missing_extra_metrics"),
            "meta.extra.window.duration_us": ("meta", "extra", "window", "duration_us"),
            "meta.extra.window.rounds": ("meta", "extra", "window", "rounds"),
            "meta.extra.window_quality": ("meta", "extra", "window_quality"),
        },
    )
    _compare_quality_flags(
        diff,
        workload,
        _get(before, ("meta", "extra", "quality_flags")),
        _get(after, ("meta", "extra", "quality_flags")),
    )
    diff.add_rows(
        category="operator_table",
        artifact=artifact,
        workload=workload,
        before=list(before.get("records") or ()),
        after=list(after.get("records") or ()),
        key_fields=("operator", "task_type"),
        value_fields={
            "calls": ("calls",),
            "total_time_us": ("total_time_us", "value"),
            "avg_time_us": ("avg_time_us", "value"),
            "max_time_us": ("max_time_us", "value"),
            "min_time_us": ("min_time_us", "value"),
            "total_provenance": ("total_time_us", "provenance"),
        },
    )


def _compare_diagnosis(diff: _Diff, workload: str, before: Mapping, after: Mapping) -> None:
    artifact = "diagnosis.json"
    _check_sequence(diff, artifact, before, "operators")
    _check_sequence(diff, artifact, after, "operators")
    diff.add_field_changes(
        category="diagnosis",
        artifact=artifact,
        workload=workload,
        before=before,
        after=after,
        fields={
            "ruleset_id": ("ruleset_id",),
            "threshold_status": ("threshold_status",),
            "summary.operators_analyzed": ("summary", "operators_analyzed"),
            "summary.candidates_total": ("summary", "candidates_total"),
            "summary.candidates_by_level": ("summary", "candidates_by_level"),
            "summary.candidates_by_bottleneck": ("summary", "candidates_by_bottleneck"),
            "summary.blocked_rules": ("summary", "blocked_rules"),
            "notes": ("notes",),
        },
    )
    before_candidates: list[dict[str, Any]] = []
    after_candidates: list[dict[str, Any]] = []
    for side, target in ((before, before_candidates), (after, after_candidates)):
        for operator in side.get("operators") or ():
            if not isinstance(operator, Mapping) or not isinstance(operator.get("operator"), str):
                continue
            if "candidates" in operator and not _check_sequence(
                diff, artifact, operator, "candidates"
            ):
                continue
            for candidate in operator.get("candidates") or ():
                if not isinstance(candidate, Mapping):
                    continue
                item = dict(candidate)
                item["operator"] = operator["operator"]
                target.append(item)
    diff.add_rows(
        category="diagnosis",
        artifact=artifact,
        workload=workload,
        before=before_candidates,
        after=after_candidates,
        key_fields=("operator", "rule_id"),
        value_fields={
            "bottleneck": ("bottleneck",),
            "level": ("level",),
            "confidence": ("confidence",),
            "hypothesis_only": ("hypothesis_only",),
        },
    )


def _opportunity_key(item: Mapping[str, Any]) -> tuple:
    workload_ids = tuple(sorted(str(workload) for workload in item.get("workload_ids") or ()))
    refs = tuple(sorted(str(ref) for ref in item.get("diagnosis_refs") or ()))
    return (
        workload_ids,
        str(item.get("operator")),
        str(item.get("bottleneck")),
        refs,
    )


def _trusted_level(item: Mapping[str, Any]) -> tuple[str, str | None]:
    """Use the repository's read-only trust gate; never trust the raw string."""
    try:
        # Import inside the function keeps --help usable when dependencies are absent.
        import opt_discovery.schemas.evidence_gate as gate

        return gate.trusted_level_for_mapping(dict(item))
    except Exception as exc:  # noqa: BLE001 - diff must report, not crash
        raw = item.get("evidence_level")
        return (
            "unknown",
            f"trust_check_failed: {exc}; 原始 evidence_level={raw!r} 不作为可信等级",
        )


def _compare_opportunities(diff: _Diff, workload: str, before: Mapping, after: Mapping) -> None:
    artifact = "opportunities.json"
    _check_sequence(diff, artifact, before, "opportunities")
    _check_sequence(diff, artifact, after, "opportunities")
    diff.add_field_changes(
        category="opportunity",
        artifact=artifact,
        workload=workload,
        before=before,
        after=after,
        fields={"count": ("count",)},
    )

    def indexed(payload: Mapping) -> dict[tuple, Mapping[str, Any]]:
        result: dict[tuple, Mapping[str, Any]] = {}
        seen: dict[tuple, int] = {}
        for item in payload.get("opportunities") or ():
            if not isinstance(item, Mapping):
                continue
            key = _opportunity_key(item)
            count = seen.get(key, 0) + 1
            seen[key] = count
            # Two distinct opportunities may share workload/operator/bottleneck
            # and rule refs.  Keep both instead of letting the latter overwrite
            # the former; the occurrence suffix makes the pairing explicit.
            if count > 1:
                key = key + (f"#{count}",)
            result[key] = item
        return result

    left = indexed(before)
    right = indexed(after)
    for key in sorted(set(left) - set(right), key=str):
        diff.add(
            category="opportunity",
            artifact=artifact,
            workload=workload,
            path="key:" + "|".join(str(part) for part in key),
            change="removed",
            before={"evidence_level": left[key].get("evidence_level")},
        )
    for key in sorted(set(right) - set(left), key=str):
        diff.add(
            category="opportunity",
            artifact=artifact,
            workload=workload,
            path="key:" + "|".join(str(part) for part in key),
            change="added",
            after={"evidence_level": right[key].get("evidence_level")},
        )
    for key in sorted(set(left) & set(right), key=str):
        path = "key:" + "|".join(str(part) for part in key)
        left_item = left[key]
        right_item = right[key]
        left_trusted, left_warning = _trusted_level(left_item)
        right_trusted, right_warning = _trusted_level(right_item)
        fields = {
            "evidence_level": (left_item.get("evidence_level"), right_item.get("evidence_level")),
            "trusted_evidence_level": (left_trusted, right_trusted),
            "trusted_level_warning": (left_warning, right_warning),
            "status": (left_item.get("status"), right_item.get("status")),
            "scores.value": (
                _get(left_item, ("scores", "value")),
                _get(right_item, ("scores", "value")),
            ),
            "scores.confidence": (
                _get(left_item, ("scores", "confidence")),
                _get(right_item, ("scores", "confidence")),
            ),
            "missing_evidence": (
                sorted(str(item.get("name")) for item in left_item.get("missing_evidence") or ()),
                sorted(str(item.get("name")) for item in right_item.get("missing_evidence") or ()),
            ),
            "source_refs": (
                sorted(str(item) for item in left_item.get("source_refs") or ()),
                sorted(str(item) for item in right_item.get("source_refs") or ()),
            ),
        }
        for label, (left_value, right_value) in fields.items():
            if not _values_equal(left_value, right_value, float_tolerance=diff.float_tolerance):
                diff.add(
                    category="opportunity",
                    artifact=artifact,
                    workload=workload,
                    path=f"{path}.{label}",
                    change="modified",
                    before=_jsonable(left_value),
                    after=_jsonable(right_value),
                )


def _compare_timeline(diff: _Diff, workload: str, before: Mapping, after: Mapping) -> None:
    artifact = "timeline.json"
    fields: dict[str, tuple[str, ...]] = {
        "busy_source": ("busy_source",),
        "window.rounds": ("window", "rounds"),
        "window.duration_us": ("window", "duration_us"),
        "span_us": ("span_us",),
        "device_busy_us": ("device_busy_us",),
        "device_busy_all_us": ("device_busy_all_us",),
        "device_idle_us": ("device_idle_us",),
        "device_idle_ratio": ("device_idle_ratio",),
        "task_count": ("task_count",),
        "gap_count": ("gap_count",),
        "gap_p50_us": ("gap_p50_us",),
        "gap_p90_us": ("gap_p90_us",),
        "gap_max_us": ("gap_max_us",),
        "analyzed_gaps": ("analyzed_gaps",),
        "gaps_by_class": ("gaps_by_class",),
        "idle_by_class_us": ("idle_by_class_us",),
        "rank_duration_inconsistent": ("rank_duration_inconsistent",),
        "notes": ("notes",),
    }
    diff.add_field_changes(
        category="timeline",
        artifact=artifact,
        workload=workload,
        before=before,
        after=after,
        fields=fields,
    )


def _compare_comm(diff: _Diff, workload: str, before: Mapping, after: Mapping) -> None:
    artifact = "comm.json"
    fields: dict[str, tuple[str, ...]] = {
        "available": ("available",),
        "records": ("records",),
        "unique_tasks": ("unique_tasks",),
        "dedup_ratio": ("dedup_ratio",),
        "bytes_total": ("bytes_total",),
        "mib_total": ("mib_total",),
        "kib_per_task": ("kib_per_task",),
        "bandwidth_mean_gbps": ("bandwidth_mean_gbps",),
        "groups": ("groups",),
        "notes": ("notes",),
    }
    diff.add_field_changes(
        category="comm",
        artifact=artifact,
        workload=workload,
        before=before,
        after=after,
        fields=fields,
    )


def _compare_graph_coverage(diff: _Diff, workload: str, before: Mapping, after: Mapping) -> None:
    artifact = "graph_coverage.json"
    fields: dict[str, tuple[str, ...]] = {
        "available": ("available",),
        "total_calls": ("total_calls",),
        "in_graph_calls": ("in_graph_calls",),
        "task_ratio": ("task_ratio",),
        "total_us": ("total_us",),
        "in_graph_us": ("in_graph_us",),
        "time_ratio": ("time_ratio",),
        "top_fallback": ("top_fallback",),
        "notes": ("notes",),
    }
    diff.add_field_changes(
        category="graph_coverage",
        artifact=artifact,
        workload=workload,
        before=before,
        after=after,
        fields=fields,
    )


def _compare_hotspots(diff: _Diff, before: Mapping, after: Mapping) -> None:
    artifact = "hotspots.json"
    _check_sequence(diff, artifact, before, "rows")
    _check_sequence(diff, artifact, after, "rows")
    diff.add_field_changes(
        category="hotspots",
        artifact=artifact,
        before=before,
        after=after,
        fields={"workloads": ("workloads",)},
    )
    left = {str(row.get("operator")): row for row in before.get("rows") or () if isinstance(row, Mapping)}
    right = {str(row.get("operator")): row for row in after.get("rows") or () if isinstance(row, Mapping)}
    for operator in sorted(set(left) - set(right)):
        diff.add(
            category="hotspots",
            artifact=artifact,
            path=f"rows[{operator}]",
            change="removed",
            before=left[operator],
        )
    for operator in sorted(set(right) - set(left)):
        diff.add(
            category="hotspots",
            artifact=artifact,
            path=f"rows[{operator}]",
            change="added",
            after=right[operator],
        )
    for operator in sorted(set(left) & set(right)):
        diff.add_rows(
            category="hotspots",
            artifact=f"{artifact}[{operator}]",
            workload="",
            before=[
                {"workload": name, **(value if isinstance(value, Mapping) else {})}
                for name, value in (left[operator].get("workloads") or {}).items()
            ],
            after=[
                {"workload": name, **(value if isinstance(value, Mapping) else {})}
                for name, value in (right[operator].get("workloads") or {}).items()
            ],
            key_fields=("workload",),
            value_fields={"share": ("share",), "calls": ("calls",), "avg_us": ("avg_us",)},
        )


def _compare_source_map(diff: _Diff, before: Mapping, after: Mapping) -> None:
    artifact = "source_map.json"
    if not _check_mapping(diff, artifact, before, "operators"):
        return
    if not _check_mapping(diff, artifact, after, "operators"):
        return
    left = {str(name): item for name, item in (before.get("operators") or {}).items()}
    right = {str(name): item for name, item in (after.get("operators") or {}).items()}
    for operator in sorted(set(left) - set(right)):
        diff.add(
            category="source_map",
            artifact=artifact,
            path=f"operators[{operator}]",
            change="removed",
            before=left[operator],
        )
    for operator in sorted(set(right) - set(left)):
        diff.add(
            category="source_map",
            artifact=artifact,
            path=f"operators[{operator}]",
            change="added",
            after=right[operator],
        )
    for operator in sorted(set(left) & set(right)):
        for label, path in (("mapped", ("mapped",)), ("tier", ("tier",)), ("reason", ("reason",))):
            left_value = _get(left[operator], path)
            right_value = _get(right[operator], path)
            if not _values_equal(left_value, right_value, float_tolerance=diff.float_tolerance):
                diff.add(
                    category="source_map",
                    artifact=artifact,
                    path=f"operators[{operator}].{label}",
                    change="modified",
                    before=_jsonable(left_value),
                    after=_jsonable(right_value),
                )


def _compare_compare_json(diff: _Diff, name: str, before: Mapping, after: Mapping) -> None:
    _check_sequence(diff, name, before, "deltas")
    _check_sequence(diff, name, after, "deltas")
    diff.add_field_changes(
        category="compare",
        artifact=name,
        before=before,
        after=after,
        fields={
            "comparable": ("comparable",),
            "same_window": ("same_window",),
            "window.rounds": ("window", "rounds"),
            "window.duration_us": ("window", "duration_us"),
            "reasons": ("reasons",),
            "warnings": ("warnings",),
            "total_ratio": ("total_ratio",),
            "observations": ("observations",),
        },
    )
    diff.add_rows(
        category="compare",
        artifact=name,
        workload="",
        before=[item for item in before.get("deltas") or () if isinstance(item, Mapping)],
        after=[item for item in after.get("deltas") or () if isinstance(item, Mapping)],
        key_fields=("operator", "presence"),
        value_fields={
            "share_a": ("share_a",),
            "share_b": ("share_b",),
            "delta_share": ("delta_share",),
            "calls_a": ("calls_a",),
            "calls_b": ("calls_b",),
            "calls_ratio": ("calls_ratio",),
            "avg_a": ("avg_a",),
            "avg_b": ("avg_b",),
            "avg_ratio": ("avg_ratio",),
        },
    )


def _compare_manifest(diff: _Diff, before: Mapping, after: Mapping) -> None:
    artifact = "run_manifest.json"
    _check_sequence(diff, artifact, before, "stages")
    _check_sequence(diff, artifact, after, "stages")
    _check_sequence(diff, artifact, before, "workloads")
    _check_sequence(diff, artifact, after, "workloads")
    diff.add_field_changes(
        category="run_manifest",
        artifact=artifact,
        before=before,
        after=after,
        fields={
            "status": ("status",),
            "comparison_status": ("comparison_status",),
            "quality_flags": ("quality_flags",),
        },
    )

    def stages(payload: Mapping) -> dict[str, Mapping]:
        return {
            str(item.get("name")): item
            for item in payload.get("stages") or ()
            if isinstance(item, Mapping)
        }

    diff.add_rows(
        category="run_manifest",
        artifact=artifact,
        workload="",
        before=[
            {"name": name, **item}
            for name, item in stages(before).items()
        ],
        after=[{"name": name, **item} for name, item in stages(after).items()],
        key_fields=("name",),
        value_fields={
            "required": ("required",),
            "status": ("status",),
            "reason_code": ("reason_code",),
        },
    )

    def workloads(payload: Mapping) -> list[Mapping]:
        return [
            item for item in payload.get("workloads") or () if isinstance(item, Mapping)
        ]

    diff.add_rows(
        category="run_manifest",
        artifact=artifact,
        workload="",
        before=workloads(before),
        after=workloads(after),
        key_fields=("workload_id",),
        value_fields={
            "status": ("status",),
            "reason_code": ("reason_code",),
            "quality_flags": ("quality_flags",),
        },
    )


def _compare_csv_pair(
    diff: _Diff,
    name: str,
    before_path: Path,
    after_path: Path,
    *,
    key_fields: Sequence[str],
) -> None:
    before_rows, before_header, before_error = _read_csv(before_path)
    after_rows, after_header, after_error = _read_csv(after_path)
    if before_error:
        diff.errors.append(f"{before_path.parent.name}/{name}: {before_error}")
        return
    if after_error:
        diff.errors.append(f"{after_path.parent.name}/{name}: {after_error}")
        return
    if before_header != after_header:
        diff.add(
            category="csv",
            artifact=name,
            path="header",
            change="modified",
            before=before_header,
            after=after_header,
        )
    common = [column for column in (before_header or []) if column in (after_header or [])]
    if not all(field in common for field in key_fields):
        diff.errors.append(f"{name}: 缺少行键列 {list(key_fields)}")
        return
    for side, payload in (("before", before_rows), ("after", after_rows)):
        for number, row in enumerate(payload, start=2):
            if any(row.get(field) in (None, "") for field in key_fields):
                diff.errors.append(
                    f"{name}: {side} 第 {number} 行缺行键 {list(key_fields)}"
                )
                return

    def rows(payload: list[dict[str, str | None]]) -> dict[tuple, dict[str, str | None]]:
        result: dict[tuple, dict[str, str | None]] = {}
        seen: dict[tuple, int] = {}
        for row in payload:
            key = tuple(row.get(field) for field in key_fields)
            count = seen.get(key, 0) + 1
            seen[key] = count
            if count > 1:
                key = key + (f"#{count}",)
            result[key] = row
        return result

    left = rows(before_rows)
    right = rows(after_rows)
    for key in sorted(set(left) - set(right), key=str):
        diff.add(
            category="csv",
            artifact=name,
            path="row:" + "|".join(str(part) for part in key),
            change="removed",
            before=left[key],
        )
    for key in sorted(set(right) - set(left), key=str):
        diff.add(
            category="csv",
            artifact=name,
            path="row:" + "|".join(str(part) for part in key),
            change="added",
            after=right[key],
        )
    for key in sorted(set(left) & set(right), key=str):
        row_label = "row:" + "|".join(str(part) for part in key)
        for column in common:
            left_value = left[key].get(column)
            right_value = right[key].get(column)
            if left_value != right_value:
                diff.add(
                    category="csv",
                    artifact=name,
                    path=f"{row_label}.{column}",
                    change="modified",
                    before=left_value,
                    after=right_value,
                )


def compare_dirs(
    before: str | Path,
    after: str | Path,
    *,
    before_label: str | None = None,
    after_label: str | None = None,
    float_tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Compare two ``analyze`` output directories and return a JSON-ready report."""
    if not math.isfinite(float_tolerance) or float_tolerance < 0:
        raise ValueError("float_tolerance 必须是有限非负数")
    before_root = Path(before)
    after_root = Path(after)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "before": before_label or before_root.name,
        "after": after_label or after_root.name,
        "status": STATUS_COMPARED,
        "errors": [],
        "ignored_noise": sorted(NOISE_KEYS),
        "changes": [],
        "summary": {"changed": False, "change_count": 0},
    }
    if not before_root.is_dir():
        report["status"] = STATUS_INVALID_INPUT
        report["errors"].append(f"before 不是目录: {before_root}")
        return report
    if not after_root.is_dir():
        report["status"] = STATUS_INVALID_INPUT
        report["errors"].append(f"after 不是目录: {after_root}")
        return report

    before_artifacts = _known_artifacts(before_root)
    after_artifacts = _known_artifacts(after_root)
    if not before_artifacts or not after_artifacts:
        report["status"] = STATUS_INVALID_INPUT
        report["errors"].append(
            "至少一侧没有任何可识别的分析产物（空目录不能视为已核对无差异）"
        )
        return report

    diff = _Diff(float_tolerance=float_tolerance)
    for name in sorted(set(after_artifacts) - set(before_artifacts)):
        diff.add(category="artifact", artifact=name, change="added")
    for name in sorted(set(before_artifacts) - set(after_artifacts)):
        diff.add(category="artifact", artifact=name, change="removed")

    before_workloads = _workload_names(before_root)
    after_workloads = _workload_names(after_root)
    for name in sorted(after_workloads - before_workloads):
        diff.add(category="workload", artifact="", workload=name, change="added")
    for name in sorted(before_workloads - after_workloads):
        diff.add(category="workload", artifact="", workload=name, change="removed")

    for name in sorted(set(before_artifacts) & set(after_artifacts)):
        before_path = before_artifacts[name]
        after_path = after_artifacts[name]
        if name.endswith("/operator_table.json"):
            workload = name.split("/", 1)[0]
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_operator_table(diff, workload, before_data, after_data)
        elif name.endswith("/diagnosis.json"):
            workload = name.split("/", 1)[0]
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_diagnosis(diff, workload, before_data, after_data)
        elif name.endswith("/opportunities.json"):
            workload = name.split("/", 1)[0]
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_opportunities(diff, workload, before_data, after_data)
        elif name.endswith("/timeline.json"):
            workload = name.split("/", 1)[0]
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_timeline(diff, workload, before_data, after_data)
        elif name.endswith("/comm.json"):
            workload = name.split("/", 1)[0]
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_comm(diff, workload, before_data, after_data)
        elif name.endswith("/graph_coverage.json"):
            workload = name.split("/", 1)[0]
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_graph_coverage(diff, workload, before_data, after_data)
        elif name == "hotspots.json":
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_hotspots(diff, before_data, after_data)
        elif name == "source_map.json":
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_source_map(diff, before_data, after_data)
        elif name == "run_manifest.json":
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_manifest(diff, before_data, after_data)
        elif name == "workload_summary.csv":
            _compare_csv_pair(diff, name, before_path, after_path, key_fields=("workload_id",))
        elif name == "operator_summary.csv":
            _compare_csv_pair(
                diff,
                name,
                before_path,
                after_path,
                key_fields=("workload_id", "operator", "task_type"),
            )
        elif name.startswith("compare_"):
            before_data = diff.read_json(before_path)
            after_data = diff.read_json(after_path)
            if isinstance(before_data, Mapping) and isinstance(after_data, Mapping):
                _compare_compare_json(diff, name, before_data, after_data)

    report["changes"] = diff.changes
    report["errors"] = diff.errors
    if diff.errors:
        report["status"] = STATUS_READ_ERROR
    report["summary"] = {
        "changed": bool(diff.changes),
        "change_count": len(diff.changes),
        "ignored_noise": sorted(NOISE_KEYS),
    }
    return report


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def write_report(report: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="比较两次 analyze 产物的语义差异（只读输入，输出另放）"
    )
    parser.add_argument("--before", required=True, help="改动前的 analyze 输出目录")
    parser.add_argument("--after", required=True, help="改动后的 analyze 输出目录")
    parser.add_argument("--out", required=True, help="差异报告 JSON 路径（不得位于输入目录内）")
    parser.add_argument(
        "--float-tolerance",
        type=float,
        default=1e-9,
        help="浮点相对容差（默认 1e-9，仅用于吸收序列化噪声）",
    )
    args = parser.parse_args(argv)

    before = Path(args.before)
    after = Path(args.after)
    out = Path(args.out)
    for root in (before, after):
        if _is_within(out, root):
            print(
                f"[artifact-diff] 输出路径不得位于输入目录内: {out}",
                file=sys.stderr,
            )
            return 2

    try:
        report = compare_dirs(before, after, float_tolerance=args.float_tolerance)
    except ValueError as exc:
        print(f"[artifact-diff] 参数不合法: {exc}", file=sys.stderr)
        return 2
    try:
        target = write_report(report, out)
    except OSError as exc:
        print(f"[artifact-diff] 写报告失败: {exc}", file=sys.stderr)
        return 1

    summary = report["summary"]
    print(
        f"[artifact-diff] status={report['status']} changed={summary['changed']} "
        f"changes={summary['change_count']} -> {target}"
    )
    for error in report["errors"]:
        print(f"  ! {error}", file=sys.stderr)
    if report["status"] == STATUS_COMPARED:
        return 0
    return 2 if report["status"] == STATUS_INVALID_INPUT else 1


__all__ = ["compare_dirs", "main", "write_report"]
