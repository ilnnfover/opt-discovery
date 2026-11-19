# AE-03 独立验收 Review

> 最新结论见文末「本地使用范围验收」。`d8a4577` 为 **PASS WITH CONDITIONS**，可合并 PR #2 并开始 AE-04。

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

## 修复后独立复审（2026-11-19）

**结论：FAIL。** 双 rank 的标记传播、轮次契约、不可比比值清理已通过复验。仍可复现质量标记降级绕过、timeline 失败后继续比较、旧通信数据进入本轮汇总，以及发现或比较失败后保留旧运行清单。**可以准备 AE-04，暂不以 AE-03 已通过为依据进入 AE-04 正式验收或合并。**

复审提交为 `7d10937a8c95d93195146ee0707f1869f88dcfa7`，对照原审查提交 `ea6cd83` 的 14 文件差异和实施报告 §10。AE-03 整体基线仍为 `e7d8aae`。复审开始时仅有未跟踪的任务书。以下行号以本次 HEAD 为准。

| 上轮编号 | 复审状态 | 核查结果 |
|---|---|---|
| P1-1 | 已关闭原假成功反例 | 缺导出、缺日志、默认 timeline 缺 DB 的新增回归通过。显式跳过 timeline 的状态仍有冲突，见 F2-3。 |
| P1-2 | 部分关闭 | 缺少预期 rank、第三 rank 异常、部分 rank 缺 DB 的回归通过。当前验证使用 rank 数量。输入存在但 timeline 执行失败时，下游仍可把表判为可比，见 F1-2。 |
| P1-3 | 已关闭原轮次绕过 | 实际 CLI 读入轮次不一致的两表后返回 4，两个比较入口都调用采集契约。 |
| P1-4 | 已关闭 | 阻断报告保留两侧原始量，total_ratio 为 null、deltas 为空，终端对不可比输出 `total_ratio=-`。 |
| P1-5 | 部分关闭 | 旧 workload 整行与旧 timeline 不再登记为本轮产物。仍会读取同一 workload 的旧 comm.json，见 F1-3。 |
| P1-6 | 部分关闭 | 缺映射会替换旧清单，分析端单 workload 写失败有记录。发现异常和独立 compare 执行失败仍可留下旧清单，见 F1-4。 |
| P1-7 | 部分关闭 | 非法 severity 和缺失 severity 的已知阻断码会阻断。使用合法值 degraded 可绕过，见 F1-1。 |
| P2-1 | 部分关闭 | analyze 的自定义路径和空 JSON 回归通过。独立 compare 的相对基准仍错，缺哈希仍可通过，见 F2-1。 |
| P2-2 | 部分关闭 | compare 输入 SHA-256 和 analyze 的配置指纹已补充。compare 读入错误仍把原始路径写入清单，见 F2-2。 |
| P2-3 | 已关闭 | 新测试使用实际 CSV、轮次日志和 SQLite，调用生产 CLI，重读落盘表后断言 compare 退出 4。隔离移除任一标记挂载，两次均在对应落盘标记断言处失败。原两个 strict xfail 已移除。 |
| P3-1 | 已关闭 | 新字段移到 generated_at 之后，保留原先位置参数顺序。 |

### 剩余问题

未发现 P0。下列问题以运行契约和原始任务书的验收要求为准。

| 编号 | 位置 | 证据与影响 | 修复建议 |
|---|---|---|---|
| F1-1（P1） | `opt_discovery/schemas/quality.py:80`、`:95` | 将真实形态样例落盘表中两个已知 R05/R10 标记的 severity 改为 `degraded`，strict compare 返回 0 且 comparable=true。已知阻断码只在 severity 缺失时得到保护。 | 由已知码的契约决定最低严重性，或将 code/severity 冲突判为无效并阻断。用相同落盘输入验证 degraded 和非法值。 |
| F1-2（P1） | `opt_discovery/cli.py:1298`、`:1304`、`:1452` | 两张具有完整 CSV rank 的表，损坏 eager 主 DB 后，strict analyze 返回 3，但清单 comparison_status=success，表无质量标记，独立 strict compare 返回 0。R10 检查失败只留在 analyze 清单，消费侧无法判断时长质量未验证。 | 将必需 timeline 的失败或缺失作为可序列化的质量状态，并按受影响的比较范围限制派生结论。用损坏 DB 测试本次内置比较和独立重读。 |
| F1-3（P1） | `opt_discovery/cli.py:1477`、`opt_discovery/report/summary_csv.py:71`、`:163` | 对同一 workload 先采一条 4 MiB 通信记录，再删除 DB 并显式跳过 timeline。第二次 strict analyze 返回 0，新 workload_summary.csv 仍报 comm_mib=4.0、comm_tasks=1，本轮清单未登记 comm.json，校验却通过。目录级 include 只解决了跨 workload 复用。 | 汇总按本轮产物清单取文件，或读本轮对象。覆盖同一 workload 的可选产物缺失和读取失败。 |
| F1-4（P1） | `opt_discovery/cli.py:962`、`:1127`、`:1146`、`:1542` | 已有导出目录失去可解析 CSV 时，discover_all 抛错，analyze 返回 1 且清单 run_id 沿用旧运行。独立 compare 的产物路径换成目录后，返回 1 并打印 traceback，旧 success 清单未更新。缺映射虽已写新 failed 清单，parse 仍误记 passed。 | 将发现、配置、比较和写出纳入相同结算。阶段按实际进入和完成情况记录；保证仍可写时落本轮失败状态。测试用实际文件系统故障覆盖两个 CLI 入口。 |
| F2-1（P2） | `opt_discovery/cli.py:937`、`opt_discovery/experiment/run_manifest.py:246` | 独立 compare 的清单仍按 out_dir 记录相对路径。实测 `--manifest` 指到另一个目录时 compare 返回 0，check-manifest 返回 1。有效顶层结构的 artifact 删除 sha256 后，校验返回 0。 | 两个写入路径统一使用清单父目录。校验产物项是对象且哈希必填，并拒绝无法核实的条目。 |
| F2-2（P2） | `opt_discovery/cli.py:951`、`:956` | compare 的 load_error 直接存储异常字符串，缺输入文件时可把绝对路径写入清单，与 README 承诺不一致。 | 共享两个入口的路径清理逻辑，清单保留逻辑标签与错误码。 |
| F2-3（P2） | `opt_discovery/cli.py:1231`、`:1485`、`:1555` | TP=2 的 CSV 与日志完整，显式 `--skip-timeline` 且未提供 DB 时，timeline 标为 required=false、not_requested，运行却因 rank_timeline_incomplete 返回 3。阶段契约与退出条件冲突。 | 明确选择跳过时哪些证据仍为比较所必需。仅为实际要求的 timeline 检查结算失败，或以明确状态限制需 timeline 的结论。 |

