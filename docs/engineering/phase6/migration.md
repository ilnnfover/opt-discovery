# AE-07 证据升级与实验反馈迁移说明

本说明对应基线 `e184711`（AE-06 已合并）之后的 AE-07 增量。目标是让**裸 benchmark
观察**与**经过契约核验的实验**分开，任何调用路径都不能只凭 CLI 参数或 ID 把证据级别
升到 `confirmed`。

## 1. 两层输入

| 输入 | 用途 | 是否改变 `evidence_level` |
|---|---|---|
| `--compare bench_compare.json` | 记录整轮 A/B 观察，写入 `extra["measured"]`，标记 `source=benchmark_observation`、`verified=false` | 否 |
| `--validation validation.json` | `ValidationBundle` 结构化验证，唯一生产升级路径 | 是，受契约约束 |

两者**不能同时提供**。`--compare` 只描述整轮 benchmark，`--validation` 绑定单个生成
实例；同时提供会直接退出 2，避免用整轮指标去匹配 kernel/单算子效果。

## 2. 结构化验证契约

公开 Python API：

```python
from opt_discovery.schemas.evidence_gate import (
    ValidationBundle,
    promote_evidence,
    evaluate_validation,
    withdraw_evidence,
)

bundle = ValidationBundle.from_dict(payload)
decision = evaluate_validation(opportunity, bundle, artifact_root=None)
promote_evidence(opportunity, bundle, artifact_root=None)
```

`promote_evidence` 与 CLI `feedback --validation` 走同一契约。`evaluate_validation`
只读，返回 `PromotionDecision(allowed/promoted/target_level/reasons)`。

### 2.1 必填绑定字段

`ValidationBundle` 必须显式给出，缺项或类型不合法直接 `EvidenceGateError`：

- `opportunity_id`：必须与目标 `Opportunity.opportunity_id` 完全一致。同名算子、
  同一 `--operator` 或同一 `--change` 都不能单独匹配。
- `opportunity_instance_id`：必须与目标机会生成时写入的 `instance_id` 完全一致。
  该字段直接构造/生成时产生 UUID 并随 JSON 重读保留；旧 JSON 缺少它时按历史来源
  不明处理，不可升级。审计复制到另一个实例不会通过。
- `operator`、`workload_id`：必须与机会一致。
- `change_id`：被测变更 ID；撤回后同一 change 不能复活。
- `source`：`measured` / `manual` 可升级；`synthetic` / `benchmark` / `unknown`
  只作为不可升级输入。
- `baseline` / `candidate`：必须不同，且每条 result 必须完全一致。
- `input`：baseline/candidate 共用的输入描述（如 `batch_size`、`input_length`），
  每条 result 的 `input` 必须完全相同。
- `results`：至少一条；每条包含 scope、可比性、正确性、性能、effect、samples、
  uncertainty 和 artifact 引用。
- `artifacts`：`name` + `path` + `sha256`；升级时读取文件并校验摘要。用户本地工具
  不需要签名、外部验证服务或数据库，只需要给出真实产物路径与摘要。

### 2.2 级别规则

- 规则诊断上限仍是 `probable`，本次不改诊断引擎。
- 单条有效结果（kernel 或 operator/model/serving 或 workload）最多升一级，且不超过
  `probable`；`workload` scope 不归因到单个算子，直接拒绝升级。
- `operator` 是单算子局部实验，不是端到端。只有 `model` / `serving` 才计入端到端；
  kernel + operator 仍停留在 `probable`。
- 只有同一机会、同一实例、同一 change、同一 baseline/candidate、同一输入，且
  **kernel 结果与 `model`/`serving` 结果使用不相交的 artifact 摘要**时，才可以
  `confirmed`。
- kernel-only、operator-only、同一 scope 重复、同一 artifact 混充两侧、两侧交叉错配
  都只能到 `probable` 或被拒绝。重复提交同一份单范围证据不会逐次跳到 `probable`；
  合法的双范围 bundle 一次调用即可确认。
- `samples >= 3`、`effect` 严格为正、存在 `uncertainty`、`comparable`/`correctness`
  为 true 是升级前提。`min_effect` 必须是 `[0, 1]` 内的有限值，并写入审计；重读按
  同一策略重放，不会出现“保存时 confirmed、重读回落 probable”。0 阈值也不把零改善
  当作收益。5% 是判定阈值，**不是统计显著性**。
- 验证失败、样本不足、无明显效果不会自动降低已有 `evidence_level`；需要显式
  `withdraw_evidence`。

### 2.3 可运行正例

以下脚本在临时目录生成两个独立产物并完成一次 `confirmed` 升级：

