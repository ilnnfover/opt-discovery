# AE-08 实施报告

**验收结论为 PASS WITH CONDITIONS。** AE-08 达到本地 CPU 开发和验收要求。AE-00～AE-08 整体在现有数据覆盖及人工评审边界内达到 Agent-ready。

## 基线与范围

分支 `feat/ae-08-agent-ready` 从 GitHub `main` 的 `5ccc856bd8a909bd14179b8505bf705386b50c39` 创建，基线包含 AE-07 合并。依据原始任务书 AE-08、通用 DoD 和工程总体验收项。工作开始时只有原有 `docs/tasks/` 未跟踪。

CodeBuddy DeepSeek V4.1 Flash 实施与修复。Codex 核对 Git、审查变更、独立执行验收并撰写本报告。项目导航、验证摘要和产物比较沿用 `make check`、`make verify`、真实回归脚本与现有产物结构。

## 基线执行

在 `git archive 5ccc856` 展开的 `/tmp/ae08-baseline.73Ko9w` 中执行，解释器为 `/tmp/ae06-review-env.42pW5C/bin/python`，环境清除 `OPT_DISCOVERY_DATA_ROOT`。

| 实际检查 | 结果 |
|---|---|
| `make check PYTHON=<Python 3.12 venv>` | PASS，退出 0。Ruff、架构检查、mypy（54 个文件）和配置检查均完成。 |
| `make verify PYTHON=<Python 3.12 venv> DEMO_OUT=/tmp/ae08-baseline.73Ko9w/demo` | PASS，退出 0。485 passed、29 skipped，四组合成产物均生成。 |
| 使用合成产物调用 `summary --allow-legacy --dir <before>` 和 `compare --a <eager-table> --b <graph-table> --out-dir <before>` | 均退出 0，自动生成两个 summary CSV、一个可比的 `compare_*.json` 和 `run_manifest.json`。 |
| 在临时副本中修改一个时长为 0，增加阻断型 `rank_merge_inconsistent`，再次调用 `summary` 和 `compare` | 生成实际的 CSV 数值差异和两份不可比状态。对比 JSON 为 `comparable=false`，清单 `status` 与 `comparison_status` 都为 `incomparable`。现有非 strict 命令按原契约退出 0。 |

完整的首轮反例及集中修复记录见[独立审查报告](review-report.md)。

## 最终交付

| 交付 | 实现与范围 |
|---|---|
| `docs/feature-map.md`、`AGENTS.md` | 按用户目标连接现有 CLI、实现、输入、输出、测试与运行前提。开发指南约 86 行，引用可执行检查及已有回归矩阵。 |
| `.agents/skills/verify-opt-discovery/SKILL.md` | 仓库级验证说明，涵盖 CPU 检查选择、真实回归、前后产物及五态解释。校验器通过。在本次 CodeBuddy headless 会话中未观察到自动加载，指南保留手动读取入口。 |
| `scripts/verify_summary.py`、`tools/verification.py` | 执行现有 `make` 入口及直接真实回归脚本，在新目录写五态 JSON、实际 argv、退出码、日志和 demo 产物。进程内比较单独写 `invocation`，`argv` 与 `exit_code` 为 `null`。 |
| `scripts/compare_artifacts.py`、`tools/artifact_diff.py` | 从两份现成输出生成可审阅的语义差异。覆盖关键 JSON/CSV、Quality Flags、受信证据等级、比较状态和清单状态。输入只读。 |
| `.github/pull_request_template.md` | 给出范围、数据和 Schema 兼容、五态摘要、前后差异、风险、限制及高风险人工审查字段。 |
| `tests/test_verification_summary.py`、`tests/test_artifact_diff.py` | 35 个新增测试，覆盖入口、五态、错误输入、输出隔离、产物字段和真实脚本的缺数据状态。 |

初次实现由指定模型完成。根据独立审查清单完成一次集中修复，模型在该调用达到轮次上限。随后用同一模型收尾解释器替换绕过和日志中的进程内调用表示，结构化结果为 `success`。每次开发调用前后均核对 Git 状态。正式补丁只改工程导航、验证工具及专项测试。

## 独立执行记录

