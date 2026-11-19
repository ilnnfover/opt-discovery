# AE-05 实施报告：架构依赖与领域 Lint

日期：2026-11-19
基线：`main` @ `f0fb206`（AE-04 合并后）
分支：`feat/ae-05-arch-lint`
范围：仅 AE-05；未开发 AE-06。

## 1. 目标

任务书要求把容易重复违反的架构/领域约定变成确定性检查：

- 核心 Schema/领域类型不依赖 CLI/report/discovery；
- diagnosis 不直接解析原始 Profiler 数据；
- report 不重新诊断、不改证据；
- discovery 不绕过公共数据契约；
- 单位转换、证据升级、缺失指标、异常吞没、机器路径、阈值来源可被静态检查；
- 规则有编号、文件位置和正确 API 提示；
- 正例、反例与必要的误报测试；
- 接入 `make check` 和 GitHub CI。

## 2. 依赖审计

| 模块 | 实际依赖 | 结论 |
|---|---|---|
| `schemas/` | 仅标准库 + 同层 schema + `yaml`/`json` | 无向上依赖，可加硬约束 |
| `diagnosis/` | schemas、yaml、pathlib | 不依赖 parsers/collectors/events |
| `report/` | 标准库、generator/summary_csv | 不依赖 diagnosis/parsers/collectors |
| `discovery/` | diagnosis、schemas | 不依赖 parsers/collectors/CLI |
| `analyzer/` | schemas、primitives、events、parsers 常量 | 作为分析层允许读取标准化事件 |
| `cli.py` | 各层公共 API | 编排层，无 csv/sqlite 直接解析 |

已有工程检查：Ruff、mypy、`validate`、pytest（3.10/3.11/3.12）、真实数据闸门。AE-05 只新增轻量 AST/YAML 检查器，不引入 import-linter。

## 3. 实现

### 3.1 检查器

`tools/arch_lint.py`（标准库 AST + PyYAML）：

- 解析 `opt_discovery/**/*.py`，解析相对/绝对 import 到完整模块名；
- `configs/quality/arch_lint.yaml` 配置架构规则、OD002 授权适配器、OD005 例外；
- 诊断格式：`RULE path:line: message [hint: 正确 API]`，退出码 1。

### 3.2 架构规则

| 规则 | 范围 | 约束 |
|---|---|---|
| `ARCH:core-schemas` | `opt_discovery/schemas/` | 禁止导入 cli/report/discovery/diagnosis/analyzer/collectors/parsers/experiment/source_analysis |
| `ARCH:diagnosis-no-raw-profiler` | `opt_discovery/diagnosis/` | 禁止导入 parsers/collectors/events |
| `ARCH:report-read-only` | `opt_discovery/report/` | 禁止导入 diagnosis/discovery/parsers/collectors/events |
| `ARCH:discovery-no-raw-profiler` | `opt_discovery/discovery/` | 禁止导入 parsers/collectors/events/cli/report |

### 3.3 领域规则

| 编号 | 检测 | 范围/误报控制 |
|---|---|---|
| **OD001** | `Metric(... or 0)` 把缺失值静默变成 0 | 只匹配 `Metric` 构造的 value 参数；`Metric(0.0, ..., MEASURED)` 合法 |
| **OD002** | 直接 `* 1000` / `/ 1000` 单位换算 | 仅 BinOp，跳过 f-string 展示；`events.py`/`primitives.py`/timeline/parsers/collectors/units 及 report 的 ms 展示列授权 |
| **OD003** | diagnosis/discovery/report 出现原始 Profiler 数据源标识 | 字符串常量中的 `op_summary`/`op_statistic`/`mindstudio_profiler_output`/`msprof_`；不禁止合法报告文案 |
| **OD004** | 直接改写 `severity/level/verdict` 或 `quality_flags/evidence` 容器 | 只匹配赋值目标；允许 `schemas/quality.py` 内部实现 |
| **OD005** | 宽泛 `except Exception: pass` | 仅 `Exception`/`BaseException`/裸 except 且 body 只有 `pass`；`OSError` 清理不报；CLI 尽力保护路径登记理由 |
| **OD006** | 硬编码 `/root/`、`/home/`、`/tmp/`、盘符路径 | 仅生产代码字符串常量；测试/文档不在扫描范围 |
| **OD007** | 阈值缺少 `provenance` 或 `basis/source` | 只检查 `configs/rules/*.yaml`、`configs/thresholds/*.yaml` 的 `thresholds` 项 |

