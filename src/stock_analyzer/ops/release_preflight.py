"""Environment-level release preflight checks."""

# mypy: disable-error-code=misc

from __future__ import annotations

import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from stock_analyzer.config import StockAnalyzerConfig

CheckLevel = Literal["error", "warn", "info"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleasePreflightCheck(_StrictModel):
    """Single release preflight check."""

    category: str
    name: str
    passed: bool
    level: CheckLevel
    detail: str


class ReleasePreflightReport(_StrictModel):
    """Structured report for environment-level release checks."""

    ready: bool
    checked_at: datetime
    project_root: str
    config_path: str
    app_mode: str
    bind_host: str
    bind_port: int | None = None
    frontend_dist: str = ""
    checks: list[ReleasePreflightCheck] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def run_release_preflight(
    config: StockAnalyzerConfig,
    *,
    project_root: str | Path | None = None,
    config_path: str | Path | None = None,
    bind_host: str = "127.0.0.1",
    bind_port: int | None = None,
    require_frontend: bool = True,
    disk_warn_gb: float = 4.0,
    disk_fail_gb: float = 1.0,
) -> ReleasePreflightReport:
    """Run environment-level release checks before startup."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    resolved_config_path = _resolve_config_path(root=root, config_path=config_path)
    frontend_dist = _resolve_frontend_dist_dir(project_root=root)

    checks: list[ReleasePreflightCheck] = []
    checks.append(_python_version_check())
    checks.append(
        ReleasePreflightCheck(
            category="config",
            name="config_path",
            passed=resolved_config_path.exists(),
            level="error",
            detail=str(resolved_config_path),
        )
    )
    checks.append(
        _frontend_dist_check(
            frontend_dist=frontend_dist,
            require_frontend=require_frontend,
        )
    )
    checks.append(_secret_key_check(config=config))
    checks.extend(_notification_checks(config=config))
    checks.extend(_wecom_checks(config=config))
    checks.extend(_feishu_checks(config=config))
    checks.extend(_path_checks(config=config, root=root))
    checks.append(
        _disk_space_check(
            root=root,
            warn_threshold_gb=disk_warn_gb,
            fail_threshold_gb=disk_fail_gb,
        )
    )
    if bind_port is not None:
        checks.append(_port_check(host=bind_host, port=bind_port))

    blockers = [
        f"{item.category}:{item.name}"
        for item in checks
        if item.level == "error" and not item.passed
    ]
    warnings = [
        f"{item.category}:{item.name}"
        for item in checks
        if item.level == "warn" and not item.passed
    ]
    return ReleasePreflightReport(
        ready=not blockers,
        checked_at=datetime.now(),
        project_root=str(root),
        config_path=str(resolved_config_path),
        app_mode=config.app.mode,
        bind_host=bind_host,
        bind_port=bind_port,
        frontend_dist=str(frontend_dist) if frontend_dist is not None else "",
        checks=checks,
        blockers=blockers,
        warnings=warnings,
    )


def _resolve_config_path(root: Path, config_path: str | Path | None) -> Path:
    if config_path is not None:
        return Path(config_path)
    return root / "config" / "default.yaml"


def _resolve_frontend_dist_dir(project_root: Path) -> Path | None:
    candidates = (
        project_root / "frontend_dist",
        project_root / "frontend" / "dist",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _python_version_check() -> ReleasePreflightCheck:
    passed = sys.version_info >= (3, 11)
    detail = ".".join(str(part) for part in sys.version_info[:3])
    return ReleasePreflightCheck(
        category="runtime",
        name="python_version",
        passed=passed,
        level="error",
        detail=detail,
    )


def _frontend_dist_check(
    *,
    frontend_dist: Path | None,
    require_frontend: bool,
) -> ReleasePreflightCheck:
    passed = frontend_dist is not None
    return ReleasePreflightCheck(
        category="ui",
        name="frontend_dist",
        passed=passed,
        level="error" if require_frontend else "warn",
        detail=str(frontend_dist) if frontend_dist is not None else "frontend build output missing",
    )


def _secret_key_check(config: StockAnalyzerConfig) -> ReleasePreflightCheck:
    raw_secret = config.command_channel.secret_key.strip()
    placeholders = {"", "change-me", "replace_with_strong_secret", "secret"}
    passed = raw_secret not in placeholders
    level: CheckLevel = "warn" if config.app.mode.strip().lower() == "simulation" else "error"
    if passed:
        detail = "custom secret configured"
    else:
        detail = "command channel secret still uses placeholder"
    return ReleasePreflightCheck(
        category="security",
        name="command_channel_secret",
        passed=passed,
        level=level,
        detail=detail,
    )


def _notification_checks(config: StockAnalyzerConfig) -> list[ReleasePreflightCheck]:
    checks: list[ReleasePreflightCheck] = []
    channels = [
        ("primary", config.notifications.primary.strip().lower()),
        ("backup", config.notifications.backup.strip().lower()),
    ]
    for role, channel in channels:
        if channel in {"", "console", "none"}:
            checks.append(
                ReleasePreflightCheck(
                    category="notifications",
                    name=f"{role}_{channel or 'none'}",
                    passed=True,
                    level="info",
                    detail="no credential required",
                )
            )
            continue
        passed = _notification_channel_ready(config=config, channel=channel)
        checks.append(
            ReleasePreflightCheck(
                category="notifications",
                name=f"{role}_{channel}",
                passed=passed,
                level="error",
                detail=_notification_channel_detail(channel=channel, passed=passed),
            )
        )
    return checks


def _notification_channel_ready(config: StockAnalyzerConfig, channel: str) -> bool:
    if channel == "pushplus":
        return bool(config.notifications.pushplus_token.strip())
    if channel in {"wecom", "wechat"}:
        return bool(config.notifications.wecom_webhook.strip())
    if channel in {"feishu", "lark"}:
        return bool(config.notifications.feishu_webhook.strip())
    if channel in {"feishu_app", "lark_app"}:
        return bool(
            config.notifications.feishu_app_id.strip()
            and config.notifications.feishu_app_secret.strip()
            and config.notifications.feishu_app_receive_id.strip()
        )
    if channel in {"telegram", "tg"}:
        return bool(
            config.notifications.telegram_bot_token.strip()
            and config.notifications.telegram_chat_id.strip()
        )
    if channel in {"email", "smtp"}:
        return bool(
            config.notifications.email_smtp_host.strip()
            and config.notifications.email_sender.strip()
            and config.notifications.email_password.strip()
            and config.notifications.email_receivers
        )
    if channel in {"custom", "webhook", "custom_webhook"}:
        return bool(config.notifications.custom_webhook_url.strip())
    return False


def _notification_channel_detail(channel: str, *, passed: bool) -> str:
    if passed:
        return f"{channel} credential configured"
    if channel == "pushplus":
        return "requires notifications.pushplus_token"
    if channel in {"wecom", "wechat"}:
        return "requires notifications.wecom_webhook"
    if channel in {"feishu", "lark"}:
        return "requires notifications.feishu_webhook"
    if channel in {"feishu_app", "lark_app"}:
        return (
            "requires notifications.feishu_app_id, "
            "notifications.feishu_app_secret, and notifications.feishu_app_receive_id"
        )
    if channel in {"telegram", "tg"}:
        return "requires telegram_bot_token and telegram_chat_id"
    if channel in {"email", "smtp"}:
        return "requires smtp host, sender, password, and receivers"
    if channel in {"custom", "webhook", "custom_webhook"}:
        return "requires notifications.custom_webhook_url"
    return f"unsupported channel: {channel}"


def _wecom_checks(config: StockAnalyzerConfig) -> list[ReleasePreflightCheck]:
    if not config.wecom_interaction.enabled:
        return [
            ReleasePreflightCheck(
                category="wecom",
                name="disabled",
                passed=True,
                level="info",
                detail="wecom interaction disabled",
            )
        ]
    checks = [
        ReleasePreflightCheck(
            category="wecom",
            name="token",
            passed=bool(config.wecom_interaction.token.strip()),
            level="error",
            detail="requires wecom_interaction.token when enabled",
        )
    ]
    if config.wecom_interaction.enforce_receive_id:
        checks.append(
            ReleasePreflightCheck(
                category="wecom",
                name="receive_id",
                passed=bool(config.wecom_interaction.receive_id.strip()),
                level="error",
                detail="requires wecom_interaction.receive_id when enforce_receive_id=true",
            )
        )
    return checks


def _feishu_checks(config: StockAnalyzerConfig) -> list[ReleasePreflightCheck]:
    if not config.feishu_interaction.enabled:
        return [
            ReleasePreflightCheck(
                category="feishu",
                name="disabled",
                passed=True,
                level="info",
                detail="feishu interaction disabled",
            )
        ]
    checks = [
        ReleasePreflightCheck(
            category="feishu",
            name="subscription_mode",
            passed=config.feishu_interaction.subscription_mode in {"webhook", "long_connection"},
            level="error",
            detail=(
                "requires feishu_interaction.subscription_mode to be "
                "webhook or long_connection"
            ),
        ),
        ReleasePreflightCheck(
            category="feishu",
            name="app_credentials",
            passed=bool(
                config.notifications.feishu_app_id.strip()
                and config.notifications.feishu_app_secret.strip()
            ),
            level="error",
            detail=(
                "requires notifications.feishu_app_id and "
                "notifications.feishu_app_secret when feishu interaction is enabled"
            ),
        ),
    ]
    if config.feishu_interaction.subscription_mode == "webhook":
        checks.append(
            ReleasePreflightCheck(
                category="feishu",
                name="verification_token",
                passed=bool(config.feishu_interaction.verification_token.strip()),
                level="error",
                detail=(
                    "requires feishu_interaction.verification_token when "
                    "subscription_mode=webhook"
                ),
            )
        )
    return checks


def _path_checks(config: StockAnalyzerConfig, root: Path) -> list[ReleasePreflightCheck]:
    checks = [
        _check_directory(
            category="paths",
            name="warehouse_db_dir",
            path=_resolve_path(root, config.market_warehouse.db_path).parent,
            must_exist=False,
            writable=True,
            level="error",
        ),
        _check_directory(
            category="paths",
            name="warehouse_package_root",
            path=_resolve_path(root, config.market_warehouse.package_root),
            must_exist=True,
            writable=False,
            level="error",
        ),
        _check_directory(
            category="paths",
            name="runtime_state_dir",
            path=_resolve_path(root, config.command_channel.state_persist_path).parent,
            must_exist=False,
            writable=True,
            level="error",
        ),
        _check_directory(
            category="paths",
            name="runtime_history_dir",
            path=_resolve_path(root, config.command_channel.history_archive_dir),
            must_exist=False,
            writable=True,
            level="error",
        ),
        _check_directory(
            category="paths",
            name="acceptance_export_dir",
            path=_resolve_path(root, config.acceptance.export_dir),
            must_exist=False,
            writable=True,
            level="error" if config.acceptance.export_enabled else "info",
        ),
        _check_directory(
            category="paths",
            name="sim_broker_export_dir",
            path=_resolve_path(root, config.sim_broker_weekly.export_dir),
            must_exist=False,
            writable=True,
            level="error" if config.sim_broker_weekly.export_enabled else "info",
        ),
        _check_directory(
            category="paths",
            name="tdx_output_root",
            path=_resolve_path(root, config.tdx_sync.output_root),
            must_exist=False,
            writable=True,
            level="error" if config.tdx_sync.enabled else "info",
        ),
    ]
    tdx_requires_vipdoc = bool(config.tdx_sync.enabled and config.tdx_sync.auto_run)
    vipdoc_root = config.tdx_sync.vipdoc_root.strip()
    if vipdoc_root:
        checks.append(
            _check_directory(
                category="paths",
                name="tdx_vipdoc_root",
                path=Path(vipdoc_root),
                must_exist=True,
                writable=False,
                level="warn",
            )
        )
    elif tdx_requires_vipdoc:
        checks.append(
            ReleasePreflightCheck(
                category="paths",
                name="tdx_vipdoc_root",
                passed=False,
                level="warn",
                detail="tdx_sync.vipdoc_root is empty",
            )
        )
    return checks


def _resolve_path(root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _check_directory(
    *,
    category: str,
    name: str,
    path: Path,
    must_exist: bool,
    writable: bool,
    level: CheckLevel,
) -> ReleasePreflightCheck:
    try:
        if must_exist and not path.exists():
            return ReleasePreflightCheck(
                category=category,
                name=name,
                passed=False,
                level=level,
                detail=f"missing: {path}",
            )
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        if writable:
            probe = path / f".release_preflight_{uuid4().hex}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        elif not path.is_dir():
            return ReleasePreflightCheck(
                category=category,
                name=name,
                passed=False,
                level=level,
                detail=f"not a directory: {path}",
            )
        return ReleasePreflightCheck(
            category=category,
            name=name,
            passed=True,
            level=level,
            detail=str(path),
        )
    except Exception as exc:
        return ReleasePreflightCheck(
            category=category,
            name=name,
            passed=False,
            level=level,
            detail=str(exc),
        )


def _disk_space_check(
    *,
    root: Path,
    warn_threshold_gb: float,
    fail_threshold_gb: float,
) -> ReleasePreflightCheck:
    usage = shutil.disk_usage(root)
    free_gb = usage.free / (1024**3)
    if free_gb < fail_threshold_gb:
        return ReleasePreflightCheck(
            category="resources",
            name="disk_free_gb",
            passed=False,
            level="error",
            detail=f"{free_gb:.2f} GiB remaining",
        )
    if free_gb < warn_threshold_gb:
        return ReleasePreflightCheck(
            category="resources",
            name="disk_free_gb",
            passed=False,
            level="warn",
            detail=f"{free_gb:.2f} GiB remaining",
        )
    return ReleasePreflightCheck(
        category="resources",
        name="disk_free_gb",
        passed=True,
        level="info",
        detail=f"{free_gb:.2f} GiB remaining",
    )


def _port_check(*, host: str, port: int) -> ReleasePreflightCheck:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return ReleasePreflightCheck(
            category="network",
            name="bind_port",
            passed=True,
            level="error",
            detail=f"{host}:{port} available",
        )
    except OSError as exc:
        return ReleasePreflightCheck(
            category="network",
            name="bind_port",
            passed=False,
            level="error",
            detail=str(exc),
        )
    finally:
        sock.close()
