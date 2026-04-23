from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.risk.controls import RiskController
from stock_analyzer.types import RiskStatus


def _load_default() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def test_risk_controller_blocks_new_buy_in_degraded_mode() -> None:
    config = _load_default()
    risk = RiskController(config)
    risk.update_degraded_mode(hard_degraded_mode=True, soft_degraded_mode=False)
    status = risk.evaluate(current_equity=1.0)
    assert status.degraded_mode is True
    assert status.hard_degraded_mode is True
    assert status.soft_degraded_mode is False
    assert status.can_open_new_position is False
    assert status.reason == "degraded_stop_new_buy"


def test_risk_controller_keeps_new_buy_open_under_soft_degraded_mode() -> None:
    config = _load_default()
    risk = RiskController(config)
    risk.update_degraded_mode(hard_degraded_mode=False, soft_degraded_mode=True)
    status = risk.evaluate(current_equity=1.0)
    assert status.degraded_mode is True
    assert status.hard_degraded_mode is False
    assert status.soft_degraded_mode is True
    assert status.can_open_new_position is True
    assert status.reason == "soft_degraded_monitoring"


def test_risk_controller_freezes_on_large_drawdown() -> None:
    config = _load_default()
    risk = RiskController(config)
    status = risk.evaluate(current_equity=0.84)
    assert status.action == "freeze"
    assert status.drawdown_pct >= 15.0


def test_risk_status_contract_includes_split_degraded_flags() -> None:
    names = {item.name for item in fields(RiskStatus)}
    assert "hard_degraded_mode" in names
    assert "soft_degraded_mode" in names
