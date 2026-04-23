"""Week6 analysis engines: main-force tracking, allocation, calendar/global/regulatory factors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from stock_analyzer.config import (
    GlobalMarketConfig,
    HolidayRiskConfig,
    RegulatoryFactorConfig,
    Week6AllocationProfilesConfig,
    Week6MainForceConfig,
)


@dataclass(slots=True)
class MainForceTracker:
    """Score symbols by persistent trend and turnover/volume behavior."""

    config: Week6MainForceConfig

    def analyze_symbol(self, symbol: str, bars: pd.DataFrame) -> dict[str, object]:
        if bars.empty:
            return {
                "symbol": symbol,
                "score": 0.0,
                "status": "no_data",
                "accumulation_days_10": 0,
                "turnover_stability": 0.0,
                "trend_strength_20d": 0.0,
            }

        recent = bars.tail(max(20, self.config.lookback_days))
        close = recent["close"].astype(float)
        turnover = recent["turnover"].astype(float)
        volume = recent["volume"].astype(float) if "volume" in recent.columns else turnover.copy()
        returns = close.pct_change().fillna(0.0)

        latest_turnover = float(turnover.iloc[-1]) if len(turnover) > 0 else 0.0
        turnover_base = float(turnover.tail(6).iloc[:-1].mean()) if len(turnover) >= 6 else 0.0
        turnover_ratio = latest_turnover / turnover_base if turnover_base > 0 else 1.0

        turnover_mean = float(turnover.mean()) if len(turnover) > 0 else 0.0
        turnover_std = float(turnover.std(ddof=0)) if len(turnover) > 0 else 0.0
        stability = 1.0
        if turnover_mean > 0:
            stability = max(0.0, min(1.0, 1.0 - turnover_std / turnover_mean))

        trend_20d = 0.0
        if len(close) >= 20:
            base = float(close.iloc[-20])
            if base > 0:
                trend_20d = float(close.iloc[-1] / base - 1.0)

        positive_flow_days = 0
        for idx in range(max(1, len(recent) - 10), len(recent)):
            ret = float(returns.iloc[idx])
            turn = float(turnover.iloc[idx])
            prior = float(turnover.iloc[idx - 1]) if idx - 1 >= 0 else turn
            if ret > 0 and turn >= prior:
                positive_flow_days += 1

        accumulation_norm = min(1.0, positive_flow_days / 10.0)
        trend_norm = _clamp((trend_20d + 0.10) / 0.30, 0.0, 1.0)
        turnover_norm = _clamp((turnover_ratio - 0.8) / 1.2, 0.0, 1.0)
        holder_reduction_norm = _holder_reduction_norm(recent)
        block_trade_buy_norm = _block_trade_buy_norm(recent)
        sideways_days_norm = _sideways_days_norm(close.tail(20))
        volume_contraction_norm = _volume_contraction_norm(volume.tail(10))
        financing_trend_norm = _financing_trend_norm(recent)
        turnover_drop_norm = _clamp(1.0 - turnover_norm, 0.0, 1.0)

        completion_norm = (
            0.30 * holder_reduction_norm
            + 0.20 * block_trade_buy_norm
            + 0.15 * sideways_days_norm
            + 0.15 * volume_contraction_norm
            + 0.10 * financing_trend_norm
            + 0.10 * turnover_drop_norm
        )
        base_norm = 0.45 * trend_norm + 0.30 * stability + 0.25 * accumulation_norm
        score = 100.0 * (0.80 * base_norm + 0.20 * completion_norm)
        status = "strong" if score >= self.config.strong_score else "normal"
        return {
            "symbol": symbol,
            "score": round(score, 2),
            "completion_score": round(completion_norm * 100.0, 2),
            "status": status,
            "accumulation_days_10": positive_flow_days,
            "turnover_stability": round(stability, 4),
            "trend_strength_20d": round(trend_20d, 4),
            "turnover_ratio_5d": round(turnover_ratio, 4),
            "signals": {
                "trend_norm": round(trend_norm, 4),
                "turnover_norm": round(turnover_norm, 4),
                "accumulation_norm": round(accumulation_norm, 4),
            },
            "completion_factors": {
                "holder_reduction_norm": round(holder_reduction_norm, 4),
                "block_trade_buy_norm": round(block_trade_buy_norm, 4),
                "sideways_days_norm": round(sideways_days_norm, 4),
                "volume_contraction_norm": round(volume_contraction_norm, 4),
                "financing_trend_norm": round(financing_trend_norm, 4),
                "turnover_drop_norm": round(turnover_drop_norm, 4),
            },
        }


@dataclass(slots=True)
class StrategyAllocationEngine:
    """Output strategy weights by market regime."""

    profiles: Week6AllocationProfilesConfig

    def infer_regime(
        self,
        drawdown_pct: float,
        global_risk_score: float,
        empty_signal_triggered: bool,
    ) -> str:
        if drawdown_pct >= 10.0 or global_risk_score <= 40.0 or empty_signal_triggered:
            return "crash"
        if drawdown_pct <= 5.0 and global_risk_score >= 60.0:
            return "trend"
        return "range"

    def allocation(self, regime: str) -> dict[str, float]:
        if regime == "trend":
            return _normalize_profile(self.profiles.trend)
        if regime == "crash":
            return _normalize_profile(self.profiles.crash)
        return _normalize_profile(self.profiles.range)


@dataclass(slots=True)
class CalendarFactorEngine:
    """Seasonal and pre-holiday adjustments."""

    config: HolidayRiskConfig

    def evaluate(self, now: date) -> dict[str, object]:
        month = now.month
        weekday = now.weekday()  # Monday=0
        season_tag = "neutral"
        threshold_adjust = 0.0
        if month in {2, 3, 4}:
            season_tag = "spring_bias"
            threshold_adjust = 1.0
        elif month in {11, 12}:
            season_tag = "year_end_risk"
            threshold_adjust = -2.0

        days_to_weekend = max(0, 4 - weekday) if weekday <= 4 else 0
        pre_holiday = days_to_weekend <= max(0, self.config.pre_holiday_reduce_days)
        position_multiplier = self.config.max_position_multiplier if pre_holiday else 1.0
        return {
            "season_tag": season_tag,
            "threshold_adjust": threshold_adjust,
            "pre_holiday_reduce": pre_holiday,
            "position_multiplier": round(position_multiplier, 4),
            "days_to_weekend": days_to_weekend,
        }


@dataclass(slots=True)
class GlobalMarketFactorEngine:
    """Cross-market risk/position adjustment from snapshot."""

    config: GlobalMarketConfig

    def evaluate(self, snapshot: dict[str, float] | None) -> dict[str, object]:
        if not self.config.enabled:
            return {
                "enabled": False,
                "risk_score": 50.0,
                "threshold_adjust": 0.0,
                "position_adjust_pct": 0.0,
                "snapshot": {},
            }

        data = snapshot or {}
        us_change = float(data.get("us_index_change_pct", 0.0))
        a50_change = float(data.get("a50_change_pct", 0.0))
        usd_cnh_change = float(data.get("usd_cnh_change_pct", 0.0))
        commodity_change = float(data.get("commodity_change_pct", 0.0))
        corr = float(data.get("a_share_correlation", 0.60))

        corr_decay = 0.5 if abs(corr) < self.config.correlation_decay_threshold else 1.0
        raw_score = (
            50.0
            + corr_decay * (us_change * 8.0 + a50_change * 10.0 - usd_cnh_change * 6.0)
            + commodity_change * 4.0
        )
        risk_score = _clamp(raw_score, 0.0, 100.0)
        threshold_adjust = _clamp(
            (risk_score - 50.0) / 25.0 * self.config.threshold_adjust_max,
            -self.config.threshold_adjust_max,
            self.config.threshold_adjust_max,
        )
        position_adjust_pct = _clamp(
            (risk_score - 50.0) / 50.0 * self.config.position_adjust_max_pct,
            -self.config.position_adjust_max_pct,
            self.config.position_adjust_max_pct,
        )
        return {
            "enabled": True,
            "risk_score": round(risk_score, 2),
            "threshold_adjust": round(threshold_adjust, 4),
            "position_adjust_pct": round(position_adjust_pct, 4),
            "correlation_decay": corr_decay < 1.0,
            "snapshot": {
                "us_index_change_pct": us_change,
                "a50_change_pct": a50_change,
                "usd_cnh_change_pct": usd_cnh_change,
                "commodity_change_pct": commodity_change,
                "a_share_correlation": corr,
            },
        }


@dataclass(slots=True)
class RegulatoryFactorEngine:
    """Convert watchlist tags to symbol-level regulatory actions."""

    config: RegulatoryFactorConfig

    def evaluate(
        self,
        symbols: list[str],
        watchlist: dict[str, dict[str, Any]],
    ) -> dict[str, object]:
        normalized = [item.strip() for item in symbols if item.strip()]
        symbol_actions: list[dict[str, object]] = []
        exclude_set = {item.strip().lower() for item in self.config.exclude_tags}
        for symbol in normalized:
            info = watchlist.get(symbol, {})
            tag = str(info.get("tag", "")).strip().lower()
            note = str(info.get("note", "")).strip()
            if not self.config.enabled or not tag:
                symbol_actions.append(
                    {"symbol": symbol, "tag": "", "action": "normal", "penalty_score": 0.0}
                )
                continue
            action = "degrade"
            if tag in exclude_set and "exclude" in self.config.action:
                action = "exclude"
            symbol_actions.append(
                {
                    "symbol": symbol,
                    "tag": tag,
                    "note": note,
                    "action": action,
                    "penalty_score": self.config.penalty_score if action == "degrade" else 0.0,
                }
            )

        excluded = [item["symbol"] for item in symbol_actions if item["action"] == "exclude"]
        degraded = [item["symbol"] for item in symbol_actions if item["action"] == "degrade"]
        return {
            "enabled": self.config.enabled,
            "watched_symbols": len(watchlist),
            "actions": symbol_actions,
            "excluded_symbols": excluded,
            "degraded_symbols": degraded,
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _normalize_profile(weights: dict[str, float]) -> dict[str, float]:
    total = sum(float(value) for value in weights.values())
    if total <= 0:
        return {"trend": 0.0, "oversold": 0.0, "event": 0.0}
    return {key: round(float(value) / total, 4) for key, value in weights.items()}


def _holder_reduction_norm(bars: pd.DataFrame) -> float:
    if "holder_count" not in bars.columns or len(bars) < 2:
        return 0.5
    holders = pd.to_numeric(bars["holder_count"], errors="coerce").dropna()
    if len(holders) < 2:
        return 0.5
    first = float(holders.iloc[0])
    last = float(holders.iloc[-1])
    if first <= 0:
        return 0.5
    reduction = (first - last) / first
    return _clamp((reduction + 0.20) / 0.40, 0.0, 1.0)


def _block_trade_buy_norm(bars: pd.DataFrame) -> float:
    if "block_trade_net" not in bars.columns:
        return 0.5
    block_net = pd.to_numeric(bars["block_trade_net"], errors="coerce").fillna(0.0)
    if len(block_net) == 0:
        return 0.5
    recent = float(block_net.tail(10).sum())
    scale = max(abs(recent), float(block_net.abs().mean()) * 10.0, 1.0)
    return _clamp((recent / scale + 1.0) / 2.0, 0.0, 1.0)


def _sideways_days_norm(close: pd.Series) -> float:
    if len(close) == 0:
        return 0.5
    max_days = min(20, len(close))
    if max_days <= 1:
        return 0.5
    reference = pd.to_numeric(close.tail(max_days), errors="coerce").dropna()
    if len(reference) <= 1:
        return 0.5
    anchor = float(reference.iloc[-1])
    if anchor <= 0:
        return 0.5
    diffs = (reference / anchor - 1.0).abs()
    sideways_days = int((diffs <= 0.03).sum())
    return _clamp(sideways_days / max_days, 0.0, 1.0)


def _volume_contraction_norm(volume: pd.Series) -> float:
    parsed = pd.to_numeric(volume, errors="coerce").dropna()
    if len(parsed) < 3:
        return 0.5
    latest = float(parsed.iloc[-1])
    baseline = float(parsed.iloc[:-1].mean())
    if baseline <= 0:
        return 0.5
    ratio = latest / baseline
    return _clamp((1.3 - ratio) / 0.6, 0.0, 1.0)


def _financing_trend_norm(bars: pd.DataFrame) -> float:
    if "financing_balance" not in bars.columns:
        return 0.5
    financing = pd.to_numeric(bars["financing_balance"], errors="coerce").dropna()
    if len(financing) < 5:
        return 0.5
    short = float(financing.tail(5).mean())
    long = float(financing.mean())
    if long <= 0:
        return 0.5
    ratio = short / long
    return _clamp((1.2 - ratio) / 0.6, 0.0, 1.0)
