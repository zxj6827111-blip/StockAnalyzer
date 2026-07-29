"""Local market warehouse backed by DuckDB with offline-package materialization."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from stock_analyzer.data.intraday_summary import load_intraday_summary
from stock_analyzer.data.tdx_offline_provider import (
    _SELECTED_COLUMNS,
    _normalize_frame,
    _normalize_symbol,
    _resolve_symbol_path,
)

_DUCK_CONNECTION = Any

_DAILY_TABLE = "daily_bars"
_FINANCIAL_SNAPSHOT_TABLE = "financial_snapshots"
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
    "adjustment_anchor_factor",
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
    """Persist normalized market data into DuckDB and export runtime package files."""

    def __init__(self, *, db_path: str | Path, package_root: str | Path) -> None:
        self._db_path = Path(db_path).expanduser()
        self._package_root = Path(package_root).expanduser()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def package_root(self) -> Path:
        return self._package_root

    def ensure_schema(self) -> None:
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

    def apply_financial_snapshots_to_daily(self, *, symbol: str) -> pd.DataFrame:
        """Re-materialize daily bars financial columns from PIT snapshots."""
        from stock_analyzer.data.financial_pit import apply_financial_snapshots_asof
        daily = self.fetch_all_daily_bars(symbol=symbol)
        if daily.empty:
            return daily
        snaps = self.fetch_financial_snapshots(symbol=symbol)
        if snaps.empty:
            return daily
        enriched = apply_financial_snapshots_asof(daily, snaps, only_fill_pending=False)
        self.replace_daily_bars(symbol=symbol, frame=enriched)
        write_package_daily_bars(
            package_root=self.package_root,
            symbol=symbol,
            frame=enriched,
        )
        return enriched


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

        contracts = [
            self.price_series_contract(symbol=item)
            for item in self.list_symbols()
        ]
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
            if not stored_factors.empty and not stored_factors.map(
                lambda value: math.isclose(
                    float(value), old_factor, rel_tol=0.0, abs_tol=1e-9
                )
            ).all():
                raise ValueError("stored_anchor_factor_mismatch")

            ratio = old_factor / new_factor
            for column in ("open", "high", "low", "close"):
                if column in rebased.columns:
                    rebased[column] = pd.to_numeric(
                        rebased[column], errors="coerce"
                    ) * ratio

            for column in (
                "price_series_mode",
                "adjustment_source",
                "adjustment_anchor_date",
                "adjustment_anchor_factor",
            ):
                if column in rebased.columns:
                    rebased[column] = cast(Any, fresh_meta.get(column))

        merged = (
            fresh_daily.copy()
            if rebased.empty
            else pd.concat([rebased, fresh_daily], axis=0)
        )
        return merged[~merged.index.duplicated(keep="last")].sort_index()

    def clone_to_shadow_paths(
        self,
        *,
        shadow_db_path: str | Path,
        shadow_package_root: str | Path,
    ) -> MarketWarehouse:
        """Create a new empty shadow warehouse without deleting/overwriting the current one."""
        return MarketWarehouse(db_path=shadow_db_path, package_root=shadow_package_root)

    def has_daily_data(self) -> bool:
        if not self._table_exists(_DAILY_TABLE):
            return False
        with self._connect_readonly() as connection:
            count = connection.execute(
                f"SELECT COUNT(*) FROM {_DAILY_TABLE}"
            ).fetchone()
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
            connection.register("intraday_stage_df", payload)
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
            connection.unregister("intraday_stage_df")

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
        self.package_root.mkdir(parents=True, exist_ok=True)
        daily_summary = self._daily_summary()
        daily_file_summary = _package_daily_file_summary(self.package_root)
        intraday_summary = {
            interval: self._intraday_summary(interval)
            for interval in _INTRADAY_TABLES
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
                    "package_symbol_files_total": intraday_file_summary[interval][
                        "symbols_total"
                    ],
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
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return self._connect()

    def _connect_readonly(self) -> _DUCK_CONNECTION:
        return self._connect()

    def _connect(self) -> _DUCK_CONNECTION:
        import duckdb

        return cast(_DUCK_CONNECTION, duckdb.connect(database=str(self._db_path)))


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
