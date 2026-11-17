"""Kernel/operator -> source index (P1-1).

映射难点（已用 $NPU_STACK 的真实仓库验证）：

* Profiler 的 OP Type 多为内核/C++ 符号：GroupedMatmul、MoeGatingTopK 只在 csrc 里，
  Python 侧是 snake_case（fused_gdn_gating）；
* hcom_allReduce_ 是 HCCL 运行时名，源码中不存在同名符号；
* MatMulV2/Add/Cast 是 CANN 内置算子，源码里没有定义。

因此索引分四层，逐层降级并记录 confidence；宁可 unmapped 也不硬指一个错位置。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "SourceHit",
    "OperatorSources",
    "SourceIndex",
    "write_index",
    "RUNTIME_OPS",
    "BUILTIN_OPS",
    "SKIP_DIRS",
    "TIER_CONFIDENCE",
]

RUNTIME_OPS = ("hcom_", "hccl_", "memset", "memcpy")

BUILTIN_OPS = frozenset(
    {
        "Add", "Cast", "Mul", "Div", "Sub", "MatMul", "MatMulV2", "MatMulV3",
        "BatchMatMulV2", "Transpose", "Concat", "ConcatD", "Slice", "ZerosLike",
        "Fill", "Range", "Abs", "Sigmoid", "Gelu", "Cos", "Sin", "Reciprocal",
        "RealDiv", "LayerNormV3", "MemSet",
    }
)

SKIP_DIRS = frozenset(
    {
        "__pycache__", ".git", ".mypy_cache", ".pytest_cache", "build", "dist",
        "node_modules", "tests", "test", "docs", "examples", "benchmarks",
        "third_party", "vendors",
    }
)
_SUFFIXES = {".py": "python", ".cpp": "cpp", ".cc": "cpp", ".h": "cpp", ".hpp": "cpp"}
MAX_HITS = 8

#: 各层的置信度（binding 最高：注册表是确定性映射）
TIER_CONFIDENCE = {"binding": 0.95, "exact": 0.9, "snake": 0.7, "prefixed": 0.65}
_TIER_RANK = {"exact": 0, "snake": 1, "prefixed": 2, "unmapped": 9, "builtin": 8, "runtime": 8}


@dataclass(frozen=True)
class SourceHit:
    repo: str
    path: str
    line: int
    language: str
    symbol: str
    snippet: str

    def ref(self) -> str:
        return f"{self.repo}:{self.path}:{self.line}"

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "path": self.path,
            "line": self.line,
            "language": self.language,
            "symbol": self.symbol,
            "snippet": self.snippet,
        }


@dataclass
class OperatorSources:
    operator: str
    tier: str
    confidence: float
    hits: list[SourceHit] = field(default_factory=list)
    reason: str | None = None
    binding: dict | None = None

    @property
    def mapped(self) -> bool:
        return bool(self.hits)

    def refs(self, limit: int = 3) -> tuple[str, ...]:
        return tuple(hit.ref() for hit in self.hits[:limit])

    def to_dict(self) -> dict:
        return {
            "operator": self.operator,
            "tier": self.tier,
            "confidence": self.confidence,
            "mapped": self.mapped,
            "reason": self.reason,
            "hit_count": len(self.hits),
            "binding": self.binding,
            "hits": [hit.to_dict() for hit in self.hits],
        }


def _snake_case(name: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    text = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", text)
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()


def _variants(operator: str) -> list[tuple[str, str, float]]:
    variants: list[tuple[str, str, float]] = [(operator, "exact", 0.9)]
    snake = _snake_case(operator)
    if snake and snake != operator.lower():
        variants.append((snake, "snake", 0.7))
        variants.append((f"npu_{snake}", "prefixed", 0.65))
        variants.append((f"aclnn_{snake}", "prefixed", 0.6))
    stripped = operator.rstrip("_")
    if stripped and stripped != operator:
        variants.append((stripped, "exact", 0.85))
    return variants


class SourceIndex:
    """One pass over the repos; collects hits for a given operator set."""

    def __init__(
        self,
        repos: Sequence[str | Path],
        *,
        include_cpp: bool = True,
        bindings=None,  # BindingTable | None
    ) -> None:
        self.repos = [Path(repo) for repo in repos]
        for repo in self.repos:
            if not repo.is_dir():
                raise FileNotFoundError(f"{repo}: 源码目录不存在")
        self.include_cpp = include_cpp
        self.bindings = bindings

    def scan(self, operators: Iterable[str]) -> dict[str, OperatorSources]:
        names = sorted({op for op in operators if op})
        lookup: dict[str, tuple[str, str, float]] = {}
        for op in names:
            for variant, tier, confidence in _variants(op):
                lookup.setdefault(variant, (op, tier, confidence))
        if not lookup:
            return {}

        pattern = re.compile(
            "|".join(re.escape(item) for item in sorted(lookup, key=len, reverse=True))
        )
        results = {op: self._empty_result(op) for op in names}
        resolved_by_binding: set[str] = set()
        if self.bindings is not None:
            resolved_by_binding = self._apply_bindings(names, results)

        pending = {
            variant: value for variant, value in lookup.items() if value[0] not in resolved_by_binding
        }
        if pending:
            pending_pattern = re.compile(
                "|".join(re.escape(item) for item in sorted(pending, key=len, reverse=True))
            )
            for repo in self.repos:
                self._scan_repo(repo, pending_pattern, pending, results)
        return results

    def _apply_bindings(self, names, results) -> set[str]:
        """注册表命中优先于文本匹配：命中后不再做通用扫描。"""
        resolved: set[str] = set()
        repo_root = Path(self.bindings.repo) if self.bindings.repo else (self.repos[0] if self.repos else Path("."))
        for operator in names:
            matches = self.bindings.resolve(operator)
            if not matches:
                continue
            kind, entry = matches[0]
            entry_ = results[operator]
            if entry_.tier in ("builtin", "runtime"):
                continue
            hits: list[SourceHit] = []
            if entry.impl_file and entry.impl_line:
                hits.append(
                    SourceHit(
                        repo=repo_root.name,
                        path=entry.impl_file,
                        line=entry.impl_line,
                        language="cpp",
                        symbol=entry.schema,
                        snippet=_line_snippet(repo_root / entry.impl_file, entry.impl_line),
                    )
                )
            if entry.def_line:
                hits.append(
                    SourceHit(
                        repo=repo_root.name,
                        path="csrc/torch_binding.cpp",
                        line=entry.def_line,
                        language="cpp",
                        symbol=entry.schema,
                        snippet=_line_snippet(repo_root / "csrc" / "torch_binding.cpp", entry.def_line),
                    )
                )
            if not hits and entry.include:
                hits.append(
                    SourceHit(
                        repo=repo_root.name,
                        path=f"csrc/{entry.include}",
                        line=1,
                        language="cpp",
                        symbol=entry.schema,
                        snippet="#include (注册表未定位到实现)",
                    )
                )
            if not hits:
                continue
            entry_.hits = hits
            entry_.tier = "binding"
            entry_.confidence = TIER_CONFIDENCE["binding"]
            entry_.reason = None
            entry_.binding = {
                "schema": entry.schema,
                "func": entry.func,
                "match": kind,
                "registered_at": f"csrc/torch_binding.cpp:{entry.def_line}",
            }
            resolved.add(operator)
        return resolved

    def _empty_result(self, operator: str) -> OperatorSources:
        if operator.lower().startswith(RUNTIME_OPS):
            return OperatorSources(
                operator, "runtime", 0.0,
                reason="HCCL/RTS 运行时算子，源码中无同名符号（应查 HCCL 日志/配置）",
            )
        if operator in BUILTIN_OPS:
            return OperatorSources(
                operator, "builtin", 0.0,
                reason="CANN 内置算子（Profiler 直接给出名字），vLLM 源码中无定义",
            )
        return OperatorSources(operator, "unmapped", 0.0, reason="未在给定源码中命中")

    def _scan_repo(self, repo: Path, pattern, lookup, results) -> None:
        for path in repo.rglob("*"):
            if path.suffix not in _SUFFIXES:
                continue
            if path.suffix in {".cpp", ".cc", ".h", ".hpp"} and not self.include_cpp:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not pattern.search(text):
                continue
            relative = path.relative_to(repo).as_posix()
            language = _SUFFIXES[path.suffix]
            seen: set[tuple[str, int]] = set()
            for line_no, line in enumerate(text.splitlines(), start=1):
                for match in pattern.finditer(line):
                    operator, tier, confidence = lookup[match.group(0)]
                    entry = results[operator]
                    if len(entry.hits) >= MAX_HITS:
                        continue
                    if entry.tier in ("builtin", "runtime"):
                        # CANN 内置算子/HCCL 运行时名：源码里的同名串不是它的实现
                        continue
                    if language == "cpp":
                        confidence = round(confidence - 0.05, 3)
                    if (relative, line_no) in seen:
                        continue
                    seen.add((relative, line_no))
                    entry.hits.append(
                        SourceHit(
                            repo=repo.name,
                            path=relative,
                            line=line_no,
                            language=language,
                            symbol=match.group(0),
                            snippet=line.strip()[:160],
                        )
                    )
                    if _TIER_RANK.get(tier, 9) < _TIER_RANK.get(entry.tier, 9):
                        # 同一算子可能先命中 snake 变体、后命中精确符号：保留更强者
                        entry.tier = tier
                        entry.confidence = confidence
                        entry.reason = None
        for entry in results.values():
            if len(entry.hits) == 1:
                entry.confidence = round(entry.confidence * 0.9, 3)

    def to_dict(self, results: dict[str, OperatorSources]) -> dict:
        mapped = sum(1 for item in results.values() if item.mapped)
        by_tier: dict[str, int] = {}
        for item in results.values():
            by_tier[item.tier] = by_tier.get(item.tier, 0) + 1
        return {
            "schema_version": 1,
            "repos": [str(repo) for repo in self.repos],
            "include_cpp": self.include_cpp,
            "summary": {
                "operators": len(results),
                "mapped": mapped,
                "unmapped": len(results) - mapped,
                "by_tier": by_tier,
            },
            "operators": {op: item.to_dict() for op, item in sorted(results.items())},
        }


_LINE_CACHE: dict[str, list[str]] = {}


def _line_snippet(path: Path, line: int) -> str:
    key = str(path)
    if key not in _LINE_CACHE:
        try:
            _LINE_CACHE[key] = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            _LINE_CACHE[key] = []
    lines = _LINE_CACHE[key]
    if 0 < line <= len(lines):
        return lines[line - 1].strip()[:160]
    return ""


def write_index(payload: dict, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
