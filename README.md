# Optimization Opportunity Discovery

把 Ascend Profiler 的导出数据，变成一份可评审的结论：**哪个算子值得改、凭什么这么说、还缺什么证据、改完怎么验证**。

工具只做**发现与诊断**，不做优化，也不承诺收益。所有结论都带证据等级，缺数据就明确说缺什么。

面向：Qwen3.5-35B-A3B / Ascend 910B / vLLM-Ascend，Prefill·Decode × Eager·Graph 四组负载。

---

## 快速开始

依赖：Python 3.12 + `pyyaml`（测试用 `pytest`）。分析阶段**不需要 NPU**，只要有导出目录即可。

**路径配置**：项目不硬编码任何机器路径。先按 `.env.example` 生成本地配置：

```bash
cp .env.example .env.local     # 填好 PROF_ROOT / NPU_STACK / MODEL_PATH
source .env.local              # 之后所有命令可直接用 $PROF_ROOT
```

未设置 `OPT_DISCOVERY_DATA_ROOT` 时，命令默认使用相对路径 `./exports`。

```bash
# 1) 一条命令跑完四组：解析 → 诊断 → 机会 → 热点矩阵 → 两两对比
#    exports-root 下的目录名须与 workload YAML 的 labels.export_dir 一致
python -m opt_discovery.cli analyze --exports-root $PROF_ROOT/unified-run \
    --workloads-dir configs/workloads/qwen35-35b-a3b-910b --rounds 0-2 --out out/steady --source-map

# 2) 看结果
cat out/steady/report.md                     # 可读报告
column -s, -t out/steady/workload_summary.csv   # 每组一行
column -s, -t out/steady/operator_summary.csv   # 每算子一行

# 3) 单点排查（按需）
python -m opt_discovery.cli timeline --export $PROF_ROOT/unified-run/qwen35-prefill \
    --workload configs/workloads/qwen35-35b-a3b-910b/prefill_eager.yaml --rounds 0-2
```

`analyze` 约 90 秒（decode 单窗口有 357 万条 Host 事件），产出：

```
out/steady/
├── report.md                    可读报告（7 章）
├── workload_summary.csv         每组一行的扁平表
├── operator_summary.csv         每算子一行的扁平表
├── hotspots.json                跨 Workload 热点矩阵
├── source_map.json              算子 → 源码位置
├── compare_*.json               4 组两两对比
└── <workload>/
    ├── operator_table.json      标准化算子表
    ├── diagnosis.json           逐算子诊断（含未命中规则与缺失指标）
    ├── opportunities.json       排序后的优化机会
    ├── timeline.json            设备空闲归因
    ├── graph_coverage.json      图覆盖率
    └── comm.json                通信字节量
```

---

## 它回答哪几类问题

| 你想知道 | 用哪个命令 | 看什么 |
|---|---|---|
| 哪个算子最值得改？ | `analyze` / `report` | `opportunities.json` 的排序、`report.md` 第 5 章 |
| 是不是下发受限？ | `timeline` | 设备空闲占比、Host ACL 覆盖了空闲的多少 |
| Eager 和 Graph 差在哪？ | `check-runs` → `compare` | 契约是否一致；Δshare / 调用数比 / 单次耗时比 |
| 通信到底有没有减少？ | `comm` | **字节量**（唯一跨模式守恒的量） |
| 图覆盖到哪一步？ | `graph-coverage` | 图内任务占比 / 按时间占比 / fallback 清单 |
| 该改哪个文件？ | `source-map` | 算子 → 实现文件:行（含注册项） |
| 改完有没有用？ | `bench` → `bench-compare` → `feedback` | 整轮延迟变化；回填后证据级别是否升级 |
| 阈值靠得住吗？ | `calibrate` → `validate-thresholds` | 哪些是实测、哪些只能人工冻结 |

---

## 数据可信度：每个数字都带来源

**四级来源**（`provenance`）：`measured` 实测 → `derived` 推算 → `declared` 人工声明 → `assumed` 占位。
没采集到的指标一律是 `null`，**绝不填 0 或估值**。

**三级结论**（`evidence_level`，由最弱的输入决定）：

| 级别 | 什么情况下才能给 |
|---|---|
| `hypothesis` | 有观测，但阈值是占位值 |
| `probable` | 观测与阈值都可实测/已冻结，或做过单变量端到端实验 |
| `confirmed` | 端到端 + kernel 级双重验证（当前为 0 个，这是刻意的） |

**三道闸门**决定一条结论能有多硬：

