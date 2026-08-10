from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import load_config
from stock_analyzer.research.daily_review_report import (
    compute_daily_review_report,
    persist_daily_review_report,
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
            "2026-03-10T09:31:00",
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
            "2026-03-10T09:31:00",
            entry_price=20.0,
            quantity=200.0,
            fee=10.0,
            target_position=0.3,
        ),
        _trade(
            "sell",
            "600001",
            "2026-03-10T14:30:00",
            exit_price=18.0,
            exit_quantity=200.0,
            exit_fee=10.0,
        ),
    ]


def test_daily_review_report_trading_stats() -> None:
    report = compute_daily_review_report(
        date="2026-03-10",
        trades=_sample_trades(),
    )
    assert report["status"] == "ok"
    assert report["engine"] == "daily_review_report"
    assert report["date"] == "2026-03-10"
    stats = _as_mapping(report["trading_stats"])
    assert stats["date"] == "2026-03-10"
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
    assert _as_float(stats["avg_holding_days"]) == pytest.approx(0.0)
    assert _as_int(stats["symbols_traded"]) == 2


def test_daily_review_report_filters_records_to_day() -> None:
    trades = _sample_trades() + [
        _trade(
            "buy",
            "600002",
            "2026-03-11T09:31:00",
            entry_price=5.0,
            quantity=100.0,
        ),
        _trade(
            "buy",
            "600003",
            "2026-04-02T09:31:00",
            entry_price=5.0,
            quantity=100.0,
        ),
    ]
    report = compute_daily_review_report(date="2026-03-10", trades=trades)
    inputs = _as_mapping(report["inputs"])
    assert _as_int(inputs["trades"]) == 4
    assert _as_int(inputs["skipped_trades"]) == 2
    stats = _as_mapping(report["trading_stats"])
    assert _as_int(stats["open_trades"]) == 2
    assert _as_int(stats["symbols_traded"]) == 2


def test_daily_review_report_signals_section() -> None:
    signals = [
        {
            "symbol": "600001",
            "grade": "S",
            "action": "buy",
            "timestamp": "2026-03-10T09:00:00",
        },
        {
            "symbol": "600002",
            "grade": "S",
            "action": "buy",
            "timestamp": "2026-03-11T09:00:00",
        },
        {
            "symbol": "600003",
            "grade": "A",
            "action": "buy",
            "timestamp": "2026-03-10T09:05:00",
        },
        {
            "symbol": "600004",
            "grade": "S",
            "action": "sell",
            "timestamp": "2026-03-10T09:10:00",
        },
    ]
    report = compute_daily_review_report(
        date="2026-03-10",
        trades=_sample_trades(),
        signals=signals,
    )
    section = _as_mapping(report["signals"])
    assert _as_int(section["total"]) == 3
    by_grade = _as_mapping(section["by_grade"])
    assert _as_int(by_grade["S"]) == 2
    assert _as_int(by_grade["A"]) == 1
    s_level_buys = _as_mapping_list(section["s_level_buys"])
    assert _as_int(section["s_level_buys_count"]) == 1
    assert s_level_buys[0]["symbol"] == "600001"


def test_daily_review_report_position_changes() -> None:
    positions = [
        _position(
            "600001",
            target_position=0.2,
            status="open",
            peak_pnl_pct=0.15,
            opened_at="2026-03-10T09:35:00",
        ),
        _position(
            "600002",
            target_position=0.2,
            status="closed",
            peak_pnl_pct=0.02,
            opened_at="2026-03-05T09:30:00",
        ),
    ]
    report = compute_daily_review_report(
        date="2026-03-10",
        trades=_sample_trades(),
        positions=positions,
    )
    changes = _as_mapping(report["position_changes"])
    assert _as_int(changes["open_positions"]) == 2
    opened_today = _as_mapping_list(changes["opened_today"])
    assert len(opened_today) == 1
    assert opened_today[0]["symbol"] == "600001"
    assert _as_int(changes["buy_orders"]) == 2
    assert _as_int(changes["sell_orders"]) == 2
    take_profit_due = _as_mapping_list(changes["take_profit_due"])
    assert len(take_profit_due) == 1
    assert take_profit_due[0]["symbol"] == "600001"
    assert _as_float(take_profit_due[0]["peak_pnl_pct"]) == pytest.approx(0.15)


