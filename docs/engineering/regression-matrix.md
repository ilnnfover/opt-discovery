# AE-02 历史问题回归矩阵

> 错误目录来源：`README.md` 「已知数据约束」1–12。
> 组织方式：`tests/regression/`（74 个用例：R01–R12 共 36 个 + 质量闸门 15 个 + 端到端传播 1 个
> + 补充闸门 2 个 + verify-real 契约 20 个）。AE-03 已把 R05/R10 的 strict xfail 转为正式断言。
> CPU-only、无私有数据依赖。
> 所有 fixture 在测试运行时生成（`tests/regression/_helpers.py`），脱敏且小于 10 行/文件。
> 期望值来自独立算术（磁盘行数、Count 列求和、ns→µs、`total/calls`），不复用被测聚合逻辑。

## 1. R01–R12 映射

| ID | 历史错误 | 现有实现（关键符号） | 最小 fixture | 独立预期 | 反例 / 故障注入 | 不变量 | 状态 |
|---|---|---|---|---|---|---|---|
| **R01** | `op_summary` 按 100 万行切片，只读一片或按字典序合并 | `collectors/profiler_dir._natural_key`、`_build`；`parsers/csv_mapping.MappingCsvParser.parse`（按传入顺序流式） | 3 个分片文件 `slice_1/2/10`；6 行拆 3 片 vs 单文件 | 文件顺序 `[1,2,10]`；分片与单文件逐算子 `(calls,total)` 相等；`MatMul(3,300µs)`、`Add(3,180µs)` | 只读首片 → `(2,200)` 断言失败；字典序顺序断言失败 | INV-12 | enforced |
| **R02** | `op_statistic` 不含 HCCL，被当成完整通信时间 | `analyzer/hotspot.hotspot_matrix`（note）；`csv_mapping` 的 `when` 过滤 | 2 行 `op_statistic`（无 `hcom_*`） | 无通信记录、无 `communication_time_us`（None 而非 0）；矩阵 note 含「不含 HCCL/不可直接比较」 | 若把缺通信当 0 秒或伪造通信占比，断言失败 | INV-05/11 | enforced |
| **R03** | 同一 OP Type 按 Core Type 拆行，重复计数或用平均值的平均 | `csv_mapping._Group.add_bucket/to_record`（`avg=total/calls`，`DERIVED`） | 两行 `MatMulV3`（717/54 calls，实测 total） | calls=771、total=226478.259、avg=293.72…µs；且 ≠ `(224.423+1214.199)/2` | 平均值的平均 = 719.31 → `!=` 断言失败 | INV-01/03 | enforced |
| **R04** | 流水时间占比 >1 被 clamp 到 1 | `diagnosis/facts.build_facts`（不夹取）；`diagnosis/engine`（>1 追加说明） | record `total=100`，`aicore=150`、`vec=20`；另一例 `mte2=260` | `compute_pipe_time_fraction=1.5`、`mte2_time_fraction=2.6`，≠1.0 | 任意 clamp 会让等式与 `!=1.0` 失败 | INV-06/07 | enforced |
| **R05** | rank1 无 CSV / 回填口径不同 / 两 rank 不可合并 | `analyzer/rank_quality` 评估 → `QualityFlag(rank_merge_inconsistent)` 落盘 → `compare_tables` 闸门；`cli --strict` | 两 rank op_summary（时长 1x vs 4x）；rank1 仅 `device_1/sqlite/op_counter.db` | 4x → 评估不一致 → `comparable=False`、`deltas=[]`、reasons 含 code；`--strict` 退出 3；缺 rank → 2；sqlite → `口径不同/不可直接比较` | 静默合并 / compare 仍输出差异 / strict 仍 0 | INV-04/05 | enforced（AE-03，`test_quality_gate.py` + `test_quality_end_to_end.py` + `test_r05` strict） |
| **R06** | 只有 `ENUM_MSTX_EVENT_TYPE` 枚举表，被当成已采集；缺同步指标被当成实测 0 | `collectors/mstx.load_mstx`（只认 `MsprofTx`）；`timeline.summarize` 的 `mstx_available`/`silent_unattributed_us` | ①仅枚举表；②`MsprofTx` 空；③枚举表 + 200 µs 静默空洞的 timeline | ①`available=False` + 「没有开启 MSTX/无法进一步归因」；②`True` + 「未实际打点」；③`mstx_available=False`、`silent_unattributed_us>0`、无「同步等待合计」措辞 | 枚举表当数据 / 把缺失同步指标写成实测 0 → 断言失败 | INV-05/11 | enforced |
| **R07** | Graph 下算子集合收缩（71→44）被当成「算子未执行/已删除」 | `analyzer/compare.compare_tables`（`presence=only_a`、模式差异告警）；`tests/regression/_claims.py` 递归扫描键名与文本 | eager 有 `FusedBias`，graph 无 | `presence=only_a`、`share_b=None`、告警含「捕获/融合」「线索」；序列化前后均无 `saving/eliminat/收益/节省/消除/提速` | 向 observations / deltas 嵌套字段 / window 深层键注入「预计收益 8%」「saved_us」→ `assert_no_claims` 必须抛错 | INV-04/07 | enforced（收益类结论） |
| **R08** | `COMMUNICATION` 的 Task Duration 是 ns，被当 µs 或重复缩放 | `csv_mapping.COMMUNICATION_DURATION_SCALE` + `ExtraMetricSpec.scale`；`timeline.load_op_intervals` 复用 | 一行 `hcom_allReduce` `110462688` + 一行 `MatMul` 同值 | comm = 110462.688 µs（≠1.1e8、≠110.46）；kernel 不缩放；`communication_time_us` 只在通信行且同值 | 删除 mapping 的 `scale` → 回到 1.1e8（用例失败） | INV-03 | enforced（本轮修复 D3） |
| **R09** | 33/46 列导出差异、FLOPs 单位、未知列 | `csv_mapping._report_column_issues`（新增 `missing_extra_metrics`）、`configs/parsers/ascend_op_summary.yaml` | 33 列风格表头（含 `Block Num/aic_cube_fops/Mystery Col`）；46 列风格表头 | 缺 `mac/mte/cube_utilization` 有标记且指标为 None；`flops` 单位 `flop`、值不缩放；`block_num` 取 mean；未知列进 `unmapped_columns` | 缺列被静默吞掉 → `missing_extra_metric` 断言失败 | INV-01/11 | enforced |
| **R10** | decode-eager rank1 长 kernel 时长截断 → 误判「卡在等/负载不均」 | `timeline.summarize` 产出 `rank_duration_inconsistent` → `rank_quality_from_timeline` 转阻断标记 → `compare_tables` 闸门 | 两 rank 各 4 个任务（1000 µs vs 34 µs）；另一条直接挂 `rank_duration_inconsistent` 标记 | flag=True；notes 不出现「TP 两卡明显不对称」；标记进入表后 compare `comparable=False`、无 deltas | 走不对称解释 / compare 忽略标记 | INV-05 | enforced（AE-03，`test_quality_gate.py` + `test_quality_end_to_end.py`） |
| **R11** | 小算子 Task Duration < aicore_time → 利用率 >100% | `diagnosis/facts.build_facts`（分母 `aicore_time`） | `total=2.3µs`、`aicore=19.7µs`、`flops=1e9`、设备档案实测峰值 | util = 0.1797（= `1e9/19.7e-6/282.5e12`）；<1；用 Task Duration 会得 1.54 | 换分母 → 等式与 `<1` 同时失败；缺 `aicore_time` 时无该事实 | INV-01/03 | enforced |
| **R12** | epoch→首个忙区间被算作空洞（1e15 伪 gap）；显式窗口与回退窗口语义混用 | `timeline._gaps_from_merged`（cursor 从首个忙碌区间起）、`summarize(window=None)` 的回退统计 | ①窗口 500 µs、两段 100 µs 忙区间 + 50 µs 空洞、epoch 1.79e15 vs 0；②同一 TASK 数据分别用显式窗口与 `window=None` | ①`gap_count=1`、`gap_max=50`、`idle=300`，epoch 无关；②显式 span/idle=500/300，回退=250/50，两者空洞同为 50 µs | cursor 改为 0 → 两个用例失败；窗口混用 → span/idle 断言失败 | INV-06 | enforced |

