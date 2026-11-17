"""CLI for the discovery MVP.

Subcommands:

* ``validate``   -- check rule file / workload files / parser mapping consistency
* ``demo``       -- run the pipeline on *synthetic* data (pipeline self-test)
* ``parse-csv``  -- parse a profiler CSV (optionally limited to a round window)
* ``diagnose``   -- run bottleneck rules on a parsed operator table
* ``rounds``     -- show the harness rounds and the steady-state window
* ``compare``    -- compare two parsed tables (Eager/Graph or Prefill/Decode)
* ``analyze``    -- end-to-end run over every workload that declares an export dir
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import sys
from pathlib import Path

from .analyzer.collection_audit import audit_collection
from .analyzer.comm import summarize_comm
from .analyzer.contract import compare_contracts, load_table_json
from .analyzer.graph_coverage import summarize_coverage
from .analyzer.compare import compare_tables
from .analyzer.timeline import annotate_diagnosis as annotate_with_timeline
from .analyzer.timeline import summarize as summarize_timeline
from .analyzer.hotspot import hotspot_matrix, matrix_to_text, operator_hotspots
from .analyzer.window import Window, select_rounds_window
from .collectors import discover_all
from .collectors.mstx import load_mstx
from .collectors.workload_log import RunLog
from .diagnosis.engine import DiagnosisEngine, DiagnosisReport
from .diagnosis.rules import RuleSet
from .diagnosis.thresholds import apply_overlay, load_overlay, overlay_summary, write_overlay
from .discovery.generator import opportunities_from_diagnosis
from .experiment.analyzer import compare_benchmarks, load_run
from .experiment.calibration import DEFAULT_MIN_SAMPLES, calibrate
from .experiment.validation import validate_thresholds
from .experiment.feedback import apply_feedback, load_comparison
from .experiment.runner import run_benchmark
from .parsers.csv_mapping import CsvMapping, MappingCsvParser
from .parsers.sqlite_op_report import SqliteOpReportParser
from .report.generator import write_report as write_markdown_report
from .report.summary_csv import write_summaries
from .parsers.synthetic import SCENARIOS, build_synthetic_table
from .schemas.operator import OperatorTable
from .schemas.opportunity import read_opportunities, write_opportunities
from .source_analysis.mapping import (
    DEFAULT_REPOS,
    attach_to_opportunities,
    build_source_map,
)
from .source_analysis.mapping import summarize as summarize_sources
from .schemas.workload import WorkloadSpec

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = _REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"
DEFAULT_WORKLOADS = _REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"
DEFAULT_MAPPING = _REPO_ROOT / "configs" / "parsers" / "ascend_op_statistic.yaml"
SUMMARY_MAPPING = _REPO_ROOT / "configs" / "parsers" / "ascend_op_summary.yaml"
DEFAULT_THRESHOLDS = _REPO_ROOT / "configs" / "thresholds" / "ascend_910b.calibrated.yaml"

#: profiler 导出根目录：优先取环境变量，未设置时用相对路径 ./exports。
#: 不硬编码机器路径，公开仓库在任意机器上都能跑（未设置且目录不存在时命令会明确报错）。
DATA_ROOT = Path(os.environ.get("OPT_DISCOVERY_DATA_ROOT", "exports"))


def _load_device_profile(ruleset: RuleSet, override: str | Path | None = None) -> dict | None:
    """加载 micro-benchmark 设备档案（configs/thresholds/<id>.device_profile.json）。

    供 aicore_utilization / active_core_ratio / arithmetic_intensity_low 的
    实测标定与诊断事实使用；档案不存在时返回 None（相关事实不产出）。
    """
    path = Path(override) if override else (
        _REPO_ROOT / "configs" / "thresholds" / f"{ruleset.device_profile_id}.device_profile.json"
    )
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_ruleset(path: str | Path, thresholds: str | Path | None = None) -> RuleSet:
    """加载规则集；若存在标定覆盖文件则叠加（--thresholds 优先，其次默认路径）。"""
    ruleset = RuleSet.load(path)
    overlay_path = Path(thresholds) if thresholds else DEFAULT_THRESHOLDS
    if overlay_path.is_file():
        ruleset = apply_overlay(ruleset, load_overlay(overlay_path))
    return ruleset


def load_workloads(directory: str | Path) -> list[WorkloadSpec]:
    path = Path(directory)
    if not path.exists():
        raise FileNotFoundError(f"workload directory not found: {path}")
    specs = [WorkloadSpec.from_yaml(item) for item in sorted(path.glob("*.yaml"))]
    if not specs:
        raise FileNotFoundError(f"no *.yaml workload files under {path}")
    return specs


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def cmd_validate(args: argparse.Namespace) -> int:
    base = RuleSet.load(args.rules)
    ruleset = load_ruleset(args.rules, args.thresholds)
    print(f"[rules] {ruleset.identifier}  status={ruleset.status}  rules={len(ruleset.rules)}")
    assumed = [
        name for name, threshold in ruleset.thresholds.items()
        if threshold.provenance.value == "assumed"
    ]
    print(f"[rules] thresholds={len(ruleset.thresholds)}  未校准(assumed)={len(assumed)}")
    if ruleset is not base:
        base_assumed = sum(
            1 for item in base.thresholds.values() if item.provenance.value == "assumed"
        )
        measured = sum(
            1 for item in ruleset.thresholds.values() if item.provenance.value == "measured"
        )
        declared = sum(
            1 for item in ruleset.thresholds.values() if item.provenance.value == "declared"
        )
        print(
            f"[rules] 已叠加标定覆盖: measured={measured} declared={declared} "
            f"（基线 assumed={base_assumed}）"
        )
    for name in sorted(ruleset.thresholds):
        print(f"        - {ruleset.thresholds[name].describe()}")
    if not ruleset.is_calibrated:
        print("[rules] 注意：阈值未校准，依赖阈值的结论最高只能到 hypothesis")

    workloads = load_workloads(args.workloads_dir)
    print(f"\n[workloads] {len(workloads)} 个")
    for spec in workloads:
        print(f"        - {spec.workload_id}  {spec.execution.label}  verified={spec.verified}")
        if spec.unverified_fields:
            print(f"          未确认字段: {', '.join(spec.unverified_fields)}")

    print("\n[comparability] 只比较执行模式/阶段之外的差异")
    for i, left in enumerate(workloads):
        for right in workloads[i + 1 :]:
            if left.execution.phase is not right.execution.phase:
                continue  # 跨阶段对比另有口径要求，此处只检查同阶段
            ok, diffs = left.comparable_with(right)
            status = "可对齐" if ok else "不可对齐"
            print(f"        {left.workload_id} <-> {right.workload_id}: {status}")
            for diff in diffs:
                print(f"            ! {diff}")

    for mapping_path in (args.mapping, args.summary_mapping):
        if not mapping_path:
            continue
        mapping = CsvMapping.load(mapping_path)
        print(f"\n[parser] {mapping.parser} v{mapping.version}  ({Path(mapping_path).name})")
        print(f"         row_semantics:   {mapping.row_semantics}")
        print(f"         aggregate.by:    {list(mapping.aggregate.by)}")
        print(f"         required fields: {list(mapping.missing_required_fields)}")
        print(f"         extra metrics:   {[m.name for m in mapping.extra_metrics]}")
        if mapping.verified_against:
            print(f"         verified_against:{dict(mapping.verified_against)}")
        if mapping.needs_verification:
            print("         注意：列映射尚未与真实导出核对（needs_verification: true）")
    return 0


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------
def cmd_demo(args: argparse.Namespace) -> int:
    if args.scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario {args.scenario!r}; available: {sorted(SCENARIOS)}")
    ruleset = RuleSet.load(args.rules)
    engine = DiagnosisEngine(ruleset)
    workloads = load_workloads(args.workloads_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("警告：本次使用的是合成数据（synthetic），用于验证 schema 与规则流水线，")
    print("      不代表任何真实 Profiling 指标或性能收益。")
    print("=" * 78)

    for spec in workloads:
        table = build_synthetic_table(args.scenario, spec)
        report = engine.diagnose_table(table)
        opportunities = opportunities_from_diagnosis(report)

        table.write_json(out_dir / spec.workload_id / "operator_table.json")
        report.write_json(out_dir / spec.workload_id / "diagnosis.json")
        write_opportunities(opportunities, out_dir / spec.workload_id / "opportunities.json")

        print(f"\n[{spec.workload_id}] scenario={args.scenario} operators={len(table.records)}")
        print(_format_candidates(report))
        print(_format_missing(report))
        print(f"  -> {len(opportunities)} 个候选机会写入 {out_dir / spec.workload_id}")

    print(f"\n完成。产物目录: {out_dir.resolve()}")
    return 0


def _format_candidates(report: DiagnosisReport) -> str:
    lines = [f"  {'operator':<18}{'level':<14}{'conf':<7}bottleneck / rule"]
    for operator in report.operators:
        if not operator.candidates:
            lines.append(f"  {operator.operator:<18}{'-':<14}{'-':<7}(无候选，见 blocked 规则)")
            continue
        for candidate in operator.candidates:
            lines.append(
                f"  {operator.operator:<18}{candidate.level.value:<14}"
                f"{candidate.confidence:<7.2f}{candidate.bottleneck} / {candidate.rule_id}"
            )
    return "\n".join(lines)


def _format_missing(report: DiagnosisReport) -> str:
    summary = report.summary()
    missing = summary["missing_facts_top"][:6]
    if not missing:
        return "  需要补采的指标: 无"
    parts = ", ".join(f"{item['fact']}({item['blocked_rules']})" for item in missing)
    return f"  阻断判定的缺失指标(times): {parts}"


# ---------------------------------------------------------------------------
# parse-csv
# ---------------------------------------------------------------------------
def cmd_parse_csv(args: argparse.Namespace) -> int:
    mapping = CsvMapping.load(args.mapping)
    parser = MappingCsvParser(mapping)
    workload = WorkloadSpec.from_yaml(args.workload)
    if any(ch in args.csv for ch in "*?"):
        files = sorted(Path(p) for p in _glob.glob(args.csv))
    else:
        files = [Path(args.csv)]
    if not files or not files[0].is_file():
        print(f"找不到输入文件: {args.csv}", file=sys.stderr)
        return 2
    if not parser.can_parse(files[0]):
        print("无法识别该 CSV（必需列未命中）。请先核对表头：", file=sys.stderr)
        print(f"  head -1 {files[0]}", file=sys.stderr)
        return 2

    window: Window | None = None
    if args.rounds:
        if not args.log:
            print("--rounds 需要同时提供 --log（workload.log）", file=sys.stderr)
            return 2
        run_log = RunLog.parse(args.log)
        window = select_rounds_window(run_log, _parse_rounds(args.rounds))

    table = parser.parse(files, workload, window=window)
    target = table.write_json(args.out)
    print(f"解析 {table.meta.row_count} 行 -> {len(table.records)} 条算子记录 -> {target}")
    print(
        f"row_semantics={table.meta.extra.get('row_semantics')} "
        f"aggregate_by={table.meta.extra.get('aggregate_by')} "
        f"skipped={table.meta.skipped_rows} warnings={len(table.meta.warnings)}"
    )
    if window is not None:
        print(
            f"窗口 rounds={window.rounds} 时长={window.duration_us / 1e6:.3f}s "
            f"窗口外行数={table.meta.extra.get('rows_outside_window')} "
            f"quality={table.meta.extra.get('window_quality')}"
        )
    total = table.total_operator_time_us()
    if total is not None:
        print(f"累计时间合计: {total:.1f} us")
    for warning in table.meta.warnings[:6]:
        print(f"  [{warning.code}] {warning.message}")
    if table.meta.unmapped_columns:
        print(f"未映射列({len(table.meta.unmapped_columns)}): {list(table.meta.unmapped_columns)[:8]}")
    return 0


# ---------------------------------------------------------------------------
# rounds
# ---------------------------------------------------------------------------
def cmd_rounds(args: argparse.Namespace) -> int:
    run_log = RunLog.parse(Path(args.export) / "workload.log")
    print(f"[log] {run_log.path}")
    print(
        f"[log] prompt_tokens={run_log.prompt_tokens} load_seconds={run_log.load_seconds} "
        f"profile_start_us={run_log.profile_start_us}"
    )
    for round_ in run_log.rounds:
        print(
            f"  round {round_.index}: {round_.duration_us / 1e6:8.3f}s  "
            f"start={round_.start_us:.0f} end={round_.end_us:.0f}"
        )
    if args.rounds:
        window = select_rounds_window(run_log, _parse_rounds(args.rounds))
        print(f"[window] rounds={window.rounds} 时长={window.duration_us / 1e6:.3f}s")
        print(f"[window] {json.dumps(window.to_dict(), ensure_ascii=False)}")
    return 0


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------
def cmd_timeline(args: argparse.Namespace) -> int:
    from .collectors import discover_bundle

    bundle = discover_bundle(args.workload, args.export)
    rank = bundle.rank(args.rank)
    db_files = sorted(rank.directory.glob("msprof_*.db"))
    if not db_files:
        print(f"{rank.directory}: 未找到 msprof_*.db", file=sys.stderr)
        return 2
    db = db_files[0]

    window = None
    if args.rounds:
        if bundle.run_log is None:
            print("缺少 workload.log，无法按轮次切窗", file=sys.stderr)
            return 2
        window = select_rounds_window(bundle.run_log, _parse_rounds(args.rounds))

    mstx = load_mstx(db, window)
    summary = summarize_timeline(
        db, window, min_gap_us=args.min_gap_us, mstx=mstx, extra_db_paths=db_files[1:]
    )
    target = summary.write_json(args.out) if args.out else None

    print(f"[timeline] {bundle.workload_id}  rank={rank.rank}  db={db.name}")
    if window is not None:
        print(f"  window: rounds={window.rounds} {window.duration_us / 1e6:.3f}s")
    print(
        f"  设备: busy={summary.device_busy_us / 1e6:.3f}s idle={summary.device_idle_ratio * 100:.1f}% "
        f"tasks={summary.task_count} gaps={summary.gap_count} "
        f"(p50={summary.gap_p50_us:.0f}us p90={summary.gap_p90_us:.0f}us max={summary.gap_max_us / 1000:.2f}ms)"
    )
    print(
        f"  空洞归因(>={summary.gap_threshold_us:.0f}us, {summary.analyzed_gaps} 个): "
        f"acl_busy={summary.gaps_by_class.get('acl_busy', 0)} "
        f"host_silent={summary.gaps_by_class.get('host_silent', 0)} "
        f"mixed={summary.gaps_by_class.get('mixed', 0)}"
    )
    print(
        f"  Host: acl_calls={summary.host_acl_calls} 忙碌占比={summary.host_busy_ratio_of_span * 100:.1f}% "
        f"覆盖已分析空洞={summary.acl_coverage_of_analyzed_idle * 100:.1f}%"
    )
    print(
        f"  下发: submit_calls={summary.submit_calls} ({summary.submit_per_task:.2f}/task) "
        f"累计 API 时长={summary.submit_time_us / 1000:.2f}ms"
    )
    if summary.overlap_available:
        print(
            f"  重叠分析: compute={summary.compute_share_of_span * 100:.1f}% "
            f"comm={summary.communication_us / summary.span_us * 100:.1f}% "
            f"comm_exposed={summary.comm_exposed_share_of_span * 100:.1f}% "
            f"free={summary.free_us / summary.span_us * 100:.1f}% "
            f"(暴露比例={summary.comm_exposed_ratio * 100:.0f}%)"
        )
    print(f"  方向性提示: {summary.bottleneck_hint}")
    for name, count, total in summary.top_acl_names[:5]:
        print(f"    {name:<34}{count:>9} 次 {total / 1000:8.2f}ms")
    for note in summary.notes:
        print(f"  - {note}")
    if target:
        print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# check-collection（补采契约审计）
# ---------------------------------------------------------------------------
def cmd_check_collection(args: argparse.Namespace) -> int:
    report = audit_collection(args.dir)
    print(f"[check-collection] {report.root}")
    print(f"  verdict: {report.verdict}  runs={len(report.runs)}")
    for item in report.items:
        print(f"  [{'OK  ' if item.ok else 'FAIL'}] {item.name:<20}{item.detail}")
    if report.verdict == "incomplete":
        print("  hint: 已采各组参数一致；缺组是采集被暂停/未完成，不是参数不一致")
    if args.out:
        target = write_json_file(args.out, report.to_dict())
        print(f"-> {target}")
    # 0=完全通过, 2=数据未齐(非错误), 1=参数不一致/无数据
    return {"pass": 0, "incomplete": 2, "fail": 1, "no_data": 1}[report.verdict]


# ---------------------------------------------------------------------------
# graph-coverage
# ---------------------------------------------------------------------------
def cmd_graph_coverage(args: argparse.Namespace) -> int:
    table = OperatorTable.read_json(args.table)
    coverage = summarize_coverage(table, top=args.top)
    print(f"[graph-coverage] {coverage.workload_id}  mode={coverage.mode} graph={coverage.graph_mode}")
    if coverage.total_calls == 0:
        print("  没有 in_graph 标记（该表不是从 op_summary 解析的）")
        return 1
    print(
        f"  图内任务 {coverage.in_graph_calls}/{coverage.total_calls} "
        f"({coverage.task_ratio * 100:.1f}%)  "
        f"按时间 {coverage.in_graph_us / 1e6:.3f}s / {coverage.total_us / 1e6:.3f}s "
        f"({coverage.time_ratio * 100:.1f}%)"
    )
    for note in coverage.notes:
        print(f"  - {note}")
    if coverage.top_fallback:
        print(f"  {'operator':<26}{'calls':>8}{'task_ratio':>12}{'time_ratio':>12}{'fallback_ms':>13}")
        for item in coverage.top_fallback:
            print(
                f"  {item.operator:<26}{item.calls:8d}{item.task_ratio * 100:11.1f}%"
                f"{item.time_ratio * 100:11.1f}%{(item.total_us - item.in_graph_us) / 1000:13.1f}"
            )
    target = coverage.write_json(args.out)
    print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# source-map
# ---------------------------------------------------------------------------
def cmd_source_map(args: argparse.Namespace) -> int:
    table = OperatorTable.read_json(args.table)
    operators = {record.operator for record in table.records}
    repos = [item for item in (args.repos or "").split(",") if item] or list(DEFAULT_REPOS)
    payload = build_source_map(
        operators,
        repos,
        include_cpp=not args.no_cpp,
        binding_path=args.binding,
        use_bindings=not args.no_bindings,
    )
    target = write_json_file(args.out, payload)
    print(f"[source-map] {table.workload_id}")
    print(f"  {summarize_sources(payload)}")
    print(f"  repos: {payload['repos']}")
    mapped = [op for op, item in payload["operators"].items() if item["mapped"]][:5]
    for op in mapped:
        item = payload["operators"][op]
        first = item["hits"][0]
        chain = item.get("binding") or {}
        suffix = f"  <- {chain['schema']} @ {chain['registered_at']}" if chain else ""
        print(
            f"  {op:<28} {item['tier']:<9} {first['repo']}:{first['path']}:{first['line']}{suffix}"
        )
    unmapped = [op for op, item in payload["operators"].items() if not item["mapped"]][:5]
    for op in unmapped:
        print(f"  {op:<28} 未命中：{payload['operators'][op]['reason']}")
    print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# check-runs（采集契约闸门）
# ---------------------------------------------------------------------------
def cmd_check_runs(args: argparse.Namespace) -> int:
    report = compare_contracts(load_table_json(args.a), load_table_json(args.b))
    print(f"[check-runs] {report.a_id}  vs  {report.b_id}")
    print(f"  verdict: {report.verdict}")
    width = max(len(item.name) for item in report.checks)
    for item in report.checks:
        mark = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "INFO"}[item.status]
        print(f"  [{mark}] {item.name:<{width}}  {item.a!s:>14}  vs  {item.b!s:<14} {item.note}")
    if report.verdict == "incomparable":
        print("  结论：两侧采集契约不一致，不应据此出对比结论")
    out_dir = Path(args.out_dir)
    target = report.write_json(out_dir / "contract.json")
    print(f"-> {target}")
    return 0 if report.verdict != "incomparable" else 1


# ---------------------------------------------------------------------------
# calibrate（阈值标定）
# ---------------------------------------------------------------------------
def cmd_calibrate(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    tables: dict[str, OperatorTable] = {}
    for path in sorted(root.glob("*/operator_table.json")):
        table = OperatorTable.read_json(path)
        tables[table.workload_id] = table
    if not tables:
        print(f"{root}: 没有找到 */operator_table.json", file=sys.stderr)
        return 2

    timelines: dict[str, dict] = {}
    for path in sorted(root.glob("*/timeline.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        timelines[path.parent.name] = payload

    ruleset = RuleSet.load(args.rules)
    device_profile = _load_device_profile(ruleset, getattr(args, "device_profile", None))
    report = calibrate(
        tables,
        ruleset,
        source=str(root),
        min_samples=args.min_samples,
        timelines=timelines,
        device_profile=device_profile,
    )
    print(f"[calibrate] 来源 {root}（{len(tables)} 个 Workload）")
    print(f"  {report.calibrated}/{len(report.proposals)} 项可标定：")
    for item in report.proposals:
        if item.status != "calibrated":
            continue
        print(
            f"  {item.name:<42}{item.current_value:6.2f} -> {item.proposed_value:5.2f}"
            f"   (raw {item.raw_value:.3f}, n={item.samples})"
        )
    other = [item for item in report.proposals if item.status != "calibrated"]
    print(f"  其余 {len(other)} 项：")
    for item in other:
        print(f"  {item.name:<42}{item.status:<22}{item.note[:56]}")

    validation = None
    if args.validation:
        validation_path = Path(args.validation)
        if not validation_path.is_file():
            print(f"{validation_path}: 留出验证报告不存在", file=sys.stderr)
            return 2
        validation = json.loads(validation_path.read_text(encoding="utf-8"))

    if args.dry_run:
        print("  [dry-run] 未写文件")
        return 0
    thresholds = report.to_thresholds_with_validation(validation)
    target = write_overlay(
        args.out,
        thresholds,
        device_profile_id=ruleset.device_profile_id,
        source=f"{root}（{len(tables)} 个 Workload）",
        notes=report.notes,
    )
    measured = [n for n, t in thresholds.items() if t.provenance.value == "measured"]
    declared = [n for n, t in thresholds.items() if t.provenance.value == "declared"]
    print(f"  写入 {len(thresholds)} 项：measured={len(measured)} declared={len(declared)}")
    for name in declared:
        print(f"    declared: {name}（留一验证不稳，已冻结为固定值）")
    merged = apply_overlay(ruleset, load_overlay(target))
    print(f"  {overlay_summary(merged)}")
    print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# validate-thresholds（留出验证）
# ---------------------------------------------------------------------------
def cmd_validate_thresholds(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    tables: dict[str, OperatorTable] = {}
    for path in sorted(root.glob("*/operator_table.json")):
        table = OperatorTable.read_json(path)
        tables[table.workload_id] = table
    if len(tables) < 3:
        print(f"{root}: 需要至少 3 个 Workload，实际 {len(tables)}", file=sys.stderr)
        return 2

    timelines: dict[str, dict] = {}
    for path in sorted(root.glob("*/timeline.json")):
        timelines[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))

    ruleset = RuleSet.load(args.rules)
    device_profile = _load_device_profile(ruleset, getattr(args, "device_profile", None))
    report = validate_thresholds(
        tables,
        ruleset,
        source=str(root),
        min_samples=args.min_samples,
        timelines=timelines,
        validation_min_samples=args.validation_min_samples,
        device_profile=device_profile,
    )
    target = report.write_json(args.out)

    print(f"[validate-thresholds] 来源 {root}，{report.mode}")
    print(f"  verdict: {report.verdict}")
    print(f"  {'threshold':<42}{'full':>7}  {'fold(留出标定)':<28}{'holdout(自身)':<26}{'drift':>8}  status")
    for item in report.drifts:
        if item.status in ("unmeasurable",):
            continue
        folds = ",".join(f"{v:.2f}" for v in item.fold_values)
        holds = ",".join(f"{v:.2f}" for v in item.holdout_values)
        drift = "-" if item.rel_drift is None else f"{item.rel_drift * 100:.1f}%"
        print(
            f"  {item.name:<42}{item.calibration_value:7.2f}  {folds:<28}{holds:<26}{drift:>8}  {item.status}"
        )
    print()
    for fold in report.folds:
        print(
            f"  fold held_out={fold.held_out:<32} candidates {fold.candidates_baseline} vs "
            f"{fold.candidates_fold}  overlap={fold.rule_overlap * 100:.1f}%  "
            f"level_changes={len(fold.level_changes)}"
        )
        if fold.only_baseline:
            print(f"      仅全量阈值命中: {list(fold.only_baseline[:3])}")
        if fold.only_fold:
            print(f"      仅折内阈值命中: {list(fold.only_fold[:3])}")
    for note in report.notes:
        print(f"  - {note}")
    print(f"-> {target}")
    return 0 if report.verdict != "fail" else 1


# ---------------------------------------------------------------------------
# feedback（把 A/B 结果回填到机会）
# ---------------------------------------------------------------------------
def cmd_feedback(args: argparse.Namespace) -> int:
    opportunities = read_opportunities(args.opportunities)
    comparison = load_comparison(args.compare)
    result = apply_feedback(
        opportunities,
        comparison,
        operator=args.operator,
        change=args.change,
        min_effect=args.min_effect,
    )
    target = write_opportunities(opportunities, args.out or args.opportunities)
    print(f"[feedback] {comparison.get('a')} -> {comparison.get('b')}  verdict={result.verdict}")
    print(f"  作用域: {'单算子 ' + result.operator if result.operator else '工作负载级（不归因到单个算子）'}")
    print(
        f"  matched={result.matched} upgraded={result.upgraded} "
        f"rejected={result.rejected} untouched={result.untouched}"
    )
    for note in result.notes:
        print(f"  - {note}")
    for item in opportunities:
        if item.extra.get("measured"):
            measured = item.extra["measured"]
            print(
                f"  {item.opportunity_id} {item.operator:<24} status={item.status.value:<10} "
                f"level={item.evidence_level.value:<10} delta={measured.get('latency_median_delta_pct')}%"
            )
    print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# bench（跑现有 harness，产出整轮延迟）
# ---------------------------------------------------------------------------
def cmd_bench(args: argparse.Namespace) -> int:
    run = run_benchmark(
        args.harness,
        args.out,
        label=args.label,
        python=args.python,
        timeout_s=args.timeout,
        msprof_args=args.msprof_args,
        print_only=args.print_only,
    )
    print(f"[bench] {run.label}  harness={Path(run.harness).name}")
    print(f"  command: {' '.join(run.command)}")
    if args.print_only:
        print("  print_only：未执行")
        return 0
    print(f"  exit={run.exit_code} wall={run.wall_s:.1f}s rounds={len(run.rounds)}")
    for item in run.rounds:
        speed = f"{item.tokens_per_s:.2f} tok/s" if item.tokens_per_s else "-"
        print(f"    round {item.index}: {item.duration_s:.3f}s  out_tokens={item.out_tokens}  {speed}")
    summary = run.to_dict()["summary"]
    if summary.get("rounds"):
        latency = summary["latency_s"]
        print(
            f"  中位 {latency['median']:.3f}s  p90 {latency['p90']:.3f}s  "
            f"max {latency['max']:.3f}s"
        )
    for note in run.notes:
        print(f"  - {note}")
    target = run.write_json(Path(args.out) / "benchmark.json")
    print(f"-> {target}")
    return 0 if run.exit_code in (0, None) else 1


# ---------------------------------------------------------------------------
# bench-compare
# ---------------------------------------------------------------------------
def cmd_bench_compare(args: argparse.Namespace) -> int:
    comparison = compare_benchmarks(load_run(args.a), load_run(args.b), min_effect=args.min_effect)
    print(f"[bench-compare] {comparison.a_label} -> {comparison.b_label}")
    print(f"  verdict: {comparison.verdict}  (rounds {comparison.rounds_a} vs {comparison.rounds_b})")
    for name, item in comparison.metrics.items():
        print(
            f"  {name:<22} a={item['a']}  b={item['b']}  "
            f"ratio={item['ratio']}  delta={item['delta_pct']}%"
        )
    for note in comparison.notes:
        print(f"  - {note}")
    out_dir = Path(args.out_dir)
    target = comparison.write_json(out_dir / "bench_compare.json")
    print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# comm（字节守恒量）
# ---------------------------------------------------------------------------
def cmd_comm(args: argparse.Namespace) -> int:
    from .collectors import discover_bundle

    bundle = discover_bundle(args.workload, args.export)
    rank = bundle.rank(args.rank)
    db_files = sorted(rank.directory.glob("msprof_*.db"))
    if not db_files:
        print(f"{rank.directory}: 未找到 msprof_*.db", file=sys.stderr)
        return 2
    volume = summarize_comm(db_files[0])
    if args.out:
        volume.write_json(args.out)
    print(f"[comm] {bundle.workload_id}  rank={rank.rank}")
    if not volume.available:
        print("  该 rank 没有通信采集")
        return 0
    print(
        f"  字节总量(去重)={volume.bytes_total / 1024**2:.1f} MiB  "
        f"通信任务={volume.unique_tasks}  原始行={volume.records} "
        f"(重复 {volume.dedup_ratio:.1f}x)"
    )
    print(
        f"  单任务平均={volume.kib_per_task:.1f} KiB  "
        f"平均带宽={volume.bandwidth_mean_gbps:.1f} GB/s  groups={volume.groups}"
    )
    for note in volume.notes:
        print(f"  - {note}")
    return 0


# ---------------------------------------------------------------------------
# check-ranks
# ---------------------------------------------------------------------------
def cmd_check_ranks(args: argparse.Namespace) -> int:
    """Compare the two TP ranks of one workload.

    Scope matters: a CSV rank is parsed inside the steady window, while a rank
    that fell back to sqlite ``op_report`` only has whole-run aggregates.  When
    scopes differ, totals and call counts are **not** compared; only per-call
    averages and share structure are.
    """
    from .collectors import discover_bundle

    bundle = discover_bundle(args.workload, args.export)
    rounds = _parse_rounds(args.rounds)
    window = select_rounds_window(bundle.run_log, rounds) if bundle.run_log else None

    parser = MappingCsvParser(CsvMapping.load(args.summary_mapping))
    sqlite_parser = SqliteOpReportParser()
    tables: dict[int, OperatorTable] = {}
    sources: dict[int, str] = {}
    scopes: dict[int, str] = {}
    for rank in bundle.ranks:
        if rank.op_summary_files:
            tables[rank.rank] = parser.parse(
                rank.op_summary_files, bundle.workload, window=window
            )
            sources[rank.rank] = "op_summary(窗口)"
            scopes[rank.rank] = "window"
        else:
            target = rank.op_report_db or rank.directory
            try:
                tables[rank.rank] = sqlite_parser.parse(target, bundle.workload)
                sources[rank.rank] = "sqlite op_report(全量)"
                scopes[rank.rank] = "full"
            except Exception as exc:  # noqa: BLE001
                print(f"  rank{rank.rank}: 无法解析 ({exc})")

    if len(tables) < 2:
        print(
            f"只有 {len(tables)} 个 rank 可解析；缺 CSV 的 rank 需要 device_*/sqlite/op_counter.db",
            file=sys.stderr,
        )
        return 2

    ranks = sorted(tables)
    base, peer = tables[ranks[0]], tables[ranks[1]]
    total_base = base.total_operator_time_us() or 0.0
    total_peer = peer.total_operator_time_us() or 0.0
    calls_base = sum(r.calls for r in base.records)
    calls_peer = sum(r.calls for r in peer.records)
    same_scope = scopes[ranks[0]] == scopes[ranks[1]]

    print(f"[check-ranks] {bundle.workload_id}  ranks={ranks}")
    for rank in ranks:
        print(
            f"  rank{rank}: {sources[rank]:22s} scope={scopes[rank]:7s} "
            f"records={len(tables[rank].records):3d} calls={sum(r.calls for r in tables[rank].records):9d}"
        )

    base_map = {r.operator: r for r in base.records}
    rows = []
    for record in peer.records:
        reference = base_map.get(record.operator)
        if reference is None or not reference.total_time_us or not record.total_time_us:
            continue
        share_base = reference.total_time_us.value / total_base if total_base else 0.0
        share_peer = record.total_time_us.value / total_peer if total_peer else 0.0
        avg_base = reference.total_time_us.value / reference.calls if reference.calls else 0.0
        avg_peer = record.total_time_us.value / record.calls if record.calls else 0.0
        rows.append(
            (
                record.operator,
                share_base,
                share_peer,
                avg_base,
                avg_peer,
                (avg_peer / avg_base) if avg_base else None,
            )
        )
    rows.sort(key=lambda item: -item[1])

    print()
    if same_scope:
        calls_ratio = calls_peer / calls_base if calls_base else 0.0
        total_ratio = total_peer / total_base if total_base else 0.0
        print(f"  口径相同（{scopes[ranks[0]]}）：calls_ratio={calls_ratio:.3f} total_ratio={total_ratio:.3f}")
        consistent = 0.9 <= calls_ratio <= 1.1 and 0.8 <= total_ratio <= 1.25
    else:
        print(
            f"  口径不同（{scopes[ranks[0]]} vs {scopes[ranks[1]]}）："
            "calls/total 不可直接比较，只看单次耗时与占比结构"
        )
        ratios = [item[5] for item in rows[:8] if item[5] is not None]
        median = sorted(ratios)[len(ratios) // 2] if ratios else None
        consistent = bool(median is not None and 0.7 <= median <= 1.4)
        if median is not None:
            print(f"  前 8 大算子的单次耗时比中位数：{median:.3f}")

    print(f"  判定: {'一致（可合并/可代表）' if consistent else '不一致（不可合并）'}")
    if not consistent:
        print("  依据：单次耗时整体偏离 1.0 -> 两 rank 采集状态不同（profiling 开销/设备状态）")
        print("  建议：用相同配置复采一个小窗口，或先用 Serving 指标交叉验证")

    print()
    print(f"  {'operator':<26}{'share_base':>11}{'share_peer':>11}{'avg_base(us)':>14}{'avg_peer(us)':>13}{'avg_ratio':>10}")
    for operator, share_base, share_peer, avg_base, avg_peer, ratio in rows[:8]:
        print(
            f"  {operator:<26}{share_base * 100:10.2f}%{share_peer * 100:10.2f}%"
            f"{avg_base:14.1f}{avg_peer:13.1f}{('-' if ratio is None else f'{ratio:10.3f}')}"
        )
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    target = write_markdown_report(args.dir, args.out or None)
    print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# summary（两个扁平表）
# ---------------------------------------------------------------------------
def cmd_summary(args: argparse.Namespace) -> int:
    workload_path, operator_path = write_summaries(args.dir)
    rows = sum(1 for _ in operator_path.open(encoding="utf-8-sig")) - 1
    print(f"-> {workload_path}")
    print(f"-> {operator_path}  ({rows} 行算子)")
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
def cmd_compare(args: argparse.Namespace) -> int:
    a = OperatorTable.read_json(args.a)
    b = OperatorTable.read_json(args.b)
    report = compare_tables(a, b, top=args.top)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = report.write_json(out_dir / f"compare_{a.workload_id}_vs_{b.workload_id}.json")
    print(f"[compare] {report.a_id} -> {report.b_id}  comparable={report.comparable}")
    for reason in report.reasons:
        print(f"  ! {reason}")
    for warning in report.warnings:
        print(f"  ~ {warning}")
    for note in report.observations:
        print(f"  - {note}")
    header = f"{'operator':<26}{'share_a':>10}{'share_b':>10}{'Δshare':>10}{'calls_ratio':>13}{'avg_ratio':>11}"
    print(header)
    for delta in report.deltas[: args.top]:
        def fmt(value: float | None, scale: float = 1.0) -> str:
            return "-" if value is None else f"{value * scale:.3f}"

        print(
            f"{delta.operator:<26}{fmt(delta.share_a, 100):>10}"
            f"{fmt(delta.share_b, 100):>10}{fmt(delta.delta_share, 100):>10}"
            f"{fmt(delta.calls_ratio):>13}{fmt(delta.avg_ratio):>11}"
        )
    print(f"-> {target}")
    return 0


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def cmd_analyze(args: argparse.Namespace) -> int:
    bundles = discover_all(args.workloads_dir, args.exports_root)
    if not bundles:
        print(
            "没有找到可用导出：请确认 workload YAML 中的 labels.export_dir 指向 "
            f"{args.exports_root} 下的目录",
            file=sys.stderr,
        )
        return 2

    rounds = _parse_rounds(args.rounds)
    mapping = CsvMapping.load(args.summary_mapping)
    parser = MappingCsvParser(mapping)
    ruleset = load_ruleset(args.rules, args.thresholds)
    device_profile = _load_device_profile(ruleset, getattr(args, "device_profile", None))
    engine = DiagnosisEngine(ruleset, device_profile=device_profile)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tables: dict[str, OperatorTable] = {}
    all_opportunities: dict[str, list] = {}
    print(f"[analyze] {len(bundles)} 个 export，窗口 rounds={rounds}，rank={args.rank}")
    for bundle in bundles:
        workload_id = bundle.workload_id
        if bundle.run_log is None:
            print(f"  - {workload_id}: 无 workload.log，跳过（无法按轮次切窗）")
            continue
        try:
            window = select_rounds_window(bundle.run_log, rounds)
            rank = bundle.rank(args.rank)
        except Exception as exc:  # noqa: BLE001 - 逐 Workload 报告，不中断整批
            print(f"  - {workload_id}: 跳过 ({exc})")
            continue

        table = parser.parse(rank.op_summary_files, bundle.workload, window=window)
        tables[workload_id] = table
        report = engine.diagnose_table(table)

        # TP>1 时 busy/idle 必须合并所有 rank 的库：单读一个 rank 会得出相反结论
        db_files: list[Path] = []
        for item in bundle.ranks:
            db_files.extend(sorted(item.directory.glob("msprof_*.db")))
        if db_files:
            try:
                volume = summarize_comm(db_files[0])
                table.meta.extra["comm_volume"] = volume.to_dict()
                volume.write_json(out / workload_id / "comm.json")
                if volume.available:
                    print(
                        f"    comm: {volume.bytes_total / 1024**2:.1f} MiB "
                        f"tasks={volume.unique_tasks} rows={volume.records} "
                        f"({volume.dedup_ratio:.1f}x dedup)"
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"    ! comm 失败: {exc}")

        if not args.skip_timeline:
            if db_files:
                try:
                    mstx = load_mstx(db_files[0], window)
                    timeline = summarize_timeline(
                        db_files[0], window, mstx=mstx, extra_db_paths=db_files[1:]
                    )
                    report.notes = report.notes + tuple(annotate_with_timeline(report, timeline))
                    timeline.write_json(out / workload_id / "timeline.json")
                    print(
                        f"    timeline: idle={timeline.device_idle_ratio * 100:.1f}% "
                        f"acl_cover={timeline.acl_coverage_of_analyzed_idle * 100:.1f}% "
                        f"submit={timeline.submit_per_task:.2f}/task"
                    )
                except Exception as exc:  # noqa: BLE001 - 单 workload 失败不影响整批
                    print(f"    ! timeline 失败: {exc}")

        # comm_volume / window 质量等都挂在 meta.extra 上，必须在写盘前完成
        table.write_json(out / workload_id / "operator_table.json")
        coverage = summarize_coverage(table)
        coverage.write_json(out / workload_id / "graph_coverage.json")
        if coverage.total_calls:
            print(
                f"    graph-coverage: 图内任务 {coverage.task_ratio * 100:.1f}% "
                f"/ 按时间 {coverage.time_ratio * 100:.1f}%"
            )
        report.write_json(out / workload_id / "diagnosis.json")
        generated = opportunities_from_diagnosis(report)
        all_opportunities[workload_id] = generated
        write_opportunities(generated, out / workload_id / "opportunities.json")

        quality = table.meta.extra.get("window_quality") or {}
        total = table.total_operator_time_us() or 0.0
        print(
            f"\n  [{workload_id}] {bundle.workload.execution.label}  "
            f"rows={table.meta.row_count} kept={table.meta.row_count - table.meta.extra.get('rows_outside_window', 0)}"
            f" skipped={table.meta.skipped_rows} records={len(table.records)} "
            f"total={total / 1e6:.3f}s"
        )
        print(
            f"    window: {window.rounds} {window.duration_us / 1e6:.3f}s  "
            f"busy={quality.get('busy_ratio', 0):.3f} max_gap={quality.get('max_gap_us', 0) / 1000:.1f}ms "
            f"degraded={quality.get('degraded')}"
        )
        for hotspot in operator_hotspots(table, top=3):
            share = f"{hotspot.share * 100:.2f}%" if hotspot.share is not None else "-"
            print(
                f"    top: {hotspot.operator:<24}{share:>8}  calls={hotspot.calls:<7}"
                f"avg={hotspot.avg_us:9.1f}us"
            )
        summary = report.summary()
        print(f"    diagnosis: {summary['candidates_by_bottleneck']}")
        for reason in quality.get("reasons") or ():
            print(f"    ! window: {reason}")

    if not tables:
        print("没有成功解析的 Workload", file=sys.stderr)
        return 1

    if args.source_map:
        operators = {record.operator for table in tables.values() for record in table.records}
        repos = [item for item in args.repos.split(",") if item] or list(DEFAULT_REPOS)
        payload = build_source_map(
            operators,
            repos,
            include_cpp=not args.no_source_cpp,
            binding_path=args.binding,
            use_bindings=not args.no_bindings,
        )
        write_json_file(out / "source_map.json", payload)
        attached_total = 0
        for workload_id, generated in all_opportunities.items():
            attached = attach_to_opportunities(generated, payload)
            attached_total += attached
            if attached:
                write_opportunities(generated, out / workload_id / "opportunities.json")
        print(f"\n[source-map] {summarize_sources(payload)}")
        print(f"  回填 source_refs 的机会数：{attached_total}")

    matrix = hotspot_matrix(tables, top=args.top)
    (out / "hotspots.json").write_text(
        json.dumps(matrix, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\n[hotspot matrix] 算子累计时间占比")
    print(matrix_to_text(matrix, top=args.top))

    print("\n[comparisons]")
    pairs = _comparison_pairs(bundles)
    for key_a, key_b in pairs:
        table_a, table_b = tables.get(key_a), tables.get(key_b)
        if not table_a or not table_b:
            continue
        report = compare_tables(table_a, table_b, top=args.top)
        target = report.write_json(out / f"compare_{key_a}_vs_{key_b}.json")
        print(
            f"  - {key_a} -> {key_b}: comparable={report.comparable} "
            f"total_ratio={_fmt_ratio(report)} Δtop={report.deltas[0].operator if report.deltas else '-'}"
        )
        for warning in report.warnings:
            print(f"      ~ {warning}")
        for note in report.observations[:3]:
            print(f"      - {note}")
        print(f"      -> {target}")
    workload_csv, operator_csv = write_summaries(out)
    print(f"\n[summary] {workload_csv.name} / {operator_csv.name}")
    print(f"完成。产物目录: {out.resolve()}")
    return 0


def _fmt_ratio(report) -> str:  # noqa: ANN001
    if not report.total_a_us or not report.total_b_us:
        return "-"
    return f"{report.total_b_us / report.total_a_us:.3f}"


def _comparison_pairs(bundles) -> list[tuple[str, str]]:  # noqa: ANN001
    """Same-phase (Eager vs Graph) and same-mode (Prefill vs Decode) pairs."""
    ordered = sorted(bundles, key=lambda item: item.workload_id)
    pairs: list[tuple[str, str]] = []
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            a, b = left.workload, right.workload
            same_phase = a.execution.phase is b.execution.phase
            same_mode = a.execution.mode is b.execution.mode
            if same_phase or same_mode:
                pairs.append((left.workload_id, right.workload_id))
    return pairs


def write_json_file(path: str | Path, payload: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def _parse_rounds(text: str) -> tuple[int, ...]:
    rounds: list[int] = []
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, end = chunk.split("-", 1)
            rounds.extend(range(int(start), int(end) + 1))
        else:
            rounds.append(int(chunk))
    if not rounds:
        raise ValueError(f"无法解析轮次表达式: {text!r}")
    return tuple(sorted(set(rounds)))


# ---------------------------------------------------------------------------
# parse-sqlite（CSV 导出缺失时的回填路径）
# ---------------------------------------------------------------------------
def cmd_parse_sqlite(args: argparse.Namespace) -> int:
    workload = WorkloadSpec.from_yaml(args.workload)
    parser = SqliteOpReportParser()
    table = parser.parse(Path(args.prof_dir), workload)
    target = table.write_json(args.out)
    total = table.total_operator_time_us() or 0.0
    print(
        f"从 sqlite 回填 {table.meta.row_count} 条算子统计 -> {len(table.records)} 条记录 -> {target}"
    )
    print(f"累计时间合计: {total / 1e6:.3f}s  skipped={table.meta.skipped_rows}")
    for warning in table.meta.warnings:
        print(f"  [{warning.code}] {warning.message}")
    return 0


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------
def cmd_diagnose(args: argparse.Namespace) -> int:
    ruleset = load_ruleset(args.rules, args.thresholds)
    engine = DiagnosisEngine(ruleset)
    table = OperatorTable.read_json(args.table)
    report = engine.diagnose_table(table)
    opportunities = opportunities_from_diagnosis(report)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report.write_json(out_dir / "diagnosis.json")
    write_opportunities(opportunities, out_dir / "opportunities.json")

    print(
        f"[table]   {table.workload_id}  records={len(table.records)}  "
        f"synthetic={table.meta.is_synthetic}"
    )
    print(f"[rules]   {ruleset.identifier}  status={ruleset.status}")
    if table.meta.is_synthetic:
        print("[warn]    输入为合成数据，结论不代表真实性能")
    print(_format_candidates(report))
    print(_format_missing(report))
    print(f"-> {out_dir / 'diagnosis.json'}")
    print(f"-> {out_dir / 'opportunities.json'} ({len(opportunities)} 个候选机会)")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opt-discovery", description="Optimization Opportunity Discovery (MVP)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验规则、Workload 与列映射配置")
    p_validate.add_argument("--rules", default=str(DEFAULT_RULES))
    p_validate.add_argument("--workloads-dir", default=str(DEFAULT_WORKLOADS))
    p_validate.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    p_validate.add_argument("--summary-mapping", default=str(SUMMARY_MAPPING))
    p_validate.add_argument("--thresholds", default=None, help="标定覆盖文件（默认自动查找）")
    p_validate.set_defaults(func=cmd_validate)

    p_demo = sub.add_parser("demo", help="用合成数据跑通流水线（非真实测量）")
    p_demo.add_argument("--rules", default=str(DEFAULT_RULES))
    p_demo.add_argument("--workloads-dir", default=str(DEFAULT_WORKLOADS))
    p_demo.add_argument("--scenario", default="rich", choices=sorted(SCENARIOS))
    p_demo.add_argument("--out", default="out/demo")
    p_demo.set_defaults(func=cmd_demo)

    p_parse = sub.add_parser("parse-csv", help="解析真实算子统计 CSV")
    p_parse.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    p_parse.add_argument("--workload", required=True, help="Workload YAML 路径")
    p_parse.add_argument("--csv", required=True, help="算子统计 CSV 路径")
    p_parse.add_argument("--out", default="out/operator_table.json")
    p_parse.add_argument("--rounds", help="轮次窗口，如 '1-4'；需配合 --log")
    p_parse.add_argument("--log", help="workload.log 路径（提供轮次边界）")
    p_parse.set_defaults(func=cmd_parse_csv)

    p_sqlite = sub.add_parser("parse-sqlite", help="从 device_*/sqlite 回填算子统计（无 CSV 时）")
    p_sqlite.add_argument("--prof-dir", required=True, help="PROF_* 目录（或 device_N/sqlite 目录）")
    p_sqlite.add_argument("--workload", required=True)
    p_sqlite.add_argument("--out", default="out/operator_table_sqlite.json")
    p_sqlite.set_defaults(func=cmd_parse_sqlite)

    p_rounds = sub.add_parser("rounds", help="查看 harness 记录的轮次与稳态窗口")
    p_rounds.add_argument("--export", required=True, help="导出目录（含 workload.log）")
    p_rounds.add_argument("--rounds", default="1-4")
    p_rounds.set_defaults(func=cmd_rounds)

    p_timeline = sub.add_parser("timeline", help="设备空洞归因（TASK + CANN_API）")
    p_timeline.add_argument("--export", default=str(DATA_ROOT / "qwen35-prefill"))
    p_timeline.add_argument("--workload", required=True)
    p_timeline.add_argument("--rounds", default="1-4")
    p_timeline.add_argument("--rank", type=int, default=0)
    p_timeline.add_argument("--min-gap-us", type=float, default=100.0)
    p_timeline.add_argument("--out", default=None)
    p_timeline.set_defaults(func=cmd_timeline)

    p_ranks = sub.add_parser("check-ranks", help="校验同一 Workload 的两个 rank 是否一致")
    p_ranks.add_argument("--export", required=True)
    p_ranks.add_argument("--workload", required=True)
    p_ranks.add_argument("--summary-mapping", default=str(SUMMARY_MAPPING))
    p_ranks.add_argument("--rounds", default="1-4")
    p_ranks.set_defaults(func=cmd_check_ranks)

    p_cc = sub.add_parser("check-collection", help="审计统一补采是否满足参数契约")
    p_cc.add_argument("--dir", default=str(DATA_ROOT / "unified"))
    p_cc.add_argument("--out", default=None)
    p_cc.set_defaults(func=cmd_check_collection)

    p_cov = sub.add_parser("graph-coverage", help="图覆盖率（FULL/PIECEWISE 覆盖到哪一步）")
    p_cov.add_argument("--table", required=True)
    p_cov.add_argument("--top", type=int, default=8)
    p_cov.add_argument("--out", default="out/graph_coverage.json")
    p_cov.set_defaults(func=cmd_graph_coverage)

    p_src = sub.add_parser("source-map", help="算子 -> 源码位置映射")
    p_src.add_argument("--table", required=True, help="operator_table.json")
    p_src.add_argument("--repos", default=",".join(DEFAULT_REPOS))
    p_src.add_argument("--no-cpp", action="store_true", help="只索引 .py")
    p_src.add_argument("--binding", default=None, help="torch_binding.cpp 路径（默认自动查找）")
    p_src.add_argument("--no-bindings", action="store_true", help="不解析注册表")
    p_src.add_argument("--out", default="out/source_map.json")
    p_src.set_defaults(func=cmd_source_map)

    p_runs = sub.add_parser("check-runs", help="采集契约闸门：两次运行是否可以比较")
    p_runs.add_argument("--a", required=True, help="operator_table.json 或其所在目录")
    p_runs.add_argument("--b", required=True)
    p_runs.add_argument("--out-dir", default="out/contract")
    p_runs.set_defaults(func=cmd_check_runs)

    p_vt = sub.add_parser("validate-thresholds", help="留一 Workload 验证标定阈值能否推广")
    p_vt.add_argument("--dir", default="out/steady")
    p_vt.add_argument("--rules", default=str(DEFAULT_RULES))
    p_vt.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES,
                      help="全量标定门槛（与 calibrate 一致）")
    p_vt.add_argument("--validation-min-samples", type=int, default=3,
                      help="折内/留出组标定门槛，需更宽松才有足够折")
    p_vt.add_argument("--out", default="out/threshold_validation.json")
    p_vt.add_argument("--device-profile", default=None,
                      help="micro-benchmark 设备档案 JSON（默认按 ruleset 的 device_profile_id 自动查找）")
    p_vt.set_defaults(func=cmd_validate_thresholds)

    p_cal = sub.add_parser("calibrate", help="从已有导出标定阈值，生成覆盖文件（P2）")
    p_cal.add_argument("--dir", default="out/steady")
    p_cal.add_argument("--rules", default=str(DEFAULT_RULES))
    p_cal.add_argument("--out", default=str(DEFAULT_THRESHOLDS))
    p_cal.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    p_cal.add_argument("--validation", default=None, help="validate-thresholds 的输出；用于把不稳项降级为 declared")
    p_cal.add_argument("--dry-run", action="store_true")
    p_cal.add_argument("--device-profile", default=None,
                       help="micro-benchmark 设备档案 JSON（默认按 ruleset 的 device_profile_id 自动查找）")
    p_cal.set_defaults(func=cmd_calibrate)

    p_fb = sub.add_parser("feedback", help="把 bench 的 A/B 结果回填到机会（迭代 6）")
    p_fb.add_argument("--opportunities", required=True)
    p_fb.add_argument("--compare", required=True, help="bench_compare.json")
    p_fb.add_argument("--operator", default=None, help="单变量实验：本次只改了哪个算子")
    p_fb.add_argument("--change", default=None, help="改动描述，会写进机会的 extra")
    p_fb.add_argument("--min-effect", type=float, default=0.05)
    p_fb.add_argument("--out", default=None, help="默认原地更新")
    p_fb.set_defaults(func=cmd_feedback)

    p_bench = sub.add_parser("bench", help="执行现有 harness，产出整轮延迟 benchmark.json")
    p_bench.add_argument("--harness", required=True, help="如 <你的 harness>.py（打印 ACTIVE_ROUND 的脚本）")
    p_bench.add_argument("--out", required=True)
    p_bench.add_argument("--label", default=None)
    p_bench.add_argument("--python", default="python3")
    p_bench.add_argument("--timeout", type=float, default=None)
    p_bench.add_argument("--msprof-args", default=None, help="透传给 msprof 的参数（可选）")
    p_bench.add_argument("--print-only", action="store_true", help="只打印将要执行的命令")
    p_bench.set_defaults(func=cmd_bench)

    p_bc = sub.add_parser("bench-compare", help="比较两次 benchmark.json")
    p_bc.add_argument("--a", required=True)
    p_bc.add_argument("--b", required=True)
    p_bc.add_argument("--min-effect", type=float, default=0.05)
    p_bc.add_argument("--out-dir", default="out/bench")
    p_bc.set_defaults(func=cmd_bench_compare)

    p_comm = sub.add_parser("comm", help="通信字节量（跨执行模式唯一可比的量）")
    p_comm.add_argument("--export", default=str(DATA_ROOT / "qwen35-prefill"))
    p_comm.add_argument("--workload", required=True)
    p_comm.add_argument("--rank", type=int, default=0)
    p_comm.add_argument("--out", default=None)
    p_comm.set_defaults(func=cmd_comm)

    p_report = sub.add_parser("report", help="把 analyze 产物渲染成 report.md")
    p_report.add_argument("--dir", default="out/steady")
    p_report.add_argument("--out", default=None)
    p_report.set_defaults(func=cmd_report)

    p_summary = sub.add_parser("summary", help="导出 workload_summary.csv / operator_summary.csv")
    p_summary.add_argument("--dir", default="out/steady")
    p_summary.set_defaults(func=cmd_summary)

    p_compare = sub.add_parser("compare", help="对比两个已解析的 OperatorTable")
    p_compare.add_argument("--a", required=True, help="基线 operator_table.json")
    p_compare.add_argument("--b", required=True, help="对照 operator_table.json")
    p_compare.add_argument("--top", type=int, default=15)
    p_compare.add_argument("--out-dir", default="out/compare")
    p_compare.set_defaults(func=cmd_compare)

    p_analyze = sub.add_parser("analyze", help="对全部 Workload 端到端分析（稳态窗口）")
    p_analyze.add_argument("--exports-root", default=str(DATA_ROOT),
                          help="profiler 导出根目录（默认 $OPT_DISCOVERY_DATA_ROOT 或 ./exports）")
    p_analyze.add_argument("--workloads-dir", default=str(DEFAULT_WORKLOADS))
    p_analyze.add_argument("--summary-mapping", default=str(SUMMARY_MAPPING))
    p_analyze.add_argument("--rules", default=str(DEFAULT_RULES))
    p_analyze.add_argument("--thresholds", default=None)
    p_analyze.add_argument("--rounds", default="1-4")
    p_analyze.add_argument("--rank", type=int, default=0)
    p_analyze.add_argument("--top", type=int, default=12)
    p_analyze.add_argument("--out", default="out/steady")
    p_analyze.add_argument("--skip-timeline", action="store_true", help="跳过 TASK/CANN_API 空洞归因")
    p_analyze.add_argument("--source-map", action="store_true", help="索引源码并回填 source_refs")
    p_analyze.add_argument("--repos", default=",".join(DEFAULT_REPOS))
    p_analyze.add_argument("--no-source-cpp", action="store_true")
    p_analyze.add_argument("--binding", default=None, help="torch_binding.cpp 路径")
    p_analyze.add_argument("--no-bindings", action="store_true")
    p_analyze.add_argument("--device-profile", default=None,
                           help="micro-benchmark 设备档案 JSON（默认按 ruleset 的 device_profile_id 自动查找）")
    p_analyze.set_defaults(func=cmd_analyze)

    p_diag = sub.add_parser("diagnose", help="对已解析的 OperatorTable 运行规则诊断")
    p_diag.add_argument("--table", required=True, help="operator_table.json 路径")
    p_diag.add_argument("--rules", default=str(DEFAULT_RULES))
    p_diag.add_argument("--thresholds", default=None, help="标定覆盖文件（默认 configs/thresholds/...）")
    p_diag.add_argument("--out-dir", default="out/diagnosis")
    p_diag.set_defaults(func=cmd_diagnose)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
