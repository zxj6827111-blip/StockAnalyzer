"""共享 Week5 选股引擎（live / historical 双上下文，PLAN 历史回测复用）。

从 ``RuntimeWeek5Service._run_week5_scan_impl`` 抽出的漏斗编排层：
质量筛选 → Feature Snapshot → Light → Deep → monster/trend → 风控与
final selection。所有对外副作用（生产报告落盘、关注池同步、通知、审计、
分钟同步）都收敛到 :class:`Week5RunPolicy` 的开关与 :class:`Week5EngineBackend`
的钩子之后，保证：

- 生产 ``run_week5_scan`` 通过 **live context** 调用本引擎，行为与旧实现一致；
- 历史回测通过 **historical context** 调用同一个引擎（同一套阶段实现，
  避免复制一套近似算法后逐渐漂移）。

引擎本身不持有服务状态：阶段实现走 ``backend``（live/historical 共用同一批
纯函数式 stage helper），账户状态/股票池/模型信息/工件目录由
:class:`Week5RunContext` 显式注入。historical 模式的差异全部由 policy 声明：
禁止实时 overlay、禁用分钟同步与新鲜度门（改标 ``intraday_degraded``）、
新闻固定中性、市场广度从 as-of 日线现算、账户状态中性、生产写路径关闭。

注意本模块与 ``week5_service`` 的导入方向：引擎在模块顶层复用 week5_service
的纯函数 helper，因此 week5_service 必须在函数体内延迟导入本模块（当前实现
如此），避免循环导入。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol, cast

import pandas as pd

from stock_analyzer.feature.snapshot import (
    SNAPSHOT_FILENAME,
    FeatureSnapshotManifest,
    build_feature_snapshot,
    snapshot_is_current,
)
from stock_analyzer.runtime.services.week5_service import (
    _as_float,
    _as_int,
    _bars_from_post_scan_enrichment,
    _board_decision_dict,
    _build_fresh_deep_frame,
    _build_intraday_freshness_blocked_report,
    _dedupe_preserve_order,
    _latest_bar_dict,
    _normalize_a_share_symbol,
    _overextension_decision_dict,
    _prepare_intraday_sync_symbols,
    _record_intraday_sync_health_audits,
    _resolve_calendar_provider,
    _resolve_pinned_symbols_after_freshness,
    _resolve_positive_int,
    _resolve_required_intraday_date,
    _string_list,
    _sync_intraday_symbols,
)

if TYPE_CHECKING:
    from stock_analyzer.config import StockAnalyzerConfig


# 引擎进度回调：与 Week5ScanProgress.update 的关键字约定一致。
ProgressCallback = Callable[..., None]


@dataclass(slots=True)
class Week5AccountState:
    """本轮扫描使用的账户状态（live=真实状态；historical=中性假设）。"""

    current_equity: float = 1.0
    watchlist: list[str] = field(default_factory=list)
    pause_new_buy: bool = False
    # 连续无 actionable 信号次数（live 来自 run summaries；historical 固定 0）。
    no_buy_streak: int = 0
    # monster 策略当前持仓的目标仓位列表（historical 为空 = 空仓）。
    monster_positions: list[float] = field(default_factory=list)


@dataclass(slots=True)
class Week5ModelInfo:
    """本轮使用的模型/代码/配置身份（历史回测的可复现性标注）。"""

    model_id: str = ""
    trained_at: str = ""
    code_commit: str = ""
    config_hash: str = ""

    def to_payload(self) -> dict[str, str]:
        return {
            "model_id": self.model_id,
            "trained_at": self.trained_at,
            "code_commit": self.code_commit,
            "config_hash": self.config_hash,
        }


@dataclass(slots=True)
class Week5RunContext:
    """一次扫描的数据与身份上下文。

    - ``mode == "live"``：``config/provider/run_pipeline_fn`` 为空，一切经由
      backend（生产服务现有路径）；
    - ``mode == "historical"``：``as_of`` 必填，``provider`` 是
      :class:`AsOfMarketDataProvider`，``run_pipeline_fn`` 由历史 runner 提供
      （内部走 ``AnalyzerPipeline.run_once(as_of=...)``），``config`` 是任务
      独立配置副本（feature snapshot root / selection snapshot path 指向
      ``artifact_dir``），``account`` 为中性账户状态。
    """

    mode: str  # "live" | "historical"
    now: datetime
    as_of: date | None = None
    config: Any = None
    # provider/_run_pipeline_fn 保持 Any：底层 provider 链与 pipeline 适配层
    # 的能力面是鸭子类型（status/list_symbols/fetch_* 等），按 object 逐个
    # cast 的噪音远大于收益。
    provider: Any = None
    # 历史 pipeline 执行钩子：run_pipeline_fn(symbols=..., strategy=...,
    # current_equity=..., on_symbol_progress=..., transform_max_workers=...)
    run_pipeline_fn: Any = None
    symbols: list[str] | None = None
    pinned_symbols: list[str] = field(default_factory=list)
    account: Week5AccountState = field(default_factory=Week5AccountState)
    model_info: Week5ModelInfo = field(default_factory=Week5ModelInfo)
    # 历史任务的独立工件目录（feature snapshot / selection snapshot 根）。
    artifact_dir: Path | None = None
    progress: ProgressCallback | None = None
    sync_reason: str = ""
    scan_profile: str = ""
    # live 透传的调度覆盖参数（offhours refresh / 恢复路径使用）。
    force_universe_scan: bool = False
    recovery_mode: bool = False
    sync_watchlist: bool | None = None
    sync_top_k_override: int | None = None
    prefilter_enabled_override: bool | None = None
    prefilter_top_k_override: int | None = None
    universe_max_symbols_override: int | None = None
    deep_candidate_target_override: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"live", "historical"}:
            raise ValueError(f"unknown week5 run mode: {self.mode}")
        if self.mode == "historical":
            if self.as_of is None:
                raise ValueError("historical week5 run requires as_of")
            if self.provider is None or self.run_pipeline_fn is None:
                raise ValueError(
                    "historical week5 run requires provider and run_pipeline_fn"
                )
            if self.config is None:
                raise ValueError("historical week5 run requires task config copy")


@dataclass(slots=True)
class Week5RunPolicy:
    """本轮扫描允许的行为面（live 默认全开；historical 全关）。"""

    mode: str = "live"
    # 允许实时数据（realtime overlay / 当前市场快照）。historical 固定 False。
    allow_realtime_data: bool = True
    # 分钟数据同步与新鲜度门（live snapshot_funnel 的 fail-closed 组件）。
    intraday_sync_enabled: bool = True
    # 通知/关注池同步/生产状态写入/审计。historical 全 False。
    notify: bool = True
    sync_watchlist: bool = True
    persist_production_state: bool = True
    # 市场广度：live 读预写 market_breadth.json；historical 从 as-of 日线现算。
    recompute_market_breadth: bool = False
    # 新闻门：live 按 config.evolution.news_risk_mode；historical 固定中性
    # （final selector 以 news_mode_override="off" 执行）。
    news_neutralized: bool = False

    @classmethod
    def live(cls) -> Week5RunPolicy:
        return cls(mode="live")

    @classmethod
    def historical(cls) -> Week5RunPolicy:
        return cls(
            mode="historical",
            allow_realtime_data=False,
            intraday_sync_enabled=False,
            notify=False,
            sync_watchlist=False,
            persist_production_state=False,
            recompute_market_breadth=True,
            news_neutralized=True,
        )


class Week5EngineBackend(Protocol):
    """引擎依赖的阶段实现钩子（由 RuntimeWeek5Service 提供）。

    全部为读路径或显式副作用钩子：引擎不直接触碰服务内部状态。
    """

    @property
    def config(self) -> StockAnalyzerConfig: ...

    def build_data_gate(
        self,
        *,
        snapshot_manifest: object,
        snapshot_current: bool,
        latest_trade_date: str,
        now: object,
    ) -> dict[str, Any]: ...

    def prefer_local_symbol_universe(self) -> bool: ...

    def resolve_symbol_universe(self, **kwargs: Any) -> dict[str, Any]: ...

    def universe_seed_trade_date(self) -> str: ...

    def select_universe_quality_candidates(self, **kwargs: Any) -> dict[str, Any]: ...

    def ensure_feature_snapshot(self, *, symbols: list[str], scope: str) -> dict[str, Any]: ...

    def light_stage_from_snapshot(
        self, *, frame: pd.DataFrame, target: int, allowed_exchanges: set[str]
    ) -> dict[str, Any]: ...

    def deep_stage_from_snapshot(
        self, *, frame: pd.DataFrame, target: int, light_report: dict[str, object]
    ) -> dict[str, Any]: ...

    def prefilter_universe_symbols(
        self, *, symbols: list[str], top_k_override: int | None = None
    ) -> dict[str, Any]: ...

    def run_pipeline(self, **kwargs: Any) -> dict[str, Any]: ...

    def select_live_runtime_provider(self) -> Any: ...

    def score_signal_pool_candidate(
        self,
        *,
        signal: Any,
        prefilter_detail: Any,
    ) -> dict[str, Any]: ...

    def apply_execution_aware_rerank(
        self, *, candidates: list[dict[str, object]]
    ) -> dict[str, Any]: ...

    def final_signal_selector(
        self,
        *,
        signals: list[dict[str, object]],
        data_gate_status: str,
        min_threshold_lift: float = 0.0,
        news_mode_override: str | None = None,
    ) -> dict[str, Any]: ...

    def build_first_board_candidate(
        self, *, symbol: str, bars: Any, signal: dict[str, object]
    ) -> dict[str, Any] | None: ...

    def detect_symbol_anomaly(
        self, *, symbol: str, bars: pd.DataFrame
    ) -> dict[str, object] | None: ...

    def estimate_sentiment(self, *, monster_report: dict[str, object]) -> tuple[float, bool]: ...

    def market_breadth_gate(self, *, now: datetime) -> tuple[dict[str, object], float]: ...

    def build_gate_blocked_report(
        self,
        *,
        now: datetime,
        reasons: list[str],
        data_snapshot_id: str,
        snapshot_current: bool,
        scan_profile: str,
        watchlist_size: int,
    ) -> dict[str, Any]: ...

    def build_dual_track_output(self, **kwargs: Any) -> dict[str, Any]: ...

    def store_report(self, report: dict[str, object]) -> None: ...

    def record_audit(
        self,
        *,
        event_type: str,
        level: str = "info",
        trace_id: str = "",
        payload: dict[str, object] | None = None,
    ) -> None: ...

    def sync_watchlist_from_report(
        self,
        *,
        report: dict[str, object],
        reason: str,
        top_k_override: int | None = None,
    ) -> dict[str, Any]: ...

    def watchlist_sync_diagnostics(
        self, *, report: dict[str, object], top_k_override: int | None = None
    ) -> dict[str, Any]: ...

    def build_scan_notification_content(self, **kwargs: Any) -> str: ...

    def notify_scan(
        self,
        *,
        symbol_list: list[str],
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
        watchlist_sync: dict[str, object],
        runtime_mode: str,
        has_warning: bool,
        trace_id: str,
        now: datetime,
    ) -> None: ...

    def notify_actionable_signals(
        self, report: dict[str, object], *, trace_id: str, title_prefix: str
    ) -> None: ...

    # live 分钟同步路径依赖的服务内部访问（historical 不会调用）。
    def is_intraday_scheduler_scan(self, *, now: datetime, sync_reason: str) -> bool: ...

    def latest_preserved_watchlist_symbols(
        self, *, top_k_override: Any = None
    ) -> list[str]: ...

    def market_warehouse(self) -> Any: ...

    def provider(self) -> Any: ...

    def provider_graph(self) -> list[Any]: ...

    def runtime_source_mode(self) -> str: ...


class Week5SelectionEngine:
    """共享漏斗编排器：一次 run() 产出一份完整 Week5 扫描报告。"""

    def __init__(
        self,
        *,
        backend: Week5EngineBackend,
        context: Week5RunContext,
        policy: Week5RunPolicy,
    ) -> None:
        self._backend = backend
        self._ctx = context
        self._policy = policy
        self._config = context.config if context.config is not None else backend.config
        self._historical = policy.mode == "historical"

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self) -> dict[str, object]:
        ctx = self._ctx
        config = self._config
        backend = self._backend
        now = ctx.now
        quality_selection_ms = 0
        light_stage_ms = 0
        deep_stage_ms = 0
        snapshot_manifest = None
        snapshot_frame = None
        snapshot_current = False
        snapshot_root = self._snapshot_root()
        manifest, frame = load_feature_snapshot_from_root(snapshot_root)
        if manifest is not None and frame is not None:
            snapshot_manifest = manifest
            snapshot_frame = frame
            snapshot_current = bool(snapshot_is_current(manifest, config, now))
        data_gate: dict[str, Any] = self._build_data_gate(
            snapshot_manifest=snapshot_manifest,
            snapshot_current=snapshot_current,
            latest_trade_date=str(snapshot_manifest.trade_date) if snapshot_manifest else "",
        )
        gate_status = str(data_gate.get("status", "ok"))
        deep_candidate_target = max(
            1,
            _resolve_positive_int(
                ctx.deep_candidate_target_override,
                fallback=_as_int(config.week5.deep_candidate_target, default=20),
            ),
        )
        intraday_scheduler_mode = (
            backend.is_intraday_scheduler_scan(now=now, sync_reason=ctx.sync_reason)
            if not self._historical
            else False
        )
        prefilter_enabled = (
            bool(ctx.prefilter_enabled_override)
            if ctx.prefilter_enabled_override is not None
            else bool(config.week5.universe_prefilter_enabled)
        )
        configured_prefilter_top_k = max(
            1,
            _resolve_positive_int(
                ctx.prefilter_top_k_override,
                fallback=_as_int(config.week5.universe_prefilter_top_k, default=500),
            ),
        )
        symbol_source = "watchlist"
        prefilter_report = _empty_prefilter_report(
            config,
            enabled=prefilter_enabled,
            top_k=configured_prefilter_top_k,
        )
        historical_universe_report: dict[str, object] | None = None
        prefilter_details_by_symbol: dict[str, dict[str, object]] = {}
        prefer_local_universe = (
            backend.prefer_local_symbol_universe() if not self._historical else True
        )
        should_scan_universe = bool(ctx.force_universe_scan)
        universe_board_quota: dict[str, object] = {}
        quality_selection_report: dict[str, object] | None = None
        universe_errors_list: list[str] | None = None

        if ctx.progress is not None:
            ctx.progress(phase="quality")

        explicit_symbols = [str(item).strip() for item in (ctx.symbols or []) if str(item).strip()]
        if ctx.symbols is not None and not ctx.force_universe_scan:
            raw_symbols = explicit_symbols
            symbol_source = "manual_input"
            prefilter_report["reason"] = "manual_symbols"
            if self._historical:
                prefilter_report["explicit_pool"] = True
                prefilter_report["explicit_pool_note"] = "manual_symbols_not_full_market"
        elif self._historical:
            # 历史全市场：从完整 provider 索引生成 as-of 有效股票池
            # （未来上市无历史数据自动淘汰；退市按 as-of 时的数据可得性判断，
            # 不用当前退市名单整体排除），再走质量选择。
            resolution = self._resolve_historical_universe()
            raw_symbols = resolution["symbols"]
            symbol_source = str(resolution["source"])
            should_scan_universe = True
            prefilter_report["reason"] = "universe_scan"
            prefilter_report["universe_source"] = symbol_source
            historical_universe_report = dict(resolution["report"])
            prefilter_report["historical_universe"] = historical_universe_report
            quality_report_value = resolution.get("quality_selection_report")
            if isinstance(quality_report_value, dict):
                quality_selection_report = quality_report_value
                prefilter_report["universe_quality_selection"] = quality_report_value
                prefilter_report["universe_quality_selector_mode"] = str(
                    quality_report_value.get("selector_mode", "")
                ).strip()
            errors_value = resolution.get("universe_errors")
            if isinstance(errors_value, list) and errors_value:
                universe_errors_list = [str(item) for item in errors_value][:20]
                prefilter_report["universe_errors"] = universe_errors_list
            quota_value = resolution.get("board_quota")
            if isinstance(quota_value, dict) and quota_value:
                universe_board_quota = quota_value
                prefilter_report["universe_board_quota"] = quota_value
        else:
            raw_symbols = list(ctx.account.watchlist)
            if not raw_symbols and intraday_scheduler_mode and not ctx.force_universe_scan:
                raw_symbols = backend.latest_preserved_watchlist_symbols(
                    top_k_override=ctx.sync_top_k_override,
                )
                if raw_symbols:
                    symbol_source = "intraday_preserved_watchlist"
                    prefilter_report["reason"] = "intraday_preserve_existing"
            if ctx.force_universe_scan or (not raw_symbols and not intraday_scheduler_mode):
                quality_selector_enabled = bool(
                    config.week5.universe_quality_selector_enabled
                )
                if quality_selector_enabled:
                    universe = backend.resolve_symbol_universe(
                        max_symbols=0,
                        allow_seed_fallback=True,
                        allow_online_sources=not prefer_local_universe,
                    )
                    full_universe_symbols = _string_list(universe.get("symbols", []))
                    quality_target = _resolve_positive_int(
                        ctx.universe_max_symbols_override,
                        fallback=_as_int(
                            config.week5.universe_quality_target_size,
                            default=300,
                        ),
                    )
                    quality_trade_date = backend.universe_seed_trade_date()
                    quality_ruleset_id = str(
                        config.evolution.universe_spec.universe_ruleset_id
                    )
                    quality_board_scope = list(config.evolution.universe_spec.board_scope)
                    selection_started = perf_counter()
                    selection = backend.select_universe_quality_candidates(
                        symbols=full_universe_symbols,
                        target_size=quality_target,
                        trade_date=quality_trade_date,
                        reference_date=now.date().isoformat(),
                        ruleset_id=quality_ruleset_id,
                        board_scope=quality_board_scope,
                    )
                    quality_selection_ms = max(1, int((perf_counter() - selection_started) * 1000))
                    selection_report = selection.get("report", {})
                    if not isinstance(selection_report, dict):
                        selection_report = {}
                    raw_symbols = _string_list(selection.get("selected", []))
                    selector_mode = str(selection_report.get("selector_mode", "")).strip()
                    symbol_source = str(universe.get("source", "universe")) + ":quality_selector"
                    should_scan_universe = True
                    prefilter_report["reason"] = "universe_scan"
                    prefilter_report["universe_source"] = symbol_source
                    prefilter_report["universe_quality_selection"] = selection_report
                    prefilter_report["universe_quality_selector_mode"] = selector_mode
                    quality_selection_report = selection_report
                    board_quotas = selection_report.get("board_quotas")
                    universe_board_quota = {
                        "truncation_mode": "quality_ranked_board_floor",
                        "cap": quality_target,
                        "effective_cap": max(
                            0,
                            _as_int(
                                selection_report.get("selected_count"),
                                default=len(raw_symbols),
                            ),
                        ),
                        "board_scope": quality_board_scope,
                        "boards": board_quotas if isinstance(board_quotas, dict) else {},
                        "seed_trade_date": quality_trade_date,
                        "ruleset_id": quality_ruleset_id,
                        "selector_mode": selector_mode,
                    }
                    prefilter_report["universe_board_quota"] = universe_board_quota
                    raw_errors = universe.get("errors", [])
                    if isinstance(raw_errors, list):
                        universe_errors_list = [
                            str(item).strip() for item in raw_errors if str(item).strip()
                        ][:20]
                        prefilter_report["universe_errors"] = universe_errors_list
                else:
                    universe_max_symbols = (
                        max(0, _as_int(ctx.universe_max_symbols_override, default=0))
                        if ctx.universe_max_symbols_override is not None
                        else (0 if prefer_local_universe else 1200)
                    )
                    universe = backend.resolve_symbol_universe(
                        max_symbols=universe_max_symbols,
                        allow_seed_fallback=True,
                        allow_online_sources=not prefer_local_universe,
                    )
                    raw_symbols = _string_list(universe.get("symbols", []))
                    symbol_source = str(universe.get("source", "universe"))
                    should_scan_universe = True
                    prefilter_report["reason"] = "universe_scan"
                    prefilter_report["universe_source"] = symbol_source
                    raw_quota = universe.get("board_quota")
                    if isinstance(raw_quota, dict):
                        universe_board_quota = raw_quota
                        prefilter_report["universe_board_quota"] = raw_quota
                    raw_errors = universe.get("errors", [])
                    if isinstance(raw_errors, list):
                        universe_errors_list = [
                            str(item).strip() for item in raw_errors if str(item).strip()
                        ][:20]
                        prefilter_report["universe_errors"] = universe_errors_list
            else:
                if prefilter_report.get("reason") == "not_requested":
                    prefilter_report["reason"] = "existing_watchlist"

        symbol_list = [str(item).strip() for item in raw_symbols if str(item).strip()]
        feature_snapshot_report: dict[str, object] | None = None
        snapshot_ensure_ms = 0
        snapshot_mode = bool(
            snapshot_manifest is not None and snapshot_frame is not None and snapshot_current
        )
        if ctx.progress is not None:
            ctx.progress(phase="snapshot", total=len(symbol_list))
        if should_scan_universe and symbol_list and bool(config.week5.feature_snapshot_enabled):
            snapshot_ensure_started = perf_counter()
            if self._historical:
                feature_snapshot_report = self._build_historical_snapshot(
                    symbols=symbol_list,
                )
            else:
                feature_snapshot_report = backend.ensure_feature_snapshot(
                    symbols=symbol_list,
                    scope="universe_quality",
                )
            snapshot_ensure_ms = max(1, int((perf_counter() - snapshot_ensure_started) * 1000))
            if not bool(feature_snapshot_report.get("ok", False)):
                snapshot_manifest = None
                snapshot_frame = None
                snapshot_current = False
            else:
                manifest, frame = load_feature_snapshot_from_root(snapshot_root)
                if manifest is not None and frame is not None:
                    snapshot_manifest = manifest
                    snapshot_frame = frame
                    snapshot_current = bool(snapshot_is_current(manifest, config, now))
                else:
                    snapshot_manifest = None
                    snapshot_frame = None
                    snapshot_current = False
            snapshot_mode = bool(
                snapshot_manifest is not None and snapshot_frame is not None and snapshot_current
            )
            data_gate = self._build_data_gate(
                snapshot_manifest=snapshot_manifest,
                snapshot_current=snapshot_current,
                latest_trade_date=str(snapshot_manifest.trade_date) if snapshot_manifest else "",
            )
            gate_status = str(data_gate.get("status", "ok"))

        scan_profile_name = ctx.scan_profile.strip() or "default"
        if scan_profile_name in ("offhours_friday_full_deep", "offhours_weekend_full_deep"):
            funnel_policy = "intentional_full_deep"
        elif should_scan_universe:
            funnel_policy = "snapshot_funnel"
        else:
            funnel_policy = "direct_non_universe"
        required_intraday_date: object | None = None
        cal_source: object | None = None
        if (
            self._policy.intraday_sync_enabled
            and snapshot_manifest is not None
            and str(snapshot_manifest.trade_date).strip()
        ):
            cal_source = _resolve_calendar_provider(self._backend_service())
            required_intraday_date = _resolve_required_intraday_date(
                str(snapshot_manifest.trade_date).strip(), cal_source
            )

        if ctx.progress is not None:
            ctx.progress(funnel_policy=funnel_policy)
            ctx.progress(phase="light")

        if should_scan_universe and symbol_list and prefilter_enabled:
            # 自动调度触发：scheduler_* 前缀（intraday/nightly）或 offhours_refresh
            # 全市场漏斗；手动恢复保留绕过能力；历史全市场任务与调度路径同等
            # fail-closed。（2026-09-05 修复：原 blocked 中止只挂在非 snapshot
            # 预过滤分支，snapshot_funnel 主路径被绕过——data_gate=blocked 形同
            # 虚设。提升到分支顶部后两条路径同等 fail-closed。）
            auto_scheduled_scan = bool(
                ctx.sync_reason.strip().lower().startswith("scheduler_")
                or (
                    ctx.sync_reason.strip().lower() == "offhours_refresh"
                    and funnel_policy == "snapshot_funnel"
                )
            )
            if gate_status == "blocked" and (auto_scheduled_scan or self._historical):
                gate_reasons = [str(item) for item in (data_gate.get("reasons") or [])]
                blocked_payload = backend.build_gate_blocked_report(
                    now=now,
                    reasons=gate_reasons,
                    data_snapshot_id=str(snapshot_manifest.data_snapshot_id)
                    if snapshot_manifest
                    else "",
                    snapshot_current=snapshot_current,
                    scan_profile=scan_profile_name,
                    watchlist_size=len(ctx.account.watchlist),
                )
                if feature_snapshot_report is not None:
                    blocked_payload["feature_snapshot"] = feature_snapshot_report
                blocked_payload["funnel_policy"] = funnel_policy
                if self._historical:
                    blocked_payload["historical_context"] = self._historical_context_payload()
                self._finalize_report_writes(
                    blocked_payload,
                    audit_event="week5_scan_blocked_data_gate",
                    audit_payload={"reasons": gate_reasons},
                )
                return blocked_payload
            if snapshot_mode:
                light_started = perf_counter()
                light_target = max(1, int(config.week5.light_candidate_target))
                allowed_exchanges_for_light = {
                    str(item).strip().upper()
                    for item in config.evolution.universe_spec.board_scope
                    if str(item).strip()
                }
                prefilter_report = backend.light_stage_from_snapshot(
                    frame=cast(pd.DataFrame, snapshot_frame),
                    target=light_target,
                    allowed_exchanges=allowed_exchanges_for_light,
                )
                light_stage_ms = max(1, int((perf_counter() - light_started) * 1000))
            else:
                prefilter_report = backend.prefilter_universe_symbols(
                    symbols=symbol_list,
                    top_k_override=configured_prefilter_top_k,
                )
            prefilter_report["reason"] = "universe_scan"
            prefilter_report["universe_source"] = symbol_source
            if universe_board_quota:
                prefilter_report["universe_board_quota"] = universe_board_quota
            if historical_universe_report is not None:
                prefilter_report["historical_universe"] = historical_universe_report
            if quality_selection_report is not None:
                prefilter_report["universe_quality_selection"] = quality_selection_report
                prefilter_report["universe_quality_selector_mode"] = str(
                    quality_selection_report.get("selector_mode", "")
                ).strip()
            if universe_errors_list is not None:
                prefilter_report["universe_errors"] = universe_errors_list
            raw_shortlisted = prefilter_report.get("shortlisted", [])
            if isinstance(raw_shortlisted, list):
                prefilter_details_by_symbol = {
                    normalized: item
                    for item in raw_shortlisted
                    if isinstance(item, dict)
                    for normalized in [_normalize_a_share_symbol(item.get("symbol"))]
                    if normalized
                }
            prefilter_symbols = _string_list(prefilter_report.get("symbols", []))
            if not prefilter_symbols:
                prefilter_symbols = [
                    str(item.get("symbol", "")).strip()
                    for item in raw_shortlisted
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ]
            symbol_list = prefilter_symbols
            symbol_source = f"{symbol_source}:prefilter"

        deep_report: dict[str, Any] = {}
        deep_stage_ran = False
        deep_symbols: list[str] = []
        deep_selected_count = 0
        intraday_degraded = False
        intraday_coverage_ratio = 1.0
        if ctx.progress is not None:
            ctx.progress(phase="deep")
        if snapshot_mode and funnel_policy != "intentional_full_deep":
            deep_stage_ran = True
            light_symbols = [
                str(item.get("symbol", "")).strip()
                for item in (prefilter_report.get("shortlisted", []) or [])
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            ]
            if not light_symbols:
                light_symbols = list(prefilter_report.get("symbols", []) or [])
                light_symbols = [str(s).strip() for s in light_symbols if str(s).strip()]
            deep_started = perf_counter()
            if self._policy.intraday_sync_enabled:
                (
                    deep_report,
                    blocked_live,
                    deep_stage_ms,
                ) = self._run_live_deep_stage(
                    light_symbols=light_symbols,
                    snapshot_frame=cast(pd.DataFrame, snapshot_frame),
                    snapshot_mode=snapshot_mode,
                    funnel_policy=funnel_policy,
                    scan_profile_name=scan_profile_name,
                    prefilter_report=prefilter_report,
                    deep_candidate_target=deep_candidate_target,
                    cal_source=cal_source,
                    required_intraday_date=required_intraday_date,
                    deep_started=deep_started,
                )
                if blocked_live:
                    # 两条 blocked 路径（新鲜度门 / fresh frame 构建失败）已在
                    # _run_live_deep_stage 内部完成 store_report + audit（payload
                    # 更精确），这里只补 historical 标注后短路返回，绝不重复落盘。
                    if self._historical:
                        deep_report["historical_context"] = self._historical_context_payload()
                    return deep_report
                deep_symbols = [
                    str(item.get("symbol", "")).strip()
                    for item in deep_report.get("selected", [])
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ]
                deep_selected_count = len(deep_symbols)
                if deep_symbols:
                    symbol_list = deep_symbols
                    symbol_source = f"{symbol_source}:snapshot_deep"
                elif funnel_policy == "snapshot_funnel":
                    symbol_list = []
                    symbol_source = f"{symbol_source}:snapshot_deep_empty_fail_closed"
                prefilter_report["deep_stage"] = deep_report
            else:
                # historical：跳过分钟同步与新鲜度门。deep 输入直接取 light
                # shortlist，从 as-of provider 现算 daily+intraday 特征行；
                # 分钟数据缺失时 intraday 列自然为 NaN 并显著标记降级。
                deep_symbols = light_symbols
                if deep_symbols:
                    fresh_result: dict[str, Any] = _build_fresh_deep_frame(
                        provider=ctx.provider,
                        warehouse=None,
                        vendor_overlay=None,
                        symbols=deep_symbols,
                        required_date=ctx.as_of,
                        lookback_days=max(
                            60, int(config.week5.feature_snapshot_lookback_days)
                        ),
                    )
                    fresh_frame = fresh_result.get("frame")
                    intraday_ratio = self._probe_historical_intraday_coverage(deep_symbols)
                    intraday_coverage_ratio = intraday_ratio
                    intraday_degraded = intraday_ratio < 1.0
                    if isinstance(fresh_frame, pd.DataFrame) and not fresh_frame.empty:
                        deep_report = backend.deep_stage_from_snapshot(
                            frame=fresh_frame,
                            target=deep_candidate_target,
                            light_report=prefilter_report,
                        )
                        deep_report["fresh_frame_used"] = True
                        deep_report["fresh_frame_failed"] = list(
                            fresh_result.get("failed", [])
                        )
                    else:
                        deep_report = backend.deep_stage_from_snapshot(
                            frame=cast(pd.DataFrame, snapshot_frame),
                            target=deep_candidate_target,
                            light_report=prefilter_report,
                        )
                        deep_report["fresh_frame_used"] = False
                    deep_report["historical_intraday"] = {
                        "coverage_ratio": round(intraday_ratio, 4),
                        "degraded": intraday_degraded,
                        "as_of": ctx.as_of.isoformat() if ctx.as_of else "",
                    }
                deep_stage_ms = max(1, int((perf_counter() - deep_started) * 1000))
                deep_selected_count = len(deep_symbols)
                if deep_symbols:
                    symbol_list = deep_symbols
                    symbol_source = f"{symbol_source}:snapshot_deep"
                elif funnel_policy == "snapshot_funnel":
                    symbol_list = []
                    symbol_source = f"{symbol_source}:snapshot_deep_empty_fail_closed"
                prefilter_report["deep_stage"] = deep_report
                prefilter_report["intraday_degraded"] = intraday_degraded
                prefilter_report["intraday_coverage_ratio"] = round(intraday_coverage_ratio, 4)

        deep_empty_reason = ""
        if deep_stage_ran and not deep_symbols:
            if _as_int(deep_report.get("light_shortlist_count"), default=0) <= 0:
                deep_empty_reason = "light_shortlist_empty"
            elif _as_int(deep_report.get("snapshot_match_rows"), default=0) <= 0:
                deep_empty_reason = "no_snapshot_matching_rows"
            else:
                deep_empty_reason = "deep_selected_empty"

        quality_selector_mode = (
            str(quality_selection_report.get("selector_mode", "")).strip()
            if isinstance(quality_selection_report, dict)
            else ""
        )
        degraded_fail_closed = bool(
            should_scan_universe
            and not bool(prefilter_report.get("applied", False))
            and quality_selector_mode == "degraded_fallback"
        )
        if degraded_fail_closed:
            symbol_list = []
            prefilter_report["degraded_fail_closed"] = True
            prefilter_report["degraded_fail_closed_reason"] = "quality_unavailable_without_snapshot"

        normalized_pinned_symbols = _resolve_pinned_symbols_after_freshness(
            prefilter_report=prefilter_report,
            pinned_symbols=ctx.pinned_symbols,
        )
        pinned_added_symbols: list[str] = []
        if normalized_pinned_symbols:
            existing_symbols = {
                symbol
                for symbol in (_normalize_a_share_symbol(item) for item in symbol_list)
                if symbol
            }
            pinned_added_symbols = [
                symbol for symbol in normalized_pinned_symbols if symbol not in existing_symbols
            ]
            if pinned_added_symbols:
                symbol_list.extend(pinned_added_symbols)
                symbol_source = f"{symbol_source}:pinned"
        prefilter_report["pinned_symbols"] = list(normalized_pinned_symbols)
        prefilter_report["pinned_count"] = len(pinned_added_symbols)
        prefilter_report["selected_count"] = len(symbol_list)
        original_monster_scan_count = len(symbol_list)
        monster_scan_cap = (
            max(
                1,
                _as_int(
                    config.week5.monster_scan_intraday_max_symbols,
                    default=_as_int(config.week5.live_runtime_max_symbols, default=15),
                ),
            )
            if intraday_scheduler_mode
            else max(0, _as_int(config.week5.monster_scan_max_symbols, default=120))
        )
        monster_scan_cap_applied = bool(
            monster_scan_cap > 0 and len(symbol_list) > monster_scan_cap
        )
        if degraded_fail_closed:
            ranking_mode = "degraded_fail_closed_pinned_only"
        else:
            ranking_mode = (
                "prefilter_order" if bool(prefilter_report.get("applied", False)) else "input_order"
            )
        if monster_scan_cap_applied:
            pinned_set = set(normalized_pinned_symbols)
            pinned_first = [
                symbol
                for symbol in symbol_list
                if (_normalize_a_share_symbol(symbol) or symbol) in pinned_set
            ]
            non_pinned = [
                symbol
                for symbol in symbol_list
                if (_normalize_a_share_symbol(symbol) or symbol) not in pinned_set
            ]
            if not bool(prefilter_report.get("applied", False)) and isinstance(
                quality_selection_report, dict
            ):
                selected_payload = quality_selection_report.get("selected", [])
                quality_score_by_symbol = (
                    {
                        normalized: _as_float(item.get("score"), default=0.0)
                        for item in selected_payload
                        if isinstance(item, dict)
                        for normalized in [_normalize_a_share_symbol(item.get("symbol"))]
                        if normalized
                    }
                    if isinstance(selected_payload, list)
                    else {}
                )
                selector_mode = str(quality_selection_report.get("selector_mode", "")).strip()
                if quality_score_by_symbol and selector_mode in {
                    "quality",
                    "quality_all_eligible",
                    "snapshot_fallback",
                }:
                    non_pinned.sort(
                        key=lambda symbol: (
                            -quality_score_by_symbol.get(
                                _normalize_a_share_symbol(symbol) or symbol,
                                0.0,
                            ),
                            _normalize_a_share_symbol(symbol) or symbol,
                        )
                    )
                    ranking_mode = "universe_quality_score"
            symbol_list = _dedupe_preserve_order([*pinned_first, *non_pinned])[:monster_scan_cap]
            symbol_source = f"{symbol_source}:monster_cap"
            prefilter_report["selected_count"] = len(symbol_list)
        funnel_report: dict[str, object] = {
            "mode": "snapshot" if snapshot_mode else "direct",
            "policy": funnel_policy,
            "deep_stage_ran": deep_stage_ran,
            "deep_symbols_empty": bool(deep_stage_ran and deep_selected_count == 0),
            "deep_empty_reason": deep_empty_reason,
            "deep_selected_count": deep_selected_count,
            "pinned_added_count": len(pinned_added_symbols),
            "pipeline_input_count": len(symbol_list),
        }
        if funnel_policy == "intentional_full_deep":
            selection_source = "intentional_full_deep"
        elif deep_stage_ran and deep_selected_count:
            selection_source = "snapshot_deep"
        elif funnel_policy == "snapshot_funnel" and deep_stage_ran:
            selection_source = "deep_empty_pinned_only"
        else:
            selection_source = "direct_scan"
        if funnel_policy == "snapshot_funnel" and deep_stage_ran:
            deep_plus_pinned = deep_selected_count + len(pinned_added_symbols)
            effective_input_cap = (
                min(monster_scan_cap, deep_plus_pinned)
                if monster_scan_cap > 0
                else deep_plus_pinned
            )
        else:
            effective_input_cap = monster_scan_cap
        monster_scan_controls: dict[str, object] = {
            "cap": monster_scan_cap,
            "cap_applied": monster_scan_cap_applied,
            "intraday_scheduler_mode": intraday_scheduler_mode,
            "input_count": original_monster_scan_count,
            "selected_count": len(symbol_list),
            "dropped_count": max(0, original_monster_scan_count - len(symbol_list)),
            "ranking_mode": ranking_mode,
            "selection_source": selection_source,
            "effective_input_cap": effective_input_cap,
        }
        prefilter_report["monster_scan_controls"] = monster_scan_controls

        if not symbol_list:
            empty_fail_closed = bool(
                funnel_policy == "snapshot_funnel" and deep_stage_ran and deep_selected_count == 0
            )
            empty_reason = (
                "snapshot_deep_empty_fail_closed" if empty_fail_closed else "empty_watchlist"
            )
            empty_report: dict[str, object] = {
                "timestamp": now.isoformat(),
                "trace_id": "",
                "watchlist_size": 0,
                "symbol_source": symbol_source,
                "scan_profile": scan_profile_name,
                "funnel_policy": funnel_policy,
                "prefilter": prefilter_report,
                "funnel": dict(funnel_report),
                "monster_scan_controls": dict(monster_scan_controls),
                "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
                "signal_pool": {"candidate_count": 0, "candidates": []},
                "anomalies": {"event_count": 0, "events": []},
                "empty_signal": {
                    "triggered": True,
                    "reasons": [empty_reason],
                    "no_buy_streak": 0,
                    "buy_signals": 0,
                    "drawdown_pct": 0.0,
                    "risk_action": "unknown",
                },
                "monster_isolation": {
                    "can_open_new_position": False,
                    "reasons": ["empty_watchlist"],
                    "total_monster_position": 0.0,
                    "max_monster_position": 0.0,
                    "sentiment_score": 0.0,
                },
                "summary": {
                    "first_board_candidates": 0,
                    "leaders": 0,
                    "anomalies": 0,
                    "empty_signal_triggered": True,
                    "can_open_monster": False,
                    "watchlist_synced": False,
                },
            }
            if self._historical:
                empty_report["historical_context"] = self._historical_context_payload()
            self._finalize_report_writes(
                empty_report,
                audit_event="week5_scan",
                audit_payload={"watchlist_size": 0, "reason": empty_reason},
            )
            return empty_report

        if ctx.progress is not None:
            ctx.progress(
                phase="final_pipeline",
                total=len(symbol_list),
                current_symbol="",
            )

        def _pipeline_heartbeat(symbol: str, index: int, total: int, started: bool) -> None:
            if ctx.progress is not None:
                ctx.progress(
                    phase="final_pipeline",
                    completed=index if started else index + 1,
                    total=total,
                    current_symbol=symbol,
                )

        output_mode = str(config.week5.week5_output_mode).strip().lower() or "legacy"
        dual_track = output_mode == "dual_track"
        transform_workers = max(
            1,
            int(config.week5.final_pipeline_transform_max_workers),
        )
        monster_report = self._run_strategy_pipeline(
            symbols=symbol_list,
            strategy="monster",
            on_symbol_progress=_pipeline_heartbeat,
            transform_workers=transform_workers,
        )
        trend_report: dict[str, object] | None = None
        trend_pipeline_duration_ms = 0
        if dual_track:
            trend_started = perf_counter()
            trend_report = self._run_strategy_pipeline(
                symbols=symbol_list,
                strategy="trend",
                on_symbol_progress=_pipeline_heartbeat,
                transform_workers=transform_workers,
            )
            trend_pipeline_duration_ms = max(1, int((perf_counter() - trend_started) * 1000))
        trace_id = str(monster_report.get("trace_id", ""))
        if ctx.progress is not None:
            ctx.progress(trace_id=trace_id)
        monster_runtime_payload: dict[str, object] = {}
        monster_runtime = monster_report.get("runtime")
        if isinstance(monster_runtime, dict):
            monster_runtime_payload = dict(monster_runtime)
        raw_signals = monster_report.get("signals")
        signal_map: dict[str, dict[str, object]] = {}
        min_history_days = max(1, int(config.evolution.universe_spec.min_list_days))
        first_board_scan_lookback_days = max(
            40,
            min_history_days,
            int(config.evolution.universe_spec.first_board_scan_lookback_days),
        )
        if isinstance(raw_signals, list):
            for item in raw_signals:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    signal_map[symbol] = item
        trend_signal_map: dict[str, dict[str, object]] = {}
        if dual_track and isinstance(trend_report, dict):
            trend_raw = trend_report.get("signals")
            if isinstance(trend_raw, list):
                for item in trend_raw:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol", "")).strip()
                    if symbol:
                        trend_signal_map[symbol] = item

        signal_pool_candidates: list[dict[str, object]] = []
        candidate_signal_map = trend_signal_map if dual_track else signal_map
        for symbol, item in candidate_signal_map.items():
            reason_values = _string_list(item.get("reasons", []))
            if any(
                str(reason).strip().startswith("insufficient_history_days:")
                for reason in reason_values
            ):
                continue
            normalized_symbol = _normalize_a_share_symbol(symbol) or symbol
            candidate = backend.score_signal_pool_candidate(
                signal=item,
                prefilter_detail=prefilter_details_by_symbol.get(normalized_symbol),
            )
            bars = _bars_from_post_scan_enrichment(
                str(item.get("post_scan_enrichment", "")).strip()
            )
            overextension_decision: dict[str, object] = {
                "level": "none",
                "penalty": 0.0,
                "reject_new_buy": False,
                "reasons": [],
                "metrics": {},
            }
            board_decision: dict[str, object] = {
                "consecutive_limit_up": 0,
                "current_limit_state": "none",
                "board": "",
                "reject_new_buy": False,
                "reasons": [],
            }
            if bars is not None and not bars.empty:
                overextension_decision = _overextension_decision_dict(
                    row=_latest_bar_dict(bars),
                    config=config.overextension,
                )
                board_decision = _board_decision_dict(
                    bars=bars,
                    symbol=symbol,
                    config=config.board_risk,
                    limit_rule=config.limit_rule,
                )
            candidate["overextension"] = overextension_decision
            candidate["board_risk"] = board_decision
            candidate["reject_new_buy"] = bool(
                overextension_decision.get("reject_new_buy", False)
                or board_decision.get("reject_new_buy", False)
            )
            signal_pool_candidates.append(candidate)
        execution_rerank = backend.apply_execution_aware_rerank(
            candidates=signal_pool_candidates,
        )
        ranking_score_key = str(execution_rerank.get("score_key", "shortlist_score")).strip()
        if not ranking_score_key:
            ranking_score_key = "shortlist_score"
        signal_pool_candidates = sorted(
            signal_pool_candidates,
            key=lambda item: (
                -_as_float(
                    item.get(ranking_score_key),
                    default=_as_float(item.get("shortlist_score"), default=0.0),
                ),
                -_as_float(item.get("shortlist_score"), default=0.0),
                -_as_float(item.get("score"), default=0.0),
                str(item.get("symbol", "")),
            ),
        )
        shortlist_top_n = max(
            1,
            _as_int(config.week5.universe_prefilter_shortlist_top_n, default=50),
        )
        for index, item in enumerate(signal_pool_candidates):
            item["shortlist_rank"] = index + 1
            item["shortlist_selected"] = index < shortlist_top_n

        gate_reasons = [str(item) for item in (data_gate.get("reasons") or [])]
        snapshot_only_blocked = bool(gate_reasons) and all(
            reason.startswith("feature_snapshot") for reason in gate_reasons
        )
        recovery_direct_scan = bool(ctx.recovery_mode) and snapshot_only_blocked
        final_selector_gate_status = (
            "ok" if recovery_direct_scan else str(data_gate.get("status", "ok"))
        )
        breadth_meta: dict[str, object] = {"enabled": False}
        breadth_lift = 0.0
        if self._historical:
            if bool(config.week5.market_breadth_enabled):
                breadth_meta, breadth_lift = self._historical_market_breadth(now=now)
                if bool(breadth_meta.get("block_new_buy", False)):
                    final_selector_gate_status = "market_breadth_blocked"
        elif bool(config.week5.market_breadth_enabled):
            breadth_meta, breadth_lift = backend.market_breadth_gate(now=now)
            if bool(breadth_meta.get("block_new_buy", False)):
                final_selector_gate_status = "market_breadth_blocked"
        final_selector = backend.final_signal_selector(
            signals=signal_pool_candidates,
            data_gate_status=final_selector_gate_status,
            min_threshold_lift=breadth_lift,
            news_mode_override="off" if self._policy.news_neutralized else None,
        )
        if recovery_direct_scan:
            # Advisory only: the output is inspected, never treated as a
            # production signal source.
            advisory_signals = list(final_selector.get("final_signals", []))
            for item in advisory_signals:
                if isinstance(item, dict):
                    item["advisory"] = True
            final_selector["advisory_only"] = True
            final_selector["advisory_signals"] = advisory_signals
            final_selector["final_signals"] = []
            final_selector["selected_count"] = 0

        shortlist_preview = [
            {
                "symbol": str(item.get("symbol", "")).strip(),
                "shortlist_score": _as_float(item.get("shortlist_score"), default=0.0),
                "score": _as_float(item.get("score"), default=0.0),
                "shortlist_reasons": _string_list(item.get("shortlist_reasons", []))[:6],
                **(
                    {
                        "execution_reranked_score": _as_float(
                            item.get("execution_reranked_score"),
                            default=_as_float(item.get("shortlist_score"), default=0.0),
                        ),
                        "execution_aware_score": _as_float(
                            item.get("execution_aware_score"),
                            default=0.0,
                        ),
                        "execution_high_risk": bool(item.get("execution_high_risk", False)),
                    }
                    if ranking_score_key == "execution_reranked_score"
                    else {}
                ),
            }
            for item in signal_pool_candidates[: min(shortlist_top_n, 10)]
        ]
        raw_stages = prefilter_report.get("stages")
        if isinstance(raw_stages, dict):
            raw_stage2 = raw_stages.get("stage2")
            if isinstance(raw_stage2, dict):
                raw_stage2.update(
                    {
                        "applied": True,
                        "status": "completed" if signal_pool_candidates else "no_candidates",
                        "score_key": ranking_score_key,
                        "shortlist_top_n": shortlist_top_n,
                        "input_count": len(signal_pool_candidates),
                        "advanced_count": min(shortlist_top_n, len(signal_pool_candidates)),
                        "preview": shortlist_preview,
                        "execution_rerank": dict(execution_rerank),
                    }
                )

        if ctx.progress is not None:
            ctx.progress(phase="first_board_anomaly")

        first_board_anomaly_started = perf_counter()
        first_board_candidates: list[dict[str, object]] = []
        anomalies: list[dict[str, object]] = []
        bars_provider = (
            ctx.provider if self._historical else backend.select_live_runtime_provider()
        )
        for symbol in symbol_list:
            signal = signal_map.get(symbol, {})
            bars = _bars_from_post_scan_enrichment(
                str(signal.get("post_scan_enrichment", "")).strip()
                if isinstance(signal, dict)
                else ""
            )
            if bars is None:
                try:
                    bars = bars_provider.fetch_daily_bars(
                        symbol=symbol,
                        lookback_days=first_board_scan_lookback_days,
                    )
                except Exception as exc:
                    anomalies.append(
                        {
                            "symbol": symbol,
                            "types": ["data_source_error"],
                            "detail": str(exc),
                        }
                    )
                    continue
            if len(bars) < min_history_days:
                anomalies.append(
                    {
                        "symbol": symbol,
                        "types": ["insufficient_history"],
                        "history_days": len(bars),
                        "required_history_days": min_history_days,
                    }
                )
                continue
            if len(bars) < 2:
                continue
            board_candidate = backend.build_first_board_candidate(
                symbol=symbol,
                bars=bars,
                signal=signal,
            )
            if board_candidate is not None:
                first_board_candidates.append(board_candidate)

            anomaly = backend.detect_symbol_anomaly(
                symbol=symbol,
                bars=bars,
            )
            if anomaly is not None:
                anomalies.append(anomaly)
        first_board_anomaly_ms = max(1, int((perf_counter() - first_board_anomaly_started) * 1000))

        empty_signal = self._evaluate_empty_signal(monster_report=monster_report)
        isolation = self._monster_isolation_gate(
            monster_report=monster_report,
            empty_signal=empty_signal,
        )

        max_stock_position = config.monster_risk.max_stock_position
        block_all = not isolation["can_open_new_position"]
        for item in first_board_candidates:
            suggested = _as_float(item.get("suggested_position"), default=0.0)
            isolated = block_all or suggested > max_stock_position
            item["isolated"] = isolated
            if suggested > max_stock_position:
                item["isolation_reason"] = f"suggested_position_exceeds_{max_stock_position:.4f}"
            elif block_all:
                isolation_reason_values = isolation.get("reasons", [])
                if isinstance(isolation_reason_values, list):
                    reason_text = ",".join(str(x) for x in isolation_reason_values)
                else:
                    reason_text = ""
                item["isolation_reason"] = reason_text
            else:
                item["isolation_reason"] = ""

        leaders = sorted(
            first_board_candidates,
            key=lambda item: (
                -_as_float(item.get("leader_score"), default=0.0),
                -_as_float(item.get("score"), default=0.0),
                str(item.get("symbol", "")),
            ),
        )[:3]

        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "trace_id": trace_id,
            "watchlist_size": len(symbol_list),
            "symbol_source": symbol_source,
            "scan_profile": scan_profile_name,
            "emergency_direct_scan": recovery_direct_scan,
            "recovery_mode": bool(ctx.recovery_mode),
            "run_mode": self._policy.mode,
            "prefilter": prefilter_report,
            "data_snapshot_id": str(snapshot_manifest.data_snapshot_id)
            if snapshot_manifest is not None
            else "",
            "data_gate": dict(data_gate),
            "market_breadth": dict(breadth_meta),
            "feature_snapshot": feature_snapshot_report,
            "scan_stages": {
                "quality_selection": {
                    "duration_ms": quality_selection_ms,
                    "selected_count": _as_int(
                        quality_selection_report.get("selected_count", 0),
                        default=0,
                    )
                    if isinstance(quality_selection_report, dict)
                    else 0,
                },
                "snapshot_ensure": {
                    "duration_ms": snapshot_ensure_ms,
                    "enabled": bool(config.week5.feature_snapshot_enabled),
                    "requested_count": (
                        _as_int(
                            feature_snapshot_report.get("requested_symbol_count", 0),
                            default=0,
                        )
                        if isinstance(feature_snapshot_report, dict)
                        else 0
                    ),
                    "published_count": (
                        _as_int(
                            feature_snapshot_report.get("published_symbol_count", 0),
                            default=0,
                        )
                        if isinstance(feature_snapshot_report, dict)
                        else 0
                    ),
                    "ok": bool(feature_snapshot_report.get("ok", False))
                    if isinstance(feature_snapshot_report, dict)
                    else False,
                },
                "light_stage": {
                    "duration_ms": light_stage_ms,
                    "mode": str(prefilter_report.get("mode", "")),
                    "shortlisted_count": _as_int(
                        prefilter_report.get("shortlisted_count", 0), default=0
                    ),
                },
                "deep_stage": {
                    "duration_ms": deep_stage_ms,
                    "mode": str(deep_report.get("mode", "")),
                    "selected_count": _as_int(deep_report.get("selected_count", 0), default=0),
                },
                "final_pipeline": {
                    "duration_ms": _as_int(monster_runtime_payload.get("duration_ms"), default=0),
                    "symbols": len(symbol_list),
                    **backend_final_pipeline_timing(monster_runtime_payload),
                },
                "first_board_anomaly": {
                    "duration_ms": first_board_anomaly_ms,
                },
            },
            "funnel": {
                **funnel_report,
                "light_candidate_target": max(1, int(config.week5.light_candidate_target)),
                "deep_candidate_target": deep_candidate_target,
                "final_signal_cap": max(0, int(config.week5.final_signal_cap)),
                "allow_zero_signal": bool(config.week5.allow_zero_signal),
                "light_count": _as_int(prefilter_report.get("shortlisted_count", 0), default=0),
                "deep_count": len(symbol_list),
                "final_count": _as_int(final_selector.get("selected_count", 0), default=0),
                "final_selection": dict(final_selector),
            },
            "runtime_source": {
                "mode": (
                    "realtime_overlay"
                    if (
                        not self._historical
                        and backend.runtime_source_mode() == "realtime_overlay"
                    )
                    else "offline_only"
                ),
                "provider": str(config.data_source.runtime_live_provider).strip() or "offline",
            },
            "runtime": {
                "monster_pipeline": monster_runtime_payload,
            },
            "monster_scan_controls": dict(monster_scan_controls),
            "first_board": {
                "interval_minutes": max(1, config.week5.first_board_interval_min),
                "window_intervals": list(config.week5.first_board_window_intervals),
                "windows": list(config.week5.first_board_windows),
                "candidate_count": len(first_board_candidates),
                "candidates": first_board_candidates,
                "leaders": leaders,
            },
            "signal_pool": {
                "candidate_count": len(signal_pool_candidates),
                # 全量口径的 action 计数（candidates 列表本身截断到前 100，
                # 全市场扫描时按截断列表统计会低估，这里独立提供全量计数）。
                "action_counts": _signal_pool_action_counts(signal_pool_candidates),
                "candidates": signal_pool_candidates[:100],
                "execution_rerank": dict(execution_rerank),
                "ranking": {
                    "mode": "two_stage_funnel",
                    "score_key": ranking_score_key,
                    "shortlist_top_n": shortlist_top_n,
                    "selected_count": min(shortlist_top_n, len(signal_pool_candidates)),
                    "selected_symbols": [
                        str(item.get("symbol", "")).strip()
                        for item in signal_pool_candidates[:shortlist_top_n]
                        if str(item.get("symbol", "")).strip()
                    ],
                    "preview": shortlist_preview,
                    "execution_rerank": dict(execution_rerank),
                },
            },
            "dual_track": backend.build_dual_track_output(
                final_selector=final_selector,
                signal_map=signal_map,
                trend_signal_map=trend_signal_map,
                dual_track=dual_track,
                trend_pipeline_duration_ms=trend_pipeline_duration_ms,
            ),
            "anomalies": {
                "event_count": len(anomalies),
                "events": anomalies,
            },
            "empty_signal": empty_signal,
            "monster_isolation": isolation,
            "summary": {
                "first_board_candidates": len(first_board_candidates),
                "leaders": len(leaders),
                "anomalies": len(anomalies),
                "empty_signal_triggered": bool(empty_signal.get("triggered", False)),
                "can_open_monster": bool(isolation.get("can_open_new_position", False)),
                "prefilter_applied": bool(prefilter_report.get("applied", False)),
                "prefilter_shortlisted": _as_int(
                    prefilter_report.get("shortlisted_count"),
                    default=len(symbol_list),
                ),
                "monster_scan_cap_applied": monster_scan_cap_applied,
                "monster_scan_dropped_count": max(
                    0,
                    original_monster_scan_count - len(symbol_list),
                ),
                "execution_rerank_applied": bool(execution_rerank.get("applied", False)),
            },
        }
        if self._historical:
            report["historical_context"] = self._historical_context_payload(
                intraday_degraded=intraday_degraded,
                intraday_coverage_ratio=intraday_coverage_ratio,
            )

        watchlist_sync = self._apply_watchlist_sync(
            report=report,
            symbol_source=symbol_source,
            intraday_scheduler_mode=intraday_scheduler_mode,
        )
        report["watchlist_sync"] = watchlist_sync
        summary = report.get("summary")
        if isinstance(summary, dict):
            summary["watchlist_synced"] = bool(watchlist_sync.get("updated", False))

        has_warning = bool(empty_signal.get("triggered", False)) or len(anomalies) > 0
        self._finalize_report_writes(
            report,
            audit_event="week5_scan",
            audit_payload={
                "watchlist_size": len(symbol_list),
                "first_board_candidates": len(first_board_candidates),
                "anomalies": len(anomalies),
                "empty_signal_triggered": bool(empty_signal.get("triggered", False)),
                "can_open_monster": bool(isolation.get("can_open_new_position", False)),
                "watchlist_sync": watchlist_sync,
            },
            trace_id=trace_id,
            has_warning=has_warning,
        )
        # 与原实现一致：recovery 运行只禁关注池同步与 final 生效（advisory），
        # 通知照发——不在这里做 recovery 短路。
        if self._policy.notify:
            backend.notify_scan(
                symbol_list=symbol_list,
                first_board_candidates=first_board_candidates,
                leaders=leaders,
                anomalies=anomalies,
                empty_signal=empty_signal,
                watchlist_sync=watchlist_sync,
                runtime_mode=(
                    "realtime_overlay"
                    if backend.runtime_source_mode() == "realtime_overlay"
                    else "offline_only"
                ),
                has_warning=has_warning,
                trace_id=trace_id,
                now=now,
            )
            if not bool(
                getattr(config.week5, "full_market_automation_enabled", False)
            ):
                backend.notify_actionable_signals(
                    monster_report,
                    trace_id=trace_id,
                    title_prefix="week5 scan",
                )
        return report

    # ------------------------------------------------------------------
    # 上下文辅助
    # ------------------------------------------------------------------
    def _backend_service(self) -> object:
        return getattr(self._backend, "service", None)

    def _snapshot_root(self) -> str:
        if self._historical and self._ctx.artifact_dir is not None:
            return str(Path(self._ctx.artifact_dir) / "features_light")
        root = str(self._config.week5.feature_snapshot_root).strip()
        return root or "artifacts/features_light"

    def _historical_context_payload(
        self,
        *,
        intraday_degraded: bool = False,
        intraday_coverage_ratio: float = 1.0,
    ) -> dict[str, object]:
        ctx = self._ctx
        return {
            "as_of": ctx.as_of.isoformat() if ctx.as_of else "",
            "decision_time": ctx.now.isoformat(),
            "account": {
                "neutral": True,
                "current_equity": ctx.account.current_equity,
                "pause_new_buy": ctx.account.pause_new_buy,
                "no_buy_streak": ctx.account.no_buy_streak,
                "monster_positions": len(ctx.account.monster_positions),
            },
            "model": dict(ctx.model_info.to_payload()),
            "news_neutralized": True,
            "intraday_degraded": intraday_degraded,
            "intraday_coverage_ratio": round(intraday_coverage_ratio, 4),
            "market_breadth_recomputed": True,
            "realtime_data_allowed": False,
        }

    def _build_data_gate(
        self,
        *,
        snapshot_manifest: object,
        snapshot_current: bool,
        latest_trade_date: str,
    ) -> dict[str, Any]:
        if not self._historical:
            return self._backend.build_data_gate(
                snapshot_manifest=snapshot_manifest,
                snapshot_current=snapshot_current,
                latest_trade_date=latest_trade_date,
                now=self._ctx.now,
            )
        return build_historical_data_gate(
            provider=self._ctx.provider,
            config=self._config,
            snapshot_manifest=snapshot_manifest,
            snapshot_current=snapshot_current,
            latest_trade_date=latest_trade_date,
            now=self._ctx.now,
        )

    # ------------------------------------------------------------------
    # historical 股票池
    # ------------------------------------------------------------------
    def _resolve_historical_universe(self) -> dict[str, Any]:
        """从完整 provider 索引生成 as-of 有效股票池并执行质量选择。"""
        ctx = self._ctx
        config = self._config
        provider = ctx.provider
        assert provider is not None and ctx.as_of is not None  # historical 契约
        index_symbols = _dedupe_preserve_order(
            [
                normalized
                for normalized in (
                    _normalize_a_share_symbol(item) for item in provider.list_symbols()
                )
                if normalized
            ]
        )
        # as-of 有效性：批量取 lookback=1 的"最近一根"（≤ as_of），只保留
        # 截止 as_of 仍有数据、且最近数据在 staleness 窗口内的 symbol。
        # 未来上市（as_of 前无任何数据）与 as_of 前已退市（数据早已停更）
        # 都在这一步被剔除，不依赖当前退市名单。
        max_staleness_days = max(
            0, _as_int(config.week5.universe_quality_max_staleness_days, default=10)
        )
        valid_symbols: list[str] = []
        batch_error = ""
        try:
            probe = provider.fetch_universe_quality_metrics(
                symbols=index_symbols,
                lookback_days=1,
                end_date=ctx.as_of,
            )
            if isinstance(probe, pd.DataFrame) and not probe.empty:
                dates = pd.to_datetime(probe["date"], errors="coerce")
                probe = probe.assign(_date=dates).dropna(subset=["_date"])
                as_of_ts = pd.Timestamp(ctx.as_of)
                for symbol_value, row_date in zip(
                    probe["symbol"].astype(str), probe["_date"], strict=True
                ):
                    normalized = _normalize_a_share_symbol(symbol_value)
                    if not normalized:
                        continue
                    if (as_of_ts - row_date).days <= max_staleness_days:
                        valid_symbols.append(normalized)
        except Exception as exc:  # noqa: BLE001 - 股票池解析失败降级为空池 + 报告
            batch_error = f"{type(exc).__name__}: {exc}"
        valid_symbols = _dedupe_preserve_order(sorted(valid_symbols))
        quality_target = max(
            1, _as_int(config.week5.universe_quality_target_size, default=300)
        )
        quality_selection_report: dict[str, object] | None = None
        selected = valid_symbols
        selector_error = ""
        if valid_symbols:
            selection = self._backend.select_universe_quality_candidates(
                symbols=valid_symbols,
                target_size=quality_target,
                trade_date=ctx.as_of.isoformat(),
                reference_date=ctx.as_of.isoformat(),
                ruleset_id=str(config.evolution.universe_spec.universe_ruleset_id),
                board_scope=list(config.evolution.universe_spec.board_scope),
                end_date=ctx.as_of,
                selection_snapshot_path=(
                    str(Path(self._snapshot_root()).parent / "universe_selection.json")
                ),
            )
            quality_report_value = selection.get("report", {})
            if isinstance(quality_report_value, dict):
                quality_selection_report = quality_report_value
            selected = _string_list(selection.get("selected", []))
            selector_error = str(
                (quality_selection_report or {}).get("fallback_reason", "") or ""
            )
        return {
            "symbols": selected,
            "source": "provider_index:as_of_quality_selector",
            "quality_selection_report": quality_selection_report,
            "board_quota": (
                quality_selection_report.get("board_quotas", {})
                if isinstance(quality_selection_report, dict)
                else {}
            ),
            "universe_errors": ([batch_error] if batch_error else [])
            + ([f"selector_degraded:{selector_error}"] if selector_error else []),
            "report": {
                "provider_index_count": len(index_symbols),
                "as_of_valid_count": len(valid_symbols),
                "selected_count": len(selected),
                "max_staleness_days": max_staleness_days,
                "batch_error": batch_error,
                "quality_target": quality_target,
            },
        }

    def _build_historical_snapshot(self, *, symbols: list[str]) -> dict[str, object]:
        """把历史 Feature Snapshot 构建到任务独立目录（绝不触碰生产 root）。"""
        ctx = self._ctx
        config = self._config
        provider = ctx.provider
        assert provider is not None
        return build_feature_snapshot(
            config=config,
            provider=provider,
            symbols=list(symbols),
            # Feature Snapshot transform 最多 4 worker（PLAN 性能约束）。
            max_workers=max(1, min(4, int(config.week5.feature_snapshot_max_workers))),
            batch_size=max(1, int(config.week5.feature_snapshot_batch_size)),
            scope="week5_historical",
            force=True,
        )

    def _probe_historical_intraday_coverage(self, symbols: list[str]) -> float:
        """探测 as-of 分钟摘要覆盖率（1m 口径，失败按 0 处理）。"""
        provider = self._ctx.provider
        if provider is None or not symbols:
            return 0.0
        covered = 0
        try:
            frames = provider.fetch_intraday_summaries(list(symbols), "1m", lookback_days=10)
            for symbol in symbols:
                frame = frames.get(symbol)
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    covered += 1
        except Exception:
            return 0.0
        return covered / max(1, len(symbols))

    def _historical_market_breadth(self, *, now: datetime) -> tuple[dict[str, object], float]:
        """从 as-of 日线现算历史市场广度（不读生产 market_breadth.json）。"""
        from stock_analyzer.ops.market_breadth import (
            breadth_usage_policy,
            compute_market_breadth_from_warehouse,
        )

        provider = self._ctx.provider
        if provider is None:
            return _breadth_unavailable_meta(), 0.0
        snapshot = compute_market_breadth_from_warehouse(provider, now=now)
        if snapshot is None:
            return _breadth_unavailable_meta(), 0.0
        policy = breadth_usage_policy(
            snapshot=snapshot,
            now=now,
            trend_min_threshold=float(
                _as_float(self._config.week5.final_signal_min_threshold, default=70.0)
            ),
            disable_if_below=float(self._config.week5.market_breadth_disable_if_below),
            max_intraday_heartbeat_sec=48.0 * 3600.0,
        )
        stale_lift = float(_as_float(policy.get("trend_min_threshold_lift"), default=0.0))
        meta: dict[str, object] = {
            "enabled": True,
            "block_new_buy": bool(policy.get("block_new_buy", False)),
            "reason": str(policy.get("reason", "breadth_ok")),
            "score": policy.get("score"),
            "trend_min_threshold_lift": round(stale_lift, 4),
            "source": "historical_recompute",
        }
        return meta, stale_lift

    # ------------------------------------------------------------------
    # live 深度阶段（分钟同步 + 新鲜度门 + fresh deep frame + pinned）
    # ------------------------------------------------------------------
    def _run_live_deep_stage(
        self,
        *,
        light_symbols: list[str],
        snapshot_frame: pd.DataFrame,
        snapshot_mode: bool,
        funnel_policy: str,
        scan_profile_name: str,
        prefilter_report: dict[str, Any],
        deep_candidate_target: int,
        cal_source: object | None,
        required_intraday_date: object | None,
        deep_started: float,
    ) -> tuple[dict[str, Any], bool, int]:
        """live 深度阶段：分钟同步、1m+5m 新鲜度门、fresh deep frame、pinned。

        返回 ``(deep_report, blocked, deep_stage_ms)``；blocked=True 时
        ``deep_report`` 是一份完整 blocked 报告，由调用方短路返回。
        """
        from stock_analyzer.ops.intraday_freshness import (
            IntradayFreshnessReport,
            build_intraday_freshness_report,
        )

        backend = self._backend
        config = self._config
        ctx = self._ctx
        deep_stage_ms = 0
        eligible, pinned_candidates, unsupported_market, sync_targets = (
            _prepare_intraday_sync_symbols(
                light_symbols=light_symbols,
                pinned_symbols=ctx.pinned_symbols,
            )
        )
        _delta_warehouse = None
        _vendor_overlay = None
        try:
            _delta_warehouse = backend.market_warehouse()
        except Exception:
            _delta_warehouse = None
        try:
            _vendor_overlay = backend.provider()
            if not hasattr(_vendor_overlay, "_intraday_warehouse"):
                for candidate_provider in backend.provider_graph():
                    if hasattr(candidate_provider, "_intraday_warehouse"):
                        _vendor_overlay = candidate_provider
                        break
        except Exception:
            pass
        sync_report: dict[str, Any] = {}
        if (
            sync_targets
            and required_intraday_date is not None
            and bool(getattr(config.week5, "intraday_sync_enabled", True))
        ):
            _sync_primary = str(getattr(config.week5, "intraday_sync_primary", "sina"))
            _sync_fallback = str(getattr(config.week5, "intraday_sync_fallback", "sina"))
            try:
                sync_report = _sync_intraday_symbols(
                    warehouse=_delta_warehouse,
                    vendor_overlay=_vendor_overlay,
                    symbols=sync_targets,
                    required_trade_date=required_intraday_date,
                    primary=_sync_primary,
                    fallback=_sync_fallback,
                    concurrency=max(
                        1, int(getattr(config.week5, "intraday_sync_concurrency", 4))
                    ),
                    timeout_sec=max(
                        1, int(getattr(config.week5, "intraday_sync_timeout_sec", 5))
                    ),
                    deadline_sec=max(
                        30,
                        int(getattr(config.week5, "intraday_sync_deadline_sec", 180)),
                    ),
                    tushare_provider=(
                        cal_source
                        if callable(getattr(cal_source, "fetch_minute_bars", None))
                        else None
                    ),
                )
            except Exception as exc:
                sync_report = {
                    "error": f"{type(exc).__name__}:{exc}",
                    "symbols_total": len(sync_targets),
                    "ok": 0,
                    "failed": len(sync_targets),
                    "detail": {"primary": _sync_primary, "fallback": _sync_fallback},
                }
            _record_intraday_sync_health_audits(
                service=self._backend_service(), sync_report=sync_report
            )
            for src in (_vendor_overlay, _delta_warehouse):
                try:
                    fn = getattr(src, "clear_cache", None)
                    if callable(fn):
                        fn()
                except Exception:
                    pass
        elif sync_targets and required_intraday_date is None:
            sync_report = {"skipped": True, "reason": "no_required_intraday_date"}

        freshness_report: dict[str, Any] = {}
        freshness_obj: Any = None
        if eligible and required_intraday_date is not None:
            try:
                report_1m = build_intraday_freshness_report(
                    warehouse=_delta_warehouse,
                    vendor_overlay=_vendor_overlay,
                    symbols=eligible,
                    required_trade_date=required_intraday_date,  # type: ignore[arg-type]
                    interval="1m",
                    deep_candidate_target=deep_candidate_target,
                )
                report_5m = build_intraday_freshness_report(
                    warehouse=_delta_warehouse,
                    vendor_overlay=_vendor_overlay,
                    symbols=eligible,
                    required_trade_date=required_intraday_date,  # type: ignore[arg-type]
                    interval="5m",
                    deep_candidate_target=deep_candidate_target,
                )
                fresh_1m = set(report_1m.fresh_symbols)
                fresh_5m = set(report_5m.fresh_symbols)
                fresh_both = sorted(fresh_1m & fresh_5m)
                freshness_obj = report_1m
                freshness_obj.fresh_symbols = fresh_both
                freshness_obj.fresh_count = len(fresh_both)
                freshness_obj.delta_missing = sorted(
                    set(report_1m.delta_missing) | set(report_5m.delta_missing)
                )
                freshness_obj.summary_missing = sorted(
                    set(report_1m.summary_missing) | set(report_5m.summary_missing)
                )
                freshness_obj.effective_stale = sorted(
                    set(report_1m.effective_stale) | set(report_5m.effective_stale)
                )
                freshness_obj.session_incomplete = sorted(
                    set(report_1m.session_incomplete) | set(report_5m.session_incomplete)
                )
                eligible_for_ratio = max(
                    1, len([s for s in eligible if s not in set(report_1m.unsupported_market)])
                )
                freshness_obj.fresh_ratio = (
                    round(len(fresh_both) / eligible_for_ratio, 4) if eligible else 0.0
                )
                freshness_obj.source_breakdown = {
                    k: max(
                        report_1m.source_breakdown.get(k, 0),
                        report_5m.source_breakdown.get(k, 0),
                    )
                    for k in set(report_1m.source_breakdown) | set(report_5m.source_breakdown)
                }
                freshness_report = freshness_obj.to_dict()
                freshness_report["interval"] = "1m+5m"
                freshness_report["fresh_1m_count"] = len(fresh_1m)
                freshness_report["fresh_5m_count"] = len(fresh_5m)
            except Exception as exc:
                freshness_report = {"error": f"{type(exc).__name__}:{exc}"}
        elif not eligible:
            fallback_obj = IntradayFreshnessReport(required_trade_date=None)
            freshness_obj = fallback_obj
            freshness_report = fallback_obj.to_dict()
            freshness_report["required_trade_date"] = (
                str(required_intraday_date) if required_intraday_date else ""
            )

        fresh_ratio_min = float(getattr(config.week5, "intraday_fresh_ratio_min", 0.95))
        fresh_count = int(freshness_report.get("fresh_count", 0)) if freshness_report else 0
        fresh_ratio = (
            float(freshness_report.get("fresh_ratio", 0.0)) if freshness_report else 0.0
        )
        eligible_count = (
            int(freshness_report.get("eligible_count", len(eligible)))
            if freshness_report
            else len(eligible)
        )
        _is_synthetic = _detect_synthetic_provider(backend)
        should_gate = bool(
            snapshot_mode
            and funnel_policy == "snapshot_funnel"
            and eligible
            and not _is_synthetic
        )
        if should_gate and (
            fresh_ratio < fresh_ratio_min or fresh_count < deep_candidate_target
        ):
            blocked = _build_intraday_freshness_blocked_report(
                now=ctx.now,
                scan_profile=scan_profile_name,
                freshness_report=freshness_report,
                funnel_policy=funnel_policy,
            )
            blocked["prefilter"] = prefilter_report
            blocked["intraday_sync"] = sync_report
            blocked["unsupported_market"] = unsupported_market
            blocked["required_intraday_date"] = (
                str(required_intraday_date) if required_intraday_date else ""
            )
            self._finalize_report_writes(
                blocked,
                audit_event="week5_scan_blocked_intraday_freshness",
                audit_payload={
                    "fresh_count": fresh_count,
                    "fresh_ratio": fresh_ratio,
                    "eligible_count": eligible_count,
                    "deep_candidate_target": deep_candidate_target,
                },
            )
            deep_stage_ms = max(1, int((perf_counter() - deep_started) * 1000))
            return blocked, True, deep_stage_ms
        prefilter_report["intraday_sync"] = sync_report
        prefilter_report["intraday_freshness"] = freshness_report
        prefilter_report["unsupported_market"] = unsupported_market
        prefilter_report["unsupported_market_count"] = len(unsupported_market)
        prefilter_report["required_intraday_date"] = (
            str(required_intraday_date) if required_intraday_date else ""
        )

        # pinned 必须与 eligible 通过同一道新鲜度门；未评估的 pinned 走
        # 按需探测（synthetic 测试环境直接放行，与旧实现一致）。
        pinned_fresh: list[str] = []
        if pinned_candidates:
            for pinned_symbol in pinned_candidates:
                if freshness_obj is not None:
                    evaled = (
                        set(getattr(freshness_obj, "fresh_symbols", []))
                        | set(getattr(freshness_obj, "effective_stale", []))
                        | set(getattr(freshness_obj, "session_incomplete", []))
                        | set(getattr(freshness_obj, "unsupported_market", []))
                        | set(getattr(freshness_obj, "summary_missing", []))
                        | set(getattr(freshness_obj, "delta_missing", []))
                    )
                    if pinned_symbol in evaled:
                        if pinned_symbol in getattr(freshness_obj, "fresh_symbols", []):
                            pinned_fresh.append(pinned_symbol)
                    elif _is_synthetic:
                        pinned_fresh.append(pinned_symbol)
                    else:
                        try:
                            pinned_report = build_intraday_freshness_report(
                                warehouse=_delta_warehouse,
                                vendor_overlay=_vendor_overlay,
                                symbols=[pinned_symbol],
                                required_trade_date=required_intraday_date,  # type: ignore[arg-type]
                                interval="1m",
                                deep_candidate_target=1,
                            )
                            pinned_report_5m = build_intraday_freshness_report(
                                warehouse=_delta_warehouse,
                                vendor_overlay=_vendor_overlay,
                                symbols=[pinned_symbol],
                                required_trade_date=required_intraday_date,  # type: ignore[arg-type]
                                interval="5m",
                                deep_candidate_target=1,
                            )
                            if (
                                pinned_symbol in pinned_report.fresh_symbols
                                and pinned_symbol in pinned_report_5m.fresh_symbols
                            ):
                                pinned_fresh.append(pinned_symbol)
                        except Exception:
                            # 检查失败不放行（fail-closed）。
                            pass
                elif _is_synthetic:
                    pinned_fresh.append(pinned_symbol)
        prefilter_report["_fresh_pinned_symbols"] = pinned_fresh

        fresh_symbols = (
            list(freshness_report.get("fresh_symbols", [])) if freshness_report else eligible
        )
        if not fresh_symbols:
            fresh_symbols = eligible
        fresh_deep_frame: object | None = None
        fresh_failed: list[str] = []
        if fresh_symbols and required_intraday_date is not None:
            try:
                result: dict[str, Any] = _build_fresh_deep_frame(
                    provider=backend.provider(),
                    warehouse=_delta_warehouse,
                    vendor_overlay=_vendor_overlay,
                    symbols=fresh_symbols,
                    required_date=required_intraday_date,
                    lookback_days=max(
                        60, int(config.week5.feature_snapshot_lookback_days)
                    ),
                )
                fresh_deep_frame = result.get("frame")
                fresh_failed = list(result.get("failed", []))
            except Exception:
                fresh_deep_frame = None
        if isinstance(fresh_deep_frame, pd.DataFrame) and not fresh_deep_frame.empty:
            fresh_light_report = dict(prefilter_report)
            fresh_light_report["shortlisted"] = (
                [
                    s
                    for s in (prefilter_report.get("shortlisted", []) or [])
                    if str(s.get("symbol", "")).strip() in set(fresh_symbols)
                ]
                if isinstance(prefilter_report.get("shortlisted"), list)
                else []
            )
            deep_report = backend.deep_stage_from_snapshot(
                frame=fresh_deep_frame,
                target=deep_candidate_target,
                light_report=fresh_light_report,
            )
            deep_report["fresh_frame_used"] = True
            deep_report["fresh_frame_failed"] = fresh_failed
        elif (
            not _is_synthetic
            and snapshot_mode
            and funnel_policy == "snapshot_funnel"
            and int(freshness_report.get("fresh_count", 0)) > 0
        ):
            # 分钟同步成功但 fresh frame 构建失败：阻断 deep 阶段，
            # 绝不静默回退 daily-only 快照（P1-6 语义，与旧实现一致）。
            deep_report = _build_intraday_freshness_blocked_report(
                now=ctx.now,
                scan_profile=scan_profile_name,
                freshness_report=freshness_report,
                funnel_policy=funnel_policy,
            )
            deep_report["fresh_frame_used"] = False
            deep_report["fresh_frame_failed"] = fresh_failed
            deep_report["fresh_blocked_reason"] = "fresh_deep_frame_empty_or_none"
            deep_report["prefilter"] = prefilter_report
            deep_report["intraday_sync"] = sync_report
            deep_report["unsupported_market"] = unsupported_market
            deep_report["required_intraday_date"] = (
                str(required_intraday_date) if required_intraday_date else ""
            )
            self._finalize_report_writes(
                deep_report,
                audit_event="week5_scan_blocked_intraday_freshness",
                audit_payload={"reason": "fresh_deep_frame_empty_or_none"},
            )
            deep_stage_ms = max(1, int((perf_counter() - deep_started) * 1000))
            return deep_report, True, deep_stage_ms
        else:
            deep_report = backend.deep_stage_from_snapshot(
                frame=snapshot_frame,
                target=deep_candidate_target,
                light_report=prefilter_report,
            )
            deep_report["fresh_frame_used"] = False
            deep_report["fresh_frame_failed"] = fresh_failed
        deep_stage_ms = max(1, int((perf_counter() - deep_started) * 1000))
        if not _is_synthetic:
            deep_report["selected"] = [
                item
                for item in deep_report.get("selected", [])
                if isinstance(item, dict)
                and str(item.get("symbol", "")).strip() in set(fresh_symbols)
            ]
        return deep_report, False, deep_stage_ms

    # ------------------------------------------------------------------
    # pipeline 执行（live/historical 分叉点）
    # ------------------------------------------------------------------
    def _run_strategy_pipeline(
        self,
        *,
        symbols: list[str],
        strategy: str,
        on_symbol_progress: Callable[[str, int, int, bool], None],
        transform_workers: int,
    ) -> dict[str, object]:
        ctx = self._ctx
        if self._historical:
            assert ctx.run_pipeline_fn is not None
            return cast(
                dict[str, object],
                ctx.run_pipeline_fn(
                    symbols=symbols,
                    strategy=strategy,
                    current_equity=ctx.account.current_equity,
                    on_symbol_progress=on_symbol_progress,
                    transform_max_workers=1,  # as_of 模式不支持 transform 并行
                ),
            )
        return self._backend.run_pipeline(
            symbols=symbols,
            strategy=strategy,
            current_equity=ctx.account.current_equity,
            use_live_runtime=True,
            dry_run_execution=True,
            job_name=f"week5_scan_{strategy}",
            on_symbol_progress=on_symbol_progress,
            transform_max_workers=transform_workers,
            capture_post_scan_enrichment=True,
        )

    # ------------------------------------------------------------------
    # 账户状态评估（用 context.account 替代服务内部状态）
    # ------------------------------------------------------------------
    def _evaluate_empty_signal(
        self,
        *,
        monster_report: dict[str, object],
    ) -> dict[str, object]:
        raw_signals = monster_report.get("signals")
        buy_signals = 0
        if isinstance(raw_signals, list):
            for item in raw_signals:
                if not isinstance(item, dict):
                    continue
                if str(item.get("action", "")).lower() == "buy":
                    buy_signals += 1
        no_buy_streak = self._ctx.account.no_buy_streak
        risk = monster_report.get("risk", {})
        if not isinstance(risk, dict):
            risk = {}
        drawdown_pct = _as_float(risk.get("drawdown_pct"), default=0.0)
        risk_action = str(risk.get("action", ""))
        reasons: list[str] = []
        if drawdown_pct >= self._config.week5.empty_signal_drawdown_pct:
            reasons.append("drawdown_threshold")
        if no_buy_streak >= max(1, self._config.week5.empty_signal_no_buy_runs):
            reasons.append("no_buy_streak")
        if risk_action in {"freeze", "degraded"} and buy_signals == 0:
            reasons.append("risk_gate_without_buy")
        return {
            "triggered": len(reasons) > 0,
            "reasons": reasons,
            "no_buy_streak": no_buy_streak,
            "buy_signals": buy_signals,
            "drawdown_pct": round(drawdown_pct, 4),
            "risk_action": risk_action,
        }

    def _monster_isolation_gate(
        self,
        *,
        monster_report: dict[str, object],
        empty_signal: dict[str, object],
    ) -> dict[str, object]:
        monster_positions = self._ctx.account.monster_positions
        total_monster_position = sum(monster_positions)
        max_monster_position = max(monster_positions) if monster_positions else 0.0
        sentiment_score, sentiment_available = self._backend.estimate_sentiment(
            monster_report=monster_report
        )
        reasons: list[str] = []
        soft_reasons: list[str] = []
        if total_monster_position >= self._config.monster_risk.max_total_position:
            reasons.append("max_total_position")
        if max_monster_position >= self._config.monster_risk.max_stock_position:
            reasons.append("max_stock_position")
        if (
            sentiment_available
            and sentiment_score < self._config.monster_risk.disable_if_sentiment_below
        ):
            if total_monster_position <= 0 and _as_int(
                empty_signal.get("no_buy_streak"), default=0
            ) >= max(1, self._config.week5.empty_signal_no_buy_runs):
                soft_reasons.append("low_sentiment_recovery_soft")
            else:
                reasons.append("low_sentiment")
        empty_signal_reasons = empty_signal.get("reasons", [])
        if not isinstance(empty_signal_reasons, list):
            empty_signal_reasons = []
        if bool(empty_signal.get("triggered", False)):
            if "drawdown_threshold" in {str(reason) for reason in empty_signal_reasons}:
                reasons.append("empty_signal_drawdown")
            else:
                soft_reasons.append("empty_signal_soft")
        return {
            "can_open_new_position": len(reasons) == 0,
            "reasons": reasons,
            "soft_reasons": soft_reasons,
            "total_monster_position": round(total_monster_position, 4),
            "max_monster_position": round(max_monster_position, 4),
            "sentiment_score": round(sentiment_score, 2),
            "sentiment_available": bool(sentiment_available),
        }

    # ------------------------------------------------------------------
    # 副作用收敛点（policy 守卫）
    # ------------------------------------------------------------------
    def _finalize_report_writes(
        self,
        report: dict[str, object],
        *,
        audit_event: str,
        audit_payload: dict[str, object],
        trace_id: str = "",
        has_warning: bool = False,
    ) -> None:
        if self._policy.persist_production_state:
            self._backend.store_report(report)
            self._backend.record_audit(
                event_type=audit_event,
                level="warn" if has_warning else "info",
                trace_id=trace_id,
                payload=audit_payload,
            )
        # historical：生产报告与审计事件都不写（隔离要求），报告只随返回值
        # 交给历史任务自己的落盘链路。

    def _apply_watchlist_sync(
        self,
        *,
        report: dict[str, object],
        symbol_source: str,
        intraday_scheduler_mode: bool,
    ) -> dict[str, object]:
        ctx = self._ctx
        backend = self._backend
        requested_sync = (
            bool(ctx.sync_watchlist)
            if ctx.sync_watchlist is not None
            else (ctx.symbols is None and bool(self._config.week5.auto_sync_watchlist))
        )
        should_sync = requested_sync and not intraday_scheduler_mode
        # 显式恢复运行只做观察，绝不改动关注池。
        if ctx.recovery_mode:
            should_sync = False
        watchlist_sync: dict[str, object] = {
            "enabled": requested_sync,
            "updated": False,
            "reason": (
                "intraday_preserve_existing"
                if requested_sync and intraday_scheduler_mode
                else "disabled"
            ),
            "watchlist_before": len(ctx.account.watchlist),
            "watchlist_after": len(ctx.account.watchlist),
            "symbols": list(ctx.account.watchlist),
        }
        if should_sync:
            # 关注池只能从 final selection 同步；final 为空时保留旧池
            # （keep_if_empty），绝不从 signal pool 补位。
            watchlist_sync = backend.sync_watchlist_from_report(
                report=report,
                reason=ctx.sync_reason or f"week5_scan:{symbol_source}",
                top_k_override=ctx.sync_top_k_override,
            )
        else:
            watchlist_sync["diagnostics"] = backend.watchlist_sync_diagnostics(
                report=report,
                top_k_override=ctx.sync_top_k_override,
            )
        return watchlist_sync


# ----------------------------------------------------------------------
# 模块级工具（不依赖引擎状态）
# ----------------------------------------------------------------------
def _breadth_unavailable_meta() -> dict[str, object]:
    return {
        "enabled": True,
        "block_new_buy": False,
        "reason": "breadth_unavailable",
        "score": None,
        "trend_min_threshold_lift": 0.0,
    }


def _signal_pool_action_counts(
    candidates: list[dict[str, object]],
) -> dict[str, int]:
    """全量 signal pool 的 action 计数（不受 candidates 截断影响）。"""
    counts: dict[str, int] = {}
    for item in candidates:
        action = str(item.get("action", "")).strip().lower() or "unknown"
        counts[action] = counts.get(action, 0) + 1
    return counts


def _detect_synthetic_provider(backend: Week5EngineBackend) -> bool:
    try:
        provider = backend.provider()
        key = ""
        if hasattr(provider, "status") and callable(provider.status):
            status_payload = provider.status()
            key = str(
                status_payload.get("provider_key", status_payload.get("provider_mode", ""))
            ).lower()
        provider_name = type(provider).__name__.lower() if provider is not None else ""
        return (
            "synthetic" in key or "synthetic" in provider_name or "recording" in provider_name
        )
    except Exception:
        return False


def backend_final_pipeline_timing(
    runtime_payload: dict[str, object],
) -> dict[str, object]:
    """final pipeline 聚合子阶段耗时 + 最慢 5 只股票（Phase 1 可观测性）。"""
    timing: dict[str, object] = {}
    stage_ms = runtime_payload.get("pipeline_stage_ms")
    if isinstance(stage_ms, dict):
        for key in (
            "fetch_bars_ms",
            "feature_engine_ms",
            "inference_ms",
            # 子阶段细分：intraday/market-context 已含在 feature_engine_ms
            # 桶内，此处并列展示供耗时下钻。
            "intraday_ms",
            "market_context_ms",
            "cross_review_ms",
            "score_risk_ms",
            "learning_persist_ms",
            "completed_count",
        ):
            timing[key] = _as_int(stage_ms.get(key), default=0)
    parallel_transform = runtime_payload.get("pipeline_parallel_transform")
    if isinstance(parallel_transform, dict):
        timing["parallel_transform"] = dict(parallel_transform)
    raw_symbol_ms = runtime_payload.get("pipeline_symbol_ms")
    symbol_ms: list[dict[str, object]] = []
    if isinstance(raw_symbol_ms, list):
        for item in raw_symbol_ms:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            if symbol:
                symbol_ms.append(
                    {
                        "symbol": symbol,
                        "duration_ms": _as_int(item.get("duration_ms"), default=0),
                    }
                )
    symbol_ms.sort(
        key=lambda item: (
            -_as_int(item.get("duration_ms"), default=0),
            str(item.get("symbol", "")),
        )
    )
    timing["slowest_symbols"] = symbol_ms[:5]
    return timing


def build_historical_data_gate(
    *,
    provider: object | None,
    config: StockAnalyzerConfig,
    snapshot_manifest: object,
    snapshot_current: bool,
    latest_trade_date: str,
    now: datetime,
) -> dict[str, Any]:
    """historical 数据信任门：镜像 live 语义，剔除"当前状态"类输入。

    - provider status 来自 as-of provider（offline 链 + as_of 标注）；
    - staleness 以历史决策时点（as_of 收盘）为基准；
    - week6 当前数据质量分是"当前 watchlist 覆盖"的当前状态信息，
      与历史时点无关，不参与历史门控（payload 显式标注 skipped）。
    """
    status = "ok"
    reasons: list[str] = []
    provider_status: dict[str, object] = {}
    status_fn = getattr(provider, "status", None) if provider is not None else None
    if callable(status_fn):
        try:
            provider_status = status_fn()
        except Exception:
            provider_status = {}
    if isinstance(provider_status, dict):
        if bool(provider_status.get("degraded_mode", False)):
            reasons.append("provider_degraded")
        if bool(provider_status.get("hard_degraded_mode", False)):
            reasons.append("provider_hard_degraded")
        provider_key = str(
            provider_status.get("provider_key", provider_status.get("provider_mode", ""))
        ).lower()
        if provider_key and "synthetic" in provider_key:
            reasons.append("provider_synthetic")
    if str(latest_trade_date).strip():
        try:
            parsed = date.fromisoformat(latest_trade_date[:10])
        except ValueError:
            parsed = None
        if parsed is not None:
            staleness = (now.date() - parsed).days
            max_staleness = max(
                0, _as_int(config.week5.max_data_staleness_days, default=3)
            )
            if staleness > max_staleness:
                reasons.append(f"data_stale:{staleness}d")
    snapshot_enabled = bool(config.week5.feature_snapshot_enabled)
    require_current = bool(config.week5.feature_snapshot_require_current)
    if snapshot_enabled and require_current:
        if snapshot_manifest is None or not snapshot_current:
            reasons.append("feature_snapshot_stale")
    if any(
        reason.startswith(("provider_", "feature_snapshot", "data_quality_blocked"))
        for reason in reasons
    ):
        status = "blocked"
    elif reasons:
        status = "watch_only"
    return {
        "status": status,
        "reasons": reasons,
        "data_quality_score": None,
        "data_quality_score_note": "skipped_for_historical",
        "source_trust_levels": {
            "provider": str(provider_status.get("provider_mode", "")),
            "degraded_mode": bool(provider_status.get("degraded_mode", False)),
            "synthetic_fallback_allowed": bool(config.data_source.synthetic_fallback_allowed),
            "synthetic": bool(
                provider_status
                and "synthetic" in str(
                    provider_status.get("provider_key", provider_status.get("provider_mode", ""))
                ).lower()
            ),
        },
        "financial_trust_level": "missing",
        "as_of": now.date().isoformat(),
    }


def load_feature_snapshot_from_root(
    root: str,
) -> tuple[FeatureSnapshotManifest | None, pd.DataFrame | None]:
    """按显式 root 加载快照（historical 任务目录隔离的读取入口）。"""
    root_path = Path(root).expanduser()
    manifest_path = root_path / "current.json"
    if not manifest_path.exists():
        return None, None
    try:
        manifest = FeatureSnapshotManifest.from_payload(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        return None, None
    if manifest is None:
        return None, None
    frame_path = root_path / manifest.data_snapshot_id / SNAPSHOT_FILENAME
    if not frame_path.exists():
        return None, None
    try:
        frame = pd.read_parquet(frame_path)
    except Exception:
        return None, None
    return manifest, frame


def _empty_prefilter_report(
    config: StockAnalyzerConfig,
    *,
    enabled: bool,
    top_k: int,
) -> dict[str, Any]:
    """prefilter 报告骨架。

    ``enabled``/``top_k`` 必须传入解析 override 之后的值：offhours 等调用方
    通过 ``prefilter_enabled_override``/``prefilter_top_k_override`` 覆盖调度
    行为，报告里的初始字段必须如实反映本轮实际生效的口径（与原实现一致）。
    """
    shortlist_top_n = max(
        1, _as_int(config.week5.universe_prefilter_shortlist_top_n, default=50)
    )
    return {
        "enabled": bool(enabled),
        "applied": False,
        "lookback_days": max(
            120, _as_int(config.week5.universe_prefilter_lookback_days, default=240)
        ),
        "top_k": int(top_k),
        "shortlist_top_n": shortlist_top_n,
        "universe_count": 0,
        "eligible_count": 0,
        "shortlisted_count": 0,
        "scoring_mode": "two_stage_funnel",
        "symbols": [],
        "shortlisted": [],
        "preview": [],
        "pinned_count": 0,
        "pinned_symbols": [],
        "reason": "not_requested",
        "stages": {
            "stage1": {
                "applied": False,
                "status": "not_run",
                "score_key": "baseline_score",
                "input_count": 0,
                "eligible_count": 0,
                "advanced_count": 0,
                "weights": {
                    "trend": 0.40,
                    "capital_flow": 0.25,
                    "price_volume": 0.15,
                    "liquidity": 0.10,
                    "risk_penalty": 0.10,
                },
                "preview": [],
            },
            "stage2": {
                "applied": False,
                "status": "not_run",
                "score_key": "shortlist_score",
                "shortlist_top_n": shortlist_top_n,
                "input_count": 0,
                "advanced_count": 0,
                "weights": {
                    "signal": 0.35,
                    "capital_flow": 0.25,
                    "trend": 0.15,
                    "price_volume": 0.15,
                    "execution_liquidity": 0.10,
                    "risk_penalty": 0.10,
                },
                "preview": [],
            },
        },
    }


__all__ = [
    "Week5AccountState",
    "Week5EngineBackend",
    "Week5ModelInfo",
    "Week5RunContext",
    "Week5RunPolicy",
    "Week5SelectionEngine",
    "backend_final_pipeline_timing",
    "build_historical_data_gate",
    "load_feature_snapshot_from_root",
]
