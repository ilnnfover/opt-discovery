---
name: verify-opt-discovery
description: Choose and run opt-discovery verification (make check, make verify, verify-real), produce the five-state machine-readable summary, and review analyze artifacts and before/after semantic differences. Use when changing parsing, timeline, hotspots, diagnosis, opportunities, comparison, communication, graph coverage, source mapping, feedback, thresholds, reports, or verification tooling.
---

# Verify Opt-Discovery

导航优先：先读 [`docs/feature-map.md`](../../../docs/feature-map.md)，只跑与改动相关的入口。
本 skill 不替代 Makefile、`scripts/verify_real.py` 或测试；不安装全局工具。

## 1. 选择入口

| 改动 | 最小验证 |
|---|---|
| 文档、注释、无行为变化 | `make check` |
| 纯 CPU 代码、Schema、Parser、Analyzer、诊断、对比、报告 | `make check` + `make verify DEMO_OUT=<新目录>` |
| 解析 / Timeline / 证据 / 阈值 / 采集契约等分析语义 | 上面两项 + `make verify-real`；缺数据记 `BLOCKED` |
| 只审阅已有产物 | 不重跑；直接检查 `run_manifest.json`、`quality_flags`、证据等级和 compare 状态 |

**不要**为了省事跳过 `make check`/`make verify`；**不要**在缺数据时把 `make verify-real` 写成 PASS。

## 2. 一键摘要（推荐）

```bash
python scripts/verify_summary.py --out <不存在的新目录>
```

- 默认 CPU-only：复用 `make check` / `make verify`，真实数据步骤为 `NOT_RUN`。
- 需要真实回归：加 `--real`；缺数据会得到 `BLOCKED`（退出 2），有用例跳过为 `SKIPPED`（退出 3）。
- 改变分析语义时附加改动前后产物：

```bash
python scripts/verify_summary.py --out <新目录> \
  --artifacts-a <before-analyze-dir> --artifacts-b <after-analyze-dir>
```

- 输出目录必须不存在；摘要和日志在 `<新目录>/verification_summary.json` 与 `logs/`。
- 五态：`PASS` / `FAIL` / `BLOCKED` / `SKIPPED` / `NOT_RUN`。只有实际执行且退出 0 的步骤才是 PASS；
  `make` 子命令失败返回 2 时仍是 FAIL，不是 BLOCKED。
- 摘要失败也保留非零退出；失败先看对应 `logs/<step>.log`。

## 3. 直接命令

```bash
make check
make verify DEMO_OUT=<新目录>/demo
make verify-real                 # 需要 OPT_DISCOVERY_DATA_ROOT；缺数据退出 2
```

`make verify-real` 只是 Make 包装：任何非零子命令（包括脚本以 3 退出的 SKIPPED）都会被
折叠为退出 2。需要区分 0 PASS / 1 FAIL / 2 BLOCKED / 3 SKIPPED 时，直接执行
`python scripts/verify_real.py`；`scripts/verify_summary.py --real` 也是直接调用该脚本。

## 4. 审阅已有产物

按顺序检查：

1. `run_manifest.json`：`status`、`stages[].status/reason_code`、`comparison_status`、`workloads[]`。
2. 每个 Workload 的 `operator_table.json`：`meta.extra.quality_flags`（阻断型标记会使比较失效）；
   缺失指标是 `None`/缺失标记，不是 0。
3. `diagnosis.json`：`summary.blocked_rules`、`candidates_by_level`；规则命中最高 probable。
4. `opportunities.json`：看 `evidence_level` 的**受信等级**（`schemas/evidence_gate.trusted_level_for_mapping`），
   不把原始 `confirmed` 字符串直接当可信结论；检查 `missing_evidence`、`is_synthetic`。
5. `compare_*.json`：`comparable`、`reasons`、`warnings`、`deltas`、`total_ratio`；
   不可比时不得引用速度差。
6. `hotspots.json` / `workload_summary.csv` / `operator_summary.csv`：只读本轮 manifest 登记的文件。
7. `timeline.json` / `comm.json` / `graph_coverage.json`：缺 DB、MSTX、记录数等限制要跟着结论一起报告。

## 5. 前后产物差异

```bash
python scripts/compare_artifacts.py \
  --before <before-analyze-dir> --after <after-analyze-dir> \
  --out <新目录>/artifact_diff.json
```

- 输入只读，输出另放；输出不得位于输入目录内。
- 报告关键 JSON、summary CSV、Quality Flags、Opportunity 受信等级、compare 状态和
  `run_manifest` 状态差异；忽略时间戳、`run_id`、随机 `instance_id` 和机器路径。
- 空目录、非法 JSON/CSV 会 fail closed（`invalid_input` / `read_error`），不是“无差异”。
- 差异是评审证据，不是新的质量闸门；是否合并仍按 AGENTS.md 的高风险人审要求。

## 6. BLOCKED / NOT_RUN 怎么报

| 情况 | 报告方式 |
|---|---|
| `OPT_DISCOVERY_DATA_ROOT` 未设置或四组导出不齐 | `BLOCKED`，退出 2；给出解除条件 |
| 真实测试有用例 skip | 直接脚本/`verify_summary.py --real`：`SKIPPED`，退出 3；`make verify-real` 会把 3 折叠为 2，仍需按日志区分；不是 PASS |
| 未运行 NPU / 完整真实分析 | `NOT_RUN`，写原因；不能声称 PASS |
| 外部源码仓库缺失 | source-map 未命中；核心分析可成功，但 source_refs 为空 |
| 可选步骤失败 | 记录阶段失败与原因；不要把它伪装成核心成功或整体失败 |

如果仓库级 skill 未被自动发现：按 `AGENTS.md` 的“先读哪里”手动打开本文件；
不要复制到全局目录或安装额外组件。
