"""历史日期回溯选股（as-of scan），PLAN Task 3。

docs/plan_asof_backtest_holding_curve.md 第五节 Task 3 的落地：给定一个或多个
历史日期（``as_of``），用**当前**代码与模型重新计算「那天会选出哪些股票」，同时
提供两条硬性正确性保障：

1. 防泄露断言 —— ``pipeline.AnalyzerPipeline`` 在 ``as_of`` 非 None 时会对每次
   取数结果做 ``bars.index.max() <= as_of`` 校验（见 ``pipeline._assert_no_future_leak``），
   任何未来数据泄露立即抛 ``FutureDataLeakError``，不会静默产出乐观结果。
2. 两个已知泄露点被显式禁用 —— 本模块构造 ``AnalyzerPipeline`` 时永远使用
   ``build_runtime_provider``（离线 provider 链），从不构造
   ``build_realtime_runtime_provider``/``HybridRuntimeProvider``；候选标的来源
   使用显式传入的 symbols 或运行时 watchlist，从不使用无 ``end_date`` 参数的
   ``fetch_universe_quality_metrics`` 做全市场粗筛。

并行策略（PLAN 第四节，不可按日期并行）：
    按标的维度并行（<= 8 worker），每只票对所有目标 as_of 日期共用同一次
    ``fetch_daily_bars``（取最长回看窗口一次性拉满history），在内存中按各个
    ``as_of`` 切片复用，避免同一只股票的 zip 数据被反复解压。这与「按日期开
    并发各自加载数据」（内存会被并发放大，NAS 宿主只有 10.7G 可用）完全不同。

intraday 降级（不得污染生产 fail-closed 语义）：
    本模块构造的 ``AnalyzerPipeline`` 使用一份**局部 copy** 的
    ``DataSourceConfig``（``intraday_runtime_mode`` 覆写为 ``duckdb_optional``），
    只作用于这一个独立 provider 实例，绝不修改传入的全局 ``config`` 对象，也不
    触碰 ``RuntimeService`` 现有的 ``self._pipeline``/``self._realtime_pipeline``。
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import cast

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.data.provider import DataSourceError, FutureDataLeakError, MarketDataProvider
from stock_analyzer.data.provider_factory import build_runtime_provider
from stock_analyzer.pipeline import AnalyzerPipeline
from stock_analyzer.types import PipelineSignal

# 标的维度并行度上限（PLAN 第四节：瓶颈是内存不是 CPU，NAS 宿主 8 核但仅
# 10.7G 可用内存，禁止无限扩大并发）。
MAX_SYMBOL_WORKERS = 8


@dataclass(slots=True)
class AsofSymbolResult:
    """单只标的在单个 as_of 日期上的 as-of 扫描结果。"""

    symbol: str
    as_of: date
    status: str  # "ok" | "error"
    signal: PipelineSignal | None = None
    error: str = ""


@dataclass(slots=True)
class AsofScanReport:
    """一次 as-of 扫描（可覆盖多个日期）的完整结果。"""

    as_of_dates: list[date]
    symbols_requested: list[str]
    results: list[AsofSymbolResult] = field(default_factory=list)
    caveats: dict[str, object] = field(default_factory=dict)

    def candidates_for(self, as_of: date) -> list[PipelineSignal]:
        """返回某个 as_of 日期上 action == "buy" 的候选信号（按 score 降序）。"""
        matched = [
            item.signal
            for item in self.results
            if item.as_of == as_of and item.status == "ok" and item.signal is not None
        ]
        buys = [signal for signal in matched if signal is not None and signal.action == "buy"]
        return sorted(buys, key=lambda signal: signal.score, reverse=True)

    def errors(self) -> list[AsofSymbolResult]:
        return [item for item in self.results if item.status == "error"]


def _build_asof_provider(config: StockAnalyzerConfig) -> MarketDataProvider:
    """构造离线 provider 链，永不包含 HybridRuntimeProvider（已知泄露点之一）。

    ``build_runtime_provider``（而非 ``build_realtime_runtime_provider``）从不
    构造带盘中 live overlay 的 ``HybridRuntimeProvider`` —— 这是
    ``AnalyzerPipeline.__init__`` 在未显式传入 provider 时使用的同一条构造路径，
    此处显式调用只是为了让"永不使用 realtime provider"这个约束在本模块内
    也是显式可读的，而不依赖 AnalyzerPipeline 默认行为不被后续改动破坏。
    """
    return cast(MarketDataProvider, build_runtime_provider(config.data_source, synthetic_seed=2026))


def _asof_backtest_config(config: StockAnalyzerConfig) -> StockAnalyzerConfig:
    """返回一份局部 copy 的配置：intraday 独立降级为 duckdb_optional。

    只在这份 copy 上生效，不修改传入的 config 对象（该对象通常是调用方持有的
    全局单例）。``duckdb_optional`` 语义等价于 as_of 模式下
    ``pipeline._prepare_symbol_inputs`` 的降级路径需要的效果 —— provider 初始化
    阶段不再因 manifest 缺失/duckdb_required 而直接抛错，取数缺失时返回空表，
    交由 FeatureEngineer 产出 NaN 标准列。
    """
    original_ds = config.data_source
    if original_ds.intraday_runtime_mode == "duckdb_optional":
        return config
    patched_ds = original_ds.model_copy(update={"intraday_runtime_mode": "duckdb_optional"})
    return config.model_copy(update={"data_source": patched_ds})


def _resolve_top_n(symbols: Sequence[str], top_n: int | None) -> list[str]:
    normalized = list(
        dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip())
    )
    if top_n is None or top_n <= 0:
        return normalized
    return normalized[: max(1, int(top_n))]


def _scan_one_symbol(
    *,
    config: StockAnalyzerConfig,
    symbol: str,
    strategy: str,
    as_of_dates: list[date],
    current_equity: float,
) -> list[AsofSymbolResult]:
    """单只标的：构造独立 pipeline，对所有目标 as_of 日期做切片复用。

    每个 worker 线程各自持有一个 AnalyzerPipeline 实例（provider 内部有内存态
    缓存，不同线程互不共享 provider 实例更安全），但同一实例内对同一只票的
    多个 as_of 日期依次调用 run_once —— provider 链（vendor_zip_overlay 的
    baseline zip 解析、CachedProvider 缓存）天然按 (symbol, lookback_days,
    end_date) 做 key，同一只票的历史 zip 只解压一次，不同 end_date 只是在内存
    frame 上做切片，这就是 PLAN 第四节要求的"取数复用层"。
    """
    provider = _build_asof_provider(config)
    patched_config = _asof_backtest_config(config)
    # sample_store/feature_schema_registry/label_policy_registry 全部留空
    # （默认 None）：AnalyzerPipeline._persist_learning_snapshot 在这些依赖任一
    # 为 None 时直接跳过写入，天然保证回测不会污染生产学习样本库/DuckDB。
    pipeline = AnalyzerPipeline(config=patched_config, provider=provider)
    results: list[AsofSymbolResult] = []
    for as_of in as_of_dates:
        try:
            report = pipeline.run_once(
                symbols=[symbol],
                strategy=strategy,
                current_equity=current_equity,
                as_of=as_of,
            )
        except FutureDataLeakError as exc:
            # 防泄露断言拦截：必须原样向上传递为一条错误结果，不能被吞掉，
            # 否则等价于让泄露静默通过。
            results.append(
                AsofSymbolResult(symbol=symbol, as_of=as_of, status="error", error=str(exc))
            )
            continue
        except DataSourceError as exc:
            results.append(
                AsofSymbolResult(symbol=symbol, as_of=as_of, status="error", error=str(exc))
            )
            continue
        signal = report.signals[0] if report.signals else None
        results.append(AsofSymbolResult(symbol=symbol, as_of=as_of, status="ok", signal=signal))
    return results


def run_asof_scan(
    *,
    config: StockAnalyzerConfig,
    symbols: Sequence[str],
    as_of_dates: Sequence[date],
    strategy: str = "trend",
    current_equity: float = 1.0,
    top_n: int | None = None,
    max_workers: int = MAX_SYMBOL_WORKERS,
    model_trained_at: str = "",
) -> AsofScanReport:
    """跑一次（或多个日期的）as-of 回溯选股扫描。

    Args:
        config: 应用配置（只读，不会被本函数修改；内部会对 data_source 做局部
            copy 用于 intraday 降级）。
        symbols: 候选标的清单（调用方负责提供，例如当前 watchlist 或用户指定
            的标的池）。本函数不做全市场粗筛，因此不依赖任何缺 end_date 参数
            的批量接口。
        as_of_dates: 一个或多个历史日期。
        top_n: 候选标的清单裁剪上限（PLAN 第四节：Top N 限流，避免全市场
            5500 只逐票深度特征工程）。None 或 <=0 表示不裁剪。
        max_workers: 标的维度并行 worker 数，硬上限 MAX_SYMBOL_WORKERS(8)。
        model_trained_at: 当前生效模型的训练时间（ISO 日期字符串），用于
            lookahead-bias caveat 展示。该值来自运行时状态
            （``RuntimeService`` 的 ``_training_bootstrap_state``），本模块
            不持有 service 引用，由调用方（api/backtest.py）传入。

    Returns:
        AsofScanReport：包含每个 (symbol, as_of) 组合的结果，以及 caveats 字段
        （模型训练时间/intraday 降级/新闻中性化标注，供 API/前端展示口径）。
    """
    normalized_symbols = _resolve_top_n(symbols, top_n)
    normalized_dates = sorted({d for d in as_of_dates})
    worker_count = max(
        1, min(int(max_workers), MAX_SYMBOL_WORKERS, max(1, len(normalized_symbols)))
    )

    all_results: list[AsofSymbolResult] = []
    if normalized_symbols and normalized_dates:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    _scan_one_symbol,
                    config=config,
                    symbol=symbol,
                    strategy=strategy,
                    as_of_dates=normalized_dates,
                    current_equity=current_equity,
                ): symbol
                for symbol in normalized_symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    all_results.extend(future.result())
                except Exception as exc:  # noqa: BLE001 - 单只票的意外异常不能打断整批
                    for as_of in normalized_dates:
                        all_results.append(
                            AsofSymbolResult(
                                symbol=symbol,
                                as_of=as_of,
                                status="error",
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        )

    model_trained_at_normalized = str(model_trained_at or "").strip()
    caveats: dict[str, object] = {
        "lookahead_bias": True,
        "model_trained_at": model_trained_at_normalized,
        "news_neutralized": True,
        "intraday_degraded": True,
        "worker_count": worker_count,
        "symbols_scanned": len(normalized_symbols),
        "dates_scanned": [d.isoformat() for d in normalized_dates],
    }
    return AsofScanReport(
        as_of_dates=normalized_dates,
        symbols_requested=normalized_symbols,
        results=all_results,
        caveats=caveats,
    )
