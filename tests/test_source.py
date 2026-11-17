"""P1-1：算子 -> 源码映射。"""

from __future__ import annotations

from pathlib import Path

import pytest

from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.rules import RuleSet
from opt_discovery.discovery.generator import opportunities_from_diagnosis
from opt_discovery.parsers.synthetic import build_synthetic_table
from opt_discovery.schemas.workload import WorkloadSpec
from opt_discovery.source_analysis.indexer import SourceIndex
from opt_discovery.source_analysis.mapping import attach_to_opportunities, summarize

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "configs" / "rules" / "bottleneck_rules.v1.yaml"
WORKLOAD_DIR = REPO_ROOT / "configs" / "workloads" / "qwen35-35b-a3b-910b"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "vllm-ascend-hust"
    (root / "vllm_ascend" / "ops").mkdir(parents=True)
    (root / "csrc").mkdir()
    (root / "tests").mkdir()
    (root / "vllm_ascend" / "ops" / "attention.py").write_text(
        "def forward():\n"
        "    return torch_npu.npu_fused_infer_attention_score(q, k, v)\n",
        encoding="utf-8",
    )
    (root / "vllm_ascend" / "ops" / "triton_gating.py").write_text(
        "def moe_gating_top_k_kernel(x):\n    return x\n",
        encoding="utf-8",
    )
    (root / "csrc" / "torch_binding.cpp").write_text(
        '#include "gmm/grouped_matmul.h"\n'
        "void GroupedMatmul() {}\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_noise.py").write_text(
        "GroupedMatmul_in_tests_should_be_ignored = 1\n", encoding="utf-8"
    )
    return root


def test_exact_and_prefixed_and_snake_tiers(repo: Path):
    index = SourceIndex([repo])
    results = index.scan(
        ["GroupedMatmul", "FusedInferAttentionScore", "MoeGatingTopK"]
    )
    assert results["GroupedMatmul"].mapped
    assert results["GroupedMatmul"].tier == "exact"
    assert results["FusedInferAttentionScore"].tier == "prefixed"
    assert results["MoeGatingTopK"].tier == "snake"
    assert all(hit.language in ("python", "cpp") for hit in results["GroupedMatmul"].hits)


def test_tests_directory_is_skipped(repo: Path):
    index = SourceIndex([repo])
    results = index.scan(["GroupedMatmul_in_tests_should_be_ignored"])
    # 该算子名只出现在 tests/ 下，跳过目录后应未命中
    assert not results["GroupedMatmul_in_tests_should_be_ignored"].mapped


def test_builtin_and_runtime_are_never_mapped(repo: Path):
    (repo / "csrc" / "noise.cpp").write_text(
        'const char* k = "MatMulV2";\nconst char* h = "hcom_allReduce";\n', encoding="utf-8"
    )
    results = SourceIndex([repo]).scan(["MatMulV2", "hcom_allReduce_"])
    assert results["MatMulV2"].tier == "builtin"
    assert not results["MatMulV2"].mapped
    assert results["hcom_allReduce_"].tier == "runtime"
    assert "HCCL" in (results["hcom_allReduce_"].reason or "")


def test_hits_are_deduplicated_per_line(repo: Path):
    results = SourceIndex([repo]).scan(["GroupedMatmul"])
    keys = {(hit.path, hit.line) for hit in results["GroupedMatmul"].hits}
    assert len(keys) == len(results["GroupedMatmul"].hits)


def test_payload_summary_counts(repo: Path):
    index = SourceIndex([repo])
    payload = index.to_dict(index.scan(["GroupedMatmul", "MatMulV2", "NopeX"]))
    assert payload["summary"]["operators"] == 3
    assert payload["summary"]["mapped"] == 1
    assert payload["summary"]["unmapped"] == 2
    assert "命中 1" in summarize(payload)


