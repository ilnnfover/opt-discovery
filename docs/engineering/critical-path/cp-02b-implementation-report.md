# CP-02B 跨 Stream 依赖与同步重建

## 范围与基线

基于 `origin/main` 的 `fa517dc`（PR #11）。沿用 CP-01 Schema、CP-02A TASK 读者与 DAG 求解器。输入范围是单采集、单 Rank、单设备及明确选择的多个 Stream。跨 Rank、图模型内部回放、归因和机会排序属于后续阶段。

## 真实证据审计

只读检查 2026-11-21 Decode Eager 和 Graph 的两个 Rank。采样窗口为 ACTIVE_ROUND 1 起点后 10–20 ms。下表统计设备任务和 Host 调用的起点落窗记录，并要求同类、同 `connectionId` 且样本内唯一才列为 API → TASK 对照。

| Decode | Rank | TASK Record / Wait | Host Record / StreamWait | 唯一同类 API → TASK | 同 `connectionId` 的异类匹配 |
|---|---:|---:|---:|---:|---:|
| Eager | 0 | 8 / 4 | 9 / 4 | 12 | 0 |
| Eager | 1 | 4 / 2 | 9 / 4 | 6 | 0 |
| Graph | 0 | 81 / 62 | 7 / 4 | 7 | 0 |
| Graph | 1 | 20 / 16 | 10 / 6 | 6 | 0 |

TASK 有 `taskType`、`streamId`、`connectionId` 与 ns 端点。CANN_API 有 `name`、`globalTid`、`connectionId` 与 ns 端点。两表均缺 Event 句柄和 Wait 使用的 Record 代次。同一 `connectionId` 在已核对样本中关联一个调用及其同类设备任务。

对上述 31 组关联继续核查辅助表。匹配到的 `HostTask.request_id` 为 `-1`，`ApiData.item_id` 为 `0`，`AscendTask.index_id` 为 `-1`，这些值没有区分 Record/Wait 实例。四个合并库中只找到 MSTX 枚举表。四份 Trace 的同步任务和 Host API 抽样同样只显示任务定位与 HostToDevice 关联字段。对常见句柄键和跨 Stream flow 名称做定向查找，未找到可用于配对的原始记录。

