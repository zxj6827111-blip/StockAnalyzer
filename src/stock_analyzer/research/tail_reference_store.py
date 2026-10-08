"""尾盘链路的研究侧参考数据库（改进计划 §3.1"补齐并校验…在独立研究库补齐后验证"）。

现状：``research/minute_bar_store.py`` 只建分钟表，而交易日历 / RAW 日线 / 精确涨跌停 /
停复牌 / 证券历史状态的写入**全在生产仓库**（``data/market_warehouse.py``）。于是
"补齐后验证"没有独立落点：历史重建只能读生产库，涨跌停与停牌这些买入/卖出硬门
也就无法在研究侧自证。本模块把那五类参考数据按**契约要吃的形状**复制进独立研究库，
只读生产库、绝不写它。

四条写进代码的约束：

1. 日线口径必须显式声明且只接受 ``raw``。用 QFQ 价格模拟真实成交是 ADR-002 明令
   禁止的，这里直接 raise 而不是告警。
2. 每个来源都带 ``source`` / ``as_of`` / ``coverage_complete``。生产库里没有这张表时
   报 ``source_table_missing`` 并保留为空，**不补 0、不补行、不猜**
   （§3.1"数据无法证实时标记为不足"）。
3. 写入按主键 ``INSERT OR REPLACE``：重复同步是幂等的，不会积累重复行。
4. 涨跌停只认落库口径；比例近似出来的上下限必须显式 ``approximated=True``，
   读侧默认拒绝（``limit_prices_require_exact``）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.data.trading_calendar import is_open_trading_date
from stock_analyzer.research.minute_bar_store import PRICE_BASES, PRICE_BASIS_RAW

#: 研究库里参考数据的表名。刻意加 ``ref_`` 前缀，避免与生产仓库同名造成误读。
REFERENCE_TABLES: dict[str, str] = {
    "trade_calendar": "ref_trade_calendar",
    "daily_bars": "ref_daily_bars_raw",
    "limit_prices": "ref_limit_prices",
    "suspend_status": "ref_suspend_status",
    "security_status": "ref_security_status",
}

#: 生产仓库里对应的来源表（``data/market_warehouse.py`` 的 DDL）。
WAREHOUSE_TABLES: dict[str, str] = {
    "daily_bars": "daily_bars",
    "daily_trade_status": "daily_trade_status",
    "security_status": "security_status",
}

SOURCE_WAREHOUSE = "market_warehouse"
SOURCE_WAREHOUSE_DERIVED = "market_warehouse_daily_bars_derived"
SOURCE_STATIC_CALENDAR = "data.trading_calendar"
#: vendor 离线包的全A日K 走的是**独立来源声明**：生产仓库 ``price_series_mode``
#: 为 NULL 时无法声明口径，而这条来源的口径由包本身的结构保证（日K 是原始价，
#: 复权因子单独成包），所以必须能和研究库里的仓库副本区分开。
SOURCE_VENDOR_ZIP_DAILY = "vendor_zip_daily_raw"

#: 默认与分钟库同文件：尾盘重建要同时读分钟 bar 与这些日级参考数据。
REFERENCE_DB_DEFAULT = "artifacts/research/tail_minute_bars.duckdb"

_INGESTED = "ingested_at"


class TailReferenceError(RuntimeError):
    """参考数据自身的契约不成立（口径未声明、必需列缺失、上下限不精确）。"""


def _ddl(table: str) -> str:
    if table == REFERENCE_TABLES["trade_calendar"]:
        return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                trade_date DATE NOT NULL,
                is_open BOOLEAN NOT NULL,
                exchange VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                {_INGESTED} TIMESTAMP NOT NULL,
                PRIMARY KEY (trade_date, exchange)
            )
        """
    if table == REFERENCE_TABLES["daily_bars"]:
        return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                symbol VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                volume DOUBLE, turnover DOUBLE, float_market_cap DOUBLE,
                name VARCHAR, is_st BOOLEAN, is_delisting_risk BOOLEAN,
                board VARCHAR,
                price_basis VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                {_INGESTED} TIMESTAMP NOT NULL,
                PRIMARY KEY (symbol, trade_date)
            )
        """
    if table == REFERENCE_TABLES["limit_prices"]:
        return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                symbol VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                up_limit DOUBLE, down_limit DOUBLE,
                approximated BOOLEAN NOT NULL,
                source VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                coverage_complete BOOLEAN NOT NULL,
                {_INGESTED} TIMESTAMP NOT NULL,
                PRIMARY KEY (symbol, trade_date)
            )
        """
    if table == REFERENCE_TABLES["suspend_status"]:
        return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                symbol VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                suspended BOOLEAN NOT NULL,
                suspend_type VARCHAR,
                trade_status VARCHAR,
                source VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                coverage_complete BOOLEAN NOT NULL,
                {_INGESTED} TIMESTAMP NOT NULL,
                PRIMARY KEY (symbol, trade_date)
            )
        """
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            symbol VARCHAR NOT NULL,
            effective_from DATE NOT NULL,
            effective_to DATE,
            status_type VARCHAR NOT NULL,
            status_value VARCHAR,
            board VARCHAR,
            exchange VARCHAR,
            source VARCHAR NOT NULL,
            as_of VARCHAR NOT NULL,
            coverage_complete BOOLEAN NOT NULL,
            {_INGESTED} TIMESTAMP NOT NULL,
            PRIMARY KEY (symbol, effective_from, status_type)
        )
    """


