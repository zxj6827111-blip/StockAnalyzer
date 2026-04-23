"""Evolution production preflight checks."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field
from stock_analyzer.config import EvolutionConfig
from stock_analyzer.evolution.ops.recovery import (
    DependencyCheckResult as DependencyCheckResult,
)
from stock_analyzer.evolution.ops.recovery import (
    check_environment_dependencies,
)

__all__ = [
    "DependencyCheckResult",
    "EvolutionPreflightReport",
    "PreflightConfigCheck",
    "PreflightPathCheck",
    "run_evolution_preflight",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreflightPathCheck(_StrictModel):
    """Writable path check result."""

    name: str
    path: str
    writable: bool
    detail: str


class PreflightConfigCheck(_StrictModel):
    """Configuration sanity check result."""

    name: str
    passed: bool
    level: str
    detail: str


class EvolutionPreflightReport(_StrictModel):
    """End-to-end preflight report for production hardening."""

    ready: bool
    checked_at: datetime
    project_root: str
    strict_dependency_check: bool
    dependency: DependencyCheckResult
    path_checks: list[PreflightPathCheck] = Field(default_factory=list)
    config_checks: list[PreflightConfigCheck] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


def run_evolution_preflight(
    config: EvolutionConfig,
    project_root: str | Path | None = None,
) -> EvolutionPreflightReport:
    """Run preflight checks before enabling strict production mode.

    Checks include:
    1. Environment dependencies (CLI tools and Python modules).
    2. Writable paths for suggestions/manifests/compliance logs.
    3. Critical config sanity (`code_commit_id`, `active_champion_id`, schedule format).

    Args:
        config: Evolution configuration.
        project_root: Optional project root for relative path resolution.

    Returns:
        Structured preflight report.
    """
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    checked_at = datetime.now()
    dependency = check_environment_dependencies(
        required_cli=tuple(config.dependency_required_cli),
        required_modules=tuple(config.dependency_required_modules),
    )

    suggestions_path = _resolve_path(root=root, raw_path=config.suggestions_dir)
    manifest_path = _resolve_path(root=root, raw_path=config.manifest_path)
    compliance_db_path = _resolve_path(root=root, raw_path=config.compliance_db_path)

    path_checks = [
        _check_writable_directory(name="suggestions_dir", directory=suggestions_path),
        _check_writable_directory(name="manifest_dir", directory=manifest_path.parent),
        _check_writable_directory(name="compliance_db_dir", directory=compliance_db_path.parent),
    ]
    config_checks = _config_sanity_checks(config=config)

    blockers: list[str] = []
    if config.strict_dependency_check and not dependency.all_available:
        blockers.append("missing_dependencies_with_strict_mode")
    blockers.extend([f"path_not_writable:{item.name}" for item in path_checks if not item.writable])
    blockers.extend(
        [
            f"config:{item.name}"
            for item in config_checks
            if item.level == "error" and not item.passed
        ]
    )

    return EvolutionPreflightReport(
        ready=(len(blockers) == 0),
        checked_at=checked_at,
        project_root=str(root),
        strict_dependency_check=config.strict_dependency_check,
        dependency=dependency,
        path_checks=path_checks,
        config_checks=config_checks,
        blockers=blockers,
    )


def _resolve_path(root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _check_writable_directory(name: str, directory: Path) -> PreflightPathCheck:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".preflight_probe_{uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return PreflightPathCheck(
            name=name,
            path=str(directory),
            writable=True,
            detail="writable",
        )
    except Exception as exc:
        return PreflightPathCheck(
            name=name,
            path=str(directory),
            writable=False,
            detail=str(exc),
        )


def _config_sanity_checks(config: EvolutionConfig) -> list[PreflightConfigCheck]:
    checks: list[PreflightConfigCheck] = []

    code_commit = config.code_commit_id.strip()
    checks.append(
        PreflightConfigCheck(
            name="code_commit_id",
            passed=bool(code_commit and code_commit != "unknown"),
            level="error",
            detail="must be non-empty and not 'unknown'",
        )
    )

    champion_id = config.active_champion_id.strip()
    checks.append(
        PreflightConfigCheck(
            name="active_champion_id",
            passed=bool(champion_id),
            level="error",
            detail="must be non-empty",
        )
    )

    checks.append(
        PreflightConfigCheck(
            name="offhours_time",
            passed=_valid_hhmm(config.offhours_time),
            level="error",
            detail="must be hh:mm format",
        )
    )

    dry_run_policy = config.dry_run_policy.strip().lower()
    policy_ok = dry_run_policy in {"fixed", "auto"}
    checks.append(
        PreflightConfigCheck(
            name="dry_run_policy",
            passed=policy_ok,
            level="error",
            detail="must be one of: fixed, auto",
        )
    )

    if not policy_ok:
        dry_run_policy = "fixed"

    checks.append(
        PreflightConfigCheck(
            name="dry_run",
            passed=(config.dry_run if dry_run_policy == "fixed" else True),
            level="warn",
            detail=(
                "recommended to keep true during pre-production validation"
                if dry_run_policy == "fixed"
                else "resolved by runtime app.mode via dry_run_policy=auto"
            ),
        )
    )

    price_series_mode = str(config.execution_spec.price_series_mode).strip().lower()
    dividend_treatment = str(config.execution_spec.dividend_treatment).strip().lower()
    valid_price_modes = {"raw", "qfq", "hfq"}
    valid_dividend_treatments = {"implicit_by_qfq", "explicit_cashflow"}
    checks.append(
        PreflightConfigCheck(
            name="price_series_mode",
            passed=price_series_mode in valid_price_modes,
            level="error",
            detail="must be one of: raw, qfq, hfq",
        )
    )
    checks.append(
        PreflightConfigCheck(
            name="dividend_treatment",
            passed=dividend_treatment in valid_dividend_treatments,
            level="error",
            detail="must be one of: implicit_by_qfq, explicit_cashflow",
        )
    )
    price_dividend_consistent = True
    consistency_detail = "binding_ok"
    if price_series_mode in {"qfq", "hfq"} and dividend_treatment != "implicit_by_qfq":
        price_dividend_consistent = False
        consistency_detail = "qfq_or_hfq_requires_implicit_by_qfq"
    elif price_series_mode == "raw" and dividend_treatment != "explicit_cashflow":
        price_dividend_consistent = False
        consistency_detail = "raw_requires_explicit_cashflow"
    checks.append(
        PreflightConfigCheck(
            name="price_dividend_binding",
            passed=price_dividend_consistent,
            level="error",
            detail=consistency_detail,
        )
    )

    return checks


def _valid_hhmm(raw: str) -> bool:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        return False
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59
