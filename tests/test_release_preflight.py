from __future__ import annotations

import socket
from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.ops.release_preflight import run_release_preflight


def _load_base_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def _prepare_project_root(tmp_path: Path, config: StockAnalyzerConfig) -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "default.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend" / "dist").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text(
        "<div id='root'></div>",
        encoding="utf-8",
    )
    (tmp_path / "artifacts" / "warehouse" / "package").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text("SA__APP__MODE=simulation\n", encoding="utf-8")

    config.market_warehouse.package_root = "artifacts/warehouse/package"
    config.market_warehouse.db_path = "artifacts/warehouse/market.duckdb"
    config.command_channel.state_persist_path = "artifacts/runtime/runtime_state.json"
    config.command_channel.history_archive_dir = "artifacts/runtime/history"
    config.acceptance.export_dir = "artifacts/acceptance"
    config.sim_broker_weekly.export_dir = "artifacts/week7/sim_broker_weekly"
    config.tdx_sync.output_root = "artifacts/imports/tdx_offline_package"
    config.tdx_sync.vipdoc_root = ""
    config.command_channel.secret_key = "really-strong-secret"
    config.notifications.primary = "console"
    config.notifications.backup = "console"


def test_release_preflight_ready_for_tmp_project(tmp_path: Path) -> None:
    config = _load_base_config()
    _prepare_project_root(tmp_path, config)

    report = run_release_preflight(
        config,
        project_root=tmp_path,
        config_path=tmp_path / "config" / "default.yaml",
        bind_port=None,
    )

    assert report.ready is True
    assert (
        report.frontend_dist.endswith("frontend\\dist")
        or report.frontend_dist.endswith("frontend/dist")
    )
    assert "paths:tdx_vipdoc_root" in report.warnings


def test_release_preflight_placeholder_secret_blocks_non_simulation(tmp_path: Path) -> None:
    config = _load_base_config()
    _prepare_project_root(tmp_path, config)
    config.app.mode = "production"
    config.command_channel.secret_key = "change-me"

    report = run_release_preflight(
        config,
        project_root=tmp_path,
        config_path=tmp_path / "config" / "default.yaml",
        bind_port=None,
    )

    assert report.ready is False
    assert "security:command_channel_secret" in report.blockers


def test_release_preflight_detects_busy_port(tmp_path: Path) -> None:
    config = _load_base_config()
    _prepare_project_root(tmp_path, config)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    busy_port = int(sock.getsockname()[1])
    try:
        report = run_release_preflight(
            config,
            project_root=tmp_path,
            config_path=tmp_path / "config" / "default.yaml",
            bind_port=busy_port,
        )
    finally:
        sock.close()

    assert report.ready is False
    assert "network:bind_port" in report.blockers


def test_release_preflight_accepts_feishu_app_notification_credentials(tmp_path: Path) -> None:
    config = _load_base_config()
    _prepare_project_root(tmp_path, config)
    config.notifications.primary = "feishu_app"
    config.notifications.feishu_app_id = "cli_a"
    config.notifications.feishu_app_secret = "cli_s"
    config.notifications.feishu_app_receive_id = "ou_xxx"

    report = run_release_preflight(
        config,
        project_root=tmp_path,
        config_path=tmp_path / "config" / "default.yaml",
        bind_port=None,
    )

    assert "notifications:primary_feishu_app" not in report.blockers


def test_release_preflight_requires_feishu_interaction_credentials(tmp_path: Path) -> None:
    config = _load_base_config()
    _prepare_project_root(tmp_path, config)
    config.feishu_interaction.enabled = True
    config.feishu_interaction.subscription_mode = "webhook"
    config.feishu_interaction.verification_token = ""
    config.notifications.feishu_app_id = ""
    config.notifications.feishu_app_secret = ""

    report = run_release_preflight(
        config,
        project_root=tmp_path,
        config_path=tmp_path / "config" / "default.yaml",
        bind_port=None,
    )

    assert "feishu:verification_token" in report.blockers
    assert "feishu:app_credentials" in report.blockers


def test_release_preflight_accepts_feishu_interaction_configuration(tmp_path: Path) -> None:
    config = _load_base_config()
    _prepare_project_root(tmp_path, config)
    config.feishu_interaction.enabled = True
    config.feishu_interaction.subscription_mode = "webhook"
    config.feishu_interaction.verification_token = "verify-token"
    config.notifications.feishu_app_id = "cli_a"
    config.notifications.feishu_app_secret = "cli_s"

    report = run_release_preflight(
        config,
        project_root=tmp_path,
        config_path=tmp_path / "config" / "default.yaml",
        bind_port=None,
    )

    assert "feishu:verification_token" not in report.blockers
    assert "feishu:app_credentials" not in report.blockers


def test_release_preflight_accepts_feishu_long_connection_without_verification_token(
    tmp_path: Path,
) -> None:
    config = _load_base_config()
    _prepare_project_root(tmp_path, config)
    config.feishu_interaction.enabled = True
    config.feishu_interaction.subscription_mode = "long_connection"
    config.feishu_interaction.verification_token = ""
    config.notifications.feishu_app_id = "cli_a"
    config.notifications.feishu_app_secret = "cli_s"

    report = run_release_preflight(
        config,
        project_root=tmp_path,
        config_path=tmp_path / "config" / "default.yaml",
        bind_port=None,
    )

    assert "feishu:subscription_mode" not in report.blockers
    assert "feishu:verification_token" not in report.blockers
    assert "feishu:app_credentials" not in report.blockers
