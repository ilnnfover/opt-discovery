# AE-06 独立审查报告

**最终结论为 PASS WITH CONDITIONS。** 可由用户决定提交 PR。真实回归保留一项历史场景跳过。见[集中修复后增量复核](#集中修复后增量复核2026-11-20)。

## 首轮审查记录

**首轮结论为 FAIL。** 首轮候选包含可执行的属性与真实数据检查框架。三条新 CPU 用例、一条授权真实数据用例、采集绑定和部分真实关键断言尚未达到验收要求。指定模型额度当时阻塞了集中修复。

审查比较 `ba3e6b3` 与 `feat/ae-06-property-real-regression` 的工作区差异，包括未跟踪的新增测试和逻辑采集 fixture。CodeBuddy 使用 `deepseek-v4.1-flash`。Codex 在其调用前后核查 Git 状态，审核期间仅新增文档。四组本地 Profiling 数据为此前授权的统一补采，验证只读输入，演示与测试输出写入临时目录。

## 验收矩阵

| 项目 | 状态 | 证据 |
|---|---|---|
| Hypothesis 和确定性 CPU profile | 部分通过 | 两个 Python 版本可收集并执行。默认示例数为 30。三条用例失败。 |
| 并集、窗口与多 rank | 通过现有 CPU 范围 | 小整数集合验证并集与交集、重复输入和有效域占用，SQLite 生产路径验证多 rank。真实窗口用例读取了错误的元组结构。 |
| Parser 和 Metric | 失败 | 合法聚合行序、分片和缺列用例通过。坏行、实测零和派生平均值的测试预期或 fixture 有误。 |
| Diagnosis、Comparison、Evidence | 通过现有契约 | 缺必要证据与不可比样例、工作负载级缺归因、合成和实测混比的拦截通过。直接反馈 API 的可信来源约束按照范围留给 AE-07。 |
| 四组真实采集身份与来源 | 失败 | 基本身份和两个 rank 的正例通过。原 manifest 的固定参数被改动、或一个 rank 链接改指其他工作负载时，仍可通过对应核验。 |
| 真实关键指标的回归有效性 | 失败 | 图覆盖率用例接受把时间占比替换成任务占比的故障。独立真数据核对得到 Host 并集及通信字节量，但候选的真实回归尚未绑定这些可观察契约。 |
| `make check` 与合成流程 | 通过 | 静态与配置检查退出 0，单独执行 `make demo` 完成。`make verify` 整体失败。 |
| 真实入口状态 | 部分通过 | 未设置数据目录时 BLOCKED 2，带缺数据断言的 CI 命令通过。提供真实数据时真实执行并退出 1。 |

## 一次集中修复清单

无 P0。以下三组 P1 为同一轮阻断清单。

| 级别 | 代码位置 | 反例、影响与修复要求 |
|---|---|---|
| P1-1 | `tests/property/test_contracts.py:352`、`:380`、`:398`；`opt_discovery/parsers/csv_mapping.py:706`、`:732` | `make verify` 和 Python 3.10 专项均报三个失败。① 坏 duration 保留一个缺时间的 `MatMul` 组是基线行为。断言两个跳过、已记录告警、缺值组不贡献 calls 和时间、有效 `Add` 独立总量，保留现有 Parser 契约。② 实测零测试的记录 ID 为 `wl`，表 ID 为 `fixture-wl`。让测试 ID 一致后验证 0 和 None。③ `width=32` 无法精确表示边界 `0.01`，Hypothesis 拒绝生成。使用可表示边界、整数缩放或 64 位生成，并保留派生来源与实测来源的断言。 |
| P1-2 | `scripts/verify_real.py:97`、`:143`、`:155`；`tests/test_real_unified_manifest.py:63`、`:78` | 将读到内存的真实 manifest 中 `fixed.max_num_seqs` 从 32 改成 1、清空 `profiling.switches`，保留声明哈希，检查仍返回 `[]`。在临时根下把 prefill graph 的 rank 1 链接改指 prefill eager 的 rank 1，保留 graph 的原日志、manifest 和 alias 文件名，身份和 rank 专项仍为 8 passed。真数据可能被错配后当作同一采集。核对少量有意义的固定参数与仪表，比如 TP、批参数、精度及开关。将已发现 rank 的实际来源和 `checks.ranks` 记录绑定，支持授权根的 alias 布局。增加两个反例，维持缺文件 BLOCKED 与内容不符 FAIL 的区分。 |
| P1-3 | `tests/test_real_unified_manifest.py:101`、`:109`、`:130`，`scripts/verify_real.py:256` | 真数据 `verify_real` 实际为 1 failed、25 passed、1 skipped。直接 `item.start_us` 取值失败，因为 `round_spans` 为 `(round,start,end)` 元组。修正 fixture 断言，验证窗口来自实测日志。把 `GraphCoverage.time_ratio` 在进程内替换成 `task_ratio` 后，图覆盖用例仍 1 passed。用原始 Model ID、调用次数和独立时长作为两个 oracle。授权数据也实际呈现 Host 并集与逐条累加的差异，通信记录数与字节量差异见实施报告。将这些轻量真数据样本绑定到现有真实入口，或复用同等有效的已有断言，避免重复全量采集解析。 |

### 非阻断项

- P2。真实失败汇总把 `run.executed` 标成 `passed`。候选实际 25 通过时脚本显示 26 passed、1 failed。状态判定仍返回 1。可让失败、错误、跳过和通过数量互斥。代码位置 `scripts/verify_real.py:222`。
- P2。排列测试的两侧在调用生产 `IntervalUnion` 前都排序为相同序列。独立并集和 Timeline 粒度断言有价值。可把顺序不变性放在公共入口上，或明确该例只验证排序输入规范。代码位置 `tests/property/test_contracts.py:103`。
- P3。`test_feedback_payload_has_no_verified_source_boundary_yet` 固定将来可扩展的字段缺省。可以把缺口留在报告，并保留能够保护既有用户语义的合成混比和缺归因测试。代码位置 `tests/property/test_contracts.py:678`。

## 验证与后续

完整命令、基线、候选测试数量和独立原始数据计算记录见[实施报告](implementation-report.md)。除整套与定向运行外，审核还在临时目录中交换真实 rank 链接，使用内存副本更改 manifest，并在独立 pytest 进程中替换图覆盖属性。上述三种验证均未改动仓库生产代码、测试文件或原始数据。

下一轮只需在指定模型恢复后按 P1-1 至 P1-3 集中修复，并复跑同一批正反例与基本门禁。历史缺 CSV 回填的显式跳过保留。未执行新采集、NPU 测试、托管 CI 或任何推送与合并。

## 集中修复后增量复核（2026-11-20）

基线仍为 `ba3e6b3`。复核范围为三组集中修复、直接受影响的真实回归入口和既有检查。`opt_discovery/` 相对基线无差异。

| AE-06 验收项 | 最终判定 | 独立证据 |
|---|---|---|
| Hypothesis、并集、多 rank、窗口、Parser、Metric | PASS | `tests/conftest.py:31` 的确定性 profile 在 Python 3.12 和 3.10 实际运行。`tests/property/test_contracts.py:84`、`:184` 用集合 oracle 核验并集、交集和窗口有效域。`:313` 调用 Parser，`:352` 检查告警、坏行的缺测组及有效行聚合，`:388` 区分 0 和缺测，`:414` 保留来源及派生数值断言。P1-1 关闭。 |
| Diagnosis、Comparison、Evidence | PASS | `tests/property/test_contracts.py:491`、`:579`、`:660`、`:679` 分别覆盖缺必要证据、不可比、归因范围及合成与实测混比。直接反馈入口的来源校验属于 AE-07。 |
| 四组采集身份、轮次与两 rank 来源 | PASS | `scripts/verify_real.py:102` 验证实际配置字段，`:204` 将发现的 rank 目录绑定到日志所属采集。`tests/test_real_unified_manifest.py:130` 和 `:145` 对四组真实数据启用这些检查。定向反例结果见下表。P1-2 关闭。 |
| 实测窗口、Graph、Host ACL、通信量 | PASS | `tests/test_real_unified_manifest.py:175` 对照日志并走真实 Parser。`:209` 对照原始 CSV 的任务数和 Decimal 时长。`:253` 独立扫描 `CANN_API` 区间端点并调用生产 Timeline。`:295` 从 `COMMUNICATION_TASK_INFO` 独立计算去重字节后核对生产汇总。P1-3 关闭。 |
| 异常 rank 和缺测证据 | PASS WITH CONDITIONS | CPU R05、R06 及质量闸门回归均通过，真实样本的无依据诊断约束通过。`tests/test_real_data.py:458` 显式跳过本批输入未出现的缺 CSV 回填分支。没有新增 HBM 实测或 NPU 采集。 |
| CPU、合成及真实入口 | PASS WITH CONDITIONS | `make check` 和 `make verify` 均退出 0。`make verify` 为 393 passed、29 skipped，随后四组 synthetic 演示完成。缺数据时 `make verify-real` 报 BLOCKED 且非零，`--expect-blocked` 退出 0。有数据时 `make verify-real` 和独立脚本均为 28 passed、1 skipped、0 failed、0 errors，状态 SKIPPED。脚本退出 3，Make 退出 2。 |

### 反例复核

| 初始漏检路径 | 独立执行结果 |
|---|---|
| 保留 manifest 声明哈希，分别更改固定参数和采样开关 | 使用授权 manifest 的内存副本将 `fixed.max_num_seqs` 由 32 改为 1、或清空 `profiling.switches`。两组输入均返回对应字段诊断。CPU 回归为 `tests/regression/test_verify_real_identity.py:89`。 |
| graph 的 rank 1 误接到 eager | 使用临时数据别名，仅替换 prefill graph 的 rank 1 链接。身份与 rank 子集在原始输入为 8 passed、4 deselected，错接后为预期的 1 failed、7 passed、4 deselected。误接的采集目录由 `scripts/verify_real.py:245` 处捕获。 |
| 图的时间占比误用任务占比 | 在独立 pytest 进程内用 `task_ratio` 替换 `GraphCoverage.time_ratio`。`tests/test_real_unified_manifest.py:246` 处按预期拒绝约 `0.46957478`，独立原始时长比例为约 `0.57320364`。 |

其他执行记录见[实施报告](implementation-report.md#71-最终验证记录)。定向 CPU 回归 133 项通过，Python 3.10 专项 56 项通过，`git diff --check` 通过。反例只改动内存或临时目录。

### 剩余问题与交付判断

- **P0/P1** 无。三组原 P1 均关闭，增量复核未发现阻断项。
- **原 P2 计数问题关闭。** `scripts/verify_real.py:80` 扣除失败、错误、跳过，`:337` 用实际通过数生成汇总。本轮真实汇总与 pytest 的计数一致。
- **P2 保留。** `tests/property/test_contracts.py:104` 的乱序输入在 `:73` 被测试 helper 排序，两次生产调用最终使用相同顺序。该例对排序漏回归不敏感。若后续扩展，宜通过负责排序的真实入口传入两组顺序。并集 oracle、重复及非法乱序测试仍独立执行。
- **P3 保留。** `tests/property/test_contracts.py:691` 固定反馈 payload 缺少来源字段。AE-07 增加来源契约时应替换为行为断言。

**PASS WITH CONDITIONS。** 验收条件是继续单列历史缺 CSV 场景的真实数据跳过，由本轮已通过的 CPU R05 提供覆盖。两项剩余问题不阻断 AE-06，可由用户决定提交 PR。新采集、NPU 实测和托管 CI 未执行。AE-07 未开始。