| 闸门 | 规则 | 效果 |
|---|---|---|
| 样本门槛 | 分位数标定至少 12 个样本 | n=4 的 `host_*` 判 `insufficient_samples`，不写入配置 |
| 留一验证 | 用 3 组标定、在留出组上诊断 | 判 `unstable` 的阈值降级为 `declared` |
| 证据上限 | 规则命中最到 `probable` | `confirmed` 只留给端到端 + kernel 级验证过的机会 |

结果（2026-11-16 设备档案接入后）：**16 个阈值 = 2 measured + 8 declared + 6 assumed**；
阈值标定覆盖率从 31% 升至 62%，余下 assumed 项的采集配方见"阈值标定"与"未实现"。
这不是没做完，是刻意的——没有留出数据验证，就不该说"确认"。

---

## 实测结论（2026-11-15 统一补采，rounds 0–2，op_summary 口径）

busy 统计已从 `TASK` 表切换到 `op_summary` 算子区间（`busy_source` 字段，见"已知数据约束"），
多 rank 合并口径（busy_any / busy_all / per-rank）：

| Workload | 窗口 | 忙碌（任一卡） | rank busy | 空闲归因 ACL 覆盖 | 方向 |
|---|---|---|---|---|---|
| decode-eager | 48.018 s | 57.8% | 52.0% / 7.4%⚠️ | 74.8% | communication_exposed |
| decode-graph | 3.291 s | 84.1% | 78.0% / 79.6% | 25.9% | undetermined |
| prefill-eager | 1.065 s | 35.8% | 20.7% / 20.7% | 31.4% | undetermined |
| prefill-graph | 0.546 s | 55.1% | 42.6% / 42.6% | 28.3% | undetermined |

⚠️ decode-eager rank1 的 7.4% 是采集伪影（见"已知数据约束"第 8 条），不是真实负载不对称。

### 站得住的结论

**1. Prefill 的 Eager→Graph 收益来自"少下发"，不是"算得快"。**
共享算子单次耗时比全部落在 0.99~1.14 之间（GroupedMatmul 1.14x、其余 ≈1.0x），
调用数 8034 → 8184（1.02x，Graph 反而略多）——收益全部来自调度/合并层面。

**2. Prefill 的空闲不是下发造成的。**
prefill-eager 64.2% 空闲里 Host ACL 只覆盖 31.4%，且两 rank 完全对称（20.7%/20.7%）。
此前"eager 空闲 72.8% vs graph 21.4% 的反转"是多 rank 合并修复前的伪影。

**3. Graph 把通信记录合并了，不是通信消失了。**
decode 通信字节量 21,089 MiB → 21,980 MiB（1.042x，守恒），
但 HCCL 记录数 132,413 → 16,399（8.1x 少）。通信占比/调用次数**跨模式不可比**，只能比字节量。

**4. Decode 的算子利用率远低于 prefill，根因是算术强度不是切分。**
设备实测峰值 282.5 TFLOPS（8192³ bf16）；prefill 大 GEMM 利用率 56.9%~87.9%，
decode 全部 ~10%——batch=1 的瘦 GEMM 算术强度低于 roofline 拐点（230.3 FLOP/Byte），
喂不满算力单元。提升方向是 batch/连续解码，不是调单个 matmul。

**5. decode-eager 的主要候选是通信暴露。**
重叠分析 compute=52.0% / comm=100.0% / comm_not_overlapped=48.0%，
方向性提示 `communication_exposed`（通信未与计算重叠占窗口 48%）。

### 不能下结论的

**1. Decode 的 Eager→Graph 端到端差异。**
decode-graph 空闲 15.9% vs decode-eager 42.2%，但 rank1 采集伪影（busy 7.4% vs 硬件 cycle
证明两卡计算量差 0.4%）使 eager 侧 busy 是下界估计；结论待复采确认。

**2. 通信占比不能解读为"通信被优化掉"。**
原因见结论 3；`comm` 命令给出的是字节量，不是占比。

---

## 能力细节

### 稳定窗口：为什么按轮次，不按空闲

采集脚本会在日志里打印 `ACTIVE_ROUND n start/end`，与算子时间戳同一时钟域。
统一补采用 5 warmup + 3 active，窗口取 **rounds 0–2**（`provenance=measured`，
窗口边界来自日志实测而非假设）；历史采集是 10 warmup + 5 active 取 round 1~4。
窗口质量校验实测：decode-eager 最大空洞 109.4 ms，标记 `degraded` 并写明原因。

不用"按空闲间隔自动切窗"的原因：Graph 的收益本身就在减少空闲，
用它切窗口等于用被比的东西定义比较基准，Eager 与 Graph 就不可比了。

