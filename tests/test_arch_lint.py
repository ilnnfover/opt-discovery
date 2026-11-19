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
        "PATH = 'mindstudio_profiler_output/op_summary_1.csv'\n",
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
