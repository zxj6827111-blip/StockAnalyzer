"""Structured daily review report (PRD §15 review closed loop, day granularity).

Companion of ``monthly_review_report.py``: same field family, but filtered to
a single trading day so users/frontend get a per-day structured review.

Inputs
------
- ``trades``: portfolio trade records (``portfolio/book.py`` ``to_dict``
  shape) with ``side``, ``symbol``, ``timestamp``, ``reason``,
  ``entry_price``/``quantity`` (buy) and ``exit_price``/``exit_quantity``
  (sell), ``fee``/``exit_fee``.
- ``positions``: end-of-day position snapshot (``positions()`` shape) with
  ``symbol``, ``target_position``, ``opened_at``, ``status``,
  ``peak_pnl_pct``.
- ``signals``: S-level signal records with ``symbol``, ``grade``, ``action``
  and a date key (``timestamp``/``decision_time``/``date``).
- ``outcomes``: execution outcome records (``OutcomeRecord`` JSON shape)
  with ``realized_slippage_bp``, ``execution_fill_ratio`` and a date key.
- ``reconcile``: sim-vs-broker weekly report or its ``sim_vs_broker``
  summary directly (used for the execution-quality alignment component).

Output sections
---------------
- ``trading_stats``: day open/close counts, FIFO round trips, win rate,
  PnL/fees, profit factor, holding days (reuses the monthly stats helper).
- ``signals``: day signal counts by grade plus S-level buy candidates.
- ``position_changes``: end-of-day holdings, positions opened that day,
  day buy/sell order counts and take-profit-due holdings.
- ``execution_quality``: slippage/fill/reconcile scores (day outcomes).
- ``discipline``: discipline score block (reuses ``compute_discipline_score``
  on day-filtered records) plus ``discipline_hints``: day-flavored Chinese
  hints. A day's trade sample is small, so the score is informational only;
  monthly review remains the decision granularity (position-cut etc.).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from stock_analyzer.research.monthly_review_report import (
    _as_float,
    _as_int,
    _compute_execution_quality,
    _compute_trading_stats,
    _record_timestamp,
    compute_discipline_score,
)

_CLOSED_POSITION_STATUSES = {"closed", "exit_due"}


def compute_daily_review_report(
    *,
    date: str,
    trades: Sequence[Mapping[str, object]] | None = None,
    positions: Sequence[Mapping[str, object]] | None = None,
    signals: Sequence[Mapping[str, object]] | None = None,
    outcomes: Sequence[Mapping[str, object]] | None = None,
    reconcile: Mapping[str, object] | None = None,
    over_position_threshold: float = 0.5,
    stop_loss_threshold: float = -0.08,
    take_profit_trigger: float = 0.10,
    discipline_pass_threshold: float = 85.0,
) -> dict[str, object]:
    """Compute the daily review report.

    ``date`` must be a ``YYYY-MM-DD`` label. Trades/signals/outcomes are
    filtered to that day; ``positions`` is treated as the end-of-day
    snapshot (not day filtered).
    """
    day = _normalize_day(str(date))
    if not day:
        return _empty_report(status="invalid_input", date=str(date))

    day_trades = _records_on_day(list(trades or []), day)
    day_signals = _records_on_day(list(signals or []), day)
    day_outcomes = _records_on_day(list(outcomes or []), day)
    day_positions = list(positions or [])

    stats = _compute_trading_stats(day_trades, month=day, label_key="date")
    execution = _compute_execution_quality(day_outcomes, reconcile=reconcile)
    discipline = compute_discipline_score(
        trades=day_trades,
        positions=day_positions,
        signals=day_signals,
        outcomes=day_outcomes,
        reconcile=reconcile,
        over_position_threshold=over_position_threshold,
        stop_loss_threshold=stop_loss_threshold,
        take_profit_trigger=take_profit_trigger,
        discipline_pass_threshold=discipline_pass_threshold,
    )

    if not day_trades and not day_positions and not day_outcomes:
        return _empty_report(status="empty", date=day)

    return {
        "status": "ok",
        "engine": "daily_review_report",
        "date": day,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "trades": len(day_trades),
            "positions": len(day_positions),
            "signals": len(day_signals),
            "outcomes": len(day_outcomes),
            "skipped_trades": len(list(trades or [])) - len(day_trades),
        },
        "trading_stats": stats,
        "signals": _signal_summary(day_signals),
        "position_changes": _position_changes(
            day_positions,
            day_trades,
            day=day,
            take_profit_trigger=take_profit_trigger,
        ),
        "execution_quality": execution,
        "discipline": discipline,
        "discipline_hints": _build_daily_hints(stats=stats, discipline=discipline),
    }


def persist_daily_review_report(
    *,
    report: Mapping[str, object],
    output_path: str | Path,
) -> str:
    """Write the daily review report as UTF-8 JSON and return the path."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)


def _records_on_day(
    records: list[Mapping[str, object]],
    day: str,
) -> list[Mapping[str, object]]:
    filtered: list[Mapping[str, object]] = []
    for record in records:
        timestamp = _record_timestamp(record)
        if timestamp is None:
            continue
        if timestamp.strftime("%Y-%m-%d") == day:
            filtered.append(record)
    return filtered


