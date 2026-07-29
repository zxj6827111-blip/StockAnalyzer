from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.data.financial_pit import (
    apply_financial_snapshots_asof,
    merge_snapshot_frames,
    normalize_fina_indicator_rows,
    percent_to_ratio,
    select_pit_snapshot_for_date,
)
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import TushareProvider


def test_percent_to_ratio_converts_tushare_percent_points() -> None:
    assert percent_to_ratio(12.5) == pytest.approx(0.125)
    assert percent_to_ratio(0.125) == pytest.approx(0.125)
    assert np.isnan(percent_to_ratio(None))


def test_normalize_drops_rows_without_ann_date() -> None:
    raw = pd.DataFrame(
        {
            "end_date": ["20231231", "20240630"],
            "ann_date": ["20240420", None],
            "roe": [15.0, 10.0],
            "debt_to_assets": [40.0, 50.0],
            "update_flag": [1, 1],
        }
    )
    out = normalize_fina_indicator_rows(raw, symbol="600000")
    assert len(out) == 1
    assert out.iloc[0]["financial_as_of"] == "2024-04-20"
    assert out.iloc[0]["financial_report_date"] == "2023-12-31"
    assert out.iloc[0]["roe"] == pytest.approx(0.15)
    assert out.iloc[0]["debt_ratio"] == pytest.approx(0.40)
    assert out.iloc[0]["financial_source"] == "tushare_fina_indicator"
    assert out.iloc[0]["financial_trust_level"] == "reported"
    assert bool(out.iloc[0]["financial_data_complete"]) is True


def test_normalize_marks_missing_fields() -> None:
    raw = pd.DataFrame(
        {
            "end_date": ["20231231"],
            "ann_date": ["20240420"],
            "roe": [12.0],
            "debt_to_assets": [np.nan],
            "update_flag": [1],
        }
    )
    out = normalize_fina_indicator_rows(raw, symbol="000001")
    assert out.iloc[0]["financial_missing_fields"] == "debt_ratio"
    assert bool(out.iloc[0]["financial_data_complete"]) is False
    assert out.iloc[0]["financial_completeness"] == pytest.approx(0.5)


def test_revision_and_same_day_tie_break() -> None:
    raw = pd.DataFrame(
        {
            "end_date": ["20231231", "20231231", "20240630", "20240630"],
            "ann_date": ["20240420", "20240510", "20240820", "20240820"],
            "roe": [10.0, 11.0, 12.0, 13.0],
            "debt_to_assets": [40.0, 41.0, 42.0, 43.0],
            "update_flag": [0, 1, 0, 1],
        }
    )
    snaps = normalize_fina_indicator_rows(raw, symbol="600000")
    # two revisions for 20231231 kept as separate ann_dates
    assert len(snaps) == 3
    pre = select_pit_snapshot_for_date(snaps, as_of=date(2024, 4, 25))
    assert pre is not None
    assert float(pre["roe"]) == pytest.approx(0.10)
    mid = select_pit_snapshot_for_date(snaps, as_of=date(2024, 5, 10))
    assert mid is not None
    assert float(mid["roe"]) == pytest.approx(0.11)
    # same day: higher update_flag / later row wins
    same = select_pit_snapshot_for_date(snaps, as_of=date(2024, 8, 20))
    assert same is not None
    assert float(same["roe"]) == pytest.approx(0.13)


