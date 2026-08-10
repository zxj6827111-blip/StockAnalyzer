"""Monthly review report with user execution discipline scoring (PRD §15).

Inputs
------
- ``trades``: portfolio trade records (``portfolio/book.py`` ``to_dict``
  shape) with keys ``side``, ``symbol``, ``timestamp``, ``reason``,
  ``entry_price``/``quantity`` (buy) and ``exit_price``/``exit_quantity``
  (sell), ``fee``/``exit_fee``.
- ``positions``: end-of-month position snapshot (``positions()`` shape)
  with ``symbol``, ``target_position``, ``opened_at``, ``status``,
  ``peak_pnl_pct``, ``take_profit_stage``.
- ``signals``: S-level signal records with ``symbol``, ``grade``, ``action``
  and a date key (``timestamp``/``decision_time``/``date``).
- ``outcomes``: execution outcome records (``OutcomeRecord`` JSON shape)
  with ``realized_slippage_bp``, ``execution_fill_ratio`` and a date key
  (``outcome_updated_at``/``label_mature_time``).
- ``reconcile``: sim-vs-broker weekly report (``reconcile_weekly_report``
  shape) or its ``sim_vs_broker`` summary directly.

Discipline scoring rules (interpretable and documented)
-------------------------------------------------------
The discipline score starts from 100 and subtracts penalties per component.
Each component yields a 0-100 score; the total is the weighted average of
the *available* components (weights renormalized over present components):

- ``manual_intervention`` (weight 0.20): trades whose reason starts with
  ``manual_``. Penalty = ratio of manual trades × 60, capped at 30.
- ``unplanned_adjustments`` (weight 0.15): trades whose reason is
  ``manual_set_position`` / ``manual_adjust_position`` / ``manual_override``
  (ad-hoc order changes without a system signal). Penalty = count × 8,
  capped at 32.
- ``over_position`` (weight 0.15): buy trades in the month or open
  positions whose ``target_position`` exceeds ``over_position_threshold``
  (default 0.5, i.e. >50% of capital in one symbol). Penalty = events × 10,
  capped at 40.
- ``ignored_s_level_signals`` (weight 0.20): S-grade buy signals in the
  month with no matching trade for the same symbol in the month. Penalty =
  miss rate × 80, capped at 40. Component skipped when no S-grade buy
  signals were recorded.
- ``take_profit_delay`` (weight 0.10): open positions whose
  ``peak_pnl_pct`` already reached ``take_profit_trigger`` (default 0.10)
  but are still held at month end. Penalty = count × 10, capped at 30.
- ``stop_loss_violations`` (weight 0.10): FIFO-matched closed round trips
  whose return is below ``stop_loss_threshold`` (default -0.08), i.e. the
  loss went past the stop line. Penalty = count × 10, capped at 30.
- ``execution_quality`` (weight 0.10): mean realized slippage
  (penalty = bp/20 capped at 25), mean fill ratio (penalty = (1-ratio)×50
  capped at 25) and sim-vs-broker alignment (penalty = (1-alignment)×50 +
  min(max_abs_diff×300, 15), capped at 40). Component skipped when neither
  outcomes nor reconcile data is available.

Grade mapping: >=90 excellent, >= discipline_pass_threshold (default 85)
good, >=75 watch, >=60 needs_improvement, else poor. Per PRD §15 a score
below the pass threshold triggers a 10% position reduction next month
(``position_cut_next_month`` + ``position_cut_ratio``).

Trading statistics
------------------
Open/close counts come from buy/sell trade sides of the month. Round trips
are matched per symbol with FIFO lot matching (buy queue vs sell fills);
win rate, profit factor, average PnL and holding days are derived from the
matched lots. Month-level buy/sell fees are subtracted from gross PnL.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

_AD_HOC_MANUAL_REASONS = {
    "manual_set_position",
    "manual_adjust_position",
    "manual_override",
}

_DISCIPLINE_WEIGHTS: dict[str, float] = {
    "manual_intervention": 0.20,
    "unplanned_adjustments": 0.15,
    "over_position": 0.15,
    "ignored_s_level_signals": 0.20,
    "take_profit_delay": 0.10,
    "stop_loss_violations": 0.10,
    "execution_quality": 0.10,
}


def compute_monthly_review_report(
    *,
    year_month: str,
    trades: Sequence[Mapping[str, object]] | None = None,
    positions: Sequence[Mapping[str, object]] | None = None,
    signals: Sequence[Mapping[str, object]] | None = None,
    outcomes: Sequence[Mapping[str, object]] | None = None,
    reconcile: Mapping[str, object] | None = None,
    min_closed_trades: int = 3,
    over_position_threshold: float = 0.5,
    stop_loss_threshold: float = -0.08,
    take_profit_trigger: float = 0.10,
    discipline_pass_threshold: float = 85.0,
    position_cut_ratio: float = 0.10,
) -> dict[str, object]:
    """Compute the monthly review report.

    ``year_month`` must be a ``YYYY-MM`` label. Trades/signals/outcomes are
    filtered to that month; ``positions`` is treated as the end-of-month
    snapshot (not month filtered).
    """
    month = _normalize_month(str(year_month))
    if not month:
        return _empty_report(
            status="invalid_input",
            month=str(year_month),
            discipline_pass_threshold=discipline_pass_threshold,
            position_cut_ratio=position_cut_ratio,
        )

    month_trades = _records_in_month(list(trades or []), month)
    month_signals = _records_in_month(list(signals or []), month)
    month_outcomes = _records_in_month(list(outcomes or []), month)
    month_positions = list(positions or [])

    stats = _compute_trading_stats(month_trades, month=month)
    execution = _compute_execution_quality(
        month_outcomes,
        reconcile=reconcile,
    )
    discipline = compute_discipline_score(
        trades=month_trades,
        positions=month_positions,
        signals=month_signals,
        outcomes=month_outcomes,
        reconcile=reconcile,
        over_position_threshold=over_position_threshold,
        stop_loss_threshold=stop_loss_threshold,
        take_profit_trigger=take_profit_trigger,
        discipline_pass_threshold=discipline_pass_threshold,
        position_cut_ratio=position_cut_ratio,
    )

    if not month_trades and not month_positions and not month_outcomes:
        return _empty_report(
            status="empty",
            month=month,
            discipline_pass_threshold=discipline_pass_threshold,
            position_cut_ratio=position_cut_ratio,
        )

    verdict = _build_verdict(discipline)
    recommendations = _build_recommendations(
        stats=stats,
        execution=execution,
        discipline=discipline,
        verdict=verdict,
        min_closed_trades=min_closed_trades,
        discipline_pass_threshold=discipline_pass_threshold,
        position_cut_ratio=position_cut_ratio,
    )

    return {
        "status": "ok",
        "engine": "monthly_review_report",
        "month": month,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "trades": len(month_trades),
            "positions": len(month_positions),
            "signals": len(month_signals),
            "outcomes": len(month_outcomes),
            "skipped_trades": len(list(trades or [])) - len(month_trades),
        },
        "trading_stats": stats,
        "execution_quality": execution,
        "discipline": discipline,
        "verdict": verdict,
        "recommendations": recommendations,
    }


def compute_discipline_score(
    *,
    trades: Sequence[Mapping[str, object]] | None = None,
    positions: Sequence[Mapping[str, object]] | None = None,
    signals: Sequence[Mapping[str, object]] | None = None,
    outcomes: Sequence[Mapping[str, object]] | None = None,
    reconcile: Mapping[str, object] | None = None,
    over_position_threshold: float = 0.5,
    stop_loss_threshold: float = -0.08,
    take_profit_trigger: float = 0.10,
    discipline_pass_threshold: float = 85.0,
    position_cut_ratio: float = 0.10,
) -> dict[str, object]:
    """Compute the user execution discipline score (see module docstring)."""
    trade_list = list(trades or [])
    position_list = list(positions or [])
    signal_list = list(signals or [])
    outcome_list = list(outcomes or [])

    manual_trades = [
        item
        for item in trade_list
        if str(item.get("reason", "")).strip().lower().startswith("manual_")
    ]
    manual_ratio = len(manual_trades) / len(trade_list) if trade_list else 0.0
    manual_score = 100.0 - min(manual_ratio * 60.0, 30.0)

    unplanned = [
        item
        for item in trade_list
        if str(item.get("reason", "")).strip().lower() in _AD_HOC_MANUAL_REASONS
    ]
    unplanned_score = 100.0 - min(len(unplanned) * 8.0, 32.0)

    over_position_events = _count_over_position_events(
        trade_list,
        position_list,
        threshold=over_position_threshold,
    )
    over_position_score = 100.0 - min(over_position_events * 10.0, 40.0)

    s_level = [
        item
        for item in signal_list
        if str(item.get("grade", "")).strip().upper() == "S"
        and str(item.get("action", "")).strip().lower() == "buy"
    ]
    traded_symbols = {
        str(item.get("symbol", "")).strip()
        for item in trade_list
        if str(item.get("symbol", "")).strip()
    }
    ignored_signals = [
        item
        for item in s_level
        if str(item.get("symbol", "")).strip() not in traded_symbols
    ]
    if s_level:
        miss_rate = len(ignored_signals) / len(s_level)
        signal_score = 100.0 - min(miss_rate * 80.0, 40.0)
    else:
        miss_rate = 0.0
        signal_score = None

    delayed = [
        item
        for item in position_list
        if _as_float(item.get("peak_pnl_pct"), default=0.0) >= float(take_profit_trigger)
        and str(item.get("status", "")).strip() not in {"closed", "exit_due"}
    ]
    take_profit_delay_score = 100.0 - min(len(delayed) * 10.0, 30.0)

    stop_loss_violations = _count_stop_loss_violations(
        trade_list,
        stop_loss_threshold=stop_loss_threshold,
    )
    stop_loss_score = 100.0 - min(stop_loss_violations * 10.0, 30.0)

    execution = _compute_execution_quality(outcome_list, reconcile=reconcile)
    execution_score = execution.get("score")
    if not isinstance(execution_score, (int, float)):
        execution_score = None

    components: dict[str, dict[str, object]] = {
        "manual_intervention": {
            "count": len(manual_trades),
            "total_trades": len(trade_list),
            "ratio": round(manual_ratio, 4),
            "score": round(manual_score, 2),
        },
        "unplanned_adjustments": {
            "count": len(unplanned),
            "reasons": sorted({str(item.get("reason", "")).strip() for item in unplanned}),
            "score": round(unplanned_score, 2),
        },
        "over_position": {
            "count": over_position_events,
            "threshold": over_position_threshold,
            "score": round(over_position_score, 2),
        },
        "ignored_s_level_signals": {
            "count": len(ignored_signals),
            "total": len(s_level),
            "miss_rate": round(miss_rate, 4),
        },
        "take_profit_delay": {
            "count": len(delayed),
            "trigger": take_profit_trigger,
            "score": round(take_profit_delay_score, 2),
        },
        "stop_loss_violations": {
            "count": stop_loss_violations,
            "threshold": stop_loss_threshold,
            "score": round(stop_loss_score, 2),
        },
        "execution_quality": {
            "score": round(execution_score, 2) if execution_score is not None else None,
            "slippage_score": execution.get("slippage_score"),
            "fill_score": execution.get("fill_score"),
            "reconcile_score": execution.get("reconcile_score"),
        },
    }
    components["ignored_s_level_signals"]["score"] = (
        round(signal_score, 2) if signal_score is not None else None
    )

    score, used_weights = _weighted_discipline_score(components)
    verdict = _build_verdict(
        {
            "total_score": score,
            "grade": _discipline_grade(
                score,
                discipline_pass_threshold=discipline_pass_threshold,
            ),
            "passed": score >= float(discipline_pass_threshold),
            "position_cut_next_month": score < float(discipline_pass_threshold),
            "position_cut_ratio": (
                float(position_cut_ratio)
                if score < float(discipline_pass_threshold)
                else 0.0
            ),
        }
    )

    return {
        "total_score": round(score, 2),
        "grade": verdict["grade"],
        "passed": verdict["passed"],
        "position_cut_next_month": verdict["position_cut_next_month"],
        "position_cut_ratio": verdict["position_cut_ratio"],
        "weights": {key: _DISCIPLINE_WEIGHTS[key] for key in sorted(used_weights)},
        "available_components": sorted(used_weights),
        "components": components,
    }


def persist_monthly_review_report(
    *,
    report: Mapping[str, object],
    output_path: str | Path,
) -> str:
    """Write the monthly review report as UTF-8 JSON and return the path."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)


