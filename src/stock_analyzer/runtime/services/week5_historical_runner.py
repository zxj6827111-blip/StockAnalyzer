"""Week5 历史回测 runner（week5_daily 算法的 historical context 装配层）。

职责（PLAN 历史回测复用 Week5 每日主选股链路）：

1. 构造离线 provider 链（``build_runtime_provider``，永不包含实时 overlay）
   并用 :class:`AsOfMarketDataProvider` 把所有取数锚定到 ``as_of``；
2. 构造任务独立配置副本：feature snapshot root / selection snapshot 指向
   任务目录，intraday 降级为 ``duckdb_optional``，transform worker 上限 4；
3. 构造 ``AnalyzerPipeline`` 并提供 ``run_pipeline_fn`` 钩子
   （``run_once(as_of=...)``：日线 end_date 截断 + 防泄露断言 + 新闻中性 +
   分钟降级），供共享引擎的 monster/trend 轨复用；
4. 装配中性账户状态（空仓、无暂停、no_buy_streak=0）与模型/代码/配置身份
   标注，然后调用与生产 ``run_week5_scan`` 完全相同的
   :class:`Week5SelectionEngine`。

隔离保证：历史任务绝不写生产 feature snapshot、selection snapshot、关注池、
runtime state、推荐生命周期、学习样本或通知；引擎的
``Week5RunPolicy.historical()`` 关闭全部生产副作用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.data.asof_provider import AsOfMarketDataProvider
from stock_analyzer.pipeline import AnalyzerPipeline
from stock_analyzer.runtime.services.week5_selection_engine import (
    Week5AccountState,
    Week5ModelInfo,
    Week5RunContext,
    Week5RunPolicy,
    Week5SelectionEngine,
)


def _asof_backtest_config(
    config: StockAnalyzerConfig, *, task_dir: Path
) -> StockAnalyzerConfig:
    """任务独立配置副本：snapshot root 指向任务目录 + intraday 降级。"""
    ds_updates: dict[str, Any] = {}
    if config.data_source.intraday_runtime_mode != "duckdb_optional":
        ds_updates["intraday_runtime_mode"] = "duckdb_optional"
    patched_ds = (
        config.data_source.model_copy(update=ds_updates)
        if ds_updates
        else config.data_source
    )
    week5_updates: dict[str, Any] = {
        "feature_snapshot_root": str(Path(task_dir) / "features_light"),
        "universe_quality_snapshot_path": str(Path(task_dir) / "universe_selection.json"),
        # Feature Snapshot transform worker 上限 4（PLAN 性能约束）
        "feature_snapshot_max_workers": max(
            1, min(4, int(config.week5.feature_snapshot_max_workers))
        ),
    }
    patched_week5 = config.week5.model_copy(update=week5_updates)
    return config.model_copy(update={"data_source": patched_ds, "week5": patched_week5})


def _resolve_model_info(
    *,
    service: Any,
    config: StockAnalyzerConfig,
) -> Week5ModelInfo:
    """记录本轮使用的模型 ID / 训练时间 / 代码 commit / 配置 hash。"""
    model_id = ""
    trained_at = ""
    try:
        registry = getattr(service, "_model_registry", None)
        champion = (
            registry.active_champion(suppress_read_errors=True)
            if registry is not None
            else None
        )
        if champion is not None:
            model_id = str(getattr(champion, "model_id", "") or "")
            raw_trained_at = (
                getattr(champion, "trained_at", None)
                or getattr(champion, "created_at", None)
                or ""
            )
            trained_at = str(raw_trained_at or "")
            metrics = getattr(champion, "metrics_summary", {})
            if not trained_at and isinstance(metrics, dict):
                trained_at = str(metrics.get("trained_at", "") or "")
    except Exception:
        model_id = ""
        trained_at = ""
    try:
        bootstrap_status = service.training_bootstrap_status()
        if isinstance(bootstrap_status, dict) and not trained_at:
            trained_at = str(bootstrap_status.get("last_bootstrap_at", "") or "")
    except Exception:
        pass
    code_commit = str(getattr(config.evolution, "code_commit_id", "") or "")
    try:
        config_hash = hashlib.sha256(
            json.dumps(config.model_dump(), ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        config_hash = ""
    return Week5ModelInfo(
        model_id=model_id,
        trained_at=trained_at,
        code_commit=code_commit,
        config_hash=config_hash,
    )


def _pipeline_payload(report: object, pipeline: AnalyzerPipeline) -> dict[str, object]:
    """把 ``PipelineReport`` 转成共享引擎期望的 run_pipeline payload。"""
    if is_dataclass(report) and not isinstance(report, type):
        payload = asdict(report)
    else:  # pragma: no cover - run_once 恒返回 PipelineReport
        return {}
    timestamp = getattr(report, "timestamp", None)
    if isinstance(timestamp, datetime):
        payload["timestamp"] = timestamp.isoformat()
    runtime: dict[str, object] = {}
    stage_ms = getattr(pipeline, "_last_pipeline_stage_ms", None)
    if isinstance(stage_ms, dict):
        runtime["pipeline_stage_ms"] = dict(stage_ms)
    symbol_ms = getattr(pipeline, "_last_symbol_stage_ms", None)
    if symbol_ms is not None:
        runtime["pipeline_symbol_ms"] = [
            asdict(item) if is_dataclass(item) and not isinstance(item, type) else item
            for item in symbol_ms
        ]
    parallel_transform = getattr(pipeline, "_last_parallel_transform", None)
    if isinstance(parallel_transform, dict):
        runtime["pipeline_parallel_transform"] = dict(parallel_transform)
    payload["runtime"] = runtime
    payload.setdefault("risk", {})
    return cast(dict[str, object], payload)


def run_week5_historical_day(
    *,
    service: Any,
    as_of: date,
    task_dir: Path,
    symbols: list[str] | None = None,
    base_provider: object | None = None,
    on_progress: Any = None,
    scan_profile: str = "week5_daily",
) -> dict[str, object]:
    """对单个历史日期执行完整 Week5 每日主选股链路（historical context）。

    Args:
        service: 生产 ``StockAnalyzerService``（只读使用：配置、模型注册表、
            训练状态；历史任务不经过它的任何写路径）。
        as_of: 历史交易日（收盘后决策时点 = 当日 15:00）。
        task_dir: 任务独立工件目录（feature snapshot / selection snapshot）。
        symbols: 显式股票池；None/空表示历史全市场（provider 索引）。
        base_provider: 任务级共享的离线 provider 链（跨日期复用底层缓存）；
            None 时在本次调用内构造。
        on_progress: 引擎进度回调（阶段 → universe/quality/snapshot/light/
            deep/final）。

    Returns:
        一份完整的 Week5 扫描报告 dict（含 ``historical_context`` 标注）。
    """
    from stock_analyzer.data.provider_factory import build_runtime_provider

    config = cast(StockAnalyzerConfig, service._config)  # noqa: SLF001
    hist_config = _asof_backtest_config(config, task_dir=task_dir)
    if base_provider is None:
        base_provider = build_runtime_provider(hist_config.data_source, synthetic_seed=2026)
    provider = AsOfMarketDataProvider(base_provider, as_of)
    # 历史决策时点：收盘后 15:00（与 pipeline as-of 决策时点一致）。
    decision_time = datetime.combine(as_of, datetime.min.time()).replace(hour=15)
    pipeline = AnalyzerPipeline(config=hist_config, provider=provider)

    def run_pipeline_fn(
        *,
        symbols: list[str],
        strategy: str,
        current_equity: float,
        on_symbol_progress: Any = None,
        transform_max_workers: int = 1,
    ) -> dict[str, object]:
        # run_once(as_of=...)：日线 end_date 截断 + 未来数据断言 + 新闻中性
        # + intraday 缺失降级为 NaN 列（分钟数据存在时仍使用历史分钟特征，
        # 由 AsOfMarketDataProvider 负责把分钟摘要裁剪到 as_of）。
        report = pipeline.run_once(
            symbols=symbols,
            strategy=strategy,
            current_equity=current_equity,
            on_symbol_progress=on_symbol_progress,
            capture_post_scan_enrichment=True,
            as_of=as_of,
        )
        return _pipeline_payload(report, pipeline)

    context = Week5RunContext(
        mode="historical",
        now=decision_time,
        as_of=as_of,
        config=hist_config,
        provider=provider,
        run_pipeline_fn=run_pipeline_fn,
        symbols=list(symbols) if symbols else None,
        account=Week5AccountState(
            # 中性账户假设：空仓、无暂停开仓、无今日持仓、no_buy_streak=0。
            current_equity=1.0,
            watchlist=[],
            pause_new_buy=False,
            no_buy_streak=0,
            monster_positions=[],
        ),
        model_info=_resolve_model_info(service=service, config=hist_config),
        artifact_dir=Path(task_dir),
        progress=on_progress,
        scan_profile=scan_profile,
    )
    engine = Week5SelectionEngine(
        backend=cast(Any, service._week5_service),  # noqa: SLF001 - backend 契约
        context=context,
        policy=Week5RunPolicy.historical(),
    )
    return engine.run()


def build_historical_base_provider(config: StockAnalyzerConfig, *, task_dir: Path) -> object:
    """任务级离线 provider 链（跨日期共享底层解压/缓存）。"""
    from stock_analyzer.data.provider_factory import build_runtime_provider

    hist_config = _asof_backtest_config(config, task_dir=task_dir)
    return build_runtime_provider(hist_config.data_source, synthetic_seed=2026)
