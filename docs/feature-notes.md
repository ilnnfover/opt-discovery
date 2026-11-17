# 功能实现与局限说明

> **本文定位**：README 讲"能做什么"，本文讲"怎么做到的"和"做不到/做不好的地方"。
> 每一个局限都带实测证据（数字、文件、现象），没有泛泛而谈的猜测。
>
> **更新**：2026-11-16（统一补采 rounds 0–2 数据 + 阈值标定 v2 + op_summary busy 口径）

---

## 阅读指南

- **想快速了解全貌**：先看下面的「一页速览」和「数据流」。
- **想排查某个具体问题**：用「常见任务 → 去哪节」定位，再看对应章节的「局限」列表。
- **想改代码**：「实现」段给出了关键文件路径与核心函数名，可直接跳转。
- **局限分级**：文中加粗的局限（如 **rank1 采集伪影**）是已影响结论可信度的关键项，
  普通编号项是设计边界或环境限制；两者都不隐瞒。
- **术语**：首次出现的专有名词在「术语表」中有简短解释。

### 常见任务 → 去哪节

| 你遇到的问题 | 看哪节 |
|---|---|
| 两个 rank 的 busy 对不上 / “空闲反转” | [§6 时间线](#6-时间线设备空闲归因) |
| 通信占比突然变得极高或极低 | [§4 CSV 解析](#4-映射驱动的-csv-解析) + [§7 通信量](#7-通信量守恒) |
| 阈值为什么是这个值 / 能不能跨 workload 用 | [§11 阈值标定](#11-阈值体系与标定) |
| “下发受限”结论为什么被否定 | [§6 时间线](#6-时间线设备空闲归因) + [§10 规则](#10-规则引擎) |
| 新导出的格式变了，哪些指标会丢 | [§4 CSV 解析](#4-映射驱动的-csv-解析) |
| 换模型/换卡后哪些东西必须重做 | [§3 契约](#3-采集契约审计) + [§11 阈值](#11-阈值体系与标定) |

---

## 一页速览

| 阶段 | 功能点 | 一句话实现 | 最大局限 |
|---|---|---|---|
| 数据入口 | [§1 目录发现](#1-目录发现与-bundle-构建) | workload YAML label → 导出目录 → PROF_* 按 rank 建 bundle | 目录布局强约定，多一层 PROF 易踩坑 |
| 数据入口 | [§2 轮次窗口](#2-轮次窗口切分) | 日志 `ACTIVE_ROUND` 实测边界切窗 | 窗口内不做二次清洗 |
| 数据入口 | [§3 契约审计](#3-采集契约审计) | manifest sha256 + 9 项可比性闸门 | 只能拒绝比较，不能修历史数据 |
| 数据加工 | [§4 CSV 解析](#4-映射驱动的-csv-解析) | YAML 列映射 + 流式逐实例聚合 + computed 派生 | bytes 是 shape 估算上界 |
| 数据加工 | [§5 sqlite 回填](#5-sqlite-回填) | op_counter.db 交叉验证后回填 | 时钟不同域，metrics 覆盖 76% |
| 分析 | [§6 时间线](#6-时间线设备空闲归因) | op_summary 区间多 rank 并/交 + ACL 空洞归因 | **rank1 采集伪影无法修复** |
| 分析 | [§7 通信量](#7-通信量守恒) | globalTaskId 去重，只认字节量 | 无消息大小分布 |
| 分析 | [§8 图覆盖率](#8-图覆盖率) | Model ID 有效/无效双口径 | 不回答“为什么没覆盖” |
| 诊断 | [§9 事实层](#9-事实层) | 单位归一 + 设备档案注入校准事实 | 组级聚合丢失实例尾部 |
| 诊断 | [§10 规则引擎](#10-规则引擎) | YAML 声明式规则 + 结构化 blocked | 不支持算子序列条件 |
| 诊断 | [§11 阈值标定](#11-阈值体系与标定) | 三类来源 + 留一双维度验证 | 分布型阈值是“本地水位” |
| 闭环 | [§12 机会回填](#12-机会生成与回填闭环) | 三级证据 + 单变量实验升级 | confirmed 恒为 0（刻意） |
| 闭环 | [§13 Eager vs Graph](#13-eager-vs-graph-对比) | 契约前置 + Δshare/耗时比 | 跨阶段无归一化视角 |
| 闭环 | [§14 源码映射](#14-源码映射) | 四层降级匹配到 file:line | 前缀匹配可能多义 |
| 质量 | [§15 测试](#15-测试策略) | 119 用例 + 实测 bug 回归 | 集成用例依赖本机路径 |

### 数据流（括号中为章节号）

```text
采集目录 ──§1 发现──> bundle ──§2 切窗──> Window
                                          │
                     ┌────────────────────┘
                     v
        §4 CSV 解析 / §5 sqlite 回填 ──> OperatorTable（算子表）
                     │
        ┌────────────┼─────────────┬──────────────┐
        v            v             v              v
    §6 时间线   §7 通信量     §8 图覆盖     §9 事实层
        │                                   │
        └──────────── 证据注入 ─────────────> §10 规则引擎
                                              │  （§11 阈值来自标定）
                                              v
                                      §12 机会 ──> §13 对比 / §14 源码
                                              │
                                              v
                                      bench → feedback（闭环）
```

### 术语表

| 术语 | 含义 |
|---|---|
| **provenance** | 数据来源分级：`measured` 实测 → `derived` 推算 → `declared` 人工冻结 → `assumed` 占位 |
| **evidence_level** | 结论分级：`hypothesis` → `probable` → `confirmed`（当前刻意保持 0 个） |
| **busy_any / busy_all** | 任一卡忙 / 两卡都忙的区间并集 / 交集时长；多 rank 不可只看单卡 |
| **window** | 分析窗口，来自日志实测的轮次边界（epoch µs） |
| **OperatorTable** | 标准化算子表：一行 = 一个（算子名 × Task Type）组，含 calls/时长/额外指标 |
| **设备档案** | micro-benchmark 实测的峰值常数（282.5 TFLOPS / 1226.5 GB/s / 24 核），注入后产出校准事实 |
| **degraded** | 窗口质量标记（如最大空洞 >100 ms），提示该窗口结论要降级看待 |

---

## 1. 目录发现与 bundle 构建

```text
workload YAML (labels.export_dir)
   └─> exports_root/<label>/
         ├─ workload.log ──────────────> RunLog（轮次来源）
         └─ PROF_*/
               ├─ device_* ───────────> rank N
               └─ mindstudio_profiler_output/op_summary_*.csv ─> RankExport
```

**实现**（`collectors/profiler_dir.py`）

- `discover_bundle(workload_yaml, export_dir)`：从 workload YAML 的 `labels.export_dir`
  定位导出目录，glob `PROF_*` 作为 rank 目录，每个 rank 需要
  `mindstudio_profiler_output/op_summary*.csv`（或 `device_*/sqlite/op_counter.db` 回退）。
- `discover_all()` 遍历 workload 目录，按 label 匹配 exports_root 下的子目录，批量建 bundle。
- rank 号从 `device_*` 目录名提取（`device_0` → rank 0），无设备目录时按 PROF 目录顺序补位。

**局限**

1. 目录布局强约定：`export_dir` 必须同时容纳 `PROF_*` 与 `workload.log`。统一补采的
   实际布局是 `<bundle>/PROF/PROF_*`（多一层 PROF），单 workload 的 `timeline` 命令
   需要显式传 `--export <bundle>/PROF`，容易踩坑。
2. rank 识别依赖 `device_*` 目录名；若某 rank 聚合失败没有设备目录，顺序补位可能导致
   rank 号与真实 rank 不一致。
3. `task_id` 在 `ge_summary` 中不唯一（(model_id, task_id, stream_id) 有 6.5 万个重复组），
   任何按 task_id 的 join 都有歧义，需要额外序列信息才能消解。

---

## 2. 轮次窗口切分

```text
harness 日志: ACTIVE_ROUND 0..7 start/end (epoch µs)
   └─> select_rounds_window(rounds=[0,1,2])
         ├─ start = round0.start, end = round2.end  (provenance=measured)
         └─> 质量校验: max_gap > 100ms ──yes──> 标 degraded（不改窗口）
                              └──no───> 正常窗口
```

**实现**（`analyzer/window.py`）

- harness 在日志里打印 `ACTIVE_ROUND n start/end`，与算子时间戳同一时钟域（epoch µs）。
- `select_rounds_window(run_log, rounds)` 取若干轮的 [首轮起点, 末轮终点] 作为窗口，
  `provenance=measured`（边界来自日志实测而非假设）。
- 不用"按空闲间隔自动切窗"：Graph 的收益本身就在减少空闲，用空闲切窗等于用被比的东西
  定义比较基准，Eager 与 Graph 不可比。
- 窗口质量只校验不改窗口：流式计算 busy_ratio / max_gap_us，空洞超 100 ms 标记 degraded。

**局限**

1. 窗口边界精度受限于日志打印的时机（Python 侧打印，与 device 时间有微小偏差）。
2. round 内混入的编译/缓存效应只能靠 warmup 轮排除，窗口内部不做二次清洗。
3. 实测 decode-eager 窗口内有 109.4 ms 的孤立空洞（超 100 ms 阈值）标 degraded——
   这是"如实标注"，但 degraded 窗口的数值结论要降级看待。

---

## 3. 采集契约审计

```text
collection_contract.yaml ──sha256──> collection_manifest.json (每组)
   ├─ check-collection ──manifest vs 契约──> pass / fail
   └─ check-runs(A,B) ──9 项检查──> comparable?
                                     ├─ fail ──> incomparable（拒绝出结论）
                                     └─ pass ──> 允许 compare
```

**实现**（`analyzer/contract.py` + `check-collection` / `check-runs`）

- `configs/collection_contract.yaml` 固化除自变量（mode/graph_mode）外的全部采集条件：
  模型路径、TP、精度、seed、`max_num_seqs=32`、轮次、msprof 开关、需要清理的环境变量。
- 补采脚本每组写 `collection_manifest.json`（契约 sha256、实际命令、自检结果），
  `check-collection` 据此判 pass/fail，替代人工核对 YAML。
- `check-runs` 对两个已解析表查 9 项可比性（阶段、硬件、输入形状、采集参数、轮次、
  算子集合、通信字节量、记录数），任一 fail 判 `incomparable`，明确拒绝出结论。

**局限**

1. 契约只覆盖"写进 manifest 的字段"——采集机器上的隐式状态（缓存热度、驱动版本漂移）
   无法契约化。
2. 契约闸门只能拒绝比较，不能修复历史数据的不一致（如旧四组 `max_num_seqs` 不一致，
   只能靠统一补采重采解决）。
3. 字节量一致性检查的容差是经验值（1.04x 守恒判 pass），对更大的采集噪声无自适应。

---

## 4. 映射驱动的 CSV 解析

```text
op_summary_*.csv (80 万行/rank)
   └─ csv.reader 流式逐行 ──> 每行:
         ├─ Task Duration ──COMMUNICATION?── ns×0.001 : µs
         ├─ Shape + Dtype ──computed──> bytes_moved + 配对时长
         └─ key=(operator, task_type_raw) ──> calls / Σduration / Σextra
                  └─> OperatorTable（missing/unmapped columns 告警）
```

**实现**（`parsers/csv_mapping.py`）

- 列映射全部在 `configs/parsers/ascend_op_summary.yaml` 声明：算子名、时长、dtype、
  shape、extra 指标、聚合键。改列映射不动代码。
- 流式逐行解析（decode 单 rank 80 万行），按 `(operator, task_type_raw)` 聚合——
  同一 OP Type 按 Core Type 拆行（MatMulV3 = AI_CORE + MIX_AIC），不拆会重复计数。
- 逐实例行语义（`row_semantics: instance`）：calls = 行数，duration = 逐行求和。
- **已知单位陷阱在解析层修复**：`COMMUNICATION` 行的 `Task Duration` 实际是 ns
  （其余行是 µs，两者混在同一列），用 `COMMUNICATION_DURATION_SCALE=0.001` 缩放。
  该陷阱同时存在于 TASK 表和 op_summary CSV。
- **computed 派生指标**：`bytes_moved`（逐实例 shape×dtype 估算搬运字节，读写合计口径）
  与 `bytes_moved_time_us`（配对时长，只累计成功估算出字节的实例），保证带宽比值的
  分子分母同源；`when.task_type` 把 COMMUNICATION 行排除（走链路不走 HBM）。
- 解析器自动报告：`missing_columns`（映射声明了但 CSV 没有）、`unmapped_columns`
  （CSV 有但映射没覆盖），帮助发现导出格式漂移。

**局限**

1. 33 列精简导出（2026-11-15 起）没有 mte/mac/fixpipe/scalar 分列，相关 extra 指标
   全部缺失，依赖它们的标定项（`mac_pipe_*`、`memory_pipe_*`）无法重标。
2. 多 shape 算子聚合时 shape 被丢弃（"N distinct shapes; shape left null"）——
   bytes 估算因此必须放在逐实例层，但动态维（-1）实例的字节被跳过
   （分母由配对时长对齐，不会把分母算大）。
3. bytes 估算忽略 L2/片上缓存复用，是 HBM 流量上界近似：只写算子（ZerosLike/MemSet）
   被高估约 2 倍，个别算子估算带宽超过物理峰值（实测 max 4861%）。
4. dtype 字节表只覆盖常见类型（bf16/fp16/fp32/int8~64 等）；`DT_UNDEFINED` 组跳过。
5. 映射的 `verified_against` 是人工核对记录（列名/口径），导出格式变更时需要人工复核。

---

## 5. sqlite 回填

```text
rank 目录有 op_summary CSV?
   ├─ yes ──> 常规 CSV 解析
   └─ no  ──> device_*/sqlite/op_counter.db::op_report
                 └─ 70/70 键与 CSV 交叉验证 ──> 回填 OperatorRecord（标记降级来源）
```

**实现**（`parsers/sqlite_op_report.py` + `parse-sqlite` 命令）

- CSV 缺失时从 `device_*/sqlite/op_counter.db::op_report` 回填算子统计。
- 曾与 CSV 交叉验证：70/70 键零差异。

**局限**

1. 回填产物与 CSV 口径不完全等价（不含通信算子的某些细节），仅作为"无 CSV 时的
   降级路径"，不能混入 CSV 解析结果做聚合。
2. `ai_core_op_summary.db` 的 `task_time` 表大量 UNKNOWN/-1 行、时钟是相对 ns
   （与窗口的 epoch µs 不同域），不能直接当时间线用；`ai_core_metrics` 的
   `aic_total_time` 覆盖率约 76%，且头部有 ~146 ms 的计数器聚集伪影（两 rank 都有），
   只适合做 rank 间相对比较，不适合当绝对时长。

---

## 6. 时间线：设备空闲归因

```text
每个 rank 的 msprof_*.db
   ├─ 同级发现 op_summary CSV ──> load_op_intervals ──────┐
   └─ 无 CSV ──> TASK 表（强制告警，来源=task_table）──────┤
                                                          v
                                   区间按窗口裁剪 ──> 每 rank merge
                                                          │
                       ┌──────────────────────────────────┼──────────────┐
                       v                                  v              v
                 union: busy_any                 intersection:    per-rank busy
                       │                          busy_all             │
                       └──────────────┬───────────────────┘              │
                                      v                                 │
                          gaps（从首个忙碌区间起算）                      │
                                      │                                 │
                          CANN_API 覆盖判定                              │
                       acl_busy / host_silent / mixed                   │
                                      │                                 │
                          (可选 MsprofTx 补静默归因) <───────────────────┘
                                      v
                          timeline.json {busy_source, per_rank, notes}
```

**实现**（`analyzer/timeline.py`）

- **busy 数据源选择**：优先各 rank 的 `op_summary` 算子区间
  （`find_op_summary` 按 db 父目录发现 CSV，`load_op_intervals` 流式提取 start/duration），
  缺失时回退 `TASK` 表。`timeline.json` 的 `busy_source` 记录实际来源，
  per-rank 逐 rank 标注 `source`；回退 rank 逐个点名告警。
- **多 rank 合并**：两 rank 区间分别 merge 后求并集（`busy_any`）与交集（`busy_all`）；
  单读一个 rank 的 busy 会随排序翻转出相反结论（实测 prefill 曾出现 45.9% vs 72.5% 的
  "idle 反转"，修复后两 rank 对称 20.7%/20.7%）。
- **空洞归因**：对 ≥100 µs 的空洞看 `CANN_API`（Host ACL）是否在下发，
  分类 `acl_busy`（≥50%）/ `host_silent`（≤10%）/ `mixed`；覆盖率统计取并集
  （多线程并发事件累加会把覆盖率算到 4593%）。
- **窗口过滤与空洞起点**：区间按窗口裁剪；空洞从首个忙碌区间起算——epoch 原点到
  首个算子之间不是设备空洞，否则产生 1e15 量级伪 gap（已修复的实测 bug）。
- **rank 不一致检测**：任务数接近但累计时长差 ≥2 倍时标 `rank_duration_inconsistent`，
  此时 per-rank busy 不解读为"某卡在等"。
- **MSTX 关联**：`collectors/mstx.py` 读 `MsprofTx` 表把静默空洞归因到打点阶段；
  可用性取决于采集开关（见局限 4）。

口径速查（`timeline.json` 字段）：

| 字段 | 定义 | 误读风险 |
|---|---|---|
| `device_busy_us` | `busy_any`：任一 rank 忙碌区间并集时长 | 不是“设备利用率”，多卡下是“有没有卡在干活” |
| `device_busy_all_us` | `busy_all`：两 rank 区间交集时长 | TP 不同步时会远小于 busy_any，属正常 |
| `device_idle_ratio` | `(span − busy_any) / span` | rank1 数据伪影时会偏大（见局限 2） |
| `busy_source` | 实际数据源：`op_summary` / `task_table` | 非 `op_summary` 时所有 busy 结论降级 |
| `rank_duration_inconsistent` | 任务数接近但累计时长差 ≥2× | 为真时不能解读“某卡在等” |

**局限**

1. `TASK` 表不可信（单位混用 + 两 rank 时长和差 18.6 倍），回退路径的数字只能参考；
   这是环境问题，分析器只能标记不能修复。
2. **rank1 采集伪影**：decode-eager rank1 的 CSV 长 kernel 时长被截断
   （MatMul p99=34.7 µs vs rank0 2980 µs；同一 task_id 硬件实测 146.6 ms vs CSV 34 µs）。
   硬件 `aic_total_time` 两 rank 差 0.4% 证明计算量相同——busy 7.4% 是伪影。
   分析器标记 `rank_duration_inconsistent` 但无法修复；免重采恢复（aic_metrics 回填）
   覆盖率仅 76% + task_id 歧义，只能验证对称性，得不到精确数字。graph 模式 rank1 正常。
3. 空洞归因二分类粗糙：`host_silent` 无法区分 Python 调度、数据准备还是等待，
   需要 MSTX 打点（见局限 4）。
4. **MSTX 通路的环境限制**：msprof CLI 的 `--msproftx=on` 不支持 PyTorch 场景
   （官方文档明确）；正确用法是 `torch_npu.profiler` + `_ExperimentalConfig(mstx=True)`，
   已在 910B2 实测可用（range/marker 全部落盘，可传 stream 关联 device 侧活动）。
   vLLM-Ascend wrapper 默认 `msprof_tx=False`，接入需改造。
5. `OVERLAP_ANALYSIS` 表在 decode 口径出现 comm=100%，语义（通信任务全程按通信计）
   与直觉不符，只能作参考不能单独下结论。
6. 空洞从首个忙碌区间起算是刻意设计（span−busy 承担窗口两端），窗口两端的空闲
   不计入 gap 统计——与部分用户直觉不同，需要注意。

---

## 7. 通信量守恒

```text
TASK 表的 HCCL 行
   └─ group by globalTaskId（1~3 行 plane 重复，2.0x dedup）
         └─ Σbytes ──> 字节总量 + 单任务均量
                       ├─ eager: 21,089 MiB / 132,413 条
                       └─ graph: 21,980 MiB / 16,399 条（守恒，记录被合并）
```

**实现**（`analyzer/comm.py` + `comm` 命令）

- 读 `TASK` 表的 HCCL 任务，按 `globalTaskId` 去重（同一通信任务有 1~3 行 plane 重复，
  实测 2.0x dedup），输出字节总量与单任务均量。
- 字节量是**唯一跨执行模式守恒的量**：Graph 把 HCCL 记录合并（decode 132,413 → 16,399 条，
  字节 21,089 → 21,980 MiB），占比与调用次数跨模式不可比。

**局限**

1. 依赖 TASK 表的 HCCL 行；TASK 表的其他字段失真不影响字节量，且字节量已与
   Graph 侧交叉验证守恒（1.042x），但表级风险仍在。
2. 只给总量，没有按通信算子/消息大小分桶的分布。

---

## 8. 图覆盖率

```text
op_summary 每行 Model ID
   ├─ 4294967295 ──> 图外（eager 全部）
   └─ 有效值 ──────> 图内
          └─ 双口径统计: 图内任务占比 | 图内时间占比
                 └─> 输出 fallback 算子清单（图外且耗时高的算子）
```

**实现**（`analyzer/graph_coverage.py`）

- 判据是 `op_summary` 的 `Model ID`：有效值 = 图内，`4294967295` = 图外
  （eager 恒为无效值，覆盖率 0 属预期）。
- 双口径：图内任务占比 vs 按时间占比（图外任务更长，两口径差异本身就是信息），
  输出 fallback 算子清单（如 decode-graph 的 `Add` 1789 ms、prefill-graph 的
  GDN 路径整段在图外）。

**局限**

1. 只回答"覆盖到哪"，不回答"为什么没覆盖"（PIECEWISE 切分点的根因要靠源码映射与
   算子名推断）。
2. Model ID 有效性的判据是对导出格式的经验观察，若 CANN 变更图外标记值需重新核对。

---

## 9. 事实层

```text
OperatorRecord
   ├─ 核心字段 ──> 单位归一（time→µs, ratio, bytes）
   ├─ extra 指标 ──> 透传 + 来源列
   ├─ 派生 ──> *_time_fraction / *_over_device / arithmetic_intensity
   └─ device_profile 存在?
         ├─ yes ─> aicore_utilization = flops/aicore_time/peak_bf16
         │         active_core_ratio = block_num/24
         │         bandwidth_util = bytes/配对时长/peak_hbm
         └─ no ───> 校准事实不产出（相关规则 blocked）
          ↓
FactSet（每个 fact 带 provenance + source，证据链可回溯）
```

**实现**（`diagnosis/facts.py`）

- `build_facts(record)` 把一条算子记录展平为单位归一化的事实集：时长字段自动换算成 µs，
  ratio/bytes 字段自动归一，extra 指标全部透传并记录来源。
- 派生事实：`*_time_fraction`（流水时间/任务时长）、`*_over_device`、`time_share`、
  `avg_over_min`、`arithmetic_intensity`（flops/bytes）。
- **设备档案注入**（`device_profile` 参数）产出三个校准事实：
  - `aicore_utilization` = flops / aicore_time / peak_bf16——分母必须用流水活跃时间，
    实测小算子 Task Duration 可小于 aicore_time（2.3 µs vs 19.7 µs），用错分母利用率 >1；
  - `active_core_ratio` = block_num / ai_core_count；
  - `memory_bandwidth_utilization` = bytes_moved / 配对时长 / peak_hbm_gbps
    （通信行排除；bytes 缺失时不产出）。
- 每个事实带 provenance 与 source（哪个文件哪列），证据链可回溯。

**局限**

1. `arithmetic_intensity` 需要 `bytes_moved`，而 bytes 是估算——算术强度事实目前
   只对 shape 可解析的实例存在，且继承估算误差。
2. 事实是算子组级（聚合后）的，不是实例级——实例级的尾部分布（如 decode 的
   MatMul p50=3.2 µs 但 max=488 ms 的极端偏斜）在组级事实上不可见。
3. 校准事实依赖设备档案文件存在；档案缺失时这三类规则全部 blocked（明确报告，
   但不会给结论）。

---

## 10. 规则引擎

```text
FactSet × Rule（11 条）
   ├─ applies_to 不符 ──> not_applicable
   ├─ required 不满足 ──> blocked（列出缺失事实）/ not_matched
   └─ required 全满足 ──> fired（supporting 事实提升置信度）
          ↓
OperatorDiagnosis {rule_evaluations[], evidence_level}
          ↓
Opportunity（分数排序 + caveat 注入）
```

**实现**（`diagnosis/rules.py` + `engine.py`）

- 11 条规则用 YAML 声明：`applies_to`（phase/mode/task_type 过滤）、`required`
  （全部满足才触发）、`supporting`（加分项，提升置信度）、`missing_evidence`
  （判不了时输出缺什么指标、为什么、怎么采集）。
- 每条规则对每个算子产出审计记录：`fired / not_matched / blocked / not_applicable`，
  blocked 时列出缺失事实——"判定不了"本身是结构化输出。
- 字面量阈值必须写 `justification`；无 required 条件的规则必须声明
  `allow_hypothesis_only: true`——防止"拍脑袋规则"混进配置。
- `DiagnosisEngine(device_profile=...)` 把设备档案透传给事实层。

**局限**

1. 规则间的优先级/互斥没有显式建模——同一算子可能同时触发多条规则，排序交给
   机会生成分数，规则本身不知道彼此。
2. 规则的 `required` 条件是静态阈值比较，不支持跨算子的上下文条件
   （如"该算子前后有通信"这类序列模式）。
3. 置信度/级别的映射是手写的分数逻辑，规则越多越难全局校准。

## 11. 阈值体系与标定

```text
输入源                     标定配方                    留一验证
设备档案 ──────────> 设备常数（roofline/peak）
OperatorTable ─────> 分布分位数 p50/p75/p90 ─┐
timeline.json ─────> host_* 占比 ────────────┤
                                             v
                                    夹取区间 + 0.05 取整
                                             v
                              3 组标定→第 4 组诊断（4 折）
                              ├─ stable ──────> measured
                              ├─ unstable ────> declared（写明原因）
                              └─ 样本不足 ────> insufficient（不写入）
                                             v
                              apply_overlay → RuleSet（16 项，带 basis）
```

**实现**（`experiment/calibration.py` + `validation.py` + 阈值覆盖层）

- 16 个阈值三类来源，provenance 显式：`measured`（实测）→ `declared`（人工冻结）
  → `assumed`（占位）；策略值（`small_kernel_time_us` 等）显式不参与标定。
- 四种标定配方：
  - 设备常数：`arithmetic_intensity_low` = roofline 拐点（282.5 TFLOPS / 1226.5 GB/s，
    卡上实测），零样本依赖，留一漂移 0%；
  - 分布分位数（对比组逻辑）：`aicore_utilization_saturated` = 计算类算子利用率 p90
    （夹取 [0.3, 1.0]）、`..._ceiling` = 搬运类 p75、`low_core_ratio` = kernel 核占比 p50、
    `bandwidth_utilization_saturated` = 搬运类估算带宽 p90（夹取上限 1.0）；
  - 流水占比类：`compute_pipe_time_fraction_*` 等（33 列导出下样本受限）；
  - timeline 类：`host_gap_ratio` / `host_overhead_ratio`（来自 timeline.json）。
- 护栏：样本门槛 n≥12（不足判 `insufficient_samples` 不写入）、夹取区间、0.05 步进取整。
- **留一验证**（leave-one-workload-out）：每轮 3 个 workload 标定、第 4 个诊断，
  分两个独立维度——结论稳定性（候选集/级别）与阈值稳定性（数值漂移）；
  不稳的项自动降级 declared，basis 写明"为什么不算实测"。
- 设备档案（`--device-profile` 或按 ruleset 的 device_profile_id 自动查找）注入全部
  标定与诊断路径。
- 当前状态：10/16 项标定或冻结（measured=2、declared=8），6 项 assumed 占位。

关键阈值状态速查（完整 16 项见 `configs/thresholds/ascend_910b.calibrated.yaml`）：

| 阈值 | 当前值 | provenance | 一句话依据 |
|---|---|---|---|
| `arithmetic_intensity_low` | 230.3 | measured | 设备 roofline 拐点，留一漂移 0% |
| `aicore_utilization_ceiling` | 0.10 | measured | 搬运类利用率 p75（噪声底） |
| `aicore_utilization_saturated` | 0.55 | declared | 计算类利用率 p90，跨 phase 混合 |
| `low_core_ratio` | 0.95 | declared | kernel 核占比 p50，漂移 233% |
| `bandwidth_utilization_saturated` | 1.00 | declared | shape 估算带宽 p90，撞夹取上限 |
| `sync_wait_ratio` | 占位 | assumed | 需 MSTX 同步区间，待重采 |

**局限**

1. **分布型阈值是"本地正常水位"不是设备常数**：`aicore_utilization_saturated=0.55`
   是 prefill（87.9%）与 decode（~10%）的混合 p90，留一折值 0.85/0.55/0.55——
   跨 workload 族共享有灵敏度损失（prefill 中间档 55–85% 的算子不报）。
   共享经留一验证结论稳定（重叠 91–97%），当前可用，但 `level_changes` 持续非零时
   应按 phase 族拆分（机制：规则已有 applies_to，缺 overlay 的 phase scope）。
2. **带宽阈值 = 1.0 是估算噪声的上界**：shape×dtype 估算忽略缓存复用，
   只写算子高估约 2 倍，p90 撞夹取上限。标定机制如实暴露了这一点（declared + note），
   但阈值本身在拿到真实 HBM 字节前不具备区分度。
3. **样本量小**：4 个 workload 是分位数标定的地板；`host_*`（n=4）、
   `mac/memory_pipe_*`（33 列无数据）都因门槛拒绝标定。
4. 设备档案的带宽口径（读写合计 vs 单向）因卡释放无法复测，已在档案中声明为假设，
   待卡可用后用两种口径分别计时复核。
5. 标定是 in-sample 的（标定与诊断用同一批数据），系统已在 notes 里声明
   "余量 <10% 的命中应视为 provisional"。

---

## 12. 机会生成与回填闭环

```text
OperatorDiagnosis.candidates
   └─ score = 时间占比 × 可优化性 ──> 排序编号 ──> Opportunities
          └─ evidence_level: hypothesis（占位阈值）/ probable（实测阈值）
                   ↓
        bench（整轮延迟）──> bench-compare（n≥3, 变化 ±5%）
                   ↓
        feedback:
          ├─ 单算子 ≤ −5% ──> accepted 升一级（上限 probable）
          └─ 工作负载级 A/B ──> validating（不升级，无法归因单算子）
```

**实现**（`discovery/generator.py` + `experiment/feedback.py` + bench 三命令）

- 诊断 → 机会：按分数（时间占比×可优化性）排序编号，附带证据（诊断+源码位置+
  阈值可信度）。
- 证据三级：hypothesis（阈值占位）→ probable（实测/已冻结，或单变量实验验证）
  → confirmed（端到端+kernel 级，刻意保持 0 个）。
- `bench` 跑现有 harness 解析整轮延迟 → `bench-compare`（<3 轮 insufficient、
  中位变化 <±5% 无显著差异）→ `feedback` 回填：单算子实验 b_faster ≤−5% 则
  accepted 升一级（上限 probable）；工作负载级 A/B 只能 validating 不升级。

**局限**

1. 回填的归因边界：工作负载级 A/B 无法归因到单个算子，只有单算子单变量实验才能升级——
   这要求实验环境能精确复现单算子改动。
2. bench 依赖 harness 的日志格式（ACTIVE_ROUND），harness 变更要同步适配。
3. confirmed 门槛高是刻意设计，但也意味着系统永远不给出"一定有效"的承诺。

---

## 13. Eager vs Graph 对比

```text
OperatorTable A + B
   └─ check-runs（9 项契约）
         ├─ fail ──> incomparable（拒绝出结论，写明原因）
         └─ pass ──> Δshare / 调用数比 / 共享算子单次耗时比 / 单侧算子
                       └─ 自动告警：模式差异 | 图集合收缩 | HCCL 记录合并
```

**实现**（`analyzer/compare.py`）

- 先过 `check-runs` 契约，不可比直接拒绝并写明原因。
- 输出 Δshare、调用数比、共享算子单次耗时比、仅在单侧出现的算子清单。
- 自动告警：跨阶段对比只比占比结构；Graph 下算子集合收缩（71 → 44），
  "消失的算子"≠ 未执行；HCCL 记录合并使通信占比/次数跨模式不可比。

**局限**

1. 对比基于算子组级聚合，不同 shape 的同名算子合并后单次耗时比是混合均值
   （Shape 混合时 parser 已标注 shape_count，但比值解读要小心）。
2. 跨阶段（prefill vs decode）只判 `incomparable`，没有归一化后的可比视角
   （如按 token 归一）。

---

## 14. 源码映射

```text
算子名 ──> 四层降级匹配（命中即停，记录置信度）
   ├─ ① binding: torch_binding.cpp 注册表 (0.95) ──> file:line
   ├─ ② exact: 同名符号 ────────────────────────> file:line
   ├─ ③ snake: CamelCase → snake_case ──────────> file:line
   └─ ④ prefixed: npu_ / aclnn_ 前缀 ───────────> file:line
        全部未命中 ──> builtin / runtime（不算失败，说明无源码）
```

**实现**（`source_analysis/` + `source-map` 命令）

- 四层降级匹配，逐层记录置信度：`binding`（torch_binding.cpp 注册表，0.95）→
  `exact`（同名符号）→ `snake`（CamelCase→snake_case）→ `prefixed`（npu_/aclnn_ 前缀）。
- 实测 71 个算子：命中 37（binding 9 / exact 20 / prefixed 3 / snake 5），
  未命中 34（builtin 21、runtime 3、未匹配 10）——CANN 内置算子和 HCCL 运行时
  算子本来就没有源码定义，未命中不是失败。

**局限**

1. 依赖 torch_binding.cpp 的注册表路径可发现；编译变体或裁剪版会漏。
2. 蛇形/前缀匹配可能多义（同名但对应多个实现），只给 file:line 不保证是热路径实现。

---

## 15. 测试策略

```text
pytest tests/
   ├─ 单测（解析/合并/标定/事实/回归用例）──> pass / fail
   └─ 集成用例（真实数据路径）
         ├─ 本机有 $PROF_ROOT 数据 ──> 运行真实数据断言
         └─ 无数据 ──────────────────> skip（不伪造通过）
```

**实现**（`tests/`）

- 119 个用例：单测覆盖解析器/合并/标定/事实计算的关键路径，含本周实测 bug 的
  回归用例（多 rank 合并、COMMUNICATION ns 缩放、epoch 伪 gap、bytes 配对、
  computed 指标解析、留一降级）。
- 15 个真实数据集成用例缺数据自动 skip。

**局限**

1. 集成用例依赖本机数据路径（`$PROF_ROOT/...`），换环境全部 skip。
2. 阈值标定的回归保护是"数字不变"级别，没有统计分布的黄金样本。

---

## 总结：设计的核心取舍

整个系统围绕一个原则构建：**"判定不了"是合法且结构化的输出，静默给出错误结论才是失败**。
本周用它抓出的三个"伪问题"（idle 反转、空卡、通信瓶颈）全部是数据缺陷而非真实性能
特征——这不是系统的失败，恰恰是可信度设计（来源标注、不一致检测、留一验证、
拒绝比较）按预期工作的证据。当前剩余的局限（真实字节缺失、同步归因、rank1 伪影）
都有明确的解决路径，且已写入 README 的"未实现"清单与触发条件。

---

## 相关文档

- `README.md`：快速开始、实测结论、命令全表、数据约束、未实现清单
- `plan.md`：项目演进计划与历史决策
- `configs/thresholds/ascend_910b.device_profile.json`：设备档案口径与假设说明
- `configs/thresholds/ascend_910b.calibrated.yaml`：当前阈值覆盖（含 basis，可追溯每一项的标定依据）
