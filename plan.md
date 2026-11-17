# Optimization Opportunity Discovery 的可行实施方案

建议采用先做离线分析工具，再逐步演进为 Agent的路线。第一阶段不要急着让 LLM 自动读源码、修改 Kernel，而是先把 Profiling 数据转化为可信的优化机会。

整个系统的目标是：

> 输入推理引擎的 Profiling 数据、Benchmark 数据和源码仓库，自动输出：值得优化的算子或执行路径、性能瓶颈、根因证据、候选优化方向、预期收益和验证方案。

结合你目前的 Qwen3.5 35B-A3B + Ascend 910B 环境，以及已有的四组 Prefill/Decode、Eager/Graph Profiling 数据，我建议分成四个阶段实施。

整体实施路线

1. 阶段一：数据采集与标准化

   解析 Profiler、Benchmark 和模型配置，构建统一数据格式。

2. 阶段二：性能分析与瓶颈诊断

   自动识别热点、执行开销、计算/访存/调度/同步瓶颈。

3. 阶段三：源码关联与机会生成

   将性能事件映射到推理引擎实现，生成有证据支持的优化假设。

4. 阶段四：优先级排序与实验反馈

   输出优化任务，执行 Benchmark，验证收益并更新知识库。

## 一、先明确第一版的交付目标

第一版（MVP）不需要实现一个能自主修改代码的完整 Agent。只需要实现一个命令行工具：

```
python discovery.py \
    --profile-dir ./profiles/qwen35 \
    --workload-config ./configs/qwen35_910b.yaml \
    --source-dir ./vllm-ascend \
    --output-dir ./reports
```

自动生成以下结果：

| 输出文件               | 内容                         |
| ---------------------- | ---------------------------- |
| `workload_summary.csv` | 不同工作负载的整体性能       |
| `operator_summary.csv` | 算子耗时、调用次数、时间占比 |
| `hotspots.json`        | 热点算子及其适用工作负载     |
| `diagnosis.json`       | 瓶颈类型、证据和置信度       |
| `opportunities.json`   | 候选优化机会及优先级         |
| `report.md`            | 人类可读的分析报告           |

第一版的验收标准：

1. 能解析已有 Profiling 数据，不依赖手工复制数据。
2. 能区分 Prefill、Decode、Eager、Graph 等执行模式。
3. 能识别热点算子和不同工作负载下的热点变化。
4. 能给出瓶颈诊断及对应证据，而不是只给出优化建议。
5. 能生成可以执行的 Benchmark 验证方案。

## 二、阶段一：构建 Profiling 数据采集与标准化模块

这是整个系统最重要的基础。后续的诊断和 Agent 推理质量，都取决于输入数据是否可靠。

### 1. 收集三类数据

A. Workload 数据

描述 Profiling 数据是在什么条件下采集的。

建议每次运行都保存一份配置：

```
workload_id: qwen35-prefill-graph-pw

model:
  name: Qwen3.5-35B-A3B
  dtype: bfloat16

hardware:
  device: Ascend 910B
  device_count: 1

execution:
  phase: prefill
  mode: graph
  graph_mode: FULL_AND_PIECEWISE
  npugraph_ex: true

input:
  batch_size: 1
  input_length: 1024
  output_length: 1

environment:
  vllm_version: xxx
  vllm_ascend_commit: xxx
  torch_version: xxx
```

这里要特别注意：

- `1024→1` 是请求级输入输出长度，不是所有算子的实际 Shape。
- 每个算子的真实 Shape 仍然需要从 Profiling 或算子调用信息中获取。
- 不同执行模式之间的对比，需要保证模型、输入、硬件和统计口径一致。

B. Operator 数据

从 Ascend Profiler 中提取：

