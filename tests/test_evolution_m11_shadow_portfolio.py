from __future__ import annotations

from pathlib import Path

from stock_analyzer.evolution.modules.m11_shadow_loader import load_m11_shadow_observations
from stock_analyzer.evolution.modules.m11_shadow_portfolio import evaluate_m11_shadow_portfolio


def test_m11_shadow_stable_with_small_delta() -> None:
    result = evaluate_m11_shadow_portfolio(
        records=[
            {
                "open": 10.0,
                "close": 10.2,
                "champion_shadow_return": 0.020,
                "challenger_shadow_return": 0.018,
                "champion_signal": 1,
                "challenger_signal": 1,
            },
            {
                "open": 8.0,
                "close": 8.08,
                "champion_shadow_return": 0.010,
                "challenger_shadow_return": 0.009,
                "champion_signal": 1,
                "challenger_signal": 1,
            },
            {
                "open": 6.0,
                "close": 5.97,
                "champion_shadow_return": -0.005,
                "challenger_shadow_return": -0.006,
                "champion_signal": 0,
                "challenger_signal": 0,
            },
        ]
    )
    assert result.status == "stable"
    assert result.score > 70.0
    assert result.redlines["drawdown_delta"] is False
    assert result.redlines["tail_loss_delta"] is False
    assert result.redlines["execution_divergence"] is False


def test_m11_shadow_redline_breach_on_drawdown_tail_and_divergence() -> None:
    result = evaluate_m11_shadow_portfolio(
        records=[
            {
                "open": 10.0,
                "close": 9.0,
                "champion_shadow_return": -0.01,
                "challenger_shadow_return": -0.10,
                "champion_signal": 0,
                "challenger_signal": 1,
            },
            {
                "open": 10.0,
                "close": 8.8,
                "champion_shadow_return": -0.02,
                "challenger_shadow_return": -0.12,
                "champion_signal": 0,
                "challenger_signal": 1,
            },
            {
                "open": 10.0,
                "close": 9.1,
                "champion_shadow_return": -0.01,
                "challenger_shadow_return": -0.09,
                "champion_signal": 0,
                "challenger_signal": 1,
            },
        ]
    )
    assert result.status == "redline_breach"
    assert result.score < 70.0
    assert any(result.redlines.values()) is True
    assert result.metrics.execution_divergence_ratio > 0.35
    assert len(result.attribution) == 3


def test_m11_shadow_no_data_returns_neutral() -> None:
    result = evaluate_m11_shadow_portfolio(records=[{"open": 0.0, "close": 0.0}])
    assert result.status == "no_data"
    assert result.score == 50.0
    assert result.metrics.valid_samples == 0


def test_m11_shadow_accepts_independent_loader_observations(tmp_path: Path) -> None:
    artifact = tmp_path / "m11_shadow.json"
    artifact.write_text(
        (
            '[{"symbol":"600000.SH","champion_return":0.01,"challenger_return":0.012,'
            '"champion_signal":1,"challenger_signal":1},'
            '{"symbol":"000001.SZ","champion_return":-0.005,"challenger_return":-0.006,'
            '"champion_signal":0,"challenger_signal":0}]'
        ),
        encoding="utf-8",
    )
    observations = load_m11_shadow_observations(path=artifact)
    result = evaluate_m11_shadow_portfolio(shadow_observations=observations)
    assert result.status == "stable"
    assert result.metrics.valid_samples == 2
    assert result.metrics.execution_divergence_ratio == 0.0
