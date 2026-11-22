# CP-01 Profiler 可观测性审计

## 审计边界

源码基线为 `a67bf64`。直接只读检查本地忽略目录 `export_only_prof_dir/` 下三份 2026-11-15 单设备探针，包括合并 SQLite、CSV、Trace JSON 和辅助 SQLite。以下表格用合并 DB 文件名标识探针。三个 DB 的 `RANK_DEVICE_MAP` 均为 `(-1, 0)`，每份有 3 条任务且都在 Stream 47，属于未知分布式 Rank 的单设备样本。2026-11-21 四组双 Rank 回归使用[采集身份 fixture](../../../configs/collection_fixtures/qwen35-35b-a3b-910b-unified-20261121.json)，本机未发现该批原始导出且未配置 `OPT_DISCOVERY_DATA_ROOT`，该部分为 **BLOCKED**。已归档的分析 JSON 与 CPU fixture 只能说明既有契约和历史风险。

## 当前事件与来源

| 来源 | 读者/契约 | 直接观测与限制 |
|---|---|---|
| `op_summary*.csv` | [events.py](../../../opt_discovery/events.py) 的 `normalize_instance_row` / `iter_op_summary_events` 输出 `KernelEvent(operator, rank, start_us, duration_us, task_type, source, stream_id)`，`TaskType` 区分 Kernel、通信、搬运、同步及其他。流式读取，Rank 由调用方提供，来源可定位 CSV 记录号。 | 三份均含 `Device_id, Model ID, Task ID, Stream ID, Context ID, Op Name, OP Type, Task Type, Task Start Time(us), Task Duration(us), Task Wait Time(us)`。三个探针的 9 条 CSV 记录均为 `Task Type=N/A`，每份在相同 Stream/Task ID 上与 TASK 起点以十进制精度逐条相等。通用任务模型/上下文的 `4294967295` 为这批中的占位值。现读者不保留 Task/Model/Context/Device ID 或 Wait Time。Wait Time 即使有数值也没有等待对象或边端点。 |
| `TASK` 与 `COMPUTE_TASK_INFO` | `iter_task_intervals` 只读 `startNs/endNs`，返回 `TaskIntervalEvent`，不暴露 Stream、原行或任务关联。 | TASK 三份都有 `startNs, endNs, deviceId, connectionId, globalTaskId, globalPid, taskType, contextId, streamId, taskId, modelId`。COMPUTE_TASK_INFO 有 `globalTaskId, name, opType, taskType` 等，探针内 9 个 TASK 各命中一条 compute 信息，解码的 OP Type 均为 `N/A`。未验证其他任务类型枚举值和 join 基数。 |
| `CANN_API` / `STRING_IDS` | `iter_host_spans` 做 `CANN_API.name=STRING_IDS.id` 左连接，`iter_host_calls` 暴露名称、时间与文件引用。 | 三份有 23/27/24 条 API，字段为 `startNs, endNs, type, globalTid, connectionId, name`。`STRING_IDS(id,value)` 解析名称，不提供关联身份。Host 活动可识别，现读者不保留原行、线程或 connectionId。 |
| `MSTX_EVENTS` / `MsprofTx` | [mstx.py](../../../opt_discovery/collectors/mstx.py) 的 `load_mstx` 读区间、名称和线程，以名称中的 sync/wait 等给 Timeline 提示。 | 第三份有 6 条 `MSTX_EVENTS`，列含 `startNs,endNs,eventType,rangeId,category,message,globalTid,endGlobalTid,domainId,connectionId`。3 条 `probe_marker`、3 条 `probe_range`，各类内部 rangeId 重用，6 条与 TASK 的 connectionId 无匹配。其辅助库 `MsprofTx` 为 0 行，`MsprofTxEx` 为 6 行，有 `mark_id/domain`，当前读者不消费后者。前两份合并库仅有枚举表。 |
| Trace JSON、辅助库 | 现有事件读者不读取 Trace flow 或这些关联字段。 | 三份 Trace 均有 3 个完整 `HostToDevice` flow（`s/f`）。第三份另有 6 个 `MsTx` 起点，未发现配对终点。`runtime.db::HostTask`、`ascend_task.db::AscendTask`、`api_event.db::ApiData` 含 `connection_id`，前两者另含 Stream/Task 等字段。这些是后续交叉校验来源。 |
| 同步、通信和时间映射 | [timeline.py](../../../opt_discovery/analyzer/timeline.py) 的 `load_overlap_totals` 按 `OVERLAP_ANALYSIS.type` 统计区间；[comm.py](../../../opt_discovery/analyzer/comm.py) 按 `COMMUNICATION_TASK_INFO.globalTaskId` 去重，取 `size/bandwidth`。 | 每份出现 `aclrtSynchronizeDeviceWithTimeout`（4/7/4 条），未发现记录受等待任务集合或 Record/Wait 对应键的表，也无 `COMMUNICATION_TASK_INFO`。有 `OVERLAP_ANALYSIS(startNs,endNs,type)`、`SESSION_TIME_INFO`、`HOST_INFO`、`RANK_DEVICE_MAP`；每份辅助设备 `time.db::Time` 一行，含 `device_id,dev_mon,dev_wall,dev_cntvct,host_mon,host_wall`，对应另一份 Time 表为空。 |

