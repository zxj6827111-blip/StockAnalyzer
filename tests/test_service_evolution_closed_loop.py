from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.types import PipelineSignal


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _new_service() -> StockAnalyzerService:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2027)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    return service


def test_service_provider_status_surfaces_latest_evolution_runtime_controls() -> None:
    service = _new_service()
    fresh_timestamp = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    _patch_attr(service, "_last_evolution_report", {
        "timestamp": fresh_timestamp,
        "run_id": "evo-run-1",
        "runtime_controls": {
            "degraded_mode": True,
            "degraded_reason": "m10_degraded",
            "threshold_shift": 1.5,
            "position_multiplier": 0.7,
            "global_risk_delta": 12.0,
        },
    })

    status = _as_mapping(service.provider_status())
    controls = _as_mapping(status["evolution"])
    week6_controls = _as_mapping(service._build_week6_execution_controls(
        strategy="trend",
        symbols=["600000"],
        drawdown_pct=0.0,
    ))

    assert controls["source"] == "evolution"
    assert controls["source_run_id"] == "evo-run-1"
    assert controls["source_timestamp"] == fresh_timestamp
    assert controls["degraded_reason"] == "m10_degraded"
    assert status["hard_degraded_mode"] is False
    assert status["soft_degraded_mode"] is True
    assert _as_mapping(week6_controls["evolution"])["threshold_shift"] == 1.5
    assert _as_mapping(week6_controls["evolution"])["position_multiplier"] == 0.7
    assert _as_mapping(week6_controls["threshold_shift_components"])["evolution"] == 1.5
    assert _as_mapping(week6_controls["position_multiplier_components"])["evolution"] == 0.7


def test_service_provider_status_ignores_stale_evolution_runtime_controls() -> None:
    service = _new_service()
    service._config.evolution.runtime_controls_max_age_hours = 6.0
    stale_timestamp = (datetime.now() - timedelta(hours=10)).isoformat(timespec="seconds")
    _patch_attr(service, "_last_evolution_report", {
        "timestamp": stale_timestamp,
        "run_id": "evo-stale-1",
        "runtime_controls": {
            "degraded_mode": True,
            "conservative_mode": True,
            "degraded_reason": "m2_extreme",
            "threshold_shift": 9.0,
            "position_multiplier": 0.39,
            "global_risk_delta": -30.0,
        },
    })

    status = _as_mapping(service.provider_status())
    controls = _as_mapping(status["evolution"])
    week6_controls = _as_mapping(service._build_week6_execution_controls(
        strategy="trend",
        symbols=["600000"],
        drawdown_pct=0.0,
    ))

    assert controls["stale"] is True
    assert controls["stale_reason"] == "runtime_controls_expired"
    assert controls["source_timestamp"] == stale_timestamp
    assert status["soft_degraded_mode"] is False
    assert controls["threshold_shift"] == 0.0
    assert controls["position_multiplier"] == 1.0
    assert _as_mapping(week6_controls["evolution"])["threshold_shift"] == 0.0
    assert _as_mapping(week6_controls["evolution"])["position_multiplier"] == 1.0
    assert _as_mapping(week6_controls["threshold_shift_components"])["evolution"] == 0.0
    assert _as_mapping(week6_controls["position_multiplier_components"])["evolution"] == 1.0


def test_service_m1_negative_case_constraints_penalize_matching_buy_signal() -> None:
    service = _new_service()
    _patch_attr(service, "_last_evolution_report", {
        "modules": {
            "m1": {
                "cases_preview": [
                    {
                        "symbol": "600000",
                        "bucket": "severe",
                        "reason_codes": ["data_incomplete"],
                    }
                ],
                "negative_case_count": 1,
                "reason_counts": {"data_incomplete": 1},
            }
        }
    })
    signals = [
        PipelineSignal(
            symbol="600000",
            strategy="trend",
            score=92.0,
            grade="S",
            action="buy",
            target_position=0.6,
            probabilities={"lgbm": 0.8, "xgb": 0.55, "meta": 0.75},
            reasons=["predictor_mode:controlled_heuristic"],
        )
    ]

    summary = _as_mapping(service._apply_m1_negative_case_constraints(
        signals=signals,
        strategy="trend",
        use_live_runtime=False,
    ))

    assert summary["available"] is True
    assert summary["applied"] is True
    assert summary["penalized"] == 1
    assert signals[0].score < 92.0
    assert any(reason.startswith("m1_negative_case_similarity:") for reason in signals[0].reasons)


def test_service_generates_m9_failure_retention_report() -> None:
    service = _new_service()

    report = _as_mapping(service.generate_m9_failure_retention_report())
    degraded_modules = _as_mapping(report["degraded_modules"])

    assert Path(str(report["output_path"])).exists() is True
    assert _as_float(report["retention_ratio"]) >= 0.8
    assert _as_float(report["retained_fields"]) <= _as_float(report["total_fields"])
    assert _as_float(report["total_fields"]) >= 18
    assert degraded_modules["m2"] == "degraded_run"
    assert degraded_modules["m10"] == "degraded_run"
    assert degraded_modules["m11"] == "degraded_run"
    assert degraded_modules["eval_profiles"] == "degraded_run"
    assert degraded_modules["utility_execution"] == "degraded_run"


