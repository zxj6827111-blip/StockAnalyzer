from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

from stock_analyzer.config import DataSourceConfig
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.provider_factory import build_primary_provider
from stock_analyzer.data.vendor_zip_overlay import (
    VendorZipOverlayProvider,
    write_vendor_zip_daily_index,
)
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.market_sync_service import RuntimeMarketSyncService


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content.encode("utf-8"))


def _build_daily_fixture(root: Path) -> Path:
    daily_csv = "\n".join(
        [
            "code,datetime,open,high,low,close,volume,amount,circ_mv",
            "600000.SH,2025-12-30,10,11,9,10.5,100,123.4,200000",
            "600000.SH,2025-12-31,10.5,11.5,10,11,200,234.5,210000",
        ]
    )
    _write_zip(root / "全A日K" / "2025.zip", {"2025/600000.SH.csv": daily_csv})
    duplicate_csv = daily_csv.replace("600000.SH", "000001.SZ")
    _write_zip(
        root / "全A日K" / "2025(1).zip",
        {"2025/000001.SZ.csv": duplicate_csv},
    )
    index_path = root / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=root, output_path=index_path)
    return index_path


def _provider(root: Path, index_path: Path) -> VendorZipOverlayProvider:
    return VendorZipOverlayProvider(
        data_root=str(root),
        index_path=str(index_path),
        delta_db_path=str(root / "delta" / "market_delta.duckdb"),
        delta_package_root=str(root / "delta" / "package"),
    )


def test_daily_index_prefers_canonical_archive_and_records_latest_date(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    assert provider.list_symbols() == ["600000"]
    assert provider.latest_daily_dates() == {"600000": date(2025, 12, 31)}
    status = provider.status()
    assert status["symbols_total"] == 1
    assert status["index_archives_total"] == 1
    assert status["delta_package_writes_enabled"] is False


def test_daily_overlay_scales_vendor_units_and_delta_wins_duplicate_date(
    tmp_path: Path,
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    delta_package = tmp_path / "delta" / "package"
    warehouse = MarketWarehouse(db_path=delta_db, package_root=delta_package)
    delta = pd.DataFrame(
        {
            "open": [11.0, 12.5],
            "high": [12.0, 13.5],
            "low": [10.5, 12.0],
            "close": [12.0, 13.0],
            "volume": [9999.0, 8888.0],
            "turnover": [999_900.0, 888_800.0],
            "float_market_cap": [2_200_000_000.0, 2_300_000_000.0],
            "price_series_mode": ["raw", "raw"],
            "adjustment_source": ["tushare_raw", "tushare_raw"],
        },
        index=pd.to_datetime(["2025-12-31", "2026-01-02"]),
    )
    warehouse.replace_daily_bars(symbol="600000", frame=delta)

    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_package_root=str(delta_package),
    )
    bars = provider.fetch_daily_bars("600000", lookback_days=10)

    assert list(bars.index.strftime("%Y-%m-%d")) == [
        "2025-12-30",
        "2025-12-31",
        "2026-01-02",
    ]
    assert float(bars.loc["2025-12-30", "volume"]) == 10_000.0
    assert float(bars.loc["2025-12-30", "turnover"]) == 123_400.0
    assert float(bars.loc["2025-12-31", "close"]) == 12.0
    assert float(bars.loc["2025-12-31", "volume"]) == 9999.0
    assert provider.latest_daily_dates()["600000"] == date(2026, 1, 2)


def test_vendor_minute_zip_is_loaded_on_demand(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    today = date.today()
    interval_dir = tmp_path / "沪深分钟数据" / "Stock_1min_2000-now"
    archive_name = f"{today:%Y-%m}_1min.zip"
    minute_csv = "\n".join(
        [
            "datetime,code,name,open,close,high,low,volume,amount,pct_chg,amplitude",
            f"{today.isoformat()} 09:30:00,sh600000,测试,10,10.1,10.2,9.9,10,10000,1,3",
            f"{today.isoformat()} 09:31:00,sh600000,测试,10.1,10.2,10.3,10,20,20400,1,3",
        ]
    )
    entry_name = f"{today:%Y-%m}_1min/{today:%Y%m%d}_1min/sh600000.csv"
    _write_zip(interval_dir / archive_name, {entry_name: minute_csv})

    summary = _provider(tmp_path, index_path).fetch_intraday_summary(
        "600000",
        "1m",
        lookback_days=5,
    )

    assert len(summary) == 1
    assert float(summary.iloc[-1]["minute_count"]) == 2.0


def test_provider_factory_builds_vendor_overlay(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    config = DataSourceConfig(
        primary="vendor_zip_overlay",
        local_data_root=str(tmp_path),
        warehouse_db_path=str(tmp_path / "delta" / "market_delta.duckdb"),
        vendor_zip_index_path=str(index_path),
    )

    provider = build_primary_provider(config)

    assert isinstance(provider, VendorZipOverlayProvider)


def _write_factors_zip(root: Path, entries: dict[str, str]) -> None:
    """Write ``复权因子/复权因子_前复权.zip`` with ``YYYY/<ts_code>.csv`` entries.

    Factor CSV header is ``股票代码,交易日期,复权因子``; dates are ``YYYYMMDD``.
    """
    _write_zip(root / "复权因子" / "复权因子_前复权.zip", entries)


def _build_qfq_daily_fixture(root: Path) -> Path:
    """Daily fixture with an ex-dividend gap: raw close 10.0 -> 9.0 on 12/31.

    Factors: 0.9 up to 2025-12-30, then 1.0 from 2025-12-31 (latest anchor),
    so the qfq close series is continuous (9.0, 9.0) while raw is not.
    """
    daily_csv = "\n".join(
        [
            "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount",
            "600000.SH,2025-12-30,10,11,9,10,10.5,0,0,100,123.4",
            "600000.SH,2025-12-31,9,9.5,8.5,9,10,0,0,200,234.5",
        ]
    )
    _write_zip(root / "全A日K" / "2025.zip", {"2025/600000.SH.csv": daily_csv})
    _write_factors_zip(
        root,
        {
            "2025/600000.SH.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "600000.SH,20251201,0.9",
                    "600000.SH,20251231,1.0",
                ]
            )
        },
    )
    index_path = root / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=root, output_path=index_path)
    return index_path


