"""M4-H Historical Data Inventory（只读盘点）。

本脚本是 M4-H 阶段 0 的证据来源：对本地权威历史数据库做**只读**盘点，
产出可复现的 JSON 事实清单，供 Historical Locked OOS 的区间分级与 fold 规划使用。

设计约束：
- 只读打开 DuckDB（``read_only=True``），不做任何写入、不改动源库。
- 所有"事实"都必须由本脚本实测得出，禁止从文档转述中抄写数字。
- 每个统计项都记录其 SQL 口径，便于第三方复算。

用法::

    python scripts/alpha_v2_m4h_inventory.py \
        --market-db artifacts/warehouse/market.duckdb \
        --out artifacts/alpha_v2/m4h/historical_data_inventory.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

SCHEMA_ID = "alpha_v2_m4h_historical_data_inventory.v1"

# 用户协议要求必须单列的发展污染窗口（项目已反复用于开发/选择）。
DEVELOPMENT_CONTAMINATED_RANGE = ("2025-06-02", "2026-03-31")

# 关键列：逐一按年统计非空覆盖率（存在性由 DESCRIBE 决定）。
KEY_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "turnover",
    "float_market_cap",
    "up_limit",
    "down_limit",
    "suspended",
    "board",
    "is_st",
    "is_delisting_risk",
    "adj_factor",
    "price_series_mode",
    "financial_report_date",
)

# 与 raw 可成交价直接相关的列（执行契约依赖）。
EXECUTION_RELEVANT_COLUMNS: tuple[str, ...] = (
    "open",
    "close",
    "up_limit",
    "down_limit",
    "suspended",
    "pre_close",
)


def _sha256_file(path: Path, *, chunk: int = 1 << 22) -> str:
    """对数据库文件做完整性指纹（证据可追溯：盘点针对的是哪个物理文件）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    return None if row is None else row[0]


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[Any, ...]]:
    return con.execute(sql).fetchall()