### 实际复验

| 检查 | 结果 |
|---|---|
| `git diff --check ea6cd83 HEAD` | PASS，无空白错误。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | PASS，退出 0，Ruff、mypy（51 文件）、配置验证通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae03-followup-demo.cEJX56` | PASS / SKIPPED，退出 0，236 passed、17 skipped、无 xfail，synthetic demo 完成。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` | PASS，81 passed，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 env -u OPT_DISCOVERY_DATA_ROOT python3 scripts/verify_real.py --expect-blocked` | PASS，缺数据闸门返回 0，真实回归状态为 BLOCKED。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae03-followup-probe.fPfjWg.py`，另用临时样例损坏主 DB 及删除两 rank 的 DB | 双 rank 正常传播、轮次不一致及非法 severity 按预期受限。剩余反例的实际结果见 F1/F2。 |
| 隔离 `git archive HEAD` 副本，各自移除 R05、R10 的 attach_quality，再分别执行 `python3 -m pytest -q -o addopts= -p no:cacheprovider tests/regression/test_quality_end_to_end.py` | 两次均退出 1，各自在对应落盘标记断言失败。仓库工作树未变异。 |
| 真实 Profiling 数据、NPU、托管 CI、其他 Python 版本 | BLOCKED / NOT RUN。本次使用 Python 3.12.13。实施报告中的真实数据和其他解释器结果未作为本次实测。 |

进入 AE-04 正式验收或合并前，应关闭 F1 项并将对应反例纳入回归。原表中已关闭的首轮反例无需重复修复。

## 最终增量复核（2026-11-19）

**结论为 FAIL，PR #2 暂缓合并。** 原先的降级绕过、时间线失败未传播、旧通信记录混入以及原先复现的清单失败场景均已通过复验。输出目录创建失败仍能留下可通过校验的旧成功清单，清单与报告路径重合时 strict 还会在覆盖报告后返回 0。两项均影响 AE-03 的运行与产物契约。

审查范围为 `feat/ae-03-run-contract` 上的 `7d10937a8c95d93195146ee0707f1869f88dcfa7..7a78498322598ad90ac1c4e348258914ea9f56eb`，对照实施报告 §11。复核开始时 HEAD 与本地跟踪引用一致，仅有未跟踪的 `docs/tasks/`。

| 上轮问题 | 判定 | 复核证据 |
|---|---|---|
| F1-1 已知阻断标记被降级 | **关闭** | 将实际分析生成的 R05/R10 标记改为 `degraded`，另起进程执行 strict compare，退出 4、comparable=false。新回归通过 JSON 往返进入真实 CLI，覆盖 code 与 severity 冲突。 |
| F1-2 timeline 失败传播 | **关闭** | 损坏双 rank 样例的主 DB，strict analyze 返回 3，清单 comparison_status=incomparable，表随盘带 `timeline_unavailable`。删除单个 rank 的 DB 得到 `rank_timeline_incomplete`。两次均能让独立 compare 返回 4，且比值为空。 |
| F1-3 同一 workload 复用旧通信产物 | **关闭** | 首次用 SQLite 产出 4 MiB、1 任务，第二次移除 DB 并显式跳过 timeline。新 CSV 的 `comm_mib`、`comm_tasks` 两列均为空，旧 comm.json 未被登记，清单完整性校验通过。新回归的字符串断言能命中原反例，独立探针用 CSV 列名验证。 |
| F1-4 失败结算 | **部分关闭** | 原反例均通过。导出发现失败、独立 compare 报告写入失败均生成新 failed 清单，写入失败不再打印 traceback。缺映射退出 1 并生成新清单，parse 和 timeline 均为 not_requested。输出目录创建失败仍未结算，见下方 P1。 |
| F2-1 自定义清单路径与缺哈希 | **原反例关闭** | 独立 compare 把清单写在另一目录后，compare 与 check-manifest 均返回 0。删除 sha256 后校验返回 1。自定义清单与产物路径重合还有独立的 P1 缺口。 |
| F2-2 compare 输入失败路径脱敏 | **关闭** | 新回归用目录充当输入，另起进程也用绝对路径指向不存在的表。两种输入失败均返回 1，load_error 清单详情含 `<path>`，无临时根路径。 |
| F2-3 显式跳过 timeline | **部分关闭（P2）** | TP=2 的两个 workload 均移除 DB 并加 `--skip-timeline` 后，analyze 退出 0，timeline 为 required=false、not_requested，表上仅有降级提示，独立 compare 也退出 0。逐 workload 仍全被记为 partial，见下方。 |

