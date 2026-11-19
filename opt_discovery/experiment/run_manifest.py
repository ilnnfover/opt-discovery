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
import os
import platform
import subprocess
import sys
import tempfile
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
COMPARISON_DESCRIPTIVE = "descriptive"
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


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record(path: Path, root: Path, label: str | None = None) -> dict[str, Any]:
    # 清单可以放在输出目录之外；用相对路径（允许 ../）保持可校验且不泄露绝对路径
    relative = os.path.relpath(path.resolve(), root.resolve()).replace(os.sep, "/")
    return {
        "name": label or relative,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": sha256_of(path),
    }


def collect_artifacts(entries: Mapping[str, Path], root: Path) -> tuple[dict[str, Any], ...]:
    """``label -> 绝对路径`` → 可写清单（相对 root，含 sha256）。

    登记过的产物若在写清单前消失/不可读，必须失败而不是静默跳过。
    """
    collected: list[dict[str, Any]] = []
    for label, path in entries.items():
        if not path.is_file():
            raise FileNotFoundError(f"已登记的产物不存在: {label} -> {path.name}")
        collected.append(artifact_record(path, root, label))
    return tuple(sorted(collected, key=lambda item: item["path"]))


def write_manifest(path: str | Path, manifest: RunManifest) -> Path:
    """原子写清单：同目录唯一临时文件 + os.replace。

    不能使用固定 ``<target>.tmp``：如果用户把某个输入命名为该路径，会被截断。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False)
    handle, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def read_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_manifest(path: str | Path) -> list[str]:
    """校验清单结构与产物哈希；返回问题列表（空表示通过）。

    结构不合法（空 JSON、数组、缺字段、字段类型错误）同样返回问题，
    不能因为“没有可校验的产物”就当作通过。
    """
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"无法读取清单: {exc}"]

    if not isinstance(payload, dict):
        return [f"清单不是 JSON 对象: {type(payload).__name__}"]

    problems: list[str] = []
    if str(payload.get("schema_version")) != SCHEMA_VERSION:
        problems.append(
            f"schema_version 不是 {SCHEMA_VERSION}: {payload.get('schema_version')!r}"
        )
    if payload.get("status") not in STATUSES:
        problems.append(f"status 不合法: {payload.get('status')!r}")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        problems.append("缺少 run_id")
    if "is_synthetic" in payload and not isinstance(payload["is_synthetic"], bool):
        problems.append("is_synthetic 不是布尔值")
    if "strict" in payload and not isinstance(payload["strict"], bool):
        problems.append("strict 不是布尔值")
    if payload.get("comparison_status") not in {
        COMPARISON_NOT_REQUESTED,
        COMPARISON_SUCCESS,
        COMPARISON_INCOMPARABLE,
        COMPARISON_DESCRIPTIVE,
        COMPARISON_FAILED,
    }:
        problems.append(
            f"comparison_status 不合法: {payload.get('comparison_status')!r}"
        )

    stages = payload.get("stages")
    if not isinstance(stages, list):
        problems.append("stages 不是列表")
    else:
        for index, item in enumerate(stages):
            if not isinstance(item, dict):
                problems.append(f"stages[{index}] 不是对象")
                continue
            if not isinstance(item.get("name"), str) or not item.get("name"):
                problems.append(f"stages[{index}] 缺少 name")
            if not isinstance(item.get("required"), bool):
                problems.append(f"stages[{index}] required 不是布尔值")
            if item.get("status") not in STAGE_STATUSES:
                problems.append(f"stages[{index}] status 不合法: {item.get('status')!r}")

    workloads = payload.get("workloads")
    if workloads is not None:
        if not isinstance(workloads, list):
            problems.append("workloads 不是列表")
        else:
            for index, item in enumerate(workloads):
                if not isinstance(item, dict):
                    problems.append(f"workloads[{index}] 不是对象")
                    continue
                if not isinstance(item.get("workload_id"), str) or not item.get(
                    "workload_id"
                ):
                    problems.append(f"workloads[{index}] 缺少 workload_id")
                if not isinstance(item.get("status"), str):
                    problems.append(f"workloads[{index}] 缺少 status")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        problems.append("artifacts 不是列表")
        return problems

    for item in artifacts:
        if not isinstance(item, dict):
            problems.append(f"artifact 项不是对象: {type(item).__name__}")
            continue
        relative = item.get("path")
        if not isinstance(relative, str) or not relative:
            problems.append("artifact 缺少 path（或不是字符串）")
            continue
        if Path(relative).is_absolute():
            problems.append(f"artifact path 不能是绝对路径: {relative}")
            continue
        expected = item.get("sha256")
        if not isinstance(expected, str) or not expected:
            problems.append(f"artifact 缺少 sha256: {relative}")
            continue
        if "bytes" in item and not isinstance(item["bytes"], int):
            problems.append(f"artifact bytes 不是整数: {relative}")
            continue
        target = manifest_path.parent / relative
        if not target.is_file():
            problems.append(f"缺少产物: {relative}")
            continue
        try:
            actual = sha256_of(target)
        except OSError as exc:
            problems.append(f"无法读取产物 {relative}: {exc}")
            continue
        if actual != expected:
            problems.append(f"产物被修改: {relative}")

    # success 必须真的有产物，且所有必需阶段为 passed
    if payload.get("status") == STATUS_SUCCESS:
        if not artifacts:
            problems.append("success 清单没有任何产物")
        if isinstance(stages, list):
            for item in stages:
                if (
                    isinstance(item, dict)
                    and item.get("required") is True
                    and item.get("status") != STAGE_PASSED
                ):
                    problems.append(
                        f"success 清单包含未通过的必需阶段: {item.get('name')}"
                    )
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
    """配置指纹：只记文件名 + sha256，不写绝对路径；缺失/不可读即失败。"""
    items: list[dict[str, Any]] = []
    for path in paths:
        if not path:
            continue
        if not Path(path).is_file():
            raise FileNotFoundError(f"配置不存在或不可读: {Path(path).name}")
        items.append({"name": Path(path).name, "sha256": sha256_of(path)})
    return items


def artifact_allowlist(manifest_path: str | Path) -> set[str] | None:
    """读取清单登记的相对路径；没有清单时返回 None（legacy 行为）。"""
    target = Path(manifest_path)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    return {
        item["path"]
        for item in artifacts
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }


def manifest_workload_ids(manifest_path: str | Path) -> set[str]:
    target = Path(manifest_path)
    if not target.is_file():
        return set()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {
        item["workload_id"]
        for item in payload.get("workloads") or ()
        if isinstance(item, dict) and isinstance(item.get("workload_id"), str)
    }


def refresh_manifest_artifacts(
    manifest_path: str | Path, entries: Mapping[str, Path]
) -> None:
    """重建某个已登记产物后同步其 sha256/bytes，保持 check-manifest 一致。"""
    target = Path(manifest_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    root = target.parent
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return
    by_path = {
        item.get("path"): item
        for item in artifacts
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    changed = False
    for label, file in entries.items():
        relative = os.path.relpath(file.resolve(), root.resolve()).replace(os.sep, "/")
        item = by_path.get(relative)
        if item is None:
            continue
        item["bytes"] = file.stat().st_size
        item["sha256"] = sha256_of(file)
        item["name"] = item.get("name") or label
        changed = True
    if not changed:
        return
    handle, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "COMPARISON_FAILED",
    "COMPARISON_DESCRIPTIVE",
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
    "artifact_allowlist",
    "collect_artifacts",
    "fingerprint_configs",
    "manifest_workload_ids",
    "refresh_manifest_artifacts",
    "new_run_id",
    "read_manifest",
    "sha256_of",
    "tool_info",
    "verify_manifest",
    "write_manifest",
]
