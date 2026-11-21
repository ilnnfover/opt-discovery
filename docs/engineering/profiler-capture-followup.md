# Profiling 采集契约与 Decode Eager 复核

## 采集与分析

本次变更以 `620a539` 为基线。采集端输出 flat `PROF_*` 布局，读取端兼容历史 `PROF/PROF_*` 布局。分析按采集清单绑定 workload、rank、轮次与仪表配置，并将采集身份写入 `operator_table.json`。Cube 派生指标要求输入覆盖相同的实例，Vector FLOPs 单独保留。Timeline 记录同一个 rank 的下发与任务数，Graph 的下发、空闲和同步在 workload 窗口内解释。

三批采集清单均与两张卡、五轮及每轮 64 个输出 token 一致。两组 ArithmeticUtilization 导出含 Cube 与 Vector FLOPs。PipeUtilization 的 Full Graph 导出有 59/65 组 Cube 利用率，计数列缺失。Full Graph 用同一批原始采集、相同的 0–4 轮和 rank 0 运行前后代码，移除了 33 条逐算子下发假设和对应的 6 条机会。58 条 `probable` 候选和 49 条 `probable` 机会仍在，单组 `analyze --strict` 两次均退出 0。下发比例从 0.045 改为 0.090 次每任务，分子为 44,510 次，分母为同 rank 的 497,229 条任务。[CANN MSTX 采集文档](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/910/devaids/Profiling/atlasprofiling_16_0015.html)

| 检查 | 实际入口 | 状态与结果 |
|---|---|---|
| 静态、类型和配置 | `make check` | PASS，退出 0 |
| 测试与合成演示 | `make verify DEMO_OUT="$VERIFY_OUT/demo"` | PASS，退出 0。529 通过，29 跳过 |
| 历史真实回归 | `python scripts/verify_real.py` | SKIPPED，退出 3。28 通过，1 跳过，两张卡都有 CSV，无法重现缺 CSV 的回填场景 |
| 原始 Eager 与 Graph | `python -m opt_discovery.cli analyze --strict --exports-root "$CAPTURES" --workloads-dir "$DECODE_WORKLOADS" --out "$PAIR_OUT"` | 退出 3。Eager 为 `partial`，Graph 为 `success`，比较状态为 `incomparable` |
| 严格比较 | `python -m opt_discovery.cli compare --strict --a "$EAGER_TABLE" --b "$GRAPH_TABLE" --out-dir "$COMPARE_OUT"` | 退出 4。`total_ratio` 为空，`deltas` 为空 |
| Full Graph 产物差异 | `python scripts/compare_artifacts.py --before "$BASELINE_FULL_OUT" --after "$FULL_OUT" --out "$DIFF_JSON"` | PASS，退出 0。报告 72 处差异，分布于诊断、机会、Timeline 和 CSV |
| MSTX 数据库与包装脚本 | 合成 SQLite 回归、`bash -n scripts/enable_mstx.sh`、包装脚本的成功和失败退出测试 | PASS。真实 MSTX 采集为 NOT_RUN，两种 Graph 导出均无有效打点 |

`VERIFY_OUT` 为 `python scripts/verify_summary.py --real --out "$VERIFY_OUT"` 创建的目录。该命令保留历史真实回归的退出码 3，摘要和日志分别写入 `verification_summary.json` 和 `logs/`。

## 原始 Decode Eager 的时长差异

使用 `workload.log` 的 0–3 轮窗口，按任务起点划分。CSV 的计算任务时长按 µs 读取。参照合并库中的 `TASK`，只取通过 `globalTaskId` 与 `COMPUTE_TASK_INFO` 对应的计算任务。通信和其他设备任务分别统计。

| 轮次 | Rank 0 计算时长（s） | Rank 1 计算时长（s） |
|---|---:|---:|
| 0 | 0.469 | 13.177 |
| 1 | 0.563 | 7.434 |
| 2 | 0.521 | 3.997 |
| 3 | 0.496 | 9.334 |
| 合计 | 2.050 | 33.942 |

Rank 0 与 rank 1 各有 307,552 与 307,436 条计算任务。按算子看，`MatMulV2` 的调用数为 66,916 与 66,986，累计 0.434 s 与 10.469 s。Rank 1 的长样本在 CSV 为 10,369.808 µs，在 `TASK` 为 10,369,808 ns，在按同一任务锚定时钟后的设备级 `ascend_task.db::AscendTask` 为 10,369,807.188 ns。差异已存在于设备级解析结果。PMU 只有约 43% 的该算子实测样本，覆盖率限制了反推完整时长。

两份主会话都缺 host 和 device 的 `end_info`，CANN 分析日志报告缺少结束元数据和未知编号。原始 Eager 的 `rank_merge_inconsistent`、`rank_duration_inconsistent` 持续阻断比较。0–3 轮用户请求的墙钟时间分别作为日志观测保留，算子累计时长采用各自的质量闸门。

### 离线重解析

在独立副本保留原始采集文件和元数据，排除既有数据库与 CSV。使用同一版本 CANN 执行 `msprof --parse=on --output="$REEXPORT/PROF"` 和 `msprof --export=on --type=text --summary-format=csv --output="$REEXPORT/PROF"`，均退出 0。两个重新生成的 `ascend_task.db` 和两份 `op_summary*.csv` 与原文件的 SHA-256 分别一致。现有采集经离线重导后仍保留同样的 rank 时长差异。结束元数据缺失的具体原因仍需通过补采验证。[CANN 离线解析和导出文档](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/910/devaids/Profiling/atlasprofiling_16_0021.html)

