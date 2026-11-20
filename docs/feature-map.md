# Opt-Discovery Feature Map

> 目标：让新 Agent 从一个目标直接找到**真实命令、实现、输入输出、测试节点、环境前提和验收产物**。
> 只记录当前工作树存在的入口；不存在的命令不写入。历史问题与测试的逐条映射见
> [`docs/engineering/regression-matrix.md`](engineering/regression-matrix.md)。

## 1. 能力地图（按目标）

| 目标 | 入口 | 公共实现 | 输入 | 输出 / 验收产物 | 代表测试节点 | 环境前提 |
|---|---|---|---|---|---|---|
| 配置自检 | `python -m opt_discovery.cli validate` | `opt_discovery/cli.py:cmd_validate`、`diagnosis/rules.py`、`parsers/csv_mapping.py`、`schemas/workload.py` | `configs/rules/*.yaml`、`configs/workloads/**/*.yaml`、`configs/parsers/*.yaml`、`configs/thresholds/*` | stdout 摘要；无文件产物 | `tests/test_pipeline.py::test_cli_validate_ok`、`test_config_workloads_load`；`tests/test_csv_parser.py::test_shipped_mappings_are_verified` | CPU、无真实数据 |
| 目录发现与解析 | 独立：`parse-csv`、`parse-sqlite`；组合：`analyze`、`demo` | `collectors/profiler_dir.py`、`collectors/workload_log.py`、`parsers/csv_mapping.py`、`parsers/sqlite_op_report.py`、`parsers/synthetic.py` | `PROF_*/mindstudio_profiler_output/op_summary*.csv`（自然序分片）；无 CSV 时 `device_*/sqlite/op_counter.db`；Workload YAML；列映射 YAML | `operator_table.json`（`parse-csv`/`parse-sqlite` 可指定 `--out`；`analyze` 写到 `<out>/<workload>/operator_table.json`） | `tests/test_collectors.py`、`tests/test_csv_parser.py`、`tests/test_events.py`、`tests/regression/test_r01_slice_order.py`、`test_r03_core_type_rollup.py`、`test_r08_communication_unit.py`、`test_r09_export_columns.py` | 真实导出；`demo` 用合成数据 |
| 轮次 / 稳态窗口 | 独立：`rounds`；被 `parse-csv`、`timeline`、`check-ranks`、`analyze` 使用 | `collectors/workload_log.py`、`analyzer/window.py` | 导出目录下的 `workload.log`（`ACTIVE_ROUND` 边界） | stdout；`analyze` 写入 `meta.extra.window` / `window_quality` | `tests/test_collectors.py::test_round_window_uses_harness_timestamps`、`tests/test_p0.py::test_parse_rounds_reads_harness_markers`、`tests/property/test_contracts.py::test_selected_rounds_window_covers_min_max_of_selected` | 真实 `workload.log`；无日志时 `analyze` 记 `missing_run_log` |
| Timeline（设备空洞归因） | 独立：`timeline`；组合：`analyze`（默认执行，`--skip-timeline` 可跳过） | `analyzer/timeline.py`、`collectors/mstx.py` | 导出目录 / `msprof_*.db`；可选 `MsprofTx`（MSTX）表；workload YAML；轮次窗口 | 独立命令 stdout 或 `--out`；`analyze` 写 `<out>/<workload>/timeline.json` | `tests/test_timeline.py`、`tests/test_timeline_op_summary.py`、`tests/test_mstx.py`、`tests/test_events.py`、`tests/regression/test_r06_mstx_enum_only.py`、`test_r10_truncated_duration.py`、`test_r12_window_origin.py` | 真实 sqlite / `msprof_*.db`；MSTX 未采集时显式标记，不伪造同步原因 |
| 热点 | 无独立 CLI；`analyze` 末尾输出 + Python API | `analyzer/hotspot.py:operator_hotspots`、`hotspot_matrix`、`matrix_to_text`；`analyze` 汇总调用 | 内存中的 `OperatorTable`（`analyze` 解析结果） | `<out>/hotspots.json`；终端矩阵 | `tests/test_analyzer.py::test_hotspots_rank_and_share`、`test_hotspot_matrix_marks_absent_operator`；`tests/test_p0.py::test_summary_csv_matches_artifacts` | 先完成解析；无独立入口 |
| 诊断 / 机会 | 独立：`diagnose`；组合：`analyze`、`demo` | `diagnosis/engine.py`、`diagnosis/facts.py`、`diagnosis/rules.py`、`diagnosis/thresholds.py`、`discovery/generator.py`、`schemas/opportunity.py`、`schemas/evidence_gate.py` | 已解析的 `operator_table.json`（独立 `diagnose`）；或解析结果直接进入 `analyze` | `diagnosis.json`、`opportunities.json`；`demo` 同形态；终端候选与 blocked 规则 | `tests/test_diagnosis.py`、`tests/test_pipeline.py`、`tests/test_opportunity.py`、`tests/test_evidence_gate.py`、`tests/regression/test_r04_pipe_fraction.py`、`test_r07_graph_operator_shrink.py`、`test_r11_utilization_denominator.py` | CPU；普通规则最高 `probable`，`confirmed` 必须走验证契约 |
| 对比（Eager/Graph、Prefill/Decode） | 独立：`compare`（两个表）、`check-runs`（采集契约）；组合：`analyze` 自动生成同阶段/同模式配对 | `analyzer/compare.py`、`analyzer/contract.py`、`analyzer/rank_quality.py`、`schemas/quality.py` | 两个 `operator_table.json`；或 `analyze` 的表 | `<out>/compare_<a>_vs_<b>.json`、`contract.json`、`run_manifest.json`（`comparison_status`） | `tests/test_analyzer.py`、`tests/test_p0.py`、`tests/test_run_contract.py`、`tests/regression/test_r05_rank_merge.py`、`test_r07_graph_operator_shrink.py`、`test_r10_truncated_duration.py`、`tests/regression/test_quality_gate.py` | 两侧表；契约/质量闸门未通过时不输出可用速度差 |
| 通信量 | 独立：`comm`；组合：`analyze`（有 `msprof_*.db` 时） | `analyzer/comm.py` | `msprof_*.db` 的通信任务表 | 独立命令 stdout 或 `--out`；`analyze` 写 `<out>/<workload>/comm.json` | `tests/test_comm.py`、`tests/test_analyzer.py::test_compare_uses_comm_bytes_instead_of_call_count`、`tests/test_real_data.py::test_communication_only_exists_in_op_summary` | 真实 db；字节按 `globalTaskId` 去重，记录数不可当通信量 |
| Graph 覆盖率 | 独立：`graph-coverage`；组合：`analyze` 自动写 | `analyzer/graph_coverage.py` | 由 `op_summary` 解析出的 `OperatorTable`（需要 `Model ID` 汇总出的 `in_graph` 标记） | `graph_coverage.json`；独立命令 stdout | `tests/test_graph_coverage.py`、`tests/regression/test_r07_graph_operator_shrink.py`、`tests/test_real_unified_manifest.py::test_representative_graph_coverage_reports_task_and_time_separately` | 真实 `op_summary`；eager 模式 0% 是符合预期的“无图”，不是缺数据 |
| 源码映射 | 独立：`source-map`；组合：`analyze --source-map` | `source_analysis/indexer.py`、`source_analysis/mapping.py`、`source_analysis/bindings.py` | `operator_table.json`；外部源码仓库（默认 `vllm-ascend`、`vllm`，可用 `OPT_DISCOVERY_REPOS` 或 `--repos`）；可选 `torch_binding.cpp` | `source_map.json`；命中时回填 `opportunities.json` 的 `source_refs` | `tests/test_source.py`、`tests/test_run_contract.py::test_optional_source_map_failure_keeps_success` | 需要外部源码；缺失时返回未命中原因，是**可选步骤**，失败不把核心分析判失败 |
| 反馈 / 证据升级 | 独立：`feedback` | `experiment/feedback.py`、`schemas/evidence_gate.py`、`schemas/opportunity.py` | `opportunities.json`；`--compare` 的 benchmark 比较（仅观察）或 `--validation` 的结构化 `ValidationBundle` + 引用产物 | 原地或 `--out` 更新 `opportunities.json`；JSON 审计字段是顶层 `evidence_gate`（Python 对象内部在 `extra["evidence_gate"]`） | `tests/test_feedback.py`、`tests/test_evidence_gate.py`、`tests/test_arch_lint.py::test_od004_feedback_cannot_jump_to_confirmed` | 结构化验证需要真实/人工实验与可读产物；工作负载级 A/B 不归因到单算子 |
| 阈值标定 / 留出验证 | 独立：`calibrate`、`validate-thresholds`；组合：`analyze` 自动叠加覆盖文件 | `experiment/calibration.py`、`experiment/validation.py`、`diagnosis/thresholds.py` | `out/steady/*/operator_table.json` 与可选 `timeline.json`；规则 YAML；设备档案 JSON | 阈值覆盖 YAML（`--out`）、`threshold_validation.json`；`validate` 读取覆盖 | `tests/test_calibration.py`、`tests/test_validation.py`、`tests/regression/test_supplementary_gates.py` | 至少 3 个 Workload；设备档案标定门槛 12 样本；不足时 `insufficient_samples`，不猜值 |
| Benchmark 执行 / 对比 | 独立：`bench`、`bench-compare` | `experiment/runner.py`、`experiment/analyzer.py` | `--harness` 脚本（打印 `ACTIVE_ROUND`）；两次 `benchmark.json` | `benchmark.json`、`bench_compare.json` | `tests/test_p0.py::test_run_benchmark_executes_stub_and_writes_json`、`test_compare_benchmarks_verdicts` | 真正执行 harness 需要模型/NPU 环境；`--print-only` 不执行 |
| 报告 / 汇总 / 清单 | 独立：`report`、`summary`、`check-manifest` | `report/generator.py`、`report/summary_csv.py`、`experiment/run_manifest.py` | `analyze` 输出目录 + `run_manifest.json`（或显式 `--manifest`；旧目录需 `--allow-legacy`） | `report.md`、`workload_summary.csv`、`operator_summary.csv`；清单校验 stdout/退出码 | `tests/test_p0.py::test_summary_csv_matches_artifacts`、`tests/test_run_contract.py::test_downstream_summary_and_report_ignore_stale_artifacts` | CPU；默认拒绝无清单的旧目录扫描 |
| 采集审计 | 独立：`check-collection`、`check-ranks` | `analyzer/collection_audit.py`、`analyzer/rank_quality.py` | 统一补采目录 / 单 Workload 导出 | stdout + 退出码（0/1/2）；`--out` 可写审计 JSON | `tests/test_collection_audit.py`、`tests/regression/test_r05_rank_merge.py`、`tests/test_real_unified_manifest.py` | 真实四组统一补采；缺数据为 `incomplete`/非零，不是 PASS |
| 合成流水线自测 | 独立：`demo` | `parsers/synthetic.py`、`diagnosis/engine.py`、`discovery/generator.py` | `--scenario`、Workload YAML | `<out>/<workload>/{operator_table,diagnosis,opportunities}.json` | `tests/test_pipeline.py::test_cli_demo_writes_outputs`、`test_rich_data_produces_candidates_and_opportunities` | CPU；**合成数据，不代表真实性能** |

