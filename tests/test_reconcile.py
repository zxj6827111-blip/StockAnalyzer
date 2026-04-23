from __future__ import annotations

from datetime import datetime

from stock_analyzer.portfolio.reconcile import reconcile_positions


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


def _as_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def test_reconcile_positions_detects_mismatch_and_missing() -> None:
    strategy = {"600000": 0.2, "000001": 0.1}
    broker = {"600000": 0.19, "300750": 0.1}
    report = reconcile_positions(
        strategy_positions=strategy,
        broker_positions=broker,
        timestamp=datetime.fromisoformat("2026-03-01T15:30:00"),
        tolerance=0.005,
    )
    payload = report.to_dict()
    assert payload["status"] == "mismatch"
    assert _as_int(payload["mismatch_count"]) >= 2
    assert "000001" in _as_text_list(payload["missing_in_broker"])
    assert "300750" in _as_text_list(payload["missing_in_strategy"])
