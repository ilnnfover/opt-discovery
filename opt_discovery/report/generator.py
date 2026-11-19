"""Render a human-readable ``report.md`` from the JSON artifacts.

Input is the directory produced by ``opt_discovery.cli analyze``:

    out/steady/
      hotspots.json
      compare_<a>_vs_<b>.json
      <workload_id>/
        operator_table.json
        diagnosis.json
        timeline.json      (optional)
        opportunities.json

The generator never recomputes metrics; it only formats existing numbers and
keeps every caveat the pipeline produced.  Anything missing is written as
"未产出" instead of being silently skipped.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["build_report", "write_report"]


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def _num(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "-" if value is None else f"{value:.{digits}f}{suffix}"


def build_report(
    out_dir: str | Path,
    *,
    title: str = "Optimization Opportunity Report",
    artifacts: set[str] | None = None,
) -> str:
    """``artifacts``：本轮 run_manifest 登记的相对路径集合；None 表示兼容旧目录。"""
    root = Path(out_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"{root}: 目录不存在")

    def _registered(relative: str) -> bool:
        return artifacts is None or relative in artifacts

    def _load_rel(relative: str) -> dict[str, Any] | None:
        return _load(root / relative) if _registered(relative) else None

    workloads = sorted(
        p
        for p in root.iterdir()
        if p.is_dir() and _registered(f"{p.name}/operator_table.json")
    )
    tables = {w.name: _load(w / "operator_table.json") for w in workloads}
    diagnoses = {w.name: _load_rel(f"{w.name}/diagnosis.json") for w in workloads}
    timelines = {w.name: _load_rel(f"{w.name}/timeline.json") for w in workloads}
    comms = {w.name: _load_rel(f"{w.name}/comm.json") for w in workloads}
    coverages = {w.name: _load_rel(f"{w.name}/graph_coverage.json") for w in workloads}
    opportunities = {w.name: _load_rel(f"{w.name}/opportunities.json") for w in workloads}
    hotspots = _load_rel("hotspots.json")
    comparisons = [
        data
        for path in sorted(root.glob("compare_*.json"))
        if _registered(path.name) and (data := _load(path))
    ]

    lines: list[str] = []
    add = lines.append

    add(f"# {title}")
    add("")
    add(f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    add(f"数据目录：`{root.resolve()}`")
    add("")
    if any((tables[name] or {}).get("meta", {}).get("is_synthetic") for name in tables):
        add("> **警告**：以下结果包含合成数据，不代表任何真实性能指标。")
        add("")

    # -- 1. 数据与窗口 ----------------------------------------------------
    add("## 1. 数据与窗口")
    add("")
    add("| Workload | 阶段/模式 | 解析行数 | 窗口轮次 | 窗口时长 | 设备忙 | 窗口质量 | 告警 |")
    add("|---|---|---|---|---|---|---|---|")
    for name in tables:
        table = tables[name] or {}
        meta = table.get("meta", {})
        extra = meta.get("extra", {})
        window = extra.get("window") or {}
        quality = extra.get("window_quality") or {}
        workload = table.get("workload", {})
        execution = workload.get("execution", {})
        label = f"{execution.get('phase', '?')}/{execution.get('mode', '?')}"
        busy = quality.get("busy_ratio")
        degraded = quality.get("degraded")
        add(
            f"| `{name}` | {label} | {meta.get('row_count', '-'):,} | "
            f"{window.get('rounds', '-')} | "
            f"{_num(window.get('duration_us', 0) / 1e6 if window else None, 3, ' s')} | "
            f"{_pct(busy)} | "
            f"{'degraded' if degraded else 'ok' if quality else '未产出'} | "
            f"{len(meta.get('warnings', []))} |"
        )
    add("")

    # -- 2. 时间线证据 ----------------------------------------------------
    add("## 2. 时间线与瓶颈方向")
    add("")
    add("| Workload | 设备空闲 | ACL 覆盖空闲 | 下发/task | comm 未重叠占窗口 | 方向性提示 |")
    add("|---|---|---|---|---|---|")
    for name, timeline in timelines.items():
        if not timeline:
            add(f"| `{name}` | 未产出 | - | - | - | - |")
            continue
        add(
            f"| `{name}` | {_pct(timeline.get('device_idle_ratio'))} | "
            f"{_pct(timeline.get('acl_coverage_of_analyzed_idle'))} | "
            f"{_num(timeline.get('submit_per_task'))} | "
            f"{_pct(timeline.get('comm_exposed_share_of_span'))} | "
            f"`{timeline.get('bottleneck_hint', '?')}` |"
        )
    add("")
    if any(coverages.values()):
        add("### Graph 覆盖率（FULL/PIECEWISE 覆盖到哪一步）")
        add("")
        add("| Workload | 模式 | 图内任务 | 按任务 | 按时间 | 主要 fallback |")
        add("|---|---|---|---|---|---|")
        for name, coverage in coverages.items():
            if not coverage or not coverage.get("total_calls"):
                add(f"| `{name}` | - | 无 in_graph 标记 | - | - | - |")
                continue
            top = coverage.get("top_fallback") or []
            fallback = ", ".join(
                f"`{item['operator']}`({(item['total_us'] - item['in_graph_us']) / 1000:.0f}ms)"
                for item in top[:3]
            )
            add(
                f"| `{name}` | {coverage['mode']}/{coverage['graph_mode']} | "
                f"{coverage['in_graph_calls']:,}/{coverage['total_calls']:,} | "
                f"{_pct(coverage['task_ratio'])} | {_pct(coverage['time_ratio'])} | "
                f"{fallback or '-'} |"
            )
        add("")
        add("> 任务占比 ≠ 时间占比：图外任务通常更长（fallback/未捕获），图收益只作用于图内部分。")
        add("")

    if any(comms.values()):
        add("### 通信量（跨执行模式唯一可比）")
        add("")
        add("| Workload | 字节(去重) | 唯一任务 | 单任务 | 平均带宽 |")
        add("|---|---|---|---|---|")
        for name, comm in comms.items():
            if not comm or not comm.get("available"):
                add(f"| `{name}` | - | - | - | - |")
                continue
            add(
                f"| `{name}` | {_num(comm['mib_total'], 1, ' MiB')} | "
                f"{comm['unique_tasks']:,} | {_num(comm['kib_per_task'], 1, ' KiB')} | "
                f"{_num(comm['bandwidth_mean_gbps'], 1, ' GB/s')} |"
            )
        add("")
        add("> 记录数在 Eager/Graph 之间不可比（Graph 会合并 HCCL 记录），只能比字节量。")
        add("")

    timeline_notes = {
        note
        for timeline in timelines.values()
        if timeline
        for note in timeline.get("notes", [])
        if "重叠分析" in note or "通信" in note
    }
    for note in sorted(timeline_notes):
        add(f"- {note}")
    add("")

    # -- 3. 热点 ----------------------------------------------------------
    add("## 3. 算子热点（累计时间占比）")
    add("")
    if hotspots and hotspots.get("rows"):
        header = "| 算子 | " + " | ".join(_short(w) for w in hotspots["workloads"]) + " |"
        add(header)
        add("|---|" + "---|" * len(hotspots["workloads"]))
        for row in hotspots["rows"][:15]:
            cells = []
            for workload_id in hotspots["workloads"]:
                cell = row["workloads"].get(workload_id)
                cells.append(_pct(cell["share"]) if cell else "-")
            add(f"| `{row['operator']}` | " + " | ".join(cells) + " |")
        add("")
        add(f"> {hotspots.get('note', '')}")
    else:
        add("未产出 `hotspots.json`")
    add("")

    # -- 4. 诊断 ----------------------------------------------------------
    add("## 4. 瓶颈诊断")
    add("")
    for name, diagnosis in diagnoses.items():
        if not diagnosis:
            add(f"### `{name}`\n\n未产出 `diagnosis.json`\n")
            continue
        summary = diagnosis.get("summary", {})
        add(f"### `{name}`")
        add("")
        add(f"- 候选总数：{summary.get('candidates_total', 0)}，按瓶颈："
            f"`{summary.get('candidates_by_bottleneck', {})}`")
        add(f"- 按可信级别：`{summary.get('candidates_by_level', {})}`")
        blocked = summary.get("blocked_rules", [])[:5]
        if blocked:
            add("- 无法判定的规则（缺指标）：")
            for item in blocked:
                add(f"  - `{item['rule_id']}`：覆盖 {item['operators']} 个算子，缺 "
                    f"{', '.join(item['missing'])}")
        missing = summary.get("missing_facts_top", [])[:5]
        if missing:
            add("- 最该补采的指标："
                + "，".join(f"`{m['fact']}`({m['blocked_rules']})" for m in missing))
        add("")

    # -- 5. 机会 ----------------------------------------------------------
    add("## 5. 优化机会（按优先级分数排序）")
    add("")
    add("| ID | 算子 | Workload | 瓶颈 | 级别 | 分数 | 影响 | 缺失证据 |")
    add("|---|---|---|---|---|---|---|---|")
    for name, payload in opportunities.items():
        for item in (payload or {}).get("opportunities", [])[:5]:
            scores = item.get("scores") or {}
            add(
                f"| {item['opportunity_id']} | `{item['operator']}` | `{name}` | "
                f"{item['bottleneck']} | {item['evidence_level']} | "
                f"{_num(scores.get('value'), 4)} | {_num(scores.get('impact'), 3)} | "
                f"{len(item.get('missing_evidence', []))} |"
            )
    add("")
    add("> 分数为占位模型：impact 取累计时间占比（非关键路径），"
        "feasibility/cost 为经验占位值，详见各机会的 `score_basis`。")
    add("")

    # -- 5.5 实测反馈 ------------------------------------------------------
    measured_rows = []
    for name, payload in opportunities.items():
        for item in (payload or {}).get("opportunities", []):
            measured = item.get("measured") or {}
            if measured:
                measured_rows.append((name, item, measured))
    if measured_rows:
        add("## 5.5 实测反馈（bench A/B 回填）")
        add("")
        add("| 机会 | 算子 | Workload | 状态 | 级别 | 中位延迟变化 | 判定 |")
        add("|---|---|---|---|---|---|---|")
        for name, item, measured in measured_rows[:20]:
            delta = measured.get("latency_median_delta_pct")
            add(
                f"| {item['opportunity_id']} | `{item['operator']}` | `{name}` | "
                f"{item.get('status')} | {item.get('evidence_level')} | "
                f"{'-' if delta is None else f'{delta:+.1f}%'} | {measured.get('verdict')} |"
            )
        add("")
        add("> 工作负载级 A/B 不能归因到单个算子；只有单变量实验（--operator）才升级证据级别。")
        add("")

    # -- 6. 对比 ----------------------------------------------------------
    add("## 6. 跨 Workload 对比")
    add("")
    for data in comparisons:
        add(f"### `{data['a']}` → `{data['b']}`")
        add("")
        add(f"- 可比：{data.get('comparable')}；同窗口：{data.get('same_window')}")
        for reason in data.get("reasons", []):
            add(f"- 不可比原因：{reason}")
        for warning in data.get("warnings", []):
            add(f"- ⚠️ {warning}")
        for note in data.get("observations", []):
            add(f"- {note}")
        if data.get("deltas"):
            add("")
            add("| 算子 | Δshare | calls 比 | 单次耗时比 |")
            add("|---|---|---|---|")
            for delta in data["deltas"][:8]:
                share = delta.get("delta_share")
                add(
                    f"| `{delta['operator']}` | "
                    f"{'-' if share is None else f'{share * 100:+.2f}%'} | "
                    f"{_num(delta.get('calls_ratio'))} | {_num(delta.get('avg_ratio'))} |"
                )
        add("")

    # -- 7. 证据缺口 ------------------------------------------------------
    add("## 7. 仍未解决的证据缺口")
    add("")
    gaps = sorted(
        {
            note
            for diagnosis in diagnoses.values()
            if diagnosis
            for note in diagnosis.get("notes", [])
            if "Timeline" in note or "缺失" in note or "未采集" in note
        }
    )
    for note in gaps[:10]:
        add(f"- {note}")
    if not gaps:
        add("- 无")
    add("")
    return "\n".join(lines)


def write_report(
    out_dir: str | Path,
    target: str | Path | None = None,
    *,
    title: str = "Optimization Opportunity Report",
    artifacts: set[str] | None = None,
) -> Path:
    root = Path(out_dir)
    text = build_report(root, title=title, artifacts=artifacts)
    path = Path(target) if target else root / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _short(workload_id: str) -> str:
    for marker in ("prefill", "decode"):
        index = workload_id.rfind(marker)
        if index >= 0:
            return workload_id[index:]
    return workload_id[:14]
