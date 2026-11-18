"""运行清单（AE-03）。

目的：让自动化调用者能区分「程序退出 0」「JSON 存在」「分析真的完整成功」。

* 状态取值：``success`` / ``partial`` / ``incomparable`` / ``failed``；
* 阶段状态：``passed`` / ``failed`` / ``skipped`` / ``blocked`` / ``not_requested``；
* 每次运行有独立 ``run_id``，产物记录相对路径 + sha256，便于识别旧输出残留；
* 不写入绝对路径、环境变量值或秘密（数据根目录只记逻辑标签）。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import __version__ as PACKAGE_VERSION

SCHEMA_VERSION = "1.0"

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_INCOMPARABLE = "incomparable"
STATUS_FAILED = "failed"
STATUSES = (STATUS_SUCCESS, STATUS_PARTIAL, STATUS_INCOMPARABLE, STATUS_FAILED)

STAGE_PASSED = "passed"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"
STAGE_BLOCKED = "blocked"
STAGE_NOT_REQUESTED = "not_requested"
STAGE_STATUSES = (
    STAGE_PASSED,
    STAGE_FAILED,
    STAGE_SKIPPED,
    STAGE_BLOCKED,
    STAGE_NOT_REQUESTED,
)

COMPARISON_NOT_REQUESTED = "not_requested"
COMPARISON_SUCCESS = "success"
COMPARISON_INCOMPARABLE = "incomparable"
COMPARISON_FAILED = "failed"

#: strict 模式退出码（与 README/CLI 文档一致）
EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_PARTIAL = 3
EXIT_INCOMPARABLE = 4

STATUS_EXIT = {
    STATUS_SUCCESS: EXIT_SUCCESS,
    STATUS_FAILED: EXIT_FAILED,
    STATUS_PARTIAL: EXIT_PARTIAL,
    STATUS_INCOMPARABLE: EXIT_INCOMPARABLE,
}


@dataclass(frozen=True)
class StageRecord:
    name: str
    required: bool
    status: str
    reason_code: str | None = None
    artifacts: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STAGE_STATUSES:
            raise ValueError(f"unknown stage status {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "artifacts": list(self.artifacts),
        }
        if self.reason_code:
            payload["reason_code"] = self.reason_code
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class WorkloadRecord:
    workload_id: str
    status: str
    reason_code: str | None = None
    artifacts: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workload_id": self.workload_id,
            "status": self.status,
            "artifacts": list(self.artifacts),
            "quality_flags": list(self.quality_flags),
        }
        if self.reason_code:
            payload["reason_code"] = self.reason_code
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    status: str
    strict: bool
    is_synthetic: bool
    stages: tuple[StageRecord, ...]
    workloads: tuple[WorkloadRecord, ...] = ()
    quality_flags: tuple[str, ...] = ()
    comparison_status: str = COMPARISON_NOT_REQUESTED
    artifacts: tuple[Mapping[str, Any], ...] = ()
    inputs: Mapping[str, Any] = field(default_factory=dict)
    tool: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    previous_run_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown run status {self.status!r}")
        if not self.created_at:
            object.__setattr__(
                self, "created_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )

    @property
    def exit_code(self) -> int:
        return STATUS_EXIT[self.status]

    def stage(self, name: str) -> StageRecord | None:
        for item in self.stages:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "is_synthetic": self.is_synthetic,
            "strict": self.strict,
            "created_at": self.created_at,
            "stages": [stage.to_dict() for stage in self.stages],
            "workloads": [item.to_dict() for item in self.workloads],
            "quality_flags": list(self.quality_flags),
            "comparison_status": self.comparison_status,
            "artifacts": [dict(item) for item in self.artifacts],
            "inputs": dict(self.inputs),
            "tool": dict(self.tool),
        }
        if self.previous_run_id:
            payload["previous_run_id"] = self.previous_run_id
        return payload


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record(path: Path, root: Path, label: str | None = None) -> dict[str, Any]:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return {
        "name": label or relative,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": sha256_of(path),
    }


def collect_artifacts(entries: Mapping[str, Path], root: Path) -> tuple[dict[str, Any], ...]:
    """``label -> 绝对路径`` → 可写清单（相对 root，含 sha256）。"""
    collected: list[dict[str, Any]] = []
    for label, path in entries.items():
        if not path.is_file():
            continue
        collected.append(artifact_record(path, root, label))
    return tuple(sorted(collected, key=lambda item: item["path"]))


def write_manifest(path: str | Path, manifest: RunManifest) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(target)
    return target


def read_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_manifest(path: str | Path) -> list[str]:
    """校验清单里的产物仍然存在且哈希一致；返回问题列表（空表示通过）。"""
    manifest_path = Path(path)
    payload = read_manifest(manifest_path)
    root = manifest_path.parent
    problems: list[str] = []
    for item in payload.get("artifacts") or ():
        relative = item.get("path")
        if not relative:
            problems.append("artifact 缺少 path")
            continue
        target = root / relative
        if not target.is_file():
            problems.append(f"缺少产物: {relative}")
            continue
        expected = item.get("sha256")
        if expected and sha256_of(target) != expected:
            problems.append(f"产物被修改: {relative}")
    return problems


def tool_info(repo_root: Path | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": "opt-discovery",
        "version": PACKAGE_VERSION,
        "python": sys.version.split()[0],
        "platform": platform.system().lower(),
    }
    root = repo_root or Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return info
    commit = result.stdout.strip()
    if result.returncode == 0 and commit:
        info["git_commit"] = commit
    return info


def fingerprint_configs(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """配置指纹：只记文件名 + sha256，不写绝对路径。"""
    items: list[dict[str, Any]] = []
    for path in paths:
        if path and path.is_file():
            items.append({"name": path.name, "sha256": sha256_of(path)})
    return items


__all__ = [
    "COMPARISON_FAILED",
    "COMPARISON_INCOMPARABLE",
    "COMPARISON_NOT_REQUESTED",
    "COMPARISON_SUCCESS",
    "EXIT_FAILED",
    "EXIT_INCOMPARABLE",
    "EXIT_INVALID",
    "EXIT_PARTIAL",
    "EXIT_SUCCESS",
    "RunManifest",
    "SCHEMA_VERSION",
    "STAGE_BLOCKED",
    "STAGE_FAILED",
    "STAGE_NOT_REQUESTED",
    "STAGE_PASSED",
    "STAGE_SKIPPED",
    "STATUSES",
    "STATUS_EXIT",
    "STATUS_FAILED",
    "STATUS_INCOMPARABLE",
    "STATUS_PARTIAL",
    "STATUS_SUCCESS",
    "StageRecord",
    "WorkloadRecord",
    "collect_artifacts",
    "fingerprint_configs",
    "new_run_id",
    "read_manifest",
    "sha256_of",
    "tool_info",
    "verify_manifest",
    "write_manifest",
]
