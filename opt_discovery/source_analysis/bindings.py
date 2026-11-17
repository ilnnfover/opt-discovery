"""解析 ``csrc/torch_binding.cpp`` 的算子注册表（P1-1 增强）。

注册表形态：

    #include "moe/moe_gating_top_k/moe_gating_top_k_torch_adpt.h"
    ops.def("moe_gating_top_k(Tensor x, ...) -> ...");
    ops.impl("moe_gating_top_k", torch::kPrivateUse1, &vllm_ascend::moe_gating_top_k);

所以「算子名 -> 实现文件」分三步：

1. 抓 ``ops.def`` / ``ops.impl``（impl 可能跨行）；
2. 在 ``csrc`` 里找该函数符号的**定义行**（排除以 ``;`` 结尾的声明）；
3. 兜底用 schema 名匹配 ``#include`` 路径。

实测（vllm-ascend-hust）：77 条 impl、72 个唯一函数，72/72 可定位。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = ["BindingEntry", "BindingTable", "parse_binding_table", "find_binding_file", "write_bindings"]

_DEF_RE = re.compile(r'\.def\(\s*"([A-Za-z0-9_]+)\(')
_IMPL_RE = re.compile(
    r'\.impl\(\s*"([A-Za-z0-9_]+)"\s*,\s*[^,]+,\s*&([A-Za-z0-9_:]+)\s*\)', re.S
)
_INCLUDE_RE = re.compile(r'#include\s+"([^"]+)"')
_SOURCE_SUFFIXES = {".cpp", ".cc", ".h", ".hpp"}


@dataclass(frozen=True)
class BindingEntry:
    schema: str
    func: str
    def_line: int
    impl_line: int | None = None
    impl_file: str | None = None
    include: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "func": self.func,
            "def_line": self.def_line,
            "impl_line": self.impl_line,
            "impl_file": self.impl_file,
            "include": self.include,
        }


@dataclass
class BindingTable:
    source: str
    repo: str
    entries: tuple[BindingEntry, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def resolve(self, operator: str) -> list[tuple[str, BindingEntry]]:
        snake = _snake(operator)
        matches: list[tuple[str, BindingEntry]] = []
        for entry in self.entries:
            schema = entry.schema
            if schema == snake:
                matches.append(("exact", entry))
            elif schema in (f"npu_{snake}", f"aclnn_{snake}"):
                matches.append(("prefixed", entry))
            elif schema.endswith(f"_{snake}"):
                matches.append(("suffix", entry))
            elif schema.startswith(snake) or schema.startswith(f"npu_{snake}"):
                # 例：ChunkGatedDeltaRule -> chunk_gated_delta_rule_fwd_h
                matches.append(("stem", entry))
        order = {"exact": 0, "prefixed": 1, "suffix": 2, "stem": 3}
        matches.sort(key=lambda item: (order[item[0]], len(item[1].schema)))
        seen: set[str] = set()
        unique: list[tuple[str, BindingEntry]] = []
        for kind, entry in matches:
            if entry.schema in seen:
                continue
            seen.add(entry.schema)
            unique.append((kind, entry))
        return unique

    @property
    def resolved_ratio(self) -> float:
        if not self.entries:
            return 0.0
        return sum(1 for item in self.entries if item.impl_file) / len(self.entries)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "repo": self.repo,
            "entry_count": len(self.entries),
            "resolved": sum(1 for item in self.entries if item.impl_file),
            "resolved_ratio": round(self.resolved_ratio, 3),
            "notes": list(self.notes),
            "entries": [item.to_dict() for item in self.entries],
        }


def _snake(name: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    text = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", text)
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()


def find_binding_file(repos: Iterable[str | Path]) -> Path | None:
    for repo in repos:
        root = Path(repo)
        direct = root / "csrc" / "torch_binding.cpp"
        if direct.is_file():
            return direct
        matches = sorted(root.glob("**/torch_binding.cpp"))
        if matches:
            return matches[0]
    return None


def parse_binding_table(torch_binding: str | Path) -> BindingTable:
    path = Path(torch_binding)
    if not path.is_file():
        raise FileNotFoundError(f"{path}: 找不到 torch_binding.cpp")
    repo_root = path.parent.parent
    text = path.read_text(encoding="utf-8", errors="ignore")

    def_lines: dict[str, int] = {}
    for match in _DEF_RE.finditer(text):
        def_lines.setdefault(match.group(1), text[: match.start()].count("\n") + 1)
    impls = [(m.group(1), m.group(2).split("::")[-1]) for m in _IMPL_RE.finditer(text)]
    includes = [m.group(1) for m in _INCLUDE_RE.finditer(text)]
    if not impls:
        return BindingTable(
            source=str(path), repo=str(repo_root),
            notes=("未解析到 ops.impl 注册项：注册表格式可能不同",),
        )

    funcs = {func for _, func in impls}
    definitions = _locate_definitions(repo_root / "csrc", funcs, includes)
    entries = [
        BindingEntry(
            schema=schema,
            func=func,
            def_line=def_lines.get(schema, 0),
            impl_line=definitions.get(func, (None, None))[1],
            impl_file=definitions.get(func, (None, None))[0],
            include=_match_include(schema, includes),
        )
        for schema, func in impls
    ]
    notes = [
        f"注册项 {len(entries)} 条，函数 {len(funcs)} 个",
        f"定位到实现 {sum(1 for item in entries if item.impl_file)} 条",
    ]
    unresolved = [item.schema for item in entries if not item.impl_file]
    if unresolved:
        notes.append("未定位实现: " + ", ".join(unresolved[:5]))
    return BindingTable(source=str(path), repo=str(repo_root), entries=tuple(entries), notes=tuple(notes))


def _locate_definitions(csrc: Path, funcs: set[str], includes: list[str]):
    result: dict[str, tuple[str | None, int | None]] = {}
    remaining = set(funcs)

    def scan(files: Iterable[Path]) -> None:
        if not remaining:
            return
        alternation = "|".join(sorted(map(re.escape, remaining), key=len, reverse=True))
        pattern = re.compile(r"^\s*[\w:<>,*\s&]+\b(" + alternation + r")\s*\(", re.M)
        for file in files:
            if not file.is_file() or file.suffix not in _SOURCE_SUFFIXES:
                continue
            try:
                content = file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in pattern.finditer(content):
                func = match.group(1)
                if func not in remaining:
                    continue
                line_end = content.find("\n", match.end())
                tail = content[match.end(): line_end if line_end > 0 else len(content)]
                if tail.rstrip().endswith(";"):
                    continue
                line = content[: match.start()].count("\n") + 1
                result[func] = (file.relative_to(csrc.parent).as_posix(), line)
                remaining.discard(func)

    scan(csrc.parent / include for include in includes if (csrc.parent / include).is_file())
    if remaining:
        scan(csrc.rglob("*"))
    for func in remaining:
        result[func] = (None, None)
    return result


def _match_include(schema: str, includes: list[str]) -> str | None:
    for include in includes:
        if schema in include:
            return include
    stem = schema[4:] if schema.startswith("npu_") else schema
    for include in includes:
        if stem in include:
            return include
    return None


def write_bindings(table: BindingTable, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(table.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return target
