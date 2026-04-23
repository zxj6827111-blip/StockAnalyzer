from __future__ import annotations

from stock_analyzer.evolution.modules.m2_regime_hmm import (
    M2OptunaLikeConfig,
    RegimeModelParams,
    RegimeObservation,
    RegimeStateController,
    evaluate_m2_regime,
    infer_regime,
    tune_regime_with_optuna_like_search,
)


def test_infer_regime_extreme_case() -> None:
    inference = infer_regime(
        RegimeObservation(
            atr_ratio=0.07,
            sector_dispersion=0.20,
            turnover_zscore=0.2,
        )
    )
    assert inference.state == "extreme"
    assert 0.0 <= inference.confidence <= 1.0


def test_regime_controller_switch_requires_two_high_conf_days() -> None:
    controller = RegimeStateController(active_state="range")
    high_conf_obs = RegimeObservation(
        atr_ratio=0.02,
        sector_dispersion=0.10,
        turnover_zscore=1.4,
    )

    first = controller.update(high_conf_obs)
    second = controller.update(high_conf_obs)
    assert first.active_state == "range"
    assert first.switched is False
    assert second.active_state == "trend_up"
    assert second.switched is True


def test_evaluate_m2_regime_returns_score() -> None:
    controller = RegimeStateController(active_state="range")
    result = evaluate_m2_regime(
        controller=controller,
        observation=RegimeObservation(
            atr_ratio=0.02,
            sector_dispersion=0.25,
            turnover_zscore=0.1,
        ),
    )
    assert 0.0 <= result.score <= 100.0
    assert result.snapshot.active_state in {"range", "trend_up", "trend_down", "extreme"}


def test_regime_controller_dump_and_load_roundtrip() -> None:
    observation = RegimeObservation(
        atr_ratio=0.02,
        sector_dispersion=0.12,
        turnover_zscore=1.5,
    )
    controller = RegimeStateController(active_state="range")
    first = controller.update(observation)
    assert first.pending_days == 1
    payload = controller.dump_state()

    restored = RegimeStateController(active_state="range")
    restored.load_state(payload)
    second = restored.update(observation)
    assert second.active_state == "trend_up"
    assert second.switched is True


def test_m2_optuna_like_tuning_reports_insufficient_samples() -> None:
    result = tune_regime_with_optuna_like_search(
        observations=[
            RegimeObservation(atr_ratio=0.02, sector_dispersion=0.20, turnover_zscore=0.1),
            RegimeObservation(atr_ratio=0.03, sector_dispersion=0.22, turnover_zscore=0.2),
        ],
        config=M2OptunaLikeConfig(min_samples=5, n_trials=16),
    )
    assert result.tuned is False
    assert result.reason == "insufficient_samples"
    assert result.trials == 0


def test_m2_optuna_like_tuning_updates_params_with_enough_samples() -> None:
    observations: list[RegimeObservation] = []
    for idx in range(40):
        observations.append(
            RegimeObservation(
                atr_ratio=0.02 + (idx % 5) * 0.003,
                sector_dispersion=0.18 + (idx % 4) * 0.02,
                turnover_zscore=0.6 + (idx % 7) * 0.25,
            )
        )
    baseline = RegimeModelParams()
    result = tune_regime_with_optuna_like_search(
        observations=observations,
        baseline_params=baseline,
        config=M2OptunaLikeConfig(
            min_samples=20,
            n_trials=64,
            min_improvement=0.0,
            random_seed=7,
        ),
    )
    assert result.sample_count == 40
    assert result.trials == 64
    assert result.params.switch_confirm_days >= 1
    assert result.objective_score >= result.baseline_score - 1e-12
