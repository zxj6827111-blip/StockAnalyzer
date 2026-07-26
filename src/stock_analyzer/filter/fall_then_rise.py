"""先跌后涨5日外 (fall-then-rise) TDX signal filter for the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from stock_analyzer.config import FallThenRiseConfig
from stock_analyzer.feature.tdx_indicators import (
    compute_fall_then_rise,
    resample_session_60m,
)

_FLAG_COLUMNS = (
    "ftr_ma5x28",
    "ftr_ma7x35",
    "ftr_m60_dtg",
    "ftr_mtm_dtg",
    "ftr_hist_turn",
    "ftr_above_ma",
)


@dataclass(slots=True)
class FallThenRiseDecision:
    applied: bool
    signal: bool = False
    signal_daily: bool = False
    min60_available: bool = False
    allowed: bool = True
    bonus_score: float = 0.0
    bonus_condition: str = ""
    flags: dict[str, bool] = field(default_factory=dict)
    reason: str = ""

    def trace(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "signal": self.signal,
            "signal_daily": self.signal_daily,
            "min60_available": self.min60_available,
            "allowed": self.allowed,
            "bonus_score": round(self.bonus_score, 2),
            "bonus_condition": self.bonus_condition,
            "flags": dict(self.flags),
            "reason": self.reason,
        }


_VALID_MODES = {"annotate", "bonus", "gate"}
_VALID_BONUS_CONDITIONS = {"signal", "signal_daily", "ma_gates"}


class FallThenRiseFilter:
    """Evaluate the 先跌后涨5日外 signal on pipeline daily bars.

    Modes:
        annotate: record the signal in the decision trace only (no effect).
        bonus:    add ``bonus_points`` to the score when the condition set
                  selected by ``bonus_condition`` fires.
        gate:     block buy actions for symbols without the full signal.
    """

    def __init__(
        self,
        config: FallThenRiseConfig,
        *,
        vipdoc_root: str = "",
    ) -> None:
        mode = config.mode.strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError(f"invalid fall_then_rise.mode: {config.mode!r}")
        bonus_condition = config.bonus_condition.strip().lower()
        if bonus_condition not in _VALID_BONUS_CONDITIONS:
            raise ValueError(
                f"invalid fall_then_rise.bonus_condition: {config.bonus_condition!r}"
            )
        self._config = config
        self._mode = mode
        self._bonus_condition = bonus_condition
        root = str(config.vipdoc_root).strip() or str(vipdoc_root).strip()
        self._vipdoc_root = Path(root) if root else None

    def evaluate(
        self,
        *,
        symbol: str,
        strategy: str,
        bars: pd.DataFrame,
    ) -> FallThenRiseDecision:
        config = self._config
        normalized_strategy = strategy.strip().lower()
        apply_to = {item.strip().lower() for item in config.apply_to if item.strip()}
        applied = bool(config.enabled) and (not apply_to or normalized_strategy in apply_to)
        if not applied or bars.empty or "close" not in bars.columns:
            return FallThenRiseDecision(applied=False, reason="not_applied")

        min60_close = self._load_min60_close(symbol)
        flags_frame = compute_fall_then_rise(
            bars,
            min60_close,
            min60_missing_policy=config.min60_missing_policy,
        )
        latest = flags_frame.iloc[-1]
        signal = bool(latest["ftr_signal"])
        decision = FallThenRiseDecision(
            applied=True,
            signal=signal,
            signal_daily=bool(latest["ftr_signal_daily"]),
            min60_available=min60_close is not None,
            flags={column: bool(latest[column]) for column in _FLAG_COLUMNS},
        )
        if self._mode == "gate":
            decision.allowed = signal
            decision.reason = "fall_then_rise_pass" if signal else "fall_then_rise_block"
        elif self._mode == "bonus":
            decision.bonus_condition = self._bonus_condition
            if self._bonus_condition == "ma_gates":
                fired = bool(latest["ftr_ma5x28"]) and bool(latest["ftr_ma7x35"])
            elif self._bonus_condition == "signal_daily":
                fired = bool(latest["ftr_signal_daily"])
            else:
                fired = signal
            decision.bonus_score = float(config.bonus_points) if fired else 0.0
            decision.reason = (
                f"fall_then_rise_bonus:{self._bonus_condition}"
                if fired
                else "fall_then_rise_no_bonus"
            )
        else:
            decision.reason = "fall_then_rise_annotate"
        return decision

    def _load_min60_close(self, symbol: str) -> pd.Series | None:
        if self._vipdoc_root is None or not self._vipdoc_root.exists():
            return None
        try:
            from stock_analyzer.data.intraday_summary import read_tdx_minute_bars

            bars = read_tdx_minute_bars(
                vipdoc_root=self._vipdoc_root, symbol=symbol, interval="5m"
            )
        except Exception:
            return None
        if bars.empty:
            return None
        resampled = resample_session_60m(bars)
        if resampled.empty:
            return None
        return resampled["close"]
