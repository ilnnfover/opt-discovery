# AE-04 独立验收 Review

**结论为 FAIL，暂缓合并 AE-04，暂不进入依赖该事件边界的 AE-05。** CSV Reader 已证实可流式读取，现有 CPU 回归全部通过。核心 DoD 仍有四项缺口：两个实际消费者未共用实例解析，Timeline 漏读分片，新事件会丢失 epoch 短时长，事件身份也未完整保留。

审查分支为 `feat/ae-04-event-contract`，HEAD 为 `fb71da65859f81b584d0bef44febb1298f4e51e6`。实施报告所列基线 `7ca5c0f49e45a60f4f9f0f7eae44bbc2290857da` 是 AE-03 合并提交。AE-04 代码提交的直接父提交为 `001c9da68487069a9d2cfe9fa418cbb89489161d`，中间只有审查文档提交。核对了两种基线差异，代码审查和等价性复验以直接父提交为准。依据为原任务书 AE-04 和实施报告。工作区起初只有未跟踪的任务书。

## DoD 验收矩阵

| DoD | 结果 | 独立证据 |
|---|---|---|
| 区分实例、聚合、TASK 回退与 Host 调用 | 部分通过 | `KernelEvent`、`TaskIntervalEvent`、`HostCall` 为独立类型。既有 `op_statistic` 与 `op_report` 聚合解析保留。新的实例事件会填入无来源的 rank 0 或算子名并丢弃可用 stream，见 P1-4。 |
| 单位、时钟域、窗口筛选与裁剪 | FAIL | 普通 µs、SQLite ns→µs、COMMUNICATION 单次缩放和 op_summary 的相交/裁剪测试通过。生产 Reader 在 epoch 输入上把正时长返回为 0，见 P1-3。Timeline 的全区间统计仍延续旧口径，见 P2-1。 |
| 自然序多分片覆盖 | FAIL | Collector 与 Parser 保留自然序和全部分片，Timeline 仍只选择最大的一个。三个分片实际得到 4 项调用，但 Timeline 只统计 2 项，见 P1-2。 |
| 低层类型共享与依赖收敛 | PASS | `primitives` 承接 Window/Interval，历史导入路径可用。Parser 移除了对 analyzer.window 的依赖。独立进程导入 Parser、Timeline、events 均退出 0。 |
| 至少两个实际消费者复用标准化实例接口 | FAIL | Timeline 的三个读取入口对应三种不同数据源。op_summary 仍只有 Timeline 路径使用新的 Reader，`MappingCsvParser` 继续自读原始 CSV 与独立单位/窗口逻辑。无窗 TASK 路径也保留本地 SQL，见 P1-1、P2-2。 |
| 流式与性能对比 | PASS（CPU 样例） | Python 侧生成器按行消费。CSV 从 4 万行增至 16 万行，完整流式消费的 Python 分配峰值约 40 KiB。Timeline 保留显式列表和排序，40k 的基线对比通过。无索引 SQLite 可能内部排序，见 P3。 |
| 兼容与回归 | 部分通过 | 现有 CLI 演示、Parser、Timeline、R01/R03/R08/R09/R10/R12 通过。同输入的普通列表路径等价。TASK 包装的零宽窗口调用出现接口变化，见 P2-3。真实数据未在本机验证。 |

## 问题清单

未发现 P0。P1 均涉及本阶段要求的共享边界或受支持的 Profiler 输入。