窗口质量只做**校验**、不改窗口：解析时流式计算 `busy_ratio` / `max_gap_us`，
超过 100 ms 的空洞标记 `degraded` 并写明原因（decode-eager 为 `true`：最大空洞 109.4 ms，
其余三组 `false`）。

### 热点与诊断

- 热点：跨 Workload 矩阵，按算子合并 `Core Type` 变体，缺失填 `null` 而不是 0。
- 诊断：11 条 YAML 规则覆盖 6 类瓶颈（计算/访存/下发/同步/并行度/通信）；
  每条规则产出 `fired / not_matched / blocked / not_applicable` 审计记录。
- 判定不了就报**缺什么指标**，并给出采集建议（`missing_facts_top`）。

### 时间线：设备空闲归因

**busy 数据源已切换**（2026-11-16）：优先读各 rank 的 `op_summary` 算子区间
（epoch µs、单位统一、含通信行），多 rank 合并出 `busy_any` / `busy_all` / per-rank；
`TASK` 表仅在无 CSV 时回退（强制带告警）。`TASK` 表被证实不可信：
COMMUNICATION 行时长单位是 ns（其余 µs），且两 rank 任务数相同但时长和差 18.6 倍。
`timeline.json` 的 `busy_source` 字段记录实际来源，per-rank 带 `source` 逐 rank 标注。

空洞归因读 `CANN_API`（Host ACL 调用），对每个 ≥100 µs 的空洞看 Host 是否在下发：

| 归因 | 判据 | 含义 |
|---|---|---|
| `acl_busy` | Host 活动 ≥50% | 下发链路的候选证据 |
| `host_silent` | ≤10% | Host 没调 ACL；**本数据无法进一步归因** |
| `mixed` | 介于两者 | — |

统计取**并集**而非累加（同一空洞内多线程并发事件若累加，覆盖率会算出 4593%）。

`submit_per_task` 使用主库的 Host 下发调用数与同一 rank 的任务数
（`host_rank_task_count`）。`task_count` 仍是所有已分析 rank 的任务总数。
ACL 覆盖描述主库 Host 活动与已分析设备空洞的时间重叠。图重放可以一次提交多个任务。
Graph 下发与同步提示仅写入 workload 的 `diagnosis.json` 说明及 `timeline.json`。
CANN_API 和 TASK 的 `connectionId` 无法提供可用的逐算子关联。

用 `enable_mstx.sh` 启动配置了进程内 torch profiler 的 harness。补丁会挂到
vLLM-Ascend 的 worker 和 runner，在 `_ExperimentalConfig` 中启用 MSTX 和 DB 导出。
应用可以使用 `MSTX_TRACE_DIR` 作为 profiler 输出目录。
[Ascend PyTorch Profiler](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/910/devaids/Profiling/atlasprofiling_16_0033.html)

```bash
./scripts/enable_mstx.sh /path/to/new-trace -- python /path/to/configured_harness.py
./scripts/mstx/export_offline.sh /path/to/new-trace/rank_dir --with-text
opt-discovery timeline --export /path/to/capture --workload configs/workloads/qwen35-35b-a3b-910b/decode_graph.yaml \
  --mstx-db /path/to/same-rank-trace/ascend_pytorch_profiler_0.db --out /path/to/timeline.json
```

在线导出失败且保留 `PROF_*` 时，启动脚本会打印离线导出命令。离线导出会写入
指定目录，对要保留的原始采集，可将该目录复制到工作目录后执行。
独立库须来自同次采集的同一 rank，并有落在分析窗口内的有效区间。
单独复跑的 MSTX trace 应按它自己的采集窗口审阅。

### Eager vs Graph：先过契约，再比较

统一补采脚本将 `PROF_*` 直接写入批次目录。读取端也支持旧的 `PROF/PROF_*`，
可用 `--export` 直接指定带时间戳的批次目录。批量 `analyze` 按 workload YAML 的
`labels.collection_run_id` 查找采集清单，历史固定目录仍使用 `labels.export_dir`。
同一个 run ID 有多份采集时，将选定的一份放在单独的导出根下。
默认分析窗口以清单的 `fixed.window_rounds` 为准。实采轮数由 `active_rounds` 与日志核对，
可以通过 `--rounds 0-4` 显式分析全部五轮。