def test_daily_review_report_discipline_and_hints() -> None:
    report = compute_daily_review_report(
        date="2026-03-10",
        trades=_sample_trades(),
    )
    discipline = _as_mapping(report["discipline"])
    components = _as_mapping(discipline["components"])
    assert "manual_intervention" in components
    assert "stop_loss_violations" in components
    assert _as_float(discipline["total_score"]) == pytest.approx(98.57, abs=0.02)
    hints = [str(item) for item in report["discipline_hints"]]
    assert hints, "expected non-empty discipline hints"
    assert any("当日成交样本较少" in item for item in hints) is False
    assert any("止损" in item for item in hints) is True


def test_daily_review_report_default_hint_when_clean() -> None:
    report = compute_daily_review_report(
        date="2026-03-10",
        trades=[
            _trade(
                "buy",
                "600000",
                "2026-03-10T09:31:00",
                entry_price=10.0,
                quantity=100.0,
            ),
            _trade(
                "sell",
                "600000",
                "2026-03-10T14:30:00",
                exit_price=11.0,
                exit_quantity=100.0,
            ),
            _trade(
                "buy",
                "600001",
                "2026-03-10T09:32:00",
                entry_price=20.0,
                quantity=100.0,
            ),
            _trade(
                "sell",
                "600001",
                "2026-03-10T14:31:00",
                exit_price=21.0,
                exit_quantity=100.0,
            ),
        ],
    )
    hints = [str(item) for item in report["discipline_hints"]]
    assert any("当日执行纪律正常" in item for item in hints) is True


def test_daily_review_report_sample_size_hint_with_few_trades() -> None:
    report = compute_daily_review_report(
        date="2026-03-10",
        trades=[
            _trade(
                "buy",
                "600000",
                "2026-03-10T09:31:00",
                entry_price=10.0,
                quantity=100.0,
            )
        ],
    )
    hints = [str(item) for item in report["discipline_hints"]]
    assert any("当日成交样本较少" in item for item in hints) is True


def test_daily_review_report_empty_data_behavior() -> None:
    report = compute_daily_review_report(date="2026-03-10", trades=[])
    assert report["status"] == "empty"
    stats = _as_mapping(report["trading_stats"])
    assert _as_int(stats["total_trades"]) == 0
    assert report["discipline_hints"] == []


def test_daily_review_report_invalid_date() -> None:
    report = compute_daily_review_report(
        date="2026/03/10",
        trades=_sample_trades(),
    )
    assert report["status"] == "invalid_input"


def test_persist_daily_review_report_writes_json(tmp_path: Path) -> None:
    report = {
        "status": "ok",
        "engine": "daily_review_report",
        "date": "2026-03-10",
        "trading_stats": {"round_trips": 1},
        "discipline": {"total_score": 90.0},
    }
    path = tmp_path / "review" / "daily_review_report.json"
    written = persist_daily_review_report(report=report, output_path=path)
    assert Path(written).exists() is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["date"] == "2026-03-10"
    assert payload["discipline"]["total_score"] == 90.0


def test_service_build_daily_review_report_persists_and_audits() -> None:
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
        command_id="cmd-daily-review-set",
        timestamp=ts,
        action="SET_POSITION",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
    )
    envelope = CommandEnvelope(
        command_id="cmd-daily-review-set",
        timestamp=ts,
        action="SET_POSITION",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        signature=signature,
    )
    result = service.execute_command(envelope)
    assert result["accepted"] is True

    report = service.build_daily_review_report(
        output_path=str(root / "artifacts" / "test_daily_review.json")
    )
    assert report["status"] in {"ok", "empty"}
    assert report["engine"] == "daily_review_report"
    assert "date" in report
    assert "output_path" in report
    assert Path(str(report["output_path"])).exists() is True

    latest = service.latest_daily_review_report()
    assert latest is not None
    assert latest["date"] == report["date"]
    history = service.daily_review_history(limit=10)
    assert _as_int(history["records"]) >= 1

    events = [
        event
        for event in service._audit_events
        if event.get("event_type") == "daily_review_report_built"
    ]
    assert len(events) >= 1
    assert events[-1]["payload"]["date"] == report["date"]
