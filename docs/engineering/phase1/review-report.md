# AE-00～AE-02 最终独立复审

日期：2026-11-18。**结论：FAIL**。上轮提出的主要问题已有修复，但真实数据验证入口仍可把跳过的测试报告为 PASS，且一项真实数据回填测试在没有适用样本时以通过计数。AE-01 与真实回归的完成状态因此不能验收。R05、R10 的下游质量闸门仍是已登记的功能缺口。

审查范围为基线 `1785df806af30075caea409a8b64949904edfb92` 至 HEAD `7d1d445`，分支 `feat/ae-00-01-02-agent-ready`；上次审查之后的提交为 `309fcaf`、`095f335`、`7d1d445`。开始时仅 `docs/tasks/` 未跟踪；任务书从该工作区读取。未修改代码、测试或配置；本报告是唯一有意修改的受版本管理文件。

## 任务验收矩阵

| 任务 | 状态 | 证据与范围 |
|---|---|---|
| AE-00 | PASS WITH CONDITIONS | 审计记录基线、调用链和 INV-01～12。上次指出的 INV-02、INV-08 已改为部分强制；INV-01 的 JSON 默认零及其他已知缺口已有登记。`baseline-audit.md:95-100` 的 INV-04/06/09 限定为关键路径，并明确未覆盖范围；不应理解为全局保证。真实数据和报告中历史执行结果本次无法独立复现。 |
| AE-01 | FAIL | `make check`、`make verify` 在本机 CPU 通过；缺数据退出 2、CI 缺数据路径正常。但输出解析与退出码混用导致虚假 PASS 或错误分类，见问题 P1。3.10、3.11 的运行与干净环境安装是其他 agent 的记录，本次未复验；GitHub Actions 未运行。 |
| AE-02 | FAIL（完整验收） | 逐项检查了新增测试的生产入口及主要断言，三项独立变异被捕获。R05/R10 未保护下游使用，R06/R12 覆盖不完整；真实回填场景未执行却被计入通过。其余测试为对应示例提供实质性保护，不等于全面证明所有变体。 |

## R01～R12 测试有效性

状态表示验收覆盖，而非简单 pytest 运行状态；测试均位于 `tests/regression/`。

| ID | 结论 | 代码及断言证据 / 局限 |
|---|---|---|
| R01 | PASS | `test_r01_slice_order.py:34,56` 调用 discovery 与 parser，检查 1/2/10 自然序、独立总数和分片等价；本次字典序变异失败。 |
| R02 | PASS | `test_r02_statistic_missing_hccl.py:46` 检查记录及指标缺失、总量与热点口径说明；使用真实 parser。 |
| R03 | PASS | `test_r03_core_type_rollup.py:52,58` 核查 core type 保留、771 calls、加权平均及反例。 |
| R04 | PASS | `test_r04_pipe_fraction.py:30` 调用 `build_facts` 并断言 1.5 和 2.6；引擎中对大于 1 的说明未由这两个新测试直接验证。 |
| R05 | FAIL（部分覆盖） | `test_r05_rank_merge.py:42,71,97` 实际调用 CLI、collector 和 SQLite parser；检查缺 rank、不可合并文案及来源能力。不可合并时 CLI 仍返回 0，测试未证明分析下游拒绝合并；真实无 CSV 回填场景见 P1-2。 |
| R06 | FAIL（部分覆盖） | `test_r06_mstx_enum_only.py:43` 验证枚举表不代表采集及空表区别；未验证任务书要求的缺同步指标和 host_silent 不猜测原因。 |
| R07 | PASS WITH CONDITIONS | `test_r07_graph_operator_shrink.py:48,69` 实测 compare 的 presence、缺失和警告，递归检查键及中文文本；反例只覆盖词表，不能保证所有同义表达或其他输出路径。上轮顶层检查问题已改善。 |
| R08 | PASS WITH CONDITIONS | `test_r08_communication_unit.py:36,52,59` 调用 parser 和 timeline，独立断言 110462688 ns → 110462.688 µs、非通信不缩放；本次移除映射 scale 的变异被捕获。真实数据本次 BLOCKED。 |
| R09 | PASS | `test_r09_export_columns.py:63,93,114` 检查缺列标记、未知列、flop 单位和宽表数值，调用真实 parser。 |
| R10 | FAIL（部分覆盖） | `test_r10_truncated_duration.py:31` 调用 timeline 并检查 flag、说明和无不均文案；compare 不消费 flag，未达到任务书要求的相关对比受限。 |
| R11 | PASS | `test_r11_utilization_denominator.py:38,56` 调用 `build_facts`，用独立算术核对活跃时间分母，缺分母不产指标。 |
| R12 | PASS WITH CONDITIONS | `test_r12_window_origin.py:18` 使用显式窗口比较两个 epoch、检查空洞与 idle；本次 cursor=0 变异使两项失败。该文件未独立验证任务书所要求的显式窗口与回退窗口区别。 |

