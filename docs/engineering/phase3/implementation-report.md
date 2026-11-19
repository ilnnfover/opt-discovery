# AE-04 实施报告：统一 Profiler 数据标准化与访问边界

日期：2026-11-19（Asia/Shanghai）
基线：`main` @ `7ca5c0f`（AE-03 合并后）
分支：`feat/ae-04-event-contract`
范围：仅 AE-04；未改动 AE-05 lint、AE-06 属性测试/真实回归。

## 1. 目标与验收

任务书 AE-04 要求：定义 KernelEvent/EventReader 契约，统一单位与窗口语义，保留流式读取，迁移至少两个实际消费者，保持现有分析结果与 CLI 兼容。

本阶段 DoD：

| DoD | 状态 |
|---|---|
| KernelEvent / EventReader 契约 | 完成（`opt_discovery/events.py`） |
| 时间单位、区间、来源语义统一 | 完成（µs + `SourceReference`；`ns -> µs` 与 COMMUNICATION 缩放集中） |
| 保留流式读取，聚合行不伪装成实例事件 | 完成（生成器；`op_statistic` / `op_report` 聚合行不进入 KernelEvent） |
| ≥2 个实际消费者复用共享接口 | 完成（timeline 的 op_summary、TASK fallback、CANN_API 三条读取路径） |
| 现有分析结果与 CLI 兼容 | 完成（`load_*_intervals` 包装保留，全量回归通过） |
| 独立 Commit + 本报告 | 完成 |

## 2. 现有读取路径（实施前）

| 数据源 | 读取位置 | 问题 |
|---|---|---|
| `op_summary` CSV | `analyzer/timeline.load_op_intervals` 自己读写 CSV；`parsers/csv_mapping` 另有一套映射驱动解析 | 两处重复解释单位（COMMUNICATION ns）、时间戳与窗口；timeline 不读算子名 |
| `TASK` sqlite | `timeline.load_task_intervals` / `_device_gap_stats` | ns→µs 分散；无共享类型 |
| `CANN_API` Host 调用 | `timeline.load_host_intervals` / `_host_stream` | 两处重复 SQL、ns→µs、名称解析 |
| `op_statistic` | `csv_mapping` 聚合行 | 必须保持聚合语义，不能当逐实例事件 |
| `op_report` sqlite | `parsers/sqlite_op_report.py` | 聚合口径，保持独立 |
| 通信字节 | `analyzer/comm.py` | 独立适配器，非 kernel 事件 |
| MSTX | `collectors/mstx.py` | 独立事件源 |

## 3. 设计

### 3.1 稳定低层模块 `opt_discovery/primitives.py`

从 `analyzer/window.py` 抽出、不依赖 collectors/parsers/analyzer：

- `Window`、`WindowQuality`、`WindowError`、`IntervalUnion`
- `Interval`（含 `duration_us`、`overlap_us`、`clipped(window)`）
- `NS_PER_US`、`COMMUNICATION_DURATION_SCALE`、窗口阈值常量

`analyzer/window.py` 保留历史导入路径并只保留 `select_rounds_window`；`parsers/csv_mapping.py` 改为从 `primitives` 导入（含函数内 `IntervalUnion`），移除 `parsers -> analyzer.window` 依赖。

### 3.2 事件契约 `opt_discovery/events.py`

```python
@dataclass(frozen=True)
class SourceReference:
    kind: str          # op_summary | task_table | cann_api
    path: str
    row: int | None = None
    note: str | None = None

@dataclass(frozen=True)
class KernelEvent:
    operator: str
    rank: int
    start_us: float
    duration_us: float
    task_type: TaskType
    source: SourceReference
    stream_id: int | None = None
```

- `TaskIntervalEvent`：TASK 回退的逐任务区间，**没有算子身份**，与 KernelEvent 分开建模。
- `HostCall`：CANN_API Host 调用（名称 + 区间），不是 kernel。
- 读取器（生成器）：`iter_op_summary_events`、`iter_task_intervals`、`iter_host_calls`。
- 区间快路径：`iter_op_summary_intervals`、`iter_task_spans`、`iter_host_spans`，供只需要区间统计的消费者使用，避免逐行构造 dataclass。
- 错误：`EventReadError`；timeline 包装为 `TimelineError`，保持旧异常消息。

