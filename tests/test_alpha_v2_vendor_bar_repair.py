"""P3.3 Case G —— repair overlay 只能**补**，不能覆盖；且必须可逆、可验。

测试里的写入用一条 ANTI JOIN 插入，那是 ``MarketWarehouse.upsert_daily_bars`` 默认
语义的等价替身（该写入器自己的 ANTI JOIN 行为在 ``tests/test_market_warehouse*.py``
里已钉住）；本文件验的是 repair 这一层的**决策**：哪些键允许写、写了能不能退、
退的时候会不会多删。
"""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from stock_analyzer.data import vendor_bar_repair as vbr

GAP_DATE = "2025-11-17"
PROV = vbr.RepairProvenance(
    repair_batch_id="rb-p33-test",
    repair_source=vbr.ADJUSTMENT_SOURCE_REPAIR_FROM_TUSHARE,
    repair_reason=vbr.REPAIR_REASON_SOURCE_GAP,
    verified_by="p3_3_audit",
    source_file="tushare:daily/trade_date=20251117",
    source_query_time="2026-09-24T00:00:00+00:00",
)


def _planned(*rows: dict[str, object], mode: str = "raw") -> list[dict[str, object]]:
    """走一遍真实计划函数，保证补写行带上 price_series_mode / adjustment_source。"""
    return vbr.plan_insert_missing_only(
        incumbent_keys=set(),
        candidate_rows=list(rows),
        price_series_mode=mode,
        provenance=PROV,
    )[0]


def _bar(symbol: str, date: str = GAP_DATE, *, close: float = 10.0) -> dict[str, object]:
    return {
        "symbol": symbol,
        "date": date,
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": 1_000_000.0,
        "turnover": close * 1_000_000.0,
        "float_market_cap": 2_000_000_000.0,
        "board": "main",
        "is_st": False,
        "is_delisting_risk": False,
        "suspended": False,
    }


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE,"
        " low DOUBLE, close DOUBLE, volume DOUBLE, turnover DOUBLE,"
        " float_market_cap DOUBLE, board VARCHAR, is_st BOOLEAN, is_delisting_risk BOOLEAN,"
        " suspended BOOLEAN, price_series_mode VARCHAR, adjustment_source VARCHAR)"
    )
    return con


def _insert_missing_only(con: duckdb.DuckDBPyConnection, planned: list[dict]) -> int:
    """替身：与 ``upsert_daily_bars(overwrite_existing=False)`` 同一条 ANTI JOIN 规则。"""
    frame = pd.DataFrame(planned)
    frame["date"] = pd.to_datetime(frame["date"])
    con.register("df_stage", frame)
    before = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
    con.execute(
        "INSERT INTO daily_bars SELECT s.* FROM df_stage s ANTI JOIN daily_bars d "
        "ON s.symbol = d.symbol AND s.date = d.date"
    )
    con.unregister("df_stage")
    after = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
    return int(after - before)


def test_g1_plan_keeps_only_missing_keys() -> None:
    incumbent = {("600000", GAP_DATE)}
    rows = [_bar("600000", close=999.0), _bar("600001"), _bar("600002")]
    planned, skipped = vbr.plan_insert_missing_only(
        incumbent_keys=incumbent,
        candidate_rows=rows,
        price_series_mode="raw",
        provenance=PROV,
    )
    assert skipped == 1
    assert [row["symbol"] for row in planned] == ["600001", "600002"]
    assert all(row["price_series_mode"] == "raw" for row in planned)
    # 补写行必须自曝身份，不能被当成 vendor 原始交付。
    assert all(
        row["adjustment_source"] == vbr.ADJUSTMENT_SOURCE_REPAIR_FROM_TUSHARE
        for row in planned
    )


def test_g2_existing_vendor_row_survives_the_repair_batch() -> None:
    con = _warehouse()
    con.execute(
        "INSERT INTO daily_bars VALUES ('600000', ?, 1, 1, 1, 1, 1, 1, 1, 'main',"
        " false, false, false, 'raw', 'local_vendor_raw')",
        [GAP_DATE],
    )
    planned, skipped = vbr.plan_insert_missing_only(
        incumbent_keys={("600000", GAP_DATE)},
        # 同一主键、完全不同的价格：repair 源再"权威"也不许覆盖。
        candidate_rows=[_bar("600000", close=777.0), _bar("600001")],
        price_series_mode="raw",
        provenance=PROV,
    )
    assert skipped == 1 and len(planned) == 1
    assert _insert_missing_only(con, planned) == 1
    row = con.execute(
        "SELECT close, adjustment_source FROM daily_bars WHERE symbol='600000'"
    ).fetchone()
    assert row == (1.0, "local_vendor_raw")
    assert con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == 2


def test_g3_qfq_derivation_scales_prices_only_and_rejects_bad_factor() -> None:
    raw = {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 123.0}
    scaled = vbr.derive_qfq_from_raw(raw, factor=2.0)
    assert scaled == {"open": 20.0, "high": 22.0, "low": 18.0, "close": 21.0}
    # volume / turnover 不参与复权（与 delta 已存的 QFQ 侧一致，实测逐日 min/max 相同）。
    assert "volume" not in scaled
    with pytest.raises(ValueError, match="strictly positive"):
        vbr.derive_qfq_from_raw(raw, factor=0.0)
    with pytest.raises(ValueError, match="strictly positive"):
        vbr.derive_qfq_from_raw(raw, factor=-1.0)
    with pytest.raises(ValueError, match="missing price columns"):
        vbr.derive_qfq_from_raw({"open": 1.0}, factor=1.0)