### 3.4 例外

| 例外 | 原因 |
|---|---|
| `opt_discovery/report/summary_csv.py` 的 `/1000` | 输出 `max_gap_ms` 展示列，不是 profiler 原始单位换算 |
| `opt_discovery/cli.py` 的 `except Exception: pass` | 早期输入保护的最佳努力路径，原始错误已先打印，登记在 `od005_allow` |

无全局 ignore；例外均文件级并带理由。

## 4. 接入

- `make arch-lint`：`python -m tools.arch_lint --config configs/quality/arch_lint.yaml`
- `make check`：`lint` + `arch-lint` + `typecheck` + `validate`
- `.github/workflows/ci.yml` static job 新增 Architecture/domain lint 步骤
- 测试 job 继续通过 `make check` 复用同一入口

## 5. 测试

`tests/test_arch_lint.py`（34 条）：

- 当前仓库必须 0 违规；
- 四条架构规则各一个反例，schema 相对导入合法；
- OD001 反例 `Metric(x or 0)` + 合法 `Metric(0.0, MEASURED)`；
- OD002 反例 `ns * 1000`，f-string 与授权适配器合法；
- OD003 报告直接引用原始源文件被拒；
- OD004 直接改写 severity 被拒；
- OD005 宽泛 `except: pass` 被拒，窄 `OSError` 合法；
- OD006 机器路径被拒；
- OD007 缺 provenance/basis 被拒，登记完整合法。

另做真实违规探针：临时在 `analyzer/` 写入 `ns * 1000`，`make arch-lint` 退出 2 并报 OD002；删除后恢复 0。

## 6. 验证

| 检查 | 结果 |
|---|---|
| `make check` | 0（含 arch-lint） |
| `pytest` | **352 passed, 17 skipped**（369 用例） |
| `make verify DEMO_OUT=/tmp/ae05-demo` | 0，352 passed / 17 skipped |
| 真实数据 `tests/test_real_data.py` | 16 passed, 1 skipped |
| 临时违规探针 | arch-lint 退出 2；清理后 0 |
| `git diff --check` | 0 |

## 7. 兼容性

- 未修改运行时行为、CLI 参数或 Profiling 数据契约；
- 仅新增 `tools/arch_lint.py`、配置、测试、Make/CI 步骤和文档；
- 现有 Ruff/mypy/validate/pytest 入口不变，`make check` 仅增加一步。

## 8. 已知限制

- 规则是语法级检查，不做类型流/跨函数分析；OD001/OD004 只覆盖明确的构造/赋值形态。
- OD003 基于原始数据源标识字符串，不追踪 `Path` 拼装。
- OD002 通过文件授权，不解析单位语义；新增授权适配器需显式登记。
- OD007 只检查 YAML 阈值；设备档案 JSON 的 `peak_*` 字段不在任务书“阈值来源”范围内。
- 未引入 import-linter；模块依赖检查为文件级 import 前缀。
- 未跑四组真实导出的完整 `analyze`；NPU 未运行。

## 9. 复审修复（2026-11-19）

依据 `docs/engineering/phase4/review-report.md`：

