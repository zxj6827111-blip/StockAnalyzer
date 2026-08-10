from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from threading import Thread
from typing import Any, cast

import pandas as pd
import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.market_warehouse import (
    MarketWarehouse,
    load_package_daily_bars,
    write_package_daily_bars,
)
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _build_daily_frame(
    dates: list[str],
    close_values: list[float],
    *,
    include_date_column: bool,
) -> pd.DataFrame:
    volumes = [1_000_000.0 + index * 100_000.0 for index in range(len(dates))]
    frame = pd.DataFrame(
        {
            "open": [value - 0.1 for value in close_values],
            "high": [value + 0.1 for value in close_values],
            "low": [value - 0.2 for value in close_values],
            "close": close_values,
            "volume": volumes,
            "turnover": [
                volume * close
                for volume, close in zip(volumes, close_values, strict=True)
            ],
            "float_market_cap": [12_000_000_000.0] * len(dates),
        },
        index=pd.to_datetime(dates),
    )
    frame.index.name = "date"
    if not include_date_column:
        return frame
    dated = frame.reset_index()
    dated["date"] = dated["date"].dt.strftime("%Y-%m-%d")
    return dated


_ONLINE_DAILY_FRAME = _build_daily_frame(
    ["2026-03-04", "2026-03-05", "2026-03-06"],
    [10.3, 10.5, 10.8],
    include_date_column=False,
)
_SAMPLE_PACKAGE_FRAME = _build_daily_frame(
    ["2026-03-03", "2026-03-04"],
    [10.1, 10.3],
    include_date_column=True,
)
_UP_TO_DATE_PACKAGE_FRAME = _build_daily_frame(
    ["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"],
    [10.1, 10.3, 10.5, 10.7],
    include_date_column=True,
)


class FakeOnlineProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, date | None]] = []
        self._daily_frame = _ONLINE_DAILY_FRAME

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.calls.append((symbol, lookback_days, end_date))
        _ = symbol
        return self._daily_frame.copy()
        dates = pd.to_datetime(["2026-03-04", "2026-03-05", "2026-03-06"])
        frame = pd.DataFrame(
            {
                "open": [10.2, 10.4, 10.6],
                "high": [10.4, 10.6, 10.9],
                "low": [10.1, 10.3, 10.5],
                "close": [10.3, 10.5, 10.8],
                "volume": [1_100_000.0, 1_200_000.0, 1_300_000.0],
                "turnover": [11_330_000.0, 12_600_000.0, 14_040_000.0],
                "float_market_cap": [12_000_000_000.0, 12_000_000_000.0, 12_000_000_000.0],
                "name": ["示例股份", "示例股份", "示例股份"],
                "roe": [0.12, 0.12, 0.12],
                "debt_ratio": [0.32, 0.32, 0.32],
                "holder_count": [40_100.0, 40_200.0, 40_300.0],
                "block_trade_net": [100_000.0, 0.0, 0.0],
                "financing_balance": [1_010_000_000.0, 1_020_000_000.0, 1_030_000_000.0],
                "margin_financing_balance": [1_010_000_000.0, 1_020_000_000.0, 1_030_000_000.0],
                "northbound_net": [0.0, 200_000.0, 0.0],
                "dragon_tiger_flag": [0.0, 1.0, 0.0],
                "background_data_source": ["akshare", "akshare", "akshare"],
                "background_data_complete": [True, True, True],
                "financial_data_complete": [True, True, True],
                "financial_source": ["akshare", "akshare", "akshare"],
                "financial_missing_fields": ["", "", ""],
                "financial_report_date": ["20251231", "20251231", "20251231"],
                "board": ["main", "main", "main"],
                "suspended": [False, False, False],
                "is_st": [False, False, False],
                "is_delisting_risk": [False, False, False],
            },
            index=dates,
        )
        frame.index.name = "date"
        return frame

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()


class SlowOnlineProvider(FakeOnlineProvider):
    def __init__(self, *, sleep_sec: float) -> None:
        super().__init__()
        self._sleep_sec = sleep_sec

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.calls.append((symbol, lookback_days, end_date))
        time.sleep(self._sleep_sec)
        return self._daily_frame.copy()


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _build_sample_package(root: Path) -> None:
    bars_root = root / "bars"
    bars_root.mkdir(parents=True, exist_ok=True)
    _SAMPLE_PACKAGE_FRAME.to_csv(bars_root / "600000.csv", index=False)
    return
    frame = pd.DataFrame(
        {
            "date": ["2026-03-03", "2026-03-04"],
            "open": [10.0, 10.2],
            "high": [10.2, 10.4],
            "low": [9.9, 10.1],
            "close": [10.1, 10.3],
            "volume": [1_000_000.0, 1_100_000.0],
            "turnover": [10_100_000.0, 11_330_000.0],
            "float_market_cap": [12_000_000_000.0, 12_000_000_000.0],
            "name": ["示例股份", "示例股份"],
            "roe": [0.12, 0.12],
            "debt_ratio": [0.32, 0.32],
        }
    )
    frame.to_csv(bars_root / "600000.csv", index=False)


