# AE-03 实施报告：运行契约与质量闸门

- 任务：AE-03（强化 CLI / Pipeline 运行契约）
- 日期：2026-11-18
- 基线：`main` @ `e7d8aae`（AE-00~AE-02 合并后）
- 本轮范围：R05/R10 质量闸门 + strict CLI + `run_manifest.json`
- 非目标：AE-04（统一数据标准化边界，未实施）

## 1. 目标与验收对照

| 任务书要求 | 本轮实现 | 验证 |
|---|---|---|
| 保留普通模式，新增 strict 或等价验证入口 | `analyze`/`compare`/`check-ranks` 增加 `--strict`，默认行为不变 | `test_run_contract.py`、`test_r05` 双模式断言 |
| 状态 `success/partial/incomparable/failed` 与退出码 | 内部 `status` + 退出码 0/1/3/4（缺输入入口 2） | 6 类端到端 CLI 测试 |
| 不因可选 source-map 缺失宣告核心失败 | `source_map` 阶段 `required=false`，失败仅记录 | `test_optional_source_map_failure_keeps_success` |
| 质量 degraded 不得无条件等同 success/failed | 阻断型（blocking）与提示型（degraded）分离 | `test_degraded_flag_only_warns_...` |
| 比较必须依据 gate，不可比不得输出速度差 | `compare_tables` 先过质量闸门，`deltas=[]` | `test_quality_gate.py` 4 用例 |
| `run_manifest.json`（独立 run_id/阶段/质量/产物/输入/工具） | `experiment/run_manifest.py` | `test_normal_run_is_success_and_manifest_is_verifiable` |
| 防旧输出残留冒充本次输出 | 独立 run_id + 相对路径 + sha256 + `check-manifest` | `test_reused_output_dir_does_not_masquerade_old_artifacts` |
| R05/R10 strict xfail 转正式断言 | `test_known_gaps.py` 删除，新增 `test_quality_gate.py` | 全量测试无 xfail |

## 2. 质量闸门（R05/R10）

### 2.1 契约

新增 `opt_discovery/schemas/quality.py`：

```python
QualityFlag(code, message, severity="blocking"|"degraded", source, details)
```

标记随 `ParseMeta.extra["quality_flags"]` 落盘（JSON 兼容，旧表无该字段视为“未验证”）：

| code | severity | 触发条件 |
|---|---|---|
| `rank_merge_inconsistent` | blocking | 两 rank 同口径 calls/total 超出 0.90–1.10 / 0.80–1.25，或跨口径单次耗时中位数超出 0.70–1.40 |
| `rank_merge_unverified` | blocking | 声明多个 rank 但可解析不足 2 个（或评估异常） |
| `rank_duration_inconsistent` | blocking | timeline 判定「任务数接近但累计时长差数倍」（长 kernel 截断） |
| `rank_scope_mismatch` | degraded | 两 rank 口径不同（窗口 vs 全量），仅可比较单次耗时 |
| `sqlite_fallback` / `window_degraded` | degraded | 既有能力边界（本轮只登记，不新增消费） |

### 2.2 评估与传播

`opt_discovery/analyzer/rank_quality.py`：

- `assess_rank_tables`：与历史 `check-ranks` 完全同口径，返回 `RankMergeAssessment`
  （ratios、中位数、逐 rank 摘要、逐算子行）；
- `assess_bundle`：读取全部 rank（CSV 优先，缺失回退 sqlite），产出标记；
- `rank_quality_from_timeline`：把 `rank_duration_inconsistent` 转成 blocking 标记；
- CLI `check-ranks` 改为调用同一实现（输出文案保持兼容），新增 `--strict`（不一致 → 3）。

`cmd_analyze` 在解析后立即评估并把标记挂到表上，再进入诊断/落盘；`cmd_compare` 与 `analyze`
内部的对比都通过 `compare_tables` 读取标记：

```python
quality_flags = flags_from_meta(a.meta) + flags_from_meta(b.meta)
blocking_flags = blocking(quality_flags)
if blocking_flags:
    comparable = False
    reasons += [f"质量闸门未通过（{flag.code}）：{flag.message}", ...]
```

不可比时 `deltas=[]`，`observations` 明确写「数据质量闸门未通过，不得据此输出速度差或因果结论」；
`to_dict()` 增加 `quality_flags`，落盘后可被下游机器读取。

## 3. 运行契约

### 3.1 退出码

| 状态 | 含义 | strict | 普通模式 |
|---|---|---|---|
| `success` | 必需步骤通过、对比可比 | 0 | 0 |
| `failed` | 缺输入/必需步骤失败且无可解析 Workload | 1 | 1 |
| 缺输入（无 export） | 入口即阻断 | 2 | 2 |
| `partial` | 有产物但必需步骤未完成 | 3 | 0（兼容） |
| `incomparable` | 质量/契约未通过 | 4 | 0（兼容） |