| 问题 | 修复 | 回归 |
|---|---|---|
| P1-1 ARCH 漏基础领域与报告跨层入口 | `core-schemas` 增加 `opt_discovery/primitives.py`；report 禁止 `opt_discovery.cli`、`opt_discovery.experiment.feedback`；相对导入也解析后检查 | primitives 导入 report、report 相对导入 cli/feedback 均失败；同语句去重 |
| P1-2 OD003 漏 SQLite/CLI raw、误伤报告文案 | 受限制模块检查 `sqlite3` / `csv` 导入、`.connect`、`op_counter.db`/`op_report`/`msprof_`/`mindstudio_profiler_output`；CLI 追加 SQL `TASK` 检测；report 允许 `op_summary` 来源文案 | diagnosis `sqlite3.connect('op_counter.db')`、CLI `from TASK` 失败；报告来源说明通过 |
| P1-3 OD004 漏 `evidence_level`、误报 `level` | 目标改为 `severity/verdict/evidence_level`；仅 feedback.py 的 `ConfidenceLevel.PROBABLE` 升级允许，`CONFIRMED` 拒绝 | 直接 `evidence_level=CONFIRMED` 失败；feedback PROBABLE 通过；`logger.level` 通过 |
| P1-4 OD005 CLI 整文件豁免 | `od005_allow` 改为 `(file,line)` 精确匹配，只豁免已说明的 1387 行保护分支 | 同文件新增 handler 被拒；旧分支通过 |
| P1-5 OD002 无法区分换算与计数 | 仅当上下文/左值含 time/duration/ns/us/ms/busy/gap 时才拦截 `*1000`/`/1000`；补充 `*0.001` 的 ns→µs 检测 | `duration_ns*0.001` 失败；`batches*1000` 通过 |
| P2 | 空配置静默关闭 ARCH；OD001 条件补零；OD007 行号 | 空 `arch_rules` 报 `ARCH:config`；`Metric(0 if x is None else x)` 被拒；阈值报键所在行 | 新增对应测试 |
| P3 | 重复诊断、Make 注释 | 每条 import 语句每规则去重；Make 注释更新 | `test_arch_report_cannot_import...` 断言 2 条（每条语句 1 条） |

自检结果：

| 检查 | 结果 |
|---|---|
| `make check` | 0 |
| `pytest` | **352 passed, 17 skipped**（369 用例） |
| `make verify DEMO_OUT=/tmp/ae05-r2-demo2` | 0，352 passed / 17 skipped |
| 真实数据 `tests/test_real_data.py` | 16 passed, 1 skipped |
| `git diff --check` | 0 |

## 10. 第二轮增量复核修复（2026-11-19）

| 问题 | 修复 | 回归 |
|---|---|---|
| P1 · OD003 无法区分 raw 文件读取与普通文案 | 改为检测受限模块的 raw **读取调用**（`open`/`read_text`/`read_bytes`/`read_csv`）及路径子树中的 Profiler 标识；`.connect` 仅当接收者是 `sqlite3` 时判定；`csv` 导入检查先物化迭代器，report 允许 `csv.writer`；来源文案不再拦截 | 报告 `op_summary` 读取被拒、`op_counter.db`/`op_report` 文案通过；diagnosis `signal.connect(callback)` 通过；CLI `TASK` 查询被拒 |
| P1 · OD004 feedback 任意函数可写 `PROBABLE` | `od004_allow` 改为 `(file,line)` 精确匹配，仅登记 `feedback.py:130` 的既有验证升级点；同文件第二个赋值（不同行）被拒 | 合法行通过、同文件新增函数失败、普通 `logger.level` 通过 |
| P1 · OD002 计数误报且反向/下标时间换算漏报 | 仅按完整时间 token 或 `_us/_ms/_ns` 后缀判断；同时检查左右操作数与 Subscript 字符串键；`0.001 * duration_ns`、`row['duration_ns']/1000` 被拒，`items*1000`、`batches*1000` 通过 | 新增正反例 |
| P2 · OD001 非 None 条件误报 | `IfExp` 仅在比较项含 `Constant(None)` 时判定 | `state is running` 条件通过 |
| P2 · OD007 引号键行号 | 行号匹配允许键两侧引号 | `"x":` 报第 2 行 |

自检结果：

| 检查 | 结果 |
|---|---|
| `make check` | 0 |
| `pytest` | **352 passed, 17 skipped**（369 用例） |
| `make verify DEMO_OUT=/tmp/ae05-r3-demo` | 0，352 passed / 17 skipped |
| 真实数据 | 16 passed, 1 skipped |
| `git diff --check` | 0 |