def _load_test_config(package_root: Path, db_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    # default.yaml 默认禁用 market_warehouse（NAS 部署语义）；本文件全部用例
    # 都在测同步/引导流程，必须显式启用，否则服务直接返回 market_warehouse_disabled。
    config.market_warehouse.enabled = True
    config.data_source.local_data_root = str(package_root)
    config.market_warehouse.package_root = str(package_root)
    config.market_warehouse.bootstrap_source_root = str(package_root)
    config.market_warehouse.db_path = str(db_path)
    config.market_warehouse.offline_bootstrap_enabled = True
    config.market_warehouse.intraday_sync_enabled = False
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = Path(tempfile.gettempdir()) / "stock_analyzer_tests"
    config.training.artifact_path = str(temp_root / "test_model_market_warehouse.json")
    config.training.bootstrap_state_path = str(
        temp_root / "test_bootstrap_state_market_warehouse.json"
    )
    return config


def _market_warehouse_manifest_refresh_stub(
    warehouse: MarketWarehouse,
) -> dict[str, object]:
    return {
        "daily_manifest_path": str((warehouse.package_root / "manifest.json").resolve()),
        "intraday_manifest_path": str(
            (warehouse.package_root / "intraday_summary_manifest.json").resolve()
        ),
    }


def _market_warehouse_cache_refresh_stub() -> dict[str, object]:
    return {
        "deleted_bar_cache_keys": 0,
        "deleted_intraday_cache_keys": 0,
        "inner_provider_daily_cache_cleared": False,
        "inner_provider_intraday_cache_cleared": False,
    }


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    provider = SyntheticProvider(seed_offset=2029)
    original_build_runtime_provider = runtime_service_module.build_runtime_provider
    original_build_realtime_runtime_provider = (
        runtime_service_module.build_realtime_runtime_provider
    )
    original_build_market_depth_provider = runtime_service_module.build_market_depth_provider
    try:
        runtime_service_module.build_runtime_provider = (
            lambda config, synthetic_seed=2026: provider
        )
        runtime_service_module.build_realtime_runtime_provider = (
            lambda config, synthetic_seed=2026, timezone="Asia/Shanghai": provider
        )
        runtime_service_module.build_market_depth_provider = lambda config: None
        service = StockAnalyzerService(config=config)
    finally:
        runtime_service_module.build_runtime_provider = original_build_runtime_provider
        runtime_service_module.build_realtime_runtime_provider = (
            original_build_realtime_runtime_provider
        )
        runtime_service_module.build_market_depth_provider = original_build_market_depth_provider
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", provider)
    runtime_root = Path(config.market_warehouse.db_path).resolve().parent / "runtime_test_artifacts"
    runtime_root.mkdir(parents=True, exist_ok=True)
    _patch_attr(service, "_tdx_sync_history_path", runtime_root / "tdx_sync_history.jsonl")
    _patch_attr(
        service,
        "_market_warehouse_history_path",
        runtime_root / "market_warehouse_history.jsonl",
    )
    _patch_attr(
        service,
        "_market_warehouse_progress_path",
        runtime_root / "market_warehouse_progress.json",
    )
    _patch_attr(service, "_tdx_sync_history", [])
    _patch_attr(service, "_last_tdx_sync_report", None)
    _patch_attr(service, "_market_warehouse_history", [])
    _patch_attr(service, "_last_market_warehouse_report", None)
    _patch_attr(service, "_last_market_warehouse_progress", None)
    warehouse = service._market_warehouse()
    _patch_attr(service, "_market_warehouse", lambda: warehouse)
    _patch_attr(
        warehouse,
        "refresh_package_manifests",
        lambda: _market_warehouse_manifest_refresh_stub(warehouse),
    )
    _patch_attr(service, "_load_tdx_sync_history_from_disk", lambda: None)
    _patch_attr(service, "_load_market_warehouse_history_from_disk", lambda: None)
    _patch_attr(service, "_load_market_warehouse_progress_from_disk", lambda: None)
    _patch_attr(service, "_persist_market_warehouse_history_to_disk", lambda: None)
    _patch_attr(service, "_persist_market_warehouse_progress_to_disk", lambda: None)
    _patch_attr(service, "_record_audit_event", lambda *args, **kwargs: None)
    _patch_attr(service, "_invalidate_market_data_cache", _market_warehouse_cache_refresh_stub)
    return service


def _apply_lightweight_market_warehouse_state(
    service: StockAnalyzerService,
    *,
    latest_daily_dates: Mapping[str, date],
) -> None:
    warehouse = service._market_warehouse()
    daily_dates = {
        str(symbol).strip(): value
        for symbol, value in latest_daily_dates.items()
        if str(symbol).strip()
    }
    _patch_attr(warehouse, "has_daily_data", lambda: True)
    _patch_attr(
        warehouse,
        "latest_daily_dates",
        lambda symbols: {
            normalized: daily_dates[normalized]
            for normalized in [str(symbol).strip() for symbol in symbols]
            if normalized in daily_dates
        },
    )
    _patch_attr(warehouse, "latest_intraday_dates", lambda interval, symbols: {})


def _build_lightweight_daily_sync(service: StockAnalyzerService) -> object:
    def _fake_sync(
        *,
        warehouse: MarketWarehouse,
        online_provider: FakeOnlineProvider,
        symbol: str,
        force: bool,
        target_end_date: date,
        latest_daily: date | None = None,
        hard_timeout_sec: float | None = None,
    ) -> dict[str, object]:
        _ = warehouse, hard_timeout_sec
        lookback_days, sync_mode = service._resolve_market_warehouse_daily_lookback_days(
            latest_date=latest_daily,
            target_end_date=target_end_date,
            force=force,
        )
        if lookback_days <= 0:
            return {
                "status": "skipped",
                "reason": "up_to_date",
                "latest_date": latest_daily.isoformat() if latest_daily else "",
                "mode": sync_mode,
                "lookback_days": 0,
            }

        fresh_daily = online_provider.fetch_daily_bars(
            symbol=symbol,
            lookback_days=lookback_days,
            end_date=target_end_date,
        )
        if fresh_daily.empty:
            return {
                "status": "skipped",
                "reason": "no_online_daily_data",
                "latest_date": latest_daily.isoformat() if latest_daily else "",
                "mode": sync_mode,
                "lookback_days": lookback_days,
            }
        latest_online = pd.Timestamp(fresh_daily.index[-1]).date()
        if latest_daily is not None and not force and latest_online <= latest_daily:
            return {
                "status": "skipped",
                "reason": "no_new_trade_date",
                "latest_date": latest_daily.isoformat(),
                "mode": sync_mode,
                "lookback_days": lookback_days,
            }
        return {
            "status": "ok",
            "latest_date": latest_online.isoformat(),
            "rows": int(len(fresh_daily)),
            "mode": sync_mode,
            "lookback_days": lookback_days,
        }

    return _fake_sync


def test_service_market_warehouse_sync_bootstraps_and_updates_daily_package(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
        )
    )
    bootstrap = _as_mapping(report["bootstrap"])
    daily_sync = _as_mapping(report["daily_sync"])

    assert report["status"] == "ok"
    assert bootstrap["status"] in {"ok", "partial"}
    assert _as_int(daily_sync["ok"]) == 1
    assert provider_online.calls == [("600000", 7, date(2026, 3, 6))]

    package_bars = load_package_daily_bars(source_root=package_root, symbol="600000")
    assert len(package_bars) == 4
    assert package_bars.index[-1].date().isoformat() == "2026-03-06"

    warehouse = MarketWarehouse(db_path=db_path, package_root=package_root)
    warehouse_bars = warehouse.fetch_all_daily_bars(symbol="600000")
    assert len(warehouse_bars) == 4
    assert _as_float(warehouse_bars["close"].iloc[-1]) == 10.8


