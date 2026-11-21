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

在独立副本保留原始采集文件和元数据，排除既有数据库与 CSV。使用同一版本 CANN 执行 `msprof --parse=on --output="$REEXPORT/PROF"`，退出 0。新生成的两个 `ascend_task.db` 与对应的原设备库 SHA-256 分别相同，确认本次差异无法从现存原始采集恢复。结束元数据缺失的具体原因仍需通过补采验证。[CANN 离线解析和导出文档](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/910/devaids/Profiling/atlasprofiling_16_0021.html)
