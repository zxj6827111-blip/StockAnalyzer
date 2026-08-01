from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.runtime.universe_candidate_selector import (
    UniverseCandidateSelector,
    persist_selection_snapshot,
)


def _make_daily_frame(
    *,
    days: int,
    start_close: float,
    drift: float,
    turnover: float,
    float_market_cap: float,
    roe: float = 0.15,
    debt_ratio: float = 0.30,
    suspended: bool = False,
    is_st: bool = False,
    is_delisting_risk: bool = False,
    financial_data_complete: bool = True,
    financial_completeness: float = 0.95,
    background_data_complete: bool = True,
    northbound_net: float = 0.0,
    dragon_tiger_flag: float = 0.0,
    holder_count: float = 40000.0,
) -> pd.DataFrame:
    closes = [start_close * (1.0 + drift) ** i for i in range(days)]
    opens = [c * 0.998 for c in closes]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    volumes = [turnover / closes[0] * 10 for _ in closes]
    dates = pd.bdate_range(end="2026-07-31", periods=days)
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "turnover": [turnover] * days,
            "float_market_cap": [float_market_cap] * days,
            "suspended": [suspended] * days,
            "is_st": [is_st] * days,
            "is_delisting_risk": [is_delisting_risk] * days,
            "roe": [roe] * days,
            "debt_ratio": [debt_ratio] * days,
            "financial_data_complete": [financial_data_complete] * days,
            "financial_completeness": [financial_completeness] * days,
            "background_data_complete": [background_data_complete] * days,
            "northbound_net": [northbound_net] * days,
            "dragon_tiger_flag": [dragon_tiger_flag] * days,
            "holder_count": [holder_count] * days,
        },
        index=dates,
    )
    frame.index.name = "date"
    return frame


def _build_warehouse(tmp_path: Path, symbols_spec: dict[str, dict[str, Any]]) -> MarketWarehouse:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    for symbol, kwargs in symbols_spec.items():
        warehouse.replace_daily_bars(symbol=symbol, frame=_make_daily_frame(**kwargs))
    return warehouse


def _build_batch_frame(symbols_spec: dict[str, dict[str, Any]]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol, kwargs in symbols_spec.items():
        frame = _make_daily_frame(**kwargs).reset_index()
        frame.insert(0, "symbol", symbol)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


class _FrameWarehouse:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.fetch_calls = 0

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
    ) -> pd.DataFrame:
        self.fetch_calls += 1
        requested = set(symbols)
        return self.frame[self.frame["symbol"].isin(requested)].copy()


def _default_symbols_spec(count_per_board: int = 80) -> dict[str, dict[str, Any]]:
    """Build ~400 symbols across 5 boards with controlled quality.

    High-quality symbols (suffix 'A'): uptrend, high turnover/cap, strong fundamental.
    Low-quality symbols (suffix 'B'): downtrend, lower turnover, weak fundamental.
    """
    spec: dict[str, dict[str, Any]] = {}
    # Board prefixes -> (board name)
    prefixes = [
        ("000", "SZ_MAIN"),
        ("300", "SZ_GEM"),
        ("600", "SH_MAIN"),
        ("688", "SH_STAR"),
        ("830", "BSE"),
    ]
    for prefix, _board in prefixes:
        for i in range(count_per_board):
            symbol = f"{prefix}{i + 1:03d}"
            if i < count_per_board // 2:
                # High quality half
                spec[symbol] = {
                    "days": 100,
                    "start_close": 10.0 + i * 0.05,
                    "drift": 0.004,
                    "turnover": 80_000_000.0,
                    "float_market_cap": 8_000_000_000.0,
                    "roe": 0.18,
                    "debt_ratio": 0.28,
                }
            else:
                # Low quality half (still eligible, just lower score)
                spec[symbol] = {
                    "days": 100,
                    "start_close": 10.0 + i * 0.05,
                    "drift": -0.001,
                    "turnover": 12_000_000.0,
                    "float_market_cap": 1_200_000_000.0,
                    "roe": 0.03,
                    "debt_ratio": 0.65,
                }
    return spec


def _make_selector(
    warehouse: Any,
    *,
    exploration_ratio: float = 0.05,
    min_quota_per_in_scope_board: int = 10,
    fallback_sampler=None,
    **selector_kwargs: Any,
) -> UniverseCandidateSelector:
    return UniverseCandidateSelector(
        warehouse=warehouse,
        min_history_days=60,
        min_avg_turnover_20=5_000_000.0,
        min_float_market_cap=300_000_000.0,
        exploration_ratio=exploration_ratio,
        min_quota_per_in_scope_board=min_quota_per_in_scope_board,
        lookback_days=120,
        fallback_sampler=fallback_sampler,
        **selector_kwargs,
    )


