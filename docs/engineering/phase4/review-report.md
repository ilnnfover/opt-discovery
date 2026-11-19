# AE-05 独立验收 Review

**结论为 FAIL。暂缓合并 `feat/ae-05-arch-lint`，暂缓进入 AE-06。** 检查入口和退出码有效。当前规则仍会放过任务书明确禁止的跨层调用、原始数据读取、证据升级和静默回退，也会让正常的报告说明和计数运算触发合并门禁。无 P0，五项 P1 见问题清单。

复核对象为 `712610d`，`main`、本地 `origin/main` 和共同祖先均为 `f0fb206`。完整差异为 8 个文件。开始时工作区只有未跟踪的 `docs/tasks/`，其中任务书 AE-05（第 248–271 行）为验收依据。核对了实施报告、本次全部 diff、现有 Profiling 适配器及实际证据字段。违规注入使用临时目录和 `git archive` 副本。

## 验收矩阵

| 验收项 | 结果 | 正反例和证据 |
|---|---|---|
| `ARCH:core-schemas` | 部分通过 | Schema 直接导入 CLI 和包 `__init__.py` 的相对导入均退出 1，同层导入通过。基础领域模块 `primitives.py` 导入 report 退出 0。 |
| `ARCH:diagnosis-no-raw-profiler` | 导入规则通过，原始数据边界未达成 | diagnosis 相对导入项目 parser 退出 1。用标准库 SQLite 直接读真实 `op_counter.db` / `op_report` 退出 0，OD003 未补上该路径。 |
| `ARCH:report-read-only` | 失败 | 直接导入 diagnosis 退出 1，导入 schema 的 `OperatorTable` 通过。从真实 CLI 入口重新诊断、导入实际写证据的 `apply_feedback` 均退出 0。 |
| `ARCH:discovery-no-raw-profiler` | 直接导入规则通过 | 相对导入 collectors 退出 1，现有合法依赖通过。OD003 的文件读取模式单独验收。 |
| OD001 · 缺失值 | 部分通过 | `Metric(value=x or 0, …)` 被拦截，实测零合法。构造内直接 `0 if x is None else x` 仍通过。 |
| OD002 · 单位转换 | 失败 | 原始 ns `/ 1000` 被拦截，适配器和 f-string 展示通过。同一种换算 `duration_ns * 0.001` 漏报，非时间计数 `batches * 1000` 被误报。 |
| OD003 · 原始数据访问与 CLI 编排 | 失败 | 直接读取带 `op_summary` 的路径被拦截。diagnosis 的实际 SQLite 回填源和 CLI 查询 `TASK` 通过，合法的报告来源说明反而失败。 |
| OD004 · 证据升级 | 失败 | 直接改写 `meta.extra['quality_flags']` 被拦截，`schemas.quality.attach` 合法。实际 `Opportunity.evidence_level` 直接升为 `CONFIRMED` 可以通过，普通 `logger.level` 却被拦截。 |
| OD005 · 异常回退 | 失败 | 分析模块的 `except Exception: pass` 被拦截，窄类型文件清理例子通过。同样的宽异常写在 `cli.py` 中可全量通过 `make check`。 |
| OD006 · 硬编码机器路径 | 通过 | `/home/alice/profile-data` 退出 1，`out/profile-data` 通过。生产扫描范围排除了测试与文档。 |
| OD007 · 阈值来源 | 通过 | 缺来源和 provenance 的映射退出 1，完整登记通过。诊断定位粒度见 P2。 |
| YAML、例外与诊断 | 部分通过 | 仓库配置有效，诊断含规则、路径和修复提示。CLI 的整文件豁免违背最小范围要求。空配置可关闭 ARCH，报告存在重复位置和 YAML 固定行号。 |
| 规则测试 | 部分通过 | 17 个测试调用真实 `run_lint` 并使用同一份 YAML。各规则均有基础违规反例。除通过全仓扫描，OD003、OD004、OD006 缺少直接验证相邻合法语义的专门样例。现有用例未覆盖问题清单中的 P1。 |
| Make 与 CI 接入 | 通过 | 正常 `make check` 退出 0。已识别违规直接运行检查器退出 1，`make check` 退出 2。静态 CI 直接运行同一模块，测试矩阵复用 `make check`，两个步骤均无错误忽略配置。 |
| 运行时与 Profiling 契约 | 通过 | `main...HEAD` 下 `opt_discovery/` 无改动，运行时 CLI、schema 和解析器未变。完整 CPU 套件与合成演示通过。 |

## 问题清单

