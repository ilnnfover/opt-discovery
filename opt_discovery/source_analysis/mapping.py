"""把算子映射到源码位置，并回填到 Opportunity（P1-1）。

只做两件事：
1. ``build_source_map``：给定算子集合与仓库，产出可落盘的索引；
2. ``attach_to_opportunities``：把命中位置写进 ``Opportunity.source_refs``，
   并在有命中时移除"尚未建立 Kernel -> 源码映射"这条 caveat。

置信度、命中层级、未命中原因都保留在索引里，不在这里二次加工。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Sequence

from ..schemas.opportunity import Opportunity

__all__ = [
    "DEFAULT_REPOS",
    "build_source_map",
    "load_source_map",
    "attach_to_opportunities",
    "summarize",
]

#: 源码索引的候选仓库：优先取环境变量 ``OPT_DISCOVERY_REPOS``（冒号分隔），
#: 未设置时用当前目录下的相对路径，避免硬编码机器路径。
DEFAULT_REPOS = tuple(
    item for item in os.environ.get("OPT_DISCOVERY_REPOS", "").split(os.pathsep) if item
) or ("vllm-ascend", "vllm")


def build_source_map(
    operators: Iterable[str],
    repos: Sequence[str] = DEFAULT_REPOS,
    *,
    include_cpp: bool = True,
    binding_path: str | None = None,
    use_bindings: bool = True,
) -> dict:
    from .bindings import find_binding_file, parse_binding_table
    from .indexer import SourceIndex

    bindings = None
    binding_summary = None
    if use_bindings:
        path = Path(binding_path) if binding_path else find_binding_file(repos)
        if path and path.is_file():
            bindings = parse_binding_table(path)
            binding_summary = {
                "source": bindings.source,
                "entry_count": len(bindings.entries),
                "resolved": sum(1 for item in bindings.entries if item.impl_file),
                "notes": list(bindings.notes),
            }

    index = SourceIndex(repos, include_cpp=include_cpp, bindings=bindings)
    results = index.scan(operators)
    payload = index.to_dict(results)
    payload["bindings"] = binding_summary
    payload["provenance"] = {
        "source": "editable install（vllm / vllm-ascend 源码树）",
        "resolved_runtime_paths": [
            "$OPT_DISCOVERY_REPOS 中的 vllm-ascend 源码树",
            "$OPT_DISCOVERY_REPOS 中的 vllm 源码树",
        ],
        "note": "索引的是同版本干净快照；运行时会解析到 git/*-partial",
    }
    return payload


def load_source_map(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def attach_to_opportunities(
    opportunities: Iterable[Opportunity], payload: dict, *, limit: int = 3
) -> int:
    """写入 source_refs；返回成功回填的机会数。"""
    operators = payload.get("operators") or {}
    attached = 0
    for opportunity in opportunities:
        entry = operators.get(opportunity.operator)
        if not entry or not entry.get("mapped"):
            continue
        refs = tuple(
            f"{hit['repo']}:{hit['path']}:{hit['line']}"
            for hit in entry.get("hits", [])[:limit]
        )
        if not refs:
            continue
        opportunity.source_refs = refs
        opportunity.caveats = tuple(
            caveat
            for caveat in opportunity.caveats
            if "尚未建立 Kernel" not in caveat
        )
        attached += 1
    return attached


def summarize(payload: dict) -> str:
    summary = payload.get("summary") or {}
    tiers = summary.get("by_tier") or {}
    parts = ", ".join(f"{name}={count}" for name, count in sorted(tiers.items()))
    return (
        f"算子 {summary.get('operators', 0)} 个：命中 {summary.get('mapped', 0)}，"
        f"未命中 {summary.get('unmapped', 0)}（{parts}）"
    )
