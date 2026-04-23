"""Signal notification filter with cooldown de-dup and quiet windows."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time

from stock_analyzer.config import NotificationFilterConfig
from stock_analyzer.infra.cache import CacheStore
from stock_analyzer.types import PipelineSignal


class NotificationFilter:
    """Filter out low-value or duplicated notifications."""

    def __init__(self, config: NotificationFilterConfig, cache: CacheStore) -> None:
        self._config = config
        self._cache = cache

    def filter(
        self,
        signals: list[PipelineSignal],
        now: datetime | None = None,
        entry_day_symbols: set[str] | None = None,
    ) -> list[dict[str, object]]:
        if not self._config.enabled:
            return [self._signal_dict(item) for item in signals]

        if self._is_quiet_time(now):
            return []

        allowed_actions = {value.lower() for value in self._config.allowed_actions}
        accepted_signals: list[PipelineSignal] = []

        sorted_signals = sorted(signals, key=lambda item: item.score, reverse=True)
        for signal in sorted_signals:
            if signal.action.lower() not in allowed_actions:
                continue
            if signal.score < self._config.min_score:
                continue
            if self._is_t_day_silenced(signal=signal, entry_day_symbols=entry_day_symbols):
                continue

            key = self._dedup_key(signal)
            if self._cache.exists(key):
                continue

            self._cache.set(key, "1", ttl_sec=self._config.cooldown_sec)
            accepted_signals.append(signal)
            if len(accepted_signals) >= self._config.max_signals_per_run:
                break

        return [self._signal_dict(item) for item in accepted_signals]

    def _dedup_key(self, signal: PipelineSignal) -> str:
        if self._config.dedup_by_symbol_action:
            return f"notify:{signal.symbol}:{signal.action}"
        return f"notify:{signal.symbol}:{signal.action}:{signal.grade}:{round(signal.score, 2)}"

    def _is_quiet_time(self, now: datetime | None) -> bool:
        return is_quiet_time(self._config.quiet_windows, now=now)

    def _is_t_day_silenced(
        self,
        signal: PipelineSignal,
        entry_day_symbols: set[str] | None,
    ) -> bool:
        if not self._config.t_day_entry_silence_enabled:
            return False
        if not entry_day_symbols:
            return False
        if signal.symbol not in entry_day_symbols:
            return False
        return _keyword_hit(signal.reasons, self._config.t_day_silence_reason_keywords)

    @staticmethod
    def _signal_dict(signal: PipelineSignal) -> dict[str, object]:
        return asdict(signal)


def _parse_window(raw: str) -> tuple[time, time]:
    parts = raw.split("-")
    if len(parts) != 2:
        raise ValueError(f"invalid quiet window format: {raw}")
    return _parse_hhmm(parts[0]), _parse_hhmm(parts[1])


def is_quiet_time(quiet_windows: list[str], now: datetime | None = None) -> bool:
    if not quiet_windows:
        return False
    current = (now or datetime.now()).time()
    for raw in quiet_windows:
        start, end = _parse_window(raw)
        if start <= end:
            if start <= current <= end:
                return True
            continue
        if current >= start or current <= end:
            return True
    return False


def _keyword_hit(reasons: list[str], keywords: list[str]) -> bool:
    if not reasons or not keywords:
        return False
    merged = " ".join(reasons).lower()
    for keyword in keywords:
        token = keyword.strip().lower()
        if token and token in merged:
            return True
    return False


def _parse_hhmm(raw: str) -> time:
    tokens = raw.strip().split(":")
    if len(tokens) != 2:
        raise ValueError(f"invalid hh:mm: {raw}")
    return time(hour=int(tokens[0]), minute=int(tokens[1]))
