# AE-03 独立验收 Review

审查日期：2026-11-19（Asia/Shanghai）。**总体验收：FAIL。** R05 和 R10 在双 rank 样例中已从实际分析链路传到 compare，且严格比较返回 4。仍存在 strict 假成功、采集契约绕过、不可比时输出比较比值，以及将上一轮数据纳入本轮产物的问题。暂不以 AE-03 已通过为依据进入 AE-04 的正式验收或合并。可以并行准备 AE-04 的独立设计和测试。

基线为 HEAD 的直接父提交 `e7d8aaea9b8e24d78ff8229780a4bed5927d0c3c`，也与本地 `origin/main` 一致。审查对象为 `feat/ae-03-run-contract` 的 `ea6cd83df3684d1f3c8cba1b7171f8c4dd38a40b`。对照原始任务书 AE-03 和 §5.3、Phase 1 复核、Phase 2 实施报告及完整基线差异（14 个文件）。起始工作区无已跟踪改动，唯一未跟踪文件为原任务书，已纳入验收依据。

## 验收矩阵

| 任务书要求 | 判定 | 独立证据 |
|---|---|---|
| 正常运行，选定的必需步骤完成才返回 0 | FAIL | 正向测试会接受 required timeline 为 skipped。双卡只交付一个 rank 或另一个 workload 缺日志时，strict 仍可能返回 0。 |
| 缺输入和子步骤异常有稳定状态 | FAIL | 全无导出返回 2，损坏 timeline DB 的现有用例返回 3。缺日志可假成功，缺映射返回 1 却保留旧运行清单。 |
| R05/R10 从采集形态贯通到比较 | PASS WITH CONDITIONS | 临时目录中构造同任务数、累计时长 4 倍差异的两个 rank。实际 CSV、轮次日志和 SQLite 经 collector、parser、timeline 和 analyze 后，同时产出两个标记。独立 compare 重读 JSON 后返回 4，deltas 为空。缺失预期 rank 和损坏标记仍可绕过。 |
| 采集契约不通过时拒绝有效定量差异 | FAIL | 修改一个表的轮次，现有契约 API 判 incomparable，但 strict compare 返回 0。阻断标记正常生效时仍输出 total_ratio。 |
| 可选 source-map 失败保持核心成功 | PASS | 故障注入仅替换 source-map 入口，主体执行真实 CLI 编排，断言可选阶段 failed、退出 0 和 stderr。 |
| manifest 准确记录运行、阶段及产物 | FAIL | 正常、partial、incomparable 和无导出失败有基本记录。阶段状态、失败更新、复用目录及自定义清单路径存在缺口。 |
| 避免旧产物冒充本轮输出 | FAIL | 同名旧 timeline 被登记，旧 workload 数据进入本轮 CSV，check-manifest 仍返回 0。 |
| 兼容普通 CLI、Schema 及分析语义 | PASS WITH CONDITIONS | 已验证普通 compare 不可比仍返回 0、报告保留原字段并新增标记。旧表可读。ComparisonReport 的位置参数兼容性有回退风险，strict 的旧表兼容策略需与未验证状态区分。 |
| 可执行回归覆盖和 DoD | FAIL | 统一检查与回归通过，原两个 strict xfail 已删除。替代用例验证消费端，但没有锁定实际采集到 compare 的传播，也没有覆盖本报告复现的失败。 |

## 问题清单

本次未发现 P0。下列路径及行号以审查提交为准。