## 2. 组合流程：`analyze` 实际做了什么

`python -m opt_discovery.cli analyze --exports-root <root> --out <new-dir> [--strict]`

1. `discover_all` 按 Workload YAML 的 `labels.export_dir` 找四组导出；缺目录/日志/rank 都会进入 `run_manifest.json` 的失败或 partial 状态。
2. 按 `--rounds` 从 `workload.log` 切稳态窗口；`MappingCsvParser.parse` 流式读取 `op_summary*.csv`。
3. 质量闸门：`empty_window`、`missing_rounds`、`low_round_coverage`、`rank_merge_*`、`timeline_unavailable` 等随表写入 `meta.extra.quality_flags`。
4. 每 Workload：`comm.json`（有 db）→ `timeline.json`（默认必需；`--skip-timeline` 变降级）→ `operator_table.json` → `graph_coverage.json` → `diagnosis.json` → `opportunities.json`。
5. 全部 Workload 后：可选 `source_map.json`；`hotspots.json`；同阶段/同模式自动对比 `compare_*.json`；`workload_summary.csv`/`operator_summary.csv`；最后写 `run_manifest.json`。
6. 普通模式退出码兼容旧行为；`--strict` 按 `success=0 / failed=1 / partial=3 / incomparable=4` 退出。缺导出入口返回 2。