## 上轮问题复核

- 原 CI 的 `cmd || ...` 已改为 `--expect-blocked`；正常缺数据路径成功，0/1/3 等有契约测试，但新的退出码碰撞见 P2-1。
- 失败加跳过现在先分类为 FAIL（`scripts/verify_real.py:56`），有直接测试；跳过检测仍受输出格式影响，见 P1-1。
- R07 增加递归扫描及嵌套反例；扫描是有限词表。
- 审计将 INV-02/08 改为部分强制，列出未验证路径。

## 本次实际执行

| 命令或检查 | 状态 | 结果 |
|---|---|---|
| `git status --short --branch`, `git log --oneline -8`, `git diff --stat 1785df8 HEAD` | PASS | 确认范围和工作区 |
| `PYTHONDONTWRITEBYTECODE=1 make check PYTHON=python3` | PASS | Ruff、mypy 48 文件和 validate 退出 0 |
| `PYTHONDONTWRITEBYTECODE=1 make verify PYTHON=python3 DEMO_OUT=/tmp/opt-review2-demo` | PASS / SKIPPED | 203 passed、16 skipped；demo 成功，退出 0 |
| `python3 scripts/verify_real.py --expect-blocked` | PASS | 未设置数据根时正确识别 BLOCKED(2)，闸门退出 0 |
| `python3 -m pytest tests/test_real_data.py -q -p no:cacheprovider`，随后将真实输出送入 `classify` | FAIL（预期负向验证） | 16 项全部 skip，pytest 退出 0，双重 `-q` 输出无摘要；`classify` 返回 `(0, 'PASS ... ? passed，0 skipped')`。这是实际复现而非实施报告引用。 |
| 隔离 `/tmp` 副本变异 R01/R08/R12，再分别运行对应 pytest 文件 | PASS（预期捕获） | 字典序、删除 scale、cursor=0 均退出 1，分别被对应测试捕获；主树未变异。 |
| 真实数据 / NPU / 3.10 / 3.11 / GitHub Actions | BLOCKED 或 NOT RUN | 本次无真实数据根；未使用 NPU；未独立运行其他解释器和托管 CI。其他 agent 的 14 次变异及真实数据、干净环境报告是历史声明，非本次实测。 |

## 问题清单

### P1-1：真实回归可把所有跳过报告为 PASS

`scripts/verify_real.py:58-66,90-98` 从 pytest 的人类可读摘要中解析 skipped；额外 `-q`（例如用户环境 `PYTEST_ADDOPTS=-q`）会隐藏摘要，所有测试跳过时 pytest 仍退出 0，而代码返回 `PASS (? passed, 0 skipped)`。本次用实际全跳过输出直接复现。影响：缺失真实证据可能被作为完成验收。建议使用 pytest 结构化结果/插件或专门的 session hook 计数，强制至少一个测试执行且计数可解析，失败关闭；增加额外 quiet、无摘要和全跳过端到端负向用例。

### P1-2：无真实回填样本的测试静默计为通过

`tests/test_real_data.py:414-442` 的文档承诺无历史样本时显式 skip，但实际仅检查有 CSV 的目录是否有 SQLite 就 return，pytest 将其计为 passed，且循环无匹配项时可空通过。`docs/engineering/phase1/independent-verification.md:50` 的 16 passed 不能证明真实缺 CSV 回填已执行。影响：真实回归覆盖和通过数误导。建议无候选时明确 `pytest.skip` 并让 verify-real 报 SKIPPED；将有 CSV 的 SQLite 前提拆为独立测试并断言非空，保留 CPU 回填回归。

### P1-3：历史质量异常尚未在消费端受限

