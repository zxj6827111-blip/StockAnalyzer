"""S08 Data Health 与 Market Breadth 分层门。

对应蓝图 §5 P0-08 / 阶段施工提示词 S08：

```text
Data Health: broken -> 禁止新买 / degraded -> 只记录 / healthy -> 才进入 Breadth
Market Breadth: weak -> 风险门 / normal·strong -> 正常
```

三条硬约束（本文件逐个钉住）：

1. **缺失 ≠ 健康**：任一数据缺失/无法验证都不得记 ok（Codex 复审要求，
   特别是 market_breadth artifact 缺失）；
2. **coverage 坏但 breadth 高分不得放行**（蓝图 §P0-08 严禁项）；
3. **灰度**：默认 enforce=False，只产报告不改决策。
"""

from __future__ import annotations

from datetime import date

import pytest

from stock_analyzer.ops.data_health import (
    CHECK_BOARD_COVERAGE,
    CHECK_BREADTH_ARTIFACT,
    CHECK_DEGRADED,
    CHECK_EXPECTED_ACTIVE_COVERAGE,
    CHECK_MODEL_IDENTITY_HEALTH,
    CHECK_PRICE_SERIES_AVAILABILITY,
    CHECK_TRADE_DATE_FRESHNESS,
    HEALTH_BROKEN,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    combined_gate_decision,
    evaluate_data_health,
)

AS_OF = date(2026, 9, 30)


def _healthy_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "as_of": AS_OF,
        "latest_trade_date": "2026-09-30",
        "universe_snapshot": {"expected_active_count": 100, "eligible_count": 110},
        "valid_symbol_count": 98,
        "board_coverage": {"主板": 0.99, "创业板": 0.97},
        "feature_snapshot": {"current": True, "coverage_ratio": 0.99},
        "model_identity": {"status": "match", "identity_verified": True},
        "price_contract": {"execution_uncertain": False, "execution_price_mode": "raw"},
        "breadth_artifact_present": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 健康口径
# ---------------------------------------------------------------------------


def test_all_facts_healthy_is_healthy() -> None:
    report = evaluate_data_health(**_healthy_kwargs())  # type: ignore[arg-type]
    assert report.status == HEALTH_HEALTHY
    assert report.broken_checks == ()
    assert report.degraded_checks == ()
    assert report.coverage_ratio == pytest.approx(0.98)


def test_missing_facts_are_never_healthy() -> None:
    """缺失一项都不能算健康：整体至少 degraded，且 missing_artifacts 可见。"""
    report = evaluate_data_health(
        as_of=AS_OF,
        latest_trade_date="2026-09-30",
        universe_snapshot={"expected_active_count": 100},
        valid_symbol_count=100,
        model_identity={"status": "match", "identity_verified": True},
        price_contract={"execution_uncertain": False},
    )
    assert report.status == HEALTH_DEGRADED
    assert "market_breadth_artifact" in report.missing_artifacts
    assert report.check(CHECK_BREADTH_ARTIFACT).status == CHECK_DEGRADED  # type: ignore[union-attr]


def test_missing_breadth_artifact_is_not_healthy() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(breadth_artifact_present=False)  # type: ignore[arg-type]
    )
    assert report.status == HEALTH_DEGRADED
    assert report.check(CHECK_BREADTH_ARTIFACT).status == CHECK_DEGRADED  # type: ignore[union-attr]


def test_stale_trade_date_is_broken() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(latest_trade_date="2026-09-20")  # type: ignore[arg-type]
    )
    assert report.status == HEALTH_BROKEN
    assert CHECK_TRADE_DATE_FRESHNESS in report.broken_checks


def test_low_expected_active_coverage_is_broken() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(valid_symbol_count=50)  # type: ignore[arg-type]
    )
    assert report.status == HEALTH_BROKEN
    assert CHECK_EXPECTED_ACTIVE_COVERAGE in report.broken_checks
    assert report.coverage_ratio == pytest.approx(0.5)


def test_zero_expected_active_is_broken_not_100_percent() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(  # type: ignore[arg-type]
            universe_snapshot={"expected_active_count": 0}, valid_symbol_count=0
        )
    )
    assert report.status == HEALTH_BROKEN
    assert report.coverage_ratio == 0.0


def test_model_identity_fail_closed_is_broken() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(  # type: ignore[arg-type]
            model_identity={
                "status": "mismatch",
                "identity_verified": False,
                "research_fail_closed": True,
            }
        )
    )
    assert report.status == HEALTH_BROKEN
    assert CHECK_MODEL_IDENTITY_HEALTH in report.broken_checks


