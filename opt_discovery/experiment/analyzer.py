"""Compare two benchmark runs (P0-2).

守则：
* 只有样本 >= 3 轮才给结论，否则明确返回 ``insufficient_samples``；
* 只有变化超过 ``min_effect`` 才判"更快/更慢"，否则判"无显著差异"；
* 两侧 ``prompt_tokens`` 不同时打警告（不是同一负载）；
* 结论是**整轮延迟**级别的，不能直接当成 Serving 的 TTFT/TPOT。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["BenchmarkComparison", "load_run", "compare_benchmarks"]

MIN_ROUNDS = 3


@dataclass
class BenchmarkComparison:
    a_label: str
    b_label: str
    verdict: str
    rounds_a: int
    rounds_b: int
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "a": self.a_label,
            "b": self.b_label,
            "verdict": self.verdict,
            "rounds_a": self.rounds_a,
            "rounds_b": self.rounds_b,
            "metrics": self.metrics,
            "notes": list(self.notes),
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def load_run(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ratio(a: float | None, b: float | None) -> float | None:
    if a in (None, 0) or b is None:
        return None
    return b / a


def compare_benchmarks(
    a: dict[str, Any], b: dict[str, Any], *, min_effect: float = 0.05
) -> BenchmarkComparison:
    notes: list[str] = []
    rounds_a = int(a.get("summary", {}).get("rounds") or 0)
    rounds_b = int(b.get("summary", {}).get("rounds") or 0)

    if a.get("prompt_tokens") != b.get("prompt_tokens"):
        notes.append(
            f"两侧 prompt_tokens 不同（{a.get('prompt_tokens')} vs {b.get('prompt_tokens')}），"
            "不是同一负载，比值仅供参考"
        )
    if a.get("harness") == b.get("harness"):
        notes.append("两侧使用同一 harness 脚本：差异来自执行配置（eager/graph、并发、pad 等）")

    if rounds_a < MIN_ROUNDS or rounds_b < MIN_ROUNDS:
        return BenchmarkComparison(
            a_label=str(a.get("label", "a")),
            b_label=str(b.get("label", "b")),
            verdict="insufficient_samples",
            rounds_a=rounds_a,
            rounds_b=rounds_b,
            notes=tuple(notes + [f"样本不足（需要 >= {MIN_ROUNDS} 轮）"]),
        )

    metrics: dict[str, dict[str, Any]] = {}
    latency_a = a["summary"]["latency_s"]
    latency_b = b["summary"]["latency_s"]
    for key in ("median", "p90", "max"):
        ratio = _ratio(latency_a.get(key), latency_b.get(key))
        metrics[f"latency_{key}_s"] = {
            "a": latency_a.get(key),
            "b": latency_b.get(key),
            "ratio": None if ratio is None else round(ratio, 4),
            "delta_pct": None if ratio is None else round((ratio - 1) * 100, 2),
        }

    tokens_a = (a["summary"].get("tokens_per_s") or {}).get("median")
    tokens_b = (b["summary"].get("tokens_per_s") or {}).get("median")
    ratio_tokens = _ratio(tokens_a, tokens_b)
    metrics["tokens_per_s_median"] = {
        "a": tokens_a,
        "b": tokens_b,
        "ratio": None if ratio_tokens is None else round(ratio_tokens, 4),
        "delta_pct": None if ratio_tokens is None else round((ratio_tokens - 1) * 100, 2),
    }

    median_ratio = metrics["latency_median_s"]["ratio"]
    if median_ratio is None:
        verdict = "undetermined"
        notes.append("缺少可比较的中位延迟")
    elif median_ratio <= 1 - min_effect:
        verdict = "b_faster"
    elif median_ratio >= 1 + min_effect:
        verdict = "b_slower"
    else:
        verdict = "no_significant_difference"
        notes.append(
            f"中位延迟变化 {metrics['latency_median_s']['delta_pct']}% 小于判定阈值 "
            f"±{min_effect * 100:.0f}%"
        )

    notes.append("延迟为整轮墙钟（含 Python/调度开销），判定阈值可在 --min-effect 调整")
    if b.get("profiled") != a.get("profiled"):
        notes.append("一侧开启了 profiling、另一侧没有：profiling 本身会影响绝对时长")

    return BenchmarkComparison(
        a_label=str(a.get("label", "a")),
        b_label=str(b.get("label", "b")),
        verdict=verdict,
        rounds_a=rounds_a,
        rounds_b=rounds_b,
        metrics=metrics,
        notes=tuple(notes),
    )
