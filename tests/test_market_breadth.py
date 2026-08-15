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
    compute_market_breadth_from_warehouse,
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


# ---------------------------------------------------------------------------
# 广度 producer：从 warehouse 批量帧现算快照（无外部 API）
# ---------------------------------------------------------------------------


def _fake_warehouse(frame):
    class _FakeWarehouse:
        def list_symbols(self):
            return sorted(frame["symbol"].unique())

        def fetch_universe_quality_metrics(self, *, symbols, lookback_days):
            return frame

    return _FakeWarehouse()


def _rising_market_frame() -> object:
    import pandas as pd

    rows = []
    for sym, base in [("600000", 10.0), ("000001", 20.0), ("300001", 30.0)]:
        for i in range(22):
            date = pd.Timestamp("2026-08-01") + pd.Timedelta(days=i)
            close = base * (1.0 + i * 0.005)
            rows.append(
                {
                    "symbol": sym,
                    "date": date,
                    "close": close,
                    "turnover": 1000.0 + i * 10.0,
                    "board": "主板" if sym.startswith(("6", "00")) else "创业板",
                    "is_st": False,
                }
            )
    return pd.DataFrame(rows)


def test_producer_computes_breadth_from_warehouse_frame() -> None:
    snapshot = compute_market_breadth_from_warehouse(_fake_warehouse(_rising_market_frame()))
    assert snapshot is not None
    assert snapshot["total_symbols"] == 3
    assert snapshot["coverage_ratio"] == 1.0
    assert snapshot["advancers"] + snapshot["decliners"] == 3
    assert snapshot["score"]["available"] is True
    assert 0.0 <= snapshot["score"]["value"] <= 100.0
    assert snapshot["source"] == "warehouse_daily"
    # 21 个交易日窗口：每日都在涨 → 最新日创 20 日新高
    assert snapshot["new_highs_20d"] == 3


def test_producer_returns_none_on_empty_frame() -> None:
    import pandas as pd

    snapshot = compute_market_breadth_from_warehouse(
        _fake_warehouse(pd.DataFrame())
    )
    assert snapshot is None


def test_producer_returns_none_on_bad_warehouse() -> None:
    class _BrokenWarehouse:
        def list_symbols(self):
            raise OSError("db missing")

    assert compute_market_breadth_from_warehouse(_BrokenWarehouse()) is None


def test_producer_sets_freshness_and_enables_stale_lift() -> None:
    """producer 输出 freshness.date_max（最新交易日）+ as_of（当前时刻），
    使广度门能判定"轻度过期"（trend 最低分 +5）而非仅二值新鲜/阻断。"""
    frame = _rising_market_frame()
    snapshot = compute_market_breadth_from_warehouse(
        _fake_warehouse(frame),
        now=datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC),
    )
    assert snapshot is not None
    assert snapshot["freshness"]["date_max"] == "2026-08-22"
    policy = breadth_usage_policy(
        snapshot,
        now=datetime(2026, 8, 23, 14, 0, 0, tzinfo=UTC),
        trend_min_threshold=70.0,
        max_intraday_heartbeat_sec=48.0 * 3600.0,
    )
    assert policy["reason"] == "breadth_slightly_stale"
    assert policy["trend_min_threshold_lift"] == 5.0


def test_producer_board_mapping_limit_up_count() -> None:
    """warehouse 的 board 是英文（main/gem/star/bj），必须映射为中文再判涨跌停；
    否则科创板/创业板被按 10% 而非 20% 判定，涨跌停家数严重高估。"""
    import pandas as pd

    rows: list[dict[str, object]] = []
    # main（主板 10%）：最新日 +10.5% → 涨停
    for i in range(22):
        rows.append(
            {
                "symbol": "600001",
                "date": pd.Timestamp("2026-08-01") + pd.Timedelta(days=i),
                "close": 10.0 if i < 21 else 11.05,
                "turnover": 100.0,
                "board": "main",
                "is_st": False,
            }
        )
    # star（科创板 20%）：最新日 +11% → 未到 20%，不涨停
    for i in range(22):
        rows.append(
            {
                "symbol": "688001",
                "date": pd.Timestamp("2026-08-01") + pd.Timedelta(days=i),
                "close": 20.0 if i < 21 else 22.2,
                "turnover": 100.0,
                "board": "star",
                "is_st": False,
            }
        )
    snapshot = compute_market_breadth_from_warehouse(_fake_warehouse(pd.DataFrame(rows)))
    assert snapshot is not None
    assert snapshot["limit_up_count"] == 1  # 只有 main 计入涨停
    assert snapshot["total_symbols"] == 2