普通模式保持既有行为（≥1 张表即 0），但 manifest 记录真实状态；strict 供 CI/Agent 使用。

### 3.2 `run_manifest.json`

`opt_discovery/experiment/run_manifest.py`（schema 1.0）：

```json
{
  "schema_version": "1.0",
  "run_id": "analyze-20261118T...-ab12cd34",
  "status": "partial",
  "is_synthetic": false,
  "strict": true,
  "stages": [
    {"name": "discover", "required": true, "status": "passed", "artifacts": []},
    {"name": "parse", "required": true, "status": "passed", "artifacts": []},
    {"name": "timeline", "required": true, "status": "failed",
     "reason_code": "timeline_failed", "artifacts": [], "details": {"failed": ["wl"]}},
    {"name": "compare", "required": false, "status": "passed", "artifacts": []}
  ],
  "workloads": [{"workload_id": "...", "status": "partial", "quality_flags": ["rank_merge_inconsistent"]}],
  "quality_flags": ["rank_merge_inconsistent"],
  "comparison_status": "incomparable",
  "artifacts": [{"name": "hotspots.json", "path": "hotspots.json", "bytes": 123, "sha256": "..."}],
  "inputs": {"exports_root": "<data-root>", "export_dirs": ["qwen35-prefill"], "configs": [{"name": "...", "sha256": "..."}]},
  "tool": {"name": "opt-discovery", "version": "0.1.0", "python": "3.12.13", "git_commit": "e7d8aae"}
}
```

要点：

- `run_id` 每次运行唯一；`previous_run_id` 记录同目录上一次运行；
- 产物只记**相对路径 + sha256**；`check-manifest` 校验存在性与哈希；
- 输入不写绝对路径（`exports_root` 记为 `<data-root>`）；
- 阶段状态区分 `passed/failed/skipped/not_requested`；可选步骤失败不改变总体成功。

### 3.3 行为细节

- `analyze` 以前在逐 Workload 异常时 `continue` 且返回 0；现在记录到 `parse_failures` /
  `timeline_failures`，strict 返回 3，manifest 标记 `partial`；
- 解析异常不再让整批崩溃（`parse_error` 记入 workload 与 stage）；
- 无 `msprof_*.db` 时 `timeline` 记为 `skipped / no_db`（之前隐含“成功”）；
- `compare` 读取输入失败不再抛栈，返回 1 + manifest `failed / load_error`；
- `check-ranks --strict` 不一致返回 3（默认仍 0）。

## 4. 测试

