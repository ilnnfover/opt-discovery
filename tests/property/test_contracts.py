"""AE-06 属性测试：可独立计算的八类核心契约。

原则：

* 仅用小整数栅格 / 集合 / 简单算术作为 oracle，不复用生产 merge 算法；
* 浮点比较使用明确容差（``REL_TOL`` / ``ABS_TOL``），不把任意误差当严格等式；
* 每次 ``finalize`` 只调用一次，输入先排序——保留 IntervalUnion 的既有语义；
* 真实数据留给独立运行的真实回归；这里全部是 CPU 最小 fixture。
"""

from __future__ import annotations

import csv
import random
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from opt_discovery.analyzer.compare import compare_tables
from opt_discovery.analyzer.timeline import summarize
from opt_discovery.analyzer.window import Window, WindowError, select_rounds_window
from opt_discovery.collectors.workload_log import Round, RunLog
from opt_discovery.diagnosis.engine import DiagnosisEngine
from opt_discovery.diagnosis.rules import Condition, Rule, RuleSet, Threshold
from opt_discovery.experiment.feedback import apply_feedback
from opt_discovery.parsers.base import ParserError
from opt_discovery.parsers.csv_mapping import CsvMapping, MappingCsvParser
from opt_discovery.primitives import IntervalUnion
from opt_discovery.schemas.bottleneck import BottleneckType
from opt_discovery.schemas.evidence import LEVEL_CAP, ConfidenceLevel, MissingEvidence
from opt_discovery.schemas.metrics import Metric, Provenance
from opt_discovery.schemas.operator import OperatorRecord, OperatorTable, ParseMeta, TaskType
from opt_discovery.schemas.opportunity import Opportunity, ValidationPlan
from opt_discovery.schemas.workload import WorkloadSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
SUMMARY_MAPPING = REPO_ROOT / "configs" / "parsers" / "ascend_op_summary.yaml"
MAPPING = CsvMapping.load(SUMMARY_MAPPING)

#: 浮点容差：整数栅格上的合并应精确；只有 Parser 重排带来的求和顺序差异需要容差
ABS_TOL = 1e-9
REL_TOL = 1e-9


# ---------------------------------------------------------------------------
# 1) 区间并集
# ---------------------------------------------------------------------------
@st.composite
def interval_sets(draw, *, max_size: int = 8, grid: int = 40, max_width: int = 8):
    count = draw(st.integers(min_value=0, max_value=max_size))
    items: list[tuple[int, int]] = []
    for _ in range(count):
        start = draw(st.integers(min_value=0, max_value=grid))
        width = draw(st.integers(min_value=1, max_value=max_width))
        items.append((start, start + width))
    return items


def _cells(intervals: Iterable[tuple[float, float]]) -> set[int]:
    covered: set[int] = set()
    for start, end in intervals:
        covered.update(range(int(start), int(end)))
    return covered


def _finalize(intervals: Sequence[tuple[float, float]], span: float = 50.0):
    union = IntervalUnion()
    for start, end in sorted(intervals):
        union.add(start, end)
    return union.finalize(span)  # 每个实例只 finalize 一次


@example([])
@example([(0, 5), (5, 10)])          # 边界相接：必须合并，不能重复计
@example([(0, 10), (2, 4)])         # 包含
@example([(3, 8), (3, 8)])          # 完全重复
@example([(0, 2), (1, 3), (2, 4)])  # 链式重叠
@given(interval_sets())
def test_interval_union_matches_cell_set_oracle(intervals) -> None:
    quality = _finalize(intervals)
    expected = len(_cells(intervals))
    assert quality.busy_us == pytest.approx(expected, abs=ABS_TOL)
    assert 0.0 <= quality.busy_us <= 50.0
    # busy + idle = window：underfeed 就是 idle 的比例
    assert quality.busy_us + quality.underfeed_ratio * 50.0 == pytest.approx(
        50.0, abs=ABS_TOL
    )


@given(interval_sets())
def test_duplicate_rows_do_not_double_count(intervals) -> None:
    once = _finalize(intervals)
    twice = _finalize(list(intervals) + list(intervals))
    assert twice.busy_us == pytest.approx(once.busy_us, abs=ABS_TOL)
    assert twice.gap_count == once.gap_count


