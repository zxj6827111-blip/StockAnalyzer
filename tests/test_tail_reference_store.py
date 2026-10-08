"""研究侧参考数据库的验收（改进计划 §3.1"补齐并校验…在独立研究库补齐后验证"）。

钉住的是四件不许商量的事：QFQ 日线不能进来、缺的东西要报缺而不是补 0、
重复同步不积累重复行、日历要拿权威口径交叉核对而不是让行情自己证明自己。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from stock_analyzer.research.tail_reference_store import (
    REFERENCE_TABLES,
    TailReferenceError,
    TailReferenceStore,
    read_warehouse_reference_frames,
    sync_reference_from_warehouse,
)

MON = date(2026, 3, 2)
TUE = date(2026, 3, 3)
SAT = date(2026, 3, 7)  # 周六：行情里出现它就是日历冲突
_SYMBOL = "600000.SH"

_CLI = Path(__file__).resolve().parents[1] / "scripts" / "sync_tail_reference_data.py"


def _warehouse(tmp_path: Path, *, days=(MON, TUE), bars: bool = True,
               status: bool = True, security: bool = True) -> Path:
    """造一个形状与 ``data/market_warehouse.py`` 的 DDL 一致的生产仓库夹具。"""
    path = tmp_path / "market.duckdb"
    con = duckdb.connect(str(path))
    try:
        if bars:
            con.execute(
                "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, open DOUBLE, "
                "high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, turnover DOUBLE, "
                "float_market_cap DOUBLE, name VARCHAR, is_st BOOLEAN, "
                "is_delisting_risk BOOLEAN, board VARCHAR, price_series_mode VARCHAR)"
            )
            for day in days:
                # 同一天既有 raw 也有 qfq：只有 raw 允许进研究库。
                for mode in ("raw", "qfq"):
                    factor = 1.0 if mode == "raw" else 0.5
                    con.execute(
                        "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [_SYMBOL, day, 10.0 * factor, 10.5 * factor, 9.8 * factor,
                         10.2 * factor, 1e6, 1.02e7, 5e9, "测试股", False, False, "main",
                         mode],
                    )
        if status:
            con.execute(
                "CREATE TABLE daily_trade_status (symbol VARCHAR, trade_date DATE, "
                "up_limit DOUBLE, down_limit DOUBLE, suspended BOOLEAN, "
                "suspend_type VARCHAR, source VARCHAR, as_of VARCHAR, "
                "coverage_complete BOOLEAN)"
            )
            con.execute(
                "INSERT INTO daily_trade_status VALUES (?,?,?,?,?,?,?,?,?)",
                [_SYMBOL, MON, 11.22, 8.82, False, "", "tushare_stk_limit",
                 "2026-03-02", True],
            )
        if security:
            con.execute(
                "CREATE TABLE security_status (symbol VARCHAR, effective_from DATE, "
                "effective_to DATE, status_type VARCHAR, status_value VARCHAR, "
                "board VARCHAR, exchange VARCHAR, source VARCHAR, as_of VARCHAR, "
                "coverage_complete BOOLEAN)"
            )
            con.execute(
                "INSERT INTO security_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                [_SYMBOL, MON, None, "ST", "N", "main", "SSE", "tushare_namechange",
                 "2026-03-02", True],
            )
    finally:
        con.close()
    return path


def _sync(tmp_path: Path, store_path: Path, warehouse: Path,
          *, end: date = TUE) -> dict:
    reading = read_warehouse_reference_frames(warehouse, start=MON, end=end)
    with TailReferenceStore(store_path) as store:
        return sync_reference_from_warehouse(store, reading)


def _rows(store_path: Path, name: str) -> list[dict]:
    con = duckdb.connect(str(store_path), read_only=True)
    try:
        cursor = con.execute(f"SELECT * FROM {REFERENCE_TABLES[name]}")  # noqa: S608
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 写入口径
# ---------------------------------------------------------------------------


def test_reference_tables_start_empty_and_say_so(tmp_path) -> None:
    with TailReferenceStore(tmp_path / "ref.duckdb") as store:
        coverage = store.coverage()
    assert coverage["gaps"] == sorted(f"{name}_table_missing" for name in REFERENCE_TABLES)
    assert all(entry["rows"] == 0 for entry in coverage["sources"].values())


def test_only_raw_daily_bars_are_landed_and_deduplicated(tmp_path) -> None:
    """一个 symbol-day 一行；qfq 那行不进研究库（ADR-002：成交必须用未复权价）。"""
    warehouse = _warehouse(tmp_path)
    store_path = tmp_path / "ref.duckdb"
    result = _sync(tmp_path, store_path, warehouse)
    assert result["landed_rows"]["daily_bars"] == 2
    rows = _rows(store_path, "daily_bars")
    assert {row["price_basis"] for row in rows} == {"raw"}
    assert sorted(str(row["trade_date"])[:10] for row in rows) == ["2026-03-02", "2026-03-03"]
    # 重复同步幂等：按主键替换，不积累第二行。
    _sync(tmp_path, store_path, warehouse)
    assert len(_rows(store_path, "daily_bars")) == 2


def test_adjusted_basis_is_refused_rather_than_rescaled(tmp_path) -> None:
    frame = pd.DataFrame([{"symbol": _SYMBOL, "date": MON, "open": 5.0, "high": 5.2,
                           "low": 4.9, "close": 5.1}])
    with TailReferenceStore(tmp_path / "ref.duckdb") as store:
        with pytest.raises(TailReferenceError, match="raw"):
            store.upsert_daily_bars(frame, price_basis="qfq", as_of="2026-03-02")
        with pytest.raises(TailReferenceError, match="must be declared"):
            store.upsert_daily_bars(frame, price_basis="hfq", as_of="2026-03-02")


def test_frames_missing_required_columns_are_refused(tmp_path) -> None:
    with TailReferenceStore(tmp_path / "ref.duckdb") as store:
        with pytest.raises(TailReferenceError, match="missing column"):
            store.upsert_daily_bars(pd.DataFrame([{"symbol": _SYMBOL}]),
                                    price_basis="raw", as_of="2026-03-02")
        with pytest.raises(TailReferenceError, match="explicit as_of"):
            store.upsert_daily_bars(
                pd.DataFrame([{"symbol": _SYMBOL, "date": MON, "open": 1.0, "high": 1.0,
                              "low": 1.0, "close": 1.0}]),
                price_basis="raw", as_of="",
            )


# ---------------------------------------------------------------------------
# 读出口径：缺就说缺，不填 0、不猜 normal
# ---------------------------------------------------------------------------


def test_missing_reference_rows_stay_missing(tmp_path) -> None:
    """TUE 没有涨跌停与停牌声明：值必须是 None 且被点名，不能变成 0 / normal。"""
    warehouse = _warehouse(tmp_path)
    store_path = tmp_path / "ref.duckdb"
    _sync(tmp_path, store_path, warehouse)
    with TailReferenceStore(store_path) as store:
        complete = store.execution_inputs(_SYMBOL, MON)
        gap = store.execution_inputs(_SYMBOL, TUE)
    assert complete["sufficient"] is False  # 仓库里没有 trade_status 列
    assert complete["up_limit"] == pytest.approx(11.22)
    assert complete["down_limit"] == pytest.approx(8.82)
    assert complete["suspended"] is False
    assert "trade_status" in complete["missing"]
    assert gap["up_limit"] is None and gap["down_limit"] is None
    assert gap["suspended"] is None
    assert gap["open"] == pytest.approx(10.0)
    assert gap["sufficient"] is False
    assert {"limit_prices", "suspend_status", "trade_status"} <= set(gap["missing"])


def test_approximated_limit_prices_are_not_usable_by_default(tmp_path) -> None:
    frame = pd.DataFrame([{"symbol": _SYMBOL, "trade_date": MON, "up_limit": 11.0,
                           "down_limit": 9.0}])
    store_path = tmp_path / "ref.duckdb"
    with TailReferenceStore(store_path) as store:
        store.upsert_limit_prices(frame, as_of="2026-03-02", approximated=True)
    with TailReferenceStore(store_path) as store:
        strict = store.execution_inputs(_SYMBOL, MON)
        loose = store.execution_inputs(_SYMBOL, MON, limit_prices_require_exact=False)
        coverage = store.coverage()
    assert strict["up_limit"] is None and strict["limit_prices_approximated"] is True
    assert loose["up_limit"] == pytest.approx(11.0)
    assert coverage["approximated_limit_price_rows"] == 1


# ---------------------------------------------------------------------------
# 日历：交叉核对而不是自我证明
# ---------------------------------------------------------------------------


def test_calendar_flags_rows_the_authority_disputes(tmp_path) -> None:
    with TailReferenceStore(tmp_path / "ref.duckdb") as store:
        store.upsert_calendar([MON, TUE, SAT], as_of="2026-03-07")
        store.upsert_daily_bars(
            pd.DataFrame([{"symbol": _SYMBOL, "date": SAT, "open": 10.0, "high": 10.0,
                           "low": 10.0, "close": 10.0}]),
            price_basis="raw", as_of="2026-03-07",
        )
        assert store.calendar(MON, TUE) == [MON, TUE]
        assert store.calendar_conflicts() == ["2026-03-07"]
        coverage = store.coverage()
    assert coverage["sources"]["trade_calendar"]["open_sessions"] == 2
    assert coverage["calendar_conflicts"] == ["2026-03-07"]


def test_calendar_rows_cannot_be_landed_without_provenance(tmp_path) -> None:
    with TailReferenceStore(tmp_path / "ref.duckdb") as store:
        with pytest.raises(TailReferenceError, match="as_of"):
            store.upsert_calendar([MON], as_of="")


def test_row_level_provenance_beats_the_caller_default(tmp_path) -> None:
    """仓库那一行自带 source/as_of 时以它为准 —— 事后要能回答"这行涨跌停是谁给的"。"""
    store_path = tmp_path / "ref.duckdb"
    _sync(tmp_path, store_path, _warehouse(tmp_path))
    limits = _rows(store_path, "limit_prices")
    assert limits[0]["source"] == "tushare_stk_limit"
    assert str(limits[0]["as_of"])[:10] == "2026-03-02"
    # 日线帧里没有 source/as_of 列，才落到调用方给的默认口径。
    assert _rows(store_path, "daily_bars")[0]["source"] == "market_warehouse"


# ---------------------------------------------------------------------------
# 缺表要报缺，不能报"已补齐"
# ---------------------------------------------------------------------------


def test_missing_warehouse_tables_are_reported_as_gaps(tmp_path) -> None:
    warehouse = _warehouse(tmp_path, status=False, security=False)
    reading = read_warehouse_reference_frames(warehouse, start=MON, end=TUE)
    assert set(reading.gaps) == {
        "daily_trade_status_table_missing", "suspend_status_source_table_missing",
        "security_status_table_missing",
    }
    assert "daily_bars" in reading.frames
    result = _sync(tmp_path, tmp_path / "ref.duckdb", warehouse)
    assert set(result["missing_sources"]) == {
        "limit_prices_table_missing", "security_status_table_missing",
        "suspend_status_table_missing",
    }
    assert result["sufficient_sources"] == ["daily_bars", "trade_calendar"]


def test_unreadable_warehouse_is_a_visible_error(tmp_path) -> None:
    with pytest.raises(TailReferenceError, match="warehouse not found"):
        read_warehouse_reference_frames(tmp_path / "nope.duckdb", start=MON, end=TUE)


# ---------------------------------------------------------------------------
# CLI：退出码必须是真实退出码
# ---------------------------------------------------------------------------


def _cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )


def _cli_args(tmp_path, warehouse, out) -> list[str]:
    return ["--warehouse", str(warehouse), "--out", str(out),
            "--start", MON.isoformat(), "--end", TUE.isoformat(), "--quiet"]


def test_cli_lands_every_source_and_exits_zero(tmp_path) -> None:
    out = tmp_path / "ref.duckdb"
    report = tmp_path / "report.json"
    result = _cli(_cli_args(tmp_path, _warehouse(tmp_path), out)
                  + ["--report", str(report)])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["missing_sources"] == []
    assert sorted(payload["sufficient_sources"]) == sorted(REFERENCE_TABLES)
    with TailReferenceStore(out) as store:
        assert store.coverage()["sources"]["limit_prices"]["rows"] == 1


def test_cli_exits_insufficient_when_a_source_is_missing(tmp_path) -> None:
    warehouse = _warehouse(tmp_path, security=False)
    result = _cli(_cli_args(tmp_path, warehouse, tmp_path / "ref.duckdb"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "来源不足" in result.stdout


def test_cli_fails_visibly_on_an_unreadable_warehouse(tmp_path) -> None:
    result = _cli(_cli_args(tmp_path, tmp_path / "missing.duckdb", tmp_path / "ref.duckdb"))
    assert result.returncode == 5, result.stdout + result.stderr
    assert "参考数据不可用" in result.stderr
    assert not (tmp_path / "ref.duckdb").exists()


def test_vendor_daily_raw_source_declares_units_and_leaves_undeclared_flags_unknown(
    tmp_path,
) -> None:
    """生产仓库声明不了 RAW 口径时，vendor 全A日K 是可证明口径的替代日线源。

    数量倍率沿用 ``VendorZipOverlayProvider`` 的声明（手→股 ×100、千元→元 ×1000、
    万元→元 ×10000）；源里没有的证券状态必须留空，因为"没声明"不等于"不是 ST"。
    """
    import zipfile
    from datetime import date

    from stock_analyzer.research.tail_reference_store import (
        SOURCE_VENDOR_ZIP_DAILY,
        TailReferenceStore,
        read_vendor_daily_raw_frames,
    )

    root = tmp_path / "vendor"
    daily = root / "全A日K"
    daily.mkdir(parents=True)
    header = "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount,circ_mv"
    rows = [
        "600000.SH,2025-12-31,10.0,10.2,9.9,10.1,10.0,0.1,1.0,1000.0,10000.0,200000.0",
        "600000.SH,2026-01-05,10.1,10.5,10.0,10.4,10.1,0.3,2.9752,2000.0,21000.0,210000.0",
        "600000.SH,2026-01-06,10.4,10.6,10.3,10.5,10.4,0.1,0.9615,3000.0,31500.0,315000.0",
    ]
    with zipfile.ZipFile(daily / "2026.zip", "w") as archive:
        archive.writestr("2026/600000.SH.csv", f"{header}\n" + "\n".join(rows) + "\n")

    frame = read_vendor_daily_raw_frames(root, start=date(2026, 1, 1), end=date(2026, 1, 31))

    assert [str(item) for item in frame["symbol"]] == ["600000", "600000"]
    assert frame["volume"].tolist() == [200000.0, 300000.0]
    assert frame["turnover"].tolist() == [21_000_000.0, 31_500_000.0]
    assert frame["float_market_cap"].tolist() == [2_100_000_000.0, 3_150_000_000.0]
    assert set(frame["is_st"].to_numpy()) == {None}

    with TailReferenceStore(tmp_path / "ref.duckdb") as store:
        landed = store.upsert_daily_bars(
            frame, price_basis="raw", source=SOURCE_VENDOR_ZIP_DAILY, as_of="2026-01-31"
        )
        coverage = store.coverage()
    assert landed == 2
    assert coverage["sources"]["daily_bars"]["rows"] == 2
    assert "daily_bars_table_missing" not in coverage["gaps"]
