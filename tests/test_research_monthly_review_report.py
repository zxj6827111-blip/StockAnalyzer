from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import MonthlyReviewConfig, load_config
from stock_analyzer.research.monthly_review_report import (
    compute_discipline_score,
    compute_monthly_review_report,
    persist_monthly_review_report,
)
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"Expected mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [_as_mapping(item) for item in value if isinstance(item, Mapping)]


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _trade(
    side: str,
    symbol: str,
    timestamp: str,
    *,
    reason: str = "signal_trade",
    entry_price: float | None = None,
    quantity: float | None = None,
    exit_price: float | None = None,
    exit_quantity: float | None = None,
    fee: float = 0.0,
    exit_fee: float = 0.0,
    target_position: float | None = None,
) -> dict[str, object]:
    trade: dict[str, object] = {
        "side": side,
        "symbol": symbol,
        "timestamp": timestamp,
        "reason": reason,
        "fee": fee,
        "exit_fee": exit_fee,
    }
    if entry_price is not None:
        trade["entry_price"] = entry_price
    if quantity is not None:
        trade["quantity"] = quantity
    if exit_price is not None:
        trade["exit_price"] = exit_price
    if exit_quantity is not None:
        trade["exit_quantity"] = exit_quantity
    if target_position is not None:
        trade["target_position"] = target_position
    return trade


def _position(
    symbol: str,
    *,
    target_position: float = 0.2,
    status: str = "open",
    peak_pnl_pct: float = 0.0,
    opened_at: str = "2026-03-02T09:30:00",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "strategy": "trend",
        "target_position": target_position,
        "status": status,
        "peak_pnl_pct": peak_pnl_pct,
        "opened_at": opened_at,
    }


def _sample_trades() -> list[dict[str, object]]:
    return [
        _trade(
            "buy",
            "600000",
            "2026-03-02T09:31:00",
            entry_price=10.0,
            quantity=100.0,
            fee=5.0,
            target_position=0.3,
        ),
        _trade(
            "sell",
            "600000",
            "2026-03-10T14:30:00",
            exit_price=12.0,
            exit_quantity=100.0,
            exit_fee=5.0,
        ),
        _trade(
            "buy",
            "600001",
            "2026-03-03T09:31:00",
            entry_price=20.0,
            quantity=200.0,
            fee=10.0,
            target_position=0.3,
        ),
        _trade(
            "sell",
            "600001",
            "2026-03-12T14:30:00",
            exit_price=18.0,
            exit_quantity=200.0,
            exit_fee=10.0,
        ),
    ]


def test_monthly_review_report_trading_stats() -> None:
    report = compute_monthly_review_report(
        year_month="2026-03",
        trades=_sample_trades(),
    )
    assert report["status"] == "ok"
    assert report["month"] == "2026-03"
    stats = _as_mapping(report["trading_stats"])
    assert _as_int(stats["open_trades"]) == 2
    assert _as_int(stats["close_trades"]) == 2
    assert _as_int(stats["round_trips"]) == 2
    assert _as_int(stats["wins"]) == 1
    assert _as_int(stats["losses"]) == 1
    assert _as_float(stats["win_rate"]) == pytest.approx(0.5)
    assert _as_float(stats["gross_pnl"]) == pytest.approx(-200.0)
    assert _as_float(stats["total_fees"]) == pytest.approx(30.0)
    assert _as_float(stats["total_pnl"]) == pytest.approx(-230.0)
    assert _as_float(stats["profit_factor"]) == pytest.approx(0.5)
    assert _as_float(stats["avg_holding_days"]) == pytest.approx(8.5)
    assert _as_int(stats["symbols_traded"]) == 2


