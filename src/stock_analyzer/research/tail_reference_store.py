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

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.data.trading_calendar import is_open_trading_date
from stock_analyzer.research.minute_bar_store import PRICE_BASES, PRICE_BASIS_RAW
from stock_analyzer.research.suspension_evidence import (
    SSE_OFFICIAL_SUSPENSION_SOURCE,
    is_verified_full_day_suspension,
    is_verified_no_trade_adj_factor_gap,
)

#: 研究库里参考数据的表名。刻意加 ``ref_`` 前缀，避免与生产仓库同名造成误读。
REFERENCE_TABLES: dict[str, str] = {
    "trade_calendar": "ref_trade_calendar",
    "daily_bars": "ref_daily_bars_raw",
    "limit_prices": "ref_limit_prices",
    "suspend_status": "ref_suspend_status",
    "suspend_evidence": "ref_suspend_evidence",
    "security_status": "ref_security_status",
    "corporate_actions": "ref_corporate_actions",
    "corporate_action_coverage": "ref_corporate_action_coverage",
}

ADJ_FACTOR_TABLE = "ref_adj_factor_history"

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
    if table == REFERENCE_TABLES["suspend_evidence"]:
        return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                symbol VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                source VARCHAR NOT NULL,
                evidence_url VARCHAR NOT NULL,
                evidence_title VARCHAR NOT NULL,
                evidence_document_date DATE NOT NULL,
                evidence_sha256 VARCHAR NOT NULL,
                evidence_full_day BOOLEAN NOT NULL,
                evidence_as_of VARCHAR NOT NULL,
                raw_payload JSON NOT NULL,
                {_INGESTED} TIMESTAMP NOT NULL,
                PRIMARY KEY (symbol, trade_date, source)
            )
        """
    if table == REFERENCE_TABLES["corporate_actions"]:
        return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                action_id VARCHAR PRIMARY KEY,
                symbol VARCHAR NOT NULL,
                report_end_date DATE,
                announcement_date DATE,
                implementation_announcement_date DATE,
                record_date DATE,
                ex_date DATE NOT NULL,
                pay_date DATE,
                stock_listing_date DATE,
                implementation_status VARCHAR,
                cash_dividend_per_share_after_tax DOUBLE,
                cash_dividend_per_share_before_tax DOUBLE,
                stock_distribution_per_share DOUBLE,
                bonus_shares_per_share DOUBLE,
                transfer_shares_per_share DOUBLE,
                source VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                coverage_complete BOOLEAN NOT NULL,
                raw_payload JSON NOT NULL,
                {_INGESTED} TIMESTAMP NOT NULL
            )
        """
    if table == REFERENCE_TABLES["corporate_action_coverage"]:
        return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                ex_date DATE NOT NULL,
                source VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                rows_returned INTEGER NOT NULL,
                coverage_complete BOOLEAN NOT NULL,
                truncated BOOLEAN NOT NULL,
                error VARCHAR,
                response_sha256 VARCHAR,
                {_INGESTED} TIMESTAMP NOT NULL,
                PRIMARY KEY (ex_date, source)
            )
        """
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            symbol VARCHAR NOT NULL,
            effective_from DATE NOT NULL,
            effective_to DATE,
            announcement_date DATE,
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


def _corporate_action_economic_key(action: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the complete economic identity used to collapse exact vendor duplicates.

    ``ann_date`` is intentionally excluded: Tushare can return both an earlier plan row
    and a later implementation row for one action. The action dates, record date, status,
    cash/share amounts, listing date, and coverage must all agree before those rows can
    be treated as duplicate disclosures.
    """
    date_fields = (
        "report_end_date",
        "record_date",
        "ex_date",
        "pay_date",
        "stock_listing_date",
    )
    number_fields = (
        "cash_dividend_per_share_after_tax",
        "cash_dividend_per_share_before_tax",
        "stock_distribution_per_share",
        "bonus_shares_per_share",
        "transfer_shares_per_share",
    )
    dates = tuple(
        _day(action.get(name)).isoformat() if _day(action.get(name)) is not None else None
        for name in date_fields
    )
    numbers = tuple(
        round(value, 12) if (value := _number(action.get(name))) is not None else None
        for name in number_fields
    )
    return (
        str(action.get("symbol") or ""),
        *dates,
        str(action.get("implementation_status") or ""),
        *numbers,
        bool(action.get("coverage_complete")),
    )