def _legacy_test_service_market_warehouse_sync_skips_up_to_date_daily_fetch_on_weekend(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    pd.DataFrame(
        {
            "date": ["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"],
            "open": [10.0, 10.2, 10.4, 10.6],
            "high": [10.2, 10.4, 10.6, 10.8],
            "low": [9.9, 10.1, 10.3, 10.5],
            "close": [10.1, 10.3, 10.5, 10.7],
            "volume": [1_000_000.0, 1_100_000.0, 1_200_000.0, 1_300_000.0],
            "turnover": [10_100_000.0, 11_330_000.0, 12_600_000.0, 13_910_000.0],
            "float_market_cap": [12_000_000_000.0] * 4,
            "name": ["绀轰緥鑲′唤"] * 4,
            "roe": [0.12] * 4,
            "debt_ratio": [0.32] * 4,
        }
    ).to_csv(package_root / "bars" / "600000.csv", index=False)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-07 20:30:00").to_pydatetime(),
        )
    )
    daily_sync = _as_mapping(report["daily_sync"])

    assert report["status"] == "ok"
    assert _as_int(daily_sync["ok"]) == 0
    assert _as_int(daily_sync["skipped"]) == 1
    assert daily_sync["target_trade_date"] == "2026-03-06"
    assert provider_online.calls == []


def test_service_market_warehouse_sync_skips_up_to_date_daily_fetch_on_weekend(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 6)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-07 20:30:00").to_pydatetime(),
        )
    )
    daily_sync = _as_mapping(report["daily_sync"])

    assert report["status"] == "ok"
    assert _as_int(daily_sync["ok"]) == 0
    assert _as_int(daily_sync["skipped"]) == 1
    assert daily_sync["target_trade_date"] == "2026-03-06"
    assert provider_online.calls == []


def test_service_market_warehouse_sync_uses_incremental_daily_lookback(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.daily_incremental_cushion_days = 5
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
        )
    )
    daily_sync = _as_mapping(report["daily_sync"])
    mode_counts = _as_mapping(daily_sync["mode_counts"])

    assert report["status"] == "ok"
    assert _as_int(daily_sync["ok"]) == 1
    assert _as_int(mode_counts["incremental"]) == 1
    assert provider_online.calls == [("600000", 7, date(2026, 3, 6))]


def test_service_market_warehouse_sync_filters_bj_index_codes_from_requested_symbols(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.intraday_sync_enabled = False
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000", "899050", "810011"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
        )
    )
    daily_sync = _as_mapping(report["daily_sync"])

    assert report["status"] == "ok"
    assert report["symbol_source"] == "explicit_symbols"
    assert _as_int(report["symbols_total"]) == 1
    assert _as_int(daily_sync["ok"]) == 1
    assert provider_online.calls == [("600000", 7, date(2026, 3, 6))]


def test_service_market_warehouse_sync_skips_stuck_daily_symbol_after_hard_timeout(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.intraday_sync_enabled = False
    config.market_warehouse.daily_symbol_hard_timeout_sec = 0.05
    config.market_warehouse.daily_symbol_hard_timeout_sec_full_universe = 0.01
    service = _new_service(config)
    provider_online = SlowOnlineProvider(sleep_sec=0.2)
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 20)},
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-24 07:00:00").to_pydatetime(),
        )
    )
    daily_sync = _as_mapping(report["daily_sync"])
    failed_samples = cast(list[dict[str, str]], report["failed_samples"])

    assert report["status"] == "failed"
    assert report["reason"] == "all_symbols_failed"
    assert _as_int(daily_sync["failed"]) == 1
    assert _as_float(daily_sync["symbol_hard_timeout_sec"]) == 0.05
    assert daily_sync["symbol_hard_timeout_profile"] == "default"
    assert provider_online.calls == [("600000", 8, date(2026, 3, 23))]
    assert failed_samples
    assert failed_samples[0]["symbol"] == "600000"
    assert "TimeoutError:daily_fetch_600000_timeout_after_0.1s" in failed_samples[0]["reason"]


def test_service_market_warehouse_sync_uses_full_universe_timeout_override(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.intraday_sync_enabled = False
    config.market_warehouse.daily_symbol_hard_timeout_sec = 0.2
    config.market_warehouse.daily_symbol_hard_timeout_sec_full_universe = 0.05
    service = _new_service(config)
    provider_online = SlowOnlineProvider(sleep_sec=0.2)
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 20)},
    )
    _patch_attr(
        service,
        "_select_market_warehouse_symbols",
        lambda warehouse, package_root, max_symbols: ["600000"],
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            force=False,
            timestamp=pd.Timestamp("2026-03-24 07:00:00").to_pydatetime(),
        )
    )
    daily_sync = _as_mapping(report["daily_sync"])
    failed_samples = cast(list[dict[str, str]], report["failed_samples"])

    assert report["symbol_source"] == "full_universe"
    assert report["status"] == "failed"
    assert report["reason"] == "all_symbols_failed"
    assert _as_int(daily_sync["failed"]) == 1
    assert _as_float(daily_sync["symbol_hard_timeout_sec"]) == 0.05
    assert daily_sync["symbol_hard_timeout_profile"] == "full_universe_override"
    assert provider_online.calls == [("600000", 8, date(2026, 3, 23))]
    assert failed_samples
    assert failed_samples[0]["symbol"] == "600000"
    assert "TimeoutError:daily_fetch_600000_timeout_after_0.1s" in failed_samples[0]["reason"]


def test_service_market_warehouse_sync_rejects_concurrent_reentry(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config_primary = _load_test_config(package_root=package_root, db_path=db_path)
    config_secondary = _load_test_config(package_root=package_root, db_path=db_path)
    service_primary = _new_service(config_primary)
    service_secondary = _new_service(config_secondary)
    provider_primary = SlowOnlineProvider(sleep_sec=0.25)
    provider_secondary = FakeOnlineProvider()
    _patch_attr(
        service_primary,
        "_build_market_warehouse_online_provider",
        lambda: provider_primary,
    )
    _patch_attr(
        service_secondary,
        "_build_market_warehouse_online_provider",
        lambda: provider_secondary,
    )
    _apply_lightweight_market_warehouse_state(
        service_primary,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _apply_lightweight_market_warehouse_state(
        service_secondary,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _patch_attr(
        service_primary,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service_primary),
    )
    _patch_attr(
        service_secondary,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service_secondary),
    )

    thread_result: dict[str, object] = {}

    def _run_primary() -> None:
        thread_result["report"] = service_primary.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
            source_trace_id="concurrent-primary",
        )

    worker = Thread(target=_run_primary, daemon=True)
    worker.start()
    lock_path = service_primary._market_sync_service._resolve_market_warehouse_sync_lock_path()
    for _ in range(50):
        if lock_path.exists():
            break
        time.sleep(0.02)

    assert lock_path.exists()

    report_secondary = _as_mapping(
        service_secondary.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:01").to_pydatetime(),
            source_trace_id="concurrent-secondary",
        )
    )
    active_lock = _as_mapping(report_secondary["active_lock"])

    assert report_secondary["status"] == "skipped"
    assert report_secondary["reason"] == "market_warehouse_sync_in_progress"
    assert active_lock["trace_id"] == "concurrent-primary"
    assert provider_secondary.calls == []

    worker.join(timeout=5.0)
    assert not worker.is_alive()

    report_primary = _as_mapping(thread_result["report"])
    assert report_primary["status"] == "ok"
    assert provider_primary.calls == [("600000", 7, date(2026, 3, 6))]
    assert not lock_path.exists()