| 级别 | 代码位置 | 证据与影响 | 修复建议 |
|---|---|---|---|
| P1-1 实例解析仍有两条生产路径 | `opt_discovery/events.py:163`、`:229`，`opt_discovery/analyzer/timeline.py:310`，`opt_discovery/parsers/csv_mapping.py:405`、`:626`、`:918` | 新 Reader 在生产中供 Timeline 使用。Parser 的 diff 只更换了低层类型和常量导入，仍独立读取实例 CSV、解析时间戳、分类任务和缩放 COMMUNICATION。Timeline 的 TASK 与 CANN_API 调用各自适配其他数据源，无法证明有第二个消费者复用 op_summary 标准化接口。两侧窗口条件也分别实现。 | 让 Parser 和 Timeline 复用同一 op_summary 行标准化入口。映射扩展和聚合留在 Parser，分别显式应用现有的起点筛选和相交策略。保留流式读取，覆盖两个实际入口。 |
| P1-2 Timeline 对标准分片导出漏读 | `opt_discovery/analyzer/timeline.py:296`、`:444`，`opt_discovery/events.py:202` | 同一 `PROF_*` 目录写入 slice_1、slice_2、slice_10，分别有 2、1、1 条不重叠任务，每条有效时长 10 µs。Collector/Parser 得到自然顺序 1/2/10、4 次调用、40 µs，实际 `summarize` 得到 2 次任务和 20 µs busy。基线和 HEAD 都漏读，这项 AE-04 明确要求的多分片路径仍未收敛。R01 只锁定了 Collector/Parser。 | 用统一的分片来源顺序读取全部实例片，保持 Timeline 最后排序。增加分片与等价单文件的 Timeline 对照，核对任务数、busy 与每 rank 质量判断。 |
| P1-3 epoch 短事件时长归零 | `opt_discovery/events.py:182`、`:192`、`:199`、`:265`、`:282`、`:316`、`:334` | 用 epoch 约 `1790000000000000 µs` 的合法时间戳运行 Reader。普通 CSV 时长 0.1 µs、COMMUNICATION 时长 100 ns、TASK 与 CANN_API 的端点差 100 ns 四条均返回 `duration_us=0.0`。CSV 在未裁剪时也先加大 epoch 再相减，SQLite 先转两个端点的浮点数再相减。新标准化事件无法如实提供时长。 | CSV 未裁剪路径保留换算后时长，SQLite 用整数 ns 先求差再换算。执行相交与裁剪时保留原始整数时钟或相对时间坐标。以 epoch 和短事件同时验收数值与边界。 |
| P1-4 实例身份没有按来源保留 | `opt_discovery/events.py:150`、`:195`、`:207`、`:219`、`:274`，`tests/test_timeline_op_summary.py:15` | 用含受支持 `Stream ID` 列、流号为 17 和 18 的 CSV，显式传 rank=1 后两个流号仍为 None。省略 rank 时 Reader 直接赋 0，TASK 也默认为 0。缺 OP Type 的行会产出 operator=unknown 的 KernelEvent。默认值会把未知身份呈现为确定 rank，流分组也无法使用已有字段。 | 从明确的导出上下文传 rank，缺失则拒绝或显式表示未知。解析存在的 Stream ID，并保留缺流号的 None。无算子身份的行不应作为可按算子归属的完整 KernelEvent，区间视图可继续支持它。 |
| P2-1 Timeline 全事件时长与窗口统计口径混用 | `opt_discovery/analyzer/timeline.py:320`、`:454`、`:614`、`:626` | 在窗口 [12,14] µs 里放一条 [10,20] µs 的事件，两版 `summarize` 均给出 span=2、busy=10、idle=0。Reader 的 `clip=True` 明确存在，Timeline 沿用不裁剪的历史结果。该现象为保留的旧口径，单独记录。 | 分别标识完整任务累计时长与窗口内 busy。若切换窗口占用口径，作为明确结果差异补边界验收。 |
| P2-2 无窗口 TASK 路径仍独立读 SQL | `opt_discovery/analyzer/timeline.py:472`、`:495`、`:622` | 无窗口 `summarize` 走 `_device_gap_stats`，自己做 SQL 与 ns→µs。说明 TASK 的已迁移包装未覆盖全部实际路径。 | 让该流式归并迭代共享的 TASK spans，保留无窗口回退的计数和 gap 行为。 |
| P2-3 旧加载接口对点窗口行为改变 | `opt_discovery/analyzer/timeline.py:281`、`:331`，`opt_discovery/primitives.py:55` | 同一个 SQLite fixture 上，基线 `load_task_intervals(db, 1, 1)` 返回覆盖该点的 [1.0, 1.1] 区间，HEAD 抛出 WindowError。前后签名相同，新增 Window 构造改变行为。Host 包装采用相同构造方式。 | 保留历史包装的过滤行为，或声明点窗口不再支持并为两个包装补契约测试。 |
| P3 性能与来源描述可更精确 | `opt_discovery/events.py:262`、`:314`、`:283`、`:335` | Python 层按游标逐行读取。无索引 CANN_API 实测 query plan 有 `USE TEMP B-TREE FOR ORDER BY`，因此 Python 分配峰值不能代表 SQLite 排序内存。TASK/Host 目前只定位到 DB 和逻辑表，没有逐行标识。 | 文档区分 Python 流式与数据库内部排序，并说明 DB 源引用的定位粒度。 |

## 实际测试与性能

| 检查 | 结果 |
|---|---|
| `git diff --check 7ca5c0f HEAD`、`git diff --check 001c9da HEAD` | 通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | 退出 0。Ruff、mypy 53 文件、配置验证通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae04-review-demo.fPDJmZ` | 退出 0。297 passed、17 skipped，合成演示通过。 |
| `python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_events.py tests/test_timeline.py tests/test_timeline_op_summary.py tests/test_csv_parser.py tests/regression/test_r{01,03,08,09,10,12}*.py` | 54 passed。另单跑六个指定回归文件，17 passed。 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 /tmp/ae04-contract-probe.2hvfJt.py`，及基线/HEAD 共用 fixture 对比 | 实测分片 4 对 2 项任务、epoch 时长归零、rank/stream 缺失，以及基线/HEAD 的点窗口结果。见问题清单。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae04-perf-probe.vPqXrC.py` | 同机 Python 3.12.13，基线通过 git archive 隔离。40k 行同一列表加载路径，三次交替运行取中位数，观察阈值为时间变化 20%、Python 分配峰值变化 10%。基线 1.043 s / 5.542 MiB，HEAD 1.030 s / 5.526 MiB，均得 40,000 条与 400,000 µs。 |
| 同一性能探针的流式与 SQLite 检查 | 流式消费 40k / 160k 行的 Python 峰值分别约 39.94 / 39.95 KiB，首条约 32.99 KiB。无索引 Host SQL 查询计划使用临时排序。CPU 样例不证明 NPU 性能。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py --expect-blocked` | 缺数据闸门退出 0，真实回归状态为 BLOCKED。17 项真实数据用例在全量测试中跳过，NPU 与托管 CI 未运行。 |

