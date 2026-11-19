"""AE-05: architecture/domain lint positive, negative and false-positive tests."""

from __future__ import annotations

from pathlib import Path

from tools.arch_lint import run_lint

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "configs" / "quality" / "arch_lint.yaml"


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _rules(root: Path) -> set[str]:
    return {item.rule for item in run_lint(root, CONFIG)}


def _violations(root: Path, rule: str):
    return [item for item in run_lint(root, CONFIG) if item.rule == rule]


def test_current_repository_passes(tmp_path: Path) -> None:
    assert run_lint(REPO_ROOT, CONFIG) == []


def test_arch_schemas_cannot_import_cli(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/schemas/model.py", "from opt_discovery.cli import main\n")
    assert "ARCH:core-schemas" in _rules(tmp_path)


def test_arch_diagnosis_cannot_import_parsers(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/diagnosis/rules.py", "from opt_discovery.parsers import csv_mapping\n")
    assert "ARCH:diagnosis-no-raw-profiler" in _rules(tmp_path)


def test_arch_report_cannot_import_diagnosis(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/report/generator.py", "from opt_discovery.diagnosis import engine\n")
    assert "ARCH:report-read-only" in _rules(tmp_path)


def test_arch_discovery_cannot_import_collectors(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/discovery/generator.py", "from opt_discovery.collectors import discover_all\n")
    assert "ARCH:discovery-no-raw-profiler" in _rules(tmp_path)


def test_arch_allows_schemas_relative_imports(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/schemas/metrics.py", "X = 1\n")
    _write(tmp_path, "opt_discovery/schemas/operator.py", "from .metrics import X\n")
    assert run_lint(tmp_path, CONFIG) == []


def test_od001_metric_or_zero_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "from opt_discovery.schemas.metrics import Metric\n"
        "def f(measured):\n"
        "    return Metric(measured or 0, 'us')\n",
    )
    assert _violations(tmp_path, "OD001")


def test_od001_measured_zero_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "from opt_discovery.schemas.metrics import Metric, Provenance\n"
        "m = Metric(0.0, 'us', Provenance.MEASURED)\n",
    )
    assert run_lint(tmp_path, CONFIG) == []


def test_od002_raw_unit_conversion_is_scoped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "ns = 10\nvalue = ns * 1000\nshown = f'{ns / 1000:.2f}ms'\n",
    )
    violations = _violations(tmp_path, "OD002")
    assert len(violations) == 1
    assert violations[0].line == 2


def test_od002_authorized_adapter_is_allowed(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/events.py", "value = ns * 1000\n")
    assert run_lint(tmp_path, CONFIG) == []


def test_od003_raw_source_in_report_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/report/x.py",
        "def f(path):\n"
        "    return (path / 'mindstudio_profiler_output/op_summary_1.csv').read_text()\n",
    )
    assert _violations(tmp_path, "OD003")


def test_od004_direct_evidence_write_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "def f(flag):\n"
        "    flag.severity = 'blocking'\n",
    )
    assert _violations(tmp_path, "OD004")


def test_od005_broad_except_pass_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "def f():\n"
        "    try:\n"
        "        return 1\n"
        "    except Exception:\n"
        "        pass\n",
    )
    assert _violations(tmp_path, "OD005")


def test_od005_narrow_except_pass_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "def f():\n"
        "    try:\n"
        "        return 1\n"
        "    except OSError:\n"
        "        pass\n",
    )
    assert run_lint(tmp_path, CONFIG) == []


def test_od006_machine_path_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/collectors/x.py", "PATH = '/root/prof/export'\n")
    assert _violations(tmp_path, "OD006")


def test_od007_threshold_without_provenance_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "configs/rules/bad.yaml",
        "thresholds:\n"
        "  x:\n"
        "    value: 1.0\n"
        "    unit: ratio\n",
    )
    assert _violations(tmp_path, "OD007")


def test_od007_registered_threshold_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "configs/thresholds/good.yaml",
        "thresholds:\n"
        "  x:\n"
        "    value: 1.0\n"
        "    unit: ratio\n"
        "    provenance: measured\n"
        "    basis: micro-benchmark\n",
    )
    assert run_lint(tmp_path, CONFIG) == []


def test_arch_primitives_is_core_domain(tmp_path: Path) -> None:
    _write(tmp_path, "opt_discovery/primitives.py", "from opt_discovery.report import generator\n")
    assert "ARCH:core-schemas" in _rules(tmp_path)


def test_arch_report_cannot_import_cli_or_feedback(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/report/x.py",
        "def f():\n"
        "    from ..cli import main\n"
        "    return main\n"
        "from ..experiment.feedback import apply_feedback\n",
    )
    rules = _rules(tmp_path)
    assert "ARCH:report-read-only" in rules
    violations = _violations(tmp_path, "ARCH:report-read-only")
    assert len(violations) == 2  # 每条 import 语句一条，父模块/成员不重复
    assert sorted(item.line for item in violations) == [2, 4]


def test_od003_diagnosis_sqlite_and_cli_task_query(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/diagnosis/x.py",
        "import sqlite3\n"
        "conn = sqlite3.connect('op_counter.db')\n",
    )
    _write(
        tmp_path,
        "opt_discovery/cli.py",
        "import sqlite3\n"
        "SQL = 'select startNs, endNs from TASK'\n",
    )
    violations = _violations(tmp_path, "OD003")
    assert any(item.path == "opt_discovery/cli.py" and item.line == 2 for item in violations)


def test_od003_report_source_prose_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/report/x.py",
        "NOTE = '来源说明：算子表由 op_summary 导出生成。'\n",
    )
    assert run_lint(tmp_path, CONFIG) == []