def test_monthly_review_report_filters_records_to_month() -> None:
    trades = _sample_trades() + [
        _trade(
            "buy",
            "600002",
            "2026-04-02T09:31:00",
            entry_price=5.0,
            quantity=100.0,
        )
    ]
    report = compute_monthly_review_report(year_month="2026-03", trades=trades)
    inputs = _as_mapping(report["inputs"])
    assert _as_int(inputs["trades"]) == 4
    assert _as_int(inputs["skipped_trades"]) == 1
    stats = _as_mapping(report["trading_stats"])
    assert _as_int(stats["open_trades"]) == 2
    assert _as_int(stats["symbols_traded"]) == 2


def test_monthly_review_report_discipline_components_present() -> None:
    report = compute_monthly_review_report(
        year_month="2026-03",
        trades=_sample_trades(),
    )
    discipline = _as_mapping(report["discipline"])
    components = _as_mapping(discipline["components"])
    for name in (
        "manual_intervention",
        "unplanned_adjustments",
        "over_position",
        "ignored_s_level_signals",
        "take_profit_delay",
        "stop_loss_violations",
        "execution_quality",
    ):
        assert name in components, name
    assert _as_float(discipline["total_score"]) == pytest.approx(98.57, abs=0.02)
    assert discipline["grade"] == "excellent"
    assert discipline["passed"] is True
    assert discipline["position_cut_next_month"] is False


def test_discipline_score_manual_and_unplanned_penalties() -> None:
    trades = [
        _trade(
            "buy",
            "600000",
            "2026-03-02T09:31:00",
            reason="manual_set_position",
            entry_price=10.0,
            quantity=100.0,
            target_position=0.3,
        )
    ]
    score = compute_discipline_score(trades=trades)
    components = _as_mapping(score["components"])
    manual = _as_mapping(components["manual_intervention"])
    unplanned = _as_mapping(components["unplanned_adjustments"])
    assert _as_int(manual["count"]) == 1
    assert _as_float(manual["ratio"]) == pytest.approx(1.0)
    assert _as_float(manual["score"]) == pytest.approx(70.0)
    assert _as_int(unplanned["count"]) == 1
    assert _as_float(unplanned["score"]) == pytest.approx(92.0)
    assert score["grade"] in {"excellent", "good", "watch", "needs_improvement", "poor"}


def test_discipline_score_over_position_events() -> None:
    trades = [
        _trade(
            "buy",
            "600000",
            "2026-03-02T09:31:00",
            entry_price=10.0,
            quantity=100.0,
            target_position=0.8,
        )
    ]
    positions = [_position("600001", target_position=0.9)]
    score = compute_discipline_score(trades=trades, positions=positions)
    components = _as_mapping(score["components"])
    over_position = _as_mapping(components["over_position"])
    assert _as_int(over_position["count"]) == 2
    assert _as_float(over_position["score"]) == pytest.approx(80.0)


def test_discipline_score_ignored_s_level_signals() -> None:
    trades = [
        _trade(
            "buy",
            "600001",
            "2026-03-05T09:31:00",
            entry_price=10.0,
            quantity=100.0,
        )
    ]
    signals = [
        {
            "symbol": "600001",
            "grade": "S",
            "action": "buy",
            "timestamp": "2026-03-05T09:00:00",
        },
        {
            "symbol": "600002",
            "grade": "S",
            "action": "buy",
            "timestamp": "2026-03-06T09:00:00",
        },
        {
            "symbol": "600003",
            "grade": "A",
            "action": "buy",
            "timestamp": "2026-03-07T09:00:00",
        },
    ]
    score = compute_discipline_score(trades=trades, signals=signals)
    components = _as_mapping(score["components"])
    ignored = _as_mapping(components["ignored_s_level_signals"])
    assert _as_int(ignored["count"]) == 1
    assert _as_int(ignored["total"]) == 2
    assert _as_float(ignored["miss_rate"]) == pytest.approx(0.5)
    assert _as_float(ignored["score"]) == pytest.approx(60.0)
    assert "ignored_s_level_signals" in score["available_components"]


def test_discipline_score_signal_component_skipped_without_signals() -> None:
    score = compute_discipline_score(trades=_sample_trades())
    assert "ignored_s_level_signals" not in score["available_components"]