def test_service_market_warehouse_sync_reclaims_stale_lock(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )
    _patch_attr(
        service._market_sync_service,
        "_resolve_market_warehouse_sync_lock_stale_after_sec",
        lambda: 1,
    )
    lock_path = service._market_sync_service._resolve_market_warehouse_sync_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        '{"owner_token":"stale-owner","trace_id":"stale-run","stale_after_sec":1}\n',
        encoding="utf-8",
    )
    stale_ts = time.time() - 5.0
    os.utime(lock_path, (stale_ts, stale_ts))

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
            source_trace_id="stale-lock-retry",
        )
    )

    assert report["status"] == "ok"
    assert provider_online.calls == [("600000", 7, date(2026, 3, 6))]
    assert not lock_path.exists()


def test_service_market_warehouse_sync_retry_failed_only_uses_latest_failed_symbols(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={
            "600000": date(2026, 3, 4),
            "000001": date(2026, 3, 4),
        },
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )
    _patch_attr(
        service,
        "_last_market_warehouse_report",
        {
            "timestamp": "2026-03-06T20:25:00",
            "trace_id": "nightly-partial",
            "status": "partial",
            "failed_symbols": ["600000", "000001", "600000"],
            "failed_symbols_total": 2,
            "daily_sync": {"failed": 2},
            "intraday_sync": {"failed": 0},
        },
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
            retry_failed_only=True,
        )
    )

    assert report["status"] == "ok"
    assert report["symbol_source"] == "retry_failed_only"
    assert report["retry_source_trace_id"] == "nightly-partial"
    assert report["retry_source_complete"] is True
    assert _as_int(report["retry_symbols_total"]) == 2
    assert _as_int(report["symbols_total"]) == 2
    assert provider_online.calls == [
        ("600000", 7, date(2026, 3, 6)),
        ("000001", 7, date(2026, 3, 6)),
    ]


def test_service_market_warehouse_sync_retry_failed_only_rejects_incomplete_failure_report(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _patch_attr(
        service,
        "_last_market_warehouse_report",
        {
            "timestamp": "2026-03-24T23:58:00",
            "trace_id": "manual-full-universe-sync",
            "status": "partial",
            "daily_sync": {"failed": 759},
            "intraday_sync": {"failed": 0},
            "failed_samples": [
                {
                    "symbol": "600000",
                    "stage": "daily",
                    "reason": "IOException:sample",
                }
            ],
        },
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            force=False,
            timestamp=pd.Timestamp("2026-03-25 00:05:00").to_pydatetime(),
            retry_failed_only=True,
        )
    )

    assert report["status"] == "skipped"
    assert report["reason"] == "retry_source_failed_symbols_incomplete"
    assert report["retry_source_trace_id"] == "manual-full-universe-sync"
    assert report["retry_source_complete"] is False
    assert _as_int(report["retry_source_failed_symbols_total"]) == 759
    assert _as_int(report["retry_source_available_symbols_total"]) == 1
    assert provider_online.calls == []


def test_market_warehouse_background_data_quality_snapshot_reports_freshness_and_coverage(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    warehouse = MarketWarehouse(db_path=db_path, package_root=package_root)
    latest_frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "volume": [1_000_000.0],
            "turnover": [10_100_000.0],
            "float_market_cap": [12_000_000_000.0],
            "name": ["sample"],
            "roe": [0.12],
            "debt_ratio": [0.32],
            "holder_count": [40_300.0],
            "block_trade_net": [100_000.0],
            "financing_balance": [1_030_000_000.0],
            "margin_financing_balance": [1_030_000_000.0],
            "northbound_net": [200_000.0],
            "dragon_tiger_flag": [1.0],
            "background_data_source": ["akshare"],
            "background_data_complete": [True],
            "financial_data_complete": [True],
            "financial_source": ["akshare"],
            "financial_missing_fields": [""],
            "financial_report_date": ["20251231"],
            "board": ["main"],
            "suspended": [False],
            "is_st": [False],
            "is_delisting_risk": [False],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    latest_frame.index.name = "date"
    stale_frame = pd.DataFrame(
        {
            "open": [9.8],
            "high": [10.0],
            "low": [9.7],
            "close": [9.9],
            "volume": [900_000.0],
            "turnover": [8_910_000.0],
            "float_market_cap": [11_500_000_000.0],
            "name": ["sample"],
            "roe": [0.11],
            "debt_ratio": [0.33],
            "holder_count": [None],
            "block_trade_net": [0.0],
            "financing_balance": [None],
            "margin_financing_balance": [None],
            "northbound_net": [0.0],
            "dragon_tiger_flag": [0.0],
            "background_data_source": ["akshare"],
            "background_data_complete": [False],
            "financial_data_complete": [True],
            "financial_source": ["akshare"],
            "financial_missing_fields": [""],
            "financial_report_date": ["20251231"],
            "board": ["main"],
            "suspended": [False],
            "is_st": [False],
            "is_delisting_risk": [False],
        },
        index=pd.to_datetime(["2026-03-05"]),
    )
    stale_frame.index.name = "date"
    warehouse.replace_daily_bars(symbol="600000", frame=latest_frame)
    warehouse.replace_daily_bars(symbol="000001", frame=stale_frame)

    snapshot = _as_mapping(warehouse.background_data_quality_snapshot())
    fields = _as_mapping(snapshot["fields"])
    holder_count = _as_mapping(fields["holder_count"])
    source_distribution = _as_mapping(snapshot["source_distribution"])
    activity_counts = _as_mapping(snapshot["activity_counts"])

    assert snapshot["status"] == "partial"
    assert snapshot["latest_trade_date"] == "2026-03-06"
    assert _as_int(snapshot["symbols_total"]) == 2
    assert _as_int(snapshot["symbols_on_latest_trade_date"]) == 1
    assert _as_int(snapshot["symbols_stale"]) == 1
    assert _as_float(snapshot["latest_trade_date_coverage_ratio"]) == 0.5
    assert _as_int(snapshot["background_complete_count"]) == 1
    assert _as_float(snapshot["background_complete_ratio"]) == 1.0
    assert _as_int(holder_count["non_null_count"]) == 1
    assert _as_int(holder_count["non_zero_count"]) == 1
    assert _as_int(source_distribution["akshare"]) == 1
    assert _as_int(activity_counts["dragon_tiger_flag_non_zero"]) == 1
    assert snapshot["stale_symbols_sample"] == ["000001"]


def test_market_warehouse_manifest_refresh_reports_package_file_drift(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    warehouse = MarketWarehouse(db_path=db_path, package_root=package_root)
    frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.0],
            "volume": [1_000_000.0],
            "turnover": [10_000_000.0],
            "float_market_cap": [12_000_000_000.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    frame.index.name = "date"
    warehouse.replace_daily_bars(symbol="600000", frame=frame)
    warehouse.replace_daily_bars(symbol="000001", frame=frame)
    write_package_daily_bars(package_root=package_root, symbol="600000", frame=frame)

    refresh = warehouse.refresh_package_manifests()
    manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))

    assert refresh["package_consistent"] is False
    assert _as_int(refresh["db_symbols_total"]) == 2
    assert _as_int(refresh["package_symbol_files_total"]) == 1
    assert _as_int(refresh["missing_symbol_files_total"]) == 1
    assert refresh["missing_symbol_files_sample"] == ["000001"]
    assert manifest["package_consistent"] is False
    assert manifest["symbol_files_failed"] == 1