### 剩余问题

| 级别 | 代码位置 | 复现、影响及修复建议 |
|---|---|---|
| P1（F1-4 仍开放） | `opt_discovery/cli.py:891`、`:1039` | 先生成可通过校验的外置 `--manifest`，再保留该路径、把本次 `--out-dir` 或 `--out` 指向已有普通文件。两个真实 CLI 均返回 1，清单字节保持原样、状态仍为 success，check-manifest 仍返回 0。mkdir 的失败分支在解析清单路径和运行 ID 之前就返回。应先确定本次清单目标，对可写的外置清单结算新 run_id、failed 和输出错误码。 |
| P1（产物路径冲突） | `opt_discovery/cli.py:977`、`:943`，`opt_discovery/experiment/run_manifest.py:208` | 将 `compare --manifest` 指向本次 `compare_<a>_vs_<b>.json`。strict compare 返回 0、清单写 success，却把比较报告覆盖，随后 check-manifest 返回 1。应在首次写入前校验目标路径，拒绝清单与产物或输入重合。增加同路径的 CLI 反例。 |
| P2（F2-3 状态不一致） | `opt_discovery/cli.py:1276`、`:1448` | 显式跳过 timeline 生成 `timeline_skipped` 提示，逐 workload 状态逻辑把任意 quality code 当成 partial。实际两组都完成了选定的必需阶段，run status 为 success，两个 workload status 反而均为 partial。应分别根据必需步骤完成状态和质量限制填写状态字段，并对照总状态与逐项状态测试。 |

未发现 P0。两项 P1 的底层缺口在 `7d10937` 已存在，这次修复仍未覆盖，未发现由 `7a78498` 新引入的 P0/P1。上述结论仅评价本地指定提交及受影响入口。

### 实际执行