def _qfq_provider(root: Path, index_path: Path) -> VendorZipOverlayProvider:
    return VendorZipOverlayProvider(
        data_root=str(root),
        index_path=str(index_path),
        delta_db_path=str(root / "delta" / "market_delta.duckdb"),
        delta_package_root=str(root / "delta" / "package"),
        price_series_mode="qfq",
    )


def test_vendor_overlay_accepts_qfq_mode(tmp_path: Path) -> None:
    index_path = _build_qfq_daily_fixture(tmp_path)
    provider = _qfq_provider(tmp_path, index_path)

    assert provider.price_series_mode == "qfq"
    assert provider.status()["price_series_mode"] == "qfq"


def test_vendor_overlay_qfq_tolerates_history_before_factor_start(tmp_path: Path) -> None:
    """早于因子表起点的历史段用原始价（因子 1.0）填充而非 fail-closed。

    前复权因子表有历史起点（Tushare 通常从 2001 年起），而老票日 K 可能
    更早（2000 甚至 1999）——生产里这些早段是独立的年度 zip 条目，整个
    条目都在因子起点之前，bfill 无因子可借、全部 NaN。回归：旧实现直接抛
    DataSourceError，导致整只票在 qfq 模式下读不到（曾引发 285 只票回填
    失败与 offhours 无限重试风暴）。
    """
    # 2025 条目：全部日期在因子起点（2026-01-02）之前 → 因子 1.0。
    old_csv = "\n".join(
        [
            "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount",
            "600000.SH,2025-11-27,10,11,9,10,0,0,0,100,123.4",
            "600000.SH,2025-11-28,10.5,11.5,10,10.5,10,0,0,120,140.0",
        ]
    )
    _write_zip(tmp_path / "全A日K" / "2025.zip", {"2025/600000.SH.csv": old_csv})
    # 2026 条目：因子覆盖（0.9 / 1.0）。
    new_csv = "\n".join(
        [
            "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount",
            "600000.SH,2026-01-02,9,9.5,8.5,9,10,0,0,200,234.5",
            "600000.SH,2026-01-05,9.2,9.8,9,9.3,9,0,0,150,180.0",
        ]
    )
    _write_zip(tmp_path / "全A日K" / "2026.zip", {"2026/600000.SH.csv": new_csv})
    _write_factors_zip(
        tmp_path,
        {
            "2026/600000.SH.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "600000.SH,20260102,0.9",
                    "600000.SH,20260105,1.0",
                ]
            )
        },
    )
    index_path = tmp_path / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)
    provider = _qfq_provider(tmp_path, index_path)

    bars = provider.fetch_daily_bars("600000", lookback_days=10)

    assert list(bars.index.strftime("%Y-%m-%d")) == [
        "2025-11-27",
        "2025-11-28",
        "2026-01-02",
        "2026-01-05",
    ]
    # 因子起点（2026-01-02）之前的历史段用因子 1.0：保持原始价。
    assert float(bars.loc["2025-11-27", "close"]) == pytest.approx(10.0)
    assert float(bars.loc["2025-11-28", "close"]) == pytest.approx(10.5)
    assert float(bars.loc["2025-11-27", "open"]) == pytest.approx(10.0)
    # 因子起点后的日期照常乘因子（0.9 / 1.0）。
    assert float(bars.loc["2026-01-02", "close"]) == pytest.approx(9.0 * 0.9)
    assert float(bars.loc["2026-01-05", "close"]) == pytest.approx(9.3 * 1.0)


def test_vendor_overlay_rejects_unverified_hfq_price_mode(tmp_path: Path) -> None:
    index_path = _build_qfq_daily_fixture(tmp_path)
    with pytest.raises(DataSourceError, match="only raw or qfq"):
        VendorZipOverlayProvider(
            data_root=str(tmp_path),
            index_path=str(index_path),
            delta_db_path=str(tmp_path / "delta.duckdb"),
            price_series_mode="hfq",
        )


def test_vendor_overlay_qfq_multiplies_prices_keeps_volume_and_marks_metadata(
    tmp_path: Path,
) -> None:
    index_path = _build_qfq_daily_fixture(tmp_path)
    provider = _qfq_provider(tmp_path, index_path)

    bars = provider.fetch_daily_bars("600000", lookback_days=10)

    assert list(bars.index.strftime("%Y-%m-%d")) == ["2025-12-30", "2025-12-31"]
    # qfq close = raw close x factor (0.9 then 1.0); open/high/low/pre_close too.
    assert float(bars.loc["2025-12-30", "open"]) == pytest.approx(9.0)
    assert float(bars.loc["2025-12-30", "high"]) == pytest.approx(9.9)
    assert float(bars.loc["2025-12-30", "low"]) == pytest.approx(8.1)
    assert float(bars.loc["2025-12-30", "close"]) == pytest.approx(9.0)
    assert float(bars.loc["2025-12-31", "close"]) == pytest.approx(9.0)
    # volume stays actual share count (not reverse-scaled).
    assert float(bars.loc["2025-12-30", "volume"]) == 10_000.0
    assert float(bars.loc["2025-12-31", "volume"]) == 20_000.0
    # qfq metadata is explicit.
    assert str(bars.loc["2025-12-31", "price_series_mode"]) == "qfq"
    assert str(bars.loc["2025-12-31", "adjustment_source"]) == "local_vendor_qfq"
    assert str(bars.loc["2025-12-31", "adjustment_anchor_date"]) == "2025-12-31"
    assert float(bars.loc["2025-12-31", "adjustment_anchor_factor"]) == pytest.approx(1.0)
    assert str(bars.loc["2025-12-31", "background_data_source"]) == "local_vendor_qfq"