def _table_names(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [str(row[0]) for row in _rows(con, "SHOW TABLES")]


def _columns_of(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    described = con.execute(f"DESCRIBE {table}").fetchall()
    return {str(name): str(dtype) for name, dtype, *_ in described}


def _inventory_tables(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """每张表的行数与可用日期范围（凡有 date/trade_date/end_date 列都探测）。"""
    out: dict[str, Any] = {}
    for table in _table_names(con):
        cols = _columns_of(con, table)
        entry: dict[str, Any] = {
            "columns": cols,
            "column_count": len(cols),
            "row_count": int(_one(con, f"SELECT count(*) FROM {table}") or 0),
        }
        for date_col in ("date", "trade_date", "end_date", "ann_date", "as_of"):
            if date_col in cols:
                span = con.execute(
                    f"SELECT min({date_col}), max({date_col}), "
                    f"count(DISTINCT {date_col}) FROM {table}"
                ).fetchone()
                entry["date_column"] = date_col
                entry["date_min"] = str(span[0]) if span and span[0] is not None else None
                entry["date_max"] = str(span[1]) if span and span[1] is not None else None
                entry["distinct_dates"] = int(span[2] or 0) if span else 0
                break
        if "symbol" in cols:
            entry["distinct_symbols"] = int(
                _one(con, f"SELECT count(DISTINCT symbol) FROM {table}") or 0
            )
        out[table] = entry
    return out


def _daily_bars_span(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    row = con.execute(
        "SELECT min(date), max(date), count(DISTINCT date), count(DISTINCT symbol), count(*) "
        "FROM daily_bars"
    ).fetchone()
    assert row is not None
    return {
        "earliest_trade_date": str(row[0]),
        "latest_trade_date": str(row[1]),
        "trading_days": int(row[2]),
        "symbols": int(row[3]),
        "daily_bars": int(row[4]),
        "sql": "SELECT min(date), max(date), count(DISTINCT date), count(DISTINCT symbol), "
        "count(*) FROM daily_bars",
    }


def _duplicate_logical_keys(con: duckdb.DuckDBPyConnection) -> int:
    sql = (
        "SELECT count(*) FROM (SELECT symbol, date, count(*) AS c FROM daily_bars "
        "GROUP BY 1, 2 HAVING c > 1)"
    )
    return int(_one(con, sql) or 0)


def _coverage_by_year(
    con: duckdb.DuckDBPyConnection, table: str, columns: Mapping[str, str]
) -> list[dict[str, Any]]:
    """按自然年统计行数、标的数、交易日数，以及关键列的非空计数。"""
    present = [col for col in KEY_COLUMNS if col in columns]
    selects = ["year(date) AS y", "count(*) AS rows", "count(DISTINCT symbol) AS symbols",
               "count(DISTINCT date) AS days", "min(date) AS dmin", "max(date) AS dmax"]
    for col in present:
        selects.append(f'count("{col}") AS nn_{col}')
    sql = f"SELECT {', '.join(selects)} FROM {table} GROUP BY 1 ORDER BY 1"
    cursor = con.execute(sql)
    names = [desc[0] for desc in cursor.description]
    out: list[dict[str, Any]] = []
    for raw in cursor.fetchall():
        row = dict(zip(names, raw, strict=True))
        record: dict[str, Any] = {
            "year": int(row["y"]),
            "rows": int(row["rows"]),
            "symbols": int(row["symbols"]),
            "trading_days": int(row["days"]),
            "date_min": str(row["dmin"]),
            "date_max": str(row["dmax"]),
            "non_null": {col: int(row[f"nn_{col}"]) for col in present},
        }
        out.append(record)
    return out


def _survivorship_evidence(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """幸存者偏差取证：数据里是否存在"已停止出现"的标的（退市/长期停牌）。"""
    latest = str(_one(con, "SELECT max(date) FROM daily_bars"))
    queries = {
        "symbols_with_last_bar_before_2025_01_01": (
            "SELECT count(*) FROM (SELECT symbol, max(date) AS ld FROM daily_bars "
            "GROUP BY 1 HAVING max(date) < DATE '2025-01-01')"
        ),
        "symbols_with_last_bar_before_2026_01_01": (
            "SELECT count(*) FROM (SELECT symbol, max(date) AS ld FROM daily_bars "
            "GROUP BY 1 HAVING max(date) < DATE '2026-01-01')"
        ),
        "symbols_with_last_bar_before_latest_minus_30d": (
            f"SELECT count(*) FROM (SELECT symbol, max(date) AS ld FROM daily_bars "
            f"GROUP BY 1 HAVING max(date) < DATE '{latest}' - INTERVAL 30 DAY)"
        ),
        "active_symbols_on_latest_date": (
            f"SELECT count(DISTINCT symbol) FROM daily_bars WHERE date = DATE '{latest}'"
        ),
        "name_contains_delisting_marker": (
            "SELECT count(DISTINCT symbol) FROM daily_bars WHERE name LIKE '%退%'"
        ),
        "staleness_days_by_symbol_p99": (
            "SELECT quantile_cont(days_since_last, 0.99) FROM ("
            "SELECT symbol, date_diff('day', max(date), DATE '" + latest + "') AS days_since_last "
            "FROM daily_bars GROUP BY 1)"
        ),
    }
    out: dict[str, Any] = {"latest_bar_date": latest}
    for key, sql in queries.items():
        value = _one(con, sql)
        if isinstance(value, float):
            out[key] = float(value)
        else:
            out[key] = int(value) if value is not None else None
        out.setdefault("sql", {})[key] = sql
    for probe in ("600401.SH", "600401", "000418.SZ", "000418", "600806.SH", "600806"):
        out.setdefault("delisted_code_probe", {})[probe] = int(
            _one(con, f"SELECT count(*) FROM daily_bars WHERE symbol = '{probe}'") or 0
        )
    return out


def _unit_regime_probe(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """单位口径断点探测：turnover/volume 的量级在年份之间是否发生跳变。

    若 ``volume`` 以"股"计，则 ``turnover/volume`` 约等于当日均价（几元~几十元）；
    若以"手"计，则该比值约为均价的 100 倍。据此可定位单位切换日。
    """
    sql = (
        "SELECT year(date) AS y, "
        "quantile_cont(turnover / NULLIF(volume, 0), 0.5) AS ratio_median, "
        "quantile_cont(turnover / NULLIF(volume, 0), 0.05) AS ratio_p05, "
        "quantile_cont(turnover / NULLIF(volume, 0), 0.95) AS ratio_p95, "
        "count(*) AS rows "
        "FROM daily_bars WHERE volume IS NOT NULL AND volume > 0 AND turnover IS NOT NULL "
        "GROUP BY 1 ORDER BY 1"
    )
    by_year = [
        {
            "year": int(row[0]),
            "ratio_median": float(row[1]) if row[1] is not None else None,
            "ratio_p05": float(row[2]) if row[2] is not None else None,
            "ratio_p95": float(row[3]) if row[3] is not None else None,
            "rows": int(row[4]),
        }
        for row in _rows(con, sql)
    ]

    # 定位切换日：按日统计"比值 > 100"的行占比，占比从 0 跳到接近 1 的日子即断点。
    lot_like = "avg(CASE WHEN turnover / NULLIF(volume, 0) > 100 THEN 1.0 ELSE 0.0 END)"
    daily_sql = (
        "SELECT date, "
        f"{lot_like} AS share_lot_like, "
        "count(*) AS rows "
        "FROM daily_bars WHERE volume IS NOT NULL AND volume > 0 AND turnover IS NOT NULL "
        "AND date >= DATE '2025-08-01' "
        f"GROUP BY 1 ORDER BY abs({lot_like} - 0.5) DESC LIMIT 12"
    )
    transition = [
        {"date": str(row[0]), "share_lot_like": round(float(row[1]), 4), "rows": int(row[2])}
        for row in _rows(con, daily_sql)
    ]
    return {"ratio_by_year": by_year, "transition_candidates": transition, "sql": sql}


def _index_benchmark_coverage(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    if "index_daily" not in _table_names(con):
        return {"available": False}
    cols = _columns_of(con, "index_daily")
    date_col = "trade_date" if "trade_date" in cols else "date"
    codes = [
        {
            "index_code": str(row[0]),
            "rows": int(row[1]),
            "date_min": str(row[2]),
            "date_max": str(row[3]),
        }
        for row in _rows(
            con,
            f"SELECT index_code, count(*), min({date_col}), max({date_col}) "
            f"FROM index_daily GROUP BY 1 ORDER BY 2 DESC",
        )
    ]
    return {"available": True, "date_column": date_col, "codes": codes}


def _financial_source_mix(
    con: duckdb.DuckDBPyConnection, columns: Mapping[str, str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "financial_source" in columns:
        out["financial_source"] = {
            str(row[0]): int(row[1])
            for row in _rows(
                con,
                "SELECT coalesce(financial_source, '<null>'), count(*) FROM daily_bars "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
            )
        }
    if "financial_report_date" in columns:
        out["financial_report_date_top"] = {
            str(row[0]): int(row[1])
            for row in _rows(
                con,
                "SELECT coalesce(financial_report_date, '<null>'), count(*) FROM daily_bars "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
            )
        }
    if "background_data_source" in columns:
        out["background_data_source"] = {
            str(row[0]): int(row[1])
            for row in _rows(
                con,
                "SELECT coalesce(background_data_source, '<null>'), count(*) FROM daily_bars "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
            )
        }
    return out


def _history_window_after(con: duckdb.DuckDBPyConnection, start: str) -> dict[str, Any]:
    """给定起始日后，历史实际的交易日在数量与分布（fold 规划的输入）。"""
    row = con.execute(
        "SELECT count(DISTINCT date), min(date), max(date), "
        "count(DISTINCT symbol) FROM daily_bars WHERE date >= ?::DATE",
        [start],
    ).fetchone()
    assert row is not None
    by_year = [
        {"year": int(r[0]), "trading_days": int(r[1]), "avg_symbols_per_day": float(r[2])}
        for r in _rows(
            con,
            "SELECT year(date), count(DISTINCT date), "
            "count(DISTINCT symbol) * 1.0 / count(DISTINCT date) FROM daily_bars "
            f"WHERE date >= DATE '{start}' GROUP BY 1 ORDER BY 1",
        )
    ]
    return {
        "start": start,
        "trading_days": int(row[0]),
        "first_date": str(row[1]),
        "last_date": str(row[2]),
        "symbols_ever": int(row[3]),
        "by_year": by_year,
    }


def build_inventory(market_db: Path, *, probe_start: str = "2016-01-01") -> dict[str, Any]:
    started = time.perf_counter()
    con = duckdb.connect(str(market_db), read_only=True)
    try:
        bars_columns = _columns_of(con, "daily_bars")
        payload: dict[str, Any] = {
            "schema": SCHEMA_ID,
            "generated_at": datetime.now(UTC).isoformat(),
            "source": {
                "market_db": str(market_db),
                "market_db_bytes": market_db.stat().st_size,
                "market_db_sha256": _sha256_file(market_db),
                "access_mode": "read_only",
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "duckdb": duckdb.__version__,
            },
            "development_contaminated_range": list(DEVELOPMENT_CONTAMINATED_RANGE),
            "tables": _inventory_tables(con),
            "daily_bars": {
                "span": _daily_bars_span(con),
                "duplicate_logical_keys": _duplicate_logical_keys(con),
                "execution_relevant_columns_present": [
                    col for col in EXECUTION_RELEVANT_COLUMNS if col in bars_columns
                ],
                "execution_relevant_columns_absent": [
                    col for col in EXECUTION_RELEVANT_COLUMNS if col not in bars_columns
                ],
                "coverage_by_year": _coverage_by_year(con, "daily_bars", bars_columns),
            },
            "survivorship": _survivorship_evidence(con),
            "unit_regime": _unit_regime_probe(con),
            "benchmark": _index_benchmark_coverage(con),
            "financial_source_mix": _financial_source_mix(con, bars_columns),
            "history_window": _history_window_after(con, probe_start),
        }
    finally:
        con.close()
    payload["timing"] = {"inventory_seconds": round(time.perf_counter() - started, 3)}
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M4-H historical data inventory (read-only)")
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
    parser.add_argument("--out", default="artifacts/alpha_v2/m4h/historical_data_inventory.json")
    parser.add_argument("--probe-start", default="2016-01-01")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    market_db = Path(args.market_db)
    if not market_db.is_file():
        print(f"[m4h-inventory] market db not found: {market_db}", file=sys.stderr)
        return 2
    payload = build_inventory(market_db, probe_start=args.probe_start)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    span = payload["daily_bars"]["span"]
    print(
        f"[m4h-inventory] {span['earliest_trade_date']}..{span['latest_trade_date']} "
        f"days={span['trading_days']} symbols={span['symbols']} bars={span['daily_bars']} "
        f"-> {out} ({payload['timing']['inventory_seconds']}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
