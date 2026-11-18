# AE-00～AE-02 独立验收报告

日期：2026-11-18。结论：**FAIL**。已发现检查结果分类缺陷；此外本次独立审查尚未完成全部测试逐项代码核查与变异验证，不能据此宣称完整验收。

基线 `1785df806af30075caea409a8b64949904edfb92`，本轮 HEAD `98d67ab`，分支 `feat/ae-00-01-02-agent-ready`，完整范围为两者的 diff。审查开始时 `phase1/implementation-report.md` 已被工作区删除，从 HEAD 读取；`docs/tasks/` 和 `docs/engineering/phase1/` 已未跟踪。任务书未提交，以工作区版本为准。未恢复或修改这些原有改动。

## 任务验收

| 任务 | 状态 | 证据与限制 |
|---|---|---|
| AE-00 | NOT RUN（完整验收） | 阅读了基线审计和实施报告；基线与提交边界吻合。12 个不变量未逐个独立复现，不能认证 enforced 标签。审计第 87 行的 INV-02 由枚举和局部封顶推断全局不可伪造，证据不足，建议列为部分验证并核查所有构造及消费边界。第 93 行 INV-08 的全路径保证同样需独立验证。其他标签尚未完成核验。 |
| AE-01 | FAIL | CPU 测试、静态检查、配置与 demo 可运行；CI 和真实回归分类存在下述缺陷。Python 3.10 和 GitHub Actions 未实际运行。 |
| AE-02 | NOT RUN（完整验收） | 全量测试执行通过（15 项跳过）；已阅读矩阵及 R05/R07/R10 测试，尚未逐项检查所有实现，也未独立执行变异。不能将实施报告中的变异记录作为本次实测。 |

## R01～R12 有效性矩阵

下表 NOT RUN 指本次独立有效性审核或变异验证未完成，不表示 pytest 未执行。除 R05/R07/R10 外，以下覆盖描述为矩阵声明，未独立认定有效。

| ID | 目标及证据 | 独立判定 |
|---|---|---|
| R01 | 自然排序与分片聚合，`tests/regression/test_r01_slice_order.py` | NOT RUN：未独立变异 |
| R02 | 缺 HCCL，`test_r02_statistic_missing_hccl.py` | NOT RUN：未独立核验断言 |
| R03 | Core Type 加权聚合，`test_r03_core_type_rollup.py` | NOT RUN：未独立核验断言 |
| R04 | 大于 1 的流水占比，`test_r04_pipe_fraction.py` | NOT RUN：未独立核验断言 |
| R05 | `test_r05_rank_merge.py:43` 调用真实 CLI，检查不一致及缺 rank；第 76 行检查 sqlite 回填 | 部分有效：检查 CLI 文案和退出，但不证明下游分析拒绝合并；不一致场景本身仍返回 0。 |
| R06 | MSTX 枚举表，`test_r06_mstx_enum_only.py` | NOT RUN：未独立核验断言 |
| R07 | `test_r07_graph_operator_shrink.py:39` 调用 compare；检查 presence 和告警 | 部分有效：禁止收益断言仅检查部分键，未检查嵌套文本和其他字段，见问题清单。 |
| R08 | 通信单位，`test_r08_communication_unit.py` | NOT RUN：未独立变异；真实数据语义未验证 |
| R09 | 导出列差异，`test_r09_export_columns.py` | NOT RUN：未独立核验断言 |
| R10 | `test_r10_truncated_duration.py:33` 调用真实 timeline，检查标记和措辞 | 部分有效：未验证 compare 消费质量标记，不能阻止下游错误结论。 |
| R11 | 利用率分母，`test_r11_utilization_denominator.py` | NOT RUN：未独立变异 |
| R12 | epoch 空洞，`test_r12_window_origin.py` | NOT RUN：未独立变异 |

测试文件的完整路径均位于 `tests/regression/`。报告列出的 R01/R08/R11/R12 故障注入为实施者声明，未由本次审查复现。

## 实际验证

| 命令 | 状态 | 结果 |
|---|---|---|
| `git status --short --branch`; `git log --oneline -8`; `git diff --stat 1785df8 98d67ab` | PASS | 确认提交范围和原有工作区状态 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | PASS / SKIPPED | 退出 0；192 项通过、15 项跳过，后者为真实数据测试 |
| `python3 -m ruff check . --no-cache` | PASS | 退出 0 |
| `python3 -m mypy --cache-dir /tmp/opt-review-mypy` | PASS | 48 个文件无问题 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m opt_discovery.cli validate` | PASS | 配置通过 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m opt_discovery.cli demo --out /tmp/opt-review-demo` | PASS | 退出 0，四组 synthetic 输出 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py` | BLOCKED | 未设置数据根，退出 2 |
| 真实 Profiling 数据、NPU、Python 3.10、GitHub Actions | NOT RUN | 无可用数据或环境；没有重新采集 |
| 本次隔离变异 | NOT RUN | 尚未执行，实施者记录不能替代独立复现 |

`make check`、`make verify` 未直接运行；上表执行了其对应底层命令并将缓存和 demo 写至 `/tmp`。未核实全新环境安装。

## 问题清单

### P1：CI 未严格断言缺数据返回 BLOCKED

`.github/workflows/ci.yml:47` 的 `command || [ $? -eq 2 ]` 在命令返回 0 时直接成功，因此即使真实数据检查错误返回 PASS，CI 仍通过，与第 46 行的要求相反。应捕获退出码后明确断言恰为 2，并对 0、3、其他错误做负向验证。

### P1：失败与跳过并存时被标记为 SKIPPED

`scripts/verify_real.py:47` 在检查 pytest 非零退出前优先匹配 skipped；同时发生失败和跳过时报告 SKIPPED、返回 3，隐藏失败分类。应先处理非零失败，再分类跳过，并增加失败加跳过的契约测试。现有代码仍返回非零，但自动化调用者无法准确区分失败与缺失。

### P2：图收缩负向断言覆盖不完整

`tests/regression/test_r07_graph_operator_shrink.py:68` 只搜索顶层键，之后仅排除 delta 的三个精确键；收益结论可出现在 observations、warnings、嵌套字段或其他名称而不触发失败。应针对实际用户可见输出构造错误结论的反例并验证被拒绝，覆盖结构化字段及文本。

### P2：不变量全局强制状态证据不足

`docs/engineering/baseline-audit.md:87` 将 INV-02 标为 enforced，但所列枚举、来源合并和诊断封顶只证明局部行为，不能证明所有来源不可伪造或提升。影响后续任务依赖判断；应标明局部覆盖并检查构造、反序列化和消费路径。同理第 93 行 INV-08 应补充全路径证据。

## 修复与后续判断

必须修复两个检查结果分类问题并补充负向测试；补强 R07 并修订审计中过度确定的状态。必须完成 R01～R12 独立逐项核查及关键变异、在受支持 Python 版本和干净 CPU 环境验证；真实数据可用时验证解析语义变更。当前真实数据不可用，通信单位修复的真实回归不能由 synthetic 代替。R05/R10 的质量闸门缺口已在实施材料中列为后续工作。

**暂不允许以 AE-02 已通过为依据合并 AE-03～AE-04。** 可准备后续工作，但应先修复本报告阻断问题并完成独立验收；涉及真实语义的变更在真实回归前保持待验证。
