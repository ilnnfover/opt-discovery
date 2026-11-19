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

## `d2eb678` 增量复核（2026-11-19）

**结论仍为 FAIL。暂缓合并和进入 AE-06。** 本轮只复核 `712610d..d2eb678` 的修复及直接受影响的判断分支。上轮五项的原始反例均得到预期结果，ARCH 与 OD005 两项已关闭。OD002、OD003 和 OD004 的修复路径仍有三项 P1。该差异在 `opt_discovery/` 和 CI 工作流下无改动。

| 上轮事项 | 复核结果 | 证据 |
|---|---|---|
| P1-1 · ARCH 跨层依赖 | 关闭 | `primitives.py` 导入 report、report 相对导入 CLI 与反馈写入入口均被拦截。完整副本中调用 `cmd_diagnose` 的报告代码使 `make check` 退出 2。重复导入诊断已去重。 |
| P1-2 · OD003 原始数据访问 | 部分关闭 | diagnosis 读取 SQLite 回填源和 CLI 查询 TASK 均拦截，`op_summary` 报告来源文案通过。直接读取该文件及 `.connect` 判断仍有问题，见下表。 |
| P1-3 · OD004 证据升级 | 部分关闭 | report 直接赋 `CONFIRMED` 触发 OD004，普通 `logger.level` 通过。反馈模块内新增的无验证 `PROBABLE` 写入可绕过。 |
| P1-4 · OD005 CLI 豁免 | 关闭 | 原 handler 获准。同一 `cli.py` 新增 `except Exception: pass` 时，`make check` 退出 2 并定位新分支。 |
| P1-5 · OD002 单位换算 | 部分关闭 | `duration_ns * 0.001` 已拦截，`batches * 1000` 通过。交换乘法操作数后仍漏报，其他普通计数仍可误报。 |

### 剩余阻断项

| 级别 | 代码位置 | 实际影响及修复建议 |
|---|---|---|
| P1 · OD003 无法区分 raw CSV 读取和相邻合法操作 | `tools/arch_lint.py:263`、`:270`、`:294`、`:309` | 报告直接执行 `(rank / 'op_summary_1.csv').read_text()` 的完整副本 `make check` 退出 0。报告中仅说明 `op_counter.db` / `op_report` 来源，以及 diagnosis 的普通 `signal.connect(callback)`，直接运行检查器均退出 1。`csv` 导入判断还复用了已被 SQLite 判断消费的迭代器。修复应检查明确的 Profiler 路径及读取调用，限定 SQLite 名称或已知表，允许合法来源文案和 `csv.writer`。这组反例均为直接表达式。 |
| P1 · OD004 允许反馈模块绕过验证直接升级 | `tools/arch_lint.py:348`；`opt_discovery/experiment/feedback.py:126`；`tests/test_arch_lint.py:234` | 在完整的 `feedback.py` 中增加另一函数直接执行 `opportunity.evidence_level = ConfidenceLevel.PROBABLE`，`make check` 仍退出 0。豁免只看文件和右值属性名，未限定实际验证入口。新增测试的允许样例本身也是任意函数内的无验证赋值。将豁免限制到既有验证写入点，补同文件且同等级的非法赋值反例。 |
| P1 · OD002 仍误判普通计数并漏掉明确时间换算 | `tools/arch_lint.py:199`、`:221`、`:232` | `return items * 1000` 使完整副本 `make check` 退出 2，原因是 `items` 含 `ms`。等价的 `return 0.001 * duration_ns` 却让 `make check` 退出 0。`row['duration_ns'] / 1000` 的直接检查同样漏报。按完整词或单位后缀识别时间语义，并识别实际参与乘法的时间操作数以及明确的时长字段键。 |