| 级别 | 代码位置 | 实际影响及修复建议 |
|---|---|---|
| P1-1 · ARCH 漏掉基础领域模块及报告的实际跨层入口 | `configs/quality/arch_lint.yaml:8`、`:26`；`opt_discovery/primitives.py:95`；`opt_discovery/cli.py:2340`；`opt_discovery/experiment/feedback.py:88` | `primitives.py` 导入 report 得 0。报告中用函数内的 `from ..cli import cmd_diagnose` 并调用真实命令，`make check` 得 0，报告可重新诊断。导入会改写机会证据的 `apply_feedback` 也得 0。将基础领域文件纳入规则，限制 report 导入实际的 CLI 和反馈写入入口，并对这几种直接 import 增加正反例。 |
| P1-2 · OD003 既漏掉真实 raw SQLite，又拦截合法报告文案 | `tools/arch_lint.py:29`、`:207`；`opt_discovery/parsers/sqlite_op_report.py:58`、`:110` | diagnosis 中用 `sqlite3.connect(root / 'op_counter.db')` 查询 `op_report` 得 0，恰好是现有适配器读取的实际来源。CLI 直接查询 Profiler 的 `TASK` 也通过 `make check`。只写 `来源说明：算子表由 op_summary 导出生成。` 的报告函数却让 `make check` 退出 2。按受限模块内明确的 Profiler I/O、文件名或已知表访问判定，覆盖 SQLite 回填源和 CLI 的定向禁用场景，允许来源文案。无需追踪任意路径变量。 |
| P1-3 · OD004 漏掉实际证据级别字段 | `tools/arch_lint.py:234`、`:243`；`opt_discovery/schemas/opportunity.py:110`、`:118`；`opt_discovery/experiment/feedback.py:126` | `opportunity.evidence_level = ConfidenceLevel.CONFIRMED` 在报告中通过 `make check`。`Opportunity` 可变，现有正常升级路径确实写该字段，且只允许经验证升到 `PROBABLE`。反过来普通 `logger.level = 20` 会触发 OD004。保护实际字段并限定正常反馈写入路径，补非法直接升级、正常反馈及常规 `level` 赋值测试。可沿用现有 API。 |
| P1-4 · OD005 的 CLI 整文件豁免允许新增静默异常 | `tools/arch_lint.py:275`、`:373`；`configs/quality/arch_lint.yaml:54`；`opt_discovery/cli.py:1387` | 在完整 Git 副本的 `cli.py` 中另加一个无日志、无质量标记的 `except Exception: pass`。`make check` 退出 0。当前豁免覆盖整个编排入口，与配置中登记的特定错误保护分支无关。把豁免限定到已说明理由的处理点，或用显式可审计回退消除该豁免。用同文件的合法旧路径和非法新增 handler 验证。 |
| P1-5 · OD002 的数值字面量门禁无法区分典型换算和计数 | `tools/arch_lint.py:28`、`:177`；`configs/quality/arch_lint.yaml:43` | 在 analyzer 中分别注入 `duration_ns * 0.001` 和 `batches * 1000`。前者 `make check` 退出 0，后者退出 2。前者与被拦截的 `/ 1000` 执行相同单位换算，后者是正常计数。把硬拦截限制到能明确判断的单位形态，覆盖项目直接采用的 `0.001`。为通用计数、合法展示和授权适配器保留正例。 |
| P2 · 空 YAML 配置会静默关掉 ARCH | `tools/arch_lint.py:368`、`:371`、`:373` | 同一份 Schema 禁止导入样例用正式配置退出 1，配置换成 `{}` 后退出 0 并打印 `arch-lint: OK`。当前提交的正式配置正常，四条 ARCH 测试也能捕获整组规则失效。建议拒绝缺失或空的架构规则并验证豁免理由。 |
| P2 · OD001 的直接条件回退仍可绕开 | `tools/arch_lint.py:151`、`:163` | `Metric(0 if measured is None else measured, 'us', Provenance.MEASURED)` 退出 0。现有 `or 0` 的明确形态可用。可补同一构造内可确定的条件表达式或等价契约测试。 |
| P2 · OD007 只指向 YAML 第一行 | `tools/arch_lint.py:352`、`:360` | 缺来源的阈值在注入文件第 2 行起定义，诊断仍报第 1 行。规则名、文件及阈值键仍可定位。使用 YAML 节点的位置或准确的阈值键位置。 |
| P3 · 重复诊断和 Make 注释 | `tools/arch_lint.py:96`、`:132`；`Makefile:14` | 一条 `from opt_discovery.cli import main` 产生父模块和成员两条同规则诊断。Make 注释还称架构检查尚未实现。诊断每条语句和规则去重并更新注释即可。 |

## 实际执行结果

| 验证 | 结果 |
|---|---|
| `git diff --check main...HEAD`，审查完整 diff | 通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make check` | 退出 0。Ruff、架构/领域 Lint、mypy 和配置验证通过。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae05-review-demo.HxBoie` | 退出 0，335 passed、17 skipped。合成演示完成。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_arch_lint.py` | 退出 0，17 passed。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae05-review-probe.ObpJlP/verify.py /tmp/ae05-review-probe.ObpJlP/checkout` | 隔离规则正反例、空配置和完整工作树的 Make 注入已执行。识别的 OD002 用例让 Make 退出 2，静默 CLI 异常退出 0，清除注入后退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae05-review-probe.ObpJlP/gate.py /tmp/ae05-review-probe.ObpJlP/checkout` | 在完整副本中分别验证报告重新诊断、CLI raw SQL、证据升级、来源文案和两类乘法。退出码见各 P1。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py --expect-blocked` | 真实 Profiling 为 BLOCKED，复核环境未配置数据目录。托管 CI 与 NPU 未运行。 |

合并复核以五项 P1 的原反例为门槛。直接跨层调用、raw 读取、证据写入、静默回退与明确的未经授权换算应退出非零。相邻的合法文案、计数、公共 API 调用与已授权适配器应通过。P2/P3 可后续处理。