新增测试使用真实 Reader 与临时 CSV/SQLite，普通时长、单次缩放和裁剪有独立预期。包装一致性用例共用相同的 Reading 逻辑，不能独立发现共同错误。`inspect.isgenerator` 只能确认函数返回生成器，本次大样例的实际内存测量补上了这一证据。指定回归中，R01 不覆盖 Timeline 全分片，R08 不覆盖短时长与大 epoch 的组合，R10/R12 也均未覆盖新身份字段。这些缺口与全绿的 CPU 套件并存。

合并验收取决于 P1-1～P1-4。优先把标准 Reader 的全分片、时间与身份契约接到两个实际消费者，并用这些反例同时验证 Parser 与 Timeline。P2/P3 作为非阻断记录。AE-05 依赖稳定的 AE-04 边界，建议在上述核心 DoD 通过后进入。

## 修复后增量复核（2026-11-19）

**最终验收结论为 FAIL。暂不允许合并 AE-04，也暂不允许进入依赖该事件契约的 AE-05。** 本次仅复核 `feat/ae-04-event-contract` 上 `fb71da6..e8630ef` 的修复和直接经过的生产路径，依据原任务书及实施报告 §9。工作区复核前已有未跟踪的 `docs/tasks/`，本次只追加审查记录。

### 上轮问题与受影响 DoD

| 上轮项 | 增量结果 | 生产证据 |
|---|---|---|
| P1-1 两个消费者复用实例 Reader | **未关闭** | Parser 与 Timeline 共用时长换算和窗口策略函数。Parser 仍自行读取和解释 `op_summary` 实例行，只有 Timeline 消费实例 Reader。 |
| P1-2 Timeline 漏读分片 | **原问题关闭，修复路径出现 P1 回归** | 真实 `summarize` 读取有序的 slice_1、slice_2、slice_10，得到 4 项任务、40 µs busy。乱序行或分片仍能读全，但 busy 少算。 |
| P1-3 epoch 短事件归零 | **部分关闭** | 普通 CSV 0.1 µs、COMMUNICATION 100 ns、TASK 差值 100 ns 和 CANN_API 差值 100 ns 都能在结构化事件上保留 0.1 µs。实际 Timeline 的区间和 busy 仍会归零，`clip=True` 还会保留从窗口右边界开始的短事件。 |
| P1-4 实例身份 | **关闭** | SHA `e8630ef` 上的 Reader 实测保留流号 17、18，未指定 rank 时返回 `None`。生产 Reader 测试覆盖显式 rank=1，以及缺算子身份的行仅可进入区间视图。 |

上轮非阻断项中，无窗口 TASK 路径已接入共享 spans，点窗口包装的历史行为通过了针对性测试。整任务时长与窗口占用的历史口径及 P3 记录维持原定级。本次增量复核未发现 P0。

### 剩余阻断项

