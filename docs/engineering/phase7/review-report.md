# AE-08 独立审查报告

**最终结论为 PASS WITH CONDITIONS。** 无 P0/P1 阻断项。AE-00～AE-08 整体达到本地 CPU 开发的 Agent-ready 目标，真实数据覆盖与高风险人审条件继续有效。

## 初版审查

基线 `5ccc856`。对照实际 CLI、工程脚本和仓库生成的 JSON/CSV 审查。初版自动差异工具区分了 28 项真实可观察变化，其中包括 CSV 时长归零、嵌套阻断标记、规则机会等级、`comparable` 与清单状态。只改时间戳和随机实例 ID 的副本报告零变化。以下为同一轮集中修复清单。

| 级别 | 位置 | 独立证据、影响及修复要求 |
|---|---|---|
| P1-1 | `tools/verification.py:344`、`:348`、`:557` | 在无数据环境将 `--real-script` 指向仅含 `pass` 的临时文件，真实回归被错误记为 PASS、退出 0。`PYTEST_ADDOPTS=--collect-only` 时日志只有 `533 tests collected`，verify 仍为 PASS、退出 0。固定仓库真实入口，隔离能跳过测试执行的 pytest 环境选项。 |
| P1-2 | `tools/verification.py:409`、`:442` | 单独执行有效 CPU 验证并请求比较空目录，差异结果为 `invalid_input`，总摘要仍 PASS、退出 0。`--artifact-out` 指向临时 before/after 输入内的 `run_manifest.json` 会覆盖该产物，实际哈希改变。为请求的差异检查记录可聚合状态，统一输出新目录并保护输入。 |
| P1-3 | `tools/artifact_diff.py:79`、`:283`、`:520`、`:966` | 两份 `run_manifest.json` 都为 `[]` 被报告已比较且无差异。完整真实 CSV 表头后跟未闭合引号仍退出 0。相同自然键的两条机会中第一条升为 `probable`，被第二条覆盖后差异数仍为 0。严格读取关键格式，明确同键歧义，保留可核验的级别变化。 |
| P1-4 | `AGENTS.md:29`，`docs/feature-map.md:40`、`:73` | 导航将 `make verify-real` 的真实跳过标为退出 3，实际 Make 将子进程非零折叠为 2。组合流程说明也把几个已提供独立 CLI 的产物说成只由 `analyze` 生成。核对直接入口、Make 包装和导航中声明的环境与落盘字段。 |

反例使用本机临时副本。未改动原始 Profiler 导出，也未执行 NPU 采集。首轮仅靠文件存在和测试通过不足以验收。

## 最终验收矩阵（2026-11-20）

| 任务书项目 | 结果 | 实际证据 |
|---|---|---|
| Feature Map 与开发指南 | PASS | 23 个独立子命令的 `--help` 可执行，核对 35 个模块引用、31 个测试文件及 19 个命名测试。组合与独立入口及 Make/脚本退出码已纠正，P1-4 关闭。 |
| 仓库级验证说明与 PR 模板 | PASS | `SKILL.md` 通过结构校验，`AGENTS.md` 提供手动入口。PR 模板引用真实脚本，包含 Schema 与数据兼容、差异产物、结果和高风险人审。 |
| 摘要状态和真实执行 | PASS | `tools/verification.py:432`、`:477`、`:671` 固定实际执行程序。假脚本与 `/bin/true` 替换均被 CLI 拒绝，`PYTEST_ADDOPTS=--collect-only` 复测实际执行 520 项测试。`scripts/verify_real.py` 在无数据时 BLOCKED，真实套件有跳过时为 SKIPPED。原 P1-1 与其直接影响关闭。 |
| 自动差异与输入保护 | PASS | `tools/verification.py:323`、`:457`、`tools/artifact_diff.py:1029`。请求比较空目录时总状态 FAIL、非零。输出落在输入目录内时提前拒绝，临时输入 manifest 哈希保持一致。有效差异准确区分 CPU 结果、产物比较执行结果和变更内容。P1-2 关闭。 |
| 关键 JSON、CSV、Quality Flags、Evidence 与比较状态 | PASS | 真实 CLI 生成的配对产物报告 28 项变化，包括 CSV 时长到 0、`rank_merge_inconsistent`、受信等级、`comparable` 和 `comparison_status`。等价副本仅改时间戳/实例 ID 为零变化。坏 CSV 和 JSON 数组非零，两条同键机会不再互相覆盖。P1-3 关闭。 |
| 陌生 Agent 入门与故障注入 | PASS | 新会话实际读取两个导航文档，完成 `graph-coverage --top` 帮助与 CLI 测试。专项 5 passed。隔离练习最终复跑 CPU 为 521 passed、29 skipped，真实数据 BLOCKED、NPU 与完整真实分析 NOT_RUN。原有 OD002 对临时错误换算报退出 1，恢复后通过。 |
| 工程门禁与兼容 | PASS | 干净 Python 3.12 venv 从零安装并运行 `make check`、`make verify` 和 console script。最终完整结果为 520 passed、29 skipped，Python 3.10 专项与历史回归 124 passed。生产 Parser、Timeline、Diagnosis 与 Evidence Gate 相对基线无补丁。 |

实际命令、原始日志与产物见[实施报告](implementation-report.md)。两份来源明确的摘要分别记录 CPU/差异 PASS 和无真实数据的入门 BLOCKED。另行读取授权的四组真实数据执行回归，实际 28 passed、1 skipped、0 failed，汇总为 SKIPPED。NPU、新采集、全量真实分析与托管 CI 均未执行。

## 最终问题与累计 Agent-ready 判断

- **P0/P1 无。** 上述四组问题及解释器替换旁路已通过定向复核。
- **P2 无新增。** AE-07 已登记的撤回后优先级评分限制仍在本任务范围外。
- **P3，非阻断。** `tools/verification.py:263` 的 `not_executed` 栏实际枚举所有非 PASS 项。真实回归实际执行后以 SKIPPED 仍出现在该栏。`steps[].state`、`steps[].argv` 和原始退出码提供正确区分，消费者应使用这些字段。后续可使用能表达所有非通过项的字段名，或仅在该栏保留未实际执行的项目。

| 阶段 | 累计状态与依赖 |
|---|---|
| AE-00～AE-02 | 基线审计、统一工程检查和 R01～R12 都有映射。早期发现的 R05/R10 下游缺口由 AE-03 补齐，真实缺 CSV 的历史样本限制由 CPU R05 和显式跳过记录。见[回归矩阵](../regression-matrix.md)。 |
| AE-03～AE-05 | strict 与清单暴露失败，公共事件读取边界保持，架构与领域检查可执行。本轮用实际 CLI 对比产物和 OD002 故障注入复验直接相关边界。 |
| AE-06～AE-07 | 属性测试、授权真实输入回归、来源绑定及确认/撤回闸门均已纳入本地验证。历史回填真实跳过仍保留，已验收的评分限制单列。见[AE-06 复核](../phase5/review-report.md)与[AE-07 复核](../phase6/review-report.md)。 |
| AE-08 | 依据 Feature Map 的陌生 Agent 能完成真实低风险改动和可复核的验证。语义差异工具可辅助人工检查，涉 Schema、单位、阈值、采集、证据升级、性能结论和 CI 的变更继续由人评审。 |

**AE-08 及 AE-00～AE-08 整体均为 PASS WITH CONDITIONS。** 在已声明的本地单用户 CPU 工程范围内达到 Agent-ready。真实历史缺 CSV 回填继续标为跳过，性能结论仍取决于授权输入和实际实验。
