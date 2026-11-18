"""AE-02 回归用例的共享构造器。

所有样本都在测试运行时生成：小规模、脱敏、不含任何机器路径或真实导出数据。
期望值一律用「独立算术」给出（行数、Count 列求和、ns→µs 换算等），
不复用被测函数自身来计算 expected，避免自证。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

from opt_discovery.schemas.workload import (
    ExecutionMode,
    ExecutionSpec,
    GraphMode,
    HardwareSpec,
    InputSpec,
    ModelSpec,
    Phase,
    WorkloadSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "configs"
SUMMARY_MAPPING = CONFIGS / "parsers" / "ascend_op_summary.yaml"
STATISTIC_MAPPING = CONFIGS / "parsers" / "ascend_op_statistic.yaml"
RULES = CONFIGS / "rules" / "bottleneck_rules.v1.yaml"
WORKLOADS = CONFIGS / "workloads" / "qwen35-35b-a3b-910b"
DEVICE_PROFILE = CONFIGS / "thresholds" / "ascend_910b.device_profile.json"

#: op_summary 的最小可用表头（真实导出是 33/46 列的超集）
OP_SUMMARY_HEADER: tuple[str, ...] = (
    "Device_id",
    "Model ID",
    "Task Type",
    "OP Type",
    "Task Start Time(us)",
    "Task Duration(us)",
    "Input Shapes",
    "Input Data Types",
    "Output Shapes",
    "Output Data Types",
    "Block Num",
    "aicore_time(us)",
    "aiv_time(us)",
    "aic_cube_fops",
    "aiv_vector_fops",
)


def make_workload(
    workload_id: str = "qwen35-prefill-eager",
    *,
    phase: Phase = Phase.PREFILL,
    mode: ExecutionMode = ExecutionMode.EAGER,
    graph_mode: GraphMode = GraphMode.NONE,
    batch_size: int | None = 1,
    input_length: int | None = 1024,
    output_length: int | None = 1,
) -> WorkloadSpec:
    """构造可比较的 Workload（同一模型/硬件/输入，只改执行模式）。"""
    return WorkloadSpec(
        workload_id=workload_id,
        model=ModelSpec(name="Qwen3.5-35B-A3B", dtype="bfloat16", is_moe=True),
        hardware=HardwareSpec(
            device="Ascend 910B", device_count=1, device_profile_id="ascend_910b"
        ),
        execution=ExecutionSpec(phase=phase, mode=mode, graph_mode=graph_mode),
        input=InputSpec(
            batch_size=batch_size, input_length=input_length, output_length=output_length
        ),
    )


def write_csv(
    path: Path,
    header: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(header))
        for row in rows:
            writer.writerow(list(row))
    return path


def write_op_summary(
    path: Path,
    rows: Iterable[Sequence[object]],
    header: Sequence[str] = OP_SUMMARY_HEADER,
) -> Path:
    return write_csv(path, header, rows)


def summary_row(
    operator: str,
    duration_us: float,
    *,
    start_us: float = 0.0,
    task_type: str = "AI_CORE",
    model_id: str = "4294967295",
    block_num: int = 24,
    aicore_us: float | None = None,
    cube_fops: float | None = None,
) -> list[object]:
    """按 ``OP_SUMMARY_HEADER`` 顺序生成一行；未给定的指标留空（视为未采集）。"""
    return [
        0,
        model_id,
        task_type,
        operator,
        start_us,
        duration_us,
        "1,1024",
        "DT_BF16",
        "1,1024",
        "DT_BF16",
        block_num,
        "" if aicore_us is None else aicore_us,
        "",
        "" if cube_fops is None else cube_fops,
        "",
    ]


def make_rank_export(root: Path, rank: int, *, prof: str | None = None) -> Path:
    """建立 ``PROF_*/device_<rank>`` 布局，返回 PROF 目录。"""
    prof_dir = root / (prof or f"PROF_00000{rank}_fixture")
    (prof_dir / f"device_{rank}").mkdir(parents=True, exist_ok=True)
    return prof_dir


def write_rank_summary(
    root: Path,
    rank: int,
    rows: Iterable[Sequence[object]],
    *,
    file_name: str | None = None,
    header: Sequence[str] = OP_SUMMARY_HEADER,
) -> Path:
    prof_dir = make_rank_export(root, rank)
    output = prof_dir / "mindstudio_profiler_output"
    name = file_name or f"op_summary_{rank}.csv"
    return write_op_summary(output / name, rows, header)


def make_timeline_db(
    path: Path,
    *,
    host: Sequence[tuple[float, float, str]] = (),
    tasks: Sequence[tuple[float, float]] = (),
    enum_mstx: bool = False,
) -> Path:
    """建一个最小 msprof 库（TASK/CANN_API/STRING_IDS），时间单位 ns。

    ``tasks`` 写入 TASK 表（回退窗口路径使用）；``enum_mstx`` 只建
    ``ENUM_MSTX_EVENT_TYPE`` 枚举表（模拟「没开打点但有枚举」）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.executemany(
        "insert into TASK values (?, ?)",
        [(int(start * 1000), int(end * 1000)) for start, end in tasks],
    )
    connection.executemany(
        "insert into CANN_API values (?, ?, ?)",
        [(name, int(start * 1000), int(end * 1000)) for start, end, name in host],
    )
    if enum_mstx:
        connection.execute(
            "create table ENUM_MSTX_EVENT_TYPE (id integer, name text, value text)"
        )
        connection.executemany(
            "insert into ENUM_MSTX_EVENT_TYPE values (?, ?, ?)",
            [(0, "marker", "0"), (1, "push_pop", "1")],
        )
    connection.commit()
    connection.close()
    return path


def make_op_report_db(path: Path, rows: Iterable[Sequence[object]]) -> Path:
    """建 ``op_counter.db::op_report``；``total/min/avg/max`` 单位是 ns。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "create table op_report (op_type text, core_type text, occurrences integer,"
        " total_time integer, min integer, avg integer, max integer, ratio real)"
    )
    connection.executemany("insert into op_report values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return path