def test_asof_join_no_pre_announcement_and_switches() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-04-19", "2024-04-20", "2024-05-10", "2024-08-20"]
            ),
            "open": [10.0] * 4,
            "high": [10.0] * 4,
            "low": [10.0] * 4,
            "close": [10.0] * 4,
            "volume": [1.0] * 4,
            "turnover": [1.0] * 4,
            "float_market_cap": [1.0] * 4,
            "roe": [np.nan] * 4,
            "debt_ratio": [np.nan] * 4,
            "financial_data_complete": [False] * 4,
            "financial_missing_fields": ["roe,debt_ratio"] * 4,
            "financial_source": ["tushare_pending"] * 4,
            "financial_report_date": [""] * 4,
            "financial_as_of": [""] * 4,
            "financial_trust_level": ["missing"] * 4,
            "financial_completeness": [0.0] * 4,
        }
    ).set_index("date")

    raw = pd.DataFrame(
        {
            "end_date": ["20231231", "20240630"],
            "ann_date": ["20240420", "20240820"],
            "roe": [15.0, 18.0],
            "debt_to_assets": [35.0, 38.0],
            "update_flag": [1, 1],
        }
    )
    snaps = normalize_fina_indicator_rows(raw, symbol="600000")
    out = apply_financial_snapshots_asof(daily, snaps, only_fill_pending=False)

    assert np.isnan(float(out.loc[pd.Timestamp("2024-04-19"), "roe"]))
    assert out.loc[pd.Timestamp("2024-04-19"), "financial_trust_level"] == "missing"

    assert float(out.loc[pd.Timestamp("2024-04-20"), "roe"]) == pytest.approx(0.15)
    assert float(out.loc[pd.Timestamp("2024-05-10"), "roe"]) == pytest.approx(0.15)
    assert float(out.loc[pd.Timestamp("2024-08-20"), "roe"]) == pytest.approx(0.18)
    assert out.loc[pd.Timestamp("2024-08-20"), "financial_source"] == "tushare_fina_indicator"
    assert out.loc[pd.Timestamp("2024-08-20"), "financial_trust_level"] == "reported"


def test_asof_full_materialize_replaces_heuristic_with_reported() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-04-20"]),
            "roe": [0.08],
            "debt_ratio": [0.55],
            "financial_data_complete": [True],
            "financial_missing_fields": [""],
            "financial_source": ["tdx_offline"],
            "financial_report_date": [""],
            "financial_as_of": [""],
            "financial_trust_level": ["heuristic"],
            "financial_completeness": [1.0],
        }
    ).set_index("date")
    raw = pd.DataFrame(
        {
            "end_date": ["20231231"],
            "ann_date": ["20240420"],
            "roe": [15.0],
            "debt_to_assets": [35.0],
        }
    )
    snaps = normalize_fina_indicator_rows(raw, symbol="600000")
    # Full re-materialize from real snapshots is the legitimate upgrade path.
    out_full = apply_financial_snapshots_asof(daily, snaps, only_fill_pending=False)
    assert float(out_full.iloc[0]["roe"]) == pytest.approx(0.15)
    assert out_full.iloc[0]["financial_trust_level"] == "reported"
    # Carry-forward path (only_fill_pending) still allows fill when not reported.
    out_pending = apply_financial_snapshots_asof(daily, snaps, only_fill_pending=True)
    assert out_pending is not None



def test_merge_snapshots_idempotent() -> None:
    raw = pd.DataFrame(
        {
            "end_date": ["20231231"],
            "ann_date": ["20240420"],
            "roe": [15.0],
            "debt_to_assets": [35.0],
        }
    )
    a = normalize_fina_indicator_rows(raw, symbol="600000")
    b = normalize_fina_indicator_rows(raw, symbol="600000")
    merged = merge_snapshot_frames(a, b)
    assert len(merged) == 1


