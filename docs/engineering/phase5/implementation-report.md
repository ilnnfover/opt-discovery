# AE-06 实施报告

最终验收为 **PASS WITH CONDITIONS**，见[集中修复与最终验收](#7-集中修复与最终验收2026-11-20)。第 1～6 节保留首轮记录。

## 1. 基线与范围

基线为 `main` 的 `ba3e6b3`，即 AE-05 的合并提交。开发使用本地分支 `feat/ae-06-property-real-regression`，开始时只有既有未跟踪目录 `docs/tasks/`。目标是为八类数学和领域契约添加属性测试，核对四组已授权统一补采，并区分 CPU、合成和真实验证结果。

CodeBuddy 的 DeepSeek V4.1 Flash 写入了首轮源码和测试。会话结束于 429 额度限制，服务端给出的重置时间为北京时间 2026-11-20 14:04:45。Codex 完成了独立审核、测试执行和本报告。本轮实现仍为待修复状态。

## 2. 实际改动

| 文件 | 内容 |
|---|---|
| `pyproject.toml`、`tests/conftest.py`、`.gitignore` | 加入 Hypothesis 开发依赖，注册默认 30 个样例的确定性 profile，支持输出复现数据，忽略本地缓存。 |
| `tests/property/test_contracts.py` | 添加区间并集、多 rank Timeline、窗口、Parser、Metric、Diagnosis、Comparison 和 Evidence 的 CPU 属性及边界测试。 |
| `configs/collection_fixtures/qwen35-35b-a3b-910b-unified-v1.json` | 记录四组逻辑身份、采集哈希 `c6edbf7bb2fddc3d`、实测轮次与分析窗口 `(0,1,2)`。当前采集配置文件的哈希不同。 |
| `scripts/verify_real.py`、`tests/regression/test_verify_real_identity.py` | 复用真实数据入口和四种既有状态，添加文件存在性和 manifest 身份检查，注册新增真实测试文件。 |
| `tests/test_real_unified_manifest.py` | 四组采集身份和 rank 核验，以及代表性 prefill 窗口与图覆盖测试。 |

## 3. 核对指标与预期

| 指标 | 独立核对与候选实现的状态 |
|---|---|
| 区间并集与多 rank | 小整数栅格集合计算并集和交集，期望 `0 ≤ busy_all ≤ busy_any ≤ window`。虚拟 SQLite rank 位于不同目录，用例通过。 |
| 窗口与来源 | 四组均有 2 个可用 rank，日志轮次均为 `0..4`。分析选用 `0..2`。改动 manifest 固定参数或把图模式的一个 rank 链接换成 eager 输入，现有身份断言仍通过，详见审核报告。 |
| Parser 与 Metric | 分片、行序、缺列、实测零和来源类型均有测试。错行组预期、测试 workload ID、32 位浮点生成边界导致三个用例失败。 |
| Comparison、Diagnosis 和 Evidence | 硬失败时无可用差异、缺必要证据时阻断、工作负载级结果无法归因到单算子、合成和实测混比被阻断。反馈 API 的可验证来源边界仍归 AE-07。 |
| 通信量 | 对 decode 两种模式的真实 rank 0 SQLite，独立逐条读取并按 `globalTaskId` 取最大 size。eager 为 262957 条、132413 个任务、22113334272 字节。graph 为 16399 条、16399 个任务、23047250056 字节。整份采集口径下，记录量变化约 16 倍，去重字节量相差约 4.2%。 |
| 图覆盖 | 对 prefill graph rank 0 的 `0..2` 轮直接读取原始 CSV。`Model ID` 有效的任务为 3843/8184，任务占比约 46.96%，时长占比约 57.32%。时长使用独立 Decimal 求和及 COMMUNICATION 转换。候选测试的两个范围断言不足以区分这两项。 |
| Host ACL 与 MSTX | prefill eager rank 0 选择与窗口相交的完整调用区间。独立整数 ns 端点扫描得到 54007 次调用、并集 306651.08 µs、直接累加 571638.14 µs。运行时结果与并集一致，容差 `0.001 µs`。该样本 MSTX 不可用。 |
| rank 异常与缺测证据 | CPU 回归继续覆盖历史 rank 时长截断、运算对比限制和缺 MSTX 场景。本轮没有重新采集 HBM 或 NPU 性能。 |

## 4. 验证执行记录

命令使用 Python 3.12.13 和 Hypothesis 6.170.15，另用 Python 3.10.22 和 Hypothesis 6.168.5 运行兼容性专项。真实验证的环境变量指向服务器上已授权的统一补采别名目录。

| 检查 | 实际命令与结果 |
|---|---|
| 基线 CPU、合成、真实 | 基线 `make check` 和 `make verify` 均退出 0，357 passed、17 skipped，合成演示完成。基线 `OPT_DISCOVERY_DATA_ROOT=<授权根> python3 scripts/verify_real.py` 退出 3，16 passed、1 skipped。 |
| 候选静态 | `PYTHONDONTWRITEBYTECODE=1 make check PYTHON=/tmp/ae06-review-env.42pW5C/bin/python` 退出 0。 |
| 候选整体验证 | 无数据环境执行 `make verify PYTHON=/tmp/ae06-review-env.42pW5C/bin/python DEMO_OUT=/tmp/ae06-review-demo.N6qhJH`，Make 退出 2。pytest 为 3 failed、386 passed、27 skipped，该命令未执行演示。 |
| 候选独立合成演示 | `make demo PYTHON=/tmp/ae06-review-env.42pW5C/bin/python DEMO_OUT=/tmp/ae06-candidate-synthetic.q4Yygk` 退出 0，四组合成 Workload 均生成产物。 |
| 定向 CPU 回归和属性 | Python 3.12 执行 `python -m pytest -o addopts='' -q -p no:cacheprovider tests/property tests/regression tests/test_feedback.py tests/test_timeline.py tests/test_graph_coverage.py tests/test_comm.py`，退出 1，3 failed、126 passed。 |
| 最低版本定向 | Python 3.10 执行 `python -m pytest -o addopts='' -q -p no:cacheprovider --tb=no tests/property tests/regression/test_verify_real_identity.py tests/regression/test_verify_real_contract.py`，退出 1，3 failed、49 passed。 |
| 候选真实回归 | `OPT_DISCOVERY_DATA_ROOT=<授权根> /tmp/ae06-review-env.42pW5C/bin/python scripts/verify_real.py` 退出 1。pytest 为 1 failed、25 passed、1 skipped。汇总文案将已执行用例误写为 26 passed。 |
| 缺数据契约 | 清除 `OPT_DISCOVERY_DATA_ROOT` 后，`make verify-real PYTHON=/tmp/ae06-review-env.42pW5C/bin/python` 和直接执行脚本均退出 2 并报告 BLOCKED。脚本带 `--expect-blocked` 时退出 0。 |

## 5. 行为与限制

`opt_discovery/`、产品 CLI 和数据 Schema 相对基线没有差异。新增验证入口继续严格区分 `BLOCKED`、`SKIPPED` 与 `FAIL`，错误情况的通过数量展示需修正。历史缺 CSV 回填在此批采集无法复现，保持显式跳过，由 CPU R05 承担覆盖。读取真实数据期间未重采或启动 NPU。

## 6. 结论

首轮实现保留在本地功能分支，静态检查和独立合成演示通过，完整 CPU 与真实验证仍失败。CodeBuddy 指定模型限额阻塞了集中修复。[独立审查报告](review-report.md)列出恢复验收所需的原反例和定向修复范围。

## 7. 集中修复与最终验收（2026-11-20）

额度重置后，Codex 核对 `feat/ae-06-property-real-regression` 工作区和基线 `ba3e6b3`，通过 `codebuddy -p -y --model deepseek-v4.1-flash` 交付同一批 P1 修复。首次调用达到轮次上限，后续调用接续完成并退出 0。两次调用前后均核查 Git 状态。CodeBuddy 修改验证脚本、fixture 和测试，Codex 负责独立核对、验收执行和文档。

| 首轮问题 | 集中修复与最终状态 |
|---|---|
| CPU 三项失败 | Parser 测试保留坏时长组的缺测契约并核对告警、调用数和有效行总时长。实测零测试统一 workload ID，派生均值使用 32 位精确可表示的生成边界。完整 CPU 和 Python 3.10 专项通过。 |
| 真实采集身份 | 核对 fixture 所列固定参数和采样开关，将发现的 rank 目录与 `workload.log` 所属采集的 manifest 绑定。原目录和授权别名布局通过。保留声明哈希的参数改动及跨采集 rank 链接均被拦截。 |
| 真实指标 | 用日志轮次核对窗口边界和实际过滤，用原始 CSV 独立核对图内任务数、时长及两个占比，用原始 SQLite 分别核对 Host ACL 区间并集、通信记录数和去重字节量。将时间占比替换为任务占比后测试失败。 |
| 验证报告计数 | 真实回归的通过数使用结构化结果中实际通过的计数，汇总分别报告通过、失败、错误和跳过。 |

### 7.1 最终验证记录

`$PY312`、`$PY310` 分别表示 Python 3.12 和 Python 3.10 的本轮虚拟环境。`$DATA_ROOT` 表示用户已授权的本机统一补采别名目录。所有真实回归均只读输入。

| 命令 | 观察结果 |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 make check PYTHON="$PY312"` | PASS，退出 0。Ruff、架构检查、mypy（53 个源文件）、CLI 配置校验通过。 |
| `env -u OPT_DISCOVERY_DATA_ROOT PYTHONDONTWRITEBYTECODE=1 make verify PYTHON="$PY312" DEMO_OUT=/tmp/ae06-accept.8Nqi7k` | PASS，退出 0。393 passed、29 skipped。合成流程生成四组 Workload 产物。CPU 运行的跳过数包含需要真实输入的用例。 |
| `env -u OPT_DISCOVERY_DATA_ROOT "$PY312" -m pytest -o addopts='' -q -p no:cacheprovider tests/property tests/regression tests/test_feedback.py tests/test_timeline.py tests/test_graph_coverage.py tests/test_comm.py` | PASS，退出 0，133 passed。 |
| `"$PY310" -m pytest -o addopts='' -q -p no:cacheprovider tests/property tests/regression/test_verify_real_identity.py tests/regression/test_verify_real_contract.py` | PASS，退出 0，56 passed。 |
| `env -u OPT_DISCOVERY_DATA_ROOT make verify-real PYTHON="$PY312"` | BLOCKED，Make 退出 2。该命令未运行真实用例。 |
| `env -u OPT_DISCOVERY_DATA_ROOT "$PY312" scripts/verify_real.py --expect-blocked` | PASS，退出 0，内层状态符合预期为 BLOCKED。 |
| `OPT_DISCOVERY_DATA_ROOT="$DATA_ROOT" "$PY312" scripts/verify_real.py` | SKIPPED，脚本退出 3。四组均已发现，28 passed、1 skipped、0 failed、0 errors。 |
| `OPT_DISCOVERY_DATA_ROOT="$DATA_ROOT" PYTHONDONTWRITEBYTECODE=1 make verify-real PYTHON="$PY312"` | SKIPPED，重复得到 28 passed、1 skipped、0 failed、0 errors。底层脚本退出 3，Make 报 `Error 3` 并退出 2。 |
| `PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 "$PY312" /tmp/ae06-concentrated.QvqAZb/independent.py` | PASS，退出 0。三个负向验证触发预期拒绝或预期测试失败，详见复核报告。 |
| `git diff --check` | PASS，退出 0。 |

### 7.2 最终状态与已知限制

同一批授权输入的基线真实回归为 16 passed、1 skipped，最终为 28 passed、1 skipped。新增 12 项为采集身份、rank 来源、窗口、图覆盖、Host ACL 并集和通信量验证。第 3 节记录的独立指标仍支持原结论。相对基线 `opt_discovery/` 无差异，产品 CLI 和数据 Schema 无变更。

仍跳过 `tests/test_real_data.py:436` 的历史缺 CSV 回填用例。本批导出各 rank 均有 CSV，该分支由 CPU R05 覆盖。新采集、NPU 实测、托管 CI 均未执行。直接反馈入口的可验证证据来源属于 AE-07。代码和报告保留在本地工作区，提交 PR 由用户决定。
