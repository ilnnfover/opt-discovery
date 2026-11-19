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

`tests/test_arch_lint.py`（17 条）：

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
| `pytest` | **335 passed, 17 skipped**（352 用例） |
| `make verify DEMO_OUT=/tmp/ae05-demo` | 0，335 passed / 17 skipped |
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
