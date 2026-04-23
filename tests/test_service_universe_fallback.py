from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [str(item) for item in value]
    assert len(items) == len(value)
    return items


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.state_persist_path = str(tmp_path / "runtime_state.json")
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.evolution.auto_run = False
    config.acceptance.auto_run = False
    config.cloud_backup.enabled = False
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.idle_queue.universe_cache_path = str(tmp_path / "universe_cache.json")
    config.idle_queue.universe_cache_max_age_hours = 24
    return config


def test_universe_falls_back_to_efinance_spot_when_akshare_fails(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "akshare"
    config.data_source.local_data_root = ""
    config.market_warehouse.package_root = ""
    service = StockAnalyzerService(config=config)

    def _raise_akshare() -> list[str]:
        raise RuntimeError("akshare boom")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_akshare)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_akshare)
    _patch_attr(service, "_fetch_a_share_universe_from_efinance", lambda: ["600000", "000001"])

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=2))
    assert resolved["source"] == "efinance_spot"
    assert resolved["count"] == 2
    assert resolved["degraded"] is False


def test_universe_primary_efinance_then_fallback_to_akshare(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "efinance"
    service = StockAnalyzerService(config=config)

    _patch_attr(service, "_fetch_a_share_universe_from_efinance", lambda: ["600000"])
    _patch_attr(service, "_fetch_a_share_universe_from_akshare", lambda: ["600000", "000001"])

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=2))
    assert resolved["source"] == "akshare_spot"
    assert resolved["count"] == 2
    assert resolved["degraded"] is False
    errors = _as_text_list(resolved.get("errors", []))
    assert any("efinance_universe_too_small" in item for item in errors)


def test_universe_offline_mode_uses_watchlist_without_online_fetch(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "akshare"
    config.data_source.local_data_root = ""
    config.market_warehouse.package_root = ""
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000", "000001"]

    def _raise_online() -> list[str]:
        raise AssertionError("online source should not be called")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_online)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_online)
    _patch_attr(service, "_fetch_a_share_universe_from_efinance", _raise_online)

    resolved = _as_mapping(
        service._resolve_symbol_universe(
            max_symbols=10,
            min_symbols=2,
            allow_online_sources=False,
        )
    )
    assert resolved["source"] == "watchlist_fallback"
    assert resolved["count"] == 2
    assert resolved["degraded"] is True
    errors = _as_text_list(resolved.get("errors", []))
    assert "online_sources_disabled" in errors


def test_market_warehouse_primary_prefers_local_package_universe(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "market_warehouse"
    config.data_source.local_data_root = ""
    package_root = tmp_path / "warehouse_package"
    bars_root = package_root / "bars"
    bars_root.mkdir(parents=True, exist_ok=True)
    for symbol in ("600000", "000001", "300001"):
        (bars_root / f"{symbol}.csv").write_text("date,close\n2026-03-16,10\n", encoding="utf-8")
    config.market_warehouse.package_root = str(package_root)

    service = StockAnalyzerService(config=config)

    def _raise_online() -> list[str]:
        raise AssertionError("online source should not be called")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_online)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_online)
    _patch_attr(service, "_fetch_a_share_universe_from_efinance", _raise_online)

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=2))
    assert resolved["source"] == "local_files_primary"
    assert resolved["count"] == 3
    assert resolved["degraded"] is False


def test_market_warehouse_primary_filters_bj_index_codes_from_local_package_universe(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "market_warehouse"
    config.data_source.local_data_root = ""
    package_root = tmp_path / "warehouse_package"
    bars_root = package_root / "bars"
    bars_root.mkdir(parents=True, exist_ok=True)
    for symbol in ("600000", "899050", "810011"):
        (bars_root / f"{symbol}.csv").write_text("date,close\n2026-03-16,10\n", encoding="utf-8")
    config.market_warehouse.package_root = str(package_root)

    service = StockAnalyzerService(config=config)

    def _raise_online() -> list[str]:
        raise AssertionError("online source should not be called")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_online)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_online)
    _patch_attr(service, "_fetch_a_share_universe_from_efinance", _raise_online)

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=1))
    symbols = _as_text_list(resolved.get("symbols", []))

    assert resolved["source"] == "local_files_primary"
    assert resolved["count"] == 1
    assert resolved["degraded"] is False
    assert symbols == ["600000"]