# ---------------------------------------------------------------------------
# 1. High-quality symbols stably enter the 300
# ---------------------------------------------------------------------------
def test_high_quality_symbols_stably_selected(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=80)
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    symbols = sorted(spec.keys())
    selector = _make_selector(warehouse)
    result = selector.select(
        symbols=symbols,
        target_size=300,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    selected = result["selected"]
    report = result["report"]
    assert len(selected) == 300
    # High-quality symbols (first half of each board) should be in the selected set.
    high_quality = {s for s in symbols if int(s[-3:]) <= 40}
    missing_high = high_quality - set(selected)
    # Allow a tiny slack for board-quota edge effects, but the vast majority must be in.
    assert len(missing_high) <= 5, f"too many high-quality misses: {len(missing_high)}"
    assert report["selector_mode"] == "quality"
    assert report["selected_count"] == 300
    core_scores = [
        item["score"]
        for item in report["selected"]
        if "core_quality_selected" in item["reason_codes"]
    ]
    assert core_scores == sorted(core_scores, reverse=True)


# ---------------------------------------------------------------------------
# 2. Hard-filter failures never enter core or exploration
# ---------------------------------------------------------------------------
def test_hard_filter_failures_excluded(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=20)
    # Inject hard-filter failures.
    spec["000901"] = {  # suspended
        "days": 100,
        "start_close": 12.0,
        "drift": 0.003,
        "turnover": 80_000_000.0,
        "float_market_cap": 5_000_000_000.0,
        "suspended": True,
    }
    spec["300901"] = {  # ST
        "days": 100,
        "start_close": 12.0,
        "drift": 0.003,
        "turnover": 80_000_000.0,
        "float_market_cap": 5_000_000_000.0,
        "is_st": True,
    }
    spec["600901"] = {  # delisting risk
        "days": 100,
        "start_close": 12.0,
        "drift": 0.003,
        "turnover": 80_000_000.0,
        "float_market_cap": 5_000_000_000.0,
        "is_delisting_risk": True,
    }
    spec["688901"] = {  # low turnover
        "days": 100,
        "start_close": 12.0,
        "drift": 0.003,
        "turnover": 100_000.0,
        "float_market_cap": 5_000_000_000.0,
    }
    spec["830901"] = {  # insufficient history
        "days": 30,
        "start_close": 12.0,
        "drift": 0.003,
        "turnover": 80_000_000.0,
        "float_market_cap": 5_000_000_000.0,
    }
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    symbols = sorted(spec.keys())
    selector = _make_selector(warehouse)
    result = selector.select(
        symbols=symbols,
        target_size=200,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    selected = set(result["selected"])
    for bad in ("000901", "300901", "600901", "688901", "830901"):
        assert bad not in selected, f"hard-filter failure {bad} leaked into selection"
    report = result["report"]
    rejected = report["rejected_count_by_reason"]
    assert rejected.get("suspended", 0) >= 1
    assert rejected.get("is_st", 0) >= 1
    assert rejected.get("delisting_risk", 0) >= 1
    assert rejected.get("low_avg_turnover_20", 0) >= 1
    assert rejected.get("insufficient_history", 0) >= 1


# ---------------------------------------------------------------------------
# 3. Board-internal quality sorting
# ---------------------------------------------------------------------------
def test_board_internal_quality_sorting(tmp_path: Path) -> None:
    # Two symbols in the same board, clearly different quality.
    spec = {
        "600001": {  # high quality
            "days": 100,
            "start_close": 10.0,
            "drift": 0.006,
            "turnover": 100_000_000.0,
            "float_market_cap": 10_000_000_000.0,
            "roe": 0.20,
            "debt_ratio": 0.25,
        },
        "600002": {  # low quality
            "days": 100,
            "start_close": 10.0,
            "drift": -0.003,
            "turnover": 8_000_000.0,
            "float_market_cap": 600_000_000.0,
            "roe": 0.02,
            "debt_ratio": 0.70,
        },
    }
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    selector = _make_selector(warehouse, exploration_ratio=0.0)
    result = selector.select(
        symbols=["600001", "600002"],
        target_size=1,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    assert result["selected"] == ["600001"]
    payload = result["report"]["selected"]
    assert payload[0]["symbol"] == "600001"
    assert payload[0]["score"] > 0


# ---------------------------------------------------------------------------
# 4. Board minimum quota + global high-score backfill
# ---------------------------------------------------------------------------
def test_board_min_quota_and_global_backfill(tmp_path: Path) -> None:
    # SH_STAR board has only 3 symbols (small board) but high quality; SH_MAIN has many.
    spec: dict[str, dict[str, Any]] = {}
    # SH_STAR: 3 high-quality symbols
    for i in range(1, 4):
        spec[f"68800{i}"] = {
            "days": 100,
            "start_close": 20.0,
            "drift": 0.005,
            "turnover": 90_000_000.0,
            "float_market_cap": 6_000_000_000.0,
            "roe": 0.17,
            "debt_ratio": 0.30,
        }
    # SH_MAIN: 60 medium-quality symbols (6-digit codes 600101..600160)
    for i in range(1, 61):
        spec[f"6001{i:02d}"] = {
            "days": 100,
            "start_close": 10.0,
            "drift": 0.001,
            "turnover": 30_000_000.0,
            "float_market_cap": 3_000_000_000.0,
            "roe": 0.08,
            "debt_ratio": 0.45,
        }
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    selector = _make_selector(warehouse, exploration_ratio=0.0, min_quota_per_in_scope_board=3)
    result = selector.select(
        symbols=sorted(spec.keys()),
        target_size=20,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    selected = set(result["selected"])
    # All 3 SH_STAR symbols must be selected via minimum quota.
    for i in range(1, 4):
        assert f"68800{i}" in selected
    board_quotas = result["report"]["board_quotas"]
    assert board_quotas["SH_STAR"]["selected_count"] == 3
    assert board_quotas["SH_STAR"]["in_scope"] is True
    # The remainder must be filled from SH_MAIN by global quality.
    assert board_quotas["SH_MAIN"]["selected_count"] == 17


def test_board_quota_is_floor_not_population_cap() -> None:
    spec: dict[str, dict[str, Any]] = {}
    for i in range(1, 31):
        spec[f"600{i:03d}"] = {
            "days": 100,
            "start_close": 10.0,
            "drift": 0.006,
            "turnover": 100_000_000.0,
            "float_market_cap": 10_000_000_000.0,
            "roe": 0.20,
            "debt_ratio": 0.20,
        }
        spec[f"300{i:03d}"] = {
            "days": 100,
            "start_close": 10.0,
            "drift": -0.002,
            "turnover": 10_000_000.0,
            "float_market_cap": 1_000_000_000.0,
            "roe": 0.01,
            "debt_ratio": 0.70,
        }
    result = _make_selector(
        _FrameWarehouse(_build_batch_frame(spec)),
        exploration_ratio=0.0,
        min_quota_per_in_scope_board=3,
    ).select(
        symbols=sorted(spec),
        target_size=20,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE"],
    )
    board_quotas = result["report"]["board_quotas"]
    assert board_quotas["SH_MAIN"]["quota"] == 3
    assert board_quotas["SZ_GEM"]["quota"] == 3
    assert board_quotas["SH_MAIN"]["selected_count"] == 17
    assert board_quotas["SZ_GEM"]["selected_count"] == 3
    assert sum(symbol.startswith("600") for symbol in result["selected"]) == 17
    assert sum(symbol.startswith("300") for symbol in result["selected"]) == 3


# ---------------------------------------------------------------------------
# 5. exploration_ratio=0 -> no random exploration at all
# ---------------------------------------------------------------------------
def test_exploration_ratio_zero_no_random(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=40)
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    selector = _make_selector(warehouse, exploration_ratio=0.0)
    result = selector.select(
        symbols=sorted(spec.keys()),
        target_size=100,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    report = result["report"]
    assert report["exploration_selected_count"] == 0
    assert report["core_selected_count"] == 100
    # Every selected symbol carries the core reason code.
    for item in report["selected"]:
        assert "core_quality_selected" in item["reason_codes"]
        assert "exploration_deterministic" not in item["reason_codes"]


# ---------------------------------------------------------------------------
# 6. Same input + same date -> reproducible result
# ---------------------------------------------------------------------------
def test_reproducible_same_input_date(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=40)
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    symbols = sorted(spec.keys())
    selector = _make_selector(warehouse, exploration_ratio=0.05)
    args = {
        "symbols": symbols,
        "target_size": 120,
        "trade_date": "2026-07-31",
        "ruleset_id": "a_share_default_v1",
        "board_scope": ["SSE", "SZSE", "BSE"],
    }
    r1 = selector.select(**args)
    r2 = selector.select(**args)
    assert r1["selected"] == r2["selected"]
    assert r1["report"]["output_symbol_hash"] == r2["report"]["output_symbol_hash"]


def test_input_order_does_not_change_selection(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=40)
    frame = _build_batch_frame(spec)
    symbols = sorted(spec.keys())
    shuffled = symbols[:]
    import random

    random.Random(7).shuffle(shuffled)
    assert shuffled != symbols
    base = {
        "target_size": 120,
        "trade_date": "2026-07-31",
        "ruleset_id": "a_share_default_v1",
        "board_scope": ["SSE", "SZSE", "BSE"],
    }
    r1 = _make_selector(_FrameWarehouse(frame), exploration_ratio=0.05).select(
        symbols=symbols, **base
    )
    r2 = _make_selector(_FrameWarehouse(frame), exploration_ratio=0.05).select(
        symbols=shuffled, **base
    )
    assert r1["selected"] == r2["selected"]
    for key in set(r1["report"]) | set(r2["report"]):
        if key in ("elapsed_ms", "generated_at"):
            continue
        assert r1["report"][key] == r2["report"][key]


# ---------------------------------------------------------------------------
# 7. Different date -> only exploration rotates; core high-score pool stable
# ---------------------------------------------------------------------------
def test_different_date_core_stable(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=40)
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    symbols = sorted(spec.keys())
    selector = _make_selector(warehouse, exploration_ratio=0.05)
    base = {
        "symbols": symbols,
        "target_size": 120,
        "ruleset_id": "a_share_default_v1",
        "board_scope": ["SSE", "SZSE", "BSE"],
    }
    r1 = selector.select(trade_date="2026-07-30", **base)
    r2 = selector.select(trade_date="2026-07-31", **base)
    core1 = {
        item["symbol"]
        for item in r1["report"]["selected"]
        if "core_quality_selected" in item["reason_codes"]
    }
    core2 = {
        item["symbol"]
        for item in r2["report"]["selected"]
        if "core_quality_selected" in item["reason_codes"]
    }
    # Core pool must be identical across dates (deterministic, data-driven).
    assert core1 == core2
    # Output hash may differ (exploration rotates) but core is stable.
    assert r1["report"]["core_selected_count"] == r2["report"]["core_selected_count"]


# ---------------------------------------------------------------------------
# 8. Eligible below target -> all returned, no duplicates
# ---------------------------------------------------------------------------
def test_eligible_below_target_all_returned_no_dupes(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=4)  # 20 symbols total
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    symbols = sorted(spec.keys())
    selector = _make_selector(warehouse, exploration_ratio=0.05)
    result = selector.select(
        symbols=symbols,
        target_size=300,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    selected = result["selected"]
    assert len(selected) == len(set(selected)), "duplicates in selection"
    assert len(selected) == len(symbols)
    assert result["report"]["selector_mode"] == "quality_all_eligible"
    assert result["report"]["exploration_selected_count"] == 0


# ---------------------------------------------------------------------------
# 9. Batch failure -> degraded fallback with complete metadata
# ---------------------------------------------------------------------------
def test_batch_failure_fallback_metadata(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=10)
    warehouse = _FrameWarehouse(_build_batch_frame(spec))

    captured: dict[str, Any] = {}
    fallback_boards = {
        "SH_MAIN": {
            "exchange": "SSE",
            "in_scope": True,
            "input_count": 10,
            "quota": 10,
            "selected_count": 10,
        }
    }

    def fake_fallback(symbols, *, cap, board_scope, universe_ruleset_id, seed_trade_date):
        captured["cap"] = cap
        captured["board_scope"] = list(board_scope)
        captured["seed_trade_date"] = seed_trade_date
        # Return a deterministic subset as the fallback would.
        chosen = sorted(symbols)[:cap]
        meta = {
            "truncation_mode": "random_quota",
            "cap": cap,
            "boards": fallback_boards,
        }
        return chosen, meta

    selector = _make_selector(warehouse, fallback_sampler=fake_fallback)

    # Force batch failure by monkeypatching the warehouse batch method.
    def _failing_fetch(*, symbols, lookback_days):
        raise RuntimeError("simulated duckdb failure")

    warehouse.fetch_universe_quality_metrics = _failing_fetch  # type: ignore[method-assign]

    result = selector.select(
        symbols=sorted(spec.keys()),
        target_size=50,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    report = result["report"]
    assert report["selector_mode"] == "degraded_fallback"
    assert report["fallback_reason"] == "batch_fetch_error:RuntimeError"
    assert report["fallback_source"] == "quota_sampler"
    assert report["hard_eligible_count"] == 0
    assert report["batch_calls"] == 1
    assert report["input_count"] == len(spec)
    assert report["selected_count"] == len(result["selected"])
    assert report["trade_date"] == "2026-07-31"
    assert report["ruleset_id"] == "a_share_default_v1"
    assert report["input_symbol_hash"]
    assert report["output_symbol_hash"]
    assert captured["cap"] == 50
    assert report["board_quotas"] == fallback_boards
    assert report["fallback_sampler_meta"]["truncation_mode"] == "random_quota"
    assert "truncation_mode" not in report["board_quotas"]
    assert captured["seed_trade_date"] == "2026-07-31"


# ---------------------------------------------------------------------------
# 10. Selection report fields and symbol hash correctness
# ---------------------------------------------------------------------------
def test_selection_report_fields_and_hash(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=30)
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    symbols = sorted(spec.keys())
    selector = _make_selector(warehouse, exploration_ratio=0.05)
    result = selector.select(
        symbols=symbols,
        target_size=80,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    report = result["report"]
    required_fields = {
        "input_count",
        "hard_eligible_count",
        "rejected_count_by_reason",
        "selected_count",
        "core_selected_count",
        "exploration_selected_count",
        "selected_by_board",
        "score_distribution",
        "selected",
        "trade_date",
        "ruleset_id",
        "selector_mode",
        "fallback_reason",
        "input_symbol_hash",
        "output_symbol_hash",
        "board_quotas",
        "applied_weights",
        "batch_symbol_count",
        "batch_coverage_ratio",
        "target_size",
    }
    assert required_fields <= set(report.keys())
    assert report["input_count"] == len(symbols)
    assert report["hard_eligible_count"] > 0
    assert report["selected_count"] == 80
    assert isinstance(report["score_distribution"], dict)
    assert report["score_distribution"]["count"] == report["hard_eligible_count"]
    assert isinstance(report["selected_by_board"], dict)
    # Each selected payload has score / components / reason_codes.
    for item in report["selected"]:
        assert "symbol" in item
        assert "score" in item
        assert "components" in item
        assert "reason_codes" in item
        comps = item["components"]
        assert {
            "trend",
            "capital_flow",
            "price_volume",
            "liquidity",
            "fundamental",
            "risk_penalty",
        } <= set(comps.keys())
    # Symbol hash: deterministic over sorted selected.
    import hashlib

    expected = hashlib.sha256("\n".join(sorted(result["selected"])).encode("utf-8")).hexdigest()[
        :16
    ]
    assert report["output_symbol_hash"] == expected
    assert report["input_symbol_hash"] != report["output_symbol_hash"]


def test_configured_weights_reverse_ranking() -> None:
    spec = {
        "600001": {
            "days": 120,
            "start_close": 10.0,
            "drift": 0.006,
            "turnover": 80_000_000.0,
            "float_market_cap": 5_000_000_000.0,
            "roe": 0.01,
            "debt_ratio": 0.75,
        },
        "600002": {
            "days": 120,
            "start_close": 10.0,
            "drift": -0.001,
            "turnover": 80_000_000.0,
            "float_market_cap": 5_000_000_000.0,
            "roe": 0.30,
            "debt_ratio": 0.10,
        },
    }
    frame = _build_batch_frame(spec)
    trend_weights = {
        "trend": 1.0,
        "capital_flow": 0.0,
        "price_volume": 0.0,
        "liquidity": 0.0,
        "fundamental": 0.0,
        "risk_penalty": 0.0,
    }
    fundamental_weights = {
        "trend": 0.0,
        "capital_flow": 0.0,
        "price_volume": 0.0,
        "liquidity": 0.0,
        "fundamental": 1.0,
        "risk_penalty": 0.0,
    }
    common = {
        "symbols": sorted(spec),
        "target_size": 1,
        "trade_date": "2026-07-31",
        "reference_date": "2026-07-31",
        "ruleset_id": "a_share_default_v1",
        "board_scope": ["SSE"],
    }
    trend_result = _make_selector(
        _FrameWarehouse(frame),
        exploration_ratio=0.0,
        weights=trend_weights,
    ).select(**common)
    fundamental_result = _make_selector(
        _FrameWarehouse(frame),
        exploration_ratio=0.0,
        weights=fundamental_weights,
    ).select(**common)
    assert trend_result["selected"] == ["600001"]
    assert fundamental_result["selected"] == ["600002"]
    assert trend_result["report"]["applied_weights"]["trend"] == 1.0
    assert fundamental_result["report"]["applied_weights"]["fundamental"] == 1.0


def test_partial_coverage_uses_snapshot_then_quota_fallback(tmp_path: Path) -> None:
    spec = {
        f"600{i:03d}": {
            "days": 100,
            "start_close": 10.0,
            "drift": 0.001 + i * 0.0001,
            "turnover": 50_000_000.0,
            "float_market_cap": 4_000_000_000.0,
            "roe": 0.10,
            "debt_ratio": 0.30,
        }
        for i in range(1, 11)
    }
    symbols = sorted(spec)
    full_frame = _build_batch_frame(spec)
    snapshot_path = tmp_path / "snapshot.json"
    initial = _make_selector(
        _FrameWarehouse(full_frame),
        exploration_ratio=0.0,
    ).select(
        symbols=symbols,
        target_size=5,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    persist_selection_snapshot(initial["report"], snapshot_path)

    partial_frame = full_frame[full_frame["symbol"] == symbols[0]].copy()
    snapshot_result = _make_selector(
        _FrameWarehouse(partial_frame),
        exploration_ratio=0.0,
        min_batch_coverage_ratio=0.90,
        snapshot_path=snapshot_path,
        snapshot_max_age_days=7,
    ).select(
        symbols=symbols,
        target_size=5,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    snapshot_report = snapshot_result["report"]
    assert snapshot_report["selector_mode"] == "snapshot_fallback"
    assert snapshot_report["fallback_source"] == "quality_snapshot"
    assert snapshot_report["batch_symbol_count"] == 1
    assert snapshot_report["missing_batch_symbol_count"] == 9
    assert snapshot_report["batch_coverage_ratio"] == 0.1
    assert snapshot_result["selected"] == initial["selected"]

    fallback_calls: list[int] = []

    def _quota_fallback(symbols, *, cap, **_kwargs):
        fallback_calls.append(cap)
        return sorted(symbols)[:cap], {"truncation_mode": "test_quota"}

    quota_result = _make_selector(
        _FrameWarehouse(partial_frame),
        exploration_ratio=0.0,
        min_batch_coverage_ratio=0.90,
        snapshot_path=tmp_path / "missing.json",
        fallback_sampler=_quota_fallback,
    ).select(
        symbols=symbols,
        target_size=5,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    quota_report = quota_result["report"]
    assert quota_report["selector_mode"] == "degraded_fallback"
    assert quota_report["fallback_source"] == "quota_sampler"
    assert quota_report["snapshot_fallback_unavailable_reason"] == "snapshot_not_found"
    assert fallback_calls == [5]


def test_snapshot_fallback_tops_up_from_current_universe(tmp_path: Path) -> None:
    original_spec = {
        f"600{i:03d}": {
            "days": 100,
            "start_close": 10.0,
            "drift": 0.002 + i * 0.0001,
            "turnover": 50_000_000.0,
            "float_market_cap": 4_000_000_000.0,
            "roe": 0.10,
            "debt_ratio": 0.30,
        }
        for i in range(1, 6)
    }
    original_symbols = sorted(original_spec)
    snapshot_path = tmp_path / "topup-snapshot.json"
    original_result = _make_selector(
        _FrameWarehouse(_build_batch_frame(original_spec)),
        exploration_ratio=0.0,
    ).select(
        symbols=original_symbols,
        target_size=5,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    persist_selection_snapshot(original_result["report"], snapshot_path)

    current_symbols = ["600001", "600101", "600102", "600103", "600104"]
    partial_frame = _build_batch_frame({"600001": original_spec["600001"]})
    sampler_calls: list[tuple[list[str], int]] = []

    def _quota_topup(symbols, *, cap, **_kwargs):
        sampler_calls.append((list(symbols), cap))
        return list(symbols)[:cap], {"truncation_mode": "snapshot_topup_test"}

    result = _make_selector(
        _FrameWarehouse(partial_frame),
        exploration_ratio=0.0,
        min_batch_coverage_ratio=0.90,
        snapshot_path=snapshot_path,
        snapshot_max_age_days=7,
        fallback_sampler=_quota_topup,
    ).select(
        symbols=current_symbols,
        target_size=5,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    report = result["report"]
    assert result["selected"][0] == "600001"
    assert set(result["selected"][1:]) == set(current_symbols[1:])
    assert report["target_size"] == 5
    assert report["selected_count"] == 5
    assert report["fallback_topup_count"] == 4
    assert report["fallback_source"] == "quality_snapshot+quota_sampler"
    assert report["fallback_sampler_error"] == ""
    assert report["fallback_sampler_meta"]["truncation_mode"] == "snapshot_topup_test"
    assert sampler_calls == [(current_symbols[1:], 4)]
    topup_payload = report["selected"][1:]
    assert all(item["score"] == 0.0 for item in topup_payload)
    assert all(
        {"degraded_fallback", "snapshot_quota_topup"} <= set(item["reason_codes"])
        for item in topup_payload
    )


def test_nan_inf_market_fields_are_hard_rejected() -> None:
    base = {
        "days": 100,
        "start_close": 10.0,
        "drift": 0.002,
        "turnover": 50_000_000.0,
        "float_market_cap": 4_000_000_000.0,
        "roe": 0.10,
        "debt_ratio": 0.30,
    }
    spec = {symbol: dict(base) for symbol in ("600001", "600002", "600003", "600004")}
    frame = _build_batch_frame(spec)
    frame.loc[frame["symbol"] == "600001", "turnover"] = np.nan
    frame.loc[frame["symbol"] == "600002", "float_market_cap"] = np.inf
    frame.loc[frame["symbol"] == "600003", "close"] = np.nan
    result = _make_selector(_FrameWarehouse(frame), exploration_ratio=0.0).select(
        symbols=sorted(spec),
        target_size=4,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    assert result["selected"] == ["600004"]
    rejected = result["report"]["rejected_count_by_reason"]
    assert rejected["invalid_avg_turnover_20"] == 1
    assert rejected["invalid_float_market_cap"] == 1
    assert rejected.get("invalid_history", 0) + rejected.get("insufficient_history", 0) == 1


def test_schema_drift_enters_auditable_fallback() -> None:
    spec = {
        "600001": {
            "days": 100,
            "start_close": 10.0,
            "drift": 0.002,
            "turnover": 50_000_000.0,
            "float_market_cap": 4_000_000_000.0,
        }
    }
    frame = _build_batch_frame(spec).drop(columns=["roe"])

    def _quota_fallback(symbols, *, cap, **_kwargs):
        return symbols[:cap], {"truncation_mode": "schema_fallback"}

    result = _make_selector(_FrameWarehouse(frame), fallback_sampler=_quota_fallback).select(
        symbols=["600001"],
        target_size=1,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    report = result["report"]
    assert report["selector_mode"] == "degraded_fallback"
    assert report["fallback_reason"] == "schema_error:missing_required_columns:roe"
    assert report["fallback_source"] == "quota_sampler"


def test_stale_and_financial_thresholds_are_hard_rejected() -> None:
    base = {
        "days": 100,
        "start_close": 10.0,
        "drift": 0.002,
        "turnover": 50_000_000.0,
        "float_market_cap": 4_000_000_000.0,
        "roe": 0.10,
        "debt_ratio": 0.30,
    }
    spec = {symbol: dict(base) for symbol in ("600001", "600002", "600003", "600004", "600005")}
    spec["600003"]["financial_data_complete"] = False
    spec["600004"]["roe"] = -0.01
    spec["600005"]["debt_ratio"] = 0.90
    frame = _build_batch_frame(spec)
    stale_mask = frame["symbol"] == "600002"
    frame.loc[stale_mask, "date"] = frame.loc[stale_mask, "date"] - pd.Timedelta(days=30)
    result = _make_selector(
        _FrameWarehouse(frame),
        exploration_ratio=0.0,
        max_staleness_days=10,
        require_financial_data=True,
        min_roe=0.0,
        max_debt_ratio=0.80,
    ).select(
        symbols=sorted(spec),
        target_size=5,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    assert result["selected"] == ["600001"]
    rejected = result["report"]["rejected_count_by_reason"]
    assert rejected["stale_market_data"] == 1
    assert rejected["financial_data_incomplete"] == 1
    assert rejected["roe_below_min"] == 1
    assert rejected["debt_ratio_above_max"] == 1


def test_missing_financial_data_respected_when_require_financial_data() -> None:
    base = {
        "days": 100,
        "start_close": 10.0,
        "drift": 0.002,
        "turnover": 50_000_000.0,
        "float_market_cap": 4_000_000_000.0,
    }
    spec = {
        "600001": dict(base, roe=0.10, debt_ratio=0.30),
        # Claims complete financials but ROE is missing.
        "600002": dict(
            base,
            roe=np.nan,
            debt_ratio=0.30,
            financial_data_complete=True,
            financial_completeness=1.0,
        ),
        # Honest missing financials (e.g. ZIP-history-only rows).
        "600003": dict(base, roe=np.nan, debt_ratio=np.nan, financial_data_complete=False),
        # Claims complete financials but debt_ratio is missing.
        "600004": dict(
            base,
            roe=0.10,
            debt_ratio=np.nan,
            financial_data_complete=True,
            financial_completeness=1.0,
        ),
    }
    frame = _build_batch_frame(spec)
    result = _make_selector(
        _FrameWarehouse(frame),
        exploration_ratio=0.0,
        require_financial_data=True,
    ).select(
        symbols=["600001", "600002", "600003", "600004"],
        target_size=2,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    assert result["selected"] == ["600001"]
    rejected = result["report"]["rejected_count_by_reason"]
    assert rejected["missing_roe"] == 1
    assert rejected["missing_debt_ratio"] == 1
    assert rejected["financial_data_incomplete"] == 1
    assert rejected["missing_debt_ratio"] == 1


def test_exploration_quota_spans_multiple_boards() -> None:
    spec = _default_symbols_spec(count_per_board=20)
    result = _make_selector(
        _FrameWarehouse(_build_batch_frame(spec)),
        exploration_ratio=0.30,
    ).select(
        symbols=sorted(spec),
        target_size=50,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    exploration = [
        item["symbol"]
        for item in result["report"]["selected"]
        if "exploration_deterministic" in item["reason_codes"]
    ]
    assert len(exploration) == 15
    assert len({str(symbol)[:3] for symbol in exploration}) >= 4


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------
def test_persist_selection_snapshot_writes_audit_file(tmp_path: Path) -> None:
    spec = _default_symbols_spec(count_per_board=10)
    warehouse = _FrameWarehouse(_build_batch_frame(spec))
    selector = _make_selector(warehouse)
    result = selector.select(
        symbols=sorted(spec.keys()),
        target_size=20,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    snapshot_path = tmp_path / "snap" / "selection.json"
    written = persist_selection_snapshot(result["report"], snapshot_path)
    assert written.exists()
    import json

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["selected_count"] == 20
    assert payload["trade_date"] == "2026-07-31"


def test_persist_selection_snapshot_rejects_incomplete_success(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "existing-snapshot.json"
    snapshot_path.write_text('{"sentinel":"keep"}', encoding="utf-8")
    report = {
        "selector_mode": "quality_all_eligible",
        "target_size": 2,
        "selected_count": 1,
        "selected": [{"symbol": "600001", "score": 80.0}],
    }
    try:
        persist_selection_snapshot(report, snapshot_path)
    except ValueError as exc:
        assert "incomplete quality selection" in str(exc)
    else:
        raise AssertionError("incomplete successful report must not be persisted")
    assert snapshot_path.read_text(encoding="utf-8") == '{"sentinel":"keep"}'


# ---------------------------------------------------------------------------
# Performance: 5000-scale candidate selection under 30s, single batch call
# ---------------------------------------------------------------------------
def test_performance_5000_scale_under_30s() -> None:
    import time

    rng = np.random.default_rng(42)
    prefixes = ["000", "300", "600", "688", "830"]
    per_board = 999
    symbols = [f"{prefix}{i:03d}" for prefix in prefixes for i in range(1, per_board + 1)]
    days = 100
    symbol_count = len(symbols)
    symbol_values = np.repeat(np.asarray(symbols, dtype=object), days)
    step_values = np.tile(np.arange(days), symbol_count)
    start_values = np.repeat(5.0 + rng.random(symbol_count) * 30.0, days)
    drift_values = np.repeat(
        rng.choice(np.asarray([-0.002, 0.0, 0.002, 0.005]), symbol_count),
        days,
    )
    close_values = start_values * np.power(1.0 + drift_values, step_values)
    turnover_values = np.repeat(rng.uniform(8_000_000.0, 200_000_000.0, symbol_count), days)
    market_cap_values = np.repeat(rng.uniform(800_000_000.0, 30_000_000_000.0, symbol_count), days)
    frame = pd.DataFrame(
        {
            "symbol": symbol_values,
            "date": np.tile(
                pd.bdate_range(end="2026-07-31", periods=days).to_numpy(),
                symbol_count,
            ),
            "open": close_values * 0.998,
            "high": close_values * 1.01,
            "low": close_values * 0.99,
            "close": close_values,
            "volume": turnover_values / close_values * 10.0,
            "turnover": turnover_values,
            "float_market_cap": market_cap_values,
            "suspended": False,
            "is_st": False,
            "is_delisting_risk": False,
            "roe": np.repeat(rng.uniform(0.0, 0.25, symbol_count), days),
            "debt_ratio": np.repeat(rng.uniform(0.1, 0.7, symbol_count), days),
            "financial_data_complete": True,
            "financial_completeness": 0.95,
            "background_data_complete": True,
            "holder_count": 40_000.0,
            "northbound_net": 0.0,
            "dragon_tiger_flag": 0.0,
        }
    )
    warehouse = _FrameWarehouse(frame)
    selector = _make_selector(warehouse, exploration_ratio=0.05)

    start = time.perf_counter()
    result = selector.select(
        symbols=symbols,
        target_size=300,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    elapsed = time.perf_counter() - start
    assert result["report"]["batch_calls"] == 1
    assert result["report"]["selected_count"] == 300
    assert warehouse.fetch_calls == 1
    assert elapsed < 30.0, f"selector too slow: {elapsed:.2f}s"
