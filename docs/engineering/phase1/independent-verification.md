# AE-02 独立逐项核查与变异验证（第二轮）

> 目的：回应独立验收报告中「R01–R12 未逐项核查、未独立变异」与「通信单位修复缺少真实数据回归」两项。
> 方法：对每条约束（1）阅读测试断言，确认期望值来自独立算术而非被测函数；（2）在 `/tmp` 的**整树副本**中
> 注入对应历史错误，运行该条测试，要求必须失败（退出码非零）；主工作树不引入错误。
> 数据：真实回归使用本地授权四组统一补采（只读，未重采、未占用 NPU）。

## 1. 环境

| 解释器 | 依赖 | 全量结果 |
|---|---|---|
| Python 3.12.13 | PyYAML 6.0.3、pytest 8.3.2、ruff 0.17.5、mypy 2.4.1 | 211 passed, 17 skipped, 2 xfailed |
| Python 3.11.6（`/usr/bin/python3.11` + venv） | PyYAML 6.0.3、pytest 9.2.0 | 211 passed, 17 skipped, 2 xfailed |
| Python 3.10.22（uv 独立构建 + 干净 venv） | PyYAML 6.0.3、pytest 9.2.0、ruff 0.17.5、mypy 2.4.1 | 211 passed, 17 skipped, 2 xfailed；真实回归 16 passed, 1 skipped（→ SKIPPED） |

3.10 的获取方式：`install_binary` 工具两次失败（安装器报 3.14.3 校验失败），改用 `pip install uv` +
`uv python install 3.10`（独立构建，自带 sqlite3/ssl/zlib），再建 venv。该解释器**不是**系统包，
不影响系统 Python；详见 §6。

3.10 与 3.11 均已实际运行（3.10 通过 uv 独立构建 + 干净 venv），用于验证「不依赖 3.12 特性」；CI 矩阵 3.10/3.11/3.12 覆盖托管环境。

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

R06/R12 在第三轮复审后新增的边界用例（缺同步指标、显式 vs 回退窗口）为断言级补充，
未另做独立变异；它们验证的是「不把缺失当实测」与两种窗口口径的差异，结论见 `regression-matrix.md` §1。

## 3. 新增：真实数据上的通信单位回归

`tests/test_real_data.py::test_communication_time_matches_record_total`：遍历真实 `op_summary` 导出的
COMMUNICATION 记录，断言 `communication_time_us.unit == "us"`、值与 `total_time_us` 在 1e-9 相对误差内相等、
且至少存在一条通信记录。移除 `scale` 后该用例失败（见上表 R08-real），说明合成 fixture 之外也有真实语义保护；
修复前该断言会得到 1000 倍偏差。

真实回归现状：`OPT_DISCOVERY_DATA_ROOT=<授权根> python scripts/verify_real.py` → **SKIPPED（16 passed, 1 skipped）**，退出 3。

原因：`test_sqlite_fallback_fills_rank1_without_csv` 在「所有 rank 都有 CSV」的补采上不可复现，已按复审要求改为
显式 `pytest.skip`（不再静默计为通过），因此入口整体不能记 PASS。回填能力由 CPU 回归
`test_r05_rank_merge.py` 覆盖；「有 CSV 的 rank 是否带 sqlite」由新增用例
`test_sqlite_available_for_exported_ranks` 无条件断言（非空循环）。

## 4. 跨版本发现并修复的缺陷

在 Python 3.11 上运行全量测试时发现：

```
FAILED tests/test_analyzer.py::test_hotspots_rank_and_share - assert 0.9999999999999999 == 1.0
```

根因：CPython 3.12 起内建 `sum()` 对浮点使用补偿求和，3.10/3.11 会得到 0.9999999999999999；
测试用**精确相等**断言「占比之和为 1」，属于版本相关的偶然通过。

修复：改为 `pytest.approx(1.0, abs=1e-12)`（`hotspots[0].share` 同样改为 approx），并保留注释说明原因。
修复后 3.11 / 3.12 均 211 passed, 17 skipped, 2 xfailed。该缺陷说明「最低支持版本验证」不能由单一解释器替代。

## 5. 全新 CPU 环境安装验证（AE-01）

脚本：`scripts/check_clean_env.sh`（Makefile 入口 `make clean-env-check PYTHON=python3.10`）。步骤：

1. 把工作树复制到临时目录，排除 `.git`、`out/`、`export_only_prof_dir/`、`__pycache__`、各类缓存与 `*.egg-info`；
2. 用指定解释器建 **干净 venv**（不继承系统 site-packages）；
3. `pip install --no-cache-dir -e ".[dev]"`（强制真实解析/下载，不复用本站缓存）；
4. 依次执行 `make check`（ruff+mypy+validate）、`make verify`（pytest+demo）、`opt-discovery --help`；
5. `verify-real`：有 `OPT_DISCOVERY_DATA_ROOT` 时接受 PASS(0) 或 SKIPPED(3)（SKIPPED 不计为通过并打印），
   否者用 `--expect-blocked` 断言 BLOCKED；FAILED(1)/错配一律非零。

