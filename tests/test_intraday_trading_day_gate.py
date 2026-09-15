"""必需交易日门口径：停牌剔出分母 + 低流动性分钟根数重定标。

背景（2026-09-14 NAS 实测，night_scan 连续 ``blocked_data_gate``）：

- 门是比例门 ``fresh_ratio >= 0.95``（``week5_selection_engine``）。当晚 7/100 陈旧
  （0.93）触发拦截，而 9/11 是 3/100（0.97）放行。
- 这 7 只分两类：``600929`` / ``605577`` 当日**停牌**（日线 8/28→9/14 与
  9/8→9/14 断档，两侧都有 bar），其余 5 只当日有成交但量小、行情源省略无成交分钟
  （216~228 根）。把停牌算成数据故障、把低流动性算成会话不完整，都会让陈旧集合
  只增不减并最终永久拦死夜扫。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from stock_analyzer.data.intraday_sync import (
    SESSION_COMPLETE_MINUTE_THRESHOLD,
    SESSION_COMPLETE_MINUTE_THRESHOLD_5M,
    _check_minute_session_completeness,
)
from stock_analyzer.ops.intraday_freshness import (
    NOT_TRADING,
    TRADE_STATE_UNKNOWN,
    TRADED,
    build_intraday_freshness_report,
    resolve_daily_trade_state,
)

REQUIRED = date(2026, 9, 11)


class _DailyWarehouse:
    """只实现 freshness 用到的日线/分钟读取的最小仓库替身。"""

    def __init__(
        self,
        *,
        daily: dict[str, list[date]] | None = None,
        intraday_latest: dict[str, date] | None = None,
        minute_counts: dict[tuple[str, date], int] | None = None,
    ) -> None:
        self._daily = daily or {}
        self._intraday_latest = intraday_latest or {}
        self._minute_counts = minute_counts or {}

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        days = [d for d in self._daily.get(symbol, []) if end_date is None or d <= end_date]
        if not days:
            return pd.DataFrame()
        frame = pd.DataFrame(
            {"close": [1.0] * len(days)}, index=pd.to_datetime(days[-lookback_days:])
        )
        return frame.sort_index()

    def latest_intraday_dates(self, *, interval: str, symbols: list[str]) -> dict[str, date]:
        return {s: self._intraday_latest[s] for s in symbols if s in self._intraday_latest}

    def fetch_intraday_summary(
        self, symbol: str, interval: str = "1m", lookback_days: int = 10
    ) -> pd.DataFrame:
        counts = {d: n for (s, d), n in self._minute_counts.items() if s == symbol}
        if not counts:
            return pd.DataFrame()
        frame = pd.DataFrame(
            {"minute_count": list(counts.values())},
            index=pd.to_datetime(list(counts.keys())),
        )
        return frame

    # BJ 探测走这条；所有替身票都返回非北交所板块。
    def fetch_all_daily_bars(self, *, symbol: str) -> pd.DataFrame:
        days = self._daily.get(symbol, [])
        if not days:
            return pd.DataFrame()
        frame = pd.DataFrame({"date": pd.to_datetime(days), "board": "sz_main"})
        return frame.set_index("date").sort_index()


def _report(warehouse: _DailyWarehouse, symbols: list[str]):
    return build_intraday_freshness_report(
        warehouse=warehouse,
        vendor_overlay=None,
        symbols=symbols,
        required_trade_date=REQUIRED,
        interval="1m",
        deep_candidate_target=1,
    )


# --- resolve_daily_trade_state 的三种判定 -------------------------------------


def test_suspension_window_confirms_not_trading() -> None:
    """必需日两侧都有日线、只缺当日 → 确证停牌（600929 / 605577 的真实形态）。"""
    warehouse = _DailyWarehouse(
        daily={"600929": [date(2026, 8, 27), date(2026, 8, 28), date(2026, 9, 14)]}
    )
    assert resolve_daily_trade_state(warehouse, "600929", REQUIRED) == NOT_TRADING


def test_bar_on_required_date_is_traded() -> None:
    warehouse = _DailyWarehouse(daily={"605058": [date(2026, 9, 10), REQUIRED, date(2026, 9, 14)]})
    assert resolve_daily_trade_state(warehouse, "605058", REQUIRED) == TRADED


def test_missing_after_bar_is_unknown_not_not_trading() -> None:
    """只有左侧有日线（右侧无 bar）无法区分停牌与数据断供 → fail-closed 回 unknown。"""
    warehouse = _DailyWarehouse(
        daily={"301108": [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)]}
    )
    assert resolve_daily_trade_state(warehouse, "301108", REQUIRED) == TRADE_STATE_UNKNOWN


def test_warehouse_without_capability_is_unknown() -> None:
    assert resolve_daily_trade_state(None, "600929", REQUIRED) == TRADE_STATE_UNKNOWN
    assert resolve_daily_trade_state(object(), "600929", REQUIRED) == TRADE_STATE_UNKNOWN


# --- 报告层：停牌剔出分母 ----------------------------------------------------


def test_suspended_symbol_leaves_denominator_and_stale_list() -> None:
    """停牌票不进 effective_stale/summary_missing，也不占新鲜度预算。"""
    warehouse = _DailyWarehouse(
        daily={
            "600929": [date(2026, 8, 27), date(2026, 8, 28), date(2026, 9, 14)],
            "000088": [date(2026, 9, 10), REQUIRED, date(2026, 9, 14)],
        },
        intraday_latest={"000088": REQUIRED},
        minute_counts={("000088", REQUIRED): 238},
    )
    report = _report(warehouse, ["600929", "000088"])

    assert report.not_trading == ["600929"]
    assert "600929" not in report.effective_stale
    assert "600929" not in report.summary_missing
    assert "600929" not in report.delta_missing
    assert report.fresh_symbols == ["000088"]
    # 分母只剩 000088，且它是新鲜的 → 100%（旧口径会是 1/2 = 50%）
    assert report.fresh_ratio == 1.0
    assert report.source_breakdown["not_trading"] == 1


def test_all_suspended_keeps_ratio_fail_closed() -> None:
    """全部停牌时分母为空——比率保持 fail-closed 的 0.0，不伪造成「全都新鲜」。

    此时拦截应当来自 ``fresh_count < deep_candidate_target``（一根分钟数据都没有，
    漏斗本来就无事可做），而不是来自被停牌票挤占的分母。若这里改成 1.0，等于让
    「一只都没数据」的夜晚静默通过比例门。
    """
    warehouse = _DailyWarehouse(
        daily={"600929": [date(2026, 8, 28), date(2026, 9, 14)]},
    )
    report = _report(warehouse, ["600929"])
    assert report.not_trading == ["600929"]
    assert report.effective_stale == []
    assert report.fresh_count == 0
    assert report.fresh_ratio == 0.0


# --- 阈值重定标：低流动性放行、真截断仍拦 ------------------------------------


def _minute_frame(day: date, count: int, *, first: str = "09:30", last: str = "15:00"):
    stamps = pd.date_range(f"{day.isoformat()} {first}", f"{day.isoformat()} {last}", periods=count)
    return pd.DataFrame({"close": [1.0] * count}, index=stamps)


def test_illiquid_session_is_complete() -> None:
    """实测低流动性尾部（216~228 根）现在算完整——它们只是无成交分钟被省略。"""
    for count in (216, 224, 226, 228):
        ok, reason = _check_minute_session_completeness(_minute_frame(REQUIRED, count), REQUIRED)
        assert ok is True, (count, reason)


def test_morning_only_truncation_still_rejected() -> None:
    """截断仍由墙钟检查拦下：只取到上午盘 → 末根 11:30 < 14:55。"""
    ok, reason = _check_minute_session_completeness(
        _minute_frame(REQUIRED, 150, first="09:30", last="11:30"), REQUIRED
    )
    assert ok is False
    assert reason.startswith("last_bar_early")


def test_half_day_bar_count_still_rejected() -> None:
    """根数下限仍有下界：半日量级（120 根）即便墙钟凑齐也不能算完整。"""
    ok, reason = _check_minute_session_completeness(_minute_frame(REQUIRED, 150), REQUIRED)
    assert ok is False
    assert reason.startswith("insufficient_bars")


def test_thresholds_are_single_sourced() -> None:
    """读数侧与写数侧必须引用同一常量，防止两处各自漂移。"""
    import stock_analyzer.ops.intraday_freshness as freshness

    assert freshness.SESSION_COMPLETE_MINUTE_THRESHOLD == SESSION_COMPLETE_MINUTE_THRESHOLD
    assert freshness.SESSION_COMPLETE_MINUTE_THRESHOLD_5M == SESSION_COMPLETE_MINUTE_THRESHOLD_5M
    # 1m 阈值必须低于实测低流动性尾部（216），且高于半日量级（≈120）。
    assert 120 < SESSION_COMPLETE_MINUTE_THRESHOLD < 216
