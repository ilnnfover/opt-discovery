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