```python
import hashlib
import json
import tempfile
from pathlib import Path

from opt_discovery.schemas.bottleneck import BottleneckType
from opt_discovery.schemas.evidence import ConfidenceLevel, MissingEvidence
from opt_discovery.schemas.evidence_gate import (
    ArtifactReference,
    EvidenceSource,
    ValidationBundle,
    ValidationResult,
    ValidationScope,
    promote_evidence,
)
from opt_discovery.schemas.opportunity import (
    Opportunity,
    ValidationPlan,
)

root = Path(tempfile.mkdtemp(prefix="ae07-validation-"))

def artifact(name: str) -> ArtifactReference:
    path = root / f"{name}.json"
    path.write_text(json.dumps({"artifact": name}), encoding="utf-8")
    return ArtifactReference(
        name=name,
        path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

kernel = artifact("kernel")
e2e = artifact("e2e")

def result(scope: ValidationScope, artifact_name: str) -> ValidationResult:
    return ValidationResult(
        scope=scope,
        baseline="baseline-build",
        candidate="candidate-build",
        input={"batch_size": 1, "input_length": 1024},
        comparable=True,
        correctness=True,
        performance_passed=True,
        effect=0.12,
        samples=5,
        uncertainty=0.01,
        artifacts=(artifact_name,),
    )

bundle = ValidationBundle(
    opportunity_id="OPP-001",
    opportunity_instance_id="fixture-instance-001",
    operator="MatMul",
    workload_id="qwen35-35b-a3b-prefill-graph",
    change_id="fused-matmul-001",
    source=EvidenceSource.MANUAL,
    baseline="baseline-build",
    candidate="candidate-build",
    input={"batch_size": 1, "input_length": 1024},
    results=(
        result(ValidationScope.KERNEL, kernel.name),
        result(ValidationScope.MODEL, e2e.name),
    ),
    artifacts=(kernel, e2e),
)

opportunity = Opportunity(
    opportunity_id="OPP-001",
    operator="MatMul",
    workload_ids=("qwen35-35b-a3b-prefill-graph",),
    observation="fixture",
    hypothesis="fixture",
    bottleneck=BottleneckType.COMPUTE_BOUND,
    evidence_level=ConfidenceLevel.HYPOTHESIS,
    missing_evidence=[MissingEvidence(name="validation", reason="not run yet")],
    validation_plan=ValidationPlan(
        kernel=("kernel check",), model=(), serving=(), correctness=("compare output",)
    ),
    is_synthetic=False,
    instance_id="fixture-instance-001",
)
promote_evidence(opportunity, bundle)
assert opportunity.evidence_level is ConfidenceLevel.CONFIRMED
(root / "validation.json").write_text(
    json.dumps(bundle.to_dict(), indent=2), encoding="utf-8"
)
print(root)
```

对应 CLI：

```bash
opt-discovery feedback \
  --opportunities out/steady/<wl>/opportunities.json \
  --validation /tmp/ae07-validation-xxxx/validation.json
# --compare 与 --validation 不能同时使用；裸比较只观察
```

## 3. 历史数据与兼容

1. **旧 `opportunities.json` 仍可读。** `hypothesis` / `probable` 不受影响；
   旧的 `--compare` 结果仍会回填为观察记录。
2. **旧 `confirmed` 没有验证审计时会被保守降级。** 读取 `opportunities.json` 时，
   `confirmed` 必须附带 `extra["evidence_gate"]["validations"]` 中未撤回、摘要匹配、
   `target_level=confirmed`、`confirmed_ready=true`，且 bundle 的
   `opportunity_instance_id` 与机会的 `instance_id` 一致的记录。产物默认按审计中记录
   的路径重新校验摘要；文件缺失或变化同样降级。否则读取结果是 `probable（不可信）`，
   并在 `extra["evidence_gate"]["trust"]` 保留原级别、原因和不可信状态。
3. **`instance_id` 与 `is_synthetic` 的新语义。** 新生成的机会会写 `instance_id`
   （UUID）和 `is_synthetic: true/false`。历史文件缺少 `instance_id` 或
   `is_synthetic` 时仍可读，但来源按**未知**处理，不能直接升级；`is_synthetic`
   缺失不会被默认成“已知真实”。需要继续验证时，请用真实已有输入重新运行 diagnosis
   生成目标机会（获得新的 `instance_id`），再针对该实例生成新的 validation bundle。
   合成机会、合成实验（`source=synthetic`）永远不能升级为真实验证。
4. **旧 CLI 语义。** `--operator` / `--change` / `--compare` 仍可用，但裸比较只记录
   生命周期观察，不再升级证据级别。需要升级时补充 `--validation`；结构化输入中的
   绑定字段不会被 CLI 参数、默认值或 bench payload 自动补齐。`--compare` 与
   `--validation` 不能同时提供（退出 2）；结构化验证被拒绝时仍会写入审计和 caveat，
   但命令返回非零，便于脚本识别。
5. **撤回。** 使用 `withdraw_evidence(opportunity, reason=..., source=...)`。撤回会
   保留原验证 bundle、decision、摘要和原因；同一 `change_id` 或被撤回的 artifact
   摘要不能再次用于升级。需要重新验证时请使用新的 `change_id` 与新产物。

## 4. 边界与限制

- 本契约校验的是本地结构化输入和产物摘要，不提供密码学签名、外部公证或远程验证
  服务；它防止的是误用、错配和静默补齐，而不是任意 Python 内部反射。`source=manual`
  只表示操作者声明“这些结果与文件一致”，并不声称系统从文件自动验证了全部测量。
- `confirmed` 仍要求真实测量结果；5% 阈值和至少 3 轮只是判定门槛，不代表统计显著。
- 本次不新增 Model 与 Serving 双层硬门槛；kernel + `model` 或 `serving` 任一端到端
  scope 即可满足 confirmed 的独立证据要求，具体 scope 记录在 bundle 与审计中。
  `operator` 只是单算子局部实验，不计入端到端。