| 字段            | 作用                               |
| --------------- | ---------------------------------- |
| operator_name   | 算子名称                           |
| kernel_name     | 实际执行的 Kernel                  |
| input_shape     | 输入 Shape                         |
| dtype           | 数据类型                           |
| calls           | 调用次数                           |
| total_time      | 累计执行时间                       |
| avg_time        | 平均执行时间                       |
| min/max_time    | 延迟分布                           |
| device_time     | Device 执行时间                    |
| task_type       | Kernel / Memcpy / Communication 等 |
| source_location | 可选，源码位置                     |

第一版不必强求所有硬件指标。先确保算子名称、调用次数、执行时间、Shape 能稳定解析。

C. Timeline 数据

除了算子统计，还要保留时间线信息。

重点提取：

- CPU 侧算子提交时间。
- NPU Kernel 执行时间。
- Stream 和 Task 之间的依赖关系。
- Event、Barrier、同步事件。
- H2D/D2H 或设备间通信。
- NPU 空闲区间。

这是为了避免一个常见误判：

假设某个算子的累计执行时间很长，并不意味着它一定处在端到端关键路径上。多 Stream 并行执行时，算子累计时间可能大于整体执行时间。

因此至少区分：

- Operator Total Time：算子累计时间。
- Device Busy Time：设备实际忙碌时间。
- Critical Path Time：关键路径时间。
- End-to-End Latency：端到端延迟。

### 2. 建立统一数据 Schema

建议先使用 JSON 或 SQLite 存储，不需要一开始就引入复杂数据库。

例如：

```
{
  "workload_id": "qwen35-prefill-graph-pw",
  "operator": "Matmul",
  "kernel": "MatmulKernel",
  "shape": [1024, 4096, 4096],
  "dtype": "bfloat16",
  "calls": 32,
  "total_time_us": 1200,
  "avg_time_us": 37.5,
  "device_time_us": 1200,
  "metrics": {
    "flops": null,
    "memory_bytes": null,
    "occupancy": null
  },
  "source": {
    "file": null,
    "line": null
  }
}
```

注意：上面的数值仅用于展示数据格式，不代表实际 Profiling 结果。`null` 表示没有可靠数据，不能用估算值冒充实测值。

### 3. 实现 Profile Parser

建议先完成三个 Parser：

```
collectors/
├── ascend_profiler.py
├── benchmark_parser.py
└── workload_parser.py

parser/
├── operator_parser.py
├── timeline_parser.py
└── normalize.py
```

模块职责：

- `ascend_profiler.py`：读取原始 Profiler 文件。
- `operator_parser.py`：提取算子级统计。
- `timeline_parser.py`：提取 CPU/NPU 执行时间线。
- `benchmark_parser.py`：读取端到端性能数据。
- `normalize.py`：统一字段、时间单位、算子名称和 Workload ID。

阶段一验收： 将四组已有 Profiling 数据全部解析成统一格式，并能生成算子统计表。

## 三、阶段二：性能分析与瓶颈诊断

这一阶段先实现一个基于规则的分析引擎，不要一开始就依赖 LLM 判断性能瓶颈。规则分析的结果更容易复现，也方便后续验证 Agent 的判断是否正确。

### 1. Hotspot Detection：热点识别

建议从三个维度识别热点，而不是只按算子平均耗时排序。

维度 A：累计耗时

\[ T_i^{total}=N_i\times \overline{T_i} \]

\[ P_i=\frac{T_i^{total}}{\sum_j T_j^{total}} \]

其中 \(N_i\) 是调用次数，\(\overline{T_i}\) 是平均执行时间。

它可以找出占用总算子执行时间较多的算子，但在并行场景中，不能直接解释为端到端耗时贡献。

维度 B：单次耗时

识别单次执行特别慢的算子。例如一个算子只执行一次，但耗时占据整个 Prefill 的较大比例。

维度 C：跨 Workload 表现

对四组数据分别分析：

| 对比                   | 主要目的                                |
| ---------------------- | --------------------------------------- |
| Prefill Eager vs Graph | 判断 Graph 是否减少了执行开销           |
| Decode Eager vs Graph  | 判断 Graph 是否减少了小算子的调度开销   |
| Prefill vs Decode      | 判断不同推理阶段的热点变化              |
| 相同算子不同 Shape     | 判断算子是否存在特定 Shape 下的性能问题 |