| 实采指标组 | 采集字段 | 分析事实 | 条件 |
|---|---|---|---|
| PipeUtilization | `cube_utilization(%)` | `cube_utilization` | 对有效样本取均值，再按百分比归一化 |
| ArithmeticUtilization | `aic_cube_fops` | `flops`、`aicore_utilization` | 利用率另需 Cube 流水时间与设备 BF16 峰值 |
| ArithmeticUtilization | `aiv_vector_fops` | `vector_flops` | Vector 峰值尚未标定，保留实测计数 |
| ArithmeticUtilization | `aic_cube_fops`、输入输出 Shape 与 dtype | `arithmetic_intensity` | 依赖估算搬运字节，口径为 Cube FLOPs / bytes |

同次采集只选一组 `--aic-metrics`。上表对应当前实采的 33/46 列导出。
仪表组记在 `meta.extra.collection.profiling`，
实际列覆盖见 `meta.extra.extra_metric_coverage`，其中 `missing_column`、`empty`、
`partial`、`complete` 和 `not_applicable` 区分缺列、全空、部分采到、完整和不适用。
缺测值保留为空，采集组变化会触发比较契约检查。
Cube 派生比率要求相关列覆盖同一批实例。分子或分母仅覆盖部分实例时保留已测计数，
暂不产出 `aicore_utilization` 或 `arithmetic_intensity`。

```bash
check-runs --a out/steady/<wl-a> --b out/steady/<wl-b>
```

检查阶段、硬件、输入形状、采集参数、窗口轮次、算子集合、通信字节量与记录数。
两侧都有采集清单时再检查契约哈希和仪表组。任一项目失败即判 `incomparable`。

`compare` 另外会自动告警：执行模式不同、Graph 下算子集合收缩（71 → 44 个，
"消失的算子" ≠ 未执行）、采集参数差异。

### 通信量守恒

```bash
comm --export <dir> --workload <yaml>
```

按 `globalTaskId` 去重（同一通信任务有 1~3 行 plane 重复），输出字节总量：

| Run | 原始行 | 唯一任务 | 字节 | 单任务 |
|---|---|---|---|---|
| decode-eager | 366,797 | 132,415 | 21,248 MiB | 164 KiB |
| decode-graph | 21,856 | 21,856 | 22,210 MiB | **1,041 KiB** |

字节量守恒、记录数差 6x、单条大 6.3x → 这就是"记录被合并"的定量证据。

### 图覆盖率

`FULL_AND_PIECEWISE` 不等于全图化。判据是 `op_summary` 的 `Model ID`
（有效值 = 图内，`4294967295` = 图外；eager 恒为无效值，所以覆盖率为 0 属预期）。

| Workload | 图内任务 | 按时间 | 主要 fallback |
|---|---|---|---|
| decode-graph | 95.8% | **58.5%** | `Add`(1789 ms) |
| prefill-graph | 60.5% | 67.4% | `ChunkGatedDeltaRule`(15 ms) |

两条读法：**任务覆盖率 ≠ 时间覆盖率**（图外任务更"长"）；
**prefill 的 GDN 路径完全在图外**（`ChunkGatedDeltaRule`/`CausalConv1d` 图内占比 0%），
这就是 PIECEWISE 的真实切分点。

### 源码映射

```bash
source-map --table out/steady/<wl>/operator_table.json
```

四层降级，逐层记录置信度，宁缺毋滥：

| tier | 触发条件 | 例 |
|---|---|---|
| `binding` | 命中 `csrc/torch_binding.cpp` 注册表（**0.95**） | `MoeGatingTopK` → `moe_gating_top_k` |
| `exact` | 源码同名符号 | `CausalConv1d` → `csrc/.../causal_conv1d_310_torch_adpt.h:19` |
| `snake` | CamelCase → snake_case | `GroupedMatmul` → `grouped_matmul_*` |
| `prefixed` | `npu_` / `aclnn_` 前缀 | `FusedInferAttentionScore` → `npu_fused_infer_attention_score` |

注册表解析：77 条 `ops.def/impl`、72 个唯一函数，**77/77 定位到 file:line**，
输出形如 `实现文件:行 ← schema @ csrc/torch_binding.cpp:行`。

实测 71 个算子：命中 37（`binding` 9 / `exact` 20 / `prefixed` 3 / `snake` 5），
未命中 34（`builtin` 21、`runtime` 3、未匹配 10）。
**未命中不是失败**：CANN 内置算子和 HCCL 运行时算子本来就没有源码定义。

### 验证与回填

```bash
bench --harness $NPU_STACK/qwen35-decode-only.py --out out/bench/eager --label eager
bench-compare --a out/bench/a/benchmark.json --b out/bench/b/benchmark.json
# 只记录观察，不升级证据：
feedback --opportunities out/steady/<wl>/opportunities.json \
         --compare out/bench/bench_compare.json --operator <Op> --change "..."
# 结构化验证（唯一升级路径）：
feedback --opportunities out/steady/<wl>/opportunities.json \
         --validation validation.json
```

