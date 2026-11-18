"""AE-03：质量异常从真实采集形态贯通到独立 compare（P2-3）。

与 `test_quality_gate.py`（直接注入标记）不同，这里走完整链路：
CSV + workload.log + sqlite -> collector -> parser -> timeline -> analyze 落盘
-> 独立 compare 读取 JSON。用于锁定「标记生成/落盘/消费」的连接，防止只保留
消费端断言而误以为端到端已覆盖。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml
from conftest import REPO_ROOT

from opt_discovery.cli import main

CONFIGS = REPO_ROOT / "configs"
WORKLOAD_DIR = CONFIGS / "workloads" / "qwen35-35b-a3b-910b"

LOG_TEXT = """PROMPT_TOKENS 8 LOAD_SECONDS 1.5
PROFILE_START_ABS 1000.0
WARMUP_DONE_ABS 999.5
ACTIVE_ROUND 0 start 1000.0 end 1001.0 sample 'x'
ACTIVE_ROUND 1 start 1001.0 end 1002.0 sample 'x'
ACTIVE_ROUND 2 start 1002.0 end 1004.0 sample 'x'
"""

HEADER = "Device_id,OP Type,Task Type,Task Start Time(us),Task Duration(us),Input Shapes\n"


def _rows(scale: float) -> list[tuple[str, int, float]]:
    return [
        ("MatMul", 1001_100_000, 1000.0 * scale),
        ("Add", 1001_102_000, 500.0 * scale),
        ("MatMul", 1003_100_000, 1200.0 * scale),
    ]


def _db(prof: Path) -> Path:
    path = prof / "msprof_0.db"
    connection = sqlite3.connect(path)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.commit()
    connection.close()
    return path


def _export(root: Path, name: str, *, rank_scales: list[float]) -> Path:
    export = root / name
    export.mkdir(parents=True)
    (export / "workload.log").write_text(LOG_TEXT, encoding="utf-8")
    for rank, scale in enumerate(rank_scales):
        prof = export / f"PROF_{rank:06d}_test"
        output = prof / "mindstudio_profiler_output"
        output.mkdir(parents=True)
        (prof / f"device_{rank}").mkdir()
        body = "".join(
            f'0,{operator},AI_CORE,{start},{duration},"8,16"\n'
            for operator, start, duration in _rows(scale)
        )
        (output / "op_summary_1.csv").write_text(HEADER + body, encoding="utf-8")
        _db(prof)
    return export


def _workloads(tmp_path: Path) -> Path:
    target = tmp_path / "workloads"
    target.mkdir()
    for name in ("prefill_eager", "prefill_graph"):
        raw = yaml.safe_load((WORKLOAD_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        raw["hardware"]["device_count"] = 2
        (target / f"{name}.yaml").write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return target


def test_rank_and_duration_flags_propagate_to_compare(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    # eager：两个 rank 任务数相同、累计时长 4x（R05 + R10 的真实触发形态）
    _export(exports, "qwen35-prefill", rank_scales=[1.0, 4.0])
    # graph 对照：两个 rank 一致
    _export(exports, "qwen35-prefill-graph-noex", rank_scales=[1.0, 1.0])
    workloads = _workloads(tmp_path)
    out = tmp_path / "out"

    code = main(
        [
            "analyze",
            "--exports-root",
            str(exports),
            "--workloads-dir",
            str(workloads),
            "--out",
            str(out),
            "--rounds",
            "1-2",
            "--strict",
        ]
    )
    assert code == 3  # 阻断型质量标记 -> partial

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert {"rank_merge_inconsistent", "rank_duration_inconsistent"} <= set(
        manifest["quality_flags"]
    )

    table_path = out / "qwen35-35b-a3b-prefill-eager" / "operator_table.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    flags = {item["code"]: item for item in table["meta"]["extra"]["quality_flags"]}
    assert "rank_merge_inconsistent" in flags
    assert "rank_duration_inconsistent" in flags
    # 两个标记来自不同生产者：rank 评估 vs timeline
    assert flags["rank_merge_inconsistent"]["source"] == "rank_quality"
    assert flags["rank_duration_inconsistent"]["source"] == "timeline"
    assert table["meta"]["extra"]["rank_quality"]["consistent"] is False

    graph_table = out / "qwen35-35b-a3b-prefill-graph" / "operator_table.json"
    compare_dir = tmp_path / "cmp"
    compare_code = main(
        [
            "compare",
            "--a",
            str(table_path),
            "--b",
            str(graph_table),
            "--out-dir",
            str(compare_dir),
            "--strict",
        ]
    )
    assert compare_code == 4
    payload = json.loads(
        (compare_dir / "compare_qwen35-35b-a3b-prefill-eager_vs_qwen35-35b-a3b-prefill-graph.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["comparable"] is False
    assert payload["deltas"] == []
    assert payload["total_ratio"] is None
    assert {flag["code"] for flag in payload["quality_flags"]} >= {
        "rank_merge_inconsistent",
        "rank_duration_inconsistent",
    }