`hotspots.json`、自动 `compare_*.json`、两个 summary CSV、timeline 标注、comm、graph coverage、可选 source map 在 `analyze` 组合流程里都会生成；其中 timeline、comm、graph coverage、source-map 以及 `summary` 也有独立 CLI，`hotspots.json` 和自动配对的 `compare_*.json` 目前只由 `analyze` 汇总生成。CPU-only 指不需要 NPU，不等于独立命令不需要既有的真实/fixture 输入。

## 3. 环境与前置条件

| 条件 | 影响的命令 | 说明 |
|---|---|---|
| CPU-only | `validate`、`demo`、`parse-csv`、`parse-sqlite`、`rounds`、`diagnose`、`compare`、`check-runs`、`graph-coverage`、`source-map`、`report`、`summary`、`check-manifest`、`feedback`（有现成输入时）、`calibrate`/`validate-thresholds`（有现成表时）、`make check`、`make verify` | 不需要 NPU 或真实数据；`demo` 使用合成数据 |
| 真实导出 | `timeline`、`comm`、`check-ranks`、`check-collection`、`analyze`、`make verify-real` | 根目录由 `OPT_DISCOVERY_DATA_ROOT` 指定；`verify_real.py` 缺数据必须 BLOCKED（直接脚本退出 2，Make 包装把任何非零折叠为 2），不是 PASS |
| 外部源码 | `source-map`、`analyze --source-map` | 默认在当前目录找 `vllm-ascend`、`vllm`；也可用 `OPT_DISCOVERY_REPOS`（路径分隔符分隔）或 `--repos`；可选 binding 文件 |
| MSTX | `timeline`、`analyze` 的同步归因 | 只有 `MsprofTx` 实际有事件才算采集；只有枚举表或空表时显式标记“没有开启 MSTX/未实际打点” |
| NPU / 模型 | `bench`（真实执行）、`feedback --validation` 引用真实实验产物 | 本轮不运行 NPU；未执行时验证摘要必须写 `NOT_RUN` 或 `BLOCKED`，不能写 PASS |
| 真实实验产物 | `feedback --validation` | `ValidationBundle` 引用的 baseline/candidate 产物必须可读且摘要一致；否则拒绝升级 |