### 3.3 窗口语义

| 模式 | 语义 | 使用方 |
|---|---|---|
| `clip=False`（默认） | 与窗口相交的事件保留**完整时长** | timeline（沿历史语义） |
| `clip=True` | 区间裁剪到 `[window.start_us, window.end_us)`，零长度丢弃 | 需要严格窗口时长的调用方，新增测试覆盖 |
| SQL 读取（TASK/CANN_API） | `endNs >= start AND startNs <= end`，与窗口相交即保留；Python 侧不重复过滤 | timeline，单一过滤来源 |

`op_summary` 行保持文件自然顺序；需要排序的调用方显式排序（timeline 包装保留排序）。

### 3.4 单位与来源

- sqlite `Ns` 仅在 `events.py` 转为 µs（`NS_PER_US`）。
- `Task Duration(us)` 的 COMMUNICATION 行按 `COMMUNICATION_DURATION_SCALE=0.001` 缩放，常量从 parsers 移到 primitives，parsers 仍再导出以兼容。
- 每条事件带 `SourceReference(kind, path, row)`；不编造 rank/stream：TASK 无算子名、Host 无 rank，分别用独立类型或默认 rank 参数表达。

## 4. 消费者迁移

| 消费者 | 迁移前 | 迁移后 |
|---|---|---|
| timeline op_summary 区间 | 本地 CSV 解析 + COMMUNICATION 缩放 | `iter_op_summary_intervals`；保留 `load_op_intervals` 包装、排序与异常语义 |
| timeline TASK fallback | 本地 SQL + ns→µs | `iter_task_spans`；`load_task_intervals` 包装 |
| timeline Host ACL | 两套 SQL + ns→µs + 名称解析 | `iter_host_spans` / `iter_host_calls`；`load_host_intervals`、`_host_stream` 共用 |
| 其他 | `op_statistic`/`op_report` 聚合、通信字节、MSTX | 明确不迁移（源语义不同），避免把聚合行伪装成实例 |

## 5. 性能（同机 CPU，Python 3.12.13）

放大 fixture：200,000 行 op_summary（5.6MB，5% COMMUNICATION）；Host 500,000 行 CANN_API。

| 路径 | 基线（`7ca5c0f`） | 本次 | 峰值内存 |
|---|---|---|---|
| `load_op_intervals`（list） | 5.512s / 27.51MiB | 5.564s / 27.49MiB | 持平 |
| 流式首个事件 | 无（本地 list 实现） | 0.001s / 0.03MiB | 流式 |
| 流式消费全部事件 | 无 | 6.861s / 0.05MiB | O(1) |
| `_host_stream` 500k | 4.0s / 0.0MiB | 1.55s / 0.0MiB | 持平，更快 |

结论：timeline 的 op_summary 列表路径时间/内存与基线持平；新增流式 API 内存为常数级；Host 路径更快。未在 NPU 上测量，不对真实 Profiler 性能作提升声明。

## 6. 测试与验证

新增 `tests/test_events.py`（23 条）：

- op_summary filter/clip 语义、COMMUNICATION 单位缩放、来源行号、rank、坏行/零时长跳过；
- 缺时间列报 `EventReadError`；生成器惰性；
- TASK ns→µs、窗口过滤、缺表错误；
- Host 名称解析、ns→µs、窗口过滤；
- timeline 包装与 Reader 语义一致；
- `primitives.Window` 与 `analyzer.window.Window` 同一对象；csv_mapping 无 analyzer 依赖（AST 检查）。

| 检查 | 退出码 | 结果 |
|---|---|---|
| `make check` | 0 | Ruff / mypy 53 文件 / validate |
| `python -m pytest`（3.12） | 0 | 312 passed, 17 skipped（329 用例） |
| `python -m pytest`（3.11） | 0 | 312 passed, 17 skipped |
| `make verify DEMO_OUT=/tmp/ae04-demo2` | 0 | 合成演示完成 |
| `tests/test_real_data.py`（授权数据） | 0 | 16 passed, 1 skipped |
| `verify_real.py --expect-blocked` | 0 | 缺数据 BLOCKED |
| R01/R03/R08/R09/R10/R12 | 0 | 17 passed |