`bench` 直接跑现有 harness（它们没有 CLI 参数，只打印 `ACTIVE_ROUND`），解析整轮延迟；
`bench-compare` 判定阈值写死（<3 轮 → `insufficient_samples`；中位延迟变化 <±5% → 无显著差异）。

反馈分两层，且**不能同时提供**：`--compare` 是**裸观察**，只写 `measured`
（`source=benchmark_observation`，`verified=false`），不改变 `evidence_level`；
`--validation` 是结构化 `ValidationBundle`，由 `schemas.evidence_gate.promote_evidence`
统一校验机会、生成实例、算子、workload、change、baseline/candidate、输入、可比性、
正确性、样本、不确定性、来源和产物摘要。两者同时提供会直接退出 2，避免用整轮
benchmark 去匹配单算子/kernel 效果。

结构化 bundle 必须显式引用机会的 `opportunity_instance_id`（生成时写入的 UUID，
序列化和重读保留）。单个算子实验（`operator`）不是端到端：kernel + operator 最多到
`probable`；只有 kernel + `model` 或 `serving` 且产物摘要独立时才能 `confirmed`。
样本阈值 `--min-effect` 必须是 `[0, 1]` 内的有限值并随审计记录；即使允许 0 阈值，
零改善也不会被判为收益。

| 场景 | 状态 | 证据级别 |
|---|---|---|
| 裸单算子观察 `b_faster`（≤ −5%） | `accepted` | **不变** |
| 裸单算子观察 `b_slower`（≥ +5%） | `rejected` | **不变** |
| **工作负载级 A/B** | `validating` | **不变**（不能归因到单个算子） |
| 结构化单条有效实验（含 operator） | `accepted` | 升一级，**上限 probable** |
| 结构化 kernel + 独立 `model`/`serving` 有效结果 | `accepted` | 可 `confirmed` |

历史无审计的 `confirmed`、缺少 `instance_id`/`is_synthetic` 的旧记录会在读取/报告时
保守降级为 `probable（不可信）`，并在报告中明确标注；缺失或摘要变化的产物同样会
触发降级。完整契约、正例和历史数据处理见
[AE-07 迁移说明](docs/engineering/phase6/migration.md)。

### 阈值标定

```bash
validate-thresholds --dir out/steady-calib4
calibrate --dir out/steady-calib4 --validation out/threshold_validation_bw.json
```

阈值三类来源（provenance 显式标注，"能测的才标定"）：

| 类别 | 项 | 值 | 依据 |
|---|---|---|---|
| **设备常数**（micro-benchmark 实测） | `arithmetic_intensity_low` | 230.3 | roofline 拐点 = 282.5 TFLOPS ÷ 1226.5 GB/s，留一漂移 0% |
| | `aicore_utilization_ceiling` | 0.10 | 搬运类算子利用率 p75（天然不用 cube 的噪声底） |
| **分布分位数**（workload 相对） | `aicore_utilization_saturated` | 0.55 | 计算类算子利用率 p90（prefill 87.9% / decode ~10% 的混合 p90） |
| | `low_core_ratio` | 0.95 | kernel 活跃核占比 p50（Block Num/24，n=208） |
| | `bandwidth_utilization_saturated` | 1.00 | shape×dtype 估算带宽 p90（撞夹取上限，**上界性质**） |
| | `compute_pipe_time_fraction_*` | 0.95/0.05 | 流水占比分布 |
| 策略值（不标定） | `small_kernel_time_us`、`high_call_count`、`communication_share_threshold` | — | 与设备能力无关 |

利用率分母用 `aicore_time`（流水活跃时间）而非任务时长——实测小算子
Task Duration 可小于 aicore_time，用错分母利用率会 >100%。

**适用边界**：设备常数可跨 workload 共享；分布分位数是"本地正常水位"，
同一 workload 族内共享已验证（留一结论稳定性 pass），跨模型/跨卡必须重标。
当前默认覆盖 10 项（measured=2 / declared=8），`bandwidth_utilization_saturated`
待采集真实 HBM 字节后重标（现为 shape×dtype 估算上界）。

---

## 统一补采：消除参数不一致

历史四组除了"执行模式"这个自变量外，还有不该有的差异：`max_num_seqs` 只有 Graph 两组
显式设了 32（Eager 用默认值）；decode 的 rank1 没有导出 CSV；采集命令也没留痕。
`check-runs` 能把这些问题报出来，但只能报警，修不了数据本身。