def test_od004_evidence_level_and_generic_level(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "def f(opportunity, logger):\n"
        "    opportunity.evidence_level = 'confirmed'\n"
        "    logger.level = 20\n",
    )
    violations = _violations(tmp_path, "OD004")
    assert len(violations) == 1
    assert violations[0].line == 2

    import yaml

    _write(
        tmp_path,
        "opt_discovery/experiment/feedback.py",
        "from opt_discovery.schemas.evidence import ConfidenceLevel\n"
        "def f(opportunity):\n"
        "    opportunity.evidence_level = ConfidenceLevel.PROBABLE\n",
    )
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    config["od004_allow"] = [
        {"file": "opt_discovery/experiment/feedback.py", "line": 3, "reason": "test"}
    ]
    config_path = tmp_path / "arch_lint.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    assert not [
        item
        for item in run_lint(tmp_path, config_path)
        if item.rule == "OD004"
        and item.path == "opt_discovery/experiment/feedback.py"
    ]


def test_od002_other_conversion_forms_and_counts(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "duration_ns = 10\n"
        "duration_us = duration_ns * 0.001\n"
        "batches = 3 * 1000\n",
    )
    violations = _violations(tmp_path, "OD002")
    assert len(violations) == 1
    assert violations[0].line == 2


def test_od001_conditional_zero_fallback(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "from opt_discovery.schemas.metrics import Metric, Provenance\n"
        "def f(measured):\n"
        "    return Metric(0 if measured is None else measured, 'us', Provenance.MEASURED)\n",
    )
    assert _violations(tmp_path, "OD001")


def test_od005_allowance_is_line_scoped(tmp_path: Path) -> None:
    import yaml

    _write(
        tmp_path,
        "opt_discovery/cli.py",
        "def f():\n"
        "    try:\n"
        "        return 1\n"
        "    except Exception:\n"
        "        pass\n"
        "def g():\n"
        "    try:\n"
        "        return 2\n"
        "    except Exception:\n"
        "        pass\n",
    )
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    config["od005_allow"] = [
        {"file": "opt_discovery/cli.py", "line": 4, "reason": "test"}
    ]
    config_path = tmp_path / "arch_lint.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    violations = [item for item in run_lint(tmp_path, config_path) if item.rule == "OD005"]
    assert len(violations) == 1
    assert violations[0].line == 9


def test_empty_config_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "empty.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    assert any(
        item.rule == "ARCH:config" for item in run_lint(tmp_path, config_path)
    )


def test_od007_reports_threshold_key_line(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "configs/rules/bad.yaml",
        "thresholds:\n"
        "  x:\n"
        "    value: 1.0\n"
        "    unit: ratio\n",
    )
    violations = _violations(tmp_path, "OD007")
    assert violations[0].line == 2


def test_od004_feedback_cannot_jump_to_confirmed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/experiment/feedback.py",
        "from opt_discovery.schemas.evidence import ConfidenceLevel\n"
        "def f(opportunity):\n"
        "    opportunity.evidence_level = ConfidenceLevel.CONFIRMED\n",
    )
    assert _violations(tmp_path, "OD004")


def test_od004_feedback_second_write_is_not_exempt(tmp_path: Path) -> None:
    import yaml

    _write(
        tmp_path,
        "opt_discovery/experiment/feedback.py",
        "from opt_discovery.schemas.evidence import ConfidenceLevel\n"
        "def verified(opportunity):\n"
        "    opportunity.evidence_level = ConfidenceLevel.PROBABLE\n"
        "def unverified(opportunity):\n"
        "    opportunity.evidence_level = ConfidenceLevel.PROBABLE\n",
    )
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    config["od004_allow"] = [
        {"file": "opt_discovery/experiment/feedback.py", "line": 3, "reason": "verified"}
    ]
    config_path = tmp_path / "arch_lint.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    violations = [item for item in run_lint(tmp_path, config_path) if item.rule == "OD004"]
    assert len(violations) == 1
    assert violations[0].line == 5


def test_od003_report_raw_read_is_rejected_but_prose_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/report/x.py",
        "from pathlib import Path\n"
        "def f(rank):\n"
        "    return (rank / 'op_summary_1.csv').read_text()\n",
    )
    _write(
        tmp_path,
        "opt_discovery/report/y.py",
        "NOTE = '来源：op_counter.db / op_report 导出'\n",
    )
    violations = _violations(tmp_path, "OD003")
    assert len(violations) == 1
    assert violations[0].path == "opt_discovery/report/x.py"


def test_od003_signal_connect_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/diagnosis/x.py",
        "def f(signal, callback):\n"
        "    signal.connect(callback)\n",
    )
    assert run_lint(tmp_path, CONFIG) == []


def test_od002_reversed_operand_and_subscript_key(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "def f(row, duration_ns):\n"
        "    a = 0.001 * duration_ns\n"
        "    b = row['duration_ns'] / 1000\n"
        "    items = 5\n"
        "    return items * 1000\n",
    )
    violations = _violations(tmp_path, "OD002")
    assert [item.line for item in violations] == [2, 3]


def test_od001_non_none_conditional_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "opt_discovery/analyzer/x.py",
        "from opt_discovery.schemas.metrics import Metric, Provenance\n"
        "def f(state):\n"
        "    return Metric(1 if state is running else 0, 'count', Provenance.DERIVED)\n",
    )
    assert run_lint(tmp_path, CONFIG) == []


def test_od007_quoted_key_line_is_accurate(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "configs/rules/bad.yaml",
        'thresholds:\n'
        '  "x":\n'
        "    value: 1.0\n"
        "    unit: ratio\n",
    )
    violations = _violations(tmp_path, "OD007")
    assert violations[0].line == 2