### Eager 补采

CodeBuddy 在隔离工作目录使用既有 Eager 契约。采集脚本的实验调整是在最后一个 `ACTIVE_ROUND` 结束并完成设备同步后，显式请求 vLLM EngineCore 关闭。原始采集和仓库中的 harness 保留。

首个启动在 worker 初始化时遇到显存不足和 OpenMP 线程池异常，完成 0 轮。一个 worker 报告 25.81 GiB 可用显存，要求 51.81 GiB。外层 msprof 退出 0，采集自检识别到无有效 rank 和轮次，收集器退出 1。该批次不进入比较。重试在相关基准进程退出、两张卡连续空闲后启动。

#### 第二轮补采（OMP_NUM_THREADS=1 + jemalloc 预加载）

为规避首轮 worker 初始化阶段的 OpenMP 线程池异常，第二轮把 `OMP_NUM_THREADS=1` 提前到 Python 启动前，并预加载 jemalloc（`LD_PRELOAD=/usr/lib64/libjemalloc.so.2:`），其余沿用同一契约和隔离 harness（最后一轮结束后显式关闭引擎）。

本轮没有再出现显存不足，但仍然在 worker 初始化阶段崩溃，完成 0 轮。

| 项 | 值 |
|---|---|
| 采集目录 | `/root/prof/opt-eager-audit.f7CaOu/manual-eager.t4cNoB` |
| 批次目录 | `20261121-141744-decode-eager` |
| 收集器退出码 | 1 |
| `checks.ranks` | 空；两个 `PROF_*` 均判为 `no_operator_export` |
| `checks.active_rounds` | 0 / 5 |
| 崩溃点 | `ATen/ParallelOpenMP.cpp:64`，`set_num_threads` → `Invalid thread pool!` |

关键日志（`workload.log`）：

```
multiprocessing/popen_fork.py:66: DeprecationWarning:
This process (pid=485842) is multi-threaded, use of fork() may lead to deadlocks in the child.
  what():  pool INTERNAL ASSERT FAILED at "/pytorch/aten/src/ATen/ParallelOpenMP.cpp":64
Exception raised from set_num_threads at /pytorch/aten/src/ATen/ParallelOpenMP.cpp:64
```

判断：异常发生在 `fork` 之后的子进程调用 `set_num_threads` 时。父进程已初始化 OpenMP 线程池，fork 出的 EngineCore 继承了无效的池对象；因此把 `OMP_NUM_THREADS` 调成 1 无法规避——崩溃取决于池对象状态，与线程数无关。下一轮应改多进程启动方式（例如 `VLLM_WORKER_MULTIPROC_METHOD=spawn`），而不是继续调线程数。该改法尚未验证。

本轮未执行严格比较：收集器退出 1、`checks.ok=false`，按门槛不进入 `verify-recapture.sh`。原始产物、`collector.log`、`collection_manifest.json`、`workload.log` 均保留在上述目录。

#### 第三轮补采（改多进程启动方式，成功）

第二轮的判断得到验证：崩溃与线程数无关，改线程数无效。真正的修法是让 EngineCore 不再 `fork`，配合两处配套改动：

| 改动 | 原因 |
|---|---|
| `VLLM_WORKER_MULTIPROC_METHOD=spawn` | 避免子进程继承父进程已初始化的 OpenMP 池（`set_num_threads` 断言） |
| `PYTHONPATH` 追加而非覆盖 | spawn 子进程是全新解释器；覆盖会丢掉 `set_env.sh` 提供的 CANN 路径，worker 报 `No module named 'acl'` |
| harness 加 `if __name__ == "__main__"` | spawn 会用 `runpy` 重新执行主文件，无保护会递归构建引擎并触发 `bootstrapping phase` 报错 |

`OMP_NUM_THREADS=1`、jemalloc 预加载、显式关闭引擎等第二轮设置全部保留。

采集结果：

| 项 | 值 |
|---|---|
| 采集目录 | `/root/prof/opt-eager-audit.f7CaOu/manual-eager.IvFC7q` |
| 批次目录 | `20261121-151524-decode-eager` |
| 收集器退出码 | 0 |
| `checks.ok` | true |
| rank | device_0 / device_1，各 1 份 `op_summary`，合并库 1011.2 / 1011.9 MB |
| 轮次 | 5 / 5，`out_tokens` 均为 64 |
| 排除目录 | 1 个空会话（msprof 二次 attach） |

严格比较（`verify-recapture.sh`）：

| 步骤 | 退出码 | 结果 |
|---|---:|---|
| `analyze --strict --rounds 0-3` | 0 | `status=success` |
| `compare --strict`（Eager vs Graph） | 0 | `status=success` |

**结论**：原始 Eager 批次因 worker 初始化失败无法复现的问题已修复；新采集与原 Graph 表在严格阈值下可比，此前的 `rank_merge_inconsistent` / `rank_duration_inconsistent` 未在新批次上复现。

中间失败记录（同一目录，`smoke-spawn*.log`）：`No module named 'vllm_ascend_split_batch.*'`（工作区副本缺 `.py`，仅剩 `__pycache__`，属既有环境问题、非致命）、`bootstrapping phase`（缺 `__main__` 保护）、`No module named 'acl'`（`PYTHONPATH` 被覆盖）。
