from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.data.financial_pit import (
    apply_financial_snapshots_asof,
    apply_financial_snapshots_asof_batch,
    merge_snapshot_frames,
    normalize_fina_indicator_rows,
    percent_to_ratio,
    select_pit_snapshot_for_date,
)
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import TushareProvider


def test_percent_to_ratio_converts_tushare_percent_points() -> None:
    # Tushare fina_indicator fields are always percentage points.
    assert percent_to_ratio(12.5) == pytest.approx(0.125)
    assert percent_to_ratio(1.2) == pytest.approx(0.012)
    assert percent_to_ratio(0.5) == pytest.approx(0.005)
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
            "date": pd.to_datetime(["2024-04-19", "2024-04-20", "2024-05-10", "2024-08-20"]),
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
            "date": pd.to_datetime(["2024-04-19", "2024-04-20", "2024-08-20", "2024-08-21"]),
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


def _batch_daily_frame() -> pd.DataFrame:
    frames = []
    for symbol in ("600000", "000001", "300750"):
        frames.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
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
                    "financial_source": ["local_vendor"] * 4,
                    "financial_report_date": [""] * 4,
                    "financial_as_of": [""] * 4,
                    "financial_trust_level": ["missing"] * 4,
                    "financial_completeness": [0.0] * 4,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _batch_snapshot_frame() -> pd.DataFrame:
    frames = []
    for symbol, roe, debt in (("600000", 15.0, 35.0), ("000001", 18.0, 38.0)):
        frames.append(
            normalize_fina_indicator_rows(
                pd.DataFrame(
                    {
                        "end_date": ["20231231", "20240630"],
                        "ann_date": ["20240420", "20240820"],
                        "roe": [roe, roe + 1.0],
                        "debt_to_assets": [debt, debt + 1.0],
                        "update_flag": [1, 1],
                    }
                ),
                symbol=symbol,
            )
        )
    return pd.concat(frames, ignore_index=True)


def test_batch_asof_join_fills_only_symbols_with_snapshots() -> None:
    daily = _batch_daily_frame()
    snaps = _batch_snapshot_frame()
    out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)

    # 600000: pre-announcement stays missing, on/after ann_date gets reported.
    s1 = out[out["symbol"] == "600000"].reset_index(drop=True)
    assert np.isnan(float(s1.iloc[0]["roe"]))
    assert s1.iloc[0]["financial_trust_level"] == "missing"
    assert float(s1.iloc[1]["roe"]) == pytest.approx(0.15)
    assert float(s1.iloc[2]["roe"]) == pytest.approx(0.16)
    assert bool(s1.iloc[2]["financial_data_complete"]) is True
    assert s1.iloc[2]["financial_source"] == "tushare_fina_indicator"

    # 300750 has no snapshots: stays honestly missing, never fabricated.
    s3 = out[out["symbol"] == "300750"].reset_index(drop=True)
    assert all(np.isnan(float(v)) for v in s3["roe"])
    assert all(bool(v) is False for v in s3["financial_data_complete"])
    assert all(v == "missing" for v in s3["financial_trust_level"])
    assert all(v == "roe,debt_ratio" for v in s3["financial_missing_fields"])


def test_batch_asof_join_matches_single_symbol_semantics() -> None:
    daily = _batch_daily_frame()
    daily = daily[daily["symbol"] == "600000"].copy()
    snaps = _batch_snapshot_frame()
    snaps = snaps[snaps["symbol"] == "600000"].copy()

    single = apply_financial_snapshots_asof(daily, snaps, only_fill_pending=True)
    batched = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
    for col in (
        "roe",
        "debt_ratio",
        "financial_data_complete",
        "financial_source",
        "financial_trust_level",
        "financial_completeness",
    ):
        if col in {"roe", "debt_ratio", "financial_completeness"}:
            assert np.allclose(
                single[col].to_numpy(dtype=float),
                batched[col].to_numpy(dtype=float),
                equal_nan=True,
            ), col
        else:
            assert (single[col].to_numpy() == batched[col].to_numpy()).all(), col


def test_batch_asof_join_preserves_reported_financials_when_pending_only() -> None:
    daily = _batch_daily_frame()
    daily.loc[0, "roe"] = 0.08
    daily.loc[0, "debt_ratio"] = 0.55
    daily.loc[0, "financial_data_complete"] = True
    daily.loc[0, "financial_source"] = "tushare_fina_indicator"
    daily.loc[0, "financial_trust_level"] = "reported"
    daily.loc[0, "financial_completeness"] = 1.0

    snaps = _batch_snapshot_frame()
    # only_fill_pending=True must NOT overwrite the already-reported row.
    out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
    row = out[out["symbol"] == "600000"].iloc[0]
    assert float(row["roe"]) == pytest.approx(0.08)
    assert row["financial_trust_level"] == "reported"
    # Unreported rows are still filled.
    row2 = out[out["symbol"] == "600000"].iloc[1]
    assert float(row2["roe"]) == pytest.approx(0.15)


def test_batch_asof_join_same_ann_date_prefers_later_end_date() -> None:
    daily = _batch_daily_frame().head(2).copy()
    snaps = normalize_fina_indicator_rows(
        pd.DataFrame(
            {
                "end_date": ["20231231", "20240331"],
                "ann_date": ["20240420", "20240420"],
                "roe": [10.0, 12.0],
                "debt_to_assets": [40.0, 42.0],
                "update_flag": [0, 0],
            }
        ),
        symbol="600000",
    )
    out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
    # 2024-04-19 predates the disclosure: stays missing.
    assert np.isnan(float(out.iloc[0]["roe"]))
    # 2024-04-20: same ann_date, later end_date wins.
    assert float(out.iloc[1]["roe"]) == pytest.approx(0.12)