## 2. 补充重点（已登记，不重复实现）

| 重点 | 测试节点 | 断言要点 | 不变量 |
|---|---|---|---|
| 多线程 Host ACL 事件用区间并集 | `tests/test_timeline.py::test_gap_attribution_uses_union_not_sum` | 100+80 并发事件取并集 130 µs，而非 180 µs；覆盖率 130/300 | INV-06 |
| `op_summary` 优先 + TASK fallback 告警 | `tests/test_timeline_op_summary.py::test_op_summary_preferred_over_task_table`、`::test_fallback_to_task_table_warns`、`::test_multi_rank_partial_fallback_warns_per_rank` | `busy_source` 正确；回退 note 点名 rank 且含「不可信」 | INV-05/11 |
| 通信任务按 `globalTaskId` 去重 | `tests/test_comm.py::test_dedup_by_task`、`::test_per_task_size_reveals_merged_records` | 3 行→2 任务；字节 3072；记录数差 ≠ 通信量减少 | INV-04 |
| 阈值样本门槛（设备档案路径 12） | `tests/regression/test_supplementary_gates.py::test_device_calibration_floor_is_twelve_samples` | 4/8 样本 → `insufficient_samples`，`proposed_value=None`，不写入 overlay | INV-07/11 |
| 留出验证 unstable 降级 declared | `tests/test_validation.py::test_unstable_thresholds_are_frozen_as_declared` | 不稳定 → `DECLARED` + 「不作为实测值声称」 | INV-02/07 |
| 对比契约不通过（incomparable） | `tests/test_p0.py::test_contract_fails_on_phase_mismatch`、`::test_contract_fails_on_window_mismatch`、`::test_contract_warns_on_capture_sizes_and_comm_mismatch` | verdict=`incomparable`，明确失败项；不留可用速度差 | INV-05 |
| 采集审计缺字段 | `tests/regression/test_supplementary_gates.py::test_missing_contract_sha_fails_instead_of_crashing` | 缺 `contract_sha256` → 判定失败（<missing>），不崩溃 | INV-11 |