| 编号 | 位置 | 证据与影响 | 修复建议 |
|---|---|---|---|
| P1-1 | `opt_discovery/cli.py:1039`、`:1139`、`:1375`，`opt_discovery/collectors/profiler_dir.py:189` | 移除 eager 的 workload.log 后，graph 正常完成，strict 返回 0，清单总体 success，eager 记录 skipped。存在部分输出但必需输入未完成。全无导出时 parse 仍记 passed。默认 timeline 缺 DB 时 required=true、skipped 也可总体成功。discover 对缺失的配置导出目录直接 continue，调用方看不到该项。 | 明确本次选中的 workload 及阶段要求，分别记录缺导出、缺日志、缺 DB、明确禁用及未执行，从阶段结果派生状态和退出码。增加混合成功与缺输入的测试。 |
| P1-2 | `opt_discovery/analyzer/rank_quality.py:265`、`:137`，`opt_discovery/cli.py:1191` | `declared=len(bundle.ranks)` 统计实际找到的 rank。两个 device_count=2 的 workload 各交付 rank0，strict 返回 0，质量标记为空。超过两个 rank 时仅比较排序后的前两个，且两个表可用后其余解析问题被丢弃。只找到部分 timeline DB 也没有覆盖检查。 | 以 workload/采集契约记录的预期 rank 集合核对已发现、可解析和参与 timeline 的集合。缺失或未验证的 rank 需显式限制相关比较。增加 TP=2 缺 rank 及多 rank 测试。 |
| P1-3 | `opt_discovery/analyzer/compare.py:171`、`:190`，`opt_discovery/cli.py:949`、`:1351` | 两表改为不同窗口轮次，`compare_contracts` 判 incomparable，strict compare 仍返回 0、输出 total_ratio。分析内比较同样只调用 compare_tables。旧的独立 check-runs 命令不能替代比较入口的契约。 | 在比较入口应用既有采集契约。区分允许保留的描述性结构和需要受限的绝对量/比值。对轮次或已知输出长度不同的输入断言严格比较非零。 |
| P1-4 | `opt_discovery/analyzer/compare.py:92`、`:260`，`opt_discovery/cli.py:1358`、`:1391` | 实际 R05/R10 分析已判不可比、deltas=[]，JSON 仍给出 total_ratio=1.0，analyze 终端仍有比值输出。该字段可被自动化误当成跨 workload 的有效定量对比。 | 不可比时将派生比值置空，终端省略，分别保存两侧原始描述量。断言 JSON 和终端都不提供此比值。 |
| P1-5 | `opt_discovery/cli.py:1275`、`:1370`，`opt_discovery/report/summary_csv.py:62`、`:149`，`tests/test_run_contract.py:188` | 先生成两个 workload 的产物，再移除 eager 日志并加 `--skip-timeline` 复用目录。manifest 将 graph 的旧 timeline 列入 artifacts，新 CSV 仍有旧 eager 行，check-manifest 返回 0。哈希只验证收录内容一致。现有旧产物测试只放置未被应用识别的 stale.json。 | 以本轮成功写出的路径登记产物，并基于本轮文件集合生成汇总，或采用独立运行目录。回归应使用真实同名 timeline、comm 及本轮失败的旧 workload。 |
| P1-6 | `opt_discovery/cli.py:1124`、`:1188`、`:1235`、`:949`、`:1694` | 配置加载和多项必需执行位于本地失败记录之外。复用目录并指定不存在的映射，CLI 实际返回 1，磁盘仍保留上次 success 清单及原 run_id。诊断、产物写出和 compare 执行失败也缺少对应的完成处理。 | 为配置、执行和写入建立一致的失败结算，正常可写时落本轮 failed/partial 清单。明确安全的发布边界，防止失败后沿用旧成功状态。增加缺配置和写失败回归。 |
| P1-7 | `opt_discovery/schemas/quality.py:65`、`:84`，`opt_discovery/analyzer/compare.py:175` | 将实跑所得两个阻断标记的 severity 改为不合法值，strict compare 返回 0、comparable=true。无 severity 时还默认 degraded。旧表缺字段与当前表携带坏字段是不同情形。 | 保留历史表读取，严格比较遇到存在但无效的标记时应拒绝或判未验证并限制输出。对已知阻断码检查严重性，并增加 JSON 往返负向用例。 |
| P2-1 | `opt_discovery/cli.py:924`、`:1101`，`opt_discovery/experiment/run_manifest.py:221` | `compare --manifest` 写到输出目录以外时，命令返回 0，但同一清单 check-manifest 返回 1。写入端相对 out，校验端相对清单父目录。单独检查空 JSON 对象又返回 0，因为没有验证清单结构。 | 统一并明确 artifact 根目录，或在参数入口拒绝不可解析的路径组合。校验清单版本、字段、产物项与哈希完整性，区分完整性校验与运行成功。 |
| P2-2 | `opt_discovery/cli.py:899`、`:1020`、`:1152`、`:1164` | compare 只记录输入文件名，无法区分内容不同的同名表。analyze 指纹只覆盖部分规则/映射配置，缺少影响结果的 workload YAML 和显式 device profile 等。异常详情可将原始异常中的绝对路径带入清单。 | 记录可用的输入和实际生效配置指纹，保留逻辑标签和清理后的错误详情。测试同名不同内容与失败路径信息。 |
| P2-3 | `tests/regression/test_quality_gate.py:58`、`:90`、`:133`，`docs/engineering/regression-matrix.md:18`、`:23` | 原 xfail 已转为正式断言。R05 在测试里调用评估后手工 attach，R10 和 CLI 测试直接注入标记。移除 analyze 的标记生成或落盘连接，这些用例仍能通过。 | 将本次独立探针的双 rank 输入纳入自动回归，分别验证两个标记来源、落盘及独立读取的阻断。矩阵注明消费端与端到端的覆盖差异。 |
| P3-1 | `opt_discovery/analyzer/compare.py:69` | `quality_flags` 插在原 `generated_at` 之前。若外部调用者以位置参数传入时间戳，会绑定到新字段。仓库内的关键字调用通过。 | 将新增字段放在旧字段之后，或明确收窄并记录构造接口。 |

## 本次实际执行

| 命令 | 状态 | 结果 |
|---|---|---|
| `git status --short --branch`、`git log --oneline -12`、`git diff --stat e7d8aae HEAD`、`git diff --check e7d8aae HEAD` | PASS | 确认基线、HEAD、工作区和变更范围，diff 检查退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | PASS | Ruff、mypy（51 文件）、validate 通过，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae03-demo.Uq8c9e` | PASS / SKIPPED | 222 passed、17 skipped、无 xfail，synthetic demo 完成，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` | PASS | 67 passed，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae03-review-probe.1HRMdn.py` | PASS / FAIL（按场景） | 调用实际 CLI 子进程。双 rank R05/R10 路径返回 4，但仍带比值。缺 rank、缺日志、窗口契约、旧产物、配置失败后的旧清单及自定义清单复现见上表。 |
| 用临时 JSON 修改实际分析表的 quality_flags，再运行 `python3 -m opt_discovery.cli compare --strict` | FAIL（负向验收） | 非法 severity 被忽略，退出 0，comparable=true。 |
| 真实 Profiling 数据、NPU、托管 CI、其他 Python 版本 | SKIPPED / NOT RUN | 本轮验证为本机 CPU 和临时采集形态样例。真实测试全量执行时跳过，未独立验证真实数据或 NPU 结果。 |

AE-04 正式验收或合并前，应先修复 P1 项并将已复现的场景纳入回归，特别是从真实格式输入到产物再到独立 compare 的传播、复用目录、异常结算和严格比较契约。审查仅新增本文。