| 命令或探针 | 结果 |
|---|---|
| `git diff --check 7a78498^ 7a78498` | PASS，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | PASS，退出 0，Ruff、mypy（51 文件）、配置验证通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae03-final-demo.dJxXt4` | 退出 0，243 passed、17 skipped、无 xfail，合成演示通过。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` | PASS，88 passed，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae03-followup-probe.fPfjWg.py` 与 `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae03-final-probe.PRg6Vv.py` | 在临时目录调用真实 CLI 子进程，实测上表的退出码、落盘状态、CSV 字段及清单校验结果。 |
| 隔离 `git archive HEAD` 副本故障注入 | 分别移除已知码降级限制、timeline 失败时的表标记、汇总的本次产物过滤。各自运行对应新增测试，三次均以断言失败退出 1。实际工作树未变更生产代码。 |
| `PYTHONDONTWRITEBYTECODE=1 env -u OPT_DISCOVERY_DATA_ROOT python3 scripts/verify_real.py --expect-blocked` | 缺数据闸门通过，真实回归为 BLOCKED。NPU、托管 CI 与其他 Python 版本未在本轮运行。本机为 Python 3.12.13。 |

PR #2 的验收条件是关闭上述两项 P1，并以独立 CLI 复验确认输出目录初始化失败会结算可写清单，以及路径冲突无法发布 success。

## 合并前复验（2026-11-19）

**结论为 FAIL。PR #2 暂缓合并，AE-04 可先准备，正式启动应以通过验收的 AE-03 为基线。** 本次只复核 `7a78498..2589cc71bd361ebb7addc32ddde5455043c9f93b`，对照实施报告 §12。本地 HEAD 与跟踪引用一致。

| 上轮问题 | 复验结果 |
|---|---|
| 无法创建输出目录时旧成功清单保留 | 原反例通过。输出路径为已有普通文件且外置清单可写时，analyze 和 compare 均退出 1、发布新 run_id 和 failed 清单。与输入路径冲突时仍可覆盖输入，见下表。 |
| compare 清单覆盖比较报告 | 原反例通过。比较报告与清单同路径时退出 1，冲突路径未写入，安全位置的清单记为 failed。analyze 采用回退路径时仍会生成无法通过校验的 success 清单，见下表。 |
| 显式跳过 timeline 时逐 workload 误记 partial | 关闭。用两个完整 TP=2 CSV 工作负载、均无 DB 的场景复验。显式跳过时退出 0，总体与各 workload 均为 success，timeline 标为 not_requested。 |

| 级别 | 位置 | 复现及影响 | 修复建议 |
|---|---|---|---|
| P1 | `opt_discovery/cli.py:909`、`:940`、`:998`、`:1014` | 用有效算子表作 `--a`，并将 `--manifest` 指向同一文件。若 `--b` 不存在，或 `--out-dir` 是普通文件，compare 均退出 1，却把有效表覆盖为 failed 清单。冲突保护在读表和创建输出目录之后才执行。读表失败的旁路仍在，输出目录失败时覆盖输入是此次新增结算路径带来的回退。 | 在所有失败路径写清单之前检查输入路径冲突，选择经过校验的备用路径或保留输入并返回错误。测试需断言两类失败下原始输入字节不变。 |
| P1 | `opt_discovery/cli.py:1225`、`:1236` | 将 analyze 的 `--manifest` 指向本轮某个 workload 子目录的 `operator_table.json`。算子表得以保留，命令退出 0，回退清单记 success，但校验退出 1。产物相对路径按原清单父目录计算，实际清单写在输出根目录。 | 写入前拒绝冲突，或以最终清单所在目录生成产物路径，保证 success 时清单可校验。增加嵌套产物冲突用例。 |

`make check` 退出 0。`make verify DEMO_OUT=/tmp/ae03-merge-demo.Z6dQtT` 退出 0，246 passed、17 skipped，合成演示通过。`python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` 得 91 passed。另在临时目录通过实际 CLI 子进程复验，命令为 `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae03-merge-probe.LpHFRT.py`，并用有效输入表再次确认两类失败路径会覆盖原文件。真实 Profiling 回归在本次全量测试中跳过，NPU 未运行。

## 输入文件保护复验（2026-11-19）

**原两项 P1 已关闭。AE-03 的合并验收仍为 FAIL，PR #2 暂缓合并。** 本次仅审查 `2589cc7..e6c0770399bbca267c79c029a47e42831b28b23a`，对照实施报告 §13。本地 HEAD 与跟踪引用一致。以下复现均使用临时文件。

- **compare 失败时的直接路径冲突通过复验。** `--manifest` 和有效的 `--a` 同路径，分别触发对照表缺失和输出目录创建失败。两次均退出 1，原表字节不变，备用清单为 failed，校验退出 0。
- **analyze 清单与嵌套产物冲突通过复验。** 指向 workload 的 operator_table.json 时退出 1，算子表保留，输出目录的备用清单为 failed，校验退出 0。

| 级别 | 位置 | 复现、影响和修复建议 |
|---|---|---|
| P1 | `opt_discovery/experiment/run_manifest.py:208`、`opt_discovery/cli.py:910` | 用有效算子表作为 `--a`，文件名为 `compare-run.json.tmp`，并把清单指定为同目录的 `compare-run.json`。compare 退出 0、发布 success，原输入表消失。清单写入会截断固定的 `.tmp` 文件，再把它移动到目标路径，最终路径冲突检查无法保护该输入。应使用同目录独占创建的唯一临时文件原子替换，并验证有效输入字节保持不变。 |
| P1 | `opt_discovery/cli.py:1152`、`:1272`、`:1289` | `analyze --manifest <workloads-dir>/prefill_eager.yaml` 会正常读取临时 workload YAML，最后用 success 清单覆盖该 YAML，并退出 0。analyze 的检查只覆盖本轮产物。应在写入前保护已知输入路径，失败结算也应走同一校验，回归测试应断言 YAML 原始字节保留。 |

这两处问题均在实施报告所述的「清单不得与输入或本轮产物重合」范围内。它们来自既有的清单写入和 analyze 路径，当前修复未覆盖。

`make check` 通过。`make verify DEMO_OUT=/tmp/ae03-recheck-demo.LyMXUq` 退出 0，249 passed、17 skipped，合成演示通过。`python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` 得 94 passed。使用 `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae03-input-safety.wCRttt.py` 执行独立 CLI 复验，检查输入字节和清单状态。真实 Profiling 回归在本次全量测试中跳过，NPU 未运行。

## AE-03 合并前问题总表（2026-11-19）

**结论为 FAIL，暂缓合并 PR #2。** 下表汇总了 `e6c0770399bbca267c79c029a47e42831b28b23a` 的未关闭问题。以 AE-03 基线 `e7d8aae`、原始任务书和实施报告 §9～13 为依据，检查本阶段改动和直接下游入口 `analyze`、`compare`、`check-runs`、`check-ranks`、`check-manifest`、`report`、`summary`。部分问题先于最新修复存在，在当前验收范围以当前行为计。本轮未发现 P0，真实 NPU 与文件系统并发场景未实测。

### P1 合并阻断项

| 编号 | 位置 | 实测结果和影响 | 成组修复建议 |
|---|---|---|---|
| A1 输入可被输出覆盖或移除 | `opt_discovery/experiment/run_manifest.py:208`、`opt_discovery/cli.py:910`、`:1030`、`:1152`、`:1233`、`:1531`，`opt_discovery/analyzer/compare.py:110` | 四种临时文件实测均退出 0。① `compare-run.json.tmp` 是有效输入，清单目标是 `compare-run.json`，固定临时文件截断并移动输入，导致原输入消失。② `analyze --manifest` 指向实际读取的 workload YAML，该配置被成功清单覆盖。③ 现有比较报告路径是算子输入的硬链接，路径不同所以通过 `resolve()` 检查，报告写入截断输入。④ 复用目录时 `graph_coverage.json` 若为指向 workload YAML 的符号链接，analyze 覆盖 YAML，随后清单校验仍通过。 | 把输入保护、产物写入和清单发布作为一个契约。对已选择的输入及目标预检，包括实际配置、日志、CSV、DB、默认/自定义/备用清单和全部产物。考虑解析后的路径、现有文件身份和链接。原子写入使用同目录独占创建的唯一临时文件。失败和成功分支都测字节保留。 |
| A2 路径拒绝后旧成功清单仍有效 | `opt_discovery/cli.py:1030` | 先在正常输出目录完成 compare 并生成可校验的外置清单。下一次沿用该清单，将有效输入表放到本次预定比较报告路径。compare 拒绝写报告并退出 1，旧清单原样保留、校验仍为 0。 | 冲突拒绝也走统一失败结算，在安全目标可写时发布本轮 failed 和新 run_id。与 A1 一起验证失败发布无副作用。 |
| A3 空窗口与缺轮次发布为成功 | `opt_discovery/parsers/csv_mapping.py:447`、`:481`，`opt_discovery/cli.py:1384`、`:1710`，`opt_discovery/analyzer/compare.py:183` | 实际解析只有 CSV 表头的单 rank 导出，strict analyze 返回 0，记录数为 0。仅移除选定第 2 轮的任务行，生产解析器生成 `missing_rounds=[2]`，strict analyze 仍返回 0，比较可比并输出 `total_ratio=1.8`。独立 compare 的一侧 records 为空时也返回 0 并生成 deltas。 | 定义空窗口、缺轮次、低覆盖各自的质量状态。生产者落盘所需证据，两个比较入口据此限制绝对时间、调用量及比值。空数据可以出审计，但不能作为成功比较输入。 |
| A4 比较元数据未验证或不合法仍能出结论 | `opt_discovery/schemas/quality.py:114`，`opt_discovery/analyzer/compare.py:187`、`:230`，`opt_discovery/schemas/workload.py:255`，`README.md:470` | 对有效落盘表分别设置 `quality_flags={}`、`verified=false`、`A.is_synthetic=true / B.is_synthetic=false`。三次 strict compare 均退出 0、有 deltas 和比值，后一情形甚至没有合成数据警告。空对象被 `or ()` 当成缺字段，`verified=false` 与 README 承诺不符。混用合成与实测时只对一个方向提示，提示也不限制结论。 | 区分缺字段、空列表与格式错误。让 verified 状态与文档契约一致。两种混合来源方向用同一规则，限制真实定量结论。显式定义旧表无新标记的兼容策略。 |
| A5 rank 完整性和比较口径可被放宽 | `opt_discovery/cli.py:787`、`:847`，`opt_discovery/analyzer/rank_quality.py:178`、`:199`、`:313` | 工作负载声明 4 rank、实际交付 2 个，`check-ranks --strict` 返回 0 并宣告可合并，analyze 却返回 3。直接调用生产 rank 评估，两份窗口表的 calls 和 total 相差 4 倍、单次耗时一致，正常判不可合并。加入一份来源为 full、单次耗时一致的第三表后，整体变为 consistent、只带 degraded 提示。全局 scope 让原来同口径的两个 peer 走了弱校验。 | 在 check-ranks/analyze 共享 rank 完整性判定，按 pair 区分相同和不同 scope。验证预期数量、身份、重复、额外和缺失 rank，以及一份 SQLite 回填时其他窗口 rank 仍受原约束。 |
| A6 下游 summary/report 复用旧产物 | `opt_discovery/cli.py:871`、`:880`，`opt_discovery/report/summary_csv.py:162`，`opt_discovery/report/generator.py:48` | 首轮用实际 SQLite 生成 4 MiB/1 任务，次轮移除 DB 并显式跳过 timeline。次轮 analyze 的 CSV 正确留空，随后独立 summary 又写回 4.0 MiB 并返回 0，当前清单的 CSV 哈希被破坏。独立 report 同样将旧通信量显示为本轮结果，且还会扫描旧 timeline 和旧 compare。 | 所有默认下游读入口共用当前运行清单的产物集合。重建 CSV 应选择独立目标或同步更新产物清单。定义旧目录无清单时的兼容行为。 |

### P2 应一并处置的契约缺口

| 编号 | 位置 | 证据和处置建议 |
|---|---|---|
| B1 清单验证边界不完整 | `opt_discovery/experiment/run_manifest.py:223`、`:250`，`opt_discovery/cli.py:897`、`:1125` | **实测** 有版本、ID、success 以及空 artifacts，但没有 stages 等字段的最简对象通过；artifact.path 为数字时校验抛 traceback；已有清单是 JSON 数组时 compare 在读 `.get()` 处中断。对各字段做类型和必需性校验，对 success 再检查对应命令必需的产物。明确 check-manifest 的退出 0 只表示完整性时，应让自动化另读 status。 |
| B2 清单阶段状态和失败原因不一致 | `opt_discovery/cli.py:977`、`:1063`、`:1326`、`:1753`、`:1835` | **实测** 无导出且要求 source-map 时，发现失败而 source_map 被记 passed；compare 报告写入失败时 compare stage 为 failed、顶层却是 not_requested；缺映射虽总体 failed，但没有对应阶段错误码。**代码审查** 多对比较部分失败时仍可能保留另一对的 success。用阶段实际进入/结果派生状态，比较保留每对结果。 |
| B3 可选步骤失败只在终端出现 | `opt_discovery/cli.py:1454`、`:1466` | **实测** comm 表缺字段使通信计算失败，strict analyze 仍成功，终端提示错误，清单没有该步骤的状态或理由。可选步骤可以不影响退出码，仍需记录 unavailable/failed，避免消费者把缺数据当作未请求或零值。 |
| B4 重复 workload ID 会覆盖和自比 | `opt_discovery/cli.py:1298`、`:1397`、`:1531`、`:1880` | **实测** 目录含两份相同 workload ID 的 YAML 时 strict 成功，清单有两个同 ID 条目，产物只剩一个目录并生成这个 ID 与自身的比较。同 ID 不同来源时也共用键与写入路径。应在写出前验证 ID 唯一，或明确支持并赋予各输入独立 ID。 |
| B5 同一比较契约在两个入口获得的证据不同 | `opt_discovery/analyzer/compare.py:204`，`opt_discovery/analyzer/contract.py:216`、`:254`，`opt_discovery/cli.py:499` | **生产函数实测** 完整表的合同检查出现 `operator_set`、`work.count` warnings。compare 仅传 workload 和 meta，合同检查读到 0 条 records，比较报告丢失相应证据和警告。check-runs 只查采集契约，不读取 compare 附加的阻断标记。应共享契约所需的摘要及警告结果，文档注明哪些闸门由哪个入口执行。 |
| B6 默认跨阶段配对的 strict 语义待定 | `opt_discovery/cli.py:1723`、`:1880`，`opt_discovery/analyzer/contract.py:120` | **实测** 四组完整样例的两对同阶段比较可比，两对自动生成的跨阶段比较不可比，导致整体 strict 返回 4。跨阶段拒绝绝对量本身符合契约。需明确此类自动配对是描述性还是 strict 必需，补默认四组组合验收测试。 |
| B7 指纹与清单结算还有未保护区间 | `opt_discovery/cli.py:961`、`:994`、`:1215`、`:1283`，`opt_discovery/experiment/run_manifest.py:198`、`:292` | **代码审查** 输入哈希位于 compare 读表异常处理之外，配置和产物哈希也在写清单异常处理之外。文件在检查和读取间消失或不可读时可能绕过失败清单；产物消失时 `collect_artifacts` 可静默跳过。哈希与实际消费是独立读取，先后修改时不一定对应。应覆盖实际读取、记录和发布，明确可选/必需文件的结算。 |
| B8 同目录同时运行及中断的代际隔离 | `opt_discovery/cli.py:1531`、`:1677`、`:1700`，`opt_discovery/experiment/run_manifest.py:208` | **代码审查、未并发实测** 两个运行用相同产物路径并覆写，独立 run_id 只在清单，无法隔离产物。固定临时清单路径也会冲突。进程在产物写出后、清单发布前中断可能形成混合产物。定义同目标并发是拒绝、串行或独立运行目录，再用锁或分目录发布守住该约定。 |

### 一次性修复与复验范围

| 工作组 | 复验场景 |
|---|---|
| 输出写入和失败发布（A1、A2、B1、B2、B7、B8） | 对真实 CLI 同时覆盖普通/外置/备用清单、绝对/相对路径、输入与报告、配置及 CSV/DB 与产物、已有临时同名文件、符号链接/硬链接、不可写/创建失败/读失败/写失败、没有安全清单目标、已有成功清单、清单形状错误和同目录重复运行。断言判定、run_id、输入字节和全部必需产物哈希。 |
| 统一采集质量与比较范围（A3、A4、A5、B5、B6） | 用实际导出形态和独立 JSON 重读覆盖零行、缺轮次、低覆盖、未验证与畸形标记、合成/真实双向、完整/缺失/混合口径 rank、timeline 跳过/失败以及默认四组配对。断言失败条件与禁出的派生指标，并保留有效正例。 |
| 下游结果与运行标识（A6、B3、B4） | 首轮有数据、次轮部分失败或缺可选产物，随后独立 summary/report。断言无旧行/旧结论、清单校验保持一致、可选失败明确可查，重复 ID 不发布无歧义的 success。 |

此前已关闭的原反例保留回归即可，包括 R05/R10 从实际导出到独立 compare、已知阻断码降级、必需 timeline 的已测失败路径、轮次契约、分析内禁止旧 comm、清单与比较报告/嵌套产物的直接冲突、自定义清单路径和缺哈希。当前合并结论取决于 A1～A6，B 项明确供同一批修复和验收使用。后续复核以此表为边界，检查此批修复和它们直接改变的路径。

### 本次验证记录

- `PYTHONDONTWRITEBYTECODE=1 make check` 退出 0。Ruff、mypy 和配置验证通过。
- `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae03-inventory-demo.897ju6` 退出 0。249 passed、17 skipped，合成演示通过。
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` 退出 0，94 passed。
- 独立 CLI 探针在临时目录执行，脚本为 `/tmp/ae03-inventory.6nleUg.py`、`/tmp/ae03-inventory-more.swi40d.py`、`/tmp/ae03-inventory-boundaries.qEMLGs.py`。固定临时路径与覆盖 YAML 复用上一轮 `/tmp/ae03-input-safety.wCRttt.py` 的实测。同口径 rank 与采集合同证据使用生产函数的独立 Python 探针。
- 本轮真实 Profiling 测试跳过。NPU、托管 CI、其他解释器和并发写入未在本轮验证。