def _day(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _flag(value: Any) -> bool:
    return bool(value) and str(value).lower() not in ("false", "0", "none", "")


def _provenance(item: Mapping[Any, Any], default: str, *, field: str = "source") -> str:
    """行级 source / as_of 优先：仓库那一行自带的口径比调用方给的默认值更具体。"""
    value = str(item.get(field) or "").strip()
    return value or str(default).strip()


def _coverage(item: Mapping[Any, Any], fallback: bool) -> bool:
    """仓库带了 ``coverage_complete`` 就用它；没带才用行内可判定的回退值。"""
    if "coverage_complete" in item:
        return _flag(item.get("coverage_complete"))
    return fallback


class TailReferenceStore:
    """研究侧参考数据：写进去的形状就是尾盘标签与历史重建要吃的形状。"""

    def __init__(self, path: Path | str | None = None) -> None:
        import duckdb

        self.path = Path(path or REFERENCE_DB_DEFAULT)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.path))
        for table in REFERENCE_TABLES.values():
            self._con.execute(_ddl(table))

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> TailReferenceStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 写入 ----------------------------------------------------------------

    def _write(self, table: str, rows: list[tuple[Any, ...]], columns: Sequence[str]) -> int:
        if not rows:
            return 0
        placeholders = ", ".join("?" for _ in columns)
        self._con.executemany(
            f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            rows,
        )
        return len(rows)

    def upsert_calendar(
        self,
        trade_dates: Iterable[Any],
        *,
        exchange: str = "SSE",
        source: str = SOURCE_STATIC_CALENDAR,
        as_of: str,
    ) -> int:
        """日历行必须同时带上"派生自哪张表"和"权威日历说这天开不开市"。

        只用行情派生日期当日历，等于让数据自己证明自己完整；这里把
        ``data/trading_calendar.is_open_trading_date`` 的判定并排放进来，冲突会在
        ``calendar_conflicts()`` 里暴露成缺口而不是被静默吸收。
        """
        if not str(as_of or "").strip():
            raise TailReferenceError("calendar rows need an explicit as_of")
        now = datetime.now()
        rows: list[tuple[Any, ...]] = []
        for value in trade_dates:
            day = _day(value)
            if day is None:
                continue
            rows.append((day, is_open_trading_date(day), str(exchange), str(source),
                         str(as_of), now))
        return self._write(
            REFERENCE_TABLES["trade_calendar"], rows,
            ("trade_date", "is_open", "exchange", "source", "as_of", _INGESTED),
        )

    def upsert_daily_bars(
        self,
        frame: pd.DataFrame,
        *,
        price_basis: str,
        source: str = SOURCE_WAREHOUSE,
        as_of: str,
    ) -> int:
        """只接受显式声明为 raw 的日线；QFQ 直接 raise（ADR-002）。"""
        if str(price_basis) not in PRICE_BASES:
            raise TailReferenceError(
                f"price_basis must be declared as one of {PRICE_BASES}, got {price_basis!r}"
            )
        if str(price_basis) != PRICE_BASIS_RAW:
            raise TailReferenceError(
                "execution and labels must not be rebuilt on adjusted prices; "
                f"refusing price_basis={price_basis!r} (raw only)"
            )
        if not str(as_of or "").strip():
            raise TailReferenceError("daily bar rows need an explicit as_of")
        required = ("symbol", "date", "open", "high", "low", "close")
        missing = [name for name in required if name not in frame.columns]
        if missing:
            raise TailReferenceError(f"daily bar frame is missing column(s) {missing}")
        now = datetime.now()
        rows: list[tuple[Any, ...]] = []
        for item in frame.to_dict("records"):
            day = _day(item.get("date"))
            symbol = str(item.get("symbol") or "").strip()
            if day is None or not symbol:
                continue
            rows.append((
                symbol, day,
                _number(item.get("open")), _number(item.get("high")),
                _number(item.get("low")), _number(item.get("close")),
                _number(item.get("volume")), _number(item.get("turnover")),
                _number(item.get("float_market_cap")),
                str(item.get("name") or "") or None,
                _optional_flag(item.get("is_st")), _optional_flag(item.get("is_delisting_risk")),
                str(item.get("board") or "") or None,
                PRICE_BASIS_RAW, _provenance(item, source),
                _provenance(item, str(as_of), field="as_of"), now,
            ))
        return self._write(
            REFERENCE_TABLES["daily_bars"], rows,
            ("symbol", "trade_date", "open", "high", "low", "close", "volume", "turnover",
             "float_market_cap", "name", "is_st", "is_delisting_risk", "board",
             "price_basis", "source", "as_of", _INGESTED),
        )

    def upsert_limit_prices(
        self,
        frame: pd.DataFrame,
        *,
        source: str = SOURCE_WAREHOUSE,
        as_of: str,
        approximated: bool = False,
    ) -> int:
        """精确涨跌停（tushare ``stk_limit``，doc_id=183）落库口径。

        ``approximated=True`` 表示上下限是按涨跌停比例推算的，不是申报值：
        这类行照样留档，但 ``execution_inputs()`` 默认不吃它。
        """
        if not str(as_of or "").strip():
            raise TailReferenceError("limit price rows need an explicit as_of")
        missing = [name for name in ("symbol", "trade_date") if name not in frame.columns]
        if missing:
            raise TailReferenceError(f"limit price frame is missing column(s) {missing}")
        now = datetime.now()
        rows: list[tuple[Any, ...]] = []
        for item in frame.to_dict("records"):
            day = _day(item.get("trade_date"))
            symbol = str(item.get("symbol") or "").strip()
            if day is None or not symbol:
                continue
            up = _number(item.get("up_limit"))
            down = _number(item.get("down_limit"))
            rows.append((symbol, day, up, down, bool(approximated), _provenance(item, source),
                         _provenance(item, str(as_of), field="as_of"),
                         _coverage(item, up is not None and down is not None), now))
        return self._write(
            REFERENCE_TABLES["limit_prices"], rows,
            ("symbol", "trade_date", "up_limit", "down_limit", "approximated", "source",
             "as_of", "coverage_complete", _INGESTED),
        )

    def upsert_suspend_status(
        self,
        frame: pd.DataFrame,
        *,
        source: str = SOURCE_WAREHOUSE,
        as_of: str,
    ) -> int:
        """停复牌（tushare ``suspend_d``，doc_id=214）+ 交易状态声明。

        ``trade_status`` 缺就留 NULL：§3.3 要求"未知交易状态不得生成已实现盈亏标签"，
        补成 ``normal`` 就是把未知说成可交易。
        """
        if not str(as_of or "").strip():
            raise TailReferenceError("suspend rows need an explicit as_of")
        missing = [name for name in ("symbol", "trade_date", "suspended")
                   if name not in frame.columns]
        if missing:
            raise TailReferenceError(f"suspend frame is missing column(s) {missing}")
        now = datetime.now()
        rows: list[tuple[Any, ...]] = []
        for item in frame.to_dict("records"):
            day = _day(item.get("trade_date"))
            symbol = str(item.get("symbol") or "").strip()
            if day is None or not symbol:
                continue
            status = str(item.get("trade_status") or "").strip() or None
            rows.append((
                symbol, day, _flag(item.get("suspended")),
                str(item.get("suspend_type") or "") or None, status,
                _provenance(item, source), _provenance(item, str(as_of), field="as_of"),
                _coverage(item, status is not None), now,
            ))
        return self._write(
            REFERENCE_TABLES["suspend_status"], rows,
            ("symbol", "trade_date", "suspended", "suspend_type", "trade_status", "source",
             "as_of", "coverage_complete", _INGESTED),
        )

    def upsert_security_status(
        self,
        frame: pd.DataFrame,
        *,
        source: str = SOURCE_WAREHOUSE,
        as_of: str,
    ) -> int:
        """证券历史状态是**带日期的区间**，压成当日布尔值就丢了 ST/退市风险的时点。"""
        if not str(as_of or "").strip():
            raise TailReferenceError("security status rows need an explicit as_of")
        missing = [name for name in ("symbol", "effective_from", "status_type")
                   if name not in frame.columns]
        if missing:
            raise TailReferenceError(f"security status frame is missing column(s) {missing}")
        now = datetime.now()
        rows: list[tuple[Any, ...]] = []
        for item in frame.to_dict("records"):
            start = _day(item.get("effective_from"))
            symbol = str(item.get("symbol") or "").strip()
            status_type = str(item.get("status_type") or "").strip()
            if start is None or not symbol or not status_type:
                continue
            rows.append((
                symbol, start, _day(item.get("effective_to")), status_type,
                str(item.get("status_value") or "") or None,
                str(item.get("board") or "") or None,
                str(item.get("exchange") or "") or None,
                _provenance(item, source), _provenance(item, str(as_of), field="as_of"),
                _coverage(item, True), now,
            ))
        return self._write(
            REFERENCE_TABLES["security_status"], rows,
            ("symbol", "effective_from", "effective_to", "status_type", "status_value",
             "board", "exchange", "source", "as_of", "coverage_complete", _INGESTED),
        )

    # -- 读取 ----------------------------------------------------------------

    def _scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        row = self._con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self._con.execute(sql, params or [])
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def calendar(self, start: date, end: date) -> list[date]:
        rows = self._rows(
            f"SELECT trade_date FROM {REFERENCE_TABLES['trade_calendar']} "
            "WHERE is_open AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [start, end],
        )
        out: list[date] = []
        for row in rows:
            day = _day(row["trade_date"])
            if day is not None:
                out.append(day)
        return out

    def calendar_conflicts(self) -> list[str]:
        """行情派生日期与权威日历不一致的交易日 —— 校验日历，而不是猜日历。"""
        rows = self._rows(
            f"SELECT DISTINCT b.trade_date FROM {REFERENCE_TABLES['daily_bars']} b "
            f"LEFT JOIN {REFERENCE_TABLES['trade_calendar']} c "
            "ON b.trade_date = c.trade_date "
            "WHERE c.trade_date IS NULL OR NOT c.is_open"
        )
        return sorted(str(_day(row["trade_date"])) for row in rows)

    def execution_inputs(
        self,
        symbol: str,
        trade_date: date,
        *,
        limit_prices_require_exact: bool = True,
    ) -> dict[str, Any]:
        """买卖判定要吃的四件事：OHLCV、上下限、停牌标记、交易状态。

        缺就是 ``None`` 并写进 ``missing``，不填 0、不填 ``normal``。
        """
        bars = self._rows(
            f"SELECT open, high, low, close, volume, turnover, float_market_cap, "
            f"is_st, is_delisting_risk, board, price_basis "
            f"FROM {REFERENCE_TABLES['daily_bars']} WHERE symbol = ? AND trade_date = ?",
            [str(symbol), trade_date],
        )
        limits = self._rows(
            f"SELECT up_limit, down_limit, approximated FROM {REFERENCE_TABLES['limit_prices']} "
            "WHERE symbol = ? AND trade_date = ?",
            [str(symbol), trade_date],
        )
        suspends = self._rows(
            f"SELECT suspended, suspend_type, trade_status "
            f"FROM {REFERENCE_TABLES['suspend_status']} WHERE symbol = ? AND trade_date = ?",
            [str(symbol), trade_date],
        )
        bar = bars[0] if bars else {}
        limit = limits[0] if limits else {}
        status = suspends[0] if suspends else {}
        approximated = bool(limit.get("approximated"))
        usable_limits = {} if (approximated and limit_prices_require_exact) else limit
        missing = [
            name for name, present in (
                ("daily_bar", bool(bars)),
                ("limit_prices", bool(limits)
                 and not (approximated and limit_prices_require_exact)),
                ("suspend_status", bool(suspends)),
                ("trade_status", status.get("trade_status") is not None),
            ) if not present
        ]
        return {
            "symbol": str(symbol),
            "trade_date": trade_date.isoformat(),
            "open": bar.get("open"), "high": bar.get("high"), "low": bar.get("low"),
            "close": bar.get("close"), "volume": bar.get("volume"),
            "turnover": bar.get("turnover"), "float_market_cap": bar.get("float_market_cap"),
            "is_st": bar.get("is_st"), "is_delisting_risk": bar.get("is_delisting_risk"),
            "board": bar.get("board"),
            "price_basis": bar.get("price_basis"),
            "up_limit": usable_limits.get("up_limit"),
            "down_limit": usable_limits.get("down_limit"),
            "limit_prices_approximated": approximated,
            "suspended": status.get("suspended"),
            "suspend_type": status.get("suspend_type"),
            "trade_status": status.get("trade_status"),
            "missing": missing,
            "sufficient": not missing,
        }

    def day_limits(
        self,
        symbol: str,
        trade_date: date,
        *,
        limit_prices_require_exact: bool = True,
    ) -> dict[str, Any]:
        """``MinuteBarStore.bars_for(day_limits=...)`` 要的那一份日级权威字段。

        只返回非空键：分钟源自己没有 ``up_limit`` / 状态列，缺的字段必须由契约判成
        "无有效价格 / 未知状态"，不能在这一层补成看起来可用的值。
        """
        inputs = self.execution_inputs(
            symbol, trade_date, limit_prices_require_exact=limit_prices_require_exact
        )
        return {
            key: inputs[key]
            for key in ("up_limit", "down_limit", "suspended", "suspend_type", "trade_status")
            if inputs.get(key) is not None
        }

    def daily_bar_series(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        limit_prices_require_exact: bool = True,
    ) -> dict[str, Any]:
        """出场模拟要吃的 ``[(日期, raw 日线)]``，逐日带上精确上下限与状态声明。

        这里不跳过也不粉饰任何缺口，因为两种缺法后果完全不同：

        * 日历说这天开市、日线却没有 → ``missing_session_days``。少了中间一天，
          "第 5 个交易日退出"就落在错误的日期上，而契约只看得见 bar 序列，看不见洞。
        * 停复牌表里没有这天的 ``trade_status`` → ``undeclared_status_days``。
          契约据此判未知状态、不产出已实现盈亏标签（§3.3）。注意生产仓库的
          ``daily_trade_status`` 表**没有** ``trade_status`` 列，所以从仓库同步来的
          研究库这一项通常是满的缺口 —— 这是数据来源没补齐，不是策略亏损。
        * 日历本身为空 → ``calendar_declared=False``，无法校验序列完整性。
        """
        bars = self._rows(
            f"SELECT trade_date, open, high, low, close, volume, price_basis "
            f"FROM {REFERENCE_TABLES['daily_bars']} "
            "WHERE symbol = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [str(symbol), start, end],
        )
        limits = {
            _day(row["trade_date"]): row
            for row in self._rows(
                f"SELECT trade_date, up_limit, down_limit, approximated "
                f"FROM {REFERENCE_TABLES['limit_prices']} "
                "WHERE symbol = ? AND trade_date BETWEEN ? AND ?",
                [str(symbol), start, end],
            )
            if _day(row["trade_date"]) is not None
        }
        status = {
            _day(row["trade_date"]): row
            for row in self._rows(
                f"SELECT trade_date, suspended, suspend_type, trade_status "
                f"FROM {REFERENCE_TABLES['suspend_status']} "
                "WHERE symbol = ? AND trade_date BETWEEN ? AND ?",
                [str(symbol), start, end],
            )
            if _day(row["trade_date"]) is not None
        }

        sessions: list[tuple[date, dict[str, Any]]] = []
        missing_limits_days: list[str] = []
        undeclared_status_days: list[str] = []
        for item in bars:
            day = _day(item["trade_date"])
            if day is None:
                continue
            bar: dict[str, Any] = {
                "open": item["open"], "high": item["high"], "low": item["low"],
                "close": item["close"], "volume": item["volume"],
                "price_basis": item["price_basis"],
            }
            limit = limits.get(day) or {}
            approximated = bool(limit.get("approximated"))
            usable = bool(limit) and not (approximated and limit_prices_require_exact)
            up_limit = limit.get("up_limit") if usable else None
            down_limit = limit.get("down_limit") if usable else None
            if up_limit is not None:
                bar["up_limit"] = up_limit
            if down_limit is not None:
                bar["down_limit"] = down_limit
            # 入场要 up_limit、出场要 down_limit：任一个不可用，这天就判不出涨跌停锁死。
            if up_limit is None or down_limit is None:
                missing_limits_days.append(day.isoformat())
            declared = False
            row = status.get(day)
            if row is not None:
                for key in ("suspended", "suspend_type", "trade_status"):
                    if row.get(key) is not None:
                        bar[key] = row[key]
                declared = row.get("trade_status") is not None
            if not declared:
                undeclared_status_days.append(day.isoformat())
            sessions.append((day, bar))

        expected = self.calendar(start, end)
        present = {day for day, _ in sessions}
        missing_session_days = [day.isoformat() for day in expected if day not in present]
        return {
            "symbol": str(symbol),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sessions": sessions,
            "calendar_declared": bool(expected),
            "expected_sessions": len(expected),
            "missing_session_days": missing_session_days,
            "missing_limit_price_days": missing_limits_days,
            "undeclared_status_days": undeclared_status_days,
        }

    def coverage(self) -> dict[str, Any]:
        """每个来源各报行数与日期跨度；缺表就是 0 行 + 明确的 gaps 条目。"""
        per_source: dict[str, Any] = {}
        for name, table in REFERENCE_TABLES.items():
            rows = int(self._scalar(f"SELECT COUNT(*) FROM {table}") or 0)
            entry: dict[str, Any] = {"table": table, "rows": rows}
            if table == REFERENCE_TABLES["trade_calendar"]:
                entry["open_sessions"] = int(
                    self._scalar(f"SELECT COUNT(*) FROM {table} WHERE is_open") or 0
                )
            elif table == REFERENCE_TABLES["security_status"]:
                entry["symbols"] = int(
                    self._scalar(f"SELECT COUNT(DISTINCT symbol) FROM {table}") or 0
                )
            else:
                entry["symbols"] = int(
                    self._scalar(f"SELECT COUNT(DISTINCT symbol) FROM {table}") or 0
                )
            per_source[name] = entry
        approximated = int(
            self._scalar(
                f"SELECT COUNT(*) FROM {REFERENCE_TABLES['limit_prices']} WHERE approximated"
            ) or 0
        )
        unknown_status = int(
            self._scalar(
                f"SELECT COUNT(*) FROM {REFERENCE_TABLES['suspend_status']} "
                "WHERE trade_status IS NULL"
            ) or 0
        )
        return {
            "path": str(self.path),
            "sources": per_source,
            "approximated_limit_price_rows": approximated,
            "suspend_rows_without_trade_status": unknown_status,
            "calendar_conflicts": self.calendar_conflicts(),
            "gaps": self.gaps(per_source),
        }

    @staticmethod
    def gaps(sources: Mapping[str, Any]) -> list[str]:
        found = {
            f"{name}_table_missing" for name, entry in sources.items()
            if int(entry["rows"]) == 0
        }
        return sorted(found)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_flag(value: Any) -> bool | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return _flag(value)


