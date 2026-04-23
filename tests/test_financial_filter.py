from __future__ import annotations

import math

from stock_analyzer.config import FinancialFilterConfig
from stock_analyzer.filter.financial import FinancialRiskFilter


def test_financial_filter_blocks_trend_when_fundamentals_missing() -> None:
    filterer = FinancialRiskFilter(
        config=FinancialFilterConfig(
            enabled=True,
            missing_data_policy="reject",
            apply_to=["trend"],
            trend_mode="block",
        )
    )
    decision = filterer.evaluate(
        symbol="600000",
        strategy="trend",
        snapshot={"is_st": False, "is_delisting_risk": False},
    )
    assert decision.allowed is False
    assert "financial_filter:missing_roe" in decision.reasons
    assert "financial_filter:missing_debt_ratio" in decision.reasons


def test_financial_filter_allows_trend_when_fundamentals_good() -> None:
    filterer = FinancialRiskFilter(config=FinancialFilterConfig(enabled=True))
    decision = filterer.evaluate(
        symbol="600000",
        strategy="trend",
        snapshot={
            "is_st": False,
            "is_delisting_risk": False,
            "roe": 0.12,
            "debt_ratio": 0.42,
        },
    )
    assert decision.allowed is True
    assert decision.penalty_score == 0.0


def test_financial_filter_allows_trend_when_missing_data_policy_is_allow() -> None:
    filterer = FinancialRiskFilter(
        config=FinancialFilterConfig(
            enabled=True,
            missing_data_policy="allow",
            apply_to=["trend"],
        )
    )
    decision = filterer.evaluate(
        symbol="600000",
        strategy="trend",
        snapshot={"is_st": False, "is_delisting_risk": False},
    )
    assert decision.allowed is True
    assert decision.reasons == []


def test_financial_filter_penalizes_monster_mode() -> None:
    filterer = FinancialRiskFilter(
        config=FinancialFilterConfig(
            enabled=True,
            apply_to=["trend", "oversold"],
            monster_mode="score_penalty",
            monster_penalty=10.0,
        )
    )
    decision = filterer.evaluate(
        symbol="600000",
        strategy="monster",
        snapshot={
            "is_st": True,
            "is_delisting_risk": False,
            "roe": 0.01,
            "debt_ratio": 0.90,
        },
    )
    assert decision.allowed is True
    assert decision.penalty_score == 10.0
    assert any(reason.startswith("financial_penalty:") for reason in decision.reasons)


def test_financial_filter_penalizes_trend_mode() -> None:
    filterer = FinancialRiskFilter(
        config=FinancialFilterConfig(
            enabled=True,
            apply_to=["trend"],
            trend_mode="score_penalty",
            trend_penalty=6.0,
        )
    )
    decision = filterer.evaluate(
        symbol="600000",
        strategy="trend",
        snapshot={
            "is_st": False,
            "is_delisting_risk": False,
            "roe": 0.01,
            "debt_ratio": 0.90,
        },
    )
    assert decision.allowed is True
    assert decision.penalty_score == 6.0
    assert any(reason.startswith("financial_penalty:") for reason in decision.reasons)


def test_financial_filter_keeps_st_as_hard_block_in_trend_penalty_mode() -> None:
    filterer = FinancialRiskFilter(
        config=FinancialFilterConfig(
            enabled=True,
            apply_to=["trend"],
            trend_mode="score_penalty",
            trend_penalty=6.0,
        )
    )
    decision = filterer.evaluate(
        symbol="600000",
        strategy="trend",
        snapshot={
            "is_st": True,
            "is_delisting_risk": False,
            "roe": 0.01,
            "debt_ratio": 0.90,
        },
    )
    assert decision.allowed is False
    assert decision.penalty_score == 0.0
    assert "financial_filter:st" in decision.reasons


def test_financial_filter_treats_nan_as_missing_under_reject_policy() -> None:
    filterer = FinancialRiskFilter(
        config=FinancialFilterConfig(
            enabled=True,
            missing_data_policy="reject",
            apply_to=["trend"],
            trend_mode="block",
        )
    )
    decision = filterer.evaluate(
        symbol="600000",
        strategy="trend",
        snapshot={
            "is_st": False,
            "is_delisting_risk": False,
            "roe": math.nan,
            "debt_ratio": math.nan,
            "financial_data_complete": False,
        },
    )
    assert decision.allowed is False
    assert "financial_filter:missing_financial_data" in decision.reasons
    assert "financial_filter:missing_roe" in decision.reasons
    assert "financial_filter:missing_debt_ratio" in decision.reasons