## A1～A6 整批复验（2026-11-19）

**AE-03 合并验收为 FAIL，PR #2 暂缓合并。** 复验 HEAD `120681897954fef933340f9af622d095eb1b9814`，对照 `e6c0770` 后三个修复提交和实施报告 §14～16。A1 和 A6 各有一组可独立复现的剩余条件。原任务书 §3 将 AE-02 列为 AE-04 的直接依赖，并允许不冲突的并行工作。可以开始 AE-04 的独立设计和测试，生产重构优先等 AE-03 收口。`1206818` 仍不适合作为已验收的 AE-03 基线。

| 项目 | 判定与证据 |
|---|---|
| A1 文件保护 | **部分关闭。** 最新生产路径回归覆盖固定临时文件名、配置冲突、报告硬链接和产物符号链接并通过。采集输入在发现完成后才登记，提前失败路径仍有缺口。 |
| A2 失败清单 | **原反例通过。** 报告目标与输入重合时保护输入并生成新 failed 清单，回归断言旧 run_id 被替换。 |
| A3 空窗口、缺轮次、低覆盖 | **专项回归通过。** 生产 CLI 构造实际 CSV 并检查 blocking 标记、strict 非零，独立空表比较阻断。 |
| A4 质量元数据 | **专项回归通过。** 空对象标记、未验证 workload、合成和真实混用均触发阻断。 |
| A5 rank 约束 | **专项回归通过。** 声明 rank 数参与 check-ranks strict 判定，混合口径下同口径 pair 仍受原阈值校验。 |
| A6 下游产物读取 | **部分关闭。** 默认有效清单下，独立 CLI 复验确认旧 4 MiB 通信量没有回到 CSV 或 Markdown，校验仍通过。外置、旧默认及损坏默认清单组合仍失败。 |

