"""P2-3 news_risk_mode 渐进启用框架（off|shadow|penalty|conditional_veto）。"""

from __future__ import annotations

from stock_analyzer.news.risk_mode import (
    PENALTY_MIN_CONFIDENCE,
    SHADOW_MIN_EVENTS,
    SHADOW_MIN_TRADE_DAYS,
    VETO_MIN_CONFIDENCE,
    ShadowStats,
    evaluate_news_risk_mode,
    shadow_stats_ready,
)


def _decision(
    *,
    available: bool = True,
    score: float = 0.8,
    max_negative_confidence: float = 0.0,
    hard_veto: bool = False,
    event_types: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict[str, object]:
    return {
        "score": score,
        "available_for_symbol": available,
        "hard_veto": hard_veto,
        "max_negative_confidence": max_negative_confidence,
        "matched_event_ids": ["evt-1"],
        "event_types": event_types or [],
        "sources": sources or [],
        "reasons": [],
    }


def test_off_mode_ignores_news_risk() -> None:
    outcome = evaluate_news_risk_mode(
        mode_value="off",
        decision=_decision(score=0.1, max_negative_confidence=0.9, hard_veto=True),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome.mode == "off"
    assert outcome.applied is False
    assert outcome.action == "none"
    assert outcome.hard_veto is False


def test_shadow_mode_records_but_does_not_apply() -> None:
    outcome = evaluate_news_risk_mode(
        mode_value="shadow",
        decision=_decision(score=0.1, max_negative_confidence=0.9, hard_veto=True),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome.mode == "shadow"
    assert outcome.applied is False
    assert outcome.action == "shadow_record"
    assert outcome.penalty_amount == 0.0


def test_penalty_mode_strong_negative_high_confidence() -> None:
    outcome = evaluate_news_risk_mode(
        mode_value="penalty",
        decision=_decision(score=0.2, max_negative_confidence=0.75),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome.applied is True
    assert outcome.action == "penalty"
    assert outcome.penalty_amount > 0.0
    assert outcome.hard_veto is False


def test_penalty_mode_low_confidence_no_penalty() -> None:
    outcome = evaluate_news_risk_mode(
        mode_value="penalty",
        decision=_decision(score=0.2, max_negative_confidence=0.3),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome.applied is False
    assert outcome.action == "none"
    assert outcome.penalty_amount == 0.0


def test_conditional_veto_requires_whitelist_and_sources() -> None:
    # 无事件类型白名单 + 无来源 → 不否决
    outcome = evaluate_news_risk_mode(
        mode_value="conditional_veto",
        decision=_decision(
            score=0.1,
            max_negative_confidence=0.9,
            hard_veto=True,
            event_types=[],
            sources=[],
        ),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome.hard_veto is False
    assert "not_whitelisted" in outcome.reasons

    # 命中白名单但来源不足 → 不否决
    outcome2 = evaluate_news_risk_mode(
        mode_value="conditional_veto",
        decision=_decision(
            score=0.1,
            max_negative_confidence=0.9,
            hard_veto=True,
            event_types=["立案调查"],
            sources=["自媒体"],
        ),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome2.hard_veto is False
    assert "source_not_sufficient" in outcome2.reasons


def test_conditional_veto_applies_with_official_source() -> None:
    outcome = evaluate_news_risk_mode(
        mode_value="conditional_veto",
        decision=_decision(
            score=0.1,
            max_negative_confidence=0.9,
            hard_veto=True,
            event_types=["立案调查"],
            sources=["证监会"],
        ),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome.applied is True
    assert outcome.action == "veto"
    assert outcome.hard_veto is True


def test_conditional_veto_requires_confidence_08() -> None:
    outcome = evaluate_news_risk_mode(
        mode_value="conditional_veto",
        decision=_decision(
            score=0.1,
            max_negative_confidence=0.7,  # < 0.8
            hard_veto=True,
            event_types=["立案调查"],
            sources=["证监会"],
        ),
        shadow_stats=None,
        llm_available=True,
    )
    assert outcome.hard_veto is False
    assert any(reason.startswith("confidence_below_veto") for reason in outcome.reasons)


def test_llm_unavailable_degrades_without_blocking() -> None:
    outcome = evaluate_news_risk_mode(
        mode_value="penalty",
        decision=_decision(score=0.1, max_negative_confidence=0.9),
        shadow_stats=None,
        llm_available=False,
    )
    assert outcome.degraded is True
    assert outcome.applied is False
    assert "llm_unavailable_degraded" in outcome.reasons


def test_shadow_stats_ready_gate() -> None:
    # 不满足门槛
    stats = ShadowStats(
        trade_days=5,
        valid_events=100,
        success_rate=0.9,
        human_agreement_rate=0.7,
        negative_recall=0.8,
        cost=10.0,
        avg_latency_ms=500.0,
        error_rate=0.1,
        avg_confidence=0.8,
    )
    assert stats.ready_for_penalty() is False
    assert shadow_stats_ready(stats)["ready_for_penalty"] is False

    # 满足门槛
    ready = ShadowStats(
        trade_days=12,
        valid_events=250,
        success_rate=0.96,
        human_agreement_rate=0.85,
        negative_recall=0.92,
        cost=30.0,
        avg_latency_ms=400.0,
        error_rate=0.02,
        avg_confidence=0.85,
    )
    assert ready.ready_for_penalty() is True
    assert shadow_stats_ready(ready)["ready_for_penalty"] is True


def test_shadow_constants() -> None:
    assert SHADOW_MIN_TRADE_DAYS == 10
    assert SHADOW_MIN_EVENTS == 200
    assert PENALTY_MIN_CONFIDENCE == 0.6
    assert VETO_MIN_CONFIDENCE == 0.8