def test_service_background_status_treats_clean_full_sync_staleness_as_nonblocking(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    warehouse = service._market_warehouse()
    _patch_attr(
        warehouse,
        "background_data_quality_snapshot",
        lambda: {
            "status": "partial",
            "reason": "latest_trade_date_not_full_universe",
            "status_reasons": ["latest_trade_date_not_full_universe"],
            "symbols_total": 5190,
            "symbols_on_latest_trade_date": 5180,
            "symbols_stale": 10,
            "stale_symbols_sample": ["002231", "603056"],
        },
    )
    _patch_attr(
        service,
        "_last_market_warehouse_report",
        {
            "timestamp": "2026-03-25T12:34:53.308580",
            "trace_id": "manual-full-universe-sync-20260325-1234",
            "status": "ok",
            "symbol_source": "full_universe",
            "failed_symbols_total": 0,
            "failed_symbols": [],
        },
    )

    payload = _as_mapping(service.market_warehouse_background_data_status())

    assert payload["status"] == "ok"
    assert payload["raw_status"] == "partial"
    assert payload["raw_reason"] == "latest_trade_date_not_full_universe"
    assert payload["nonblocking_reasons"] == ["latest_trade_date_not_full_universe"]
    assert _as_int(payload["latest_sync_failed_symbols_total"]) == 0
    assert _as_int(payload["nonblocking_stale_symbols_total"]) == 10


def test_service_market_warehouse_sync_limits_intraday_to_focus_symbols(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    pd.DataFrame(
        {
            "date": ["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"],
            "open": [10.0, 10.2, 10.4, 10.6],
            "high": [10.2, 10.4, 10.6, 10.8],
            "low": [9.9, 10.1, 10.3, 10.5],
            "close": [10.1, 10.3, 10.5, 10.7],
            "volume": [1_000_000.0, 1_100_000.0, 1_200_000.0, 1_300_000.0],
            "turnover": [10_100_000.0, 11_330_000.0, 12_600_000.0, 13_910_000.0],
            "float_market_cap": [12_000_000_000.0] * 4,
            "name": ["绀轰緥鑲′唤"] * 4,
            "roe": [0.12] * 4,
            "debt_ratio": [0.32] * 4,
        }
    ).to_csv(package_root / "bars" / "000001.csv", index=False)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.intraday_sync_enabled = True
    config.market_warehouse.intraday_intervals = ["1m"]
    config.market_warehouse.intraday_sync_scope = "focus"
    config.market_warehouse.intraday_focus_max_symbols = 10
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={
            "600000": date(2026, 3, 6),
            "000001": date(2026, 3, 6),
        },
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )
    service.state.watchlist = ["600000"]
    intraday_calls: list[tuple[str, str]] = []

    def _fake_sync(**kwargs: object) -> dict[str, object]:
        intraday_calls.append((str(kwargs["symbol"]), str(kwargs["interval"])))
        return {"status": "ok", "interval": str(kwargs["interval"])}

    _patch_attr(service, "_sync_market_warehouse_intraday_symbol", _fake_sync)

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000", "000001"],
            force=False,
            timestamp=pd.Timestamp("2026-03-07 20:30:00").to_pydatetime(),
        )
    )
    intraday_sync = _as_mapping(report["intraday_sync"])

    assert report["status"] == "ok"
    assert _as_int(intraday_sync["symbols_targeted"]) == 1
    assert intraday_calls == [("600000", "1m")]


def test_service_market_warehouse_sync_bootstraps_online_when_offline_bootstrap_disabled(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "warehouse" / "market.duckdb"
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.offline_bootstrap_enabled = False
    config.market_warehouse.online_bootstrap_lookback_days = 750
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    warehouse = service._market_warehouse()
    _patch_attr(warehouse, "has_daily_data", lambda: False)
    _patch_attr(warehouse, "latest_daily_dates", lambda symbols: {})
    _patch_attr(warehouse, "latest_intraday_dates", lambda interval, symbols: {})
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-10 20:30:00").to_pydatetime(),
        )
    )
    bootstrap = _as_mapping(report["bootstrap"])
    daily_sync = _as_mapping(report["daily_sync"])
    mode_counts = _as_mapping(daily_sync["mode_counts"])

    assert report["status"] == "ok"
    assert bootstrap["status"] == "skipped"
    assert bootstrap["reason"] == "offline_bootstrap_disabled"
    assert _as_int(daily_sync["ok"]) == 1
    assert _as_int(mode_counts["bootstrap"]) == 1
    assert provider_online.calls == [("600000", 750, date(2026, 3, 10))]


def test_service_market_warehouse_sync_uses_target_trade_date_as_online_end_date(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.intraday_sync_enabled = False
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 20)},
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-24 10:32:00").to_pydatetime(),
        )
    )

    assert report["target_trade_date"] == "2026-03-23"
    assert provider_online.calls == [("600000", 8, date(2026, 3, 23))]


