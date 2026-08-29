"""Resilient wrapper to support degrade mode and fallback provider."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from stock_analyzer.config import DataSourceConfig
from stock_analyzer.data.provider import (
    DataSourceError,
    MarketDataProvider,
    RequiredIntradayDataError,
    fetch_intraday_summaries_compat,
)


@dataclass(slots=True)
class ResilientProvider:
    """Handle primary/backup switching and degraded mode tracking."""

    primary: MarketDataProvider
    config: DataSourceConfig
    backup: MarketDataProvider | None = None
    consecutive_failures: int = 0
    degraded_mode: bool = False
    last_error: str = ""

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        try:
            frame = _fetch_daily_bars_compat(
                provider=self.primary,
                symbol=symbol,
                lookback_days=lookback_days,
                end_date=end_date,
            )
        except Exception as exc:
            self.consecutive_failures += 1
            self.last_error = str(exc)
            if self.consecutive_failures >= self.config.switch_after_failures:
                self.degraded_mode = True

            if self.config.enable_cache_fallback and self.backup is not None:
                try:
                    return _fetch_daily_bars_compat(
                        provider=self.backup,
                        symbol=symbol,
                        lookback_days=lookback_days,
                        end_date=end_date,
                    )
                except Exception as backup_exc:
                    raise DataSourceError(
                        f"primary failed ({exc}) and backup failed ({backup_exc})"
                    ) from backup_exc

            raise DataSourceError(f"primary failed for {symbol}: {exc}") from exc

        self.consecutive_failures = 0
        self.degraded_mode = False
        self.last_error = ""
        return frame

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        try:
            frame = self.primary.fetch_intraday_summary(
                symbol=symbol,
                interval=interval,
                lookback_days=lookback_days,
            )
        except RequiredIntradayDataError:
            raise
        except Exception as exc:
            self.consecutive_failures += 1
            self.last_error = str(exc)
            if self.consecutive_failures >= self.config.switch_after_failures:
                self.degraded_mode = True

            if self.config.enable_cache_fallback and self.backup is not None:
                try:
                    return self.backup.fetch_intraday_summary(
                        symbol=symbol,
                        interval=interval,
                        lookback_days=lookback_days,
                    )
                except RequiredIntradayDataError:
                    raise
                except Exception as backup_exc:
                    raise DataSourceError(
                        f"primary failed ({exc}) and backup failed ({backup_exc})"
                    ) from backup_exc

            raise DataSourceError(f"primary failed for {symbol}: {exc}") from exc

        self.consecutive_failures = 0
        self.degraded_mode = False
        self.last_error = ""
        return frame

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        try:
            frames = fetch_intraday_summaries_compat(
                self.primary,
                symbols=symbols,
                interval=interval,
                lookback_days=lookback_days,
            )
        except RequiredIntradayDataError:
            raise
        except Exception as exc:
            self.consecutive_failures += 1
            self.last_error = str(exc)
            if self.consecutive_failures >= self.config.switch_after_failures:
                self.degraded_mode = True
            if self.config.enable_cache_fallback and self.backup is not None:
                try:
                    return fetch_intraday_summaries_compat(
                        self.backup,
                        symbols=symbols,
                        interval=interval,
                        lookback_days=lookback_days,
                    )
                except RequiredIntradayDataError:
                    raise
                except Exception as backup_exc:
                    raise DataSourceError(
                        f"primary batch failed ({exc}) and backup failed ({backup_exc})"
                    ) from backup_exc
            raise DataSourceError(f"primary batch failed: {exc}") from exc
        self.consecutive_failures = 0
        self.degraded_mode = False
        self.last_error = ""
        return frames

    def list_symbols(self) -> list[str]:
        """透传 primary 的全索引符号清单（Week5 历史回测股票池解析依赖）。

        只走 primary：``list_symbols`` 是本地索引/warehouse 的批量元数据接口，
        在线 backup（efinance/akshare）不提供该能力，fallback 无意义；primary
        缺失或执行失败都应显式冒泡（fail-closed），而不是静默降级成空池。
        """
        method = getattr(self.primary, "list_symbols", None)
        if not callable(method):
            raise DataSourceError("primary provider does not expose list_symbols()")
        symbols = method()
        return list(symbols) if symbols is not None else []

    def fetch_universe_quality_metrics(self, *args: object, **kwargs: object) -> pd.DataFrame:
        """透传批量质量指标查询（``end_date`` 等关键字参数原样传递）。

        Week5 历史回测的 as-of 质量选池依赖该接口；透传层不做任何截断或
        改写，as-of 语义由 primary 实现与 AsOfMarketDataProvider 负责。签名
        用 ``*args/**kwargs`` 以跟随下游演进（避免每加一个筛选参数就要改
        这一层）。与 ``list_symbols`` 同理只走 primary、fail-closed。
        """
        method = getattr(self.primary, "fetch_universe_quality_metrics", None)
        if not callable(method):
            raise DataSourceError(
                "primary provider does not expose fetch_universe_quality_metrics()"
            )
        result = method(*args, **kwargs)
        if not isinstance(result, pd.DataFrame):
            raise DataSourceError("fetch_universe_quality_metrics must return a DataFrame")
        return result

    def status(self) -> dict[str, object]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "degraded_mode": self.degraded_mode,
            "last_error": self.last_error,
        }

    def latest_daily_dates(
        self,
        *,
        symbols: list[str] | None = None,
    ) -> dict[str, date] | None:
        """Transparent pass-through to the primary so the incremental snapshot
        layer can drive date-based dirtiness through the resilient wrapper.

        Returns ``None`` when the primary has NO date interface (see
        CachedProvider.latest_daily_dates for the None-vs-empty contract).
        """
        primary = getattr(self.primary, "latest_daily_dates", None)
        if not callable(primary):
            return None
        result = primary(symbols=symbols)
        return result if isinstance(result, dict) else None


def _fetch_daily_bars_compat(
    *,
    provider: MarketDataProvider,
    symbol: str,
    lookback_days: int,
    end_date: date | None,
) -> pd.DataFrame:
    if end_date is None:
        return provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
    try:
        return provider.fetch_daily_bars(
            symbol=symbol,
            lookback_days=lookback_days,
            end_date=end_date,
        )
    except TypeError as exc:
        if "end_date" not in str(exc):
            raise
        return provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
