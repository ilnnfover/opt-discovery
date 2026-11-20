# AE-07 实施报告

**最终结论为 PASS WITH CONDITIONS。** 证据升级契约通过独立验收。撤回后的优先级分数和历史真实回归跳过项见末节。

## 基线与范围

开发分支为 `feat/ae-07-evidence-gate`，基线为 GitHub `main` 上 AE-06 的合并提交 `e184711315e87ccb5ab92ddd4399ded3a6bae161`。实施依据为原始任务书 AE-07 及其通用 DoD。AE-06 验收记录中留给本阶段的直接反馈来源缺口纳入本次任务。

CodeBuddy 使用 DeepSeek V4.1 Flash 实现和集中修复。Codex 审查代码、独立执行检查并编写两份验收报告。每次调用前后核对 Git 状态。既有未跟踪的任务书保留在工作区。真实输入只读，验证产物写入临时目录。

## 基线与首轮执行记录

`$PY312` 表示 `/tmp/ae06-review-env.42pW5C/bin/python`。`$DATA_ROOT` 表示已授权的四组统一补采别名目录。基线命令在 `git archive e184711` 的临时目录执行。

| 阶段 | 命令或独立核对 | 结果 |
|---|---|---|
| 基线静态 | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 make check PYTHON="$PY312"` | PASS，退出 0。 |
| 基线 CPU 与合成 | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 make verify PYTHON="$PY312" DEMO_OUT=/tmp/ae07-baseline.LCIOSk/demo-out` | PASS，退出 0。393 passed、29 skipped，四组合成产物均生成。 |
| 基线真实 | `OPT_DISCOVERY_DATA_ROOT="$DATA_ROOT" PYTHONDONTWRITEBYTECODE=1 "$PY312" scripts/verify_real.py` | SKIPPED，脚本退出 3。28 passed、1 skipped，为缺 CSV 回填的历史分支。 |
| 基线历史产物 | 只读检查本地 `out` 下的现存 `opportunities.json` | 共 12 份文件，306 条 `hypothesis`，未发现历史 `confirmed`。 |
| 基线漏洞反例 | 在临时 Python 进程调用 `apply_feedback`、`Opportunity.from_dict` 和 `build_report` | 裸比较将一条合成机会升至 `probable`。标成 `confirmed` 的无验证 JSON 被直接读入，报告也显示 `confirmed`。 |
| 首轮 CPU 与合成 | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 make verify PYTHON="$PY312" DEMO_OUT=/tmp/ae07-initial-review.SjjvNu/demo` | PASS，退出 0。457 passed、29 skipped，四组合成流程完成。 |
| 首轮静态 | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 make check PYTHON="$PY312"` | FAIL，退出 2。末版还有 `opt_discovery/experiment/feedback.py:33` 的 Ruff F401。 |
| 基线与首轮真实机会 | 在相同四组授权数据的 rank 0 读取 `op_statistic`，调用 Parser、诊断、机会生成和 JSON 重读 | 两版分别得到相同的 `hypothesis` 数量。decode eager 2、decode graph 34、prefill eager 16、prefill graph 17。没有产生 `confirmed`。 |

首轮 `codebuddy -p -y --model deepseek-v4.1-flash` 达到轮次上限。虽然终端进程退出 0，模型的结构化结果为 `error_during_execution`。首轮验收根据独立实测判为 FAIL，见[审查报告](review-report.md)。

## 最终实现与迁移

