from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.evolution.ops.preflight import run_evolution_preflight


def _load_base_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def test_preflight_ready_when_dependencies_empty_and_config_sane(tmp_path: Path) -> None:
    config = _load_base_config()
    config.evolution.dependency_required_cli = []
    config.evolution.dependency_required_modules = []
    config.evolution.strict_dependency_check = True
    config.evolution.code_commit_id = "git:abc123"
    config.evolution.active_champion_id = "champion_prod_1"
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"

    report = run_evolution_preflight(config=config.evolution, project_root=tmp_path)
    assert report.ready is True
    assert report.dependency.all_available is True
    assert all(item.writable for item in report.path_checks)


def test_preflight_not_ready_when_strict_and_dependencies_missing(tmp_path: Path) -> None:
    config = _load_base_config()
    config.evolution.dependency_required_cli = ["definitely-missing-cli"]
    config.evolution.dependency_required_modules = ["definitely_missing_module_xyz"]
    config.evolution.strict_dependency_check = True
    config.evolution.code_commit_id = "git:abc123"
    config.evolution.active_champion_id = "champion_prod_1"

    report = run_evolution_preflight(config=config.evolution, project_root=tmp_path)
    assert report.ready is False
    assert "missing_dependencies_with_strict_mode" in report.blockers


def test_preflight_not_ready_when_code_commit_id_unknown(tmp_path: Path) -> None:
    config = _load_base_config()
    config.evolution.dependency_required_cli = []
    config.evolution.dependency_required_modules = []
    config.evolution.strict_dependency_check = True
    config.evolution.code_commit_id = "unknown"
    config.evolution.active_champion_id = "champion_prod_1"

    report = run_evolution_preflight(config=config.evolution, project_root=tmp_path)
    assert report.ready is False
    assert any(blocker == "config:code_commit_id" for blocker in report.blockers)


def test_preflight_not_ready_when_dry_run_policy_invalid(tmp_path: Path) -> None:
    config = _load_base_config()
    config.evolution.dependency_required_cli = []
    config.evolution.dependency_required_modules = []
    config.evolution.strict_dependency_check = True
    config.evolution.code_commit_id = "git:abc123"
    config.evolution.active_champion_id = "champion_prod_1"
    config.evolution.dry_run_policy = "invalid-policy"

    report = run_evolution_preflight(config=config.evolution, project_root=tmp_path)
    assert report.ready is False
    assert any(blocker == "config:dry_run_policy" for blocker in report.blockers)


def test_preflight_ready_with_git_auto_when_dependency_check_not_strict(tmp_path: Path) -> None:
    config = _load_base_config()
    config.evolution.dependency_required_cli = ["definitely-missing-cli"]
    config.evolution.dependency_required_modules = ["definitely_missing_module_xyz"]
    config.evolution.strict_dependency_check = False
    config.evolution.code_commit_id = "git:auto"
    config.evolution.active_champion_id = "champion_prod_1"

    report = run_evolution_preflight(config=config.evolution, project_root=tmp_path)
    assert report.ready is True
    assert "missing_dependencies_with_strict_mode" not in report.blockers
    assert not any(blocker == "config:code_commit_id" for blocker in report.blockers)


def test_preflight_not_ready_when_price_dividend_binding_invalid_for_qfq(tmp_path: Path) -> None:
    config = _load_base_config()
    config.evolution.dependency_required_cli = []
    config.evolution.dependency_required_modules = []
    config.evolution.strict_dependency_check = True
    config.evolution.code_commit_id = "git:abc123"
    config.evolution.active_champion_id = "champion_prod_1"
    config.evolution.execution_spec.price_series_mode = "qfq"
    config.evolution.execution_spec.dividend_treatment = "explicit_cashflow"

    report = run_evolution_preflight(config=config.evolution, project_root=tmp_path)
    assert report.ready is False
    assert "config:price_dividend_binding" in report.blockers


def test_preflight_not_ready_when_price_dividend_binding_invalid_for_raw(tmp_path: Path) -> None:
    config = _load_base_config()
    config.evolution.dependency_required_cli = []
    config.evolution.dependency_required_modules = []
    config.evolution.strict_dependency_check = True
    config.evolution.code_commit_id = "git:abc123"
    config.evolution.active_champion_id = "champion_prod_1"
    config.evolution.execution_spec.price_series_mode = "raw"
    config.evolution.execution_spec.dividend_treatment = "implicit_by_qfq"

    report = run_evolution_preflight(config=config.evolution, project_root=tmp_path)
    assert report.ready is False
    assert "config:price_dividend_binding" in report.blockers