@dataclass(frozen=True)
class WarehouseReading:
    """从生产仓库**只读**取到的参考帧，外加"哪张表不存在"的如实记录。"""

    frames: dict[str, pd.DataFrame]
    gaps: tuple[str, ...]
    as_of: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames": {name: int(len(frame)) for name, frame in self.frames.items()},
            "gaps": list(self.gaps),
            "as_of": self.as_of,
        }


def _table_exists(con: Any, table: str) -> bool:
    return int(con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()[0] or 0) > 0


def read_warehouse_reference_frames(
    warehouse_path: Path | str,
    *,
    start: date,
    end: date,
    symbols: Sequence[str] | None = None,
) -> WarehouseReading:
    """复用现有仓库接口读五类参考数据；表不存在就报缺，不返回空帧冒充"已补齐"。"""
    import duckdb

    path = Path(warehouse_path)
    if not path.exists():
        raise TailReferenceError(f"warehouse not found: {path}")
    con = duckdb.connect(str(path), read_only=True)
    frames: dict[str, pd.DataFrame] = {}
    gaps: list[str] = []
    try:
        if _table_exists(con, WAREHOUSE_TABLES["daily_bars"]):
            where = "price_series_mode = 'raw' AND date BETWEEN ? AND ?"
            params: list[Any] = [start, end]
            if symbols:
                where += f" AND symbol IN ({', '.join('?' for _ in symbols)})"
                params.extend([str(symbol) for symbol in symbols])
            frames["daily_bars"] = con.execute(
                "SELECT symbol, date, open, high, low, close, volume, turnover, "
                "float_market_cap, name, is_st, is_delisting_risk, board "
                f"FROM {WAREHOUSE_TABLES['daily_bars']} WHERE {where}",
                params,
            ).fetch_df()
            frames["trade_dates"] = con.execute(
                f"SELECT DISTINCT date FROM {WAREHOUSE_TABLES['daily_bars']} "
                "WHERE date BETWEEN ? AND ? ORDER BY date",
                [start, end],
            ).fetch_df()
        else:
            gaps.append(f"{WAREHOUSE_TABLES['daily_bars']}_table_missing")

        if _table_exists(con, WAREHOUSE_TABLES["daily_trade_status"]):
            status = con.execute(
                "SELECT symbol, trade_date, up_limit, down_limit, suspended, suspend_type, "
                "source, as_of, coverage_complete "
                f"FROM {WAREHOUSE_TABLES['daily_trade_status']} "
                "WHERE trade_date BETWEEN ? AND ?",
                [start, end],
            ).fetch_df()
            frames["limit_prices"] = status
            frames["suspend_status"] = status
        else:
            gaps.append(f"{WAREHOUSE_TABLES['daily_trade_status']}_table_missing")
            gaps.append("suspend_status_source_table_missing")

        if _table_exists(con, WAREHOUSE_TABLES["security_status"]):
            frames["security_status"] = con.execute(
                "SELECT symbol, effective_from, effective_to, status_type, status_value, "
                "board, exchange, source, as_of, coverage_complete "
                f"FROM {WAREHOUSE_TABLES['security_status']}"
            ).fetch_df()
        else:
            gaps.append(f"{WAREHOUSE_TABLES['security_status']}_table_missing")
    finally:
        con.close()
    return WarehouseReading(
        frames=frames,
        gaps=tuple(sorted(set(gaps))),
        as_of=end.isoformat(),
    )