def _compute_trading_stats(
    trades: list[Mapping[str, object]],
    *,
    month: str,
) -> dict[str, object]:
    open_trades = [
        item for item in trades if str(item.get("side", "")).strip().lower() == "buy"
    ]
    close_trades = [
        item for item in trades if str(item.get("side", "")).strip().lower() == "sell"
    ]

    matched = _fifo_match_round_trips(trades)
    wins = [item for item in matched if _as_float(item.get("pnl"), default=0.0) > 0.0]
    losses = [item for item in matched if _as_float(item.get("pnl"), default=0.0) <= 0.0]
    gross_pnl = sum(_as_float(item.get("pnl"), default=0.0) for item in matched)
    total_fees = _total_fees(trades)
    net_pnl = gross_pnl - total_fees

    holding_days = [_as_int(item.get("holding_days"), default=0) for item in matched]
    profit_sum = sum(
        max(0.0, _as_float(item.get("pnl"), default=0.0)) for item in matched
    )
    loss_abs_sum = sum(
        abs(min(0.0, _as_float(item.get("pnl"), default=0.0))) for item in matched
    )

    return {
        "month": month,
        "open_trades": len(open_trades),
        "close_trades": len(close_trades),
        "total_trades": len(trades),
        "round_trips": len(matched),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(matched), 4) if matched else 0.0,
        "total_pnl": round(net_pnl, 4),
        "gross_pnl": round(gross_pnl, 4),
        "total_fees": round(total_fees, 4),
        "avg_pnl": round(net_pnl / len(matched), 4) if matched else 0.0,
        "profit_factor": (
            round(profit_sum / loss_abs_sum, 4) if loss_abs_sum > 0.0 else None
        ),
        "avg_holding_days": (
            round(sum(holding_days) / len(holding_days), 2) if holding_days else 0.0
        ),
        "max_holding_days": max(holding_days) if holding_days else 0,
        "symbols_traded": len(
            {
                str(item.get("symbol", "")).strip()
                for item in trades
                if str(item.get("symbol", "")).strip()
            }
        ),
    }