## 2.1 失败信息（注入旧行为时实际看到的断言）

| ID | 失败时的关键消息 | 定位信息 |
|---|---|---|
| R01 | `assert ['op_summary_slice_1.csv', ...] != sorted(names)` / `{'MatMul': (2, 200.0)} != {'MatMul': (4, 400.0)}` | 文件名顺序与逐算子 `(calls,total)` |
| R03 | `293.74 != 719.31`（avg 被算成平均值的平均） | `record.avg_time_us.value` |
| R04 | `1.5 != 1.0` | `compute_pipe_time_fraction` |
| R05 | `report.comparable is True`（应为 False）；`code != 3`（strict）；`deltas` 非空 | compare 报告 / CLI 退出码 |
| R06 | `trace.available is False` 失败（枚举表被当成数据）；或 notes 出现「同步等待合计」 | `MstxTrace.available`、`TimelineSummary.notes` |
| R07 | `find_claims(payload) == []` 失败（文本/嵌套键命中）；注入探针时 `DID NOT RAISE AssertionError` | `_claims.find_claims` 返回的 JSON 路径 |
| R08 | `110462688.0 != 110462.688`（漏缩放） | `Metric.value` 与 `interval` 时长 |
| R09 | `{'mac_time_us', ...} <= missing` 失败（缺列未标记） | `meta.extra['missing_extra_metrics']` |
| R10 | `comparable is True`（应为 False）；reasons 不含 `rank_duration_inconsistent` | compare 报告 |
| R11 | `1.539… != 0.1797…` 且 `utilization < 1` 失败 | `aicore_utilization` |
| R12 | `gap_max_us == 50` 失败（变成 1.79e15）；或 `fallback.span_us != 250` | `TimelineSummary.gap_max_us/span_us` |

