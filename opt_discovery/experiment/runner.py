"""Run the existing vLLM harness and turn its stdout into a benchmark record (P0-2).

The harness is **not** a CLI tool: ``$NPU_STACK/qwen35-*.py`` 里参数全部硬编码，
它只往 stdout 打印带时间戳的轮次标记：

    PROCESS_START_ABS 1794...
    PROMPT_TOKENS 1 LOAD_SECONDS 77.5
    WARMUP_DONE_ABS 1794...
    PROFILE_START_ABS 1794...
    ACTIVE_ROUND 0 start 1794... end 1794... out_tokens 64
    ...
    PROFILE_STOP_ABS 1794...

So "benchmark" here = 每一轮的墙钟时长与 out_tokens，粒度是**整轮延迟**
（含 Python 调用开销），不是 TTFT/TPOT。这一点写在输出的 ``notes`` 里，避免被误读。

Profiling 是外部启动的（脚本内不含 msprof），因此本模块把 ``--msprof-args`` 当作
透传参数：给了就包一层 msprof，没给就只跑 harness。
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "RoundSample",
    "BenchmarkRun",
    "parse_rounds",
    "run_benchmark",
    "summarize_run",
]

_ROUND_RE = re.compile(
    r"ACTIVE_ROUND\s+(?P<index>\d+)\s+start\s+(?P<start>\d+(?:\.\d+)?)"
    r"\s+end\s+(?P<end>\d+(?:\.\d+)?)(?:.*?out_tokens\s+(?P<tokens>\d+))?"
)
_PROMPT_RE = re.compile(r"PROMPT_TOKENS\s+(?P<tokens>\d+)")
_LOAD_RE = re.compile(r"LOAD_SECONDS\s+(?P<seconds>\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class RoundSample:
    index: int
    duration_s: float
    out_tokens: int | None = None

    @property
    def tokens_per_s(self) -> float | None:
        if not self.out_tokens or self.duration_s <= 0:
            return None
        return self.out_tokens / self.duration_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "duration_s": round(self.duration_s, 6),
            "out_tokens": self.out_tokens,
            "tokens_per_s": None if self.tokens_per_s is None else round(self.tokens_per_s, 3),
        }


@dataclass
class BenchmarkRun:
    label: str
    harness: str
    command: list[str]
    log_path: str
    exit_code: int | None
    wall_s: float
    rounds: tuple[RoundSample, ...] = ()
    prompt_tokens: int | None = None
    load_s: float | None = None
    profiled: bool = False
    started_at: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        summary = summarize_run(self.rounds)
        return {
            "schema_version": 1,
            "label": self.label,
            "harness": self.harness,
            "command": self.command,
            "log_path": self.log_path,
            "exit_code": self.exit_code,
            "wall_s": round(self.wall_s, 3),
            "prompt_tokens": self.prompt_tokens,
            "load_s": self.load_s,
            "profiled": self.profiled,
            "started_at": self.started_at,
            "rounds": [item.to_dict() for item in self.rounds],
            "summary": summary,
            "notes": list(self.notes),
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def parse_rounds(text: str) -> tuple[list[RoundSample], int | None, float | None]:
    rounds: list[RoundSample] = []
    for match in _ROUND_RE.finditer(text):
        start = float(match.group("start"))
        end = float(match.group("end"))
        tokens = match.group("tokens")
        rounds.append(
            RoundSample(
                index=int(match.group("index")),
                duration_s=end - start,
                out_tokens=int(tokens) if tokens is not None else None,
            )
        )
    prompt = _PROMPT_RE.search(text)
    load = _LOAD_RE.search(text)
    return (
        sorted(rounds, key=lambda item: item.index),
        int(prompt.group("tokens")) if prompt else None,
        float(load.group("seconds")) if load else None,
    )


def _percentile(values: Sequence[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def summarize_run(rounds: Sequence[RoundSample]) -> dict[str, Any]:
    durations = [item.duration_s for item in rounds]
    tokens = [item.tokens_per_s for item in rounds if item.tokens_per_s is not None]
    if not durations:
        return {"rounds": 0}
    return {
        "rounds": len(durations),
        "latency_s": {
            "min": round(min(durations), 6),
            "median": round(_percentile(durations, 0.5) or 0.0, 6),
            "max": round(max(durations), 6),
            "p90": round(_percentile(durations, 0.9) or 0.0, 6),
        },
        "tokens_per_s": {
            "median": round(_percentile(tokens, 0.5), 3) if tokens else None,
            "min": round(min(tokens), 3) if tokens else None,
            "max": round(max(tokens), 3) if tokens else None,
        },
    }


def run_benchmark(
    harness: str | Path,
    out_dir: str | Path,
    *,
    label: str | None = None,
    python: str = "python3",
    timeout_s: float | None = None,
    msprof_args: str | None = None,
    env_extra: dict[str, str] | None = None,
    print_only: bool = False,
) -> BenchmarkRun:
    """Execute the harness once and parse its rounds."""
    script = Path(harness)
    if not script.is_file():
        raise FileNotFoundError(f"{script}: harness 脚本不存在")

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    log_path = target / "workload.log"

    command = [python, str(script)]
    profiled = bool(msprof_args)
    if msprof_args:
        command = ["msprof", *shlex.split(msprof_args), *command]

    run_label = label or script.stem
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    notes: list[str] = [
        "延迟口径：整轮墙钟时间（含 Python/调度开销），不是 TTFT/TPOT",
        "Profiling 由外部启动；本记录的 profiled 字段仅表示是否透传了 msprof 参数",
    ]

    if print_only:
        return BenchmarkRun(
            label=run_label,
            harness=str(script),
            command=command,
            log_path=str(log_path),
            exit_code=None,
            wall_s=0.0,
            profiled=profiled,
            started_at=started_at,
            notes=tuple(notes + ["print_only：未真正执行"]),
        )

    env = None
    if env_extra:
        import os

        env = {**os.environ, **env_extra}

    started = time.time()
    with log_path.open("w", encoding="utf-8") as handle:
        try:
            completed = subprocess.run(
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                env=env,
                check=False,
            )
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = -1
            notes.append(f"超时（{timeout_s}s），harness 已被终止")
    wall = time.time() - started

    text = log_path.read_text(encoding="utf-8", errors="replace")
    rounds, prompt_tokens, load_s = parse_rounds(text)
    if not rounds:
        notes.append("未解析到 ACTIVE_ROUND：harness 可能启动失败，请查看 workload.log")
    if code not in (0, None):
        notes.append(f"harness 退出码 {code}")

    return BenchmarkRun(
        label=run_label,
        harness=str(script),
        command=command,
        log_path=str(log_path),
        exit_code=code,
        wall_s=wall,
        rounds=tuple(rounds),
        prompt_tokens=prompt_tokens,
        load_s=load_s,
        profiled=profiled,
        started_at=started_at,
        notes=tuple(notes),
    )
