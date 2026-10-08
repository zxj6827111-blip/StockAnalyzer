"""trend 尾盘链路的数据就绪审计：能算标签吗？算不了的话缺哪一样。

改进计划 §3.1 要求"补齐并校验交易日历、RAW 行情、精确涨跌停、停复牌和证券历史
状态""校验指数数据链路，确保相对强弱等特征真实计算，缺失不能被填零后当成有效
信息"，并要求"数据无法证实时标记为不足"。§5 进一步明确：历史分钟行情不足时，
尾盘策略验证**必须记为阻塞**，不能用开盘回测代替。

本模块就是那条"记为阻塞"的实现：它只做只读探测，按表实际拥有的列来判定
（不同代际的仓库列集合不一样，假设列存在会把缺数据错报成 0 覆盖率），并区分
三种状态：

``ok``            该项足以支撑尾盘契约的成交模拟；
``insufficient``  项存在但覆盖不到门槛——相关特征必须标为不可用，不得填零；
``blocked``       项根本不存在——尾盘策略验证整体不可执行，不得用开盘口径顶替。

用法：::

    from duckdb import connect
    report = audit_trend_data_readiness(connect(path, read_only=True))
    if report["readiness"] != "ready":  # 阻塞/不足都不能进入选股质量验收
        ...
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    TrendStrategyContract,
)

READINESS_READY = "ready"
READINESS_INSUFFICIENT = "insufficient"
READINESS_BLOCKED = "blocked"

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient"
STATUS_BLOCKED = "blocked"

#: 判定"够用"的门槛。写成常量是为了让验收报告能引用同一组数字，而不是每次口头说。
MIN_LIMIT_PRICE_COVERAGE = 0.90
MIN_TRADE_STATUS_COVERAGE = 0.90
MIN_INDEX_CONTINUITY = 0.95
MIN_DAILY_ROWS_PER_DAY = 500
#: 尾盘窗口需要覆盖到的分钟数：14:30-14:50 每 5 分钟一个确认点，成交还要窗口后一根。
MIN_TAIL_BARS_PER_DAY = 4

BAR_TIME_COLUMNS = ("bar_time", "end_time", "datetime", "trade_time", "timestamp", "time")

MINUTE_TABLES = {
    "1min": ("intraday_summary_1m", "intraday_minute_bars", "minute_bars_1min"),
    "5min": ("intraday_summary_5m", "intraday_minute_bars_5m", "minute_bars_5min"),
}


@dataclass
class ReadinessCheck:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "note": self.note}


class _Probe:
    """只读探测包装：表/列缺失返回 None，而不是抛错或当成空覆盖。"""

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    def tables(self) -> set[str]:
        rows = self._conn.execute("SHOW TABLES").fetchall()
        return {str(row[0]) for row in rows}

    def columns(self, table: str) -> set[str]:
        try:
            rows = self._conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table],
            ).fetchall()
        except Exception:  # noqa: BLE001 - 探测失败就是"不知道"，不能当成有数据
            return set()
        return {str(row[0]).strip().lower() for row in rows}

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        try:
            return self._conn.execute(sql, params or []).fetchone()[0]
        except Exception:  # noqa: BLE001
            return None

    def rows(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        try:
            return list(self._conn.execute(sql, params or []).fetchall())
        except Exception:  # noqa: BLE001
            return []


def _pick(existing: Mapping[str, Any] | set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in existing:
            return candidate
    return None


def audit_trend_data_readiness(
    *,
    connection: Any,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    benchmark_codes: tuple[str, ...] = ("000300", "399001"),
    min_rows_per_day: int = MIN_DAILY_ROWS_PER_DAY,
    minute_connection: Any | None = None,
) -> dict[str, Any]:
    """按尾盘策略契约的要求探测仓库数据，返回可审计的就绪报告。

    ``minute_connection`` 指向带时刻的分钟研究库（可选）：只有它真的非空，
    ``tail_window_minute_bars`` 才会从 blocked 翻成 ok。
    """
    probe = _Probe(connection)
    try:
        tables = probe.tables()
    except Exception as exc:  # noqa: BLE001 - 连不上库就是彻底阻塞，不能报 ready
        check = ReadinessCheck("warehouse", STATUS_BLOCKED, {"error": str(exc)},
                               "无法读取仓库表清单")
        return {
            "readiness": READINESS_BLOCKED,
            "checks": [check.as_dict()],
            "blocking_gaps": [check.name],
            "insufficient_items": [],
            "contract_version": contract.contract_version,
            "contract_digest": contract.digest(),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    checks: list[ReadinessCheck] = []
    bars_table = _pick(tables, ("daily_bars", "stock_daily", "daily"))
    checks.append(_audit_daily_bars(probe, bars_table, min_rows_per_day))
    checks.append(_audit_price_mode(probe, bars_table))
    checks.append(_audit_limit_prices(probe, bars_table, tables))
    checks.append(_audit_trade_status(probe, bars_table, tables))
    checks.append(_audit_security_status_intervals(probe, tables))
    checks.append(_audit_index_continuity(probe, bars_table, tables, benchmark_codes))
    checks.append(_audit_tail_minute_bars(
        probe, tables,
        minute_probe=(_Probe(minute_connection) if minute_connection is not None else None),
    ))
    checks.append(_audit_calendar(probe, bars_table))

    blocking = [check.name for check in checks if check.status == STATUS_BLOCKED]
    weak = [check.name for check in checks if check.status == STATUS_INSUFFICIENT]
    readiness = (
        READINESS_BLOCKED if blocking
        else READINESS_INSUFFICIENT if weak
        else READINESS_READY
    )
    return {
        "readiness": readiness,
        "checks": [check.as_dict() for check in checks],
        "blocking_gaps": blocking,
        "insufficient_items": weak,
        "contract_version": contract.contract_version,
        "contract_digest": contract.digest(),
        "tail_entry_window": [contract.entry_window_start, contract.entry_window_end],
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def _audit_daily_bars(
    probe: _Probe, table: str | None, min_rows_per_day: int
) -> ReadinessCheck:
    name = "daily_bars_coverage"
    if table is None:
        return ReadinessCheck(name, STATUS_BLOCKED, {}, "仓库里没有日线表")
    rows = int(probe.scalar(f"SELECT COUNT(*) FROM {table}") or 0)
    date_column = _pick(probe.columns(table), ("date", "trade_date"))
    if rows == 0 or date_column is None:
        return ReadinessCheck(name, STATUS_BLOCKED, {"rows": rows}, "日线表为空或缺日期列")
    min_date = str(probe.scalar(f"SELECT MIN({date_column}) FROM {table}") or "")
    max_date = str(probe.scalar(f"SELECT MAX({date_column}) FROM {table}") or "")
    per_day = probe.rows(
        f"SELECT COUNT(DISTINCT symbol), {date_column} FROM {table} "
        f"GROUP BY {date_column} ORDER BY {date_column}"
    )
    thin_days = [str(day) for count, day in per_day if int(count or 0) < min_rows_per_day]
    counts = [int(count or 0) for count, _ in per_day]
    return ReadinessCheck(
        name,
        STATUS_OK if per_day and not thin_days else STATUS_INSUFFICIENT,
        {
            "table": table,
            "rows": rows,
            "trade_days": len(per_day),
            "min_date": min_date,
            "max_date": max_date,
            "median_symbols_per_day": _median(counts),
            "thin_day_count": len(thin_days),
            "thin_day_sample": thin_days[:10],
        },
        "日覆盖不足的日子会被当成不可用样本，不补数据",
    )


def _audit_price_mode(probe: _Probe, table: str | None) -> ReadinessCheck:
    """成交模拟必须用 raw；仓库若不带复权口径声明，就无法证明它是 raw。"""
    name = "raw_price_basis_declared"
    if table is None:
        return ReadinessCheck(name, STATUS_BLOCKED, {}, "无日线表")
    columns = probe.columns(table)
    mode_column = _pick(columns, ("price_mode", "adjust_flag", "dividend_treatment"))
    if mode_column is None:
        return ReadinessCheck(
            name, STATUS_BLOCKED,
            {"table": table, "looked_for": ["price_mode", "adjust_flag",
                                            "dividend_treatment"]},
            "仓库未声明复权口径，不能用于成交模拟（QFQ 价只能进特征）",
        )
    values = probe.rows(
        f"SELECT {mode_column}, COUNT(*) FROM {table} GROUP BY {mode_column}"
    )
    raw_rows = sum(int(count or 0) for value, count in values
                   if str(value).strip().lower() in {"raw", "none", "0", "unadjusted"})
    total = sum(int(count or 0) for _, count in values)
    share = (raw_rows / total) if total else 0.0
    return ReadinessCheck(
        name,
        STATUS_OK if share >= 0.99 else STATUS_BLOCKED if raw_rows == 0 else STATUS_INSUFFICIENT,
        {"column": mode_column, "distinct": {str(v): int(c or 0) for v, c in values},
         "raw_share": share},
        "raw 占比不足时，成交与净收益只能标为不确定",
    )


def _audit_limit_prices(probe: _Probe, bars_table: str | None, tables: set[str]) -> ReadinessCheck:
    """精确涨跌停：优先 stk_limit 落库值；缺失时只能用比例近似，覆盖率必须报出来。"""
    name = "precise_limit_price_coverage"
    status_table = "daily_trade_status" if "daily_trade_status" in tables else None
    if status_table is not None:
        total = int(probe.scalar(f"SELECT COUNT(*) FROM {status_table}") or 0)
        with_limits = int(probe.scalar(
            f"SELECT COUNT(*) FROM {status_table} "
            "WHERE up_limit IS NOT NULL AND down_limit IS NOT NULL"
        ) or 0)
        sources = {str(row[0]): int(row[1] or 0) for row in probe.rows(
            f"SELECT source, COUNT(*) FROM {status_table} GROUP BY source"
        )}
        complete = int(probe.scalar(
            f"SELECT COUNT(*) FROM {status_table} WHERE coverage_complete"
        ) or 0)
        share = (with_limits / total) if total else 0.0
        return ReadinessCheck(
            name,
            STATUS_OK if total and share >= MIN_LIMIT_PRICE_COVERAGE else STATUS_INSUFFICIENT,
            {"table": status_table, "rows": total, "exact_limit_share": share,
             "coverage_complete_share": (complete / total) if total else 0.0,
             "sources": sources},
            "tushare stk_limit(doc_id=183) 落库口径；不足的门禁日改用比例近似并计入不确定",
        )
    if bars_table is None:
        return ReadinessCheck(name, STATUS_BLOCKED, {}, "既无 daily_trade_status 也无日线表")
    columns = probe.columns(bars_table)
    up = _pick(columns, ("up_limit", "limit_up", "high_limit"))
    if up is None:
        return ReadinessCheck(
            name, STATUS_BLOCKED, {"table": bars_table},
            "没有精确涨跌停字段：涨跌停判定只能按比例近似",
        )
    total = int(probe.scalar(f"SELECT COUNT(*) FROM {bars_table}") or 0)
    filled = int(probe.scalar(f"SELECT COUNT({up}) FROM {bars_table}") or 0)
    share = (filled / total) if total else 0.0
    return ReadinessCheck(
        name,
        STATUS_OK if share >= MIN_LIMIT_PRICE_COVERAGE else STATUS_INSUFFICIENT,
        {"table": bars_table, "column": up, "rows": total, "exact_limit_share": share},
    )


def _audit_trade_status(probe: _Probe, bars_table: str | None, tables: set[str]) -> ReadinessCheck:
    """停复牌：缺 bar 不等于停牌，所以停牌标记必须是显式来源。"""
    name = "suspension_flag_coverage"
    candidate_tables = [t for t in (bars_table, "daily_trade_status") if t in tables]
    evidence: dict[str, Any] = {}
    any_ok = False
    for table in candidate_tables:
        columns = probe.columns(table)
        column = _pick(columns, ("suspended", "is_suspended", "suspend"))
        if column is None:
            continue
        total = int(probe.scalar(f"SELECT COUNT(*) FROM {table}") or 0)
        declared = int(probe.scalar(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL"
        ) or 0)
        flagged = int(probe.scalar(
            f"SELECT COUNT(*) FROM {table} WHERE {column}"
        ) or 0)
        share = (declared / total) if total else 0.0
        evidence[table] = {"column": column, "rows": total, "declared_share": share,
                           "suspended_rows": flagged}
        any_ok = any_ok or share >= MIN_TRADE_STATUS_COVERAGE
    if not evidence:
        return ReadinessCheck(
            name, STATUS_BLOCKED, {},
            "没有显式停牌字段：缺 bar 会被当成停牌，违反计划 §3.1",
        )
    return ReadinessCheck(
        name, STATUS_OK if any_ok else STATUS_INSUFFICIENT, evidence,
        "tushare suspend_d(doc_id=214) 与仓库 suspended 字段",
    )


def _audit_security_status_intervals(probe: _Probe, tables: set[str]) -> ReadinessCheck:
    name = "security_status_intervals"
    if "security_status" not in tables:
        return ReadinessCheck(name, STATUS_BLOCKED, {},
                              "无 PIT 证券状态区间表：ST/上市/退市无法按时间还原")
    total = int(probe.scalar("SELECT COUNT(*) FROM security_status") or 0)
    symbols = int(probe.scalar("SELECT COUNT(DISTINCT symbol) FROM security_status") or 0)
    complete = int(probe.scalar(
        "SELECT COUNT(*) FROM security_status WHERE coverage_complete"
    ) or 0)
    overlaps = probe.rows(
        "SELECT a.symbol, a.effective_from, b.effective_from FROM security_status a "
        "JOIN security_status b ON a.symbol = b.symbol AND a.status_type = b.status_type "
        "AND a.effective_from < b.effective_from "
        "AND (a.effective_to IS NULL OR a.effective_to >= b.effective_from) LIMIT 5"
    )
    share = (complete / total) if total else 0.0
    return ReadinessCheck(
        name,
        STATUS_OK if total and not overlaps and share >= MIN_TRADE_STATUS_COVERAGE
        else STATUS_INSUFFICIENT,
        {"rows": total, "symbols": symbols, "coverage_complete_share": share,
         "overlap_sample": [list(row) for row in overlaps]},
    )


def _audit_index_continuity(
    probe: _Probe, bars_table: str | None, tables: set[str], benchmark_codes: tuple[str, ...]
) -> ReadinessCheck:
    """指数链路：缺失时必须显式不可用，相对强弱不能填零当成有效信息。"""
    name = "benchmark_index_continuity"
    if "index_daily" not in tables:
        return ReadinessCheck(name, STATUS_BLOCKED, {},
                              "无 index_daily：市场相对强弱族不可计算（不得填零）")
    if bars_table is None:
        return ReadinessCheck(name, STATUS_INSUFFICIENT, {}, "无日线表，无法比对交易日")
    date_column = _pick(probe.columns(bars_table), ("date", "trade_date")) or "date"
    bars_dates = [
        str(row[0])
        for row in probe.rows(
            f"SELECT DISTINCT {date_column} FROM {bars_table} ORDER BY 1 DESC LIMIT 250"
        )
    ]
    placeholders = ",".join("?" for _ in bars_dates)
    per_code: dict[str, Any] = {}
    best_share = 0.0
    for code in benchmark_codes:
        matched = int(probe.scalar(
            f"SELECT COUNT(DISTINCT trade_date) FROM index_daily WHERE "
            f"CAST(trade_date AS VARCHAR) IN ({placeholders}) AND "
            "(index_code = ? OR index_code LIKE ?)",
            [*bars_dates, code, f"{code}%"],
        ) or 0)
        max_date = str(probe.scalar(
            "SELECT MAX(trade_date) FROM index_daily WHERE index_code = ? OR "
            "index_code LIKE ?", [code, f"{code}%"]
        ) or "")
        share = (matched / len(bars_dates)) if bars_dates else 0.0
        best_share = max(best_share, share)
        per_code[code] = {"matched_trade_days": matched, "share_of_recent_bars": share,
                          "max_date": max_date}
    return ReadinessCheck(
        name,
        STATUS_OK if best_share >= MIN_INDEX_CONTINUITY else STATUS_INSUFFICIENT,
        {"index_trade_days": per_code, "best_share": best_share,
         "recent_bar_days": len(bars_dates)},
        "份额不足时 rs/excess_* 特征必须标为不可用，而不是补 0",
    )


def _audit_tail_minute_bars(
    probe: _Probe,
    tables: set[str],
    *,
    minute_probe: _Probe | None = None,
) -> ReadinessCheck:
    """尾盘窗口能不能重建：需要**带时间戳的**分钟 bar，而不是日级汇总。

    ``intraday_summary_1m/5m`` 只有 12 个日级聚合列（minute_count / last30_return
    等），没有 bar 时刻列，因此 14:30–14:50 的逐 5 分钟确认与"确认后下一根成交"
    无法从落库数据重建。这一项必须是 ``blocked``，且**不得**改用开盘价回测顶替。

    ``minute_probe`` 是带时刻的**分钟研究库**（``research/minute_bar_store.py``
    的产物）：它存在且非空时本项才允许翻成 ok，判定标准不变，只是多看一个来源。
    """
    name = "tail_window_minute_bars"
    sources: list[tuple[str, _Probe, set[str]]] = [("warehouse", probe, tables)]
    if minute_probe is not None:
        sources.append(("research_minute", minute_probe, minute_probe.tables()))
    looked: dict[str, Any] = {}
    for label, active, available in sources:
        for interval, candidates in MINUTE_TABLES.items():
            table = _pick(available, candidates)
            key = interval if label == "warehouse" else f"{label}:{interval}"
            if table is None:
                looked[key] = {"table": None, "candidates": list(candidates),
                               "source": label}
                continue
            columns = active.columns(table)
            time_column = _pick(columns, BAR_TIME_COLUMNS)
            rows = int(active.scalar(f"SELECT COUNT(*) FROM {table}") or 0)
            tail_bars = None
            if time_column is not None:
                tail_bars = active.scalar(
                    f"SELECT COUNT(*) FROM {table} WHERE "
                    f"date_part('hour', CAST({time_column} AS TIMESTAMP)) = 14 AND "
                    f"date_part('minute', CAST({time_column} AS TIMESTAMP)) BETWEEN 30 AND 50"
                )
            looked[key] = {
                "table": table,
                "source": label,
                "rows": rows,
                "has_bar_time_column": time_column is not None,
                "bar_time_column": time_column,
                "tail_window_bar_rows": int(tail_bars or 0),
                "columns": sorted(columns),
            }
    usable = any(
        isinstance(info, dict) and info.get("has_bar_time_column") and info.get("rows")
        for info in looked.values()
    )
    if usable:
        return ReadinessCheck(name, STATUS_OK, looked)
    return ReadinessCheck(
        name,
        STATUS_BLOCKED,
        looked,
        "落库的分钟表只有日级聚合、没有 bar 时刻列，14:30-14:50 确认与确认后成交无法重建；"
        "尾盘策略验证记为阻塞，必须继续采集带时刻的分钟行情"
        "（scripts/sync_tail_minute_bars.py → research/minute_bar_store.py），"
        "不得用开盘回测代替",
    )


def _audit_calendar(probe: _Probe, bars_table: str | None) -> ReadinessCheck:
    """日历完备性：用日线实际交易日做基准，并报告周末泄漏（应为 0）。"""
    name = "trade_calendar_consistency"
    if bars_table is None:
        return ReadinessCheck(name, STATUS_BLOCKED, {}, "无日线表")
    columns = probe.columns(bars_table)
    date_column = _pick(columns, ("date", "trade_date"))
    if date_column is None:
        return ReadinessCheck(name, STATUS_BLOCKED, {}, "日线表无日期列")
    weekend_days = probe.rows(
        f"SELECT DISTINCT {date_column} FROM {bars_table} "
        "WHERE dayofweek(CAST(" + date_column + " AS DATE)) IN (0, 6) LIMIT 10"
    )
    holiday_2026 = probe.rows(
        f"SELECT DISTINCT {date_column} FROM {bars_table} WHERE "
        "CAST(" + date_column + " AS DATE) BETWEEN DATE '2026-10-01' AND DATE '2026-10-08' "
        "ORDER BY 1"
    )
    total_days = int(probe.scalar(
        f"SELECT COUNT(DISTINCT {date_column}) FROM {bars_table}"
    ) or 0)
    detail = {
        "distinct_trade_days": total_days,
        "weekend_leakage": [str(row[0]) for row in weekend_days],
        "national_day_window": [str(row[0]) for row in holiday_2026],
    }
    if weekend_days:
        return ReadinessCheck(
            name, STATUS_INSUFFICIENT, detail,
            "周末出现日线：日历被当成'非节假日的工作日即开市'，跨年样本会错位",
        )
    return ReadinessCheck(name, STATUS_OK, detail)


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def readiness_exit_code(report: Mapping[str, Any]) -> int:
    """0 就绪 / 3 数据不足 / 5 阻塞。真实退出码，不靠文档声明。"""
    readiness = str(report.get("readiness") or "")
    if readiness == READINESS_BLOCKED:
        return 5
    if readiness == READINESS_INSUFFICIENT:
        return 3
    return 0


def format_blocking_gaps(report: Mapping[str, Any]) -> str:
    checks = {str(item["name"]): item for item in report.get("checks", [])}
    lines: list[str] = []
    for name in report.get("blocking_gaps", []):
        check = checks.get(str(name), {})
        note = str(check.get("note") or "") if isinstance(check, dict) else ""
        lines.append(f"BLOCKED {name}: {note}")
    for name in report.get("insufficient_items", []):
        check = checks.get(str(name), {})
        note = str(check.get("note") or "") if isinstance(check, dict) else ""
        lines.append(f"INSUFFICIENT {name}: {note}")
    return "\n".join(lines)


__all__ = [
    "BAR_TIME_COLUMNS",
    "MIN_DAILY_ROWS_PER_DAY",
    "MIN_INDEX_CONTINUITY",
    "MIN_LIMIT_PRICE_COVERAGE",
    "MIN_TAIL_BARS_PER_DAY",
    "MIN_TRADE_STATUS_COVERAGE",
    "READINESS_BLOCKED",
    "READINESS_INSUFFICIENT",
    "READINESS_READY",
    "STATUS_BLOCKED",
    "STATUS_INSUFFICIENT",
    "STATUS_OK",
    "ReadinessCheck",
    "audit_trend_data_readiness",
    "format_blocking_gaps",
    "readiness_exit_code",
]
