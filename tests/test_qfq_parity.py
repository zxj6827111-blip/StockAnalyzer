"""P3.3.1 —— QFQ 对账判据的回归测试（任务书 §9 Test 1..6）。

夹具是两份临时 duckdb delta 库（raw / qfq），逐键插入 bar；因子可用性用注入的
``has_factor`` 判定函数模拟，因此这组测试测的是**判据本身**，不依赖 vendor ZIP 格式。
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable
from pathlib import Path

import duckdb
import pytest

from stock_analyzer.data import qfq_parity as qp

DDL = (
    "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE,"
    " low DOUBLE, close DOUBLE, price_series_mode VARCHAR)"
)


def _library(path: Path, rows: Iterable[tuple[str, str]], mode: str) -> str:
    con = duckdb.connect(str(path))
    con.execute(DDL)
    for symbol, day in rows:
        con.execute(
            "INSERT INTO daily_bars VALUES (?, ?, 1, 1, 1, 1, ?)", [symbol, day, mode]
        )
    con.close()
    return str(path)


def _assess(
    tmp_path: Path,
    raw_rows: Iterable[tuple[str, str]],
    qfq_rows: Iterable[tuple[str, str]],
    *,
    has_factor: object | None = None,
):
    raw_db = _library(tmp_path / "raw.duckdb", raw_rows, "raw")
    qfq_db = _library(tmp_path / "qfq.duckdb", qfq_rows, "qfq")
    return qp.assess_qfq_parity(
        raw_db=raw_db, qfq_db=qfq_db, window=["2026-07-01", "2026-07-31"],
        has_factor=has_factor,  # type: ignore[arg-type]
    )


def test_t1_paired_rows_pass(tmp_path: Path) -> None:
    rows = [("000001", "2026-07-20"), ("600000", "2026-07-20")]
    parity = _assess(tmp_path, rows, rows)
    assert parity.ok is True
    assert parity.reason == ""
    assert parity.raw_rows == 2 and parity.qfq_rows == 2
    assert parity.derivation_gap_count == 0
    assert parity.to_payload()["ok"] is True


def test_t2_both_sides_absent_is_not_reported(tmp_path: Path) -> None:
    """两侧同缺不在本模块职责内（可能是停牌 / 换号 / 对称断供，原理上不可分）。"""
    parity = _assess(tmp_path, [("000001", "2026-07-20")], [("000001", "2026-07-20")])
    assert parity.ok is True
    assert parity.raw_present_qfq_missing == 0
    assert parity.qfq_present_raw_missing == 0


def test_t3_raw_and_factor_present_but_qfq_missing_is_derivation_gap(
    tmp_path: Path,
) -> None:
    parity = _assess(
        tmp_path,
        [("000001", "2026-07-20"), ("000001", "2026-07-21")],
        [("000001", "2026-07-21")],
        has_factor=lambda symbol, day: True,
    )
    assert parity.ok is False
    assert parity.reason == qp.REASON_DERIVATION_GAP
    assert parity.derivation_gap_count == 1
    assert parity.factor_missing_for_raw == 0
    assert parity.keys == [("000001", "2026-07-20")]
    payload = parity.to_payload()
    assert payload["ok"] is False and payload["reason"] == qp.REASON_DERIVATION_GAP


def test_t4_missing_factor_is_classified_separately(tmp_path: Path) -> None:
    """因子确实取不到时是 QFQ_FACTOR_MISSING，**不许**混进"正常停牌"或不报。"""
    parity = _assess(
        tmp_path,
        [("000001", "2026-07-20")],
        [],
        has_factor=lambda symbol, day: False,
    )
    assert parity.ok is False
    assert parity.reason == qp.REASON_FACTOR_MISSING
    assert parity.factor_missing_for_raw == 1
    assert parity.derivation_gap_count == 0


def test_t5_qfq_row_without_raw_is_a_structural_defect(tmp_path: Path) -> None:
    parity = _assess(tmp_path, [], [("600000", "2026-07-20")], has_factor=None)
    assert parity.ok is False
    assert parity.qfq_present_raw_missing == 1
    assert parity.reasons[qp.REASON_ROW_WITHOUT_RAW] == ["600000@2026-07-20"]


def test_t6_counts_are_exact_over_symbols_and_dates(tmp_path: Path) -> None:
    """任务书 Test 6：3 票 × 2 日，其中 5 个键缺 QFQ → ``derivation_gap_count == 5``。"""
    raw = [(symbol, day) for symbol in ("000001", "000002", "000003") for day in
           ("2026-07-20", "2026-07-21")]
    qfq = [("000001", "2026-07-20")]
    parity = _assess(tmp_path, raw, qfq, has_factor=lambda symbol, day: True)
    assert parity.derivation_gap_count == 5
    assert parity.raw_present_qfq_missing == 5
    assert parity.raw_rows == 6 and parity.qfq_rows == 1
    assert parity.affected_dates == ["2026-07-20", "2026-07-21"]
    assert parity.top_affected_dates == {"2026-07-20": 2, "2026-07-21": 3}
    assert parity.affected_symbols == ["000001", "000002", "000003"]
    assert len(parity.classified) == 5


def test_t7_factor_availability_respects_piecewise_factors() -> None:
    """因子是分段常数：晚于该日的因子**不能**覆盖该日，早于的才能 ffill 上来。"""
    has = qp.factor_availability_from_series({"000001": ["2026-07-10", "2026-08-05"]})
    assert has("000001", "2026-07-20") is True
    assert has("000001", "2026-07-01") is False  # 整段因子都晚于这一天
    assert has("999999", "2026-07-20") is False  # 根本没有这只票的因子
    empty = qp.factor_availability_from_series({"000001": []})
    assert empty("000001", "2026-07-20") is False


def test_t8_frame_of_is_key_level_for_manifest_reconciliation(tmp_path: Path) -> None:
    parity = _assess(
        tmp_path,
        [("000001", "2026-07-20"), ("600000", "2026-07-20")],
        [],
        has_factor=lambda symbol, day: symbol == "000001",
    )
    frame = qp.frame_of(parity)
    assert list(frame.columns) == ["symbol", "trade_date", "reason"]
    assert sorted(frame["symbol"]) == ["000001", "600000"]
    assert set(frame["reason"]) == {qp.REASON_DERIVATION_GAP, qp.REASON_FACTOR_MISSING}


def test_t9_window_shape_is_validated(tmp_path: Path) -> None:
    raw_db = _library(tmp_path / "raw.duckdb", [], "raw")
    qfq_db = _library(tmp_path / "qfq.duckdb", [], "qfq")
    with pytest.raises(ValueError, match="window"):
        qp.assess_qfq_parity(raw_db=raw_db, qfq_db=qfq_db, window=["2026-07-01"])


def test_t10_read_only_is_enforced_on_the_source_libraries(tmp_path: Path) -> None:
    """对账器绝不能顺手把生产库改了——它只以 READ_ONLY ATTACH 打开。"""
    raw_db = _library(tmp_path / "raw.duckdb", [("000001", "2026-07-20")], "raw")
    qfq_db = _library(tmp_path / "qfq.duckdb", [], "qfq")
    before = (tmp_path / "raw.duckdb").read_bytes()
    qp.assess_qfq_parity(
        raw_db=raw_db, qfq_db=qfq_db, window=["2026-07-01", "2026-07-31"], has_factor=None
    )
    assert (tmp_path / "raw.duckdb").read_bytes() == before
    with duckdb.connect(qfq_db, read_only=True) as con:
        with pytest.raises(duckdb.Error):
            con.execute("INSERT INTO daily_bars VALUES ('x','2026-07-20',1,1,1,1,'qfq')")


FACTOR_CSV = "股票代码,交易日期,复权因子\n{code},20260720,1.0\n{code},20260721,1.0\n"


def _factor_zip(tmp_path: Path, codes: list[str]) -> Path:
    root = tmp_path / "vendor"
    (root / "复权因子").mkdir(parents=True, exist_ok=True)
    archive = root / "复权因子" / "复权因子_前复权.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for code in codes:
            if code == "555555":
                zf.writestr(f"2026/{code}.SZ.csv", "坏表头,没有因子列\nx,y\n")
                continue
            zf.writestr(f"2026/{code}.SZ.csv", FACTOR_CSV.format(code=f"{code}.SZ"))
    return root


def test_factor_date_index_parses_only_requested_symbols(tmp_path: Path) -> None:
    """成本闸：全量解析 5,837 只票实测 279 秒，健康夜扫描不该付这份钱。"""
    root = _factor_zip(tmp_path, ["000001", "600000", "555555"])
    full = qp.factor_date_index(root)
    assert full["000001"] == ["2026-07-20", "2026-07-21"]
    assert "555555" not in full, "解析不出可用因子的票必须按'没有因子'处理"
    picked = qp.factor_date_index(root, symbols=["600000"])
    assert set(picked) == {"600000"}
    assert qp.factor_date_index(root, symbols=["000001", "555555"]) == {
        "000001": ["2026-07-20", "2026-07-21"]
    }


def test_asymmetry_keys_prepasses_without_the_factor_archive(tmp_path: Path) -> None:
    """有差异时先拿到键集，再决定要不要为归因付解析成本（§4 的覆盖集合口径）。"""
    raw_db = _library(
        tmp_path / "raw2.duckdb",
        [("000001", "2026-07-20"), ("000001", "2026-07-21")],
        "raw",
    )
    qfq_db = _library(tmp_path / "qfq2.duckdb", [("000001", "2026-07-21")], "qfq")
    raw_only, qfq_only = qp.asymmetry_keys(
        raw_db=raw_db, qfq_db=qfq_db, window=["2026-07-01", "2026-07-31"]
    )
    assert raw_only == {("000001", "2026-07-20")}
    assert qfq_only == set()
    with pytest.raises(ValueError, match="window"):
        qp.asymmetry_keys(raw_db=raw_db, qfq_db=qfq_db, window=["2026-07-01"])
