"""Resilient wrapper to support degrade mode and fallback provider."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from stock_analyzer.config import DataSourceConfig
from stock_analyzer.data.provider import DataSourceError, MarketDataProvider


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
            frame = self.primary.fetch_daily_bars(
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
                    return self.backup.fetch_daily_bars(
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
                except Exception as backup_exc:
                    raise DataSourceError(
                        f"primary failed ({exc}) and backup failed ({backup_exc})"
                    ) from backup_exc

            raise DataSourceError(f"primary failed for {symbol}: {exc}") from exc

        self.consecutive_failures = 0
        self.degraded_mode = False
        self.last_error = ""
        return frame

    def status(self) -> dict[str, object]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "degraded_mode": self.degraded_mode,
            "last_error": self.last_error,
        }
