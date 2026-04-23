from __future__ import annotations

from datetime import UTC, datetime

from stock_analyzer.evolution.modules.shadow_online_model_v2 import (
    run_shadow_online_model_v2,
    shadow_online_v2_result_to_dict,
)


def test_shadow_online_model_v2_updates_with_richer_shadow_records() -> None:
    records = [
        {
            "symbol": "600000.SH",
            "trade_date": "2026-03-01",
            "label_mature_time": "2026-03-02T15:00:00",
            "label": 1,
            "sample_weight": 1.2,
            "data_quality_score": 0.95,
            "execution_fill_ratio": 0.92,
            "realized_slippage_bp": 11.0,
            "realized_return": 0.08,
            "champion_scores": {"p_meta": 0.42, "p_lgbm": 0.40, "p_xgb": 0.43},
            "shadow_scores": {"p_meta": 0.55, "p_lgbm": 0.57, "p_xgb": 0.53},
            "champion_signal": 0,
            "shadow_signal": 1,
            "score_breakdown": {"trend": 0.7, "quality": 0.4},
            "risk_context": {"volatility_regime": 0.25, "suspended": False},
            "regime_context": {"bull": 1, "dispersion": 0.18},
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.2,
            "volume": 1_500_000,
        },
        {
            "symbol": "000001.SZ",
            "trade_date": "2026-03-01",
            "label_mature_time": "2026-03-02T15:00:00",
            "label": 0,
            "sample_weight": 0.9,
            "data_quality_score": 0.90,
            "execution_fill_ratio": 0.88,
            "realized_slippage_bp": 16.0,
            "realized_return": -0.06,
            "champion_scores": {"p_meta": 0.58, "p_lgbm": 0.60, "p_xgb": 0.56},
            "shadow_scores": {"p_meta": 0.48, "p_lgbm": 0.45, "p_xgb": 0.50},
            "champion_signal": 1,
            "shadow_signal": 0,
            "score_breakdown": {"trend": -0.4, "quality": 0.1},
            "risk_context": {"volatility_regime": 0.35, "suspended": False},
            "regime_context": {"bull": 0, "dispersion": 0.24},
            "open": 10.0,
            "high": 10.1,
            "low": 9.6,
            "close": 9.7,
            "volume": 1_200_000,
        },
        {
            "symbol": "300001.SZ",
            "trade_date": "2026-03-02",
            "label_mature_time": "2026-03-03T15:00:00",
            "label": 1,
            "sample_weight": 1.0,
            "data_quality_score": 1.0,
            "execution_fill_ratio": 0.96,
            "realized_slippage_bp": 9.0,
            "realized_return": 0.07,
            "champion_scores": {"p_meta": 0.47, "p_lgbm": 0.46, "p_xgb": 0.48},
            "shadow_scores": {"p_meta": 0.61, "p_lgbm": 0.63, "p_xgb": 0.58},
            "champion_signal": 0,
            "shadow_signal": 1,
            "score_breakdown": {"trend": 0.6, "quality": 0.3},
            "risk_context": {"volatility_regime": 0.22, "suspended": False},
            "regime_context": {"bull": 1, "dispersion": 0.15},
            "open": 9.8,
            "high": 10.2,
            "low": 9.7,
            "close": 10.1,
            "volume": 980_000,
        },
        {
            "symbol": "002594.SZ",
            "trade_date": "2026-03-02",
            "label_mature_time": "2026-03-03T15:00:00",
            "label": 0,
            "sample_weight": 1.1,
            "data_quality_score": 0.92,
            "execution_fill_ratio": 0.84,
            "realized_slippage_bp": 18.0,
            "realized_return": -0.05,
            "champion_scores": {"p_meta": 0.57, "p_lgbm": 0.55, "p_xgb": 0.58},
            "shadow_scores": {"p_meta": 0.44, "p_lgbm": 0.43, "p_xgb": 0.46},
            "champion_signal": 1,
            "shadow_signal": 0,
            "score_breakdown": {"trend": -0.5, "quality": 0.2},
            "risk_context": {"volatility_regime": 0.31, "suspended": False},
            "regime_context": {"bull": 0, "dispersion": 0.26},
            "open": 11.0,
            "high": 11.1,
            "low": 10.5,
            "close": 10.6,
            "volume": 1_050_000,
        },
    ]

    result = run_shadow_online_model_v2(
        records=records,
        now=datetime(2026, 3, 4, 20, 40, tzinfo=UTC),
        previous_state=None,
        max_samples=10,
        min_samples=3,
        learning_rate=0.1,
        preview_limit=2,
        signal_threshold=0.5,
    )
    payload = shadow_online_v2_result_to_dict(result)

    assert result.status == "updated"
    assert result.metrics.valid_samples == 4
    assert result.metrics.updates_applied == 4
    assert result.metrics.avg_execution_fill_ratio > 0.0
    assert result.metrics.signal_divergence_ratio > 0.0
    assert len(result.preview) == 2
    assert "shadow_p_meta" in result.state["feature_names"]
    assert "execution_fill_ratio" in result.state["feature_names"]
    assert "risk:volatility_regime" in result.state["feature_names"]
    assert payload["metrics"]["valid_samples"] == 4


def test_shadow_online_model_v2_skips_unmatured_records() -> None:
    records = [
        {
            "symbol": "600000.SH",
            "trade_date": "2026-03-03",
            "label_mature_time": "2026-03-06T15:00:00",
            "label": 1,
            "shadow_scores": {"p_meta": 0.55},
        },
        {
            "symbol": "000001.SZ",
            "trade_date": "2026-03-01",
            "label_mature_time": "2026-03-02T15:00:00",
            "label": 1,
            "shadow_scores": {"p_meta": 0.52},
            "champion_scores": {"p_meta": 0.48},
            "execution_fill_ratio": 0.9,
            "realized_slippage_bp": 8.0,
            "sample_weight": 1.0,
            "data_quality_score": 1.0,
        },
    ]

    result = run_shadow_online_model_v2(
        records=records,
        now=datetime(2026, 3, 4, 20, 40, tzinfo=UTC),
        previous_state=None,
        max_samples=10,
        min_samples=1,
        learning_rate=0.1,
        preview_limit=5,
    )

    assert result.status == "updated"
    assert result.samples_considered == 1
    assert result.samples_used == 1
    assert result.preview[0]["symbol"] == "000001.SZ"


def test_shadow_online_model_v2_accepts_reconciled_records_before_label_mature_time() -> None:
    result = run_shadow_online_model_v2(
        records=[
            {
                "symbol": "600000.SH",
                "trade_date": "2026-03-03",
                "label_mature_time": "2026-03-10T15:00:00",
                "maturity_status": "reconciled",
                "reconcile_status": "ok",
                "label": 1,
                "shadow_scores": {"p_meta": 0.55},
                "champion_scores": {"p_meta": 0.48},
                "execution_fill_ratio": 0.9,
                "realized_slippage_bp": 8.0,
                "sample_weight": 1.0,
                "data_quality_score": 1.0,
            },
        ],
        now=datetime(2026, 3, 4, 20, 40, tzinfo=UTC),
        previous_state=None,
        max_samples=10,
        min_samples=1,
        learning_rate=0.1,
        preview_limit=5,
    )

    assert result.status == "updated"
    assert result.samples_considered == 1
    assert result.samples_used == 1
