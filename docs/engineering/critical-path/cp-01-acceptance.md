# CP-01 最终验收报告

## 1. 本阶段目标

核查真实 Profiler 事件能支持哪些关联，交付可追溯的事件与依赖数据契约。

## 2. 实际审计范围

以 `a67bf64` 为源码基线，核对了事件读者、时间原语、Timeline、窗口、MSTX、通信、CSV/SQLite 解析、质量和证据约定、诊断、机会排序、CLI、报告及相关测试。核对了本地三份单设备探针及仓库的 2026-11-21 采集身份 fixture。探针的 SQLite、CSV 和 Trace 均只读。详见[可观测性审计](observability-audit.md)。

## 3. Profiler 可观测性结论

实际观察到 Host API、Device TASK 区间、单 Stream ID 和提交连接键。探针有 9 条 TASK，`op_summary` 对应记录的 Task Type 均为 `N/A`。真实通信、Record/Wait 和 Graph Replay 语义超出该样本的覆盖。

## 4. 可确认的依赖关系

- 9/9 个同库 `connectionId` 唯一匹配了 Host launch 和 Device TASK，Profiler Trace 的起点对逐条吻合。3 个 Device 起点先于对应 Host 调用结束，提交证据描述开始到开始。
- 每份探针的同一设备进程与 Stream 内，3 个任务的执行时间与递增 Task ID 一致且无重叠。通用 FIFO 约束和生产导出的身份基数仍待验证。

## 5. 不可确认的依赖关系

Record → Wait、跨 Stream 同步、通信阻塞和跨 Rank Collective、阶段因果边、Graph 内部依赖均缺端点证据。未知关系由带 `reason_code` 的记录表达。三份探针的单设备 Host/Device 对齐不足以校验双 Rank 时钟。

## 6. Schema 与接口变更

新增独立 Python 模块 `opt_discovery.schemas.critical_path`。`ExecutionNode` 按采集、来源表或类型、逻辑路径与行定位生成稳定标识，保留规范化微秒和可空的事件时长、Rank、Stream。依赖边要求可定位来源、明确的关联依据与现有 `Provenance`。结果携带 `Window`、作用域、路径状态、可空覆盖率和现有 `QualityFlag`。覆盖率分母需要独立证据确定，缺分母时为 `null`。状态区分完整、局部、无法确定和不可用。现有公共接口和输出的兼容性由现有回归及专项兼容测试验证。

## 7. 修改文件清单

- [observability-audit.md](observability-audit.md)
- [cp-01-acceptance.md](cp-01-acceptance.md)
- [critical_path.py](../../../opt_discovery/schemas/critical_path.py)
- [test_critical_path_schema.py](../../../tests/test_critical_path_schema.py)

## 8. 测试执行结果

复验使用现有开发虚拟环境的解释器（下表记为 `"$CP01_PYTHON"`）。完整 argv、退出码和日志索引见 `out/cp01-verified/verification_summary.json`。

