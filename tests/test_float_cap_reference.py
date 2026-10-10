from datetime import date

import duckdb
import pandas as pd
import pytest

from stock_analyzer.research.float_cap_reference import TABLE, apply_float_cap_reference

#: 数据供应商取不到流通市值时的占位常量（ADR-004 §3）。
PLACEHOLDER = 12_000_000_000.0


def _ref_db(tmp_path):
    path = tmp_path / "ref.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        f"CREATE TABLE {TABLE} (symbol VARCHAR, trade_date DATE, float_market_cap DOUBLE)"
    )
    con.execute(
        f"INSERT INTO {TABLE} VALUES "
        "('600000', DATE '2026-04-03', 5000000000.0), "
        "('600001', DATE '2026-04-03', 5000000000.0)"
    )
    con.close()
    return path


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["600000", "600001", "600002"],
        "date": [date(2026, 4, 3)] * 3,
        "float_market_cap": [PLACEHOLDER, 3_000_000_000.0, PLACEHOLDER],
    })


def test_placeholder_rows_get_real_caps_and_uncovered_rows_keep_their_value(tmp_path) -> None:
    out, stats = apply_float_cap_reference(_frame(), _ref_db(tmp_path))
    # 600000/600001 都以真值为准（600001 原来那份 3e9 被独立来源覆盖，差异另计），
    # 600002 没有真值——保留原值，不填 0 也不装作补上了。
    assert out["float_market_cap"].tolist() == [5e9, 5e9, PLACEHOLDER]
    assert list(out.columns) == ["symbol", "date", "float_market_cap"]
    assert stats["rows_with_reference"] == 2
    assert stats["rows_replaced_from_placeholder"] == 1
    assert stats["rows_left_placeholder_without_reference"] == 1
    # 门能否判定看占位比例：替换前 2/3、替换后 1/3，这个翻转就是"市值门重新判得动"的形式。
    assert stats["placeholder_share_before"] == pytest.approx(0.666667, abs=1e-5)
    assert stats["gate_non_evaluable_before"] is True
    assert stats["placeholder_share_after"] == pytest.approx(0.333333, abs=1e-5)
    assert stats["gate_non_evaluable_after"] is False


def test_disagreement_between_two_measured_values_is_counted_not_hidden(tmp_path) -> None:
    out, stats = apply_float_cap_reference(_frame(), _ref_db(tmp_path))
    assert out is not None
    # 600001 两边都声称是观测值却差 40%：计数必须可见，否则"补采结果与原库不一致"会静默消失。
    assert stats["rows_both_claim_measured"] == 1
    assert stats["rows_measured_differing_beyond_1pct"] == 1


def test_duplicate_reference_rows_fail_loudly_instead_of_multiplying(tmp_path) -> None:
    path = tmp_path / "dup.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        f"CREATE TABLE {TABLE} (symbol VARCHAR, trade_date DATE, float_market_cap DOUBLE)"
    )
    con.execute(
        f"INSERT INTO {TABLE} VALUES "
        "('600000', DATE '2026-04-03', 5000000000.0), "
        "('600000', DATE '2026-04-03', 6000000000.0)"
    )
    con.close()
    with pytest.raises(SystemExit, match="not unique"):
        apply_float_cap_reference(_frame(), path)