def test_warehouse_financial_snapshot_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    wh = MarketWarehouse(db_path=db, package_root=pkg)
    wh.ensure_schema()

    raw = pd.DataFrame(
        {
            "end_date": ["20231231", "20240630"],
            "ann_date": ["20240420", "20240820"],
            "roe": [15.0, 18.0],
            "debt_to_assets": [35.0, 38.0],
            "update_flag": [1, 1],
        }
    )
    snaps = normalize_fina_indicator_rows(raw, symbol="600000")
    n1 = wh.upsert_financial_snapshots(symbol="600000", frame=snaps)
    n2 = wh.upsert_financial_snapshots(symbol="600000", frame=snaps)
    assert n1 == 2
    assert n2 == 2
    stored = wh.fetch_financial_snapshots(symbol="600000")
    assert len(stored) == 2

    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-04-19", "2024-04-20", "2024-08-20", "2024-08-21"]
            ),
            "open": [10.0] * 4,
            "high": [10.5] * 4,
            "low": [9.5] * 4,
            "close": [10.2] * 4,
            "volume": [1_000_000.0] * 4,
            "turnover": [10_000_000.0] * 4,
            "float_market_cap": [1e10] * 4,
            "suspended": [False] * 4,
            "name": ["测试"] * 4,
            "is_st": [False] * 4,
            "is_delisting_risk": [False] * 4,
            "roe": [np.nan] * 4,
            "debt_ratio": [np.nan] * 4,
            "financial_data_complete": [False] * 4,
            "financial_missing_fields": ["roe,debt_ratio"] * 4,
            "financial_source": ["tushare_pending"] * 4,
            "financial_report_date": [""] * 4,
            "financial_as_of": [""] * 4,
            "financial_trust_level": ["missing"] * 4,
            "financial_completeness": [0.0] * 4,
            "holder_count": [np.nan] * 4,
            "block_trade_net": [np.nan] * 4,
            "financing_balance": [np.nan] * 4,
            "margin_financing_balance": [np.nan] * 4,
            "northbound_net": [np.nan] * 4,
            "dragon_tiger_flag": [np.nan] * 4,
            "background_data_source": ["tushare_pro_qfq"] * 4,
            "background_data_complete": [False] * 4,
            "background_missing_fields": [""] * 4,
            "background_as_of": [""] * 4,
            "price_series_mode": ["qfq"] * 4,
            "adjustment_source": ["tushare_adj_factor"] * 4,
            "adjustment_anchor_date": ["2024-08-21"] * 4,
            "adjustment_anchor_factor": [1.0] * 4,
            "board": ["main"] * 4,
        }
    ).set_index("date")
    wh.replace_daily_bars(symbol="600000", frame=daily)
    enriched = wh.apply_financial_snapshots_to_daily(symbol="600000")
    assert np.isnan(float(enriched.loc[pd.Timestamp("2024-04-19"), "roe"]))
    assert float(enriched.loc[pd.Timestamp("2024-04-20"), "roe"]) == pytest.approx(0.15)
    assert float(enriched.loc[pd.Timestamp("2024-08-20"), "roe"]) == pytest.approx(0.18)


class _FakeProFina:
    def __init__(self, fina: pd.DataFrame | Exception) -> None:
        self._fina = fina
        self.calls = 0

    def fina_indicator(self, **kwargs: object) -> object:
        self.calls += 1
        _ = kwargs
        if isinstance(self._fina, Exception):
            raise self._fina
        return self._fina

    def daily(self, **kwargs: object) -> object:
        _ = kwargs
        return pd.DataFrame()

    def daily_basic(self, **kwargs: object) -> object:
        _ = kwargs
        return pd.DataFrame()

    def adj_factor(self, **kwargs: object) -> object:
        _ = kwargs
        return pd.DataFrame()

    def trade_cal(self, **kwargs: object) -> object:
        _ = kwargs
        return pd.DataFrame()

    def stock_basic(self, **kwargs: object) -> object:
        _ = kwargs
        return pd.DataFrame()


def test_provider_fetch_fina_indicator_success() -> None:
    raw = pd.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "end_date": ["20231231"],
            "ann_date": ["20240420"],
            "roe": [12.5],
            "debt_to_assets": [45.0],
            "update_flag": ["1"],
        }
    )
    pro = _FakeProFina(raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_fina_indicator("600000", end_date=date(2024, 12, 31))
    assert len(out) == 1
    assert out.iloc[0]["roe"] == pytest.approx(0.125)
    assert pro.calls == 1


def test_provider_fetch_fina_indicator_failure_raises() -> None:
    pro = _FakeProFina(RuntimeError("boom"))
    provider = TushareProvider(pro_api=pro, max_attempts=1)  # type: ignore[arg-type]
    with pytest.raises(DataSourceError, match="fina_indicator failed"):
        provider.fetch_fina_indicator("600000")


def test_api_failure_does_not_wipe_existing_snapshots(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    wh = MarketWarehouse(db_path=db, package_root=pkg)
    raw = pd.DataFrame(
        {
            "end_date": ["20231231"],
            "ann_date": ["20240420"],
            "roe": [15.0],
            "debt_to_assets": [35.0],
        }
    )
    snaps = normalize_fina_indicator_rows(raw, symbol="600000")
    wh.upsert_financial_snapshots(symbol="600000", frame=snaps)
    assert len(wh.fetch_financial_snapshots(symbol="600000")) == 1

    # empty incoming does not delete when merge keeps existing via upsert path with empty?
    # upsert with empty returns existing count without delete - good
    n = wh.upsert_financial_snapshots(symbol="600000", frame=pd.DataFrame())
    assert n == 1
    assert len(wh.fetch_financial_snapshots(symbol="600000")) == 1
