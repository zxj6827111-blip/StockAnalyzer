"""Pre-registered independent confirmation for one candidate and three controls.

This module consumes final, mature trade returns and the daily return series emitted by
the fixed-cash account ledger. It does not select a candidate or attest that a window was
unopened; those facts must be supplied and are enforced as explicit release gates.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist
from typing import Any

import numpy as np

CONFIRMATORY_CONTROLS = ("same_day_style", "legacy_same_pool", "legacy_full_policy")
CONFIRMATORY_ARMS = ("candidate", *CONFIRMATORY_CONTROLS)
BOOTSTRAP_DRAWS = 20_000
BOOTSTRAP_SEED = 20261010
MAIN_BLOCK_LENGTH = 10
SENSITIVITY_BLOCK_LENGTHS = (5, 20)
FAMILY_SIZE = 8
BONFERRONI_ALPHA = 0.05 / FAMILY_SIZE
MIN_TEST_SESSIONS = 120
MIN_SESSIONS_PER_FOLD = 30
MIN_TEST_FOLDS = 4
MIN_MATURED_FILLS_PER_ARM = 300
MIN_PROFITABILITY_IMPROVEMENT = 0.05
P05_NONINFERIORITY_MARGIN = -0.02
POWER_TARGET = 0.80


@dataclass(frozen=True)
class ConfirmatorySession:
    """One complete trading-session outcome for one arm."""

    trade_date: date
    arm_id: str
    fold_id: int
    session_index: int
    net_returns: tuple[float, ...]
    account_daily_return: float


def estimate_development_power_sample_size(
    candidate: Sequence[ConfirmatorySession],
    control: Sequence[ConfirmatorySession],
    *,
    target_effect: float = MIN_PROFITABILITY_IMPROVEMENT,
    block_length: int = MAIN_BLOCK_LENGTH,
    alpha: float = BONFERRONI_ALPHA,
    power: float = POWER_TARGET,
) -> dict[str, Any]:
    """Estimate complete sessions needed from development-only 10-session block variance.

    The estimate uses non-overlapping blocks within each uninterrupted fold segment. It
    is a planning estimate, not a pass result; fewer than two observed blocks is blocked.
    """
    if target_effect <= 0 or block_length < 1 or not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("invalid power-analysis parameters")
    paired, blockers = _pair_sessions(candidate, control)
    if blockers:
        return {"status": "blocked", "blockers": blockers}
    if any(row.arm_id != "candidate" for row in candidate) or any(
        row.arm_id not in CONFIRMATORY_CONTROLS for row in control
    ):
        return {"status": "blocked", "blockers": ["development_arm_identity_mismatch"]}
    segments = _segments(paired)
    block_differences: list[float] = []
    unestimable_blocks = 0
    for segment in segments:
        complete = len(segment) // block_length
        for block_index in range(complete):
            block = segment[block_index * block_length : (block_index + 1) * block_length]
            cand_wins = sum(value > 0 for row in block for value in row[0].net_returns)
            cand_count = sum(len(row[0].net_returns) for row in block)
            ctrl_wins = sum(value > 0 for row in block for value in row[1].net_returns)
            ctrl_count = sum(len(row[1].net_returns) for row in block)
            if cand_count and ctrl_count:
                block_differences.append(cand_wins / cand_count - ctrl_wins / ctrl_count)
            else:
                unestimable_blocks += 1
    if unestimable_blocks:
        return {
            "status": "blocked",
            "blockers": ["development_block_has_no_matured_fills_in_one_or_both_arms"],
            "unestimable_development_blocks": unestimable_blocks,
            "complete_development_blocks": len(block_differences),
        }
    if len(block_differences) < 2:
        return {
            "status": "blocked",
            "blockers": ["fewer_than_two_complete_development_blocks_for_variance"],
            "complete_development_blocks": len(block_differences),
        }
    block_sd = float(np.std(np.asarray(block_differences, dtype=float), ddof=1))
    z_alpha = NormalDist().inv_cdf(1.0 - alpha)
    z_power = NormalDist().inv_cdf(power)
    if block_sd == 0.0:
        required_blocks = 1
    else:
        required_blocks = math.ceil(((z_alpha + z_power) * block_sd / target_effect) ** 2)
    required_blocks = max(1, required_blocks)
    return {
        "status": "estimated",
        "estimand": "paired_matured_trade_profitability_rate_difference",
        "development_only": True,
        "development_sessions": len(paired),
        "complete_development_blocks": len(block_differences),
        "block_length_sessions": int(block_length),
        "block_difference_sd": block_sd,
        "target_effect": float(target_effect),
        "one_sided_alpha": float(alpha),
        "power": float(power),
        "required_complete_blocks": required_blocks,
        "estimated_sessions": required_blocks * int(block_length),
        "minimum_sessions": MIN_TEST_SESSIONS,
        "required_sessions": max(MIN_TEST_SESSIONS, required_blocks * int(block_length)),
        "method": "normal_approximation_on_nonoverlapping_paired_block_differences",
    }


def evaluate_independent_confirmation(
    *,
    sessions: Sequence[ConfirmatorySession],
    trading_calendar: Sequence[date],
    candidate_frozen_date: date,
    unopened_window_attested: bool,
    candidate_artifact_sha256: str,
    required_power_sessions: int,
    bootstrap_draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Evaluate the fixed 8-test family using paired moving-block resampling.

    Every arm must provide the same complete calendar with real fold/session identities.
    All interval lower bounds use one-sided Bonferroni alpha 0.05/8. The 5/20-session
    sensitivity checks must support the same gate as the 10-session primary analysis.
    This function evaluates statistics only; provenance and release gates belong to
    the command-line orchestrator and cannot be inferred from the statistical result.
    """
    days = list(trading_calendar)
    if not days or days != sorted(set(days)):
        return _blocked_report(["trading_calendar_must_be_nonempty_unique_and_sorted"])
    if not re.fullmatch(r"[0-9a-f]{64}", candidate_artifact_sha256):
        return _blocked_report(["candidate_artifact_identity_missing"])
    if candidate_frozen_date >= days[0]:
        return _blocked_report(["confirmation_window_does_not_start_after_candidate_freeze"])
    if not unopened_window_attested:
        return _blocked_report(["independent_window_not_attested_unopened"])
    if required_power_sessions < MIN_TEST_SESSIONS:
        return _blocked_report(["development_power_sample_size_missing_or_below_minimum"])
    if bootstrap_draws < 1:
        return _blocked_report(["bootstrap_draws_must_be_positive"])

    by_arm: dict[str, dict[date, ConfirmatorySession]] = defaultdict(dict)
    blockers: list[str] = []
    for row in sessions:
        if row.arm_id not in CONFIRMATORY_ARMS:
            blockers.append(f"unregistered_arm:{row.arm_id}")
            continue
        if row.trade_date in by_arm[row.arm_id]:
            blockers.append(f"duplicate_arm_session:{row.arm_id}:{row.trade_date}")
            continue
        if (
            row.trade_date not in days
            or row.fold_id < 0
            or row.session_index < 0
            or not math.isfinite(float(row.account_daily_return))
        ):
            blockers.append(f"invalid_session_identity_or_return:{row.arm_id}:{row.trade_date}")
            continue
        if any(not math.isfinite(float(value)) for value in row.net_returns):
            blockers.append(f"non_finite_trade_return:{row.arm_id}:{row.trade_date}")
            continue
        by_arm[row.arm_id][row.trade_date] = row
    for arm_id in CONFIRMATORY_ARMS:
        missing = [day.isoformat() for day in days if day not in by_arm[arm_id]]
        if missing:
            blockers.append(f"incomplete_calendar:{arm_id}:{','.join(missing[:5])}")
    if blockers:
        return _blocked_report(blockers)

    paired = [tuple(by_arm[arm_id][day] for arm_id in CONFIRMATORY_ARMS) for day in days]
    for rows in paired:
        identity = (rows[0].fold_id, rows[0].session_index)
        if any((row.fold_id, row.session_index) != identity for row in rows[1:]):
            return _blocked_report(["paired_arms_have_different_fold_or_session_identity"])
    sequence_blockers = _session_sequence_blockers([rows[0] for rows in paired])
    if sequence_blockers:
        return _blocked_report(sequence_blockers)
    segments = _segments_four_arm(paired)
    fold_counts: dict[int, int] = defaultdict(int)
    for rows in paired:
        fold_counts[rows[0].fold_id] += 1
    if len(paired) < MIN_TEST_SESSIONS:
        blockers.append(f"complete_sessions_below_{MIN_TEST_SESSIONS}")
    if len(fold_counts) < MIN_TEST_FOLDS:
        blockers.append(f"test_folds_below_{MIN_TEST_FOLDS}")
    if any(count < MIN_SESSIONS_PER_FOLD for count in fold_counts.values()):
        blockers.append(f"test_fold_sessions_below_{MIN_SESSIONS_PER_FOLD}")
    trade_counts = {
        arm_id: sum(len(by_arm[arm_id][day].net_returns) for day in days)
        for arm_id in CONFIRMATORY_ARMS
    }
    for arm_id, count in trade_counts.items():
        if count < MIN_MATURED_FILLS_PER_ARM:
            blockers.append(f"matured_fills_below_{MIN_MATURED_FILLS_PER_ARM}:{arm_id}")
    if len(paired) < int(required_power_sessions):
        blockers.append(f"sessions_below_development_power_requirement_{required_power_sessions}")
    if blockers:
        return {
            **_blocked_report(blockers),
            "complete_sessions": len(paired),
            "matured_fills_by_arm": trade_counts,
            "test_sessions_per_fold": dict(sorted(fold_counts.items())),
        }

    point = _point_metrics(paired)
    distributions: dict[int, list[dict[str, float]]] = {}
    for block_length in (MAIN_BLOCK_LENGTH, *SENSITIVITY_BLOCK_LENGTHS):
        distributions[block_length], failure = _bootstrap_metrics(
            segments,
            block_length=block_length,
            draws=bootstrap_draws,
            seed=seed,
        )
        if failure:
            return _blocked_report([f"bootstrap_block_{block_length}:{failure}"])

    test_specs = _test_specs(point)
    test_results: dict[str, Any] = {}
    primary_failed = False
    sensitivity_conflict = False
    for test_name, (estimate, threshold) in test_specs.items():
        primary = _lower_bound(distributions[MAIN_BLOCK_LENGTH], test_name)
        sensitivity = {
            str(length): _lower_bound(distributions[length], test_name)
            for length in SENSITIVITY_BLOCK_LENGTHS
        }
        primary_passed = estimate > threshold and primary > threshold
        sensitivity_passed = [bound > threshold for bound in sensitivity.values()]
        passed = primary_passed
        if not passed:
            primary_failed = True
        conclusions = [primary_passed, *sensitivity_passed]
        if any(conclusions) and not all(conclusions):
            sensitivity_conflict = True
        test_results[test_name] = {
            "estimate": estimate,
            "required_point_threshold": threshold,
            "one_sided_lower_bound_99_375": primary,
            "sensitivity_lower_bounds": sensitivity,
            "passed_primary_gate": passed,
            "passed_all_sensitivity_gates": all(
                bound > threshold for bound in sensitivity.values()
            ),
        }
    state = "quality_failed" if primary_failed else "quality_passed"
    if sensitivity_conflict:
        state = "sample_insufficient"
    return {
        "schema": "tail_independent_confirmation.v1",
        "state": state,
        "passed": state == "quality_passed",
        "scope": "statistical_protocol_only",
        "release_eligible": False,
        "candidate_artifact_sha256": candidate_artifact_sha256,
        "candidate_frozen_date": candidate_frozen_date.isoformat(),
        "unopened_window_attested": bool(unopened_window_attested),
        "window": {
            "first_session": days[0].isoformat(),
            "last_session": days[-1].isoformat(),
            "complete_sessions": len(days),
            "folds": dict(sorted(fold_counts.items())),
            "matured_fills_by_arm": trade_counts,
            "required_power_sessions": int(required_power_sessions),
        },
        "statistical_protocol": {
            "family_size": FAMILY_SIZE,
            "bonferroni_alpha_per_test": BONFERRONI_ALPHA,
            "one_sided_lower_bound_percentile": 100.0 * BONFERRONI_ALPHA,
            "draws": int(bootstrap_draws),
            "seed": int(seed),
            "bit_generator": "PCG64",
            "block_lengths_sessions": [MAIN_BLOCK_LENGTH, *SENSITIVITY_BLOCK_LENGTHS],
            "resampling": "paired_moving_blocks_within_fold_and_contiguous_session_run",
        },
        "tests": test_results,
        "note": ("工程结果不能自行证明 unopened attestation；输入必须来自已冻结候选和独立窗口。"),
    }


