from __future__ import annotations

from pathlib import Path

from stock_analyzer.evolution.modules.m11_shadow_loader import load_m11_shadow_observations
from stock_analyzer.evolution.modules.m11_shadow_portfolio import evaluate_m11_shadow_portfolio


def test_m11_shadow_stable_with_small_delta() -> None:
    result = evaluate_m11_shadow_portfolio(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "close": 10.2,
                "champion_shadow_return": 0.020,
                "challenger_shadow_return": 0.018,
                "champion_signal": 1,
                "challenger_signal": 1,
                "trade_date": "2026-06-01",
                "label_mature_time": "2026-06-08",
                "champion_probability": 0.8,
                "challenger_probability": 0.7,
            },
            {
                "symbol": "000001.SZ",
                "open": 8.0,
                "close": 8.08,
                "champion_shadow_return": 0.010,
                "challenger_shadow_return": 0.009,
                "champion_signal": 1,
                "challenger_signal": 1,
                "trade_date": "2026-06-02",
                "label_mature_time": "2026-06-09",
                "champion_probability": 0.6,
                "challenger_probability": 0.65,
            },
            {
                "symbol": "600036.SH",
                "open": 6.0,
                "close": 5.97,
                "champion_shadow_return": -0.005,
                "challenger_shadow_return": -0.006,
                "champion_signal": 0,
                "challenger_signal": 0,
                "trade_date": "2026-06-03",
                "label_mature_time": "2026-06-10",
            },
        ]
    )
    assert result.status == "stable"
    assert result.score > 70.0
    assert result.redlines["drawdown_delta"] is False
    assert result.redlines["tail_loss_delta"] is False
    assert result.redlines["execution_divergence"] is False
    # 事件驱动主口径：signal==1 的两行各占一个仓位（默认上限内），全部到期结算。
    assert result.metrics.champion_open_positions == 0
    assert result.metrics.challenger_open_positions == 0
    assert result.redlines["insufficient_date_coverage"] is False
    # 已知小样例精确值：仓位 = 入场时 current_nav/10。两笔分别于 06-01/06-02
    # 入场（期间无结算，NAV 均为 1.0 → size 均 0.1），06-08/06-09 到期结算。
    assert result.metrics.champion_slot_final_nav == __import__("pytest").approx(
        1.0 + 0.020 * 0.1 + 0.010 * 0.1
    )
    # legacy 逐笔全仓复利保留为对照字段。
    assert result.metrics.legacy_champion_cum_return == __import__("pytest").approx(
        1.02 * 1.01 * 0.995 - 1.0
    )


def test_m11_shadow_redline_breach_on_drawdown_tail_and_divergence() -> None:
    result = evaluate_m11_shadow_portfolio(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "close": 9.0,
                "champion_shadow_return": -0.01,
                "challenger_shadow_return": -0.10,
                "champion_signal": 0,
                "challenger_signal": 1,
                "trade_date": "2026-06-01",
                "label_mature_time": "2026-06-05",
                "challenger_probability": 0.9,
            },
            {
                "symbol": "000001.SZ",
                "open": 10.0,
                "close": 8.8,
                "champion_shadow_return": -0.02,
                "challenger_shadow_return": -0.12,
                "champion_signal": 0,
                "challenger_signal": 1,
                "trade_date": "2026-06-02",
                "label_mature_time": "2026-06-06",
                "challenger_probability": 0.8,
            },
            {
                "symbol": "600036.SH",
                "open": 10.0,
                "close": 9.1,
                "champion_shadow_return": -0.01,
                "challenger_shadow_return": -0.09,
                "champion_signal": 0,
                "challenger_signal": 1,
                "trade_date": "2026-06-03",
                "label_mature_time": "2026-06-07",
                "challenger_probability": 0.85,
            },
        ]
    )
    assert result.status == "redline_breach"
    assert result.score < 70.0
    assert any(result.redlines.values()) is True
    assert result.metrics.execution_divergence_ratio > 0.35
    assert len(result.attribution) == 3
    # champion 无仓位（signal=0）→ 回撤 0；challenger 连续亏损 → drawdown delta 触发。
    assert result.metrics.champion_max_drawdown == 0.0
    assert result.metrics.challenger_max_drawdown > 0.0


def test_m11_shadow_insufficient_date_coverage_is_explicit_redline() -> None:
    # 缺日期的 signal=1 行不得静默回退：必须显式进入 insufficient_date_coverage。
    result = evaluate_m11_shadow_portfolio(
        records=[
            {
                "champion_shadow_return": 0.02,
                "challenger_shadow_return": 0.018,
                "champion_signal": 1,
                "challenger_signal": 1,
            }
        ]
    )
    assert result.status == "insufficient_date_coverage"
    assert result.redlines["insufficient_date_coverage"] is True
    assert result.metrics.champion_slot_final_nav == 1.0  # 缺陷仓位不参与模拟
    assert result.metrics.coverage_defects


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
            '"champion_signal":1,"challenger_signal":1,'
            '"trade_date":"2026-06-01","label_mature_time":"2026-06-08",'
            '"champion_prob":0.7,"shadow_v2_probability":0.66},'
            '{"symbol":"000001.SZ","champion_return":-0.005,"challenger_return":-0.006,'
            '"champion_signal":0,"challenger_signal":0,'
            '"trade_date":"2026-06-02","label_mature_time":"2026-06-09"}]'
        ),
        encoding="utf-8",
    )
    observations = load_m11_shadow_observations(path=artifact)
    result = evaluate_m11_shadow_portfolio(shadow_observations=observations)
    assert result.status == "stable"
    assert result.metrics.valid_samples == 2
    assert result.metrics.execution_divergence_ratio == 0.0
    assert result.redlines["insufficient_date_coverage"] is False
    assert result.metrics.champion_event_days > 0
