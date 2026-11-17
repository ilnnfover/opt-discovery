"""Communication volume: the one conserved quantity across execution modes.

Why this exists
---------------
In Graph mode msprof reports far fewer HCCL records, so "communication share"
and even "communication call count" are not comparable between Eager and Graph.
The **transferred bytes** are, because they are determined by the model
(tokens x layers x hidden x TP), not by the launch mechanism.

Measured on ``$PROF_ROOT`` (decode, 5 rounds):

===========  ===========  ===========  ==============
run          records      unique tasks bytes (dedup)
===========  ===========  ===========  ==============
eager        366,797      132,415      22.28 GB
graph         21,890       21,890      23.29 GB
===========  ===========  ===========  ==============

=> bytes are ~equal (+4.5%), records differ 6x: Graph **merges** HCCL records.

Dedup rule: ``COMMUNICATION_TASK_INFO`` has 1-3 rows per ``globalTaskId``
(plane / notify duplicates).  Taking ``max(size)`` per task removes the
duplication: eager 45.15 GB -> 22.28 GB (ratio ~2.0, matching the duplicate
rows), graph 23.29 GB -> 23.29 GB (no duplicates).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CommVolume", "CommVolumeError", "summarize_comm"]


class CommVolumeError(RuntimeError):
    """Raised when the sqlite cannot provide communication records."""


@dataclass
class CommVolume:
    db_path: str
    available: bool
    records: int = 0
    unique_tasks: int = 0
    bytes_total: float = 0.0
    bandwidth_mean_gbps: float = 0.0
    groups: int = 0
    notes: tuple[str, ...] = ()

    @property
    def dedup_ratio(self) -> float:
        return self.records / self.unique_tasks if self.unique_tasks else 0.0

    @property
    def kib_per_task(self) -> float:
        return self.bytes_total / self.unique_tasks / 1024 if self.unique_tasks else 0.0

    def to_dict(self) -> dict:
        return {
            "db_path": self.db_path,
            "available": self.available,
            "records": self.records,
            "unique_tasks": self.unique_tasks,
            "dedup_ratio": round(self.dedup_ratio, 3),
            "bytes_total": self.bytes_total,
            "mib_total": round(self.bytes_total / 1024**2, 2),
            "kib_per_task": round(self.kib_per_task, 1),
            "bandwidth_mean_gbps": round(self.bandwidth_mean_gbps, 2),
            "groups": self.groups,
            "notes": list(self.notes),
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def summarize_comm(db_path: str | Path) -> CommVolume:
    """Read communication volume (whole run; the table has no timestamps)."""
    path = Path(db_path)
    if not path.is_file():
        raise CommVolumeError(f"{path}: 文件不存在")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover
        raise CommVolumeError(f"{path}: 无法打开 sqlite: {exc}") from exc

    with connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        if "COMMUNICATION_TASK_INFO" not in tables:
            return CommVolume(
                db_path=str(path),
                available=False,
                notes=("缺少 COMMUNICATION_TASK_INFO，该 rank 没有通信采集",),
            )
        rows = connection.execute(
            """
            select count(*), coalesce(sum(size), 0), count(distinct groupName)
            from COMMUNICATION_TASK_INFO
            """
        ).fetchone()
        records = int(rows[0])
        dedup = connection.execute(
            """
            select count(*), coalesce(sum(size), 0), coalesce(avg(bandwidth), 0)
            from (select globalTaskId, max(size) as size, max(bandwidth) as bandwidth
                  from COMMUNICATION_TASK_INFO group by globalTaskId)
            """
        ).fetchone()

    unique_tasks = int(dedup[0])
    bytes_total = float(dedup[1])
    notes: list[str] = [
        "字节数为按 globalTaskId 去重后的总量（同一通信任务的 plane 重复行只计一次）",
        "通信量与执行模式无关，可用于跨 Eager/Graph 校验；记录条数不可比",
    ]
    if records and unique_tasks and records / unique_tasks > 1.5:
        notes.append(
            f"原始行数是任务的 {records / unique_tasks:.1f} 倍（多 plane 重复），已按任务去重"
        )
    return CommVolume(
        db_path=str(path),
        available=True,
        records=records,
        unique_tasks=unique_tasks,
        bytes_total=bytes_total,
        bandwidth_mean_gbps=float(dedup[2]) / 1e9,
        groups=int(rows[2] or 0),
        notes=tuple(notes),
    )