def _pair_sessions(
    candidate: Sequence[ConfirmatorySession], control: Sequence[ConfirmatorySession]
) -> tuple[list[tuple[ConfirmatorySession, ConfirmatorySession]], list[str]]:
    candidate_by_date = {row.trade_date: row for row in candidate}
    control_by_date = {row.trade_date: row for row in control}
    if len(candidate_by_date) != len(candidate) or len(control_by_date) != len(control):
        return [], ["duplicate_development_session"]
    if set(candidate_by_date) != set(control_by_date):
        return [], ["development_arms_have_unpaired_sessions"]
    pairs = [(candidate_by_date[day], control_by_date[day]) for day in sorted(candidate_by_date)]
    if any(
        (left.fold_id, left.session_index) != (right.fold_id, right.session_index)
        for left, right in pairs
    ):
        return [], ["development_arms_have_mismatched_session_identity"]
    sequence_blockers = _session_sequence_blockers([pair[0] for pair in pairs])
    if sequence_blockers:
        return [], sequence_blockers
    return pairs, []


def _session_sequence_blockers(rows: Sequence[ConfirmatorySession]) -> list[str]:
    seen_folds: set[int] = set()
    previous: ConfirmatorySession | None = None
    for row in rows:
        if previous is None:
            seen_folds.add(row.fold_id)
        elif row.fold_id != previous.fold_id:
            if row.fold_id in seen_folds:
                return ["fold_identity_reappears_after_a_later_fold"]
            seen_folds.add(row.fold_id)
        elif row.session_index != previous.session_index + 1:
            return ["session_indices_are_not_contiguous_within_fold"]
        previous = row
    return []