| 级别 | 代码位置 | 实际证据与影响 | 修复建议 |
|---|---|---|---|
| P1 · 乱序输入少算 Timeline busy | `opt_discovery/analyzer/timeline.py:371`、`:463`、`:478`、`:627`；`tests/test_events.py:243` | 临时标准导出中，同一 CSV 按 `[30,40]`、`[10,20]` 写两行。用同一数据运行 `summarize`，`fb71da6` 得 2 项任务、20 µs，`e8630ef` 得 2 项、10 µs。三个分片按自然文件顺序给出 `[30,40]`、`[10,20]`、`[20,30]` 时，得到 3 项、10 µs，预期 30 µs。修复沿文件顺序拼接，省去了旧包装的时间排序，`_merge` 直接依赖起点顺序。新增分片测试只核对 Reader 输出条数和时长总和，未经过该区间并集计算。 | 在区间并集前恢复全局起点排序。用 `summarize` 对照乱序单文件、乱序分片和按时间排序的等价数据，并核对 busy 和 gap。 |
| P1 · 短事件在 Timeline 中归零且窗口裁剪失准 | `opt_discovery/events.py:248`、`:331`；`opt_discovery/analyzer/timeline.py:531`、`:627`、`:661`；`opt_discovery/primitives.py:69`；`tests/test_events.py:208` | 以 `1_790_000_000_000_000 µs` 为起点，事件 Reader 得 0.1 µs，CSV/TASK 区间视图均得 0.0 µs，实际 `summarize` 有 2 项 device task，device busy 和 Host busy 均为 0.0 µs。以该 epoch 为窗口右边界，起点正好落在右边界的 0.1 µs CSV 事件经 `clip=True` 后仍返回 0.1 µs，预期无交集。有关路径将时长加回大浮点时间戳后再用于区间计算。新增测试断言了事件字段，未验证实际聚合或该边界。 | 在相交、裁剪和聚合时保留整数 ns 或使用窗口相对坐标。对 `summarize` 及裁剪 API 覆盖同一 epoch fixture，断言正的 busy 和窗口右边界排除。 |
| P1 · 实例 Reader 复用仍未达到核心 DoD | `docs/tasks/Opt-Discovery-Agent-Engineering-Task.md:219`；`opt_discovery/events.py:175`、`:248`；`opt_discovery/parsers/csv_mapping.py:393`、`:417`、`:635`、`:921`；`opt_discovery/analyzer/timeline.py:467` | 代码跟踪确认 Timeline 调用 `iter_op_summary_intervals`，Parser 则进入自己的 `csv.reader` 循环、解析时间戳并分类。共享 `normalize_duration` 和 `window_span` 已减少重复计算。任务书要求已有消费者共用实例 Reader，第二个生产消费者仍未接入。 | 让两个生产入口共享可配置的实例行读取与标准化路径。Parser 保留映射额外字段、聚合及起点筛选，Timeline 保留相交筛选。测试从两条入口读取同一份导出，核对各自承诺的结果。 |

### 实际验证

| 命令 | 结果 |
|---|---|
| `git diff --check fb71da6 HEAD` | PASS，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | PASS，Ruff、mypy（53 个文件）和配置验证退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae04-followup-demo.1NYVtu` | PASS，302 passed、17 skipped，合成演示完成。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_events.py tests/test_timeline.py tests/test_timeline_op_summary.py tests/test_csv_parser.py tests/regression/test_r{01,03,08,09,10,12}*.py` | PASS，59 passed，包含六组指定回归。 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 /tmp/ae04-delta-probe.lriJLZ.py` | 独立临时 CSV/SQLite 反例直调生产 Reader 和 `summarize`，并用隔离的 `fb71da6` 对照。文件乱序、区间精度和右边界裁剪的验收预期为 FAIL，实际值见上表。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py --expect-blocked` | BLOCKED，缺真实数据根目录。命令退出 0 表示缺数据闸门如预期生效。真实 Profiling、NPU 和托管 CI 均未验证。 |

## 第二次增量复核（2026-11-19）

**AE-04 验收结论为 FAIL。暂不允许合并或进入 AE-05。** 审查分支 `feat/ae-04-event-contract`，对照 `e8630ef..62e661a` 和实施报告 §10。复核范围为上轮三项 P1 及此次修复直接改变的路径。复核开始时已有未跟踪的 `docs/tasks/`，本次只追加本报告。

### 受影响的 DoD

| 验收项 | 结论 | 实测 |
|---|---|---|
| 分片读取、全局排序和区间并集 | PASS，上轮问题关闭 | `summarize` 对乱序单文件得到 2 项、20 µs，对乱序三分片得到 3 项、30 µs，有序三分片仍为 4 项、40 µs。 |
| 两个生产消费者复用实例标准化 | PASS，复用条款关闭 | Timeline 的 CSV Reader 和 `MappingCsvParser.parse` 实际调用同一 `normalize_instance_row`。内置映射下，普通 0.1 µs 与 COMMUNICATION 100 ns 两行在两侧均汇总为 0.2 µs。映射及窗口兼容性见下表。 |
| epoch 精度、窗口裁剪及归因 | FAIL，上轮问题部分关闭 | 上轮窗口样例的 device/Host busy 从 0/0 恢复到 0.2/0.1 µs，右边界起点正确排除。跨右边界裁剪、连续短 SQLite 事件、无窗回退及 Parser busy 仍有误。本次坐标改动还引入了窗口左边界的 Host 并集回归。 |
| 原有 Parser 结果、质量元数据和 Timeline 指标兼容 | FAIL | 同一 CSV/SQLite fixture 与 `e8630ef` 对照，复现下表的任务分类、单位、窗口行数和 Host busy 回归。 |

### 阻断项