def test_discipline_score_take_profit_delay_and_stop_loss() -> None:
    positions = [
        _position("600001", target_position=0.2, status="open", peak_pnl_pct=0.15),
        _position("600002", target_position=0.2, status="open", peak_pnl_pct=0.02),
    ]
    trades = [
        _trade(
            "buy",
            "600010",
            "2026-03-02T09:31:00",
            entry_price=10.0,
            quantity=100.0,
        ),
        _trade(
            "sell",
            "600010",
            "2026-03-05T14:30:00",
            exit_price=8.5,
            exit_quantity=100.0,
        ),
    ]
    score = compute_discipline_score(trades=trades, positions=positions)
    components = _as_mapping(score["components"])
    delay = _as_mapping(components["take_profit_delay"])
    assert _as_int(delay["count"]) == 1
    assert _as_float(delay["score"]) == pytest.approx(90.0)
    stop_loss = _as_mapping(components["stop_loss_violations"])
    assert _as_int(stop_loss["count"]) == 1
    assert _as_float(stop_loss["score"]) == pytest.approx(90.0)


def test_discipline_score_execution_quality_from_outcomes_and_reconcile() -> None:
    outcomes = [
        {
            "realized_slippage_bp": 10.0,
            "execution_fill_ratio": 0.95,
            "outcome_updated_at": "2026-03-08T15:30:00",
        }
    ]
    reconcile = {
        "sim_vs_broker": {
            "alignment_rate": 0.95,
            "max_abs_diff": 0.01,
            "mismatch_records": 2,
        },
        "mismatch_records": 2,
    }
    score = compute_discipline_score(
        trades=_sample_trades(),
        outcomes=outcomes,
        reconcile=reconcile,
    )
    components = _as_mapping(score["components"])
    execution = _as_mapping(components["execution_quality"])
    assert _as_float(execution["slippage_score"]) == pytest.approx(99.5, abs=0.01)
    assert _as_float(execution["fill_score"]) == pytest.approx(97.5, abs=0.01)
    assert _as_float(execution["reconcile_score"]) == pytest.approx(94.5, abs=0.01)
    assert _as_float(execution["score"]) == pytest.approx(97.17, abs=0.02)
    assert "execution_quality" in score["available_components"]


def test_discipline_score_verdict_triggers_position_cut_below_threshold() -> None:
    trades = [
        _trade(
            "buy",
            "600000",
            "2026-03-02T09:31:00",
            reason="manual_set_position",
            entry_price=10.0,
            quantity=100.0,
            target_position=0.9,
        ),
        _trade(
            "sell",
            "600000",
            "2026-03-03T14:30:00",
            exit_price=7.0,
            exit_quantity=100.0,
        ),
    ]
    positions = [
        _position("600010", target_position=0.2, status="open", peak_pnl_pct=0.15),
        _position("600011", target_position=0.2, status="open", peak_pnl_pct=0.18),
    ]
    score = compute_discipline_score(
        trades=trades,
        positions=positions,
        discipline_pass_threshold=85.0,
        position_cut_ratio=0.10,
    )
    assert _as_float(score["total_score"]) < 85.0
    assert score["passed"] is False
    assert score["position_cut_next_month"] is True
    assert _as_float(score["position_cut_ratio"]) == pytest.approx(0.10)


def test_monthly_review_report_empty_data_behavior() -> None:
    report = compute_monthly_review_report(year_month="2026-03", trades=[])
    assert report["status"] == "empty"
    verdict = _as_mapping(report["verdict"])
    assert verdict["grade"] == "insufficient_data"
    assert verdict["passed"] is True
    assert verdict["position_cut_next_month"] is False
    stats = _as_mapping(report["trading_stats"])
    assert _as_int(stats["total_trades"]) == 0


def test_monthly_review_report_invalid_month() -> None:
    report = compute_monthly_review_report(
        year_month="2026/03",
        trades=_sample_trades(),
    )
    assert report["status"] == "invalid_input"