def _segments(
    paired: Sequence[tuple[ConfirmatorySession, ConfirmatorySession]],
) -> list[list[tuple[ConfirmatorySession, ConfirmatorySession]]]:
    segments: list[list[tuple[ConfirmatorySession, ConfirmatorySession]]] = []
    for pair in paired:
        if (
            not segments
            or pair[0].fold_id != segments[-1][-1][0].fold_id
            or pair[0].session_index != segments[-1][-1][0].session_index + 1
        ):
            segments.append([pair])
        else:
            segments[-1].append(pair)
    return segments


def _segments_four_arm(
    paired: Sequence[tuple[ConfirmatorySession, ...]],
) -> list[list[tuple[ConfirmatorySession, ...]]]:
    segments: list[list[tuple[ConfirmatorySession, ...]]] = []
    for rows in paired:
        if (
            not segments
            or rows[0].fold_id != segments[-1][-1][0].fold_id
            or rows[0].session_index != segments[-1][-1][0].session_index + 1
        ):
            segments.append([rows])
        else:
            segments[-1].append(rows)
    return segments


def _point_metrics(
    paired: Sequence[tuple[ConfirmatorySession, ...]],
) -> dict[str, float]:
    metrics: dict[str, float] = {}
    all_returns: dict[str, list[float]] = {arm: [] for arm in CONFIRMATORY_ARMS}
    for index, arm_id in enumerate(CONFIRMATORY_ARMS):
        all_returns[arm_id] = [value for rows in paired for value in rows[index].net_returns]
    for control in CONFIRMATORY_CONTROLS:
        candidate_returns = all_returns["candidate"]
        control_returns = all_returns[control]
        if not candidate_returns or not control_returns:
            continue
        metrics[f"profitability_rate:{control}"] = sum(
            value > 0 for value in candidate_returns
        ) / len(candidate_returns) - sum(value > 0 for value in control_returns) / len(
            control_returns
        )
        metrics[f"p05_difference:{control}"] = float(np.percentile(candidate_returns, 5)) - float(
            np.percentile(control_returns, 5)
        )
    if all_returns["candidate"]:
        metrics["mean_net_return:candidate"] = float(np.mean(all_returns["candidate"]))
    metrics["account_daily_mean:candidate"] = float(
        np.mean([rows[0].account_daily_return for rows in paired])
    )
    return metrics


