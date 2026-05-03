"""Signal notification filter with cooldown de-dup and quiet windows."""

from __future__ import annotations

from copy import deepcopy
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
        self._last_diagnostics: dict[str, object] | None = None

    def filter(
        self,
        signals: list[PipelineSignal],
        now: datetime | None = None,
        entry_day_symbols: set[str] | None = None,
        trace_id: str = "",
    ) -> list[dict[str, object]]:
        current = now or datetime.now()
        if not self._config.enabled:
            diagnostics = self._base_diagnostics(
                signals=signals,
                now=current,
                trace_id=trace_id,
                quiet_window_hit=False,
            )
            accepted = [self._signal_dict(item) for item in signals]
            diagnostics["enabled"] = False
            diagnostics["accepted_count"] = len(accepted)
            diagnostics["status"] = "disabled_passthrough"
            diagnostics["accepted_symbols"] = [item.symbol for item in signals]
            self._last_diagnostics = diagnostics
            return accepted

        quiet_window_hit = self._is_quiet_time(current)
        diagnostics = self._base_diagnostics(
            signals=signals,
            now=current,
            trace_id=trace_id,
            quiet_window_hit=quiet_window_hit,
        )
        if quiet_window_hit:
            diagnostics["rejected_by_quiet_window"] = len(signals)
            diagnostics["status"] = "quiet_window"
            for signal in sorted(signals, key=lambda item: item.score, reverse=True)[:5]:
                self._append_rejected_example(
                    diagnostics,
                    signal=signal,
                    reason="quiet_window",
                )
            self._last_diagnostics = diagnostics
            return []

        allowed_actions = {value.lower() for value in self._config.allowed_actions}
        accepted_signals: list[PipelineSignal] = []

        sorted_signals = sorted(signals, key=lambda item: item.score, reverse=True)
        for signal in sorted_signals:
            if signal.action.lower() not in allowed_actions:
                self._increment_diagnostics(diagnostics, "rejected_by_action")
                self._append_rejected_example(
                    diagnostics,
                    signal=signal,
                    reason="action",
                )
                continue
            if signal.score < self._config.min_score:
                self._increment_diagnostics(diagnostics, "rejected_by_score")
                self._append_rejected_example(
                    diagnostics,
                    signal=signal,
                    reason="score",
                )
                continue
            if self._is_t_day_silenced(signal=signal, entry_day_symbols=entry_day_symbols):
                self._increment_diagnostics(diagnostics, "rejected_by_t_day_silence")
                self._append_rejected_example(
                    diagnostics,
                    signal=signal,
                    reason="t_day_silence",
                )
                continue

            key = self._dedup_key(signal)
            if self._cache.exists(key):
                self._increment_diagnostics(diagnostics, "rejected_by_cooldown")
                self._append_rejected_example(
                    diagnostics,
                    signal=signal,
                    reason="cooldown",
                    detail=key,
                )
                continue

            if len(accepted_signals) >= self._config.max_signals_per_run:
                self._increment_diagnostics(diagnostics, "rejected_by_max_signals_per_run")
                self._append_rejected_example(
                    diagnostics,
                    signal=signal,
                    reason="max_signals_per_run",
                )
                continue

            self._cache.set(key, "1", ttl_sec=self._config.cooldown_sec)
            accepted_signals.append(signal)

        accepted = [self._signal_dict(item) for item in accepted_signals]
        diagnostics["accepted_count"] = len(accepted)
        diagnostics["accepted_symbols"] = [item.symbol for item in accepted_signals]
        diagnostics["status"] = "ok"
        self._last_diagnostics = diagnostics
        return accepted

    def latest_diagnostics(self) -> dict[str, object] | None:
        """Return the last filtering decision breakdown for status payloads."""
        return deepcopy(self._last_diagnostics)

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

    def _base_diagnostics(
        self,
        *,
        signals: list[PipelineSignal],
        now: datetime,
        trace_id: str,
        quiet_window_hit: bool,
    ) -> dict[str, object]:
        return {
            "trace_id": trace_id.strip(),
            "timestamp": now.isoformat(),
            "enabled": bool(self._config.enabled),
            "input_count": len(signals),
            "accepted_count": 0,
            "rejected_by_action": 0,
            "rejected_by_score": 0,
            "rejected_by_t_day_silence": 0,
            "rejected_by_cooldown": 0,
            "rejected_by_quiet_window": 0,
            "rejected_by_max_signals_per_run": 0,
            "quiet_window_hit": quiet_window_hit,
            "min_score": float(self._config.min_score),
            "allowed_actions": list(self._config.allowed_actions),
            "max_signals_per_run": int(self._config.max_signals_per_run),
            "cooldown_sec": int(self._config.cooldown_sec),
            "dedup_by_symbol_action": bool(self._config.dedup_by_symbol_action),
            "t_day_entry_silence_enabled": bool(self._config.t_day_entry_silence_enabled),
            "top_rejected_examples": [],
            "accepted_symbols": [],
            "status": "pending",
        }

    @staticmethod
    def _increment_diagnostics(diagnostics: dict[str, object], key: str) -> None:
        current = diagnostics.get(key, 0)
        diagnostics[key] = int(current) + 1 if isinstance(current, (int, float)) else 1

    @staticmethod
    def _append_rejected_example(
        diagnostics: dict[str, object],
        *,
        signal: PipelineSignal,
        reason: str,
        detail: str = "",
    ) -> None:
        raw_examples = diagnostics.get("top_rejected_examples")
        if not isinstance(raw_examples, list) or len(raw_examples) >= 5:
            return
        item = {
            "symbol": signal.symbol,
            "action": signal.action,
            "score": float(signal.score),
            "grade": signal.grade,
            "reason": reason,
        }
        if detail:
            item["detail"] = detail
        raw_examples.append(item)


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