| 级别 | 代码位置 | 实际证据与影响 | 修复建议 |
|---|---|---|---|
| P1 · Parser 忽略任务类型映射与默认值 | `opt_discovery/parsers/csv_mapping.py:423`、`:488`、`:653`、`:795`；`opt_discovery/events.py:180`、`:199` | 对同一 CSV，用带默认 `kernel` 的配置解析空任务类型，`e8630ef` 得 `kernel`，`62e661a` 得 `unknown`。显式把 `CustomCollective` 映射为 `communication` 后，值为 10,000 的时长从 `communication / 10 µs` 变为 `other / 10,000 µs`。共享入口采用默认分类器并把结果送入 `_accumulate`，覆盖了 Parser 的配置语义，影响时长和按任务类型筛选的额外指标。 | 让 Parser 调用共享入口时传入映射解析器，保留默认类型。用生产 Parser 测试空任务类型、自定义通信别名、COMMUNICATION 单次缩放和额外指标。 |
| P1 · Parser 窗口内实例被漏计，缺失时间戳被当成 0 | `opt_discovery/parsers/csv_mapping.py:419`、`:445`、`:462`、`:501`；`opt_discovery/events.py:184`、`:191` | 窗口 `[1000,3000]` 内有带 CSV 引号的 `"1,020" / "1,000"`、0 时长和 `N/A` 时间戳三行。`e8630ef` 保留前两行并记 `outside=0, skipped=1`，`62e661a` 得空表、`outside=3, skipped=0`。缺少时间戳列的 CSV 放入 `[0,100]` 时，旧版跳过并记该轮 0 行，新版按起点 0 纳入并记该轮 1 行。现有数字格式、调用数和缺失轮次判定均受影响。 | 共享行处理时沿用 Parser 的数字格式和零时长规则。区分缺时钟、缺时长和真正窗口外的行，保持原统计与告警，窗口筛选禁止使用合成起点。 |
| P1 · Host 并集把合法端点 0 当成缺失 | `opt_discovery/analyzer/timeline.py:670`、`:683`、`:709` | `summarize` 在 `[100,200] µs` 内按既有相交口径纳入 `[90,100]`、`[130,140]` 两次 Host 调用。旧版与完整区间并集预期均为 20 µs，新版为 50 µs。第一段结束于相对坐标 0，`host_seg_end or start` 把两段之间的空白算进 busy。 | 用显式 `None` 检查判断并集端点是否初始化，并给真实 `summarize` 增加左边界与分离调用的回归。 |
| P1 · epoch 精度仍在裁剪、区间和窗口质量计算时丢失 | `opt_discovery/primitives.py:69`、`:83`；`opt_discovery/events.py:378`、`:438`；`opt_discovery/analyzer/timeline.py:516`、`:670`；`opt_discovery/parsers/csv_mapping.py:491` | 设 `E=1_790_000_000_000_000 µs`。从 `E` 开始的 0.3 µs 事件裁到 `E+0.25` 后仍返回 0.3 µs。两条相邻的 100 ns TASK 和 CANN_API 记录在有窗 Timeline 中各只计 0.1 µs，预期各 0.2 µs，两个起点在换算绝对浮点 µs 时已合并。无窗 Timeline 对单条 100 ns 仍输出 device/Host busy 0/0。生产 Parser 对 0.5 µs 窗口里的 0.1 µs 任务保留算子时长，却报告窗口 busy 为 0 并误标 `degraded`，预期 busy ratio 为 20%。 | 用相对坐标计算裁剪，并在 SQLite 整数时钟上先减共同原点再换算。窗口和无窗聚合保持 device、Host、MSTX 同一坐标约定，Parser 的 busy 计量用相对起点和标准化时长。对实际消费者断言裁剪上限、两段独立区间、无窗 busy 与窗口质量。 |

本次未发现 P0。上轮已关闭的事件身份和点窗口包装维持原结论。上轮关于整任务时长与窗口 busy 的历史口径及 P3 记录仍为非阻断项。

### 实际验证

| 命令 | 结果 |
|---|---|
| `git diff --check e8630ef HEAD` | PASS，退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | PASS，Ruff、mypy（53 文件）、配置验证通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae04-final-demo.UJOyPk` | PASS，306 passed、17 skipped，合成演示完成。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_events.py tests/test_timeline.py tests/test_timeline_op_summary.py tests/test_csv_parser.py tests/regression/test_r{01,03,08,09,10,12}*.py` | PASS，63 passed，包含指定六组回归。 |
| 在 `git archive e8630ef` 的临时副本中只移入新版 `tests/test_events.py`，以 `pytest -k 'summarize_epoch_short_event_has_busy or window_clip_drops_event_at_right_boundary or summarize_handles_unordered_slices or parser_and_reader_share_epoch_normalization'` 执行 | 旧版 3 failed、1 passed、13 deselected。前三例分别捕获旧版短事件、右边界、乱序错误。Parser/Reader 时长相等用例在旧版同样通过，共享调用路径由当前生产代码对照确认。 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 /tmp/ae04-delta-probe.lriJLZ.py`，及在当前和隔离的上一提交运行 `/tmp/ae04-final-probe.6uBVXI/probe.py` | 通过真实 Parser、Reader 和 Timeline 重跑旧反例并对照最新变化。排序、右边界起点和原单条有窗样例通过，新增或剩余不符见阻断项。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py --expect-blocked` | BLOCKED，未配置真实数据根目录。退出 0 仅说明缺数据闸门正常。真实 Profiling 未执行，NPU、托管 CI 未运行。 |

