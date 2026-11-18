# AE-00 仓库基线与差距审计

> 审计日期：2026-11-18
> 范围：当前工作树（公开仓库 + 本地授权数据）。文中不记录机器绝对路径、密钥或私有模型路径。

## 1. 基线与环境

| 项 | 值 |
|---|---|
| 基线 commit | `1785df806af30075caea409a8b64949904edfb92`（Initial commit, 2026-11-17） |
| 分支 | `main` |
| 起始工作树 | 无已跟踪文件改动；仅 `docs/tasks/`（任务书）为未跟踪文件；`out/`、`export_only_prof_dir/` 已在 `.gitignore` |
| Python | 3.12.13（`/usr/local/python3.12.13/bin/python`） |
| 运行时依赖 | PyYAML 6.0.3 |
| 测试依赖 | pytest 8.3.2 |
| 本轮新增检查工具 | ruff 0.17.5、mypy 2.4.1（写入 `[project.optional-dependencies].dev`） |
| 包结构 | 48 个 `opt_discovery/**/*.py`；19 个功能测试文件 + 14 个新增回归测试文件（37 个用例） |

`requires-python = ">=3.10"`，但 README 推荐 3.12。本机只有 3.12，3.10 仅通过
`ruff target-version=py310` / `mypy python_version=3.10` 与 CI 矩阵声明，**未在本机实际运行**
（见 §8 阻塞）。

## 2. 执行记录（本轮改动前的基线）

| 检查 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 单元/契约测试 | `python -m pytest` | 0 | **155 passed, 15 skipped**（README 的 “104 passed/119 用例” 已过期） |
| 配置自检 | `python -m opt_discovery.cli validate` | 0 | 11 规则 / 16 阈值（measured 2、declared 8、assumed 6）/ 4 Workload |
| 合成数据全链路 | `python -m opt_discovery.cli demo --out <tmp>` | 0 | 4 组 Workload 均产出候选（全部 `hypothesis`，`is_synthetic=True`） |
| 真实数据回归 | `OPT_DISCOVERY_DATA_ROOT=<统一补采根> python -m pytest tests/test_real_data.py` | **1** | 6 passed / 9 skipped / **4 failed**（详见 §4-D5） |
| 单文件真实回归 | `python -m pytest tests/test_real_data.py`（不设数据根） | 0 | 15 skipped（缺数据，符合预期） |

数据可用性：
- 未设置 `OPT_DISCOVERY_DATA_ROOT` 时默认 `./exports`，仓库内不存在 → 真实用例整组 skip。
- 本地授权环境存在四组 2026-11-15 统一补采导出（目录名与
  `configs/workloads/qwen35-35b-a3b-910b/*.yaml` 的 `labels.export_dir` 一致，含 `workload.log`）。
  本轮以只读方式使用，未重新采集、未占用 NPU。
- 每个导出目录还残留一次「空 PROF 会话」（只有 `api_statistic`/`npu_module_mem`，无算子级 CSV），
  属于采集产物的真实形态，解析/测试必须显式跳过它而不是当成 rank。

## 3. 实际结构（按数据流）

```
collectors/profiler_dir.py   discover_bundle/discover_all
  -> PROF_*/mindstudio_profiler_output/op_summary*.csv（自然序切片）
  -> PROF_*/device_*/sqlite/op_counter.db（回填）
parsers/csv_mapping.py       MappingCsvParser.parse（流式、窗口过滤、聚合、单位、缺失标记）
parsers/sqlite_op_report.py  op_report 回填（无时间戳/无流水指标）
schemas/{metrics,operator,workload,evidence,opportunity}.py  数据契约
analyzer/{window,timeline,hotspot,compare,comm,graph_coverage,contract}.py
diagnosis/{facts,engine,rules,thresholds}.py                 事实/规则/阈值覆盖
discovery/generator.py       规则候选 -> Opportunity（证据等级上限 probable）
experiment/{runner,analyzer,feedback,calibration,validation}.py
report/{generator,summary_csv}.py
cli.py                       22 个子命令（编排层）
```

关键调用链（新增 Agent 最容易踩的位置）：