def _deduplicate_identical_corporate_actions(
    action_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse only exact economic duplicates and return a visible alias audit."""
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for raw in action_rows:
        action = dict(raw)
        grouped.setdefault(_corporate_action_economic_key(action), []).append(action)

    unique: list[dict[str, Any]] = []
    duplicate_groups: list[dict[str, Any]] = []
    for rows in grouped.values():
        # Prefer the latest disclosure, then the latest implementation notice. The
        # economic key already guarantees that these dates cannot change the action.
        rows.sort(
            key=lambda row: (
                _day(row.get("announcement_date")) or date.min,
                _day(row.get("implementation_announcement_date")) or date.min,
                str(row.get("action_id") or ""),
            ),
        )
        canonical = rows[-1]
        unique.append(canonical)
        if len(rows) == 1:
            continue
        aliases = [str(row.get("action_id") or "") for row in rows[:-1]]
        canonical["duplicate_action_ids"] = aliases
        duplicate_groups.append(
            {
                "canonical_action_id": str(canonical.get("action_id") or ""),
                "duplicate_action_ids": aliases,
                "symbol": str(canonical.get("symbol") or ""),
                "ex_date": (
                    _day(canonical.get("ex_date")).isoformat()
                    if _day(canonical.get("ex_date")) is not None
                    else ""
                ),
                "collapsed_count": len(rows) - 1,
                "reason": "exact_economic_identity_across_disclosure_rows",
            }
        )
    unique.sort(
        key=lambda row: (
            _day(row.get("ex_date")) or date.min,
            str(row.get("action_id") or ""),
        )
    )
    duplicate_groups.sort(key=lambda row: (row["ex_date"], row["symbol"]))
    return unique, duplicate_groups


def _adj_factor_ddl() -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {ADJ_FACTOR_TABLE} (
            symbol VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            adj_factor DOUBLE,
            source VARCHAR NOT NULL,
            as_of VARCHAR NOT NULL,
            coverage_complete BOOLEAN NOT NULL,
            response_sha256 VARCHAR,
            {_INGESTED} TIMESTAMP NOT NULL,
            PRIMARY KEY (symbol, trade_date)
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
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError:
        return None


def _research_symbol(value: Any) -> str:
    """Match research tables' bare-code convention while accepting Tushare ts_code.

    Daily/minute research stores use six-digit codes (for example ``600000``), while
    Tushare returns ``600000.SH``. Keep the full source row in ``raw_payload``; only the
    join key is normalized.
    """
    text = str(value or "").strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _flag(value: Any) -> bool:
    return bool(value) and str(value).lower() not in ("false", "0", "none", "")


def _nullable_text(value: Any) -> str | None:
    """Normalize CSV / pandas nulls before turning optional text into a string."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


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
        # 旧研究库需要保留原内容并增量迁移；PIT 公告时间缺失时由就绪审计阻断。
        self._con.execute(
            f"ALTER TABLE {REFERENCE_TABLES['security_status']} "
            "ADD COLUMN IF NOT EXISTS announcement_date DATE"
        )
        self._con.execute(_adj_factor_ddl())

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
        # DuckDB 的 executemany 在 Python 层逐行绑定参数：补采到的 stk_limit 只需
        # 2.9 万行就要几十秒，全市场 77.6 万行则直接跑不完。改成注册临时帧再批量 INSERT，
        # 列名仍由调用方显式给出，去重语义保持 INSERT OR REPLACE。
        staging_view = f"staging_{table}"
        staged = pd.DataFrame(rows, columns=list(columns))
        self._con.register(staging_view, staged)
        try:
            self._con.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                f"SELECT {', '.join(columns)} FROM {staging_view}"
            )
        finally:
            self._con.unregister(staging_view)
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
            rows.append(
                (day, is_open_trading_date(day), str(exchange), str(source), str(as_of), now)
            )
        return self._write(
            REFERENCE_TABLES["trade_calendar"],
            rows,
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
            rows.append(
                (
                    symbol,
                    day,
                    _number(item.get("open")),
                    _number(item.get("high")),
                    _number(item.get("low")),
                    _number(item.get("close")),
                    _number(item.get("volume")),
                    _number(item.get("turnover")),
                    _number(item.get("float_market_cap")),
                    str(item.get("name") or "") or None,
                    _optional_flag(item.get("is_st")),
                    _optional_flag(item.get("is_delisting_risk")),
                    str(item.get("board") or "") or None,
                    PRICE_BASIS_RAW,
                    _provenance(item, source),
                    _provenance(item, str(as_of), field="as_of"),
                    now,
                )
            )
        return self._write(
            REFERENCE_TABLES["daily_bars"],
            rows,
            (
                "symbol",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "turnover",
                "float_market_cap",
                "name",
                "is_st",
                "is_delisting_risk",
                "board",
                "price_basis",
                "source",
                "as_of",
                _INGESTED,
            ),
        )

    def suspended_symbol_days(self) -> set[tuple[str, date]]:
        """已声明停牌的 ``(symbol, trade_date)``。

        可交易声明（由"当天确有成交的 RAW 日线"推出）不得覆盖这些行 —— 覆盖了就是
        把停牌说成能买，§3.1 的"不把缺 bar 当停牌"反过来同样成立。
        """
        rows = self._con.execute(
            f"SELECT symbol, trade_date FROM {REFERENCE_TABLES['suspend_status']} WHERE suspended"
        ).fetchall()
        declared: set[tuple[str, date]] = set()
        for symbol, day in rows:
            parsed = _day(day)
            if parsed is not None and str(symbol).strip():
                declared.add((str(symbol).strip(), parsed))
        return declared

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
            rows.append(
                (
                    symbol,
                    day,
                    up,
                    down,
                    bool(approximated),
                    _provenance(item, source),
                    _provenance(item, str(as_of), field="as_of"),
                    _coverage(item, up is not None and down is not None),
                    now,
                )
            )
        return self._write(
            REFERENCE_TABLES["limit_prices"],
            rows,
            (
                "symbol",
                "trade_date",
                "up_limit",
                "down_limit",
                "approximated",
                "source",
                "as_of",
                "coverage_complete",
                _INGESTED,
            ),
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
        missing = [
            name for name in ("symbol", "trade_date", "suspended") if name not in frame.columns
        ]
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
            rows.append(
                (
                    symbol,
                    day,
                    _flag(item.get("suspended")),
                    str(item.get("suspend_type") or "") or None,
                    status,
                    _provenance(item, source),
                    _provenance(item, str(as_of), field="as_of"),
                    _coverage(item, status is not None),
                    now,
                )
            )
        return self._write(
            REFERENCE_TABLES["suspend_status"],
            rows,
            (
                "symbol",
                "trade_date",
                "suspended",
                "suspend_type",
                "trade_status",
                "source",
                "as_of",
                "coverage_complete",
                _INGESTED,
            ),
        )

    def upsert_corporate_actions(
        self,
        frame: pd.DataFrame,
        *,
        source: str,
        as_of: str,
    ) -> int:
        """导入已实施的公司行动原始事实，单位按每股保存，保留源行供复核。

        这里不把缺失金额或比例改成 0，除非调用方携带 reconcile CLI 生成的哈希绑定官方证据。
        是否可用于账本由逐条标签门判定；来源行始终保留。
        ``action_id`` 不含数值字段，供应商更正同一行动时会按同一键替换为最新披露值。
        """
        if not str(source or "").strip() or not str(as_of or "").strip():
            raise TailReferenceError("corporate-action rows need explicit source and as_of")
        if frame is None or frame.empty:
            return 0
        rows: list[tuple[Any, ...]] = []
        now = datetime.now()
        for raw in frame.to_dict("records"):
            source_symbol = str(raw.get("ts_code") or raw.get("symbol") or "").strip()
            symbol = _research_symbol(source_symbol)
            ex_date = _day(raw.get("ex_date"))
            if not symbol or ex_date is None:
                raise TailReferenceError("corporate-action row requires symbol and ex_date")
            report_end = _day(raw.get("end_date"))
            announcement = _day(raw.get("ann_date"))
            normalized = {
                # The source identity stays fully qualified so an upsert replaces the
                # earlier qualified-key row after the research join key is normalized.
                "symbol": source_symbol.upper(),
                "end_date": raw.get("end_date"),
                "ann_date": raw.get("ann_date"),
            }
            action_id = hashlib.sha256(
                json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str).encode(
                    "utf-8"
                )
            ).hexdigest()
            row_source = str(source)
            payload = dict(raw)
            evidence_raw = _nullable_text(raw.get("official_reconciliation_evidence_json"))
            record_source = str(raw.get("record_source") or "").strip()
            if record_source:
                if record_source != "tushare_dividend_ex_date+cninfo_official" or not evidence_raw:
                    raise TailReferenceError(
                        "official corporate-action reconciliation requires "
                        "its verified evidence payload"
                    )
                try:
                    evidence = json.loads(evidence_raw)
                except json.JSONDecodeError as exc:
                    raise TailReferenceError(
                        "official corporate-action evidence JSON is malformed"
                    ) from exc
                if not isinstance(evidence, Mapping):
                    raise TailReferenceError(
                        "official corporate-action evidence payload must be an object"
                    )
                documents = evidence.get("documents")
                official = evidence.get("official_reconciliation")
                if (
                    evidence.get("source") != "cninfo_official_disclosure"
                    or not isinstance(documents, list)
                    or not documents
                    or not isinstance(official, Mapping)
                    or official.get("implementation_status") != "实施"
                ):
                    raise TailReferenceError(
                        "official corporate-action evidence payload is incomplete"
                    )
                for document in documents:
                    if not isinstance(document, Mapping):
                        raise TailReferenceError(
                            "official corporate-action document evidence is malformed"
                        )
                    evidence_hash = str(document.get("sha256") or "")
                    if len(evidence_hash) != 64 or any(
                        c not in "0123456789abcdef" for c in evidence_hash
                    ):
                        raise TailReferenceError(
                            "official corporate-action evidence has an invalid SHA-256"
                        )
                    evidence_url = str(document.get("url") or "")
                    host = (
                        evidence_url.split("/", 3)[2:3]
                        if evidence_url.startswith("https://")
                        else []
                    )
                    if not host or host[0] not in {
                        "static.cninfo.com.cn",
                        "dataclouds.cninfo.com.cn",
                    }:
                        raise TailReferenceError(
                            "official corporate-action evidence URL is not a CNINFO host"
                        )
                for source_field, official_field in (
                    ("cash_div", "cash_dividend_per_share_before_tax"),
                    ("cash_div_tax", "cash_dividend_per_share_after_tax"),
                    ("stk_div", "stock_distribution_per_share"),
                    ("stk_bo_rate", "bonus_shares_per_share"),
                    ("stk_co_rate", "transfer_shares_per_share"),
                ):
                    actual = _number(raw.get(source_field))
                    declared = _number(official.get(official_field))
                    if (
                        actual is None
                        or declared is None
                        or not math.isclose(
                            actual,
                            declared,
                            rel_tol=0.0,
                            abs_tol=1e-10,
                        )
                    ):
                        raise TailReferenceError(
                            f"official corporate-action evidence does not match {source_field}"
                        )
                payload["official_reconciliation_evidence"] = evidence
                row_source = record_source
            total_ratio = _number(raw.get("stk_div"))
            bonus_ratio = _number(raw.get("stk_bo_rate"))
            transfer_ratio = _number(raw.get("stk_co_rate"))
            # Tushare exposes the total and its two components. A missing component is
            # provably zero only when the declared total is zero or equals the other
            # declared component; otherwise preserve NULL and let the execution gate fail closed.
            if total_ratio is not None and math.isclose(total_ratio, 0.0, abs_tol=1e-12):
                bonus_ratio = transfer_ratio = 0.0
            elif total_ratio is not None and bonus_ratio is None and transfer_ratio is not None:
                if math.isclose(total_ratio, transfer_ratio, abs_tol=1e-6):
                    bonus_ratio = 0.0
            elif total_ratio is not None and transfer_ratio is None and bonus_ratio is not None:
                if math.isclose(total_ratio, bonus_ratio, abs_tol=1e-6):
                    transfer_ratio = 0.0
            rows.append(
                (
                    action_id,
                    symbol,
                    report_end,
                    announcement,
                    _day(raw.get("imp_ann_date")),
                    _day(raw.get("record_date")),
                    ex_date,
                    _day(raw.get("pay_date")),
                    _day(raw.get("div_listdate")),
                    _provenance(raw, "", field="div_proc") or None,
                    _number(raw.get("cash_div")),
                    _number(raw.get("cash_div_tax")),
                    total_ratio,
                    bonus_ratio,
                    transfer_ratio,
                    row_source,
                    str(as_of),
                    _coverage(raw, True),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                )
            )
        columns = (
            "action_id",
            "symbol",
            "report_end_date",
            "announcement_date",
            "implementation_announcement_date",
            "record_date",
            "ex_date",
            "pay_date",
            "stock_listing_date",
            "implementation_status",
            "cash_dividend_per_share_after_tax",
            "cash_dividend_per_share_before_tax",
            "stock_distribution_per_share",
            "bonus_shares_per_share",
            "transfer_shares_per_share",
            "source",
            "as_of",
            "coverage_complete",
            "raw_payload",
            _INGESTED,
        )
        return self._write(REFERENCE_TABLES["corporate_actions"], rows, columns)

    def upsert_corporate_action_coverage(
        self,
        frame: pd.DataFrame,
        *,
        source: str,
        as_of: str,
    ) -> int:
        """每个交易日都必须有一条来源覆盖记录，连零事件日也不能省略。"""
        if not str(source or "").strip() or not str(as_of or "").strip():
            raise TailReferenceError("corporate-action coverage needs explicit source and as_of")
        if frame is None or frame.empty:
            return 0
        rows: list[tuple[Any, ...]] = []
        now = datetime.now()
        for raw in frame.to_dict("records"):
            ex_date = _day(raw.get("ex_date"))
            if ex_date is None:
                raise TailReferenceError("corporate-action coverage row requires ex_date")
            coverage_as_of = str(raw.get("as_of") or as_of)
            coverage_source = str(raw.get("source") or source)
            error = _nullable_text(raw.get("error"))
            truncated = _flag(raw.get("truncated"))
            complete = _flag(raw.get("coverage_complete")) and not truncated and error is None
            rows.append(
                (
                    ex_date,
                    coverage_source,
                    coverage_as_of,
                    int(_number(raw.get("rows_returned")) or 0),
                    complete,
                    truncated,
                    error,
                    _nullable_text(raw.get("response_sha256")),
                    now,
                )
            )
        return self._write(
            REFERENCE_TABLES["corporate_action_coverage"],
            rows,
            (
                "ex_date",
                "source",
                "as_of",
                "rows_returned",
                "coverage_complete",
                "truncated",
                "error",
                "response_sha256",
                _INGESTED,
            ),
        )

    def upsert_adj_factors(self, frame: pd.DataFrame, *, source: str, as_of: str) -> int:
        """Load per-symbol factor observations used only to cross-check action coverage."""
        if not str(source or "").strip() or not str(as_of or "").strip():
            raise TailReferenceError("adjustment-factor rows need explicit source and as_of")
        if frame is None or frame.empty:
            return 0
        required = ("trade_date", "adj_factor")
        missing = [name for name in required if name not in frame.columns]
        if missing:
            raise TailReferenceError(f"adjustment-factor frame is missing column(s) {missing}")
        now = datetime.now()
        rows: list[tuple[Any, ...]] = []
        for raw in frame.to_dict("records"):
            # Prefer the fully qualified code when the source also carries it. CSV
            # readers commonly infer a bare code such as 000001 as integer 1, losing
            # leading zeroes before this normalization point.
            symbol = _research_symbol(raw.get("ts_code") or raw.get("symbol"))
            day = _day(raw.get("trade_date"))
            if not symbol or day is None:
                raise TailReferenceError("adjustment-factor row requires symbol and trade_date")
            value = _number(raw.get("adj_factor"))
            complete = _flag(raw.get("coverage_complete")) and value is not None and value > 0
            rows.append(
                (
                    symbol,
                    day,
                    value,
                    _provenance(raw, source),
                    _provenance(raw, as_of, field="as_of"),
                    complete,
                    _nullable_text(raw.get("response_sha256")),
                    now,
                )
            )
        written = self._write(
            ADJ_FACTOR_TABLE,
            rows,
            (
                "symbol",
                "trade_date",
                "adj_factor",
                "source",
                "as_of",
                "coverage_complete",
                "response_sha256",
                _INGESTED,
            ),
        )
        self._con.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{ADJ_FACTOR_TABLE}_lookup "
            f"ON {ADJ_FACTOR_TABLE} (symbol, trade_date)"
        )
        return written

    def corporate_action_window(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        """取持仓窗口公司行动，并逐开市日核对全市场采集覆盖。"""
        factor_start = start - timedelta(days=31)
        following_sessions = self.calendar(end + timedelta(days=1), end + timedelta(days=31))
        factor_validation_end = following_sessions[0] if following_sessions else end
        expected = self.calendar(start, end)
        coverage_rows = self._rows(
            f"SELECT ex_date, source, as_of, rows_returned, coverage_complete, truncated, error "
            f"FROM {REFERENCE_TABLES['corporate_action_coverage']} "
            "WHERE ex_date BETWEEN ? AND ? ORDER BY ex_date, source",
            [factor_start, factor_validation_end],
        )
        coverage_by_day = {
            _day(item["ex_date"]): item
            for item in coverage_rows
            if _day(item["ex_date"]) is not None
        }
        missing_days = [day.isoformat() for day in expected if day not in coverage_by_day]
        incomplete_days = [
            day.isoformat()
            for day in expected
            if day in coverage_by_day and not bool(coverage_by_day[day]["coverage_complete"])
        ]
        action_rows = self._rows(
            f"SELECT action_id, symbol, report_end_date, announcement_date, "
            f"implementation_announcement_date, record_date, ex_date, pay_date, "
            f"stock_listing_date, implementation_status, "
            f"cash_dividend_per_share_after_tax, cash_dividend_per_share_before_tax, "
            f"stock_distribution_per_share, bonus_shares_per_share, "
            f"transfer_shares_per_share, source, as_of, coverage_complete, raw_payload "
            f"FROM {REFERENCE_TABLES['corporate_actions']} "
            "WHERE symbol = ? AND ex_date BETWEEN ? AND ? ORDER BY ex_date, action_id",
            [_research_symbol(symbol), factor_start, factor_validation_end],
        )
        all_economic_actions, duplicate_action_groups = _deduplicate_identical_corporate_actions(
            action_rows
        )
        economic_actions = [
            action for action in all_economic_actions
            if start <= (_day(action.get("ex_date")) or date.min) <= end
        ]
        actions_by_day: dict[date, list[dict[str, Any]]] = {}
        for action in all_economic_actions:
            action_day = _day(action.get("ex_date"))
            if action_day is not None:
                actions_by_day.setdefault(action_day, []).append(action)
        factor_rows = self._rows(
            f"SELECT trade_date, adj_factor, coverage_complete FROM {ADJ_FACTOR_TABLE} "
            "WHERE symbol = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [_research_symbol(symbol), factor_start, factor_validation_end],
        )
        factors = {
            _day(item["trade_date"]): item
            for item in factor_rows
            if _day(item["trade_date"]) is not None
        }

        def valid_factor(day: date) -> float | None:
            row = factors.get(day)
            value = _number(row.get("adj_factor")) if row is not None else None
            if row is None or not bool(row["coverage_complete"]) or value is None or value <= 0:
                return None
            return float(value)

        minute_table_exists = bool(self._scalar(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'minute_bars_1min'"
        ) or 0)

        def proven_no_trade_factor_gap(day: date) -> bool:
            if day in factors:
                return False
            factor_days = sorted(factors)
            previous_days = [
                item for item in factor_days if item < day and valid_factor(item) is not None
            ]
            next_days = [
                item for item in factor_days if item > day and valid_factor(item) is not None
            ]
            if not previous_days or not next_days or not minute_table_exists:
                return False
            action_coverage = coverage_by_day.get(day)
            action_coverage_complete = bool(
                action_coverage
                and action_coverage.get("source") == "tushare_dividend_ex_date"
                and action_coverage.get("coverage_complete")
                and not action_coverage.get("truncated")
                and not str(action_coverage.get("error") or "").strip()
            )
            raw_daily_exists = bool(self._scalar(
                f"SELECT COUNT(*) FROM {REFERENCE_TABLES['daily_bars']} "
                "WHERE symbol = ? AND trade_date = ?",
                [_research_symbol(symbol), day],
            ) or 0)
            minute_bar_count = int(self._scalar(
                "SELECT COUNT(*) FROM minute_bars_1min WHERE symbol = ? AND trade_date = ?",
                [_research_symbol(symbol), day],
            ) or 0)
            return is_verified_no_trade_adj_factor_gap(
                self.verified_full_day_suspension(symbol, day),
                has_raw_daily_bar=raw_daily_exists,
                raw_minute_bar_count=minute_bar_count,
                action_coverage_complete=action_coverage_complete,
                has_corporate_action=day in actions_by_day,
                previous_factor=valid_factor(previous_days[-1]),
                next_factor=valid_factor(next_days[0]),
            )

        prior_days = self.calendar(factor_start, start - timedelta(days=1))
        prior_suspension_exempt_days = [
            day for day in prior_days
            if valid_factor(day) is None and proven_no_trade_factor_gap(day)
        ]
        prior_missing_days = [
            day for day in prior_days
            if valid_factor(day) is None and day not in prior_suspension_exempt_days
        ]
        prior_valid_days = [day for day in prior_days if valid_factor(day) is not None]
        prior_day = prior_valid_days[-1] if prior_valid_days else None
        previous_factor = valid_factor(prior_day) if prior_day is not None else None
        factor_previous_missing = not prior_valid_days or bool(prior_missing_days)
        factor_missing_days = [
            day.isoformat()
            for day in expected
            if valid_factor(day) is None
        ]
        factor_gap_exemptions: list[str] = [
            day.isoformat() for day in prior_suspension_exempt_days
        ]
        unmatched_factor_changes: list[str] = []
        actions_without_factor_change: list[str] = []
        if factor_previous_missing:
            previous_factor = None
        for day in expected:
            current = factors.get(day)
            current_factor = valid_factor(day)
            if current_factor is not None and current_factor > 0 and previous_factor is not None:
                changed = not math.isclose(
                    current_factor / previous_factor,
                    1.0,
                    rel_tol=1e-8,
                    abs_tol=1e-10,
                )
                if changed and day not in actions_by_day:
                    unmatched_factor_changes.append(day.isoformat())
                if not changed and day in actions_by_day:
                    actions_without_factor_change.append(day.isoformat())
                previous_factor = float(current_factor)
            else:
                if current is None and proven_no_trade_factor_gap(day):
                    factor_gap_exemptions.append(day.isoformat())
                else:
                    previous_factor = None

        combined_coverage: dict[str, dict[str, Any]] = {}
        incomplete_combined: list[str] = []
        for day in expected:
            base = coverage_by_day.get(day, {})
            factor_row = factors.get(day)
            factor_complete = (
                valid_factor(day) is not None
                and day.isoformat() not in unmatched_factor_changes
            )
            suspension_exempt = day.isoformat() in factor_gap_exemptions
            effective_factor_complete = factor_complete or suspension_exempt
            complete = bool(base.get("coverage_complete")) and effective_factor_complete
            if not complete:
                incomplete_combined.append(day.isoformat())
            combined_coverage[day.isoformat()] = {
                **base,
                "coverage_complete": complete,
                "dividend_source_coverage_complete": bool(base.get("coverage_complete")),
                "adj_factor_row_present": factor_row is not None,
                "adj_factor_coverage_complete": effective_factor_complete,
                "adj_factor_suspension_exempt": suspension_exempt,
                "adj_factor": (
                    float(factor_row["adj_factor"])
                    if factor_row is not None and factor_row["adj_factor"] is not None
                    else None
                ),
            }
        return {
            "symbol": str(symbol),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "actions": economic_actions,
            "duplicate_action_groups": duplicate_action_groups,
            "coverage_by_day": combined_coverage,
            "missing_coverage_days": missing_days,
            "incomplete_coverage_days": sorted(set(incomplete_days + incomplete_combined)),
            "adj_factor_missing_days": factor_missing_days,
            "adj_factor_suspension_exempt_days": factor_gap_exemptions,
            "adj_factor_unresolved_missing_days": sorted(
                set(factor_missing_days) - set(factor_gap_exemptions)
            ),
            "adj_factor_previous_session_missing": factor_previous_missing,
            "unmatched_adj_factor_change_days": unmatched_factor_changes,
            "actions_without_adj_factor_change_days": actions_without_factor_change,
            "coverage_complete": bool(expected)
            and not missing_days
            and not incomplete_combined
            and not factor_previous_missing,
        }

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
        missing = [
            name
            for name in ("symbol", "effective_from", "status_type")
            if name not in frame.columns
        ]
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
            rows.append(
                (
                    symbol,
                    start,
                    _day(item.get("effective_to")),
                    _day(item.get("announcement_date")),
                    status_type,
                    str(item.get("status_value") or "") or None,
                    str(item.get("board") or "") or None,
                    str(item.get("exchange") or "") or None,
                    _provenance(item, source),
                    _provenance(item, str(as_of), field="as_of"),
                    _coverage(item, True),
                    now,
                )
            )
        return self._write(
            REFERENCE_TABLES["security_status"],
            rows,
            (
                "symbol",
                "effective_from",
                "effective_to",
                "announcement_date",
                "status_type",
                "status_value",
                "board",
                "exchange",
                "source",
                "as_of",
                "coverage_complete",
                _INGESTED,
            ),
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
            name
            for name, present in (
                ("daily_bar", bool(bars)),
                (
                    "limit_prices",
                    bool(limits) and not (approximated and limit_prices_require_exact),
                ),
                ("suspend_status", bool(suspends)),
                ("trade_status", status.get("trade_status") is not None),
            )
            if not present
        ]
        return {
            "symbol": str(symbol),
            "trade_date": trade_date.isoformat(),
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "volume": bar.get("volume"),
            "turnover": bar.get("turnover"),
            "float_market_cap": bar.get("float_market_cap"),
            "is_st": bar.get("is_st"),
            "is_delisting_risk": bar.get("is_delisting_risk"),
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

    def verified_full_day_suspension(
        self,
        symbol: str,
        trade_date: date,
    ) -> dict[str, Any] | None:
        """Return only source-validated full-day Tushare or official SSE evidence.

        ``sync_tail_trade_references`` validates the complete ``suspend_d`` event list,
        rejects intraday/resume mixtures, and rejects positive-volume RAW daily conflicts
        before persisting Tushare rows. Official SSE rows must also have a hash-checked,
        date-matched document in ``ref_suspend_evidence``. A generic ``suspended`` flag is
        not enough to skip missing entry prices during a historical rebuild.
        """
        rows = self._rows(
            f"SELECT suspended, suspend_type, trade_status, source, as_of, coverage_complete "
            f"FROM {REFERENCE_TABLES['suspend_status']} "
            "WHERE symbol = ? AND trade_date = ?",
            [str(symbol), trade_date],
        )
        if not rows:
            return None
        row = rows[0]
        row["trade_date"] = trade_date.isoformat()
        if str(row.get("source") or "").strip() == SSE_OFFICIAL_SUSPENSION_SOURCE:
            proof_rows = self._rows(
                f"SELECT evidence_url, evidence_title, evidence_document_date, "
                f"evidence_sha256, evidence_full_day, evidence_as_of, raw_payload "
                f"FROM {REFERENCE_TABLES['suspend_evidence']} "
                "WHERE symbol = ? AND trade_date = ? AND source = ?",
                [str(symbol), trade_date, SSE_OFFICIAL_SUSPENSION_SOURCE],
            )
            if not proof_rows:
                return None
            proof = proof_rows[0]
            try:
                evidence_payload = json.loads(str(proof["raw_payload"]))
            except (TypeError, json.JSONDecodeError):
                return None
            row.update(
                {
                    "evidence_url": proof["evidence_url"],
                    "evidence_title": proof["evidence_title"],
                    "evidence_document_date": str(_day(proof["evidence_document_date"])),
                    "evidence_sha256": proof["evidence_sha256"],
                    "evidence_full_day": proof["evidence_full_day"],
                    "evidence_as_of": proof["evidence_as_of"],
                    "evidence_start_date": evidence_payload.get("evidence_start_date"),
                    "evidence_end_date": evidence_payload.get("evidence_end_date"),
                }
            )
        if not is_verified_full_day_suspension(row):
            return None
        return dict(row)

    def upsert_official_suspension_evidence(
        self,
        frame: pd.DataFrame,
        *,
        as_of: str,
    ) -> int:
        """Persist hash-verified official exchange documents for full-day suspensions."""
        required = {
            "symbol",
            "trade_date",
            "source",
            "evidence_url",
            "evidence_title",
            "evidence_document_date",
            "evidence_sha256",
            "evidence_full_day",
        }
        missing = sorted(required - set(frame.columns))
        if missing:
            raise TailReferenceError(f"suspension evidence is missing column(s) {missing}")
        if not str(as_of or "").strip():
            raise TailReferenceError("suspension evidence needs an explicit as_of")
        now = datetime.now()
        rows: list[tuple[Any, ...]] = []
        for item in frame.to_dict("records"):
            day = _day(item.get("trade_date"))
            symbol = str(item.get("symbol") or "").strip()
            if day is None or not symbol:
                raise TailReferenceError("suspension evidence needs symbol and trade_date")
            payload = {
                **item,
                "as_of": str(as_of),
                "suspended": True,
                "suspend_type": "S",
                "trade_status": "suspended",
                "coverage_complete": True,
            }
            period_start = item.get("evidence_start_date")
            period_end = item.get("evidence_end_date")
            if (period_start is None) != (period_end is None):
                raise TailReferenceError("official suspension evidence has a partial date range")
            if period_start is not None and period_end is not None:
                payload["evidence_start_date"] = _day(period_start)
                payload["evidence_end_date"] = _day(period_end)
            if not is_verified_full_day_suspension(
                {
                    **payload,
                    "as_of": str(as_of),
                    "evidence_as_of": str(as_of),
                    "evidence_full_day": bool(item.get("evidence_full_day")),
                    "evidence_document_date": str(_day(item.get("evidence_document_date"))),
                    "trade_date": day.isoformat(),
                }
            ):
                raise TailReferenceError("official suspension evidence is invalid")
            rows.append(
                (
                    symbol,
                    day,
                    SSE_OFFICIAL_SUSPENSION_SOURCE,
                    str(item["evidence_url"]).strip(),
                    str(item["evidence_title"]).strip(),
                    _day(item["evidence_document_date"]),
                    str(item["evidence_sha256"]).strip().lower(),
                    True,
                    str(as_of),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                )
            )
        return self._write(
            REFERENCE_TABLES["suspend_evidence"],
            rows,
            (
                "symbol",
                "trade_date",
                "source",
                "evidence_url",
                "evidence_title",
                "evidence_document_date",
                "evidence_sha256",
                "evidence_full_day",
                "evidence_as_of",
                "raw_payload",
                _INGESTED,
            ),
        )

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

        daily_by_day = {
            _day(item["trade_date"]): item for item in bars if _day(item["trade_date"]) is not None
        }
        sessions: list[tuple[date, dict[str, Any]]] = []
        missing_limits_days: list[str] = []
        undeclared_status_days: list[str] = []
        explicitly_suspended: set[str] = {
            "s",
            "suspend",
            "suspended",
            "halt",
            "halted",
            "h",
            "p",
            "停牌",
            "暂停交易",
        }
        for day in self.calendar(start, end):
            item = daily_by_day.get(day)
            bar: dict[str, Any] = {}
            if item is not None:
                bar.update(
                    {
                        "open": item["open"],
                        "high": item["high"],
                        "low": item["low"],
                        "close": item["close"],
                        "volume": item["volume"],
                        "price_basis": item["price_basis"],
                    }
                )
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
            declared = False
            row = status.get(day)
            if row is not None:
                for key in ("suspended", "suspend_type", "trade_status"):
                    if row.get(key) is not None:
                        bar[key] = row[key]
                declared = row.get("trade_status") is not None
            raw_status = str((row or {}).get("trade_status") or "").strip().lower()
            suspended = _flag((row or {}).get("suspended")) or raw_status in explicitly_suspended
            if suspended:
                bar["suspended"] = True
            if not declared:
                undeclared_status_days.append(day.isoformat())
            if item is None and not suspended:
                continue
            if not suspended and (up_limit is None or down_limit is None):
                missing_limits_days.append(day.isoformat())
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
            "daily_bar_days": sorted(day.isoformat() for day in daily_by_day),
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
            elif table in {
                REFERENCE_TABLES["corporate_actions"],
                REFERENCE_TABLES["suspend_status"],
                REFERENCE_TABLES["daily_bars"],
                REFERENCE_TABLES["limit_prices"],
            }:
                entry["symbols"] = int(
                    self._scalar(f"SELECT COUNT(DISTINCT symbol) FROM {table}") or 0
                )
            elif table == REFERENCE_TABLES["corporate_action_coverage"]:
                entry["complete_dates"] = int(
                    self._scalar(f"SELECT COUNT(*) FROM {table} WHERE coverage_complete") or 0
                )
            per_source[name] = entry
        factor_rows = int(self._scalar(f"SELECT COUNT(*) FROM {ADJ_FACTOR_TABLE}") or 0)
        per_source["adj_factor_history"] = {
            "table": ADJ_FACTOR_TABLE,
            "rows": factor_rows,
            "symbols": int(
                self._scalar(f"SELECT COUNT(DISTINCT symbol) FROM {ADJ_FACTOR_TABLE}") or 0
            ),
            "complete_rows": int(
                self._scalar(f"SELECT COUNT(*) FROM {ADJ_FACTOR_TABLE} WHERE coverage_complete")
                or 0
            ),
        }
        approximated = int(
            self._scalar(
                f"SELECT COUNT(*) FROM {REFERENCE_TABLES['limit_prices']} WHERE approximated"
            )
            or 0
        )
        unknown_status = int(
            self._scalar(
                f"SELECT COUNT(*) FROM {REFERENCE_TABLES['suspend_status']} "
                "WHERE trade_status IS NULL"
            )
            or 0
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
        required_sources = set(REFERENCE_TABLES) - {"suspend_evidence"}
        found = {
            f"{name}_table_missing"
            for name, entry in sources.items()
            if name in required_sources and int(entry["rows"]) == 0
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
    return (
        int(
            con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
            ).fetchone()[0]
            or 0
        )
        > 0
    )


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
            status_columns = {
                str(row[0]).strip().lower()
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = ?",
                    [WAREHOUSE_TABLES["security_status"]],
                ).fetchall()
            }
            announcement_date_select = (
                "announcement_date"
                if "announcement_date" in status_columns
                else "NULL::DATE AS announcement_date"
            )
            frames["security_status"] = con.execute(
                "SELECT symbol, effective_from, effective_to, "
                f"{announcement_date_select}, status_type, status_value, "
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
        landed["daily_bars"] = store.upsert_daily_bars(
            bars, price_basis=PRICE_BASIS_RAW, as_of=reading.as_of
        )
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
            name for name in REFERENCE_TABLES if int(coverage["sources"][name]["rows"]) > 0
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
            frame.columns = [str(name).lstrip("\ufeff").strip().lower() for name in frame.columns]
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
                rows.append(
                    {
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
                    }
                )
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