def test_persist_monthly_review_report_writes_json(tmp_path: Path) -> None:
    report = {
        "status": "ok",
        "engine": "monthly_review_report",
        "month": "2026-03",
        "trading_stats": {"round_trips": 1},
        "discipline": {"total_score": 90.0},
    }
    path = tmp_path / "review" / "monthly_review_report.json"
    written = persist_monthly_review_report(report=report, output_path=path)
    assert Path(written).exists() is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["month"] == "2026-03"
    assert payload["discipline"]["total_score"] == 90.0


def test_monthly_review_config_defaults_match_prd() -> None:
    config = MonthlyReviewConfig()
    assert config.enabled is True
    assert config.report_time == "21:30"
    assert config.export_dir == "artifacts/review/monthly"
    assert config.discipline_pass_threshold == 85.0
    assert config.position_cut_ratio == 0.10
    assert config.over_position_threshold == 0.5
    assert config.stop_loss_threshold == -0.08
    assert config.take_profit_trigger == 0.10

    loaded = load_config(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
    assert loaded.monthly_review.enabled is True
    assert loaded.monthly_review.report_time == "21:30"


def test_monthly_review_config_validation() -> None:
    assert MonthlyReviewConfig(report_time="9:5").report_time == "09:05"
    assert MonthlyReviewConfig(report_time="").report_time == ""
    with pytest.raises(ValidationError):
        MonthlyReviewConfig(report_time="25:00")
    assert MonthlyReviewConfig(over_position_threshold=1.5).over_position_threshold == 1.0
    assert MonthlyReviewConfig(position_cut_ratio=-0.2).position_cut_ratio == 0.0


def test_monthly_review_report_job_registered_with_monthly_predicate() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    service = StockAnalyzerService(config=config)

    job = service._scheduler._jobs.get("monthly_review_report")
    assert job is not None
    assert job.trigger_time.isoformat() == "21:30:00"
    assert job.date_predicate is not None
    assert job.date_predicate(date(2026, 3, 31)) is True
    assert job.date_predicate(date(2026, 3, 30)) is False


def test_monthly_review_report_job_skipped_when_disabled() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.monthly_review.enabled = False
    service = StockAnalyzerService(config=config)

    assert "monthly_review_report" not in service._scheduler._jobs


def test_service_build_monthly_review_report_persists_and_audits() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.command_channel.secret_key = "test-secret"
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.notification_filter.enabled = False
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.monthly_review.auto_notify = False
    service = StockAnalyzerService(config=config)

    ts = int(time.time())
    signature = SignedCommandProcessor.build_signature(
        secret_key=config.command_channel.secret_key,
        command_id="cmd-monthly-review-set",
        timestamp=ts,
        action="SET_POSITION",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
    )
    envelope = CommandEnvelope(
        command_id="cmd-monthly-review-set",
        timestamp=ts,
        action="SET_POSITION",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        signature=signature,
    )
    result = service.execute_command(envelope)
    assert result["accepted"] is True

    report = service.build_monthly_review_report(
        output_path=str(root / "artifacts" / "test_monthly_review.json")
    )
    assert report["status"] in {"ok", "empty"}
    assert "output_path" in report
    assert Path(str(report["output_path"])).exists() is True

    latest = service.latest_monthly_review_report()
    assert latest is not None
    assert latest["month"] == report["month"]
    history = service.monthly_review_history(limit=10)
    assert _as_int(history["records"]) >= 1

    events = [
        event
        for event in service._audit_events
        if event.get("event_type") == "monthly_review_report_built"
    ]
    assert len(events) >= 1
    assert events[-1]["payload"]["month"] == report["month"]


def test_job_monthly_review_report_runs_and_returns_report() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.monthly_review.auto_notify = False
    service = StockAnalyzerService(config=config)

    payload = service._job_monthly_review_report()
    report = payload.get("report")
    assert isinstance(report, Mapping)
    assert report["engine"] == "monthly_review_report"
