"""Tests for the batch nightly vendor update mode."""

from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.update_vendor_daily_from_tushare import (
    _daily_to_25_columns,
    _distribute_batch_day,
    _fetch_market_wide_by_date,
    _load_factor_entry_map,
    _merge_factor_rows_scaled,
    _read_last_date_fast,
    _rebuild_daily_year_zip,
    _run_batch,
    _symbol_daily_last_date,
    _update_last_date_index,
)


def _fake_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "600519.SH"],
            "trade_date": ["20260731", "20260731", "20260731"],
            "open": [10.0, 10.0, 100.0],
            "high": [10.5, 10.5, 105.0],
            "low": [9.8, 9.8, 98.0],
            "close": [10.2, 10.2, 102.0],
            "pre_close": [10.0, 10.0, 100.0],
            "change": [0.2, 0.2, 2.0],
            "pct_chg": [2.0, 2.0, 2.0],
            "vol": [1000.0, 1000.0, 500.0],
            "amount": [10200.0, 10200.0, 51000.0],
        }
    )


def _fake_basic_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "600519.SH"],
            "trade_date": ["20260731", "20260731"],
            "turnover_rate": [1.2, 0.5],
            "turnover_rate_f": [1.2, 0.5],
            "volume_ratio": [1.1, 0.9],
            "pe": [10.0, 30.0],
            "pe_ttm": [9.5, 29.0],
            "pb": [1.0, 8.0],
            "ps": [2.0, 15.0],
            "ps_ttm": [1.9, 14.5],
            "dv_ratio": [2.0, 1.0],
            "dv_ttm": [2.1, 1.1],
            "total_share": [100.0, 12.5],
            "float_share": [80.0, 12.5],
            "free_share": [75.0, 12.5],
            "total_mv": [1020.0, 1275.0],
            "circ_mv": [816.0, 1275.0],
        }
    )


def _fake_adj_frame(ts_codes: list[str], trade_date: str, factor: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ts_codes,
            "trade_date": [trade_date] * len(ts_codes),
            "adj_factor": [factor] * len(ts_codes),
        }
    )


class _FakePro:
    def __init__(self) -> None:
        self.daily_calls: list[dict[str, object]] = []
        self.adj_calls: list[dict[str, object]] = []

    def daily(self, **kwargs: object) -> pd.DataFrame:
        self.daily_calls.append(kwargs)
        offset = int(kwargs.get("offset", 0))
        frame = _fake_daily_frame()
        if offset == 0:
            return frame
        return frame.iloc[0:0]

    def daily_basic(self, **kwargs: object) -> pd.DataFrame:
        _ = kwargs
        return _fake_basic_frame()

    def adj_factor(self, **kwargs: object) -> pd.DataFrame:
        self.adj_calls.append(kwargs)
        return _fake_adj_frame(
            ts_codes=["000001.SZ", "600519.SH"],
            trade_date=str(kwargs.get("trade_date", "")),
            factor=2.5,
        )

    def trade_cal(self, **kwargs: object) -> pd.DataFrame:
        _ = kwargs
        return pd.DataFrame({"cal_date": ["20260731"]})


class _FakeApi:
    def __init__(self, pro: _FakePro) -> None:
        self._pro = pro

    def _resolve_pro_api(self) -> _FakePro:
        return self._pro

    def _call_with_retry(self, fn: object) -> object:
        return fn()


def test_fetch_market_wide_by_date_pages_daily() -> None:
    pro = _FakePro()
    day = _fetch_market_wide_by_date(api=_FakeApi(pro), trade_date="20260731")
    assert not day["daily"].empty
    assert not day["basic"].empty
    assert not day["adj"].empty
    assert pro.daily_calls[0]["trade_date"] == "20260731"
    assert "offset" in pro.daily_calls[0]