## 7. 兼容性

- `opt_discovery.analyzer.window` 仍导出 `Window/WindowQuality/IntervalUnion`，旧导入有效；`analyzer.__init__` 不变。
- `timeline.load_op_intervals/load_task_intervals/load_host_intervals` 签名与返回结构不变；异常仍为 `TimelineError`。
- `csv_mapping.COMMUNICATION_DURATION_SCALE` 仍可导入。
- CLI 与产物 schema 无变化。

## 8. 已知限制

- `op_summary` 仍会被 timeline 物化为 `Interval` 列表（排序/合并需要），内存 O(事件数)，与基线一致；流式 API 本身不缓存。
- `clip=True` 仅实现于 op_summary；TASK/CANN_API 依赖 SQL 相交过滤，不提供裁剪语义（真实消费方不需要，显式记录）。
- TASK fallback 没有算子身份，无法升级为 KernelEvent；未伪造 operator/stream。
- `op_statistic`、`op_report`、通信字节、MSTX 保持各自适配器，未强行统一（任务明确要求区分聚合行与实例行）。
- 未运行四组真实导出的完整 `analyze`；真实数据只用既有 `test_real_data.py` 验证解析/时间线语义。NPU 未运行。
- 同一输出目录并发仍不支持（AE-03 已知限制），本阶段未改变。
- 未新增 import-linter；`parsers -> analyzer` 只用本模块的 AST 测试守住 csv_mapping，全量依赖 lint 属 AE-05。

## 9. 复审问题修复（2026-11-19）

依据 `docs/engineering/phase3/review-report.md`：

| 编号 | 问题 | 修复 | 回归 |
|---|---|---|---|
| P1-1 | Parser 与 Timeline 各自解释实例行 | `normalize_duration`（单位 + COMMUNICATION ns）与 `window_span(policy=...)` 移入 `primitives`；Parser 实例行改用共享入口（起点筛选 `policy="start"`），Timeline 用 Reader（`intersect`/`clip`）；映射扩展与聚合仍留在 Parser | `test_primitives_are_shared_without_parser_to_analyzer_import`；全量 CSV 回归 |
| P1-2 | Timeline 只读取一个 op_summary 分片 | 新增 `op_summary_paths()`（自然序：slice_1/2/10），`_rank_intervals` 读取全部切片后再由 Timeline 排序合并；`find_op_summary` 保留兼容 | `test_timeline_reads_all_op_summary_slices`（4 任务 / 40µs） |
| P1-3 | epoch 上短事件时长归零 | CSV 未裁剪路径保留原始时长（不先加 epoch 再减）；裁剪用左右扣减；SQLite 先整数 ns 求差再除 1000 | `test_epoch_short_durations_survive_all_readers`（CSV 0.1µs、COMM 100ns、TASK/Host 100ns） |
| P1-4 | rank/stream 身份丢失 | `KernelEvent.rank: int \| None`，默认 `None`，由调用方显式传入；解析可选 `Stream ID`；无 `OP Type` 的行不产出 KernelEvent（区间视图仍保留） | `test_op_reader_preserves_stream_id_and_unknown_rank`、`test_rows_without_operator_are_not_kernel_events` |
| P2-2 | 无窗口 TASK 路径仍本地 SQL | `_device_gap_stats` 改用 `iter_task_spans`（SQL 仍负责窗口过滤），删除重复的 ns→µs 与行解析 | 全量 `test_timeline` 与真实数据回归 |
| P2-3 | 点窗口包装抛 `WindowError` | `load_task_intervals` / `load_host_intervals` 对 `end <= start` 保持“覆盖该点”的旧过滤语义，不构造非法 Window | `test_point_window_wrappers_keep_historical_filter` |
| P3 | 性能/来源描述 | Report 明确 Python 流式 ≠ SQLite 内部排序；TASK/Host 的 `SourceReference` 定位到 DB/表级，不伪造逐行号 | 文档 |

P2-1（Timeline 保留“整任务时长/窗口 busy”旧口径）未改变语义，作为已知差异记录；若切换口径需单独结果验收。

