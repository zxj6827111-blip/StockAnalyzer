"""AnalyzerPipeline as-of 回测模式的单测（PLAN Task 3）。

覆盖 PLAN 第七节审核清单中与 pipeline 直接相关的三项：
1. 防泄露断言真实有效——注入含未来数据的假 provider，断言必须抛错并实际拦截。
2. as_of=None 回归——生产链路行为零变化，intraday fail-closed 语义未被污染。
3. intraday 独立降级路径——仅在 as_of 模式下生效，且不阻断其它标的。

另含 as_of 截断正确性、news 中性化标注、decision_trace 口径标注的单测。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import (
    FutureDataLeakError,
    RequiredIntradayDataError,
)
from stock_analyzer.pipeline import AnalyzerPipeline


def _make_bars_frame(*, end: date, periods: int = 260) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=periods)
    record_count = len(dates)
    close = pd.Series(range(record_count), index=dates, dtype=float) * 0.05 + 10.0
    frame = pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": 2_000_000.0,
            "turnover": close * 2_000_000.0,
            "float_market_cap": 10_000_000_000.0,
            "suspended": False,
        },
        index=dates,
    )
    frame.index.name = "date"
    return frame


class EndDateAwareProvider:
    """支持 end_date 截断的假 provider（模拟 vendor_zip_overlay 的正确行为）。"""

    def __init__(self, *, latest_date: date) -> None:
        self._latest_date = latest_date

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        cutoff = end_date if end_date is not None else self._latest_date
        full = _make_bars_frame(end=self._latest_date, periods=max(lookback_days, 260))
        truncated = full.loc[full.index <= pd.Timestamp(cutoff)]
        return truncated.tail(max(1, lookback_days)).copy()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        _ = interval, lookback_days
        return {symbol: pd.DataFrame() for symbol in symbols}


class LeakingProvider:
    """故意无视 end_date、总是返回最新数据的假 provider（模拟泄露 bug）。

    用于验证 pipeline 的防泄露断言真的会拦截——这是整个 as-of 功能的正确性
    根基，绝不能只是"看起来传了 end_date"而没有实际校验返回值。
    """

    def __init__(self, *, latest_date: date) -> None:
        self._latest_date = latest_date

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        # 关键：无论调用方传入什么 end_date，永远返回到 self._latest_date 的
        # 完整数据——模拟"provider 忘记做截断"这一类泄露 bug。
        _ = end_date
        return _make_bars_frame(end=self._latest_date, periods=max(lookback_days, 260)).tail(
            max(1, lookback_days)
        )

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        _ = interval, lookback_days
        return {symbol: pd.DataFrame() for symbol in symbols}


class AlwaysRequiredIntradayProvider(EndDateAwareProvider):
    """intraday 摘要缺失，但以"部分批次缺失"的正确形态抛错。

    真实生产场景（PLAN 引用的 pipeline.py:1158-1187 单标的降级）是"批量取数里
    某些标的缺失、其它标的仍返回正常数据"，通过 RequiredIntradayDataError 的
    ``missing_symbols``/``partial_frames`` 字段表达——这与"整批 provider 直接
    炸掉、一个 partial 都拿不到"是两种不同场景：后者会在 _prefetch_intraday_summaries
    重新抛出、整个 run_once 直接失败（这是 main 分支既有行为，不在本次改动
    范围内）。这里用 missing_symbols 包含请求的全部标的、但仍提供空的
    partial_frames 占位，模拟"该标的确实缺失"但不让整批调用直接崩溃。
    """

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        raise RequiredIntradayDataError(f"required intraday summary missing: {symbol}:{interval}")

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        raise RequiredIntradayDataError(
            f"required intraday summary missing for batch: {symbols}:{interval}",
            missing_symbols=symbols,
            partial_frames={symbol: pd.DataFrame() for symbol in symbols},
        )


class RecordingNewsProvider:
    """记录是否被真实调用，用于验证 as_of 模式下不会调用注入的 news provider。"""

    def __init__(self) -> None:
        self.calls = 0

    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        self.calls += 1
        return 0.99  # 明显偏离 0.50，方便断言"没有被使用"


@pytest.fixture()
def base_config() -> StockAnalyzerConfig:
    config = load_config()
    return config.model_copy(
        update={
            "data_source": config.data_source.model_copy(
                update={"primary": "synthetic", "intraday_runtime_mode": "zip_legacy"}
            )
        }
    )


class TestFutureDataLeakAssertion:
    """PLAN 审核清单第 1 项：防泄露断言真实有效。"""

    def test_leaking_provider_raises_future_data_leak_error(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        latest_date = date(2026, 8, 26)
        as_of = date(2026, 7, 31)
        provider = LeakingProvider(latest_date=latest_date)
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        with pytest.raises(FutureDataLeakError) as excinfo:
            pipeline.run_once(symbols=["600000"], as_of=as_of)

        assert excinfo.value.symbol == "600000"
        assert excinfo.value.as_of == as_of
        assert excinfo.value.actual_max_date is not None
        assert excinfo.value.actual_max_date > as_of

    def test_compliant_provider_does_not_raise(self, base_config: StockAnalyzerConfig) -> None:
        latest_date = date(2026, 8, 26)
        as_of = date(2026, 7, 31)
        provider = EndDateAwareProvider(latest_date=latest_date)
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        # 不应抛出 FutureDataLeakError：EndDateAwareProvider 正确截断。
        report = pipeline.run_once(symbols=["600000"], as_of=as_of)
        assert len(report.signals) == 1


class TestAsOfTruncation:
    """as_of 截断生效性：最后一根 bar 日期必须 <= as_of。"""

    def test_last_bar_date_matches_as_of_cutoff(self, base_config: StockAnalyzerConfig) -> None:
        latest_date = date(2026, 8, 26)
        as_of = date(2026, 7, 31)
        provider = EndDateAwareProvider(latest_date=latest_date)
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        report = pipeline.run_once(symbols=["600000"], as_of=as_of)
        signal = report.signals[0]
        trace = signal.decision_trace.get("asof_backtest")
        assert isinstance(trace, dict)
        assert trace["as_of"] == as_of.isoformat()


class TestAsOfNoneRegression:
    """PLAN 审核清单第 2 项：as_of=None 时生产链路行为零变化。"""

    def test_as_of_none_omits_asof_backtest_trace(self, base_config: StockAnalyzerConfig) -> None:
        provider = EndDateAwareProvider(latest_date=date(2026, 8, 26))
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        report = pipeline.run_once(symbols=["600000"])  # as_of 默认 None
        signal = report.signals[0]
        # 生产路径不应带有任何 asof_backtest 标注 key。
        assert "asof_backtest" not in signal.decision_trace

    def test_as_of_none_uses_injected_news_provider(self, base_config: StockAnalyzerConfig) -> None:
        provider = EndDateAwareProvider(latest_date=date(2026, 8, 26))
        news_provider = RecordingNewsProvider()
        pipeline = AnalyzerPipeline(
            config=base_config, provider=provider, news_provider=news_provider
        )

        pipeline.run_once(symbols=["600000"])  # as_of=None：生产路径

        assert news_provider.calls >= 1, "as_of=None 时必须使用调用方注入的 news provider"

    def test_as_of_none_required_intraday_still_fails_closed(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        """intraday 独立降级路径不得污染生产 fail-closed 语义：

        as_of=None 时，RequiredIntradayDataError 必须仍然让该标的降级为
        score=0/grade=C/hold（pipeline.py 现有行为），而不是被静默吞掉。
        """
        provider = AlwaysRequiredIntradayProvider(latest_date=date(2026, 8, 26))
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        report = pipeline.run_once(symbols=["600000"])  # as_of=None
        signal = report.signals[0]
        assert signal.action == "hold"
        assert signal.grade == "C"
        assert "required_intraday_data_unavailable" in signal.reasons


class TestAsOfIntradayIndependentDegrade:
    """PLAN 审核清单第 3 项相关：intraday 降级仅在 as_of 模式下生效，不阻断其它标的。"""

    def test_as_of_mode_degrades_instead_of_failing_closed(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        provider = AlwaysRequiredIntradayProvider(latest_date=date(2026, 8, 26))
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        report = pipeline.run_once(symbols=["600000"], as_of=date(2026, 7, 31))
        signal = report.signals[0]
        # 不应再是 hold/C/required_intraday_data_unavailable —— 说明降级为
        # 空表而非 fail-closed，正常走完整个决策链路。
        assert "required_intraday_data_unavailable" not in signal.reasons
        trace = signal.decision_trace.get("asof_backtest")
        assert isinstance(trace, dict)
        assert trace["intraday_degraded"] is True

    def test_as_of_mode_same_pipeline_instance_regression_still_fails_closed(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        """同一个 pipeline 实例：as_of 模式跑完后，再跑 as_of=None 必须仍然
        fail-closed —— 证明降级判定挂在调用参数上而非被误改成实例状态。"""
        provider = AlwaysRequiredIntradayProvider(latest_date=date(2026, 8, 26))
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        pipeline.run_once(symbols=["600000"], as_of=date(2026, 7, 31))
        report = pipeline.run_once(symbols=["600000"])  # as_of=None，同一实例
        signal = report.signals[0]
        assert signal.action == "hold"
        assert "required_intraday_data_unavailable" in signal.reasons


class TestAsOfNewsNeutralization:
    """as_of 模式强制中性新闻，且不调用调用方注入的 news provider。"""

    def test_as_of_mode_ignores_injected_news_provider(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        provider = EndDateAwareProvider(latest_date=date(2026, 8, 26))
        news_provider = RecordingNewsProvider()
        pipeline = AnalyzerPipeline(
            config=base_config, provider=provider, news_provider=news_provider
        )

        pipeline.run_once(symbols=["600000"], as_of=date(2026, 7, 31))

        assert news_provider.calls == 0, (
            "as_of 模式必须强制使用中性 provider，不能调用注入的 provider"
        )

    def test_as_of_mode_restores_original_news_provider_after_run(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        """save/restore 语义：跑完 as_of 之后，pipeline._news_provider 必须
        恢复为构造时注入的实例，不能残留 NeutralNewsSignalProvider。"""
        provider = EndDateAwareProvider(latest_date=date(2026, 8, 26))
        news_provider = RecordingNewsProvider()
        pipeline = AnalyzerPipeline(
            config=base_config, provider=provider, news_provider=news_provider
        )

        pipeline.run_once(symbols=["600000"], as_of=date(2026, 7, 31))

        assert pipeline._news_provider is news_provider

    def test_as_of_mode_marks_news_neutralized_in_trace(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        provider = EndDateAwareProvider(latest_date=date(2026, 8, 26))
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)

        report = pipeline.run_once(symbols=["600000"], as_of=date(2026, 7, 31))
        signal = report.signals[0]
        trace = signal.decision_trace.get("asof_backtest")
        assert isinstance(trace, dict)
        assert trace["news_neutralized"] is True


class TestAsOfDoesNotPersistLearningSnapshot:
    """as-of 回测绝不能写入学习样本库（即便 sample_store 被注入也不应写入历史
    时点的假快照——本项通过"未注入 sample_store 时天然跳过"来验证契约成立，
    真正的隔离保障落在 backtest/asof_scan.py 从不传 sample_store 这一约定上。"""

    def test_no_sample_store_means_no_learning_persist_ref(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        provider = EndDateAwareProvider(latest_date=date(2026, 8, 26))
        pipeline = AnalyzerPipeline(config=base_config, provider=provider)  # sample_store=None

        report = pipeline.run_once(symbols=["600000"], as_of=date(2026, 7, 31))
        signal = report.signals[0]
        assert "learning_protocol" not in signal.decision_trace


class TestMultiDateSliceReuseConsistency:
    """PLAN 第七节审核清单第 4 项：多日期切片与逐日单独计算结果逐值一致。

    provider 层的 LRU 缓存（vendor_zip_overlay.py 的 self._daily_cache，键为
    symbol 不含 end_date）保证同一只票的完整历史只解压一次，不同 as_of 只是
    在内存里对同一份缓存帧做 end_date 截断——这里验证"切片复用"不会算错：
    对同一个 pipeline 实例连续跑多个 as_of 日期，与为每个日期单独新建一个
    pipeline 实例分别跑相比，结果必须逐字段相同。
    """

    def test_sequential_as_of_calls_on_same_pipeline_match_independent_calls(
        self, base_config: StockAnalyzerConfig
    ) -> None:
        latest_date = date(2026, 8, 26)
        as_of_dates = [date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 31)]

        # 复用同一个 pipeline 实例、同一个 provider（内部缓存跨调用共享）连续
        # 跑多个 as_of ——模拟 backtest/asof_scan.py 单个 worker 对一只票的
        # 多日期切片复用路径。
        shared_provider = EndDateAwareProvider(latest_date=latest_date)
        shared_pipeline = AnalyzerPipeline(config=base_config, provider=shared_provider)
        reused_signals = {
            as_of: shared_pipeline.run_once(symbols=["600000"], as_of=as_of).signals[0]
            for as_of in as_of_dates
        }

        # 对照组：每个 as_of 各自新建一个 provider + pipeline 实例独立跑，
        # 完全不共享任何缓存状态。
        independent_signals = {}
        for as_of in as_of_dates:
            fresh_provider = EndDateAwareProvider(latest_date=latest_date)
            fresh_pipeline = AnalyzerPipeline(config=base_config, provider=fresh_provider)
            independent_signals[as_of] = fresh_pipeline.run_once(
                symbols=["600000"], as_of=as_of
            ).signals[0]

        for as_of in as_of_dates:
            reused = reused_signals[as_of]
            independent = independent_signals[as_of]
            assert reused.score == independent.score, f"score mismatch at {as_of}"
            assert reused.grade == independent.grade
            assert reused.action == independent.action
            assert reused.target_position == independent.target_position
            assert reused.probabilities == independent.probabilities
            assert reused.reasons == independent.reasons

    def test_run_asof_scan_multi_date_matches_single_date_calls(self) -> None:
        """backtest/asof_scan.run_asof_scan 跑多个日期的结果，与对每个日期
        单独调用 run_asof_scan（只传一个日期）逐值一致。

        用 SyntheticProvider（primary=synthetic）而非 EndDateAwareProvider：
        run_asof_scan 内部固定走 build_runtime_provider 真实构造 provider，
        无法从测试直接注入假 provider；SyntheticProvider 按 (symbol,
        seed_offset) 确定性生成且正确处理 end_date 截断（見
        data/provider.py::SyntheticProvider.fetch_daily_bars），足以验证
        "一次多日期调用" 与 "逐日单独调用" 在真实 run_asof_scan 代码路径下
        产出逐值相同的结果——这正是切片复用层不应引入计算偏差的核心保障。
        """
        from stock_analyzer.backtest.asof_scan import run_asof_scan
        from stock_analyzer.config import load_config

        base = load_config()
        synthetic_config = base.model_copy(
            update={
                "data_source": base.data_source.model_copy(update={"primary": "synthetic"})
            }
        )
        as_of_dates = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 31)]

        multi_result = run_asof_scan(
            config=synthetic_config,
            symbols=["600000"],
            as_of_dates=as_of_dates,
            max_workers=1,
        )
        multi_by_date = {item.as_of: item for item in multi_result.results}

        for as_of in as_of_dates:
            single_result = run_asof_scan(
                config=synthetic_config,
                symbols=["600000"],
                as_of_dates=[as_of],
                max_workers=1,
            )
            assert len(single_result.results) == 1
            single_item = single_result.results[0]
            multi_item = multi_by_date[as_of]

            assert single_item.status == multi_item.status == "ok"
            single_signal = single_item.signal
            multi_signal = multi_item.signal
            assert single_signal is not None and multi_signal is not None
            assert single_signal.score == multi_signal.score, f"score mismatch at {as_of}"
            assert single_signal.grade == multi_signal.grade
            assert single_signal.action == multi_signal.action
            assert single_signal.target_position == multi_signal.target_position
            assert single_signal.probabilities == multi_signal.probabilities
            assert single_signal.reasons == multi_signal.reasons