最后形成一张热点矩阵：

| Operator         | Prefill Eager | Prefill Graph | Decode Eager | Decode Graph |
| ---------------- | ------------- | ------------- | ------------ | ------------ |
| Matmul           | 高            | 高            | 中           | 中           |
| Attention        | 高            | 高            | 高           | 高           |
| Elementwise      | 中            | 低            | 高           | 中           |
| Layout Transform | 中            | 中            | 中           | 中           |

这张表里的高、中、低只是展示格式，实际需要根据测量数据和统一阈值生成。

### 2. Bottleneck Diagnosis：瓶颈诊断

建议第一版支持六类瓶颈。

| 瓶颈类型              | 典型证据                     | 候选优化方向                     |
| --------------------- | ---------------------------- | -------------------------------- |
| Compute-bound         | 计算吞吐接近设备可达水平     | 指令、矩阵计算、Tiling           |
| Memory-bound          | 带宽利用率高、访存效率低     | 数据布局、访存合并、减少冗余加载 |
| Launch-bound          | CPU 提交和调度开销明显       | Graph、算子融合                  |
| Synchronization-bound | Event、Barrier、等待时间明显 | 减少同步、调整 Stream            |
| Parallelism-bound     | 并行度不足、设备利用不充分   | 调整 Tiling、任务划分            |
| Communication-bound   | 通信时间或等待时间明显       | 通信计算重叠、通信算法优化       |

不要仅凭一个指标就确定瓶颈。例如：

- Kernel 很短，不等于一定是 Launch-bound。
- 带宽利用率低，不等于一定不是 Memory-bound，也可能是访存不连续或并行度不足。
- 算子执行时间高，不等于一定是 Compute-bound。

因此建议采用规则产生候选诊断，证据完整度决定置信度的方式。

例如：

```
def diagnose_operator(profile):    candidates = []    if profile.cpu_submit_time is not None:        if profile.cpu_submit_time > profile.device_time * 0.5:            candidates.append({                "type": "launch_bound",                "evidence": [                    "CPU submit time is significant"                ]            })    if profile.memory_bandwidth is not None:        if profile.memory_bandwidth > profile.bandwidth_threshold:            candidates.append({                "type": "memory_bound",                "evidence": [                    "Measured bandwidth is close to the configured threshold"                ]            })    return candidates
```



这只是规则框架。实际实现时要根据 Ascend Profiler 指标定义、单位和采集条件确定阈值，不能直接照搬示例中的 `0.5`。

### 3. 增加 Eager / Graph 专项分析

这是你当前四组 Profiling 数据最值得先做的分析。

建议把 Eager 和 Graph 的差异拆成三部分：

1. Host 侧开销变化：Python 调度、Host 算子提交、Launch 等。
2. Device 侧执行变化：实际 Kernel 数量、Kernel 执行时间、融合情况。
3. 执行覆盖变化：哪些 Shape 命中 FULL Graph，哪些走 Piecewise 或 Eager。

最终不要只输出“Graph 比 Eager 快了多少”，而应该输出：

```
观察：
Decode Graph 的端到端延迟下降。

证据：
1. Host 侧提交事件数量下降。
2. 部分小算子的执行路径发生变化。
3. Device Kernel 执行时间变化较小。

初步判断：
收益可能主要来自 Host 调度开销下降，
而不是单个 Device Kernel 的计算效率提升。

下一步：
进一步检查 Graph 覆盖率和关键路径。
```

这类结论必须由实际 Profile 支持，不能预设 Graph 一定减少了多少开销。



## 四、阶段三：源码关联与优化机会生成

这一阶段才开始引入源码分析和 LLM。

核心任务是：从 Profiler 中的 Kernel 找到它在 vLLM-Ascend 中的实现位置，再结合瓶颈诊断提出优化假设。

### 1. 建立 Kernel 到源码的映射

建议建立如下映射链：

Model Forward