def test_vendor_overlay_qfq_removes_ex_dividend_gap_while_raw_keeps_it(
    tmp_path: Path,
) -> None:
    index_path = _build_qfq_daily_fixture(tmp_path)
    qfq = _qfq_provider(tmp_path, index_path).fetch_daily_bars("600000", lookback_days=10)
    raw = _provider(tmp_path, index_path).fetch_daily_bars("600000", lookback_days=10)

    qfq_closes = [float(value) for value in qfq["close"]]
    raw_closes = [float(value) for value in raw["close"]]
    # qfq is continuous across the ex-dividend date; raw has the fake gap.
    assert qfq_closes == pytest.approx([9.0, 9.0])
    assert raw_closes == pytest.approx([10.0, 9.0])
    # qfq price == raw price x factor on every row.
    assert float(qfq.loc["2025-12-30", "close"]) == pytest.approx(
        float(raw.loc["2025-12-30", "close"]) * 0.9
    )
    assert float(qfq.loc["2025-12-31", "close"]) == pytest.approx(
        float(raw.loc["2025-12-31", "close"]) * 1.0
    )
    # raw mode metadata stays exactly the current raw contract.
    assert str(raw.loc["2025-12-31", "adjustment_source"]) == "local_vendor_raw"
    assert str(raw.loc["2025-12-31", "price_series_mode"]) == "raw"


def test_vendor_overlay_qfq_factor_gap_dates_use_nearest_prior_factor(
    tmp_path: Path,
) -> None:
    daily_csv = "\n".join(
        [
            "code,datetime,open,high,low,close,volume,amount",
            "600000.SH,2025-11-28,10,11,9,10,100,123.4",
            "600000.SH,2025-12-29,10,11,9,10,100,123.4",
            "600000.SH,2025-12-30,10,11,9,10,100,123.4",
            "600000.SH,2025-12-31,10,11,9,10,100,123.4",
        ]
    )
    _write_zip(tmp_path / "全A日K" / "2025.zip", {"2025/600000.SH.csv": daily_csv})
    # Factors exist only on 2025-12-01 (0.8) and 2025-12-31 (1.0).
    _write_factors_zip(
        tmp_path,
        {
            "2025/600000.SH.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "600000.SH,20251201,0.8",
                    "600000.SH,20251231,1.0",
                ]
            )
        },
    )
    index_path = tmp_path / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)
    provider = _qfq_provider(tmp_path, index_path)

    bars = provider.fetch_daily_bars("600000", lookback_days=10)

    # 2025-12-29/30 have no factor row: they use the nearest prior factor 0.8.
    assert float(bars.loc["2025-12-29", "close"]) == pytest.approx(8.0)
    assert float(bars.loc["2025-12-30", "close"]) == pytest.approx(8.0)
    # A date before the earliest factor still gets a factor (earliest, no silent raw).
    assert float(bars.loc["2025-11-28", "close"]) == pytest.approx(8.0)
    assert float(bars.loc["2025-12-31", "close"]) == pytest.approx(10.0)


def test_vendor_overlay_qfq_missing_factor_archive_raises_with_symbol(
    tmp_path: Path,
) -> None:
    index_path = _build_qfq_daily_fixture(tmp_path)
    _ = index_path
    # Remove the factor archive so qfq cannot resolve any factor.
    import shutil

    shutil.rmtree(tmp_path / "复权因子")
    provider = _qfq_provider(tmp_path, index_path)

    with pytest.raises(DataSourceError, match="600000"):
        provider.fetch_daily_bars("600000", lookback_days=10)


def test_vendor_overlay_qfq_missing_symbol_factors_raise_with_symbol(
    tmp_path: Path,
) -> None:
    index_path = _build_qfq_daily_fixture(tmp_path)
    _ = index_path
    # Factor archive exists but has no entry for 600000.SH.
    _write_factors_zip(
        tmp_path,
        {
            "2025/000001.SZ.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "000001.SZ,20251231,1.0",
                ]
            )
        },
    )
    provider = _qfq_provider(tmp_path, index_path)

    with pytest.raises(DataSourceError, match="600000"):
        provider.fetch_daily_bars("600000", lookback_days=10)


def test_vendor_overlay_qfq_batch_quality_metrics_multiplies_prices(
    tmp_path: Path,
) -> None:
    index_path = _build_qfq_daily_fixture(tmp_path)
    provider = _qfq_provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(symbols=["600000"], lookback_days=10)

    rows = frame[frame["symbol"] == "600000"].sort_values("date")
    assert len(rows) == 2
    closes = [float(value) for value in rows["close"]]
    assert closes == pytest.approx([9.0, 9.0])
    volumes = [float(value) for value in rows["volume"]]
    assert volumes == pytest.approx([10_000.0, 20_000.0])