def test_unverified_model_identity_is_degraded_not_ok() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(  # type: ignore[arg-type]
            model_identity={"status": "no_champion", "identity_verified": False}
        )
    )
    assert report.status == HEALTH_DEGRADED
    assert report.check(CHECK_MODEL_IDENTITY_HEALTH).status == CHECK_DEGRADED  # type: ignore[union-attr]


def test_qfq_execution_price_is_degraded() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(  # type: ignore[arg-type]
            price_contract={
                "execution_uncertain": True,
                "execution_uncertain_reason": "execution_price_mode=qfq 不是 raw",
                "execution_price_mode": "qfq",
            }
        )
    )
    assert report.status == HEALTH_DEGRADED
    assert report.check(CHECK_PRICE_SERIES_AVAILABILITY).status == CHECK_DEGRADED  # type: ignore[union-attr]


def test_board_coverage_low_is_degraded() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(board_coverage={"主板": 0.5})  # type: ignore[arg-type]
    )
    assert report.check(CHECK_BOARD_COVERAGE).status == CHECK_DEGRADED  # type: ignore[union-attr]


def test_payload_is_auditable() -> None:
    payload = evaluate_data_health(**_healthy_kwargs()).to_payload()  # type: ignore[arg-type]
    assert payload["coverage_denominator"] == "expected_active"
    assert payload["policy"] == "missing_or_unverifiable_is_never_healthy"
    assert len(payload["checks"]) == 7
    for key in ("as_of", "status", "broken_checks", "degraded_checks", "missing_artifacts"):
        assert key in payload


# ---------------------------------------------------------------------------
# 分层门：数据健康先判，广度后判
# ---------------------------------------------------------------------------


def test_gate_blocks_when_data_health_broken_even_if_breadth_strong() -> None:
    """蓝图严禁项：coverage 坏但 breadth 高分不得放行。"""
    report = evaluate_data_health(
        **_healthy_kwargs(valid_symbol_count=40)  # type: ignore[arg-type]
    )
    decision = combined_gate_decision(
        report=report,
        breadth_policy={"block_new_buy": False, "reason": "breadth_ok"},
        enforce=True,
    )
    assert decision["block_new_buy"] is True
    assert str(decision["reason"]).startswith("data_health_broken")
    assert decision["breadth_block_new_buy"] is False  # 广度本身是放行的 → 说明是健康门拦下的


def test_gate_degrades_to_observe_only_not_block() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(breadth_artifact_present=False)  # type: ignore[arg-type]
    )
    decision = combined_gate_decision(
        report=report, breadth_policy={"block_new_buy": True, "reason": "breadth_ok"}, enforce=True
    )
    # degraded → 只观测，不阻断（数据缺口不制造"无票"）
    assert decision["block_new_buy"] is False
    assert decision["reason"] == "data_health_degraded:observe_only"


def test_gate_uses_breadth_when_data_health_healthy() -> None:
    report = evaluate_data_health(**_healthy_kwargs())  # type: ignore[arg-type]
    blocked = combined_gate_decision(
        report=report,
        breadth_policy={"block_new_buy": True, "reason": "breadth_below_threshold"},
        enforce=True,
    )
    assert blocked["block_new_buy"] is True
    assert blocked["reason"] == "breadth_below_threshold"
    allowed = combined_gate_decision(
        report=report, breadth_policy={"block_new_buy": False, "reason": "breadth_ok"}, enforce=True
    )
    assert allowed["block_new_buy"] is False


def test_grey_period_default_never_changes_decision() -> None:
    """灰度默认：即使数据健康 broken，也只是记录建议，不改决策。"""
    report = evaluate_data_health(
        **_healthy_kwargs(valid_symbol_count=10)  # type: ignore[arg-type]
    )
    decision = combined_gate_decision(
        report=report, breadth_policy={"block_new_buy": False}, enforce=False
    )
    assert decision["enforced"] is False
    assert decision["block_new_buy"] is False
    assert decision["reason"] == "grey_period_observation_only"
    assert decision["shadow_recommendation"]["block_new_buy"] is True


def test_broken_beats_degraded_in_aggregation() -> None:
    report = evaluate_data_health(
        **_healthy_kwargs(  # type: ignore[arg-type]
            breadth_artifact_present=False, latest_trade_date="2026-08-01"
        )
    )
    assert report.status == HEALTH_BROKEN
    assert CHECK_TRADE_DATE_FRESHNESS in report.broken_checks
    assert CHECK_BREADTH_ARTIFACT in report.degraded_checks