def test_attach_writes_source_refs_and_drops_caveat():
    workload = WorkloadSpec.from_yaml(WORKLOAD_DIR / "prefill_graph.yaml")
    table = build_synthetic_table("rich", workload)
    report = DiagnosisEngine(RuleSet.load(RULES)).diagnose_table(table)
    opportunities = opportunities_from_diagnosis(report)
    assert opportunities
    target = opportunities[0]
    assert any("尚未建立 Kernel" in item for item in target.caveats)

    payload = {
        "operators": {
            target.operator: {
                "mapped": True,
                "hits": [{"repo": "vllm-ascend-hust", "path": "csrc/x.cpp", "line": 7}],
            }
        }
    }
    attached = attach_to_opportunities(opportunities, payload)
    assert attached >= 1
    assert target.source_refs == ("vllm-ascend-hust:csrc/x.cpp:7",)
    assert not any("尚未建立 Kernel" in item for item in target.caveats)


# ---------------------------------------------------------------------------
# 注册表（torch_binding.cpp）解析
# ---------------------------------------------------------------------------
BINDING_CPP = '''\
#include "moe/moe_gating_top_k/moe_gating_top_k_torch_adpt.h"
TORCH_LIBRARY_EXPAND(CONCAT(_C, _ascend), ops)
{
    ops.def(
        "moe_gating_top_k(Tensor x, "
        "                  int k) -> (Tensor y)"
    );
    ops.impl("moe_gating_top_k", torch::kPrivateUse1, &vllm_ascend::moe_gating_top_k);

    ops.def("npu_add_rms_norm_bias(Tensor x) -> Tensor");
    ops.impl("npu_add_rms_norm_bias", torch::kPrivateUse1,
             &vllm_ascend::npu_add_rms_norm_bias);
}
'''


@pytest.fixture()
def binding_repo(tmp_path: Path) -> Path:
    root = tmp_path / "vllm-ascend-hust"
    csrc = root / "csrc"
    (csrc / "moe" / "moe_gating_top_k").mkdir(parents=True)
    (csrc / "moe" / "add_rms_norm_bias").mkdir(parents=True)
    (csrc / "torch_binding.cpp").write_text(BINDING_CPP, encoding="utf-8")
    (csrc / "moe" / "moe_gating_top_k" / "moe_gating_top_k_torch_adpt.h").write_text(
        "at::Tensor moe_gating_top_k(const at::Tensor& x, int64_t k)\n"
        "{\n"
        "    return x;\n"
        "}\n",
        encoding="utf-8",
    )
    (csrc / "moe" / "add_rms_norm_bias" / "add_rms_norm_bias_torch_adpt.h").write_text(
        "at::Tensor npu_add_rms_norm_bias(const at::Tensor& x);\n"
        "at::Tensor npu_add_rms_norm_bias(const at::Tensor& x)\n"
        "{\n"
        "    return x;\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def test_binding_table_resolves_schema_and_impl(binding_repo: Path):
    from opt_discovery.source_analysis.bindings import parse_binding_table

    table = parse_binding_table(binding_repo / "csrc" / "torch_binding.cpp")
    assert len(table.entries) == 2
    assert table.resolved_ratio == 1.0

    matches = table.resolve("MoeGatingTopK")
    assert matches and matches[0][0] == "exact"
    entry = matches[0][1]
    assert entry.schema == "moe_gating_top_k"
    assert entry.impl_file.endswith("moe_gating_top_k_torch_adpt.h")
    assert entry.impl_line == 1  # 定义行（同名声明带 ; 被跳过）
    assert entry.def_line > 0


def test_binding_takes_precedence_over_text_match(binding_repo: Path):
    from opt_discovery.source_analysis.bindings import parse_binding_table

    table = parse_binding_table(binding_repo / "csrc" / "torch_binding.cpp")
    index = SourceIndex([binding_repo], bindings=table)
    results = index.scan(["MoeGatingTopK", "MatMulV2"])

    mapped = results["MoeGatingTopK"]
    assert mapped.tier == "binding"
    assert mapped.binding is not None
    assert mapped.binding["schema"] == "moe_gating_top_k"
    assert mapped.hits[0].path.endswith("moe_gating_top_k_torch_adpt.h")
    assert any("torch_binding.cpp" in hit.path for hit in mapped.hits)
    assert results["MatMulV2"].tier == "builtin"


def test_without_bindings_falls_back_to_text_match(binding_repo: Path):
    index = SourceIndex([binding_repo])
    results = index.scan(["MoeGatingTopK"])
    assert results["MoeGatingTopK"].tier in ("snake", "exact")
    assert results["MoeGatingTopK"].binding is None