模型计算逻辑

vLLM-Ascend Operator

算子调用与后端适配

Backend Implementation

Torch / ACLNN / 自定义算子 / Triton 等

Compiler / Graph

图优化、算子融合、执行模式

Actual NPU Kernel

Profiler 中观察到的设备执行事件

第一版可以采用半自动方式：

- 通过算子名称搜索源码。
- 通过 Python 调用栈追踪调用路径。
- 通过算子注册信息定位 Backend。
- 通过 Profiler 的 Kernel 名称、Task 类型和调用关系辅助匹配。
- 对无法可靠匹配的事件标记为 `unmapped`，不要强行关联。

建议映射结果保存为：

```
{
  "operator": "Attention",
  "framework_entry": "path/to/model_or_attention.py",
  "backend_entry": "path/to/backend.py",
  "kernel_name": "actual_kernel_name",
  "mapping_confidence": "medium",
  "mapping_evidence": [
    "operator call stack",
    "backend dispatch"
  ]
}
```

其中路径和名称需要从实际仓库及 Profile 中提取，不应该预先写死。

### 2. 建立瓶颈到优化策略的映射

例如：

| 发现的问题              | 生成的优化假设                     | 需要检查的内容                       |
| ----------------------- | ---------------------------------- | ------------------------------------ |
| 高频小算子              | 算子融合可能降低调度和中间数据开销 | 是否已有编译器融合、是否位于关键路径 |
| Layout Transform 耗时高 | 减少不必要的 Layout 转换           | 上下游算子是否能使用相同布局         |
| Memory-bound            | 优化访存方式                       | 是否存在非合并访问、冗余读写         |
| Tiling 效率低           | 调整任务划分                       | Shape、核数、Tile Size               |
| Synchronization-bound   | 减少同步或调整执行顺序             | Stream、Event、Barrier 依赖          |
| Graph 覆盖不足          | 优化 Graph 捕获范围或 Shape 覆盖   | 哪些执行路径没有命中 Graph           |

尤其要加入一条检查：编译器是否已经完成了这个优化。

例如，发现多个连续 Elementwise 算子时，不能直接建议手工融合。还需要确认：

1. 编译器是否已经融合。
2. 融合后的 Kernel 是否仍然存在。
3. 中间 Tensor 是否真的发生了额外读写。
4. 融合后是否会增加寄存器压力或降低并行度。

### 3. 使用 LLM 生成优化机会

LLM 不直接读取整个仓库，也不直接修改 Kernel。第一版只给它提供结构化证据：

```
输入：
1. Workload Profile
2. Operator Profile
3. Timeline 分析结果
4. 瓶颈规则诊断结果
5. 关联源码片段
6. 跨 Workload 对比数据

输出：
1. 性能问题
2. 根因假设
3. 源码证据
4. 优化方向
5. 预期影响范围
6. 缺少的验证数据
```

建议固定输出格式：

```
{
  "opportunity_id": "OPP-001",
  "operator": "LayoutTransform",
  "workload": "qwen35-decode-graph",
  "bottleneck": "memory_or_launch",
  "observation": "该算子在当前工作负载中具有较高累计耗时",
  "hypothesis": "可能存在冗余布局转换",
  "evidence": [
    "Profiler operator statistics",
    "Source call path"
  ],
  "missing_evidence": [
    "Actual memory traffic",
    "Whether compiler fusion has removed intermediate operations"
  ],
  "suggested_actions": [
    "Inspect producer-consumer layout compatibility",
    "Check compiler fusion result"
  ],
  "validation_plan": [
    "Compare operator latency",
    "Compare end-to-end latency",
    "Check numerical correctness"
  ],
  "status": "discovered"
}
```

注意 `memory_or_launch` 在这里代表待验证的候选诊断，而不是已经确定的根因。LLM 必须能够输出“证据不足，需要补充 Profile”。



## 五、阶段四：机会排序与实验反馈

发现很多优化机会后，需要回答一个问题：先优化哪个？

### 1. 建立优化优先级评分