@given(interval_sets(), st.booleans(), st.integers(min_value=0, max_value=7))
def test_permutation_invariance_after_required_sort(intervals, reverse, rotate) -> None:
    ordered = sorted(intervals)
    permuted = list(reversed(ordered)) if reverse else list(ordered)
    if permuted:
        shift = rotate % len(permuted)
        permuted = permuted[shift:] + permuted[:shift]

    # IntervalUnion 要求输入排序；打乱后必须先排序再喂入
    assert _finalize(permuted).busy_us == pytest.approx(
        _finalize(ordered).busy_us, abs=ABS_TOL
    )


def test_empty_input_has_explicit_semantics() -> None:
    quality = _finalize([], span=100.0)
    assert quality.busy_us == 0.0
    assert quality.underfeed_ratio == 1.0
    assert quality.degraded is True
    assert any("忙碌比例" in reason for reason in quality.reasons)


def test_invalid_window_and_out_of_order_rows_are_visible() -> None:
    with pytest.raises(WindowError):
        Window(5.0, 5.0, rounds=(1,))
    with pytest.raises(WindowError):
        Window(6.0, 5.0, rounds=(1,))

    union = IntervalUnion()
    union.add(10.0, 20.0)
    union.add(5.0, 8.0)  # 乱序不是合法输入，必须被报告而不是静默修正
    quality = union.finalize(30.0)
    assert quality.degraded is True
    assert any("乱序" in reason for reason in quality.reasons)


def test_extreme_epoch_and_sub_microsecond_values() -> None:
    base = 10**12
    quality = _finalize([(base, base + 5), (base + 3, base + 9), (base + 3, base + 9)],
                        span=base + 20)
    # 大 epoch 上的 5~9us 合并，浮点 ULP 远小于 1us，容差取 1e-3
    assert quality.busy_us == pytest.approx(9.0, abs=1e-3)

    tiny = _finalize([(0.0, 0.001), (0.0005, 0.002)], span=0.01)
    assert tiny.busy_us == pytest.approx(0.002, abs=1e-12)


# ---------------------------------------------------------------------------
# 2) 多 rank Timeline
# ---------------------------------------------------------------------------
OP_HEADER = ("OP Type", "Task Type", "Task Start Time(us)", "Task Duration(us)")
RANK_SPAN = 40.0