实测：

| 解释器 | 安装结果 | make check | make verify | verify-real | 退出码 |
|---|---|---|---|---|---|
| 3.10.22 | PyYAML 6.0.3 / pytest 9.2.0 / ruff 0.17.5 / mypy 2.4.1 | 0 | 211 passed, 17 skipped, 2 xfailed | BLOCKED 断言通过 | 0 |
| 3.10.22 + 真实数据 | 同上 | 0 | 211 passed, 17 skipped, 2 xfailed | SKIPPED（16 passed, 1 skipped） | 0（脚本接受 SKIPPED 并注明不计通过） |
| 3.12.13 | 同上（pytest 9.2.0） | 0 | 211 passed, 17 skipped, 2 xfailed | BLOCKED 断言通过 | 0 |

顺带发现并修复：`make test` 原本写 `pytest -q`，与 pyproject 的 `addopts = "-q"` 叠加成双 `-q`，
会吞掉 `N passed / N skipped` 摘要（与 verify-real 的 skip 检测同类风险）。已去掉 Makefile 里的 `-q`。
CI 也改为直接调用 `make check` / `make verify`，避免本地与 CI 命令漂移。

覆盖边界：干净 venv 只能隔离 Python 依赖，不能验证操作系统/内核/驱动差异（项目为 CPU-only 分析，
不依赖 CANN/NPU）；真正的空白机器验证仍建议在容器或新主机上跑同一脚本。

## 6. 仍未完成 / 限制

| 项 | 现状 |
|---|---|
| 全新机器/容器隔离 | 已用「干净 venv + 工作树副本 + `--no-cache-dir`」模拟；未在真正空白 OS 或容器中验证 |
| R05/R10 下游闸门 | 仍只有 CLI/时间线层标记；`compare` 尚未消费。已用 **strict xfail** 用例锁定预期（`tests/regression/test_known_gaps.py`），AE-03 实现后会 XPASS 并强制更新 |
| 真实无 CSV 回填场景 | 统一补采不可复现，真实回归显式 SKIPPED；CPU 回归覆盖 |
| 真实数据趋势回归 | 现有真实用例验证的是口径/契约，不比对历史数值趋势（AE-06） |
| 变异为人工注入 | 未接入自动化变异测试工具（如 mutmut），仅保留本轮脚本化证据 |

## 7. 第三轮复审问题修复（2026-11-18）

| 复审项 | 问题 | 修复与证据 |
|---|---|---|
| P1-1 | 解析 pytest 文本摘要，额外 `-q` 时全跳过被误判 PASS | `verify_real` 改用 `--junitxml` 结构化计数（`parse_junit`），并清除 `PYTEST_ADDOPTS`；`tests=0`/`executed=0`/junit 缺失或不可解析均失败关闭。回归用例：`test_all_skipped_with_extra_quiet_is_not_pass`（真实执行、`PYTEST_ADDOPTS=-q`、16 个用例全 skip → SKIPPED，非 PASS）、`test_classify` 参数化全跳过/空集合 |
| P1-2 | 无回填样本时静默计为 passed | `test_sqlite_fallback_fills_rank1_without_csv` 改为显式 `pytest.skip`；新增 `test_sqlite_available_for_exported_ranks` 无条件断言（非空列表）。真实回归因此如实变为 SKIPPED（16 passed, 1 skipped） |
| P1-3 | R05/R10 下游未受限 | 未在 AE-02 内实现闸门（属 AE-03），新增 `tests/regression/test_known_gaps.py` 两条 strict xfail 用例描述应有行为；矩阵维持 partially，并注明可由 XPASS 追踪 |
| P2-1 | BLOCKED 与 pytest 退出码 2 撞码 | 退出码不再参与判定：内部 `Outcome` 枚举（PASS/SKIPPED/FAILED/BLOCKED），`gate_exit` 只认 `Outcome.BLOCKED`。回归用例：`test_pytest_interrupt_code_two_is_not_blocked` |
| P2-2 | R06 缺同步指标/host_silent 边界、R12 缺回退窗口对照 | R06 新增 `test_missing_sync_metric_is_not_reported_as_measured_zero`（`mstx_available=False`、`silent_unattributed_us>0`、无「同步等待合计」措辞）；R12 新增 `test_explicit_and_fallback_window_have_distinct_semantics`（显式 span/idle=500/300，回退=250/50，空洞同为 50µs） |
| P3 | 报告自相矛盾（3.10 已运行 vs 只能 CI） | §1 已统一为「3.10/3.11 均已实际运行」，删除“仍只能由 CI 声明” |