## 第三次增量复核（2026-11-19）

**AE-04 验收结论为 FAIL。暂不允许合并或进入 AE-05。** 本次审查 `feat/ae-04-event-contract` 上的 `62e661a..d9950de`，对照实施报告 §11，仅复测上轮四项 P1 和修复直接影响的路径。开始时已有未跟踪的 `docs/tasks/`，本次只追加审查报告。

### 上轮问题与 DoD

| 验收项 | 结论 | 实际证据 |
|---|---|---|
| 实例标准化复用 | PASS | Parser 和 Timeline 仍走共享实例行标准化。已关闭的分片排序用原探针复测，单文件 20 µs、三分片 30 µs。 |
| Parser 任务类型映射和单位 | FAIL，原问题未关闭 | 再次用同一映射和 CSV 得到 `unknown` 和 `other / 10,000 µs`，预期 `kernel` 和 `communication / 10 µs`。 |
| Parser 窗口筛选和质量记录 | 部分通过 | 千分位数字、零时长、`N/A` 起点及缺时间戳列的原反例通过。合法起点配缺失时长时，窗口筛选和轮次计数仍偏离原契约。 |
| Host 左边界并集 | PASS，原问题关闭 | 两个合法调用的总 busy 由 50 µs 恢复为 20 µs。设备空闲归因有独立回归，见下表。 |
| epoch 精度和区间分析 | 部分通过 | 跨右边界裁剪为 0.25 µs，窗口内相邻短事件聚合为 0.2 µs，无窗单事件 device/Host busy 为 0.1/0.1 µs，Parser busy ratio 为 20% 且质量未降级。SQLite 窗口边界仍可漏事件，多 rank 和无窗空闲归因回退。 |

### 剩余阻断项

| 级别 | 代码位置 | 影响和复现证据 | 修复建议 |
|---|---|---|---|
| P1 · Parser 仍忽略任务类型映射 | `opt_discovery/parsers/csv_mapping.py:446`、`:482`、`:520`、`:830`；`opt_discovery/events.py:180` | 两个 Parser 调用都没有传其映射分类器。`e8630ef` 对空类型应用 `kernel` 默认值，对自定义通信别名的 10,000 ns 得 10 µs。`62e661a` 和 `d9950de` 均输出 `unknown` 及 `other / 10,000 µs`。任务分类、时间及按类型筛选的指标受影响。 | 在 Parser 的有窗及无窗入口传入 `self._resolve_task_type`，以默认值、自定义通信别名及缩放结果验证。 |
| P1 · 缺时长时绕过 Parser 起点窗口筛选 | `opt_discovery/parsers/csv_mapping.py:435`、`:460`、`:513` | 窗口 `[100,200]`，两轮 `[100,150]`、`[150,200]`。输入为窗口外起点 10、窗口内起点 120，两行时长均为 `N/A`，再加入起点 160 的有效任务。`e8630ef` 输出 `outside=1, skipped=1, rounds={1:1,2:1}`，`d9950de` 输出 `outside=0, skipped=2, rounds={1:0,2:1}`，并生成一个原本应由窗口筛掉的算子组。该轮误标无采集数据。 | 解析出起点后先做窗口筛选和轮次计数，再处理时长和聚合，保持缺时长与缺起点的既有统计口径。 |
| P1 · 多 rank 和无窗空闲归因使用不同时间原点 | `opt_discovery/analyzer/timeline.py:611`、`:649`、`:663`、`:673`、`:689`、`:769`；使用入口 `opt_discovery/cli.py:375`、`:1789` | 两段 TASK 位于 `[1000,1250]`、`[1450,1600]` µs，另分别用一个 Host 调用和一个真实 SQLite MSTX 标记完整覆盖 200 µs 空洞。`62e661a` 在单 rank 有窗、多 rank 有窗、无窗均给 Host 覆盖 200 µs，单独的 MSTX 样例也覆盖 200 µs。`d9950de` 单 rank 有窗保持 200 µs，其余两条路径的 Host 和 MSTX 覆盖均为 0，Host 分类从 `acl_busy` 变为 `host_silent`。多 rank 未设置窗口原点，无窗未传递 TASK 原点，导致归因结果随入口变化。 | 给设备、Host 与 MSTX 使用共同时间原点。有窗统一用窗口起点，无窗传递首个 TASK 的实际原点。在三条生产路径核对覆盖量和分类。 |
| P1 · SQLite 窗口界限在筛选前丢失 ns 精度 | `opt_discovery/events.py:382`、`:385`、`:454`、`:457` | 取 `E=1_790_000_000_000_000 µs`，窗口起点 `E+0.25 µs` 应对应 `E×1000+250 ns`，浮点乘法得到 `E×1000+256 ns`。一条在该边界后 2 ns 结束、确实相交的 52 ns TASK 与同范围 Host 调用均被 SQL 丢弃。同样再放入一条窗口内 100 ns 记录，`d9950de` 只统计 1 项任务和 1 次 Host 调用，各应为 2。相对坐标修复保留了已选中事件的时长，查询边界仍沿用舍入后的绝对值。 | 用同一精确换算得到 SQLite 整数 ns 的筛选界限和原点，并保留相交口径。用含小数 µs 边界的真实 SQL 样例断言两个 Reader 的纳入与排除。 |