def _rank_db(root: Path, name: str, intervals: Sequence[tuple[int, int]]) -> Path:
    prof = root / f"PROF_{name}"
    prof.mkdir(parents=True)
    db = prof / f"msprof_{name}.db"
    connection = sqlite3.connect(db)
    connection.execute("create table TASK (startNs integer, endNs integer)")
    connection.execute("create table CANN_API (name integer, startNs integer, endNs integer)")
    connection.execute("create table STRING_IDS (id integer, value text)")
    connection.commit()
    connection.close()

    output = prof / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    with (output / "op_summary_0001.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(OP_HEADER)
        for start, end in intervals:
            writer.writerow(["Op", "AI_CORE", start, end - start])
    return db


@st.composite
def rank_intervals(draw):
    return draw(interval_sets(max_size=6, grid=30, max_width=5))


@given(a=rank_intervals(), b=rank_intervals())
def test_multi_rank_busy_all_never_exceeds_busy_any(a, b, tmp_path: Path) -> None:
    case = Path(tempfile.mkdtemp(prefix="ranks-", dir=tmp_path))
    db0 = _rank_db(case, "a", a)
    db1 = _rank_db(case, "b", b)

    summary = summarize(
        db0, Window(0.0, RANK_SPAN, rounds=(1,)), extra_db_paths=[db1]
    )

    any_cells = _cells(a) | _cells(b)
    all_cells = _cells(a) & _cells(b)
    assert summary.device_busy_us == pytest.approx(len(any_cells), abs=ABS_TOL)
    assert summary.device_busy_all_us == pytest.approx(len(all_cells), abs=ABS_TOL)
    # 同一有效域：0 <= busy_all <= busy_any <= window
    assert 0.0 <= summary.device_busy_all_us <= summary.device_busy_us <= RANK_SPAN
    assert summary.device_idle_us == pytest.approx(
        RANK_SPAN - summary.device_busy_us, abs=ABS_TOL
    )
    assert summary.device_busy_us + summary.device_idle_us == pytest.approx(
        RANK_SPAN, abs=ABS_TOL
    )


# ---------------------------------------------------------------------------
# 3) 窗口
# ---------------------------------------------------------------------------
def _run_log() -> RunLog:
    return RunLog(
        path="fixture-workload.log",
        rounds=tuple(Round(index=index, start_us=index * 100.0, end_us=index * 100.0 + 80.0)
                     for index in range(10)),
    )


@given(st.lists(st.integers(min_value=0, max_value=9), min_size=1, max_size=5, unique=True),
       st.booleans())
def test_selected_rounds_window_covers_min_max_of_selected(rounds, reverse) -> None:
    wanted = sorted(rounds, reverse=reverse)
    window = select_rounds_window(_run_log(), wanted)

    assert window.rounds == tuple(sorted(rounds))
    assert window.start_us == min(index * 100.0 for index in rounds)
    assert window.end_us == max(index * 100.0 + 80.0 for index in rounds)
    assert window.provenance == "measured"
    assert window.duration_us == window.end_us - window.start_us


def test_empty_or_unknown_rounds_are_rejected() -> None:
    with pytest.raises(WindowError):
        select_rounds_window(_run_log(), ())
    with pytest.raises(WindowError):
        select_rounds_window(_run_log(), (99,))


# ---------------------------------------------------------------------------
# 4) Parser
# ---------------------------------------------------------------------------
@st.composite
def parser_rows(draw, *, max_size: int = 10):
    rows = []
    for _ in range(draw(st.integers(min_value=1, max_value=max_size))):
        rows.append(
            (
                draw(st.sampled_from(["MatMul", "Add", "Cast"])),
                draw(st.sampled_from(["AI_CORE", "AI_VECTOR_CORE", "MIX_AIC"])),
                draw(st.integers(min_value=0, max_value=1000)),
                draw(st.floats(min_value=0.0, max_value=1000.0,
                               allow_nan=False, allow_infinity=False, width=32)),
                draw(st.floats(min_value=0.0, max_value=100.0,
                               allow_nan=False, allow_infinity=False, width=32)),
            )
        )
    return rows


OP_HEADER_WITH_WAIT = (
    "OP Type",
    "Task Type",
    "Task Start Time(us)",
    "Task Duration(us)",
    "Task Wait Time(us)",
)


def _write_rows(path: Path, rows: Sequence[Sequence[object]], *,
                header: Sequence[str] = OP_HEADER_WITH_WAIT):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(header))
        for row in rows:
            writer.writerow(list(row))
    return path


def _workload(workload_id: str = "fixture-wl") -> WorkloadSpec:
    return WorkloadSpec.from_dict(
        {
            "workload_id": workload_id,
            "model": {"name": "fixture"},
            "hardware": {"device": "fixture-device"},
        }
    )


def _parse(files: Sequence[Path]) -> OperatorTable:
    return MappingCsvParser(MAPPING).parse(files, _workload())


def _signature(table: OperatorTable) -> dict[tuple[str, str], tuple[int, float]]:
    return {
        (record.operator, record.attributes.get("task_type_raw", "")): (
            record.calls,
            record.total_time_us.value,
        )
        for record in table.records
        if record.total_time_us is not None
    }


def _assert_same_content(left: OperatorTable, right: OperatorTable) -> None:
    a, b = _signature(left), _signature(right)
    assert set(a) == set(b)
    for key in a:
        assert a[key][0] == b[key][0], key
        assert a[key][1] == pytest.approx(b[key][1], rel=REL_TOL, abs=ABS_TOL), key


