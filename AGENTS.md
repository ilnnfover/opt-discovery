# AGENTS.md

本仓库把 Ascend Profiler 导出转成**带证据的算子优化机会**，不是自动 Kernel 优化器。
改动先导航，再复现，再实现，最后报告真实执行结果。

## 1. 先读哪里

| 要做什么 | 先读 |
|---|---|
| 找命令 / 实现 / 输入输出 / 测试节点 | [`docs/feature-map.md`](docs/feature-map.md) |
| 查历史缺陷与回归节点（R01–R12） | [`docs/engineering/regression-matrix.md`](docs/engineering/regression-matrix.md) |
| 查基线、已知缺口与环境限制 | [`docs/engineering/baseline-audit.md`](docs/engineering/baseline-audit.md) |
| 改证据 / 比较 / 阈值语义 | [`docs/engineering/phase6/implementation-report.md`](docs/engineering/phase6/implementation-report.md)、[`phase6/migration.md`](docs/engineering/phase6/migration.md) |
| 改代码前想跑什么 | `make check`、`make verify`、必要时 `make verify-real` |
| 验证步骤 / 产物审阅 / BLOCKED 处理 | [`.agents/skills/verify-opt-discovery/SKILL.md`](.agents/skills/verify-opt-discovery/SKILL.md)（仓库级 skill；若工具未自动发现，按本表手动读取，不要安装全局副本） |

不要在未读 Feature Map 的情况下猜命令；不要引用不存在的子命令。

## 2. 开发闭环

1. **核对**：读相关模块、测试和产物形态；记录当前分支、基线、未提交改动。
2. **复现**：先写最小反例或契约测试，证明修改前的行为确实不符合预期。
3. **实现**：复用现有 Schema、Parser、Analyzer、证据闸门；只在职责独立时新增抽象。
4. **验证**：
   - `make check`：Ruff + 架构/领域 Lint + mypy + CLI `validate`。
   - `make verify DEMO_OUT=<新目录>`：普通测试 + 回归 + 属性测试 + synthetic demo。
   - 改变解析/分析/证据/标定语义时，执行 `make verify-real`；它缺数据记录 `BLOCKED`。
     GNU Make 把任何非零子命令折叠为退出 2，所以经 Make 的跳过也表现为 2，不能据此
     区分 BLOCKED 与 SKIPPED。需要原样保留状态时直接运行
     `python scripts/verify_real.py`（0 PASS / 1 FAIL / 2 BLOCKED / 3 SKIPPED）。
   - 需要一份机器可读汇总时：
     `python scripts/verify_summary.py --out <新目录> [--real]`
     它复用 `make check` / `make verify`，并直接执行 `scripts/verify_real.py` 以保留
     0/1/2/3 语义。**输出目录必须不存在**；摘要、日志和差异报告都只写到该新目录内。
5. **报告**：列出实际命令、退出码、日志/产物路径、未执行项及原因。失败保留非零退出。
   没有运行的 NPU、真实数据或完整分析写 `NOT_RUN`，不伪装成功。

## 3. 领域原则（不可用默认值绕过）

- **缺失 ≠ 0**：未采集、未映射、缺分母就是 `None`/缺失标记；不要用 `or 0`、`fillna(0)` 或展示默认值补。
- **来源不可提升**：`measured` / `derived` / `declared` / `assumed` 由数据源和 Schema 决定；
  规则命中最高 `probable`，`confirmed` 必须走 `schemas/evidence_gate.py` 的验证契约。
- **单位只在来源边界换算**：`ns → µs` 等缩放集中在已授权的 parser/adapter；不要二次缩放。
- **图/记录数不是守恒量**：Graph 记录少不等于算子未执行；通信量看按 `globalTaskId` 去重后的字节。
- **质量差/不可比不得出结论**：读取 `meta.extra.quality_flags` 与 `run_manifest.json`；
  阻断型标记存在时 `compare` 不得输出速度差或因果判断。
- **合成数据隔离**：`demo` 与 synthetic fixture 只能自测，不能作为真实采集或性能收益证据。
- **有界处理**：大导出保持流式/分片；不要无意把全部原始行物化到内存。
- **审计留痕**：缺失字段、来源回退、阈值依据、比较拒绝原因必须可机器读取。

## 4. 高风险变更必须人审

以下变更**不得自动合并**，必须由人审核后合并；本仓库不启用自动合并：

- Schema / JSON 字段 / 单位约定 / 质量标记语义；
- 阈值、规则、证据升级与撤回逻辑；
- 采集契约、`run_manifest` 状态与退出码；
- 比较闸门、性能结论或 `confirmed` 相关路径；
- CI 工作流、`Makefile` 检查目标、验证工具本身；
- 修改真实数据路径、采集脚本或任何会接触 NPU 的代码。

PR 使用 [`.github/pull_request_template.md`](.github/pull_request_template.md)，必须填写
scope、数据/Schema 兼容性、验证摘要、关键语义产物差异、风险与限制。

## 5. 安全边界

- 采集与 NPU 只在明确授权时运行；真实导出只读，不改变用户既有工作流。
- 不修改全局配置、远端设置或无关模块。
- 不提交机器绝对路径、密钥、私有模型路径或原始数据。
- 不删除测试、不放宽断言、不扩大 `skip/xfail`、不以文档代替实现。
- 遇到缺数据/缺外部源码：完成可执行部分，明确记录阻塞输入、复现方式和恢复条件。

## 6. 失败汇报

按以下字段汇报，不要只写“已完成”：

| 字段 | 要求 |
|---|---|
| 命令 | 实际 argv，不用“已运行测试”代替 |
| 退出码 | 原样记录；`make` 子命令失败被折叠为 2 时仍记 `FAIL` |
| 状态 | `PASS` / `FAIL` / `BLOCKED` / `SKIPPED` / `NOT_RUN` |
| 日志/产物 | 路径 + 关键字段；验证摘要写 `verification_summary.json` |
| 未执行项 | 明确原因与解除条件（缺数据、缺 NPU、缺外部源码、未选择的步骤） |

产物差异可用 `python scripts/compare_artifacts.py --before <dir> --after <dir> --out <new.json>`
生成；差异是评审证据，不是新的质量闸门。