def test_service_market_warehouse_sync_updates_progress_snapshot(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-10 20:30:00").to_pydatetime(),
        )
    )
    progress = service.latest_market_warehouse_progress()

    assert report["status"] == "ok"
    assert report["progress_path"] == service._to_evolution_relative(
        service._market_warehouse_progress_path
    )
    assert progress is not None
    progress_view = _as_mapping(progress)
    assert progress_view["status"] == "ok"
    assert progress_view["phase"] == "completed"
    assert _as_int(progress_view["symbols_total"]) == 1
    assert _as_int(progress_view["symbols_completed"]) == 1
    assert _as_float(progress_view["progress_ratio"]) == 1.0


def test_service_market_warehouse_sync_refreshes_progress_when_symbol_changes(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    config.market_warehouse.intraday_sync_enabled = False
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    symbols = [f"{600000 + index:06d}" for index in range(41)]
    progress_writes: list[dict[str, object]] = []
    original_store_progress = service._store_market_warehouse_progress
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={symbol: date(2026, 3, 4) for symbol in symbols},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )

    def _capture_progress(progress: dict[str, object]) -> None:
        progress_writes.append(dict(progress))
        original_store_progress(progress)

    _patch_attr(service, "_store_market_warehouse_progress", _capture_progress)

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=symbols,
            force=False,
            timestamp=pd.Timestamp("2026-03-10 20:30:00").to_pydatetime(),
        )
    )

    assert report["status"] == "ok"
    assert len(progress_writes) > 5
    assert any(
        str(progress.get("current_symbol", "")) == "600004"
        and str(progress.get("current_stage", "")) == "daily"
        for progress in progress_writes
    )
    assert any(
        str(progress.get("current_symbol", "")) == "600040"
        and str(progress.get("current_stage", "")) == "daily"
        for progress in progress_writes
    )
    assert any(
        str(progress.get("phase", "")) == "completed"
        and str(progress.get("current_stage", "")) == "finalize"
        for progress in progress_writes
    )


def test_service_market_warehouse_sync_materializes_package_when_missing(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )
    warehouse = service._market_warehouse()
    materialize_calls = 0

    def _fake_materialize_runtime_package() -> dict[str, object]:
        nonlocal materialize_calls
        materialize_calls += 1
        return {
            "status": "ok",
            "symbols_total": 1,
            "daily_written": 1,
            "intraday_written": {"1m": 0, "5m": 0},
        }

    _patch_attr(warehouse, "has_materialized_package", lambda: False)
    _patch_attr(warehouse, "materialize_runtime_package", _fake_materialize_runtime_package)

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-10 20:30:00").to_pydatetime(),
        )
    )
    package_materialization = _as_mapping(report["package_materialization"])

    assert report["status"] == "ok"
    assert materialize_calls == 1
    assert package_materialization["status"] == "ok"
    assert _as_int(package_materialization["daily_written"]) == 1