def test_service_run_pipeline_enriches_learning_snapshot_feedback(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0

    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2029)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)

    m8_artifact_path = tmp_path / "m8_latest.json"
    m8_artifact_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "symbol": "600000",
                        "recommendation": "review",
                        "best_similarity": 0.91,
                        "passed_gates": 5,
                        "gate_total": 6,
                        "failed_gates": ["registry"],
                        "registry_signature": "sig-600000",
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _patch_attr(service, "_last_evolution_report", {
        "run_id": "evo-learning-1",
        "timestamp": "2026-03-26T14:55:00+00:00",
        "modules": {
            "m3": {"vector_profile_id": "m3_profile_v2"},
            "m7": {
                "status": "ok",
                "ledger": {
                    "effectiveness": {
                        "average_effectiveness": 0.72,
                        "source_reliability": [
                            {
                                "source": "新华社",
                                "samples": 4,
                                "mean_effectiveness": 0.88,
                            }
                        ],
                    }
                },
            },
            "m8": {
                "summary": {"records": 1},
                "artifact_uri": str(m8_artifact_path),
            },
        },
    })

    original_apply_m1 = service._apply_m1_negative_case_constraints
    original_build_m7 = service._build_evolution_m7_news_records

    def _stub_apply_m1(*, signals: list[PipelineSignal], strategy: str, use_live_runtime: bool) -> dict[str, object]:
        if signals:
            service._record_signal_runtime_learning_feedback(
                signal=signals[0],
                module_name="m1",
                payload={
                    "m1_negative_case_applied": True,
                    "m1_negative_case_bucket": "severe",
                    "m1_negative_case_similarity": 0.84,
                    "m1_similarity": 0.84,
                    "m1_negative_case_penalty": 11.5,
                    "m1_reason_codes": ["data_incomplete"],
                },
            )
        return {"available": True, "applied": True, "penalized": 1, "matches": []}

    try:
        _patch_attr(service, "_apply_m1_negative_case_constraints", _stub_apply_m1)
        _patch_attr(
            service,
            "_build_evolution_m7_news_records",
            lambda symbols: [
                {
                    "symbol": "600000",
                    "headline": "政策催化带动主线升温",
                    "source": "新华社",
                    "provider": "local_news",
                    "sentiment": 0.25,
                    "llm_confidence": 0.82,
                    "proxy_generated": False,
                }
            ],
        )
        report = _as_mapping(
            service.run_pipeline(
                symbols=["600000"],
                strategy="trend",
                current_equity=1.0,
            )
        )
    finally:
        _patch_attr(service, "_apply_m1_negative_case_constraints", original_apply_m1)
        _patch_attr(service, "_build_evolution_m7_news_records", original_build_m7)

    raw_signals = report.get("signals", [])
    assert isinstance(raw_signals, list)
    first_signal = _as_mapping(raw_signals[0])
    snapshot_id = str(first_signal.get("snapshot_id", "")).strip()
    assert snapshot_id

    decision_trace = _as_mapping(first_signal["decision_trace"])
    runtime_feedback = _as_mapping(decision_trace["runtime_feedback"])
    assert _as_mapping(runtime_feedback["m1"])["m1_negative_case_bucket"] == "severe"
    assert _as_mapping(runtime_feedback["m3"])["m3_match_score"] == 0.91
    assert _as_mapping(runtime_feedback["m7"])["m7_source_reliability"] == 0.88

    snapshot = service._sample_store.get_snapshot(snapshot_id)  # noqa: SLF001
    assert snapshot is not None
    assert snapshot.risk_context["m1_negative_case_bucket"] == "severe"
    assert snapshot.risk_context["m1_negative_case_similarity"] == 0.84
    assert snapshot.regime_context["m3_match_score"] == 0.91
    assert snapshot.regime_context["m3_vector_profile_id"] == "m3_profile_v2"
    assert snapshot.news_context["m7_effectiveness_score"] == 0.72
    assert snapshot.news_context["m7_source_reliability"] == 0.88
    assert snapshot.news_context["m7_top_source"] == "新华社"
    assert snapshot.feature_vector["lp_m1_negative_case_applied"] == 1.0
    assert snapshot.feature_vector["lp_m1_negative_case_bucket_severe"] == 1.0
    assert snapshot.feature_vector["lp_m1_negative_case_similarity"] == 0.84
    assert snapshot.feature_vector["lp_m3_match_score"] == 0.91
    assert snapshot.feature_vector["lp_m3_gate_pass_ratio"] == 0.8333
    assert snapshot.feature_vector["lp_m7_effectiveness_score"] == 0.72
    assert snapshot.feature_vector["lp_m7_source_reliability"] == 0.88