def _test_specs(point: dict[str, float]) -> dict[str, tuple[float, float]]:
    specs = {
        f"profitability_rate:{control}": (
            point.get(f"profitability_rate:{control}", math.nan),
            MIN_PROFITABILITY_IMPROVEMENT,
        )
        for control in CONFIRMATORY_CONTROLS
    }
    specs["mean_net_return:candidate"] = (point.get("mean_net_return:candidate", math.nan), 0.0)
    specs["account_daily_mean:candidate"] = (
        point.get("account_daily_mean:candidate", math.nan),
        0.0,
    )
    specs.update(
        {
            f"p05_difference:{control}": (
                point.get(f"p05_difference:{control}", math.nan),
                P05_NONINFERIORITY_MARGIN,
            )
            for control in CONFIRMATORY_CONTROLS
        }
    )
    return specs


def _bootstrap_metrics(
    segments: Sequence[Sequence[tuple[ConfirmatorySession, ...]]],
    *,
    block_length: int,
    draws: int,
    seed: int,
) -> tuple[list[dict[str, float]], str | None]:
    if any(len(segment) < block_length for segment in segments):
        return [], "a_contiguous_segment_is_shorter_than_the_block_length"
    rng = np.random.Generator(np.random.PCG64(seed))
    distributions: list[dict[str, float]] = []
    for _ in range(int(draws)):
        sampled: list[tuple[ConfirmatorySession, ...]] = []
        for segment in segments:
            starts: Any = np.arange(len(segment) - block_length + 1, dtype=np.int64)
            indices: list[int] = []
            while len(indices) < len(segment):
                start = int(rng.choice(starts))
                indices.extend(range(start, start + block_length))
            sampled.extend(segment[index] for index in indices[: len(segment)])
        metrics = _point_metrics(sampled)
        if len(metrics) != FAMILY_SIZE or not all(
            math.isfinite(value) for value in metrics.values()
        ):
            return [], "bootstrap_replicate_has_an_empty_or_non_finite_metric"
        distributions.append(metrics)
    return distributions, None


def _lower_bound(distributions: Sequence[dict[str, float]], metric: str) -> float:
    values = np.asarray([row[metric] for row in distributions], dtype=float)
    return float(np.quantile(values, BONFERRONI_ALPHA, method="linear"))


def _blocked_report(blockers: Sequence[str]) -> dict[str, Any]:
    return {
        "schema": "tail_independent_confirmation.v1",
        "state": "engineering_blocked",
        "passed": False,
        "scope": "statistical_protocol_only",
        "release_eligible": False,
        "blockers": list(blockers),
    }


__all__ = [
    "BONFERRONI_ALPHA",
    "BOOTSTRAP_DRAWS",
    "BOOTSTRAP_SEED",
    "CONFIRMATORY_ARMS",
    "ConfirmatorySession",
    "evaluate_independent_confirmation",
    "estimate_development_power_sample_size",
]
