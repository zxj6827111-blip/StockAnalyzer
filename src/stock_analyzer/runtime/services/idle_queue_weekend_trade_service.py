"""Idle queue weekend trade-analysis helpers."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueWeekendTradeService:
    """Run trade-history-based weekend analytics tasks."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def idle_task_we_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-06",
            subdir="cost_sensitivity",
            filename="cost_sensitivity_report.json",
        )
        trades = service._portfolio.trades(limit=1000)
        if not trades:
            empty_trade_payload: dict[str, object] = {
                "task_id": "WE-P1-06",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_recent_trades",
                "tiers": [],
            }
            service._idle_write_json(output_path, empty_trade_payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_recent_trades",
                "output_files": [str(output_path)],
            }

        bars_cache: dict[str, pd.DataFrame] = {}

        parsed_trades: list[dict[str, object]] = []
        for item in trades:
            timestamp = _parse_iso_datetime(item.get("timestamp"))
            symbol = str(item.get("symbol", "")).strip()
            side = str(item.get("side", "")).strip().lower()
            if timestamp is None or not symbol or side not in {"buy", "sell"}:
                continue
            parsed_trades.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "side": side,
                    "target_position": max(
                        _as_float(item.get("target_position"), default=0.0),
                        0.0,
                    ),
                }
            )

        parsed_trades.sort(key=self._trade_sort_key)

        open_lots: dict[str, list[dict[str, object]]] = {}
        realized_returns: list[tuple[float, float]] = []
        for item in parsed_trades:
            symbol = str(item.get("symbol", ""))
            side = str(item.get("side", ""))
            item_timestamp = item.get("timestamp")
            if not isinstance(item_timestamp, datetime):
                continue
            timestamp = item_timestamp
            weight = max(_as_float(item.get("target_position"), default=0.0), 0.01)
            if side == "buy":
                open_lots.setdefault(symbol, []).append(
                    {
                        "timestamp": timestamp,
                        "weight": weight,
                    }
                )
                continue
            queue = open_lots.get(symbol, [])
            if not queue:
                continue
            entry = queue.pop(0)
            entry_time = entry.get("timestamp")
            if not isinstance(entry_time, datetime):
                continue
            entry_price = self._resolve_trade_price(
                bars_cache=bars_cache,
                symbol=symbol,
                when=entry_time,
                mode="entry",
            )
            exit_price = self._resolve_trade_price(
                bars_cache=bars_cache,
                symbol=symbol,
                when=timestamp,
                mode="exit",
            )
            if entry_price is None or exit_price is None or entry_price <= 0.0:
                continue
            realized_returns.append((exit_price / entry_price - 1.0, weight))

        for symbol, queue in open_lots.items():
            if not queue:
                continue
            bars = bars_cache.get(symbol)
            if bars is None or bars.empty or "close" not in bars.columns:
                continue
            latest_close = _numeric_series(bars, "close")
            if latest_close.empty:
                continue
            mark_price = float(latest_close.iloc[-1])
            if mark_price <= 0.0:
                continue
            for lot in queue:
                entry_time = lot.get("timestamp")
                if not isinstance(entry_time, datetime):
                    continue
                weight = max(_as_float(lot.get("weight"), default=0.0), 0.01)
                entry_price = self._resolve_trade_price(
                    bars_cache=bars_cache,
                    symbol=symbol,
                    when=entry_time,
                    mode="entry",
                )
                if entry_price is None or entry_price <= 0.0:
                    continue
                realized_returns.append((mark_price / entry_price - 1.0, weight))

        if not realized_returns:
            payload: dict[str, object] = {
                "task_id": "WE-P1-06",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_priced_trades",
                "tiers": [],
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_priced_trades",
                "output_files": [str(output_path)],
            }

        total_weight = max(sum(weight for _, weight in realized_returns), 1e-9)
        gross_edge = sum(ret * weight for ret, weight in realized_returns) / total_weight
        base_cost = (
            max(0.0, service._config.backtest_matcher.commission_rate) * 2.0
            + max(0.0, service._config.backtest_matcher.transfer_fee_rate) * 2.0
            + max(0.0, service._config.backtest_matcher.stamp_tax_rate)
        )
        tiers = [
            ("standard", 1.0),
            ("conservative", 1.5),
            ("very_conservative", 2.0),
            ("extreme", 3.0),
        ]
        tier_items: list[dict[str, object]] = []
        for name, multiplier in tiers:
            cost_drag = base_cost * multiplier
            pnl = gross_edge - cost_drag
            effective_wins = [1.0 if ret - cost_drag > 0.0 else 0.0 for ret, _ in realized_returns]
            tier_items.append(
                {
                    "tier": name,
                    "cost_multiplier": multiplier,
                    "cost_drag": round(cost_drag, 6),
                    "estimated_pnl": round(pnl, 6),
                    "effective_win_rate": round(
                        sum(effective_wins) / max(len(effective_wins), 1),
                        6,
                    ),
                }
            )

        report_payload: dict[str, object] = {
            "task_id": "WE-P1-06",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "ok",
            "coverage": f"{len(tier_items)}/{len(tiers)} tiers",
            "trade_samples": len(realized_returns),
            "gross_edge": round(gross_edge, 6),
            "base_cost": round(base_cost, 6),
            "tiers": tier_items,
        }
        service._idle_write_json(output_path, report_payload)
        return {
            "status": "ok",
            "output_files": [str(output_path)],
            "tiers": len(tier_items),
        }

    def _resolve_trade_price(
        self,
        *,
        bars_cache: dict[str, pd.DataFrame],
        symbol: str,
        when: datetime,
        mode: str,
    ) -> float | None:
        service = self._service
        bars = bars_cache.get(symbol)
        if bars is None:
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=420)
            except Exception:
                bars = pd.DataFrame()
            bars_cache[symbol] = bars
        if bars.empty or "close" not in bars.columns:
            return None
        close = _numeric_series(bars, "close")
        if close.empty:
            return None
        target_day = when.date()
        close_dates = pd.DatetimeIndex(close.index).date
        if mode == "entry":
            candidate = close[close_dates >= target_day]
            if not candidate.empty:
                return float(candidate.iloc[0])
            fallback = close[close_dates <= target_day]
            return float(fallback.iloc[-1]) if not fallback.empty else float(close.iloc[-1])
        candidate = close[close_dates <= target_day]
        if not candidate.empty:
            return float(candidate.iloc[-1])
        later = close[close_dates >= target_day]
        return float(later.iloc[0]) if not later.empty else float(close.iloc[-1])

    @staticmethod
    def _trade_sort_key(item: dict[str, object]) -> datetime:
        timestamp = item.get("timestamp")
        return timestamp if isinstance(timestamp, datetime) else datetime.min


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    return cast(pd.Series, _runtime_service_module()._numeric_series(frame, column))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))