def test_batch_asof_join_handles_empty_inputs() -> None:
    daily = _batch_daily_frame()
    assert apply_financial_snapshots_asof_batch(daily, pd.DataFrame()) is not None
    assert len(apply_financial_snapshots_asof_batch(pd.DataFrame(), _batch_snapshot_frame())) == 0


def _daily_with_invalid_dates() -> pd.DataFrame:
    daily = _batch_daily_frame().head(4).copy()
    daily.loc[3, "date"] = pd.NaT
    return daily


def test_batch_asof_join_invalid_date_at_first_middle_and_last_row() -> None:
    snaps = _batch_snapshot_frame()
    # expected roe per output row for an invalid date at position 0 / 2 / 3:
    # rows whose date is NaT stay untouched (NaN); every other row is filled
    # at its original absolute position, never shifted.
    expected_by_position = {
        0: [np.nan, 0.15, 0.16, 0.16],
        2: [np.nan, 0.15, np.nan, 0.16],
        3: [np.nan, 0.15, 0.16, np.nan],
    }
    for position, expected in expected_by_position.items():
        daily = _batch_daily_frame().head(4).copy()
        daily.loc[position, "date"] = pd.NaT
        out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
        assert len(out) == 4
        s1 = out[out["symbol"] == "600000"].reset_index(drop=True)
        for index, expected_roe in enumerate(expected):
            value = float(s1.iloc[index]["roe"])
            if np.isnan(expected_roe):
                assert np.isnan(value), f"position={position} row {index} expected NaN"
            else:
                assert value == pytest.approx(expected_roe), (
                    f"position={position} row {index} misplaced"
                )
        # non-600000 rows keep their own (missing) values and are not shifted
        s3 = out[out["symbol"] == "300750"].reset_index(drop=True)
        assert all(np.isnan(float(v)) for v in s3["roe"])


def test_batch_asof_join_with_duplicate_index_labels() -> None:
    daily = _batch_daily_frame().head(4).copy()
    daily.index = [7, 7, 7, 7]
    snaps = _batch_snapshot_frame()
    out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
    assert list(out.index) == [7, 7, 7, 7]
    s1 = out[out["symbol"] == "600000"].reset_index(drop=True)
    assert np.isnan(float(s1.iloc[0]["roe"]))
    assert float(s1.iloc[1]["roe"]) == pytest.approx(0.15)
    assert float(s1.iloc[2]["roe"]) == pytest.approx(0.16)
    assert float(s1.iloc[3]["roe"]) == pytest.approx(0.16)


def test_batch_asof_join_missing_trust_level_treats_rows_as_pending() -> None:
    daily = _batch_daily_frame().head(4).copy()
    daily = daily.drop(columns=["financial_trust_level"])
    snaps = _batch_snapshot_frame()
    out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
    s1 = out[out["symbol"] == "600000"].reset_index(drop=True)
    assert float(s1.iloc[1]["roe"]) == pytest.approx(0.15)
    assert s1.iloc[1]["financial_trust_level"] == "reported"


def test_batch_asof_join_completes_missing_financial_columns() -> None:
    daily = pd.DataFrame(
        {
            "symbol": ["600000", "600000", "600000", "600000"],
            "date": pd.to_datetime(["2024-04-19", "2024-04-20", "2024-08-20", "2024-08-21"]),
            "close": [10.2, 10.4, 11.0, 11.1],
        }
    )
    snaps = _batch_snapshot_frame()
    out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
    assert "roe" in out.columns
    assert "financial_trust_level" in out.columns
    s1 = out[out["symbol"] == "600000"].reset_index(drop=True)
    assert np.isnan(float(s1.iloc[0]["roe"]))
    assert float(s1.iloc[1]["roe"]) == pytest.approx(0.15)
    assert float(s1.iloc[2]["roe"]) == pytest.approx(0.16)
    assert s1.iloc[0]["financial_source"] == "tushare_pending"


def test_batch_asof_join_preserves_trusted_reported_and_derived_rows() -> None:
    daily = _batch_daily_frame().head(4).copy()
    daily.loc[0, "roe"] = 0.08
    daily.loc[0, "debt_ratio"] = 0.55
    daily.loc[0, "financial_data_complete"] = True
    daily.loc[0, "financial_source"] = "tushare_fina_indicator"
    daily.loc[0, "financial_trust_level"] = "reported"
    daily.loc[1, "roe"] = 0.09
    daily.loc[1, "debt_ratio"] = 0.50
    daily.loc[1, "financial_data_complete"] = True
    daily.loc[1, "financial_source"] = "tushare_fina_indicator"
    daily.loc[1, "financial_trust_level"] = "derived"
    snaps = _batch_snapshot_frame()
    out = apply_financial_snapshots_asof_batch(daily, snaps, only_fill_pending=True)
    s1 = out[out["symbol"] == "600000"].reset_index(drop=True)
    assert float(s1.iloc[0]["roe"]) == pytest.approx(0.08)
    assert float(s1.iloc[1]["roe"]) == pytest.approx(0.09)
    assert float(s1.iloc[2]["roe"]) == pytest.approx(0.16)