新增/调整：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_run_contract.py` | 6 | 正常、缺输入、必需步骤失败（partial）、不可比（4）、可选步骤失败、旧产物/tamper/`check-manifest` |
| `tests/regression/test_quality_gate.py` | 4 | 4x 链路 → compare 阻断；截断标记阻断；degraded 仅警告；CLI strict 4 / 普通 0 + manifest |
| `tests/regression/test_r05_rank_merge.py` | +1 | `check-ranks --strict` → 3，普通 0 |
| `tests/regression/test_known_gaps.py` | 删除 | 原 2 条 strict xfail 转为正式断言 |

全量：**275 passed, 17 skipped**（292 用例，无 xfail）。

## 5. 实际改动

| 文件 | 改动 |
|---|---|
| `opt_discovery/schemas/quality.py` | 新增：`QualityFlag`、severity、`attach/flags_from_meta/blocking/degraded`、稳定 code |
| `opt_discovery/analyzer/rank_quality.py` | 新增：rank 评估（与历史 CLI 同口径）、bundle 评估、timeline 标记转换 |
| `opt_discovery/analyzer/compare.py` | 增加 `quality_flags` 字段与序列化；`compare_tables` 先过质量闸门；不可比说明区分契约/质量 |
| `opt_discovery/cli.py` | `check-ranks` 复用评估 + `--strict`；`analyze` 接入质量标记、`--strict`/`--run-id`/`--manifest`、阶段状态、manifest；`compare` 接入 `--strict` 与 manifest、输入错误可判定；新增 `check-manifest` |
| `opt_discovery/experiment/run_manifest.py` | 新增：状态/阶段契约、run_id、sha256 产物、工具/输入指纹、写盘与校验 |
| `tests/regression/test_quality_gate.py` | 新增：4 条正式断言（取代 xfail） |
| `tests/regression/test_known_gaps.py` | 删除 |
| `tests/test_run_contract.py` | 新增：6 条 CLI 契约用例 |
| `docs/engineering/regression-matrix.md` | R05/R10 → enforced；AE-03 说明 |
| `docs/engineering/baseline-audit.md` | D6/D7 关闭；INV-05 调整；§10 关闭项 |
| `README.md` | strict 退出码、manifest、质量闸门说明 |

## 6. 验证记录

环境：Python 3.12.13（主）；3.11.6 复验；pytest 9.2.0（3.10/3.11 环境）与 8.3.2（系统）。

| 检查 | 退出码 | 结果 |
|---|---|---|
| `make check`（ruff + mypy + validate） | 0 | 51 source files，0 类型错误 |
| `python -m pytest` | 0 | 275 passed, 17 skipped（292 用例） |
| `python -m pytest tests/regression` | 0 | 67 passed |
| 真实数据 `tests/test_real_data.py` | 0 | 16 passed, 1 skipped（回填场景显式 skip） |
| `python scripts/verify_real.py --expect-blocked` | 0 | 缺数据 BLOCKED 闸门 |
| 3.11 全量 | 0 | 275 passed, 17 skipped |

未执行：真实四组导出的完整 `analyze`（解析 + timeline 成本高、输出体积大）；AE-03 的契约用
合成导出结构（workload.log + op_summary + sqlite 回填形态）覆盖，真实解析语义由 AE-02 的真实用例
单独保护。

## 7. 兼容性与行为变化

1. **默认行为不变**：`analyze`/`compare`/`check-ranks` 不加 `--strict` 时退出码与之前一致；
   `analyze` 仍写原有产物路径，新增 `run_manifest.json`。
2. `compare` 报告中新增 `quality_flags` 字段（只增不改）；`deltas` 在阻断时为空数组。
3. `analyze` 对解析异常的处理从「直接抛栈中断整批」改为「记录失败并继续」；
   strict 返回 3，普通模式返回 0（与之前“跳过继续”的语义一致，但可审计）。
4. `ParseMeta.extra["quality_flags"]` 为新增字段；旧 JSON 无该字段时按“未验证”处理，不阻断。
5. `check-manifest` 为新增子命令；不改变既有命令。

## 8. 未完成 / 剩余风险

| 项 | 说明 | 归属 |
|---|---|---|
| `compare --strict` 不强制要求 quality_flags 存在 | 避免破坏手工 JSON 工作流；绕过 `analyze` 的构造仍可“未验证” | AE-07（证据流转契约） |
| `analyze` 内部对比未应用 `check-runs` 的采集契约 gate | 仅用 `comparable_with` + 质量标记；契约 gate 仍是独立命令 | AE-07 |
| 质量标记只覆盖 rank 合并与时长截断 | `sqlite_fallback`/`window_degraded` 仍是 degraded，不阻断 | 视需求 |
| `check-ranks` 未写 manifest | 任务是“比较入口”，rank 校验属于诊断工具 | AE-03 之后按需 |
| 真实导出完整 analyze 未跑 | 成本/体积；以合成契约测试 + 真实解析测试分工 | AE-06 |
| AE-04 未实施 | 本轮范围外 | AE-04 |

## 9. 提交

| 项 | 值 |
|---|---|
| 分支 | `feat/ae-03-run-contract`（基于 `main` @ `e7d8aae`） |
| 提交 | 见 `git log -1`（独立提交，不混入 AE-00~AE-02 历史） |
| 远程 | 未 push（待确认） |

## 10. 复审问题修复（2026-11-19）

复审报告：`docs/engineering/phase2/review-report.md`（FAIL）。逐项处置：

| 编号 | 问题 | 修复 | 回归 |
|---|---|---|---|
| P1-1 | strict 假成功：缺日志/缺导出/timeline 缺 DB 仍 success | 按 workload YAML 枚举应存在的导出（`missing_export`）；缺日志 `missing_run_log`、窗口/rank `window_or_rank`、缺 DB `timeline:missing_db` 均计入 partial；无导出时 parse 阶段 `not_requested`；状态从阶段结果派生 | `test_missing_workload_log_...`、`test_missing_export_dir_...`、`test_failed_timeline_...` |
| P1-2 | rank 期望值取自“发现的 rank”，多 rank/部分 DB 未覆盖 | `expected_ranks = hardware.device_count`；声明与实际不符 → `rank_merge_unverified`（blocking）；任一 rank 解析问题保留；>2 rank 逐一与基线比较；部分 DB → `rank_timeline_incomplete`（blocking） | `test_declared_tp_rank_missing_...`、`test_partial_timeline_db_...`、`test_more_than_two_ranks_...` |
| P1-3 | 比较入口绕过采集契约 | `compare_tables` 内嵌 `compare_contracts`（与 `check-runs` 同一实现）；`incomparable` → `comparable=False` + 原因，strict 退出 4 | `test_contract_mismatch_blocks_compare`、`test_incomparable_analysis_returns_4` |
| P1-4 | 不可比仍输出 `total_ratio` 与终端比值 | `to_dict` 仅在 `comparable=True` 时给 `total_ratio`；`_fmt_ratio` 不可比返回 `-` | `test_incomparable_analysis_returns_4`（JSON + `total_ratio=-`） |
| P1-5 | 旧 timeline/旧 workload 进入本轮清单与 CSV | 产物只登记**本轮实际写出**的路径；`write_summaries(include=selected_ids)` 只汇总本轮成功 workload | `test_reused_output_dir_does_not_masquerade_old_artifacts`（真实同名 timeline + 缺日志第二轮） |
| P1-6 | 配置/写出失败后保留旧 success 清单 | 配置加载、诊断、写出、比较各自结算；失败必写本轮 failed/partial 清单（覆盖旧 run_id） | `test_config_failure_replaces_previous_success_manifest`、`test_artifact_write_failure_is_settled`、`test_parse_error_details_are_redacted` |
| P1-7 | 非法 severity 被静默丢弃 | `parse_meta_flags` 返回问题列表；存在但不可解析 → `quality_flag_invalid`（blocking）；缺 severity 的已知阻断码按 blocking | `test_invalid_quality_flag_is_blocking`、`test_known_blocking_code_without_severity_is_blocking` |
| P2-1 | manifest 写在输出目录外时校验失败；空 JSON 返回 0 | 产物路径改为相对**清单父目录**（允许 `../`）；`verify_manifest` 校验 schema_version/status/run_id/artifacts 结构 | `test_custom_manifest_path_outside_out_dir_is_verifiable`、`test_empty_manifest_is_rejected` |
| P2-2 | 输入指纹不足、错误详情含绝对路径 | compare 记录 `a/b` 的 sha256；analyze 指纹覆盖 workload YAML/映射/规则/阈值/设备档案；错误详情把数据根/工作目录替换为 `<path>` | `test_compare_cli_strict_refuses_incomparable`（sha256）、`test_parse_error_details_are_redacted` |
| P2-3 | 质量测试手工挂标记，未锁定传播 | 新增 `test_quality_end_to_end.py`：真实 CSV+log+db → analyze → 落盘 JSON → 独立 compare 读盘并退出 4 | `test_rank_and_duration_flags_propagate_to_compare` |
| P3-1 | `quality_flags` 插在旧字段中间，位置参数有风险 | 移到 `ComparisonReport` 末尾（`generated_at` 之后）；仓库内构造均为关键字参数 | 全部 compare 测试 |

验证（3.12 / 3.11）：`make check` 0；`pytest` **246 passed, 17 skipped**（263 用例，67 个回归）；
真实数据 16 passed / 1 skipped；`verify-real --expect-blocked` 0。

## 11. 第二轮复审修复（F1/F2，2026-11-19）

复审基线 `7d10937`。逐项处置：

| 编号 | 问题 | 修复 | 回归 |
|---|---|---|---|
| **F1-1** | 已知阻断码可被 `severity=degraded` 降级 | `QualityFlag.__post_init__`：`code` 属于 `KNOWN_BLOCKING_CODES` 时 severity 必须为 blocking，否则抛错；`from_dict` 解析时冲突 → `quality_flag_invalid`（blocking） | `test_known_blocking_code_cannot_be_downgraded_in_json` |
| **F1-2** | timeline 失败只记数在 manifest，表无标记，独立 compare 仍 0 | timeline 缺 DB/执行异常 → `timeline_unavailable`（blocking）随表落盘；比较入口读取后 `comparable=False` | `test_timeline_failure_makes_table_uncomparable`（损坏主 DB + 独立 compare 4） |
| **F1-3** | 同一 workload 的旧 `comm.json` 进入本轮 CSV | `write_summaries(..., artifacts=本轮清单路径)`，只读取本轮登记过的可选产物 | `test_stale_comm_is_not_used_in_summary` |
| **F1-4** | 发现异常沿用旧清单；compare 输入目录抛栈；配置失败 parse 误记 passed | `discover_all` 包失败结算（`discover_error`）；compare 的 mkdir/读取/执行/写出全部结算并写本轮 failed 清单；`_analyze_stages` 增加 `failure_stage`：配置失败时 parse 及之后 `not_requested` | `test_discover_error_settles_manifest`、`test_compare_input_directory_settles_manifest`、`test_config_failure_replaces_previous_success_manifest`（含 parse 断言） |
| **F2-1** | compare 的清单仍相对 out_dir；缺 sha256 可通过 | compare 与 analyze 统一以**清单父目录**为 artifact 根；`verify_manifest` 要求每项为对象且 sha256 必填 | `test_compare_custom_manifest_and_missing_sha` |
| **F2-2** | compare 的 load_error 写入原始绝对路径 | 抽出 `_redact_error(exc, roots)`，两个入口共享；compare 的 load/执行错误均脱敏 | `test_compare_input_directory_settles_manifest`（断言 `<path>` 且无 tmp 路径） |
| **F2-3** | 显式 `--skip-timeline` 仍做 DB 覆盖检查，导致 status 与阶段冲突 | 跳过时不再检查 DB 覆盖，改为 `timeline_skipped`（degraded）；仅为“必须执行 timeline”结算失败 | `test_skip_timeline_with_tp2_is_not_failure` |

### 为什么上一轮没有解决

| 现象 | 上一轮的做法 | 缺口 |
|---|---|---|
| F1-1 降级绕过 | 只在 `severity` 缺失时按 code 默认 blocking | 把 severity 当作可信输入，没有“code 决定下限”的约束 |
| F1-2 timeline 失败不可判定 | 失败只写入 manifest 的 stage/详情 | 没有把“时间线不可用”转成随表落盘的质量状态；消费端只看表 |
| F1-3 旧 comm 进 CSV | 汇总按 workload 目录过滤（`include`） | 仍按目录读文件，未按本轮产物清单读；可选产物残留未被发现 |
| F1-4 旧清单/误记 passed | 只覆盖了“单 workload 写失败”和“缺映射”两条路径 | `discover_all`、compare 的 mkdir/执行/写出未纳入结算；`_save` 没有“阶段未执行”的概念 |
| F2-1 清单根目录 | analyze 已改为相对清单目录 | compare 仍用 `out_dir`，两个入口不一致；校验只在 sha 存在时比较 |
| F2-2 路径泄漏 | analyze 有脱敏，compare 直接用 `str(exc)` | 脱敏逻辑未共享 |
| F2-3 跳过冲突 | 无条件执行 DB 覆盖检查 | 没有区分“用户显式跳过”与“必需 timeline 失败” |

共性原因：上一轮把“代码路径已修”当作“消费端可判定”，缺少“落盘产物 → 独立进程重读”的反例。
本轮为每条 F 项都补了读取磁盘/跨进程的断言，并用变异验证确认断言是承重的。

验证（3.12 / 3.11）：`make check` 0；`pytest` **243 passed, 17 skipped**（260 用例，67 个回归）。

## 12. 最终增量复核修复（2026-11-19）

| 级别 | 问题 | 修复 | 回归 |
|---|---|---|---|
| P1 | `--out`/`--out-dir` 指向普通文件时，初始化失败直接返回 1，旧 success 清单原样保留 | 两个入口先解析清单路径与 run_id，再建目录；失败分支写入本轮 `failed` 清单（阶段 `output` / `output_dir_error`，详情脱敏），覆盖旧 run_id | `test_output_dir_failure_settles_manifest`、`test_compare_output_dir_failure_settles_manifest` |
| P1 | `--manifest` 与 compare 报告/输入同路径时，报告先写、清单后覆盖，命令仍返回 0 | 写入前校验清单路径不得与报告产物或输入重合；冲突时拒绝，返回 1，并把 failed 清单写到安全的 `out_dir/run_manifest.json`（若也冲突则放弃写入并报错） | `test_compare_manifest_cannot_overwrite_report` |
| P2 | `--skip-timeline` 时逐 workload 状态被任意 quality code 判为 partial | 只有必需步骤失败或**blocking** 标记才置 partial；degraded（`timeline_skipped`）保持 success，与 run 级状态一致 | `test_skip_timeline_with_tp2_is_not_failure`（补 workload 状态断言） |

### 为什么上一轮没有解决

| 现象 | 上轮做法 | 缺口 |
|---|---|---|
| P1 输出目录失败 | 失败结算写在 mkdir 之后（`_save`/`_finish` 只覆盖后续步骤） | 初始化顺序在前，mkdir 失败时还没有 run_id/清单目标，直接 return |
| P1 清单路径冲突 | 已把 artifact 根统一到清单父目录 | 只改了“相对谁”，没有校验清单路径本身与产物/输入是否重合 |
| P2 逐 workload 状态 | 修了 run 级状态（success/partial 由失败列表派生） | workload 级仍用“任意 quality code”判断，未区分 blocking/degraded |

验证（3.12 / 3.11）：`make check` 0；`pytest` **275 passed, 17 skipped**（292 用例）。

## 13. 合并前复验修复（2026-11-19）

| 级别 | 问题 | 修复 | 回归 |
|---|---|---|---|
| P1 | compare 的 `--manifest` 与输入表重合时，**失败路径**（`--b` 不存在、`--out-dir` 是普通文件）仍把输入表覆盖为 failed 清单 | 入口先建立 `input_reserved`，所有写清单的路径（输出目录失败、读表失败、成功路径）统一经 `_safe_manifest_target()`：与输入/产物冲突则退到同目录 `run_manifest.json`，否则拒绝写入；读表失败前不再直接 `_finish` | `test_compare_load_failure_never_overwrites_input`、`test_compare_output_failure_never_overwrites_input`（断言输入字节不变） |
| P1 | analyze 的 `--manifest` 指向 workload 的 `operator_table.json` 时，回退清单写到输出根但产物路径仍按原子目录计算，命令返回 0 且 `check-manifest` 失败 | 检测到清单与产物重合即**拒绝**，把 failed 清单（`manifest_path_conflict`）写到 `out/run_manifest.json` 并返回 1；不再发布 success | `test_analyze_manifest_cannot_overwrite_workload_table` |

### 为什么上一轮没有解决

| 现象 | 上轮做法 | 缺口 |
|---|---|---|
| 失败路径覆盖输入 | 冲突校验放在读表成功、`mkdir` 成功之后 | `_output_dir_failure` 与 `load_error` 分支在冲突校验之前写清单；没有“输入保留集”这一入口级约束 |
| analyze 回退清单不可校验 | 冲突时把清单改写到 `out/run_manifest.json`，仍按原清单父目录收集产物并返回 success | 只改了写入位置，没有同步 artifact 根，也没有把冲突判为失败 |

现在规则统一为：**清单不得与输入或本轮产物重合**；有冲突时优先退到同目录 `run_manifest.json`，无法安全落盘则不写并返回非零，绝不覆盖输入。

验证（3.12 / 3.11）：`make check` 0；`pytest` **275 passed, 17 skipped**（292 用例）。

## 14. 合并前问题总表 · 批次 1（A1/A2/B1/B4）

按「输入与清单安全」一组处理，不改变质量语义（A3/A4/A5/A6 留批次 2/3）。

| 编号 | 问题 | 修复 | 回归 |
|---|---|---|---|
| A1 | 固定 `<target>.tmp` 会截断同名输入；`resolve()` 不识别硬链接/符号链接；产物符号链接可覆盖输入 | `write_manifest` 改为同目录 `tempfile.mkstemp` + `os.replace` + `fsync`；新增 `_same_file()`（`os.path.samefile` + resolve 兜底）统一判重；analyze 建立 `protected_inputs`（workload YAML、映射、规则、阈值、设备档案、`workload.log`、op_summary、op_report、`msprof_*.db`），写入前 `_guard_output()` 先移除旧符号链接再判重；清单冲突经 `_manifest_target()` 选安全路径 | `test_manifest_tmp_name_does_not_clobber_input`、`test_report_hardlink_to_input_is_rejected`、`test_analyze_manifest_cannot_overwrite_config_yaml`、`test_symlinked_artifact_does_not_clobber_input` |
| A2 | 拒绝路径直接 `return 1`，旧 success 清单仍有效 | 报告路径冲突也写本轮 failed 清单（新 run_id，`report_path_conflict`）；清单冲突写 `manifest_path_conflict` | `test_report_hardlink_to_input_is_rejected`（断言 run_id 变化） |
| B1 | 缺 `stages`/数字 `path`/顶层数组可通过或抛 traceback | `verify_manifest` 校验顶层类型、schema_version、status、run_id、stages（name/required/status）、workloads、artifacts（path 字符串且非绝对、sha256 必填、bytes 类型）；success 要求有产物且必需阶段 passed；读取/哈希异常转为问题项 | `test_check_manifest_rejects_bad_shapes`、`test_empty_manifest_is_rejected` |
| B4 | 重复 `workload_id` 互相覆盖并自比 | 发现前检测重复，失败并记 `duplicate_workload_id`（含 duplicates 列表） | `test_duplicate_workload_ids_are_rejected` |

### 为什么上一轮没有解决

| 现象 | 上轮做法 | 缺口 |
|---|---|---|
| A1 临时文件截断输入 | 只把清单改为原子 `replace`，临时名固定为 `<target>.tmp` | 没有考虑“临时文件名本身可能是用户输入”，也没做独占创建 |
| A1 链接身份 | 全部使用 `Path.resolve()` 判重 | `resolve()` 只解析路径，不识别硬链接；符号链接产物在写入时仍跟随到目标 |
| A2 拒绝不结算 | 冲突分支先 `print` 再 `return 1` | 认为“拒绝即安全”，忽略了旧 success 清单会被下游读取 |
| B1 校验边界 | 只校验 `artifacts` 项与部分顶层字段 | 没有要求 `stages` 存在/类型正确，也没有 success 的完成态约束 |
| B4 重复 ID | 假定每个 YAML 的 `workload_id` 唯一 | `discover_all` 用字典按 ID 合并，重复时静默覆盖 |

验证（3.12 / 3.11）：`make check` 0；`pytest` **275 passed, 17 skipped**（292 用例）。

## 15. 合并前问题总表 · 批次 2（A3/A4/A5/B3）

| 编号 | 问题 | 修复 | 回归 |
|---|---|---|---|
| A3 | 空窗口、缺轮次、低覆盖仍发布 success；独立 compare 空表也出 deltas | analyze 依 `window_quality` 生成 blocking 标记：`empty_window`、`missing_rounds`、`low_round_coverage`，随表落盘；`compare_tables` 对任一侧 `records` 为空追加 `empty_table`（blocking） | `test_empty_window_is_blocking`、`test_missing_round_is_blocking`、`test_low_round_coverage_is_blocking`、`test_empty_table_is_not_comparable` |
| A4 | `quality_flags={}` 被当缺字段；合成/实测混用只单向警告；`verified=false` 仍可比 | `parse_meta_flags` 区分“缺字段”与“存在但类型错误”（`{}` → `quality_flag_invalid`）；compare 增加 `synthetic_mismatch`（双向）与 `verified_false`（blocking） | `test_quality_flags_empty_mapping_is_invalid`、`test_synthetic_and_measured_mix_is_blocked`、`test_unverified_workload_is_not_comparable` |
| A5 | 全局 scope 让第三个 full rank 把同口径 pair 降级；check-ranks 不校验声明 rank 数 | `assess_rank_tables` 改为 **per-pair** scope，并接受 `expected` 核对声明数；`check-ranks` 传入 `hardware.device_count`，输出「声明/可解析」 | `test_mixed_scope_does_not_downgrade_same_scope_pair`、`test_check_ranks_uses_declared_rank_count` |
| B3 | 可选 comm 失败只在终端提示 | 新增 `comm` 阶段（required=false）：`failed/comm_error`、`passed`、`skipped/no_db`，失败详情进清单 | `test_optional_comm_failure_is_recorded` |

### 为什么上一轮没有解决

| 现象 | 上轮做法 | 缺口 |
|---|---|---|
| A3 空窗口/缺轮次 | parser 只设置 `window_quality.degraded` 与 warning，analyze 照常 success | 质量只停留在“分析内提示”，没有转成随表落盘的 blocking 状态，独立 compare 读不到 |
| A4 `{}` | `(extra.get("quality_flags") or ())` 把空 dict 当缺字段 | 没区分“字段不存在”与“字段存在但类型错误” |
| A4 合成/verified | 只对 `not a.is_synthetic and b.is_synthetic` 单项 warning；`comparable_with` 从未读 `verified` | 缺少双向与 blocking 规则；README 的 verified 契约未落到代码 |
| A5 全局 scope | `same_scope = len({scopes}) == 1` 作用于全部 rank | 多 rank 时一个 full 表就会让同口径 pair 走中位数弱校验 |
| B3 可选步骤 | comm 失败只 `print` | 清单没有对应阶段，无法区分“未请求/缺数据/失败” |

验证（3.12 / 3.11）：`make check` 0；`pytest` **275 passed, 17 skipped**（292 用例）。

## 16. 合并前问题总表 · 批次 3（A6/B2/B5/B6/B7）

| 编号 | 问题 | 修复 | 回归 |
|---|---|---|---|
| A6 | 独立 `summary`/`report` 扫描目录，读回旧 `comm.json`/旧 timeline，并破坏清单 CSV 哈希 | 两个入口优先读 `run_manifest.json` 的 artifact 白名单与 workload 列表；`write_summaries`/`build_report` 接受 `artifacts`；`summary` 重建 CSV 后调用 `refresh_manifest_artifacts` 同步 sha256；无清单时保留 legacy 扫描并提示 | `test_downstream_summary_and_report_ignore_stale_artifacts` |
| B2 | 无导出时 source_map/comm 记 passed；compare 写失败时顶层 not_requested；缺映射无阶段错误码；多对比较状态被最后一次覆盖 | 新增 `config` 阶段（`config_error`），`early/later_executed` 同时依赖 `discover_ok`；compare 写失败显式 `comparison_status=failed`；analyze 收集逐对结果后派生总体状态（failed > incomparable > success > descriptive） | `test_config_failure_marks_config_stage`、`test_no_export_marks_optional_stages_not_requested`、`test_compare_write_failure_sets_failed_status` |
| B5 | compare 只传 workload+meta，契约读取 0 条 records，丢失 operator_set/work.count 警告 | `compare_contracts` 增加 `operator_counts` 摘要读取；`compare` 传算子→calls 映射；契约 warnings 进入 `ComparisonReport.warnings` | `test_compare_contract_sees_operator_counts` |
| B6 | 默认四组会生成跨阶段配对，使 strict 恒为 4 | analyze 将自动跨阶段配对标记为 `descriptive`（写报告、不影响 strict 状态与退出码），新增 `comparison_status=descriptive`；同阶段配对仍严格把关 | `test_cross_phase_pairs_are_descriptive_only` |
| B7 | 已登记产物在写清单前消失会被静默跳过；配置指纹静默跳过缺失文件 | `collect_artifacts` 缺失即 `FileNotFoundError`，analyze/compare 转为 failed 清单（`artifact_missing`）；`fingerprint_configs` 缺失即失败；配置指纹在加载后立即计算 | `test_collect_artifacts_and_fingerprints_fail_closed` |

### 为什么上一轮没有解决

| 现象 | 上轮做法 | 缺口 |
|---|---|---|
| A6 下游读旧产物 | 修了 analyze 内部汇总（`include`+本轮 artifacts） | 独立 `summary`/`report` 命令没有接入清单；重建 CSV 也不同步哈希 |
| B2 阶段不一致 | 只修了“单 workload 写失败”和“拒绝路径” | `_analyze_stages` 的 `failure_stage` 未覆盖 `discover_ok=False` 与 config；compare `_finish` 对 report=None 一律 not_requested；多对比较取最后一次写入而非汇总 |
| B5 契约证据 | 只把 workload+meta 传给 `compare_contracts` | 契约依赖 records 计算算子集合/调用量，摘要缺失导致检查退化为空集 |
| B6 跨阶段 | 默认 `_comparison_pairs` 生成同模式跨阶段对，契约判不可比计入 strict | 没有区分“自动探索性配对”与“用户显式比较” |
| B7 产物缺失 | `collect_artifacts` 对缺失文件 `continue` | 静默跳过会让 success 清单缺少产物；配置指纹同样静默 |

B8（同目录并发/中断）本轮不修：单用户 CLI 无锁语义，已在 README 明确“同一输出目录不支持并发”，需要并发请使用独立目录。TOCTOU 的极端窗口仍属已知限制。

验证（3.12 / 3.11）：`make check` 0；`pytest` **275 passed, 17 skipped**（292 用例）。

## 17. 输入保护与下游清单复验修复（2026-11-19）

| 级别 | 问题 | 修复 | 回归 |
|---|---|---|---|
| P1 | 采集输入只在 discover 之后登记；重复 ID、发现失败、输出目录失败等早期路径仍可能用 failed 清单覆盖 CSV/DB/日志 | 新增 `_protect_export_inputs()`：按 workload YAML 的 `export_dir` glob `workload.log`、`op_summary*.csv`、`op_report*.csv`、`*.db` 并登记；重复 ID、发现异常在实际写清单前调用；输出目录失败分支尽力加载 YAML 后同样保护 | `test_early_failure_does_not_overwrite_export_csv`（断言 CSV 字节不变） |
| P1 | 下游 `summary`/`report` 只认 `<out>/run_manifest.json`：外置清单看不见、旧默认清单会读回旧 4 MiB、损坏清单静默回退目录扫描 | 两个命令新增 `--manifest` 与 `--allow-legacy`；显式/默认清单存在但无效即返回 1；无清单默认拒绝扫描，只有 `--allow-legacy` 才按旧目录扫描；产物路径统一以**清单父目录**为根解析；CSV 重建后仍同步清单哈希 | `test_external_manifest_drives_downstream_commands`、`test_corrupt_or_missing_manifest_is_not_silently_ignored` |

### 为什么上一轮没有解决

| 现象 | 上轮做法 | 缺口 |
|---|---|---|
| 早期失败覆盖采集输入 | 先 discover 再登记 op_summary/DB/log；早失败分支只保护配置文件 | 登记顺序晚于重复 ID/发现/输出目录失败的发生点；`_manifest_target` 的“输入集”里没有采集文件 |
| 外置清单不可见 | 下游只读默认 `<out>/run_manifest.json` | 没有 `--manifest` 入口；外置清单路径（`../`）无法用 out 目录相对路径匹配 |
| 损坏清单静默 legacy | `artifact_allowlist` 失败返回 `None`，下游继续扫目录 | “清单无效”与“从未有清单”被混为一谈；缺失清单时自动 legacy 扫描会读回旧产物 |

验证（3.12 / 3.11）：`make check` 0；`pytest` **275 passed, 17 skipped**（292 用例）。
