# AE-07 独立审查报告

**最终结论为 PASS WITH CONDITIONS。** 原 P1 和直接影响中的阻断项已关闭。剩余一项优先级分数问题，见最终问题清单。

## 首轮审查记录

**首轮结论为 FAIL。** 基线为 `e184711`。首轮 457 项 CPU 测试通过且合成流程可运行，静态检查仍有 Ruff F401。独立通过公开 Python、CLI 和报告入口执行的反例发现下列证据契约问题，已在一次集中修复中交给指定模型。

| 级别 | 首轮位置 | 证据、影响与修复要求 |
|---|---|---|
| P1-1 | `opt_discovery/schemas/evidence_gate.py:67`、`:694`，`opt_discovery/schemas/opportunity.py:216` | `kernel + operator` 在缺端到端测量时得到 `confirmed`。改写一次新诊断机会的 observation、维持局部 OPP ID、算子及 workload，再复制原审计，读取仍为 `confirmed`。历史无 `is_synthetic` 记录也能被确认。应区分算子局部与端到端，绑定持久的机会来源身份，旧来源缺失时只保留可读性。 |
| P1-2 | `opt_discovery/schemas/evidence_gate.py:671`、`:994`、`:1014`，`opt_discovery/schemas/opportunity.py:275` | 公开升级 API 传 `NaN` 阈值时将 0 效果确认。0.04 效果在 0.03 阈值确认后重读变为 `probable`。信任回放应校验独立双范围、已通过决策和实际引用，坚持有效且一致的阈值，并在读取、写出及报告消费时拦截无效确认。首轮末已拦截将单 kernel 或原被拒审计手动改标 `confirmed`，复核保留这两条反例。 |
| P1-3 | `opt_discovery/experiment/feedback.py:147`、`:268`、`:275`，`opt_discovery/cli.py:716` | 实际 CLI 对正确性失败返回 0 并写入 `verified=true`、`validated=1`。双输入时 `comparable=false`、合成来源、非有限轮数、缺 A/B 或错误 workload 仍能确认。只记录真正通过的验证，拒绝状态可见，两个输入渠道互斥或经过完整一致性验证。 |
| P1-4 | `opt_discovery/experiment/feedback.py:33`，`tools/arch_lint.py:431`，`configs/quality/arch_lint.yaml:67` | Ruff 使 `make check` 退出 2。OD004 豁免以文件与 RHS 名匹配，无法限制到受控写点。修复静态错误，使用精确豁免并补同文件待检测负例。 |

这些反例在内存、临时验证产物或本地进程中执行。基线与首轮的 CPU、合成和真实机会核对记录见[实施报告](implementation-report.md)。

## 集中修复期间的定向反例

第一次集中修复的候选中，独立探针已通过双范围实验、单范围上限、跨实例与旧来源拒绝、自定义阈值回放、产物改写或撤回以及 CLI 正确性失败六组验证。进一步检查同一修改的直接影响时，复现以下问题并交回 CodeBuddy 收尾。

| 级别 | 修复候选位置 | 独立证据与要求 |
|---|---|---|
| P1 | `opt_discovery/schemas/evidence_gate.py:931`，`opt_discovery/experiment/feedback.py:203` | 将合法机会的 `evaluate_validation` 判定传给另一个同 `change_id`、不同实例且来源为 synthetic 的验证，公开 `promote_evidence` 仍把第二个机会写成 `confirmed`。写入入口必须自己生成实际判定。 |
| P1 | `opt_discovery/experiment/feedback.py:195` | 文件中两个机会都为 `OPP-042`、实例不同。有效验证正确升级了一条，但 CLI 同时报告 `matched=2 validated=1 upgraded=1 blocked=1` 并退出 1。定位必须同时使用本地 ID 和实例 ID。 |
| P2 | `opt_discovery/schemas/evidence_gate.py:1217` | 读取或写出路径降级后已经记录 `trust.trusted=false`。报告按 `probable` 原样呈现时会丢失不可信提示。报告应保留标记，重新验证通过后再清除。 |

## 最终验收矩阵（2026-11-20）

