"""审计一次统一补采是否真的满足契约（参数一致性）。

输入：`recollect_contract.py` 产出的 `collection_summary.json` 与各组
`collection_manifest.json`。输出逐项 PASS/FAIL，避免"看起来一致"。

判定：
* 所有组必须用同一份契约（sha256 相同）；
* `fixed` 与 `profiling` 必须逐项相同（dict 全等）；
* 组间差异只允许出现在 `variables` 的三个自变量里；
* 每组自身的 `checks.ok`（两 rank CSV、轮数）必须为 True。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONTRACT = (
    Path(__file__).resolve().parents[2] / "configs" / "collection_contract.yaml"
)

__all__ = ["AuditItem", "CollectionAudit", "audit_collection", "load_collection_runs"]

ALLOWED_VARIABLES = {"mode", "graph_mode", "output_length"}


@dataclass(frozen=True)
class AuditItem:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class CollectionAudit:
    root: str
    verdict: str
    items: tuple[AuditItem, ...]
    runs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "verdict": self.verdict,
            "runs": list(self.runs),
            "items": [item.to_dict() for item in self.items],
        }


def load_collection_runs(root: Path) -> list[tuple[Path, dict]]:
    """Select the latest directory per run ID for both audit and acceptance."""
    direct = root / "collection_manifest.json"
    paths = [direct] if direct.is_file() else sorted(root.glob("*/collection_manifest.json"))
    latest: dict[str, tuple[Path, dict]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        key = str(payload.get("run_id") or path.parent.name)
        latest[key] = (path.parent, payload)
    return [latest[key] for key in sorted(latest)]


def audit_collection(
    root: str | Path, contract_path: str | Path | None = None
) -> CollectionAudit:
    path = Path(root)
    manifests = [payload for _, payload in load_collection_runs(path)]
    items: list[AuditItem] = []
    if not manifests:
        return CollectionAudit(
            root=str(path), verdict="no_data",
            items=(AuditItem("manifest", False, "没有找到 collection_manifest.json"),),
        )

    ids = [item["run_id"] for item in manifests]
    contract_file = Path(contract_path) if contract_path else DEFAULT_CONTRACT
    if contract_file.is_file():
        expected_ids = {
            str(run["id"]) for run in yaml.safe_load(contract_file.read_text(encoding="utf-8"))["runs"]
        }
        missing = sorted(expected_ids - set(ids))
        items.append(
            AuditItem(
                "runs_complete",
                not missing,
                f"契约 {len(expected_ids)} 组，已采 {len(set(ids))} 组；缺 {missing or '无'}",
            )
        )

    # 缺字段时不能静默：既避免 sorted({None, str}) 的 TypeError，也让缺失成为可见失败
    hashes = sorted(
        str(item["contract_sha256"]) if item.get("contract_sha256") else "<missing>"
        for item in manifests
    )
    items.append(
        AuditItem("contract_sha256", len(set(hashes)) == 1, f"契约 sha256 = {hashes}")
    )

    for key in ("fixed", "profiling"):
        values = [json.dumps(item.get(key), sort_keys=True, ensure_ascii=False) for item in manifests]
        items.append(
            AuditItem(key, len(set(values)) == 1, f"{len(set(values))} 种取值")
        )

    variables = {item["run_id"]: item.get("variables") or {} for item in manifests}
    unexpected = sorted(
        {k for value in variables.values() for k in value} - ALLOWED_VARIABLES
    )
    items.append(
        AuditItem(
            "variables",
            not unexpected,
            f"允许的自变量={sorted(ALLOWED_VARIABLES)}；越界={unexpected or '无'}",
        )
    )

    checks = {
        item["run_id"]: item.get("exit_code", 0) == 0
        and (item.get("checks") or {}).get("ok") is True
        for item in manifests
    }
    items.append(
        AuditItem(
            "per_run_checks",
            all(checks.values()),
            ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in checks.items()),
        )
    )

    failures = {item.name for item in items if not item.ok}
    if not failures:
        verdict = "pass"
    elif failures == {"runs_complete"}:
        # 已采的组参数一致，只是采样被暂停/尚未补齐 —— 与"参数不一致"区分开
        verdict = "incomplete"
    else:
        verdict = "fail"
    return CollectionAudit(root=str(path), verdict=verdict, items=tuple(items), runs=tuple(ids))
