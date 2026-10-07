"""数据就绪审计验收（改进计划 §3.1 数据契约 + §5 分钟行情阻塞条款）。

要点：缺数据必须报成 blocked/insufficient，不能报成 0 覆盖率后继续往下跑，
也不能因为"日线能查到"就以为尾盘窗口能重建。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from stock_analyzer.research.trend_data_readiness import (
    READINESS_BLOCKED,
    READINESS_INSUFFICIENT,
    READINESS_READY,
    STATUS_BLOCKED,
    STATUS_OK,
    audit_trend_data_readiness,
    format_blocking_gaps,
    readiness_exit_code,
)

START = date(2026, 9, 1)
# 只用工作日：仓库里出现周末日线本身就该被日历检查抓出来，不能当成 fixture 噪声。
DAYS = [
    START + timedelta(days=offset)
    for offset in range(40)
    if (START + timedelta(days=offset)).weekday() < 5
]
SYMBOLS = [f"60000{i}.SH" for i in range(600)]


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def _seed_bars(conn, *, price_mode: bool = True, suspended: bool = True,
               limits: bool = True, weekend: bool = False) -> None:
    """建一张按生产仓库形态裁剪过的 daily_bars。"""
    columns = "symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE"
    if limits:
        columns += ", up_limit DOUBLE, down_limit DOUBLE"
    if suspended:
        columns += ", suspended BOOLEAN"
    if price_mode:
        columns += ", price_mode VARCHAR"
    conn.execute(f"CREATE TABLE daily_bars ({columns})")
    days = ([START + timedelta(days=offset) for offset in range(25)]
            if weekend else DAYS)
    for day in days:
        for symbol in SYMBOLS:
            row = {"symbol": symbol, "date": day, "open": 10.0, "high": 10.2,
                   "low": 9.9, "close": 10.1}
            if limits:
                row.update({"up_limit": 11.1, "down_limit": 9.1})
            if suspended:
                row["suspended"] = False
            if price_mode:
                row["price_mode"] = "raw"
            placeholders = ", ".join("?" for row in row)
            conn.execute(
                f"INSERT INTO daily_bars ({', '.join(row)}) VALUES ({placeholders})",
                list(row.values()),
            )


def _seed_index(conn, *, coverage: float = 1.0) -> None:
    conn.execute(
        "CREATE TABLE index_daily (index_code VARCHAR, trade_date DATE, close DOUBLE)"
    )
    for day in DAYS:
        if _rng(day) > coverage:
            continue
        conn.execute("INSERT INTO index_daily VALUES ('000300.SH', ?, 4000.0)", [day])


def _rng(day: date) -> float:
    return (day.toordinal() % 10) / 10.0


def _seed_minute_summary(conn) -> None:
    """生产仓库的真实形态：日级聚合，没有任何 bar 时刻列。"""
    conn.execute(
        "CREATE TABLE intraday_summary_1m (symbol VARCHAR, date DATE, "
        "minute_count DOUBLE, last30_return DOUBLE, close_position DOUBLE)"
    )
    for day in DAYS:
        for symbol in SYMBOLS[:20]:
            conn.execute(
                "INSERT INTO intraday_summary_1m VALUES (?, ?, 240, 0.01, 0.6)",
                [symbol, day],
            )


def _seed_minute_bars(conn) -> None:
    conn.execute(
        "CREATE TABLE intraday_minute_bars (symbol VARCHAR, bar_time TIMESTAMP, "
        "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE)"
    )
    for day in DAYS:
        for minute in range(30, 51):
            conn.execute(
                "INSERT INTO intraday_minute_bars VALUES (?, ?, 10.0, 10.0, 10.0, 10.0, 100)",
                ["600000.SH", datetime(day.year, day.month, day.day, 14, minute)],
            )


def _statuses(conn) -> None:
    conn.execute("CREATE TABLE security_status (symbol VARCHAR, effective_from DATE, "
                 "effective_to DATE, status_type VARCHAR, coverage_complete BOOLEAN)")
    for symbol in SYMBOLS:
        conn.execute(
            "INSERT INTO security_status VALUES (?, ?, NULL, 'list_status', true)",
            [symbol, START],
        )


def _full_ready_conn() -> duckdb.DuckDBPyConnection:
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn)
    _seed_minute_bars(conn)
    _statuses(conn)
    return conn


def test_missing_warehouse_tables_are_blocked_not_empty() -> None:
    conn = _connect()
    conn.execute("CREATE TABLE unrelated (x INT)")
    report = audit_trend_data_readiness(connection=conn)
    assert report["readiness"] == READINESS_BLOCKED
    assert "daily_bars_coverage" in report["blocking_gaps"]
    assert "tail_window_minute_bars" in report["blocking_gaps"]
    assert readiness_exit_code(report) == 5


def test_summary_only_minute_table_blocks_tail_window_reconstruction() -> None:
    """生产仓库现状：分钟表只有日级聚合列 —— 14:30-14:50 不可重建，必须 blocked。"""
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_summary(conn)
    report = audit_trend_data_readiness(connection=conn)
    blocked = {item["name"] for item in report["checks"]
               if item["status"] == STATUS_BLOCKED}
    assert "tail_window_minute_bars" in blocked
    assert report["readiness"] == READINESS_BLOCKED
    assert "不得用开盘回测代替" in format_blocking_gaps(report)


def test_bar_timestamped_minute_table_lifts_the_block() -> None:
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    report = audit_trend_data_readiness(connection=conn)
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["tail_window_minute_bars"]["status"] == STATUS_OK
    assert checks["tail_window_minute_bars"]["detail"]["1min"][
        "tail_window_bar_rows"] > 0
    assert report["readiness"] == READINESS_READY
    assert readiness_exit_code(report) == 0


def test_undeclared_price_basis_blocks_execution_simulation() -> None:
    """无复权口径声明时不能拿这批价格去模拟成交（可能是 QFQ）。"""
    conn = _connect()
    _seed_bars(conn, price_mode=False)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    report = audit_trend_data_readiness(connection=conn)
    assert "raw_price_basis_declared" in report["blocking_gaps"]


def test_only_qfq_prices_are_blocked_for_fills() -> None:
    conn = _connect()
    _seed_bars(conn)
    conn.execute("UPDATE daily_bars SET price_mode = 'qfq'")
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    report = audit_trend_data_readiness(connection=conn)
    assert "raw_price_basis_declared" in report["blocking_gaps"]


def test_absent_limit_field_is_blocked_not_guessed_by_percentage() -> None:
    """既无 daily_trade_status 又无 up_limit 字段：精确涨跌停无从谈起。"""
    conn = _connect()
    _seed_bars(conn, limits=False)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    report = audit_trend_data_readiness(connection=conn)
    assert "precise_limit_price_coverage" in report["blocking_gaps"]
    assert report["readiness"] == READINESS_BLOCKED


def test_sparse_precise_limits_downgrade_to_insufficient() -> None:
    """字段在但只有零星覆盖：门禁改用比例近似，必须报"不足"而不是"没问题"。"""
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    conn.execute("UPDATE daily_bars SET up_limit = NULL WHERE symbol > '600002.SH'")
    report = audit_trend_data_readiness(connection=conn)
    assert "precise_limit_price_coverage" in report["insufficient_items"]
    assert report["readiness"] == READINESS_INSUFFICIENT
    assert readiness_exit_code(report) == 3


def test_index_gap_is_insufficient_not_zero_filled() -> None:
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn, coverage=0.3)
    _statuses(conn)
    _seed_minute_bars(conn)
    report = audit_trend_data_readiness(connection=conn)
    check = {item["name"]: item for item in report["checks"]}["benchmark_index_continuity"]
    assert check["status"] == "insufficient"
    assert check["detail"]["best_share"] < 0.95
    assert "benchmark_index_continuity" in report["insufficient_items"]


def test_weekend_bars_flag_calendar_inconsistency() -> None:
    conn = _connect()
    _seed_bars(conn, weekend=True)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    report = audit_trend_data_readiness(connection=conn)
    assert "trade_calendar_consistency" in report["insufficient_items"]


def test_overlapping_security_status_intervals_are_insufficient() -> None:
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    conn.execute(
        "INSERT INTO security_status VALUES ('600000.SH', ?, ?, 'is_st', true)",
        [START, None],
    )
    conn.execute(
        "INSERT INTO security_status VALUES ('600000.SH', ?, NULL, 'list_status', true)",
        [START + timedelta(days=1)],
    )
    _seed_minute_bars(conn)
    report = audit_trend_data_readiness(connection=conn)
    assert "security_status_intervals" in report["insufficient_items"]


def test_report_always_pins_the_contract_it_was_audited_against() -> None:
    report = audit_trend_data_readiness(connection=_full_ready_conn())
    assert report["contract_digest"]
    assert report["contract_version"] == "trend_tail_v1"
    assert report["tail_entry_window"] == ["14:30", "14:50"]


def test_unreadable_connection_is_blocked_rather_than_ready() -> None:
    class Broken:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("database is locked")

    report = audit_trend_data_readiness(connection=Broken())
    assert report["readiness"] == READINESS_BLOCKED
    assert report["blocking_gaps"] == ["warehouse"]


def test_cli_writes_the_report_and_returns_the_blocking_exit_code(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "market.duckdb"
    conn = duckdb.connect(str(db_path))
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_summary(conn)
    conn.close()

    out_path = tmp_path / "readiness.json"
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "audit_trend_data_readiness.py"),
         "--db", str(db_path), "--out", str(out_path)],
        cwd=str(root), capture_output=True, text=True, check=False,
        env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 5, result.stdout + result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["readiness"] == READINESS_BLOCKED
    assert "tail_window_minute_bars" in report["blocking_gaps"]
