"""T-1 feature engineering to avoid future-data leakage."""

from __future__ import annotations

import numpy as np
import pandas as pd


class FeatureEngineer:
    """Build online-usable features from historical bars."""

    required_columns = {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "float_market_cap",
    }

    def transform(
        self,
        bars: pd.DataFrame,
        intraday_1m: pd.DataFrame | None = None,
        intraday_5m: pd.DataFrame | None = None,
        market_index: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        missing = self.required_columns - set(bars.columns)
        if missing:
            raise ValueError(f"missing required columns: {sorted(missing)}")

        ordered = bars.sort_index().copy()
        open_px = ordered["open"].astype(float)
        high = ordered["high"].astype(float)
        low = ordered["low"].astype(float)
        close = ordered["close"].astype(float)
        volume = ordered["volume"].astype(float)
        turnover = ordered["turnover"].astype(float)
        float_market_cap = ordered["float_market_cap"].astype(float)
        is_st = _optional_binary(ordered, "is_st")
        is_delisting_risk = _optional_binary(ordered, "is_delisting_risk")
        roe = _optional_numeric(ordered, "roe")
        debt_ratio = _optional_numeric(ordered, "debt_ratio")
        holder_count = _optional_numeric(ordered, "holder_count")
        block_trade_net = _optional_numeric(ordered, "block_trade_net")
        margin_financing = _optional_numeric(ordered, "margin_financing_balance")
        northbound_net = _optional_numeric(ordered, "northbound_net")
        dragon_tiger_flag = _optional_binary(ordered, "dragon_tiger_flag")
        board_code = _encode_board(
            ordered.get("board", pd.Series(index=ordered.index, dtype=object))
        )

        prev_close = close.shift(1)
        price_range = (high - low).clip(lower=0.0)
        true_range = pd.concat(
            [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        returns = close.pct_change()
        log_returns = pd.Series(
            np.log(close.replace(0.0, np.nan).to_numpy()),
            index=close.index,
        ).diff()
        turnover_rate = _safe_div(turnover, float_market_cap)
        vwap = _safe_div(turnover, volume)
        vwap = vwap.fillna(close)

        raw = pd.DataFrame(index=ordered.index)

        # Price return and shape factors.
        raw["close_t1"] = close
        raw["ret_1d"] = returns
        raw["ret_2d"] = close.pct_change(2)
        raw["ret_3d"] = close.pct_change(3)
        raw["ret_5d"] = close.pct_change(5)
        raw["ret_10d"] = close.pct_change(10)
        raw["ret_20d"] = close.pct_change(20)
        raw["ret_60d"] = close.pct_change(60)
        raw["log_ret_1d"] = log_returns
        raw["log_ret_5d"] = log_returns.rolling(5, min_periods=1).sum()
        raw["overnight_gap"] = _safe_div(open_px, prev_close) - 1.0
        raw["intraday_ret"] = _safe_div(close, open_px) - 1.0
        raw["hl_range_pct"] = _safe_div(high - low, prev_close)
        raw["body_pct"] = _safe_div(close - open_px, price_range.replace(0.0, np.nan))
        upper_shadow = pd.Series(np.maximum(open_px, close), index=close.index)
        lower_shadow = pd.Series(np.minimum(open_px, close), index=close.index)
        raw["upper_shadow_pct"] = _safe_div(high - upper_shadow, price_range)
        raw["lower_shadow_pct"] = _safe_div(lower_shadow - low, price_range)

        # Moving averages and trend.
        for window in (3, 5, 8, 10, 13, 20, 30, 60):
            raw[f"ma{window}"] = close.rolling(window, min_periods=1).mean()
            raw[f"ema{window}"] = close.ewm(span=window, adjust=False).mean()
            raw[f"volatility_{window}"] = returns.rolling(window, min_periods=1).std(ddof=0)
            raw[f"volume_ratio_{window}"] = _safe_div(
                volume,
                volume.rolling(window, min_periods=1).mean(),
            )
            raw[f"turnover_ratio_{window}"] = _safe_div(
                turnover_rate,
                turnover_rate.rolling(window, min_periods=1).mean(),
            )

        raw["ma_gap_5_20"] = _safe_div(raw["ma5"], raw["ma20"]) - 1.0
        raw["ma_gap_10_30"] = _safe_div(raw["ma10"], raw["ma30"]) - 1.0
        raw["ma_gap_20_60"] = _safe_div(raw["ma20"], raw["ma60"]) - 1.0
        raw["ema_gap_12_26"] = (
            close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        )
        raw["trend_slope_20"] = close.pct_change(20) / 20.0
        raw["trend_slope_60"] = close.pct_change(60) / 60.0

        # Volatility and range.
        raw["atr5"] = true_range.rolling(5, min_periods=1).mean()
        raw["atr14"] = true_range.rolling(14, min_periods=1).mean()
        raw["atr20"] = true_range.rolling(20, min_periods=1).mean()
        raw["atr_ratio"] = _safe_div(raw["atr14"], prev_close)
        raw["downside_vol20"] = returns.clip(upper=0.0).rolling(20, min_periods=1).std(ddof=0)
        raw["upside_vol20"] = returns.clip(lower=0.0).rolling(20, min_periods=1).std(ddof=0)
        rolling_max20 = high.rolling(20, min_periods=1).max()
        rolling_min20 = low.rolling(20, min_periods=1).min()
        raw["distance_high20"] = _safe_div(close, rolling_max20) - 1.0
        raw["distance_low20"] = _safe_div(close, rolling_min20) - 1.0
        raw["drawdown_20"] = _safe_div(close, close.rolling(20, min_periods=1).max()) - 1.0

        # Oscillators and momentum indicators.
        macd_line = (
            close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        )
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        raw["macd_line"] = macd_line
        raw["macd_signal"] = macd_signal
        raw["macd_hist"] = macd_line - macd_signal
        raw["rsi6"] = _rsi(close, period=6)
        raw["rsi14"] = _rsi(close, period=14)
        raw["rsi24"] = _rsi(close, period=24)
        stoch_k, stoch_d, stoch_j = _stochastic_kdj(high=high, low=low, close=close, period=9)
        raw["stoch_k"] = stoch_k
        raw["stoch_d"] = stoch_d
        raw["stoch_j"] = stoch_j
        raw["cci14"] = _cci(high=high, low=low, close=close, period=14)
        raw["williams_r14"] = _williams_r(high=high, low=low, close=close, period=14)

        # Volume and turnover factors.
        raw["turnover_rate"] = turnover_rate
        raw["turnover_zscore20"] = _rolling_zscore(turnover_rate, window=20)
        raw["volume_zscore20"] = _rolling_zscore(volume, window=20)
        raw["price_volume_corr20"] = close.rolling(20, min_periods=5).corr(volume)
        raw["mfi14"] = _mfi(high=high, low=low, close=close, volume=volume, period=14)
        raw["obv_norm"] = _safe_div(
            _obv(close=close, volume=volume),
            volume.rolling(20, min_periods=1).mean(),
        )
        raw["obv_slope5"] = _rolling_diff(raw["obv_norm"], window=5)
        raw["adl_norm"] = _safe_div(
            _adl(high=high, low=low, close=close, volume=volume),
            volume.cumsum(),
        )
        raw["adl_slope5"] = _rolling_diff(raw["adl_norm"], window=5)
        raw["pvt_norm"] = _safe_div(_pvt(close=close, volume=volume), volume.cumsum())
        raw["vwap_gap5"] = _safe_div(close, vwap.rolling(5, min_periods=1).mean()) - 1.0

        # Channel and ranking factors.
        boll_mid = close.rolling(20, min_periods=1).mean()
        boll_std = close.rolling(20, min_periods=1).std(ddof=0)
        boll_up = boll_mid + 2.0 * boll_std
        boll_down = boll_mid - 2.0 * boll_std
        raw["boll_mid20"] = boll_mid
        raw["boll_width20"] = _safe_div(4.0 * boll_std, boll_mid.abs().replace(0.0, np.nan))
        raw["boll_pos20"] = _safe_div(close - boll_down, (boll_up - boll_down).replace(0.0, np.nan))
        raw["close_rank20"] = close.rolling(20, min_periods=1).rank(pct=True)
        raw["volume_rank20"] = volume.rolling(20, min_periods=1).rank(pct=True)
        raw["turnover_rank20"] = turnover_rate.rolling(20, min_periods=1).rank(pct=True)
        raw["amplitude_rank20"] = price_range.rolling(20, min_periods=1).rank(pct=True)

        # Background factors from financial/fund-flow snapshots.
        background_completion_score = pd.concat(
            [
                holder_count.notna().astype(float),
                block_trade_net.notna().astype(float),
                margin_financing.notna().astype(float),
                northbound_net.notna().astype(float),
                dragon_tiger_flag.notna().astype(float),
                roe.notna().astype(float),
                debt_ratio.notna().astype(float),
                _optional_binary(ordered, "background_data_complete"),
            ],
            axis=1,
        ).mean(axis=1)
        background = pd.DataFrame(
            {
                "bg_is_st": is_st,
                "bg_is_delisting_risk": is_delisting_risk,
                "bg_roe": roe,
                "bg_debt_ratio": debt_ratio,
                "bg_roe_rank60": roe.rolling(60, min_periods=1).rank(pct=True),
                "bg_debt_ratio_rank60": debt_ratio.rolling(60, min_periods=1).rank(pct=True),
                "bg_financial_quality": _safe_div(roe, debt_ratio.abs().replace(0.0, np.nan)),
                "bg_holder_reduction20": -_safe_div(
                    holder_count.diff(20),
                    holder_count.shift(20).abs().replace(0.0, np.nan),
                ),
                "bg_block_trade_net10": block_trade_net.rolling(10, min_periods=1).sum(),
                "bg_margin_trend20": _safe_div(
                    margin_financing.diff(20),
                    margin_financing.shift(20).abs().replace(0.0, np.nan),
                ),
                "bg_northbound_net5": northbound_net.rolling(5, min_periods=1).sum(),
                "bg_dragon_tiger_freq20": dragon_tiger_flag.rolling(20, min_periods=1).mean(),
                "bg_board_code": board_code,
                "holder_count_chg_5": _safe_div(
                    holder_count.diff(5),
                    holder_count.shift(5).abs().replace(0.0, np.nan),
                ),
                "holder_count_chg_20": _safe_div(
                    holder_count.diff(20),
                    holder_count.shift(20).abs().replace(0.0, np.nan),
                ),
                "holder_count_chg_60": _safe_div(
                    holder_count.diff(60),
                    holder_count.shift(60).abs().replace(0.0, np.nan),
                ),
                "holder_count_zscore_60": _rolling_zscore(holder_count, window=60),
                "holder_count_decrease_streak": _decrease_streak(holder_count),
                "northbound_net_5": northbound_net.rolling(5, min_periods=1).sum(),
                "northbound_net_10": northbound_net.rolling(10, min_periods=1).sum(),
                "northbound_net_20": northbound_net.rolling(20, min_periods=1).sum(),
                "northbound_net_60": northbound_net.rolling(60, min_periods=1).sum(),
                "northbound_net_zscore_60": _rolling_zscore(northbound_net, window=60),
                "northbound_momentum_5v20": _safe_div(
                    northbound_net.rolling(5, min_periods=1).sum(),
                    northbound_net.rolling(20, min_periods=1).sum().abs().replace(0.0, np.nan),
                ),
                "financing_balance_chg_5": _safe_div(
                    margin_financing.diff(5),
                    margin_financing.shift(5).abs().replace(0.0, np.nan),
                ),
                "financing_balance_chg_20": _safe_div(
                    margin_financing.diff(20),
                    margin_financing.shift(20).abs().replace(0.0, np.nan),
                ),
                "financing_balance_chg_60": _safe_div(
                    margin_financing.diff(60),
                    margin_financing.shift(60).abs().replace(0.0, np.nan),
                ),
                "financing_balance_zscore_60": _rolling_zscore(margin_financing, window=60),
                "financing_balance_trend_5v20": _safe_div(
                    margin_financing.rolling(5, min_periods=1).mean(),
                    margin_financing.rolling(20, min_periods=1).mean().replace(0.0, np.nan),
                )
                - 1.0,
                "block_trade_net_5": block_trade_net.rolling(5, min_periods=1).sum(),
                "block_trade_net_20": block_trade_net.rolling(20, min_periods=1).sum(),
                "block_trade_frequency_20": block_trade_net.ne(0.0)
                .rolling(20, min_periods=1)
                .mean(),
                "block_trade_direction_10": np.sign(block_trade_net)
                .rolling(10, min_periods=1)
                .mean(),
                "roe_trend_60": _safe_div(
                    roe.diff(60),
                    roe.shift(60).abs().replace(0.0, np.nan),
                ),
                "debt_ratio_stability_60": -_safe_div(
                    debt_ratio.rolling(60, min_periods=5).std(ddof=0),
                    debt_ratio.abs().rolling(60, min_periods=5).mean().replace(0.0, np.nan),
                ),
                "background_completion_score": background_completion_score,
            },
            index=ordered.index,
        )
        raw = pd.concat([raw, background], axis=1)
        raw = pd.concat(
            [
                raw,
                _prepare_intraday_summary(
                    frame=intraday_1m,
                    prefix="i1m",
                    target_index=ordered.index,
                ),
                _prepare_intraday_summary(
                    frame=intraday_5m,
                    prefix="i5m",
                    target_index=ordered.index,
                ),
                _prepare_market_index_frame(
                    frame=market_index,
                    target_index=ordered.index,
                ),
            ],
            axis=1,
        )

        # Defragment once before the final block to avoid repeated frame reallocation.
        raw = raw.copy()

        # Distribution and seasonality.
        calendar_index = pd.DatetimeIndex(ordered.index)
        raw["realized_skew20"] = returns.rolling(20, min_periods=5).skew()
        raw["realized_kurt20"] = returns.rolling(20, min_periods=5).kurt()
        raw["weekday_sin"] = np.sin(2.0 * np.pi * calendar_index.weekday / 7.0)
        raw["weekday_cos"] = np.cos(2.0 * np.pi * calendar_index.weekday / 7.0)
        raw["month_sin"] = np.sin(2.0 * np.pi * calendar_index.month / 12.0)
        raw["month_cos"] = np.cos(2.0 * np.pi * calendar_index.month / 12.0)

        # Shift all factors by one trading day to enforce strict T-1 availability.
        features = raw.shift(1)
        features = features.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return features


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    result = numerator / denominator.replace(0.0, np.nan)
    return pd.Series(result, index=numerator.index)


def _optional_numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").astype(float)


def _optional_binary(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(0.0, index=frame.index, dtype=float)
    raw = frame[column]
    if raw.dtype == bool:
        return raw.astype(float)
    normalized = raw.astype(str).str.strip().str.lower()
    true_tokens = {"1", "true", "y", "yes", "st", "risk", "warning"}
    return normalized.isin(true_tokens).astype(float)


def _decrease_streak(series: pd.Series) -> pd.Series:
    parsed = pd.to_numeric(series, errors="coerce")
    streak: list[float] = []
    current = 0.0
    previous = np.nan
    for value in parsed.tolist():
        if pd.notna(value) and pd.notna(previous) and float(value) < float(previous):
            current += 1.0
        elif pd.notna(value):
            current = 0.0
        else:
            current = 0.0
        streak.append(current)
        previous = value
    return pd.Series(streak, index=series.index, dtype=float)


def _prepare_intraday_summary(
    *,
    frame: pd.DataFrame | None,
    prefix: str,
    target_index: pd.Index,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(index=target_index)

    prepared = frame.copy()
    if "symbol" in prepared.columns:
        prepared = prepared.drop(columns=["symbol"])
    if not isinstance(prepared.index, pd.DatetimeIndex):
        prepared.index = pd.to_datetime(prepared.index, errors="coerce")
    prepared = prepared[prepared.index.notna()].sort_index()
    if prepared.empty:
        return pd.DataFrame(index=target_index)

    numeric_columns: list[str] = []
    for column in prepared.columns:
        series = pd.to_numeric(prepared[column], errors="coerce")
        if series.notna().any():
            prepared[column] = series.astype(float)
            numeric_columns.append(column)
    if not numeric_columns:
        return pd.DataFrame(index=target_index)

    prepared = prepared[numeric_columns]
    prepared = prepared.rename(columns={column: f"{prefix}_{column}" for column in numeric_columns})
    return prepared.reindex(target_index)


def _prepare_market_index_frame(
    *,
    frame: pd.DataFrame | None,
    target_index: pd.Index,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(index=target_index)

    prepared = frame.copy()
    if not isinstance(prepared.index, pd.DatetimeIndex):
        prepared.index = pd.to_datetime(prepared.index, errors="coerce")
    prepared = prepared[prepared.index.notna()].sort_index()
    if prepared.empty:
        return pd.DataFrame(index=target_index)

    numeric_columns: list[str] = []
    for column in prepared.columns:
        series = pd.to_numeric(prepared[column], errors="coerce")
        if series.notna().any():
            prepared[column] = series.astype(float)
            numeric_columns.append(column)
    if not numeric_columns:
        return pd.DataFrame(index=target_index)

    return prepared[numeric_columns].reindex(target_index)


def _encode_board(series: pd.Series) -> pd.Series:
    normalized = series.astype(str).str.strip().str.lower()
    mapped = normalized.map(
        {
            "main": 0.0,
            "sh_main": 0.0,
            "sz_main": 0.0,
            "chi_next": 0.5,
            "gem": 0.5,
            "创业板": 0.5,
            "star": 1.0,
            "kechuang": 1.0,
            "科创板": 1.0,
            "bj": 0.3,
            "beijing": 0.3,
            "北交所": 0.3,
            "主板": 0.0,
        }
    )
    return mapped.fillna(0.0).astype(float)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = _safe_div(avg_gain, avg_loss)
    return 100.0 - (100.0 / (1.0 + rs))


def _stochastic_kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    lowest = low.rolling(period, min_periods=1).min()
    highest = high.rolling(period, min_periods=1).max()
    rsv = _safe_div(close - lowest, (highest - lowest).replace(0.0, np.nan)) * 100.0
    k = rsv.ewm(com=2.0, adjust=False).mean()
    d = k.ewm(com=2.0, adjust=False).mean()
    j = 3.0 * k - 2.0 * d
    return k, d, j


def _cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    typical = (high + low + close) / 3.0
    ma = typical.rolling(period, min_periods=1).mean()
    mad = (typical - ma).abs().rolling(period, min_periods=1).mean()
    return _safe_div(typical - ma, 0.015 * mad.replace(0.0, np.nan))


def _williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    lowest = low.rolling(period, min_periods=1).min()
    highest = high.rolling(period, min_periods=1).max()
    return -100.0 * _safe_div(highest - close, (highest - lowest).replace(0.0, np.nan))


def _mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int,
) -> pd.Series:
    typical = (high + low + close) / 3.0
    money_flow = typical * volume
    direction = typical.diff()
    pos_flow = money_flow.where(direction > 0.0, 0.0)
    neg_flow = money_flow.where(direction < 0.0, 0.0).abs()
    pos_sum = pos_flow.rolling(period, min_periods=1).sum()
    neg_sum = neg_flow.rolling(period, min_periods=1).sum()
    ratio = _safe_div(pos_sum, neg_sum.replace(0.0, np.nan))
    return 100.0 - (100.0 / (1.0 + ratio))


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    result = (direction * volume).cumsum()
    return pd.Series(result, index=close.index)


def _adl(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    clv = _safe_div((close - low) - (high - close), (high - low).replace(0.0, np.nan))
    return (clv.fillna(0.0) * volume).cumsum()


def _pvt(close: pd.Series, volume: pd.Series) -> pd.Series:
    ret = close.pct_change().fillna(0.0)
    return (ret * volume).cumsum()


def _rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=1).mean()
    std = series.rolling(window, min_periods=1).std(ddof=0)
    return _safe_div(series - mean, std.replace(0.0, np.nan))


def _rolling_diff(series: pd.Series, window: int) -> pd.Series:
    baseline = series.rolling(window, min_periods=1).mean()
    return series - baseline