| 级别 | 编号与位置 | 独立 CLI 实测和影响 | 修复建议 |
|---|---|---|---|
| P1 | A1，`opt_discovery/cli.py:1360`、`:1472`、`:1513` | 在临时导出上加入重复 workload ID，并用 `--manifest` 指向该 workload 原始 op_summary CSV。CLI 如期退出 1，却把 CSV 覆盖成 failed 清单。重复 ID 经 `_save` 发布失败时，采集输入尚未登记保护。 | 让所有早期失败都在输入保护范围内选清单目标，保护原始导出，即使配置或发现不能完成。测试在重复 ID、坏配置、发现失败时断言 CSV/DB/日志字节保留。 |
| P1 | A6，`opt_discovery/cli.py:890`、`:907` | 首轮产生真实形态的 4 MiB 通信记录，次轮移除 DB 并显式跳过 timeline。次轮 CSV 空白且清单校验通过。用外置 `--manifest` 后，独立 `summary` 和 `report` 均退出 0、恢复旧 4 MiB，最新外置清单随后校验失败。有旧默认清单时同样发生。将当前默认清单截断后，两个命令继续按旧目录扫描并返回 0。 | 两个下游入口都应解析明确选定的本轮清单，包括自定义名称和位置，统一相对路径并同步正确清单。已有但无效的清单应报错，历史无清单目录的扫描行为需明确区分。 |

