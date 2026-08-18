"""研究数据覆盖审计工具。

以只读方式打开 DuckDB 仓库，对研究流程依赖的核心数据表做覆盖度、
完整性与一致性审计，并按研究门禁 (research gates) 给出 GO / NO-GO 判定。

设计原则：
- 只读打开 (read_only=True)，绝不创建表、不修改 schema、不更新 mtime；
- 不依赖 MarketWarehouse（避免触发 ensure_schema 等写操作）；
- 直接使用 duckdb.connect 进行查询；
- 输出 JSON 与 Markdown 两种格式。

退出码：
- 0 = GO（所有研究门禁通过）
- 1 = NO-GO（存在任一门禁未通过）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 项目根与 src 路径，便于在直接执行脚本时复用项目内模块（此处未使用
# MarketWarehouse，但保持路径注入以与其它脚本约定一致）。
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import duckdb  # noqa: E402  (需在 sys.path 注入后导入)

# ----------------------------------------------------------------------------
# 研究门禁默认阈值
# ----------------------------------------------------------------------------
GATES = {
    "exact_limit_label_coverage": 0.98,  # daily_trade_status symbols / daily_bars symbols
    "moneyflow_active_coverage": 0.90,  # moneyflow symbols / daily_bars symbols
    "price_series_mode_consistency": 1.0,  # 要求 mixed_symbols == 0
    "symbol_identity_mapping_coverage": 0.995,  # identity_mapping canonical / daily_bars symbols
    "duplicate_natural_keys": 0,  # 自然键重复数必须为 0
    "min_history_depth_days": 250,  # 最新交易日股票中历史深度达标占比阈值见下
}

# 历史深度阈值（交易日），用于统计覆盖的 symbol 数量
HISTORY_DEPTH_THRESHOLDS = (20, 60, 120, 250)


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------


def _table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    """通过 information_schema 判断表是否存在，避免触发任何写操作。"""
    row = con.execute(
        """
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0] > 0)


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> Any:
    """执行查询并返回首行首列值，空结果返回 None。"""
    row = con.execute(sql, params or []).fetchone()
    if row is None:
        return None
    return row[0]


def _query_all(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | None = None,
) -> list[Any]:
    """执行查询并返回全部行（列表）。"""
    return con.execute(sql, params or []).fetchall()


def _safe_col_index(con: duckdb.DuckDBPyConnection, table: str, column: str) -> bool:
    """判断表中是否存在某列，避免对缺失列执行查询报错。"""
    row = con.execute(
        """
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = ? AND column_name = ?
        """,
        [table, column],
    ).fetchone()
    return bool(row and row[0] > 0)


def _rows_to_dicts(rows: list[Any], columns: list[str]) -> list[dict[str, Any]]:
    """将 fetchall 的行列表转换为字典列表。"""
    return [dict(zip(columns, row, strict=False)) for row in rows]


# ----------------------------------------------------------------------------
# 各审计模块
# ----------------------------------------------------------------------------


def audit_daily_bars(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """审计 daily_bars 表：行数、股票覆盖、历史深度、重复键、空值与价格异常。"""
    result: dict[str, Any] = {
        "table_exists": False,
        "rows": 0,
        "symbols": 0,
        "trade_date_count": 0,
        "min_date": None,
        "max_date": None,
        "latest_trade_date_symbols": 0,
        "history_depth": {f"ge_{t}_days": 0 for t in HISTORY_DEPTH_THRESHOLDS},
        "duplicate_symbol_date": [],
        "ohlcv_null_rows": 0,
        "nonpositive_close_rows": 0,
        "ohlc_relation_anomaly_rows": 0,
    }
    if not _table_exists(con, "daily_bars"):
        return result
    result["table_exists"] = True

    # 总行数与去重股票数
    result["rows"], result["symbols"] = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM daily_bars"
    ).fetchone()

    # 日期范围
    min_date, max_date = con.execute("SELECT MIN(date), MAX(date) FROM daily_bars").fetchone()
    result["min_date"] = str(min_date) if min_date is not None else None
    result["max_date"] = str(max_date) if max_date is not None else None

    # 交易日数（用于历史覆盖率门禁的分母）
    result["trade_date_count"] = int(
        _scalar(con, "SELECT COUNT(DISTINCT date) FROM daily_bars") or 0
    )

    # 最新交易日股票覆盖
    latest_symbols = _scalar(
        con,
        """
        SELECT COUNT(DISTINCT symbol) FROM daily_bars
        WHERE date = (SELECT MAX(date) FROM daily_bars)
        """,
    )
    result["latest_trade_date_symbols"] = int(latest_symbols or 0)

    # 各 symbol 历史深度统计：对每只股票 COUNT(*)，再统计 >= 各阈值的股票数
    depth_rows = _query_all(
        con,
        """
        SELECT db.symbol, COUNT(*) AS cnt
        FROM daily_bars db
        INNER JOIN (
            SELECT DISTINCT symbol
            FROM daily_bars
            WHERE date = (SELECT MAX(date) FROM daily_bars)
        ) active ON active.symbol = db.symbol
        GROUP BY db.symbol
        """,
    )
    counts = [r[1] for r in depth_rows]
    for threshold in HISTORY_DEPTH_THRESHOLDS:
        result["history_depth"][f"ge_{threshold}_days"] = sum(1 for c in counts if c >= threshold)

    # 重复 symbol/date 自然键
    dup_rows = _query_all(
        con,
        """
        SELECT symbol, date, COUNT(*) AS cnt
        FROM daily_bars
        GROUP BY symbol, date
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        LIMIT 100
        """,
    )
    result["duplicate_symbol_date"] = _rows_to_dicts(dup_rows, ["symbol", "date", "cnt"])

    # OHLCV 空值
    result["ohlcv_null_rows"] = int(
        _scalar(
            con,
            """
        SELECT COUNT(*) FROM daily_bars
        WHERE open IS NULL OR high IS NULL OR low IS NULL
           OR close IS NULL OR volume IS NULL
        """,
        )
        or 0
    )

    # 异常价格：close <= 0
    result["nonpositive_close_rows"] = int(
        _scalar(
            con,
            "SELECT COUNT(*) FROM daily_bars WHERE close IS NOT NULL AND close <= 0",
        )
        or 0
    )

    # OHLC 关系异常：high < low, open > high, open < low, close > high, close < low
    result["ohlc_relation_anomaly_rows"] = int(
        _scalar(
            con,
            """
        SELECT COUNT(*) FROM daily_bars
        WHERE high IS NOT NULL AND low IS NOT NULL
          AND (
            high < low
            OR (open IS NOT NULL AND (open > high OR open < low))
            OR (close IS NOT NULL AND (close > high OR close < low))
          )
        """,
        )
        or 0
    )

    return result