口径出处还包括 [profiler_dir.py](../../../opt_discovery/collectors/profiler_dir.py) 的分片/Rank 发现与 [op_summary 映射](../../../configs/parsers/)。`op_statistic` 和 SQLite `op_report` 是聚合表，只能用于既有算子分析。真实通信、Graph 覆盖与双 Rank 检查的测试契约见 [test_real_data.py](../../../tests/test_real_data.py) 和 [test_real_unified_manifest.py](../../../tests/test_real_unified_manifest.py)。

## 依赖来源与验证结论

| 依赖 | 证据与结论 |
|---|---|
| Host 提交 → Device 开始 | **样本已验证**。每份按同库 `TASK.connectionId=CANN_API.connectionId` 得到 3 个唯一 `launch` 匹配，Trace 的 `s/f` 起点与两表的 `startNs` 精确一致（9/9）。这是从 **Host 调用开始** 到 Device 开始的提交关联。有 3 个 Device 起点早于对应 Host 调用结束。整段 Host 调用不能作为 Device 的完成→开始前驱，提交到开始的差也不能直接计作阻塞开销。connectionId 在不同采集间复用，禁止全局 join。 |
| 同 Stream 的执行顺序 | **样本内顺序已验证，普适约束 NOT_VERIFIED**。每份 Stream 47 的 3 个 TASK 有同一设备和进程范围，Task ID 随执行起点递增，区间互不重叠。CP-02 需在同采集/进程/设备/Rank/Stream 范围验证执行类型、FIFO 或顺序字段、非重叠和重复身份，再建有条件的顺序边。等时、重叠、未知通道身份或序号复用无法解决时记录 unresolved。 |
| Record → Wait、跨 Stream 同步 | **unresolved**。API 名称可证实调用存在，缺事件句柄和代次、记录/等待端 Stream 及完成条件。MSTX range ID、名称、调用时长、时间邻接或重叠无法补足端点证据。任何跨 Stream 边都需显式可复核的关联键及语义。 |
| Wait 返回/设备同步 → 后续活动 | **unresolved**。探针可见同步 API 但缺受等待对象及后继关系。Phase marker 只能界定观测范围。 |
| 通信 → 消费者 / 跨 Rank Collective | **unresolved**。通信覆盖、未重叠时间以及按任务去重的字节属于观测量。缺 communicator、collective 序号、成员身份和消费者/完成同步信息，无法证明通信阻塞。 |
| Graph Replay 和内部依赖 | **NOT_VERIFIED**。探针没有有效 Model ID 或 replay 身份。Graph 记录会融合或收缩。算子名、Task ID 或 Model ID 单独不能标识 replay 实例。缺少逐 replay/capture 序号及内部映射时保留原始记录身份，未单独出现的节点留作不可观测。 |

最小复核查询（每个合并库以 SQLite URI 的 `mode=ro` 打开），Trace 按 `cat=HostToDevice` 与相同 `id` 配对 `ph=s/f` 后用十进制做核对。