def test_distribute_batch_day_merges_by_year_and_25_columns() -> None:
    updates_by_year: dict[int, dict[str, pd.DataFrame]] = {}
    factor_updates: dict[str, pd.DataFrame] = {}
    _distribute_batch_day(
        daily=_fake_daily_frame(),
        basic=_fake_basic_frame(),
        adj=_fake_adj_frame(["000001.SZ"], "20260731", 2.5),
        updates_by_year=updates_by_year,
        factor_updates=factor_updates,
        skip_factors=False,
    )
    # 2026 bucket contains both symbols with 25 vendor columns.
    assert 2026 in updates_by_year
    assert set(updates_by_year[2026].keys()) == {"000001.SZ", "600519.SH"}
    frame = updates_by_year[2026]["000001.SZ"]
    assert frame.columns.tolist() == _daily_to_25_columns(
        ts_code="000001.SZ",
        daily=_fake_daily_frame().iloc[[0]],
        basic=_fake_basic_frame().iloc[[0]],
    ).columns.tolist()
    assert "code" in frame.columns
    assert frame["datetime"].iloc[0] == "20260731"
    # Adj factors accumulated per symbol.
    assert "000001.SZ" in factor_updates


def test_distribute_batch_day_concats_multiple_days() -> None:
    updates_by_year: dict[int, dict[str, pd.DataFrame]] = {}
    factor_updates: dict[str, pd.DataFrame] = {}
    for trade_date in ("20260730", "20260731"):
        daily = _fake_daily_frame().copy()
        daily["trade_date"] = trade_date
        _distribute_batch_day(
            daily=daily,
            basic=_fake_basic_frame(),
            adj=_fake_adj_frame(["000001.SZ"], trade_date, 2.5),
            updates_by_year=updates_by_year,
            factor_updates=factor_updates,
            skip_factors=False,
        )
    frame = updates_by_year[2026]["000001.SZ"]
    assert len(frame) == 2
    assert sorted(frame["datetime"].tolist()) == ["20260730", "20260731"]


def test_rebuild_daily_year_zip_roundtrip(tmp_path: Path) -> None:
    daily_root = tmp_path / "全A日K"
    daily_root.mkdir()
    updates_by_year: dict[int, dict[str, pd.DataFrame]] = {}
    _distribute_batch_day(
        daily=_fake_daily_frame(),
        basic=_fake_basic_frame(),
        adj=pd.DataFrame(),
        updates_by_year=updates_by_year,
        factor_updates={},
        skip_factors=True,
    )
    report = _rebuild_daily_year_zip(daily_root, 2026, updates_by_year[2026])
    assert report["entries_total"] == 2
    assert report["latest_dates"] == {
        "000001": "2026-07-31",
        "600519": "2026-07-31",
    }
    archive_path = daily_root / "2026.zip"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "2026/000001.SZ.csv" in names
    assert "2026/600519.SH.csv" in names


def _build_zip_fixture(tmp_path: Path) -> Path:
    daily_root = tmp_path / "全A日K"
    daily_root.mkdir()
    csv_text = (
        "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount\n"
        "000001.SZ,2026-07-30,10.0,10.5,9.8,10.2,10.0,0.2,2.0,1000.0,10200.0\n"
        "000001.SZ,2026-07-31,10.2,10.7,10.0,10.4,10.2,0.2,2.0,1100.0,11440.0\n"
    )
    with zipfile.ZipFile(daily_root / "2026.zip", "w") as archive:
        archive.writestr("2026/000001.SZ.csv", csv_text)
        archive.writestr("2026/600519.SH.csv", csv_text)
    return daily_root


def test_read_last_date_fast(tmp_path: Path) -> None:
    daily_root = _build_zip_fixture(tmp_path)
    latest = _read_last_date_fast(daily_root / "2026.zip", "2026/000001.SZ.csv")
    assert latest == date(2026, 7, 31)


def test_symbol_daily_last_date_uses_index(tmp_path: Path) -> None:
    daily_root = _build_zip_fixture(tmp_path)
    full_scan = _symbol_daily_last_date(daily_root, "000001.SZ")
    assert full_scan == date(2026, 7, 31)
    index = {
        "version": 1,
        "symbols": {"000001.SZ": {"latest_date": "2026-07-31", "entries": []}},
    }
    assert _symbol_daily_last_date(daily_root, "000001.SZ", index=index) == date(2026, 7, 31)


