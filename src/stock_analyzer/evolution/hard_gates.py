"""Automated hard-gate checks for evolution run."""

from __future__ import annotations

from collections.abc import Mapping


def evaluate_hard_gates(
    *,
    module_details: Mapping[str, object],
    min_samples_per_bucket: int,
) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    eval_profiles = module_details.get("eval_profiles", {})
    if not isinstance(eval_profiles, Mapping):
        eval_profiles = {}
    trading_gate = eval_profiles.get("trading_distribution_gate", {})
    if not isinstance(trading_gate, Mapping):
        trading_gate = {}
    trading_pass = bool(trading_gate.get("pass", True))
    checks.append(
        {
            "gate": "trading_fill_distribution",
            "passed": trading_pass,
            "reason_codes": (
                list(trading_gate.get("reason_codes", []))
                if isinstance(trading_gate.get("reason_codes"), list)
                else []
            ),
        }
    )

    utility = module_details.get("utility_execution", {})
    if not isinstance(utility, Mapping):
        utility = {}
    k_base = _as_int(utility.get("k_base"), default=0)
    k_dynamic = _as_int(utility.get("k_dynamic"), default=0)
    trim_reason_codes = (
        [str(item) for item in utility.get("trim_reason_codes", []) if isinstance(item, str)]
        if isinstance(utility.get("trim_reason_codes"), list)
        else []
    )
    dynamic_k_audit_pass = True
    dynamic_k_reasons: list[str] = []
    if k_base > 0 and 0 < k_dynamic < k_base and not trim_reason_codes:
        dynamic_k_audit_pass = False
        dynamic_k_reasons.append("missing_trim_reason_codes")
    checks.append(
        {
            "gate": "dynamic_k_audit",
            "passed": dynamic_k_audit_pass,
            "reason_codes": dynamic_k_reasons,
        }
    )

    bucket_sample_count = _as_int(utility.get("bucket_sample_count"), default=0)
    mapping_level_used = str(utility.get("mapping_level_used", ""))
    mapping_steps = (
        [str(item) for item in utility.get("mapping_fallback_steps", []) if isinstance(item, str)]
        if isinstance(utility.get("mapping_fallback_steps"), list)
        else []
    )
    mapping_pass = True
    mapping_reasons: list[str] = []
    if bucket_sample_count > 0 and bucket_sample_count < max(1, int(min_samples_per_bucket)):
        if mapping_level_used == "regime_x_liquidity_x_volatility":
            mapping_pass = False
            mapping_reasons.append("insufficient_bucket_without_fallback")
        if not any("bucket_min_samples" in step for step in mapping_steps):
            mapping_pass = False
            mapping_reasons.append("missing_bucket_min_samples_trace")
    checks.append(
        {
            "gate": "mapping_min_samples",
            "passed": mapping_pass,
            "reason_codes": mapping_reasons,
        }
    )

    reconcile = module_details.get("reconcile_drift", {})
    if not isinstance(reconcile, Mapping):
        reconcile = {}
    required = [
        "target_vs_filled_weight_p50",
        "target_vs_filled_weight_p90",
        "target_vs_filled_weight_max",
        "filled_vs_eod_weight_p50",
        "filled_vs_eod_weight_p90",
        "filled_vs_eod_weight_max",
        "position_drift_ratio",
    ]
    reconcile_pass = True
    reconcile_reasons: list[str] = []
    if _as_int(reconcile.get("valid_rows"), default=0) > 0:
        for key in required:
            if key not in reconcile:
                reconcile_pass = False
                reconcile_reasons.append(f"missing_{key}")
    checks.append(
        {
            "gate": "reconcile_drift_audit",
            "passed": reconcile_pass,
            "reason_codes": reconcile_reasons,
        }
    )

    failed_checks = [item for item in checks if not bool(item.get("passed", False))]
    return {
        "all_passed": len(failed_checks) == 0,
        "failed_gate_count": len(failed_checks),
        "failed_gates": [str(item.get("gate", "")) for item in failed_checks],
        "checks": checks,
    }


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            return default
    return default