def test_runtime_universe_comes_from_provider_index(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    service = object.__new__(StockAnalyzerService)
    object.__setattr__(service, "_provider", _provider(tmp_path, index_path))
    object.__setattr__(service, "_realtime_provider", None)

    assert service._load_symbol_universe_from_provider() == ["600000"]


def test_market_sync_uses_vendor_latest_date_instead_of_bootstrap_window(
    tmp_path: Path,
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)
    config = SimpleNamespace(
        market_warehouse=SimpleNamespace(
            daily_lookback_days=120,
            online_bootstrap_lookback_days=750,
            daily_incremental_enabled=True,
            daily_incremental_cushion_days=5,
        )
    )
    service = SimpleNamespace(
        _provider=provider,
        _realtime_provider=None,
        _config=config,
    )
    sync = RuntimeMarketSyncService(service)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "empty_delta.duckdb",
        package_root=tmp_path / "empty_package",
    )

    latest = sync._resolve_market_warehouse_latest_daily_dates(
        warehouse=warehouse,
        symbols=["600000"],
    )
    lookback_days, mode = sync._resolve_market_warehouse_daily_lookback_days(
        latest_date=latest["600000"],
        target_end_date=date(2026, 1, 5),
        force=False,
    )

    assert latest["600000"] == date(2025, 12, 31)
    assert mode == "incremental"
    assert lookback_days == 10
    assert lookback_days < 750


def test_market_sync_builds_tushare_in_raw_mode_for_vendor_overlay() -> None:
    config = SimpleNamespace(
        data_source=DataSourceConfig(primary="vendor_zip_overlay"),
        market_warehouse=SimpleNamespace(tushare_token=""),
        evolution=SimpleNamespace(execution_spec=SimpleNamespace(price_series_mode="qfq")),
    )
    service = SimpleNamespace(_config=config)
    sync = RuntimeMarketSyncService(service)

    provider = sync._build_market_warehouse_online_single_provider(
        provider_name="tushare",
        request_interval=0.0,
        socket_timeout_sec=1.0,
        max_attempts=1,
    )

    assert cast(Any, provider)._price_series_mode == "raw"


def test_market_sync_warehouse_disables_package_writes_for_vendor_overlay(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        data_source=DataSourceConfig(primary="vendor_zip_overlay"),
    )
    service = SimpleNamespace(
        _config=config,
        _resolve_market_warehouse_db_path=lambda: tmp_path / "delta.duckdb",
        _resolve_market_warehouse_package_root=lambda: tmp_path / "package",
    )

    warehouse = RuntimeMarketSyncService(service)._market_warehouse()

    assert warehouse.package_writes_enabled is False


def test_vendor_delta_sync_does_not_export_package_bars(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta.duckdb",
        package_root=tmp_path / "package",
        package_writes_enabled=False,
    )
    fresh = pd.DataFrame(
        {
            "open": [11.0],
            "high": [12.0],
            "low": [10.5],
            "close": [11.8],
            "volume": [1000.0],
            "turnover": [11_800.0],
            "float_market_cap": [2_000_000_000.0],
            "price_series_mode": ["raw"],
            "adjustment_source": ["tushare_raw"],
        },
        index=pd.to_datetime(["2026-01-02"]),
    )

    class _OnlineProvider:
        def fetch_daily_bars(
            self,
            symbol: str,
            lookback_days: int = 120,
            *,
            end_date: date | None = None,
        ) -> pd.DataFrame:
            _ = symbol, lookback_days, end_date
            return fresh.copy()

    service = SimpleNamespace(
        _resolve_market_warehouse_daily_lookback_days=lambda **kwargs: (10, "incremental"),
        _extract_price_series_meta_from_frame=lambda frame: {
            "price_series_mode": "raw",
            "adjustment_source": "tushare_raw",
            "adjustment_anchor_date": "",
            "adjustment_anchor_factor": None,
        },
        _resolve_market_warehouse_price_series_action=lambda **kwargs: {
            "action": "incremental",
            "reason": "anchor_compatible",
        },
        _carry_forward_market_warehouse_financial_fields=lambda **kwargs: kwargs["fresh_daily"],
    )
    sync = RuntimeMarketSyncService(service)

    report = sync._sync_market_warehouse_daily_symbol(
        warehouse=warehouse,
        online_provider=cast(Any, _OnlineProvider()),
        symbol="600000",
        force=False,
        target_end_date=date(2026, 1, 2),
        latest_daily=date(2025, 12, 31),
        hard_timeout_sec=2.0,
    )

    assert report["status"] == "ok"
    assert len(warehouse.fetch_all_daily_bars(symbol="600000")) == 1
    assert not (warehouse.package_root / "bars").exists()
    assert not (warehouse.package_root / "manifest.json").exists()
    assert warehouse.refresh_package_manifests()["reason"] == "package_writes_disabled"


# ---------------------------------------------------------------------------
# fetch_universe_quality_metrics batch interface (Week5 quality selector)
# ---------------------------------------------------------------------------
_UNIVERSE_QUALITY_REQUIRED_COLUMNS = {
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "suspended",
    "is_st",
    "is_delisting_risk",
    "roe",
    "debt_ratio",
    "financial_data_complete",
    "financial_completeness",
    "background_data_complete",
    "holder_count",
    "northbound_net",
    "dragon_tiger_flag",
}


def _daily_csv(*, symbol: str, dates: list[str]) -> str:
    rows = [f"{symbol},{dt},10,11,9,10.5,100,123.4,200000" for dt in dates]
    return "\n".join(["code,datetime,open,high,low,close,volume,amount,circ_mv", *rows])