def test_update_last_date_index_incremental(tmp_path: Path) -> None:
    daily_root = _build_zip_fixture(tmp_path)
    index_path = tmp_path / "vendor_daily_index.json"
    index_path.write_text(
        json.dumps(
            {"version": 1, "symbols": {"000001.SZ": {"latest_date": "2026-07-01", "entries": []}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = _update_last_date_index(
        index_path=index_path,
        daily_root=daily_root,
        updated_symbols={"000001.SZ"},
    )
    assert report["updated"] is True
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["symbols"]["000001.SZ"]["latest_date"] == "2026-07-31"


def test_update_last_date_index_uses_rebuild_dates_without_zip_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_root = tmp_path / "daily"
    daily_root.mkdir()
    index_path = tmp_path / "vendor_daily_index.json"
    index_path.write_text(
        json.dumps(
            {"version": 1, "symbols": {"000001": {"latest_date": "2026-07-01", "entries": []}}}
        ),
        encoding="utf-8",
    )

    def forbidden_zip_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("rebuild dates must avoid ZIP reads")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zip_open)
    report = _update_last_date_index(
        index_path=index_path,
        daily_root=daily_root,
        updated_symbols={"000001.SZ"},
        rebuild_latest_dates={"000001.SZ": date(2026, 7, 31)},
    )

    assert report["dates_from_rebuild"] == 1
    assert report["dates_from_fallback"] == 0
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["symbols"]["000001"]["latest_date"] == "2026-07-31"


def test_update_last_date_index_fallback_opens_archive_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_root = _build_zip_fixture(tmp_path)
    index_path = tmp_path / "vendor_daily_index.json"
    index_path.write_text(
        json.dumps(
            {
                "version": 1,
                "symbols": {
                    "000001": {"latest_date": "2026-07-01", "entries": []},
                    "600519": {"latest_date": "2026-07-01", "entries": []},
                },
            }
        ),
        encoding="utf-8",
    )
    opened: list[str] = []
    real_zipfile = zipfile.ZipFile

    class CountingZipFile(real_zipfile):  # type: ignore[misc, valid-type]
        def __init__(self, file: object, *args: object, **kwargs: object) -> None:
            opened.append(str(file))
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", CountingZipFile)
    report = _update_last_date_index(
        index_path=index_path,
        daily_root=daily_root,
        updated_symbols={"000001.SZ", "600519.SH"},
    )

    assert report["dates_from_fallback"] == 2
    assert opened == [str(daily_root / "2026.zip")]


def _build_factor_fixture(tmp_path: Path, values: dict[str, float]) -> Path:
    factors_root = tmp_path / "复权因子"
    factors_root.mkdir()
    csv_text = "股票代码,交易日期,复权因子\n"
    for trade_date, value in values.items():
        csv_text += f"000001.SZ,{trade_date},{value}\n"
    with zipfile.ZipFile(factors_root / "复权因子_前复权.zip", "w") as archive:
        archive.writestr("2026/000001.SZ.csv", csv_text)
    return factors_root


def test_merge_factor_rows_scaled_matches_full_reanchor(tmp_path: Path) -> None:
    # Old stored qfq series anchored at 2026-07-30 (latest = 1.0).
    factors_root = _build_factor_fixture(
        tmp_path,
        {"20260729": 0.98, "20260730": 1.00},
    )
    adj_new = _fake_adj_frame(["000001.SZ"], "20260731", 3.0)
    adj_old = _fake_adj_frame(["000001.SZ"], "20260730", 2.5)

    merged = _merge_factor_rows_scaled(
        ts_code="000001.SZ",
        adj_new_day=adj_new,
        adj_old_day=adj_old,
        factors_root=factors_root,
        archive_name="复权因子_前复权.zip",
        anchor="latest",
    )
    assert merged is not None
    by_date = dict(zip(merged["交易日期"], merged["复权因子"], strict=True))
    # Full-reanchor equivalent: qfq(T) = adj(T)/adj(T_new) with adj(T_old)=2.5.
    assert by_date["20260729"] == pytest.approx(0.98 * 2.5 / 3.0)
    assert by_date["20260730"] == pytest.approx(2.5 / 3.0)
    assert by_date["20260731"] == pytest.approx(1.0)


def test_merge_factor_rows_scaled_multiple_new_dates(tmp_path: Path) -> None:
    """Multiple new days must each be scaled by their own adj factor."""
    factors_root = _build_factor_fixture(
        tmp_path,
        {"20260729": 1.00},
    )
    # Two new days: adj jumps on 07-31 (corporate action).
    adj_new = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20260730", "20260731"],
            "adj_factor": [2.5, 3.0],
        }
    )
    adj_old = _fake_adj_frame(["000001.SZ"], "20260729", 2.0)

    merged = _merge_factor_rows_scaled(
        ts_code="000001.SZ",
        adj_new_day=adj_new,
        adj_old_day=adj_old,
        factors_root=factors_root,
        archive_name="复权因子_前复权.zip",
        anchor="latest",
    )
    assert merged is not None
    by_date = dict(zip(merged["交易日期"], merged["复权因子"], strict=True))
    # qfq(T) = adj(T)/adj(T_end): 07-30 -> 2.5/3.0, 07-31 -> 1.0; old 07-29 -> 1.0*2.0/3.0.
    assert by_date["20260729"] == pytest.approx(2.0 / 3.0)
    assert by_date["20260730"] == pytest.approx(2.5 / 3.0)
    assert by_date["20260731"] == pytest.approx(1.0)

    hfq_root = tmp_path / "复权因子_hfq"
    hfq_root.mkdir()
    with zipfile.ZipFile(hfq_root / "复权因子_后复权.zip", "w") as archive:
        archive.writestr(
            "2026/000001.SZ.csv",
            "股票代码,交易日期,复权因子\n000001.SZ,20260729,1.0\n",
        )
    hfq_merged = _merge_factor_rows_scaled(
        ts_code="000001.SZ",
        adj_new_day=adj_new,
        adj_old_day=adj_old,
        factors_root=hfq_root,
        archive_name="复权因子_后复权.zip",
        anchor="earliest",
    )
    assert hfq_merged is not None
    hfq_by_date = dict(zip(hfq_merged["交易日期"], hfq_merged["复权因子"], strict=True))
    # hfq keeps history; each new day: last_old * adj(d)/adj(T_old).
    assert hfq_by_date["20260729"] == pytest.approx(1.0)
    assert hfq_by_date["20260730"] == pytest.approx(1.0 * 2.5 / 2.0)
    assert hfq_by_date["20260731"] == pytest.approx(1.0 * 3.0 / 2.0)


def test_merge_factor_rows_scaled_suspended_anchor_day_seeds(tmp_path: Path) -> None:
    """Stock suspended on the anchor day must still update (seed fallback)."""
    factors_root = _build_factor_fixture(
        tmp_path,
        {"20260729": 0.98, "20260730": 1.00},
    )
    adj_new = _fake_adj_frame(["000001.SZ"], "20260731", 3.0)
    # Anchor day frame does not contain the stock (suspended that day); the
    # caller filters by ts_code, so the stock's slice is empty.
    adj_old = _fake_adj_frame(["600000.SH"], "20260730", 2.5)
    adj_old_filtered = adj_old[adj_old["ts_code"] == "000001.SZ"]

    merged = _merge_factor_rows_scaled(
        ts_code="000001.SZ",
        adj_new_day=adj_new,
        adj_old_day=adj_old_filtered,
        factors_root=factors_root,
        archive_name="复权因子_前复权.zip",
        anchor="latest",
    )
    assert merged is not None
    by_date = dict(zip(merged["交易日期"], merged["复权因子"], strict=True))
    # Seeded from the new day only: qfq of a single day is exactly 1.0.
    assert set(by_date.keys()) == {"20260731"}
    assert by_date["20260731"] == pytest.approx(1.0)


def test_merge_factor_rows_scaled_hfq_keeps_history(tmp_path: Path) -> None:
    factors_root = tmp_path / "复权因子"
    factors_root.mkdir()
    csv_text = (
        "股票代码,交易日期,复权因子\n"
        "000001.SZ,20260729,1.0\n"
        "000001.SZ,20260730,1.02\n"
    )
    with zipfile.ZipFile(factors_root / "复权因子_后复权.zip", "w") as archive:
        archive.writestr("2026/000001.SZ.csv", csv_text)
    adj_new = _fake_adj_frame(["000001.SZ"], "20260731", 3.0)
    adj_old = _fake_adj_frame(["000001.SZ"], "20260730", 2.5)
    merged = _merge_factor_rows_scaled(
        ts_code="000001.SZ",
        adj_new_day=adj_new,
        adj_old_day=adj_old,
        factors_root=factors_root,
        archive_name="复权因子_后复权.zip",
        anchor="earliest",
    )
    assert merged is not None
    by_date = dict(zip(merged["交易日期"], merged["复权因子"], strict=True))
    assert by_date["20260729"] == pytest.approx(1.0)
    assert by_date["20260730"] == pytest.approx(1.02)
    assert by_date["20260731"] == pytest.approx(1.02 * 3.0 / 2.5)


def test_load_factor_entry_map_groups_years(tmp_path: Path) -> None:
    factors_root = tmp_path / "复权因子"
    factors_root.mkdir()
    with zipfile.ZipFile(factors_root / "复权因子_前复权.zip", "w") as archive:
        archive.writestr(
            "2025/000001.SZ.csv",
            "股票代码,交易日期,复权因子\n"
            "000001.SZ,20251225,0.90\n"
            "000001.SZ,20251228,0.95\n",
        )
        archive.writestr(
            "2026/000001.SZ.csv",
            "股票代码,交易日期,复权因子\n"
            "000001.SZ,20260105,0.98\n"
            "000001.SZ,20251228,0.97\n",
        )
        archive.writestr(
            "2026/600519.SH.csv",
            "股票代码,交易日期,复权因子\n" "600519.SH,20260105,1.00\n",
        )
        archive.writestr("__MACOSX/._000001.SZ.csv", "noise\n")
        archive.writestr("2026/not_a_code.csv", "股票代码,交易日期,复权因子\n")

    stored_map = _load_factor_entry_map(factors_root, "复权因子_前复权.zip")
    assert set(stored_map) == {"000001.SZ", "600519.SH"}
    frame = stored_map["000001.SZ"]
    assert frame["交易日期"].astype(str).tolist() == ["20251225", "20251228", "20260105"]
    by_date = dict(zip(frame["交易日期"].astype(str), frame["复权因子"], strict=True))
    # Duplicate date across year entries resolved with the later entry winning.
    assert by_date["20251228"] == pytest.approx(0.97)
    assert by_date["20251225"] == pytest.approx(0.90)
    assert by_date["20260105"] == pytest.approx(0.98)
    assert stored_map["600519.SH"]["复权因子"].iloc[0] == pytest.approx(1.0)


def test_merge_factor_rows_scaled_uses_stored_map(tmp_path: Path) -> None:
    factors_root = _build_factor_fixture(
        tmp_path,
        {"20260729": 0.98, "20260730": 1.00},
    )
    adj_new = _fake_adj_frame(["000001.SZ"], "20260731", 3.0)
    adj_old = _fake_adj_frame(["000001.SZ"], "20260730", 2.5)

    without_map = _merge_factor_rows_scaled(
        ts_code="000001.SZ",
        adj_new_day=adj_new,
        adj_old_day=adj_old,
        factors_root=factors_root,
        archive_name="复权因子_前复权.zip",
        anchor="latest",
    )
    stored_map = _load_factor_entry_map(factors_root, "复权因子_前复权.zip")
    with_map = _merge_factor_rows_scaled(
        ts_code="000001.SZ",
        adj_new_day=adj_new,
        adj_old_day=adj_old,
        factors_root=factors_root,
        archive_name="复权因子_前复权.zip",
        anchor="latest",
        stored_map=stored_map,
    )
    assert with_map is not None
    assert without_map is not None
    pd.testing.assert_frame_equal(with_map, without_map)


def test_run_batch_records_date_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """日期级异常必须记入 errors（8-13 现场空 errors 导致 judge 误判的根因）。"""
    import scripts.update_vendor_daily_from_tushare as updater_mod

    class _Pro:
        def trade_cal(self, **kwargs: object) -> pd.DataFrame:
            return pd.DataFrame({"cal_date": ["20260731"]})

    class _Api:
        def _resolve_pro_api(self) -> _Pro:
            return _Pro()

        def _call_with_retry(self, fn: object) -> object:
            return fn() if callable(fn) else fn

    def _boom(api: object, trade_date: str) -> dict[str, pd.DataFrame]:
        raise RuntimeError("rate-limit 500/min")

    monkeypatch.setattr(updater_mod, "_fetch_market_wide_by_date", _boom)
    report = _run_batch(
        api=_Api(),
        end_date=date(2026, 7, 31),
        daily_root=tmp_path / "全A日K",
        factors_root=tmp_path / "复权因子",
        skip_factors=True,
        dry_run=False,
        index_path="",
    )

    assert report["ok"] is False
    assert report["dates_failed"] == ["20260731"]
    assert report["errors"] == ["20260731:RuntimeError:rate-limit 500/min"]
    assert report["symbols_updated"] == 0
