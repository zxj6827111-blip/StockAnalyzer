"""P1 统一过热风险模型（bars/snapshot 共用 evaluator）+ snapshot 新 raw 列。"""

from __future__ import annotations

import pandas as pd
import pytest

from stock_analyzer.config import LimitRuleConfig, OverextensionConfig
from stock_analyzer.feature.snapshot import FORMAT_VERSION, RAW_SNAPSHOT_COLUMNS, _limit_up_streak
from stock_analyzer.risk.overextension import evaluate_overextension


def _config(**kwargs: object) -> OverextensionConfig:
    defaults: dict[str, object] = {}
    defaults.update(kwargs)
    return OverextensionConfig(**defaults)


def _base_row(**overrides: float) -> dict[str, float]:
    row: dict[str, float] = {
        "close": 10.0,
        "ma5": 9.8,
        "atr14": 0.5,
        "ret5": 0.05,
        "gap_pct": 0.01,
        "volume_ratio_5d": 1.0,
    }
    row.update(overrides)
    return row


def test_overextension_none_when_low_bias() -> None:
    decision = evaluate_overextension(_base_row(), _config())
    assert decision.level == "none"
    assert decision.penalty == 0.0
    assert decision.reject_new_buy is False
    assert decision.reasons == []


def test_overextension_warn_on_bias_10_to_15() -> None:
    # bias_ma5 = 12% → warn 档，扣 0.3
    decision = evaluate_overextension(_base_row(close=11.2, ma5=10.0), _config())
    assert decision.level == "warn"
    assert decision.penalty == pytest.approx(0.3)
    assert decision.reject_new_buy is False
    assert "bias_or_atr_distance_warn" in decision.reasons
    assert decision.metrics["bias_ma5"] == pytest.approx(0.12, abs=1e-9)


def test_overextension_reject_on_bias_above_15() -> None:
    # bias_ma5 = 18% → reject 档，trend 轨拒绝新买入
    decision = evaluate_overextension(_base_row(close=11.8, ma5=10.0), _config())
    assert decision.level == "reject"
    assert decision.reject_new_buy is True
    assert "bias_or_atr_distance_reject" in decision.reasons


def test_overextension_reject_on_atr_distance_above_3() -> None:
    # |close - ma5| / atr = 4 → 超过 3 ATR → reject
    decision = evaluate_overextension(_base_row(close=12.0, ma5=10.0), _config())
    assert decision.level == "reject"
    assert decision.reject_new_buy is True
    assert decision.metrics["atr_distance"] == pytest.approx(4.0)


def test_overextension_warn_on_atr_distance_between_2_and_3() -> None:
    # distance = 1/0.45 ≈ 2.22 → warn
    decision = evaluate_overextension(_base_row(close=11.0, ma5=10.0, atr14=0.45), _config())
    assert decision.level == "warn"
    assert decision.reject_new_buy is False


def test_overextension_extra_risk_items_add_penalty() -> None:
    # ret5=0.35 且 gap=0.08 且 量价背离（volume_ratio=3, bias=12%>10%）
    decision = evaluate_overextension(
        _base_row(
            close=11.2,
            ma5=10.0,
            ret5=0.35,
            gap_pct=0.08,
            volume_ratio_5d=3.0,
        ),
        _config(),
    )
    assert {"ret5_high", "large_gap", "volume_divergence"} <= set(decision.reasons)
    # 主档 warn 0.3，附加项不再叠加（取 max）
    assert decision.penalty == pytest.approx(0.3)
    assert decision.level == "warn"


def test_overextension_falls_back_to_bars_style_fields() -> None:
    # bars 路径无 ma5/atr14 时回退 ma5_from_ma20 / atr_20d
    decision = evaluate_overextension(
        {
            "close": 12.0,
            "ma5_from_ma20": 10.0,
            "atr_20d": 0.5,
            "ret5": 0.0,
            "gap_pct": 0.0,
            "volume_ratio_5d": 1.0,
        },
        _config(),
    )
    assert decision.level == "reject"
    assert decision.metrics["atr_distance"] == pytest.approx(4.0)


def test_snapshot_format_version_bumped_and_columns_extended() -> None:
    assert FORMAT_VERSION == 2
    for column in (
        "ma5",
        "ma10",
        "atr14",
        "bias_ma5",
        "bias_ma10",
        "ret5",
        "gap_pct",
        "volume_ratio_5d",
        "consecutive_limit_up",
    ):
        assert column in RAW_SNAPSHOT_COLUMNS


def _bar_frame(dates: list[str], closes: list[float], *, symbol: str = "600000") -> pd.DataFrame:
    index = pd.to_datetime(dates)
    prev = [closes[0]] + closes[:-1]
    frame = pd.DataFrame(
        {
            "open": [c * 0.995 for c in closes],
            "close": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "volume": [1_000_000.0] * len(closes),
            "turnover": [c * 1_000_000.0 for c in closes],
            "pre_close": prev,
            "is_st": [False] * len(closes),
            "float_market_cap": [10_000_000_000.0] * len(closes),
        },
        index=index,
    )
    frame.attrs["symbol"] = symbol
    return frame


def _limit_rule() -> LimitRuleConfig:
    return LimitRuleConfig()


def test_limit_up_streak_counts_board_aware_10pct() -> None:
    # 主板 10%：10.0→11.0→12.5 连续两日涨停（第三日 12.5 ≥ 11.0*1.1=12.1）
    frame = _bar_frame(
        dates=["2026-08-11", "2026-08-12", "2026-08-13"],
        closes=[10.0, 11.0, 12.5],
        symbol="600000",
    )
    assert _limit_up_streak(bars=frame, symbol="600000", limit_rule=_limit_rule()) == 2


def test_limit_up_streak_breaks_on_non_limit_day() -> None:
    # 最新日 11.4（11.0*1.1=12.1 未到）未涨停 → 连板已断，streak=0
    frame = _bar_frame(
        dates=["2026-08-11", "2026-08-12", "2026-08-13"],
        closes=[10.0, 11.0, 11.4],
        symbol="600000",
    )
    assert _limit_up_streak(bars=frame, symbol="600000", limit_rule=_limit_rule()) == 0


def test_limit_up_streak_zero_without_any_limit_day() -> None:
    frame = _bar_frame(
        dates=["2026-08-11", "2026-08-12", "2026-08-13"],
        closes=[10.0, 10.2, 10.4],
        symbol="600000",
    )
    assert _limit_up_streak(bars=frame, symbol="600000", limit_rule=_limit_rule()) == 0
