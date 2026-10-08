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


def test_bar_timestamped_minute_table_lifts_the_block(tmp_path) -> None:
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    reference = _reference_db(tmp_path)
    report = audit_trend_data_readiness(connection=conn, reference_connection=reference)
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["tail_window_minute_bars"]["status"] == STATUS_OK
    assert checks["tail_window_minute_bars"]["detail"]["1min"][
        "tail_window_bar_rows"] > 0
    assert report["readiness"] == READINESS_READY
    assert readiness_exit_code(report) == 0
    reference.close()


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
        # **不注入 PYTHONPATH**：文档里的命令就是 `python scripts/audit_...`，
        # 脚本必须自己能找到 src/，否则照着清单做解锁的人第一跳就崩。
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 5, result.stdout + result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["readiness"] == READINESS_BLOCKED
    assert "tail_window_minute_bars" in report["blocking_gaps"]


# --- 研究库参考数据副本：§3.1 的"补齐后验证" ------------------------------------

_REF_SYMBOLS = SYMBOLS[:20]
_AS_OF = "2026-10-08"


def _reference_db(
    tmp_path,
    *,
    declare_status: bool = True,
    approximated: bool = False,
    security: bool = True,
    extra_bar_days=(),
    days=None,
):
    """把研究库副本落到文件，返回**只读**连接（审计不该改被审计的库）。"""
    import pandas as pd

    from stock_analyzer.research.tail_reference_store import TailReferenceStore

    days = list(days) if days is not None else list(DAYS)
    bar_days = days + list(extra_bar_days)
    path = tmp_path / "reference.duckdb"
    with TailReferenceStore(path) as store:
        store.upsert_calendar(days, as_of=_AS_OF)
        store.upsert_daily_bars(
            pd.DataFrame([
                {"symbol": symbol, "date": day, "open": 10.0, "high": 10.2,
                 "low": 9.9, "close": 10.1, "volume": 1e6}
                for day in bar_days for symbol in _REF_SYMBOLS
            ]),
            price_basis="raw", as_of=_AS_OF,
        )
        store.upsert_limit_prices(
            pd.DataFrame([
                {"symbol": symbol, "trade_date": day, "up_limit": 11.1, "down_limit": 9.1}
                for day in bar_days for symbol in _REF_SYMBOLS
            ]),
            as_of=_AS_OF, approximated=approximated,
        )
        statuses = [
            {"symbol": symbol, "trade_date": day, "suspended": False}
            for day in bar_days for symbol in _REF_SYMBOLS
        ]
        if declare_status:
            for row in statuses:
                row["trade_status"] = "normal"
        store.upsert_suspend_status(pd.DataFrame(statuses), as_of=_AS_OF)
        if security:
            store.upsert_security_status(
                pd.DataFrame([
                    {"symbol": symbol, "effective_from": START, "status_type": "ST",
                     "status_value": "N"}
                    for symbol in _REF_SYMBOLS
                ]),
                as_of=_AS_OF,
            )
    return duckdb.connect(str(path), read_only=True)


def _ready_conn() -> duckdb.DuckDBPyConnection:
    conn = _connect()
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    _seed_minute_bars(conn)
    return conn


def _by_name(report) -> dict:
    return {item["name"]: item for item in report["checks"]}


def test_unvalidated_reference_copy_is_named_not_assumed(tmp_path) -> None:
    """"仓库有数据"不等于"能重建标签"：副本没校验就不能报 ready。"""
    report = audit_trend_data_readiness(connection=_ready_conn())
    assert report["readiness"] == READINESS_INSUFFICIENT
    assert "reference_copy_validated" in report["insufficient_items"]
    assert "没有传 --reference-db" in _by_name(report)["reference_copy_validated"]["note"]
    assert readiness_exit_code(report) == 3


def test_validated_reference_copy_lifts_readiness_to_ready(tmp_path) -> None:
    reference = _reference_db(tmp_path)
    report = audit_trend_data_readiness(connection=_ready_conn(),
                                       reference_connection=reference)
    reference.close()
    assert report["readiness"] == READINESS_READY, report["insufficient_items"]
    assert [name for name in report["blocking_gaps"]] == []


def test_missing_reference_sources_block_instead_of_reporting_zero_coverage(tmp_path) -> None:
    """空文件里一张参考表都没有：这是"没建副本"，不是"覆盖率为 0"。"""
    path = tmp_path / "empty.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE unrelated (x INT)")
    conn.close()
    report = audit_trend_data_readiness(
        connection=_ready_conn(),
        reference_connection=duckdb.connect(str(path), read_only=True),
    )
    checks = _by_name(report)
    assert checks["reference_copy_present"]["status"] == STATUS_BLOCKED
    assert "sync_tail_reference_data" in checks["reference_copy_present"]["note"]
    assert set(checks["reference_copy_present"]["detail"]["missing_required_sources"]) == {
        "trade_calendar", "daily_bars", "limit_prices", "suspend_status",
    }
    assert report["readiness"] == READINESS_BLOCKED