def test_price_series_action_rebuild_required_for_legacy(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    warehouse = service._market_warehouse()
    warehouse.bootstrap_from_offline_package(source_root=package_root)
    # No warehouse_meta => unknown contract
    action = service._resolve_market_warehouse_price_series_action(
        warehouse=warehouse,
        symbol="600000",
        fresh_meta={
            "price_series_mode": "qfq",
            "adjustment_source": "tushare_adj_factor",
            "adjustment_anchor_date": "2026-03-06",
            "adjustment_anchor_factor": 16.0,
        },
        force=False,
    )
    assert action["action"] == "rebuild_required"
    assert "legacy" in str(action["reason"])


def test_price_series_action_reanchor_when_factor_changes(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    warehouse = service._market_warehouse()
    warehouse.bootstrap_from_offline_package(source_root=package_root)
    warehouse.update_price_series_contract(
        symbol="600000",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-03-04",
        adjustment_anchor_factor=10.0,
    )
    action = service._resolve_market_warehouse_price_series_action(
        warehouse=warehouse,
        symbol="600000",
        fresh_meta={
            "price_series_mode": "qfq",
            "adjustment_source": "tushare_adj_factor",
            "adjustment_anchor_date": "2026-03-06",
            "adjustment_anchor_factor": 20.0,
        },
        force=True,
    )
    assert action["action"] == "reanchor"
    assert action.get("force_is_not_full_rebuild") is True


def test_price_series_action_uses_only_requested_symbol_contract(
    tmp_path: Path,
) -> None:
    config = _load_test_config(
        package_root=tmp_path / "package",
        db_path=tmp_path / "warehouse" / "market.duckdb",
    )
    service = _new_service(config)
    warehouse = service._market_warehouse()
    frame = _build_daily_frame(
        ["2026-03-04"],
        [10.0],
        include_date_column=False,
    )
    warehouse.replace_daily_bars(symbol="600000", frame=frame)
    warehouse.replace_daily_bars(symbol="000001", frame=frame)
    warehouse.update_price_series_contract(
        symbol="600000",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-03-04",
        adjustment_anchor_factor=10.0,
    )
    warehouse.update_price_series_contract(
        symbol="000001",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-03-04",
        adjustment_anchor_factor=20.0,
    )

    action = service._resolve_market_warehouse_price_series_action(
        warehouse=warehouse,
        symbol="000001",
        fresh_meta={
            "price_series_mode": "qfq",
            "adjustment_source": "tushare_adj_factor",
            "adjustment_anchor_date": "2026-03-06",
            "adjustment_anchor_factor": 20.0,
        },
        force=False,
    )

    assert action["action"] == "incremental"
    assert _as_mapping(action["contract"])["adjustment_anchor_factor"] == 20.0


def test_carry_forward_trusted_financial_as_of_and_pending(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)

    existing = pd.DataFrame(
        {
            "open": [10.0, 10.1, 10.2],
            "high": [10.0, 10.1, 10.2],
            "low": [10.0, 10.1, 10.2],
            "close": [10.0, 10.1, 10.2],
            "volume": [1.0, 1.0, 1.0],
            "turnover": [1.0, 1.0, 1.0],
            "float_market_cap": [1.0, 1.0, 1.0],
            "roe": [0.12, 0.12, 0.12],
            "debt_ratio": [0.3, 0.3, 0.3],
            "financial_data_complete": [True, True, True],
            "financial_missing_fields": ["", "", ""],
            "financial_source": ["reported_filing", "reported_filing", "reported_filing"],
            "financial_report_date": ["20251231", "20251231", "20251231"],
            "financial_as_of": ["2026-03-04", "2026-03-04", "2026-03-04"],
            "financial_trust_level": ["reported", "reported", "reported"],
            "financial_completeness": [1.0, 1.0, 1.0],
        },
        index=pd.to_datetime(["2026-03-03", "2026-03-04", "2026-03-05"]),
    )
    existing.index.name = "date"

    # Fresh pending rows before and after as-of
    fresh = pd.DataFrame(
        {
            "open": [10.3, 10.4],
            "high": [10.3, 10.4],
            "low": [10.3, 10.4],
            "close": [10.3, 10.4],
            "volume": [1.0, 1.0],
            "turnover": [1.0, 1.0],
            "float_market_cap": [1.0, 1.0],
            "roe": [float("nan"), float("nan")],
            "debt_ratio": [float("nan"), float("nan")],
            "financial_data_complete": [False, False],
            "financial_missing_fields": ["roe,debt_ratio", "roe,debt_ratio"],
            "financial_source": ["tushare_pending", "tushare_pending"],
            "financial_report_date": ["", ""],
            "financial_as_of": ["", ""],
            "financial_trust_level": ["missing", "missing"],
            "financial_completeness": [0.0, 0.0],
        },
        index=pd.to_datetime(["2026-03-03", "2026-03-06"]),
    )
    fresh.index.name = "date"

    carried = service._carry_forward_market_warehouse_financial_fields(
        existing_daily=existing,
        fresh_daily=fresh,
    )
    # Before as-of: no carry
    assert pd.isna(carried.loc[pd.Timestamp("2026-03-03"), "roe"])
    # After as-of: carry trusted values; pending does not wipe truth
    assert float(carried.loc[pd.Timestamp("2026-03-06"), "roe"]) == pytest.approx(0.12)
    assert carried.loc[pd.Timestamp("2026-03-06"), "financial_trust_level"] == "reported"
    assert carried.loc[pd.Timestamp("2026-03-06"), "financial_source"] == "reported_filing"


def test_carry_forward_skips_heuristic_default(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)

    existing = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.0],
            "low": [10.0],
            "close": [10.0],
            "volume": [1.0],
            "turnover": [1.0],
            "float_market_cap": [1.0],
            "roe": [0.08],
            "debt_ratio": [0.55],
            "financial_data_complete": [False],
            "financial_missing_fields": [""],
            "financial_source": ["akshare_tx_default"],
            "financial_report_date": [""],
            "financial_as_of": ["2026-03-04"],
            "financial_trust_level": ["heuristic"],
            "financial_completeness": [0.5],
        },
        index=pd.to_datetime(["2026-03-04"]),
    )
    existing.index.name = "date"
    fresh = pd.DataFrame(
        {
            "open": [10.1],
            "high": [10.1],
            "low": [10.1],
            "close": [10.1],
            "volume": [1.0],
            "turnover": [1.0],
            "float_market_cap": [1.0],
            "roe": [float("nan")],
            "debt_ratio": [float("nan")],
            "financial_data_complete": [False],
            "financial_missing_fields": ["roe,debt_ratio"],
            "financial_source": ["tushare_pending"],
            "financial_report_date": [""],
            "financial_as_of": [""],
            "financial_trust_level": ["missing"],
            "financial_completeness": [0.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    fresh.index.name = "date"
    carried = service._carry_forward_market_warehouse_financial_fields(
        existing_daily=existing,
        fresh_daily=fresh,
    )
    assert pd.isna(carried.loc[pd.Timestamp("2026-03-06"), "roe"])
    assert carried.loc[pd.Timestamp("2026-03-06"), "financial_source"] == "tushare_pending"


def test_carry_forward_uses_latest_effective_financial_snapshot(
    tmp_path: Path,
) -> None:
    config = _load_test_config(
        package_root=tmp_path / "package",
        db_path=tmp_path / "warehouse" / "market.duckdb",
    )
    service = _new_service(config)
    existing = pd.DataFrame(
        {
            "roe": [0.10, 0.20],
            "debt_ratio": [0.30, 0.40],
            "financial_data_complete": [True, True],
            "financial_missing_fields": ["", ""],
            "financial_source": ["reported_filing", "reported_filing"],
            "financial_report_date": ["20251231", "20260331"],
            "financial_as_of": ["2026-03-04", "2026-04-01"],
            "financial_trust_level": ["reported", "reported"],
            "financial_completeness": [1.0, 1.0],
        },
        index=pd.to_datetime(["2026-03-04", "2026-04-01"]),
    )
    existing.index.name = "date"
    fresh = pd.DataFrame(
        {
            "roe": [float("nan")] * 3,
            "debt_ratio": [float("nan")] * 3,
            "financial_data_complete": [False] * 3,
            "financial_missing_fields": ["roe,debt_ratio"] * 3,
            "financial_source": ["tushare_pending"] * 3,
            "financial_report_date": [""] * 3,
            "financial_as_of": [""] * 3,
            "financial_trust_level": ["missing"] * 3,
            "financial_completeness": [0.0] * 3,
        },
        index=pd.to_datetime(["2026-03-03", "2026-03-05", "2026-04-02"]),
    )
    fresh.index.name = "date"

    carried = service._carry_forward_market_warehouse_financial_fields(
        existing_daily=existing,
        fresh_daily=fresh,
    )

    assert pd.isna(carried.loc[pd.Timestamp("2026-03-03"), "roe"])
    assert float(carried.loc[pd.Timestamp("2026-03-05"), "roe"]) == pytest.approx(0.10)
    assert float(carried.loc[pd.Timestamp("2026-04-02"), "roe"]) == pytest.approx(0.20)
    assert float(carried.loc[pd.Timestamp("2026-04-02"), "debt_ratio"]) == pytest.approx(
        0.40
    )


def test_sync_counts_returned_rebuild_required_as_failed(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir(parents=True, exist_ok=True)
    config = _load_test_config(
        package_root=package_root,
        db_path=tmp_path / "warehouse" / "market.duckdb",
    )
    config.market_warehouse.intraday_sync_enabled = True
    config.market_warehouse.intraday_intervals = ["5m"]
    config.market_warehouse.intraday_sync_scope = "all"
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 4)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        lambda **kwargs: {
            "status": "failed",
            "reason": "legacy_price_series_metadata_unknown",
            "mode": "rebuild_required",
        },
    )
    intraday_calls: list[str] = []
    _patch_attr(
        service,
        "_sync_market_warehouse_intraday_symbol",
        lambda **kwargs: intraday_calls.append(str(kwargs["symbol"])),
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
        )
    )
    daily_sync = _as_mapping(report["daily_sync"])
    intraday_sync = _as_mapping(report["intraday_sync"])

    assert report["status"] == "failed"
    assert report["reason"] == "all_symbols_failed"
    assert _as_int(daily_sync["failed"]) == 1
    assert _as_int(daily_sync["skipped"]) == 0
    assert _as_int(intraday_sync["ok"]) == 0
    assert intraday_calls == []
    assert report["failed_symbols"] == ["600000"]
    assert _as_int(report["failed_symbols_total"]) == 1
    assert cast(list[dict[str, str]], report["failed_samples"])[0]["reason"] == (
        "legacy_price_series_metadata_unknown"
    )


def test_partial_enrichment_symbol_is_retryable(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    db_path = tmp_path / "warehouse" / "market.duckdb"
    _build_sample_package(package_root)
    config = _load_test_config(package_root=package_root, db_path=db_path)
    service = _new_service(config)
    provider_online = FakeOnlineProvider()
    _patch_attr(service, "_build_market_warehouse_online_provider", lambda: provider_online)
    _apply_lightweight_market_warehouse_state(
        service,
        latest_daily_dates={"600000": date(2026, 3, 6)},
    )
    _patch_attr(
        service,
        "_sync_market_warehouse_daily_symbol",
        _build_lightweight_daily_sync(service),
    )
    _patch_attr(
        service._market_sync_service,
        "_enrich_market_warehouse_symbol",
        lambda **kwargs: {
            "status": "partial",
            "failed_phases": [],
            "partial_phases": ["p2"],
            "phases": {},
        },
    )

    report = _as_mapping(
        service.run_market_warehouse_sync(
            symbols=["600000"],
            force=False,
            timestamp=pd.Timestamp("2026-03-06 20:30:00").to_pydatetime(),
        )
    )

    assert report["status"] == "partial"
    assert report["failed_symbols"] == ["600000"]
    assert _as_int(report["failed_symbols_total"]) == 1
    retry_plan = _as_mapping(report["retry_plan"])
    assert retry_plan["symbols"] == ["600000"]
    assert retry_plan["next_action"] == "targeted_retry"


def test_reanchor_failure_does_not_advance_symbol_meta(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    config = _load_test_config(
        package_root=package_root,
        db_path=tmp_path / "warehouse" / "market.duckdb",
    )
    service = _new_service(config)
    warehouse = service._market_warehouse()
    existing = _build_daily_frame(
        ["2026-03-02", "2026-03-03"],
        [10.0, 10.2],
        include_date_column=False,
    )
    existing["price_series_mode"] = "qfq"
    existing["adjustment_source"] = "tushare_adj_factor"
    existing["adjustment_anchor_date"] = "2026-03-03"
    existing["adjustment_anchor_factor"] = 10.0
    warehouse.replace_daily_bars(symbol="600000", frame=existing)
    warehouse.update_price_series_contract(
        symbol="600000",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-03-03",
        adjustment_anchor_factor=10.0,
    )

    provider_online = FakeOnlineProvider()
    fresh = _build_daily_frame(
        ["2026-03-03", "2026-03-04"],
        [5.1, 5.2],
        include_date_column=False,
    )
    fresh["price_series_mode"] = "qfq"
    fresh["adjustment_source"] = "tushare_adj_factor"
    fresh["adjustment_anchor_date"] = "2026-03-04"
    fresh["adjustment_anchor_factor"] = 20.0
    fresh.attrs["price_series_meta"] = {
        "price_series_mode": "qfq",
        "adjustment_source": "tushare_adj_factor",
        "adjustment_anchor_date": "2026-03-04",
        "adjustment_anchor_factor": 20.0,
    }
    provider_online._daily_frame = fresh

    def _fail_reanchor(**kwargs: object) -> pd.DataFrame:
        _ = kwargs
        raise ValueError("simulated_reanchor_failure")

    _patch_attr(warehouse, "handle_reanchor_symbol", _fail_reanchor)
    with pytest.raises(ValueError, match="simulated_reanchor_failure"):
        service._sync_market_warehouse_daily_symbol(
            warehouse=warehouse,
            online_provider=provider_online,
            symbol="600000",
            force=False,
            target_end_date=date(2026, 3, 4),
            latest_daily=date(2026, 3, 3),
        )

    assert provider_online.calls == [("600000", 6, date(2026, 3, 4))]
    assert warehouse.price_series_contract(symbol="600000")[
        "adjustment_anchor_factor"
    ] == 10.0


def test_reanchor_sync_rebases_full_history_with_single_fetch(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    config = _load_test_config(
        package_root=package_root,
        db_path=tmp_path / "warehouse" / "market.duckdb",
    )
    service = _new_service(config)
    warehouse = service._market_warehouse()
    existing = _build_daily_frame(
        ["2026-03-02", "2026-03-03"],
        [10.0, 10.2],
        include_date_column=False,
    )
    existing["price_series_mode"] = "qfq"
    existing["adjustment_source"] = "tushare_adj_factor"
    existing["adjustment_anchor_date"] = "2026-03-03"
    existing["adjustment_anchor_factor"] = 10.0
    warehouse.replace_daily_bars(symbol="600000", frame=existing)
    warehouse.update_price_series_contract(
        symbol="600000",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-03-03",
        adjustment_anchor_factor=10.0,
    )

    provider_online = FakeOnlineProvider()
    fresh = _build_daily_frame(
        ["2026-03-03", "2026-03-04"],
        [5.1, 5.2],
        include_date_column=False,
    )
    fresh["price_series_mode"] = "qfq"
    fresh["adjustment_source"] = "tushare_adj_factor"
    fresh["adjustment_anchor_date"] = "2026-03-04"
    fresh["adjustment_anchor_factor"] = 20.0
    fresh.attrs["price_series_meta"] = {
        "price_series_mode": "qfq",
        "adjustment_source": "tushare_adj_factor",
        "adjustment_anchor_date": "2026-03-04",
        "adjustment_anchor_factor": 20.0,
    }
    provider_online._daily_frame = fresh

    result = _as_mapping(
        service._sync_market_warehouse_daily_symbol(
            warehouse=warehouse,
            online_provider=provider_online,
            symbol="600000",
            force=False,
            target_end_date=date(2026, 3, 4),
            latest_daily=date(2026, 3, 3),
        )
    )

    loaded = warehouse.fetch_all_daily_bars(symbol="600000")
    packaged = load_package_daily_bars(source_root=package_root, symbol="600000")
    assert result["status"] == "ok"
    assert result["mode"] == "reanchor"
    assert provider_online.calls == [("600000", 6, date(2026, 3, 4))]
    assert len(loaded) == 3
    assert len(packaged) == 3
    assert float(loaded.loc[pd.Timestamp("2026-03-02"), "close"]) == pytest.approx(5.0)
    assert float(loaded.loc[pd.Timestamp("2026-03-02"), "volume"]) == 1_000_000.0
    assert float(loaded.loc[pd.Timestamp("2026-03-02"), "turnover"]) == 10_000_000.0
    assert warehouse.price_series_contract(symbol="600000")[
        "adjustment_anchor_factor"
    ] == 20.0
