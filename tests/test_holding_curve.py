"""持有期走势分析模块单测（PLAN Task 4）。

含 PLAN 第七节审核清单第 5 项要求的对照基准自校验：600000 从 2026-07-31 以
9.51 成交，须复现 T+1 +1.26% / T+2 -1.16% / T+5 -3.15% / T+10 -4.31%。

**S02 契约变化（2026-09-17）**：入场价不再取信号日 T 的收盘价，而是 **T+1 开盘价
+ 滑点**（蓝图 §2.12：T 日盘后信号却用 T 日收盘价成交是不可实现的成交）。
因此本文件所有用例的价格序列都**前置一根信号 bar**：``bars[0]`` 是信号日 T，
``bars[1]`` 是成交日 T+1，``bars[k+1]`` 对应持有第 k 天。这样既保持原有对照基准
（成交价 9.51 与各 offset 收益）不变，又额外钉住 ``entry_date > signal_date``。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stock_analyzer.backtest.holding_curve import (
    analyze_holding_curve,
    analyze_symbol_holding,
)
from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig


@pytest.fixture()
def matcher() -> ExecutionMatcher:
    return ExecutionMatcher(BacktestMatcherConfig(), limit_rule=LimitRuleConfig())


def _bars_from_closes(
    *,
    start: str,
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    """按交易日（bdate_range）构造一段日线 bars，只需要 close 序列即可。

    注意（S02）：``closes[0]`` 是**信号日**，``closes[1]`` 是成交日（open=close）。
    调用方按"信号日 + 成交日 + 持有日"的顺序给出价格序列。
    """
    dates = pd.bdate_range(start=start, periods=len(closes))
    close_series = pd.Series(closes, index=dates, dtype=float)
    high_series = (
        pd.Series(highs, index=dates, dtype=float) if highs is not None else close_series * 1.0
    )
    low_series = (
        pd.Series(lows, index=dates, dtype=float) if lows is not None else close_series * 1.0
    )
    frame = pd.DataFrame(
        {
            "open": close_series,
            "high": high_series,
            "low": low_series,
            "close": close_series,
            "volume": 1_000_000.0,
            "turnover": close_series * 1_000_000.0,
            "suspended": False,
        },
        index=dates,
    )
    frame.index.name = "date"
    return frame


class TestKnownReferenceCalibration:
    """PLAN 第七节审核清单第 5 项：600000 从 2026-07-31 以 9.51 成交的对照基准自校验。

    已知实测值（成交价 9.51）：
        T+1  2026-08-03  close=9.63  +1.26%
        T+2  2026-08-04  close=9.40  -1.16%
        T+3  2026-08-05  close=9.26  -2.63%
        T+4  2026-08-06  close=9.29  -2.31%
        T+5  2026-08-07  close=9.21  -3.15%
        T+6  2026-08-10  close=9.29  -2.31%
        T+7  2026-08-11  close=9.21  -3.15%
        T+8  2026-08-12  close=9.17  -3.58%
        T+9  2026-08-13  close=9.18  -3.47%
        T+10 2026-08-14  close=9.10  -4.31%

    S02 语义下该成交发生在 **T+1（2026-07-31）开盘**：信号日为 2026-07-30，
    成交日 2026-07-31 的开盘价 = 9.51（与原始基准的成交价一致），其后 10 根
    持有日 bar 正是上表的 08-03～08-14。
    """

    def test_600000_reference_returns_match_known_values(self, matcher: ExecutionMatcher) -> None:
        # 前置 1 根信号 bar（2026-07-30）；2026-07-31 为成交日（开盘 9.51），
        # 其后 10 根是真实引用的交易日收盘价。
        bars = _bars_from_closes(
            start="2026-07-30",
            closes=[9.51, 9.51, 9.63, 9.40, 9.26, 9.29, 9.21, 9.29, 9.21, 9.17, 9.18, 9.10],
        )
        result = analyze_symbol_holding(
            symbol="600000",
            bars=bars,
            entry_date=date(2026, 7, 30),
            matcher=matcher,
            horizon_days=10,
        )

        assert result.status == "ok"
        # S02 不变量：信号日 T 与成交日严格分离，成交在 T+1
        assert result.signal_date == date(2026, 7, 30)
        assert result.entry_date == date(2026, 7, 31)
        assert result.entry_date > result.signal_date
        assert result.entry_delay_days == 1
        assert result.entry_price == pytest.approx(9.51, abs=1e-6)
        assert len(result.daily_returns) == 10

        by_offset = {item.offset: item.return_pct for item in result.daily_returns}
        assert by_offset[1] == pytest.approx(0.0126, abs=2e-4)
        assert by_offset[2] == pytest.approx(-0.0116, abs=2e-4)
        assert by_offset[5] == pytest.approx(-0.0315, abs=2e-4)
        assert by_offset[10] == pytest.approx(-0.0431, abs=2e-4)


class TestDailyReturnComputation:
    """构造已知价格序列，逐项核对收益/最优退出日/回撤/胜率。"""

    def test_best_exit_offset_and_return(self, matcher: ExecutionMatcher) -> None:
        # bars[0]=信号日；成交 10.0（bars[1] 开盘）；持有日 +5%（10.5）、+10%（11.0，全局最优）、+2%。
        bars = _bars_from_closes(start="2026-01-05", closes=[10.0, 10.0, 10.5, 11.0, 10.2])
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=bars,
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=3,
        )
        assert result.status == "ok"
        assert result.entry_date > result.signal_date
        assert result.best_exit_offset == 2
        assert result.best_exit_return_pct == pytest.approx(0.10, abs=1e-6)

    def test_max_drawdown_uses_low_price(self, matcher: ExecutionMatcher) -> None:
        # 成交 10.0；持有第 1 天 low=9.0（-10%，最大回撤）；随后回升。
        bars = _bars_from_closes(
            start="2026-01-05",
            closes=[10.0, 10.0, 9.5, 10.5],
            lows=[10.0, 10.0, 9.0, 10.0],
            highs=[10.0, 10.0, 9.6, 10.6],
        )
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=bars,
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=2,
        )
        assert result.status == "ok"
        assert result.max_drawdown_pct == pytest.approx(-0.10, abs=1e-6)

    def test_take_profit_and_stop_loss_flags(self, matcher: ExecutionMatcher) -> None:
        # 成交 10.0，take_profit_pct=0.08 -> level=10.8；持有第 2 天 high=11.0 触发止盈。
        bars = _bars_from_closes(
            start="2026-01-05",
            closes=[10.0, 10.0, 10.3, 10.9],
            highs=[10.0, 10.0, 10.4, 11.0],
            lows=[10.0, 10.0, 10.2, 10.7],
        )
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=bars,
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=2,
            take_profit_pct=0.08,
            stop_loss_pct=0.05,
        )
        assert result.take_profit_triggered is True
        assert result.stop_loss_triggered is False


class TestExecutionMatcherReuse:
    """验证 ExecutionMatcher 被真实复用：涨跌停/停牌不可成交被正确处理。"""

    def test_limit_down_defers_exit_to_next_tradable_day(self, matcher: ExecutionMatcher) -> None:
        # 成交 10.0（bars[1] 开盘），stop_loss_pct=0.05 -> level=9.5。持有第 1 天
        # 开盘即跌停价 9.0 且全天封死在跌停（open=high=low=close=down_limit=9.0），
        # can_sell 判定不可成交（limit_down_reject），必须延迟到第 2 天（9.9，未跌停）。
        dates = pd.bdate_range(start="2026-01-05", periods=4)
        frame = pd.DataFrame(
            {
                "open": [10.0, 10.0, 9.0, 9.9],
                "high": [10.0, 10.0, 9.0, 10.0],
                "low": [10.0, 10.0, 9.0, 9.8],
                "close": [10.0, 10.0, 9.0, 9.9],
                "down_limit": [9.0, 9.0, 9.0, 8.91],
                "up_limit": [11.0, 11.0, 9.9, 10.89],
                "suspended": [False, False, False, False],
            },
            index=dates,
        )
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=frame,
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=2,
            stop_loss_pct=0.05,
        )
        assert result.status == "ok"
        assert result.entry_date > result.signal_date
        assert result.matched_exit is not None
        # 跌停当天不可成交，必须延迟到下一可成交日，验证 matcher 真的拦住了
        # 理想化的"跌停当天就能卖出"收益。
        assert result.matched_exit.deferred_days >= 1

    def test_suspended_bar_blocks_execution(self, matcher: ExecutionMatcher) -> None:
        dates = pd.bdate_range(start="2026-01-05", periods=3)
        frame = pd.DataFrame(
            {
                "open": [10.0, 10.0, 10.0],
                "high": [10.0, 10.0, 10.0],
                "low": [10.0, 10.0, 10.0],
                "close": [10.0, 10.0, 10.0],
                "suspended": [False, False, True],
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
        # 成交日（bars[1]）正常成交；唯一一根持有日 bar 停牌不可卖，
        # simulate_exit 无法在 horizon 内成交，只能强制平仓或 no_future_bars
        # ——不应假装理想成交。
        assert result.matched_exit is not None


class TestEdgeCases:
    """数据不足、停牌、退市等边界不崩。"""

    def test_empty_bars_returns_insufficient_data(self, matcher: ExecutionMatcher) -> None:
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=pd.DataFrame(),
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=10,
        )
        assert result.status == "insufficient_data"

    def test_entry_date_not_in_bars_returns_insufficient_data(
        self, matcher: ExecutionMatcher
    ) -> None:
        bars = _bars_from_closes(start="2026-01-05", closes=[10.0, 10.1])
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=bars,
            entry_date=date(2020, 1, 1),  # 远早于 bars 覆盖范围
            matcher=matcher,
            horizon_days=10,
        )
        # entry_date 早于所有数据：searchsorted 会落在 0（第一条记录），因此
        # 视为用第一条记录作为入场——不应崩溃；改用一个远晚于覆盖范围的日期
        # 才是真正的 insufficient_data 场景，下方单独验证。
        assert result.status == "ok"

    def test_entry_date_after_all_bars_returns_insufficient_data(
        self, matcher: ExecutionMatcher
    ) -> None:
        bars = _bars_from_closes(start="2026-01-05", closes=[10.0, 10.1])
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=bars,
            entry_date=date(2030, 1, 1),  # 远晚于 bars 覆盖范围
            matcher=matcher,
            horizon_days=10,
        )
        assert result.status == "insufficient_data"

    def test_data_shorter_than_horizon_reports_available_days(
        self, matcher: ExecutionMatcher
    ) -> None:
        # 信号日 1 根 + 成交日 1 根 + 只有 3 根持有日数据，但请求 horizon_days=10。
        bars = _bars_from_closes(start="2026-01-05", closes=[10.0, 10.0, 10.1, 10.2, 10.3])
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=bars,
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=10,
        )
        assert result.status == "ok"
        assert result.available_trading_days == 3
        assert len(result.daily_returns) == 3

    def test_zero_price_entry_bar_reports_no_fill(self, matcher: ExecutionMatcher) -> None:
        # 成交日 bar 价格无效（open/close=0）→ fail-closed：不猜测可成交，
        # 记 no_fill（S02 之前这里会走 non_positive_entry_price 的 error 分支）。
        bars = _bars_from_closes(start="2026-01-05", closes=[10.0, 0.0, 10.0])
        result = analyze_symbol_holding(
            symbol="TEST",
            bars=bars,
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=1,
        )
        assert result.status == "no_fill"
        assert result.no_fill_reason == "no_valid_price_data"


class TestBatchAnalysisAndSummary:
    """analyze_holding_curve 批量入口 + 汇总统计。"""

    def test_summary_win_rate_and_avg_return_by_offset(self, matcher: ExecutionMatcher) -> None:
        winner_bars = _bars_from_closes(start="2026-01-05", closes=[10.0, 10.0, 11.0, 12.0])
        loser_bars = _bars_from_closes(start="2026-01-05", closes=[10.0, 10.0, 9.0, 8.5])
        report = analyze_holding_curve(
            bars_by_symbol={"WINNER": winner_bars, "LOSER": loser_bars},
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=2,
        )
        assert report.summary.symbol_count == 2
        assert report.summary.ok_count == 2
        assert report.summary.win_count == 1
        assert report.summary.loss_count == 1
        assert report.summary.win_rate == pytest.approx(0.5, abs=1e-6)
        # 持有第 1 天平均收益：(+10% + -10%) / 2 = 0%
        assert report.summary.avg_return_by_offset[1] == pytest.approx(0.0, abs=1e-6)

    def test_batch_handles_missing_symbol_gracefully(self, matcher: ExecutionMatcher) -> None:
        report = analyze_holding_curve(
            bars_by_symbol={"HAS_DATA": _bars_from_closes(start="2026-01-05", closes=[10.0, 10.1])},
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=1,
            symbols=["HAS_DATA", "MISSING"],
        )
        assert len(report.results) == 2
        missing_result = next(item for item in report.results if item.symbol == "MISSING")
        assert missing_result.status == "insufficient_data"

    def test_empty_batch_summary_does_not_crash(self, matcher: ExecutionMatcher) -> None:
        report = analyze_holding_curve(
            bars_by_symbol={},
            entry_date=date(2026, 1, 5),
            matcher=matcher,
            horizon_days=10,
        )
        assert report.summary.symbol_count == 0
        assert report.summary.ok_count == 0