## 4. 验收产物速查

| 产物 | 产生者 | 关键字段 |
|---|---|---|
| `operator_table.json` | 解析 / `analyze` / `demo` | `meta.extra.quality_flags`、`meta.extra.window`、`records[].calls/total_time_us` |
| `timeline.json` | `timeline` / `analyze` | `busy_source`、`device_idle_ratio`、`gap_*`、`gaps_by_class`、`notes` |
| `comm.json` | `comm` / `analyze` | `available`、`unique_tasks`、`bytes_total`、`dedup_ratio` |
| `graph_coverage.json` | `graph-coverage` / `analyze` | `available`、`task_ratio`、`time_ratio`、`top_fallback` |
| `diagnosis.json` | `diagnose` / `analyze` / `demo` | `summary.candidates_*`、`operators[].candidates`、`blocked_rules` |
| `opportunities.json` | `diagnose` / `analyze` / `demo` / `feedback` | `evidence_level`、`status`、`scores`、`instance_id`、`is_synthetic` |
| `hotspots.json` | `analyze` | `rows[].workloads[].share/calls/avg_us` |
| `compare_*.json` | `compare` / `analyze` | `comparable`、`reasons`、`warnings`、`total_ratio`、`deltas` |
| `run_manifest.json` | `analyze` / `compare` | `status`、`stages`、`workloads`、`quality_flags`、`comparison_status`、`artifacts` |
| `verification_summary.json` | `scripts/verify_summary.py` | 五态 `steps[]`、`claims`、`not_executed`、日志与 `artifact_diff` |
| `artifact_diff.json` | `scripts/compare_artifacts.py` | `status`、`changes[]`、`summary.changed`、忽略的噪声字段 |

## 5. 验证入口

```bash
make check                     # ruff + arch-lint + mypy + CLI validate
make verify DEMO_OUT=<new-dir> # pytest + synthetic demo
make verify-real               # Make 包装：任何非零子命令折叠为退出 2（含跳过）
python scripts/verify_real.py  # 直接入口：0 PASS / 1 FAIL / 2 BLOCKED / 3 SKIPPED
python scripts/verify_summary.py --out <new-dir> [--real] \
    [--artifacts-a <before> --artifacts-b <after>]
python scripts/compare_artifacts.py --before <before> --after <after> --out <diff.json>
```

- 历史问题回归矩阵：[`docs/engineering/regression-matrix.md`](engineering/regression-matrix.md)（R01–R12、补充闸门、故障注入）。
- 基线审计：[`docs/engineering/baseline-audit.md`](engineering/baseline-audit.md)。
- AE-07 终验与迁移：[`phase6/implementation-report.md`](engineering/phase6/implementation-report.md)、[`phase6/migration.md`](engineering/phase6/migration.md)。
- 机器可读验证摘要的五态定义见 `tools/verification.py` 顶部注释；真实数据、NPU 和完整分析未执行时必须保持 `NOT_RUN`/`BLOCKED`。

### 配对 before/after 产物（改变分析语义时）

1. 固定同一逻辑输入：相同 `OPT_DISCOVERY_DATA_ROOT`、Workload YAML、`--rounds`、`--rank`、阈值覆盖；只切换代码版本。
2. 用两个**新目录**分别跑 `analyze`（不要复用旧输出目录，避免旧文件混入）：
   `python -m opt_discovery.cli analyze ... --out /tmp/<case>-before` 与 `/tmp/<case>-after`。
3. 生成差异：`python scripts/compare_artifacts.py --before /tmp/<case>-before --after /tmp/<case>-after --out /tmp/<case>-diff.json`。
4. 私有数据上的配对只在授权环境本地执行；公开 CI 不访问私有导出，也不把产物差异设成新的质量闸门。
