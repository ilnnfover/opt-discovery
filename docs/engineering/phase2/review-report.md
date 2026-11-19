# AE-03 独立验收 Review

> 最新结论见文末「最终增量复核」。`7a78498` 的验收结果为 **FAIL**，PR #2 暂缓合并。

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
