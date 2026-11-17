"""Collection-contract gate: refuse to compare runs that were not collected alike.

这次踩过的坑说明"能不能比"必须先于"差多少"：

* 两轮并发/批不同（M=1 vs 图捕获 pad 到 16），导致 kernel 耗时不可比；
* Graph 把 HCCL 记录合并，导致通信条数/占比不可比（字节量才守恒）；
* 一侧 rank 缺失 CSV，另一侧完整；
* 窗口轮次不同，差异里混进了窗口口径。

本模块只做检查与解释，不给结论。任何 ``fail`` 都会让 ``verdict`` 变成
``incomparable``，提示调用方不要据此出结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 只有 graph 模式才存在的采集参数；eager 侧缺失不算"未记录"
GRAPH_ONLY_PARAMS = frozenset({"cudagraph_capture_sizes"})

__all__ = ["ContractCheck", "ContractReport", "compare_contracts", "load_table_json"]

FAIL = "fail"
WARN = "warn"
PASS = "pass"
INFO = "info"

#: 通信字节量允许的相对偏差（超出则说明通信工作量不同）
COMM_BYTES_TOLERANCE = 0.25


@dataclass(frozen=True)
class ContractCheck:
    name: str
    a: Any
    b: Any
    status: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "a": self.a,
            "b": self.b,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class ContractReport:
    a_id: str
    b_id: str
    verdict: str
    checks: tuple[ContractCheck, ...]

    @property
    def failures(self) -> list[ContractCheck]:
        return [item for item in self.checks if item.status == FAIL]

    @property
    def warnings(self) -> list[ContractCheck]:
        return [item for item in self.checks if item.status == WARN]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "a": self.a_id,
            "b": self.b_id,
            "verdict": self.verdict,
            "failures": [item.name for item in self.failures],
            "warnings": [item.name for item in self.warnings],
            "checks": [item.to_dict() for item in self.checks],
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def load_table_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if target.is_dir():
        target = target / "operator_table.json"
    if not target.is_file():
        raise FileNotFoundError(f"{target}: 不是 operator_table.json，也不是包含它的目录")
    return json.loads(target.read_text(encoding="utf-8"))


def _workload(table: dict) -> dict:
    return table.get("workload") or {}


_KNOWN_INPUT_KEYS = {"batch_size", "input_length", "output_length", "num_requests", "extra"}


def _input_extra(table: dict) -> dict:
    """采集参数：既可能是 input.extra，也可能是 input 下的额外键。"""
    data = _workload(table).get("input") or {}
    flat = {key: value for key, value in data.items() if key not in _KNOWN_INPUT_KEYS}
    flat.update(data.get("extra") or {})
    return flat


def _check(name: str, a: Any, b: Any, status: str, note: str) -> ContractCheck:
    return ContractCheck(name=name, a=a, b=b, status=status, note=note)


def compare_contracts(a: dict[str, Any], b: dict[str, Any]) -> ContractReport:
    wa, wb = _workload(a), _workload(b)
    checks: list[ContractCheck] = []

    # 1. 阶段：prefill 与 decode 的绝对量不可比
    phase_a = (wa.get("execution") or {}).get("phase")
    phase_b = (wb.get("execution") or {}).get("phase")
    checks.append(
        _check(
            "execution.phase",
            phase_a,
            phase_b,
            PASS if phase_a == phase_b else FAIL,
            "同一阶段才可比较绝对量" if phase_a == phase_b else "跨阶段只能比较占比结构",
        )
    )

    # 2. 硬件
    hw_a, hw_b = wa.get("hardware") or {}, wb.get("hardware") or {}
    same_hw = (hw_a.get("device"), hw_a.get("device_count")) == (
        hw_b.get("device"),
        hw_b.get("device_count"),
    )
    checks.append(
        _check(
            "hardware",
            f"{hw_a.get('device')}x{hw_a.get('device_count')}",
            f"{hw_b.get('device')}x{hw_b.get('device_count')}",
            PASS if same_hw else FAIL,
            "设备与卡数一致" if same_hw else "设备/卡数不同，无法归因",
        )
    )

    # 3. 输入形状
    ia, ib = wa.get("input") or {}, wb.get("input") or {}
    for key in ("batch_size", "input_length", "output_length"):
        left, right = ia.get(key), ib.get(key)
        if left is None or right is None:
            status, note = INFO, "一侧未记录该字段，无法校验"
        elif left == right:
            status, note = PASS, "一致"
        else:
            status, note = FAIL, f"{key} 不同（{left} vs {right}），不是同一负载"
        checks.append(_check(f"input.{key}", left, right, status, note))

    # 4. 采集参数（并发 / pad / max_num_seqs）
    #    graph 专属参数在 eager 侧本来就不存在，不能算"一侧未记录"
    extra_a, extra_b = _input_extra(a), _input_extra(b)
    mode_a_for_param = (wa.get("execution") or {}).get("mode")
    mode_b_for_param = (wb.get("execution") or {}).get("mode")
    eager_side = "eager" in (mode_a_for_param, mode_b_for_param)
    for key in sorted(set(extra_a) | set(extra_b)):
        left, right = extra_a.get(key), extra_b.get(key)
        if key in GRAPH_ONLY_PARAMS and eager_side:
            checks.append(
                _check(
                    f"input.{key}",
                    left,
                    right,
                    INFO,
                    "eager 模式不适用图捕获参数，不参与比较",
                )
            )
            continue
        if left == right:
            status, note = PASS, "一致"
        elif left is None or right is None:
            status, note = WARN, "一侧未记录该采集参数"
        else:
            status, note = WARN, f"{key} 不同（{left} vs {right}），会影响绝对时长"
        checks.append(_check(f"input.{key}", left, right, status, note))

    # 5. 执行模式（这是通常要比较的变量，所以只记录不拦）
    mode_a = (wa.get("execution") or {}).get("mode")
    mode_b = (wb.get("execution") or {}).get("mode")
    checks.append(
        _check(
            "execution.mode",
            mode_a,
            mode_b,
            INFO,
            "执行模式差异是被比较的对象，不算契约违规" if mode_a != mode_b else "同模式",
        )
    )

    # 6. 窗口
    window_a = ((a.get("meta") or {}).get("extra") or {}).get("window") or {}
    window_b = ((b.get("meta") or {}).get("extra") or {}).get("window") or {}
    rounds_a, rounds_b = window_a.get("rounds"), window_b.get("rounds")
    same_window = rounds_a == rounds_b
    checks.append(
        _check(
            "window.rounds",
            rounds_a,
            rounds_b,
            PASS if same_window else FAIL,
            "窗口一致" if same_window else "轮次窗口不同，差异里会混入窗口口径",
        )
    )

    # 7. 算子集合与实现路径
    ops_a = {record["operator"] for record in a.get("records", [])}
    ops_b = {record["operator"] for record in b.get("records", [])}
    only_a, only_b = ops_a - ops_b, ops_b - ops_a
    shape_counts_a = _shape_counts(a)
    shape_counts_b = _shape_counts(b)
    differing = [op for op in ops_a & ops_b if shape_counts_a.get(op) != shape_counts_b.get(op)]
    status = PASS if not only_a and not only_b and not differing else WARN
    checks.append(
        _check(
            "operator_set",
            f"{len(ops_a)} ops",
            f"{len(ops_b)} ops",
            status,
            (
                f"仅 A: {sorted(only_a)[:4]}; 仅 B: {sorted(only_b)[:4]}; "
                f"shape 组合数不同: {differing[:4]}"
            )
            if status != PASS
            else "算子集合与 shape 组合一致",
        )
    )

    # 8. 通信字节量（唯一守恒量）
    bytes_a = ((a.get("meta") or {}).get("extra") or {}).get("comm_volume", {}).get("bytes_total")
    bytes_b = ((b.get("meta") or {}).get("extra") or {}).get("comm_volume", {}).get("bytes_total")
    if bytes_a and bytes_b:
        ratio = bytes_b / bytes_a
        if abs(ratio - 1) <= COMM_BYTES_TOLERANCE:
            status, note = PASS, f"字节量 {ratio:.3f}x（±{COMM_BYTES_TOLERANCE:.0%} 内，通信工作量一致）"
        elif abs(ratio - 1) <= COMM_BYTES_TOLERANCE * 2:
            status, note = WARN, f"字节量 {ratio:.3f}x，差异需解释（token 数/层数？）"
        else:
            status, note = FAIL, f"字节量 {ratio:.3f}x，通信工作量不同，占比不可比"
        checks.append(_check("comm.bytes", round(bytes_a / 1024**2, 1), round(bytes_b / 1024**2, 1), status, note))
    else:
        checks.append(_check("comm.bytes", bytes_a, bytes_b, INFO, "至少一侧缺少通信量数据"))

    # 9. 记录数（工作量）
    calls_a = sum(int(record.get("calls") or 0) for record in a.get("records", []))
    calls_b = sum(int(record.get("calls") or 0) for record in b.get("records", []))
    if calls_a and calls_b:
        ratio = calls_b / calls_a
        status = PASS if 0.8 <= ratio <= 1.25 else WARN
        checks.append(
            _check(
                "work.count",
                calls_a,
                calls_b,
                status,
                f"记录数比 {ratio:.3f}"
                + ("" if status == PASS else "：工作量差异较大，先确认是否同一负载"),
            )
        )

    failures = [item for item in checks if item.status == FAIL]
    warnings = [item for item in checks if item.status == WARN]
    if failures:
        verdict = "incomparable"
    elif warnings:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"

    return ContractReport(
        a_id=wa.get("workload_id", "a"),
        b_id=wb.get("workload_id", "b"),
        verdict=verdict,
        checks=tuple(checks),
    )


def _shape_counts(table: dict) -> dict[str, str]:
    counts: dict[str, str] = {}
    for record in table.get("records", []):
        value = (record.get("attributes") or {}).get("shape_count")
        if value is not None:
            counts[record["operator"]] = str(value)
    return counts