def audit_price_series(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """审计 daily_bars.price_series_mode：qfq/raw 行数、mixed symbols、切换日期。"""
    result: dict[str, Any] = {
        "table_exists": False,
        "has_column": False,
        "qfq_rows": 0,
        "raw_rows": 0,
        "mixed_symbols": 0,
        "mode_switch_points": [],
    }
    if not _table_exists(con, "daily_bars"):
        return result
    result["table_exists"] = True

    has_col = _safe_col_index(con, "daily_bars", "price_series_mode")
    result["has_column"] = has_col
    if not has_col:
        return result

    # qfq / raw 行数
    qfq_rows = _scalar(
        con,
        "SELECT COUNT(*) FROM daily_bars WHERE price_series_mode = 'qfq'",
    )
    raw_rows = _scalar(
        con,
        "SELECT COUNT(*) FROM daily_bars WHERE price_series_mode = 'raw'",
    )
    result["qfq_rows"] = int(qfq_rows or 0)
    result["raw_rows"] = int(raw_rows or 0)

    # mixed symbols：同一 symbol 出现多于一种 mode
    mixed_rows = _query_all(
        con,
        """
        SELECT symbol FROM (
            SELECT symbol, COUNT(DISTINCT price_series_mode) AS mode_cnt
            FROM daily_bars
            WHERE price_series_mode IS NOT NULL
            GROUP BY symbol
        ) WHERE mode_cnt > 1
        """,
    )
    mixed_symbols = [r[0] for r in mixed_rows]
    result["mixed_symbols"] = len(mixed_symbols)

    # 每个 mixed symbol 的 mode 切换点：按 date 排序，mode 变化的日期
    switch_points: list[dict[str, Any]] = []
    for symbol in mixed_symbols[:50]:  # 限制样本量避免大表全扫
        rows = _query_all(
            con,
            """
            SELECT date, price_series_mode FROM daily_bars
            WHERE symbol = ? AND price_series_mode IS NOT NULL
            ORDER BY date
            """,
            [symbol],
        )
        prev_mode: str | None = None
        for date_val, mode_val in rows:
            if prev_mode is not None and mode_val != prev_mode:
                switch_points.append(
                    {
                        "symbol": symbol,
                        "date": str(date_val) if date_val is not None else None,
                        "from": prev_mode,
                        "to": mode_val,
                    }
                )
            prev_mode = mode_val
    result["mode_switch_points"] = switch_points

    return result


def audit_trade_status(
    con: duckdb.DuckDBPyConnection,
    daily_bars_symbols: int,
    daily_bars_latest_date: str | None = None,
) -> dict[str, Any]:
    """审计 daily_trade_status：行数、股票数、覆盖率、最新标签日期、coverage_complete。

    覆盖率优先用"最新交易日有标签的股票数 / 最新交易日活跃股票数"，
    而非简单的 symbols / daily_bars_symbols——后者允许每只股票只有
    一天数据也达到 98%。
    """
    result: dict[str, Any] = {
        "table_exists": False,
        "rows": 0,
        "symbols": 0,
        "trade_date_count": 0,
        "symbol_ratio": None,
        "latest_label_date": None,
        "coverage_complete_rows": 0,
        "latest_trade_date_active_symbols": 0,
        "latest_trade_date_labeled_symbols": 0,
        "symbol_date_coverage_ratio": None,
        "min_daily_coverage_ratio": None,
    }
    if not _table_exists(con, "daily_trade_status"):
        return result
    result["table_exists"] = True

    rows, symbols = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM daily_trade_status"
    ).fetchone()
    result["rows"] = int(rows or 0)
    result["symbols"] = int(symbols or 0)

    if daily_bars_symbols > 0:
        result["symbol_ratio"] = round(result["symbols"] / daily_bars_symbols, 6)

    # 有标签的交易日数
    result["trade_date_count"] = int(
        _scalar(con, "SELECT COUNT(DISTINCT trade_date) FROM daily_trade_status") or 0
    )

    latest = _scalar(con, "SELECT MAX(trade_date) FROM daily_trade_status")
    result["latest_label_date"] = str(latest) if latest is not None else None

    if _safe_col_index(con, "daily_trade_status", "coverage_complete"):
        result["coverage_complete_rows"] = int(
            _scalar(
                con,
                "SELECT COUNT(*) FROM daily_trade_status WHERE coverage_complete = TRUE",
            )
            or 0
        )

    if not _table_exists(con, "daily_bars"):
        return result

    # symbol-date 交集覆盖率：有标签的 (symbol, trade_date) 数 /
    # daily_bars 中活跃的 (symbol, date) 数——按日期求交集而非仅比较
    # 各自的 DISTINCT date 数量，避免日期错位和每天仅 1 只覆盖也通过。
    coverage_row = _query_all(
        con,
        """
        SELECT
            COUNT(*) AS matched_pairs,
            (SELECT COUNT(*) FROM (
                SELECT DISTINCT symbol, date FROM daily_bars
            )) AS total_active_pairs
        FROM (
            SELECT DISTINCT ts.symbol, ts.trade_date
            FROM daily_trade_status ts
            INNER JOIN daily_bars db
              ON db.symbol = ts.symbol AND db.date = ts.trade_date
            WHERE ts.up_limit IS NOT NULL
        )
        """,
    )
    if coverage_row:
        matched = int(coverage_row[0][0] or 0)
        total = int(coverage_row[0][1] or 0)
        if total > 0:
            result["symbol_date_coverage_ratio"] = round(matched / total, 6)
            # 逐日最低覆盖率
            min_ratio_row = _query_all(
                con,
                """
                WITH daily_counts AS (
                    SELECT
                        db.date AS d,
                        COUNT(DISTINCT db.symbol) AS active,
                        COUNT(DISTINCT ts.symbol) AS labeled
                    FROM daily_bars db
                    LEFT JOIN daily_trade_status ts
                      ON ts.symbol = db.symbol AND ts.trade_date = db.date
                     AND ts.up_limit IS NOT NULL
                    GROUP BY db.date
                )
                SELECT MIN(CAST(labeled AS DOUBLE) / NULLIF(active, 0))
                FROM daily_counts
                WHERE active > 0
                """,
            )
            if min_ratio_row and min_ratio_row[0][0] is not None:
                result["min_daily_coverage_ratio"] = round(float(min_ratio_row[0][0]), 6)

    # 最新交易日的活跃股票数和有标签的股票数
    if daily_bars_latest_date:
        active = _scalar(
            con,
            "SELECT COUNT(DISTINCT symbol) FROM daily_bars WHERE date = ?",
            [daily_bars_latest_date],
        )
        result["latest_trade_date_active_symbols"] = int(active or 0)
        labeled = _scalar(
            con,
            "SELECT COUNT(DISTINCT symbol) FROM daily_trade_status "
            "WHERE trade_date = ? AND up_limit IS NOT NULL",
            [daily_bars_latest_date],
        )
        result["latest_trade_date_labeled_symbols"] = int(labeled or 0)

    return result