B8 的同目录并发已在 README 明确为不支持，本轮在单进程运行约定下验收。没有以并发限制作为合并阻断。P1 结论来自上表两个顺序执行场景。

- `PYTHONDONTWRITEBYTECODE=1 make check` 通过。
- `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae03-close-demo.Q1XJlP` 退出 0，272 passed、17 skipped，合成演示通过。
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` 退出 0，117 passed。
- `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae03-close-probe.VXfLl4.py` 在临时目录独立运行标准 CLI，验证上述 A1、A6 的失败结果及默认清单的正例。
- 真实 Profiling 用例在本机跳过，NPU 未运行。

## 剩余 P1 复验（2026-11-19）

**PR #2 暂缓合并，AE-03 验收仍为 FAIL。** 本轮只复验 `1206818..cea80742bf9b8f584fa142516d9859e4eb6c55f1` 对 A1、A6 的修复。原任务书允许 AE-04 不冲突的设计和测试并行，建议共用生产模块的重构以通过验收的基线为起点。

| 项目 | 通过的场景 | 仍阻断的实测场景 |
|---|---|---|
| A1 输入保护 | 重复 workload ID 时，以原始 CSV 为 `--manifest`，退出 1 且原始字节保留。 | 一个有效 workload YAML 和一个语法损坏的 YAML 同时存在时，相同命令退出 1，却覆盖有效 workload 的原始 CSV。输出目录为普通文件时也复现。普通配置加载失败和 mkdir 失败分支的列表推导会因坏 YAML 丢掉可识别输入，随后仍写失败清单。应让所有失败结算都保护采集输入，或者在无法确立保护边界时禁用该目标。位置 `opt_discovery/cli.py:1364`、`:1514`。 |
| A6 下游清单选择 | 显式传入外置清单后，summary/report 均成功，旧 4 MiB 未回到 CSV 或 Markdown，清单校验通过。默认清单缺失、JSON 截断或顶层数组时，两命令非零退出。 | 首轮写默认清单、次轮复用目录并改写外置清单后，旧默认清单仍被接受。无参数的 summary/report 都返回 0、呈现旧 4 MiB，之后最新外置清单校验失败。应明确记录输出目录对应的当前清单，或使较旧、与目录内容不匹配的默认清单无法被静默采用。位置 `opt_discovery/cli.py:894`、`:900`。 |

`make check` 通过。`make verify DEMO_OUT=/tmp/ae03-accept-demo.pq6rXP` 退出 0，275 passed、17 skipped，合成演示通过。专项 `python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_run_contract.py tests/regression` 通过 120 项。独立 CLI 探针 `/tmp/ae03-last-replay.nXTZw4.py` 在临时目录验证输入字节、CSV、报告和清单完整性。真实 Profiling 数据用例在本机跳过，NPU 未运行。

## A1/A6 验收矩阵复验（2026-11-19）

**此前未通过的原反例均已通过。PR #2 仍暂缓合并，新增默认清单指针写入绕过了 A1 的输入保护。** 复验 `cea8074..d8a45773588da8ad5735fd2e1733fa0842b5954b`，对照实施报告 §18 和此前固定的 A1/A6 验收条件。

| 项目 | 复验结果 |
|---|---|
| A1 早期失败保护 | 生产 CLI 回归覆盖重复 ID、坏 YAML、发现失败、输出目录失败与 CSV/DB/日志的 12 个组合，均通过。独立进程复验还确认坏 YAML 与输出目录失败同时发生时原始 CSV 字节不变。 |
| A6 清单选择与产物隔离 | 独立进程复验显式外置清单、默认位置指针替换旧清单，以及无效、缺失清单。正常场景的 summary/report 没有读回旧 4 MiB，当前外置清单校验通过。无效或缺失清单时退出 1，当前 CSV 与有效清单保持一致。 |
| A1 新增指针写入 **P1** | `opt_discovery/cli.py:1099`、`:2266` 与 `opt_discovery/experiment/run_manifest.py:394`。在临时目录用有效算子表作为 `--a <out-dir>/run_manifest.json`，另指定安全的外置清单。正常比较返回 0，输入表却被替换为指针。让 `--b` 缺失后，命令返回 1，仍替换输入表。外置清单目标经过保护检查，新增的默认指针写入没有沿用输入保护。应对指针目标应用同一保护约束，覆盖成功和失败分支，并在两个场景都断言原输入字节不变。 |

`make check` 通过。`make verify DEMO_OUT=/tmp/ae03-matrix-demo.xH3Wp9` 退出 0，289 passed、17 skipped，合成演示通过。专项回归 134 passed。独立 CLI 探针在临时目录执行，两种指针覆盖情形均已复现。真实 Profiling 数据用例跳过，NPU 未运行。本轮合并阻断只剩新增指针写入对 A1 的回退，AE-04 可继续不冲突的独立设计与测试。

## 本地使用范围验收

**`d8a4577` 为 PASS WITH CONDITIONS，可合并 PR #2 并开始 AE-04。** 按用户确认的本地使用范围，以单进程运行和专用输出目录为验收条件。A1/A6 原始矩阵的复验已经通过，上一节所述指针覆盖情形保留为已知限制。使用时将输入表和输出目录分开，避免把输入表放在输出目录下的保留路径 `run_manifest.json`。

依据为同一提交上已执行的 `make check`、全量测试 289 passed / 17 skipped、专项回归 134 passed，以及 A1/A6 独立 CLI 复验。17 项真实 Profiling 用例在本机跳过，NPU 未运行。本节记录验收范围变更与合并建议。


## 最终验收结论（2026-11-19）

| 判定项 | 最终结论 |
|---|---|
| AE-03 验收 | **PASS WITH CONDITIONS**，适用于本地单进程运行，输入与输出分开存放。复验提交为 `d8a45773588da8ad5735fd2e1733fa0842b5954b`。 |
| PR #2 | **可以合并**。 |
| AE-04 | **允许进入**，可在合并后以 AE-03 的结果为基线开始开发。 |

A1/A6 原始验收矩阵通过。A1 的 12 个 CLI 回归组合覆盖重复 workload ID、无效 YAML、导出发现失败和输出目录创建失败，均断言原始 CSV、DB 和日志字节不变。A6 的独立 CLI 复验覆盖默认清单、外置清单、旧默认清单与无效清单。正常运行时旧 4 MiB 通信量未回到 CSV 或 Markdown，当前清单通过校验。无效清单时命令退出 1，现有 CSV 保持原样。对应实现及分轮证据见前文。

| 剩余问题 | 代码位置 | 实际证据与处置 |
|---|---|---|
| 默认清单指针可能覆盖放在保留路径上的输入表 | `opt_discovery/cli.py:1099`、`:2266`，`opt_discovery/experiment/run_manifest.py:397`、`:422` | 独立 CLI 复验将有效算子表放在 `<out-dir>/run_manifest.json` 并另指定外置清单。正常比较退出 0，对照表缺失时退出 1，两次输入均被指针替换。该路径属于专用输出目录的保留文件，按已确认的本地使用范围列为已知限制，合并可继续。 |

同一提交上的验证记录为 `make check` 通过，`make verify` 退出 0，289 passed、17 skipped，合成演示通过。运行契约与回归专项 134 passed。17 项真实 Profiling 用例在本机跳过，NPU 与同一输出目录的并发运行未验证。结论沿用上述实测记录。本节只记录验收与合并建议，无实际合并操作。