`tests/regression/test_r05_rank_merge.py:42-52` 仅验证 CLI 文案且接受 0；`tests/regression/test_r10_truncated_duration.py:31-44` 仅验证 timeline 标记与文案。`docs/engineering/baseline-audit.md:96` 已承认 compare 未消费截断 flag。影响：异常 rank 仍可能进入不受限制的分析或对比，不能满足任务书 AE-02 中 R05/R10 的完整预期。建议添加覆盖实际分析/对比消费链的反例，并在 AE-03 实现可靠闸门；在此之前矩阵维持部分覆盖，不能标为完整通过。

### P2-1：BLOCKED 与 pytest 失败共用退出码 2

`scripts/verify_real.py:56-57,77-79,98-100` 透传 pytest 退出码 2（如中断或收集错误），而 `gate_exit` 将任意 2 认作缺数据并返回 0。本次直接验证 `classify(2, ...)` 报 FAIL 后 `gate_exit(2, expect_blocked=True)` 却报 OK。公开 CI 默认无数据时不触发，但当数据目录存在并发生该错误时可能误报缺数据保护通过。建议内部传递带状态的结果，而非只比较共用的整数，并加入此端到端反例。

### P2-2：R06/R12 必需边界尚无直接覆盖

`tests/regression/test_r06_mstx_enum_only.py:43` 未检查同步指标缺失和 host_silent 原因限制；`tests/regression/test_r12_window_origin.py:18-30` 两例均用同一种显式窗口，未对照回退窗口语义。影响：矩阵的 enforced 表述比任务书要求更宽。建议增加实际消费路径的反例及两种窗口来源的比较；在完成前将对应矩阵标为部分覆盖。

### P3：验证报告存在自相矛盾

`docs/engineering/phase1/independent-verification.md:14,20` 一处声明已运行 3.10，另一处称 3.10 只能由 CI 声明。影响：读者无法确定证据状态；建议核对日志后统一表述。

## 兼容性、限制与后续判断

本轮相对于基线存在通信时间缩放、缺列告警和 `ParseMeta.extra` 类型等行为变化；本次未发现足够证据认定其他 API 契约全面兼容。真实数据和 NPU 未用于本次复审，不能独立确认通信单位真实结果或性能结论。现存 D6/D7、单位双边界、证据来源及内存缺口已在审计中登记，不把 synthetic 结果解释为真实性能。

必须修复 P1-1、P1-2，并补齐或明确阻塞 AE-02 中的 R05/R10、R06/R12 相关要求；P2-1 应一并修复并回归验证。**允许开展 AE-03～AE-04 的修复工作，但不应把 AE-00～AE-02 标为全部通过或以此合并其下游，直到上述验收缺口及真实数据状态得到明确处理。**

## 2026-11-19 修复后复核与后续工作判断

复核 HEAD `ef79fc54bc26fa1b8fb6c487c4612f6a1a620e2e`。此前真实回归摘要误判问题已改为解析 JUnit 结构化结果，并对缺失结果失败关闭；无 CSV 回填样本现在明确 skip；内部使用独立状态区分缺数据与 pytest 失败。R06 与 R12 增加了相应边界用例。上述修复及契约测试已审阅。

本次执行：

| 检查 | 结果 |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 make check PYTHON=python3` | PASS：Ruff、mypy、配置验证通过 |
| `PYTHONDONTWRITEBYTECODE=1 make verify PYTHON=python3 DEMO_OUT=/tmp/opt-review3-demo` | PASS / SKIPPED / XFAIL：211 passed、17 skipped、2 xfailed；synthetic demo 通过 |
| `python3 scripts/verify_real.py --expect-blocked` | PASS：未设置数据根时识别 BLOCKED；此结果不表示真实回归通过 |
| `python3 -m pytest tests/regression/test_verify_real_contract.py tests/regression/test_known_gaps.py -ra -o addopts='' -p no:cacheprovider` | 20 passed、2 xfailed |
| 真实 Profiling 数据与 NPU | BLOCKED / NOT RUN：本次未使用真实数据或 NPU |

**判断：可以开展 AE-03，并可准备 AE-04；AE-02 尚不能标记为完整通过。** 两个 strict xfail 对应 R05、R10：比较路径尚未消费 rank 不可合并及截断标记。应在 AE-03 实现质量闸门、验证实际输入到比较的传播链，并将预期失败改为正式通过的测试。下游变更合并前应完成这些验证；真实数据回归须单独报告，不能将跳过计为通过。