未发现 P0。上轮已关闭的事件身份、分片及点窗口兼容结论保留。已记录的整任务时长与窗口 busy 历史口径仍为非阻断项。

### 实际验证

| 命令 | 结果 |
|---|---|
| `git diff --check 62e661a HEAD` | PASS。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | PASS。Ruff、mypy（53 个文件）、配置验证通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae04-close-demo.6E6hSV` | PASS，312 passed、17 skipped，合成演示完成。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_events.py tests/test_timeline.py tests/test_timeline_op_summary.py tests/test_csv_parser.py tests/regression/test_r{01,03,08,09,10,12}*.py` | PASS，69 passed，含指定六组回归。 |
| 将当前 `tests/test_events.py` 复制到 `git archive 62e661a` 的临时副本，使用 `pytest -k` 运行此次新增的六例 | 旧版 6 failed、17 deselected。新版六例随定向套件通过。断言覆盖真实生产路径中的数值和质量记录，缺少任务类型映射、多 rank 和无窗归因的回归。 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 /tmp/ae04-close-probe.3dQ2qX/probe.py`，另对照 `62e661a`、`e8630ef` 并运行同目录 `gap-class-probe.py` | 使用临时 CSV 和真实 SQLite 读取函数复跑原反例，验证缺时长、归因和 SQLite 查询边界。逐项实际值见验收矩阵及问题清单。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py --expect-blocked` | BLOCKED，数据根目录未配置。退出 0 表示缺数据闸门符合预期。真实 Profiling 未执行，NPU 和托管 CI 未运行。 |

## 当前提交的完整问题清单

本节汇总 `d9950de` 在约定的 AE-04 增量范围内全部已确认的未关闭项。复查时生产代码、测试和配置均无未提交改动。**结论为 FAIL，暂不允许合并或进入 AE-05。无 P0，修复验收集中在以下四组 P1。**

| 级别 | 代码位置 | 一次验收所需的结果 |
|---|---|---|
| P1-1 · Parser 任务类型映射 | `opt_discovery/parsers/csv_mapping.py:446`、`:482`、`:830` | 有窗和无窗均使用配置的默认值和别名。空类型应为 `kernel`，自定义通信类型的 10,000 ns 应为 10 µs，并匹配对应的通信额外指标。当前仍为 `unknown`、`other / 10,000 µs`。 |
| P1-2 · Parser 缺时长时的窗口行为 | `opt_discovery/parsers/csv_mapping.py:435`、`:460`、`:513` | 按有效起点先筛窗口和统计轮次，再处理时长。第三次复核样例应得 `outside=1, skipped=1, rounds={1:1,2:1}`，且窗口外算子不进入结果。保留本轮已经通过的千分位数字、零时长和缺起点处理。 |
| P1-3 · Timeline 归因原点 | `opt_discovery/analyzer/timeline.py:611`、`:649`、`:663`、`:673`、`:689` | 对同一份 SQLite 样例，单 rank 有窗、多 rank 有窗、无窗都得到 Host 覆盖 200 µs、`acl_busy`。独立 MSTX 样例也都覆盖 200 µs。当前后两条路径均把覆盖算为 0。设备、Host 和 MSTX 应使用同一个原点。 |
| P1-4 · 窗口相交的时间精度 | `opt_discovery/events.py:382`、`:454`；`opt_discovery/primitives.py:69`、`:75` | SQLite 边界复测仍偏移 6 ns，漏掉 1 条 TASK 和 1 条 Host。补测发现 CSV 也有同组误差：epoch `E` 开始、时长 0.20 µs 的事件与 `[E+0.25,E+1]` 无交集，`iter_op_summary_events` 与 `summarize` 却分别返回 1 个事件和 0.20 µs busy，`clip=True` 正确返回空。SQLite 筛选使用精确整数 ns 边界，CSV 相交比较用时间差和时长，并保留真正相交时的整任务时长。 |

非阻断项保留三条。P2 为 Timeline 沿用整段相交任务统计的历史口径，busy 可能超过窗口长度。P2 为 `load_op_intervals`、`load_task_intervals`、`load_host_intervals` 的绝对浮点区间精度限制，实测三者对 epoch 上的 0.1 µs 均返回 0，事件 Reader 的 `duration_us` 均为 0.1 µs。CLI 与 `summarize` 当前未调用这三个包装，建议在兼容接口文档注明精度限制。P3 为 SQLite 内部排序内存与 DB/表级来源定位的说明，见原报告和实施报告。对应代码为 `opt_discovery/analyzer/timeline.py:284`、`:322`、`:343`、`:650` 和 `opt_discovery/events.py:384`、`:456`。