- `MappingCsvParser.parse` → `_accumulate` → `_Group.to_record`：**单位换算与缺失处理的唯一入口**；
- `analyzer/timeline.summarize` → `_rank_intervals` → `load_op_intervals`：busy 来源优先级
  `op_summary` > `TASK`（回退必须告警），多 rank 用并集；
- `analyzer/compare.compare_tables` → `WorkloadSpec.comparable_with` + `_summarize`；
- `diagnosis/engine` → `facts.build_facts` → 规则 required/supporting → 证据等级封顶；
- `cli.cmd_analyze`：逐 Workload `try/except` 后 `continue`（见 §4-D6）。

## 4. 基线发现的真实缺陷（均已复现并修复，除非另注）

| ID | 缺陷 | 复现方式 | 影响 | 处置 |
|---|---|---|---|---|
| D1 | 循环导入：`import opt_discovery.parsers` 失败（`parsers → analyzer → timeline → parsers.csv_mapping`） | `python -c "import opt_discovery.parsers"` | 单独运行某测试文件（如 `tests/test_real_data.py`）直接 ImportError；全量 `pytest` 因导入顺序偶然通过 | `csv_mapping` 把 `Window` 改为 `TYPE_CHECKING` 导入；`import` 冒烟检查 |
| D2 | `collection_audit` 缺 `contract_sha256` 时 `sorted({None, str})` 抛 `TypeError` | 两个 manifest，其中一个无该字段 | 审计崩溃而非给出失败结论 | 缺失值显式记为 `<missing>` 并判定失败；新增回归用例 |
| D3 | `communication_time_us` 未做 ns→µs 换算（`total_time_us` 已换算） | COMMUNICATION 行 `110462688` → 额外指标变成 1.1e8 µs | 该指标下游（`communication_over_device` 等）被放大 1000x | 在列映射中声明 `scale: 0.001`，解析器按源契约只缩放一次；新增 R08 回归 |
| D4 | `__all__` 声明不存在的符号（`DEFAULT_MAX_UNDERFEED`、`BaseParser`） | `from opt_discovery.analyzer.window import *` | 用户按导出名导入即失败 | 修正 `__all__`（`DEFAULT_MIN_BUSY_RATIO`、删除 `BaseParser`） |
| D5 | 真实数据测试断言写死旧口径（69 行 / 771 calls / >30000 行 / 必须有 `mac_time_us` / 固定 2 分片 / rank1 必缺 CSV） | 对统一补采导出运行真实用例 | 4 失败 + 9 被空 PROF 会话连带 skip，真实覆盖被静默丢弃 | 改为「与磁盘/源列独立核对」+ 33/46 列契约 + 空会话识别；见 `regression-matrix.md` |
| D6 | `cli.cmd_analyze` 吞掉逐 Workload 异常后仍 `return 0` | 缺 `workload.log` / 解析失败 | 「退出 0 + 部分产物」被误判为成功 | **未修**（AE-03 范围：strict 运行契约与 manifest） |
| D7 | `rank_duration_inconsistent` 只写进 timeline notes，`compare` 不消费 | 构造两 rank 时长差 4x | 质量异常仍可进入跨模式对比 | **未修**（AE-03/AE-07 范围），已用 R10 锁定「不得解释为负载不均」 |
| D8 | 样本门槛两套：`calibrate(min_samples=3)` 与设备档案路径硬编码 `DEFAULT_MIN_SAMPLES=12` | `calibrate(..., device_profile=...)`，4 样本 | README 写「分位数标定至少 12 个样本」，与函数默认值不一致 | **未统一**（策略变更需单独评审）；回归已锁定设备档案路径的 12 门槛 |
| D9 | `load_host_intervals` 用 `fetchall()` 物化窗口内全部 Host 事件 | decode 单窗口 357 万条 | 内存峰值高于「流式」表述 | **未修**（AE-06 性能基线），见 INV-12 |
| D10 | `OperatorRecord.__post_init__` 会静默把非 `us` 时间指标归一 | 构造 `Metric(2, "ms")` 的 record | 单位换算出现第二个边界，审计困难 | **未修**（AE-04 统一标准化边界） |

## 5. 不变量覆盖矩阵（改动后）