## 3. 故障注入 / 变异验证（隔离副本，未在主分支引入错误）

在 `/tmp` 的仓库副本中注入历史错误行为，运行对应用例，确认测试失败后退回，不动主工作树：

| 变异 | 命令（副本内） | 退出码 | 失败用例 |
|---|---|---|---|
| R01：分片改回字典序 `sorted()` | `pytest tests/regression/test_r01_slice_order.py -q` | 1 | `test_natural_order_keeps_slice_10_after_slice_2` |
| R08：删除 `ascend_op_summary.yaml` 的 `scale: 0.001` | `pytest tests/regression/test_r08_communication_unit.py -q` | 1 | `test_communication_time_metric_is_only_on_communication_rows` |
| R11：利用率分母换成 `total_time_us` | `pytest tests/regression/test_r11_utilization_denominator.py -q` | 1 | `test_utilization_uses_aicore_time_not_task_duration` |
| R12：`_gaps_from_merged` 的 cursor 从 0 起算 | `pytest tests/regression/test_r12_window_origin.py -q` | 1 | `test_epoch_origin_does_not_create_a_pseudo_gap` 等 2 个 |
| verify-real 分类回退为「skip 优先」 | `pytest tests/regression/test_verify_real_contract.py -q` | 1 | `test_failure_with_skips_is_failure_not_skipped` |
| `gate_exit` 改为任意退出码都通过 | `pytest tests/regression/test_verify_real_contract.py -q` | 1 | `test_expect_blocked_gate_accepts_only_two[5-1]`、`test_expect_blocked_rejects_false_pass`、`..._rejects_skip` |
| `_claims` 只扫顶层键、不递归 | `pytest tests/regression/test_r07_graph_operator_shrink.py -q` | 1 | `test_claims_guard_rejects_injected_conclusions` |

## 4. 真实数据回归（独立执行）

- 入口：`make verify-real`（`scripts/verify_real.py`），要求 `OPT_DISCOVERY_DATA_ROOT` 指向四组统一补采。
- 本轮实测：**SKIPPED（16 passed, 1 skipped）**，退出码 3。未执行项是无 CSV 回填场景
  （统一补采后所有 rank 都有 CSV，按复审要求显式 `pytest.skip`，不再静默计为通过）。
  因此真实回归**不能记 PASS**；回填能力由 CPU 回归 R05 覆盖，`sqlite` 前提由
  `test_sqlite_available_for_exported_ranks` 无条件断言。
- 通信单位另由真实用例 `test_communication_time_matches_record_total` 锁定
  （删 `scale` 即失败，见 `phase1/independent-verification.md`）。
- 判定基于 `--junitxml` 结构化计数：全 skip/未收集到用例 -> SKIPPED 或 FAILED，绝不 PASS；
  缺数据 -> BLOCKED（退出码 2），且与 pytest 的退出码 2 解耦（内部 `Outcome` 枚举）。
- 真实回归只验证口径与契约（不逐字比对时间值）：
  `test_communication_only_exists_in_op_summary`、`test_no_rows_are_silently_dropped`、
  `test_round_window_shrinks_prefill_to_steady_state`、`test_export_has_two_ranks` 等。

## 5. 明确缺口（未假装已覆盖）

| 缺口 | 现状 | 归属 |
|---|---|---|
| R07 无 Schema 级封禁 | 由 `_claims` 全字段扫描 + 注入反例锁定；生产代码仍可能新增字段（测试会拦住） | AE-07（若需要运行时校验） |
| ~~R05/R10 下游质量闸门~~ | **AE-03 已关闭**：质量标记随表落盘、`compare` 拒绝不可比、`--strict` 非零退出；原 xfail 已转为 `test_quality_gate.py` 正式断言 | 已完成 |
| INV-12 有界内存无基线 | 未做峰值内存/耗时测量 | AE-06 |
| 真实 CSV 缺失 rank 的回填场景 | 统一补采后不可复现，CPU 回归替代 | AE-06（若重采） |