def _build_multi_year_daily_fixture(root: Path) -> Path:
    """Two symbols across two annual archives (2024 + 2025)."""
    _write_zip(
        root / "全A日K" / "2024.zip",
        {
            "bars/600000.SH.csv": _daily_csv(
                symbol="600000.SH", dates=["2024-01-02", "2024-01-03"]
            ),
            "bars/000001.SZ.csv": _daily_csv(
                symbol="000001.SZ", dates=["2024-12-30", "2024-12-31"]
            ),
        },
    )
    _write_zip(
        root / "全A日K" / "2025.zip",
        {
            "bars/600000.SH.csv": _daily_csv(
                symbol="600000.SH", dates=["2025-12-29", "2025-12-30", "2025-12-31"]
            ),
            "bars/000001.SZ.csv": _daily_csv(
                symbol="000001.SZ", dates=["2025-12-29", "2025-12-30", "2025-12-31"]
            ),
        },
    )
    index_path = root / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=root, output_path=index_path)
    return index_path


def _delta_warehouse(root: Path) -> MarketWarehouse:
    warehouse = MarketWarehouse(
        db_path=root / "delta" / "market_delta.duckdb",
        package_root=root / "delta" / "package",
        package_writes_enabled=False,
    )
    return warehouse


def _delta_frame(
    *,
    symbol: str,
    dates: list[str],
    close: float = 13.0,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [close - 0.1] * len(dates),
            "high": [close + 0.2] * len(dates),
            "low": [close - 0.2] * len(dates),
            "close": [close] * len(dates),
            "volume": [9999.0] * len(dates),
            "turnover": [999_900.0] * len(dates),
            "float_market_cap": [2_200_000_000.0] * len(dates),
            "suspended": [False] * len(dates),
            "is_st": [False] * len(dates),
            "is_delisting_risk": [False] * len(dates),
            "roe": [0.18] * len(dates),
            "debt_ratio": [0.30] * len(dates),
            "financial_data_complete": [True] * len(dates),
            "financial_completeness": [0.95] * len(dates),
            "background_data_complete": [True] * len(dates),
            "holder_count": [40_000.0] * len(dates),
            "northbound_net": [0.0] * len(dates),
            "dragon_tiger_flag": [0.0] * len(dates),
            "price_series_mode": ["raw"] * len(dates),
            "adjustment_source": ["tushare_raw"] * len(dates),
        },
        index=pd.to_datetime(dates),
    )


def test_batch_quality_metrics_returns_multiple_symbols_with_required_columns(
    tmp_path: Path,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001", "999999"],
        lookback_days=10,
    )

    assert sorted(frame["symbol"].unique()) == ["000001", "600000"]
    assert _UNIVERSE_QUALITY_REQUIRED_COLUMNS <= set(frame.columns)
    # date is a plain column (not only an index).
    assert "date" in frame.columns
    assert not isinstance(frame.index, pd.DatetimeIndex)
    for symbol in ("600000", "000001"):
        symbol_rows = frame[frame["symbol"] == symbol]
        assert symbol_rows["date"].is_monotonic_increasing
    # ZIP rows keep vendor scales and honest missing financial fields.
    zip_only = frame[frame["symbol"] == "000001"]
    assert float(zip_only.iloc[-1]["volume"]) == 10_000.0
    assert bool(zip_only.iloc[-1]["financial_data_complete"]) is False
    assert pd.isna(zip_only.iloc[-1]["roe"])
    assert pd.isna(zip_only.iloc[-1]["debt_ratio"])
    assert pd.isna(zip_only.iloc[-1]["holder_count"])


def test_batch_quality_metrics_limits_rows_per_symbol_to_lookback_days(
    tmp_path: Path,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=3,
    )

    assert len(frame[frame["symbol"] == "600000"]) == 3
    assert len(frame[frame["symbol"] == "000001"]) == 3
    # The 3 newest rows are kept: 3 from 2025 + 0 from 2024.
    assert frame[frame["symbol"] == "600000"]["date"].max().year == 2025


def test_batch_quality_metrics_delta_wins_on_duplicate_date(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    delta_package = tmp_path / "delta" / "package"
    warehouse = MarketWarehouse(db_path=delta_db, package_root=delta_package)
    warehouse.replace_daily_bars(
        symbol="600000",
        frame=_delta_frame(
            symbol="600000",
            dates=["2025-12-31", "2026-01-02"],
        ),
    )
    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_package_root=str(delta_package),
    )

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000"],
        lookback_days=10,
    )

    rows = frame[frame["symbol"] == "600000"]
    dates = rows["date"].dt.strftime("%Y-%m-%d").tolist()
    assert dates == ["2025-12-30", "2025-12-31", "2026-01-02"]
    duplicated = rows[rows["date"].dt.strftime("%Y-%m-%d") == "2025-12-31"]
    assert len(duplicated) == 1
    # Delta wins on the shared date: real financial fields present.
    assert float(duplicated.iloc[0]["close"]) == 13.0
    assert float(duplicated.iloc[0]["roe"]) == 0.18
    assert bool(duplicated.iloc[0]["financial_data_complete"]) is True
    # Delta-only latest row is included.
    assert rows.iloc[-1]["date"].strftime("%Y-%m-%d") == "2026-01-02"


def test_batch_quality_metrics_empty_input_returns_empty_frame(tmp_path: Path) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(symbols=[], lookback_days=10)

    assert isinstance(frame, pd.DataFrame)
    assert frame.empty


def test_batch_quality_metrics_unknown_symbols_yield_no_rows(tmp_path: Path) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["999999", "123456.SH"],
        lookback_days=10,
    )

    assert isinstance(frame, pd.DataFrame)
    assert frame.empty


