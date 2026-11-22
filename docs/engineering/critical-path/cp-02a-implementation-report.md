# CP-02A 实现与验收

## 基线与范围

从 `origin/main` 的 `87eef26`（PR #10）开发。CP-02A 读取单采集、单文件、单 Rank、单设备、单 Stream 的窗口，复用 CP-01 结果契约。端到端关键路径、跨 Stream/Rank 的依赖、归因、排序和 CLI 属于后续阶段。

## 读入与证据

`iter_task_execution_events` 通过只读 SQLite URI 读取指定窗口。以采集 ID、逻辑库路径、TASK 表与 SQLite 行号保留来源，同时读取设备、进程、Stream、任务 ID、子图上下文及模型标识。用整数 ns 确定窗口边界与区间，先减窗口原点再转为 µs。时长从原始整数端点差独立计算，包含原点标签的结果窗口使用相同坐标系。跨窗口任务保留未解析原因。

官方 CANN 文档规定同一 Stream 内的任务按入队次序执行。TASK 的 `contextId` 用于区分子图小算子。选窗内核验相同库、采集、Rank、设备、进程和 Stream，且两端子图字段一致。观测到的 `UINT32_MAX` 标志是独立任务的子图占位值，保留原字段用于核对。它不建立运行时 Context 身份。[CANN Stream 概念](https://www.hiascend.com/doc_center/source/zh/canncommercial/900/programug/acldevg/runtime_doc_dev_0011.html) · [CANN TASK 导出字段](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/900/devaids/Profiling/atlasprofiling_16_1144.html)

候选边还要求同队列内的 Task ID 连续且未复用、Global Task ID 可定位且未复用、已知独立任务类型、模型为非 Graph 标志，以及时间区间互不重叠。时间用于检查一致性。边存两端 TASK 行引用和关联字段。缺失标识、未知类型、Graph 模型、跳号、复用、重叠和解析精度冲突以 `UnresolvedDependency` 表示。

## 构图与求解

`build_task_dag` 在选定窗口内构图。图以窗口内任务数量为界。求解使用 Kahn 拓扑排序和按节点时长加权的动态规划。权重不含任务间隙，未知覆盖率保留 `null`。求解返回局部 `partial`、证据不足 `undetermined` 或受阻 `unavailable`。质量阻断或依赖环返回无路径结果。

组合入口为 `analyze_task_stream(db_path, scope, source_path=..., device_id=...)`。调用方从已核验的采集记录绑定 Rank、设备和源库，提供以 epoch µs 表示的窗口。可将既有质量标记通过 `quality_flags` 传入。

## 验证结果

下表中的 `"$CP02A_PYTHON"` 为开发虚拟环境解释器，解析后的完整 argv 见机器摘要。`"$CP02A_DATA_ROOT"` 为四组 2026-11-21 双 Rank 导出的根目录。

| 实际命令 | 退出码 | 状态 | 结果 |
|---|---:|---|---|
| `"$CP02A_PYTHON" -m pytest tests/test_critical_path.py`（实现前） | 2 | FAIL | 新分析模块尚不存在，见 `out/cp02a-review/schema-before.log` |
| `"$CP02A_PYTHON" -m pytest tests/test_critical_path.py`（复审反例） | 1 | FAIL | 3 个证据边界反例失败，见 `out/cp02a-review/final-counterexamples.log` |
| `"$CP02A_PYTHON" scripts/verify_summary.py --out out/cp02a-cpu`（首次） | 1 | FAIL | 两处类型检查失败，已修复，见 `out/cp02a-cpu/logs/check.log` |
| `"$CP02A_PYTHON" scripts/verify_summary.py --out out/cp02a-cpu-final` | 0 | PASS | 汇总在 `out/cp02a-cpu-final/verification_summary.json` |
| `make check PYTHON="$CP02A_PYTHON"`（摘要执行） | 0 | PASS | Ruff、架构检查、mypy、CLI validate，见 `logs/check.log` |
| `make verify PYTHON="$CP02A_PYTHON" DEMO_OUT="$PWD/out/cp02a-cpu-final/demo"`（摘要执行） | 0 | PASS | 635 项通过、29 项真实用例在此轮跳过，四组合成 demo，见 `logs/verify.log` |
| `"$CP02A_PYTHON" -m pytest tests/test_critical_path.py tests/test_critical_path_schema.py tests/test_events.py tests/test_timeline.py` | 0 | PASS | 119 项通过，含 CP-02A 专项 33 项，见 `out/cp02a-review/specialized-final.log` |
| `OPT_DISCOVERY_DATA_ROOT="$CP02A_DATA_ROOT" CP02A_DECODE_OUT=out/cp02a-decode-final PYTHONPATH=. "$CP02A_PYTHON" out/cp02a-review/verify_decode.py` | 0 | PASS | 真实 Decode 专项，见 `out/cp02a-decode-final/summary.json` 和 `out/cp02a-review/decode-final.log` |
| `OPT_DISCOVERY_DATA_ROOT="$CP02A_DATA_ROOT" "$CP02A_PYTHON" scripts/verify_summary.py --out out/cp02a-real --no-check --no-verify --real` | 3 | SKIPPED | 既有真实回归 28 通过、1 跳过、0 失败，见 `out/cp02a-real/verification_summary.json` |

既有真实回归的唯一跳过项为历史 Rank 缺 CSV 的 SQLite 回填场景，本批两个 Rank 均有 CSV。NPU 采集和完整端到端分析为 NOT_RUN。现有管线继续使用原入口，合成 demo 复核沿用 synthetic 隔离与受信证据等级。

### Decode 核对

统一补采 Decode Eager 和 Decode Graph 的 Rank 0、Device 0，ACTIVE_ROUND 1 起点后 **10–20 ms**。采集身份与 fixture 匹配。通过 SQLite 只读查询回读输出引用的两端 `TASK.rowid`，逐边核对连续 ID、设备/进程/流/子图字段及原始 ns 区间。三个结果均通过 CP-01 JSON 往返。

| Workload / Stream | 状态 | 局部路径节点 / 边 | 节点累计时长 |
|---|---|---:|---:|
| Decode Eager / 47 | partial | 10 / 9 | 91.76 µs |
| Decode Graph / 47，所选链为非图任务 | partial | 14 / 13 | 35.64 µs |
| Decode Graph / 267，图模型任务 | undetermined | 0 / 0 | 缺失 |

以上是各自窗口内证据允许的路径。窗口观测时长均为 10,000 µs，依赖覆盖率均为 `null`。

### 独立复审

CodeBuddy `deepseek-v4.1-flash` 实现适配、构图与求解以及初始测试。首版真实核对因子图哨兵误判未得到预期边，退出 1。CodeBuddy 继续修正子图字段、任务标识与求解队列，达到轮次上限。Codex 以反例补齐了剩余的 P0 证据校验，包括精确 ns 相接在 CP-01 浮点坐标下的冲突、后续任务重叠使端点失效，以及缺 Stream 的记录充当证据断点。首次全量静态检查的两个类型错误也已修复，最终复验通过。

## 已知限制

- 图范围为调用者选择的单设备流窗口，内存随选窗任务和边增长。构图为排序加线性扫描，求解时间和额外空间为 O(V+E)。
- 连续 Task ID 判定只在选窗内采用。跳号、标识复用、部分区间和未经验证的任务类型会断开证据链。路径时长是有效节点的累计观测时长。
- Graph 模型回放、通信、信号同步和跨 Stream/Rank 的等待关系缺少已核验证据。端到端时间归因及优化收益没有在本阶段测量。

**CP-02A 验收 PASS**。真实 Decode 专项 PASS，既有四组真实回归总状态保持 SKIPPED。
