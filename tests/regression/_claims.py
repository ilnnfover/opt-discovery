"""扫描「图捕获被当成算子删除/收益」类结论的守卫。

用途：``compare`` 的输出是直接给用户看的 JSON。历史错误是把「Graph 下算子集合
收缩」解释成「算子被删除/可省时间」。这类结论可能藏在任意嵌套字段、键名或
文本里，因此必须递归扫描，而不是只查顶层键或固定几个 delta 字段。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 键名里出现即视为「收益/消除」类结论（英文）
FORBIDDEN_KEY_TOKENS: tuple[str, ...] = (
    "saving",
    "saved_us",
    "eliminat",
    "estimated_gain",
    "gain_us",
    "benefit",
)

#: 文本里出现即视为「收益/消除」类结论（中文）
FORBIDDEN_TEXT_TOKENS: tuple[str, ...] = ("收益", "节省", "消除", "提速")


def find_claims(payload: Any) -> list[str]:
    """返回命中的位置描述；空列表表示没有不该有的结论。"""
    findings: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                key_text = str(key)
                lowered = key_text.lower()
                for token in FORBIDDEN_KEY_TOKENS:
                    if token in lowered:
                        findings.append(f"{path}.{key_text}（键名含 {token!r}）")
                walk(value, f"{path}.{key_text}")
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            for token in FORBIDDEN_TEXT_TOKENS:
                if token in node:
                    findings.append(f"{path}（文本含 {token!r}）")

    walk(payload, "$")
    return findings


def assert_no_claims(payload: Any) -> None:
    """断言输出里没有「消除/节省」型结论；命中时给出具体路径。"""
    findings = find_claims(payload)
    assert not findings, (
        "输出中出现了“算子被消除/可节省时间”类结论，不能作为观测事实："
        + "; ".join(findings)
    )