def test_batch_quality_metrics_skips_missing_qfq_factor_symbol(tmp_path: Path) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    # Only 000001.SZ has qfq factors; 600000.SH must be skipped, not raised.
    _write_factors_zip(
        tmp_path,
        {
            "2025/000001.SZ.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "000001.SZ,20251231,1.0",
                ]
            )
        },
    )
    provider = _qfq_provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=10,
    )

    assert sorted(frame["symbol"].unique()) == ["000001"]
    assert _UNIVERSE_QUALITY_REQUIRED_COLUMNS <= set(frame.columns)
    assert "600000" not in frame["symbol"].tolist()


def test_batch_quality_metrics_qfq_empty_year_csv_falls_back_to_older_year(
    tmp_path: Path,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    # Rewrite 2025.zip: 600000.SH is header-only (no data rows), so the batch
    # path must fall back to its 2024 rows instead of zeroing/skipping the
    # symbol as a missing-factor case.
    _write_zip(
        tmp_path / "全A日K" / "2025.zip",
        {
            "bars/600000.SH.csv": "code,datetime,open,high,low,close,volume,amount,circ_mv",
            "bars/000001.SZ.csv": _daily_csv(
                symbol="000001.SZ", dates=["2025-12-29", "2025-12-30", "2025-12-31"]
            ),
        },
    )
    # Factor rows anchor 2024 (ffill covers the 2024 archive dates) and 2025.
    _write_factors_zip(
        tmp_path,
        {
            "2025/600000.SH.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "600000.SH,20240102,1.0",
                    "600000.SH,20251231,1.0",
                ]
            ),
            "2025/000001.SZ.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "000001.SZ,20240102,1.0",
                    "000001.SZ,20251231,1.0",
                ]
            ),
        },
    )
    index_path = tmp_path / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)
    provider = _qfq_provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=10,
    )

    # 600000 is kept via its 2024 annual archive; the empty 2025 CSV is an
    # empty year, not a factor failure, so no skip/zero happens.
    assert sorted(frame["symbol"].unique()) == ["000001", "600000"]
    assert _UNIVERSE_QUALITY_REQUIRED_COLUMNS <= set(frame.columns)
    six_hundred = frame[frame["symbol"] == "600000"]
    assert six_hundred["date"].max().year == 2024
    assert len(six_hundred) == 2


def test_batch_quality_metrics_structural_error_still_raises(tmp_path: Path) -> None:
    bad_csv = "\n".join(
        [
            "code,datetime",
            "600000.SH,2025-12-30",
            "600000.SH,2025-12-31",
        ]
    )
    _write_zip(
        tmp_path / "全A日K" / "2025.zip",
        {
            "bars/600000.SH.csv": bad_csv,
            "bars/000001.SZ.csv": _daily_csv(
                symbol="000001.SZ", dates=["2025-12-29", "2025-12-30", "2025-12-31"]
            ),
        },
    )
    _write_factors_zip(
        tmp_path,
        {
            "2025/000001.SZ.csv": "\n".join(
                [
                    "股票代码,交易日期,复权因子",
                    "000001.SZ,20251231,1.0",
                ]
            )
        },
    )
    index_path = tmp_path / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)
    provider = _qfq_provider(tmp_path, index_path)

    with pytest.raises(DataSourceError, match="missing required column"):
        provider.fetch_universe_quality_metrics(
            symbols=["600000", "000001"],
            lookback_days=10,
        )


def test_batch_quality_metrics_delta_only_symbols_still_returned(tmp_path: Path) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    delta_package = tmp_path / "delta" / "package"
    warehouse = MarketWarehouse(db_path=delta_db, package_root=delta_package)
    warehouse.replace_daily_bars(
        symbol="600519",
        frame=_delta_frame(symbol="600519", dates=["2026-01-05", "2026-01-06"]),
    )
    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_package_root=str(delta_package),
    )

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600519", "600000"],
        lookback_days=10,
    )

    assert "600519" in frame["symbol"].tolist()
    delta_rows = frame[frame["symbol"] == "600519"]
    assert len(delta_rows) == 2
    assert float(delta_rows.iloc[-1]["roe"]) == 0.18


def test_batch_quality_metrics_opens_each_annual_zip_once_and_newest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)
    opened: list[str] = []
    real_zipfile = zipfile.ZipFile

    class _CountingZipFile(real_zipfile):  # type: ignore[misc, valid-type]
        def __init__(self, file: object, *args: object, **kwargs: object) -> None:
            opened.append(str(file))
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", _CountingZipFile)

    # lookback=2: newest year (2025) alone satisfies both symbols, so the
    # older 2024 archive must not be opened at all.
    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=2,
    )

    assert opened == [str(tmp_path / "全A日K" / "2025.zip")]
    assert len(frame[frame["symbol"] == "600000"]) == 2
    assert len(frame[frame["symbol"] == "000001"]) == 2
    # Every symbol in the same annual archive shares one open.
    assert len(set(opened)) == 1


def test_batch_quality_metrics_opens_older_years_only_when_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)
    opened: list[str] = []
    real_zipfile = zipfile.ZipFile

    class _CountingZipFile(real_zipfile):  # type: ignore[misc, valid-type]
        def __init__(self, file: object, *args: object, **kwargs: object) -> None:
            opened.append(str(file))
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", _CountingZipFile)

    # lookback=5 > 3 rows available in 2025, so 2024 must be read for both.
    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=5,
    )

    assert set(opened) == {
        str(tmp_path / "全A日K" / "2025.zip"),
        str(tmp_path / "全A日K" / "2024.zip"),
    }
    assert len(frame[frame["symbol"] == "600000"]) == 5
    assert frame[frame["symbol"] == "600000"]["date"].min().year == 2024