本次在当前提交重跑 `/tmp/ae04-close-probe.3dQ2qX/probe.py`、同目录 `gap-class-probe.py` 和 `/tmp/ae04-review-inventory.ySExJA/clock.py`，核对了上述 CSV、SQLite 和生产 Timeline 输出。全量测试记录沿用同一提交的第三次复核结果，`make verify` 为 312 passed、17 skipped。真实 Profiling 仍为 BLOCKED，NPU 未运行。分片排序、共享实例标准化、事件身份、点窗口兼容、Host 左边界并集以及已通过的短事件样例维持关闭。

## `344c2b9` 增量复核（2026-11-19）

**验收结论为 FAIL。暂缓合并与进入 AE-05。** 本次对照 `d9950de..344c2b9` 与实施报告 §12，复核上轮问题及此次修改直接经过的代码。上轮四组问题均已关闭。此次时钟修复改变了两个公开 Reader 的相对时间参数语义，产生一项 P1。

| 上轮验收项 | 复核结果 | 实际证据 |
|---|---|---|
| P1-1 · Parser 任务类型映射 | 关闭 | 有窗、无窗均将空类型解析为 `kernel`。自定义通信别名按 10,000 ns → 10 µs 换算，`communication_time_us` 同为 10 µs。 |
| P1-2 · 缺时长时的窗口行为 | 关闭 | 样例恢复 `outside=1, skipped=1, rounds={1:1,2:1}`，窗口外算子未进入结果。千分位、零时长与缺起点的既有用例继续通过。 |
| P1-3 · Timeline 归因原点 | 关闭 | 使用独立主、次 rank SQLite 文件及 `load_mstx` 的实际读取路径。单 rank 有窗、多 rank 有窗及无窗均有 200 µs Host 覆盖并归类 `acl_busy`，独立 MSTX 样例均覆盖 200 µs。 |
| P1-4 · 窗口相交精度 | 关闭 | SQLite 原反例恢复 2 项 TASK 和 2 次 Host 调用，device/Host busy 各 0.152 µs。CSV 原反例在事件 Reader 和 `summarize` 中均得到 0 项，device busy 为 0。 |

| 级别 | 代码位置 | 实际影响与修复建议 |
|---|---|---|
| P1 · 公开 Reader 的首事件相对坐标回归 | `opt_discovery/events.py:416`、`:450`、`:496` | 用 `origin_us=None` 显式请求按首行定位时，`d9950de` 的 TASK 与 Host Reader 均返回起点 `[0.0, 0.1] µs`。`344c2b9` 把 `None` 变成零偏移，返回 epoch 绝对坐标，两个起点之差变为 0.25 µs。此前实现和 TASK Reader 文档均有首行原点语义。应保留 `None` 直到读到首行，再锁定原始整数 ns。Timeline 继续传显式 `origin_ns`，回归同时覆盖默认绝对坐标、首行相对坐标和显式整数原点。 |
| P2 · 无窗 TASK 原点预读改变缺数据行为 | `opt_discovery/events.py:377`、`:403`；`opt_discovery/analyzer/timeline.py:520` | 同一个缺 TASK 表的 SQLite 文件，上一提交返回 `TimelineError`，当前泄出 `sqlite3.OperationalError`。直接给 `summarize` 传不存在的路径时，当前还会创建空数据库文件。复用共享 Reader 的文件与表存在性检查，预读以只读方式打开，并保留既有错误类型。 |

无 P0。上一节的历史统计口径、旧绝对区间表示和来源说明继续作为非阻断记录。

| 验证 | 结果 |
|---|---|
| `git diff --check d9950de HEAD` 和 `PYTHONDONTWRITEBYTECODE=1 make check` | PASS。Ruff、mypy 与配置检查通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae04-344-demo.5v9S4t` | PASS，316 passed、17 skipped，合成演示完成。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_events.py tests/test_timeline.py tests/test_timeline_op_summary.py tests/test_csv_parser.py tests/regression/test_r{01,03,08,09,10,12}*.py` | PASS，73 passed。 |
| 在 `git archive d9950de` 的临时副本中运行本次新增的四个测试 | 预期失败，4 failed、23 deselected。四个测试在 `344c2b9` 的定向套件中通过。 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 /tmp/ae04-344-probe.s8DriW/check.py`，并从隔离的 `d9950de` 目录运行同一探针 | 独立 CSV 和 SQLite 探针实测上轮四组均关闭。新增相对坐标回归与缺数据路径回归见上表。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py --expect-blocked` | BLOCKED，未配置真实数据根目录。NPU 与托管 CI 未运行。 |