def sync_reference_from_warehouse(
    store: TailReferenceStore,
    reading: WarehouseReading,
    *,
    exchange: str = "SSE",
) -> dict[str, Any]:
    """把读到的帧落进研究库，逐源报告落了什么、缺了什么。"""
    landed: dict[str, int] = {}
    dates = reading.frames.get("trade_dates")
    if dates is not None and not dates.empty:
        landed["trade_calendar"] = store.upsert_calendar(
            dates["date"].tolist(),
            exchange=exchange,
            source=SOURCE_WAREHOUSE_DERIVED,
            as_of=reading.as_of,
        )
    bars = reading.frames.get("daily_bars")
    if bars is not None and not bars.empty:
        # 仓库里的 raw 日线口径由 price_series_mode='raw' 过滤保证，这里显式声明。
        landed["daily_bars"] = store.upsert_daily_bars(bars, price_basis=PRICE_BASIS_RAW,
                                                       as_of=reading.as_of)
    status = reading.frames.get("limit_prices")
    if status is not None and not status.empty:
        landed["limit_prices"] = store.upsert_limit_prices(status, as_of=reading.as_of)
        landed["suspend_status"] = store.upsert_suspend_status(status, as_of=reading.as_of)
    security = reading.frames.get("security_status")
    if security is not None and not security.empty:
        landed["security_status"] = store.upsert_security_status(security, as_of=reading.as_of)
    coverage = store.coverage()
    return {
        "landed_rows": landed,
        "read_gaps": list(reading.gaps),
        "coverage": coverage,
        "sufficient_sources": sorted(
            name for name in REFERENCE_TABLES
            if int(coverage["sources"][name]["rows"]) > 0
        ),
        "missing_sources": coverage["gaps"],
    }