无 P0。两个非阻断边界可随有关规则处理。OD001 已拦截缺失值条件补零，但 `Metric(1 if state is running else 0, 'count', Provenance.DERIVED)` 也退出 1，因为判断 `is`/`is not` 时没有检查是否比较 `None`。OD007 普通键的诊断已从第 1 行修正到第 2 行，带引号的合法 YAML 键仍回落到第 1 行。对应 `tools/arch_lint.py:172`、`:466`。

### 本轮实际验证

| 验证 | 结果 |
|---|---|
| `git diff --check 712610d HEAD`、`PYTHONDONTWRITEBYTECODE=1 make check` | 退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_arch_lint.py` | 28 passed。 |
| 在 `git archive 712610d` 的副本中仅加入当前新增规则测试，定向运行 11 个新增用例 | 11 个如预期失败，17 deselected。当前提交中的相同用例均通过。 |
| 在 `/tmp/ae05-incremental.plDphI/checkout` 执行 `previous.py`、`previous-gate.py` 和 `incremental.py` | 在独立生产路径 fixture 和完整 Git 副本中复跑旧样例、直接运行真检查器并注入 `make check`。具体退出码见上两表。 |

本轮验证限于修复差异，未重跑 `make verify`、真实数据或托管 CI。复核结论以三项 P1 所列的明确正反例为合并门槛。

## `3bbc634` 增量复核（2026-11-19）

**验收结论仍为 FAIL。暂缓合并和进入 AE-06。** 本轮只检查 `d2eb678..3bbc634` 和修复直接影响的分支。上轮三项 P1 的原始反例均通过。OD002 已关闭，OD003 和 OD004 各有一项直接回归。无 P0。

| 上轮事项 | 复核结果 | 实际证据 |
|---|---|---|
| OD002 · 单位换算和普通计数 | 关闭 | 在完整副本中，`0.001 * duration_ns` 和 `row['duration_ns'] / 1000` 同一行得到两条 OD002，`make check` 退出 2。`items * 1000` 退出 0。 |
| OD003 · raw 读取与合法文案 | 原例闭合，修复路径仍阻断 | 报告读取 `op_summary_1.csv` 时 `make check` 退出 2。`csv.writer`、SQLite 来源文案和普通 `signal.connect(callback)` 通过。相邻合法 I/O 回归见下表。 |
| OD004 · 同文件未验证升级 | 原例闭合，修复路径仍阻断 | 原验证写入通过。向同一个 `feedback.py` 追加直接写 `PROBABLE` 的函数时，`make check` 退出 2。原豁免行的等级上限回归见下表。 |
| OD001、OD007 · 上轮两个非阻断例 | 关闭 | 定向套件覆盖了 `Metric` 中不比较 `None` 的条件表达式，以及带引号的 YAML 阈值键定位。 |

### 剩余阻断项

| 级别 | 代码位置 | 实际影响及修复建议 |
|---|---|---|
| P1 · OD004 的行号豁免取消了证据等级上限 | `tools/arch_lint.py:393`；`configs/quality/arch_lint.yaml:63`；`opt_discovery/experiment/feedback.py:130` | 在副本中只把真实验证分支第 130 行的 `ConfidenceLevel.PROBABLE` 改为 `ConfidenceLevel.CONFIRMED`，保留原行号及所有验证条件。上一提交的检查器退出 1，新提交的 `make check` 退出 0。该单变量验证路径只能升到 `PROBABLE`，行号单独命中使超上限写入免检。豁免同时匹配原写入点和允许的右值，补同一实际行改写为 `CONFIRMED` 的回归。 |
| P1 · OD003 把合法的错误处理和写文件误判为 raw 读取 | `tools/arch_lint.py:296`、`:307`、`:340`；任务书 `docs/tasks/Opt-Discovery-Agent-Engineering-Task.md:267` | 在 CLI 函数中仅 `from csv import Error`，捕获公共 API 抛出的 `Error` 并转换为用户可见异常，`make check` 退出 2，诊断称直接解析原始数据。在报告中用 `(out / 'op_summary_processed.csv').open('w', encoding='utf-8')` 写派生文件，亦退出 2，诊断称读取原始 Profiler。两个正例在上一提交的检查器均退出 0。将 CSV 判断限定到明确的原始读取，并区分 `open` 的读写模式。保留原始 CSV 直接读取的反例。 |

### 本轮实际验证

| 验证 | 结果 |
|---|---|
| `git diff --check d2eb678 HEAD`、`PYTHONDONTWRITEBYTECODE=1 make check` | 退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_arch_lint.py` | 34 passed。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae05-close.kBunBu/scope.py /tmp/ae05-close.kBunBu/current /tmp/ae05-close.kBunBu/previous` | 使用两个隔离的 Git 副本。当前提交实际运行 `make check`，上一提交运行同一 lint CLI，对照结果见上表。 |

按增量范围，本轮未重跑 `make verify`、真实数据或托管 CI。`opt_discovery/` 与 CI 工作流在本次修复 diff 中无改动。

## AE-05 最终验收（2026-11-19）

**验收结论为 PASS。当前工作区的 AE-05 修复达到合并条件，可以进入 AE-06。** 服务器上的 CodeBuddy 使用 `deepseek-v4.1-flash` 在隔离工作树开发修复。本次独立审核基线 `3bbc634` 上的三份开发改动，并在应用到功能分支工作区后复跑检查和合成演示。

| 上轮阻断 | 最终结果 | 独立验收结果 |
|---|---|---|
| OD004 · 行号豁免放过证据等级越界 | 已关闭 | 豁免同时匹配现有写入位置和 `ConfidenceLevel.PROBABLE`。将第 130 行改为 `CONFIRMED` 后，检查器退出 1，`make check` 退出 2。改用其他接收者的 `.PROBABLE` 或在同文件新增 `.PROBABLE` 写入均触发 OD004。既有验证升级通过。涉及 `tools/arch_lint.py:120`、`:399` 和 `configs/quality/arch_lint.yaml:64`。 |
| OD003 · 合法 CSV 错误处理和派生输出误报 | 已关闭 | CLI 仅导入 `csv.Error`、报告使用 `csv.writer` 或来源说明、`open` 和 `Path.open` 显式写入时均通过。默认读取、显式读取、含 `+` 的读写模式及直接读取 `op_summary_1.csv` 仍触发 OD003。SQLite TASK 反例继续被拦截。涉及 `tools/arch_lint.py:133`、`:299`。 |
| OD002 及上轮已关闭的事项 | 维持关闭 | 隔离副本中两种直接纳秒换算让 `make check` 退出 2，`items * 1000` 退出 0。现有 ARCH 和 OD001–OD007 用例均通过。 |

此次改动仅涉及检查器、豁免配置、Lint 回归测试和本报告。生产运行时与 CI 工作流的差异为空，未发现新的 P0 或 P1。

| 验证 | 结果 |
|---|---|
| `git diff --check`、`PYTHONDONTWRITEBYTECODE=1 make check` | PASS。Ruff、架构和领域检查、mypy、配置验证退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 make verify DEMO_OUT=/tmp/ae05-final-demo.KjUh01` | PASS，357 passed、17 skipped。四组合成演示完成。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -o addopts='' -q -p no:cacheprovider tests/test_arch_lint.py` | PASS，39 passed，包含本轮新增的五个测试。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 /tmp/ae05-codebuddy.FgCpuO/audit.py "$PWD"` | PASS，14 个独立正反例均匹配预期规则和退出码。 |
| 在隔离 Git 副本应用开发差异后，以 `scope.py` 注入并运行 `make check` | 证据越级、同文件新增未验证升级、raw CSV 读取和未经授权的纳秒换算退出 2。CLI 合法 CSV 错误处理、派生文件写入、报告合法文案与计数退出 0。 |
| `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_real.py --expect-blocked` | 真实 Profiling 为 BLOCKED。复核环境未配置数据目录，NPU 和托管 CI 未执行。 |
