"""Benchmark execution and comparison (P0-2), plus the AE-07 evidence contract."""

from ..schemas.evidence_gate import (
    ArtifactReference,
    EvidenceSource,
    ValidationBundle,
    ValidationResult,
    ValidationScope,
    promote_evidence,
    withdraw_evidence,
)
from .analyzer import BenchmarkComparison, compare_benchmarks, load_run
from .feedback import FeedbackResult, apply_feedback, load_comparison
from .runner import BenchmarkRun, RoundSample, parse_rounds, run_benchmark, summarize_run

__all__ = [
    "BenchmarkComparison",
    "compare_benchmarks",
    "load_run",
    "BenchmarkRun",
    "RoundSample",
    "parse_rounds",
    "run_benchmark",
    "summarize_run",
    "FeedbackResult",
    "apply_feedback",
    "load_comparison",
    "ArtifactReference",
    "EvidenceSource",
    "ValidationBundle",
    "ValidationResult",
    "ValidationScope",
    "promote_evidence",
    "withdraw_evidence",
]