def test_batch_quality_metrics_does_not_call_per_symbol_fetch_daily_bars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    def _forbidden(*args: object, **kwargs: object) -> pd.DataFrame:
        raise AssertionError("per-symbol fetch_daily_bars must not be called")

    monkeypatch.setattr(
        VendorZipOverlayProvider,
        "fetch_daily_bars",
        _forbidden,
    )
    delta_calls: list[int] = []

    def _counting_delta_fetch(*, symbols: list[str], lookback_days: int) -> pd.DataFrame:
        delta_calls.append(len(symbols))
        return pd.DataFrame()

    monkeypatch.setattr(
        provider._warehouse, "fetch_universe_quality_metrics", _counting_delta_fetch
    )

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=10,
    )

    assert delta_calls == [2]
    assert sorted(frame["symbol"].unique()) == ["000001", "600000"]
    assert len(frame[frame["symbol"] == "600000"]) == 5


def test_batch_quality_metrics_result_is_deterministic_regardless_of_input_order(
    tmp_path: Path,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    ordered = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=3,
    )
    shuffled = provider.fetch_universe_quality_metrics(
        symbols=["000001.SZ", "600000.SH"],
        lookback_days=3,
    )

    assert ordered["symbol"].tolist() == shuffled["symbol"].tolist()
    assert ordered["date"].tolist() == shuffled["date"].tolist()
    assert ordered["close"].tolist() == shuffled["close"].tolist()


def _financial_snapshot_frame(
    *,
    symbol: str,
    end_date: str,
    ann_date: str,
    roe: float,
    debt_ratio: float,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [symbol],
            "end_date": pd.to_datetime([end_date]),
            "ann_date": pd.to_datetime([ann_date]),
            "roe": [roe],
            "debt_ratio": [debt_ratio],
            "update_flag": [0],
            "financial_report_date": [end_date],
            "financial_as_of": [ann_date],
            "financial_source": ["tushare_fina_indicator"],
            "financial_trust_level": ["reported"],
            "financial_missing_fields": [""],
            "financial_data_complete": [True],
            "financial_completeness": [1.0],
            "coverage_complete": [True],
            "as_of": [ann_date],
            "source": ["tushare_fina_indicator"],
        }
    )


def test_batch_quality_metrics_fills_zip_rows_from_financial_snapshots(
    tmp_path: Path,
) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    warehouse = _delta_warehouse(tmp_path)
    warehouse.upsert_financial_snapshots(
        symbol="600000",
        frame=_financial_snapshot_frame(
            symbol="600000",
            end_date="2025-09-30",
            ann_date="2025-11-10",
            roe=0.15,
            debt_ratio=0.45,
        ),
    )
    provider = _provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"],
        lookback_days=10,
    )

    for symbol in ("600000", "000001"):
        symbol_rows = frame[frame["symbol"] == symbol]
        assert symbol_rows["date"].is_monotonic_increasing
    # 600000 ZIP bars disclosed after the snapshot announcement are filled
    # from the snapshot table; 2024 bars (before any announcement) stay
    # honestly missing with the same marker as the single-symbol path.
    filled = frame[frame["symbol"] == "600000"]
    assert float(
        filled[filled["date"] == pd.Timestamp("2025-12-30")]["roe"].iloc[0]
    ) == pytest.approx(0.15)
    assert float(
        filled[filled["date"] == pd.Timestamp("2025-12-30")]["debt_ratio"].iloc[0]
    ) == pytest.approx(0.45)
    assert (
        bool(
            filled[filled["date"] == pd.Timestamp("2025-12-30")]["financial_data_complete"].iloc[0]
        )
        is True
    )
    assert (
        str(filled[filled["date"] == pd.Timestamp("2025-12-30")]["financial_source"].iloc[0])
        == "tushare_fina_indicator"
    )
    old = filled[filled["date"] == pd.Timestamp("2024-01-02")]
    assert pd.isna(old["roe"].iloc[0])
    assert bool(old["financial_data_complete"].iloc[0]) is False
    assert str(old["financial_source"].iloc[0]) == "tushare_pending"
    # 000001 has no snapshots: all rows stay honestly missing.
    zip_only = frame[frame["symbol"] == "000001"]
    assert bool(zip_only.iloc[-1]["financial_data_complete"]) is False
    assert pd.isna(zip_only.iloc[-1]["roe"])


def test_batch_quality_metrics_keeps_reported_delta_financials(tmp_path: Path) -> None:
    index_path = _build_multi_year_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    delta_package = tmp_path / "delta" / "package"
    warehouse = MarketWarehouse(db_path=delta_db, package_root=delta_package)
    delta_frame = _delta_frame(
        symbol="600000",
        dates=["2025-12-31"],
    )
    delta_frame["financial_source"] = "tushare_fina_indicator"
    delta_frame["financial_trust_level"] = "reported"
    delta_frame["financial_report_date"] = "2025-09-30"
    delta_frame["financial_as_of"] = "2025-10-25"
    warehouse.replace_daily_bars(symbol="600000", frame=delta_frame)
    warehouse.upsert_financial_snapshots(
        symbol="600000",
        frame=_financial_snapshot_frame(
            symbol="600000",
            end_date="2025-09-30",
            ann_date="2025-11-10",
            roe=0.15,
            debt_ratio=0.45,
        ),
    )
    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_package_root=str(delta_package),
    )

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000"],
        lookback_days=10,
    )

    rows = frame[frame["symbol"] == "600000"]
    delta_row = rows[rows["date"] == pd.Timestamp("2025-12-31")]
    assert len(delta_row) == 1
    # Reported delta financials are trusted and never overwritten by the
    # snapshot join (only_fill_pending=True).
    assert float(delta_row.iloc[0]["roe"]) == 0.18
    assert str(delta_row.iloc[0]["financial_source"]) == "tushare_fina_indicator"
    # ZIP rows (local_vendor/missing) are filled from the snapshot table.
    zip_row = rows[rows["date"] == pd.Timestamp("2025-12-30")]
    assert float(zip_row.iloc[0]["roe"]) == pytest.approx(0.15)


