"""MSTX 打点解析（B：host_silent / sync_wait 的证据源）。

profiler 把 MSTX 落到合并库的 ``MsprofTx`` 表（另有 ``MsprofTxEx``），列由
``/usr/local/Ascend/cann-9.1.0/tools/profiler/profiler_tool/analysis/msmodel/msproftx/
msproftx_model.py`` 的查询语句确定：

    category, pid, tid, start_time, end_time, payload_type, payload_value,
    message_type, message, event_type

``event_type`` 取值见 ``ENUM_MSTX_EVENT_TYPE``：0 marker、1 push/pop、2 start/end、3 marker_ex。

单位：同一库内其它表（``TASK.startNs``）用 ns，因此这里默认 ns；但为了避免 1000 倍
量级的静默错误，加载时会用窗口做一次**单位嗅探**，取"落窗率更高"的解释并记录 note。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["MstxRange", "MstxTrace", "MstxError", "load_mstx", "SYNC_HINTS"]

#: 名字里带这些片段的范围视为"同步等待"
SYNC_HINTS = ("sync", "wait", "barrier", "join", "allreduce_wait")

_TABLE = "MsprofTx"


class MstxError(RuntimeError):
    """打点数据无法解析（表存在但列不符）。"""


@dataclass(frozen=True)
class MstxRange:
    name: str
    start_us: float
    end_us: float
    event_type: int = 2
    tid: int = 0
    category: str = ""

    @property
    def duration_us(self) -> float:
        return max(0.0, self.end_us - self.start_us)

    @property
    def is_sync(self) -> bool:
        lowered = self.name.lower()
        return any(hint in lowered for hint in SYNC_HINTS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start_us": round(self.start_us, 3),
            "end_us": round(self.end_us, 3),
            "duration_us": round(self.duration_us, 3),
            "event_type": self.event_type,
            "tid": self.tid,
            "is_sync": self.is_sync,
        }


@dataclass
class MstxTrace:
    db_path: str
    available: bool
    ranges: tuple[MstxRange, ...] = ()
    unit: str = "ns"
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_us(self) -> float:
        return sum(item.duration_us for item in self.ranges)

    @property
    def sync_us(self) -> float:
        return sum(item.duration_us for item in self.ranges if item.is_sync)

    def by_name(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for item in self.ranges:
            totals[item.name] = totals.get(item.name, 0.0) + item.duration_us
        return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "available": self.available,
            "unit": self.unit,
            "range_count": len(self.ranges),
            "total_us": round(self.total_us, 3),
            "sync_us": round(self.sync_us, 3),
            "by_name": {name: round(value, 3) for name, value in self.by_name().items()},
            "notes": list(self.notes),
        }


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute("select name from sqlite_master where type='table'")
    }


def _resolve_message(value: Any, strings: dict[int, str] | None) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, int) and strings and value in strings:
        return strings[value]
    text = str(value).strip()
    return text or "unknown"


def load_mstx(db_path: str | Path, window: Any | None = None) -> MstxTrace:
    path = Path(db_path)
    if not path.is_file():
        raise MstxError(f"{path}: 文件不存在")

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with connection:
        tables = _tables(connection)
        if _TABLE not in tables:
            return MstxTrace(
                db_path=str(path),
                available=False,
                notes=(
                    f"缺少 {_TABLE} 表：本次采集没有开启 MSTX 打点，"
                    "host_silent 只能停在“无法进一步归因”",
                ),
            )
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_TABLE})")}
        required = {"start_time", "end_time", "message"}
        if not required <= columns:
            raise MstxError(f"{path}: {_TABLE} 列不符，缺少 {sorted(required - columns)}")

        strings: dict[int, str] | None = None
        if "STRING_IDS" in tables:
            strings = {
                int(row[0]): str(row[1])
                for row in connection.execute("select id, value from STRING_IDS")
            }

        rows = list(
            connection.execute(
                f"select start_time, end_time, event_type, message, "
                f"coalesce(tid, 0), coalesce(category, '') from {_TABLE}"
            )
        )

    if not rows:
        return MstxTrace(
            db_path=str(path), available=True, notes=(f"{_TABLE} 表为空（采集时未实际打点）",)
        )

    raw = [
        (float(start), float(end), int(event or 0), _resolve_message(message, strings),
         int(tid or 0), str(category or ""))
        for start, end, event, message, tid, category in rows
    ]

    unit = _sniff_unit(raw, window)
    factor = 1000.0 if unit == "ns" else 1.0
    ranges: list[MstxRange] = []
    for start, end, event, name, tid, category in raw:
        start_us, end_us = start / factor, end / factor
        if window is not None:
            lo, hi = window.start_us, window.end_us
            if end_us <= lo or start_us >= hi:
                continue
            start_us, end_us = max(start_us, lo), min(end_us, hi)
        ranges.append(MstxRange(name, start_us, end_us, event, tid, category))

    notes = [
        f"{_TABLE}: {len(raw)} 条打点，窗口内 {len(ranges)} 条",
        f"时间单位按 {unit} 解释（用窗口落窗率嗅探）",
    ]
    if any(item.is_sync for item in ranges):
        notes.append("存在同步类打点（名字含 sync/wait/barrier），可用于支撑 synchronization_bound")
    else:
        notes.append("没有同步类打点：仍不能据此判定同步受限")
    return MstxTrace(db_path=str(path), available=True, ranges=tuple(ranges), unit=unit, notes=tuple(notes))


def _sniff_unit(rows, window: Any | None) -> str:
    """在 ns / us 之间选落窗率更高的解释。"""
    if window is None:
        return "ns"
    lo, hi = window.start_us, window.end_us
    span = max(hi - lo, 1.0)

    def inside(factor: float) -> float:
        hits = 0
        for start, end, *_ in rows:
            middle = (start + end) / 2 / factor
            if lo <= middle <= hi:
                hits += 1
        return hits / len(rows)

    ns_ratio, us_ratio = inside(1000.0), inside(1.0)
    if us_ratio > ns_ratio + 0.1:
        return "us"
    if ns_ratio > us_ratio + 0.1:
        return "ns"
    # 两者接近时按库内其它表的惯例取 ns
    return "ns" if span > 0 else "ns"