可以先使用一个简单的评分模型：

\[ Score_i = \frac{Impact_i \times Feasibility_i \times Confidence_i} {Cost_i} \]

各指标取 0～1：

- `Impact`：预计对目标 Workload 的性能影响。
- `Feasibility`：当前硬件和框架下的实现可行性。
- `Confidence`：根因证据的可信程度。
- `Cost`：实现、调试和验证成本。

需要注意，`Impact` 不应该简单等于算子耗时占比。它还要考虑：

- 是否处于关键路径。
- 优化能否覆盖多个 Workload。
- 是否会影响其他 Shape。
- 是否可能引入资源竞争。
- 对端到端延迟和吞吐量的影响。

初期可以由规则和人工给出分数，积累真实实验结果后再调整评分模型。

### 2. 自动生成实验计划

每个优化机会都要对应一个可执行实验。

例如：

| 实验项       | 内容                               |
| ------------ | ---------------------------------- |
| 优化对象     | 某个高频 Elementwise 算子          |
| 优化假设     | 融合可以减少中间 Tensor 和调度开销 |
| 修改范围     | 算子实现及调用路径                 |
| Kernel 验收  | 单算子延迟、访存量、资源占用       |
| Model 验收   | Prefill / Decode 延迟              |
| Serving 验收 | TPOT、TTFT、吞吐量                 |
| 正确性       | 与基线进行数值对比                 |

你之前关注过 Kernel → Model → Serving 三层验收，这里可以直接沿用：

- Kernel：确认算子本身确实变快。
- Model：确认模型阶段整体受益。
- Serving：确认真实请求负载下仍然受益。

### 3. 实验结果反馈给 Discovery

优化实验完成后，保存：

```
{
  "opportunity_id": "OPP-001",
  "baseline_latency_us": 100,
  "optimized_latency_us": 85,
  "kernel_speedup": 1.176,
  "model_latency_before_ms": 100,
  "model_latency_after_ms": 98,
  "correctness": "passed",
  "serving_validation": "pending",
  "result": "kernel_gain_model_gain_small"
}
```

示例数字仅用于说明格式。

这一步很重要：如果 Kernel 提升明显，但 Model 端到端收益很小，系统应该记录这种情况，并在以后降低类似优化机会的收益预期，而不是只记录 Kernel 的成功。



## 六、代码工程结构

建议第一版采用 Python 实现，避免过早引入复杂 Agent 框架。

```
optimization_discovery/
│
├── configs/
│   └── qwen35_910b.yaml
│
├── collectors/
│   ├── ascend_profiler.py
│   ├── benchmark.py
│   └── workload.py
│
├── parser/
│   ├── operator_parser.py
│   ├── timeline_parser.py
│   └── normalize.py
│
├── analyzer/
│   ├── hotspot.py
│   ├── workload_compare.py
│   ├── timeline.py
│   ├── roofline.py
│   └── bottleneck.py
│
├── source_analysis/
│   ├── source_search.py
│   ├── call_graph.py
│   └── kernel_mapping.py
│
├── discovery/
│   ├── opportunity_generator.py
│   └── opportunity_ranker.py
│
├── agent/
│   ├── diagnosis_agent.py
│   └── report_agent.py
│
├── experiment/
│   ├── benchmark_runner.py
│   └── result_analyzer.py
│
├── schemas/
│   ├── workload.py
│   ├── operator.py
│   └── opportunity.py
│
├── report/
│   └── report_generator.py
│
└── discovery.py
```

模块之间通过标准化 JSON 数据交互。这样后面即使更换 LLM、Profiler 版本或者接入其他推理引擎，也不需要重写整个系统。

## 七、推荐实施顺序与工作量

不要同时开发所有模块。建议按下面的顺序推进。

