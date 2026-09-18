"""S23 Theme / News / Intraday 增量实验框架阶段验收测试。

守住：成功判据只有同日配对超额；News/Intraday 前置不足必须阻断；
"出票更多"在结构上不可能成为证据。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.alpha_v2.research.experiments import (
    DEFAULT_MIN_AFFECTED_DATES,
    DEFAULT_MIN_AFFECTED_SYMBOL_DATES,
    DEFAULT_MIN_TRADING_DATES,
    EXPERIMENT_INTRAADAY,
    EXPERIMENT_NEWS,
    EXPERIMENT_THEME,
    STATUS_BLOCKED,
    STATUS_INSUFFICIENT_SAMPLE,
    STATUS_OK,
    ExperimentGate,
    IncrementalExperimentSpec,
    ReadinessEvidence,
    assert_never_uses_ticket_count,
    default_readiness,
    intraday_experiment_spec,
    news_experiment_spec,
    readiness_block,
    run_incremental_experiment,
    theme_experiment_spec,
)


def _frame(
    *,
    days: int = 80,
    per_day: int = 40,
    affected_share: float = 0.3,
    signal_gain: float = 0.01,
    seed: int = 616,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for day_index in range(days):
        day = f"2026-{(day_index // 28) + 1:02d}-{(day_index % 28) + 1:02d}"
        base = rng.normal(0.0, 0.02, per_day)
        affected = rng.random(per_day) < affected_share
        excess = base + np.where(affected, signal_gain, 0.0)
        for index in range(per_day):
            rows.append(
                {
                    "decision_date": day,
                    "symbol": f"{600000 + index:06d}",
                    "executable": True,
                    "maturity_date_5d": day,
                    "excess_return_5d": float(excess[index]),
                    "alpha_rank_score": float(pd.Series(base).rank(pct=True).iloc[index]),
                    "alpha_rank_score_with_theme": float(
                        pd.Series(np.where(affected, excess, base)).rank(pct=True).iloc[index]
                    ),
                    "affected": bool(affected[index]),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 样本门与前肢条件
# ---------------------------------------------------------------------------


def test_default_gate_matches_blueprint_numbers() -> None:
    gate = ExperimentGate()
    assert gate.min_trading_dates == DEFAULT_MIN_TRADING_DATES == 60
    assert gate.min_affected_dates == DEFAULT_MIN_AFFECTED_DATES == 30
    assert gate.min_affected_symbol_dates == DEFAULT_MIN_AFFECTED_SYMBOL_DATES == 200


def test_news_is_blocked_without_pit_path() -> None:
    payload = readiness_block(kind=EXPERIMENT_NEWS, evidence={"pit_path_verified": False})
    assert payload["status"] == STATUS_BLOCKED
    assert "news_pit_path_incomplete" in payload["blocked_reasons"]


def test_news_ready_once_pit_path_verified() -> None:
    payload = readiness_block(kind=EXPERIMENT_NEWS, evidence={"pit_path_verified": True})
    assert payload["status"] == STATUS_OK
    assert payload["blocked_reasons"] == []


def test_intraday_blocked_until_coverage_freshness_and_pit_all_verified() -> None:
    partial = readiness_block(
        kind=EXPERIMENT_INTRAADAY,
        evidence={
            "coverage_verified": True,
            "freshness_verified": False,
            "pit_path_verified": True,
        },
    )
    assert partial["status"] == STATUS_BLOCKED
    assert "intraday_freshness_unverified" in partial["blocked_reasons"]
    full = readiness_block(
        kind=EXPERIMENT_INTRAADAY,
        evidence={"coverage_verified": True, "freshness_verified": True, "pit_path_verified": True},
    )
    assert full["status"] == STATUS_OK


def test_zero_fill_mixing_is_explicitly_forbidden() -> None:
    payload = readiness_block(
        kind=EXPERIMENT_INTRAADAY,
        evidence={
            "coverage_verified": True,
            "freshness_verified": True,
            "pit_path_verified": True,
            "zero_fill_mixing": True,
        },
    )
    assert payload["status"] == STATUS_BLOCKED
    assert "intraday_zero_fill_mixing_forbidden" in payload["blocked_reasons"]


def test_default_readiness_reflects_current_repo_state() -> None:
    payload = default_readiness()
    assert payload[EXPERIMENT_THEME]["status"] == STATUS_OK
    assert payload[EXPERIMENT_NEWS]["status"] == STATUS_BLOCKED
    assert payload[EXPERIMENT_INTRAADAY]["status"] == STATUS_BLOCKED


# ---------------------------------------------------------------------------
# 实验执行
# ---------------------------------------------------------------------------


def test_blocked_experiment_returns_no_numbers() -> None:
    payload = run_incremental_experiment(_frame(), spec=news_experiment_spec())
    assert payload["status"] == STATUS_BLOCKED
    assert payload["reason"] == "readiness_evidence_incomplete"
    assert "paired_excess" not in payload


def test_theme_experiment_runs_and_reports_paired_excess() -> None:
    payload = run_incremental_experiment(
        _frame(days=80, affected_share=0.5, signal_gain=0.02),
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert payload["status"] == STATUS_OK
    assert payload["gate_pass"] is True
    assert payload["arms_size_equal"] is True
    assert payload["selection_size_per_arm"] == 5
    assert payload["paired_excess"]["status"] == "ok"
    assert payload["paired_excess"]["days"] == 80
    assert "affected_dates" in payload


def test_experiment_arms_have_identical_selection_size() -> None:
    """结构守卫：两臂规模相同 → "出票变多"不可能是成功解释。"""
    payload = run_incremental_experiment(
        _frame(days=80),
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert payload["arms_size_equal"] is True
    assert payload["selection_size_per_arm"] == 5


def test_insufficient_sample_is_flagged_not_passed() -> None:
    payload = run_incremental_experiment(
        _frame(days=10, per_day=10, affected_share=0.1),
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert payload["status"] == STATUS_INSUFFICIENT_SAMPLE
    assert payload["gate_pass"] is False
    assert payload["verdict"] == "awaiting_sample"


def test_zero_effect_experiment_is_inconclusive() -> None:
    frame = _frame(days=80)
    frame["alpha_rank_score_with_theme"] = frame["alpha_rank_score"]  # 信号无任何影响
    payload = run_incremental_experiment(
        frame,
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert payload["affected_symbol_dates"] == 0
    assert payload["gate_pass"] is False
    assert payload["verdict"] in {"awaiting_sample", "inconclusive"}


def test_experiment_missing_columns_is_no_data() -> None:
    frame = _frame().drop(columns=["alpha_rank_score_with_theme"])
    payload = run_incremental_experiment(
        frame,
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert payload["status"] == "no_data"
    assert payload["missing_columns"] == ["alpha_rank_score_with_theme"]


def test_experiment_excludes_non_executable_rows() -> None:
    frame = _frame(days=80)
    frame["executable"] = False
    payload = run_incremental_experiment(
        frame,
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert payload["status"] == "no_data"
    assert payload["reason"] == "no_usable_rows"


def test_experiment_empty_frame_is_no_data() -> None:
    payload = run_incremental_experiment(
        pd.DataFrame(),
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert payload["status"] == "no_data"


# ---------------------------------------------------------------------------
# 判据守卫
# ---------------------------------------------------------------------------


def test_spec_declares_paired_excess_as_only_success_criterion() -> None:
    payload = IncrementalExperimentSpec().to_payload()
    assert payload["success_criterion"] == "same_day_paired_excess"
    assert "出票数量" in payload["forbidden_success_evidence"]
    assert payload["enabled"] is False
    assert payload["mode"] == "shadow"


def test_assert_never_uses_ticket_count_requires_paired_criterion() -> None:
    good = run_incremental_experiment(
        _frame(days=80),
        spec=theme_experiment_spec(),
        evidence=ReadinessEvidence(kind=EXPERIMENT_THEME, pit_path_verified=True),
    )
    assert_never_uses_ticket_count(good)
    broken = dict(good)
    broken["spec"] = {"success_criterion": "more_tickets"}
    with pytest.raises(AssertionError, match="same_day_paired_excess"):
        assert_never_uses_ticket_count(broken)


def test_assert_never_uses_ticket_count_rejects_ok_without_gate() -> None:
    with pytest.raises(AssertionError, match="样本门未过"):
        assert_never_uses_ticket_count(
            {
                "spec": {"success_criterion": "same_day_paired_excess"},
                "status": STATUS_OK,
                "gate_pass": False,
            }
        )


def test_all_three_experiment_specs_stay_shadow_and_disabled() -> None:
    for spec in (theme_experiment_spec(), news_experiment_spec(), intraday_experiment_spec()):
        payload = spec.to_payload()
        assert payload["enabled"] is False
        assert payload["mode"] == "shadow"


def test_intraday_experiment_blocked_on_current_repo_state() -> None:
    payload = run_incremental_experiment(_frame(), spec=intraday_experiment_spec())
    assert payload["status"] == STATUS_BLOCKED
    assert set(payload["readiness"]["blocked_reasons"]) >= {
        "intraday_coverage_unverified",
        "intraday_freshness_unverified",
        "intraday_pit_unverified",
    }
