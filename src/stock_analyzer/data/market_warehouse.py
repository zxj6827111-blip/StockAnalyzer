"""Local market warehouse backed by DuckDB with offline-package materialization."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from stock_analyzer.data.intraday_summary import load_intraday_summary
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tdx_offline_provider import (
    _SELECTED_COLUMNS,
    _normalize_frame,
    _normalize_symbol,
    _resolve_symbol_path,
)

_DUCK_CONNECTION = Any

_DAILY_TABLE = "daily_bars"
_FINANCIAL_SNAPSHOT_TABLE = "financial_snapshots"
_TRADE_STATUS_TABLE = "daily_trade_status"
_MARGIN_TABLE = "margin_detail"
_MONEYFLOW_TABLE = "moneyflow"
_HK_HOLD_TABLE = "hk_hold"
_TOP_LIST_TABLE = "top_list_events"
_TOP_INST_TABLE = "top_inst_events"
_BLOCK_TRADE_TABLE = "block_trade_events"
_INDEX_DAILY_TABLE = "index_daily"
_SECURITY_STATUS_TABLE = "security_status"
_SECURITY_IDENTITY_MAPPING_TABLE = "security_identity_mapping"
_INTRADAY_TABLES = {
    "1m": "intraday_summary_1m",
    "5m": "intraday_summary_5m",
}
_INTRADAY_COLUMNS = [
    "minute_count",
    "session_return",
    "session_range_pct",
    "realized_vol",
    "vwap_gap",
    "am_return",
    "pm_return",
    "am_pm_diff",
    "last30_return",
    "last30_volume_share",
    "positive_bar_ratio",
    "close_position",
]
_DAILY_NUMERIC_COLUMNS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "roe",
    "debt_ratio",
    "financial_completeness",
    "holder_count",
    "block_trade_net",
    "financing_balance",
    "margin_financing_balance",
    "northbound_net",
    "dragon_tiger_flag",
    "moneyflow_net_amount",
    "hk_hold_ratio",
    "hk_hold_change",
    "inst_net_amount",
    "block_trade_amount",
    "block_trade_volume",
    "block_trade_premium_discount",
    "adjustment_anchor_factor",
    "up_limit",
    "down_limit",
}
_DAILY_BOOLEAN_COLUMNS = {
    "suspended",
    "is_st",
    "is_delisting_risk",
    "financial_data_complete",
    "background_data_complete",
}
_DAILY_STRING_COLUMNS = {
    "name",
    "financial_missing_fields",
    "financial_source",
    "financial_report_date",
    "financial_as_of",
    "financial_trust_level",
    "background_data_source",
    "background_missing_fields",
    "background_as_of",
    "price_series_mode",
    "adjustment_source",
    "adjustment_anchor_date",
    "board",
}


class MarketWarehouse:
    """Persist normalized market data into DuckDB and optionally export package files.

    ``read_only=True`` opens the DuckDB with ``read_only=True`` so reads can
    never create the database file, alter its mtime/size/schema or write any
    content. Read-only mode is intended for probes and audits that must
    guarantee the warehouse stays byte-for-byte untouched.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        package_root: str | Path,
        package_writes_enabled: bool = True,
        read_only: bool = False,
    ) -> None:
        self._db_path = Path(db_path).expanduser()
        self._package_root = Path(package_root).expanduser()
        self._package_writes_enabled = bool(package_writes_enabled)
        self._read_only = bool(read_only)

    @property
    def read_only(self) -> bool:
        return self._read_only

    def enforce_read_only(self) -> None:
        """Switch this warehouse to read-only mode (probes/audits).

        After this call the DuckDB is only ever opened with ``read_only=True``:
        no file creation, schema mutation, writes or connection-level side
        effects. Intended for probes that must guarantee the warehouse stays
        untouched even when constructed through a read-write path.
        """
        self._read_only = True

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def package_root(self) -> Path:
        return self._package_root

    @property
    def package_writes_enabled(self) -> bool:
        return self._package_writes_enabled

    def ensure_schema(self) -> None:
        if self._read_only:
            raise DataSourceError(
                f"market warehouse is read-only; refusing schema creation: {self._db_path}"
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect_write() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                    symbol VARCHAR,
                    date DATE,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume DOUBLE,
                    turnover DOUBLE,
                    float_market_cap DOUBLE,
                    suspended BOOLEAN,
                    name VARCHAR,
                    is_st BOOLEAN,
                    is_delisting_risk BOOLEAN,
                    roe DOUBLE,
                    debt_ratio DOUBLE,
                    financial_data_complete BOOLEAN,
                    financial_missing_fields VARCHAR,
                    financial_source VARCHAR,
                    financial_report_date VARCHAR,
                    financial_as_of VARCHAR,
                    financial_trust_level VARCHAR,
                    financial_completeness DOUBLE,
                    holder_count DOUBLE,
                    block_trade_net DOUBLE,
                    financing_balance DOUBLE,
                    margin_financing_balance DOUBLE,
                    northbound_net DOUBLE,
                    dragon_tiger_flag DOUBLE,
                    moneyflow_net_amount DOUBLE,
                    hk_hold_ratio DOUBLE,
                    hk_hold_change DOUBLE,
                    inst_net_amount DOUBLE,
                    block_trade_amount DOUBLE,
                    block_trade_volume DOUBLE,
                    block_trade_premium_discount DOUBLE,
                    background_data_source VARCHAR,
                    background_data_complete BOOLEAN,
                    background_missing_fields VARCHAR,
                    background_as_of VARCHAR,
                    price_series_mode VARCHAR,
                    adjustment_source VARCHAR,
                    adjustment_anchor_date VARCHAR,
                    adjustment_anchor_factor DOUBLE,
                    board VARCHAR
                )
                """
            )
            # NAS 上的既有 DuckDB 不会因 CREATE TABLE IF NOT EXISTS 自动补列。
            for column_name, column_type in (
                ("financial_as_of", "VARCHAR"),
                ("financial_trust_level", "VARCHAR"),
                ("financial_completeness", "DOUBLE"),
                ("background_missing_fields", "VARCHAR"),
                ("background_as_of", "VARCHAR"),
                ("price_series_mode", "VARCHAR"),
                ("adjustment_source", "VARCHAR"),
                ("adjustment_anchor_date", "VARCHAR"),
                ("adjustment_anchor_factor", "DOUBLE"),
                ("up_limit", "DOUBLE"),
                ("down_limit", "DOUBLE"),
                ("moneyflow_net_amount", "DOUBLE"),
                ("hk_hold_ratio", "DOUBLE"),
                ("hk_hold_change", "DOUBLE"),
                ("inst_net_amount", "DOUBLE"),
                ("block_trade_amount", "DOUBLE"),
                ("block_trade_volume", "DOUBLE"),
                ("block_trade_premium_discount", "DOUBLE"),
            ):
                connection.execute(
                    f"ALTER TABLE {_DAILY_TABLE} "
                    f"ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intraday_summary_1m (
                    symbol VARCHAR,
                    date DATE,
                    minute_count DOUBLE,
                    session_return DOUBLE,
                    session_range_pct DOUBLE,
                    realized_vol DOUBLE,
                    vwap_gap DOUBLE,
                    am_return DOUBLE,
                    pm_return DOUBLE,
                    am_pm_diff DOUBLE,
                    last30_return DOUBLE,
                    last30_volume_share DOUBLE,
                    positive_bar_ratio DOUBLE,
                    close_position DOUBLE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intraday_summary_5m (
                    symbol VARCHAR,
                    date DATE,
                    minute_count DOUBLE,
                    session_return DOUBLE,
                    session_range_pct DOUBLE,
                    realized_vol DOUBLE,
                    vwap_gap DOUBLE,
                    am_return DOUBLE,
                    pm_return DOUBLE,
                    am_pm_diff DOUBLE,
                    last30_return DOUBLE,
                    last30_volume_share DOUBLE,
                    positive_bar_ratio DOUBLE,
                    close_position DOUBLE
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS financial_snapshots (
                    symbol VARCHAR,
                    end_date DATE,
                    ann_date DATE,
                    roe DOUBLE,
                    debt_ratio DOUBLE,
                    update_flag INTEGER,
                    financial_report_date VARCHAR,
                    financial_as_of VARCHAR,
                    financial_source VARCHAR,
                    financial_trust_level VARCHAR,
                    financial_missing_fields VARCHAR,
                    financial_data_complete BOOLEAN,
                    financial_completeness DOUBLE,
                    coverage_complete BOOLEAN,
                    as_of VARCHAR,
                    source VARCHAR
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_snapshots_key
                ON financial_snapshots (symbol, end_date, ann_date, financial_source)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_trade_status (
                    symbol VARCHAR,
                    trade_date DATE,
                    up_limit DOUBLE,
                    down_limit DOUBLE,
                    suspended BOOLEAN,
                    suspend_type VARCHAR,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_status_key
                ON daily_trade_status (symbol, trade_date)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_detail (
                    symbol VARCHAR,
                    trade_date DATE,
                    financing_balance DOUBLE,
                    financing_buy_amount DOUBLE,
                    securities_lending_balance DOUBLE,
                    securities_lending_volume DOUBLE,
                    securities_lending_sell_volume DOUBLE,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS moneyflow (
                    symbol VARCHAR,
                    trade_date DATE,
                    buy_sm_amount DOUBLE,
                    sell_sm_amount DOUBLE,
                    buy_md_amount DOUBLE,
                    sell_md_amount DOUBLE,
                    buy_lg_amount DOUBLE,
                    sell_lg_amount DOUBLE,
                    buy_elg_amount DOUBLE,
                    sell_elg_amount DOUBLE,
                    net_mf_amount DOUBLE,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hk_hold (
                    symbol VARCHAR,
                    trade_date DATE,
                    hold_vol DOUBLE,
                    hold_ratio DOUBLE,
                    hold_market_cap DOUBLE,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS top_list_events (
                    symbol VARCHAR,
                    trade_date DATE,
                    dragon_tiger_flag DOUBLE,
                    reason_count INTEGER,
                    reasons VARCHAR,
                    buy_amount DOUBLE,
                    sell_amount DOUBLE,
                    turnover DOUBLE,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS top_inst_events (
                    symbol VARCHAR,
                    trade_date DATE,
                    institution_name VARCHAR,
                    inst_buy_amount DOUBLE,
                    inst_sell_amount DOUBLE,
                    inst_net_amount DOUBLE,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS block_trade_events (
                    symbol VARCHAR,
                    trade_date DATE,
                    block_price DOUBLE,
                    block_trade_volume DOUBLE,
                    block_trade_amount DOUBLE,
                    block_trade_premium_discount DOUBLE,
                    block_trade_net DOUBLE,
                    buyer VARCHAR,
                    seller VARCHAR,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS index_daily (
                    index_code VARCHAR,
                    trade_date DATE,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume DOUBLE,
                    turnover DOUBLE,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_index_daily_key
                ON index_daily (index_code, trade_date)
                """
            )
            # Point-in-time 证券状态表：记录每个 symbol 在特定时间区间的
            # ST / 上市 / 退市 / 停牌 / 板块 / IPO 无涨跌幅阶段等状态。
            # 关键约束：同一 symbol 的 effective 区间不得重叠。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS security_status (
                    symbol VARCHAR,
                    effective_from DATE,
                    effective_to DATE,
                    status_type VARCHAR,
                    status_value VARCHAR,
                    board VARCHAR,
                    exchange VARCHAR,
                    source VARCHAR,
                    as_of VARCHAR,
                    coverage_complete BOOLEAN
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_security_status_key
                ON security_status (symbol, effective_from, status_type)
                """
            )
            # 证券身份映射表：北交所旧代码 43/83/87 到新代码 92 的带日期映射。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS security_identity_mapping (
                    historical_symbol VARCHAR,
                    canonical_symbol VARCHAR,
                    effective_from DATE,
                    effective_to DATE,
                    source VARCHAR,
                    as_of VARCHAR
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_security_identity_mapping_key
                ON security_identity_mapping (historical_symbol, effective_from)
                """
            )

    def upsert_financial_snapshots(self, *, symbol: str, frame: pd.DataFrame) -> int:
        """Idempotent upsert of PIT financial snapshot events. Returns row count stored."""
        from stock_analyzer.data.financial_pit import merge_snapshot_frames

        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return int(len(self.fetch_financial_snapshots(symbol=normalized_symbol)))
        incoming = frame.copy()
        if "symbol" not in incoming.columns:
            incoming.insert(0, "symbol", normalized_symbol)
        else:
            incoming["symbol"] = normalized_symbol
        existing = self.fetch_financial_snapshots(symbol=normalized_symbol)
        merged = merge_snapshot_frames(existing, incoming)
        if merged.empty:
            return 0
        payload = merged.copy()
        for col in ("end_date", "ann_date"):
            if col in payload.columns:
                payload[col] = pd.to_datetime(payload[col], errors="coerce").dt.date
        self.ensure_schema()
        with self._connect_write() as connection:
            connection.execute(
                f"DELETE FROM {_FINANCIAL_SNAPSHOT_TABLE} WHERE symbol = ?",
                [normalized_symbol],
            )
            connection.register("fin_snap_stage", payload)
            cols = [
                "symbol",
                "end_date",
                "ann_date",
                "roe",
                "debt_ratio",
                "update_flag",
                "financial_report_date",
                "financial_as_of",
                "financial_source",
                "financial_trust_level",
                "financial_missing_fields",
                "financial_data_complete",
                "financial_completeness",
                "coverage_complete",
                "as_of",
                "source",
            ]
            present = [c for c in cols if c in payload.columns]
            connection.execute(
                f"""
                INSERT INTO {_FINANCIAL_SNAPSHOT_TABLE} ({", ".join(present)})
                SELECT {", ".join(present)} FROM fin_snap_stage
                """
            )
            connection.unregister("fin_snap_stage")
        return int(len(payload))

    def fetch_financial_snapshots(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_FINANCIAL_SNAPSHOT_TABLE):
            return pd.DataFrame()
        query = f"""
            SELECT *
            FROM {_FINANCIAL_SNAPSHOT_TABLE}
            WHERE symbol = ?
            ORDER BY ann_date ASC, end_date ASC, update_flag ASC
        """
        with self._connect_readonly() as connection:
            frame = cast(pd.DataFrame, connection.execute(query, [normalized_symbol]).fetch_df())
        if frame.empty:
            return frame
        for col in ("end_date", "ann_date"):
            if col in frame.columns:
                frame[col] = pd.to_datetime(frame[col], errors="coerce")
        return frame

    def fetch_financial_snapshots_batch(
        self,
        *,
        symbols: list[str] | None = None,
        as_of: date | pd.Timestamp | str | None = None,
    ) -> pd.DataFrame:
        """Batch-fetch PIT financial snapshots for many symbols in one query.

        Returns rows for all requested symbols (or the whole table when
        ``symbols`` is empty), optionally restricted to snapshots disclosed
        on or before ``as_of`` (ann_date <= as_of, PIT semantics). Columns
        match ``fetch_financial_snapshots``.
        """
        if not self._table_exists(_FINANCIAL_SNAPSHOT_TABLE):
            return pd.DataFrame()
        normalized = sorted(
            {item for item in (_normalize_symbol(value) for value in (symbols or [])) if item}
        )
        with self._connect_readonly() as connection:
            if normalized:
                symbols_df = pd.DataFrame({"symbol": normalized})
                connection.register("fin_snap_symbols", symbols_df)
                try:
                    frame = cast(
                        pd.DataFrame,
                        connection.execute(
                            f"""
                            SELECT *
                            FROM {_FINANCIAL_SNAPSHOT_TABLE}
                            WHERE symbol IN (SELECT symbol FROM fin_snap_symbols)
                            ORDER BY ann_date ASC, end_date ASC, update_flag ASC
                            """
                        ).fetch_df(),
                    )
                finally:
                    connection.unregister("fin_snap_symbols")
            else:
                frame = cast(
                    pd.DataFrame,
                    connection.execute(
                        f"""
                        SELECT *
                        FROM {_FINANCIAL_SNAPSHOT_TABLE}
                        ORDER BY ann_date ASC, end_date ASC, update_flag ASC
                        """
                    ).fetch_df(),
                )
        if frame.empty:
            return frame
        for col in ("end_date", "ann_date"):
            if col in frame.columns:
                frame[col] = pd.to_datetime(frame[col], errors="coerce")
        if as_of is not None:
            as_of_ts = pd.Timestamp(as_of)
            frame = frame[frame["ann_date"] <= as_of_ts].reset_index(drop=True)
        return frame

    def apply_financial_snapshots_to_daily(self, *, symbol: str) -> pd.DataFrame:
        """Re-materialize daily bars financial columns from PIT snapshots."""
        from stock_analyzer.data.financial_pit import apply_financial_snapshots_asof

        daily = self.fetch_all_daily_bars(symbol=symbol)
        if daily.empty:
            return daily
        snaps = self.fetch_financial_snapshots(symbol=symbol)
        if snaps.empty:
            return daily
        enriched: pd.DataFrame = apply_financial_snapshots_asof(
            daily,
            snaps,
            only_fill_pending=False,
        )
        self.replace_daily_bars(symbol=symbol, frame=enriched)
        if self.package_writes_enabled:
            write_package_daily_bars(
                package_root=self.package_root,
                symbol=symbol,
                frame=enriched,
            )
        return enriched

    def upsert_trade_status(self, *, symbol: str, frame: pd.DataFrame) -> int:
        """Idempotent upsert of daily trade status rows."""
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            dates = payload["trade_date"].tolist()
            for d in dates:
                connection.execute(
                    f"DELETE FROM {_TRADE_STATUS_TABLE} WHERE symbol = ? AND trade_date = ?",
                    [normalized_symbol, d],
                )
            connection.register("ts_stage", payload)
            cols = [
                "symbol",
                "trade_date",
                "up_limit",
                "down_limit",
                "suspended",
                "suspend_type",
                "source",
                "as_of",
                "coverage_complete",
            ]
            present = [c for c in cols if c in payload.columns]
            connection.execute(
                f"""
                INSERT INTO {_TRADE_STATUS_TABLE} ({", ".join(present)})
                SELECT {", ".join(present)} FROM ts_stage
                """
            )
            connection.unregister("ts_stage")
        return int(len(payload))

    def fetch_trade_status(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_TRADE_STATUS_TABLE):
            return pd.DataFrame()
        query = f"""
            SELECT * FROM {_TRADE_STATUS_TABLE}
            WHERE symbol = ?
            ORDER BY trade_date ASC
        """
        with self._connect_readonly() as connection:
            frame = cast(pd.DataFrame, connection.execute(query, [normalized_symbol]).fetch_df())
        if frame.empty:
            return frame
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def apply_trade_status_to_daily(self, *, symbol: str) -> pd.DataFrame:
        """Merge trade status (up_limit, down_limit, suspended) into daily bars + package."""
        daily = self.fetch_all_daily_bars(symbol=symbol)
        if daily.empty:
            return daily
        status = self.fetch_trade_status(symbol=symbol)
        if status.empty:
            return daily
        status_idx = status.set_index(pd.to_datetime(status["trade_date"]))
        for ts, row in status_idx.iterrows():
            if ts in daily.index:
                up = row.get("up_limit")
                down = row.get("down_limit")
                suspended = row.get("suspended")
                if up is not None and not pd.isna(up):
                    daily.at[ts, "up_limit"] = float(up)
                if down is not None and not pd.isna(down):
                    daily.at[ts, "down_limit"] = float(down)
                # NULL means suspend_d coverage is unavailable. Preserve the prior
                # daily value instead of coercing unknown into False.
                if suspended is not None and not pd.isna(suspended):
                    daily.at[ts, "suspended"] = bool(suspended)
        self.replace_daily_bars(symbol=symbol, frame=daily)
        if self.package_writes_enabled:
            write_package_daily_bars(
                package_root=self.package_root,
                symbol=symbol,
                frame=daily,
            )
        return daily

    def upsert_margin_detail(self, *, symbol: str, frame: pd.DataFrame) -> int:
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            for d in payload["trade_date"].tolist():
                connection.execute(
                    f"DELETE FROM {_MARGIN_TABLE} WHERE symbol = ? AND trade_date = ?",
                    [normalized_symbol, d],
                )
            connection.register("margin_stage", payload)
            cols = [
                c
                for c in payload.columns
                if c
                in (
                    "symbol",
                    "trade_date",
                    "financing_balance",
                    "financing_buy_amount",
                    "securities_lending_balance",
                    "securities_lending_volume",
                    "securities_lending_sell_volume",
                    "source",
                    "as_of",
                    "coverage_complete",
                )
            ]
            connection.execute(
                f"INSERT INTO {_MARGIN_TABLE} ({', '.join(cols)}) "
                f"SELECT {', '.join(cols)} FROM margin_stage"
            )
            connection.unregister("margin_stage")
        return int(len(payload))

    def fetch_margin_detail(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_MARGIN_TABLE):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"SELECT * FROM {_MARGIN_TABLE} WHERE symbol = ? ORDER BY trade_date",
                    [normalized_symbol],
                ).fetch_df(),
            )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def upsert_moneyflow(self, *, symbol: str, frame: pd.DataFrame) -> int:
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            for d in payload["trade_date"].tolist():
                connection.execute(
                    f"DELETE FROM {_MONEYFLOW_TABLE} WHERE symbol = ? AND trade_date = ?",
                    [normalized_symbol, d],
                )
            connection.register("mf_stage", payload)
            cols = [
                c
                for c in payload.columns
                if c
                in (
                    "symbol",
                    "trade_date",
                    "buy_sm_amount",
                    "sell_sm_amount",
                    "buy_md_amount",
                    "sell_md_amount",
                    "buy_lg_amount",
                    "sell_lg_amount",
                    "buy_elg_amount",
                    "sell_elg_amount",
                    "net_mf_amount",
                    "source",
                    "as_of",
                    "coverage_complete",
                )
            ]
            connection.execute(
                f"INSERT INTO {_MONEYFLOW_TABLE} ({', '.join(cols)}) "
                f"SELECT {', '.join(cols)} FROM mf_stage"
            )
            connection.unregister("mf_stage")
        return int(len(payload))

    def fetch_moneyflow(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_MONEYFLOW_TABLE):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"SELECT * FROM {_MONEYFLOW_TABLE} WHERE symbol = ? ORDER BY trade_date",
                    [normalized_symbol],
                ).fetch_df(),
            )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def upsert_hk_hold(self, *, symbol: str, frame: pd.DataFrame) -> int:
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            for d in payload["trade_date"].tolist():
                connection.execute(
                    f"DELETE FROM {_HK_HOLD_TABLE} WHERE symbol = ? AND trade_date = ?",
                    [normalized_symbol, d],
                )
            connection.register("hk_stage", payload)
            cols = [
                c
                for c in payload.columns
                if c
                in (
                    "symbol",
                    "trade_date",
                    "hold_vol",
                    "hold_ratio",
                    "hold_market_cap",
                    "source",
                    "as_of",
                    "coverage_complete",
                )
            ]
            connection.execute(
                f"INSERT INTO {_HK_HOLD_TABLE} ({', '.join(cols)}) "
                f"SELECT {', '.join(cols)} FROM hk_stage"
            )
            connection.unregister("hk_stage")
        return int(len(payload))

    def fetch_hk_hold(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_HK_HOLD_TABLE):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"SELECT * FROM {_HK_HOLD_TABLE} WHERE symbol = ? ORDER BY trade_date",
                    [normalized_symbol],
                ).fetch_df(),
            )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def upsert_top_list_events(self, *, symbol: str, frame: pd.DataFrame) -> int:
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            for d in payload["trade_date"].tolist():
                connection.execute(
                    f"DELETE FROM {_TOP_LIST_TABLE} WHERE symbol = ? AND trade_date = ?",
                    [normalized_symbol, d],
                )
            connection.register("tl_stage", payload)
            cols = [
                c
                for c in payload.columns
                if c
                in (
                    "symbol",
                    "trade_date",
                    "dragon_tiger_flag",
                    "reason_count",
                    "reasons",
                    "buy_amount",
                    "sell_amount",
                    "turnover",
                    "source",
                    "as_of",
                    "coverage_complete",
                )
            ]
            col_str = ", ".join(cols)
            connection.execute(
                f"INSERT INTO {_TOP_LIST_TABLE} ({col_str}) SELECT {col_str} FROM tl_stage"
            )
            connection.unregister("tl_stage")
        return int(len(payload))

    def fetch_top_list_events(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_TOP_LIST_TABLE):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"SELECT * FROM {_TOP_LIST_TABLE} WHERE symbol = ? ORDER BY trade_date",
                    [normalized_symbol],
                ).fetch_df(),
            )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def upsert_top_inst_events(self, *, symbol: str, frame: pd.DataFrame) -> int:
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            for d in payload["trade_date"].tolist():
                connection.execute(
                    f"DELETE FROM {_TOP_INST_TABLE} WHERE symbol = ? AND trade_date = ?",
                    [normalized_symbol, d],
                )
            connection.register("ti_stage", payload)
            cols = [
                c
                for c in payload.columns
                if c
                in (
                    "symbol",
                    "trade_date",
                    "institution_name",
                    "inst_buy_amount",
                    "inst_sell_amount",
                    "inst_net_amount",
                    "source",
                    "as_of",
                    "coverage_complete",
                )
            ]
            col_str = ", ".join(cols)
            connection.execute(
                f"INSERT INTO {_TOP_INST_TABLE} ({col_str}) SELECT {col_str} FROM ti_stage"
            )
            connection.unregister("ti_stage")
        return int(len(payload))

    def fetch_top_inst_events(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_TOP_INST_TABLE):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"SELECT * FROM {_TOP_INST_TABLE} WHERE symbol = ? ORDER BY trade_date",
                    [normalized_symbol],
                ).fetch_df(),
            )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def upsert_block_trade_events(self, *, symbol: str, frame: pd.DataFrame) -> int:
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            for d in payload["trade_date"].tolist():
                connection.execute(
                    f"DELETE FROM {_BLOCK_TRADE_TABLE} WHERE symbol = ? AND trade_date = ?",
                    [normalized_symbol, d],
                )
            connection.register("bt_stage", payload)
            cols = [
                c
                for c in payload.columns
                if c
                in (
                    "symbol",
                    "trade_date",
                    "block_price",
                    "block_trade_volume",
                    "block_trade_amount",
                    "block_trade_premium_discount",
                    "block_trade_net",
                    "buyer",
                    "seller",
                    "source",
                    "as_of",
                    "coverage_complete",
                )
            ]
            col_str = ", ".join(cols)
            connection.execute(
                f"INSERT INTO {_BLOCK_TRADE_TABLE} ({col_str}) SELECT {col_str} FROM bt_stage"
            )
            connection.unregister("bt_stage")
        return int(len(payload))

    def fetch_block_trade_events(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_BLOCK_TRADE_TABLE):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"SELECT * FROM {_BLOCK_TRADE_TABLE} WHERE symbol = ? ORDER BY trade_date",
                    [normalized_symbol],
                ).fetch_df(),
            )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def upsert_security_status(self, *, symbol: str, frame: pd.DataFrame) -> int:
        """Idempotent upsert of point-in-time security status rows.

        Natural key: (symbol, effective_from, status_type). Time intervals
        for the same symbol and status_type MUST NOT overlap — this method
        validates the incoming frame before writing and raises
        ``DataSourceError`` on any overlap, rather than allowing illegal
        intervals into the database and detecting them post-hoc.
        """
        normalized_symbol = _normalize_symbol(symbol)
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["symbol"] = normalized_symbol
        payload["effective_from"] = pd.to_datetime(
            payload["effective_from"], errors="coerce"
        ).dt.date
        payload = payload.dropna(subset=["effective_from"])
        if "effective_to" in payload.columns:
            payload["effective_to"] = pd.to_datetime(
                payload["effective_to"], errors="coerce"
            ).dt.date
        if payload.empty:
            return 0
        # 写前校验：同 symbol + status_type 的区间不得重叠
        self._validate_security_status_no_overlap(payload)
        self.ensure_schema()
        with self._connect_write() as connection:
            registered = False
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.register("ss_stage", payload)
                registered = True
                cols = [
                    c
                    for c in payload.columns
                    if c
                    in (
                        "symbol",
                        "effective_from",
                        "effective_to",
                        "status_type",
                        "status_value",
                        "board",
                        "exchange",
                        "source",
                        "as_of",
                        "coverage_complete",
                    )
                ]
                col_str = ", ".join(cols)
                connection.execute(
                    f"INSERT OR REPLACE INTO {_SECURITY_STATUS_TABLE} ({col_str}) "
                    f"SELECT {col_str} FROM ss_stage"
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                if registered:
                    connection.unregister("ss_stage")
        return int(len(payload))

    def fetch_security_status(
        self,
        *,
        symbol: str,
        as_of: date | None = None,
    ) -> pd.DataFrame:
        """Read point-in-time security status for a symbol.

        When ``as_of`` is provided, returns only rows where
        ``effective_from <= as_of`` and (``effective_to`` is NULL or
        ``effective_to >= as_of``), i.e. the active status on that date.
        """
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_SECURITY_STATUS_TABLE):
            return pd.DataFrame()
        filters = ["symbol = ?"]
        params: list[object] = [normalized_symbol]
        if as_of is not None:
            filters.append("effective_from <= ?")
            params.append(as_of.isoformat())
            filters.append("(effective_to IS NULL OR effective_to >= ?)")
            params.append(as_of.isoformat())
        query = f"""
            SELECT * FROM {_SECURITY_STATUS_TABLE}
            WHERE {" AND ".join(filters)}
            ORDER BY effective_from ASC
        """
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(query, params).fetch_df(),
            )
        return frame

    def upsert_security_identity_mapping(self, *, frame: pd.DataFrame) -> int:
        """Idempotent upsert of security identity mapping rows.

        Natural key: (historical_symbol, effective_from). Used to map
        legacy Beijing exchange codes (43/83/87 series) to canonical
        92-series codes with effective-date intervals.
        """
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["effective_from"] = pd.to_datetime(
            payload["effective_from"], errors="coerce"
        ).dt.date
        payload = payload.dropna(subset=["effective_from"])
        if "effective_to" in payload.columns:
            payload["effective_to"] = pd.to_datetime(
                payload["effective_to"], errors="coerce"
            ).dt.date
        if payload.empty:
            return 0
        # 写前校验：同一 historical_symbol 的 effective 区间不得重叠
        self._validate_identity_mapping_no_overlap(payload)
        self.ensure_schema()
        with self._connect_write() as connection:
            registered = False
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.register("sim_stage", payload)
                registered = True
                cols = [
                    c
                    for c in payload.columns
                    if c
                    in (
                        "historical_symbol",
                        "canonical_symbol",
                        "effective_from",
                        "effective_to",
                        "source",
                        "as_of",
                    )
                ]
                col_str = ", ".join(cols)
                connection.execute(
                    f"INSERT OR REPLACE INTO {_SECURITY_IDENTITY_MAPPING_TABLE} ({col_str}) "
                    f"SELECT {col_str} FROM sim_stage"
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                if registered:
                    connection.unregister("sim_stage")
        return int(len(payload))

    def fetch_security_identity_mapping(
        self,
        *,
        historical_symbol: str | None = None,
    ) -> pd.DataFrame:
        """Read security identity mapping (all rows or filtered by historical_symbol)."""
        if not self._table_exists(_SECURITY_IDENTITY_MAPPING_TABLE):
            return pd.DataFrame()
        if historical_symbol:
            query = f"""
                SELECT * FROM {_SECURITY_IDENTITY_MAPPING_TABLE}
                WHERE historical_symbol = ?
                ORDER BY effective_from ASC
            """
            with self._connect_readonly() as connection:
                return cast(
                    pd.DataFrame,
                    connection.execute(query, [historical_symbol]).fetch_df(),
                )
        query = f"""
            SELECT * FROM {_SECURITY_IDENTITY_MAPPING_TABLE}
            ORDER BY historical_symbol, effective_from ASC
        """
        with self._connect_readonly() as connection:
            return cast(
                pd.DataFrame,
                connection.execute(query).fetch_df(),
            )

    def _validate_security_status_no_overlap(self, payload: pd.DataFrame) -> None:
        """写前校验：同一 symbol + status_type 的 effective 区间不得重叠。

        检查 payload 内部的区间重叠 AND payload 与数据库已有记录的区间
        重叠。发现重叠时 raise DataSourceError 拒绝写入。
        """
        if "status_type" not in payload.columns:
            return
        from datetime import date as _date

        def _resolve_end(raw_end: object) -> _date:
            if raw_end is None:
                return _date.max
            if isinstance(raw_end, float) and pd.isna(raw_end):
                return _date.max
            if isinstance(raw_end, type(pd.NaT)):
                return _date.max
            try:
                if pd.isna(cast(Any, raw_end)):
                    return _date.max
            except (TypeError, ValueError):
                pass
            if isinstance(raw_end, pd.Timestamp):
                return raw_end.date()
            if isinstance(raw_end, _date):
                return raw_end
            return _date.max

        # 1. payload 内部重叠检查
        for (symbol, status_type), group in payload.groupby(["symbol", "status_type"]):
            sorted_group = group.sort_values("effective_from")
            entries = sorted_group.to_dict("records")
            for i in range(len(entries)):
                e1 = entries[i]
                end1 = _resolve_end(e1.get("effective_to"))
                for j in range(i + 1, len(entries)):
                    e2 = entries[j]
                    start2 = e2["effective_from"]
                    if start2 <= end1:
                        raise DataSourceError(
                            f"security_status overlap detected before write: "
                            f"symbol={symbol}, status_type={status_type}, "
                            f"interval_a=[{e1['effective_from']}, {e1.get('effective_to')}], "
                            f"interval_b=[{e2['effective_from']}, {e2.get('effective_to')}]"
                        )

        # 2. payload 与数据库已有记录的重叠检查
        #    排除即将被 DELETE 替换的相同自然键，保证 upsert 幂等性
        if not self._table_exists(_SECURITY_STATUS_TABLE):
            return
        for (symbol, status_type), group in payload.groupby(["symbol", "status_type"]):
            sorted_group = group.sort_values("effective_from")
            entries = sorted_group.to_dict("records")
            # 收集 payload 内的 effective_from 值（自然键的一部分），
            # 在 DB 查询中排除这些行——它们将被 DELETE+INSERT 替换
            payload_froms = [e["effective_from"] for e in entries]
            exclude_placeholders = ", ".join("?" for _ in payload_froms)
            for entry in entries:
                start = entry["effective_from"]
                end = _resolve_end(entry.get("effective_to"))
                query = f"""
                    SELECT effective_from, effective_to
                    FROM {_SECURITY_STATUS_TABLE}
                    WHERE symbol = ?
                      AND status_type = ?
                      AND effective_from <= ?
                      AND effective_from NOT IN ({exclude_placeholders})
                """
                with self._connect_readonly() as connection:
                    rows = connection.execute(
                        query,
                        [str(symbol), str(status_type), end, *payload_froms],
                    ).fetchall()
                for row in rows:
                    db_start = row[0]
                    db_end = _resolve_end(row[1])
                    if db_start <= end and start <= db_end:
                        raise DataSourceError(
                            f"security_status overlap with existing DB record: "
                            f"symbol={symbol}, status_type={status_type}, "
                            f"new=[{start}, {entry.get('effective_to')}], "
                            f"existing=[{db_start}, {row[1]}]"
                        )

    def _validate_identity_mapping_no_overlap(self, payload: pd.DataFrame) -> None:
        """写前校验：同一 historical_symbol 的 effective 区间不得重叠。

        检查 payload 内部 AND payload 与数据库已有记录的重叠。
        """
        from datetime import date as _date

        def _resolve_end(raw_end: object) -> _date:
            if raw_end is None:
                return _date.max
            if isinstance(raw_end, float) and pd.isna(raw_end):
                return _date.max
            if isinstance(raw_end, type(pd.NaT)):
                return _date.max
            try:
                if pd.isna(cast(Any, raw_end)):
                    return _date.max
            except (TypeError, ValueError):
                pass
            if isinstance(raw_end, pd.Timestamp):
                return raw_end.date()
            if isinstance(raw_end, _date):
                return raw_end
            return _date.max

        # 1. payload 内部重叠检查
        for historical, group in payload.groupby("historical_symbol"):
            sorted_group = group.sort_values("effective_from")
            entries = sorted_group.to_dict("records")
            for i in range(len(entries)):
                e1 = entries[i]
                end1 = _resolve_end(e1.get("effective_to"))
                for j in range(i + 1, len(entries)):
                    e2 = entries[j]
                    start2 = e2["effective_from"]
                    if start2 <= end1:
                        raise DataSourceError(
                            f"security_identity_mapping overlap detected before write: "
                            f"historical_symbol={historical}, "
                            f"interval_a=[{e1['effective_from']}, {e1.get('effective_to')}], "
                            f"interval_b=[{e2['effective_from']}, {e2.get('effective_to')}]"
                        )

        # 2. payload 与数据库已有记录的重叠检查
        #    排除即将被 DELETE 替换的相同自然键，保证 upsert 幂等性
        if not self._table_exists(_SECURITY_IDENTITY_MAPPING_TABLE):
            return
        for historical, group in payload.groupby("historical_symbol"):
            sorted_group = group.sort_values("effective_from")
            entries = sorted_group.to_dict("records")
            payload_froms = [e["effective_from"] for e in entries]
            exclude_placeholders = ", ".join("?" for _ in payload_froms)
            for entry in entries:
                start = entry["effective_from"]
                end = _resolve_end(entry.get("effective_to"))
                query = f"""
                    SELECT effective_from, effective_to
                    FROM {_SECURITY_IDENTITY_MAPPING_TABLE}
                    WHERE historical_symbol = ?
                      AND effective_from <= ?
                      AND effective_from NOT IN ({exclude_placeholders})
                """
                with self._connect_readonly() as connection:
                    rows = connection.execute(
                        query,
                        [str(historical), end, *payload_froms],
                    ).fetchall()
                for row in rows:
                    db_start = row[0]
                    db_end = _resolve_end(row[1])
                    if db_start <= end and start <= db_end:
                        raise DataSourceError(
                            f"security_identity_mapping overlap with existing DB record: "
                            f"historical_symbol={historical}, "
                            f"new=[{start}, {entry.get('effective_to')}], "
                            f"existing=[{db_start}, {row[1]}]"
                        )

    def validate_security_status_intervals(
        self,
        *,
        symbol: str | None = None,
    ) -> list[dict[str, str]]:
        """Read-only check: ensure PIT status intervals do not overlap.

        Returns a list of overlap descriptions (empty = all clean). Each
        entry has ``symbol``, ``status_type``, ``interval_a``,
        ``interval_b``, describing the two overlapping rows.
        """
        if not self._table_exists(_SECURITY_STATUS_TABLE):
            return []
        params: list[object] = []
        symbol_filter = ""
        if symbol:
            normalized = _normalize_symbol(symbol)
            symbol_filter = " AND a.symbol = ?"
            params.append(normalized)
        query = f"""
            SELECT a.symbol, a.status_type,
                   a.effective_from AS a_from, a.effective_to AS a_to,
                   b.effective_from AS b_from, b.effective_to AS b_to
            FROM {_SECURITY_STATUS_TABLE} a
            JOIN {_SECURITY_STATUS_TABLE} b
              ON a.symbol = b.symbol
             AND a.status_type = b.status_type
             AND a.effective_from < b.effective_from
            WHERE (a.effective_to IS NULL OR a.effective_to >= b.effective_from)
              {symbol_filter}
            ORDER BY a.symbol, a.effective_from
        """
        with self._connect_readonly() as connection:
            rows = connection.execute(query, params).fetchall()
        overlaps: list[dict[str, str]] = []
        for row in rows:
            overlaps.append(
                {
                    "symbol": str(row[0]),
                    "status_type": str(row[1]),
                    "interval_a": f"{row[2]}~{row[3]}",
                    "interval_b": f"{row[4]}~{row[5]}",
                }
            )
        return overlaps

    def upsert_index_daily(self, *, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        payload["trade_date"] = pd.to_datetime(payload["trade_date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["trade_date"])
        if payload.empty:
            return 0
        self.ensure_schema()
        with self._connect_write() as connection:
            for _, row in payload.iterrows():
                connection.execute(
                    f"DELETE FROM {_INDEX_DAILY_TABLE} WHERE index_code = ? AND trade_date = ?",
                    [str(row.get("index_code", "")), row["trade_date"]],
                )
            connection.register("idx_stage", payload)
            cols = [
                c
                for c in payload.columns
                if c
                in (
                    "index_code",
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "turnover",
                    "source",
                    "as_of",
                    "coverage_complete",
                )
            ]
            col_str = ", ".join(cols)
            connection.execute(
                f"INSERT INTO {_INDEX_DAILY_TABLE} ({col_str}) SELECT {col_str} FROM idx_stage"
            )
            connection.unregister("idx_stage")
        if self.package_writes_enabled:
            # Package-backed providers consume index history through the same P4 path.
            for index_code in sorted(payload["index_code"].astype(str).unique()):
                stored = self.fetch_index_daily(index_code=index_code)
                if stored.empty:
                    continue
                benchmark = stored.copy()
                benchmark.index = pd.to_datetime(benchmark["trade_date"], errors="coerce")
                benchmark = benchmark.loc[benchmark.index.notna()]
                benchmark.index.name = "date"
                benchmark["float_market_cap"] = float("nan")
                benchmark["suspended"] = False
                write_package_daily_bars(
                    package_root=self.package_root,
                    symbol=_normalize_symbol(index_code),
                    frame=benchmark,
                )
        return int(len(payload))

    def fetch_index_daily(self, *, index_code: str = "000300.SH") -> pd.DataFrame:
        if not self._table_exists(_INDEX_DAILY_TABLE):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"SELECT * FROM {_INDEX_DAILY_TABLE} WHERE index_code = ? ORDER BY trade_date",
                    [index_code],
                ).fetch_df(),
            )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        return frame

    def apply_p2_p3_to_daily(self, *, symbol: str) -> pd.DataFrame:
        """Project reliable P2/P3 fields back onto daily bars and runtime package.

        Semantics (honest, no fabricated values):
        - margin_detail.financing_balance -> daily.financing_balance AND
          daily.margin_financing_balance (both = Tushare 融资余额 rzye, as-of trade_date).
        - top_list.dragon_tiger_flag -> daily.dragon_tiger_flag on event days (=1).
          Non-event days keep prior value (NaN = not confirmed, NOT forced to 0).
        - northbound_net stays NaN: hk_hold is a holding stock, not a net flow.
        - block_trade_net stays NaN: no reliable buy/sell direction from API.
        - moneyflow/top_inst/block_trade use explicit amount/event fields rather than
          overloading the legacy directional fields.
        """
        daily = self.fetch_all_daily_bars(symbol=symbol)
        if daily.empty:
            return daily

        changed = False

        margin = self.fetch_margin_detail(symbol=symbol)
        if not margin.empty and "financing_balance" in margin.columns:
            m = margin.dropna(subset=["trade_date"]).copy()
            m = m.set_index(pd.to_datetime(m["trade_date"]))
            for ts, row in m.iterrows():
                if ts in daily.index:
                    val = row.get("financing_balance")
                    if val is not None and not pd.isna(val):
                        daily.at[ts, "financing_balance"] = float(val)
                        daily.at[ts, "margin_financing_balance"] = float(val)
                        changed = True

        moneyflow = self.fetch_moneyflow(symbol=symbol)
        if not moneyflow.empty and "net_mf_amount" in moneyflow.columns:
            mflow = moneyflow.dropna(subset=["trade_date"]).copy()
            mflow = mflow.set_index(pd.to_datetime(mflow["trade_date"]))
            for ts, row in mflow.iterrows():
                value = row.get("net_mf_amount")
                if ts in daily.index and value is not None and not pd.isna(value):
                    daily.at[ts, "moneyflow_net_amount"] = float(value)
                    changed = True

        hk_hold = self.fetch_hk_hold(symbol=symbol)
        if not hk_hold.empty:
            hk = hk_hold.dropna(subset=["trade_date"]).copy()
            hk["trade_date"] = pd.to_datetime(hk["trade_date"])
            hk = hk.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
            if "hold_vol" in hk.columns:
                hk["hold_change"] = pd.to_numeric(hk["hold_vol"], errors="coerce").diff()
            for _, row in hk.iterrows():
                ts = row["trade_date"]
                if ts not in daily.index:
                    continue
                hold_ratio = row.get("hold_ratio")
                hold_change = row.get("hold_change")
                if hold_ratio is not None and not pd.isna(hold_ratio):
                    daily.at[ts, "hk_hold_ratio"] = float(hold_ratio)
                    changed = True
                if hold_change is not None and not pd.isna(hold_change):
                    daily.at[ts, "hk_hold_change"] = float(hold_change)
                    changed = True

        top_list = self.fetch_top_list_events(symbol=symbol)
        if not top_list.empty and "dragon_tiger_flag" in top_list.columns:
            t = top_list.dropna(subset=["trade_date"]).copy()
            t = t.set_index(pd.to_datetime(t["trade_date"]))
            for ts, row in t.iterrows():
                if ts in daily.index:
                    flag = row.get("dragon_tiger_flag")
                    if flag is not None and not pd.isna(flag):
                        daily.at[ts, "dragon_tiger_flag"] = float(flag)
                        changed = True

        top_inst = self.fetch_top_inst_events(symbol=symbol)
        if not top_inst.empty and "inst_net_amount" in top_inst.columns:
            inst = top_inst.dropna(subset=["trade_date"]).copy()
            inst["trade_date"] = pd.to_datetime(inst["trade_date"])
            inst["inst_net_amount"] = pd.to_numeric(inst["inst_net_amount"], errors="coerce")
            inst_daily = inst.groupby("trade_date")["inst_net_amount"].sum(min_count=1)
            for ts, value in inst_daily.items():
                if ts in daily.index and not pd.isna(value):
                    daily.at[ts, "inst_net_amount"] = float(value)
                    changed = True

        block_trade = self.fetch_block_trade_events(symbol=symbol)
        if not block_trade.empty:
            block = block_trade.dropna(subset=["trade_date"]).copy()
            block["trade_date"] = pd.to_datetime(block["trade_date"])
            for column in (
                "block_trade_amount",
                "block_trade_volume",
                "block_trade_premium_discount",
            ):
                if column in block.columns:
                    block[column] = pd.to_numeric(block[column], errors="coerce")
            for ts, rows in block.groupby("trade_date"):
                if ts not in daily.index:
                    continue
                amount = rows.get("block_trade_amount", pd.Series(dtype=float)).sum(min_count=1)
                volume = rows.get("block_trade_volume", pd.Series(dtype=float)).sum(min_count=1)
                premium_series = rows.get("block_trade_premium_discount", pd.Series(dtype=float))
                premium = premium_series.mean()
                weights = rows.get("block_trade_amount", pd.Series(dtype=float))
                valid = premium_series.notna() & weights.notna() & weights.gt(0)
                if valid.any():
                    premium = float(
                        (premium_series.loc[valid] * weights.loc[valid]).sum()
                        / weights.loc[valid].sum()
                    )
                for column, value in (
                    ("block_trade_amount", amount),
                    ("block_trade_volume", volume),
                    ("block_trade_premium_discount", premium),
                ):
                    if not pd.isna(value):
                        daily.at[ts, column] = float(value)
                        changed = True

        if changed:
            self.replace_daily_bars(symbol=symbol, frame=daily)
            if self.package_writes_enabled:
                write_package_daily_bars(
                    package_root=self.package_root,
                    symbol=symbol,
                    frame=daily,
                )
        return daily

    def warehouse_meta_path(self, symbol: str | None = None) -> Path:
        if symbol:
            return self.package_root / "meta" / f"{_normalize_symbol(symbol)}.json"
        return self._db_path.with_name("warehouse_meta.json")

    def read_symbol_meta(self, symbol: str) -> dict[str, object]:
        path = self.warehouse_meta_path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        return _read_json(path) or {}

    def write_symbol_meta(self, symbol: str, payload: dict[str, object]) -> Path:
        path = self.warehouse_meta_path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_warehouse_meta(self) -> dict[str, object]:
        return _read_json(self.warehouse_meta_path())

    def write_warehouse_meta(self, payload: dict[str, object]) -> Path:
        path = self.warehouse_meta_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _price_series_contract_from_meta(meta: dict[str, object]) -> dict[str, object]:
        return {
            "price_series_mode": str(meta.get("price_series_mode", "") or "").strip().lower(),
            "adjustment_source": str(meta.get("adjustment_source", "") or "").strip(),
            "adjustment_anchor_date": str(meta.get("adjustment_anchor_date", "") or "").strip(),
            "adjustment_anchor_factor": meta.get("adjustment_anchor_factor", None),
            "known": bool(str(meta.get("price_series_mode", "") or "").strip()),
        }

    def price_series_contract(self, *, symbol: str | None = None) -> dict[str, object]:
        if symbol:
            return self._price_series_contract_from_meta(self.read_symbol_meta(symbol))

        contracts = [self.price_series_contract(symbol=item) for item in self.list_symbols()]
        known_contracts = [item for item in contracts if bool(item.get("known"))]
        if not known_contracts:
            return self._price_series_contract_from_meta({})

        def _common_value(key: str) -> object:
            values = {item.get(key) for item in known_contracts}
            return next(iter(values)) if len(values) == 1 else None

        mode = _common_value("price_series_mode")
        return {
            "price_series_mode": str(mode or "mixed"),
            "adjustment_source": str(_common_value("adjustment_source") or ""),
            "adjustment_anchor_date": str(_common_value("adjustment_anchor_date") or ""),
            "adjustment_anchor_factor": _common_value("adjustment_anchor_factor"),
            "known": len(known_contracts) == len(contracts),
            "mixed": any(
                _common_value(key) is None
                for key in ("price_series_mode", "adjustment_anchor_factor")
            ),
            "symbols_total": len(contracts),
            "symbols_known": len(known_contracts),
        }

    def update_price_series_contract(
        self,
        *,
        symbol: str,
        price_series_mode: str,
        adjustment_source: str = "",
        adjustment_anchor_date: str = "",
        adjustment_anchor_factor: float | None = None,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        meta = self.read_symbol_meta(symbol)
        meta.update(
            {
                "price_series_mode": str(price_series_mode or "").strip().lower() or "unknown",
                "adjustment_source": str(adjustment_source or "").strip(),
                "adjustment_anchor_date": str(adjustment_anchor_date or "").strip(),
                "adjustment_anchor_factor": adjustment_anchor_factor,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if extra:
            meta.update(extra)
        self.write_symbol_meta(symbol, meta)
        return meta

    def handle_reanchor_symbol(
        self,
        *,
        symbol: str,
        fresh_daily: pd.DataFrame,
        old_anchor_factor: float,
        new_anchor_factor: float,
        fresh_meta: dict[str, object],
    ) -> pd.DataFrame:
        """Rebase stored adjusted OHLC to a new anchor without changing traded units."""

        try:
            old_factor = float(old_anchor_factor)
            new_factor = float(new_anchor_factor)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_reanchor_factor") from exc
        if not (
            math.isfinite(old_factor)
            and math.isfinite(new_factor)
            and old_factor > 0.0
            and new_factor > 0.0
        ):
            raise ValueError("invalid_reanchor_factor")

        fresh_factor = fresh_meta.get("adjustment_anchor_factor")
        try:
            parsed_fresh_factor = float(cast(Any, fresh_factor))
        except (TypeError, ValueError) as exc:
            raise ValueError("fresh_anchor_factor_missing") from exc
        if not math.isclose(parsed_fresh_factor, new_factor, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("fresh_anchor_factor_mismatch")

        existing = self.fetch_all_daily_bars(symbol=symbol)
        rebased = existing.copy()
        if not rebased.empty:
            stored_factors = pd.to_numeric(
                rebased["adjustment_anchor_factor"], errors="coerce"
            ).dropna()
            if (
                not stored_factors.empty
                and not stored_factors.map(
                    lambda value: math.isclose(float(value), old_factor, rel_tol=0.0, abs_tol=1e-9)
                ).all()
            ):
                raise ValueError("stored_anchor_factor_mismatch")

            ratio = old_factor / new_factor
            for column in ("open", "high", "low", "close"):
                if column in rebased.columns:
                    rebased[column] = pd.to_numeric(rebased[column], errors="coerce") * ratio

            for column in (
                "price_series_mode",
                "adjustment_source",
                "adjustment_anchor_date",
                "adjustment_anchor_factor",
            ):
                if column in rebased.columns:
                    rebased[column] = cast(Any, fresh_meta.get(column))

        merged = fresh_daily.copy() if rebased.empty else pd.concat([rebased, fresh_daily], axis=0)
        return merged[~merged.index.duplicated(keep="last")].sort_index()

    def clone_to_shadow_paths(
        self,
        *,
        shadow_db_path: str | Path,
        shadow_package_root: str | Path,
    ) -> MarketWarehouse:
        """Create a new empty shadow warehouse without deleting/overwriting the current one."""
        return MarketWarehouse(
            db_path=shadow_db_path,
            package_root=shadow_package_root,
            package_writes_enabled=self.package_writes_enabled,
        )

    def _check_price_series_mode_consistency(self, payload: pd.DataFrame) -> None:
        """Fail-closed gate: reject writes that mix price_series_mode per symbol.

        For each symbol present in ``payload``, the incoming non-empty
        ``price_series_mode`` values must all be the same AND must match the
        mode already stored for that symbol in ``daily_bars``. A violation
        raises ``DataSourceError`` with the symbol, existing mode and new
        mode in the message so the caller can trace the contamination source.
        """
        mode_col = payload.get("price_series_mode")
        if mode_col is None:
            return
        payload_with_mode = payload.copy()
        payload_with_mode["price_series_mode"] = (
            payload_with_mode["price_series_mode"].astype(str).str.strip().str.lower()
        )
        payload_with_mode = payload_with_mode[payload_with_mode["price_series_mode"].str.len() > 0]
        if payload_with_mode.empty:
            return
        # 新行内部不得混合多种 mode（同一 symbol 的多行 mode 必须一致）
        symbol_modes = payload_with_mode.groupby("symbol")["price_series_mode"].agg(
            lambda series: sorted(set(series.dropna()))
        )
        for symbol, modes in symbol_modes.items():
            if len(modes) > 1:
                raise DataSourceError(
                    f"price_series_mode mismatch within incoming frame for "
                    f"symbol={symbol}: modes={modes}"
                )
        # 新行 mode vs 已有行 mode：从 daily_bars 读取每个 symbol 的全部
        # distinct mode（不只是最新一行）。只要已有行中出现与新行不同的
        # mode 就视为冲突——这比只比最新行更严格，能捕获历史中的 mixed 行。
        if not self._table_exists(_DAILY_TABLE):
            return
        incoming_symbols = sorted(set(symbol_modes.index))
        placeholders = ", ".join("?" for _ in incoming_symbols)
        query = f"""
            SELECT symbol, price_series_mode
            FROM (
                SELECT symbol, TRIM(price_series_mode) AS price_series_mode,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol, TRIM(price_series_mode)
                           ORDER BY date DESC
                       ) AS rn
                FROM {_DAILY_TABLE}
                WHERE symbol IN ({placeholders})
                  AND price_series_mode IS NOT NULL
                  AND TRIM(price_series_mode) != ''
            )
            WHERE rn = 1
        """
        with self._connect_readonly() as connection:
            rows = connection.execute(query, incoming_symbols).fetchall()
        for row in rows:
            existing_symbol = str(row[0]).strip()
            existing_mode = str(row[1] or "").strip().lower()
            if not existing_mode:
                continue
            new_modes = symbol_modes.get(existing_symbol)
            if not new_modes:
                continue
            new_mode = new_modes[0]
            if new_mode != existing_mode:
                raise DataSourceError(
                    f"price_series_mode mismatch for symbol={existing_symbol}: "
                    f"existing_mode={existing_mode}, new_mode={new_mode}"
                )

    def detect_price_series_mixed(
        self,
        *,
        symbols: list[str] | None = None,
    ) -> dict[str, object]:
        """Read-only detection of symbols whose daily_bars mix price_series_mode.

        Returns a dict with:
        - ``mixed_symbol_count``: number of symbols with >1 distinct non-empty mode.
        - ``mixed_symbols``: list of ``{symbol, modes, transition_dates}`` entries.
        - ``mode_distribution``: ``{mode: row_count}`` across the whole table.
        - ``empty_mode_rows``: rows where price_series_mode is NULL or empty.

        Opens the DuckDB read-only; never creates tables or mutates the file.
        """
        if not self._table_exists(_DAILY_TABLE):
            return {
                "mixed_symbol_count": 0,
                "mixed_symbols": [],
                "mode_distribution": {},
                "empty_mode_rows": 0,
            }
        params: list[object] = []
        symbol_filter = ""
        if symbols:
            normalized = sorted(
                {_normalize_symbol(symbol) for symbol in symbols if _normalize_symbol(symbol)}
            )
            if normalized:
                placeholders = ", ".join("?" for _ in normalized)
                symbol_filter = f"WHERE symbol IN ({placeholders})"
                params.extend(normalized)
        empty_cond = "price_series_mode IS NULL OR TRIM(price_series_mode) = ''"
        nonempty_cond = "price_series_mode IS NOT NULL AND TRIM(price_series_mode) != ''"
        with self._connect_readonly() as connection:
            mode_dist_rows = connection.execute(
                f"""
                SELECT COALESCE(TRIM(price_series_mode), '') AS mode, COUNT(*) AS cnt
                FROM {_DAILY_TABLE}
                {symbol_filter}
                GROUP BY mode
                ORDER BY cnt DESC
                """,
                params,
            ).fetchall()
            mode_distribution = {str(row[0]) or "(empty)": int(row[1]) for row in mode_dist_rows}

            empty_where = (
                f"{symbol_filter} AND ({empty_cond})" if symbol_filter else f"WHERE {empty_cond}"
            )
            empty_count = connection.execute(
                f"SELECT COUNT(*) FROM {_DAILY_TABLE} {empty_where}",
                params,
            ).fetchone()
            empty_mode_rows = int(empty_count[0]) if empty_count else 0

            mixed_where = (
                f"{symbol_filter} AND {nonempty_cond}"
                if symbol_filter
                else f"WHERE {nonempty_cond}"
            )
            mixed_rows = connection.execute(
                f"""
                SELECT symbol, COUNT(DISTINCT price_series_mode) AS mode_count
                FROM {_DAILY_TABLE}
                {mixed_where}
                GROUP BY symbol
                HAVING COUNT(DISTINCT price_series_mode) > 1
                ORDER BY symbol
                """,
                params,
            ).fetchall()

            mixed_symbols: list[dict[str, object]] = []
            for mixed_row in mixed_rows:
                mixed_symbol = str(mixed_row[0])
                transition_rows = connection.execute(
                    f"""
                    SELECT date, price_series_mode
                    FROM (
                        SELECT date, price_series_mode,
                               LAG(price_series_mode) OVER (ORDER BY date) AS prev_mode
                        FROM {_DAILY_TABLE}
                        WHERE symbol = ?
                          AND price_series_mode IS NOT NULL
                          AND TRIM(price_series_mode) != ''
                    )
                    WHERE price_series_mode != prev_mode OR prev_mode IS NULL
                    ORDER BY date
                    """,
                    [mixed_symbol],
                ).fetchall()
                transitions: list[dict[str, str]] = []
                for t_row in transition_rows:
                    transitions.append(
                        {
                            "date": str(t_row[0]),
                            "mode": str(t_row[1]),
                        }
                    )
                mixed_symbols.append(
                    {
                        "symbol": mixed_symbol,
                        "modes": sorted({str(t["mode"]) for t in transitions}),
                        "transition_dates": transitions,
                    }
                )
        return {
            "mixed_symbol_count": len(mixed_symbols),
            "mixed_symbols": mixed_symbols,
            "mode_distribution": mode_distribution,
            "empty_mode_rows": empty_mode_rows,
        }

    def has_daily_data(self) -> bool:
        if not self._table_exists(_DAILY_TABLE):
            return False
        with self._connect_readonly() as connection:
            count = connection.execute(f"SELECT COUNT(*) FROM {_DAILY_TABLE}").fetchone()
        return bool(count and int(count[0]) > 0)

    def list_symbols(self) -> list[str]:
        if not self._table_exists(_DAILY_TABLE):
            return []
        with self._connect_readonly() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT symbol FROM {_DAILY_TABLE} ORDER BY symbol"
            ).fetchall()
        return [str(row[0]).strip() for row in rows if str(row[0]).strip()]

    def latest_daily_date(self, *, symbol: str) -> date | None:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_DAILY_TABLE):
            return None
        with self._connect_readonly() as connection:
            row = connection.execute(
                f"SELECT MAX(date) FROM {_DAILY_TABLE} WHERE symbol = ?",
                [normalized_symbol],
            ).fetchone()
        return _coerce_date(row[0] if row else None)

    def latest_daily_dates(self, *, symbols: list[str] | None = None) -> dict[str, date]:
        if not self._table_exists(_DAILY_TABLE):
            return {}
        where_clause = ""
        params: list[object] = []
        normalized_symbols: list[str] = []
        if symbols:
            normalized_symbols = [
                normalized
                for normalized in (_normalize_symbol(symbol) for symbol in symbols)
                if normalized
            ]
            normalized_symbols = sorted(set(normalized_symbols))
            if not normalized_symbols:
                return {}
            placeholders = ", ".join("?" for _ in normalized_symbols)
            where_clause = f"WHERE symbol IN ({placeholders})"
            params.extend(normalized_symbols)
        query = f"""
            SELECT symbol, MAX(date) AS latest_date
            FROM {_DAILY_TABLE}
            {where_clause}
            GROUP BY symbol
        """
        with self._connect_readonly() as connection:
            rows = connection.execute(query, params).fetchall()
        latest: dict[str, date] = {}
        for raw_symbol, raw_date in rows:
            normalized_symbol = _normalize_symbol(raw_symbol)
            parsed = _coerce_date(raw_date)
            if normalized_symbol and parsed is not None:
                latest[normalized_symbol] = parsed
        return latest

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Batch-fetch per-symbol daily bars (last ``lookback_days`` rows each).

        Single DuckDB query returning one frame with all columns needed for
        quality scoring across the whole universe. Deliberately avoids
        per-symbol ``fetch_daily_bars`` calls so 5000+ symbols can be scored in
        one pass without opening thousands of connections or DataFrames.

        ``end_date``（as-of 契约）：非 None 时先在 SQL 里把行截断到
        ``date <= end_date``，**再**按 symbol 取最近 ``lookback_days`` 根。
        截断必须发生在窗口编号之前——"先取最近 N 根再过滤"会把历史日期的
        as-of 批量质量数据悄悄混入未来行（历史回测泄露点之一）。

        Returns an empty frame when the daily table is missing or no requested
        symbol has data. Callers must treat empty as "batch unavailable" and
        enter an explicit degraded fallback rather than silently falling back
        to per-symbol reads.
        """
        if not self._table_exists(_DAILY_TABLE):
            return pd.DataFrame()
        normalized_symbols = sorted(
            {_normalize_symbol(symbol) for symbol in (symbols or []) if _normalize_symbol(symbol)}
        )
        if not normalized_symbols:
            return pd.DataFrame()
        symbols_df = pd.DataFrame({"symbol": normalized_symbols})
        limit = max(1, int(lookback_days))
        end_clause = ""
        params: list[object] = []
        if end_date is not None:
            end_clause = "AND t.date <= ?"
            params.append(end_date.isoformat())
        params.append(limit)
        with self._connect_readonly() as connection:
            connection.register("_uqs_symbols", symbols_df)
            try:
                frame = cast(
                    pd.DataFrame,
                    connection.execute(
                        f"""
                        WITH ranked AS (
                            SELECT
                                t.*,
                                ROW_NUMBER() OVER (
                                    PARTITION BY t.symbol ORDER BY t.date DESC
                                ) AS _rn
                            FROM {_DAILY_TABLE} t
                            WHERE t.symbol IN (SELECT symbol FROM _uqs_symbols)
                            {end_clause}
                        )
                        SELECT * FROM ranked WHERE _rn <= ?
                        ORDER BY symbol, date
                        """,
                        params,
                    ).fetch_df(),
                )
            finally:
                connection.unregister("_uqs_symbols")
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).sort_values(["symbol", "date"])
        return frame

    def background_data_quality_snapshot(self) -> dict[str, object]:
        if not self._table_exists(_DAILY_TABLE):
            return {
                "status": "missing",
                "reason": "daily_table_missing",
                "db_path": str(self.db_path),
                "symbols_total": 0,
                "latest_trade_date": "",
                "symbols_on_latest_trade_date": 0,
                "symbols_stale": 0,
                "latest_trade_date_coverage_ratio": 0.0,
                "background_complete_count": 0,
                "background_complete_ratio": 0.0,
                "source_distribution": {},
                "fields": {},
                "activity_counts": {},
                "stale_symbols_sample": [],
            }
        with self._connect_readonly() as connection:
            summary_row = connection.execute(
                f"""
                WITH latest_date AS (
                    SELECT MAX(date) AS latest_trade_date
                    FROM {_DAILY_TABLE}
                )
                SELECT
                    CAST((SELECT latest_trade_date FROM latest_date) AS VARCHAR)
                        AS latest_trade_date,
                    COUNT(DISTINCT symbol) AS symbols_total,
                    COUNT(
                        DISTINCT CASE
                            WHEN date = (SELECT latest_trade_date FROM latest_date) THEN symbol
                            ELSE NULL
                        END
                    ) AS symbols_on_latest_trade_date
                FROM {_DAILY_TABLE}
                """
            ).fetchone()
            latest_trade_date = str(summary_row[0] or "") if summary_row else ""
            symbols_total = int(summary_row[1]) if summary_row and summary_row[1] is not None else 0
            symbols_on_latest_trade_date = (
                int(summary_row[2]) if summary_row and summary_row[2] is not None else 0
            )
            if not latest_trade_date or symbols_total <= 0:
                return {
                    "status": "empty",
                    "reason": "daily_table_empty",
                    "db_path": str(self.db_path),
                    "symbols_total": 0,
                    "latest_trade_date": "",
                    "symbols_on_latest_trade_date": 0,
                    "symbols_stale": 0,
                    "latest_trade_date_coverage_ratio": 0.0,
                    "background_complete_count": 0,
                    "background_complete_ratio": 0.0,
                    "source_distribution": {},
                    "fields": {},
                    "activity_counts": {},
                    "stale_symbols_sample": [],
                }

            latest_frame = cast(
                pd.DataFrame,
                connection.execute(
                    f"""
                    SELECT
                        symbol,
                        background_data_source,
                        background_data_complete,
                        holder_count,
                        block_trade_net,
                        financing_balance,
                        margin_financing_balance,
                        northbound_net,
                        dragon_tiger_flag
                    FROM {_DAILY_TABLE}
                    WHERE date = ?
                    ORDER BY symbol
                    """,
                    [latest_trade_date],
                ).fetch_df(),
            )
            stale_rows = connection.execute(
                f"""
                WITH latest_date AS (
                    SELECT MAX(date) AS latest_trade_date
                    FROM {_DAILY_TABLE}
                ),
                symbol_latest AS (
                    SELECT symbol, MAX(date) AS latest_symbol_date
                    FROM {_DAILY_TABLE}
                    GROUP BY symbol
                )
                SELECT symbol
                FROM symbol_latest
                WHERE latest_symbol_date < (SELECT latest_trade_date FROM latest_date)
                ORDER BY symbol
                LIMIT 20
                """
            ).fetchall()

        symbols_stale = max(0, symbols_total - symbols_on_latest_trade_date)
        latest_rows_total = int(len(latest_frame))
        background_complete_series = (
            pd.to_numeric(pd.Series(latest_frame["background_data_complete"]), errors="coerce")
            if "background_data_complete" in latest_frame.columns
            else pd.Series(dtype=float)
        )
        background_complete_count = (
            int(background_complete_series.fillna(0.0).ne(0.0).sum())
            if latest_rows_total > 0
            else 0
        )
        freshness_ratio = (
            round(symbols_on_latest_trade_date / symbols_total, 6) if symbols_total > 0 else 0.0
        )
        background_complete_ratio = (
            round(background_complete_count / latest_rows_total, 6)
            if latest_rows_total > 0
            else 0.0
        )
        field_metrics = {
            field: _background_field_metrics(latest_frame, field)
            for field in [
                "holder_count",
                "block_trade_net",
                "financing_balance",
                "margin_financing_balance",
                "northbound_net",
                "dragon_tiger_flag",
                "background_data_complete",
            ]
        }
        source_distribution = _background_source_distribution(latest_frame)
        status_reasons: list[str] = []
        if symbols_stale > 0:
            status_reasons.append("latest_trade_date_not_full_universe")
        if background_complete_count < latest_rows_total:
            status_reasons.append("background_data_incomplete_on_latest_trade_date")
        status = "ok" if not status_reasons else "partial"
        return {
            "status": status,
            "reason": ",".join(status_reasons),
            "status_reasons": status_reasons,
            "db_path": str(self.db_path),
            "symbols_total": symbols_total,
            "latest_trade_date": latest_trade_date,
            "symbols_on_latest_trade_date": symbols_on_latest_trade_date,
            "symbols_stale": symbols_stale,
            "latest_trade_date_coverage_ratio": freshness_ratio,
            "background_complete_count": background_complete_count,
            "background_complete_ratio": background_complete_ratio,
            "source_distribution": source_distribution,
            "fields": field_metrics,
            "activity_counts": {
                "holder_count_non_null": int(
                    field_metrics["holder_count"].get("non_null_count", 0)
                ),
                "block_trade_non_zero": int(
                    field_metrics["block_trade_net"].get("non_zero_count", 0)
                ),
                "financing_balance_non_zero": int(
                    field_metrics["financing_balance"].get("non_zero_count", 0)
                ),
                "margin_financing_balance_non_zero": int(
                    field_metrics["margin_financing_balance"].get("non_zero_count", 0)
                ),
                "northbound_net_non_zero": int(
                    field_metrics["northbound_net"].get("non_zero_count", 0)
                ),
                "dragon_tiger_flag_non_zero": int(
                    field_metrics["dragon_tiger_flag"].get("non_zero_count", 0)
                ),
            },
            "stale_symbols_sample": [
                str(row[0]).strip() for row in stale_rows if str(row[0]).strip()
            ],
        }

    def replace_daily_bars(self, *, symbol: str, frame: pd.DataFrame) -> None:
        normalized_symbol = _normalize_symbol(symbol)
        normalized = _normalize_daily_frame(frame=frame, symbol=normalized_symbol)
        if normalized.empty:
            return
        payload = normalized.reset_index().rename(columns={"index": "date"})
        payload.insert(0, "symbol", normalized_symbol)
        payload["date"] = pd.to_datetime(payload["date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["date"])
        self.ensure_schema()
        with self._connect_write() as connection:
            connection.register("daily_stage_df", payload)
            connection.execute(
                f"DELETE FROM {_DAILY_TABLE} WHERE symbol = ?",
                [normalized_symbol],
            )
            connection.execute(
                f"""
                INSERT INTO {_DAILY_TABLE} (
                    symbol, date, {", ".join(_SELECTED_COLUMNS)}
                )
                SELECT symbol, date, {", ".join(_SELECTED_COLUMNS)}
                FROM daily_stage_df
                ORDER BY date
                """
            )
            connection.unregister("daily_stage_df")

    def upsert_daily_bars(
        self,
        *,
        frame: pd.DataFrame,
        overwrite_existing: bool = False,
        enforce_price_series_mode: bool = True,
    ) -> int:
        """Idempotent batch upsert of normalized daily bars. Returns rows stored.

        ``frame`` must carry ``symbol`` and ``date`` columns plus any subset
        of the daily_bars columns; columns the caller cannot provide are
        stored as NULL instead of being fabricated (e.g. the vendor ZIP
        baseline never fabricates tushare-only fields like roe or
        holder_count).

        Default semantics (``overwrite_existing=False``) leave already-present
        (symbol, date) rows untouched, mirroring the overlay merge rule where
        the delta row wins: a full ZIP baseline import therefore never
        downgrades rows that tushare-based writers already enriched. Pass
        ``overwrite_existing=True`` to replace existing rows with the incoming
        values (used by the qfq factor-drift refresh path). Read-only
        warehouses refuse with ``DataSourceError``.

        Price-series mode consistency (``enforce_price_series_mode=True``,
        default): when the incoming frame carries a non-empty
        ``price_series_mode`` column, each symbol's new mode is compared
        against the mode already stored for that symbol. A mismatch raises
        ``DataSourceError`` with the symbol, existing mode and new mode in the
        message — this is the fail-closed gate that prevents qfq/raw
        contamination at the write boundary. Callers that deliberately mix
        modes (e.g. shadow rebuild tools operating on an isolated copy) can
        pass ``enforce_price_series_mode=False`` to bypass the check.
        """
        if frame is None or frame.empty:
            return 0
        payload = frame.copy()
        if "symbol" not in payload.columns or "date" not in payload.columns:
            raise DataSourceError("upsert_daily_bars requires symbol and date columns")
        payload["symbol"] = (
            payload["symbol"]
            .astype(str)
            .str.strip()
            .str.upper()
            .str.replace(r"\.(SH|SZ|BJ)$", "", regex=True)
        )
        payload = payload[payload["symbol"].str.fullmatch(r"\d{6}")]
        payload["date"] = pd.to_datetime(payload["date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["date"])
        if payload.empty:
            return 0
        for column in _SELECTED_COLUMNS:
            if column not in payload.columns:
                if column in _DAILY_NUMERIC_COLUMNS:
                    payload[column] = float("nan")
                elif column in _DAILY_BOOLEAN_COLUMNS:
                    payload[column] = False
                else:
                    payload[column] = ""
        columns = ["symbol", "date", *_SELECTED_COLUMNS]
        self.ensure_schema()
        if enforce_price_series_mode and "price_series_mode" in payload.columns:
            self._check_price_series_mode_consistency(payload)
        with self._connect_write() as connection:
            connection.register("daily_stage_df", payload)
            try:
                if overwrite_existing:
                    # DELETE + INSERT 必须同事务：两步之间进程崩溃会把已删
                    # 未插的缺口留在库里（重跑可自愈，但中间态无日志可查）。
                    connection.execute("BEGIN TRANSACTION")
                    try:
                        connection.execute(
                            f"""
                            DELETE FROM {_DAILY_TABLE}
                            WHERE (symbol, date) IN (
                                SELECT symbol, date FROM daily_stage_df
                            )
                            """
                        )
                        connection.execute(
                            f"""
                            INSERT INTO {_DAILY_TABLE} ({", ".join(columns)})
                            SELECT {", ".join(columns)}
                            FROM daily_stage_df
                            ORDER BY date
                            """
                        )
                    except Exception:
                        connection.execute("ROLLBACK")
                        raise
                    connection.execute("COMMIT")
                    stored = len(payload)
                else:
                    stored = int(
                        connection.execute(
                            f"""
                            SELECT COUNT(*)
                            FROM daily_stage_df AS stage
                            ANTI JOIN {_DAILY_TABLE} AS existing
                              ON existing.symbol = stage.symbol
                             AND existing.date = stage.date
                            """
                        ).fetchone()[0]
                    )
                    connection.execute(
                        f"""
                        INSERT INTO {_DAILY_TABLE} ({", ".join(columns)})
                        SELECT {", ".join(columns)}
                        FROM daily_stage_df AS stage
                        ANTI JOIN {_DAILY_TABLE} AS existing
                          ON existing.symbol = stage.symbol
                         AND existing.date = stage.date
                        ORDER BY date
                        """
                    )
            finally:
                connection.unregister("daily_stage_df")
        return stored

    def replace_intraday_summary(
        self,
        *,
        symbol: str,
        interval: str,
        frame: pd.DataFrame,
    ) -> None:
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None:
            return
        normalized_symbol = _normalize_symbol(symbol)
        normalized = _normalize_intraday_frame(frame=frame, symbol=normalized_symbol)
        if normalized.empty:
            return
        payload = normalized.reset_index().rename(columns={"index": "date"})
        payload.insert(0, "symbol", normalized_symbol)
        payload["date"] = pd.to_datetime(payload["date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["date"])
        self.ensure_schema()
        with self._connect_write() as connection:
            registered = False
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.register("intraday_stage_df", payload)
                registered = True
                connection.execute(
                    f"DELETE FROM {table_name} WHERE symbol = ?",
                    [normalized_symbol],
                )
                connection.execute(
                    f"""
                    INSERT INTO {table_name} (
                        symbol, date, {", ".join(_INTRADAY_COLUMNS)}
                    )
                    SELECT symbol, date, {", ".join(_INTRADAY_COLUMNS)}
                    FROM intraday_stage_df
                    ORDER BY date
                    """
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                if registered:
                    connection.unregister("intraday_stage_df")

    def upsert_intraday_summaries(
        self,
        *,
        interval: str,
        frame: pd.DataFrame,
    ) -> dict[str, int]:
        """Upsert a multi-symbol summary batch in one DuckDB transaction."""
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None:
            raise DataSourceError(f"unsupported intraday interval: {interval}")
        if frame is None or frame.empty:
            return {"rows": 0, "conflicts": 0}
        if "symbol" not in frame.columns or "date" not in frame.columns:
            raise DataSourceError("intraday summary batch requires symbol and date columns")
        pieces: list[pd.DataFrame] = []
        for raw_symbol, group in frame.groupby("symbol", sort=False):
            symbol = _normalize_symbol(str(raw_symbol))
            if not symbol:
                continue
            normalized = _normalize_intraday_frame(
                frame=group.drop(columns=["symbol"]).set_index("date"),
                symbol=symbol,
            )
            if normalized.empty:
                continue
            payload = normalized.reset_index().rename(columns={"index": "date"})
            payload.insert(0, "symbol", symbol)
            pieces.append(payload)
        if not pieces:
            return {"rows": 0, "conflicts": 0}
        payload = pd.concat(pieces, axis=0, sort=False, ignore_index=True)
        payload["date"] = pd.to_datetime(payload["date"], errors="coerce").dt.date
        payload = payload.dropna(subset=["symbol", "date"])
        payload = payload.drop_duplicates(subset=["symbol", "date"], keep="last")
        self.ensure_schema()
        with self._connect_write() as connection:
            connection.register("intraday_stage_df", payload)
            try:
                conflicts = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM intraday_stage_df AS stage
                        INNER JOIN {table_name} AS existing
                          ON existing.symbol = stage.symbol
                         AND existing.date = stage.date
                        """
                    ).fetchone()[0]
                )
                connection.execute("BEGIN TRANSACTION")
                try:
                    connection.execute(
                        f"""
                        DELETE FROM {table_name}
                        USING intraday_stage_df AS stage
                        WHERE {table_name}.symbol = stage.symbol
                          AND {table_name}.date = stage.date
                        """
                    )
                    connection.execute(
                        f"""
                        INSERT INTO {table_name} (
                            symbol, date, {", ".join(_INTRADAY_COLUMNS)}
                        )
                        SELECT symbol, date, {", ".join(_INTRADAY_COLUMNS)}
                        FROM intraday_stage_df
                        ORDER BY symbol, date
                        """
                    )
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute("COMMIT")
            finally:
                connection.unregister("intraday_stage_df")
        return {"rows": len(payload), "conflicts": conflicts}

    def delete_intraday_range(
        self,
        *,
        interval: str,
        start_date: date,
        end_date: date,
    ) -> int:
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None or not self._table_exists(table_name):
            return 0
        with self._connect_write() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE date BETWEEN ? AND ?",
                [start_date.isoformat(), end_date.isoformat()],
            ).fetchone()
            connection.execute(
                f"DELETE FROM {table_name} WHERE date BETWEEN ? AND ?",
                [start_date.isoformat(), end_date.isoformat()],
            )
        return int(row[0]) if row else 0

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_DAILY_TABLE):
            return pd.DataFrame()
        filters = ["symbol = ?"]
        params: list[object] = [normalized_symbol]
        if end_date is not None:
            filters.append("date <= ?")
            params.append(end_date.isoformat())
        query = f"""
            SELECT date, {", ".join(_SELECTED_COLUMNS)}
            FROM (
                SELECT date, {", ".join(_SELECTED_COLUMNS)}
                FROM {_DAILY_TABLE}
                WHERE {" AND ".join(filters)}
                ORDER BY date DESC
                LIMIT ?
            ) AS recent
            ORDER BY date ASC
        """
        params.append(max(1, int(lookback_days)))
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(query, params).fetch_df(),
            )
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
        return _normalize_daily_frame(frame=frame, symbol=normalized_symbol)

    def fetch_all_daily_bars(self, *, symbol: str) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(_DAILY_TABLE):
            return pd.DataFrame()
        query = f"""
            SELECT date, {", ".join(_SELECTED_COLUMNS)}
            FROM {_DAILY_TABLE}
            WHERE symbol = ?
            ORDER BY date ASC
        """
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(query, [normalized_symbol]).fetch_df(),
            )
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
        return _normalize_daily_frame(frame=frame, symbol=normalized_symbol)

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        table_name = _INTRADAY_TABLES.get(interval)
        normalized_symbols = sorted(
            {
                normalized
                for normalized in (_normalize_symbol(symbol) for symbol in symbols)
                if normalized
            }
        )
        if table_name is None or not normalized_symbols or not self._table_exists(table_name):
            return {}
        placeholders = ", ".join("?" for _ in normalized_symbols)
        query = f"""
            SELECT symbol, date, {", ".join(_INTRADAY_COLUMNS)}
            FROM (
                SELECT
                    symbol,
                    date,
                    {", ".join(_INTRADAY_COLUMNS)},
                    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS row_num
                FROM {table_name}
                WHERE symbol IN ({placeholders})
            ) AS recent
            WHERE row_num <= ?
            ORDER BY symbol, date
        """
        params: list[object] = [*normalized_symbols, max(1, int(lookback_days))]
        with self._connect_readonly() as connection:
            frame = cast(pd.DataFrame, connection.execute(query, params).fetch_df())
        if frame.empty:
            return {}
        result: dict[str, pd.DataFrame] = {}
        for raw_symbol, group in frame.groupby("symbol", sort=False):
            symbol = _normalize_symbol(str(raw_symbol))
            normalized = group.drop(columns=["symbol"]).copy()
            normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
            normalized = normalized.dropna(subset=["date"]).set_index("date").sort_index()
            result[symbol] = _normalize_intraday_frame(frame=normalized, symbol=symbol)
        return result

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None:
            return pd.DataFrame()
        normalized_symbol = _normalize_symbol(symbol)
        query = f"""
            SELECT date, {", ".join(_INTRADAY_COLUMNS)}
            FROM (
                SELECT date, {", ".join(_INTRADAY_COLUMNS)}
                FROM {table_name}
                WHERE symbol = ?
                ORDER BY date DESC
                LIMIT ?
            ) AS recent
            ORDER BY date ASC
        """
        if not self._table_exists(table_name):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(
                    query,
                    [normalized_symbol, max(1, int(lookback_days))],
                ).fetch_df(),
            )
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
        return _normalize_intraday_frame(frame=frame, symbol=normalized_symbol)

    def intraday_coverage(self, *, interval: str) -> dict[str, object]:
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None or not self._table_exists(table_name):
            return {"interval": interval, "rows": 0, "symbols": 0, "min_date": "", "max_date": ""}
        with self._connect_readonly() as connection:
            row = connection.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM {table_name}"
            ).fetchone()
        return {
            "interval": interval,
            "rows": int(row[0]) if row else 0,
            "symbols": int(row[1]) if row else 0,
            "min_date": str(row[2]) if row and row[2] is not None else "",
            "max_date": str(row[3]) if row and row[3] is not None else "",
        }

    def fetch_all_intraday_summary(self, *, symbol: str, interval: str) -> pd.DataFrame:
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None:
            return pd.DataFrame()
        normalized_symbol = _normalize_symbol(symbol)
        query = f"""
            SELECT date, {", ".join(_INTRADAY_COLUMNS)}
            FROM {table_name}
            WHERE symbol = ?
            ORDER BY date ASC
        """
        if not self._table_exists(table_name):
            return pd.DataFrame()
        with self._connect_readonly() as connection:
            frame = cast(
                pd.DataFrame,
                connection.execute(query, [normalized_symbol]).fetch_df(),
            )
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
        return _normalize_intraday_frame(frame=frame, symbol=normalized_symbol)

    def latest_intraday_date(self, *, symbol: str, interval: str) -> date | None:
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None:
            return None
        normalized_symbol = _normalize_symbol(symbol)
        if not self._table_exists(table_name):
            return None
        with self._connect_readonly() as connection:
            row = connection.execute(
                f"SELECT MAX(date) FROM {table_name} WHERE symbol = ?",
                [normalized_symbol],
            ).fetchone()
        return _coerce_date(row[0] if row else None)

    def latest_intraday_dates(
        self,
        *,
        interval: str,
        symbols: list[str] | None = None,
    ) -> dict[str, date]:
        table_name = _INTRADAY_TABLES.get(interval)
        if table_name is None:
            return {}
        if not self._table_exists(table_name):
            return {}
        where_clause = ""
        params: list[object] = []
        normalized_symbols: list[str] = []
        if symbols:
            normalized_symbols = [
                normalized
                for normalized in (_normalize_symbol(symbol) for symbol in symbols)
                if normalized
            ]
            normalized_symbols = sorted(set(normalized_symbols))
            if not normalized_symbols:
                return {}
            placeholders = ", ".join("?" for _ in normalized_symbols)
            where_clause = f"WHERE symbol IN ({placeholders})"
            params.extend(normalized_symbols)
        query = f"""
            SELECT symbol, MAX(date) AS latest_date
            FROM {table_name}
            {where_clause}
            GROUP BY symbol
        """
        with self._connect_readonly() as connection:
            rows = connection.execute(query, params).fetchall()
        latest: dict[str, date] = {}
        for raw_symbol, raw_date in rows:
            normalized_symbol = _normalize_symbol(raw_symbol)
            parsed = _coerce_date(raw_date)
            if normalized_symbol and parsed is not None:
                latest[normalized_symbol] = parsed
        return latest

    def bootstrap_from_offline_package(self, *, source_root: str | Path) -> dict[str, object]:
        source = Path(source_root).expanduser()
        symbols = list_package_symbols(source)
        daily_written = 0
        intraday_written: dict[str, int] = {"1m": 0, "5m": 0}
        failed_samples: list[dict[str, str]] = []
        for symbol in symbols:
            try:
                daily_frame = load_package_daily_bars(source_root=source, symbol=symbol)
                if not daily_frame.empty:
                    self.replace_daily_bars(symbol=symbol, frame=daily_frame)
                    daily_written += 1
                for interval in _INTRADAY_TABLES:
                    summary_frame = load_package_intraday_summary(
                        source_root=source,
                        symbol=symbol,
                        interval=interval,
                    )
                    if summary_frame.empty:
                        continue
                    self.replace_intraday_summary(
                        symbol=symbol,
                        interval=interval,
                        frame=summary_frame,
                    )
                    intraday_written[interval] += 1
            except Exception as exc:
                if len(failed_samples) < 20:
                    failed_samples.append(
                        {"symbol": symbol, "reason": f"{exc.__class__.__name__}:{exc}"}
                    )
        self.refresh_package_manifests()
        return {
            "status": "ok" if not failed_samples else "partial",
            "source_root": str(source),
            "symbols_total": len(symbols),
            "daily_written": daily_written,
            "intraday_written": intraday_written,
            "failed": len(failed_samples),
            "failed_samples": failed_samples,
        }

    def has_materialized_package(self) -> bool:
        if not self.package_writes_enabled:
            return False
        bars_root = self.package_root / "bars"
        if not bars_root.exists() or not bars_root.is_dir():
            return False
        for pattern in ("*.csv", "*.csv.gz", "*.parquet"):
            if any(bars_root.glob(pattern)):
                return True
        return False

    def materialize_runtime_package(
        self,
        *,
        symbols: list[str] | None = None,
        intervals: list[str] | None = None,
    ) -> dict[str, object]:
        if not self.package_writes_enabled:
            return {
                "status": "skipped",
                "reason": "package_writes_disabled",
                "symbols_total": 0,
                "daily_written": 0,
                "intraday_written": {interval: 0 for interval in _INTRADAY_TABLES},
            }
        if not self.has_daily_data():
            return {
                "status": "skipped",
                "reason": "empty_database",
                "symbols_total": 0,
                "daily_written": 0,
                "intraday_written": {interval: 0 for interval in _INTRADAY_TABLES},
            }

        target_symbols = (
            [
                normalized
                for normalized in (_normalize_symbol(symbol) for symbol in (symbols or []))
                if normalized
            ]
            if symbols is not None
            else self.list_symbols()
        )
        target_symbols = sorted(dict.fromkeys(target_symbols))
        interval_list = [
            interval
            for interval in (intervals or list(_INTRADAY_TABLES))
            if interval in _INTRADAY_TABLES
        ]
        intraday_written = {interval: 0 for interval in _INTRADAY_TABLES}
        if not target_symbols:
            return {
                "status": "skipped",
                "reason": "empty_symbol_universe",
                "symbols_total": 0,
                "daily_written": 0,
                "intraday_written": intraday_written,
            }

        daily_written = 0
        for symbol in target_symbols:
            daily_frame = self.fetch_all_daily_bars(symbol=symbol)
            if not daily_frame.empty:
                write_package_daily_bars(
                    package_root=self.package_root,
                    symbol=symbol,
                    frame=daily_frame,
                )
                daily_written += 1

            # Persist symbol-specific meta
            meta = self.read_symbol_meta(symbol)
            if meta.get("price_series_mode"):
                self.write_symbol_meta(symbol, meta)
            for interval in interval_list:
                intraday_frame = self.fetch_all_intraday_summary(symbol=symbol, interval=interval)
                if intraday_frame.empty:
                    continue
                write_package_intraday_summary(
                    package_root=self.package_root,
                    symbol=symbol,
                    interval=interval,
                    frame=intraday_frame,
                )
                intraday_written[interval] += 1

        manifest_refresh = self.refresh_package_manifests()
        return {
            "status": "ok",
            "symbols_total": len(target_symbols),
            "daily_written": daily_written,
            "intraday_written": intraday_written,
            "manifest_refresh": manifest_refresh,
        }

    def refresh_package_manifests(self) -> dict[str, object]:
        if not self.package_writes_enabled:
            return {
                "status": "skipped",
                "reason": "package_writes_disabled",
                "package_root": str(self.package_root),
            }
        self.package_root.mkdir(parents=True, exist_ok=True)
        daily_summary = self._daily_summary()
        daily_file_summary = _package_daily_file_summary(self.package_root)
        intraday_summary = {
            interval: self._intraday_summary(interval) for interval in _INTRADAY_TABLES
        }
        intraday_file_summary = {
            interval: _package_intraday_file_summary(self.package_root, interval)
            for interval in _INTRADAY_TABLES
        }

        existing_daily = _read_json(self.package_root / "manifest.json")
        db_symbols_total = int(cast(Any, daily_summary["symbols_total"]))
        package_symbols_total = int(cast(Any, daily_file_summary["symbols_total"]))
        missing_daily_symbols = sorted(
            set(cast(list[str], daily_summary["symbols"]))
            - set(cast(list[str], daily_file_summary["symbols"]))
        )
        extra_daily_symbols = sorted(
            set(cast(list[str], daily_file_summary["symbols"]))
            - set(cast(list[str], daily_summary["symbols"]))
        )
        contract = self.price_series_contract()
        daily_manifest = {
            **existing_daily,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "output_root": str(self.package_root.resolve()),
            "package_version": str(existing_daily.get("package_version", "warehouse-v1")),
            "price_series_mode": contract.get("price_series_mode")
            or existing_daily.get("price_series_mode", ""),
            "adjustment_source": contract.get("adjustment_source")
            or existing_daily.get("adjustment_source", ""),
            "adjustment_anchor_date": contract.get("adjustment_anchor_date")
            or existing_daily.get("adjustment_anchor_date", ""),
            "adjustment_anchor_factor": contract.get("adjustment_anchor_factor")
            if contract.get("adjustment_anchor_factor") is not None
            else existing_daily.get("adjustment_anchor_factor"),
            "db_symbols_total": db_symbols_total,
            "package_symbol_files_total": package_symbols_total,
            "symbol_files_total": db_symbols_total,
            "symbol_files_written": package_symbols_total,
            "symbol_files_failed": len(missing_daily_symbols),
            "package_consistent": (
                db_symbols_total == package_symbols_total
                and not missing_daily_symbols
                and not extra_daily_symbols
            ),
            "missing_symbol_files_sample": missing_daily_symbols[:20],
            "extra_symbol_files_sample": extra_daily_symbols[:20],
            "date_min": daily_summary["date_min"],
            "date_max": daily_summary["date_max"],
        }
        (self.package_root / "manifest.json").write_text(
            json.dumps(daily_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        existing_intraday = _read_json(self.package_root / "intraday_summary_manifest.json")
        intraday_manifest = {
            **existing_intraday,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "output_root": str(self.package_root.resolve()),
            "db_symbols_total": db_symbols_total,
            "package_symbol_files_total": package_symbols_total,
            "package_consistent": bool(daily_manifest["package_consistent"]),
            "symbols_total": db_symbols_total,
            "intervals": {
                interval: {
                    "db_symbols_total": summary["symbols_total"],
                    "package_symbol_files_total": intraday_file_summary[interval]["symbols_total"],
                    "symbols_total": summary["symbols_total"],
                    "files_written": intraday_file_summary[interval]["symbols_total"],
                    "failed": max(
                        0,
                        int(cast(Any, summary["symbols_total"]))
                        - int(
                            cast(
                                Any,
                                intraday_file_summary[interval]["symbols_total"],
                            )
                        ),
                    ),
                    "latest_date_max": summary["latest_date_max"],
                    "target_end_date": summary["latest_date_max"],
                }
                for interval, summary in intraday_summary.items()
            },
        }
        (self.package_root / "intraday_summary_manifest.json").write_text(
            json.dumps(intraday_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "daily_manifest_path": str((self.package_root / "manifest.json").resolve()),
            "intraday_manifest_path": str(
                (self.package_root / "intraday_summary_manifest.json").resolve()
            ),
            "db_symbols_total": db_symbols_total,
            "package_symbol_files_total": package_symbols_total,
            "package_consistent": bool(daily_manifest["package_consistent"]),
            "missing_symbol_files_total": len(missing_daily_symbols),
            "missing_symbol_files_sample": missing_daily_symbols[:20],
            "extra_symbol_files_total": len(extra_daily_symbols),
            "extra_symbol_files_sample": extra_daily_symbols[:20],
        }

    def _daily_summary(self) -> dict[str, object]:
        if not self._table_exists(_DAILY_TABLE):
            return {"symbols_total": 0, "symbols": [], "date_min": "", "date_max": ""}
        with self._connect_readonly() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT symbol) AS symbols_total,
                    CAST(MIN(date) AS VARCHAR) AS date_min,
                    CAST(MAX(date) AS VARCHAR) AS date_max
                FROM {_DAILY_TABLE}
                """
            ).fetchone()
            symbol_rows = connection.execute(
                f"SELECT DISTINCT symbol FROM {_DAILY_TABLE} ORDER BY symbol"
            ).fetchall()
        return {
            "symbols_total": int(row[0]) if row and row[0] is not None else 0,
            "symbols": [str(item[0]).strip() for item in symbol_rows if str(item[0]).strip()],
            "date_min": str(row[1] or ""),
            "date_max": str(row[2] or ""),
        }

    def _intraday_summary(self, interval: str) -> dict[str, object]:
        table_name = _INTRADAY_TABLES[interval]
        if not self._table_exists(table_name):
            return {"symbols_total": 0, "latest_date_max": ""}
        with self._connect_readonly() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT symbol) AS symbols_total,
                    CAST(MAX(date) AS VARCHAR) AS latest_date_max
                FROM {table_name}
                """
            ).fetchone()
        return {
            "symbols_total": int(row[0]) if row and row[0] is not None else 0,
            "latest_date_max": str(row[1] or ""),
        }

    def _table_exists(self, table_name: str) -> bool:
        if not self._db_path.exists():
            return False
        with self._connect_readonly() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = 'main'
                  AND table_name = ?
                """,
                [table_name],
            ).fetchone()
        return bool(row and int(row[0]) > 0)

    def _connect_write(self) -> _DUCK_CONNECTION:
        if self._read_only:
            raise DataSourceError(f"market warehouse is read-only; refusing write: {self._db_path}")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return self._connect()

    def _connect_readonly(self) -> _DUCK_CONNECTION:
        # Read paths must open DuckDB in read_only mode: multiple readers can
        # share the file concurrently and a reader never blocks (or is blocked
        # by) the nightly write window. When the database file is absent,
        # fall back to legacy behaviour (opens/creates an empty DB) so callers
        # that tolerate an empty warehouse keep working unchanged.
        import duckdb

        def _open() -> _DUCK_CONNECTION:
            if self._db_path.exists():
                try:
                    return cast(
                        _DUCK_CONNECTION,
                        duckdb.connect(database=str(self._db_path), read_only=True),
                    )
                except duckdb.ConnectionException as exc:
                    if "different configuration" in str(exc).lower():
                        # DuckDB forbids opening a second connection to the
                        # same file with a different configuration while a
                        # write connection is already open in this process.
                        # Fall back to the legacy connection so same-process
                        # read/write coexistence keeps working; cross-process
                        # readers still get the read-only fast path.
                        return cast(
                            _DUCK_CONNECTION,
                            duckdb.connect(database=str(self._db_path)),
                        )
                    raise
            return cast(_DUCK_CONNECTION, duckdb.connect(database=str(self._db_path)))

        return _connect_with_lock_retry(_open)

    def _connect(self) -> _DUCK_CONNECTION:
        import duckdb

        if self._read_only:
            if not self._db_path.exists():
                raise DataSourceError(f"read-only DuckDB does not exist: {self._db_path}")
            return cast(
                _DUCK_CONNECTION,
                duckdb.connect(database=str(self._db_path), read_only=True),
            )
        return cast(_DUCK_CONNECTION, duckdb.connect(database=str(self._db_path)))


_DUCKDB_LOCK_RETRY_ATTEMPTS = 5
_DUCKDB_LOCK_RETRY_BASE_DELAY_SEC = 0.25
_DUCKDB_LOCK_RETRY_MAX_DELAY_SEC = 2.0


def _is_retryable_duckdb_lock_error(exc: Exception) -> bool:
    message = str(exc).strip().lower()
    return "could not set lock on file" in message or "conflicting lock is held" in message


def _connect_with_lock_retry(connect_fn: Callable[[], _DUCK_CONNECTION]) -> _DUCK_CONNECTION:
    """Open a DuckDB connection, retrying transient file-lock conflicts.

    The nightly market-warehouse write window holds the database file lock for
    a long time; readers that try to open the file during that window can hit
    ``could not set lock on file`` / ``conflicting lock is held``. Retry with
    exponential backoff instead of failing the request outright.
    """
    delay_sec = _DUCKDB_LOCK_RETRY_BASE_DELAY_SEC
    for attempt in range(1, _DUCKDB_LOCK_RETRY_ATTEMPTS + 1):
        try:
            return connect_fn()
        except Exception as exc:
            if not _is_retryable_duckdb_lock_error(exc) or attempt >= _DUCKDB_LOCK_RETRY_ATTEMPTS:
                raise
            time.sleep(delay_sec)
            delay_sec = min(_DUCKDB_LOCK_RETRY_MAX_DELAY_SEC, delay_sec * 2.0)
    raise RuntimeError("unreachable")


def list_package_symbols(source_root: str | Path) -> list[str]:
    bars_root = Path(source_root).expanduser() / "bars"
    if not bars_root.exists() or not bars_root.is_dir():
        return []
    symbols: set[str] = set()
    for pattern in ("*.csv", "*.csv.gz", "*.parquet"):
        for path in bars_root.glob(pattern):
            symbol = _normalize_symbol(path.stem)
            if symbol:
                symbols.add(symbol)
    return sorted(symbols)


def load_package_daily_bars(*, source_root: str | Path, symbol: str) -> pd.DataFrame:
    normalized_symbol = _normalize_symbol(symbol)
    target = _resolve_symbol_path(Path(source_root).expanduser(), normalized_symbol)
    if target is None:
        return pd.DataFrame()
    if target.suffix.lower() == ".parquet":
        raw = pd.read_parquet(target)
    else:
        raw = pd.read_csv(target, compression="infer")
    return _normalize_daily_frame(frame=raw, symbol=normalized_symbol)


def load_package_intraday_summary(
    *,
    source_root: str | Path,
    symbol: str,
    interval: str,
) -> pd.DataFrame:
    return _normalize_intraday_frame(
        frame=load_intraday_summary(
            root=source_root,
            symbol=symbol,
            interval=interval,
            lookback_days=1_000_000,
        ),
        symbol=symbol,
    )


def _background_field_metrics(frame: pd.DataFrame, field: str) -> dict[str, float | int]:
    if field not in frame.columns:
        return {
            "non_null_count": 0,
            "non_null_ratio": 0.0,
            "non_zero_count": 0,
            "non_zero_ratio": 0.0,
        }
    series = pd.to_numeric(pd.Series(frame[field]), errors="coerce")
    total = int(len(series))
    non_null_count = int(series.notna().sum()) if total > 0 else 0
    non_zero_count = int(series.fillna(0.0).ne(0.0).sum()) if total > 0 else 0
    return {
        "non_null_count": non_null_count,
        "non_null_ratio": round(non_null_count / total, 6) if total > 0 else 0.0,
        "non_zero_count": non_zero_count,
        "non_zero_ratio": round(non_zero_count / total, 6) if total > 0 else 0.0,
    }


def _background_source_distribution(frame: pd.DataFrame) -> dict[str, int]:
    if "background_data_source" not in frame.columns or frame.empty:
        return {}
    series = (
        pd.Series(frame["background_data_source"])
        .fillna("")
        .astype(str)
        .str.strip()
        .replace("", "missing")
    )
    counts = series.value_counts(dropna=False).sort_index()
    return {str(index): int(value) for index, value in counts.items()}


def write_package_daily_bars(
    *,
    package_root: str | Path,
    symbol: str,
    frame: pd.DataFrame,
) -> Path:
    normalized_symbol = _normalize_symbol(symbol)
    normalized = _normalize_daily_frame(frame=frame, symbol=normalized_symbol)
    target_dir = Path(package_root).expanduser() / "bars"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{normalized_symbol}.csv"
    payload = normalized.reset_index()
    payload.to_csv(target_path, index=False)
    return target_path


def write_package_intraday_summary(
    *,
    package_root: str | Path,
    symbol: str,
    interval: str,
    frame: pd.DataFrame,
) -> Path:
    normalized_symbol = _normalize_symbol(symbol)
    normalized = _normalize_intraday_frame(frame=frame, symbol=normalized_symbol)
    target_dir = Path(package_root).expanduser() / "intraday_summary" / interval
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{normalized_symbol}.csv.gz"
    payload = normalized.reset_index()
    payload.insert(0, "symbol", normalized_symbol)
    payload.to_csv(target_path, index=False, compression="gzip")
    return target_path


def _package_daily_file_summary(package_root: Path) -> dict[str, object]:
    bars_root = package_root / "bars"
    symbols: set[str] = set()
    if bars_root.exists() and bars_root.is_dir():
        for pattern in ("*.csv", "*.csv.gz", "*.parquet"):
            for path in bars_root.glob(pattern):
                normalized = _normalize_symbol(path.name.split(".")[0])
                if normalized:
                    symbols.add(normalized)
    ordered = sorted(symbols)
    return {"symbols_total": len(ordered), "symbols": ordered}


def _package_intraday_file_summary(package_root: Path, interval: str) -> dict[str, object]:
    summary_root = package_root / "intraday_summary" / interval
    symbols: set[str] = set()
    if summary_root.exists() and summary_root.is_dir():
        for pattern in ("*.csv", "*.csv.gz", "*.parquet"):
            for path in summary_root.glob(pattern):
                normalized = _normalize_symbol(path.name.split(".")[0])
                if normalized:
                    symbols.add(normalized)
    ordered = sorted(symbols)
    return {"symbols_total": len(ordered), "symbols": ordered}


def _normalize_daily_frame(*, frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized_frame: pd.DataFrame = _normalize_frame(frame=frame, symbol=symbol)
    return normalized_frame


def _normalize_intraday_frame(*, frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    _ = symbol
    if frame.empty:
        return frame
    normalized = frame.copy()
    if "date" in normalized.columns:
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
        normalized = normalized.dropna(subset=["date"]).set_index("date")
    else:
        normalized.index = pd.DatetimeIndex(pd.to_datetime(normalized.index, errors="coerce"))
        normalized = normalized[normalized.index.notna()]
    normalized.index.name = "date"
    normalized = normalized.sort_index()
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    for column in _INTRADAY_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = 0.0
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0.0)
    return normalized[_INTRADAY_COLUMNS].copy()


def _coerce_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (bytes, bytearray)):
        candidate: str | int | float = value.decode("utf-8", errors="ignore")
    elif isinstance(value, (str, int, float)):
        candidate = value
    else:
        return None
    try:
        parsed = pd.Timestamp(candidate)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