`configs/collection_contract.yaml` 把除自变量外的所有条件固定下来：

| 类别 | 固定值 |
|---|---|
| 模型/并行/精度 | 同一模型路径、TP=2、bf16、seed=42 |
| 运行参数 | `max_num_seqs=32`（四组统一）、`max_model_len=8192`、`gpu_memory_utilization=0.85`、并发 1 |
| 轮次 | 5 warmup + 3 active（2026-11-15 补采），分析窗口取 rounds 0–2 |
| 仪表 | 四组同一套 msprof 开关，避免"采集开销"变成新变量 |
| 环境 | 显式清掉 `VLLM_HUST_GDN_OUTPUT_NORM_FUSED` 等 ABBA 实验遗留开关 |

```bash
python scripts/recollect_contract.py --dry-run                    # 先看命令
python scripts/recollect_contract.py --wait-idle --out-root $PROF_ROOT/unified
python -m opt_discovery.cli check-collection --dir $PROF_ROOT/unified
```

脚本会：① 从 `$NPU_STACK` 复制 harness 并注入统一参数（**不改原文件**）；
② 串行采四组；③ 每组写 `collection_manifest.json`（契约 sha256、实际命令、环境变量清理、
两 rank CSV 与轮数自检）。`check-collection` 据此判定 `pass/fail`，不再靠人工填 YAML 核对。

唯一允许不同的仍是自变量：`mode`、`graph_mode`、`output_length`。

## 已知数据约束

这些都是实测撞出来的，不是假设：

1. **`op_summary` 按 100 万行切片**：decode 每 rank 2 片，必须按自然序合并，否则只读到一半。
2. **`op_statistic` 不含 HCCL**：只解析它会漏掉约 30% 的通信时间（`hcom_*` 只在 `op_summary`）。
3. **同一 `OP Type` 按 `Core Type` 拆多行**（MatMulV3 = AI_CORE 717 + MIX_AIC 54）：不聚合会重复计数。
4. **流水时间占比可以 >1**：跨实例/跨流水重叠，引擎会在证据里显式标注，不能按单实例解释。
5. **decode 的 rank1 没有 CSV**：`end_info` 缺失导致分析阶段失败（日志有 `number_mapping Unknown key` 刷屏），
   只有原始 `msprof_*.json`。可用 `parse-sqlite` 从 `op_counter.db::op_report` 回填（与 CSV 交叉验证 70/70 键零差异），
   但该 rank 与 rank0 不可合并（调用数 0.999x、累计时间 0.098x）。
6. **MSTX 未开启**：四组导出只有 `ENUM_MSTX_EVENT_TYPE` 枚举表，没有 `MsprofTx`，
   所以 `host_silent` 只能停在"无法进一步归因"。
7. **Graph 下算子集合收缩**（71 → 44）：被图捕获/融合的算子不再单独上报，
   "消失的算子"不等于未执行。
8. **`COMMUNICATION` 行时长单位是 ns——`op_summary` CSV 同样存在**（不只 `TASK` 表）：
   统一补采实测 `hcom_allReduce` 单行 110,462,688 µs（应为 ~110 ms）。
   解析路径 `csv_mapping.py` 用 `COMMUNICATION_DURATION_SCALE=0.001` 缩放，
   `timeline.load_op_intervals` 复用同一常量。
9. **33 列精简导出**（2026-11-15 统一补采起）：无 `aic_mte*/aiv_vec_time/cube_utilization`
   分列，新增 `Block Num / aic_cube_fops / aiv_vector_fops`（flops 实测单位为 FLOPs，
   已与 8192³ micro-benchmark 对齐）。旧 46 列专属的 extra_metrics 自动缺失不阻塞解析。
10. **decode-eager rank1 的 `Task Duration` 列损坏**：短 kernel 正常、长 kernel 被截断
    （MatMul p99=34.7µs vs rank0 2980µs；同一 task_id 硬件实测 146,589µs vs CSV 34.1µs）。
    硬件 `aic_total_time` 两 rank 差 0.4% 证明计算量相同——busy 7.4% 是伪影不是"某卡在等"。
    分析器以 `rank_duration_inconsistent` 标记，不做"某卡在等"解读。
    免重采恢复（aic_metrics 回填）只能验证对称性，覆盖率 76% + task_id 歧义，
    得不到精确数字。graph 模式 rank1 正常。
11. **小算子 `Task Duration` 可小于 `aicore_time`**（如 2.3µs vs 19.7µs）：
    利用率类指标分母必须用流水活跃时间。