验证：`make check` 0；`pytest` **312 passed, 17 skipped**（319 用例）；`make verify` 0；真实数据 16 passed / 1 skipped。

## 10. 增量复核修复（2026-11-19）

| 级别 | 问题 | 修复 | 回归 |
|---|---|---|---|
| P1 | 乱序输入少算 Timeline busy | `_rank_intervals` 在跨分片拼接后全局按起点排序；`summarize` 的相对并集复用该顺序 | `test_summarize_handles_unordered_slices`（3 分片乱序 → 3 任务、30µs、gap 0） |
| P1 | epoch 短事件在 Timeline 归零、右边界裁剪失准 | Timeline 以窗口起点为相对坐标做并集（`Interval` 只在相对坐标上相加）；Host/MSTX 同步平移；`window_span(clip=True)` 排除起点恰好落在 `window.end_us` 的事件 | `test_summarize_epoch_short_event_has_busy`（busy 0.1µs）、`test_window_clip_drops_event_at_right_boundary` |
| P1 | Parser 未接入共享实例 Reader | 新增 `events.normalize_instance_row()`：一次完成起点/时长/任务类型/单位/窗口裁剪；Timeline 的 `_iter_op_rows` 与 `MappingCsvParser` 都调用它。Parser 仍负责映射扩展、聚合与额外指标，窗口策略分别为 `start`（Parser）与 `intersect`/`clip`（Timeline） | `test_parser_and_reader_share_epoch_normalization`（同一 CSV，Parser/Reader 均 0.2µs） |
| P2 | 非阻断项 | 无窗口 TASK 已用共享 spans；点窗口包装保持旧语义（上一轮已修） | 已有回归 |

说明：Timeline 的“整任务时长 / 窗口 busy”历史口径仍未改变（P2-1），仅将内部并集坐标改为窗口相对；对输出字段的语义没有切换。

验证：`make check` 0；`pytest` **312 passed, 17 skipped**（323 用例）；`make verify` 0；真实数据 16 passed / 1 skipped。

## 11. 第二轮增量复核修复（2026-11-19）

| 级别 | 问题 | 修复 | 回归 |
|---|---|---|---|
| P1 | Parser 接入共享行归一化后丢失旧语义：千分位数字、零时长、`N/A`/缺失起点 | `normalize_instance_row` 增加 `number_parser` 与 `allow_zero_duration`；Parser 传入 `_to_float`、允许 0 时长；窗口路径先用 `_row_start` 区分“缺起点（skipped+warning）”与“窗口外”；不再使用合成起点 0 | `test_parser_keeps_number_format_zero_and_missing_timestamp` |
| P1 | Host 并集把相对坐标 `0.0` 当作未初始化 | 用显式 `is None` 判断 `host_seg_start/host_seg_end`，不再 `or start` | `test_host_union_treats_zero_relative_end_as_valid`（两次调用 busy 20µs） |
| P1 | epoch 裁剪上限、相邻 100ns、无窗 busy、Parser 窗口质量仍丢精度 | `window_span(clip)` 用 `available = end - start` 扣减；TASK/CANN 先用整数 ns 减共同原点再换算；无窗 Host 以首个 Host 调用为原点；Parser 的忙碌并集改用窗口相对坐标 | `test_epoch_clip_is_bounded_by_window_end`、`test_epoch_adjacent_ns_events_keep_both_durations`、`test_no_window_single_ns_event_has_busy`、`test_parser_window_quality_uses_relative_busy` |
| P1 | Parser 与 Timeline 复用实例标准化 | Parser 与 `_iter_op_rows` 共用 `normalize_instance_row`（起点/时长/类型/单位/窗口策略）；Parser 保留映射扩展、聚合、额外指标与 `start` 策略，Timeline 保留 `intersect`/`clip` | `test_parser_and_reader_share_epoch_normalization` |

说明：Timeline 的“整任务时长/窗口 busy”历史口径（P2-1）仍未切换，只调整内部相对坐标。

验证：`make check` 0；`pytest` **312 passed, 17 skipped**（329 用例）；`make verify` 0；真实数据 16 passed / 1 skipped。