def test_no_declared_trade_status_blocks_label_production(tmp_path) -> None:
    """§3.3：状态未声明的日子出不来已实现盈亏 —— 一条都没有就是整条链阻塞。"""
    reference = _reference_db(tmp_path, declare_status=False)
    report = audit_trend_data_readiness(connection=_ready_conn(),
                                       reference_connection=reference)
    reference.close()
    check = _by_name(report)["reference_copy_trade_status_declared"]
    assert check["status"] == STATUS_BLOCKED
    assert "unknown_trade_status" in check["note"]
    assert check["detail"]["declared_trade_status_symbol_days"] == 0
    assert report["readiness"] == READINESS_BLOCKED


def test_approximated_limit_prices_are_not_counted_as_exact(tmp_path) -> None:
    reference = _reference_db(tmp_path, approximated=True)
    report = audit_trend_data_readiness(connection=_ready_conn(),
                                       reference_connection=reference)
    reference.close()
    check = _by_name(report)["reference_copy_limit_prices_exact"]
    assert check["status"] == "insufficient"
    assert check["detail"]["exact_limit_price_symbol_days"] == 0
    assert "entry_day_limit_prices" in check["note"]


def test_calendar_conflict_inside_the_copy_blocks_the_rebuild(tmp_path) -> None:
    """副本里出现权威日历说没开市的日子：第 5 个交易日会算错，不能带着矛盾出标签。"""
    saturday = date(2026, 9, 5)
    reference = _reference_db(tmp_path, extra_bar_days=[saturday])
    report = audit_trend_data_readiness(connection=_ready_conn(),
                                       reference_connection=reference)
    reference.close()
    check = _by_name(report)["reference_copy_calendar_consistent"]
    assert check["status"] == STATUS_BLOCKED
    assert str(saturday) in check["detail"]["conflicting_trade_dates"]


def test_legacy_adjusted_rows_in_the_copy_are_refused(tmp_path) -> None:
    """历史遗留的 qfq 行不能拿来做成交模拟（ADR-002）：口径混了就阻塞。"""
    from stock_analyzer.research.tail_reference_store import REFERENCE_TABLES

    reference = _reference_db(tmp_path)
    reference.close()
    copy_path = str(tmp_path / "reference.duckdb")
    writable = duckdb.connect(copy_path)
    writable.execute(
        f"UPDATE {REFERENCE_TABLES['daily_bars']} SET price_basis = 'qfq' "
        "WHERE symbol = ?", [_REF_SYMBOLS[0]]
    )
    writable.close()
    report = audit_trend_data_readiness(
        connection=_ready_conn(),
        reference_connection=duckdb.connect(copy_path, read_only=True),
    )
    check = _by_name(report)["reference_copy_raw_only"]
    assert check["status"] == STATUS_BLOCKED
    assert set(check["detail"]["price_bases"]) == {"qfq", "raw"}
    assert "ADR-002" in check["note"]


def test_empty_security_status_is_insufficient_not_blocked(tmp_path) -> None:
    """第五类来源没落地：影响资格判定的能力，但不阻塞单笔成交/出场。"""
    reference = _reference_db(tmp_path, security=False)
    report = audit_trend_data_readiness(connection=_ready_conn(),
                                       reference_connection=reference)
    reference.close()
    check = _by_name(report)["reference_copy_present"]
    assert check["status"] == "insufficient"
    assert "ref_security_status" in check["note"]
    assert report["readiness"] == READINESS_INSUFFICIENT


def _run_cli(tmp_path, args) -> subprocess.CompletedProcess:
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "audit_trend_data_readiness.py"), *args],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
        # 不注入 PYTHONPATH：脚本必须自己找到 src/（与上面的 CLI 测试同一条理由）。
        env={"PATH": "/usr/bin:/bin"},
    )


def _warehouse_file(tmp_path, *, with_bar_timestamps: bool) -> str:
    path = tmp_path / "market.duckdb"
    conn = duckdb.connect(str(path))
    _seed_bars(conn)
    _seed_index(conn)
    _statuses(conn)
    (_seed_minute_bars if with_bar_timestamps else _seed_minute_summary)(conn)
    conn.close()
    return str(path)


def test_cli_reads_the_reference_copy_only_when_told_to(tmp_path) -> None:
    """``--reference-db`` 是真的被读：不传就报"没校验"，传了才能报 ready。"""
    warehouse = _warehouse_file(tmp_path, with_bar_timestamps=True)
    _reference_db(tmp_path).close()  # 副本落到 tmp_path/reference.duckdb
    reference_path = str(tmp_path / "reference.duckdb")

    out = tmp_path / "readiness.json"
    unvalidated = _run_cli(tmp_path, ["--db", warehouse, "--reference-db", "missing.duckdb",
                                     "--out", str(out), "--quiet"])
    assert unvalidated.returncode == 3, unvalidated.stdout + unvalidated.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "reference_copy_validated" in report["insufficient_items"]

    validated = _run_cli(tmp_path, ["--db", warehouse, "--reference-db", reference_path,
                                    "--out", str(out), "--quiet"])
    assert validated.returncode == 0, validated.stdout + validated.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    names = {item["name"] for item in report["checks"]}
    assert {"reference_copy_present", "reference_copy_trade_status_declared"} <= names
    assert report["readiness"] == READINESS_READY
