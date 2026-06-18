from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.research.p0_analysis_inputs import (
    build_cross_review_failure_analysis,
    build_model_diagnosis_final,
    collect_signal_rows,
    write_p0_analysis_inputs,
)


def _load_test_config():
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_model_diagnosis_flags_zero_positive_test_split(tmp_path: Path) -> None:
    artifact = tmp_path / "model_v1.json"
    _write_json(
        artifact,
        {
            "version": "v2",
            "created_at": "2026-06-15T12:00:00",
            "feature_columns": ["a", "b"],
            "lgbm_model": {"backend": "fallback_logit"},
            "xgb_model": {"backend": "fallback_logit"},
            "training_metrics": {
                "positive_rate": 0.0,
                "test_samples": 52,
                "meta_mean_prob": 0.04175,
                "lgbm_mean_prob": 0.8,
                "xgb_mean_prob": 0.0,
            },
            "metadata": {
                "train_samples": 412,
                "calibration_samples": 52,
                "test_samples": 52,
                "degraded_model_mode": True,
            },
        },
    )

    report = build_model_diagnosis_final(
        model_artifact_path=artifact,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    assert report["status"] == "needs_label_split_repair"
    assert report["label_distribution"]["test"]["positive"] == 0
    action_codes = {item["code"] for item in report["recommended_next_actions"]}
    assert "label_split_trainability_shadow" in action_codes
    assert "model_backend_dependency_check" in action_codes


def test_cross_review_failure_analysis_replays_current_thresholds() -> None:
    config = _load_test_config()
    report = build_cross_review_failure_analysis(
        signals=[
            {
                "symbol": "600000",
                "action": "buy",
                "score": 80,
                "probabilities": {"lgbm": 0.7, "xgb": 0.65, "meta": 0.62},
            },
            {
                "symbol": "000001",
                "action": "hold",
                "score": 45,
                "probabilities": {"lgbm": 1.0, "xgb": 0.2, "meta": 0.49},
            },
        ],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    stats = report["cross_review_analysis"]["gate_statistics"]
    assert stats["total_evaluated_rows"] == 2
    assert stats["total_cross_review_pass"] == 1
    assert stats["total_cross_review_fail"] == 1
    reasons = report["cross_review_analysis"]["reason_counts"]
    assert reasons["xgb<0.55"] == 1
    assert reasons["model_diff>0.18"] == 1
    distribution = report["cross_review_analysis"]["probability_distribution"]
    assert distribution["meta"]["pass"] == 1


def test_write_p0_analysis_inputs_collects_runtime_and_week5_rows(tmp_path: Path) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.0, "test_samples": 10},
            "metadata": {"test_samples": 10},
        },
    )
    _write_json(
        runtime_state,
        {
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 80,
                        "action": "buy",
                        "probabilities": {"lgbm": 0.7, "xgb": 0.65, "meta": 0.62},
                    }
                ],
            },
            "week5_scan_latest": {
                "timestamp": "2026-06-18T11:00:00",
                "signal_pool": {
                    "candidates": [
                        {
                            "symbol": "000001",
                            "shortlist_score": 45,
                            "action": "hold",
                            "probabilities": {"lgbm": 1.0, "xgb": 0.2, "meta": 0.49},
                        }
                    ]
                },
            },
        },
    )

    rows = collect_signal_rows([runtime_state])
    assert {row["symbol"] for row in rows} == {"600000", "000001"}

    manifest = write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    assert manifest["inputs"]["signal_rows"] == 2
    assert (tmp_path / "analysis" / "model_diagnosis_final.json").exists()
    assert (
        tmp_path / "analysis" / "p4_cross_review_failure_analysis_v1.json"
    ).exists()


def test_write_p0_analysis_inputs_does_not_mutate_runtime_state(tmp_path: Path) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.0, "test_samples": 10},
            "metadata": {"test_samples": 10},
        },
    )
    original_runtime_state = {
        "latest_signals": {
            "timestamp": "2026-06-18T10:00:00",
            "signals": [
                {
                    "symbol": "600000",
                    "score": 80,
                    "action": "buy",
                    "probabilities": {"lgbm": 0.7, "xgb": 0.65, "meta": 0.62},
                }
            ],
        }
    }
    _write_json(runtime_state, original_runtime_state)
    before = runtime_state.read_text(encoding="utf-8")

    write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    assert runtime_state.read_text(encoding="utf-8") == before
    assert (tmp_path / "analysis" / "p0_analysis_inputs_manifest.json").exists()