| 实际命令 | 退出码 | 状态 | 结果与日志 |
|---|---:|---|---|
| `python -m pytest tests/test_critical_path_schema.py`（Schema 前） | 2 | FAIL | 新模块不存在，契约收集失败，`out/cp01-review/schema-before.log` |
| 同一专项命令（CodeBuddy 修复后，最终复审前） | 1 | FAIL | 旧正例使用重叠区间，1 失败 / 33 通过，`out/cp01-review/schema-intermediate.log` |
| `python -m pytest tests/test_critical_path_schema.py tests/test_events.py tests/test_timeline.py tests/test_timeline_op_summary.py tests/test_mstx.py tests/test_comm.py tests/test_schemas.py tests/test_arch_lint.py` | 0 | PASS | 157 通过，`out/cp01-review/specialized.log` |
| `python scripts/verify_summary.py --out out/cp01-verification --real`（首次） | 1 | FAIL | 默认解释器缺 Ruff 和 Hypothesis，两个 Make 步骤退出 2。切换开发虚拟环境后通过，见该目录的摘要和日志 |
| `"$CP01_PYTHON" scripts/verify_summary.py --out out/cp01-verified --real` | 2 | BLOCKED | 两项 CPU 检查通过，真实回归被数据预检阻断 |
| `make check PYTHON="$CP01_PYTHON"`（摘要执行） | 0 | PASS | Ruff、架构 lint、56 个源码文件类型检查、CLI validate，`out/cp01-verified/logs/check.log` |
| `make verify PYTHON="$CP01_PYTHON" DEMO_OUT="$PWD/out/cp01-verified/demo"`（摘要执行） | 0 | PASS | 598 通过 / 29 跳过，四组合成 demo，`out/cp01-verified/logs/verify.log` |
| `make verify-real` 与摘要中的直接脚本 `"$CP01_PYTHON" scripts/verify_real.py` | 2 / 2 | BLOCKED | 缺对应批次四组根目录，`out/cp01-review/make-verify-real.log`、`out/cp01-verified/logs/real.log` |
| `python out/cp01-review/probe_observability.py export_only_prof_dir` | 0 | PASS | 三份探针的 CSV/TASK、提交 Trace 和 Stream 顺序核对，`out/cp01-review/probe_observability.log` |
| `PYTHONPATH=. python out/cp01-review/probe_schema.py export_only_prof_dir` | 0 | PASS | 18 个节点和 9 条提交关联 JSON 往返，`out/cp01-review/probe_schema.log` |

29 个跳过项为既有真实数据测试。合成产物复核确认四组的 `is_synthetic=true`，机会受信证据等级均为 `hypothesis`（`out/cp01-review/demo-artifact-review.log`）。本改动只新增独立数据契约，既有分析入口没有接入该模块，语义对照运行未选用。首次隔离安装缺 SOCKS 支持，使用本次命令的直连环境重试后安装成功。最终验证使用已核对依赖的既有虚拟环境，日志位于 `out/cp01-review/install-dev*.log`。

## 9. Code Review 问题与修复

CodeBuddy（`deepseek-v4.1-flash`）实现 Schema 和测试。Codex 按真实样本和现有边界制订契约、独立复现首版缺陷并审查修复。首轮 CodeBuddy 达到轮次上限时已写入实现和测试，随后恢复同一会话处理复审项。

| 级别 | 位置、影响与修复 |
|---|---|
| P0 | `CriticalPathResult.__post_init__` 首版只检查端点 ID，接受跨 Stream 的顺序边。现在校验设备活动、匹配 Rank、已知相同 Stream 和串行区间，并以起点差避免 epoch 加法舍入。 |
| P0 | `ExecutionNode` 与 `DependencyEvidence` 首版接受绝对路径、空来源类型或文件级证据。现在要求采集内逻辑路径、有效来源类型及依赖证据的正整数记录定位。未知依赖仍可携带文件级线索。 |
| P0 | `CriticalPathResult.from_dict` 与 `AnalysisScope` 首版可把错误质量结构当空或接受无限窗口时长。现在拒绝错误质量和未知依赖容器、空窗口对象及非有限跨度。 |
| P1 | `ExecutionNode.node_id` 首版对调用方显示为可空类型。现在是必有的派生字符串，反序列化复核已提供的标识。完整状态要求路径时长。 |

`PYTHONPATH=. python out/cp01-review/review_contract.py` 首版退出 1（六个反例被接受），复验退出 0（六个反例均被拒绝）。日志为 `out/cp01-review/contract-before-fix.log`、`out/cp01-review/contract-after-fix.log`。CodeBuddy 第二轮达到轮次上限时已落盘核心修复。Codex 补齐串行正例和针对性回归，收紧完整状态与反序列化后完成独立验证。无未解决的 CP-01 阻断项。

## 10. 真实数据验证状态

- 单设备探针只读核对 **PASS**，范围限于样本内的字段、身份与提交关联。
- 2026-11-21 四组双 Rank 回归 **BLOCKED**。恢复条件是把 `OPT_DISCOVERY_DATA_ROOT` 指向包含该批四组完整数据与清单的根目录。
- NPU 采集与完整真实分析 **NOT_RUN**，本阶段仅使用现有导出做只读审计。