| 位置 | 行为 |
|---|---|
| `opt_discovery/schemas/evidence_gate.py:379`、`:771`、`:1027` | `ValidationBundle` 显式绑定机会实例、算子、workload、变更、比较双方、输入及来源。统一闸门验证可比性、正确性、性能改善、两侧测量引用、样本及不确定性。默认阈值为 5%，至少 3 个样本，已使用的阈值写入审计供重读。有效单范围最高 `probable`，只有同一个绑定下使用独立产物的 kernel 与 model 或 serving 结果可到 `confirmed`。 |
| `opt_discovery/schemas/opportunity.py:135`、`:224`、`:297` | 新机会写出实例 ID 和来源。生成实例 ID 在 JSON 往返时保持稳定。历史缺来源的机会可读，重新分析真实输入后才生成可绑定的新目标。读写边界复核 `confirmed`，缺引用、摘要不符或撤回的确认被降级并记录为不可信。 |
| `opt_discovery/experiment/feedback.py:145`、`opt_discovery/cli.py:665` | `--compare` 记录现有 benchmark 观察，原 CLI 参数仍可用。`--validation` 使用结构化验证，两种调用互斥。显式拒绝保留原因且返回非零，失败不覆盖既有可信测量。证据变更和生命周期分开。 |
| `opt_discovery/schemas/evidence_gate.py:1059`、`opt_discovery/report/generator.py:248` | 提供显式撤回 API，保留原验证、失效原因和来源。报告遵守同一可信边界，并在读取已降级的记录时保留不可信标记。 |
| `configs/quality/arch_lint.yaml:66` | OD004 逐个登记受控写点，保留路径、行号和精确 RHS 匹配。普通诊断的证据上限及其现有统计策略保持原样。 |

本地 `manual` 和 `measured` 由提交者声明。闸门核对关联键和产物摘要，重读时要求引用的产物仍可读取。文档给出最小 Python 与 CLI 用法、历史 JSON 的迁移方式和产物搬迁限制。示例以临时测试产物演示契约。见[迁移说明](migration.md)。

## 最终执行记录

集中修复及其拒绝审计收尾由同一指定模型完成，两次均得到结构化 `success`。以下为 Codex 独立执行结果。`$PY310` 表示 Python 3.10 虚拟环境。`$DEMO_DIR` 为 `/tmp/ae07-accepted.IltNbE/demo`。

| 检查 | 实际命令或调用 | 结果 |
|---|---|---|
| 静态与配置 | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 make check PYTHON="$PY312"` | PASS，退出 0。Ruff、架构检查、mypy 的 54 个源文件及配置验证均通过。 |
| 完整 CPU 与 synthetic | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 make verify PYTHON="$PY312" DEMO_OUT="$DEMO_DIR"` | PASS，退出 0。485 passed、29 skipped，四组合成演示完成。 |
| 最低 Python 版本 | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 "$PY310" -m pytest -o addopts='' -q -p no:cacheprovider tests/test_evidence_gate.py tests/test_feedback.py tests/test_opportunity.py tests/test_arch_lint.py tests/property/test_contracts.py` | PASS，退出 0。165 passed。 |
| 独立契约反例与正例 | `PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 "$PY312" /tmp/ae07-codebuddy.XjbYk6/probe_final.py` | PASS，退出 0。8 组通过，包含实际 CLI、直接 API、读写和报告路径。 |
| OD004 注入 | 在临时副本中追加同文件、同 RHS 的未授权写入，通过 `tools.arch_lint.run_lint` 核验 | PASS，准确命中注入处，原授权写点没有误报。 |
| 文档样例 | 使用 `$PY312` 执行迁移说明 §2.3 的 Python 代码块 | PASS，退出 0。临时测试产物获得预期的合法 `confirmed`。 |
| 缺数据门禁 | `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 "$PY312" scripts/verify_real.py --expect-blocked` | PASS，退出 0，内层真实测试状态为 BLOCKED。 |
| 授权真实数据 | `OPT_DISCOVERY_DATA_ROOT="$DATA_ROOT" PYTHONDONTWRITEBYTECODE=1 "$PY312" scripts/verify_real.py` | SKIPPED，退出 3。28 passed、1 skipped、0 failed、0 errors。 |
| 真实机会级别对照 | `OPT_DISCOVERY_DATA_ROOT="$DATA_ROOT" PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 "$PY312" /tmp/ae07-codebuddy.XjbYk6/real_opportunities_probe.py` | PASS，退出 0。结果与基线逐 Workload 一致，69 个机会均为 `hypothesis`，没有 `confirmed`。 |
| 补丁完整性 | `git diff --check` | PASS，退出 0。 |

## 已知限制与交付状态

真实回归继续显式跳过历史缺 CSV 回填场景，本批各 rank 都有 CSV。CPU R05 继续覆盖该路径。新采集、NPU 核验及托管 CI 未执行。

显式撤回会降低有效等级并禁止复用已撤回证据，但不会重算旧优先级分数。审查报告记录了实测影响及修复建议。验收通过后的改动和报告保留在本地开发分支，远端操作留待用户授权。AE-08 未开始。