| ID | 状态 | 强制机制（文件:符号） | 证据（测试节点） |
|---|---|---|---|
| INV-01 缺失≠0 | enforced | `schemas.metrics.Metric`（无占位默认）、`OperatorTable.total_operator_time_us` 返回 `None`、`csv_mapping._to_float` 空值表、`missing_extra_metrics` | `test_schemas.py::test_operator_table_total_time_returns_none_without_data`、`test_r02`、`test_r09` |
| INV-02 来源不可伪造/提升 | enforced | `Provenance` 枚举 + `weakest_provenance`；`diagnosis.engine` 证据封顶；`diagnosis.thresholds.Provenance` | `test_schemas.py::test_weakest_provenance`、`test_diagnosis.py::test_measured_thresholds_raise_to_probable_but_not_confirmed` |
| INV-03 单位只在边界换算 | **partially_enforced** | `csv_mapping.COMMUNICATION_DURATION_SCALE` + `ExtraMetricSpec.scale`；`timeline` 复用同常量。残留 D10 | `test_r08`（含漏声明 scale 的反例）、`test_schemas.py::test_time_unit_conversion` |
| INV-04 图/记录数≠守恒量 | enforced | `comm.summarize_comm` 按 `globalTaskId` 去重字节；`compare` 的图收缩告警 | `test_comm.py::test_dedup_by_task`、`test_analyzer.py::test_compare_uses_comm_bytes_instead_of_call_count`、`test_r07` |
| INV-05 质量差/不可比不得出结论 | **partially_enforced** | `analyzer.contract.compare_contracts`（`incomparable`）、`compare_tables`（`comparable=False` 不产 deltas）、window `degraded`。缺口 D6/D7 | `test_p0.py::test_contract_fails_on_phase_mismatch`、`test_r10` |
| INV-06 区间裁剪 + 并集 | enforced | `analyzer.window.IntervalUnion`、`timeline._merge`、Host 事件二分归因取并集、overlap 裁剪 | `test_timeline.py::test_gap_attribution_uses_union_not_sum`、`test_r12` |
| INV-07 不得用采集误差升级证据 | **partially_enforced** | 规则≤probable、`feedback` 的 ±5% 判定；无统计显著性检验 | `test_diagnosis.py::test_assumed_threshold_is_hypothesis_only`、`test_feedback.py` |
| INV-08 规则最高 probable | enforced | `diagnosis.engine` 置信度/等级封顶；`confirmed` 需 kernel+端到端 | `test_diagnosis.py::test_measured_thresholds_raise_to_probable_but_not_confirmed`、`test_opportunity.py` |
| INV-09 工作负载级 A/B 不归因算子 | enforced | `experiment.feedback.apply_feedback` 的 scope 分支 | `test_feedback.py::test_workload_level_result_does_not_attribute_to_operator` |
| INV-10 合成数据不得冒充实测 | **partially_enforced** | `ParseMeta.is_synthetic` → `DiagnosisReport.is_synthetic`；`compare` 警告一侧合成。`feedback`/`generator` 未校验 | `test_pipeline.py::test_cli_demo_writes_outputs` |
| INV-11 缺失/回退/不充分可机读 | **partially_enforced** | `ParseMeta.warnings` + `missing_columns` + 新增 `missing_extra_metrics`；TASK 回退 note；阈值 `provenance/basis` | `test_csv_parser.py::test_unmapped_columns_are_reported`、`test_r09`、`test_supplementary_gates` |
| INV-12 有界内存/流式 | **partially_enforced** | `MappingCsvParser` 逐行；但 op intervals 排序需保存列表，Host 事件 `fetchall`（D9） | 无性能基线（AE-06） |

“partially_enforced” 全部在 §8 列出收敛任务，不视为已保护。

## 6. README 12 类约束的基线覆盖（改动前 → 改动后）

详细矩阵见 `docs/engineering/regression-matrix.md`。摘要：