def _db_fingerprint(path: Path) -> dict[str, object]:
    import hashlib

    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _db_schema(path: Path) -> list[tuple[str, str]]:
    import duckdb

    with duckdb.connect(str(path), read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'main'
            ORDER BY table_name, column_name
            """
        ).fetchall()
    return [(f"{row[0]}", f"{row[1]}") for row in rows]


def test_read_only_cold_cache_probe_never_creates_delta_db(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    delta_package = tmp_path / "delta" / "package"
    assert not delta_db.exists()

    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_package_root=str(delta_package),
        delta_access_mode="read_only",
    )
    assert provider.delta_access_mode == "read_only"
    assert provider._warehouse is not None
    assert provider._warehouse.read_only is True

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000"],
        lookback_days=10,
    )

    assert not frame.empty
    assert not delta_db.exists()
    assert not (tmp_path / "delta").exists()
    assert _db_fingerprint(delta_db) == {"exists": False}


def test_read_only_probe_does_not_modify_existing_delta_db(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    delta_package = tmp_path / "delta" / "package"
    warehouse = MarketWarehouse(db_path=delta_db, package_root=delta_package)
    warehouse.ensure_schema()
    warehouse.replace_daily_bars(
        symbol="600000",
        frame=_delta_frame(symbol="600000", dates=["2025-12-31"]),
    )
    before = _db_fingerprint(delta_db)
    schema_before = _db_schema(delta_db)

    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_package_root=str(delta_package),
        delta_access_mode="read_only",
    )
    _ = provider.fetch_universe_quality_metrics(symbols=["600000"], lookback_days=10)
    _ = provider.latest_daily_dates(symbols=["600000"])

    after = _db_fingerprint(delta_db)
    schema_after = _db_schema(delta_db)
    assert after["sha256"] == before["sha256"]
    assert after["size"] == before["size"]
    assert after["mtime_ns"] == before["mtime_ns"]
    assert schema_after == schema_before


def test_read_only_warehouse_refuses_schema_and_writes(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "ro.duckdb",
        package_root=tmp_path / "delta" / "package",
        read_only=True,
    )
    assert warehouse.read_only is True
    with pytest.raises(DataSourceError, match="read-only"):
        warehouse.ensure_schema()
    with pytest.raises(DataSourceError, match="read-only"):
        warehouse.replace_daily_bars(
            symbol="600000",
            frame=_delta_frame(symbol="600000", dates=["2026-01-02"]),
        )
    assert not (tmp_path / "delta").exists()


def test_delta_access_mode_disabled_reads_zip_only(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_access_mode="disabled",
    )
    assert provider._warehouse is None
    bars = provider.fetch_daily_bars("600000", lookback_days=10)
    assert list(bars.index.strftime("%Y-%m-%d")) == ["2025-12-30", "2025-12-31"]
    assert not delta_db.exists()
    status = provider.status()
    assert status["delta_access_mode"] == "disabled"
    assert status["delta_db_exists"] is False


def test_unknown_delta_access_mode_rejected(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    with pytest.raises(DataSourceError, match="delta_access_mode"):
        VendorZipOverlayProvider(
            data_root=str(tmp_path),
            index_path=str(index_path),
            delta_db_path=str(tmp_path / "delta.duckdb"),
            delta_access_mode="read-write",
        )


def test_read_write_mode_still_writes_delta_cache(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_access_mode="read_write",
    )
    assert provider._warehouse is not None
    assert provider._warehouse.read_only is False
    warehouse = provider._warehouse
    warehouse.ensure_schema()
    warehouse.replace_daily_bars(
        symbol="600000",
        frame=_delta_frame(symbol="600000", dates=["2026-01-02"]),
    )
    assert delta_db.exists()
    bars = provider.fetch_daily_bars("600000", lookback_days=10)
    assert any(pd.Timestamp(ts).date().isoformat() == "2026-01-02" for ts in bars.index)


def test_read_only_warm_cache_matches_zip_read_results(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    warehouse = MarketWarehouse(db_path=delta_db, package_root=tmp_path / "delta" / "package")
    warehouse.ensure_schema()
    warehouse.replace_daily_bars(
        symbol="600000",
        frame=_delta_frame(symbol="600000", dates=["2026-01-02"]),
    )

    read_only = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_access_mode="read_only",
    )
    read_write = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_access_mode="read_write",
    )

    ro_frame = read_only.fetch_universe_quality_metrics(symbols=["600000"], lookback_days=10)
    rw_frame = read_write.fetch_universe_quality_metrics(symbols=["600000"], lookback_days=10)
    assert ro_frame.equals(rw_frame)


def test_probe_enforce_read_only_flips_overlay_delta_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(tmp_path / "delta" / "market_delta.duckdb"),
    )
    assert provider.delta_access_mode == "read_write"

    import importlib.util

    probe_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "probe_universe_quality_selector.py"
    )
    spec = importlib.util.spec_from_file_location("probe_module", probe_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._enforce_probe_read_only(provider)  # type: ignore[attr-defined]

    assert provider.delta_access_mode == "read_only"
    assert provider._warehouse is not None
    assert provider._warehouse.read_only is True
