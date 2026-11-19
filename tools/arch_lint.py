"""AE-05 architecture and domain lint.

Lightweight AST/YAML checks for the boundaries agreed in the task book.  The
checker is deliberately narrow: it does not ban legitimate ``or 0``, ``except``,
CSV imports or numeric literals globally.  Each rule has a specific shape and
file scope, and every diagnostic carries the rule id, location and the public
API or boundary to use instead.

Usage:
    python -m tools.arch_lint --config configs/quality/arch_lint.yaml
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

import yaml

__all__ = ["Violation", "run_lint", "main"]

_ZERO = (0, 0.0)
_UNIT_CONSTANTS = (1000, 1000.0, 1_000, 1e3)
_RAW_SOURCE_TOKENS = (
    "op_summary",
    "op_statistic",
    "mindstudio_profiler_output",
    "msprof_",
)
_MACHINE_PATH = re.compile(r"^(/root/|/home/|/tmp/|[A-Za-z]:[\\/])")
_THRESHOLD_PROVENANCE = {"assumed", "declared", "derived", "measured"}


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str
    hint: str

    def format(self) -> str:
        return f"{self.rule} {self.path}:{self.line}: {self.message} [hint: {self.hint}]"


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _module_name(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _inside_fstring(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FormattedValue, ast.JoinedStr)):
            return True
        current = parents.get(current)
    return False


def _matches(module: str, forbidden: str) -> bool:
    return module == forbidden or module.startswith(forbidden + ".")


def _absolute_imports(node: ast.stmt, module: str) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
        return
    if not isinstance(node, ast.ImportFrom):
        return
    if node.level == 0:
        base = node.module or ""
    else:
        package = module.split(".")[:-1]
        up = node.level - 1
        if up:
            package = package[:-up]
        base = ".".join(package + ([node.module] if node.module else []))
    if base:
        yield base
    for alias in node.names:
        candidate = f"{base}.{alias.name}" if base else alias.name
        yield candidate


def _zero(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and isinstance(node.value, (int, float))
        and node.value == 0
    )


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _check_imports(
    tree: ast.AST,
    path: Path,
    root: Path,
    rel: str,
    rules: Sequence[Mapping[str, object]],
) -> list[Violation]:
    violations: list[Violation] = []
    module = _module_name(path, root)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        reported: set[str] = set()
        for target in _absolute_imports(node, module):
            for rule in rules:
                roots = [str(item) for item in rule.get("roots", ())]
                if not any(rel == item or rel.startswith(item + "/") for item in roots):
                    continue
                forbidden = [str(item) for item in rule.get("forbid", ())]
                if not any(_matches(target, item) for item in forbidden):
                    continue
                rule_name = f"ARCH:{rule.get('name', 'rule')}"
                if rule_name in reported:
                    continue
                reported.add(rule_name)
                violations.append(
                    Violation(
                        rule_name,
                        rel,
                        node.lineno,
                        f"{rel} 不允许导入 {target}",
                        "移入 CLI/适配器，或通过 schemas 的公共 API 传递数据",
                    )
                )
    return violations


def _check_od001(tree: ast.AST, rel: str) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "Metric":
            continue
        value = None
        if node.args:
            value = node.args[0]
        else:
            for keyword in node.keywords:
                if keyword.arg == "value":
                    value = keyword.value
        suspicious = False
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            suspicious = any(_zero(item) for item in value.values)
        elif isinstance(value, ast.IfExp):
            test = value.test
            compares_none = (
                isinstance(test, ast.Compare)
                and any(isinstance(op, (ast.Is, ast.IsNot)) for op in test.ops)
                and any(
                    isinstance(item, ast.Constant) and item.value is None
                    for item in test.comparators
                )
            )
            suspicious = compares_none and (_zero(value.body) or _zero(value.orelse))
        if suspicious:
            violations.append(
                Violation(
                    "OD001",
                    rel,
                    node.lineno,
                    "Metric 构造把缺失值静默替换为 0",
                    "保留 None 并用 MissingEvidence / Provenance 表达，或显式标记 default",
                )
            )
    return violations


def _check_od002(
    tree: ast.AST, rel: str, authorized: Sequence[str]
) -> list[Violation]:
    if rel in authorized:
        return []
    parents = _parents(tree)
    time_tokens = {"us", "ms", "ns", "time", "duration", "latency", "busy", "gap"}
    violations: list[Violation] = []

    def _tokens(node: ast.AST) -> set[str]:
        text = ""
        if isinstance(node, ast.Name):
            text = node.id
        elif isinstance(node, ast.Attribute):
            text = node.attr
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            text = str(node.slice.value)
        return set(re.split(r"[^a-z0-9]+", text.lower())) - {""}

    def _time_like(node: ast.AST) -> bool:
        tokens = _tokens(node)
        return bool(tokens & time_tokens) or any(
            token.endswith(("_us", "_ms", "_ns")) for token in tokens
        )

    def _assignment_context(node: ast.AST) -> str:
        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, ast.Assign):
                target = parent.targets[0]
                if isinstance(target, ast.Name):
                    return target.id.lower()
                if isinstance(target, ast.Attribute):
                    return target.attr.lower()
            current = parent
        return ""

    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, (ast.Mult, ast.Div)):
            continue
        constant = None
        for item in (node.left, node.right):
            if isinstance(item, ast.Constant) and isinstance(item.value, (int, float)):
                value = float(item.value)
                if value in (1000.0, 1e3) or abs(value - 0.001) < 1e-12:
                    constant = value
                    break
        if constant is None:
            continue
        context_tokens = set(re.split(r"[^a-z0-9]+", _assignment_context(node))) - {""}
        time_context = bool(context_tokens & time_tokens) or any(
            token.endswith(("_us", "_ms", "_ns")) for token in context_tokens
        )
        operand_time = _time_like(node.left) or _time_like(node.right)
        if not (time_context or operand_time):
            continue
        if _inside_fstring(node, parents):
            continue
        violations.append(
            Violation(
                "OD002",
                rel,
                node.lineno,
                "直接做 1000 倍/0.001 倍单位换算",
                "使用 events/primitives 的 ns_from_us 或 schemas.units.to_us",
            )
        )
    return violations


def _check_od003(tree: ast.AST, rel: str) -> list[Violation]:
    restricted = (
        rel.startswith("opt_discovery/diagnosis/")
        or rel.startswith("opt_discovery/discovery/")
        or rel.startswith("opt_discovery/report/")
        or rel == "opt_discovery/cli.py"
    )
    if not restricted:
        return []
    violations: list[Violation] = []
    raw_tokens = (
        "op_summary",
        "op_statistic",
        "op_counter.db",
        "op_report",
        "msprof_",
        "mindstudio_profiler_output",
    )

    def _raw_in_subtree(node: ast.AST) -> bool:
        for item in ast.walk(node):
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                text = item.value.lower()
                if any(token in text for token in raw_tokens):
                    return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            targets = list(_absolute_imports(node, _module_name(Path(rel), Path("."))))
            if any(item == "sqlite3" or item.startswith("sqlite3.") for item in targets):
                violations.append(
                    Violation(
                        "OD003",
                        rel,
                        node.lineno,
                        "下游/编排模块直接导入 sqlite3",
                        "通过 parsers/sqlite_op_report 或 events 的公共 Reader 读取",
                    )
                )
            if not rel.startswith("opt_discovery/report/") and any(
                item == "csv" or item.startswith("csv.") for item in targets
            ):
                violations.append(
                    Violation(
                        "OD003",
                        rel,
                        node.lineno,
                        "下游模块直接导入 csv 解析原始数据",
                        "通过 parsers/csv_mapping 或 events 的公共 Reader 读取",
                    )
                )
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "connect":
                receiver = func.value
                receiver_name = ""
                if isinstance(receiver, ast.Name):
                    receiver_name = receiver.id
                elif isinstance(receiver, ast.Attribute):
                    receiver_name = receiver.attr
                if "sqlite" in receiver_name.lower():
                    violations.append(
                        Violation(
                            "OD003",
                            rel,
                            node.lineno,
                            "直接连接 sqlite 数据库",
                            "通过 parsers/sqlite_op_report 或 events 的公共 Reader 读取",
                        )
                    )
                    continue
            reads = (
                isinstance(func, ast.Name)
                and func.id == "open"
                or isinstance(func, ast.Attribute)
                and func.attr in {"read_text", "read_bytes", "read_csv", "open"}
            )
            if reads:
                subject = node.func.value if isinstance(func, ast.Attribute) else None
                if _raw_in_subtree(node) or (
                    subject is not None and _raw_in_subtree(subject)
                ):
                    violations.append(
                        Violation(
                            "OD003",
                            rel,
                            node.lineno,
                            "受限制模块直接读取原始 Profiler 路径",
                            "通过 parsers/csv_mapping、parsers/sqlite_op_report 或 events 的公共 Reader 读取",
                        )
                    )
                    continue
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value.lower()
        if re.search(r"\b(from|into|update|join)\s+task\b", text):
            violations.append(
                Violation(
                    "OD003",
                    rel,
                    node.lineno,
                    "直接查询 Profiler TASK 表",
                    "通过 parsers/sqlite_op_report 或 events 的公共 Reader 读取",
                )
            )
    return violations


def _check_od004(
    tree: ast.AST, rel: str, allowed: Mapping[tuple[str, int], str]
) -> list[Violation]:
    if rel == "opt_discovery/schemas/quality.py":
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr in {
                "severity",
                "verdict",
                "evidence_level",
            }:
                if target.attr == "evidence_level" and (rel, node.lineno) in allowed:
                    continue
                violations.append(
                    Violation(
                        "OD004",
                        rel,
                        node.lineno,
                        f"直接改写证据字段 {target.attr}",
                        "通过 schemas.quality.attach / QualityFlag 的公共构造函数升级证据",
                    )
                )
            if isinstance(target, ast.Subscript):
                key = target.slice
                if isinstance(key, ast.Constant) and key.value in {
                    "quality_flags",
                    "evidence",
                }:
                    violations.append(
                        Violation(
                            "OD004",
                            rel,
                            node.lineno,
                            f"直接改写 {key.value!r} 容器",
                            "通过 schemas.quality.attach 合并，禁止绕过证据升级 API",
                        )
                    )
    return violations


def _check_od005(
    tree: ast.AST, rel: str, allowed: Mapping[tuple[str, int], str]
) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        broad = node.type is None or (
            isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
        )
        if not broad:
            continue
        if (rel, node.lineno) in allowed:
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            violations.append(
                Violation(
                    "OD005",
                    rel,
                    node.lineno,
                    "宽泛异常被静默吞没（except + pass）",
                    "记录错误/质量标记或缩小异常类型；确需吞没请登记 od005_allow 并说明理由",
                )
            )
    return violations


def _check_od006(tree: ast.AST, rel: str) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if _MACHINE_PATH.match(node.value):
            violations.append(
                Violation(
                    "OD006",
                    rel,
                    node.lineno,
                    f"硬编码机器路径 {node.value!r}",
                    "通过 CLI 参数或 PROF_ROOT/环境变量传入路径",
                )
            )
    return violations


def _iter_threshold_items(data: object) -> Iterator[tuple[str, Mapping[str, object]]]:
    if not isinstance(data, Mapping):
        return
    thresholds = data.get("thresholds")
    if not isinstance(thresholds, Mapping):
        return
    for key, value in thresholds.items():
        if isinstance(value, Mapping) and "value" in value:
            yield str(key), value


def _check_od007(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    files: list[Path] = []
    for pattern in ("configs/rules/*.yaml", "configs/thresholds/*.yaml"):
        files.extend(sorted(root.glob(pattern)))
    for path in files:
        if ".bak" in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            violations.append(
                Violation(
                    "OD007",
                    _relative(path, root),
                    1,
                    f"阈值配置无法解析: {exc}",
                    "修复 YAML；阈值必须包含 provenance 与 basis/source",
                )
            )
            continue
        lines = text.splitlines()
        for key, item in _iter_threshold_items(data):
            provenance = str(item.get("provenance", "")).strip().lower()
            has_basis = bool(str(item.get("basis") or item.get("source") or "").strip())
            if provenance in _THRESHOLD_PROVENANCE and has_basis:
                continue
            line = next(
                (
                    index
                    for index, raw in enumerate(lines, start=1)
                    if re.match(
                        rf"^\s*[\"']?{re.escape(str(key))}[\"']?\s*:", raw
                    )
                ),
                1,
            )
            violations.append(
                Violation(
                    "OD007",
                    _relative(path, root),
                    line,
                    f"阈值 {key!r} 缺少登记的 provenance/basis",
                    "补充 provenance(assumed/declared/derived/measured) 与 basis/source",
                )
            )
    return violations


def run_lint(root: str | Path, config_path: str | Path) -> list[Violation]:
    root_path = Path(root).resolve()
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    arch_rules = config.get("arch_rules") or []
    authorized = [str(item) for item in config.get("od002_authorized_files") or []]
    od005_allowed = {
        (str(item["file"]), int(item["line"])): str(item.get("reason", ""))
        for item in config.get("od005_allow") or []
        if isinstance(item, Mapping) and item.get("file") and item.get("line")
    }
    od004_allowed = {
        (str(item["file"]), int(item["line"])): str(item.get("reason", ""))
        for item in config.get("od004_allow") or []
        if isinstance(item, Mapping) and item.get("file") and item.get("line")
    }

    violations: list[Violation] = []
    if not arch_rules:
        violations.append(
            Violation(
                "ARCH:config",
                str(Path(config_path)),
                1,
                "架构规则为空，检查会静默失效",
                "在 configs/quality/arch_lint.yaml 中至少登记一条 arch_rules",
            )
        )
    for path in sorted((root_path / "opt_discovery").rglob("*.py")):
        rel = _relative(path, root_path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(
                Violation("ARCH", rel, exc.lineno or 1, f"无法解析: {exc}", "修复语法")
            )
            continue
        violations.extend(_check_imports(tree, path, root_path, rel, arch_rules))
        violations.extend(_check_od001(tree, rel))
        violations.extend(_check_od002(tree, rel, authorized))
        violations.extend(_check_od003(tree, rel))
        violations.extend(_check_od004(tree, rel, od004_allowed))
        violations.extend(_check_od005(tree, rel, od005_allowed))
        violations.extend(_check_od006(tree, rel))
    violations.extend(_check_od007(root_path))
    return violations


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--config", default="configs/quality/arch_lint.yaml"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    violations = run_lint(args.root, args.config)
    for item in violations:
        print(item.format(), file=sys.stderr)
    if violations:
        print(f"arch-lint: {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print("arch-lint: OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
