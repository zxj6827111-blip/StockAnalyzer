"""P1 板块感知连板风险：build_price_limits 复用（弃固定 9.5%）+ 三连板拒绝。"""

from __future__ import annotations

import pandas as pd

from stock_analyzer.config import BoardRiskConfig, LimitRuleConfig
from stock_analyzer.risk.board_risk import (
    board_decision_to_dict,
    consecutive_limit_up_count,
    evaluate_board_risk,
)
from stock_analyzer.runtime.service import StockAnalyzerService
from tests.test_service_portfolio import _load_test_config


def _config() -> BoardRiskConfig:
    return BoardRiskConfig()


def _limit_rule() -> LimitRuleConfig:
    return LimitRuleConfig()


def _frame(dates: list[str], closes: list[float], *, symbol: str = "600000") -> pd.DataFrame:
    index = pd.to_datetime(dates)
    prev = [closes[0]] + closes[:-1]
    return pd.DataFrame(
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


def test_triple_limit_up_rejects_new_buy() -> None:
    # 主板 10% 三连板：10.0→11.0→12.1→13.31
    bars = _frame(
        ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"],
        [10.0, 11.0, 12.1, 13.31],
        symbol="600000",
    )
    decision = evaluate_board_risk(
        bars,
        symbol="600000",
        limit_rule=_limit_rule(),
        board_risk_config=_config(),
    )
    assert decision.consecutive_limit_up == 3
    assert decision.reject_new_buy is True
    assert "consecutive_limit_up_3" in decision.reasons
    assert decision.current_limit_state == "limit_up"
    payload = board_decision_to_dict(decision)
    assert payload["reject_new_buy"] is True
    assert payload["consecutive_limit_up"] == 3


def test_double_limit_up_does_not_reject() -> None:
    # 主板 10% 两连板：9.5→10.45→11.495（day2 -5% 非涨停，day3/day4 涨停）
    bars = _frame(
        ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"],
        [10.0, 9.5, 10.45, 11.495],
        symbol="600000",
    )
    decision = evaluate_board_risk(
        bars,
        symbol="600000",
        limit_rule=_limit_rule(),
        board_risk_config=_config(),
    )
    assert decision.consecutive_limit_up == 2
    assert decision.reject_new_buy is False


def test_st_board_uses_5pct_limit() -> None:
    # ST 5%：10.0→10.5 涨停（而非 10%）→ streak 1
    frame = _frame(["2026-08-12", "2026-08-13"], [10.0, 10.5], symbol="600000")
    frame["name"] = ["ST测试", "ST测试"]
    frame["is_st"] = True
    assert (
        consecutive_limit_up_count(
            bars=frame,
            symbol="600000",
            limit_rule=_limit_rule(),
            tolerance=0.001,
        )
        == 1
    )


def test_chi_next_board_uses_20pct_limit() -> None:
    # 创业板 20%：10.0→12.0 涨停 → streak 1
    frame = _frame(["2026-08-12", "2026-08-13"], [10.0, 12.0], symbol="300001")
    assert (
        consecutive_limit_up_count(
            bars=frame,
            symbol="300001",
            limit_rule=_limit_rule(),
            tolerance=0.001,
        )
        == 1
    )
    # 10.0→11.0（10%）不构成创业板涨停 → 0
    frame2 = _frame(["2026-08-12", "2026-08-13"], [10.0, 11.0], symbol="300001")
    assert (
        consecutive_limit_up_count(
            bars=frame2,
            symbol="300001",
            limit_rule=_limit_rule(),
            tolerance=0.001,
        )
        == 0
    )


def test_current_limit_down_state() -> None:
    # 最新日跌停：昨日 12.0 → 今日 10.8 = -10%（主板跌停）
    bars = _frame(
        ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"],
        [13.0, 12.0, 12.0, 10.8],
        symbol="600000",
    )
    decision = evaluate_board_risk(
        bars,
        symbol="600000",
        limit_rule=_limit_rule(),
        board_risk_config=_config(),
    )
    assert decision.current_limit_state == "limit_down"
    assert decision.reject_new_buy is False  # 跌停不构成连板


def test_final_selector_rejects_triple_limit_up_signal() -> None:
    """三连板 signal（board_risk.reject_new_buy=True）不得进入 final signals。"""
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    good_signal = {
        "symbol": "600000",
        "score": 85.0,
        "action": "buy",
        "decision_trace": {
            "risk_gate": {"passed": True},
            "cross_review_gate": {"passed": True},
        },
        "reasons": [],
        "board_risk": {
            "consecutive_limit_up": 3,
            "current_limit_state": "limit_up",
            "board": "主板",
            "reject_new_buy": True,
            "reasons": ["consecutive_limit_up_3"],
        },
    }
    normal_signal = {
        "symbol": "000001",
        "score": 82.0,
        "action": "buy",
        "decision_trace": {
            "risk_gate": {"passed": True},
            "cross_review_gate": {"passed": True},
        },
        "reasons": [],
        "board_risk": {
            "consecutive_limit_up": 1,
            "current_limit_state": "none",
            "board": "主板",
            "reject_new_buy": False,
            "reasons": [],
        },
    }
    result = service._final_signal_selector(
        signals=[good_signal, normal_signal],
        data_gate_status="ok",
    )
    final_symbols = [item["symbol"] for item in result["final_signals"]]
    assert "600000" not in final_symbols
    assert "000001" in final_symbols
    rejected_symbols = {item["symbol"] for item in result["rejected"]}
    assert "600000" in rejected_symbols
    rejected = next(item for item in result["rejected"] if item["symbol"] == "600000")
    assert "board_risk_reject_new_buy" in rejected["reject_reasons"]