12. **多 rank 合并的空洞从首个忙碌区间起算**：epoch 原点到首个算子之间不是设备空洞，
    否则会产生 1e15 量级伪 gap（已修复）。

---

## 目录

```
opt_discovery/
├── primitives.py     低层时间/区间类型（Window/Interval），不依赖 analyzer
├── events.py         共享 KernelEvent / TASK / Host 事件读取器（流式）
├── schemas/          数据契约：单位/指标+来源/算子表/Workload/证据/机会
├── collectors/       目录发现、workload.log 轮次、MSTX 打点
├── parsers/          CSV（映射驱动、流式、多切片）、sqlite 回填、合成数据
├── analyzer/         窗口、热点、对比、时间线、通信量、图覆盖率、契约
├── diagnosis/        事实层、规则引擎、阈值覆盖层
├── discovery/        诊断 → 优化机会（按分数排序后编号）
├── source_analysis/  注册表解析 + 源码索引
├── experiment/       benchmark 执行/对比/回填、阈值标定与留出验证
├── report/           report.md 与两个 CSV 扁平表
└── cli.py            22 个子命令
configs/
├── rules/            11 条瓶颈规则 + 16 个阈值（含来源与依据）
├── parsers/          已验证的列映射（op_statistic / op_summary，含 computed 派生指标）
├── workloads/        4 组 Workload 描述
└── thresholds/       设备档案（device_profile.json）+ 标定覆盖文件（measured / declared）
scripts/              MSTX 打点补丁、带打点的复采模板、复采验证流程
tests/                369 个用例（352 个 CPU 用例 + 17 个真实数据集成用例）
tests/regression/     AE-02 历史问题回归（R01–R12，CPU-only、fixture 脱敏）
```

## 开发

```bash
# 全新 CPU 环境：安装开发依赖（Python >=3.10，CI 覆盖 3.10 / 3.12）
python -m pip install -e ".[dev]"

make check        # ruff + arch-lint + mypy + CLI validate（无需 NPU / 真实数据）
make verify       # 普通测试 + 回归 + synthetic demo
make verify-real  # 真实数据回归；缺数据打印 BLOCKED 且退出码 2（不是 PASS）

# 干净环境验证：复制工作树 + 新建 venv + 从零安装 + 全部检查
make clean-env-check PYTHON=python3        # 默认解释器
make clean-env-check PYTHON=python3.10     # 最低支持版本
# 本机没有 3.10 时：python -m pip install uv && uv python install 3.10
```

底层命令仍可单独执行：

```bash
python -m pytest -q                      # 352 passed, 17 skipped（缺真实导出时）
python -m ruff check .                   # 静态检查（E4/E7/E9/F/I/B）
python -m mypy                           # 类型检查（opt_discovery，python_version=3.10）
python -m opt_discovery.cli validate     # 规则/阈值/Workload/列映射自检
python -m opt_discovery.cli demo         # 合成数据跑通全链路（不代表真实性能）
```

`verify-real` 需要设置 `OPT_DISCOVERY_DATA_ROOT` 指向四组统一补采的根目录；目录不全会
直接 `BLOCKED`。判定基于 pytest 的 `--junitxml` 结构化计数（不解析文本摘要）：全部/部分用例
skip 时退出码为 3，未收集到用例或失败为 1，绝不把跳过显示为 PASS。CI 用
`--expect-blocked` 断言「缺数据时必须 BLOCKED」（内部状态判定，与 pytest 退出码 2 解耦）。
回归矩阵（历史问题 → 测试节点 → 独立预期）见 `docs/engineering/regression-matrix.md`。

### 自动化运行契约（`--strict` + `run_manifest.json`）

普通模式保持原有行为；自动化/CI 请加 `--strict`，按运行状态返回退出码：

| 状态 | 含义 | strict 退出码 |
|---|---|---|
| `success` | 所有必需步骤通过、对比可比 | 0 |
| `failed` | 缺输入或必需步骤失败 | 1（缺输入入口为 2） |
| `partial` | 必需步骤未完成，但有部分可用产物 | 3 |
| `incomparable` | 质量闸门/采集契约未通过，不得出结论 | 4 |

```bash
python -m opt_discovery.cli analyze ... --strict      # 默认写 <out>/run_manifest.json
python -m opt_discovery.cli compare ... --strict      # 不可比 -> 4；普通模式仍为 0
python -m opt_discovery.cli check-ranks ... --strict  # rank 不一致 -> 3；普通模式仍为 0
python -m opt_discovery.cli check-manifest --path <out>/run_manifest.json
```

