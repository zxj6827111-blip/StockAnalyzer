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
    pipeline: AnalyzerPipeline | None = None,
) -> Week5ModelInfo:
    """记录本轮**实际加载**的模型身份 + 代码 commit + 配置 hash（S01）。

    唯一真相源：``pipeline.model_identity_facts()``（加载期实算哈希 + 工件自述
    created_at / schema / label 契约）。registry 只补充"这份内容登记叫什么"，
    bootstrap 状态只单独标注，**都不参与** ``trained_at``。

    2026-09-17 之前此处会在找不到 champion 时把 bootstrap 的 ``last_bootstrap_at``
    当 ``trained_at`` 报出去（蓝图 §2.9 的"报告一个模型、实际加载另一个"），
    S01 起该字段只等于工件 created_at；取不到就是空 + ``trained_at_source=unavailable``。
    """
    from stock_analyzer.models.identity import (  # noqa: WPS433 - 与 pipeline 同层延迟导入
        build_model_identity_report,
        registry_identity,
    )

    registry = getattr(service, "_model_registry", None)
    try:
        registry_snapshot = registry_identity(registry)
    except Exception:  # noqa: BLE001 - 身份收集失败不得打断回测
        registry_snapshot = {
            "champion": None,
            "registered": [],
            "registry_error": "registry_identity_failed",
            "registry_busy": False,
        }

    facts: dict[str, object]
    if pipeline is not None:
        facts = pipeline.model_identity_facts()
    else:
        # 没有已加载 pipeline（不应发生，保留兜底）：退回磁盘事实，绝不用 bootstrap 顶替。
        from stock_analyzer.models.identity import load_artifact_facts  # noqa: WPS433

        facts = load_artifact_facts(config.training.artifact_path)
        facts.setdefault("score_source", str(config.models.inference_score_source))

    report = build_model_identity_report(
        facts,
        registry_snapshot=registry_snapshot,
        claimed_content_hash=facts.get("claimed_content_hash", ""),
    )

    bootstrap_status: object = {}
    try:
        bootstrap_status = service.training_bootstrap_status()
    except Exception:  # noqa: BLE001 - 只做标注，取不到就留空
        bootstrap_status = {}
    bootstrap_last_bootstrap_at = (
        str(bootstrap_status.get("last_bootstrap_at", "") or "")
        if isinstance(bootstrap_status, dict)
        else ""
    )

    artifact_created_at = str(report.get("artifact_created_at", "") or "")
    code_commit = str(getattr(config.evolution, "code_commit_id", "") or "")
    try:
        config_hash = hashlib.sha256(
            json.dumps(config.model_dump(), ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        config_hash = ""
    hash_verified = report.get("content_hash_verified")
    return Week5ModelInfo(
        # model_id 是 registry 的**补充**身份：只有在与实算哈希对得上（match）时才是
        # 可信的名字，其余状态（含 no_champion）保持空串，避免"名字对不上内容"。
        model_id=(
            str(report.get("registry_model_id", "") or "")
            if bool(report.get("identity_verified", False))
            else ""
        ),
        trained_at=artifact_created_at,
        trained_at_source="artifact_created_at" if artifact_created_at else "unavailable",
        code_commit=code_commit,
        config_hash=config_hash,
        artifact_path=str(report.get("artifact_uri", "") or ""),
        artifact_content_hash=str(report.get("artifact_content_hash", "") or ""),
        artifact_created_at=artifact_created_at,
        feature_schema_id=str(report.get("feature_schema_id", "") or ""),
        feature_schema_hash=str(report.get("feature_schema_hash", "") or ""),
        label_policy_id=str(report.get("label_policy_id", "") or ""),
        label_policy_hash=str(report.get("label_policy_hash", "") or ""),
        dataset_manifest_id=str(report.get("dataset_manifest_id", "") or ""),
        score_source=str(report.get("score_source", "") or ""),
        output_semantics=str(report.get("output_semantics", "") or ""),
        identity_status=str(report.get("status", "") or ""),
        identity_detail=str(report.get("detail", "") or ""),
        identity_verified=bool(report.get("identity_verified", False)),
        research_fail_closed=bool(report.get("research_fail_closed", False)),
        content_hash_verified=hash_verified if isinstance(hash_verified, bool) else None,
        registry_model_id=str(report.get("registry_model_id", "") or ""),
        registry_content_hash=str(report.get("registry_content_hash", "") or ""),
        registry_error=str(report.get("registry_error", "") or ""),
        bootstrap_last_bootstrap_at=bootstrap_last_bootstrap_at,
    )


def _resolve_run_model(
    *,
    service: Any,
    config: StockAnalyzerConfig,
    decision_time: datetime,
) -> Any:
    """按 S06 解析本次 as_of 可用的历史模型（无合法模型 → unscorable，绝不回退在服模型）。"""
    from stock_analyzer.models.historical_resolver import (
        load_registry_candidates,
        resolve_historical_model,
    )

    mode = str(getattr(config.alpha_v2, "model_resolver_mode", "pit_research") or "pit_research")
    candidates = load_registry_candidates(getattr(service, "_model_registry", None))
    return resolve_historical_model(
        as_of=decision_time,
        candidates=candidates,
        mode=mode,
    )


def _unscorable_report(
    *,
    as_of: Any,
    decision_time: datetime,
    resolution: Any,
    scan_profile: str,
) -> dict[str, object]:
    """不可评分报告：结构完整但**零结果**（不跑扫描、不产出任何候选）。

    M1 的 fail-closed 原则：找不到合法的 as_of 历史模型时，正确结果是"这天不可评分"，
    而不是用当前在服模型算出一个看起来正常的结果。
    """
    payload = resolution.to_payload()
    return {
        "status": "unscorable",
        "scan_profile": scan_profile,
        "funnel": {
            "policy": "scorable_gate",
            "universe_count": 0,
            "light_count": 0,
            "deep_count": 0,
            "final_count": 0,
            "final_selection": {"selected_count": 0, "final_signals": []},
            "allow_zero_signal": True,
        },
        "prefilter": {"applied": False, "reason": "unscorable"},
        "final_selection": {"selected_count": 0, "final_signals": []},
        "historical_context": {
            "as_of": str(as_of),
            "decision_time": decision_time.isoformat(),
            "model_resolution": payload,
            "realtime_data_allowed": False,
            "news_neutralized": True,
        },
        "model_resolution": payload,
    }


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
    # S04：历史重放必须与**生产夜扫**同口径（300/100/50 + cap5 + allow_zero），
    # 因此默认 profile 是显式的 night-equivalent（引擎据此解析
    # night_alpha_v2_v1 契约）；此前默认 "week5_daily" 会落到 legacy 目标 100/100/20。
    scan_profile: str = "historical_night_equivalent",
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
    # 历史决策时点：与 pipeline as-of 模式的 _AS_OF_DECISION_TIME(15:30)
    # 严格一致，保证同一轮回测内报告时间戳与信号决策时点同源。
    decision_time = datetime.combine(as_of, datetime.min.time()).replace(hour=15, minute=30)
    # S06 时间闸门：as_of 之后创建/激活的模型一律不得加载；找不到合法模型即 unscorable。
    model_resolution = _resolve_run_model(
        service=service, config=hist_config, decision_time=decision_time
    )
    if not model_resolution.scorable:
        return _unscorable_report(
            as_of=as_of,
            decision_time=decision_time,
            resolution=model_resolution,
            scan_profile=scan_profile,
        )
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
        model_info=_resolve_model_info(service=service, config=hist_config, pipeline=pipeline),
        artifact_dir=Path(task_dir),
        progress=on_progress,
        scan_profile=scan_profile,
    )
    engine = Week5SelectionEngine(
        backend=cast(Any, service._week5_service),  # noqa: SLF001 - backend 契约
        context=context,
        policy=Week5RunPolicy.historical(),
    )
    report = engine.run()
    report["model_resolution"] = model_resolution.to_payload()
    return report


def build_historical_base_provider(config: StockAnalyzerConfig, *, task_dir: Path) -> object:
    """任务级离线 provider 链（跨日期共享底层解压/缓存）。"""
    from stock_analyzer.data.provider_factory import build_runtime_provider

    hist_config = _asof_backtest_config(config, task_dir=task_dir)
    return build_runtime_provider(hist_config.data_source, synthetic_seed=2026)
