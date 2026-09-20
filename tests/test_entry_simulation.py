"""S02 入场契约：盘后信号 → T+1 真实可成交开盘（不得 T 收盘假成交）。

对应蓝图 §5 P0-05 / 阶段施工提示词 S02 的 Done When：

```text
entry_date > signal_date   或   status = no_fill
```

覆盖用例（阶段提示词要求的最小集）：

- 正常 T+1 开盘成交（含滑点与成本）；
- 停牌不可成交（主口径 no_fill；sensitivity 窗口内顺延）；
- 一字涨停不可成交（``limit_up_open`` + ``one_price_limit_up``）；
- 普通涨停但可成交（开盘在涨停以下、收盘封板 → 开盘可成交）；
- ST 5% / 创业板・科创板 20%（涨跌幅由 limit_rule 的板块规则解析）；
- IPO 无涨跌幅期（无法解析涨停价 → fail-closed no_fill）；
- T+1 卖出规则（成交当日不得卖出）；
- 成本日期版本（买入成本按成交日费率计算；卖出印花税按日期档位）。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.backtest.holding_curve import analyze_symbol_holding
from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig, load_config

_SIGNAL = datetime(2026, 9, 17, 15, 30)
_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def limit_rule() -> LimitRuleConfig:
    return LimitRuleConfig()


@pytest.fixture()
def matcher(limit_rule: LimitRuleConfig) -> ExecutionMatcher:
    return ExecutionMatcher(BacktestMatcherConfig(), limit_rule=limit_rule)


@pytest.fixture(scope="module")
def production_matcher() -> ExecutionMatcher:
    """带真实 ``limit_rule``（涨跌幅档位 + 印花税日期档位）的 matcher。

    IPO 无涨跌幅期与成本日期档位都依赖 ``config/default.yaml`` 里的 schedule 行；
    用 ``LimitRuleConfig()`` 默认值（schedule 为空）测不出这两条真实规则。
    """
    config = load_config(_ROOT / "config" / "default.yaml")
    return ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)


def _bar(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "open": 10.0,
        "high": 10.2,
        "low": 9.9,
        "close": 10.1,
        "pre_close": 10.0,
        "suspended": False,
    }
    base.update(kwargs)
    return base


def _entry(matcher: ExecutionMatcher, bars: list[dict[str, object]], **kwargs: object):
    return matcher.simulate_entry(
        signal_date=_SIGNAL,
        future_bars=[(datetime(2026, 9, 18), bar) for bar in bars],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 基本成交语义
# ---------------------------------------------------------------------------


def test_normal_t_plus_1_open_fill(matcher: ExecutionMatcher) -> None:
    result = _entry(matcher, [_bar(open=10.0, close=10.4)], slippage_ratio=0.001, quantity=1000)
    assert result.executed is True
    assert result.entry_date is not None
    assert result.entry_date.date() == date(2026, 9, 18)
    assert result.entry_delay_days == 1
    assert result.entry_price_raw == pytest.approx(10.0)
    # 买入滑点上调价格；成本 = 佣金/过户费
    assert result.net_entry_price > result.entry_price_raw
    assert result.slippage == pytest.approx(result.net_entry_price - result.entry_price_raw)
    assert result.cost > 0


def test_entry_price_uses_open_not_close(matcher: ExecutionMatcher) -> None:
    """开盘 10.0 / 收盘 12.0 时，成交价必须是开盘价（不是当天收盘价）。"""
    result = _entry(matcher, [_bar(open=10.0, close=12.0)])
    assert result.executed is True
    assert result.entry_price_raw == pytest.approx(10.0)
    assert result.entry_price_raw != pytest.approx(12.0)


def test_no_future_bars_is_no_fill(matcher: ExecutionMatcher) -> None:
    result = _entry(matcher, [])
    assert result.executed is False
    assert result.no_fill_reason == "no_future_bars"
    assert result.entry_date is None


def test_primary_window_rejects_delayed_fill(matcher: ExecutionMatcher) -> None:
    """主口径（max_entry_sessions=1）：T+1 停牌即 no_fill，不得顺延。"""
    result = _entry(
        matcher,
        [
            _bar(suspended=True, close=10.0, open=10.0),
            _bar(open=10.1, close=10.2, pre_close=10.0),
        ],
        max_entry_sessions=1,
    )
    assert result.executed is False
    assert result.no_fill_reason == "suspended"


def test_sensitivity_window_defers_to_next_tradable_open(matcher: ExecutionMatcher) -> None:
    """sensitivity（<=3 个交易日）：T+1 停牌后顺延到 T+2 开盘，并标注延迟。"""
    result = _entry(
        matcher,
        [
            _bar(suspended=True, close=10.0, open=10.0),
            _bar(open=10.1, close=10.2, pre_close=10.0),
        ],
        max_entry_sessions=3,
    )
    assert result.executed is True
    assert result.entry_delay_days == 2
    assert result.entry_price_raw == pytest.approx(10.1)


def test_suspended_never_fills_within_window(matcher: ExecutionMatcher) -> None:
    result = _entry(
        matcher,
        [_bar(suspended=True), _bar(suspended=True), _bar(suspended=True)],
        max_entry_sessions=3,
    )
    assert result.executed is False
    assert result.no_fill_reason == "suspended"
    assert result.deferred_sessions == 3


# ---------------------------------------------------------------------------
# 涨停 / ST / 板块 / IPO
# ---------------------------------------------------------------------------


def test_one_price_limit_up_is_no_fill(matcher: ExecutionMatcher) -> None:
    """一字涨停（open=high=low=close=up_limit）无法买入。"""
    result = _entry(
        matcher,
        [
            _bar(
                open=11.0,
                high=11.0,
                low=11.0,
                close=11.0,
                pre_close=10.0,
            )
        ],
    )
    assert result.executed is False
    assert result.no_fill_reason == "limit_up_open"
    assert result.details["one_price_limit_up"] is True


def test_open_at_limit_but_traded_lower_is_still_no_fill(matcher: ExecutionMatcher) -> None:
    """开盘即涨停但盘中回落：不假设能买到（保守、不可证伪）。"""
    result = _entry(
        matcher,
        [_bar(open=11.0, high=11.0, low=10.6, close=10.7, pre_close=10.0)],
    )
    assert result.executed is False
    assert result.no_fill_reason == "limit_up_open"
    assert result.details["one_price_limit_up"] is False


def test_close_at_limit_up_but_open_below_is_fillable(matcher: ExecutionMatcher) -> None:
    """普通涨停（开盘低于涨停、盘中封板）：T+1 开盘可成交。"""
    result = _entry(
        matcher,
        [_bar(open=10.2, high=11.0, low=10.1, close=11.0, pre_close=10.0)],
    )
    assert result.executed is True
    assert result.entry_price_raw == pytest.approx(10.2)
    assert result.details["close_at_limit_up"] is True


def test_st_board_five_percent_limit(matcher: ExecutionMatcher) -> None:
    """ST：涨停幅度 5%（limit_rule 的 ST 档），开盘触及 5% 即不可成交。"""
    blocked = _entry(
        matcher,
        [_bar(open=10.5, high=10.5, low=10.5, close=10.5, pre_close=10.0, is_st=True)],
    )
    assert blocked.executed is False
    assert blocked.no_fill_reason == "limit_up_open"
    fillable = _entry(
        matcher,
        [_bar(open=10.2, high=10.5, low=10.1, close=10.5, pre_close=10.0, is_st=True)],
    )
    assert fillable.executed is True


@pytest.mark.parametrize("board", ["创业板", "科创板"])
def test_twenty_percent_boards(matcher: ExecutionMatcher, board: str) -> None:
    """创业板/科创板：涨停 20%，开盘触及 12.0（+20%）不可成交，11.0 可成交。"""
    blocked = _entry(
        matcher,
        [_bar(open=12.0, high=12.0, low=12.0, close=12.0, pre_close=10.0, board=board)],
    )
    assert blocked.executed is False
    assert blocked.no_fill_reason == "limit_up_open"
    fillable = _entry(
        matcher,
        [_bar(open=11.0, high=12.0, low=10.9, close=12.0, pre_close=10.0, board=board)],
    )
    assert fillable.executed is True


def test_ipo_no_limit_period_fails_closed(production_matcher: ExecutionMatcher) -> None:
    """IPO 无涨跌幅期（listing_days 在豁免窗口内）：解析不出涨停价 → fail-closed。"""
    result = _entry(
        production_matcher,
        [
            _bar(
                open=13.0,
                high=13.5,
                low=12.8,
                close=13.2,
                pre_close=10.0,
                board="科创板",
                listing_days=1,
            )
        ],
    )
    assert result.executed is False
    assert result.no_fill_reason == "no_valid_price_data"
    # 豁免期结束后（listing_days 超窗口）按 20% 档位解析出涨停价 → 开盘 13.0 已封板
    after_window = _entry(
        production_matcher,
        [
            _bar(
                open=13.0,
                high=13.5,
                low=12.8,
                close=13.2,
                pre_close=10.0,
                board="科创板",
                listing_days=10,
            )
        ],
    )
    assert after_window.executed is False
    assert after_window.no_fill_reason == "limit_up_open"


def test_missing_price_basis_fails_closed(matcher: ExecutionMatcher) -> None:
    """无 pre_close / pct_change 且无 up_limit 时不得猜涨跌停 → no_fill。"""
    bar = {"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "suspended": False}
    result = _entry(matcher, [bar])
    assert result.executed is False
    assert result.no_fill_reason == "no_valid_price_data"


# ---------------------------------------------------------------------------
# T+1 卖出规则与成本日期版本
# ---------------------------------------------------------------------------


def test_entry_day_cannot_be_sold_t_plus_1(matcher: ExecutionMatcher) -> None:
    """成交当日不得卖出：simulate_exit 从成交日的**下一根** bar 开始扫描。"""
    signal_t = date(2026, 1, 5)
    dates = pd.bdate_range(start="2026-01-05", periods=4)
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.0, 10.6],
            "high": [10.0, 10.0, 10.0, 10.6],
            "low": [10.0, 10.0, 10.0, 10.6],
            "close": [10.0, 10.0, 10.0, 10.6],
            "pre_close": [10.0, 10.0, 10.0, 10.0],
            "suspended": [False, False, False, False],
        },
        index=dates,
    )
    result = analyze_symbol_holding(
        symbol="TEST",
        bars=frame,
        entry_date=signal_t,
        matcher=matcher,
        horizon_days=2,
        take_profit_pct=0.05,
    )
    assert result.status == "ok"
    assert result.entry_date == date(2026, 1, 6)
    assert result.matched_exit is not None
    # 止盈线 10.5 在成交日当天就已被满足（close=10.0 除外），关键约束是
    # 退出**不可能**发生在成交日：必须在其后。
    assert result.matched_exit.exit_date.date() > result.entry_date


def test_holding_curve_never_uses_signal_close_as_entry(matcher: ExecutionMatcher) -> None:
    """守卫 S02 验收不变量：即便信号日收盘价诱人，成交价仍取 T+1 开盘。"""
    dates = pd.bdate_range(start="2026-01-05", periods=3)
    frame = pd.DataFrame(
        {
            "open": [8.0, 10.0, 10.1],
            "high": [12.0, 10.2, 10.3],
            "low": [8.0, 9.9, 10.0],
            "close": [12.0, 10.0, 10.2],  # 信号日收盘 12.0（诱人）
            "pre_close": [10.0, 12.0, 10.0],
            "suspended": [False, False, False],
        },
        index=dates,
    )
    result = analyze_symbol_holding(
        symbol="TEST",
        bars=frame,
        entry_date=date(2026, 1, 5),
        matcher=matcher,
        horizon_days=1,
    )
    assert result.status == "ok"
    assert result.entry_price_raw == pytest.approx(10.0)  # T+1 开盘，而非 12.0
    assert result.entry_date > result.signal_date


def test_entry_cost_uses_trade_date_schedule(production_matcher: ExecutionMatcher) -> None:
    """成本按**成交日**的费率档位计算（日期口径端到端可验证）。"""
    before = _entry(production_matcher, [_bar(open=10.0, close=10.0)], quantity=1000)
    cost_from_date = production_matcher.estimate_cost(
        "buy", price=before.net_entry_price, quantity=1000, trade_date=date(2026, 9, 18)
    )
    assert before.cost == pytest.approx(cost_from_date)

    # 日期档位确实参与计算：卖出印花税在 2023-08-28 前后不同（0.1% → 0.05%）
    pre_change = production_matcher.estimate_cost(
        "sell", price=10.0, quantity=10_000, trade_date=date(2023, 8, 25)
    )
    post_change = production_matcher.estimate_cost(
        "sell", price=10.0, quantity=10_000, trade_date=date(2023, 8, 28)
    )
    assert pre_change > post_change


# ---------------------------------------------------------------------------
# 批量不变量（S02 Done When）：entry_date > signal_date，否则 no_fill
# ---------------------------------------------------------------------------


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    dates = pd.bdate_range(start="2026-01-05", periods=len(rows))
    return pd.DataFrame(rows, index=dates)


def test_batch_invariant_no_t_close_fake_fill(matcher: ExecutionMatcher) -> None:
    """混合场景批跑：每只票要么成交且 ``entry_date > signal_date``，要么 no_fill。

    注意：这里的 bars **显式带 up_limit/down_limit 列**（与真实 provider 一致）。
    不带该列时 ``holding_curve._bar_snapshot`` 会补 ``close*1.1/0.9`` 的估算涨跌停，
    从而把真实板块涨跌幅（一字板/20%/ST/IPO）掩盖掉——那是独立缺陷，见
    ``s03+`` 前的 Deferred Findings（DF-S02-002）。
    """
    normal = _frame(
        [
            {"open": 10.0, "high": 10.3, "low": 9.9, "close": 10.0, "pre_close": 10.0,
             "up_limit": 11.0, "down_limit": 9.0, "suspended": False},
            {"open": 10.1, "high": 10.5, "low": 10.0, "close": 10.4, "pre_close": 10.0,
             "up_limit": 11.0, "down_limit": 9.0, "suspended": False},
            {"open": 10.4, "high": 10.6, "low": 10.2, "close": 10.5, "pre_close": 10.4,
             "up_limit": 11.44, "down_limit": 9.36, "suspended": False},
        ]
    )
    suspended = _frame(
        [
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "up_limit": 11.0, "down_limit": 9.0, "suspended": False},
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "up_limit": 11.0, "down_limit": 9.0, "suspended": True},
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "up_limit": 11.0, "down_limit": 9.0, "suspended": False},
        ]
    )
    one_price_limit_up = _frame(
        [
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "up_limit": 11.0, "down_limit": 9.0, "suspended": False},
            {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pre_close": 10.0,
             "up_limit": 11.0, "down_limit": 9.0, "suspended": False},
            {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pre_close": 11.0,
             "up_limit": 12.1, "down_limit": 9.9, "suspended": False},
        ]
    )
    results = {}
    for symbol, frame in (
        ("NORMAL", normal),
        ("SUSPENDED", suspended),
        ("ONE_PRICE_LIMIT_UP", one_price_limit_up),
    ):
        results[symbol] = analyze_symbol_holding(
            symbol=symbol,
            bars=frame,
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=1,
        )

    for symbol, result in results.items():
        if result.status == "no_fill":
            assert result.no_fill_reason, f"{symbol} no_fill 必须带原因"
            assert result.entry_date == date(2026, 1, 5)  # 未成交时保留信号日语义
            continue
        assert result.status == "ok", f"{symbol} 意外状态 {result.status}"
        assert result.signal_date is not None
        assert result.entry_date > result.signal_date, f"{symbol} 出现 T 日假成交"

    assert results["NORMAL"].status == "ok"
    assert results["SUSPENDED"].status == "no_fill"
    assert results["SUSPENDED"].no_fill_reason == "suspended"
    assert results["ONE_PRICE_LIMIT_UP"].status == "no_fill"
    assert results["ONE_PRICE_LIMIT_UP"].no_fill_reason == "limit_up_open"


def test_batch_summary_counts_no_fill_reasons(matcher: ExecutionMatcher) -> None:
    """no_fill 必须进入汇总计数（不可成交的票不进收益统计，但必须可见）。"""
    from stock_analyzer.backtest.holding_curve import analyze_holding_curve

    normal = _frame(
        [
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "suspended": False},
            {"open": 10.0, "high": 10.3, "low": 9.9, "close": 10.2, "pre_close": 10.0,
             "suspended": False},
            {"open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3, "pre_close": 10.2,
             "suspended": False},
        ]
    )
    suspended = _frame(
        [
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "suspended": False},
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "suspended": True},
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "suspended": False},
        ]
    )
    report = analyze_holding_curve(
        bars_by_symbol={"NORMAL": normal, "SUSPENDED": suspended},
        entry_date=date(2026, 1, 5),
        matcher=matcher,
        horizon_days=1,
    )
    assert report.summary.symbol_count == 2
    assert report.summary.ok_count == 1
    assert report.summary.no_fill_count == 1
    assert report.summary.no_fill_reason_counts == {"suspended": 1}