| 迭代   | 开发内容                               | 交付结果            | 建议工作量 |
| ------ | -------------------------------------- | ------------------- | ---------- |
| 迭代 1 | Profile Parser + Schema                | 统一 Profiling 数据 | 2～3 天    |
| 迭代 2 | Hotspot Detection + Workload Compare   | 热点矩阵和对比报告  | 2～3 天    |
| 迭代 3 | Timeline Analysis + Bottleneck Rules   | 瓶颈诊断报告        | 3～4 天    |
| 迭代 4 | Source Mapping + Opportunity Generator | 优化机会列表        | 3～5 天    |
| 迭代 5 | LLM Diagnosis + Report                 | 自动分析报告        | 2～3 天    |
| 迭代 6 | Benchmark Feedback                     | 优化验证闭环        | 3～5 天    |

这是一个初步排期，实际时间取决于 Profiler 数据格式、源码映射难度，以及是否能直接调用现有 Benchmark 执行器。

### 第一周：先跑通 Discovery 的基本流程

建议第一周只做三件事：

1. 把四组 Profiling 数据统一解析。
2. 生成不同 Workload 下的算子热点矩阵。
3. 对热点算子做初步瓶颈分类。

第一周结束时，应该能得到类似这样的结果：

Optimization Opportunity Report（示意）

Workload: Qwen3.5 Prefill / Decode

初步分析



发现 1：高频小算子

- 现象：调用次数较多，单次执行时间较短。

- 候选瓶颈：Launch-bound。

- 待验证：Host 提交开销、关键路径、编译器融合情况。

- 优化方向：评估算子融合或 Graph 执行路径。



  发现 2：Layout 转换

- 现象：某些 Workload 中累计耗时较高。

- 候选瓶颈：访存或调度开销。

- 待验证：实际数据搬运量、上下游布局兼容性。

- 优化方向：减少冗余 Layout 转换。



  发现 3：Graph 执行覆盖

- 现象：Eager 和 Graph 的执行事件分布不同。

- 候选问题：部分执行路径没有充分利用 Graph。

- 待验证：Graph 命中情况、Piecewise 切分和回退路径。

- 优化方向：检查 Graph 覆盖范围和执行路径。

  以上是报告格式示例，不代表已经从你的 Profiling 数据中发现了这些问题。

### 第二周：让系统能够解释为什么值得优化

重点开发：

- Kernel 到源码的关联。
- 瓶颈规则与证据收集。
- 优化机会生成。
- 优化优先级排序。

此时系统应该能够回答：

> 为什么这个算子值得优化？它在哪些 Workload 下值得优化？有什么数据证据？应该检查哪段源码？如何验证优化是否有效？

### 第三周：接入 LLM 和实验反馈

最后再接入 LLM，让它承担：

- 对规则诊断进行深度分析。
- 阅读关联源码。
- 提出多个候选优化方案。
- 生成实验计划。
- 分析实验结果。

注意，第一版先让 Agent 输出优化建议，由人决定是否修改代码。等诊断准确率和实验流程稳定后，再考虑自动修改和自动 Benchmark。



## 八、我认为最应该优先完成的三个模块

如果现在开始写代码，我建议优先级是：

P0

Operator Profile Schema

统一不同 Workload、不同执行模式下的算子信息。这是整个 Discovery 系统的数据基础。



P0

Bottleneck Diagnosis Rules

把性能指标转化为瓶颈判断，明确区分实测证据、推断和待验证假设。



P0

Opportunity Schema

统一优化机会的表达方式，使后续的 LLM、Kernel Optimization Agent 和 Benchmark 系统都能使用同一种数据结构。

一个关键设计建议：将 Opportunity Discovery 和 Kernel Optimization Agent 解耦。

Discovery 只负责发现、诊断、排序和描述优化机会；Kernel Optimization Agent 负责接收机会、修改实现、执行实验。两者通过 `Opportunity Schema` 交互。

这样以后可以把同一个 Discovery 系统接到不同的优化 Agent 上，也可以把同一个优化 Agent 用于不同的推理引擎。

下一步最适合做的是：基于你现有四组 Profiling 数据，具体设计 `Operator Profile Schema`、`Opportunity Schema` 和第一版 Python 项目骨架。