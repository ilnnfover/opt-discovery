## Scope

<!-- 这次改动解决什么问题？涉及哪些命令/模块？明确非目标。 -->

- 任务 / 目标：
- 相关命令或模块：
- 非目标：

## Data and schema compatibility

<!-- 是否改变 JSON 字段、单位、质量标记、阈值、证据等级、CLI 参数或 run_manifest？ -->

- [ ] 无 Schema / CLI / 单位 / 证据语义变化
- [ ] 有兼容性变化，已在下方说明迁移与旧数据处理
- [ ] 涉及解析 / Timeline / 对比 / 证据升级 / 阈值 / 采集契约（高风险，必须人审）

说明：

- 数据来源（synthetic / CPU fixture / 真实导出，是否只读）：
- 新增/删除/改名字段：
- 单位或缩放变化：
- 旧产物是否仍可读：

## Validation summary

<!-- 粘贴 verification_summary.json 的路径，并填写实际执行结果。禁止预填 PASS。 -->

摘要文件：`<path>/verification_summary.json`

| 检查 | 实际命令 | 退出码 | 状态（PASS/FAIL/BLOCKED/SKIPPED/NOT_RUN） | 日志/产物 |
|---|---|---|---|---|
| 静态 + validate | `make check` | | | |
| 测试 + synthetic demo | `make verify DEMO_OUT=<new-dir>` | | | |
| 真实数据（如适用） | `make verify-real` 或 `python scripts/verify_real.py` | | | |
| 专项测试 | | | | |

未执行项与原因：

- [ ] 未运行 NPU 实验
- [ ] 未运行完整真实分析
- [ ] 真实数据缺失（BLOCKED / NOT_RUN，不是 PASS）
- [ ] 其他：

## Key semantic artifact differences

<!-- 仅改变分析语义的 PR 需要。使用同一逻辑输入，改动前后各跑一次 analyze，
     产物放到两个临时目录，然后生成差异。差异是评审证据，不是新的质量闸门。 -->

命令：

```bash
python scripts/compare_artifacts.py \
  --before <before-analyze-dir> \
  --after <after-analyze-dir> \
  --out <new-dir>/artifact_diff.json
```

如果使用验证摘要，可直接附加：

```bash
python scripts/verify_summary.py --out <new-dir> \
  --artifacts-a <before-analyze-dir> --artifacts-b <after-analyze-dir>
```

需要说明的变化（没有则写“无”）：

- 关键 JSON 内容（operator_table / diagnosis / timeline / comm / graph_coverage）：
- 关键 summary CSV（workload_summary / operator_summary）：
- Quality Flags（实际嵌套路径 `meta.extra.quality_flags`、run_manifest `quality_flags`）：
- Opportunity `evidence_level` / 受信等级：
- `compare_*.json` 的 `comparable` / `reasons`：
- `run_manifest.json` 的状态、阶段、`comparison_status`：
- 新增/删除的产物或机会：
- 已忽略的非语义噪声（时间戳、run_id、随机 `instance_id`、机器路径）：

## Risk and limitations

- 行为变化及原因：
- 对既有结论/排序/证据等级的影响：
- 未覆盖的输入、平台或数据形态：
- 回滚方式：

## Human review

以下变更必须由人审核后合并，本仓库不启用自动合并：

- [ ] Schema / 单位 / 阈值 / 证据升级 / 采集契约 / 性能结论 / CI 或验证工具变更
- [ ] 已由人复核验证摘要、日志与关键产物差异