def _scaled(value: Any, factor: float) -> float | None:
    """按声明的倍率换算数量；空值保持空，不折算成 0。"""
    number = _number(value)
    return None if number is None else number * float(factor)


def read_vendor_daily_raw_frames(
    root: Path | str,
    *,
    start: date,
    end: date,
    symbols: Sequence[str] | None = None,
    daily_dir_name: str = "全A日K",
) -> pd.DataFrame:
    """从 vendor 离线包的**全A日K** 读未复权日线（改进计划 §3.1「补齐 RAW 行情」）。

    口径不靠推断：这个包的日K 就是原始价，复权因子单独成包
    （``复权因子/复权因子_前复权.zip``），所以这里不存在"这批行到底是 RAW 还是 QFQ"
    的声明缺口 —— 而生产仓库 ``price_series_mode`` 为 NULL 时缺的正是这一条。
    复用 ``build_vendor_zip_daily_index`` 做归档/成员发现（含同名包去重），
    数量口径沿用 :class:`VendorZipOverlayProvider` 的类默认值而不是另立常数：
    volume 手→股 ×100、amount 千元→元 ×1000、circ_mv 万元→元 ×10000。

    ``is_st`` / ``is_delisting_risk`` 在这个源里**没有声明**，一律留 None：
    把"没声明"写成"不是 ST"就是拿缺数据冒充有效信息。
    """
    import dataclasses
    import zipfile

    from stock_analyzer.data.vendor_zip_overlay import (
        VendorZipOverlayProvider,
        build_vendor_zip_daily_index,
    )

    source_root = Path(root).expanduser().resolve()
    index = build_vendor_zip_daily_index(root=source_root, daily_dir_name=daily_dir_name)
    wanted = (
        {str(item).strip().split(".")[0].zfill(6) for item in symbols if str(item).strip()}
        if symbols
        else None
    )
    # ``VendorZipOverlayProvider`` 是 slots dataclass：类属性是 member_descriptor，
    # 默认值只能从 fields() 取 —— 倍率仍以那里的声明为唯一出处，不在本模块重抄一遍。
    provider_defaults = {
        field.name: field.default for field in dataclasses.fields(VendorZipOverlayProvider)
    }
    volume_multiplier = float(provider_defaults["daily_volume_multiplier"])
    turnover_multiplier = float(provider_defaults["daily_turnover_multiplier"])
    rows: list[dict[str, Any]] = []
    for raw_symbol, record in dict(index.get("symbols") or {}).items():
        symbol = str(raw_symbol).strip().split(".")[0].zfill(6)
        if wanted is not None and symbol not in wanted:
            continue
        for item in list((record or {}).get("entries") or []):
            archive_path = source_root / str(item.get("zip", ""))
            entry_name = str(item.get("entry", ""))
            if not archive_path.exists() or not entry_name:
                raise TailReferenceError(
                    f"vendor daily archive unreadable: {archive_path}!{entry_name}"
                )
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    with archive.open(entry_name) as stream:
                        frame = pd.read_csv(stream, encoding="gbk")
            except (KeyError, OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
                raise TailReferenceError(
                    f"vendor daily entry unreadable: {archive_path}!{entry_name}: "
                    f"{type(exc).__name__}"
                ) from exc
            frame.columns = [
                str(name).lstrip("\ufeff").strip().lower() for name in frame.columns
            ]
            day_column = next(
                (name for name in ("datetime", "trade_date", "date") if name in frame.columns),
                "",
            )
            if not day_column:
                raise TailReferenceError(
                    f"vendor daily file has no date column: {archive_path}!{entry_name}"
                )
            days = pd.to_datetime(frame[day_column], errors="coerce")
            selected = frame.loc[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))]
            if selected.empty:
                continue
            selected = selected.assign(date=days.loc[selected.index].dt.date)
            for item_row in selected.to_dict("records"):
                rows.append({
                    "symbol": symbol,
                    "date": item_row["date"],
                    "open": _number(item_row.get("open")),
                    "high": _number(item_row.get("high")),
                    "low": _number(item_row.get("low")),
                    "close": _number(item_row.get("close")),
                    "volume": _scaled(item_row.get("volume"), volume_multiplier),
                    "turnover": _scaled(item_row.get("amount"), turnover_multiplier),
                    "float_market_cap": _scaled(item_row.get("circ_mv"), 10_000.0),
                    "name": None,
                    "is_st": None,
                    "is_delisting_risk": None,
                    "board": None,
                    "source": SOURCE_VENDOR_ZIP_DAILY,
                })
    return pd.DataFrame(rows)


__all__ = [
    "PRICE_BASIS_RAW",
    "REFERENCE_DB_DEFAULT",
    "REFERENCE_TABLES",
    "SOURCE_STATIC_CALENDAR",
    "SOURCE_WAREHOUSE",
    "SOURCE_WAREHOUSE_DERIVED",
    "WAREHOUSE_TABLES",
    "TailReferenceError",
    "TailReferenceStore",
    "WarehouseReading",
    "read_warehouse_reference_frames",
    "sync_reference_from_warehouse",
]