## 11. 已知局限与剩余风险

探针每份只有 3 条单 Stream 任务，采集外的关联键有效范围、Graph 记录收缩后的身份、跨 Rank 校时和通信依赖完整性仍待验证。来源行定位以固定采集与固定导出文件为作用域。

## 12. CP-02 前置条件

先在一轮可信的单 Rank 数据中验证流内顺序、串行语义和事件身份。建立可追溯的适配器并保留未知边。Host 提交关联纳入带权路径前须明确重叠处理与起点依赖。跨 Stream 与跨 Rank 关系分别需要显式同步证据和独立时钟核对。

## 13. 最终验收结论

**PASS**。可观测性审计、Schema 契约、证据与质量约束、序列化、旧接口兼容性和 CPU 回归符合 CP-01 验收标准。初验的四组双 Rank 回归为 **BLOCKED**，补齐输入后的结果见下文。

## 同步 PR #9 后复验

最新 `origin/main` 为 `7757169`，其文件树与原审计基线 `a67bf64` 相同。CP-01 分支合入该提交时无冲突。MSTX 与采集契约变化已纳入原基线，真实回归仍以 2026-11-21 四组采集的身份为准。

复审定位两项 P1。提交边需要验证 Host → Device 的端点方向，同时允许没有 Stream ID 的 Host 调用出现在所选设备 Stream 的路径中。窗口反序列化须在调用共用 `Window` 前拒绝布尔值及字符串时间。四个回归用例在修复前失败、修复后通过。时间单位和既有 `Window`、事件读者、CLI 接口沿用原约定。

| 实际命令 | 退出码 | 状态 | 日志或结果 |
|---|---:|---|---|
| `"$CP01_PYTHON" -m pytest tests/test_critical_path_schema.py`（修复前） | 1 | FAIL | 4 失败 / 43 通过，`out/cp01-sync-review/schema-before.log` |
| `"$CP01_PYTHON" -m pytest tests/test_critical_path_schema.py`（修复后） | 0 | PASS | 47 通过，`out/cp01-sync-review/schema-after.log` |
| `make check PYTHON="$CP01_PYTHON"`（摘要执行） | 0 | PASS | `out/cp01-sync-verified/logs/check.log` |
| `make verify PYTHON="$CP01_PYTHON" DEMO_OUT="$PWD/out/cp01-sync-verified/demo"`（摘要执行） | 0 | PASS | 602 通过 / 29 跳过，四组合成 demo，`out/cp01-sync-verified/logs/verify.log` |
| `"$CP01_PYTHON" scripts/verify_real.py`（摘要执行） | 2 | BLOCKED | 缺四组正式导出，`out/cp01-sync-verified/logs/real.log` |
| `"$CP01_PYTHON" scripts/verify_summary.py --out out/cp01-sync-verified --real` | 2 | BLOCKED | 两项 CPU 检查通过，真实回归阻塞，`out/cp01-sync-verified/verification_summary.json` |

两项真实探针核对命令复跑均退出 0。共 9 条 HostToDevice 关联通过原始证据复核，18 个节点及 9 条关联通过 JSON 往返。日志位于 `out/cp01-sync-review/`。同步后 CP-01 技术验收 **PASS**，Schema 变更须人工审阅后合并。

收到四组双 Rank 导出后，复核了四组采集身份和八个合并 SQLite 的事件、通信与 MSTX 表结构。检查使用只读连接，详见[可观测性审计](observability-audit.md)。

`OPT_DISCOVERY_DATA_ROOT="$CP01_DATA_ROOT" "$CP01_PYTHON" scripts/verify_summary.py --out out/cp01-sync-real --no-check --no-verify --real` 退出 **3，SKIPPED**。真实回归 28 通过、1 跳过、0 失败。跳过的是历史 Decode Eager Rank 缺 `op_summary` 时的 SQLite 回填场景，本批两个 Rank 都有 CSV。复核日志为 `out/cp01-sync-review/real-skip-reason.log`，真实回归摘要为 `out/cp01-sync-real/verification_summary.json`。CPU 结果保存在前一份摘要中。