def audit_moneyflow(
    con: duckdb.DuckDBPyConnection,
    daily_bars_symbols: int,
    daily_bars_latest_date: str | None = None,
) -> dict[str, Any]:
    """审计 moneyflow：行数、覆盖率、历史深度分布、大单/超大单字段覆盖。

    覆盖率优先用"最新交易日有 moneyflow 的股票数 / 最新交易日活跃股票数"，
    而非简单的 symbols / daily_bars_symbols。
    """
    result: dict[str, Any] = {
        "table_exists": False,
        "rows": 0,
        "symbols": 0,
        "trade_date_count": 0,
        "symbol_ratio": None,
        "depth_min": 0,
        "depth_median": 0,
        "depth_max": 0,
        "latest_date": None,
        "latest_trade_date_active_symbols": 0,
        "latest_trade_date_covered_symbols": 0,
        "symbol_date_coverage_ratio": None,
        "min_daily_coverage_ratio": None,
        "large_order_coverage": {
            "buy_lg_amount": 0,
            "sell_lg_amount": 0,
            "buy_elg_amount": 0,
            "sell_elg_amount": 0,
        },
    }
    if not _table_exists(con, "moneyflow"):
        return result
    result["table_exists"] = True

    rows, symbols = con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM moneyflow").fetchone()
    result["rows"] = int(rows or 0)
    result["symbols"] = int(symbols or 0)

    if daily_bars_symbols > 0:
        result["symbol_ratio"] = round(result["symbols"] / daily_bars_symbols, 6)

    # 有数据的交易日数
    result["trade_date_count"] = int(
        _scalar(con, "SELECT COUNT(DISTINCT trade_date) FROM moneyflow") or 0
    )
    latest = _scalar(con, "SELECT MAX(trade_date) FROM moneyflow")
    result["latest_date"] = str(latest) if latest is not None else None

    if not _table_exists(con, "daily_bars"):
        return result

    # symbol-date 交集覆盖率：有 moneyflow 的 (symbol, trade_date) 数 /
    # daily_bars 中活跃的 (symbol, date) 数——按日期求交集
    coverage_row = _query_all(
        con,
        """
        SELECT
            COUNT(*) AS matched_pairs,
            (SELECT COUNT(*) FROM (
                SELECT DISTINCT symbol, date FROM daily_bars
            )) AS total_active_pairs
        FROM (
            SELECT DISTINCT mf.symbol, mf.trade_date
            FROM moneyflow mf
            INNER JOIN daily_bars db
              ON db.symbol = mf.symbol AND db.date = mf.trade_date
        )
        """,
    )
    if coverage_row:
        matched = int(coverage_row[0][0] or 0)
        total = int(coverage_row[0][1] or 0)
        if total > 0:
            result["symbol_date_coverage_ratio"] = round(matched / total, 6)
            min_ratio_row = _query_all(
                con,
                """
                WITH daily_counts AS (
                    SELECT
                        db.date AS d,
                        COUNT(DISTINCT db.symbol) AS active,
                        COUNT(DISTINCT mf.symbol) AS covered
                    FROM daily_bars db
                    LEFT JOIN moneyflow mf
                      ON mf.symbol = db.symbol AND mf.trade_date = db.date
                    GROUP BY db.date
                )
                SELECT MIN(CAST(covered AS DOUBLE) / NULLIF(active, 0))
                FROM daily_counts
                WHERE active > 0
                """,
            )
            if min_ratio_row and min_ratio_row[0][0] is not None:
                result["min_daily_coverage_ratio"] = round(float(min_ratio_row[0][0]), 6)

    # 每只股票历史深度统计
    depth_rows = _query_all(
        con,
        """
        SELECT symbol, COUNT(*) AS cnt
        FROM moneyflow
        GROUP BY symbol
        """,
    )
    counts = sorted(r[1] for r in depth_rows)
    if counts:
        result["depth_min"] = int(counts[0])
        result["depth_max"] = int(counts[-1])
        n = len(counts)
        # 中位数（偶数取中间两个均值）
        if n % 2 == 1:
            result["depth_median"] = int(counts[n // 2])
        else:
            result["depth_median"] = int((counts[n // 2 - 1] + counts[n // 2]) / 2)

    latest = _scalar(con, "SELECT MAX(trade_date) FROM moneyflow")
    result["latest_date"] = str(latest) if latest is not None else None

    # 大单/超大单字段非空行数
    for field in result["large_order_coverage"]:
        if _safe_col_index(con, "moneyflow", field):
            cnt = _scalar(
                con,
                f"SELECT COUNT(*) FROM moneyflow WHERE {field} IS NOT NULL",
            )
            result["large_order_coverage"][field] = int(cnt or 0)

    # 最新交易日的活跃股票数和有 moneyflow 的股票数
    if daily_bars_latest_date:
        active = _scalar(
            con,
            "SELECT COUNT(DISTINCT symbol) FROM daily_bars WHERE date = ?",
            [daily_bars_latest_date],
        )
        result["latest_trade_date_active_symbols"] = int(active or 0)
        covered = _scalar(
            con,
            "SELECT COUNT(DISTINCT symbol) FROM moneyflow WHERE trade_date = ?",
            [daily_bars_latest_date],
        )
        result["latest_trade_date_covered_symbols"] = int(covered or 0)

    return result


def audit_security_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """审计 security_status：行数与各 status_type 覆盖、coverage_complete。"""
    result: dict[str, Any] = {
        "table_exists": False,
        "rows": 0,
        "status_type_coverage": {},
        "coverage_complete_rows": 0,
    }
    if not _table_exists(con, "security_status"):
        return result
    result["table_exists"] = True

    result["rows"] = int(_scalar(con, "SELECT COUNT(*) FROM security_status") or 0)

    status_rows = _query_all(
        con,
        """
        SELECT COALESCE(status_type, '') AS st, COUNT(*) AS cnt
        FROM security_status
        GROUP BY st
        ORDER BY cnt DESC
        """,
    )
    result["status_type_coverage"] = {st: int(cnt) for st, cnt in status_rows}

    if _safe_col_index(con, "security_status", "coverage_complete"):
        result["coverage_complete_rows"] = int(
            _scalar(
                con,
                "SELECT COUNT(*) FROM security_status WHERE coverage_complete = TRUE",
            )
            or 0
        )

    return result


def audit_financial_snapshots(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """审计 financial_snapshots：行数、股票数、最新报告期、公告日完整性。"""
    result: dict[str, Any] = {
        "table_exists": False,
        "rows": 0,
        "symbols": 0,
        "latest_report_date": None,
        "ann_date_non_null_ratio": None,
    }
    if not _table_exists(con, "financial_snapshots"):
        return result
    result["table_exists"] = True

    rows, symbols = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM financial_snapshots"
    ).fetchone()
    result["rows"] = int(rows or 0)
    result["symbols"] = int(symbols or 0)

    latest = _scalar(con, "SELECT MAX(end_date) FROM financial_snapshots")
    result["latest_report_date"] = str(latest) if latest is not None else None

    # 公告日完整性：ann_date 非空率
    if _safe_col_index(con, "financial_snapshots", "ann_date") and result["rows"] > 0:
        non_null = int(
            _scalar(con, "SELECT COUNT(*) FROM financial_snapshots WHERE ann_date IS NOT NULL") or 0
        )
        result["ann_date_non_null_ratio"] = round(non_null / result["rows"], 6)

    return result


def audit_identity_mapping(
    con: duckdb.DuckDBPyConnection,
    daily_bars_symbols: int,
) -> dict[str, Any]:
    """Audit legacy Beijing mappings at both symbol and PIT symbol-date level."""
    result: dict[str, Any] = {
        "table_exists": False,
        "rows": 0,
        "historical_symbols": 0,
        "canonical_symbols": 0,
        "canonical_ratio": None,
        "legacy_symbols_in_daily_bars": 0,
        "mapped_legacy_symbols": 0,
        "beijing_coverage_ratio": None,
        "legacy_symbol_date_pairs_in_daily_bars": 0,
        "mapped_legacy_symbol_date_pairs": 0,
        "beijing_symbol_date_coverage_ratio": None,
    }
    has_daily_bars = _table_exists(con, "daily_bars")
    if has_daily_bars:
        result["legacy_symbols_in_daily_bars"] = int(
            _scalar(
                con,
                """SELECT COUNT(DISTINCT symbol) FROM daily_bars
            WHERE symbol LIKE '43%' OR symbol LIKE '83%' OR symbol LIKE '87%'""",
            )
            or 0
        )
        result["legacy_symbol_date_pairs_in_daily_bars"] = int(
            _scalar(
                con,
                """SELECT COUNT(*) FROM (
                SELECT DISTINCT symbol, date FROM daily_bars
                WHERE symbol LIKE '43%' OR symbol LIKE '83%' OR symbol LIKE '87%'
            ) legacy_pairs""",
            )
            or 0
        )
    if not _table_exists(con, "security_identity_mapping"):
        result["beijing_coverage_ratio"] = (
            1.0 if result["legacy_symbols_in_daily_bars"] == 0 else 0.0
        )
        result["beijing_symbol_date_coverage_ratio"] = (
            1.0 if result["legacy_symbol_date_pairs_in_daily_bars"] == 0 else 0.0
        )
        return result
    result["table_exists"] = True
    rows, hist, canon = con.execute(
        """SELECT COUNT(*), COUNT(DISTINCT historical_symbol),
                  COUNT(DISTINCT canonical_symbol)
           FROM security_identity_mapping"""
    ).fetchone()
    result["rows"] = int(rows or 0)
    result["historical_symbols"] = int(hist or 0)
    result["canonical_symbols"] = int(canon or 0)
    if daily_bars_symbols > 0:
        result["canonical_ratio"] = round(result["canonical_symbols"] / daily_bars_symbols, 6)
    if not has_daily_bars:
        result["beijing_coverage_ratio"] = 1.0
        result["beijing_symbol_date_coverage_ratio"] = 1.0
        return result
    legacy_symbols = result["legacy_symbols_in_daily_bars"]
    legacy_pairs = result["legacy_symbol_date_pairs_in_daily_bars"]
    mapped_symbols = int(
        _scalar(
            con,
            """SELECT COUNT(DISTINCT db.symbol)
           FROM daily_bars db
           INNER JOIN security_identity_mapping sim
             ON sim.historical_symbol = db.symbol
           WHERE db.symbol LIKE '43%' OR db.symbol LIKE '83%' OR db.symbol LIKE '87%'""",
        )
        or 0
    )
    mapped_pairs = int(
        _scalar(
            con,
            """SELECT COUNT(*) FROM (
               SELECT DISTINCT db.symbol, db.date
               FROM daily_bars db
               INNER JOIN security_identity_mapping sim
                 ON sim.historical_symbol = db.symbol
                AND db.date >= sim.effective_from
                AND (sim.effective_to IS NULL OR db.date <= sim.effective_to)
               WHERE db.symbol LIKE '43%' OR db.symbol LIKE '83%' OR db.symbol LIKE '87%'
           ) mapped_pairs""",
        )
        or 0
    )
    result["mapped_legacy_symbols"] = mapped_symbols
    result["mapped_legacy_symbol_date_pairs"] = mapped_pairs
    result["beijing_coverage_ratio"] = (
        round(mapped_symbols / legacy_symbols, 6) if legacy_symbols else 1.0
    )
    result["beijing_symbol_date_coverage_ratio"] = (
        round(mapped_pairs / legacy_pairs, 6) if legacy_pairs else 1.0
    )
    return result


# ----------------------------------------------------------------------------
# 门禁判定
# ----------------------------------------------------------------------------


def evaluate_gates(
    daily_bars: dict[str, Any],
    price_series: dict[str, Any],
    trade_status: dict[str, Any],
    moneyflow: dict[str, Any],
    identity_mapping: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """根据审计结果计算各研究门禁的实际值与通过状态。"""
    daily_symbols = daily_bars.get("symbols", 0) or 0

    # 1. 精确涨停标签覆盖率：
    #    用 symbol-date 交集覆盖率（有标签的(symbol,date)对 / daily_bars
    #    活跃(symbol,date)对）衡量历史覆盖——避免日期错位和每天仅 1
    #    只覆盖也通过。同时检查逐日最低覆盖率。
    ts_sd_ratio = trade_status.get("symbol_date_coverage_ratio")
    ts_min_daily = trade_status.get("min_daily_coverage_ratio")
    if ts_sd_ratio is not None:
        ts_ratio = ts_sd_ratio
    else:
        # 回退到交易日比例（trade_status 表不存在时）
        ts_ratio = trade_status.get("symbol_ratio")
    ts_pass = (
        ts_ratio is not None
        and ts_ratio >= GATES["exact_limit_label_coverage"]
        and ts_min_daily is not None
        and ts_min_daily >= GATES["exact_limit_label_coverage"]
    )

    # 2. moneyflow 历史覆盖率
    mf_sd_ratio = moneyflow.get("symbol_date_coverage_ratio")
    mf_min_daily = moneyflow.get("min_daily_coverage_ratio")
    if mf_sd_ratio is not None:
        mf_ratio = mf_sd_ratio
    else:
        mf_ratio = moneyflow.get("symbol_ratio")
    mf_pass = (
        mf_ratio is not None
        and mf_ratio >= GATES["moneyflow_active_coverage"]
        and mf_min_daily is not None
        and mf_min_daily >= GATES["moneyflow_active_coverage"]
    )

    # 3. price_series_mode 一致性：
    #    - 列必须存在（has_column=True）
    #    - 全部行必须是 qfq（raw_rows == 0；系统配置要求 qfq）
    #    - 不允许空值或未知 mode（nonempty_rows == total rows）
    #    - mixed_symbols == 0
    psm_has_col = price_series.get("has_column", False)
    psm_qfq_rows = price_series.get("qfq_rows", 0) or 0
    psm_raw_rows = price_series.get("raw_rows", 0) or 0
    psm_nonempty_rows = psm_qfq_rows + psm_raw_rows
    daily_total_rows = daily_bars.get("rows", 0) or 0
    mixed = price_series.get("mixed_symbols", 0) or 0
    psm_pass = bool(
        psm_has_col
        and psm_qfq_rows > 0
        and psm_raw_rows == 0
        and mixed == 0
        and psm_nonempty_rows == daily_total_rows
    )
    consistency = 1.0 if mixed == 0 else (1.0 - mixed / max(daily_symbols, 1))

    # 4. 自然键重复数
    dup_count = len(daily_bars.get("duplicate_symbol_date", []) or [])
    dup_pass = dup_count == GATES["duplicate_natural_keys"]

    # 5. 证券身份映射覆盖率
    #    分母 = 库内需要映射的 43/83/87 旧代码数；分子 = 已映射的旧代码数。
    #    旧代码数为零时（没有需要映射的旧代码）视为通过（N/A）。
    im_legacy_pairs = identity_mapping.get("legacy_symbol_date_pairs_in_daily_bars", 0) or 0
    if im_legacy_pairs == 0:
        im_ratio = 1.0
        im_pass = True
    else:
        im_ratio = identity_mapping.get("beijing_symbol_date_coverage_ratio")
        if im_ratio is None:
            mapped_pairs = identity_mapping.get("mapped_legacy_symbol_date_pairs", 0) or 0
            im_ratio = round(mapped_pairs / im_legacy_pairs, 6)
        im_pass = im_ratio >= GATES["symbol_identity_mapping_coverage"]

    # 6. 历史深度：最新交易日股票中 history_depth >= 250 的占比
    latest_symbols = daily_bars.get("latest_trade_date_symbols", 0) or 0
    ge_250 = daily_bars.get("history_depth", {}).get("ge_250_days", 0) or 0
    depth_ratio = (ge_250 / latest_symbols) if latest_symbols > 0 else 0.0
    depth_pass = depth_ratio >= 1.0

    gates = {
        "exact_limit_label_coverage": {
            "threshold": GATES["exact_limit_label_coverage"],
            "actual": ts_ratio,
            "min_daily_coverage": ts_min_daily,
            "pass": bool(ts_pass),
        },
        "moneyflow_active_coverage": {
            "threshold": GATES["moneyflow_active_coverage"],
            "actual": mf_ratio,
            "min_daily_coverage": mf_min_daily,
            "pass": bool(mf_pass),
        },
        "price_series_mode_consistency": {
            "threshold": GATES["price_series_mode_consistency"],
            "actual": round(consistency, 6),
            "mixed_symbols": mixed,
            "has_column": psm_has_col,
            "qfq_rows": psm_qfq_rows,
            "raw_rows": psm_raw_rows,
            "nonempty_rows": psm_nonempty_rows,
            "total_rows": daily_total_rows,
            "pass": bool(psm_pass),
        },
        "duplicate_natural_keys": {
            "threshold": GATES["duplicate_natural_keys"],
            "actual": dup_count,
            "pass": bool(dup_pass),
        },
        "symbol_identity_mapping_coverage": {
            "threshold": GATES["symbol_identity_mapping_coverage"],
            "actual": im_ratio,
            "pass": bool(im_pass),
        },
        "min_history_depth_days": {
            "threshold": GATES["min_history_depth_days"],
            "actual": round(depth_ratio, 6),
            "ge_250_symbols": ge_250,
            "latest_trade_date_symbols": latest_symbols,
            "pass": bool(depth_pass),
        },
    }
    return gates


# ----------------------------------------------------------------------------
# 报告生成
# ----------------------------------------------------------------------------


def build_audit_report(db_path: Path) -> dict[str, Any]:
    """打开只读连接，执行全部审计并组装报告字典。"""
    con = duckdb.connect(database=str(db_path), read_only=True)
    try:
        daily_bars = audit_daily_bars(con)
        daily_bars_latest = daily_bars.get("max_date") or None
        price_series = audit_price_series(con)
        trade_status = audit_trade_status(
            con,
            daily_bars.get("symbols", 0) or 0,
            daily_bars_latest_date=daily_bars_latest,
        )
        moneyflow = audit_moneyflow(
            con,
            daily_bars.get("symbols", 0) or 0,
            daily_bars_latest_date=daily_bars_latest,
        )
        security_status = audit_security_status(con)
        financial_snapshots = audit_financial_snapshots(con)
        identity_mapping = audit_identity_mapping(con, daily_bars.get("symbols", 0) or 0)
    finally:
        con.close()

    gates = evaluate_gates(daily_bars, price_series, trade_status, moneyflow, identity_mapping)

    failed = [name for name, g in gates.items() if not g["pass"]]
    overall_verdict = "GO" if not failed else "NO-GO"
    overall_reason = (
        "all research gates passed" if not failed else f"gates failed: {', '.join(failed)}"
    )

    report = {
        "schema_version": "stock-analyzer.research-data-coverage-audit.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "db_path": str(db_path),
        "daily_bars": daily_bars,
        "price_series": price_series,
        "trade_status": trade_status,
        "moneyflow": moneyflow,
        "security_status": security_status,
        "financial_snapshots": financial_snapshots,
        "identity_mapping": identity_mapping,
        "gates": gates,
        "overall_verdict": overall_verdict,
        "overall_reason": overall_reason,
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    """将审计报告渲染为 Markdown 表格形式。"""
    lines: list[str] = []
    lines.append("# 研究数据覆盖审计报告")
    lines.append("")
    lines.append(f"- **生成时间**: {report.get('generated_at')}")
    lines.append(f"- **数据库路径**: `{report.get('db_path')}`")
    lines.append(f"- **总体判定**: **{report.get('overall_verdict')}**")
    lines.append(f"- **判定原因**: {report.get('overall_reason')}")
    lines.append("")

    # ---- 各表摘要 ----
    db = report.get("daily_bars", {}) or {}
    ps = report.get("price_series", {}) or {}
    ts = report.get("trade_status", {}) or {}
    mf = report.get("moneyflow", {}) or {}
    ss = report.get("security_status", {}) or {}
    fs = report.get("financial_snapshots", {}) or {}
    im = report.get("identity_mapping", {}) or {}

    lines.append("## 数据表覆盖摘要")
    lines.append("")
    lines.append("| 数据表 | 存在 | 行数 | 股票数 | 覆盖率 | 最新日期 |")
    lines.append("| --- | :---: | ---: | ---: | ---: | --- |")
    lines.append(
        f"| daily_bars | {'是' if db.get('table_exists') else '否'} "
        f"| {db.get('rows', 0)} | {db.get('symbols', 0)} | — | {db.get('max_date')} |"
    )
    lines.append(
        f"| daily_trade_status | {'是' if ts.get('table_exists') else '否'} "
        f"| {ts.get('rows', 0)} | {ts.get('symbols', 0)} | {ts.get('symbol_ratio')} | {ts.get('latest_label_date')} |"  # noqa: E501
    )
    lines.append(
        f"| moneyflow | {'是' if mf.get('table_exists') else '否'} "
        f"| {mf.get('rows', 0)} | {mf.get('symbols', 0)} | {mf.get('symbol_ratio')} | {mf.get('latest_date')} |"  # noqa: E501
    )
    lines.append(
        f"| financial_snapshots | {'是' if fs.get('table_exists') else '否'} "
        f"| {fs.get('rows', 0)} | {fs.get('symbols', 0)} | — | {fs.get('latest_report_date')} |"
    )
    lines.append(
        f"| security_status | {'是' if ss.get('table_exists') else '否'} "
        f"| {ss.get('rows', 0)} | — | — | — |"
    )
    lines.append(
        f"| security_identity_mapping | {'是' if im.get('table_exists') else '否'} "
        f"| {im.get('rows', 0)} | hist={im.get('historical_symbols', 0)} | {im.get('canonical_ratio')} | — |"  # noqa: E501
    )
    lines.append("")

    # ---- daily_bars 细节 ----
    lines.append("## daily_bars 详情")
    lines.append("")
    hd = db.get("history_depth", {}) or {}
    lines.append(f"- 最小日期: {db.get('min_date')}")
    lines.append(f"- 最大日期: {db.get('max_date')}")
    lines.append(f"- 最新交易日股票覆盖: {db.get('latest_trade_date_symbols', 0)}")
    for t in HISTORY_DEPTH_THRESHOLDS:
        lines.append(f"- 历史深度 >= {t} 日的股票数: {hd.get(f'ge_{t}_days', 0)}")
    lines.append(f"- 重复 symbol/date 自然键数: {len(db.get('duplicate_symbol_date', []) or [])}")
    lines.append(f"- OHLCV 空值行数: {db.get('ohlcv_null_rows', 0)}")
    lines.append(f"- close <= 0 异常行数: {db.get('nonpositive_close_rows', 0)}")
    lines.append(f"- OHLC 关系异常行数: {db.get('ohlc_relation_anomaly_rows', 0)}")
    lines.append("")

    # ---- price_series 细节 ----
    lines.append("## price_series_mode 详情")
    lines.append("")
    lines.append(f"- 列存在: {'是' if ps.get('has_column') else '否'}")
    lines.append(f"- qfq 行数: {ps.get('qfq_rows', 0)}")
    lines.append(f"- raw 行数: {ps.get('raw_rows', 0)}")
    lines.append(f"- mixed symbols 数: {ps.get('mixed_symbols', 0)}")
    switches = ps.get("mode_switch_points", []) or []
    lines.append(f"- mode 切换点样本数: {len(switches)}")
    if switches:
        lines.append("")
        lines.append("| symbol | date | from | to |")
        lines.append("| --- | --- | --- | --- |")
        for sp in switches[:20]:
            lines.append(
                f"| {sp.get('symbol')} | {sp.get('date')} | {sp.get('from')} | {sp.get('to')} |"
            )
    lines.append("")

    # ---- moneyflow 细节 ----
    lines.append("## moneyflow 详情")
    lines.append("")
    lines.append(
        f"- 每股历史深度 min/median/max: {mf.get('depth_min', 0)} / {mf.get('depth_median', 0)} / {mf.get('depth_max', 0)}"  # noqa: E501
    )
    lo = mf.get("large_order_coverage", {}) or {}
    lines.append(f"- buy_lg_amount 非空行: {lo.get('buy_lg_amount', 0)}")
    lines.append(f"- sell_lg_amount 非空行: {lo.get('sell_lg_amount', 0)}")
    lines.append(f"- buy_elg_amount 非空行: {lo.get('buy_elg_amount', 0)}")
    lines.append(f"- sell_elg_amount 非空行: {lo.get('sell_elg_amount', 0)}")
    lines.append("")

    # ---- security_status 细节 ----
    lines.append("## security_status 详情")
    lines.append("")
    st_cov = ss.get("status_type_coverage", {}) or {}
    if st_cov:
        lines.append("| status_type | 行数 |")
        lines.append("| --- | ---: |")
        for st, cnt in st_cov.items():
            lines.append(f"| {st} | {cnt} |")
    else:
        lines.append("- 无 status_type 覆盖数据")
    lines.append(f"- coverage_complete = TRUE 行数: {ss.get('coverage_complete_rows', 0)}")
    lines.append("")

    # ---- financial_snapshots 细节 ----
    lines.append("## financial_snapshots 详情")
    lines.append("")
    lines.append(f"- 最新报告期: {fs.get('latest_report_date')}")
    lines.append(f"- ann_date 非空率: {fs.get('ann_date_non_null_ratio')}")
    lines.append("")

    # ---- 门禁判定 ----
    lines.append("## 研究门禁判定")
    lines.append("")
    lines.append("| 门禁 | 阈值 | 实际值 | 通过 |")
    lines.append("| --- | --- | --- | :---: |")
    gates = report.get("gates", {}) or {}
    for name, g in gates.items():
        threshold = g.get("threshold")
        actual = g.get("actual")
        passed = g.get("pass")
        mark = "PASS" if passed else "FAIL"
        lines.append(f"| {name} | {threshold} | {actual} | {mark} |")
    lines.append("")
    verdict = report.get("overall_verdict")
    reason = report.get("overall_reason")
    lines.append(f"> **总体判定: {verdict}** — {reason}")
    lines.append("")
    return "\n".join(lines)


def write_text(path: Path, content: str) -> None:
    """将文本内容写入指定路径（UTF-8）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "研究数据覆盖审计：以只读方式审计 DuckDB 仓库的核心数据表覆盖度与完整性，"
            "并按研究门禁给出 GO / NO-GO 判定。"
        ),
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="DuckDB 数据库文件路径（只读打开）。",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="JSON 输出路径；为空则只打印到 stdout。",
    )
    parser.add_argument(
        "--output-md",
        default="",
        help="Markdown 输出路径；为空则不生成 Markdown 文件。",
    )
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db_path).resolve()
    if not db_path.exists():
        print(f"[audit] db_path 不存在: {db_path}", file=sys.stderr)
        return 1

    try:
        report = build_audit_report(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] 审计失败: {exc}", file=sys.stderr)
        return 1

    json_text = json.dumps(report, ensure_ascii=False, indent=2)

    # JSON 输出
    if args.output_json:
        write_text(Path(args.output_json).resolve(), json_text)
        print(f"[audit] JSON 报告已写入: {args.output_json}")
    else:
        print(json_text)

    # Markdown 输出
    if args.output_md:
        md_text = render_markdown(report)
        write_text(Path(args.output_md).resolve(), md_text)
        print(f"[audit] Markdown 报告已写入: {args.output_md}")

    return 0 if report.get("overall_verdict") == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
