from __future__ import annotations

import json

import pytest

from scripts.audit_tail_score_direction import (
    _apply_global_holm_adjustment,
    _atomic_write_new,
    _has_complete_features,
    _load_samples,
)


def test_development_loader_rejects_observed_and_mixed_capture_modes(tmp_path) -> None:
    path = tmp_path / "samples.jsonl"
    path.write_text(json.dumps({
        "symbol": "600001", "decision_date": "2025-01-02", "entry_date": "2025-01-03",
        "capture_mode": "observed_snapshot",
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="replayed development samples only"):
        _load_samples(path)


def test_development_loader_rejects_duplicate_sample_keys(tmp_path) -> None:
    row = {
        "symbol": "600001", "decision_date": "2025-01-02", "entry_date": "2025-01-03",
        "capture_mode": "replayed_recompute",
    }
    path = tmp_path / "samples.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate sample decision key"):
        _load_samples(path)


def test_development_loader_rejects_duplicate_symbol_decision_with_different_entry(
    tmp_path,
) -> None:
    first = {
        "symbol": "600001", "decision_date": "2025-01-02", "entry_date": "2025-01-03",
        "capture_mode": "replayed_recompute",
    }
    second = {**first, "entry_date": "2025-01-06"}
    path = tmp_path / "samples.jsonl"
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate sample decision key"):
        _load_samples(path)


def test_feature_completeness_rejects_missing_non_numeric_and_nonfinite_values() -> None:
    names = ("feature_a", "feature_b")
    assert _has_complete_features({"feature_a": 1, "feature_b": "2.5"}, names)
    assert not _has_complete_features({"feature_a": 1, "feature_b": None}, names)
    assert not _has_complete_features({"feature_a": 1, "feature_b": "unknown"}, names)
    assert not _has_complete_features({"feature_a": 1, "feature_b": float("nan")}, names)


def test_report_writer_refuses_to_overwrite_existing_evidence(tmp_path) -> None:
    path = tmp_path / "direction.json"
    _atomic_write_new(path, "first")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _atomic_write_new(path, "second")
    assert path.read_text(encoding="utf-8") == "first"


def test_direction_metrics_report_the_global_holm_family_scope() -> None:
    arms = [{
        "arm_id": "price_arm",
        "direction_audit": {
            "direction_comparison": {
                "metrics": {
                    "net_profit_rate": {"raw_p_one_sided_low_gt_high": 0.01},
                    "mean_net_return": {"raw_p_one_sided_low_gt_high": 0.04},
                },
                "holm": {"method": "step_down", "family_size": 2},
            }
        },
    }]
    _apply_global_holm_adjustment(
        arms,
        {
            "price_arm:net_profit_rate": 0.01,
            "price_arm:mean_net_return": 0.04,
            "blocked_arm:net_profit_rate": None,
            "blocked_arm:mean_net_return": None,
        },
        family_size=4,
    )

    comparison = arms[0]["direction_audit"]["direction_comparison"]
    assert comparison["holm"] == {
        "method": "step_down",
        "family_size": 4,
        "scope": "all registered arm-by-metric tests; blocked tests contribute p=1",
    }