`run_manifest.json` 记录：独立 `run_id`、总体状态、每个阶段（`passed/failed/skipped/not_requested`、
是否必需、`reason_code`）、逐 Workload 状态与质量标记、`comparison_status`、产物相对路径 + sha256、
输入逻辑标签与配置指纹、工具版本。**不写绝对路径/秘密**；旧输出目录里未被本次清单列出的文件，
不会被当作本次产物（`check-manifest` 可校验哈希，发现被修改/缺失的产物）。

质量闸门：rank 不可合并（`rank_merge_inconsistent`）、rank 合并未验证（`rank_merge_unverified`）、
时长截断（`rank_duration_inconsistent`）为阻断型标记，随 `ParseMeta.extra["quality_flags"]` 落盘；
`compare` 读取后直接判 `comparable=False`、不输出 deltas/`total_ratio`，并在 `reasons` 中说明原因；
比较入口同时应用采集契约（与 `check-runs` 同一实现，警告会进入比较报告的 `warnings`），
轮次/阶段/硬件不一致时同样不可比。`check-runs` 只检查采集契约；质量标记闸门在 `compare`。
同一输出目录不支持并发运行（无文件锁）；需要并行请使用独立 `--out`/`--out-dir` 与 `--manifest`。
`summary`/`report` 默认要求 `run_manifest.json`：可用 `--manifest` 指定外置清单，
确需扫描无清单的旧目录时必须显式加 `--allow-legacy`；损坏/缺失的清单不会被静默忽略。
已知阻断码由 code 决定最低严重性（`severity=degraded` 也会被拒）；标记字段无法解析按
`quality_flag_invalid` 阻断，不会静默忽略。timeline 失败/缺失会转成 blocking 标记随表落盘，
独立 `compare` 读取后同样拒绝。

加一个 Workload：在 `configs/workloads/<model>/` 放 YAML，填 `labels.export_dir`，
填完采集条件后置 `verified: true`（未核对的组会被判"不可比"）。

加一列指标：改 `configs/parsers/*.yaml` 的 `extra_metrics`，不动代码；
缺列会记 warning，多余列会记 `unmapped` 帮助修正。

加一条规则：改 `configs/rules/bottleneck_rules.v1.yaml`；
字面量阈值必须写 `justification`，无 `required` 的规则必须 `allow_hypothesis_only: true`。

## 未实现

| 项 | 为什么没做 |
|---|---|
| 关键路径分析 | 现在算的是"设备有没有空"，不是"哪个算子在关键路径上" |
| 逐算子真实 HBM 字节 | 当前 bytes 为 shape×dtype 估算（上界近似）；带宽阈值只能标到上界 1.0 |
| Event/Barrier 级同步（`sync_wait_ratio`） | MSTX 已验证可用（`torch_npu.profiler` + `mstx=True`），待带打点重采 |
| decode-eager rank1 复采确认 | CSV 长 kernel 截断缺陷待复现，复现后可向 msperf 上游报告 |
| phase 族阈值拆分 | 触发条件：`level_changes` 持续非零或每组 workload ≥3；当前样本不足 |
| MSTX 接入 vLLM 采集脚本 | vLLM-Ascend wrapper 默认 `msprof_tx=False`，需改 wrapper 或自定义 profiler |
| LLM 分析 | 证据已结构化（诊断+源码位置+实测反馈+阈值可信度），可接但优先级低于数据补齐 |
| 端到端收益承诺 | 需要 Serving 层 TTFT/TPOT 交叉验证 |

---

## 附录：命令全表

| 命令 | 用途 |
|---|---|
| `validate` | 规则/阈值/Workload/列映射自检 |
| `demo` | 合成数据自测（强制标记 `is_synthetic`） |
| `parse-csv` / `parse-sqlite` | 解析 CSV / 从 sqlite 回填 |
| `rounds` | 查看 harness 记录的轮次与窗口 |
| `analyze` | 端到端跑全部 Workload |
| `timeline` | 设备空闲归因 |
| `compare` / `check-runs` | 算子级差分 / 采集契约闸门 |
| `check-ranks` | 同一 Workload 两 rank 是否可合并 |
| `graph-coverage` | 图覆盖率与 fallback |
| `comm` | 通信字节量（跨模式唯一可比） |
| `source-map` | 算子 → 源码位置 |
| `bench` / `bench-compare` | 跑 harness / 比较两次结果 |
| `feedback` | 把 A/B 结果回填到机会 |
| `calibrate` / `validate-thresholds` | 阈值标定 / 留一验证 |
| `report` / `summary` | 生成 report.md / 两个 CSV |
| `diagnose` | 对已有算子表重跑诊断 |
