from __future__ import annotations

from collections.abc import Mapping, Sequence

from stock_analyzer.evolution.eval_profiles import evaluate_dual_eval_profiles


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _as_bool(value: object) -> bool:
    assert isinstance(value, bool)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [item for item in value if isinstance(item, str)]
    assert len(items) == len(value)
    return items


def test_dual_eval_profiles_compute_distribution_and_gate_breach() -> None:
    result = evaluate_dual_eval_profiles(
        records=[
            {"symbol": "600000.SH", "fill_status": "full_fill"},
            {"symbol": "000001.SZ", "fill_status": "no_fill"},
            {"symbol": "300750.SZ", "fill_status": "partial_fill"},
        ],
        profile_set_id="dual_eval_v1",
        no_fill_ratio_limit=0.20,
        partial_fill_ratio_limit=0.35,
        no_fill_ratio_delta_limit=0.05,
        partial_fill_ratio_delta_limit=0.08,
        baseline_no_fill_ratio=0.10,
        baseline_partial_fill_ratio=0.20,
    )
    report = _as_mapping(result.report)
    gate = _as_mapping(report["trading_distribution_gate"])
    profiles = _as_mapping(report["profiles"])
    trading = _as_mapping(profiles["trading_eval_profile"])
    assert _as_int(trading["samples"]) == 3
    assert abs(_as_float(report["no_fill_ratio"]) - (1.0 / 3.0)) < 1e-6
    assert abs(_as_float(report["partial_fill_ratio"]) - (1.0 / 3.0)) < 1e-6
    assert _as_bool(gate["pass"]) is False
    reason_codes = _as_text_list(gate["reason_codes"])
    assert "no_fill_ratio_limit_breach" in reason_codes
    assert "no_fill_ratio_delta_limit_breach" in reason_codes
    assert "partial_fill_ratio_delta_limit_breach" in reason_codes


def test_dual_eval_profiles_parse_fill_ratio_without_status() -> None:
    result = evaluate_dual_eval_profiles(
        records=[
            {"symbol": "600000.SH", "fill_ratio": 1.0},
            {"symbol": "000001.SZ", "fill_ratio": 0.5},
            {"symbol": "300750.SZ", "fill_ratio": 0.0},
        ],
        profile_set_id="dual_eval_v1",
        no_fill_ratio_limit=0.40,
        partial_fill_ratio_limit=0.40,
        no_fill_ratio_delta_limit=0.20,
        partial_fill_ratio_delta_limit=0.20,
        baseline_no_fill_ratio=0.10,
        baseline_partial_fill_ratio=0.10,
    )
    profiles = _as_mapping(result.report["profiles"])
    trading = _as_mapping(profiles["trading_eval_profile"])
    assert _as_int(trading["samples"]) == 3
    assert _as_int(trading["no_fill_count"]) == 1
    assert _as_int(trading["partial_fill_count"]) == 1
    assert _as_int(trading["full_fill_count"]) == 1
