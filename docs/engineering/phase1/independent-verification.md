# AE-02 独立逐项核查与变异验证（第二轮）

> 目的：回应独立验收报告中「R01–R12 未逐项核查、未独立变异」与「通信单位修复缺少真实数据回归」两项。
> 方法：对每条约束（1）阅读测试断言，确认期望值来自独立算术而非被测函数；（2）在 `/tmp` 的**整树副本**中
> 注入对应历史错误，运行该条测试，要求必须失败（退出码非零）；主工作树不引入错误。
> 数据：真实回归使用本地授权四组统一补采（只读，未重采、未占用 NPU）。

## 1. 环境

| 解释器 | 依赖 | 全量结果 |
|---|---|---|
| Python 3.12.13 | PyYAML 6.0.3、pytest 8.3.2、ruff 0.17.5、mypy 2.4.1 | 203 passed, 16 skipped |
| Python 3.11.6（`/usr/bin/python3.11` + venv） | PyYAML 6.0.3、pytest 9.2.0 | 203 passed, 16 skipped |
| Python 3.10 | — | **BLOCKED**：本机无 3.10；`install_binary` 两次失败（安装器报 3.14.3 校验失败），未改动系统包 |

3.11 是本地能拿到的最低主版本，用于验证「不依赖 3.12 特性」；3.10 仍只能由 CI 矩阵声明。

## 2. R01–R12 逐项核查与变异

| ID | 测试断言（非平凡性） | 注入的历史错误 | 退出码 | 失败节点 |
|---|---|---|---|---|
| R01 | 分片顺序按数值；分片聚合与单文件逐算子 `(calls,total)` 相等；期望值 300/180 µs 独立算术 | `sorted(..., key=_natural_key)` → `sorted(...)` | 1 | `test_natural_order_keeps_slice_10_after_slice_2` |
| R02 | ① 统计口径不得出现通信记录/指标；② 矩阵 note 必须声明口径差异 | ② note 改为「口径含 HCCL，可直接比较」 | 1 | `test_hotspot_matrix_states_the_capability_gap` |
| R02 | 同上（数据伪造路径） | 在 `ascend_op_statistic.yaml` 增加 `communication_time_us`（无 `when` 过滤） | 1 | `test_no_communication_is_fabricated` |
| R03 | calls=771、total=226478.259、avg=total/calls 且 ≠ 平均值的平均 | avg 分母改为 `2*calls` | 1 | `test_operator_level_rollup_uses_weighted_average` |
| R04 | `compute_pipe_time_fraction=1.5`、`mte2_time_fraction=2.6`，显式断言 ≠1.0 | `_put_max_fact` 对值取 `min(1.0, ·)` | 1 | `test_pipe_fraction_above_one_is_preserved` |
| R05 | 4x 时长 → 「不一致（不可合并）」；缺 rank → 退出 2；sqlite → 「口径不同」 | CLI 的 `consistent` 直接置 `True` | 1 | `test_check_ranks_reports_inconsistency` |
| R06 | 仅枚举表 → `available=False`；开启但空 → `True` | 缺表分支改为 `available=True` | 1 | `test_enum_table_alone_does_not_mean_mstx_was_collected` |
| R07 | 全字段/文本递归扫描；注入 3 类错误结论必须被拒绝 | 在 `_observations` 注入「预计收益 8%」 | 1 | `test_no_savings_claims_in_any_field_or_text` |
| R08 | 110462688 ns = 110462.688 µs；≠原始值与二次缩放；漏配置反例 | 删除映射 `scale: 0.001` | 1 | `test_communication_time_metric_is_only_on_communication_rows` |
| R09 | 缺列清单 + 告警码；`flops` 单位 `flop`；未知列入审计 | 删除 `missing_extra_metrics` 写入 | 1 | `test_compact_export_...`、`test_wide_export_keeps_pipe_split_metrics` |
| R10 | flag=True 且不出现「TP 两卡明显不对称」；一致时 flag=False | 判定阈值 2.0 → 1000.0 | 1 | `test_equal_task_counts_but_huge_duration_gap_is_flagged` |
| R11 | 0.1797（aicore 分母）<1；Task Duration 分母为 1.54 | 分母换成 `total_time_us` | 1 | `test_utilization_uses_aicore_time_not_task_duration` |
| R12 | `gap_max=50µs`、`idle=300µs`，epoch 偏移不影响语义 | `_gaps_from_merged` 的 cursor 改为 0 | 1 | 两个用例同时失败 |
| R08-real | 真实导出中通信行 `communication_time_us == total_time_us`（同一次换算） | 删除映射 `scale: 0.001` 后跑真实数据 | 1 | `test_communication_time_matches_record_total` |

结论：14 次注入全部被捕获（`ALL_CAUGHT`），未出现「注入错误但测试仍通过」。

## 3. 新增：真实数据上的通信单位回归

`tests/test_real_data.py::test_communication_time_matches_record_total`：遍历真实 `op_summary` 导出的
COMMUNICATION 记录，断言 `communication_time_us.unit == "us"`、值与 `total_time_us` 在 1e-9 相对误差内相等、
且至少存在一条通信记录。移除 `scale` 后该用例失败（见上表 R08-real），说明合成 fixture 之外也有真实语义保护；
修复前该断言会得到 1000 倍偏差。

真实回归现状：`OPT_DISCOVERY_DATA_ROOT=<授权根> python scripts/verify_real.py` → `16 passed, 0 skipped`，退出 0。

## 4. 跨版本发现并修复的缺陷

在 Python 3.11 上运行全量测试时发现：

```
FAILED tests/test_analyzer.py::test_hotspots_rank_and_share - assert 0.9999999999999999 == 1.0
```

根因：CPython 3.12 起内建 `sum()` 对浮点使用补偿求和，3.10/3.11 会得到 0.9999999999999999；
测试用**精确相等**断言「占比之和为 1」，属于版本相关的偶然通过。

修复：改为 `pytest.approx(1.0, abs=1e-12)`（`hotspots[0].share` 同样改为 approx），并保留注释说明原因。
修复后 3.11 / 3.12 均 203 passed, 16 skipped。该缺陷说明「最低支持版本验证」不能由单一解释器替代。

## 5. 仍未完成 / 限制

| 项 | 现状 |
|---|---|
| Python 3.10 实际运行 | 本机不可得；CI 矩阵已声明 3.10/3.11/3.12 |
| R05 下游闸门 | 只有 CLI 文案与退出码；`analyze` 不消费 rank 合并结论（AE-03） |
| R10 下游闸门 | `rank_duration_inconsistent` 未被 `compare` 消费（AE-03/AE-07） |
| 真实数据趋势回归 | 现有真实用例验证的是口径/契约，不比对历史数值趋势（AE-06） |
| 变异为人工注入 | 未接入自动化变异测试工具（如 mutmut），仅保留本轮脚本化证据 |