| ID | 改动前 | 改动后 | 新增/补齐点 |
|---|---|---|---|
| R01 分片 | 间接（2 片、字典序与自然序同结果） | enforced | 自然序 + 分片≡单文件 + 只读首片反例 |
| R02 缺 HCCL | 仅真实数据 | enforced | 合成 fixture + 矩阵 note 断言 |
| R03 Core Type | 直接 | enforced | 加权平均 vs 平均值的平均（显式负向） |
| R04 占比>1 | 直接（诊断层） | enforced | 事实层不 clamp + 单流水 2.6 |
| R05 rank 回填/不可合并 | 回填仅真实数据；「不可合并」零测试 | partially | CLI 闸门三种状态 + sqlite 能力缺失标记 |
| R06 MSTX | 缺表 | enforced | 仅枚举表、开启但空两种状态 |
| R07 算子收缩 | 间接 | partially | presence-only + 无收益字段 + 线索措辞 |
| R08 ns 单位 | 时间线有；`communication_time_us` **有缺陷** | enforced | 精确刻度 + 只缩放一次 + 漏配置反例 |
| R09 33/46 列 | 未知列有；缺列无标记、FLOPs 无断言 | enforced | `missing_extra_metrics` + flop 单位 + 双格式 |
| R10 时长截断 | 间接（TASK 构造） | partially | op_summary 两 rank + 不得解释为不均；下游比较限制待 AE-03 |
| R11 利用率分母 | **零覆盖** | enforced | aicore_time 分母 + 缺分母不伪造 |
| R12 epoch 伪 gap | 直接 | enforced | 窗口内真实 gap 与 epoch 无关 |

## 7. 兼容性与行为变化（本轮）

1. `ParseMeta.extra` 类型由 `Mapping` 改为 `dict`（解析器需要增量写入）；JSON 形态不变。
2. **`communication_time_us` 数值缩小 1000 倍**（修正 ns→µs）。所有使用该指标的
   `communication_over_device` 等派生事实随之变化；`op_statistic` 口径不受影响。
   这是缺陷修复，不是口径变更；独立预期见 `test_r08`。
3. 新增 `meta.extra.missing_extra_metrics` 与告警码 `missing_extra_metric`（只增不减）。
4. 列映射新增可选字段 `extra_metrics[].scale`（缺省 1.0，向后兼容）。
5. `__all__` 修正（`DEFAULT_MIN_BUSY_RATIO`；`BaseParser` 移除）。若外部代码导入过
   这两个错误名字，导入会失败——它们本来就不存在，属于修正。
6. 真实数据测试断言改为与源文件独立核对（更严格，不是放宽）。

## 8. 未完成 / 阻塞

| 项 | 原因 | 解除条件 |
|---|---|---|
| Python 3.10 本机执行 | 环境只提供 3.12 | 在 3.10 解释器或 CI 上运行 `make verify` |
| AE-03 strict 运行契约 | 不在本轮范围（D6/D7） | 按任务书实现 `--strict` + `run_manifest.json` |
| AE-04 统一 Reader / D10 | 不在本轮范围 | 收敛 `csv_mapping` 与 `OperatorRecord` 的单位边界 |
| AE-05 架构/领域 Lint | `make check` 尚未包含依赖规则 | 实现检查器后接入 `make check` |
| AE-06 属性测试/真实趋势回归 | 不在本轮范围 | Hypothesis + golden snapshot + 性能基线 |
| NPU 重新采集 | 未授权，且无必要 | 仅在需要复现截断/新硬件时申请 |
| CSV 缺失 rank 的真实回填场景 | 统一补采后两 rank 均有 CSV | 保留 CPU 回归；若复现历史导出则重新启用真实用例 |
| 阈值门槛统一（D8） | 涉及策略语义，需单独评审 | 决定以 12 为默认或改 README，并迁移 |

## 9. 后续任务顺序（建议）

1. **AE-03**：strict 模式、`run_manifest.json`、`analyze/compare` 状态与退出码（解决 D6）。
2. **AE-04**：把 `Window/Interval` 与「op_summary 实例事件」标准化到稳定边界（解决 D10、INV-03）。
3. **AE-05**：依赖/领域 Lint，并接入 `make check`。
4. **AE-06**：Hypothesis 属性测试 + 真实数据趋势回归 + 有界内存基线（解决 D9/INV-12）。
5. **AE-07**：统一证据升级 API（解决 D7 的下游限制）。