```sql
SELECT t.rowid, t.streamId, t.taskId, t.globalTaskId, t.connectionId,
       s.value, a.startNs, a.endNs, t.startNs, t.endNs
FROM TASK t LEFT JOIN CANN_API a ON a.connectionId = t.connectionId
LEFT JOIN STRING_IDS s ON s.id = a.name
ORDER BY t.startNs;
```

| 合并库 | TASK / CSV | Host 提交匹配 | Stream 内重叠对 | MSTX |
|---|---:|---:|---:|---|
| `msprof_20261115160539.db` | 3 / 3 | 3 | 0 | 枚举 |
| `msprof_20261115161229.db` | 3 / 3 | 3 | 0 | 枚举 |
| `msprof_20261115161600.db` | 3 / 3 | 3 | 0 | 6 条 |

## 时间、质量和补采

- [primitives.py](../../../opt_discovery/primitives.py) 与 [window.py](../../../opt_discovery/analyzer/window.py) 使用微秒。TASK/CANN SQLite 源时间为整数 epoch ns。先减整数原点或计算整数时长再转微秒，可保留亚微秒差异。CSV 普通时长为 µs，历史实采 `COMMUNICATION` 行的 `Task Duration(us)` 实为 ns，仅在现有归一化边界修正一次（[R08](../../../tests/regression/test_r08_communication_unit.py)）。保留独立 duration，不能从 epoch 浮点端点逆算短事件的权重。
- MSTX 新表按 ns，旧表的单位嗅探是有备注的启发式。默认窗口由 [workload.log 的 ACTIVE_ROUND](../../../opt_discovery/collectors/workload_log.py) 与 `select_rounds_window` 提供。阶段或窗口只说明范围。现 Timeline 使用同轮窗口和相对原点做 busy/overlap 汇总，不能当跨 Rank 校时。单设备的 Host/Device 样本已对齐，双 Rank 可直接对比仍为 **NOT_VERIFIED**。需每个 Rank 的时钟映射、偏移与误差上界和共同时间锚。
- [回归矩阵](../regression-matrix.md) 的 R01、R05、R06、R07、R08、R09、R10、R12 涵盖分片、Rank 合并、枚举空证据、Graph 收缩、单位、版本列、时长截断和窗口原点。历史 Decode Eager 的 Rank 近似同调用数却存在时长截断，不可解释为 Rank 在等待。沿用 [quality.py](../../../opt_discovery/schemas/quality.py) 的阻断标记与清单检查，缺分片、错位、损坏或作用域缺失时降级。
- 补采优先选同一采集契约的 Decode Eager/Graph、同轮、每 Rank 完整 DB/CSV/Trace 和 `workload.log`，保留 Profiler 版本、进程、设备、Stream、任务与源记录定位。验证多 Stream Record/Wait 的事件句柄/代次和端点，分别跑有、无显式 Wait 的对照。通信需采集对应 task 与 collective 身份及消费者证据。Graph 需记录 capture/replay 实例与任务映射。单机 Rank 与跨主机 Rank 的时钟对齐都应提供独立校验和误差界。

## CP-02 最小范围

先用一个可信窗口内的单采集、单设备、单 Rank TASK 实例。用采集身份、来源文件、表/类型及记录定位建立节点。只有满足同 Stream 串行证据时连局部顺序边。其他 Stream 作为独立链或孤立节点，未知关联进入 unresolved；源质量阻断时返回不可用。Host 提交关联具备样本证据，但纳入带权路径前需定义起点依赖或经证实的提交子区间，避免重复计算重叠的整段 Host 调用。显式边以外的空洞没有默认权重。节点权重是观察到的执行时长。跨边延迟需要独立证据才可计入，局部路径和观测窗口时长分别记录。

现有 Timeline 产生区间、空洞及聚合提示；`diagnosis/facts.py` 和 `engine.py` 消费算子表指标与阈值；`discovery/generator.py` 使用累计时间占比评分；`cli.py` 与 `report/generator.py` 消费这些结构化产物。现有结论均不能充当关键路径依赖。完整端到端关键路径所需的同步、时钟和覆盖在当前样本中不足。待验证假设还包括生产导出中的关联键基数、并发队列 ID 复用和 Graph 内的时间/身份稳定性。