`$PY312` 表示 `/tmp/ae06-review-env.42pW5C/bin/python`，`$PY310` 表示 Python 3.10 虚拟环境。`$FIXTURES` 表示 `/tmp/ae08-review-fixtures.31b28c`。CPU 检查均清除真实数据根环境变量。

| 验证 | 实际命令与结果 |
|---|---|
| 最终静态、完整 CPU、合成及产物比较 | `$PY312 scripts/verify_summary.py --out /tmp/ae08-final.PbLGax/verification --artifacts-a "$FIXTURES/before" --artifacts-b "$FIXTURES/changed"`。总状态 PASS，退出 0。实际 `make check`、`make verify` 各退出 0，520 passed、29 skipped，四组合成流程生成 12 份 JSON。产物比较报告 28 项变化。 |
| Python 3.10 专项与历史回归 | `$PY310 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_verification_summary.py tests/test_artifact_diff.py tests/regression`。PASS，退出 0，124 passed。 |
| 独立七组正反例 | `$PY312 /tmp/ae08-codebuddy.5wxg5A/review_probe.py`。PASS，退出 0。覆盖实际语义变化、仅噪声、坏输入、同自然键、伪解释器、缺数据、输出位置及 pytest 只收集环境。 |
| 导航与验证说明 | 逐条执行 23 个 CLI `--help`。35 个模块引用、31 个测试文件和 19 个命名测试存在。仓库级 `SKILL.md` 结构校验通过。 |
| 干净 CPU 安装 | `make clean-env-check PYTHON=python3` 首次因宿主代理的 SOCKS 依赖缺失退出 2。仅在重试进程撤销代理变量后退出 0，新 venv 从零安装，`make check`、`make verify`、console script 与缺数据断言通过。重试日志为 `/tmp/ae08-clean-direct.rbNIjJ.log`。 |
| 授权真实回归 | 设置既有授权根后执行 `$PY312 scripts/verify_summary.py --out /tmp/ae08-real-final.ik54fn/verification --no-check --no-verify --real`。实际退出 3、总体 SKIPPED，28 passed、1 skipped、0 failed、0 errors。脚本保持无数据 BLOCKED 与真实用例跳过的区别。 |
| 现有分析产物兼容 | 在临时目录复制现存 `out/steady` 输出并执行 `scripts/compare_artifacts.py`。退出 0，比较有效，副本与原目录零差异。该操作只读现有产物。 |

上表的 CPU-only 摘要将真实回归、NPU 实验和完整真实分析列为 `NOT_RUN`。另行执行的真实回归跳过历史缺 CSV 回填场景，CPU R05 继续覆盖。新采集、NPU、全量真实 `analyze` 和本阶段 GitHub Actions 均为 `NOT_RUN`。

## 新 Agent 导航验收

独立 CodeBuddy 会话从隔离副本读取 `AGENTS.md` 与 `docs/feature-map.md`，定位 `opt_discovery/cli.py` 的 `graph-coverage --top` 和 `tests/test_graph_coverage.py`。它增加帮助及直接 CLI 测试，说明选项限制终端与 JSON 的 `top_fallback` 列表，保留原聚合和默认值。定向测试 5 passed。两个文件的练习补丁仅存于 `/tmp/ae08-onboarding.CcAYXj/evidence/graph-coverage.patch`。

练习在未设置数据根时显式运行带 `--real` 的摘要命令。`make check` 和 `make verify` 退出 0，真实入口 BLOCKED、退出 2，NPU 与完整真实分析为 NOT_RUN。在隔离副本临时引入 `total_us / 1000.0` 后，`python -m tools.arch_lint --config configs/quality/arch_lint.yaml` 报 OD002、退出 1。恢复注入后检查退出 0，Git 差异仅保留帮助与测试。故障日志为 `/tmp/ae08-onboarding.CcAYXj/evidence/fault-injection-arch-lint.log`。

独立复跑最终版摘要入口，复用该会话产生的练习补丁，专项仍为 5 passed，完整 CPU 为 521 passed、29 skipped，实测 `BLOCKED`、退出 2 的归类一致。最终练习摘要为 `/tmp/ae08-onboarding-replay.DjCI2p/verification/verification_summary.json`。

## 交付状态

阶段实现、测试与报告进入一个独立本地提交。工程验收余项及既有数据边界见[独立审查报告](review-report.md)。本轮范围止于 AE-08。