def test_g4_provenance_is_complete_and_hash_is_content_sensitive() -> None:
    rows = vbr.provenance_rows(
        planned_rows=[_bar("600001")],
        price_series_mode="raw",
        provenance=PROV,
        created_at="2026-09-24T00:00:00+00:00",
    )
    assert len(rows) == 1
    record = rows.iloc[0].to_dict()
    for field in (
        "repair_batch_id",
        "repair_source",
        "source_file",
        "source_query_time",
        "source_row_hash",
        "stored_row_hash",
        "vendor_original_missing",
        "repair_reason",
        "verified_by",
    ):
        assert field in record, field
    assert record["vendor_original_missing"] is True
    assert record["repair_reason"] == vbr.REPAIR_REASON_SOURCE_GAP
    same = vbr.row_content_hash({"close": 1.0, "volume": 2.0})
    assert same == vbr.row_content_hash({"volume": 2.0, "close": 1.0})
    assert same != vbr.row_content_hash({"close": 1.0, "volume": 2.0000001})

    # 内容哈希不能依赖**取值路径**：计划侧是 ISO 字符串，回读侧可能是 Timestamp。
    # 这一条不是理论洁癖——首版 verify_repairs 就因为没归一化日期，把每一行都报成漂移。
    from datetime import date, datetime

    stamp = pd.Timestamp("2025-11-17")
    variants = [
        {"symbol": "600001", "date": "2025-11-17", "close": 1.0},
        {"symbol": "600001", "date": date(2025, 11, 17), "close": 1.0},
        {"symbol": "600001", "date": datetime(2025, 11, 17, 0, 0), "close": 1.0},
        {"symbol": "600001", "date": stamp, "close": 1.0},
    ]
    hashes = {vbr.row_content_hash(v) for v in variants}
    assert len(hashes) == 1


def test_g5_register_is_idempotent_and_refuses_silent_re_repair() -> None:
    con = _warehouse()
    rows = vbr.provenance_rows(
        planned_rows=[_bar("600001"), _bar("600002")],
        price_series_mode="raw",
        provenance=PROV,
    )
    assert vbr.register_provenance(con, rows) == 2
    assert vbr.register_provenance(con, rows) == 0  # 同内容重跑不重复登记
    changed = rows.copy()
    changed.loc[0, "stored_row_hash"] = "tampered"
    with pytest.raises(ValueError, match="already registered"):
        vbr.register_provenance(con, changed)


def test_g6_revert_deletes_only_registered_keys() -> None:
    con = _warehouse()
    planned = _planned(_bar("600001"), _bar("600002"))
    _insert_missing_only(con, planned)
    # 一条 vendor 原始行 + 一条同批次之外的补写行，都不该被这次回滚碰掉。
    con.execute(
        "INSERT INTO daily_bars VALUES ('600000', ?, 1,1,1,1,1,1,1,'main',false,false,"
        " false,'raw','local_vendor_raw')",
        [GAP_DATE],
    )
    other = vbr.plan_insert_missing_only(
        incumbent_keys=set(),
        candidate_rows=[_bar("600003")],
        price_series_mode="raw",
        provenance=vbr.RepairProvenance(
            **{**PROV.as_row_fields(), "repair_batch_id": "rb-other"}
        ),
    )[0]
    _insert_missing_only(con, other)
    rows = vbr.provenance_rows(planned_rows=planned, price_series_mode="raw", provenance=PROV)
    vbr.register_provenance(con, rows)
    assert vbr.revert_repairs(con, batch_id=PROV.repair_batch_id) == 2
    left = sorted(con.execute("SELECT symbol FROM daily_bars").df()["symbol"])
    assert left == ["600000", "600003"]


def test_g7_verify_reports_missing_and_drift() -> None:
    con = _warehouse()
    planned = _planned(_bar("600001"), _bar("600002"))
    rows = vbr.provenance_rows(planned_rows=planned, price_series_mode="raw", provenance=PROV)
    vbr.register_provenance(con, rows)
    result = vbr.verify_repairs(con, batch_id=PROV.repair_batch_id)
    assert result["status"] == "FAIL"  # 登记了但从没写进 bar 表
    assert sorted(result["missing"]) == ["600001@2025-11-17", "600002@2025-11-17"]
    _insert_missing_only(con, planned)
    assert vbr.verify_repairs(con, batch_id=PROV.repair_batch_id)["status"] == "PASS"
    con.execute("UPDATE daily_bars SET close = 3.5 WHERE symbol='600001'")
    drifted = vbr.verify_repairs(con, batch_id=PROV.repair_batch_id)
    assert drifted["status"] == "FAIL"
    assert drifted["drifted"] == ["600001@2025-11-17"]


def test_g8_provenance_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        vbr.validate_provenance(
            vbr.RepairProvenance(
                repair_batch_id="",
                repair_source="x",
                repair_reason=vbr.REPAIR_REASON_SOURCE_GAP,
                verified_by="y",
            )
        )
    with pytest.raises(ValueError, match="unknown repair_reason"):
        vbr.validate_provenance(
            vbr.RepairProvenance(
                repair_batch_id="b",
                repair_source="x",
                repair_reason="MAKE_IT_PASS",
                verified_by="y",
            )
        )