| 任务书验收项 | 状态 | 最终证据 |
|---|---|---|
| 统一入口和生产接入 | PASS | `opt_discovery/schemas/evidence_gate.py:771`、`:1027` 与 `opt_discovery/experiment/feedback.py:145` 共用证据契约。外部调用者不能传入预计算判定。临时探针验证了直接 API、实际 CLI 和 JSON 往返。 |
| 普通规则上限和单范围约束 | PASS | `opt_discovery/diagnosis/engine.py:43` 仍以 `probable` 为上限。单 operator、kernel 或端到端结果不能确认，裸 workload A/B 只记观察。实际验证 `kernel + operator` 只能到 `probable`，重复同一单范围证据不再多升一级。 |
| 双范围确认 | PASS | `opt_discovery/schemas/evidence_gate.py:869` 检查独立的 kernel 与 model 或 serving 结果。两个范围同机会、同变更、同比较和输入，并引用相互独立的产物时能确认。相关生产 API 测试验证重复 scope、共用产物、输入或双方错配被拒。 |
| 机会及来源绑定 | PASS | `opt_discovery/schemas/opportunity.py:135`、`opt_discovery/schemas/evidence_gate.py:783`。实验携带持久实例 ID，结合局部 ID、算子和 workload 核验。两个同号机会的 CLI 测试只修改目标实例。既有记录缺来源、实际合成诊断、跨实例复用审计均被拦截。直接错误绑定保留原等级，并写入具体拒绝原因。 |
| 正确性、可比性、性能和样本 | PASS | `opt_discovery/schemas/evidence_gate.py:680` 拒绝错误、不可比、缺关键数据及低样本。独立探针确认正确性失败导致 CLI 非零，且没有写 `verified=true`。非有限或负阈值、零改善被拒。合法 0.03 自定义阈值下的 0.04 效果重读仍可信。 |
| 生命周期、可信读取和显式撤回 | PASS | `opt_discovery/schemas/evidence_gate.py:1059`、`:1203`，`opt_discovery/schemas/opportunity.py:297`，`opt_discovery/report/generator.py:248`。已接受不等于已确认。普通历史 JSON 可读，非法直构或改标的 `confirmed` 无法在可信读写和报告中使用。修改产物、撤回、尝试复用及持久化降级标记均已实测。 |
| CI 护栏与向后兼容 | PASS | `make check` 和 `make verify` 均退出 0。OD004 临时注入同文件同 RHS 的另一次写入，准确报告该违规。Python 3.10 专项通过，合成流程四组均完成。 |
| 授权真实数据与既有结论 | PASS WITH CONDITIONS | 真实套件为 28 passed、1 skipped、0 failed、0 errors，退出 3。四组 rank 0 真实机会生成及 JSON 重读共 69 个 `hypothesis`，逐组与基线相同。历史缺 CSV 分支按原约定跳过，CPU R05 继续覆盖。 |

完整命令及结果见[实施报告](implementation-report.md)。最终 `make verify` 为 485 passed、29 skipped，Python 3.10 专项为 165 passed。8 组独立探针和 OD004 注入验证通过。没有执行托管 CI、新采集或 NPU 实验。

## 最终问题清单

- **P0/P1 无。** 首轮四组 P1、公开预计算判定旁路及同号实例返回假失败均已通过复核。已持久化的不可信标记能在报告中保留。
- **P2，非阻断。** `opt_discovery/schemas/evidence_gate.py:1129` 只更新证据等级，`opt_discovery/schemas/opportunity.py:291` 排序仍依赖分数。用两范围有效 fixture 得到 `confirmed` 后显式撤回至 `hypothesis`，实际 `scores.confidence` 仍为 `0.855`，总分仍为 `0.4275`。可信等级和撤回复用限制正确，但依赖优先级评分的排序仍可能沿用旧置信度。可在撤回时按目标等级重算置信度并更新分数依据，保留原验证和撤回审计。
- **P3 无。**

**PASS WITH CONDITIONS。** AE-07 的核心 DoD 已达成，可进入 PR 流程。已知评分限制和真实回归跳过继续单列。改动保留本地，提交远端及合并由用户授权。