def _signal_summary(
    signals: list[Mapping[str, object]],
) -> dict[str, object]:
    by_grade: Counter[str] = Counter()
    s_level_buys: list[dict[str, object]] = []
    for item in signals:
        grade = str(item.get("grade", "")).strip().upper()
        action = str(item.get("action", "")).strip().lower()
        if grade:
            by_grade[grade] += 1
        if grade == "S" and action == "buy":
            timestamp = _record_timestamp(item)
            s_level_buys.append(
                {
                    "symbol": str(item.get("symbol", "")).strip(),
                    "time": (
                        timestamp.isoformat(timespec="seconds")
                        if timestamp is not None
                        else ""
                    ),
                }
            )
    s_level_buys.sort(key=lambda entry: str(entry.get("time", "")))
    return {
        "total": len(signals),
        "by_grade": dict(sorted(by_grade.items())),
        "s_level_buys": s_level_buys,
        "s_level_buys_count": len(s_level_buys),
    }


def _position_changes(
    positions: list[Mapping[str, object]],
    trades: list[Mapping[str, object]],
    *,
    day: str,
    take_profit_trigger: float,
) -> dict[str, object]:
    opened_today: list[dict[str, object]] = []
    take_profit_due: list[dict[str, object]] = []
    for position in positions:
        opened_at = _record_timestamp(position)
        if opened_at is not None and opened_at.strftime("%Y-%m-%d") == day:
            opened_today.append(
                {
                    "symbol": str(position.get("symbol", "")).strip(),
                    "time": opened_at.isoformat(timespec="seconds"),
                }
            )
        if (
            _as_float(position.get("peak_pnl_pct"), default=0.0) >= float(take_profit_trigger)
            and str(position.get("status", "")).strip() not in _CLOSED_POSITION_STATUSES
        ):
            take_profit_due.append(
                {
                    "symbol": str(position.get("symbol", "")).strip(),
                    "peak_pnl_pct": round(
                        _as_float(position.get("peak_pnl_pct"), default=0.0),
                        4,
                    ),
                }
            )
    opened_today.sort(key=lambda entry: str(entry.get("time", "")))
    return {
        "open_positions": len(positions),
        "opened_today": opened_today,
        "buy_orders": sum(
            1
            for trade in trades
            if str(trade.get("side", "")).strip().lower() == "buy"
        ),
        "sell_orders": sum(
            1
            for trade in trades
            if str(trade.get("side", "")).strip().lower() == "sell"
        ),
        "take_profit_due": take_profit_due,
        "take_profit_trigger": take_profit_trigger,
    }


def _build_daily_hints(
    *,
    stats: Mapping[str, object],
    discipline: Mapping[str, object],
) -> list[str]:
    hints: list[str] = []
    components = discipline.get("components", {})
    if not isinstance(components, Mapping):
        components = {}

    manual = components.get("manual_intervention", {})
    if isinstance(manual, Mapping):
        manual_count = _as_int(manual.get("count"), default=0)
        if manual_count > 0:
            hints.append(
                f"当日存在 {manual_count} 笔手动干预交易"
                f"（占比 {_as_float(manual.get('ratio'), default=0.0) * 100:.0f}%），"
                "建议核对改单原因"
            )

    unplanned = _as_int(
        components.get("unplanned_adjustments", {}).get("count"),
        default=0,
    )
    if unplanned > 0:
        hints.append(f"当日存在 {unplanned} 笔随意改单记录，建议保持操作留痕")

    over_position = _as_int(
        components.get("over_position", {}).get("count"),
        default=0,
    )
    if over_position > 0:
        hints.append(f"当日出现 {over_position} 次超仓/单票仓位过高操作，建议遵守单票仓位上限")

    ignored_signals = _as_int(
        components.get("ignored_s_level_signals", {}).get("count"),
        default=0,
    )
    if ignored_signals > 0:
        hints.append(f"当日存在 {ignored_signals} 个未执行的 S 级买入信号，建议复盘未执行原因")

    take_profit_due = _as_int(
        components.get("take_profit_delay", {}).get("count"),
        default=0,
    )
    if take_profit_due > 0:
        hints.append(f"当日仍有 {take_profit_due} 个持仓达到止盈触发线未了结，注意止盈纪律")

    stop_loss_violations = _as_int(
        components.get("stop_loss_violations", {}).get("count"),
        default=0,
    )
    if stop_loss_violations > 0:
        hints.append(f"当日存在 {stop_loss_violations} 笔平仓击穿止损线的情形，需严格执行止损")

    execution = components.get("execution_quality", {})
    exec_score = execution.get("score") if isinstance(execution, Mapping) else None
    if isinstance(exec_score, (int, float)) and float(exec_score) < 80.0:
        hints.append("当日执行质量评分偏低，关注滑点、成交率与对账差异")

    total_trades = _as_int(stats.get("total_trades"), default=0)
    if total_trades < 3:
        hints.append(
            f"当日成交样本较少（{total_trades} 笔），纪律评分仅作提示参考，"
            "以月度复盘为决策粒度"
        )
    if not hints:
        hints.append("当日执行纪律正常，保持当前节奏")
    return hints


def _normalize_day(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19]).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _empty_report(*, status: str, date: str) -> dict[str, object]:
    return {
        "status": status,
        "engine": "daily_review_report",
        "date": date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "trades": 0,
            "positions": 0,
            "signals": 0,
            "outcomes": 0,
            "skipped_trades": 0,
        },
        "trading_stats": {
            "date": date,
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
        "signals": {
            "total": 0,
            "by_grade": {},
            "s_level_buys": [],
            "s_level_buys_count": 0,
        },
        "position_changes": {
            "open_positions": 0,
            "opened_today": [],
            "buy_orders": 0,
            "sell_orders": 0,
            "take_profit_due": [],
            "take_profit_trigger": 0.1,
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
        "discipline_hints": [],
    }
