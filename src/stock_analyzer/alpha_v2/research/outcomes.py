"""Alpha V2 多 horizon 可执行 outcome（S11 / 原 P1-01，即 **Label V2**）。

**它回答什么**：给定"T 日收盘后的决策"，把同一条决策在未来 3/5/10/15 个交易日的
**真实可成交净收益**、**相对基准的超额**、**最大不利/有利偏移**、**正收益方向**与
**TP/SL 路径标签**一次性算出来。

与 Legacy label 的关系：

- Legacy ``soup_10d_tp8_before_sl5``（10D TP/SL 路径）**保留不动**，并作为
  ``tp8_before_sl5_10d`` 继续产出（口径见下）；
- V2 新增的是 **executable outcome**：入场走 S02 的 ``simulate_entry``（T+1 开盘、
  涨停/停牌/无有效价一律 ``no_fill``），成交价用 S07 的 **raw** 序列，收益扣
  **往返成本**，因此"不可成交"样本**不会**被算成收益。

四条硬纪律（对应 Gate S11 的 Blocking 项）：

1. **不从 T 收盘价算未来收益**：入场价恒为 T+1 开盘（``entry_date > decision_date``），
   否则该行 ``executable=False``、收益列 ``not_available``；
2. **未成交不计收益**：``executable=False`` 的行所有 ``net_return_*`` / ``mae_*`` /
   ``mfe_*`` 都是 ``not_available``（不是 0，也不是"假设能成交"）；
3. **benchmark 必须显式**：超额列由调用方给出的 benchmark 序列决定，并在列里写明
   ``benchmark_name``；没有 benchmark 时超额列是 ``not_available``（不默认全市场、
   不默认指数）；
4. **horizon 成熟日可追**：每行给出 ``maturity_date_{h}d``（= 入场日起第 h 个
   该票交易日的日期）与 ``matured_{h}d``，数据不足就是"未成熟"，不提前填数。

horizon 口径（与 ``pit_dataset`` 的 ``fwd_return``、legacy soup 一致，避免两套语义）：

```text
decision_date = T（收盘后）
entry  = T+1 开盘（可成交才成立）        # 入场日 = 第 1 个持有日
exit_h = 入场日之后第 (h-1) 个该票交易日收盘
maturity_date_hd = exit_h 的日期
```
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.panel import DailyPanel, bar_view
from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import LimitRuleConfig

NOT_AVAILABLE = "not_available"

HORIZONS: tuple[int, ...] = (3, 5, 10, 15)
SHORT_HORIZONS: tuple[int, ...] = (3, 5)
PRIMARY_HORIZON = 5

LABEL_V2_SCHEMA = "alpha_v2_label_v2.v1"

# TP/SL 路径标签沿用 legacy 默认（10D、TP +8%、SL -5%）。
PATH_LABEL_HORIZON = 10
PATH_LABEL_TP_PCT = 0.08
PATH_LABEL_SL_PCT = 0.05

# 决策时点：T 日收盘后（与 ``alpha_v2.short_scan`` 的历史决策时间一致）。
DECISION_TIME_OF_DAY = time(15, 30)

# 参考名义金额：成本按"费率"进模型，金额只用来摊薄最低佣金（5 元/笔）。
# 用小额会在低价股上把最低佣金放大成几个百分点，凭空制造"成本很高的错觉"。
DEFAULT_REFERENCE_NOTIONAL = 100_000.0


@dataclass(frozen=True, slots=True)
class OutcomeSpec:
    """一次 outcome 计算的口径（全部字段都会进审计工件）。"""

    horizons: tuple[int, ...] = HORIZONS
    short_horizons: tuple[int, ...] = SHORT_HORIZONS
    primary_horizon: int = PRIMARY_HORIZON
    path_label_horizon: int = PATH_LABEL_HORIZON
    take_profit_pct: float = PATH_LABEL_TP_PCT
    stop_loss_pct: float = PATH_LABEL_SL_PCT
    conflict_policy: str = "soft_label"
    conflict_soft_label_value: float = 0.5
    reference_notional: float = DEFAULT_REFERENCE_NOTIONAL

    def horizon_columns(self, prefix: str) -> list[str]:
        return [f"{prefix}_{int(h)}d" for h in self.horizons]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": LABEL_V2_SCHEMA,
            "horizons": [int(h) for h in self.horizons],
            "short_horizons": [int(h) for h in self.short_horizons],
            "primary_horizon": int(self.primary_horizon),
            "entry_mode": "next_session_open",
            "price_basis": "raw",
            "cost_model": "round_trip_rate_from_matcher_config",
            "reference_notional": float(self.reference_notional),
            "path_label": {
                "name": "tp8_before_sl5_10d",
                "horizon": int(self.path_label_horizon),
                "take_profit_pct": float(self.take_profit_pct),
                "stop_loss_pct": float(self.stop_loss_pct),
                "conflict_policy": self.conflict_policy,
                "conflict_soft_label_value": float(self.conflict_soft_label_value),
                "note": "legacy soup 口径（价格路径不含成本/不判可成交），保留用于对照",
            },
        }


@dataclass(frozen=True, slots=True)
class DecisionPoint:
    """一条决策：T 日收盘后对某标的的判断。"""

    symbol: str
    decision_date: date


@dataclass(frozen=True, slots=True)
class OutcomeRun:
    """outcome 计算结果 + 本次运行的口径与样本账。"""

    frame: pd.DataFrame
    spec: OutcomeSpec
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_payload(),
            "rows": int(len(self.frame)),
            "diagnostics": dict(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# 成本
# ---------------------------------------------------------------------------


def round_trip_cost_rate(
    *,
    matcher: ExecutionMatcher,
    trade_date: date | None = None,
    reference_notional: float = DEFAULT_REFERENCE_NOTIONAL,
) -> dict[str, float]:
    """以参考名义金额估算的**往返成本率**（佣金 + 过户费 + 卖出印花税）。

    只用于研究口径的净收益折算：真实下单金额不同，最低佣金摊薄程度也不同，
    故这里显式声明参考金额并同时回写 ``buy_rate`` / ``sell_rate``，便于复核。
    """
    notional = max(1.0, float(reference_notional))
    quantity = max(1, int(notional // 100.0))
    price = 100.0
    amount = price * quantity
    buy_cost = matcher.estimate_cost("buy", price, quantity, trade_date=trade_date)
    sell_cost = matcher.estimate_cost("sell", price, quantity, trade_date=trade_date)
    return {
        "reference_notional": notional,
        "buy_cost_rate": round(buy_cost / amount, 8),
        "sell_cost_rate": round(sell_cost / amount, 8),
        "round_trip_cost_rate": round((buy_cost + sell_cost) / amount, 8),
    }


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------


def compute_outcomes(
    *,
    panel: DailyPanel,
    decisions: Sequence[DecisionPoint],
    spec: OutcomeSpec | None = None,
    matcher: ExecutionMatcher | None = None,
    slippage_ratio: float = 0.0,
    price_mode: str = "",
    price_mode_certified: bool = False,
    source_meta: Mapping[str, object] | None = None,
) -> OutcomeRun:
    """对每条决策算出全部 horizon 的可执行 outcome。

    只算"事实"侧（入场/退出/净收益/MAE/MFE/方向/路径标签），不含超额——
    超额由 :func:`attach_excess_returns` 按显式基准补。
    """
    resolved_spec = spec or OutcomeSpec()
    resolved_matcher = matcher or ExecutionMatcher(
        _default_matcher_config(), limit_rule=LimitRuleConfig()
    )
    cost = round_trip_cost_rate(
        matcher=resolved_matcher,
        trade_date=None,
        reference_notional=resolved_spec.reference_notional,
    )
    round_trip_rate = float(cost["round_trip_cost_rate"])

    by_symbol: dict[str, list[DecisionPoint]] = {}
    for item in decisions:
        by_symbol.setdefault(str(item.symbol), []).append(item)

    rows: list[dict[str, object]] = []
    no_fill_counts: dict[str, int] = {}
    exit_no_fill_counts: dict[str, int] = {}
    corporate_action_count = 0
    for symbol in sorted(by_symbol):
        frame = panel.symbol_bars(symbol)
        if frame is None or frame.empty:
            for item in by_symbol[symbol]:
                rows.append(
                    _base_row(
                        item,
                        spec=resolved_spec,
                        price_mode=price_mode,
                        price_mode_certified=price_mode_certified,
                        extra={"no_fill_reason": "symbol_not_in_panel"},
                    )
                )
                no_fill_counts["symbol_not_in_panel"] = (
                    no_fill_counts.get("symbol_not_in_panel", 0) + 1
                )
            continue
        dates = [ts.date() for ts in frame.index]
        positions = {day: index for index, day in enumerate(dates)}
        for item in by_symbol[symbol]:
            row = _outcome_row(
                item=item,
                frame=frame,
                positions=positions,
                calendar=panel.calendar,
                spec=resolved_spec,
                matcher=resolved_matcher,
                slippage_ratio=slippage_ratio,
                round_trip_cost_rate=round_trip_rate,
                cost=cost,
                price_mode=price_mode,
                price_mode_certified=price_mode_certified,
            )
            if not row.get("executable"):
                reason = str(row.get("no_fill_reason") or "unknown")
                no_fill_counts[reason] = no_fill_counts.get(reason, 0) + 1
            for horizon in resolved_spec.horizons:
                if row.get(f"exit_no_fill_{int(horizon)}d") is True:
                    key = f"exit_no_fill_{int(horizon)}d"
                    exit_no_fill_counts[key] = exit_no_fill_counts.get(key, 0) + 1
            if row.get("corporate_action_suspected") is True:
                corporate_action_count += 1
            rows.append(row)

    frame_out = pd.DataFrame(rows)
    diagnostics = {
        "row_count": int(len(frame_out)),
        "no_fill_total": int(sum(no_fill_counts.values())),
        "no_fill_by_reason": dict(sorted(no_fill_counts.items())),
        "exit_no_fill_by_horizon": dict(sorted(exit_no_fill_counts.items())),
        "corporate_action_suspected_rows": int(corporate_action_count),
        "cost": {key: float(value) for key, value in cost.items()},
        "slippage_ratio": float(slippage_ratio),
        "entry_mode": "next_session_open",
        "max_entry_sessions": 1,
        "price_mode": str(price_mode or "unknown"),
        "price_mode_certified": bool(price_mode_certified),
        "source_meta": dict(source_meta or {}),
    }
    return OutcomeRun(frame=frame_out, spec=resolved_spec, diagnostics=diagnostics)


def _default_matcher_config() -> Any:
    from stock_analyzer.config import BacktestMatcherConfig

    return BacktestMatcherConfig()


def _base_row(
    decision: DecisionPoint,
    *,
    spec: OutcomeSpec,
    price_mode: str,
    price_mode_certified: bool,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "decision_date": decision.decision_date.isoformat(),
        "symbol": str(decision.symbol),
        "executable": False,
        "no_fill_reason": "",
        "entry_date": NOT_AVAILABLE,
        "entry_delay_sessions": NOT_AVAILABLE,
        "entry_price_raw": NOT_AVAILABLE,
        "entry_price_net": NOT_AVAILABLE,
        "price_mode": str(price_mode or "unknown"),
        "price_mode_certified": bool(price_mode_certified),
        "execution_uncertain": not bool(price_mode_certified),
        "corporate_action_suspected": None,
        "corporate_action_flag_source": "unavailable",
    }
    for horizon in spec.horizons:
        key = int(horizon)
        row[f"matured_{key}d"] = False
        row[f"maturity_date_{key}d"] = NOT_AVAILABLE
        row[f"exit_no_fill_{key}d"] = NOT_AVAILABLE
        row[f"net_return_{key}d"] = NOT_AVAILABLE
        row[f"mae_{key}d"] = NOT_AVAILABLE
        row[f"mfe_{key}d"] = NOT_AVAILABLE
        if key in spec.short_horizons:
            row[f"up_net_{key}d"] = NOT_AVAILABLE
    row[f"tp8_before_sl5_{int(spec.path_label_horizon)}d"] = NOT_AVAILABLE
    row[f"tp8_conflict_{int(spec.path_label_horizon)}d"] = NOT_AVAILABLE
    if extra:
        row.update(dict(extra))
    return row


def _outcome_row(
    *,
    item: DecisionPoint,
    frame: pd.DataFrame,
    positions: Mapping[date, int],
    calendar: Sequence[date],
    spec: OutcomeSpec,
    matcher: ExecutionMatcher,
    slippage_ratio: float,
    round_trip_cost_rate: float,
    cost: Mapping[str, float],
    price_mode: str,
    price_mode_certified: bool,
) -> dict[str, object]:
    row = _base_row(
        item, spec=spec, price_mode=price_mode, price_mode_certified=price_mode_certified
    )
    position = positions.get(item.decision_date)
    if position is None:
        row["no_fill_reason"] = "decision_date_not_in_panel"
        return row

    next_bars = frame.iloc[position + 1 : position + 2]
    if next_bars.empty:
        row["no_fill_reason"] = "no_bar_after_decision"
        return row
    next_stamp = next_bars.index[0]
    next_date = next_stamp.date() if hasattr(next_stamp, "date") else next_stamp
    gap = _session_gap(calendar, item.decision_date, next_date)
    row["entry_delay_sessions"] = gap
    if gap != 1:
        # 主口径只认 T+1：下一根可观测 bar 不是"下一个交易日"（停牌/数据缺口/
        # 停更）→ 该决策在真实市场里买不到，记 no_fill 而不是推迟成交。
        row["no_fill_reason"] = (
            "suspended_or_missing_on_next_session" if gap > 1 else "bar_before_decision"
        )
        return row

    next_row = next_bars.iloc[0]
    listing_days = _positive_int(next_row.get("listing_days_lower_bound"))
    entry = matcher.simulate_entry(
        signal_date=datetime.combine(item.decision_date, DECISION_TIME_OF_DAY),
        future_bars=[
            (
                _to_datetime(next_stamp),
                bar_view(next_row, symbol=item.symbol, listing_days=listing_days),
            )
        ],
        slippage_ratio=max(0.0, float(slippage_ratio)),
        max_entry_sessions=1,
        quantity=0,
    )
    row["entry_date"] = next_date.isoformat()
    row["entry_price_raw"] = (
        round(float(entry.entry_price_raw), 6) if entry.executed else NOT_AVAILABLE
    )
    row["entry_price_net"] = (
        round(float(entry.net_entry_price), 6) if entry.executed else NOT_AVAILABLE
    )
    row["limit_source"] = str(dict(entry.details).get("buy_reason", "")) or NOT_AVAILABLE
    if not entry.executed:
        row["no_fill_reason"] = str(entry.no_fill_reason or "no_fill")
        row["executable"] = False
        return row
    row["executable"] = True
    row["no_fill_reason"] = ""
    row["round_trip_cost_rate"] = float(round_trip_cost_rate)
    row["buy_cost_rate"] = float(cost.get("buy_cost_rate", 0.0))
    row["sell_cost_rate"] = float(cost.get("sell_cost_rate", 0.0))

    corporate = _corporate_action_flag(
        frame, position=position, window=max(int(h) for h in spec.horizons)
    )
    row["corporate_action_suspected"] = corporate[0]
    row["corporate_action_flag_source"] = corporate[1]

    entry_price = float(entry.net_entry_price)
    tail = frame.iloc[position + 1 :]
    highs = _numeric_array(tail["high"])
    lows = _numeric_array(tail["low"])
    closes = _numeric_array(tail["close"])
    sessions = [ts.date() if hasattr(ts, "date") else ts for ts in tail.index]

    for horizon in spec.horizons:
        key = int(horizon)
        if len(closes) < key:
            row[f"matured_{key}d"] = False
            continue
        exit_index = key - 1
        exit_price_raw = closes[exit_index]
        exit_suspended = _is_true(tail.iloc[exit_index].get("suspended"))
        row[f"matured_{key}d"] = True
        row[f"maturity_date_{key}d"] = sessions[exit_index].isoformat()
        if exit_suspended or not math.isfinite(exit_price_raw) or exit_price_raw <= 0:
            row[f"exit_no_fill_{key}d"] = True
            continue
        row[f"exit_no_fill_{key}d"] = False
        exit_net = matcher.apply_slippage(
            price=float(exit_price_raw), side="sell", slippage_ratio=max(0.0, float(slippage_ratio))
        )
        exit_net = matcher.apply_price_tick(exit_net, side="sell")
        gross = exit_net / entry_price - 1.0
        row[f"net_return_{key}d"] = round(gross - float(round_trip_cost_rate), 8)
        if key in spec.short_horizons:
            row[f"up_net_{key}d"] = bool(row[f"net_return_{key}d"] > 0)

        window_low = _window_extreme(lows[:key], "min")
        window_high = _window_extreme(highs[:key], "max")
        if window_low is not None:
            row[f"mae_{key}d"] = round(window_low / entry_price - 1.0, 8)
        if window_high is not None:
            row[f"mfe_{key}d"] = round(window_high / entry_price - 1.0, 8)

    _fill_path_label(row, spec=spec, frame=frame, position=position)
    return row


def _fill_path_label(
    row: dict[str, object], *, spec: OutcomeSpec, frame: pd.DataFrame, position: int
) -> None:
    """TP/SL 路径标签（legacy soup 口径）。

    价格路径从入场 bar（T+1）起算、用 raw high/low、**不判可成交、不扣成本**——
    与 legacy ``build_soup_labels`` 完全同源（冲突策略复用同一实现），
    因此可与生产 label 直接对照；V2 的净收益口径见 ``net_return_*``。
    """
    from stock_analyzer.labels.soup import _resolve_same_bar_conflict

    key = int(spec.path_label_horizon)
    column = f"tp8_before_sl5_{key}d"
    conflict_column = f"tp8_conflict_{key}d"
    if row.get("entry_price_raw") == NOT_AVAILABLE:
        return
    entry_price = float(row["entry_price_raw"])  # type: ignore[arg-type]
    tail = frame.iloc[position + 1 : position + 1 + key]
    if len(tail) < key:
        return
    tp_price = entry_price * (1.0 + float(spec.take_profit_pct))
    sl_price = entry_price * (1.0 - float(spec.stop_loss_pct))
    outcome = 0.0
    conflict = False
    for _, bar in tail.iterrows():
        high = _finite(bar.get("high"))
        low = _finite(bar.get("low"))
        hit_tp = high is not None and high >= tp_price
        hit_sl = low is not None and low <= sl_price
        if hit_tp and hit_sl:
            outcome = float(
                _resolve_same_bar_conflict(
                    bar=bar,
                    entry_price=entry_price,
                    take_profit_price=tp_price,
                    stop_loss_price=sl_price,
                    policy=spec.conflict_policy,
                    soft_label_value=spec.conflict_soft_label_value,
                )
            )
            conflict = True
            break
        if hit_tp:
            outcome = 1.0
            break
        if hit_sl:
            outcome = 0.0
            break
    row[column] = outcome
    row[conflict_column] = conflict


def _corporate_action_flag(
    frame: pd.DataFrame, *, position: int, window: int
) -> tuple[bool | None, str]:
    """持有窗口内是否疑似除权除息：仅在数据源给出权威 ``pre_close`` 时可用。

    判定：数据源 pre_close 与自己算的上一根 raw 收盘相差超过 0.5% ⇒ 当日大概率
    是除权除息日（价格序列在除权日发生非交易性跳变）。拿不到权威前收时返回
    ``(None, "pre_close_source_unavailable")``——**不知道就写不知道**。

    窗口 = 入场日起 ``window`` 根 bar（覆盖全部 horizon）：除权日只要落在窗口内，
    该次持有期的 raw 收益就与含现金分红的经济收益不等，必须标出来。
    """
    if "pre_close_source" not in frame.columns:
        return None, "pre_close_source_unavailable"
    tail = frame.iloc[position + 1 : position + 1 + max(1, int(window))]
    if tail.empty:
        return None, "pre_close_source_unavailable"
    sources = tail["pre_close_source"].astype(str)
    if not bool((sources == "source").any()):
        return None, "pre_close_source_unavailable"
    pre_close = pd.to_numeric(tail["pre_close"], errors="coerce")
    prev_close = pd.to_numeric(tail["prev_close_raw"], errors="coerce")
    usable = (sources == "source") & pre_close.notna() & prev_close.notna() & (prev_close > 0)
    if not bool(usable.any()):
        return None, "pre_close_value_missing"
    ratio = (pre_close / prev_close - 1.0).abs()
    return bool((ratio[usable] > 0.005).any()), "pre_close_vs_prev_close"


def _session_gap(calendar: Sequence[date], start: date, end: date) -> int:
    """两个日期之间的交易日间隔（start 之后第几个交易日到达 end）。"""
    try:
        return calendar.index(end) - calendar.index(start)
    except ValueError:
        return -1


def _window_extreme(values: np.ndarray, mode: str) -> float | None:
    usable = values[np.isfinite(values) & (values > 0)]
    if usable.size == 0:
        return None
    return float(usable.min() if mode == "min" else usable.max())


def _numeric_array(series: pd.Series) -> np.ndarray:
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_int(value: object) -> int | None:
    parsed = _finite(value)
    if parsed is None or parsed <= 0:
        return None
    return int(parsed)


def _is_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes"}


def _to_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, DECISION_TIME_OF_DAY)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.combine(date.today(), DECISION_TIME_OF_DAY)


# ---------------------------------------------------------------------------
# 超额收益（benchmark）
# ---------------------------------------------------------------------------


def benchmark_series_from_outcomes(
    outcomes: pd.DataFrame,
    *,
    horizons: Sequence[int] = HORIZONS,
    pool_mask: pd.Series | None = None,
    name: str = "eligible_ew",
) -> pd.DataFrame:
    """把某个池子的同等权平均收益做成 benchmark 序列（按 decision_date × horizon）。

    ``pool_mask`` 选择基准池成员。池内**只统计可成交且已成熟**的样本——
    拿"假设能成交"的收益当基准会把基准本身吹高，从而把 Alpha 抹平。
    """
    if outcomes.empty:
        return pd.DataFrame(
            columns=["decision_date", "horizon", "benchmark_return", "benchmark_name", "pool_size"]
        )
    frame = outcomes if pool_mask is None else outcomes.loc[pool_mask.to_numpy()]
    rows: list[dict[str, object]] = []
    for horizon in horizons:
        key = int(horizon)
        column = f"net_return_{key}d"
        matured = f"matured_{key}d"
        if column not in frame.columns:
            continue
        usable = frame[frame["executable"] & frame[matured]]
        usable = usable[usable[column] != NOT_AVAILABLE]
        for decision_date, group in usable.groupby("decision_date", sort=True):
            values = pd.to_numeric(group[column], errors="coerce").dropna()
            if values.empty:
                continue
            rows.append(
                {
                    "decision_date": str(decision_date),
                    "horizon": key,
                    "benchmark_return": float(values.mean()),
                    "benchmark_name": name,
                    "pool_size": int(values.shape[0]),
                }
            )
    return pd.DataFrame(rows)


def attach_excess_returns(
    outcomes: pd.DataFrame,
    *,
    benchmark: pd.DataFrame,
    horizons: Sequence[int] = HORIZONS,
    short_horizons: Sequence[int] = SHORT_HORIZONS,
    name: str = "",
) -> pd.DataFrame:
    """给 outcome 帧补 ``excess_return_{h}d`` / ``up_excess_{h}d`` / ``benchmark_return_{h}d``。

    没有对应 benchmark 行时超额列是 ``not_available``——**不默认全市场，也不默认指数**。
    """
    frame = outcomes.copy()
    if benchmark.empty:
        for horizon in horizons:
            key = int(horizon)
            frame[f"benchmark_return_{key}d"] = NOT_AVAILABLE
            frame[f"excess_return_{key}d"] = NOT_AVAILABLE
            if key in short_horizons:
                frame[f"up_excess_{key}d"] = NOT_AVAILABLE
        frame["benchmark_name"] = str(name or NOT_AVAILABLE)
        return frame

    lookup = benchmark.copy()
    lookup["decision_date"] = lookup["decision_date"].astype(str)
    lookup["horizon"] = lookup["horizon"].astype(int)
    wide = lookup.pivot_table(
        index="decision_date", columns="horizon", values="benchmark_return", aggfunc="first"
    )
    benchmark_name = str(name or lookup["benchmark_name"].iloc[0])
    frame["__decision_key"] = frame["decision_date"].astype(str)
    decision_index = pd.Index(wide.index)

    for horizon in horizons:
        key = int(horizon)
        column = f"net_return_{key}d"
        if key not in wide.columns:
            base = pd.Series(np.nan, index=frame.index)
        else:
            aligned = wide[key].reindex(frame["__decision_key"].to_numpy())
            base = pd.Series(aligned.to_numpy(), index=frame.index)
            # decision_date 不在基准里（当日池子为空）→ 明确 not_available
            base = base.where(frame["__decision_key"].isin(decision_index))
        net = pd.to_numeric(frame[column], errors="coerce")
        valid = base.notna() & net.notna()
        frame[f"benchmark_return_{key}d"] = [
            NOT_AVAILABLE if not ok else round(float(value), 8)
            for ok, value in zip(valid.to_numpy(), base.fillna(0.0).to_numpy(), strict=True)
        ]
        excess = (net - base).where(valid)
        frame[f"excess_return_{key}d"] = [
            NOT_AVAILABLE if (not ok or pd.isna(value)) else round(float(value), 8)
            for ok, value in zip(valid.to_numpy(), excess.to_numpy(), strict=True)
        ]
        if key in short_horizons:
            frame[f"up_excess_{key}d"] = [
                NOT_AVAILABLE if (not ok or pd.isna(value)) else bool(float(value) > 0)
                for ok, value in zip(valid.to_numpy(), excess.to_numpy(), strict=True)
            ]
    frame = frame.drop(columns=["__decision_key"])
    frame["benchmark_name"] = benchmark_name
    return frame


def build_label_v2(
    *,
    panel: DailyPanel,
    decisions: Sequence[DecisionPoint],
    spec: OutcomeSpec | None = None,
    matcher: ExecutionMatcher | None = None,
    slippage_ratio: float = 0.0,
    price_mode: str = "",
    price_mode_certified: bool = False,
    benchmark_name: str = "eligible_ew",
    source_meta: Mapping[str, object] | None = None,
) -> OutcomeRun:
    """一站式：算 outcome + 以全体决策（默认 = eligible 池）等权收益为基准补超额。

    ``decisions`` 的构造方式决定了基准是谁：研究链路把"当日的 PIT eligible 截面"
    作为决策集合传入，于是默认基准就是 **Eligible Universe EW**；S12 会用不同的
    池子重新调用 :func:`attach_excess_returns` 得到 Quality Pool / Style-Matched 层。
    """
    run = compute_outcomes(
        panel=panel,
        decisions=decisions,
        spec=spec,
        matcher=matcher,
        slippage_ratio=slippage_ratio,
        price_mode=price_mode,
        price_mode_certified=price_mode_certified,
        source_meta=source_meta,
    )
    resolved_spec = run.spec
    benchmark = benchmark_series_from_outcomes(
        run.frame,
        horizons=resolved_spec.horizons,
        pool_mask=None,
        name=benchmark_name,
    )
    frame = attach_excess_returns(
        run.frame,
        benchmark=benchmark,
        horizons=resolved_spec.horizons,
        short_horizons=resolved_spec.short_horizons,
        name=benchmark_name,
    )
    diagnostics = dict(run.diagnostics)
    diagnostics["benchmark"] = {
        "name": benchmark_name,
        "definition": "equal_weight_mean_of_pool_net_return_same_entry_exit_window",
        "pool_size_median": (
            float(benchmark["pool_size"].median()) if not benchmark.empty else 0.0
        ),
        "date_horizon_rows": int(len(benchmark)),
    }
    diagnostics.update(sample_diagnostics(frame, spec=resolved_spec))
    return OutcomeRun(frame=frame, spec=resolved_spec, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# 样本账（哪一行能进主评价样本）
# ---------------------------------------------------------------------------


def sample_diagnostics(
    frame: pd.DataFrame, *, spec: OutcomeSpec | None = None
) -> dict[str, object]:
    """主评价样本账：可成交 / 成熟 / 价格口径已认证 三条都要满足。"""
    resolved = spec or OutcomeSpec()
    if frame.empty:
        return {
            "main_sample_rows": 0,
            "main_sample_status": "empty",
            "execution_uncertain_rows": 0,
            "corporate_action_suspected_rows": 0,
        }
    executable = frame["executable"].astype(bool)
    exit_no_fill = pd.Series(False, index=frame.index)
    matured_any = pd.Series(False, index=frame.index)
    for horizon in resolved.horizons:
        key = int(horizon)
        matured_column = f"matured_{key}d"
        if matured_column in frame.columns:
            matured_any = matured_any | frame[matured_column].astype(bool)
        exit_column = f"exit_no_fill_{key}d"
        if exit_column in frame.columns:
            exit_no_fill = exit_no_fill | (frame[exit_column] == True)  # noqa: E712
    certified = (
        frame["price_mode_certified"].astype(bool)
        if "price_mode_certified" in frame.columns
        else pd.Series(False, index=frame.index)
    )
    main_sample = executable & matured_any & certified
    uncertain_rows = int((~certified).sum())
    status = "ok"
    if not bool(certified.any()):
        status = "execution_price_mode_unverified"
    elif int(main_sample.sum()) == 0:
        status = "no_executable_matured_rows"
    return {
        "main_sample_rows": int(main_sample.sum()),
        "main_sample_status": status,
        "executable_rows": int(executable.sum()),
        "matured_any_rows": int(matured_any.sum()),
        "exit_no_fill_rows": int(exit_no_fill.sum()),
        "execution_uncertain_rows": uncertain_rows,
        "corporate_action_suspected_rows": int(
            (frame.get("corporate_action_suspected") == True).sum()  # noqa: E712
        ),
    }


def outcome_columns(spec: OutcomeSpec | None = None) -> list[str]:
    """outcome 帧的稳定列清单（供报告/测试断言 schema）。"""
    resolved = spec or OutcomeSpec()
    columns = [
        "decision_date",
        "symbol",
        "executable",
        "no_fill_reason",
        "entry_date",
        "entry_delay_sessions",
        "entry_price_raw",
        "entry_price_net",
        "round_trip_cost_rate",
        "price_mode",
        "price_mode_certified",
        "execution_uncertain",
        "corporate_action_suspected",
        "corporate_action_flag_source",
        "benchmark_name",
    ]
    for horizon in resolved.horizons:
        key = int(horizon)
        columns.extend(
            [
                f"matured_{key}d",
                f"maturity_date_{key}d",
                f"exit_no_fill_{key}d",
                f"net_return_{key}d",
                f"excess_return_{key}d",
                f"benchmark_return_{key}d",
                f"mae_{key}d",
                f"mfe_{key}d",
            ]
        )
        if key in resolved.short_horizons:
            columns.extend([f"up_net_{key}d", f"up_excess_{key}d"])
    columns.extend(
        [
            f"tp8_before_sl5_{int(resolved.path_label_horizon)}d",
            f"tp8_conflict_{int(resolved.path_label_horizon)}d",
        ]
    )
    return columns


__all__ = [
    "DECISION_TIME_OF_DAY",
    "DecisionPoint",
    "HORIZONS",
    "LABEL_V2_SCHEMA",
    "NOT_AVAILABLE",
    "OutcomeRun",
    "OutcomeSpec",
    "PATH_LABEL_HORIZON",
    "PRIMARY_HORIZON",
    "SHORT_HORIZONS",
    "attach_excess_returns",
    "benchmark_series_from_outcomes",
    "build_label_v2",
    "compute_outcomes",
    "outcome_columns",
    "round_trip_cost_rate",
    "sample_diagnostics",
]