@given(rows=parser_rows(), seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_parser_content_is_independent_of_order_and_shards(rows, seed, tmp_path: Path) -> None:
    case = Path(tempfile.mkdtemp(prefix="parser-", dir=tmp_path))
    baseline = _parse([_write_rows(case / "base.csv", rows)])

    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    permuted = _parse([_write_rows(case / "shuffled.csv", shuffled)])
    _assert_same_content(baseline, permuted)

    split = seed % len(rows)
    shards = _parse(
        [
            _write_rows(case / "slice_0.csv", rows[:split]),
            _write_rows(case / "slice_1.csv", rows[split:]),
        ]
    )
    _assert_same_content(baseline, shards)


def test_parser_missing_optional_column_means_unmeasured_not_zero(tmp_path: Path) -> None:
    header = ("OP Type", "Task Type", "Task Start Time(us)", "Task Duration(us)")
    path = _write_rows(
        tmp_path / "no_wait.csv", [("MatMul", "AI_CORE", 0, 10.0)], header=header
    )
    table = _parse([path])
    assert table.records
    assert all("task_wait_time_us" not in record.extra for record in table.records)


def test_parser_missing_required_column_raises(tmp_path: Path) -> None:
    path = _write_rows(
        tmp_path / "no_operator.csv",
        [(0.0, 10.0)],
        header=("Task Start Time(us)", "Task Duration(us)"),
    )
    with pytest.raises(ParserError):
        _parse([path])


def test_parser_invalid_rows_are_counted_not_silently_dropped(tmp_path: Path) -> None:
    path = _write_rows(
        tmp_path / "invalid.csv",
        [
            ("", "AI_CORE", 0, 10.0, 1.0),
            ("MatMul", "AI_CORE", 0, "bad", 1.0),
            ("Add", "AI_CORE", 0, 10.0, 1.0),
        ],
    )
    table = _parse([path])
    assert table.meta.skipped_rows == 2
    assert {warning.code for warning in table.meta.warnings} >= {"row_skipped"}
    # 现有 Parser 契约：坏 duration 的 MatMul 行已建立缺值组（calls=0），
    # 它不能贡献 calls/时间；有效 Add 行独立汇总为 1 次 / 10us。
    records = {record.operator: record for record in table.records}
    assert set(records) == {"MatMul", "Add"}
    assert records["MatMul"].calls == 0
    assert records["MatMul"].total_time_us is None
    assert records["Add"].calls == 1
    assert records["Add"].total_time_us.value == pytest.approx(10.0, abs=ABS_TOL)
    assert table.total_operator_time_us() == pytest.approx(10.0, abs=ABS_TOL)


# ---------------------------------------------------------------------------
# 5) Metric
# ---------------------------------------------------------------------------
@given(value=st.floats(min_value=0.0, max_value=1e6,
                       allow_nan=False, allow_infinity=False, width=32),
       provenance=st.sampled_from(list(Provenance)))
def test_metric_unit_conversion_preserves_provenance(value, provenance) -> None:
    metric = Metric(value, "ms", provenance)
    converted = metric.as_time_us()
    assert converted.value == pytest.approx(value * 1000.0, rel=REL_TOL, abs=ABS_TOL)
    assert converted.provenance is provenance


def test_measured_zero_is_not_missing() -> None:
    workload = _workload()
    zero = OperatorRecord(
        workload_id=workload.workload_id,
        operator="Op",
        calls=1,
        total_time_us=Metric(0.0, "us", Provenance.MEASURED),
        task_type=TaskType.KERNEL,
    )
    missing = OperatorRecord(
        workload_id=workload.workload_id,
        operator="Op",
        calls=1,
        task_type=TaskType.KERNEL,
    )
    assert zero.metric("total_time_us") is not None
    assert zero.metric("total_time_us").value == 0.0
    assert zero.metric("total_time_us").provenance is Provenance.MEASURED
    assert missing.metric("total_time_us") is None
    assert OperatorTable(workload=workload, records=[zero]).total_operator_time_us() == 0.0
    assert OperatorTable(workload=workload, records=[missing]).total_operator_time_us() is None


@given(total=st.floats(min_value=0.009999999776482582, max_value=1e6,
                       allow_nan=False, allow_infinity=False, width=32),
       calls=st.integers(min_value=1, max_value=1000))
def test_average_is_derived_unless_explicitly_measured(total, calls) -> None:
    record = OperatorRecord(
        workload_id="wl",
        operator="Op",
        calls=calls,
        total_time_us=Metric(total, "us", Provenance.MEASURED),
        task_type=TaskType.KERNEL,
    )
    average = record.effective_avg_time_us
    assert average is not None
    assert average.provenance is Provenance.DERIVED
    assert average.value == pytest.approx(total / calls, rel=REL_TOL, abs=ABS_TOL)

    explicit = OperatorRecord(
        workload_id="wl",
        operator="Op",
        calls=calls,
        total_time_us=Metric(total, "us", Provenance.MEASURED),
        avg_time_us=Metric(total / calls, "us", Provenance.MEASURED),
        task_type=TaskType.KERNEL,
    )
    assert explicit.effective_avg_time_us.provenance is Provenance.MEASURED


# ---------------------------------------------------------------------------
# 6) Diagnosis
# ---------------------------------------------------------------------------
def _test_ruleset(provenance: Provenance = Provenance.MEASURED) -> RuleSet:
    rule = Rule(
        id="compute.test_fraction",
        bottleneck=BottleneckType.COMPUTE_BOUND,
        description="property test rule",
        required=(
            Condition(fact="aicore_time_fraction", op=">=", threshold="test_ratio"),
        ),
    )
    ruleset = RuleSet(
        version=1,
        device_profile_id="fixture-device",
        rules=(rule,),
        thresholds={
            "test_ratio": Threshold(
                name="test_ratio",
                value=0.5,
                unit="ratio",
                provenance=provenance,
                basis="property test",
            )
        },
        status="calibrated" if provenance is Provenance.MEASURED else "uncalibrated",
    )
    ruleset.validate()
    return ruleset


def _diagnosis_record(present: bool, ratio: float, total: float = 1000.0) -> OperatorRecord:
    extra = (
        {"aicore_time_us": Metric(ratio * total, "us", Provenance.MEASURED)}
        if present
        else {}
    )
    return OperatorRecord(
        workload_id="wl",
        operator="Op",
        calls=1,
        total_time_us=Metric(total, "us", Provenance.MEASURED),
        task_type=TaskType.KERNEL,
        extra=extra,
    )


@given(
    present=st.booleans(),
    ratio=st.floats(min_value=0.0, max_value=2.0,
                    allow_nan=False, allow_infinity=False, width=32),
    provenance=st.sampled_from([Provenance.MEASURED, Provenance.ASSUMED]),
)
def test_diagnosis_blocks_missing_required_and_never_confirms(present, ratio, provenance) -> None:
    engine = DiagnosisEngine(_test_ruleset(provenance))
    diagnosis = engine.diagnose(_diagnosis_record(present, ratio), _workload())

    evaluation = diagnosis.evaluations[0]
    if not present:
        assert evaluation.status == "blocked"
        assert evaluation.missing_facts == ("aicore_time_fraction",)
        assert diagnosis.candidates == []
        return

    if ratio < 0.5:
        assert diagnosis.candidates == []
        return

    assert diagnosis.candidates
    for candidate in diagnosis.candidates:
        assert candidate.level in (ConfidenceLevel.HYPOTHESIS, ConfidenceLevel.PROBABLE)
        # 规则命中的上限是 probable；confirmed 只能来自端到端 + kernel 验证
        assert candidate.level is not ConfidenceLevel.CONFIRMED
        assert candidate.confidence <= LEVEL_CAP[candidate.level]
    if provenance is Provenance.ASSUMED:
        assert all(
            candidate.level is ConfidenceLevel.HYPOTHESIS
            for candidate in diagnosis.candidates
        )


# ---------------------------------------------------------------------------
# 7) Comparison
# ---------------------------------------------------------------------------
def _wl(
    workload_id: str,
    *,
    phase: str = "decode",
    device_count: int = 2,
    input_length: int = 1,
    verified: bool = True,
) -> WorkloadSpec:
    return WorkloadSpec.from_dict(
        {
            "workload_id": workload_id,
            "model": {"name": "fixture"},
            "hardware": {"device": "fixture-device", "device_count": device_count},
            "execution": {"phase": phase, "mode": "eager", "graph_mode": "none"},
            "input": {
                "batch_size": 1,
                "input_length": input_length,
                "output_length": 64,
            },
            "verified": verified,
        }
    )


def _table(
    workload: WorkloadSpec,
    totals: Sequence[tuple[str, float]],
    *,
    synthetic: bool = False,
) -> OperatorTable:
    records = [
        OperatorRecord(
            workload_id=workload.workload_id,
            operator=operator,
            calls=10,
            total_time_us=Metric(total, "us", Provenance.MEASURED),
            task_type=TaskType.KERNEL,
        )
        for operator, total in totals
    ]
    meta = ParseMeta(
        parser="property-test",
        is_synthetic=synthetic,
        extra={"window": {"rounds": [0, 1, 2]}},
    )
    return OperatorTable(workload=workload, records=records, meta=meta)


@given(
    violation=st.sampled_from(
        ["none", "input_length", "phase", "hardware", "verified", "empty", "synthetic"]
    ),
    total_a=st.floats(min_value=1.0, max_value=1e6,
                      allow_nan=False, allow_infinity=False, width=32),
    total_b=st.floats(min_value=1.0, max_value=1e6,
                      allow_nan=False, allow_infinity=False, width=32),
)
def test_comparison_hard_failure_produces_no_usable_conclusion(
    violation, total_a, total_b
) -> None:
    left = _table(_wl("a"), [("Add", total_a), ("MatMul", total_a / 2)])
    right_workload = _wl("b")
    right_records = [("Add", total_b), ("MatMul", total_b / 2)]
    right_synthetic = False

    if violation == "input_length":
        right_workload = _wl("b", input_length=8)
    elif violation == "phase":
        right_workload = _wl("b", phase="prefill")
    elif violation == "hardware":
        right_workload = _wl("b", device_count=4)
    elif violation == "verified":
        right_workload = _wl("b", verified=False)
    elif violation == "empty":
        right_records = []
    elif violation == "synthetic":
        right_synthetic = True

    right = _table(right_workload, right_records, synthetic=right_synthetic)
    report = compare_tables(left, right)

    if violation == "none":
        assert report.comparable is True
        assert report.deltas
        assert report.to_dict()["total_ratio"] is not None
        for delta in report.deltas:
            if delta.presence == "both":
                assert delta.delta_share == pytest.approx(
                    delta.share_b - delta.share_a, abs=ABS_TOL
                )
        return

    assert report.comparable is False
    assert report.deltas == []
    assert report.to_dict()["total_ratio"] is None
    assert report.reasons
    assert report.observations and "不可比" in report.observations[0]


# ---------------------------------------------------------------------------
# 8) Evidence
# ---------------------------------------------------------------------------
def _opportunity(level: ConfidenceLevel = ConfidenceLevel.HYPOTHESIS) -> Opportunity:
    missing = (
        [MissingEvidence(name="calibrated_threshold_or_metric", reason="fixture")]
        if level in (ConfidenceLevel.HYPOTHESIS, ConfidenceLevel.INSUFFICIENT_EVIDENCE)
        else []
    )
    return Opportunity(
        opportunity_id="OPP-001",
        operator="MatMul",
        workload_ids=("wl",),
        observation="fixture observation",
        hypothesis="fixture hypothesis",
        bottleneck=BottleneckType.COMPUTE_BOUND,
        evidence_level=level,
        missing_evidence=missing,
        validation_plan=ValidationPlan(
            kernel=("kernel check",), model=(), serving=(), correctness=("compare output",)
        ),
    )


def _comparison(verdict: str, delta: float | None = -20.0) -> dict:
    metrics = {}
    if delta is not None:
        metrics = {
            "latency_median_s": {"delta_pct": delta},
            "latency_p90_s": {"delta_pct": delta},
            "tokens_per_s_median": {"delta_pct": delta},
        }
    return {"a": "base", "b": "candidate", "verdict": verdict, "metrics": metrics}


@given(
    operator_scoped=st.booleans(),
    verdict=st.sampled_from(["b_faster", "insufficient_samples", "undetermined"]),
)
def test_bare_comparison_never_upgrades_evidence(operator_scoped, verdict) -> None:
    """A bare bench payload is an observation, not a validated experiment.

    AE-07: only a structured ValidationBundle may change evidence_level.  The
    old ``--operator``-only upgrade is intentionally replaced by this contract.
    """
    item = _opportunity()
    before = item.evidence_level
    result = apply_feedback(
        [item],
        _comparison(verdict),
        operator=item.operator if operator_scoped else None,
        change="fixture-change",
    )

    assert item.evidence_level is before
    assert result.upgraded == 0
    assert result.validated == 0
    assert item.status.value in ("discovered", "validating", "accepted", "rejected")
    if verdict == "insufficient_samples":
        assert result.matched == 0
        assert "measured" not in item.extra


def test_synthetic_vs_measured_comparison_is_blocked() -> None:
    workload_a = _wl("a")
    workload_b = _wl("b")
    measured = _table(workload_a, [("Add", 100.0)])
    synthetic = _table(workload_b, [("Add", 100.0)], synthetic=True)
    report = compare_tables(measured, synthetic)

    assert report.comparable is False
    assert report.deltas == []
    assert any("synthetic_mismatch" in reason for reason in report.reasons)


def test_bare_comparison_payload_is_marked_unverified_observation() -> None:
    """The observation record must say it is unverified; no silent 'manual'."""
    item = _opportunity()
    apply_feedback([item], _comparison("b_faster"), operator=item.operator, change="x")
    measured = item.extra["measured"]
    assert measured["source"] == "benchmark_observation"
    assert measured["verified"] is False
    assert measured["synthetic"] is None
    assert item.evidence_level is ConfidenceLevel.HYPOTHESIS
