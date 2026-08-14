"""P2 真实市场广度 MarketBreadthSnapshot + 版本化评分 + 过期策略。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.ops.market_breadth import (
    BREADTH_FILENAME,
    SCORE_VERSION,
    breadth_usage_policy,
    build_breadth_snapshot,
    compute_breadth_score,
    read_breadth_snapshot,
    write_breadth_snapshot,
)


def _now() -> datetime:
    return datetime(2026, 8, 14, 14, 0, 0, tzinfo=UTC)


def test_breadth_score_deterministic_and_in_range() -> None:
    score1 = compute_breadth_score(
        advancers=4000,
        decliners=800,
        limit_up_count=120,
        limit_down_count=5,
        median_return=0.012,
        new_highs_20d=600,
        new_lows_20d=40,
        turnover_change_pct=0.1,
        total_symbols=5000,
    )
    score2 = compute_breadth_score(
        advancers=4000,
        decliners=800,
        limit_up_count=120,
        limit_down_count=5,
        median_return=0.012,
        new_highs_20d=600,
        new_lows_20d=40,
        turnover_change_pct=0.1,
        total_symbols=5000,
    )
    assert score1.available is True
    assert 0.0 <= score1.score <= 100.0
    assert score1.score == score2.score  # 确定性
    assert score1.score > 60.0  # 强市 → 高分


def test_breadth_score_weak_market_is_low() -> None:
    score = compute_breadth_score(
        advancers=600,
        decliners=4200,
        limit_up_count=10,
        limit_down_count=150,
        median_return=-0.02,
        new_highs_20d=30,
        new_lows_20d=800,
        turnover_change_pct=-0.15,
        total_symbols=5000,
    )
    assert score.score < 40.0


def test_breadth_snapshot_signature_is_stable() -> None:
    snapshot = build_breadth_snapshot(
        advancers=3000,
        decliners=2000,
        limit_up_count=80,
        limit_down_count=20,
        median_return=0.005,
        new_highs_20d=300,
        new_lows_20d=100,
        turnover_change_pct=0.05,
        total_symbols=5000,
        coverage_ratio=0.99,
        as_of=_now(),
        source="delta_aggregation",
    )
    signature = snapshot["score"]["signature"]
    assert len(signature) == 16
    assert snapshot["score"]["version"] == SCORE_VERSION


def test_breadth_snapshot_low_coverage_unavailable() -> None:
    snapshot = build_breadth_snapshot(
        advancers=1000,
        decliners=1000,
        limit_up_count=20,
        limit_down_count=20,
        median_return=0.0,
        new_highs_20d=100,
        new_lows_20d=100,
        turnover_change_pct=0.0,
        total_symbols=5000,
        coverage_ratio=0.90,  # < 95%
        as_of=_now(),
        source="delta_aggregation",
    )
    assert snapshot["coverage_ok"] is False
    assert snapshot["score"]["available"] is False


def test_write_read_breadth_snapshot_round_trip(tmp_path: Path) -> None:
    snapshot = build_breadth_snapshot(
        advancers=3000,
        decliners=2000,
        limit_up_count=80,
        limit_down_count=20,
        median_return=0.005,
        new_highs_20d=300,
        new_lows_20d=100,
        turnover_change_pct=0.05,
        total_symbols=5000,
        coverage_ratio=0.99,
        as_of=_now(),
        source="delta_aggregation",
    )
    path = tmp_path / "artifacts" / "runtime" / BREADTH_FILENAME
    written = write_breadth_snapshot(snapshot, path)
    assert written.exists()
    loaded = read_breadth_snapshot(path)
    assert loaded is not None
    assert loaded["score"]["value"] == snapshot["score"]["value"]
    assert loaded["score"]["signature"] == snapshot["score"]["signature"]


def test_policy_missing_snapshot_blocks() -> None:
    policy = breadth_usage_policy(None, now=_now(), trend_min_threshold=70.0)
    assert policy["block_new_buy"] is True
    assert policy["reason"] == "breadth_snapshot_missing"


def test_policy_below_threshold_blocks() -> None:
    snapshot = build_breadth_snapshot(
        advancers=600,
        decliners=4200,
        limit_up_count=10,
        limit_down_count=150,
        median_return=-0.02,
        new_highs_20d=30,
        new_lows_20d=800,
        turnover_change_pct=-0.15,
        total_symbols=5000,
        coverage_ratio=0.99,
        as_of=_now(),
        source="delta_aggregation",
    )
    policy = breadth_usage_policy(
        snapshot,
        now=_now(),
        trend_min_threshold=70.0,
        disable_if_below=45.0,
    )
    assert policy["block_new_buy"] is True
    assert policy["reason"] == "breadth_below_threshold"


def test_policy_stale_heartbeat_blocks() -> None:
    snapshot = build_breadth_snapshot(
        advancers=3000,
        decliners=2000,
        limit_up_count=80,
        limit_down_count=20,
        median_return=0.005,
        new_highs_20d=300,
        new_lows_20d=100,
        turnover_change_pct=0.05,
        total_symbols=5000,
        coverage_ratio=0.99,
        as_of=_now() - timedelta(minutes=15),  # 心跳 > 10 分钟
        source="delta_aggregation",
    )
    policy = breadth_usage_policy(
        snapshot,
        now=_now(),
        trend_min_threshold=70.0,
    )
    assert policy["block_new_buy"] is True
    assert policy["reason"] == "breadth_heartbeat_stale"


def test_policy_slightly_stale_lifts_trend_threshold() -> None:
    snapshot = build_breadth_snapshot(
        advancers=3000,
        decliners=2000,
        limit_up_count=80,
        limit_down_count=20,
        median_return=0.005,
        new_highs_20d=300,
        new_lows_20d=100,
        turnover_change_pct=0.05,
        total_symbols=5000,
        coverage_ratio=0.99,
        as_of=_now(),
        source="delta_aggregation",
        freshness={"date_max": (_now() - timedelta(days=3)).isoformat()},
    )
    policy = breadth_usage_policy(
        snapshot,
        now=_now(),
        trend_min_threshold=70.0,
    )
    assert policy["block_new_buy"] is False
    assert policy["reason"] == "breadth_slightly_stale"
    assert policy["trend_min_threshold_lift"] == 5.0
    assert policy["monster_observe_only"] is True


def test_policy_healthy_breadth_allows() -> None:
    snapshot = build_breadth_snapshot(
        advancers=3000,
        decliners=2000,
        limit_up_count=80,
        limit_down_count=20,
        median_return=0.005,
        new_highs_20d=300,
        new_lows_20d=100,
        turnover_change_pct=0.05,
        total_symbols=5000,
        coverage_ratio=0.99,
        as_of=_now(),
        source="delta_aggregation",
        freshness={"date_max": _now().isoformat()},
    )
    policy = breadth_usage_policy(
        snapshot,
        now=_now(),
        trend_min_threshold=70.0,
    )
    assert policy["block_new_buy"] is False
    assert policy["reason"] == "breadth_ok"
    assert policy["trend_min_threshold_lift"] == 0.0


def test_policy_mixed_timezone_does_not_crash() -> None:
    """aware as_of + naive now（调用方 datetime.now()）不得抛时区 TypeError。"""
    snapshot = {
        "score": {"value": 70.0, "available": True},
        "as_of": "2026-08-14T13:55:00+00:00",
        "freshness": {"date_max": "2026-08-14T13:55:00"},
    }
    policy = breadth_usage_policy(
        snapshot,
        now=datetime(2026, 8, 14, 14, 0, 0),  # naive
        trend_min_threshold=70.0,
    )
    assert policy["block_new_buy"] is False
    assert policy["reason"] == "breadth_ok"


def test_policy_naive_as_of_with_aware_now() -> None:
    """naive as_of + aware now：as_of 按 UTC 解释，心跳仍新鲜。"""
    snapshot = {
        "score": {"value": 70.0, "available": True},
        "as_of": "2026-08-14T13:55:00",  # naive
        "freshness": {"date_max": "2026-08-14T13:55:00"},
    }
    policy = breadth_usage_policy(
        snapshot,
        now=datetime(2026, 8, 14, 14, 0, 0, tzinfo=UTC),
        trend_min_threshold=70.0,
    )
    assert policy["block_new_buy"] is False
