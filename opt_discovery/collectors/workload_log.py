"""Parse the benchmark harness log (``workload.log``).

This is the only **external** source of round boundaries.  The harness writes
one ``ACTIVE_ROUND`` line per measured round, in the same epoch-second clock as
the profiler's ``Task Start Time(us)`` (epoch microseconds), e.g.

    PROFILE_START_ABS 1794130414.75559
    ACTIVE_ROUND 0 start 1794130414.7556078 end 1794130415.0960898

Verified against ``$PROF_ROOT/qwen35-prefill``: ``PROFILE_START_ABS`` converts
to ``1794130414755590 us``, which equals the first profiled task timestamp.

Because the boundaries are recorded by the harness, not inferred from gaps, a
steady-state window derived from them is stable across Eager/Graph runs -- which
is exactly what a fair Eager-vs-Graph comparison requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Round", "RunLog", "LogParseError"]


class LogParseError(ValueError):
    """Raised when the harness log is missing required markers."""


_ROUND_RE = re.compile(
    r"ACTIVE_ROUND\s+(?P<index>\d+)\s+start\s+(?P<start>\d+(?:\.\d+)?)\s+end\s+(?P<end>\d+(?:\.\d+)?)"
    r"(?:[ \t]+out_tokens[ \t]+(?P<output>\d+))?"
)
_PROMPT_RE = re.compile(r"PROMPT_TOKENS\s+(?P<tokens>\d+)")
_LOAD_RE = re.compile(r"LOAD_SECONDS\s+(?P<seconds>\d+(?:\.\d+)?)")
_PROFILE_START_RE = re.compile(r"PROFILE_START_ABS\s+(?P<ts>\d+(?:\.\d+)?)")
_WARMUP_RE = re.compile(r"WARMUP_DONE_ABS\s+(?P<ts>\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Round:
    """One measured round, expressed in profiler time (microseconds)."""

    index: int
    start_us: float
    end_us: float
    output_tokens: int | None = None

    @property
    def duration_us(self) -> float:
        return self.end_us - self.start_us

    def to_dict(self) -> dict[str, float]:
        return {
            "index": self.index,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "duration_us": self.duration_us,
        }


@dataclass(frozen=True)
class RunLog:
    path: str
    rounds: tuple[Round, ...]
    prompt_tokens: int | None = None
    load_seconds: float | None = None
    profile_start_us: float | None = None
    warmup_done_us: float | None = None

    @property
    def round_indices(self) -> tuple[int, ...]:
        return tuple(r.index for r in self.rounds)

    @classmethod
    def parse(cls, path: str | Path) -> "RunLog":
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        rounds = tuple(
            Round(
                index=int(match.group("index")),
                start_us=float(match.group("start")) * 1e6,
                end_us=float(match.group("end")) * 1e6,
                output_tokens=int(output) if (output := match.group("output")) else None,
            )
            for match in _ROUND_RE.finditer(text)
        )
        if not rounds:
            raise LogParseError(
                f"{path}: 未找到 ACTIVE_ROUND 行；无法按轮次法判定窗口"
                "（可退回 gap 法，但必须在报告中标注 provenance=derived）"
            )
        ordered = tuple(sorted(rounds, key=lambda item: item.index))
        prompt = _PROMPT_RE.search(text)
        load = _LOAD_RE.search(text)
        start = _PROFILE_START_RE.search(text)
        warmup = _WARMUP_RE.search(text)
        return cls(
            path=str(path),
            rounds=ordered,
            prompt_tokens=int(prompt.group("tokens")) if prompt else None,
            load_seconds=float(load.group("seconds")) if load else None,
            profile_start_us=float(start.group("ts")) * 1e6 if start else None,
            warmup_done_us=float(warmup.group("ts")) * 1e6 if warmup else None,
        )

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "prompt_tokens": self.prompt_tokens,
            "load_seconds": self.load_seconds,
            "profile_start_us": self.profile_start_us,
            "warmup_done_us": self.warmup_done_us,
            "rounds": [r.to_dict() for r in self.rounds],
        }