CANN 运行时允许重复记录同一 Event。Wait 消费的是调用时对应的最近一次记录，后续 Record 可以复用该对象。因此需要能定位两端的 Event 句柄及其记录代次。API 名称、Stream、相邻时间、MSTX 区间或 `connectionId` 单独都不能还原这种关系。[CANN Record Event](https://www.hiascend.com/document/detail/en/canncommercial/850/API/appdevgapi/aclcppdevg_03_0083.html) · [CANN 跨 Stream 同步](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/900/programug/acldevg/runtime_doc_dev_0026.html)

## 实现

`analyze_task_streams` 使用已有只读 TASK 迭代器。各 Stream 共享窗口原点，流内 DAG 分别沿用 CP-02A 条件。跨流边只接受调用方提供、可回溯到底层记录的 `SyncBinding`。每条绑定包含采集身份、TASK 行定位、Event 句柄、记录代次与独立来源行。绑定生产方负责核对原始字段。构图验证端点角色、进程、设备、Rank、Stream、代次唯一性、窗口内 TASK 身份与时间一致性。Record/Wait 歧义、缺字段、错误作用域、定位冲突及重叠异常保留可机读原因。

保留 Record 的观测时长，将匹配成功的 Wait 投影为完成时刻的零权重节点。原始 Wait 区间仍可从 TASK 行与边证据定位。生产端可在 Wait 开始后完成，只有完成时间的顺序一致性用于校验。现有 Kahn 求解器可在多 Stream DAG 中选择 Fork/Join 的最长累积执行路径，等待间隙和已计入的生产端执行时间不会重复加权。质量阻断和依赖环返回不可用，局部结果使用 `partial` 或 `undetermined`，覆盖率为 `null`。

## 独立复审

CodeBuddy（`deepseek-v4.1-flash`）交付主实现及初始合成测试。首版测试暴露漏导入。Codex 独立反例发现两个会放行未经验证边的 P0：同一 TASK 定位符的相互矛盾观测被去重丢弃，以及同代次存在另一个不合格 Record 时被误判为唯一。现在冲突定位会阻断构图，代次歧义在端点准入之前保留。反例复验通过。Codex 同时修复静态类型问题，以及 Fork 夹具的节点时长求和和跨进程夹具的输入绑定。未改动已有 CP-02A 测试。

## 验证

`"$CP02B_PYTHON"` 为开发虚拟环境解释器，`"$CP02B_DATA_ROOT"` 为已有四组导出的根目录。完整参数与输出见所列日志。

| 实际命令 | 退出码 | 状态 | 结果 |
|---|---:|---|---|
| `"$CP02B_PYTHON" -m pytest tests/test_critical_path_sync.py`（最新 main 存档） | 2 | FAIL | CP-02B 入口尚不存在，见 `out/cp02b-review/baseline.log` |
| `PYTHONPATH=. "$CP02B_PYTHON" out/cp02b-review/check_contract.py` | 1 → 0 | FAIL → PASS | 两个独立 P0 反例修复前失败、修复后通过，见 `out/cp02b-review/contract-{before,final}.log` |
| `"$CP02B_PYTHON" scripts/verify_summary.py --out out/cp02b-cpu`（首次） | 1 | FAIL | Fork 夹具中第二个断言仍使用错误累计时长，`make verify` 退出 2，已修复 |
| `"$CP02B_PYTHON" scripts/verify_summary.py --out out/cp02b-cpu-final` | 0 | PASS | `out/cp02b-cpu-final/verification_summary.json` |
| `make check PYTHON="$CP02B_PYTHON"`（汇总执行） | 0 | PASS | Ruff、架构检查、mypy、配置验证 |
| `make verify PYTHON="$CP02B_PYTHON" DEMO_OUT="$PWD/out/cp02b-cpu-final/demo"`（汇总执行） | 0 | PASS | 656 通过、29 个真实用例在 CPU 轮次跳过，四组合成 demo |
| `"$CP02B_PYTHON" -m pytest tests/test_critical_path_sync.py tests/test_critical_path.py tests/test_critical_path_schema.py tests/test_events.py tests/test_timeline.py` | 0 | PASS | 140 通过，含 CP-02B 专项 21 项 |
| `OPT_DISCOVERY_DATA_ROOT="$CP02B_DATA_ROOT" CP02B_DECODE_OUT=out/cp02b-decode-final PYTHONPATH=. "$CP02B_PYTHON" out/cp02b-review/verify_decode.py` | 0 | PASS | 真实 Decode 保守降级复验，`out/cp02b-decode-final/summary.json` |
| `OPT_DISCOVERY_DATA_ROOT="$CP02B_DATA_ROOT" "$CP02B_PYTHON" scripts/verify_summary.py --out out/cp02b-real --no-check --no-verify --real` | 3 | SKIPPED | 28 通过、1 跳过、0 失败，双 Rank 均有 CSV，历史缺 CSV 回填用例不适用 |

真实 Decode 专项复验使用上述窗口的 Rank 0。逐行核对原始同步 TASK 的引用，确认所有落窗的 Record/Wait 都能在结果中定位。流内边与逐 Stream 运行 CP-02A 的输出集合相等，两个结果均通过 CP-01 JSON 往返。

| Decode | Stream | 流内边 | 跨 Stream 边 | 未解析同步 |
|---|---|---:|---:|---:|
| Eager | 44、47 | 48 | 0 | 12 |
| Graph | 43、47、266、267 | 32 | 0 | 143 |

**CP-02B 技术验收 PASS。** 真实数据的保守处理专项 PASS，真实跨 Stream 端点重建仍为 **BLOCKED**。恢复需要采集可逐行核对的 Event 句柄、Wait 消费的记录代次和两端 TASK 定位。无新采集或 NPU 运行，完整端到端分析为 NOT_RUN。