def _fifo_match_round_trips(
    trades: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Match sell fills against buy lots per symbol (FIFO) and derive PnL."""
    queues: dict[str, deque[tuple[float, float, datetime]]] = defaultdict(deque)
    matched: list[dict[str, object]] = []
    dated: list[tuple[datetime, Mapping[str, object]]] = []
    for item in trades:
        record_ts = _record_timestamp(item)
        if record_ts is not None:
            dated.append((record_ts, item))
    dated.sort(key=lambda pair: pair[0])
    ordered = [item for _, item in dated]
    for trade in ordered:
        symbol = str(trade.get("symbol", "")).strip()
        side = str(trade.get("side", "")).strip().lower()
        timestamp = _record_timestamp(trade)
        if not symbol or timestamp is None:
            continue
        if side == "buy":
            entry_price = _as_float(trade.get("entry_price"), default=0.0)
            quantity = _as_float(trade.get("quantity"), default=0.0)
            if entry_price > 0.0 and quantity > 0.0:
                queues[symbol].append((entry_price, quantity, timestamp))
        elif side == "sell":
            exit_price = _as_float(trade.get("exit_price"), default=0.0)
            exit_quantity = _as_float(trade.get("exit_quantity"), default=0.0)
            if exit_price <= 0.0 or exit_quantity <= 0.0:
                continue
            remaining = exit_quantity
            pnl = 0.0
            matched_entry_price: float | None = None
            first_buy_ts: datetime | None = None
            while remaining > 1e-9 and queues[symbol]:
                lot_entry, lot_quantity, lot_ts = queues[symbol][0]
                matched_quantity = min(lot_quantity, remaining)
                pnl += (exit_price - lot_entry) * matched_quantity
                if matched_entry_price is None:
                    matched_entry_price = lot_entry
                if first_buy_ts is None:
                    first_buy_ts = lot_ts
                remaining -= matched_quantity
                if lot_quantity - matched_quantity <= 1e-9:
                    queues[symbol].popleft()
                else:
                    queues[symbol][0] = (lot_entry, lot_quantity - matched_quantity, lot_ts)
            if first_buy_ts is not None and (exit_quantity - remaining) > 1e-9:
                holding_days = max(0, (timestamp.date() - first_buy_ts.date()).days)
                matched.append(
                    {
                        "symbol": symbol,
                        "pnl": pnl,
                        "entry_price": (
                            matched_entry_price if matched_entry_price is not None else 0.0
                        ),
                        "exit_price": exit_price,
                        "exit_quantity": round(exit_quantity - remaining, 6),
                        "holding_days": holding_days,
                    }
                )
    return matched


def _total_fees(trades: list[Mapping[str, object]]) -> float:
    total = 0.0
    for trade in trades:
        side = str(trade.get("side", "")).strip().lower()
        if side == "buy":
            total += _as_float(trade.get("fee"), default=0.0)
        elif side == "sell":
            total += _as_float(trade.get("exit_fee"), default=0.0)
    return max(0.0, total)


def _compute_execution_quality(
    outcomes: list[Mapping[str, object]],
    *,
    reconcile: Mapping[str, object] | None,
) -> dict[str, object]:
    slippage_values: list[float] = []
    for item in outcomes:
        value = _optional_float(item.get("realized_slippage_bp"))
        if value is not None:
            slippage_values.append(value)
    fill_values: list[float] = []
    for item in outcomes:
        value = _optional_float(item.get("execution_fill_ratio"))
        if value is not None:
            fill_values.append(value)

    reconcile_map = _reconcile_map(reconcile)
    alignment_rate = _optional_float(reconcile_map.get("alignment_rate"))
    max_abs_diff = _as_float(reconcile_map.get("max_abs_diff"), default=0.0)
    mismatch_records = _as_int(reconcile_map.get("mismatch_records"), default=0)

    slippage_mean = (
        sum(slippage_values) / len(slippage_values) if slippage_values else None
    )
    fill_mean = sum(fill_values) / len(fill_values) if fill_values else None

    score_parts: list[tuple[str, float]] = []
    slippage_score: float | None = None
    if slippage_mean is not None:
        slippage_score = max(0.0, 100.0 - min(abs(float(slippage_mean)) / 20.0, 25.0))
        score_parts.append(("slippage", slippage_score))
    fill_score: float | None = None
    if fill_mean is not None:
        fill_score = max(0.0, 100.0 - min((1.0 - float(fill_mean)) * 50.0, 25.0))
        score_parts.append(("fill", fill_score))
    reconcile_score: float | None = None
    if alignment_rate is not None:
        reconcile_score = max(
            0.0,
            100.0
            - min(
                (1.0 - float(alignment_rate)) * 50.0
                + min(float(max_abs_diff) * 300.0, 15.0),
                40.0,
            ),
        )
        score_parts.append(("reconcile", reconcile_score))

    execution_score = (
        sum(part_score for _, part_score in score_parts) / len(score_parts)
        if score_parts
        else None
    )

    return {
        "score": round(execution_score, 2) if execution_score is not None else None,
        "slippage_score": round(slippage_score, 2) if slippage_score is not None else None,
        "fill_score": round(fill_score, 2) if fill_score is not None else None,
        "reconcile_score": (
            round(reconcile_score, 2) if reconcile_score is not None else None
        ),
        "slippage": {
            "samples": len(slippage_values),
            "mean_bp": round(float(slippage_mean), 4) if slippage_mean is not None else None,
            "max_bp": round(max(slippage_values), 4) if slippage_values else None,
        },
        "fill_ratio": {
            "samples": len(fill_values),
            "mean": round(float(fill_mean), 4) if fill_mean is not None else None,
            "min": round(min(fill_values), 4) if fill_values else None,
        },
        "reconcile": {
            "alignment_rate": (
                round(float(alignment_rate), 4) if alignment_rate is not None else None
            ),
            "mismatch_records": mismatch_records,
            "max_abs_diff": round(float(max_abs_diff), 6),
        },
    }


def _reconcile_map(reconcile: Mapping[str, object] | None) -> dict[str, object]:
    if reconcile is None:
        return {}
    sim_vs_broker = reconcile.get("sim_vs_broker")
    if isinstance(sim_vs_broker, Mapping):
        merged = dict(reconcile)
        merged.update(sim_vs_broker)
        return merged
    return dict(reconcile)


def _count_over_position_events(
    trades: list[Mapping[str, object]],
    positions: list[Mapping[str, object]],
    *,
    threshold: float,
) -> int:
    threshold_value = max(0.0, float(threshold))
    events = 0
    for trade in trades:
        if str(trade.get("side", "")).strip().lower() != "buy":
            continue
        if _as_float(trade.get("target_position"), default=0.0) > threshold_value:
            events += 1
    for position in positions:
        if _as_float(position.get("target_position"), default=0.0) > threshold_value:
            events += 1
    return events


def _count_stop_loss_violations(
    trades: list[Mapping[str, object]],
    *,
    stop_loss_threshold: float,
) -> int:
    threshold_value = min(0.0, float(stop_loss_threshold))
    violations = 0
    for item in _fifo_match_round_trips(trades):
        entry_price = _as_float(item.get("entry_price"), default=0.0)
        exit_price = _as_float(item.get("exit_price"), default=0.0)
        if entry_price <= 0.0 or exit_price <= 0.0:
            continue
        if (exit_price / entry_price - 1.0) < threshold_value:
            violations += 1
    return violations


def _weighted_discipline_score(
    components: Mapping[str, Mapping[str, object]],
) -> tuple[float, list[str]]:
    weighted_sum = 0.0
    total_weight = 0.0
    used: list[str] = []
    for name, weight in _DISCIPLINE_WEIGHTS.items():
        score = components[name].get("score")
        if not isinstance(score, (int, float)):
            continue
        weighted_sum += float(score) * weight
        total_weight += weight
        used.append(name)
    if total_weight <= 0.0:
        return 0.0, []
    return weighted_sum / total_weight, used


def _build_verdict(discipline: Mapping[str, object]) -> dict[str, object]:
    score = _as_float(discipline.get("total_score"), default=0.0)
    grade = str(discipline.get("grade", "")).strip() or "insufficient_data"
    passed = bool(discipline.get("passed", score >= 85.0))
    return {
        "score": round(score, 2),
        "grade": grade,
        "passed": passed,
        "position_cut_next_month": bool(discipline.get("position_cut_next_month", False)),
        "position_cut_ratio": round(
            _as_float(discipline.get("position_cut_ratio"), default=0.0),
            4,
        ),
    }


def _discipline_grade(score: float, *, discipline_pass_threshold: float) -> str:
    if score >= 90.0:
        return "excellent"
    if score >= float(discipline_pass_threshold):
        return "good"
    if score >= 75.0:
        return "watch"
    if score >= 60.0:
        return "needs_improvement"
    return "poor"


def _build_recommendations(
    *,
    stats: Mapping[str, object],
    execution: Mapping[str, object],
    discipline: Mapping[str, object],
    verdict: Mapping[str, object],
    min_closed_trades: int,
    discipline_pass_threshold: float,
    position_cut_ratio: float,
) -> list[str]:
    recommendations: list[str] = []
    components = discipline.get("components", {})
    if not isinstance(components, Mapping):
        components = {}

    manual = components.get("manual_intervention", {})
    if isinstance(manual, Mapping) and _as_float(manual.get("ratio"), default=0.0) >= 0.3:
        recommendations.append("手动干预占比偏高，建议减少人工改单、回归信号执行")

    if _as_int(components.get("unplanned_adjustments", {}).get("count"), default=0) > 0:
        recommendations.append("存在随意改单记录，建议核对改单理由并保持操作留痕")

    if _as_int(components.get("over_position", {}).get("count"), default=0) > 0:
        recommendations.append("出现超仓/单票仓位过高操作，建议遵守单票仓位上限")

    if _as_int(
        components.get("ignored_s_level_signals", {}).get("count"),
        default=0,
    ) > 0:
        recommendations.append("存在忽略的 S 级买入信号，建议复盘未执行原因")

    if _as_int(components.get("take_profit_delay", {}).get("count"), default=0) > 0:
        recommendations.append("部分持仓已达止盈触发线仍未了结，注意止盈纪律")

    if _as_int(components.get("stop_loss_violations", {}).get("count"), default=0) > 0:
        recommendations.append("存在止损线被击穿后仍持有的情形，需严格执行止损")

    execution_score = discipline.get("components", {})
    exec_score = None
    if isinstance(execution_score, Mapping):
        exec_quality = execution_score.get("execution_quality")
        if isinstance(exec_quality, Mapping):
            exec_score = exec_quality.get("score")
    if isinstance(exec_score, (int, float)) and float(exec_score) < 80.0:
        recommendations.append("执行质量评分偏低，关注滑点、成交率与对账差异")

    closed_trades = _as_int(stats.get("close_trades"), default=0)
    if closed_trades < max(1, int(min_closed_trades)):
        recommendations.append(
            f"本月平仓样本不足（{closed_trades} 笔 < {int(min_closed_trades)} 笔），"
            "胜率与盈亏比结论参考性有限"
        )

    if not bool(verdict.get("passed", True)):
        recommendations.append(
            f"纪律评分低于 {float(discipline_pass_threshold):.0f} 分，"
            f"次月建议降仓 {float(position_cut_ratio) * 100:.0f}%"
        )
    if not recommendations:
        recommendations.append("本月执行纪律良好，保持当前节奏")
    return recommendations


def _records_in_month(
    records: list[Mapping[str, object]],
    month: str,
) -> list[Mapping[str, object]]:
    filtered: list[Mapping[str, object]] = []
    for record in records:
        timestamp = _record_timestamp(record)
        if timestamp is None:
            continue
        if timestamp.strftime("%Y-%m") == month:
            filtered.append(record)
    return filtered


def _record_timestamp(record: Mapping[str, object]) -> datetime | None:
    for key in (
        "timestamp",
        "decision_time",
        "date",
        "opened_at",
        "outcome_updated_at",
        "label_mature_time",
    ):
        raw = record.get(key)
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.fromisoformat(raw.strip())
            except ValueError:
                continue
    return None


def _normalize_month(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    for fmt in ("%Y-%m", "%Y-%m-%d", "%Y%m"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return ""


def _empty_report(
    *,
    status: str,
    month: str,
    discipline_pass_threshold: float,
    position_cut_ratio: float,
) -> dict[str, object]:
    return {
        "status": status,
        "engine": "monthly_review_report",
        "month": month,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "trades": 0,
            "positions": 0,
            "signals": 0,
            "outcomes": 0,
            "skipped_trades": 0,
        },
        "trading_stats": {
            "month": month,
            "open_trades": 0,
            "close_trades": 0,
            "total_trades": 0,
            "round_trips": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "gross_pnl": 0.0,
            "total_fees": 0.0,
            "avg_pnl": 0.0,
            "profit_factor": None,
            "avg_holding_days": 0.0,
            "max_holding_days": 0,
            "symbols_traded": 0,
        },
        "execution_quality": {
            "score": None,
            "slippage_score": None,
            "fill_score": None,
            "reconcile_score": None,
            "slippage": {
                "samples": 0,
                "mean_bp": None,
                "max_bp": None,
            },
            "fill_ratio": {
                "samples": 0,
                "mean": None,
                "min": None,
            },
            "reconcile": {
                "alignment_rate": None,
                "mismatch_records": 0,
                "max_abs_diff": 0.0,
            },
        },
        "discipline": {
            "total_score": 0.0,
            "grade": "insufficient_data",
            "passed": True,
            "position_cut_next_month": False,
            "position_cut_ratio": 0.0,
            "weights": {},
            "available_components": [],
            "components": {},
        },
        "verdict": {
            "score": 0.0,
            "grade": "insufficient_data",
            "passed": True,
            "position_cut_next_month": False,
            "position_cut_ratio": 0.0,
        },
        "recommendations": [],
    }


def _as_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return math.nan
    return default


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default
