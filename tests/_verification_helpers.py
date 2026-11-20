"""Shared fixtures for AE-08 artifact-diff and verification-summary tests.

The shape intentionally follows real ``analyze`` artifacts: per-workload JSON,
root-level summary CSV, compare JSON and ``run_manifest.json``.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping

from opt_discovery.report.summary_csv import OPERATOR_COLUMNS, WORKLOAD_COLUMNS


def write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def opportunity(
    workload: str = "w1",
    *,
    level: str = "hypothesis",
    instance_id: str = "instance-1",
    created_at: str = "2026-01-01T00:00:00+00:00",
    operator: str = "MatMul",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "opportunity_id": "OPP-001",
        "operator": operator,
        "workload_ids": [workload],
        "bottleneck": "compute_bound",
        "evidence_level": level,
        "observation": f"{operator} 在 {workload} 中 compute_pipe_time_fraction 高",
        "hypothesis": f"{operator} 可能受限于计算",
        "evidence": [],
        "missing_evidence": [],
        "suggested_actions": ["check tiling"],
        "validation_plan": {
            "kernel": ["compare kernel latency"],
            "model": ["compare model latency"],
            "serving": [],
            "correctness": ["compare outputs"],
        },
        "source_refs": [],
        "status": "discovered",
        "scores": {
            "impact": 0.5,
            "feasibility": 0.5,
            "confidence": 0.5,
            "cost": 0.5,
            "value": 0.25,
        },
        "score_basis": {},
        "diagnosis_refs": ["compute_bound.pipe_dominant"],
        "caveats": [],
        "created_at": created_at,
        "instance_id": instance_id,
        "is_synthetic": False,
    }
    if level in ("hypothesis", "insufficient_evidence"):
        payload["missing_evidence"] = [
            {
                "name": "end_to_end_latency",
                "reason": "尚未做端到端验证",
                "needed_for": "compute_bound.pipe_dominant",
                "how_to_collect": None,
            }
        ]
    return payload


def make_analysis_dir(root: Path, workload: str = "w1") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    wdir = root / workload
    wdir.mkdir(parents=True, exist_ok=True)

    write_json(
        wdir / "operator_table.json",
        {
            "schema_version": 1,
            "workload": {
                "workload_id": workload,
                "execution": {"phase": "prefill", "mode": "eager", "graph_mode": "none"},
            },
            "meta": {
                "parser": "test",
                "parser_version": "1",
                "source_files": ["op_summary_1.csv"],
                "row_count": 2,
                "skipped_rows": 0,
                "warnings": [],
                "unmapped_columns": [],
                "missing_columns": [],
                "is_synthetic": False,
                "extra": {
                    "quality_flags": [],
                    "missing_extra_metrics": [],
                    "window": {"rounds": [0, 1], "duration_us": 1000.0},
                    "window_quality": {
                        "busy_ratio": 0.9,
                        "degraded": False,
                        "max_gap_us": 10.0,
                    },
                },
            },
            "records": [
                {
                    "workload_id": workload,
                    "operator": "MatMul",
                    "task_type": "kernel",
                    "calls": 10,
                    "total_time_us": {
                        "value": 100.0,
                        "unit": "us",
                        "provenance": "measured",
                    },
                    "avg_time_us": {
                        "value": 10.0,
                        "unit": "us",
                        "provenance": "derived",
                    },
                    "max_time_us": {
                        "value": 12.0,
                        "unit": "us",
                        "provenance": "measured",
                    },
                    "min_time_us": {
                        "value": 8.0,
                        "unit": "us",
                        "provenance": "measured",
                    },
                }
            ],
        },
    )
    write_json(
        wdir / "diagnosis.json",
        {
            "schema_version": 1,
            "ruleset_id": "test-rules@v1",
            "threshold_status": "calibrated",
            "is_synthetic": False,
            "summary": {
                "operators_analyzed": 1,
                "candidates_total": 1,
                "candidates_by_level": {"hypothesis": 1},
                "candidates_by_bottleneck": {"compute_bound": 1},
                "blocked_rules": [],
            },
            "notes": [],
            "operators": [
                {
                    "operator": "MatMul",
                    "candidates": [
                        {
                            "rule_id": "compute_bound.pipe_dominant",
                            "bottleneck": "compute_bound",
                            "level": "hypothesis",
                            "confidence": 0.5,
                            "hypothesis_only": False,
                        }
                    ],
                }
            ],
        },
    )
    write_json(
        wdir / "opportunities.json",
        {
            "schema_version": 1,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "count": 1,
            "opportunities": [opportunity(workload)],
        },
    )
    write_json(
        wdir / "timeline.json",
        {
            "db_path": "/machine/path/msprof_0.db",
            "window": {
                "policy": "rounds",
                "rounds": [0, 1],
                "duration_us": 1000.0,
                "source": "/machine/path/workload.log",
            },
            "busy_source": "op_summary",
            "span_us": 1000.0,
            "device_busy_us": 900.0,
            "device_busy_all_us": 800.0,
            "device_idle_us": 100.0,
            "device_idle_ratio": 0.1,
            "task_count": 10,
            "gap_count": 1,
            "gap_p50_us": 10.0,
            "gap_p90_us": 10.0,
            "gap_max_us": 10.0,
            "analyzed_gaps": 1,
            "gaps_by_class": {"acl_busy": 1},
            "idle_by_class_us": {"acl_busy": 10.0},
            "rank_duration_inconsistent": False,
            "notes": [],
        },
    )
    write_json(
        wdir / "comm.json",
        {
            "db_path": "/machine/path/msprof_0.db",
            "available": True,
            "records": 4,
            "unique_tasks": 2,
            "dedup_ratio": 2.0,
            "bytes_total": 2048.0,
            "mib_total": 0.002,
            "kib_per_task": 1.0,
            "bandwidth_mean_gbps": 1.0,
            "groups": 1,
            "notes": [],
        },
    )
    write_json(
        wdir / "graph_coverage.json",
        {
            "schema_version": 1,
            "workload_id": workload,
            "mode": "eager",
            "graph_mode": "none",
            "available": False,
            "total_calls": 10,
            "in_graph_calls": 0,
            "task_ratio": 0.0,
            "total_us": 100.0,
            "in_graph_us": 0.0,
            "time_ratio": 0.0,
            "top_fallback": [],
            "notes": ["eager 模式"],
        },
    )
    write_json(
        root / "hotspots.json",
        {
            "workloads": [workload],
            "rows": [
                {
                    "operator": "MatMul",
                    "workloads": {workload: {"share": 0.5, "calls": 10, "avg_us": 10.0}},
                }
            ],
        },
    )
    with (root / "workload_summary.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKLOAD_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "workload_id": workload,
                "phase": "prefill",
                "mode": "eager",
                "graph_mode": "none",
                "record_count": 2,
                "operator_count": 1,
                "calls": 10,
                "total_us": 100.0,
                "window_rounds": "0|1",
                "window_duration_s": 0.001,
                "device_busy_ratio": 0.9,
                "window_degraded": False,
                "max_gap_ms": 0.01,
                "comm_mib": 0.002,
                "comm_tasks": 2,
                "candidates_total": 1,
                "opportunities_total": 1,
                "synthetic": False,
            }
        )
    with (root / "operator_summary.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OPERATOR_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "workload_id": workload,
                "operator": "MatMul",
                "task_type": "kernel",
                "calls": 10,
                "total_us": 100.0,
                "avg_us": 10.0,
                "max_us": 12.0,
                "share": 1.0,
                "top_bottleneck": "compute_bound",
                "top_level": "hypothesis",
                "top_confidence": 0.5,
                "rule_id": "compute_bound.pipe_dominant",
            }
        )
    write_json(
        root / f"compare_{workload}_vs_w2.json",
        {
            "schema_version": 1,
            "a": workload,
            "b": "w2",
            "comparable": True,
            "reasons": [],
            "warnings": [],
            "same_window": True,
            "window": {"rounds": [0, 1], "duration_us": 1000.0, "source": "/machine/path"},
            "total_a_us": 100.0,
            "total_b_us": 110.0,
            "total_ratio": 1.1,
            "observations": [],
            "deltas": [
                {
                    "operator": "MatMul",
                    "presence": "both",
                    "share_a": 0.5,
                    "share_b": 0.55,
                    "delta_share": 0.05,
                    "calls_a": 10,
                    "calls_b": 10,
                    "calls_ratio": 1.0,
                    "avg_a": 10.0,
                    "avg_b": 11.0,
                    "avg_ratio": 1.1,
                }
            ],
            "generated_at": "2026-01-01T00:00:00+00:00",
        },
    )
    write_json(
        root / "run_manifest.json",
        {
            "schema_version": "1.0",
            "run_id": "run-1",
            "status": "success",
            "is_synthetic": False,
            "strict": True,
            "created_at": "2026-01-01T00:00:00+00:00",
            "stages": [
                {"name": "discover", "required": True, "status": "passed", "artifacts": []},
                {"name": "parse", "required": True, "status": "passed", "artifacts": []},
                {"name": "compare", "required": False, "status": "passed", "artifacts": []},
            ],
            "workloads": [
                {
                    "workload_id": workload,
                    "status": "success",
                    "artifacts": [],
                    "quality_flags": [],
                }
            ],
            "quality_flags": [],
            "comparison_status": "success",
            "artifacts": [],
            "inputs": {},
            "tool": {},
        },
    )
    return root
