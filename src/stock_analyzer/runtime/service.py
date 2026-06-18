"""Runtime service wiring pipeline, command channel, scheduler and notifications."""

from __future__ import annotations

import hashlib
import json
import math
import os
import traceback
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from threading import Lock, RLock, Thread, current_thread
from time import perf_counter
from typing import Any, cast

import numpy as np
import pandas as pd

from stock_analyzer.backtest.walk_forward import WalkForwardEngine
from stock_analyzer.command.channel import (
    CommandEnvelope,
    RuntimeState,
    SignedCommandProcessor,
)
from stock_analyzer.config import DataSourceConfig, StockAnalyzerConfig
from stock_analyzer.data.cached_provider import CachedProvider
from stock_analyzer.data.market_depth import MarketDepthProvider
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import MarketDataProvider
from stock_analyzer.data.provider_factory import (
    build_market_depth_provider,
    build_realtime_runtime_provider,
    build_runtime_provider,
)
from stock_analyzer.evolution.champion_shadow_report import ChampionShadowReportBuilder
from stock_analyzer.evolution.core.fusion import ScoreFusionEngine
from stock_analyzer.evolution.governance.compliance import (
    ComplianceState,
)
from stock_analyzer.evolution.llm_semantic import OpenAICompatibleNewsJudge
from stock_analyzer.evolution.modules.m6_counterparty import evaluate_m6_counterparty
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator
from stock_analyzer.evolution.shadow_dataset_builder import ShadowDatasetBuilder
from stock_analyzer.evolution.shadow_online_v2_report import ShadowOnlineV2ReportBuilder
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_relative_frame
from stock_analyzer.infra.cache import CacheStore, InMemoryCache, RedisCache
from stock_analyzer.labels.soup import build_soup_labels
from stock_analyzer.learning.backfill import LearningBackfillEngine
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.market_calendar import is_a_share_trading_day
from stock_analyzer.models.adapters import inspect_model_backend_dependencies
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.registry import (
    ModelLifecycleState,
    ModelRegistry,
    ModelRegistryRecord,
    ModelRole,
)
from stock_analyzer.models.trainer import ModelTrainer
from stock_analyzer.notify.channels import NotificationMessage
from stock_analyzer.notify.filter import NotificationFilter, is_quiet_time
from stock_analyzer.pipeline import AnalyzerPipeline
from stock_analyzer.portfolio.book import PortfolioBook
from stock_analyzer.research import (
    export_qlib_bridge_bundle,
    persist_alphalens_sidecar_report,
    persist_catboost_shadow_report,
    persist_finbert_sidecar_report,
    persist_finrl_sidecar_report,
    persist_heavy_ts_shadow_report,
    persist_shap_sidecar_report,
    persist_tabular_deep_shadow_report,
    persist_tft_sidecar_report,
    run_alphalens_sidecar,
    run_catboost_shadow,
    run_finbert_sidecar,
    run_finrl_sidecar,
    run_heavy_ts_shadow,
    run_qlib_bridge,
    run_shap_sidecar,
    run_tabular_deep_shadow,
    run_tft_sidecar,
)
from stock_analyzer.research.signal_quality_auditor import SignalQualityAuditor
from stock_analyzer.runtime.news_provider_factory import build_news_provider
from stock_analyzer.runtime.notifier_factory import build_notifier
from stock_analyzer.runtime.scheduler import DailyScheduler
from stock_analyzer.runtime.services.acceptance_service import RuntimeAcceptanceService
from stock_analyzer.runtime.services.dashboard_service import RuntimeDashboardService
from stock_analyzer.runtime.services.evolution_core_service import RuntimeEvolutionCoreService
from stock_analyzer.runtime.services.evolution_release_service import RuntimeEvolutionReleaseService
from stock_analyzer.runtime.services.idle_queue_service import RuntimeIdleQueueService
from stock_analyzer.runtime.services.learning_governance_service import (
    RuntimeLearningGovernanceService,
)
from stock_analyzer.runtime.services.market_sync_service import RuntimeMarketSyncService
from stock_analyzer.runtime.services.news_service import RuntimeNewsService
from stock_analyzer.runtime.services.reconcile_service import RuntimeReconcileService
from stock_analyzer.runtime.services.runtime_ops_service import RuntimeOpsService
from stock_analyzer.runtime.services.runtime_state_service import RuntimeStateService
from stock_analyzer.runtime.services.training_service import RuntimeTrainingService
from stock_analyzer.runtime.services.week5_service import RuntimeWeek5Service
from stock_analyzer.runtime.services.week6_service import RuntimeWeek6Service
from stock_analyzer.runtime.services.week7_sim_broker_service import (
    RuntimeWeek7SimBrokerService,
)
from stock_analyzer.stress.scenarios import run_default_stress_suite
from stock_analyzer.types import PipelineSignal, SignalAction
from stock_analyzer.week6.engines import (
    CalendarFactorEngine,
    GlobalMarketFactorEngine,
    MainForceTracker,
    RegulatoryFactorEngine,
    StrategyAllocationEngine,
)

_RECOMMENDATION_STATUSES = {
    "recommended",
    "entry_triggered",
    "bought",
    "holding",
    "sell_alert",
    "sold",
    "closed",
    "watching",
    "dropped",
    "expired",
}
_RECOMMENDATION_ACTIVE_STATUSES = {
    "recommended",
    "entry_triggered",
    "bought",
    "holding",
    "sell_alert",
}
_RECOMMENDATION_TERMINAL_STATUSES = {"sold", "closed", "dropped", "expired"}
_RECOMMENDATION_EXTRA_FIELDS = {
    "recommendation_id",
    "trade_plan",
    "entry_triggered_at",
    "entry_price",
    "entry_quantity",
    "entry_trade_id",
    "exit_alert_at",
    "exit_alert_reason",
    "exit_price",
    "exit_quantity",
    "exit_trade_id",
    "closed_at",
    "closed_reason",
    "last_price",
    "last_exit_action",
    "realized_return_pct",
    "realized_pnl_amount",
    "unrealized_return_pct",
    "current_return_pct",
    "holding_days",
    "outcome_status",
}
_RECOMMENDATION_SIGNAL_EXIT_REASONS = {
    "model_sell_signal",
    "model_signal_invalidated",
    "risk_degraded_exit_review",
}


class StockAnalyzerService:
    """Stateful runtime service used by FastAPI handlers and scheduled tasks."""

    def __init__(self, config: StockAnalyzerConfig) -> None:
        self._config = config
        self._cache = self._build_cache(config)
        self._acceptance_service = RuntimeAcceptanceService(self)
        self._dashboard_service = RuntimeDashboardService(self)
        self._evolution_core_service = RuntimeEvolutionCoreService(self)
        self._evolution_release_service = RuntimeEvolutionReleaseService(self)
        self._idle_queue_service = RuntimeIdleQueueService(self)
        self._learning_governance_service = RuntimeLearningGovernanceService(self)
        self._market_sync_service = RuntimeMarketSyncService(self)
        self._news_service = RuntimeNewsService(self)
        self._reconcile_service = RuntimeReconcileService(self)
        self._runtime_ops_service = RuntimeOpsService(self)
        self._runtime_state_service = RuntimeStateService(self)
        self._training_service = RuntimeTrainingService(self)
        self._week5_service = RuntimeWeek5Service(self)
        self._week6_service = RuntimeWeek6Service(self)
        self._week7_sim_broker_service = RuntimeWeek7SimBrokerService(self)
        runtime_data_source = self._resolve_runtime_data_source_config(config)

        news_provider = build_news_provider(config)

        provider: MarketDataProvider = build_runtime_provider(
            runtime_data_source,
            synthetic_seed=2026,
        )
        if config.cache.enabled:
            provider_cache_ttl = max(1, int(config.cache.ttl_sec))
            provider = CachedProvider(
                inner=provider,
                cache=self._cache,
                ttl_sec=provider_cache_ttl,
                key_prefix="runtime_offline",
            )

        self._provider = provider
        realtime_provider: MarketDataProvider | None = None
        if config.data_source.runtime_live_enabled:
            realtime_provider = build_realtime_runtime_provider(
                runtime_data_source,
                synthetic_seed=3026,
                timezone=str(config.app.timezone).strip() or "Asia/Shanghai",
            )
            if config.cache.enabled:
                realtime_cache_ttl = min(
                    max(1, int(config.cache.ttl_sec)),
                    max(1, int(config.data_source.runtime_live_cache_ttl_sec)),
                )
                realtime_provider = CachedProvider(
                    inner=realtime_provider,
                    cache=self._cache,
                    ttl_sec=realtime_cache_ttl,
                    key_prefix="runtime_realtime",
                )

        self._realtime_provider = realtime_provider
        self._market_depth_provider: MarketDepthProvider | None = None
        if config.market_depth.enabled:
            self._market_depth_provider = build_market_depth_provider(config.market_depth)
        self._pipeline = AnalyzerPipeline(
            config=config,
            provider=provider,
            news_provider=news_provider,
        )
        self._realtime_pipeline = (
            AnalyzerPipeline(
                config=config,
                provider=realtime_provider,
                news_provider=news_provider,
            )
            if realtime_provider is not None
            else None
        )
        self._state = RuntimeState()
        self._portfolio = PortfolioBook(
            max_holdings=config.soup_strategy.max_holdings,
            max_hold_days=config.soup_strategy.max_hold_days,
            max_same_sector=config.soup_strategy.max_same_sector,
        )
        self._broker_snapshot_updated_at: str = ""
        self._broker_snapshot_source: str = ""
        self._broker_positions: dict[str, float] = {}
        self._broker_position_details: dict[str, dict[str, object]] = {}
        self._recommendation_lifecycle: dict[str, dict[str, object]] = {}
        self._recommendation_snapshot_by_id: dict[str, dict[str, object]] = {}
        self._recommendation_latest_id_by_symbol: dict[str, str] = {}
        self._last_signal_payload: list[dict[str, object]] = []
        self._last_signal_trace_id: str = ""
        self._last_signal_timestamp: str = ""
        self._last_signal_source: str = ""
        self._last_signal_storage_source: str = ""
        self._last_signal_snapshot: dict[str, object] | None = None
        self._latest_signal_snapshot_dirty = False
        self._last_notification_filter_diagnostics: dict[str, object] | None = None
        self._signal_quality_auditor = SignalQualityAuditor(config)
        self._signal_quality_audit_history: list[dict[str, object]] = []
        self._last_signal_quality_audit: dict[str, object] | None = None
        self._scheduler_now_context: datetime | None = None
        self._last_reconcile_report: dict[str, object] | None = None
        self._reconcile_history: list[dict[str, object]] = []
        self._run_summaries: list[dict[str, object]] = []
        self._latency_history_ms: list[dict[str, object]] = []
        self._audit_events: list[dict[str, object]] = []
        self._audit_seq = 0
        self._week4_acceptance_history: list[dict[str, object]] = []
        self._last_week4_acceptance_report: dict[str, object] | None = None
        self._week5_scan_history: list[dict[str, object]] = []
        self._last_week5_scan_report: dict[str, object] | None = None
        self._last_week5_market_radar_report: dict[str, object] | None = None
        self._market_radar_review_pool: list[dict[str, object]] = []
        self._week6_history: list[dict[str, object]] = []
        self._last_week6_report: dict[str, object] | None = None
        self._week6_data_quality_history: list[dict[str, object]] = []
        self._last_week6_data_quality_report: dict[str, object] | None = None
        self._tdx_sync_history: list[dict[str, object]] = []
        self._last_tdx_sync_report: dict[str, object] | None = None
        self._market_warehouse_history: list[dict[str, object]] = []
        self._last_market_warehouse_report: dict[str, object] | None = None
        self._last_market_warehouse_progress: dict[str, object] | None = None
        self._week7_sim_broker_history: list[dict[str, object]] = []
        self._last_week7_sim_broker_report: dict[str, object] | None = None
        self._global_market_snapshot: dict[str, float] = {}
        self._global_market_history: list[dict[str, object]] = []
        self._regulatory_watchlist: dict[str, dict[str, object]] = {}
        self._strategy_performance_history: list[dict[str, object]] = []
        self._strategy_kill_switch_state: dict[str, dict[str, object]] = {}
        self._service_started_at = datetime.now()
        self._cloud_backup_last_ping_at: datetime | None = None
        self._cloud_backup_last_ping_source = ""
        self._cloud_backup_alert_active = False
        self._cloud_backup_last_alert_at: datetime | None = None
        self._cloud_backup_last_recovery_at: datetime | None = None
        self._cloud_backup_armed = False
        self._provider_degraded_alert_active = False
        self._risk_circuit_breaker_alert_active = False
        self._risk_capital_protection_level = ""
        self._factor_lifecycle_history: list[dict[str, object]] = []
        self._factor_lifecycle_state: dict[str, dict[str, object]] = {}
        self._factor_graveyard: dict[str, list[dict[str, object]]] = {}
        self._week6_state_file = self._resolve_week6_state_file()
        self._main_force_tracker = MainForceTracker(config=config.week6.main_force)
        self._strategy_allocation_engine = StrategyAllocationEngine(
            profiles=config.week6.allocation_profiles
        )
        self._calendar_factor_engine = CalendarFactorEngine(config=config.holiday_risk)
        self._global_market_factor_engine = GlobalMarketFactorEngine(config=config.global_market)
        self._regulatory_factor_engine = RegulatoryFactorEngine(config=config.regulatory_factor)
        self._notifier = build_notifier(config)
        self._notification_filter = NotificationFilter(config.notification_filter, self._cache)
        self._command_processor = SignedCommandProcessor(
            config=config.command_channel,
            cache=self._cache,
            state=self._state,
        )
        self._scheduler = DailyScheduler(config=config.scheduler)
        self._market_warehouse_scheduler_launch_lock = Lock()
        self._market_warehouse_scheduler_thread: Thread | None = None
        self._audit_lock = Lock()
        self._runtime_state_io_lock = RLock()
        self._evolution_project_root = Path(__file__).resolve().parents[3]
        self._evolution_orchestrator_instance: OffhoursEvolutionOrchestrator | None = None
        self._evolution_history: list[dict[str, object]] = []
        self._last_evolution_report: dict[str, object] | None = None
        self._evolution_release_gate_history: list[dict[str, object]] = []
        self._last_evolution_release_gate: dict[str, object] | None = None
        self._evolution_release_approval_history: list[dict[str, object]] = []
        self._last_evolution_release_approval: dict[str, object] | None = None
        self._evolution_release_ticket_history: list[dict[str, object]] = []
        self._last_evolution_release_ticket: dict[str, object] | None = None
        self._learning_model_proposal_history: list[dict[str, object]] = []
        self._last_learning_model_proposal: dict[str, object] | None = None
        self._learning_model_approval_history: list[dict[str, object]] = []
        self._last_learning_model_approval: dict[str, object] | None = None
        self._learning_model_release_ticket_history: list[dict[str, object]] = []
        self._last_learning_model_release_ticket: dict[str, object] | None = None
        self._execution_risk_training_history: list[dict[str, object]] = []
        self._last_execution_risk_training: dict[str, object] | None = None
        self._execution_aware_report_history: list[dict[str, object]] = []
        self._last_execution_aware_report: dict[str, object] | None = None
        self._idle_history: list[dict[str, object]] = []
        self._last_idle_report: dict[str, object] | None = None
        self._idle_task_run_keys: dict[str, str] = {}
        self._idle_task_status: dict[str, str] = {}
        self._idle_fallback_streak: dict[str, int] = {}
        self._idle_success_streak: dict[str, int] = {}
        self._idle_blocked_tasks: dict[str, str] = {}
        self._idle_blocked_since: dict[str, str] = {}
        self._idle_trade_date_frozen: str = ""
        self._idle_window_key: str = ""
        self._idle_weekend_rotation_scores: dict[str, int] = {}
        self._idle_weekend_defer_runs: dict[str, int] = {}
        self._idle_last_weekend_trade_date: str = ""
        self._idle_manual_ack_grants: dict[str, str] = {}
        self._idle_staging_size_baseline_by_date: dict[str, int] = {}
        self._idle_weekend_size_baseline_by_trade_date: dict[str, int] = {}
        self._idle_pause_flag_until: datetime | None = None
        self._idle_resource_pause_active = False
        self._idle_resource_pause_reason = ""
        self._idle_resource_pause_last_change_at = ""
        self._idle_wd_report_runs = 0
        self._idle_wd_report_deadline_hits = 0
        self._idle_wd_report_completeness_sum = 0.0
        self._idle_history_path = self._resolve_evolution_path(
            self._config.idle_queue.history_persist_path
        )
        self._universe_cache_path = self._resolve_evolution_path(
            self._config.idle_queue.universe_cache_path
        )
        self._tdx_sync_history_path = self._resolve_evolution_path(
            "artifacts/runtime/tdx_sync_history.jsonl"
        )
        self._market_warehouse_history_path = self._resolve_evolution_path(
            "artifacts/runtime/market_warehouse_history.jsonl"
        )
        self._market_warehouse_progress_path = self._resolve_evolution_path(
            "artifacts/runtime/market_warehouse_progress.json"
        )
        self._post_market_warehouse_followup_state_path = self._resolve_evolution_path(
            "artifacts/runtime/post_warehouse_followup_state.json"
        )
        self._post_market_warehouse_followup_result_path = self._resolve_evolution_path(
            "artifacts/runtime/post_warehouse_followup_result.json"
        )
        self._runtime_state_path = self._resolve_evolution_path(
            self._config.command_channel.state_persist_path
        )
        self._runtime_state_loaded_mtime_ns = 0
        self._runtime_history_archive_dir = self._resolve_evolution_path(
            self._config.command_channel.history_archive_dir
        )
        self._last_runtime_history_archive: dict[str, object] | None = None
        self._training_bootstrap_state_path = self._resolve_evolution_path(
            self._config.training.bootstrap_state_path
        )
        self._configure_learning_protocol_runtime()
        self._score_fusion_replay = ScoreFusionEngine(
            default_weights=self._config.evolution.score_fusion_weights,
            enable_bonus_cap=self._config.evolution.score_fusion_enable_bonus_cap,
            bonus_modules=tuple(self._config.evolution.score_fusion_bonus_modules),
            bonus_neutral_score=self._config.evolution.score_fusion_bonus_neutral_score,
            bonus_cap=self._config.evolution.score_fusion_bonus_cap,
            enable_veto=self._config.evolution.score_fusion_enable_veto,
            veto_modules=tuple(self._config.evolution.score_fusion_veto_modules),
            veto_score_threshold=self._config.evolution.score_fusion_veto_score_threshold,
            veto_score_cap=self._config.evolution.score_fusion_veto_score_cap,
            veto_confidence_gate=self._config.evolution.score_fusion_veto_confidence_gate,
        )
        self._training_bootstrap_state = self._load_training_bootstrap_state()
        self._reconcile_training_bootstrap_state_with_artifact()
        self._idle_task_manifests = self._build_idle_task_manifests()
        self._load_runtime_state_from_disk()
        self._load_idle_history_from_disk()
        self._load_tdx_sync_history_from_disk()
        self._load_market_warehouse_history_from_disk()
        self._load_week6_state()
        self._maybe_auto_bootstrap_training_on_first_start()
        self._maybe_seed_watchlist_after_bootstrap()
        self._register_default_jobs()

    @property
    def _evolution_orchestrator(self) -> OffhoursEvolutionOrchestrator:
        orchestrator = self._evolution_orchestrator_instance
        if orchestrator is None:
            orchestrator = OffhoursEvolutionOrchestrator(
                config=self._config.evolution,
                project_root=self._evolution_project_root,
            )
            self._evolution_orchestrator_instance = orchestrator
        return orchestrator

    @_evolution_orchestrator.setter
    def _evolution_orchestrator(self, value: OffhoursEvolutionOrchestrator | None) -> None:
        self._evolution_orchestrator_instance = value

    def _resolve_runtime_data_source_config(
        self,
        config: StockAnalyzerConfig,
    ) -> DataSourceConfig:
        primary = str(config.data_source.primary).strip().lower()
        if primary not in {"market_warehouse", "warehouse", "warehouse_offline"}:
            return config.data_source
        package_root = (
            str(config.market_warehouse.package_root).strip()
            or str(config.data_source.local_data_root).strip()
        )
        warehouse_db_path = (
            str(config.data_source.warehouse_db_path).strip()
            or str(config.market_warehouse.db_path).strip()
        )
        return config.data_source.model_copy(
            update={
                "local_data_root": package_root,
                "warehouse_db_path": warehouse_db_path,
            }
        )

    def _configure_learning_protocol_runtime(self) -> None:
        db_path = self._resolve_learning_protocol_db_path()
        self._learning_protocol_db_path = db_path
        self._feature_schema_registry = FeatureSchemaRegistry(db_path=db_path)
        self._label_policy_registry = LabelPolicyRegistry(db_path=db_path)
        self._sample_store = SampleStore(db_path=db_path)
        self._model_registry = ModelRegistry(db_path=db_path)
        self._pipeline.configure_learning_protocol(
            sample_store=self._sample_store,
            feature_schema_registry=self._feature_schema_registry,
            label_policy_registry=self._label_policy_registry,
            model_registry=self._model_registry,
        )
        if self._realtime_pipeline is not None:
            self._realtime_pipeline.configure_learning_protocol(
                sample_store=self._sample_store,
                feature_schema_registry=self._feature_schema_registry,
                label_policy_registry=self._label_policy_registry,
                model_registry=self._model_registry,
            )

    def _resolve_learning_protocol_db_path(self) -> Path:
        bootstrap_root = self._training_bootstrap_state_path.parent
        return bootstrap_root / "learning_protocol.duckdb"

    def _resolve_learning_manifest_artifact_path(
        self,
        *,
        dataset_manifest_id: str,
        artifact_path: str | None,
    ) -> Path:
        normalized_path = str(artifact_path or "").strip()
        if normalized_path:
            candidate = Path(normalized_path).expanduser()
            if candidate.is_absolute():
                return candidate
            return self._resolve_evolution_path(str(candidate))

        configured_artifact = Path(str(self._config.training.artifact_path).strip() or "model.json")
        artifact_suffix = configured_artifact.suffix or ".json"
        safe_manifest_fragment = "".join(
            char if char.isalnum() else "_"
            for char in str(dataset_manifest_id).strip().lower()
        ).strip("_")
        if not safe_manifest_fragment:
            safe_manifest_fragment = "latest"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        bootstrap_root = self._training_bootstrap_state_path.parent
        return (
            bootstrap_root
            / "learning_manifest_artifacts"
            / f"learning_manifest_{safe_manifest_fragment}_{timestamp}{artifact_suffix}"
        )

    def _build_model_trainer(self) -> ModelTrainer:
        return ModelTrainer(
            training=self._config.training,
            labels=self._config.labels,
            models=self._config.models,
            settlement_lag_days=self._config.evolution.execution_spec.settlement_lag,
            provider=self._provider,
            market_relative_feature=self._config.market_relative_feature,
        )

    def _build_learning_backfill_engine(self) -> LearningBackfillEngine:
        return LearningBackfillEngine(
            config=self._config,
            provider=self._provider,
            sample_store=self._sample_store,
            feature_schema_registry=self._feature_schema_registry,
            label_policy_registry=self._label_policy_registry,
        )

    def _build_shadow_dataset_builder(self) -> ShadowDatasetBuilder:
        return ShadowDatasetBuilder(
            store=self._sample_store,
            model_registry=self._model_registry,
            feature_schema_registry=self._feature_schema_registry,
            label_policy_registry=self._label_policy_registry,
        )

    def _build_champion_shadow_report_builder(self) -> ChampionShadowReportBuilder:
        return ChampionShadowReportBuilder(
            store=self._sample_store,
            model_registry=self._model_registry,
            feature_schema_registry=self._feature_schema_registry,
            label_policy_registry=self._label_policy_registry,
        )

    def _build_shadow_online_v2_report_builder(self) -> ShadowOnlineV2ReportBuilder:
        return ShadowOnlineV2ReportBuilder(
            store=self._sample_store,
            model_registry=self._model_registry,
            feature_schema_registry=self._feature_schema_registry,
            label_policy_registry=self._label_policy_registry,
        )

    def _normalize_model_role(self, role: str | ModelRole) -> ModelRole:
        if isinstance(role, ModelRole):
            return role
        return ModelRole(str(role).strip().lower() or ModelRole.CHALLENGER.value)

    def _normalize_model_lifecycle_state(
        self,
        lifecycle_state: str | ModelLifecycleState,
    ) -> ModelLifecycleState:
        if isinstance(lifecycle_state, ModelLifecycleState):
            return lifecycle_state
        return ModelLifecycleState(
            str(lifecycle_state).strip().lower() or ModelLifecycleState.REGISTERED.value
        )

    def _register_model_artifact_if_supported(
        self,
        *,
        artifact_path: str,
        role: ModelRole = ModelRole.CHALLENGER,
        lifecycle_state: ModelLifecycleState = ModelLifecycleState.TRAINED,
        source: str = "",
        parent_model_id: str = "",
    ) -> dict[str, object]:
        normalized_path = str(artifact_path).strip()
        if not normalized_path:
            return {"registered": False, "reason": "artifact_path_missing"}

        try:
            resolved_path = str(Path(normalized_path).expanduser().resolve())
            artifact = ModelArtifact.load(resolved_path)
        except Exception as exc:
            return {
                "registered": False,
                "reason": f"artifact_load_failed:{exc.__class__.__name__}",
            }

        missing_protocol_fields = [
            field_name
            for field_name in (
                "dataset_manifest_id",
                "feature_schema_id",
                "feature_schema_hash",
                "label_policy_id",
                "label_policy_hash",
            )
            if not str(getattr(artifact, field_name)).strip()
        ]
        if missing_protocol_fields:
            return {
                "registered": False,
                "reason": "artifact_protocol_binding_missing",
                "missing_fields": missing_protocol_fields,
            }

        try:
            resolved_parent = str(parent_model_id).strip()
            if not resolved_parent and role != ModelRole.CHAMPION:
                resolved_parent = str(self._config.evolution.active_champion_id).strip()
            record = self._model_registry.register_artifact(
                artifact=artifact,
                artifact_uri=resolved_path,
                role=role,
                lifecycle_state=lifecycle_state,
                parent_model_id=resolved_parent,
            )
        except Exception as exc:
            return {
                "registered": False,
                "reason": f"model_registry_exception:{exc}",
            }

        payload = {
            "registered": True,
            "model_id": record.model_id,
            "role": record.role.value,
            "lifecycle_state": record.lifecycle_state.value,
            "artifact_uri": record.artifact_uri,
            "dataset_manifest_id": record.dataset_manifest_id,
            "feature_schema_id": record.feature_schema_id,
            "label_policy_id": record.label_policy_id,
            "source": source.strip(),
        }
        self._record_audit_event(
            event_type="model_registry_registered",
            level="info",
            message="model artifact registered",
            payload=payload,
        )
        return payload

    def model_registry_entries(
        self,
        *,
        limit: int = 20,
        role: str = "",
        lifecycle_state: str = "",
    ) -> dict[str, object]:
        normalized_role = self._normalize_model_role(role) if str(role).strip() else None
        normalized_state = (
            self._normalize_model_lifecycle_state(lifecycle_state)
            if str(lifecycle_state).strip()
            else None
        )
        records = self._model_registry.list_records(
            role=normalized_role,
            lifecycle_state=normalized_state,
            limit=max(1, int(limit)),
        )
        return {
            "records": len(records),
            "items": [record.model_dump(mode="json") for record in records],
            "active_champion": (
                self._model_registry.active_champion().model_dump(mode="json")
                if self._model_registry.active_champion() is not None
                else None
            ),
        }

    def model_registry_entry(self, model_id: str) -> dict[str, object] | None:
        record = self._model_registry.get_by_id(str(model_id).strip())
        return None if record is None else record.model_dump(mode="json")

    def register_model_artifact(
        self,
        *,
        artifact_path: str,
        role: str = "challenger",
        lifecycle_state: str = "trained",
        source: str = "manual_register_model_artifact",
        parent_model_id: str = "",
    ) -> dict[str, object]:
        return self._register_model_artifact_if_supported(
            artifact_path=artifact_path,
            role=self._normalize_model_role(role),
            lifecycle_state=self._normalize_model_lifecycle_state(lifecycle_state),
            source=source,
            parent_model_id=parent_model_id,
        )

    def bootstrap_active_champion_from_artifact(
        self,
        *,
        artifact_path: str = "",
        source: str = "manual_bootstrap_active_champion",
        allow_legacy_production_artifact: bool = False,
        model_id: str = "",
    ) -> dict[str, object]:
        active = self._model_registry.active_champion()
        if active is not None:
            return {
                "accepted": False,
                "reason": "active_champion_exists",
                "active_champion": active.model_dump(mode="json"),
            }
        normalized_path = str(artifact_path).strip() or str(self._config.training.artifact_path)
        resolved_path = str(self._resolve_evolution_path(normalized_path))
        existing = self._find_model_registry_record_by_artifact_uri(resolved_path)
        if existing is not None:
            try:
                record = self._approve_existing_registry_record_for_champion_bootstrap(existing)
            except Exception as exc:
                if allow_legacy_production_artifact:
                    return self._bootstrap_legacy_production_active_champion(
                        artifact_path=resolved_path,
                        source=source,
                        model_id=model_id,
                        previous_failure=f"existing_registry_record_not_bootstrappable:{exc}",
                    )
                return {
                    "accepted": False,
                    "reason": f"existing_registry_record_not_bootstrappable:{exc}",
                    "artifact_path": resolved_path,
                    "model_id": existing.model_id,
                    "role": existing.role.value,
                    "lifecycle_state": existing.lifecycle_state.value,
                }
            if record.role != ModelRole.CHAMPION:
                record = self._model_registry.update_role(
                    model_id=record.model_id,
                    role=ModelRole.CHAMPION,
                    now=datetime.now(UTC),
                )
            self._config.evolution.active_champion_id = record.model_id
            payload = {
                "accepted": True,
                "reason": "existing_registry_record_promoted",
                "model_id": record.model_id,
                "artifact_uri": record.artifact_uri,
                "role": record.role.value,
                "lifecycle_state": record.lifecycle_state.value,
                "source": source.strip(),
            }
            self._record_audit_event(
                event_type="model_registry_active_champion_bootstrap",
                level="warn",
                message="active champion bootstrapped from existing registry record",
                payload=payload,
            )
            return payload

        registered = self._register_model_artifact_if_supported(
            artifact_path=resolved_path,
            role=ModelRole.CHAMPION,
            lifecycle_state=ModelLifecycleState.APPROVED,
            source=source,
            parent_model_id="",
        )
        if not bool(registered.get("registered", False)):
            if allow_legacy_production_artifact:
                return self._bootstrap_legacy_production_active_champion(
                    artifact_path=resolved_path,
                    source=source,
                    model_id=model_id,
                    previous_failure="register_artifact_failed",
                    registration=registered,
                )
            return {
                "accepted": False,
                "reason": "register_artifact_failed",
                "artifact_path": resolved_path,
                "registration": registered,
            }
        payload = {
            "accepted": True,
            "reason": "artifact_registered_as_active_champion",
            "model_id": str(registered.get("model_id", "")),
            "artifact_uri": str(registered.get("artifact_uri", resolved_path)),
            "role": str(registered.get("role", "")),
            "lifecycle_state": str(registered.get("lifecycle_state", "")),
            "source": source.strip(),
            "registration": registered,
        }
        self._config.evolution.active_champion_id = str(registered.get("model_id", ""))
        self._record_audit_event(
            event_type="model_registry_active_champion_bootstrap",
            level="warn",
            message="active champion bootstrapped from artifact",
            payload=payload,
        )
        return payload

    def _bootstrap_legacy_production_active_champion(
        self,
        *,
        artifact_path: str,
        source: str,
        model_id: str = "",
        previous_failure: str = "",
        registration: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        resolved_path = str(Path(artifact_path).expanduser().resolve())
        try:
            artifact = ModelArtifact.load(resolved_path)
        except Exception as exc:
            return {
                "accepted": False,
                "reason": f"legacy_artifact_load_failed:{exc.__class__.__name__}",
                "artifact_path": resolved_path,
                "previous_failure": previous_failure,
            }

        existing_active = self._model_registry.active_champion(suppress_read_errors=True)
        if existing_active is not None:
            return {
                "accepted": False,
                "reason": "active_champion_exists",
                "active_champion": existing_active.model_dump(mode="json"),
            }

        record = self._build_legacy_production_champion_record(
            artifact=artifact,
            artifact_uri=resolved_path,
            model_id=model_id,
        )
        try:
            record = self._model_registry.upsert_repair_record(record)
        except Exception as exc:
            return {
                "accepted": False,
                "reason": f"legacy_champion_repair_failed:{exc}",
                "artifact_path": resolved_path,
                "model_id": record.model_id,
                "previous_failure": previous_failure,
                "registration": dict(registration or {}),
            }

        self._config.evolution.active_champion_id = record.model_id
        payload = {
            "accepted": True,
            "reason": "legacy_artifact_registered_as_active_champion",
            "model_id": record.model_id,
            "artifact_uri": record.artifact_uri,
            "role": record.role.value,
            "lifecycle_state": record.lifecycle_state.value,
            "source": source.strip(),
            "legacy_production_artifact": True,
            "dataset_manifest_id": record.dataset_manifest_id,
            "feature_schema_id": record.feature_schema_id,
            "feature_schema_hash": record.feature_schema_hash,
            "label_policy_id": record.label_policy_id,
            "label_policy_hash": record.label_policy_hash,
            "previous_failure": previous_failure,
            "registration": dict(registration or {}),
        }
        self._record_audit_event(
            event_type="model_registry_active_champion_bootstrap",
            level="warn",
            message="active champion bootstrapped from legacy production artifact",
            payload=payload,
        )
        return payload

    def _build_legacy_production_champion_record(
        self,
        *,
        artifact: ModelArtifact,
        artifact_uri: str,
        model_id: str,
    ) -> ModelRegistryRecord:
        now = datetime.now(UTC)
        resolved_model_id = str(model_id).strip() or _legacy_production_model_id(artifact_uri)
        feature_schema_id = (
            artifact.feature_schema_id.strip()
            or f"legacy_production_feature_schema_{_digest_text(artifact_uri, length=12)}"
        )
        feature_schema_hash = artifact.feature_schema_hash.strip() or _legacy_feature_schema_hash(
            artifact=artifact
        )
        label_policy_id = (
            artifact.label_policy_id.strip()
            or f"legacy_production_label_policy_{_digest_text(artifact_uri, length=12)}"
        )
        label_policy_hash = artifact.label_policy_hash.strip() or _legacy_label_policy_hash(
            artifact_uri=artifact_uri
        )
        dataset_manifest_id = (
            artifact.dataset_manifest_id.strip()
            or f"legacy_production_dataset_manifest_{_digest_text(artifact_uri, length=12)}"
        )
        return ModelRegistryRecord(
            model_id=resolved_model_id,
            role=ModelRole.CHAMPION,
            lifecycle_state=ModelLifecycleState.APPROVED,
            artifact_uri=artifact_uri,
            artifact_created_at=_parse_iso_datetime(artifact.created_at),
            dataset_manifest_id=dataset_manifest_id,
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
            label_policy_id=label_policy_id,
            label_policy_hash=label_policy_hash,
            metrics_summary=dict(artifact.training_metrics),
            created_at=now,
            updated_at=now,
            promoted_at=now,
        )

    def _find_model_registry_record_by_artifact_uri(self, artifact_uri: str):
        normalized = str(Path(artifact_uri).expanduser().resolve())
        for record in self._model_registry.list_records(limit=500, suppress_read_errors=True):
            candidate = str(record.artifact_uri).strip()
            try:
                candidate = str(Path(candidate).expanduser().resolve())
            except Exception:
                candidate = str(record.artifact_uri).strip()
            if candidate == normalized:
                return record
        return None

    def _approve_existing_registry_record_for_champion_bootstrap(self, record):
        current = record
        if current.lifecycle_state == ModelLifecycleState.REVOKED:
            raise ValueError("revoked_model_cannot_be_bootstrapped")
        if current.lifecycle_state == ModelLifecycleState.BLOCKED:
            raise ValueError("blocked_model_cannot_be_bootstrapped")
        if current.lifecycle_state == ModelLifecycleState.REGISTERED:
            current = self._model_registry.update_lifecycle(
                model_id=current.model_id,
                lifecycle_state=ModelLifecycleState.TRAINED,
                now=datetime.now(UTC),
            )
        if current.lifecycle_state == ModelLifecycleState.TRAINED:
            current = self._model_registry.update_lifecycle(
                model_id=current.model_id,
                lifecycle_state=ModelLifecycleState.SHADOW_VALIDATED,
                now=datetime.now(UTC),
            )
        if current.lifecycle_state == ModelLifecycleState.SHADOW_VALIDATED:
            current = self._model_registry.update_lifecycle(
                model_id=current.model_id,
                lifecycle_state=ModelLifecycleState.APPROVED,
                now=datetime.now(UTC),
            )
        if current.lifecycle_state != ModelLifecycleState.APPROVED:
            raise ValueError(f"unsupported_lifecycle:{current.lifecycle_state.value}")
        return current

    def update_model_registry_lifecycle(
        self,
        *,
        model_id: str,
        lifecycle_state: str,
        blocked_reason: str = "",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        record = self._model_registry.update_lifecycle(
            model_id=str(model_id).strip(),
            lifecycle_state=self._normalize_model_lifecycle_state(lifecycle_state),
            blocked_reason=blocked_reason,
            now=timestamp,
        )
        payload = record.model_dump(mode="json")
        self._record_audit_event(
            event_type="model_registry_lifecycle_updated",
            level="info",
            message="model lifecycle updated",
            payload=payload,
        )
        return payload

    def update_model_registry_role(
        self,
        *,
        model_id: str,
        role: str,
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        record = self._model_registry.update_role(
            model_id=str(model_id).strip(),
            role=self._normalize_model_role(role),
            now=timestamp,
        )
        payload = record.model_dump(mode="json")
        self._record_audit_event(
            event_type="model_registry_role_updated",
            level="info",
            message="model role updated",
            payload=payload,
        )
        return payload

    def build_shadow_dataset(
        self,
        *,
        model_id: str,
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        dataset = self._build_shadow_dataset_builder().build_for_model(
            model_id=str(model_id).strip(),
            split_names=split_names,
            max_rows=max_rows,
        )
        payload = dataset.to_dict(
            include_rows=include_rows,
            preview_limit=max(1, int(preview_limit)),
        )
        self._record_audit_event(
            event_type="shadow_dataset_built",
            level="info",
            message="shadow dataset built",
            payload={
                "shadow_dataset_id": payload["shadow_dataset_id"],
                "model_id": payload["model_id"],
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "row_count": payload["row_count"],
                "split_counts": payload["split_counts"],
                "requested_split_names": payload["requested_split_names"],
            },
        )
        return payload

    def build_champion_shadow_report(
        self,
        *,
        model_id: str,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        signal_threshold: float = 0.5,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        report = self._build_champion_shadow_report_builder().build_report(
            shadow_model_id=str(model_id).strip(),
            champion_model_id=str(champion_model_id).strip(),
            split_names=split_names,
            max_rows=max_rows,
            signal_threshold=signal_threshold,
        )
        payload = report.to_dict(
            include_rows=include_rows,
            preview_limit=max(1, int(preview_limit)),
        )
        self._record_audit_event(
            event_type="champion_shadow_report_built",
            level="info",
            message="champion shadow comparison report built",
            payload={
                "comparison_report_id": payload["comparison_report_id"],
                "champion_model_id": payload["champion_model_id"],
                "shadow_model_id": payload["shadow_model_id"],
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "row_count": payload["row_count"],
                "split_counts": payload["split_counts"],
            },
        )
        return payload

    def build_shadow_online_v2_report(
        self,
        *,
        model_id: str,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        report = self._build_shadow_online_v2_report_builder().build_report(
            shadow_model_id=str(model_id).strip(),
            champion_model_id=str(champion_model_id).strip(),
            split_names=split_names,
            max_rows=max_rows,
            max_samples=max_samples,
            min_samples=min_samples,
            learning_rate=learning_rate,
            signal_threshold=signal_threshold,
            preview_limit=preview_limit,
        )
        payload = report.to_dict(
            include_rows=include_rows,
            preview_limit=max(1, int(preview_limit)),
        )
        self._record_audit_event(
            event_type="shadow_online_v2_report_built",
            level="info",
            message="shadow online v2 report built",
            payload={
                "report_id": payload["report_id"],
                "champion_model_id": payload["champion_model_id"],
                "shadow_model_id": payload["shadow_model_id"],
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "row_count": payload["row_count"],
                "status": payload["status"],
            },
        )
        return payload

    def build_phase_d_alphalens_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        factor_columns: Sequence[str] | None = None,
        horizons: Sequence[int] = (1, 5, 10),
        quantiles: int = 5,
        output_path: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, records, _ = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        report = run_alphalens_sidecar(
            records=records,
            factor_columns=factor_columns,
            horizons=horizons,
            quantiles=quantiles,
        )
        payload = {
            "research_id": "alphalens_sidecar",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("alphalens_sidecar"))
        payload["output_path"] = persist_alphalens_sidecar_report(report=payload, output_path=target)
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_shap_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        prediction_column: str = "p_meta",
        baseline_importance: Mapping[str, object] | None = None,
        drift_threshold: float = 0.25,
        top_k: int = 5,
        output_path: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, _, frame = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        report = run_shap_sidecar(
            reference_frame=frame,
            prediction_column=prediction_column,
            baseline_importance=baseline_importance,
            drift_threshold=drift_threshold,
            top_k=top_k,
        )
        payload = {
            "research_id": "shap_sidecar",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("shap_sidecar"))
        payload["output_path"] = persist_shap_sidecar_report(report=payload, output_path=target)
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_catboost_shadow_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        feature_columns: Sequence[str] | None = None,
        label_column: str = "label",
        baseline_probability_column: str = "p_meta",
        test_ratio: float = 0.3,
        random_seed: int = 2026,
        output_path: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, _, frame = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        report = run_catboost_shadow(
            reference_frame=frame,
            feature_columns=feature_columns,
            label_column=label_column,
            baseline_probability_column=baseline_probability_column,
            test_ratio=test_ratio,
            random_seed=random_seed,
        )
        payload = {
            "research_id": "catboost_shadow",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("catboost_shadow"))
        payload["output_path"] = persist_catboost_shadow_report(report=payload, output_path=target)
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_finbert_report(
        self,
        *,
        records: Sequence[Mapping[str, object]],
        model_path: str | Path = "",
        include_neutral: bool = True,
        output_path: str | None = None,
    ) -> dict[str, object]:
        report = run_finbert_sidecar(
            records=records,
            model_path=model_path,
            include_neutral=include_neutral,
        )
        payload = {
            "research_id": "finbert_sidecar",
            "delivery_mode": "research_sidecar",
            "record_source": "manual_records",
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("finbert_sidecar"))
        payload["output_path"] = persist_finbert_sidecar_report(report=payload, output_path=target)
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_qlib_bridge_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        feature_columns: Sequence[str] | None = None,
        label_column: str = "label",
        train_ratio: float = 0.6,
        valid_ratio: float = 0.2,
        output_dir: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, records, _ = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        report = run_qlib_bridge(
            records=records,
            feature_columns=feature_columns,
            label_column=label_column,
            train_ratio=train_ratio,
            valid_ratio=valid_ratio,
        )
        bundle_dir = Path(output_dir or self._default_phase_d_bundle_dir("qlib_bridge"))
        bundle_paths = export_qlib_bridge_bundle(
            records=records,
            output_dir=bundle_dir,
            feature_columns=feature_columns,
            label_column=label_column,
        )
        payload = {
            "research_id": "qlib_bridge",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
            "bundle_paths": bundle_paths,
            "output_path": str(bundle_paths.get("manifest_path", "")),
        }
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_tabular_deep_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        feature_columns: Sequence[str] | None = None,
        label_column: str = "label",
        baseline_probability_column: str = "p_meta",
        test_ratio: float = 0.3,
        random_seed: int = 2026,
        output_path: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, _, frame = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        report = run_tabular_deep_shadow(
            reference_frame=frame,
            feature_columns=feature_columns,
            label_column=label_column,
            baseline_probability_column=baseline_probability_column,
            test_ratio=test_ratio,
            random_seed=random_seed,
        )
        payload = {
            "research_id": "tabnet_ft_transformer",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("tabular_deep_shadow"))
        payload["output_path"] = persist_tabular_deep_shadow_report(
            report=payload,
            output_path=target,
        )
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_tft_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        horizon: int = 1,
        encoder_length: int = 5,
        train_ratio: float = 0.7,
        output_path: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, records, _ = self._build_phase_d_contextual_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
            min_rows_per_symbol=max(encoder_length + max(1, horizon) + 2, 6),
        )
        report = run_tft_sidecar(
            records=records,
            horizon=horizon,
            encoder_length=encoder_length,
            train_ratio=train_ratio,
        )
        payload = {
            "research_id": "tft_sidecar",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("tft_sidecar"))
        payload["output_path"] = persist_tft_sidecar_report(report=payload, output_path=target)
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_finrl_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        feature_columns: Sequence[str] | None = None,
        reward_column: str = "realized_return",
        baseline_probability_column: str = "p_meta",
        test_ratio: float = 0.3,
        random_seed: int = 2026,
        action_threshold: float = 0.55,
        output_path: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, _, frame = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        report = run_finrl_sidecar(
            reference_frame=frame,
            feature_columns=feature_columns,
            reward_column=reward_column,
            baseline_probability_column=baseline_probability_column,
            test_ratio=test_ratio,
            random_seed=random_seed,
            action_threshold=action_threshold,
        )
        payload = {
            "research_id": "finrl_sidecar",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("finrl_sidecar"))
        payload["output_path"] = persist_finrl_sidecar_report(report=payload, output_path=target)
        self._record_phase_d_research_event(payload=payload)
        return payload

    def build_phase_d_heavy_ts_report(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        horizon: int = 3,
        lookback: int = 8,
        test_ratio: float = 0.3,
        random_seed: int = 2026,
        output_path: str | None = None,
    ) -> dict[str, object]:
        dataset_meta, records, _ = self._build_phase_d_contextual_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
            min_rows_per_symbol=max(lookback + max(1, horizon) + 2, 8),
        )
        report = run_heavy_ts_shadow(
            records=records,
            horizon=horizon,
            lookback=lookback,
            test_ratio=test_ratio,
            random_seed=random_seed,
        )
        payload = {
            "research_id": "heavy_ts_shadow",
            "delivery_mode": "research_sidecar",
            **dataset_meta,
            **report,
        }
        target = Path(output_path or self._default_phase_d_report_path("heavy_ts_shadow"))
        payload["output_path"] = persist_heavy_ts_shadow_report(report=payload, output_path=target)
        self._record_phase_d_research_event(payload=payload)
        return payload

    def train_execution_risk_model(
        self,
        *,
        artifact_path: str | None = None,
        maturity_statuses: Sequence[str] | None = None,
        max_rows: int | None = None,
        min_samples_per_target: int = 24,
        calibration_ratio: float = 0.2,
        test_ratio: float = 0.2,
        epochs: int = 240,
        learning_rate: float = 0.05,
        l2: float = 1e-3,
        seed: int = 42,
        now: datetime | None = None,
    ) -> dict[str, object]:
        return self._training_service.train_execution_risk_model(
            artifact_path=artifact_path,
            maturity_statuses=list(maturity_statuses) if maturity_statuses is not None else None,
            max_rows=max_rows,
            min_samples_per_target=min_samples_per_target,
            calibration_ratio=calibration_ratio,
            test_ratio=test_ratio,
            epochs=epochs,
            learning_rate=learning_rate,
            l2=l2,
            seed=seed,
            now=now,
        )

    def latest_execution_risk_training(self) -> dict[str, object] | None:
        return self._training_service.latest_execution_risk_training()

    def execution_risk_training_history(
        self,
        limit: int = 20,
        *,
        include_artifact_scan: bool = True,
    ) -> dict[str, object]:
        return self._training_service.execution_risk_training_history(
            limit,
            include_artifact_scan=include_artifact_scan,
        )

    def execution_risk_status(
        self,
        *,
        include_artifact_scan: bool = True,
    ) -> dict[str, object]:
        return self._training_service.execution_risk_status(
            include_artifact_scan=include_artifact_scan
        )

    def build_execution_aware_report(
        self,
        *,
        model_id: str,
        execution_risk_artifact_path: str = "",
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        return self._training_service.build_execution_aware_report(
            model_id=model_id,
            execution_risk_artifact_path=execution_risk_artifact_path,
            champion_model_id=champion_model_id,
            split_names=list(split_names) if split_names is not None else None,
            max_rows=max_rows,
            include_rows=include_rows,
            preview_limit=preview_limit,
        )

    def latest_execution_aware_report(self) -> dict[str, object] | None:
        return self._training_service.latest_execution_aware_report()

    def execution_aware_report_history(self, limit: int = 20) -> dict[str, object]:
        return self._training_service.execution_aware_report_history(limit)

    def _record_learning_backfill_event(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        raw_errors = payload.get("errors")
        has_errors = isinstance(raw_errors, list) and any(str(item).strip() for item in raw_errors)
        self._record_audit_event(
            event_type=event_type,
            level="warn" if has_errors or not bool(payload.get("ok", False)) else "info",
            message=f"{event_type} completed",
            payload=payload,
        )

    def bootstrap_learning_backfill(
        self,
        *,
        symbols: Sequence[str],
        strategy: str = "trend",
        lookback_days: int = 240,
        end_date: date | None = None,
        min_history_rows: int | None = None,
        source: str = "service_bootstrap_backfill",
    ) -> dict[str, object]:
        payload = self._build_learning_backfill_engine().bootstrap_backfill(
            symbols=symbols,
            strategy=strategy,
            lookback_days=lookback_days,
            end_date=end_date,
            min_history_rows=min_history_rows,
            source=source,
        )
        self._record_learning_backfill_event(
            event_type="learning_backfill_bootstrap",
            payload=payload,
        )
        return payload

    def incremental_learning_backfill(
        self,
        *,
        symbols: Sequence[str] | None = None,
        as_of: datetime | None = None,
        source: str = "service_incremental_backfill",
    ) -> dict[str, object]:
        payload = self._build_learning_backfill_engine().incremental_backfill(
            symbols=symbols,
            as_of=as_of,
            source=source,
        )
        self._record_learning_backfill_event(
            event_type="learning_backfill_incremental",
            payload=payload,
        )
        return payload

    def repair_learning_backfill(
        self,
        *,
        snapshot_ids: Sequence[str],
        as_of: datetime | None = None,
        source: str = "service_repair_backfill",
    ) -> dict[str, object]:
        payload = self._build_learning_backfill_engine().repair_backfill(
            snapshot_ids=snapshot_ids,
            as_of=as_of,
            source=source,
        )
        self._record_learning_backfill_event(
            event_type="learning_backfill_repair",
            payload=payload,
        )
        return payload

    def build_learning_trainable_manifest(
        self,
        *,
        symbols: Sequence[str] | None = None,
        feature_schema_id: str = "",
        feature_schema_hash: str = "",
        label_policy_id: str = "",
        label_policy_hash: str = "",
        time_window_start: datetime | None = None,
        time_window_end: datetime | None = None,
        fidelity_filter: Sequence[BackfillFidelityTier] | None = None,
        maturity_statuses: Sequence[MaturityStatus] | None = None,
        calibration_ratio: float | None = None,
        test_ratio: float | None = None,
        sample_selection_rule: str = "",
    ) -> dict[str, object]:
        payload = self._build_learning_backfill_engine().build_trainable_manifest(
            symbols=symbols,
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
            label_policy_id=label_policy_id,
            label_policy_hash=label_policy_hash,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
            fidelity_filter=fidelity_filter,
            maturity_statuses=maturity_statuses,
            calibration_ratio=calibration_ratio,
            test_ratio=test_ratio,
            sample_selection_rule=sample_selection_rule,
        )
        self._record_learning_backfill_event(
            event_type="learning_backfill_manifest",
            payload=payload,
        )
        return payload

    def backfill_learning_runtime_history_archive(
        self,
        *,
        archive_path: str,
        source: str = "runtime_history_archive",
    ) -> dict[str, object]:
        payload = self._build_learning_backfill_engine().backfill_from_runtime_history_archive(
            archive_path=archive_path,
            source=source,
        )
        self._record_learning_backfill_event(
            event_type="learning_backfill_runtime_history",
            payload=payload,
        )
        return payload

    def backfill_learning_runtime_history_archives(
        self,
        *,
        archive_dir: str = "",
        source: str = "runtime_history_archive_batch",
    ) -> dict[str, object]:
        effective_archive_dir = (
            archive_dir.strip()
            or str(self._config.command_channel.history_archive_dir).strip()
        )
        payload = self._build_learning_backfill_engine().backfill_from_runtime_history_archives(
            archive_dir=effective_archive_dir,
            source=source,
        )
        self._record_learning_backfill_event(
            event_type="learning_backfill_runtime_history_batch",
            payload=payload,
        )
        return payload

    def bootstrap_learning_from_runtime_history(
        self,
        *,
        archive_dir: str = "",
        symbols: Sequence[str] | None = None,
        build_manifest: bool = True,
        calibration_ratio: float | None = None,
        test_ratio: float | None = None,
    ) -> dict[str, object]:
        effective_archive_dir = (
            archive_dir.strip()
            or str(self._config.command_channel.history_archive_dir).strip()
        )
        backfill = self.backfill_learning_runtime_history_archives(
            archive_dir=effective_archive_dir,
            source="runtime_history_cold_start_batch",
        )
        manifest: dict[str, object]
        if not build_manifest:
            manifest = {
                "ok": True,
                "skipped": True,
                "reason": "build_manifest_disabled",
                "dataset_manifest_id": "",
                "included_snapshot_count": 0,
                "errors": [],
            }
        elif not bool(backfill.get("ok", False)):
            manifest = {
                "ok": False,
                "skipped": True,
                "reason": "backfill_failed",
                "dataset_manifest_id": "",
                "included_snapshot_count": 0,
                "errors": ["backfill_failed"],
            }
        else:
            manifest = self.build_learning_trainable_manifest(
                symbols=symbols,
                calibration_ratio=calibration_ratio,
                test_ratio=test_ratio,
            )
            manifest["skipped"] = False

        payload = {
            "ok": bool(backfill.get("ok", False)) and bool(manifest.get("ok", False)),
            "mode": "learning_runtime_history_cold_start",
            "archive_dir": effective_archive_dir,
            "symbols": [str(item).strip() for item in (symbols or []) if str(item).strip()],
            "build_manifest": bool(build_manifest),
            "processed_archives": int(backfill.get("processed_archives", 0)),
            "dataset_manifest_id": str(manifest.get("dataset_manifest_id", "")),
            "included_snapshot_count": int(manifest.get("included_snapshot_count", 0)),
            "backfill": backfill,
            "manifest": manifest,
            "errors": [
                *[
                    str(item).strip()
                    for item in backfill.get("errors", [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in manifest.get("errors", [])
                    if str(item).strip()
                ],
            ],
        }
        self._record_learning_backfill_event(
            event_type="learning_backfill_runtime_history_cold_start",
            payload=payload,
        )
        return payload

    def _update_learning_outcome_record(
        self,
        *,
        snapshot_id: str,
        timestamp: datetime,
        updates: Mapping[str, object],
        maturity_status: MaturityStatus | None = None,
    ) -> bool:
        normalized_snapshot_id = snapshot_id.strip()
        if not normalized_snapshot_id:
            return False
        outcome = self._sample_store.get_outcome(normalized_snapshot_id)
        if outcome is None:
            snapshot = self._sample_store.get_snapshot(normalized_snapshot_id)
            if snapshot is None:
                return False
            outcome = OutcomeRecord(snapshot_id=normalized_snapshot_id)
        update_payload = {str(key): value for key, value in updates.items()}
        update_payload["outcome_updated_at"] = timestamp
        base_status = maturity_status if maturity_status is not None else outcome.maturity_status
        update_payload["maturity_status"] = base_status
        candidate = outcome.model_copy(update=update_payload, deep=True)
        resolved_status = self._resolve_learning_outcome_maturity_status(
            current_status=base_status,
            candidate_outcome=candidate,
        )
        if resolved_status != candidate.maturity_status:
            candidate = candidate.model_copy(
                update={"maturity_status": resolved_status},
                deep=True,
            )
        self._sample_store.upsert_outcome(candidate)
        return True

    @staticmethod
    def _resolve_learning_outcome_maturity_status(
        *,
        current_status: MaturityStatus,
        candidate_outcome: OutcomeRecord,
    ) -> MaturityStatus:
        if current_status == MaturityStatus.FULLY_MATURED:
            return MaturityStatus.FULLY_MATURED
        has_execution = candidate_outcome.execution_fill_ratio is not None
        has_reconcile = bool(str(candidate_outcome.reconcile_status or "").strip())
        if (
            current_status in {MaturityStatus.LABEL_MATURED, MaturityStatus.RECONCILED}
            and has_execution
            and has_reconcile
        ):
            return MaturityStatus.FULLY_MATURED
        return current_status

    def _update_learning_execution_outcome(
        self,
        *,
        snapshot_id: str,
        timestamp: datetime,
        side: str,
        status: str,
        execution_price: float,
        reference_price: float,
        quantity: int,
    ) -> bool:
        normalized_status = status.strip().lower()
        filled_statuses = {"opened", "adjusted", "trimmed", "closed"}
        rejected_statuses = {
            "rejected_no_cash",
            "rejected_max_holdings",
            "rejected_same_sector",
            "rejected_execution",
            "rejected_price_unavailable",
            "rejected_quantity",
        }
        if normalized_status not in filled_statuses | rejected_statuses:
            return False
        update_payload: dict[str, object] = {}
        if normalized_status in filled_statuses:
            update_payload["execution_fill_ratio"] = 1.0
        elif normalized_status in rejected_statuses:
            update_payload["execution_fill_ratio"] = 0.0
        slippage_bp = _calculate_execution_slippage_bp(
            side=side,
            execution_price=execution_price,
            reference_price=reference_price,
        )
        if slippage_bp is not None and normalized_status in filled_statuses:
            update_payload["realized_slippage_bp"] = slippage_bp
        if not update_payload:
            return False
        return self._update_learning_outcome_record(
            snapshot_id=snapshot_id,
            timestamp=timestamp,
            updates=update_payload,
        )

    def _update_learning_outcomes_from_portfolio_update(
        self,
        *,
        signals: list[PipelineSignal],
        portfolio_update: Mapping[str, object],
        timestamp: datetime,
    ) -> dict[str, object]:
        raw_executions = portfolio_update.get("executions")
        if not isinstance(raw_executions, list):
            return {"updated": 0, "linked": 0, "symbols": [], "snapshot_ids": []}

        bars_cache: dict[str, pd.DataFrame] = {}
        signal_context_by_symbol: dict[str, dict[str, object]] = {}
        for signal in signals:
            symbol = _normalize_a_share_symbol(signal.symbol)
            snapshot_id = _extract_learning_snapshot_id(signal.decision_trace)
            if not symbol or not snapshot_id:
                continue
            signal_context_by_symbol[symbol] = {
                "snapshot_id": snapshot_id,
                "reference_price": self._resolve_latest_close_price(
                    symbol=symbol,
                    bars_cache=bars_cache,
                ),
            }
        if not signal_context_by_symbol:
            return {"updated": 0, "linked": 0, "symbols": [], "snapshot_ids": []}

        updated = 0
        linked = 0
        symbols: list[str] = []
        snapshot_ids: list[str] = []
        for item in raw_executions:
            if not isinstance(item, dict):
                continue
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol:
                continue
            context = signal_context_by_symbol.get(symbol)
            if context is None:
                continue
            linked += 1
            snapshot_id = str(context.get("snapshot_id", "")).strip()
            if not snapshot_id:
                continue
            if self._update_learning_execution_outcome(
                snapshot_id=snapshot_id,
                timestamp=timestamp,
                side=str(item.get("side", "")).strip().lower(),
                status=str(item.get("status", "")).strip().lower(),
                execution_price=_as_float(item.get("price"), default=0.0),
                reference_price=_as_float(context.get("reference_price"), default=0.0),
                quantity=_as_int(item.get("quantity"), default=0),
            ):
                updated += 1
                symbols.append(symbol)
                snapshot_ids.append(snapshot_id)
        return {
            "updated": updated,
            "linked": linked,
            "symbols": sorted({symbol for symbol in symbols if symbol}),
            "snapshot_ids": sorted({item for item in snapshot_ids if item}),
        }

    def _update_learning_outcomes_from_command_update(
        self,
        *,
        command_update: Mapping[str, object],
        timestamp: datetime,
    ) -> dict[str, object]:
        action = str(command_update.get("action", "")).strip().upper()
        status = str(command_update.get("status", "")).strip().lower()
        if action != "SET_POSITION" or status not in {"opened", "adjusted"}:
            return {"updated": 0, "linked": 0, "symbols": [], "snapshot_ids": []}

        raw_reference = command_update.get("recommendation_reference")
        recommendation_reference = raw_reference if isinstance(raw_reference, dict) else {}
        snapshot_id = _extract_learning_snapshot_id(recommendation_reference)
        if not snapshot_id:
            return {"updated": 0, "linked": 0, "symbols": [], "snapshot_ids": []}

        raw_manual_fill = command_update.get("manual_fill")
        manual_fill = raw_manual_fill if isinstance(raw_manual_fill, dict) else {}
        updated = self._update_learning_execution_outcome(
            snapshot_id=snapshot_id,
            timestamp=timestamp,
            side="buy",
            status=status,
            execution_price=_as_float(manual_fill.get("entry_price"), default=0.0),
            reference_price=_as_float(recommendation_reference.get("reference_price"), default=0.0),
            quantity=_as_int(manual_fill.get("quantity"), default=0),
        )
        symbol = _normalize_a_share_symbol(command_update.get("symbol"))
        return {
            "updated": 1 if updated else 0,
            "linked": 1,
            "symbols": [symbol] if updated and symbol else [],
            "snapshot_ids": [snapshot_id] if updated else [],
        }

    def _latest_learning_snapshot_ids_by_symbol(self) -> dict[str, str]:
        snapshot_ids_by_symbol: dict[str, str] = {}
        for raw_symbol, recommendation_id in self._recommendation_latest_id_by_symbol.items():
            symbol = _normalize_a_share_symbol(raw_symbol)
            if not symbol:
                continue
            raw_snapshot = self._recommendation_snapshot_by_id.get(str(recommendation_id).strip())
            snapshot_id = _extract_learning_snapshot_id(raw_snapshot)
            if snapshot_id:
                snapshot_ids_by_symbol[symbol] = snapshot_id

        for item in self._last_signal_payload:
            if not isinstance(item, dict):
                continue
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            snapshot_id = _extract_learning_snapshot_id(item)
            if symbol and snapshot_id:
                snapshot_ids_by_symbol.setdefault(symbol, snapshot_id)
        return snapshot_ids_by_symbol

    def _update_learning_outcomes_from_reconcile_report(
        self,
        *,
        report: Mapping[str, object],
        timestamp: datetime,
    ) -> dict[str, object]:
        status = str(report.get("status", "")).strip().lower()
        if not status:
            return {"updated": 0, "promoted": 0, "symbols": [], "snapshot_ids": []}
        if status not in {"ok", "mismatch"}:
            return {
                "updated": 0,
                "promoted": 0,
                "symbols": [],
                "snapshot_ids": [],
                "status": f"skipped_{status}",
                "reason": "reconcile_status_not_label_safe",
            }

        strategy_positions = {
            normalized: _as_float(target, default=0.0)
            for symbol, target in self._portfolio.position_map().items()
            if (normalized := _normalize_a_share_symbol(symbol))
        }
        broker_positions = {
            normalized: _as_float(target, default=0.0)
            for symbol, target in self._broker_positions.items()
            if (normalized := _normalize_a_share_symbol(symbol))
        }

        diff_by_symbol: dict[str, float] = {}
        raw_diffs = report.get("diffs")
        if isinstance(raw_diffs, list):
            for item in raw_diffs:
                if not isinstance(item, dict):
                    continue
                symbol = _normalize_a_share_symbol(item.get("symbol"))
                if symbol:
                    diff_by_symbol[symbol] = abs(_as_float(item.get("diff"), default=0.0))

        raw_missing_in_broker = report.get("missing_in_broker", [])
        missing_in_broker = {
            normalized
            for item in (raw_missing_in_broker if isinstance(raw_missing_in_broker, list) else [])
            if (normalized := _normalize_a_share_symbol(item))
        }
        raw_missing_in_strategy = report.get("missing_in_strategy", [])
        missing_in_strategy = {
            normalized
            for item in (raw_missing_in_strategy if isinstance(raw_missing_in_strategy, list) else [])
            if (normalized := _normalize_a_share_symbol(item))
        }

        relevant_symbols = set(diff_by_symbol) | missing_in_broker | missing_in_strategy
        if status in {"ok", "mismatch"}:
            relevant_symbols.update(strategy_positions)
            relevant_symbols.update(broker_positions)

        linked_snapshot_ids = self._latest_learning_snapshot_ids_by_symbol()
        updated = 0
        promoted = 0
        symbols: list[str] = []
        snapshot_ids: list[str] = []
        for symbol in sorted(relevant_symbols):
            snapshot_id = linked_snapshot_ids.get(symbol, "")
            if not snapshot_id:
                continue
            current_outcome = self._sample_store.get_outcome(snapshot_id)
            next_maturity_status: MaturityStatus | None = None
            if (
                current_outcome is not None
                and current_outcome.maturity_status == MaturityStatus.LABEL_MATURED
                and status in {"ok", "mismatch"}
            ):
                next_maturity_status = MaturityStatus.RECONCILED
                promoted += 1

            diff_value: float | None = None
            if symbol in diff_by_symbol:
                diff_value = diff_by_symbol[symbol]
            elif symbol in missing_in_broker:
                diff_value = abs(strategy_positions.get(symbol, 0.0))
            elif symbol in missing_in_strategy:
                diff_value = abs(broker_positions.get(symbol, 0.0))
            elif status == "ok":
                diff_value = 0.0
            elif status == "mismatch":
                diff_value = abs(strategy_positions.get(symbol, 0.0) - broker_positions.get(symbol, 0.0))

            update_payload: dict[str, object] = {"reconcile_status": status}
            if diff_value is not None:
                update_payload["sim_vs_broker_diff"] = round(diff_value, 6)
            if self._update_learning_outcome_record(
                snapshot_id=snapshot_id,
                timestamp=timestamp,
                updates=update_payload,
                maturity_status=next_maturity_status,
            ):
                updated += 1
                symbols.append(symbol)
                snapshot_ids.append(snapshot_id)
        return {
            "updated": updated,
            "promoted": promoted,
            "symbols": sorted({symbol for symbol in symbols if symbol}),
            "snapshot_ids": sorted({item for item in snapshot_ids if item}),
        }

    @property
    def state(self) -> RuntimeState:
        return self._state

    def provider_status(self) -> dict[str, object]:
        controls = self._resolve_evolution_runtime_controls()
        self._pipeline.set_evolution_controls(controls)
        if self._realtime_pipeline is not None:
            self._realtime_pipeline.set_evolution_controls(controls)
        status = self._pipeline.provider_status()
        status["realtime_monitoring_enabled"] = self._realtime_pipeline is not None
        if self._realtime_pipeline is not None:
            status["realtime_monitoring"] = self._realtime_pipeline.provider_status()
        status["market_depth_enabled"] = self._market_depth_provider is not None
        if self._market_depth_provider is not None:
            depth_status = self._market_depth_provider.status()
            if isinstance(depth_status, dict):
                status["market_depth"] = depth_status
        return status

    def latest_report(self) -> dict[str, object] | None:
        report = self._pipeline.latest_report()
        return None if report is None else report.to_dict()

    def latest_signals_snapshot(self) -> dict[str, object]:
        self._refresh_runtime_state_from_disk_if_changed()
        if self._last_signal_payload:
            return {
                "trace_id": self._last_signal_trace_id,
                "timestamp": self._last_signal_timestamp,
                "signals": [dict(item) for item in self._last_signal_payload],
                "source": self._last_signal_source or "memory",
                "storage_source": self._last_signal_storage_source or "memory",
            }
        persisted = self._last_signal_snapshot
        if isinstance(persisted, Mapping):
            raw_signals = persisted.get("signals")
            if isinstance(raw_signals, list) and raw_signals:
                return {
                    "trace_id": str(persisted.get("trace_id", "")).strip(),
                    "timestamp": str(persisted.get("timestamp", "")).strip(),
                    "signals": [dict(item) for item in raw_signals if isinstance(item, Mapping)],
                    "source": str(persisted.get("source", "")).strip() or "runtime_state",
                    "storage_source": "runtime_state",
                }
        week5_signals = _extract_week5_candidate_signals(self._last_week5_scan_report)
        if week5_signals:
            week5_report = self._last_week5_scan_report or {}
            return {
                "trace_id": str(week5_report.get("trace_id", "")).strip(),
                "timestamp": str(week5_report.get("timestamp", "")).strip(),
                "signals": week5_signals,
                "source": "week5_latest_candidates",
                "storage_source": "runtime_state",
            }
        return {
            "trace_id": self._last_signal_trace_id,
            "timestamp": self._last_signal_timestamp,
            "signals": [],
            "source": "empty",
            "storage_source": "",
        }

    def _update_latest_signal_snapshot(
        self,
        *,
        signal_payload: list[dict[str, object]],
        trace_id: str,
        timestamp: datetime,
        source: str,
    ) -> None:
        signals = [dict(item) for item in signal_payload if isinstance(item, Mapping)]
        self._last_signal_payload = signals
        self._last_signal_trace_id = trace_id
        self._last_signal_timestamp = timestamp.isoformat()
        self._last_signal_source = source
        self._last_signal_storage_source = "memory"
        snapshot = {
            "trace_id": trace_id,
            "timestamp": timestamp.isoformat(),
            "source": source,
            "signal_count": len(signals),
            "signals": [dict(item) for item in signals],
        }
        if self._last_signal_snapshot != snapshot:
            self._latest_signal_snapshot_dirty = True
        self._last_signal_snapshot = snapshot

    def latest_notification_filter_diagnostics(self) -> dict[str, object] | None:
        if self._last_notification_filter_diagnostics is None:
            return None
        return deepcopy(self._last_notification_filter_diagnostics)

    def run_signal_quality_audit(
        self,
        *,
        limit: int = 200,
        include_audit_events: bool = True,
    ) -> dict[str, object]:
        snapshot = self.latest_signals_snapshot()
        latest_signals = [
            dict(item) for item in snapshot.get("signals", []) if isinstance(item, dict)
        ]
        signal_source = str(snapshot.get("source", "")).strip() or "signals_latest"
        signal_storage_source = str(snapshot.get("storage_source", "")).strip()
        if not latest_signals:
            latest_signals = _extract_week5_candidate_signals(self._last_week5_scan_report)
            if latest_signals:
                signal_source = "week5_latest_candidates"
                signal_storage_source = "runtime_state"
        audit_events: list[dict[str, object]] = []
        if include_audit_events:
            raw_audit = self.audit_events(limit=limit)
            raw_events = raw_audit.get("events")
            if isinstance(raw_events, list):
                audit_events = [dict(item) for item in raw_events if isinstance(item, dict)]
        try:
            provider_status = self.provider_status()
        except Exception as exc:  # pragma: no cover - defensive status surface
            provider_status = {"status": "error", "error": str(exc)}
        try:
            learning_governance = self.learning_model_governance_status()
        except Exception as exc:  # pragma: no cover - defensive status surface
            learning_governance = {"status": "error", "error": str(exc)}
        report = self._signal_quality_auditor.build_report(
            latest_signals=latest_signals,
            audit_events=audit_events,
            notification_filter_diagnostics=self.latest_notification_filter_diagnostics(),
            provider_status=provider_status,
            week5_report=deepcopy(self._last_week5_scan_report) or {},
            learning_governance=learning_governance,
        )
        report["trace_id"] = str(snapshot.get("trace_id", "")).strip()
        report["signal_source"] = signal_source
        report["signal_storage_source"] = signal_storage_source
        report["source_signal_count"] = len(latest_signals)
        self._last_signal_quality_audit = deepcopy(report)
        self._signal_quality_audit_history.append(deepcopy(report))
        if len(self._signal_quality_audit_history) > 200:
            self._signal_quality_audit_history = self._signal_quality_audit_history[-200:]
        self._record_audit_event(
            "signal_quality_audit",
            trace_id=str(report.get("trace_id", "")),
            message="signal quality audit completed",
            payload={
                "status": str(report.get("status", "")),
                "summary": dict(report.get("summary", {}))
                if isinstance(report.get("summary"), dict)
                else {},
            },
        )
        return deepcopy(report)

    def latest_signal_quality_audit(self) -> dict[str, object]:
        if self._last_signal_quality_audit is None:
            return {"status": "no_audit"}
        return deepcopy(self._last_signal_quality_audit)

    def signal_quality_audit_history(self, limit: int = 20) -> dict[str, object]:
        normalized_limit = max(1, min(int(limit), 200))
        items = self._signal_quality_audit_history[-normalized_limit:]
        return {
            "records": len(items),
            "items": [deepcopy(item) for item in reversed(items)],
        }

    def _select_provider(self, *, use_live_runtime: bool) -> MarketDataProvider:
        if use_live_runtime and self._realtime_provider is not None:
            return self._realtime_provider
        return self._provider

    def _select_pipeline(self, *, use_live_runtime: bool) -> AnalyzerPipeline:
        pipeline = (
            self._realtime_pipeline
            if use_live_runtime and self._realtime_pipeline is not None
            else self._pipeline
        )
        pipeline.set_evolution_controls(self._resolve_evolution_runtime_controls())
        return pipeline

    def _resolve_evolution_runtime_controls(self) -> dict[str, object]:
        latest = self._last_evolution_report
        if not isinstance(latest, dict):
            return {}
        raw_controls = latest.get("runtime_controls")
        if not isinstance(raw_controls, dict):
            return {}
        controls = dict(raw_controls)
        controls.setdefault("source", "evolution")
        controls.setdefault("source_run_id", str(latest.get("run_id", "")))
        controls.setdefault("source_timestamp", str(latest.get("timestamp", "")))
        if (
            bool(controls.get("degraded_mode", False))
            and not str(controls.get("degraded_reason", "")).strip()
        ):
            raw_reasons = controls.get("reasons")
            degraded_reason = ""
            if isinstance(raw_reasons, list):
                degraded_reason = next(
                    (str(item).strip() for item in raw_reasons if str(item).strip()),
                    "",
                )
            controls["degraded_reason"] = degraded_reason or "evolution_controls"
        max_age_hours = max(0.0, float(self._config.evolution.runtime_controls_max_age_hours))
        source_timestamp = str(controls.get("source_timestamp", "")).strip()
        parsed_source = _parse_runtime_datetime(source_timestamp)
        if max_age_hours > 0 and parsed_source is not None:
            age_hours = max(0.0, (datetime.now().timestamp() - parsed_source.timestamp()) / 3600.0)
            if age_hours > max_age_hours:
                return {
                    "source": str(controls.get("source", "evolution")).strip() or "evolution",
                    "source_run_id": str(controls.get("source_run_id", "")).strip(),
                    "source_timestamp": source_timestamp,
                    "stale": True,
                    "stale_reason": "runtime_controls_expired",
                    "stale_age_hours": round(age_hours, 4),
                    "stale_max_age_hours": round(max_age_hours, 4),
                    "degraded_mode": False,
                    "conservative_mode": False,
                    "soft_degraded_mode": False,
                    "hard_degraded_mode": False,
                    "threshold_shift": 0.0,
                    "position_multiplier": 1.0,
                    "global_risk_delta": 0.0,
                    "regime_hint": "stale",
                    "reasons": [],
                    "degraded_reason": "",
                }
        controls.setdefault("stale", False)
        return controls

    def _resolve_m1_negative_case_library(self) -> dict[str, object]:
        latest = self._last_evolution_report
        if not isinstance(latest, dict):
            return {
                "available": False,
                "shared_payload_uri": "",
                "negative_case_count": 0,
                "reason_counts": {},
                "cases": [],
            }
        modules = latest.get("modules")
        if not isinstance(modules, dict):
            return {
                "available": False,
                "shared_payload_uri": "",
                "negative_case_count": 0,
                "reason_counts": {},
                "cases": [],
            }
        raw_m1 = modules.get("m1")
        if not isinstance(raw_m1, dict):
            return {
                "available": False,
                "shared_payload_uri": "",
                "negative_case_count": 0,
                "reason_counts": {},
                "cases": [],
            }

        shared_payload_uri = str(raw_m1.get("shared_payload_uri", "")).strip()
        reason_counts = raw_m1.get("reason_counts", {})
        if not isinstance(reason_counts, dict):
            reason_counts = {}
        negative_case_count = _as_int(raw_m1.get("negative_case_count"), default=0)
        cases: list[dict[str, object]] = []

        if shared_payload_uri:
            try:
                payload_path = self._resolve_evolution_path(shared_payload_uri)
                if payload_path.exists():
                    payload = json.loads(payload_path.read_text(encoding="utf-8"))
                    raw_cases = payload.get("cases", []) if isinstance(payload, dict) else []
                    if isinstance(raw_cases, list):
                        cases = [item for item in raw_cases if isinstance(item, dict)]
                    if negative_case_count <= 0 and isinstance(payload, dict):
                        negative_case_count = _as_int(
                            payload.get("negative_case_count"),
                            default=len(cases),
                        )
                    if (not reason_counts) and isinstance(payload, dict):
                        payload_reason_counts = payload.get("reason_counts", {})
                        if isinstance(payload_reason_counts, dict):
                            reason_counts = payload_reason_counts
            except Exception:
                cases = []

        if not cases:
            raw_cases_preview = raw_m1.get("cases_preview", [])
            if isinstance(raw_cases_preview, list):
                cases = [item for item in raw_cases_preview if isinstance(item, dict)]
        if negative_case_count <= 0:
            negative_case_count = len(cases)

        return {
            "available": bool(cases),
            "shared_payload_uri": shared_payload_uri,
            "negative_case_count": negative_case_count,
            "reason_counts": {
                str(key): _as_int(value, default=0)
                for key, value in reason_counts.items()
                if str(key).strip()
            },
            "cases": cases,
        }

    def _apply_m1_negative_case_constraints(
        self,
        *,
        signals: list[PipelineSignal],
        strategy: str,
        use_live_runtime: bool,
    ) -> dict[str, object]:
        library = self._resolve_m1_negative_case_library()
        reason_counts = _coerce_object_mapping(library.get("reason_counts"))
        cases = _coerce_mapping_list(library.get("cases"))
        if not bool(library.get("available", False)):
            return {
                "available": False,
                "applied": False,
                "shared_payload_uri": str(library.get("shared_payload_uri", "")),
                "negative_case_count": _as_int(library.get("negative_case_count"), default=0),
                "penalized": 0,
                "downgraded": 0,
                "scaled": 0,
                "reason_counts": dict(reason_counts),
                "matches": [],
            }

        provider = self._select_provider(use_live_runtime=use_live_runtime)
        thresholds = self._strategy_thresholds(strategy=strategy)
        lookback_days = max(
            120,
            int(self._config.evolution.universe_spec.signal_analysis_lookback_days),
        )
        bar_cache: dict[str, pd.DataFrame] = {}
        penalized = 0
        downgraded = 0
        scaled = 0
        matches: list[dict[str, object]] = []

        for signal in signals:
            normalized_symbol = _normalize_a_share_symbol(signal.symbol) or signal.symbol
            if normalized_symbol not in bar_cache:
                try:
                    fetched = provider.fetch_daily_bars(
                        symbol=normalized_symbol,
                        lookback_days=lookback_days,
                    )
                except Exception:
                    fetched = pd.DataFrame()
                bar_cache[normalized_symbol] = (
                    fetched if isinstance(fetched, pd.DataFrame) else pd.DataFrame()
                )
            bars = bar_cache[normalized_symbol]
            profile = self._build_m1_negative_case_signal_profile(signal=signal, bars=bars)

            best_case: dict[str, object] | None = None
            best_similarity = 0.0
            for raw_case in cases:
                similarity = self._m1_negative_case_similarity(
                    signal=signal,
                    profile=profile,
                    case=raw_case,
                )
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_case = raw_case

            if best_case is None or best_similarity < 0.55:
                continue

            case_reason_codes = _coerce_text_list(best_case.get("reason_codes"))
            case_bucket = str(best_case.get("bucket", "")).strip().lower()
            penalty = round(
                min(18.0, 4.0 + best_similarity * 12.0 + (2.0 if signal.action == "buy" else 0.0)),
                2,
            )
            before_action = signal.action
            before_position = signal.target_position
            signal.score = round(max(0.0, signal.score - penalty), 2)
            signal.grade = _grade_by_threshold(score=signal.score, thresholds=thresholds)

            if signal.action == "buy":
                if signal.score < thresholds["a"]:
                    signal.action = "watch" if signal.score >= thresholds["b"] else "hold"
                    signal.target_position = 0.0
                    downgraded += 1
                    if "m1_negative_case_gate" not in signal.reasons:
                        signal.reasons.append("m1_negative_case_gate")
                else:
                    reduced_position = _clamp(
                        signal.target_position * max(0.40, 1.0 - 0.35 * best_similarity),
                        0.0,
                        1.0,
                    )
                    if abs(reduced_position - signal.target_position) > 1e-9:
                        signal.target_position = round(reduced_position, 4)
                        scaled += 1
            elif signal.action == "watch" and signal.score < thresholds["b"]:
                signal.action = "hold"
                signal.target_position = 0.0
                downgraded += 1
                if "m1_negative_case_gate" not in signal.reasons:
                    signal.reasons.append("m1_negative_case_gate")

            signal.reasons.append(
                "m1_negative_case_constraint:"
                + "|".join(case_reason_codes[:3] or ["unclassified_negative_case"])
            )
            signal.reasons.append(f"m1_negative_case_similarity:{best_similarity:.3f}")
            signal.reasons.append(f"m1_negative_case_penalty:{penalty:.2f}")
            m1_feedback = {
                "m1_negative_case_applied": True,
                "m1_negative_case_bucket": case_bucket or "",
                "m1_negative_case_similarity": round(best_similarity, 4),
                "m1_similarity": round(best_similarity, 4),
                "m1_negative_case_penalty": penalty,
                "m1_reason_codes": case_reason_codes,
                "m1_matched_case_symbol": str(best_case.get("symbol", "")).strip(),
                "m1_gate_triggered": before_action != signal.action,
                "m1_scaled_position": abs(before_position - signal.target_position) > 1e-9,
                "m1_signal_profile_tags": _coerce_text_list(profile.get("tags")),
                "m1_signal_profile_metrics": _coerce_object_mapping(profile.get("metrics")),
            }
            self._record_signal_runtime_learning_feedback(
                signal=signal,
                module_name="m1",
                payload=m1_feedback,
            )
            penalized += 1
            matches.append(
                {
                    "symbol": normalized_symbol,
                    "similarity": round(best_similarity, 4),
                    "penalty": penalty,
                    "bucket": case_bucket,
                    "before_action": before_action,
                    "after_action": signal.action,
                    "before_position": round(before_position, 4),
                    "after_position": round(signal.target_position, 4),
                    "reason_codes": case_reason_codes,
                    "matched_case_symbol": str(best_case.get("symbol", "")),
                    "signal_profile": profile,
                }
            )

        return {
            "available": True,
            "applied": penalized > 0,
            "shared_payload_uri": str(library.get("shared_payload_uri", "")),
            "negative_case_count": _as_int(library.get("negative_case_count"), default=0),
            "penalized": penalized,
            "downgraded": downgraded,
            "scaled": scaled,
            "reason_counts": dict(reason_counts),
            "matches": matches[:10],
        }

    def _build_m1_negative_case_signal_profile(
        self,
        *,
        signal: PipelineSignal,
        bars: pd.DataFrame,
    ) -> dict[str, object]:
        reason_tags = {
            str(reason).strip().lower() for reason in signal.reasons if str(reason).strip()
        }
        tags: list[str] = []

        if "liquidity_failed" in reason_tags:
            tags.append("liquidity_insufficient")
        if any(
            reason.startswith("predictor_mode:") or reason.startswith("predictor_reason:")
            for reason in reason_tags
        ) or any(reason.startswith("financial_filter:") for reason in reason_tags):
            tags.append("data_incomplete")
        if "cross_review" in reason_tags or any("divergence" in reason for reason in reason_tags):
            tags.append("model_divergence")

        probabilities = signal.probabilities if isinstance(signal.probabilities, dict) else {}
        lgbm_prob = _as_float(probabilities.get("lgbm"), default=0.0)
        xgb_prob = _as_float(probabilities.get("xgb"), default=0.0)
        meta_prob = _as_float(probabilities.get("meta"), default=0.0)
        if (
            max(abs(lgbm_prob - xgb_prob), abs(meta_prob - lgbm_prob), abs(meta_prob - xgb_prob))
            >= 0.20
        ):
            if "model_divergence" not in tags:
                tags.append("model_divergence")

        metrics = {
            "ret_20d": 0.0,
            "heat_ratio": 0.0,
            "pressure_index": 0.0,
            "avg_turnover_20": 0.0,
            "float_market_cap": 0.0,
        }
        if isinstance(bars, pd.DataFrame) and not bars.empty and "close" in bars.columns:
            ordered = bars.sort_index().copy()
            close = pd.to_numeric(ordered["close"], errors="coerce").dropna()
            if len(close) >= 20:
                metrics["ret_20d"] = float(close.iloc[-1] / max(close.iloc[-20], 1e-9) - 1.0)
            recent_high = float(close.tail(min(20, len(close))).max()) if not close.empty else 0.0
            turnover_raw = (
                ordered["turnover"]
                if "turnover" in ordered.columns
                else ordered["amount"]
                if "amount" in ordered.columns
                else pd.Series(0.0, index=ordered.index)
            )
            turnover = pd.to_numeric(
                turnover_raw,
                errors="coerce",
            ).fillna(0.0)
            avg_turnover_20 = (
                float(turnover.tail(min(20, len(turnover))).mean()) if not turnover.empty else 0.0
            )
            avg_turnover_60 = (
                float(turnover.tail(min(60, len(turnover))).mean())
                if not turnover.empty
                else avg_turnover_20
            )
            heat_ratio = avg_turnover_20 / max(avg_turnover_60, 1.0)
            metrics["avg_turnover_20"] = avg_turnover_20
            metrics["heat_ratio"] = heat_ratio

            float_market_cap_raw = (
                ordered["float_market_cap"]
                if "float_market_cap" in ordered.columns
                else pd.Series(0.0, index=ordered.index)
            )
            float_market_cap = pd.to_numeric(
                float_market_cap_raw,
                errors="coerce",
            ).ffill().bfill().fillna(0.0)
            metrics["float_market_cap"] = (
                float(float_market_cap.iloc[-1]) if not float_market_cap.empty else 0.0
            )

            if signal.action == "buy" and recent_high > 0.0:
                recent_high_ratio = float(close.iloc[-1] / recent_high)
                if metrics["ret_20d"] >= 0.10 and heat_ratio >= 1.20 and recent_high_ratio >= 0.97:
                    tags.append("chase_high")

            records = [
                {str(key): value for key, value in row.items()}
                for row in ordered.tail(min(20, len(ordered))).to_dict(orient="records")
            ]
            m6_result = evaluate_m6_counterparty(records=records)
            metrics["pressure_index"] = float(m6_result.metrics.pressure_index)
            if m6_result.status == "heavy_sell_pressure":
                tags.append("high_sell_pressure")

            latest_bar = ordered.iloc[-1]
            if (
                not _coerce_bool(latest_bar.get("financial_data_complete", True))
                or not _coerce_bool(latest_bar.get("background_data_complete", True))
            ) and "data_incomplete" not in tags:
                tags.append("data_incomplete")

            if (avg_turnover_20 > 0.0 and avg_turnover_20 < 50_000_000.0) or (
                metrics["float_market_cap"] > 0.0 and metrics["float_market_cap"] < 8_000_000_000.0
            ):
                if "liquidity_insufficient" not in tags:
                    tags.append("liquidity_insufficient")

        return {
            "symbol": _normalize_a_share_symbol(signal.symbol) or signal.symbol,
            "tags": _dedupe_preserve_order(tags),
            "metrics": metrics,
        }

    def _m1_negative_case_similarity(
        self,
        *,
        signal: PipelineSignal,
        profile: Mapping[str, object],
        case: Mapping[str, object],
    ) -> float:
        signal_symbol = _normalize_a_share_symbol(signal.symbol) or signal.symbol
        case_symbol = (
            _normalize_a_share_symbol(case.get("symbol")) or str(case.get("symbol", "")).strip()
        )
        same_symbol = signal_symbol == case_symbol and bool(signal_symbol)

        current_tags = (
            set(_coerce_text_list(profile.get("tags")))
            if isinstance(profile.get("tags"), list)
            else set()
        )
        case_tags = (
            set(_coerce_text_list(case.get("reason_codes")))
            if isinstance(case.get("reason_codes"), list)
            else set()
        )
        overlap = len(current_tags & case_tags) / max(len(current_tags | case_tags), 1)

        case_bucket = str(case.get("bucket", "")).strip().lower()
        bucket_bonus = 0.05 if case_bucket == "severe" and signal.action == "buy" else 0.0
        similarity = (0.55 if same_symbol else 0.0) + 0.45 * overlap + bucket_bonus
        return _clamp(similarity, 0.0, 1.0)

    def _record_signal_runtime_learning_feedback(
        self,
        *,
        signal: PipelineSignal,
        module_name: str,
        payload: Mapping[str, object],
    ) -> None:
        normalized_module = module_name.strip().lower()
        if not normalized_module:
            return
        compact_payload = _compact_runtime_feedback_payload(payload)
        if not compact_payload:
            return
        runtime_feedback = signal.decision_trace.get("runtime_feedback")
        if not isinstance(runtime_feedback, dict):
            runtime_feedback = {}
            signal.decision_trace["runtime_feedback"] = runtime_feedback
        runtime_feedback[normalized_module] = compact_payload

    def _build_m3_learning_feedback_by_symbol(
        self,
        *,
        signals: Sequence[PipelineSignal],
        week6_execution: Mapping[str, object],
    ) -> dict[str, dict[str, object]]:
        normalized_symbols = sorted(
            {
                normalized
                for normalized in (_normalize_a_share_symbol(signal.symbol) for signal in signals)
                if normalized
            }
        )
        if not normalized_symbols:
            return {}

        latest_report = self._last_evolution_report if isinstance(self._last_evolution_report, dict) else {}
        modules = self._latest_evolution_modules()
        raw_m3 = _coerce_object_mapping(modules.get("m3"))
        raw_m8 = _coerce_object_mapping(modules.get("m8"))
        artifact_uri = str(raw_m8.get("artifact_uri", "")).strip()
        items = self._load_dashboard_m8_items(artifact_uri=artifact_uri) if artifact_uri else []

        regime = str(week6_execution.get("regime", "")).strip()
        threshold_shift = _as_float_or_none(week6_execution.get("threshold_shift"))
        position_multiplier = _as_float_or_none(week6_execution.get("position_multiplier"))
        global_risk_score = _as_float_or_none(week6_execution.get("global_risk_score"))
        vector_profile_id = str(raw_m3.get("vector_profile_id", "")).strip()
        source_run_id = str(latest_report.get("run_id", "")).strip()
        source_timestamp = str(latest_report.get("timestamp", "")).strip()

        feedback_by_symbol: dict[str, dict[str, object]] = {}
        for symbol in normalized_symbols:
            payload: dict[str, object] = {}
            if regime:
                payload["runtime_regime"] = regime
            if threshold_shift is not None:
                payload["runtime_threshold_shift"] = round(threshold_shift, 4)
            if position_multiplier is not None:
                payload["runtime_position_multiplier"] = round(position_multiplier, 4)
            if global_risk_score is not None:
                payload["runtime_global_risk_score"] = round(global_risk_score, 4)
            if vector_profile_id:
                payload["m3_vector_profile_id"] = vector_profile_id
            if source_run_id:
                payload["m3_source_run_id"] = source_run_id
            if source_timestamp:
                payload["m3_source_timestamp"] = source_timestamp
            feedback_by_symbol[symbol] = payload

        for item in items:
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol or symbol not in feedback_by_symbol:
                continue
            payload = feedback_by_symbol.setdefault(symbol, {})
            similarity = _as_float_or_none(item.get("best_similarity"))
            if similarity is not None:
                rounded_similarity = round(_clamp(similarity, 0.0, 1.0), 4)
                payload["m3_match_score"] = rounded_similarity
                payload["m3_similarity"] = rounded_similarity
                payload["pattern_memory_similarity"] = rounded_similarity
            recommendation = str(item.get("recommendation", "")).strip().lower()
            if recommendation:
                payload["m3_recommendation"] = recommendation
            registry_signature = str(item.get("registry_signature", "")).strip()
            if registry_signature:
                payload["m3_registry_signature"] = registry_signature
            failed_gates = _coerce_text_list(item.get("failed_gates"))
            if failed_gates:
                payload["m3_failed_gates"] = failed_gates
            passed_gates = _as_int(item.get("passed_gates"), default=0)
            gate_total = _as_int(item.get("gate_total"), default=0)
            if gate_total > 0:
                payload["m3_passed_gates"] = passed_gates
                payload["m3_gate_total"] = gate_total
                payload["m3_gate_pass_ratio"] = round(passed_gates / max(gate_total, 1), 4)

        return feedback_by_symbol

    def _build_m7_learning_feedback_by_symbol(
        self,
        *,
        symbols: Sequence[str],
    ) -> dict[str, dict[str, object]]:
        normalized_symbols = sorted(
            {
                normalized
                for normalized in (_normalize_a_share_symbol(symbol) for symbol in symbols)
                if normalized
            }
        )
        if not normalized_symbols:
            return {}

        modules = self._latest_evolution_modules()
        raw_m7 = _coerce_object_mapping(modules.get("m7"))
        ledger = _coerce_object_mapping(raw_m7.get("ledger"))
        effectiveness = _coerce_object_mapping(ledger.get("effectiveness"))
        average_effectiveness = _as_float_or_none(effectiveness.get("average_effectiveness"))
        source_reliability_lookup: dict[str, float] = {}
        for item in _coerce_mapping_list(effectiveness.get("source_reliability")):
            source = str(item.get("source", "")).strip()
            mean_effectiveness = _as_float_or_none(item.get("mean_effectiveness"))
            if source and mean_effectiveness is not None:
                source_reliability_lookup[source] = round(_clamp(mean_effectiveness, 0.0, 1.0), 4)

        grouped_records: dict[str, list[dict[str, object]]] = {}
        for item in self._build_evolution_m7_news_records(normalized_symbols):
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol or symbol not in normalized_symbols:
                continue
            grouped_records.setdefault(symbol, []).append(dict(item))

        feedback_by_symbol: dict[str, dict[str, object]] = {}
        status = str(raw_m7.get("status", "")).strip().lower()
        for symbol in normalized_symbols:
            records = grouped_records.get(symbol, [])
            concrete_records = [item for item in records if not bool(item.get("proxy_generated", False))]
            active_records = concrete_records or records
            payload: dict[str, object] = {}
            if status:
                payload["m7_status"] = status
            if average_effectiveness is not None:
                payload["m7_effectiveness_score"] = round(
                    _clamp(average_effectiveness, 0.0, 1.0),
                    4,
                )
            if active_records:
                payload["m7_news_count"] = len(active_records)
                payload["m7_proxy_generated"] = bool(not concrete_records and records)
                source_scores: list[float] = []
                sentiments: list[float] = []
                confidences: list[float] = []
                top_record = max(
                    active_records,
                    key=lambda item: (
                        _as_float(item.get("llm_confidence"), default=0.0),
                        abs(_as_float(item.get("sentiment"), default=0.0)),
                        str(item.get("headline", "")),
                    ),
                )
                top_source = ""
                for item in active_records:
                    source = (
                        str(item.get("source", "")).strip()
                        or str(item.get("provider", "")).strip()
                        or str(item.get("source_file", "")).strip()
                    )
                    if source and source in source_reliability_lookup:
                        source_scores.append(source_reliability_lookup[source])
                    sentiment = _as_float_or_none(item.get("sentiment"))
                    if sentiment is not None:
                        sentiments.append(_clamp(sentiment, -1.0, 1.0))
                    confidence = _as_float_or_none(item.get("llm_confidence"))
                    if confidence is not None:
                        confidences.append(_clamp(confidence, 0.0, 1.0))
                    if not top_source and source:
                        top_source = source
                if source_scores:
                    payload["m7_source_reliability"] = round(
                        sum(source_scores) / len(source_scores),
                        4,
                    )
                if sentiments:
                    payload["m7_mean_sentiment"] = round(sum(sentiments) / len(sentiments), 4)
                if confidences:
                    payload["m7_mean_confidence"] = round(sum(confidences) / len(confidences), 4)
                if top_source:
                    payload["m7_top_source"] = top_source
                headline = str(top_record.get("headline", "")).strip()
                if headline:
                    payload["m7_top_headline"] = headline
            feedback_by_symbol[symbol] = payload
        return feedback_by_symbol

    def _sync_learning_snapshot_feedback(
        self,
        *,
        signals: Sequence[PipelineSignal],
        week6_execution: Mapping[str, object],
    ) -> None:
        if self._sample_store is None or not signals:
            return

        normalized_symbols = [
            normalized
            for normalized in (_normalize_a_share_symbol(signal.symbol) for signal in signals)
            if normalized
        ]
        m3_feedback_by_symbol = self._build_m3_learning_feedback_by_symbol(
            signals=signals,
            week6_execution=week6_execution,
        )
        m7_feedback_by_symbol = self._build_m7_learning_feedback_by_symbol(symbols=normalized_symbols)

        for signal in signals:
            final_decision = signal.decision_trace.get("final_decision")
            if not isinstance(final_decision, dict):
                final_decision = {}
                signal.decision_trace["final_decision"] = final_decision
            final_decision["action"] = str(signal.action).strip().lower()
            final_decision["target_position"] = round(float(signal.target_position), 4)
            final_decision["score"] = round(float(signal.score), 4)
            final_decision["grade"] = str(signal.grade).strip()

            snapshot_id = _extract_learning_snapshot_id(signal.decision_trace)
            symbol = _normalize_a_share_symbol(signal.symbol)
            if not snapshot_id or not symbol:
                continue

            if symbol in m3_feedback_by_symbol:
                self._record_signal_runtime_learning_feedback(
                    signal=signal,
                    module_name="m3",
                    payload=m3_feedback_by_symbol[symbol],
                )
            if symbol in m7_feedback_by_symbol:
                self._record_signal_runtime_learning_feedback(
                    signal=signal,
                    module_name="m7",
                    payload=m7_feedback_by_symbol[symbol],
                )

            runtime_feedback = _coerce_object_mapping(signal.decision_trace.get("runtime_feedback"))
            risk_context = _coerce_object_mapping(runtime_feedback.get("m1"))
            regime_context = _coerce_object_mapping(runtime_feedback.get("m3"))
            news_context = _coerce_object_mapping(runtime_feedback.get("m7"))
            if not risk_context and not regime_context and not news_context:
                continue
            try:
                self._sample_store.enrich_snapshot_contexts(
                    snapshot_id=snapshot_id,
                    risk_context=risk_context,
                    news_context=news_context,
                    regime_context=regime_context,
                )
            except Exception:
                continue

    def _depth_scope_enabled(self, scope: str) -> bool:
        if self._market_depth_provider is None:
            return False
        allowed = {
            str(item).strip().lower()
            for item in self._config.market_depth.poll_scopes
            if str(item).strip()
        }
        if not allowed:
            return False
        return scope.strip().lower() in allowed

    def _fetch_market_depth_snapshots(
        self,
        *,
        symbols: list[str],
        scope: str,
        force_refresh: bool,
    ) -> dict[str, dict[str, object]]:
        if not self._depth_scope_enabled(scope):
            return {}
        normalized = _dedupe_preserve_order(
            [symbol for symbol in (_normalize_a_share_symbol(item) for item in symbols) if symbol]
        )
        if not normalized:
            return {}
        capped = normalized[: max(1, int(self._config.market_depth.max_symbols_per_poll))]
        provider = self._market_depth_provider
        if provider is None:
            return {}
        try:
            return provider.fetch_snapshots(capped, force_refresh=force_refresh)
        except Exception:
            return {}

    def preview_news_component(self, symbol: str, strategy: str = "trend") -> dict[str, object]:
        return self._news_service.preview_news_component(symbol=symbol, strategy=strategy)

    def preview_news_components(
        self,
        symbols: list[str],
        strategy: str = "trend",
    ) -> dict[str, object]:
        return self._news_service.preview_news_components(symbols=symbols, strategy=strategy)

    def preview_news_watchlist(
        self,
        strategy: str = "trend",
        limit: int = 20,
        record_audit: bool = True,
    ) -> dict[str, object]:
        return self._news_service.preview_news_watchlist(
            strategy=strategy,
            limit=limit,
            record_audit=record_audit,
        )

    def run_m7_live_news_sync(
        self,
        *,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        force_refresh: bool = False,
        enable_ai_review: bool | None = None,
    ) -> dict[str, object]:
        return self._news_service.run_m7_live_news_sync(
            symbols=symbols,
            timestamp=timestamp,
            force_refresh=force_refresh,
            enable_ai_review=enable_ai_review,
        )

    def build_live_news_briefing(
        self,
        *,
        phase: str = "premarket",
        strategy: str = "trend",
        max_symbols: int = 6,
        max_items: int = 6,
        max_age_hours: float = 18.0,
        force_refresh: bool = False,
        record_audit: bool = True,
    ) -> dict[str, object]:
        return self._news_service.build_live_news_briefing(
            phase=phase,
            strategy=strategy,
            max_symbols=max_symbols,
            max_items=max_items,
            max_age_hours=max_age_hours,
            force_refresh=force_refresh,
            record_audit=record_audit,
        )

    def _live_news_briefing_cache_key(
        self,
        *,
        phase: str,
        strategy: str,
        max_age_hours: float,
        now: datetime,
    ) -> str:
        age_bucket = round(max(1.0, max_age_hours), 2)
        return f"live-news-briefing:{now.strftime('%Y%m%d')}:{phase}:{strategy}:{age_bucket:.2f}"

    def _live_news_briefing_cache_ttl_sec(
        self,
        *,
        phase: str,
        now: datetime,
    ) -> int:
        default_ttl_sec = 30 * 60
        raw_boundary = ""
        if phase == "premarket":
            raw_boundary = str(self._config.scheduler.midday_news_time).strip()
        elif phase == "midday":
            raw_boundary = str(self._config.scheduler.close_reconcile_time).strip()
        if not raw_boundary:
            return default_ttl_sec
        try:
            boundary_clock = _parse_hhmm_time(raw_boundary)
        except ValueError:
            return default_ttl_sec
        boundary_dt = datetime.combine(now.date(), boundary_clock)
        if boundary_dt <= now:
            return default_ttl_sec
        return max(10 * 60, int((boundary_dt - now).total_seconds()))

    def _load_live_news_briefing_cache(
        self,
        cache_key: str,
    ) -> dict[str, object] | None:
        cached = self._cache.get(cache_key)
        if not cached:
            return None
        try:
            payload = json.loads(cached)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _slice_live_news_briefing_payload(
        self,
        *,
        payload: dict[str, object],
        max_symbols: int,
        max_items: int,
        cache_hit: bool,
        cache_key: str,
    ) -> dict[str, object]:
        normalized_max_symbols = max(1, int(max_symbols))
        normalized_max_items = max(1, int(max_items))
        focus_symbols_raw = payload.get("focus_symbols", [])
        focus_symbols = (
            [str(item).strip() for item in focus_symbols_raw if str(item).strip()]
            if isinstance(focus_symbols_raw, list)
            else []
        )
        selected_focus_symbols = focus_symbols[:normalized_max_symbols]
        allowed_symbols = set(selected_focus_symbols)
        sliced_items: list[dict[str, object]] = []
        raw_items = payload.get("items", [])
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if allowed_symbols and symbol and symbol not in allowed_symbols:
                    continue
                sliced_items.append(deepcopy(item))
                if len(sliced_items) >= normalized_max_items:
                    break
        sliced_payload = deepcopy(payload)
        sliced_payload["focus_symbols"] = selected_focus_symbols
        sliced_payload["focus_count"] = len(selected_focus_symbols)
        sliced_payload["items"] = sliced_items
        sliced_payload["records"] = len(sliced_items)
        sliced_payload["real_news_available"] = bool(sliced_items)
        sliced_payload["cache_hit"] = cache_hit
        sliced_payload["cache_key"] = cache_key
        return sliced_payload

    def _select_live_news_focus_symbols(
        self,
        *,
        preview: dict[str, object],
        max_symbols: int,
    ) -> list[str]:
        symbols: list[str] = []
        raw_items = preview.get("items", [])
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    symbols.append(symbol)
        latest_week5 = self._last_week5_scan_report
        if isinstance(latest_week5, dict):
            first_board = latest_week5.get("first_board", {})
            if isinstance(first_board, dict):
                for key in ("leaders", "candidates"):
                    rows = first_board.get(key)
                    if not isinstance(rows, list):
                        continue
                    for item in rows:
                        if not isinstance(item, dict):
                            continue
                        symbol = str(item.get("symbol", "")).strip()
                        if symbol:
                            symbols.append(symbol)
        symbols.extend(str(item).strip() for item in self._state.watchlist if str(item).strip())
        return _dedupe_preserve_order(symbols)[: max(1, max_symbols)]

    def _fetch_symbol_live_news(
        self,
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        normalized_symbol = str(symbol).strip()
        if not normalized_symbol:
            return []
        cache_key = f"live-news:{normalized_symbol}"
        if not force_refresh:
            cached = self._cache.get(cache_key)
            if cached:
                try:
                    parsed = json.loads(cached)
                    if isinstance(parsed, list):
                        return [item for item in parsed if isinstance(item, dict)]
                except json.JSONDecodeError:
                    pass
        try:
            ak = self._import_akshare()
            frame: pd.DataFrame = ak.stock_news_em(symbol=normalized_symbol)
        except Exception:
            return []
        if frame is None or frame.empty:
            return []
        records: list[dict[str, object]] = []
        max_age_sec = max(1.0, max_age_hours) * 3600.0
        for _, row in frame.head(30).iterrows():
            title = str(row.get("新闻标题", "")).strip()
            content = str(row.get("新闻内容", "")).strip()
            published_at = str(row.get("发布时间", "")).strip()
            source = str(row.get("文章来源", "")).strip()
            url = str(row.get("新闻链接", "")).strip()
            if not title:
                continue
            published_dt = _parse_runtime_datetime(published_at)
            if published_dt is not None:
                age_sec = max(0.0, (now - published_dt).total_seconds())
                if age_sec > max_age_sec:
                    continue
            records.append(
                {
                    "symbol": normalized_symbol,
                    "title": title,
                    "content": content,
                    "published_at": published_dt.isoformat()
                    if published_dt is not None
                    else published_at,
                    "source": source,
                    "url": url,
                }
            )
            if len(records) >= max(1, per_symbol_limit):
                break
        self._cache.set(
            cache_key,
            json.dumps(records, ensure_ascii=False),
            ttl_sec=15 * 60,
        )
        return records

    def _resolve_m7_live_news_symbols(
        self,
        *,
        symbols: list[str] | None,
        max_symbols: int,
    ) -> list[str]:
        if symbols is not None:
            selected = [
                normalized
                for normalized in (_normalize_a_share_symbol(item) for item in symbols)
                if normalized
            ]
            return _dedupe_preserve_order(selected)[: max(1, max_symbols)]

        selected = self._select_live_news_focus_symbols(
            preview={"items": []},
            max_symbols=max_symbols,
        )
        if selected:
            return selected[: max(1, max_symbols)]

        seed_symbols = self._bootstrap_seed_symbols(cap=max_symbols)
        return seed_symbols[: max(1, max_symbols)]

    def _resolve_m7_news_artifact_path(self) -> Path:
        raw_path = str(self._config.evolution.m7_news_records_path).strip()
        if raw_path:
            return self._resolve_evolution_path(raw_path)
        return self._resolve_evolution_path("artifacts/evolution/inputs/m7_news_latest.jsonl")

    def _collect_live_m7_news_records(
        self,
        *,
        symbols: list[str],
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
        enable_ai_review: bool,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        provider = str(self._config.evolution.m7_live_news_provider).strip().lower() or "akshare_em"
        summary: dict[str, object] = {
            "provider": provider,
            "symbol_count": len(symbols),
            "fetched_symbols": 0,
            "raw_items": 0,
            "records": 0,
            "ai_review": {"enabled": enable_ai_review, "attempted": 0, "succeeded": 0, "failed": 0},
            "errors": [],
        }
        if provider not in {"akshare_em"}:
            summary["errors"] = [f"unsupported_provider:{provider}"]
            return [], summary

        raw_items: list[dict[str, object]] = []
        max_workers = min(4, max(1, len(symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    self._fetch_symbol_live_news,
                    symbol=symbol,
                    now=now,
                    max_age_hours=max_age_hours,
                    per_symbol_limit=per_symbol_limit,
                    force_refresh=force_refresh,
                ): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    rows = future.result(timeout=20)
                except Exception as exc:
                    errors = summary.get("errors")
                    if isinstance(errors, list):
                        errors.append(f"{symbol}:{exc.__class__.__name__}")
                    continue
                if rows:
                    summary["fetched_symbols"] = (
                        _as_int(summary.get("fetched_symbols"), default=0) + 1
                    )
                for row in rows:
                    if isinstance(row, dict):
                        raw_items.append(dict(row))
        summary["raw_items"] = len(raw_items)

        normalized_records = self._normalize_live_m7_news_records(raw_items=raw_items)
        ai_review_summary: dict[str, object] = {
            "enabled": enable_ai_review,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
        }
        if normalized_records and enable_ai_review:
            normalized_records, ai_review_summary = self._enrich_m7_news_records_with_ai_review(
                records=normalized_records,
                enabled_override=enable_ai_review,
            )
        summary["ai_review"] = ai_review_summary
        summary["records"] = len(normalized_records)
        return normalized_records, summary

    def _normalize_live_m7_news_records(
        self,
        *,
        raw_items: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        default_cost = max(0.01, self._config.evolution.m7_default_event_cost)
        records: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for item in raw_items:
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            title = str(item.get("title", "")).strip()
            if not symbol or not title:
                continue
            published_at = str(item.get("published_at", "")).strip()
            content = str(item.get("content", "")).strip()
            event_seed = f"{symbol}|{title}|{published_at}"
            event_id = hashlib.sha256(event_seed.encode("utf-8")).hexdigest()[:24]
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            sentiment, confidence = _estimate_news_sentiment_heuristic(title=title, content=content)
            records.append(
                {
                    "event_id": event_id,
                    "symbol": symbol,
                    "headline": title,
                    "content": content,
                    "published_at": published_at,
                    "source": str(item.get("source", "")).strip(),
                    "url": str(item.get("url", "")).strip(),
                    "sentiment": sentiment,
                    "llm_sentiment": sentiment,
                    "cost": default_cost,
                    "llm_verdict": _m7_llm_verdict_from_sentiment(sentiment),
                    "llm_confidence": confidence,
                    "source_file": "__live_akshare_em__",
                    "provider": "akshare_em",
                    "proxy_generated": False,
                }
            )
        records.sort(
            key=lambda item: (
                str(item.get("published_at", "")),
                str(item.get("symbol", "")),
                str(item.get("headline", "")),
            ),
            reverse=True,
        )
        return records

    def _build_m7_news_review_judges(
        self,
        *,
        enabled_override: bool | None = None,
    ) -> tuple[OpenAICompatibleNewsJudge | None, OpenAICompatibleNewsJudge | None]:
        ai_enabled = (
            bool(enabled_override)
            if enabled_override is not None
            else bool(self._config.evolution.m7_ai_review_enabled)
        )
        if not ai_enabled:
            return None, None
        primary = self._build_m7_news_review_judge(
            provider=self._config.evolution.llm_provider,
            api_key=self._config.evolution.llm_api_key,
            model=self._config.evolution.llm_model,
            base_url=self._config.evolution.llm_base_url,
        )
        backup_base_url = (
            str(self._config.evolution.llm_backup_base_url).strip()
            or str(self._config.evolution.llm_base_url).strip()
        )
        backup = self._build_m7_news_review_judge(
            provider=self._config.evolution.llm_backup_provider,
            api_key=self._config.evolution.llm_backup_api_key,
            model=self._config.evolution.llm_backup_model,
            base_url=backup_base_url,
        )
        return primary, backup

    def _build_m7_news_review_judge(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str,
    ) -> OpenAICompatibleNewsJudge | None:
        normalized_provider = str(provider).strip().lower()
        if normalized_provider not in {"openai", "openai_compatible"}:
            return None
        return OpenAICompatibleNewsJudge(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_sec=self._config.evolution.llm_timeout_sec,
            temperature=self._config.evolution.llm_temperature,
            max_tokens=max(120, self._config.evolution.llm_max_tokens),
        )

    def _enrich_m7_news_records_with_ai_review(
        self,
        *,
        records: list[dict[str, object]],
        enabled_override: bool | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        ai_enabled = (
            bool(enabled_override)
            if enabled_override is not None
            else bool(self._config.evolution.m7_ai_review_enabled)
        )
        primary_judge, backup_judge = self._build_m7_news_review_judges(
            enabled_override=ai_enabled,
        )
        primary_ready = bool(primary_judge is not None and primary_judge.configured)
        backup_ready = bool(backup_judge is not None and backup_judge.configured)
        summary: dict[str, object] = {
            "enabled": ai_enabled,
            "configured": primary_ready or backup_ready,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "primary_calls": 0,
            "backup_calls": 0,
            "fallback_used": 0,
        }
        if not ai_enabled:
            summary["reason"] = "disabled"
            return records, summary
        if not summary["configured"]:
            summary["reason"] = "missing_llm_credentials_or_model"
            return records, summary

        max_calls = max(0, int(self._config.evolution.m7_ai_review_max_items_per_run))
        if max_calls <= 0:
            summary["reason"] = "m7_ai_review_max_items_per_run<=0"
            return records, summary

        enriched = [dict(item) for item in records]
        for item in enriched[:max_calls]:
            summary["attempted"] = _as_int(summary.get("attempted"), default=0) + 1
            review = None
            if primary_ready and primary_judge is not None:
                summary["primary_calls"] = _as_int(summary.get("primary_calls"), default=0) + 1
                review = primary_judge.review(item)
                if review.error and backup_ready and backup_judge is not None:
                    summary["fallback_used"] = _as_int(summary.get("fallback_used"), default=0) + 1
                    summary["backup_calls"] = _as_int(summary.get("backup_calls"), default=0) + 1
                    review = backup_judge.review(item)
            elif backup_ready and backup_judge is not None:
                summary["backup_calls"] = _as_int(summary.get("backup_calls"), default=0) + 1
                review = backup_judge.review(item)
            if review is None or review.error:
                summary["failed"] = _as_int(summary.get("failed"), default=0) + 1
                continue
            sentiment = _clamp(review.sentiment, -1.0, 1.0)
            item["sentiment"] = sentiment
            item["llm_sentiment"] = sentiment
            item["llm_confidence"] = _clamp(review.confidence, 0.0, 1.0)
            item["llm_verdict"] = _m7_llm_verdict_from_sentiment(sentiment)
            item["llm_news_verdict"] = review.verdict
            item["llm_reason"] = review.reason
            summary["succeeded"] = _as_int(summary.get("succeeded"), default=0) + 1
        return enriched, summary

    def _merge_m7_news_records(
        self,
        *,
        current: list[dict[str, object]],
        existing: list[dict[str, object]],
        max_records: int,
    ) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for item in current + existing:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id", "")).strip()
            if not event_id:
                event_id = hashlib.sha256(
                    (
                        f"{item.get('symbol', '')}|{item.get('headline', '')}|"
                        f"{item.get('published_at', '')}"
                    ).encode()
                ).hexdigest()[:24]
                item = {**item, "event_id": event_id}
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            merged.append(dict(item))
        merged.sort(
            key=lambda item: (
                str(item.get("published_at", "")),
                str(item.get("symbol", "")),
                str(item.get("headline", "")),
            ),
            reverse=True,
        )
        return merged[: max(1, max_records)]

    def _persist_m7_news_records(
        self,
        *,
        artifact_path: Path,
        records: list[dict[str, object]],
    ) -> None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        with artifact_path.open("w", encoding="utf-8") as fp:
            for item in records:
                fp.write(json.dumps(item, ensure_ascii=False) + "\n")

    def news_score_history(
        self,
        limit: int = 50,
        symbol: str = "",
        strategy: str = "",
    ) -> dict[str, object]:
        return self._news_service.news_score_history(
            limit=limit,
            symbol=symbol,
            strategy=strategy,
        )

    def news_score_cache_state(self) -> dict[str, object]:
        return self._news_service.news_score_cache_state()

    def clear_news_score_cache(
        self,
        symbol: str = "",
        strategy: str = "",
    ) -> dict[str, object]:
        return self._news_service.clear_news_score_cache(symbol=symbol, strategy=strategy)

    def run_pipeline(
        self,
        symbols: list[str] | None = None,
        strategy: str = "trend",
        current_equity: float | None = None,
        use_live_runtime: bool = False,
        dry_run_execution: bool = False,
        notify_enabled: bool = True,
        job_name: str = "",
    ) -> dict[str, object]:
        advisory_only_mode = bool(self._config.app.advisory_only)
        execution_dry_run = bool(dry_run_execution)
        notifications_enabled = bool(notify_enabled)
        if advisory_only_mode:
            execution_mode = "advisory_only"
        elif execution_dry_run:
            execution_mode = "portfolio_auto_apply_dry_run"
        else:
            execution_mode = "portfolio_auto_apply"
        live_auto_execute = self._live_auto_execution_enabled(use_live_runtime=use_live_runtime)
        if self._bootstrap_runtime_blocked():
            self._last_signal_payload = []
            self._last_signal_trace_id = ""
            self._last_signal_timestamp = ""
            self._last_signal_source = ""
            self._last_signal_storage_source = ""
            self._last_signal_snapshot = None
            self._latest_signal_snapshot_dirty = False
            blocked_payload = {
                "trace_id": "",
                "timestamp": datetime.now().isoformat(),
                "degraded_mode": True,
                "risk": {"status": "blocked_bootstrap_required"},
                "signals": [],
                "portfolio_update": {},
                "actionable_signals": [],
                "week6_execution": {
                    "strategy": strategy.strip().lower() or "trend",
                    "summary": {"blocked": True},
                },
                "strategy_kill_switch": {
                    "enabled": bool(self._config.strategy_kill_switch.enabled),
                    "paused": bool(self._state.pause_new_buy),
                },
                "runtime": {"duration_ms": 0},
                "execution_mode": execution_mode,
                "status": "blocked_bootstrap_required",
                "bootstrap": self.training_bootstrap_status(),
            }
            self._record_audit_event(
                event_type="pipeline_blocked_bootstrap",
                level="warn",
                payload={"strategy": strategy, "bootstrap": blocked_payload["bootstrap"]},
            )
            return blocked_payload
        started = perf_counter()
        symbol_list = symbols if symbols is not None else list(self._state.watchlist)
        equity = current_equity if current_equity is not None else self._state.current_equity
        normalized_strategy = strategy.strip().lower() or "trend"
        trace_id = ""
        timestamp = datetime.now()
        degraded_mode = False
        risk_payload: dict[str, object] = {}
        signals: list[PipelineSignal] = []
        drawdown_pct = 0.0
        multi_run_summary: dict[str, object] | None = None
        week6_execution: dict[str, object]

        pipeline_started = perf_counter()
        if normalized_strategy == "multi":
            multi_payload = self._run_multi_strategy_pipeline(
                symbols=symbol_list,
                current_equity=equity,
                use_live_runtime=use_live_runtime,
            )
            trace_id = str(multi_payload.get("trace_id", ""))
            raw_timestamp = multi_payload.get("timestamp")
            if isinstance(raw_timestamp, datetime):
                timestamp = raw_timestamp
            degraded_mode = bool(multi_payload.get("degraded_mode", False))
            raw_risk_payload = multi_payload.get("risk_payload")
            if isinstance(raw_risk_payload, dict):
                risk_payload = raw_risk_payload
            raw_signals = multi_payload.get("signals")
            if isinstance(raw_signals, list):
                parsed_signals: list[PipelineSignal] = []
                for item in raw_signals:
                    if isinstance(item, PipelineSignal):
                        parsed_signals.append(item)
                signals = parsed_signals
            drawdown_pct = _as_float(multi_payload.get("drawdown_pct"), default=0.0)
            raw_summary = multi_payload.get("summary")
            if isinstance(raw_summary, dict):
                multi_run_summary = raw_summary
        else:
            pipeline = self._select_pipeline(use_live_runtime=use_live_runtime)
            report = pipeline.run_once(
                symbols=symbol_list,
                strategy=normalized_strategy,
                current_equity=equity,
            )
            trace_id = report.trace_id
            timestamp = report.timestamp
            degraded_mode = report.degraded_mode
            risk_payload = asdict(report.risk)
            signals = report.signals
            drawdown_pct = report.risk.drawdown_pct

        pipeline_ms = int((perf_counter() - pipeline_started) * 1000)
        post_pipeline_started = perf_counter()

        m1_negative_case = self._apply_m1_negative_case_constraints(
            signals=signals,
            strategy=normalized_strategy,
            use_live_runtime=use_live_runtime,
        )

        week6_execution = self._build_week6_execution_controls(
            strategy=normalized_strategy,
            symbols=symbol_list,
            drawdown_pct=drawdown_pct,
        )
        week6_summary = self._apply_week6_execution_controls(
            signals=signals,
            strategy=normalized_strategy,
            controls=week6_execution,
        )
        kill_switch_summary = self._apply_strategy_kill_switch(
            signals=signals,
            strategy=normalized_strategy,
        )
        if self._state.pause_new_buy:
            self._apply_new_buy_pause(signals)
        portfolio_constraints = self._apply_c3_portfolio_constraints(
            signals=signals,
            strategy=normalized_strategy,
        )
        if not execution_dry_run:
            self._sync_learning_snapshot_feedback(
                signals=signals,
                week6_execution=week6_execution,
            )
        hrp_shadow = self._build_c3_hrp_shadow_portfolio(
            signals=signals,
            strategy=normalized_strategy,
        )

        portfolio_update: Mapping[str, object]
        if advisory_only_mode:
            advisory_attempts = {
                "signals": len(signals),
                "buy_signals": sum(1 for signal in signals if signal.action == "buy"),
                "non_buy_signals": sum(1 for signal in signals if signal.action != "buy"),
                "buy_new_attempted": 0,
                "buy_new_filled": 0,
                "buy_new_rejected": 0,
                "pre_trade_blocked": 0,
                "risk_gate_blocked": 0,
                "sell_executed": 0,
            }
            portfolio_update = {
                "opened": 0,
                "adjusted": 0,
                "trimmed": 0,
                "closed_expired": 0,
                "skipped_max_holdings": 0,
                "skipped_same_sector": 0,
                "skipped_no_cash": 0,
                "open_positions": len(self._portfolio.positions()),
                "status": "skipped_advisory_only",
                "execution_attempts": {},
                "advisory_attempts": advisory_attempts,
                "executions": [],
            }
        elif live_auto_execute:
            portfolio_update = self._apply_live_auto_portfolio_signals(
                trace_id=trace_id,
                timestamp=timestamp,
                signals=signals,
                use_live_runtime=use_live_runtime,
                dry_run=execution_dry_run,
            )
        else:
            closed_expired_update = self._portfolio.apply_signals(
                trace_id=trace_id,
                timestamp=timestamp,
                signals=[],
            )
            portfolio_update = {
                **closed_expired_update,
                "trimmed": 0,
                "skipped_no_cash": 0,
                "open_positions": len(self._portfolio.positions()),
                "status": "deferred_non_live_runtime",
                "executions": [],
            }
        entry_day_symbols = _entry_day_symbols(
            positions=self._portfolio.positions(),
            now=timestamp,
        )
        actionable_signals = self._notification_filter.filter(
            signals,
            now=timestamp,
            entry_day_symbols=entry_day_symbols,
            trace_id=trace_id,
        )
        notification_filter_diagnostics = self._notification_filter.latest_diagnostics()
        if notification_filter_diagnostics is not None:
            self._last_notification_filter_diagnostics = dict(notification_filter_diagnostics)
        holding_alerts = self.holding_alerts(
            now=timestamp,
            persist_peak_state=not execution_dry_run,
        )
        if live_auto_execute:
            holding_alerts = self._suppress_holding_alerts_after_auto_exits(
                holding_alerts=holding_alerts,
                portfolio_update=portfolio_update,
            )
        signal_payload = self._build_signal_payload_with_recommendation_ids(
            signals=signals,
            trace_id=trace_id,
            timestamp=timestamp,
        )
        self._update_latest_signal_snapshot(
            signal_payload=signal_payload,
            trace_id=trace_id,
            timestamp=timestamp,
            source="pipeline_run",
        )
        recommendation_sync_started = perf_counter()
        if execution_dry_run:
            recommendation_update = {
                "updated": 0,
                "symbols": [],
                "status": "skipped_execution_dry_run",
            }
            learning_outcome_update = {
                "updated": 0,
                "snapshot_ids": [],
                "status": "skipped_execution_dry_run",
            }
            holding_recommendation_update = {
                "updated": 0,
                "symbols": [],
                "status": "skipped_execution_dry_run",
            }
            execution_recommendation_update = {
                "updated": 0,
                "symbols": [],
                "status": "skipped_execution_dry_run",
            }
        else:
            recommendation_update = self._sync_recommendation_lifecycle_from_signals(
                signals=signals,
                timestamp=timestamp,
                trace_id=trace_id,
            )
            learning_outcome_update = self._update_learning_outcomes_from_portfolio_update(
                signals=signals,
                portfolio_update=portfolio_update,
                timestamp=timestamp,
            )
            holding_recommendation_update = self._sync_recommendation_lifecycle_from_holding_alerts(
                holding_alerts=holding_alerts,
                timestamp=timestamp,
                trace_id=trace_id,
            )
            execution_recommendation_update = self._sync_recommendation_lifecycle_from_auto_execution(
                portfolio_update=portfolio_update,
                timestamp=timestamp,
                trace_id=trace_id,
            )
        recommendation_sync_ms = int((perf_counter() - recommendation_sync_started) * 1000)
        portfolio_changed = (
            _as_int(portfolio_update.get("opened"), default=0) > 0
            or _as_int(portfolio_update.get("adjusted"), default=0) > 0
            or _as_int(portfolio_update.get("trimmed"), default=0) > 0
            or _as_int(portfolio_update.get("closed_expired"), default=0) > 0
            or _as_int(portfolio_update.get("closed_signals"), default=0) > 0
        )
        runtime_state_persist_reasons: list[str] = []
        if self._latest_signal_snapshot_dirty:
            runtime_state_persist_reasons.append("latest_signals")
        if _as_int(recommendation_update.get("updated"), default=0) > 0:
            runtime_state_persist_reasons.append("recommendation_update")
        if _as_int(execution_recommendation_update.get("updated"), default=0) > 0:
            runtime_state_persist_reasons.append("execution_recommendation_update")
        if _as_int(holding_recommendation_update.get("updated"), default=0) > 0:
            runtime_state_persist_reasons.append("holding_recommendation_update")
        if portfolio_changed:
            runtime_state_persist_reasons.append("portfolio_changed")

        runtime_state_persist_ms = 0
        runtime_state_persist_bytes = 0
        if runtime_state_persist_reasons:
            persist_started = perf_counter()
            self._persist_runtime_state_to_disk(include_history_sidecars=False)
            self._latest_signal_snapshot_dirty = False
            runtime_state_persist_ms = int((perf_counter() - persist_started) * 1000)
            try:
                runtime_state_persist_bytes = self._runtime_state_path.stat().st_size
            except OSError:
                runtime_state_persist_bytes = 0
        post_pipeline_ms = int((perf_counter() - post_pipeline_started) * 1000)

        payload: dict[str, object] = {
            "trace_id": trace_id,
            "timestamp": timestamp.isoformat(),
            "degraded_mode": degraded_mode,
            "risk": risk_payload,
            "signals": signal_payload,
        }
        payload["portfolio_update"] = portfolio_update
        payload["actionable_signals"] = actionable_signals
        if notification_filter_diagnostics is not None:
            payload["notification_filter_diagnostics"] = notification_filter_diagnostics
        payload["job_name"] = job_name.strip() or "pipeline_run"
        payload["symbol_count"] = len(symbol_list)
        payload["use_live_runtime"] = bool(use_live_runtime)
        payload["dry_run_execution"] = execution_dry_run
        payload["notify_enabled"] = notifications_enabled
        payload["recommendation_update"] = recommendation_update
        payload["execution_recommendation_update"] = execution_recommendation_update
        payload["holding_recommendation_update"] = holding_recommendation_update
        if execution_dry_run or _as_int(learning_outcome_update.get("updated"), default=0) > 0:
            payload["learning_outcome_update"] = learning_outcome_update
        payload["holding_alerts"] = holding_alerts
        payload["execution_mode"] = execution_mode
        payload["m1_negative_case"] = m1_negative_case
        payload["portfolio_constraints"] = portfolio_constraints
        payload["hrp_shadow"] = hrp_shadow
        week6_payload: dict[str, object] = {
            "strategy": normalized_strategy,
            "threshold_shift": _as_float(week6_execution.get("threshold_shift"), default=0.0),
            "position_multiplier": _as_float(
                week6_execution.get("position_multiplier"),
                default=1.0,
            ),
            "regime": str(week6_execution.get("regime", "")),
            "global_risk_score": _as_float(week6_execution.get("global_risk_score"), default=50.0),
            "allocation_weights": week6_execution.get("allocation_weights", {}),
            "evolution_controls": week6_execution.get("evolution", {}),
            "threshold_shift_components": week6_execution.get("threshold_shift_components", {}),
            "position_multiplier_components": week6_execution.get(
                "position_multiplier_components",
                {},
            ),
            "summary": week6_summary,
        }
        if multi_run_summary is not None:
            week6_payload["multi_run"] = multi_run_summary
        payload["week6_execution"] = week6_payload
        payload["strategy_kill_switch"] = kill_switch_summary
        duration_ms = int((perf_counter() - started) * 1000)
        payload["runtime"] = {
            "duration_ms": duration_ms,
            "pipeline_ms": pipeline_ms,
            "post_pipeline_ms": post_pipeline_ms,
            "recommendation_sync_ms": recommendation_sync_ms,
            "runtime_state_persist_ms": runtime_state_persist_ms,
            "runtime_state_persist_count": 1 if runtime_state_persist_reasons else 0,
            "runtime_state_persist_reasons": runtime_state_persist_reasons,
            "runtime_state_persist_bytes": runtime_state_persist_bytes,
            "runtime_state_persist_enabled": bool(
                self._config.command_channel.state_persist_enabled
            ),
        }
        self._record_run_summary(
            report=payload,
            current_equity=equity,
            actionable_count=len(actionable_signals),
            duration_ms=duration_ms,
            job_name=job_name,
            strategy=normalized_strategy,
            symbol_count=len(symbol_list),
            use_live_runtime=use_live_runtime,
        )
        notify_started = perf_counter()
        if notifications_enabled and not execution_dry_run:
            self._notify_expired_position_exits_if_needed(
                timestamp=timestamp,
                closed_expired=_as_int(portfolio_update.get("closed_expired"), default=0),
                trace_id=trace_id,
            )
            self._notify_risk_status_if_needed(risk_payload, trace_id=trace_id)
            self._notify_holding_alerts_if_needed(holding_alerts=holding_alerts, trace_id=trace_id)
            self._notify_provider_health_if_needed(trace_id=trace_id)
            self._notify_simulated_trade_updates_if_needed(
                portfolio_update=portfolio_update,
                trace_id=trace_id,
            )
        runtime_payload = payload.get("runtime")
        if isinstance(runtime_payload, dict):
            runtime_payload["notify_ms"] = int((perf_counter() - notify_started) * 1000)
            runtime_payload["notify_enabled"] = notifications_enabled and not execution_dry_run
        self._record_audit_event(
            event_type="pipeline_run",
            trace_id=str(payload.get("trace_id", "")),
            payload={
                "strategy": normalized_strategy,
                "symbols": symbol_list,
                "job_name": payload["job_name"],
                "symbol_count": len(symbol_list),
                "use_live_runtime": bool(use_live_runtime),
                "dry_run_execution": execution_dry_run,
                "notify_enabled": notifications_enabled,
                "signals": _signals_count(payload),
                "actionable": len(actionable_signals),
                "notification_filter_diagnostics": notification_filter_diagnostics or {},
                "duration_ms": duration_ms,
                "runtime": payload["runtime"],
                "execution_mode": execution_mode,
                "portfolio_update": _audit_portfolio_update_summary(portfolio_update),
                "week6_execution": payload["week6_execution"],
                "strategy_kill_switch": payload["strategy_kill_switch"],
            },
        )
        return payload

    def run_due_jobs(
        self,
        now: datetime | None = None,
        only_jobs: list[str] | None = None,
    ) -> list[dict[str, object]]:
        self._refresh_runtime_state_from_disk_if_changed()
        current = now or datetime.now()
        selectors = [str(item).strip() for item in (only_jobs or []) if str(item).strip()]
        retry_result: dict[str, object] | None = None
        if self._bootstrap_runtime_blocked():
            if selectors:
                self._record_audit_event(
                    event_type="scheduler_blocked_bootstrap",
                    level="warn",
                    payload={
                        "selected_jobs": selectors,
                        "bootstrap": self.training_bootstrap_status(),
                    },
                )
                return [
                    {
                        "job": "bootstrap_gate",
                        "ran": False,
                        "success": False,
                        "detail": "blocked_bootstrap_required",
                        "payload": {
                            "selected_jobs": selectors,
                            "bootstrap": self.training_bootstrap_status(),
                        },
                    }
                ]
            retry_result = self._maybe_retry_bootstrap_when_blocked(now=current)
            if self._bootstrap_runtime_blocked():
                self._record_audit_event(
                    event_type="scheduler_blocked_bootstrap",
                    level="warn",
                    payload=self.training_bootstrap_status(),
                )
                blocked = {
                    "job": "bootstrap_gate",
                    "ran": False,
                    "success": False,
                    "detail": "blocked_bootstrap_required",
                    "payload": {"bootstrap": self.training_bootstrap_status()},
                }
                return [blocked, retry_result] if retry_result is not None else [blocked]
        scheduler_state_before = self._scheduler.export_state()
        previous_scheduler_now = self._scheduler_now_context
        self._scheduler_now_context = current
        try:
            results = [
                result.to_dict()
                for result in self._scheduler.run_due(now=current, only_jobs=selectors or None)
            ]
        finally:
            self._scheduler_now_context = previous_scheduler_now
        scheduler_state_after = self._scheduler.export_state()
        if scheduler_state_after != scheduler_state_before:
            self._persist_runtime_state_to_disk()
        if retry_result is not None:
            return [retry_result, *results]
        return results

    def _job_now(self) -> datetime:
        return self._scheduler_now_context or datetime.now()

    def _runtime_mode(self) -> str:
        return str(self._config.app.mode).strip().lower()

    def _idle_policy_modes(self, raw_modes: list[str], default_modes: set[str]) -> set[str]:
        return self._idle_queue_service._idle_policy_modes(
            raw_modes,
            default_modes,
        )

    def _idle_production_canary_hit(self) -> tuple[bool, str]:
        return self._idle_queue_service._idle_production_canary_hit()

    def _idle_policy_switch(
        self,
        *,
        configured: bool,
        policy_raw: str,
        modes: set[str],
        flag_name: str,
    ) -> tuple[bool, str]:
        return self._idle_queue_service._idle_policy_switch(
            configured=configured,
            policy_raw=policy_raw,
            modes=modes,
            flag_name=flag_name,
        )

    def _resolve_idle_queue_enabled(self) -> tuple[bool, str]:
        return self._idle_queue_service._resolve_idle_queue_enabled()

    def _resolve_idle_queue_auto_run(self) -> tuple[bool, str]:
        return self._idle_queue_service._resolve_idle_queue_auto_run()

    def latest_idle_queue_report(self) -> dict[str, object] | None:
        return self._idle_queue_service.latest_idle_queue_report()

    def latest_tdx_sync_report(self) -> dict[str, object] | None:
        """Return latest TongDaXin offline sync report."""
        return self._market_sync_service.latest_tdx_sync_report()

    def tdx_sync_history(self, limit: int = 20) -> dict[str, object]:
        """Return recent TongDaXin offline sync reports."""
        return self._market_sync_service.tdx_sync_history(limit=limit)

    def latest_market_warehouse_report(self) -> dict[str, object] | None:
        """Return latest market-warehouse sync report."""
        return self._market_sync_service.latest_market_warehouse_report()

    def market_warehouse_history(self, limit: int = 20) -> dict[str, object]:
        """Return recent market-warehouse sync reports."""
        return self._market_sync_service.market_warehouse_history(limit=limit)

    def latest_market_warehouse_progress(self) -> dict[str, object] | None:
        """Return latest market-warehouse sync progress snapshot."""
        return self._market_sync_service.latest_market_warehouse_progress()

    def market_warehouse_sync_lock_status(self) -> dict[str, object]:
        return self._market_sync_service.market_warehouse_sync_lock_status()

    def market_warehouse_background_data_status(self) -> dict[str, object]:
        return self._market_sync_service.market_warehouse_background_data_status()

    def market_warehouse_runtime_status(self) -> dict[str, object]:
        return self._market_sync_service.market_warehouse_runtime_status()

    def latest_post_market_warehouse_followup_state(self) -> dict[str, object] | None:
        return self._load_json_mapping_file(self._post_market_warehouse_followup_state_path)

    def latest_post_market_warehouse_followup_result(self) -> dict[str, object] | None:
        return self._load_json_mapping_file(self._post_market_warehouse_followup_result_path)

    def _load_json_mapping_file(self, path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, Mapping):
            return dict(payload)
        return None

    def idle_queue_history(self, limit: int = 20) -> dict[str, object]:
        return self._idle_queue_service.idle_queue_history(limit)

    def idle_queue_state(self) -> dict[str, object]:
        return self._idle_queue_service.idle_queue_state()

    def idle_queue_ack_blocked(
        self,
        task_id: str = "",
        clear_all: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        return self._idle_queue_service.idle_queue_ack_blocked(
            task_id,
            clear_all,
            now,
        )

    def _idle_task_health_snapshot(self) -> list[dict[str, object]]:
        return self._idle_queue_service._idle_task_health_snapshot()

    def _idle_notification_template(
        self,
        event: str,
        payload: dict[str, object],
    ) -> tuple[str, str, str]:
        return self._idle_queue_service._idle_notification_template(
            event,
            payload,
        )

    def _idle_emit_state_notification(
        self,
        title: str,
        content: str,
        level: str = "warn",
        now: datetime | None = None,
    ) -> None:
        return self._idle_queue_service._idle_emit_state_notification(
            title,
            content,
            level,
            now,
        )

    def run_idle_queue_cycle(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._idle_queue_service.run_idle_queue_cycle(
            now,
            source_trace_id,
        )

    def _idle_refresh_pause_state(
        self,
        now: datetime,
        capacity_metrics: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        return self._idle_queue_service._idle_refresh_pause_state(
            now,
            capacity_metrics,
            context,
        )

    def _idle_task_retry_policy(self, task_id: str) -> dict[str, object]:
        return self._idle_queue_service._idle_task_retry_policy(task_id)

    def _idle_error_code(self, result: dict[str, object], timed_out: bool = False) -> str:
        return self._idle_queue_service._idle_error_code(
            result,
            timed_out,
        )

    def _idle_should_retry(
        self,
        status: str,
        error_code: str,
        attempt_index: int,
        retry_policy: dict[str, object],
    ) -> bool:
        return self._idle_queue_service._idle_should_retry(
            status,
            error_code,
            attempt_index,
            retry_policy,
        )

    def _idle_timeout_partial_report(
        self,
        task_id: str,
        context: dict[str, object],
        elapsed_seconds: float,
        max_wall_minutes: int,
        attempts: list[dict[str, object]],
    ) -> str:
        return self._idle_queue_service._idle_timeout_partial_report(
            task_id,
            context,
            elapsed_seconds,
            max_wall_minutes,
            attempts,
        )

    def _idle_run_task_with_timeout(
        self,
        task_id: str,
        context: dict[str, object],
        timeout_seconds: float | None,
    ) -> tuple[dict[str, object], bool, float]:
        return self._idle_queue_service._idle_run_task_with_timeout(
            task_id,
            context,
            timeout_seconds,
        )

    def _idle_execute_task_with_policy(
        self,
        task_id: str,
        context: dict[str, object],
    ) -> dict[str, object]:
        return self._idle_queue_service._idle_execute_task_with_policy(
            task_id,
            context,
        )

    def _idle_update_wd_report_kpi(
        self,
        context: dict[str, object],
        result: dict[str, object],
    ) -> None:
        return self._idle_queue_service._idle_update_wd_report_kpi(
            context,
            result,
        )

    def _store_idle_report(self, report: dict[str, object]) -> None:
        return self._idle_queue_service._store_idle_report(report)

    def _load_idle_history_from_disk(self) -> None:
        return self._idle_queue_service._load_idle_history_from_disk()

    def _persist_idle_report_to_disk(self, report: dict[str, object]) -> None:
        return self._idle_queue_service._persist_idle_report_to_disk(report)

    def _load_tdx_sync_history_from_disk(self) -> None:
        self._market_sync_service._load_tdx_sync_history_from_disk()

    def _persist_tdx_sync_history_to_disk(self) -> None:
        self._market_sync_service._persist_tdx_sync_history_to_disk()

    def _load_market_warehouse_history_from_disk(self) -> None:
        self._market_sync_service._load_market_warehouse_history_from_disk()

    def _persist_market_warehouse_history_to_disk(self) -> None:
        self._market_sync_service._persist_market_warehouse_history_to_disk()

    def _load_market_warehouse_progress_from_disk(self) -> None:
        self._market_sync_service._load_market_warehouse_progress_from_disk()

    def _persist_market_warehouse_progress_to_disk(self) -> None:
        self._market_sync_service._persist_market_warehouse_progress_to_disk()

    def _idle_check_time_guard_sync(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_check_time_guard_sync(context)

    def _idle_collect_capacity_metrics(
        self,
        context: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        return self._idle_queue_service._idle_collect_capacity_metrics(
            context,
            now,
        )

    def _build_idle_context(self, now: datetime) -> dict[str, object]:
        return self._idle_queue_service._build_idle_context(now)

    def _build_idle_task_manifests(self) -> dict[str, dict[str, object]]:
        return self._idle_queue_service._build_idle_task_manifests()

    def _idle_prepare_weekend_cycle_state(self, trade_date: str) -> None:
        return self._idle_queue_service._idle_prepare_weekend_cycle_state(trade_date)

    def _idle_weekend_due_tasks(self, trade_date: str, now_clock: datetime) -> list[str]:
        return self._idle_queue_service._idle_weekend_due_tasks(
            trade_date,
            now_clock,
        )

    def _idle_weekend_sorted_p1_tasks(self) -> list[str]:
        return self._idle_queue_service._idle_weekend_sorted_p1_tasks()

    def _idle_weekend_remaining_minutes(self, now_clock: datetime) -> int:
        return self._idle_queue_service._idle_weekend_remaining_minutes(now_clock)

    def _idle_should_force_weekend_task(self, task_id: str) -> bool:
        return self._idle_queue_service._idle_should_force_weekend_task(task_id)

    def _idle_record_weekend_defer(self, task_id: str, trade_date: str) -> None:
        return self._idle_queue_service._idle_record_weekend_defer(
            task_id,
            trade_date,
        )

    def _idle_latest_trade_date_for_task(self, task_id: str) -> str:
        return self._idle_queue_service._idle_latest_trade_date_for_task(task_id)

    def _idle_due_tasks(self, context: dict[str, object]) -> list[str]:
        return self._idle_queue_service._idle_due_tasks(context)

    def _idle_already_ran(self, task_id: str, trade_date: str) -> bool:
        return self._idle_queue_service._idle_already_ran(
            task_id,
            trade_date,
        )

    def _idle_set_task_status(self, task_id: str, trade_date: str, status: str) -> None:
        return self._idle_queue_service._idle_set_task_status(
            task_id,
            trade_date,
            status,
        )

    def _idle_get_task_status(self, task_id: str, trade_date: str) -> str:
        return self._idle_queue_service._idle_get_task_status(
            task_id,
            trade_date,
        )

    def _idle_mark_ran(self, task_id: str, trade_date: str) -> None:
        return self._idle_queue_service._idle_mark_ran(
            task_id,
            trade_date,
        )

    def _run_idle_task(self, task_id: str, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._run_idle_task(
            task_id,
            context,
        )

    def _idle_task_wd_p0_01(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_p0_01(context)

    def _idle_task_wd_p0_02(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_p0_02(context)

    def _idle_task_wd_p0_03(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_p0_03(context)

    def _idle_task_wd_p0_04(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_p0_04(context)

    def _idle_task_wd_p1_05(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_p1_05(context)

    def _idle_task_wd_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_p1_06(context)

    def _idle_task_wd_p1_07(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_p1_07(context)

    def _idle_validate_precompute_cache(
        self,
        *,
        path: Path,
        expected_trade_date: str,
        now: datetime,
    ) -> dict[str, object]:
        return self._idle_queue_service._idle_validate_precompute_cache(
            path=path,
            expected_trade_date=expected_trade_date,
            now=now,
        )

    def _idle_task_wd_report(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_wd_report(context)

    def _idle_task_we_p0_01(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p0_01(context)

    def _idle_task_we_p0_02(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p0_02(context)

    def _idle_task_we_learn_01(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_learn_01(context)

    def _idle_task_we_p1_03(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p1_03(context)

    def _idle_task_we_p1_04(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p1_04(context)

    def _idle_task_we_p1_05(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p1_05(context)

    def _idle_task_we_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p1_06(context)

    def _idle_task_we_p1_07(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p1_07(context)

    def _idle_task_we_p2_08(self, context: dict[str, object]) -> dict[str, object]:
        return self._idle_queue_service._idle_task_we_p2_08(context)

    def _idle_update_task_health(
        self,
        task_id: str,
        status: str,
        now: datetime | None = None,
    ) -> None:
        return self._idle_queue_service._idle_update_task_health(
            task_id,
            status,
            now,
        )

    def _idle_task_ttl(self, task_id: str) -> int:
        return self._idle_queue_service._idle_task_ttl(task_id)

    def _idle_effective_write_whitelist(self, task_id: str) -> list[dict[str, object]]:
        return self._idle_queue_service._idle_effective_write_whitelist(task_id)

    def _idle_path_within(self, path: Path, root: Path) -> bool:
        return self._idle_queue_service._idle_path_within(
            path,
            root,
        )

    def _idle_whitelist_hit(self, task_id: str, path: Path, action: str) -> bool:
        return self._idle_queue_service._idle_whitelist_hit(
            task_id,
            path,
            action,
        )

    def _idle_forbidden_hit(self, path: Path) -> bool:
        return self._idle_queue_service._idle_forbidden_hit(path)

    def _idle_assert_write_allowed(self, task_id: str, path: Path, action: str) -> None:
        return self._idle_queue_service._idle_assert_write_allowed(
            task_id,
            path,
            action,
        )

    def _idle_infer_task_id_from_output_path(self, path: Path) -> str:
        return self._idle_queue_service._idle_infer_task_id_from_output_path(path)

    def _idle_validate_relative_fragment(self, fragment: str, label: str) -> str:
        return self._idle_queue_service._idle_validate_relative_fragment(
            fragment,
            label,
        )

    def _idle_output_dir(self, trade_date: str, task_id: str, subdir: str = "") -> Path:
        return self._idle_queue_service._idle_output_dir(
            trade_date,
            task_id,
            subdir,
        )

    def _idle_output_path(
        self,
        trade_date: str,
        task_id: str,
        subdir: str,
        filename: str,
    ) -> Path:
        return self._idle_queue_service._idle_output_path(
            trade_date,
            task_id,
            subdir,
            filename,
        )

    def _idle_write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        return self._idle_queue_service._idle_write_json(
            path,
            payload,
        )

    def _idle_write_text(self, path: Path, payload: str) -> None:
        return self._idle_queue_service._idle_write_text(
            path,
            payload,
        )

    def _idle_write_checkpoint(
        self,
        task_id: str,
        trade_date: str,
        phase: str,
        now: datetime,
        extra: dict[str, object],
    ) -> None:
        return self._idle_queue_service._idle_write_checkpoint(
            task_id,
            trade_date,
            phase,
            now,
            extra,
        )

    def _idle_enforce_checkpoint_retention(self, directory: Path, task_id: str) -> None:
        return self._idle_queue_service._idle_enforce_checkpoint_retention(
            directory,
            task_id,
        )

    def _idle_find_latest_task_report(
        self,
        task_id: str,
        subdir: str,
        filename: str,
        exclude_trade_date: str,
    ) -> dict[str, object] | None:
        return self._idle_queue_service._idle_find_latest_task_report(
            task_id,
            subdir,
            filename,
            exclude_trade_date,
        )

    def _default_runtime_state_payload(self) -> dict[str, object]:
        return self._runtime_state_service._default_runtime_state_payload()

    def _runtime_state_optional_dict(self, raw: object) -> dict[str, object] | None:
        return self._runtime_state_service._runtime_state_optional_dict(raw)

    def _runtime_state_dict_list(
        self,
        raw: object,
        *,
        limit: int,
    ) -> list[dict[str, object]]:
        return self._runtime_state_service._runtime_state_dict_list(raw, limit=limit)

    def _runtime_state_latest_from_raw(
        self,
        raw_latest: object,
        history: list[dict[str, object]],
    ) -> dict[str, object] | None:
        return self._runtime_state_service._runtime_state_latest_from_raw(
            raw_latest,
            history,
        )

    def _persist_runtime_state_to_disk(self, *, include_history_sidecars: bool = True) -> None:
        with self._runtime_state_io_lock:
            self._runtime_state_service._persist_runtime_state_to_disk(
                include_history_sidecars=include_history_sidecars,
            )

    def _load_runtime_state_from_disk(self) -> None:
        with self._runtime_state_io_lock:
            self._runtime_state_service._load_runtime_state_from_disk()

    def _refresh_runtime_state_from_disk_if_changed(self) -> None:
        with self._runtime_state_io_lock:
            self._runtime_state_service._refresh_runtime_state_from_disk_if_changed()

    def _reload_runtime_state_from_disk(self) -> None:
        with self._runtime_state_io_lock:
            self._runtime_state_service._load_runtime_state_from_disk()

    def _refresh_cloud_backup_state_from_disk(self) -> None:
        with self._runtime_state_io_lock:
            self._runtime_state_service._refresh_cloud_backup_state_from_disk()

    def _merge_runtime_state_payload(
        self,
        existing_raw: object,
        current_raw: dict[str, object],
    ) -> dict[str, object]:
        return self._runtime_state_service._merge_runtime_state_payload(
            existing_raw,
            current_raw,
        )

    def _merge_runtime_state_scheduler(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, object]:
        return self._runtime_state_service._merge_runtime_state_scheduler(
            existing_raw,
            current_raw,
        )

    def _merge_runtime_state_watchlist(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> list[str]:
        if isinstance(current_raw, list):
            current = [
                symbol
                for symbol in (_normalize_a_share_symbol(item) for item in current_raw)
                if symbol
            ]
            if current:
                return _dedupe_preserve_order(current)

        existing = (
            [
                symbol
                for symbol in (_normalize_a_share_symbol(item) for item in existing_raw)
                if symbol
            ]
            if isinstance(existing_raw, list)
            else []
        )
        return _dedupe_preserve_order(existing)

    def _merge_runtime_state_portfolio(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, object]:
        return self._runtime_state_service._merge_runtime_state_portfolio(
            existing_raw,
            current_raw,
        )

    def _merge_runtime_state_numeric_mapping(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, float]:
        return self._runtime_state_service._merge_runtime_state_numeric_mapping(
            existing_raw,
            current_raw,
        )

    def _load_runtime_state_numeric_mapping(
        self,
        raw: object,
    ) -> dict[str, float]:
        return self._runtime_state_service._load_runtime_state_numeric_mapping(raw)

    def _merge_runtime_state_mapping(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, dict[str, object]]:
        return self._runtime_state_service._merge_runtime_state_mapping(
            existing_raw,
            current_raw,
        )

    def _merge_runtime_state_latest(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, object] | None:
        return self._runtime_state_service._merge_runtime_state_latest(
            existing_raw,
            current_raw,
        )

    def _merge_runtime_state_history(
        self,
        existing_raw: object,
        current_raw: object,
        *,
        limit: int,
        identity_keys: tuple[str, ...],
    ) -> list[dict[str, object]]:
        return self._runtime_state_service._merge_runtime_state_history(
            existing_raw,
            current_raw,
            limit=limit,
            identity_keys=identity_keys,
        )

    def _runtime_state_record_key(
        self,
        item: dict[str, object],
        identity_keys: tuple[str, ...],
    ) -> str:
        return self._runtime_state_service._runtime_state_record_key(item, identity_keys)

    def _replace_watchlist(self, symbols: list[str], reason: str) -> dict[str, object]:
        normalized = [
            symbol for symbol in (_normalize_a_share_symbol(item) for item in symbols) if symbol
        ]
        deduped = _dedupe_preserve_order(normalized)
        before = list(self._state.watchlist)
        if not deduped:
            return {
                "updated": False,
                "reason": "empty_symbols",
                "watchlist_before": len(before),
                "watchlist_after": len(before),
            }
        changed = deduped != before
        if changed:
            self._state.watchlist = deduped
            self._persist_runtime_state_to_disk()
        return {
            "updated": changed,
            "reason": reason,
            "watchlist_before": len(before),
            "watchlist_after": len(self._state.watchlist),
            "symbols": list(self._state.watchlist),
        }

    def _default_training_bootstrap_state(self) -> dict[str, object]:
        return self._training_service._default_training_bootstrap_state()

    def _load_training_bootstrap_state(self) -> dict[str, object]:
        return self._training_service._load_training_bootstrap_state()

    def _persist_training_bootstrap_state(self, payload: dict[str, object]) -> None:
        self._training_service._persist_training_bootstrap_state(payload)

    def _reconcile_training_bootstrap_state_with_artifact(self) -> None:
        self._training_service._reconcile_training_bootstrap_state_with_artifact()

    def training_bootstrap_status(self) -> dict[str, object]:
        return self._training_service.training_bootstrap_status()

    def _bootstrap_runtime_blocked(self) -> bool:
        return self._training_service._bootstrap_runtime_blocked()

    def _maybe_retry_bootstrap_when_blocked(self, now: datetime) -> dict[str, object] | None:
        return self._training_service._maybe_retry_bootstrap_when_blocked(now)

    def _run_bootstrap_retry(self, now: datetime, source: str) -> dict[str, object]:
        return self._training_service._run_bootstrap_retry(now, source)

    def _maybe_auto_bootstrap_training_on_first_start(self) -> None:
        self._training_service._maybe_auto_bootstrap_training_on_first_start()

    def _maybe_seed_watchlist_after_bootstrap(self) -> None:
        self._training_service._maybe_seed_watchlist_after_bootstrap()

    def _import_akshare(self) -> Any:
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("akshare_unavailable") from exc
        return ak

    def _fetch_a_share_universe_from_akshare(self) -> list[str]:
        ak = self._import_akshare()
        frame: pd.DataFrame | None = None
        errors: list[str] = []
        for func_name in ("stock_zh_a_spot_em", "stock_zh_a_spot"):
            func = getattr(ak, func_name, None)
            if not callable(func):
                continue
            try:
                candidate = func()
            except Exception as exc:
                errors.append(f"{func_name}:{exc.__class__.__name__}")
                continue
            if isinstance(candidate, pd.DataFrame) and not candidate.empty:
                frame = candidate
                break
        if frame is None or frame.empty:
            detail = ",".join(errors)
            raise RuntimeError(f"akshare_spot_empty:{detail}" if detail else "akshare_spot_empty")
        symbols = _filter_supported_universe_symbols(_extract_a_share_symbols_from_frame(frame))
        if not symbols:
            raise RuntimeError("akshare_spot_no_symbols")
        return symbols

    def _fetch_a_share_universe_from_efinance(self) -> list[str]:
        try:
            import efinance as ef  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("efinance_unavailable") from exc
        try:
            frame = ef.stock.get_realtime_quotes()
        except Exception as exc:
            raise RuntimeError("efinance_spot_failed") from exc
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise RuntimeError("efinance_spot_empty")
        symbols = _filter_supported_universe_symbols(_extract_a_share_symbols_from_frame(frame))
        if not symbols:
            raise RuntimeError("efinance_spot_no_symbols")
        return symbols

    def _preferred_universe_spot_sources(
        self,
    ) -> list[tuple[str, str, Callable[[], list[str]]]]:
        primary = str(self._config.data_source.primary).strip().lower()
        if primary in {"efinance", "ef"}:
            return [
                ("efinance_spot", "efinance", self._fetch_a_share_universe_from_efinance),
                ("akshare_spot", "akshare", self._fetch_a_share_universe_from_akshare),
            ]
        return [
            ("akshare_spot", "akshare", self._fetch_a_share_universe_from_akshare),
            ("efinance_spot", "efinance", self._fetch_a_share_universe_from_efinance),
        ]

    def _prefer_local_symbol_universe(self) -> bool:
        primary = str(self._config.data_source.primary).strip().lower()
        local_root = str(self._config.data_source.local_data_root).strip()
        warehouse_root = str(self._config.market_warehouse.package_root).strip()
        effective_local_root = local_root or warehouse_root
        return primary in {
            "market_warehouse",
            "warehouse",
            "warehouse_offline",
            "tdx_offline",
            "offline",
            "local",
        } and bool(effective_local_root)

    def _format_symbol_display(self, symbol: str, name: str = "") -> str:
        normalized_symbol = _normalize_a_share_symbol(symbol) or str(symbol).strip()
        resolved_name = str(name).strip()
        if normalized_symbol and not resolved_name:
            resolved_name = self._resolve_symbol_display_name(normalized_symbol)
        if normalized_symbol and resolved_name and resolved_name != normalized_symbol:
            return f"{normalized_symbol} {resolved_name}"
        return normalized_symbol or resolved_name

    def _resolve_symbol_display_name(self, symbol: str) -> str:
        normalized_symbol = _normalize_a_share_symbol(symbol)
        if not normalized_symbol:
            return ""
        cache_key = f"symbol-display-name:{normalized_symbol}"
        cached_name = str(self._cache.get(cache_key) or "").strip()
        if cached_name:
            return cached_name

        resolved_name = self._resolve_symbol_display_name_from_provider(normalized_symbol)
        if not resolved_name:
            resolved_name = self._resolve_symbol_display_name_from_catalog(normalized_symbol)
        if resolved_name:
            self._cache.set(cache_key, resolved_name, ttl_sec=7 * 24 * 3600)
        return resolved_name

    def _resolve_symbol_display_name_from_provider(self, symbol: str) -> str:
        seen_provider_ids: set[int] = set()
        for provider in (self._provider, self._realtime_provider):
            if provider is None:
                continue
            provider_id = id(provider)
            if provider_id in seen_provider_ids:
                continue
            seen_provider_ids.add(provider_id)
            try:
                bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=5)
            except Exception:
                continue
            if not isinstance(bars, pd.DataFrame) or bars.empty:
                continue
            for column in ("name", "stock_name", "symbol_name"):
                if column not in bars.columns:
                    continue
                for value in reversed(bars[column].tolist()):
                    candidate = str(value).strip()
                    if candidate and candidate.lower() != "nan":
                        return candidate
        return ""

    def _resolve_symbol_display_name_from_catalog(self, symbol: str) -> str:
        ak = self._import_akshare()
        if ak is None:
            return ""
        for func_name in (
            "stock_info_a_code_name",
            "stock_info_sh_name_code",
            "stock_info_sz_name_code",
        ):
            func = getattr(ak, func_name, None)
            if not callable(func):
                continue
            try:
                frame = func()
            except Exception:
                continue
            mapping = _parse_symbol_name_mapping(frame)
            candidate = mapping.get(symbol, "")
            if candidate:
                return candidate
        return ""

    def _fetch_a_share_universe_catalog_from_akshare(self) -> list[str]:
        ak = self._import_akshare()
        candidates: list[str] = []
        function_names = [
            "stock_info_a_code_name",
            "stock_info_sh_name_code",
            "stock_info_sz_name_code",
        ]
        for func_name in function_names:
            func = getattr(ak, func_name, None)
            if not callable(func):
                continue
            try:
                frame = func()
            except Exception:
                continue
            if frame is None or frame.empty:
                continue
            for col in frame.columns:
                for value in frame[col].tolist():
                    normalized = _normalize_a_share_symbol(value)
                    if normalized:
                        candidates.append(normalized)
        deduped = _filter_supported_universe_symbols(candidates)
        if not deduped:
            raise RuntimeError("akshare_catalog_empty")
        return deduped

    def _load_symbol_universe_from_local_files(self, cap: int = 12000) -> list[str]:
        roots: list[Path] = []
        configured_root = str(self._config.data_source.local_data_root).strip()
        if configured_root:
            base = Path(configured_root)
            roots.extend([base, base / "bars"])
        warehouse_root = str(self._config.market_warehouse.package_root).strip()
        if warehouse_root:
            base = Path(warehouse_root)
            roots.extend([base, base / "bars"])
        roots.extend(
            [
                self._evolution_project_root / "data" / "bars",
                self._evolution_project_root / "bars",
            ]
        )

        symbols: list[str] = []
        visited: set[Path] = set()
        for root in roots:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved in visited or not resolved.exists() or not resolved.is_dir():
                continue
            visited.add(resolved)
            try:
                iterator = resolved.rglob("*")
            except OSError:
                continue
            for path in iterator:
                if not path.is_file():
                    continue
                suffixes = "".join(path.suffixes).lower()
                if suffixes not in {".csv", ".csv.gz", ".parquet"}:
                    continue
                normalized = _normalize_a_share_symbol(path.name)
                if not normalized:
                    normalized = _normalize_a_share_symbol(path.stem)
                if not normalized or not _is_supported_universe_symbol(normalized):
                    continue
                symbols.append(normalized)
                if len(symbols) >= max(1, cap):
                    return _dedupe_preserve_order(symbols)
        return _filter_supported_universe_symbols(symbols)

    def _bootstrap_seed_symbols(self, cap: int = 200) -> list[str]:
        configured = [
            _normalize_a_share_symbol(item) for item in self._config.training.bootstrap_seed_symbols
        ]
        symbols = [item for item in configured if item]
        if not symbols:
            return []
        return _dedupe_preserve_order(symbols)[: max(1, cap)]

    def _load_symbol_universe_cache(
        self,
        *,
        max_age_hours: int,
        allow_stale: bool,
    ) -> list[str]:
        path = self._universe_cache_path
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        generated_at = _parse_iso_datetime(payload.get("generated_at"))
        if generated_at is not None and not allow_stale:
            if datetime.now() - generated_at > timedelta(hours=max(1, max_age_hours)):
                return []
        raw_symbols = payload.get("symbols")
        if not isinstance(raw_symbols, list):
            return []
        return _filter_supported_universe_symbols(raw_symbols)

    def _persist_symbol_universe_cache(self, symbols: list[str], source: str) -> None:
        path = self._universe_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(),
            "source": source,
            "count": len(symbols),
            "symbols": symbols,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _resolve_symbol_universe(
        self,
        *,
        max_symbols: int | None = None,
        min_symbols: int | None = None,
        allow_seed_fallback: bool = False,
        allow_online_sources: bool = True,
    ) -> dict[str, object]:
        min_required = max(
            1,
            min_symbols
            if min_symbols is not None
            else _as_int(self._config.idle_queue.universe_min_symbols, default=500),
        )
        cap = _as_int(max_symbols, default=0) if max_symbols is not None else 0
        errors: list[str] = []
        source = "empty"
        selected: list[str] = []
        provisional_symbols: list[str] = []
        provisional_source = ""
        prefer_local = self._prefer_local_symbol_universe()

        if prefer_local:
            local_primary_symbols = self._load_symbol_universe_from_local_files()
            if len(local_primary_symbols) >= min_required:
                selected = local_primary_symbols
                source = "local_files_primary"
                self._persist_symbol_universe_cache(selected, source=source)
            elif len(local_primary_symbols) > len(provisional_symbols):
                provisional_symbols = local_primary_symbols
                provisional_source = "local_files_primary"
                errors.append(
                    f"local_files_universe_too_small:{len(local_primary_symbols)}<{min_required}"
                )

        if not selected and allow_online_sources:
            for spot_source, error_prefix, fetcher in self._preferred_universe_spot_sources():
                if selected:
                    break
                try:
                    symbols = fetcher()
                    if len(symbols) >= min_required:
                        selected = symbols
                        source = spot_source
                        self._persist_symbol_universe_cache(selected, source=source)
                    elif len(symbols) > len(provisional_symbols):
                        provisional_symbols = symbols
                        provisional_source = spot_source
                        errors.append(
                            f"{error_prefix}_universe_too_small:{len(symbols)}<{min_required}"
                        )
                except Exception as exc:
                    errors.append(f"{error_prefix}_fetch_failed:{exc.__class__.__name__}")

            if not selected:
                try:
                    catalog_symbols = self._fetch_a_share_universe_catalog_from_akshare()
                    if len(catalog_symbols) >= min_required:
                        selected = catalog_symbols
                        source = "akshare_catalog"
                        self._persist_symbol_universe_cache(selected, source=source)
                    elif len(catalog_symbols) > len(provisional_symbols):
                        provisional_symbols = catalog_symbols
                        provisional_source = "akshare_catalog"
                        errors.append(
                            f"akshare_catalog_too_small:{len(catalog_symbols)}<{min_required}"
                        )
                except Exception as exc:
                    errors.append(f"akshare_catalog_failed:{exc.__class__.__name__}")
        elif not allow_online_sources:
            errors.append("online_sources_disabled")

        if not selected:
            cached = self._load_symbol_universe_cache(
                max_age_hours=self._config.idle_queue.universe_cache_max_age_hours,
                allow_stale=False,
            )
            if cached:
                selected = cached
                source = "cache_fresh"

        if not selected:
            stale_cached = self._load_symbol_universe_cache(
                max_age_hours=self._config.idle_queue.universe_cache_max_age_hours,
                allow_stale=True,
            )
            if stale_cached:
                selected = stale_cached
                source = "cache_stale"

        if not selected and provisional_symbols:
            selected = provisional_symbols
            source = provisional_source or "akshare_partial"

        if not selected:
            watchlist_symbols = [
                normalized
                for normalized in (
                    _normalize_a_share_symbol(item) for item in self._state.watchlist
                )
                if normalized
            ]
            if watchlist_symbols:
                selected = _dedupe_preserve_order(watchlist_symbols)
                source = "watchlist_fallback"
                errors.append("fallback_to_watchlist")

        if not selected:
            local_symbols = self._load_symbol_universe_from_local_files()
            if local_symbols:
                selected = local_symbols
                source = "local_files_fallback"
                errors.append("fallback_to_local_files")

        if not selected and allow_seed_fallback:
            seed_symbols = self._bootstrap_seed_symbols(cap=200)
            if seed_symbols:
                selected = seed_symbols
                source = "bootstrap_seed_fallback"
                errors.append("fallback_to_bootstrap_seed_symbols")

        if cap > 0:
            selected = selected[:cap]

        return {
            "source": source,
            "symbols": selected,
            "count": len(selected),
            "degraded": source not in {"akshare_spot", "efinance_spot", "local_files_primary"},
            "errors": errors,
            "cache_path": str(self._universe_cache_path),
        }

    def _prefilter_week5_universe_symbol(
        self,
        *,
        symbol: str,
        lookback_days: int,
        allowed_exchanges: set[str],
    ) -> dict[str, object] | None:
        normalized = _normalize_a_share_symbol(symbol)
        if not normalized:
            return None
        exchange = _exchange_from_a_share_symbol(normalized)
        if allowed_exchanges and exchange and exchange not in allowed_exchanges:
            return None
        bars = self._provider.fetch_daily_bars(symbol=normalized, lookback_days=lookback_days)
        if not isinstance(bars, pd.DataFrame) or bars.empty:
            return None
        frame = bars if bars.index.is_monotonic_increasing else bars.sort_index()
        if "close" not in frame.columns:
            return None
        close = pd.to_numeric(frame["close"], errors="coerce")
        valid_close = close.notna()
        if not bool(valid_close.any()):
            return None
        if not bool(valid_close.all()):
            frame = frame.loc[valid_close]
            close = close.loc[valid_close]
        # Some providers can return one trading day fewer than requested around
        # weekends/holidays; keep the candidate if the gap is only one day.
        minimum_history_days = max(2, lookback_days - 1)
        if len(close) < minimum_history_days:
            return None
        effective_lookback_days = min(lookback_days, len(close))
        tail = frame.tail(effective_lookback_days)
        close = close.tail(effective_lookback_days)
        latest = tail.iloc[-1]
        if _coerce_bool(latest.get("suspended", False)):
            return None
        if _coerce_bool(latest.get("is_st", False)) or _coerce_bool(
            latest.get("is_delisting_risk", False)
        ):
            return None

        close_index = close.index
        volume = _numeric_series(tail, "volume").reindex(close_index).fillna(0.0)
        turnover = _numeric_series(tail, "turnover").reindex(close_index)
        if turnover.isna().all():
            turnover = _numeric_series(tail, "amount").reindex(close_index)
        if turnover.isna().all():
            turnover = volume * close
        turnover = pd.to_numeric(turnover, errors="coerce").fillna(0.0)

        close_count = len(close)
        latest_close = float(close.iloc[-1])
        ma20 = float(close.tail(min(20, close_count)).mean())
        ma60 = float(close.tail(min(60, close_count)).mean())
        ma120 = float(close.tail(min(120, close_count)).mean())
        ma240 = float(close.mean())
        ret20 = 0.0
        ret60 = 0.0
        ret120 = 0.0
        if close_count > 20:
            start20 = _as_float(close.iloc[-21], default=0.0)
            if start20 > 0.0:
                ret20 = latest_close / start20 - 1.0
        if close_count > 60:
            start60 = _as_float(close.iloc[-61], default=0.0)
            if start60 > 0.0:
                ret60 = latest_close / start60 - 1.0
        if close_count > 120:
            start120 = _as_float(close.iloc[-121], default=0.0)
            if start120 > 0.0:
                ret120 = latest_close / start120 - 1.0
        recent_high = float(close.max()) if not close.empty else latest_close
        avg_turnover20 = float(turnover.tail(min(20, len(turnover))).mean())
        avg_turnover60 = float(turnover.tail(min(60, len(turnover))).mean())
        heat_ratio = avg_turnover20 / max(avg_turnover60, 1.0)
        returns20 = close.pct_change().dropna().tail(20)
        volatility20 = float(returns20.std(ddof=0)) if not returns20.empty else 0.0
        high = _numeric_series(tail, "high").reindex(close_index).fillna(close)
        low = _numeric_series(tail, "low").reindex(close_index).fillna(close)
        atr_ratio = ((high - low) / close.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)
        atr_ratio = atr_ratio.fillna(0.0)
        atr20 = (
            float(atr_ratio.tail(min(20, len(atr_ratio))).mean()) if not atr_ratio.empty else 0.0
        )
        atr60 = (
            float(atr_ratio.tail(min(60, len(atr_ratio))).mean()) if not atr_ratio.empty else atr20
        )

        volume5 = float(volume.tail(min(5, len(volume))).mean()) if not volume.empty else 0.0
        volume20 = float(volume.tail(min(20, len(volume))).mean()) if not volume.empty else 0.0
        volume_expansion = volume5 / max(volume20, 1.0)

        float_market_cap_series = _numeric_series(tail, "float_market_cap").reindex(close_index)
        float_market_cap_series = float_market_cap_series.replace([np.inf, -np.inf], np.nan)
        if float_market_cap_series.isna().all():
            float_market_cap_series = pd.Series(
                avg_turnover20 * 20.0, index=close_index, dtype=float
            )
        else:
            float_market_cap_series = (
                float_market_cap_series.ffill().bfill().fillna(avg_turnover20 * 20.0)
            )
        latest_float_market_cap = (
            float(float_market_cap_series.iloc[-1]) if not float_market_cap_series.empty else 0.0
        )
        turnover_rate20 = avg_turnover20 / max(latest_float_market_cap, 1.0)

        holder_count = (
            _numeric_series(tail, "holder_count")
            .reindex(close_index)
            .replace(
                [np.inf, -np.inf],
                np.nan,
            )
        )
        holder_count = holder_count.ffill().bfill()
        holder_count_chg60 = 0.0
        if len(holder_count) > 60:
            holder_base = float(holder_count.iloc[-61])
            if abs(holder_base) > 1e-9:
                holder_count_chg60 = float((holder_count.iloc[-1] - holder_base) / abs(holder_base))

        northbound = _numeric_series(tail, "northbound_net").reindex(close_index).fillna(0.0)
        northbound_net20 = float(northbound.tail(min(20, len(northbound))).sum())
        northbound_net60 = float(northbound.tail(min(60, len(northbound))).sum())
        northbound_flow_ratio20 = northbound_net20 / max(
            avg_turnover20 * min(20, len(northbound)), 1.0
        )

        dragon_tiger = _numeric_series(tail, "dragon_tiger_flag").reindex(close_index).fillna(0.0)
        dragon_tiger_freq20 = float(dragon_tiger.tail(min(20, len(dragon_tiger))).mean())

        financial_complete = _coerce_bool(latest.get("financial_data_complete", True))
        background_complete = _coerce_bool(latest.get("background_data_complete", True))

        ma_alignment = (
            0.30 * float(latest_close >= ma20)
            + 0.30 * float(latest_close >= ma60)
            + 0.25 * float(latest_close >= ma120)
            + 0.15 * float(latest_close >= ma240)
        )
        momentum_component = (
            0.40 * _clip01(ret20 / 0.18)
            + 0.35 * _clip01(ret60 / 0.35)
            + 0.25 * _clip01(ret120 / 0.60)
        )
        breakout_component = _clip01((latest_close / max(recent_high, 1e-9) - 0.82) / 0.18)
        trend_component = _clip01(
            0.45 * ma_alignment + 0.35 * momentum_component + 0.20 * breakout_component
        )

        holder_component = _clip01((0.05 - holder_count_chg60) / 0.10)
        northbound_component = _clip01((northbound_flow_ratio20 + 0.02) / 0.04)
        dragon_tiger_component = 0.30 + 0.70 * _clip01(dragon_tiger_freq20 / 0.08)
        capital_flow_component = _clip01(
            0.45 * holder_component + 0.35 * northbound_component + 0.20 * dragon_tiger_component
        )

        atr_compression = _clip01((1.10 - atr20 / max(atr60, 1e-6)) / 0.40)
        volume_expansion_component = _clip01((volume_expansion - 0.90) / 0.90)
        heat_component = _clip01((heat_ratio - 0.85) / 0.65)
        price_volume_component = _clip01(
            0.40 * volume_expansion_component + 0.30 * atr_compression + 0.30 * heat_component
        )

        turnover_component = _clip01(math.log10(max(avg_turnover20, 1.0)) / 9.0)
        market_cap_component = _clip01(math.log10(max(latest_float_market_cap, 1.0)) / 11.0)
        turnover_rate_component = _clip01((turnover_rate20 - 0.001) / 0.02)
        quality_component = 0.50 * (1.0 if financial_complete else 0.35) + 0.50 * (
            1.0 if background_complete else 0.35
        )
        background_completion_score = float(
            np.mean(
                [
                    1.0 if pd.notna(latest.get("holder_count")) else 0.0,
                    1.0 if pd.notna(latest.get("block_trade_net")) else 0.0,
                    1.0
                    if pd.notna(
                        latest.get("margin_financing_balance", latest.get("financing_balance"))
                    )
                    else 0.0,
                    1.0 if pd.notna(latest.get("northbound_net")) else 0.0,
                    1.0 if pd.notna(latest.get("dragon_tiger_flag")) else 0.0,
                    1.0 if pd.notna(latest.get("roe")) else 0.0,
                    1.0 if pd.notna(latest.get("debt_ratio")) else 0.0,
                    1.0 if background_complete else 0.0,
                ]
            )
        )
        liquidity_component = _clip01(
            0.45 * turnover_component
            + 0.25 * market_cap_component
            + 0.15 * turnover_rate_component
            + 0.15 * quality_component
        )

        drawdown_from_high = max(0.0, 1.0 - latest_close / max(recent_high, 1e-9))
        volatility_penalty = _clip01(max(volatility20 - 0.05, 0.0) / 0.10)
        drawdown_penalty = _clip01(max(drawdown_from_high - 0.08, 0.0) / 0.22)
        risk_penalty = _clip01(0.65 * volatility_penalty + 0.35 * drawdown_penalty)

        baseline_score = round(
            100.0
            * _clip01(
                0.40 * trend_component
                + 0.25 * capital_flow_component
                + 0.15 * price_volume_component
                + 0.10 * liquidity_component
                - 0.10 * risk_penalty
            ),
            4,
        )

        stage1_reasons: list[str] = []
        if latest_close >= ma60:
            stage1_reasons.append("trend_above_ma60")
        if ret60 > 0.08:
            stage1_reasons.append("ret60_positive")
        if capital_flow_component >= 0.55:
            stage1_reasons.append("capital_flow_support")
        if price_volume_component >= 0.55:
            stage1_reasons.append("price_volume_support")
        if liquidity_component >= 0.55:
            stage1_reasons.append("liquidity_ok")
        if risk_penalty >= 0.45:
            stage1_reasons.append("risk_penalty_high")
        if not financial_complete:
            stage1_reasons.append("financial_data_partial")
        if not background_complete:
            stage1_reasons.append("background_data_partial")

        return {
            "symbol": normalized,
            "exchange": exchange,
            "score": baseline_score,
            "baseline_score": baseline_score,
            "history_days": len(frame),
            "avg_turnover_20": round(avg_turnover20, 2),
            "avg_turnover_60": round(avg_turnover60, 2),
            "ret_20d": round(ret20, 6),
            "ret_60d": round(ret60, 6),
            "ret_120d": round(ret120, 6),
            "heat_ratio": round(heat_ratio, 6),
            "volatility_20d": round(volatility20, 6),
            "float_market_cap": round(latest_float_market_cap, 2),
            "turnover_rate_20d": round(turnover_rate20, 6),
            "holder_count_chg_60d": round(holder_count_chg60, 6),
            "northbound_net_20d": round(northbound_net20, 2),
            "northbound_net_60d": round(northbound_net60, 2),
            "dragon_tiger_freq_20d": round(dragon_tiger_freq20, 6),
            "volume_expansion_5d_20d": round(volume_expansion, 6),
            "atr_20d": round(atr20, 6),
            "atr_60d": round(atr60, 6),
            "atr_compression": round(atr_compression, 6),
            "financial_data_complete": financial_complete,
            "background_data_complete": background_complete,
            "background_completion_score": round(background_completion_score, 6),
            "stage1": {
                "score": baseline_score,
                "score_key": "baseline_score",
                "factors": {
                    "trend": round(trend_component, 6),
                    "capital_flow": round(capital_flow_component, 6),
                    "price_volume": round(price_volume_component, 6),
                    "liquidity": round(liquidity_component, 6),
                    "risk_penalty": round(risk_penalty, 6),
                },
                "reason_codes": stage1_reasons,
            },
        }

    def _prefilter_week5_universe_symbols(
        self,
        *,
        symbols: list[str],
        top_k_override: int | None = None,
    ) -> dict[str, object]:
        lookback_days = max(
            120,
            _as_int(self._config.week5.universe_prefilter_lookback_days, default=240),
        )
        configured_top_k = max(1, _as_int(self._config.week5.universe_prefilter_top_k, default=500))
        top_k = (
            max(1, _as_int(top_k_override, default=configured_top_k))
            if top_k_override is not None
            else configured_top_k
        )
        shortlist_top_n = max(
            1,
            _as_int(self._config.week5.universe_prefilter_shortlist_top_n, default=50),
        )
        normalized_symbols = [
            normalized
            for normalized in (_normalize_a_share_symbol(item) for item in symbols)
            if normalized
        ]
        deduped_symbols = _dedupe_preserve_order(normalized_symbols)
        allowed_exchanges = {
            str(item).strip().upper()
            for item in self._config.evolution.universe_spec.board_scope
            if str(item).strip()
        }
        candidates: list[dict[str, object]] = []
        errors: list[str] = []
        skipped = 0
        max_workers = min(8, max(1, len(deduped_symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    self._prefilter_week5_universe_symbol,
                    symbol=symbol,
                    lookback_days=lookback_days,
                    allowed_exchanges=allowed_exchanges,
                ): symbol
                for symbol in deduped_symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    candidate = future.result()
                except Exception as exc:
                    errors.append(f"{symbol}:{exc.__class__.__name__}")
                    continue
                if candidate is None:
                    skipped += 1
                    continue
                candidates.append(candidate)

        ranked = sorted(
            candidates,
            key=lambda item: (
                -_as_float(item.get("baseline_score"), default=0.0),
                -_as_float(item.get("avg_turnover_20"), default=0.0),
                str(item.get("symbol", "")),
            ),
        )
        shortlisted = ranked[:top_k]
        return {
            "enabled": True,
            "applied": True,
            "lookback_days": lookback_days,
            "top_k": top_k,
            "universe_count": len(deduped_symbols),
            "eligible_count": len(candidates),
            "skipped_count": skipped,
            "error_count": len(errors),
            "errors": errors[:20],
            "shortlisted_count": len(shortlisted),
            "scoring_mode": "two_stage_funnel",
            "symbols": [
                str(item.get("symbol", "")).strip()
                for item in shortlisted
                if str(item.get("symbol", "")).strip()
            ],
            "shortlisted": shortlisted,
            "preview": shortlisted[:10],
            "stages": {
                "stage1": {
                    "applied": True,
                    "status": "completed",
                    "score_key": "baseline_score",
                    "input_count": len(deduped_symbols),
                    "eligible_count": len(candidates),
                    "advanced_count": len(shortlisted),
                    "weights": {
                        "trend": 0.40,
                        "capital_flow": 0.25,
                        "price_volume": 0.15,
                        "liquidity": 0.10,
                        "risk_penalty": 0.10,
                    },
                    "preview": [
                        {
                            "symbol": str(item.get("symbol", "")).strip(),
                            "baseline_score": _as_float(
                                item.get("baseline_score"),
                                default=0.0,
                            ),
                            "reason_codes": _coerce_text_list(
                                _coerce_object_mapping(item.get("stage1")).get("reason_codes")
                            )[:6],
                        }
                        for item in shortlisted[:10]
                    ],
                },
                "stage2": {
                    "applied": False,
                    "status": "pending_signal_scan",
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

    def _score_week5_signal_pool_candidate(
        self,
        *,
        signal: Mapping[str, object],
        prefilter_detail: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        symbol = str(signal.get("symbol", "")).strip()
        reasons = _coerce_text_list(signal.get("reasons"))[:6]
        raw_score = round(_as_float(signal.get("score"), default=0.0), 2)
        grade = str(signal.get("grade", "")).strip()
        action = str(signal.get("action", "")).strip()
        suggested_position = round(_as_float(signal.get("target_position"), default=0.0), 4)
        decision_trace = _coerce_object_mapping(signal.get("decision_trace"))
        score_trace = _coerce_object_mapping(decision_trace.get("score"))
        raw_probabilities = _coerce_object_mapping(signal.get("probabilities"))
        if not raw_probabilities:
            raw_probabilities = _coerce_object_mapping(score_trace.get("probabilities"))
        probabilities = {
            str(key).strip(): float(value)
            for key, value in raw_probabilities.items()
            if str(key).strip() and isinstance(value, (int, float))
        }
        snapshot_id = _extract_learning_snapshot_id(signal)
        learning_protocol = _extract_learning_protocol_payload(signal)

        detail = prefilter_detail if isinstance(prefilter_detail, Mapping) else {}
        stage1 = detail.get("stage1", {})
        if not isinstance(stage1, Mapping):
            stage1 = {}
        factors = stage1.get("factors", {})
        if not isinstance(factors, Mapping):
            factors = {}

        prefilter_applied = bool(detail)
        trend = _as_float(factors.get("trend"), default=0.50 if not prefilter_applied else 0.0)
        capital_flow = _as_float(
            factors.get("capital_flow"),
            default=0.50 if not prefilter_applied else 0.0,
        )
        price_volume = _as_float(
            factors.get("price_volume"),
            default=0.50 if not prefilter_applied else 0.0,
        )
        liquidity = _as_float(
            factors.get("liquidity"),
            default=0.50 if not prefilter_applied else 0.0,
        )
        background_completion_score = _as_float(
            detail.get("background_completion_score"),
            default=1.0 if not prefilter_applied else 0.0,
        )
        financial_data_complete = bool(detail.get("financial_data_complete", False))
        background_data_complete = bool(detail.get("background_data_complete", False))
        risk_penalty = _as_float(
            factors.get("risk_penalty"),
            default=0.20 if not prefilter_applied else 0.0,
        )

        grade_score = {"A": 1.0, "B": 0.78, "C": 0.55, "D": 0.30}.get(grade.upper(), 0.40)
        action_score = {"buy": 1.0, "watch": 0.65, "sell": 0.0}.get(action.lower(), 0.30)
        max_stock_position = max(1e-6, float(self._config.monster_risk.max_stock_position))
        target_score = _clip01(suggested_position / max_stock_position)

        reason_penalty = 0.0
        for reason in reasons:
            lowered = reason.lower()
            if lowered == "time_invariant_violation":
                reason_penalty += 0.30
            elif lowered == "liquidity_failed":
                reason_penalty += 0.25
            elif lowered.startswith("insufficient_history_days:"):
                reason_penalty += 0.20
            elif lowered.startswith("data_source:"):
                reason_penalty += 0.20
            elif lowered.startswith("financial_filter:"):
                reason_penalty += 0.15
        reason_penalty = _clip01(reason_penalty)

        signal_strength = _clip01(
            0.55 * _clip01(raw_score / 100.0)
            + 0.25 * grade_score
            + 0.20 * max(action_score, target_score)
        )
        execution_liquidity = _clip01(0.60 * liquidity + 0.40 * max(action_score, target_score))
        shortlist_score = round(
            100.0
            * _clip01(
                0.35 * signal_strength
                + 0.25 * capital_flow
                + 0.15 * trend
                + 0.15 * price_volume
                + 0.10 * execution_liquidity
                - 0.10 * max(risk_penalty, reason_penalty)
            ),
            2,
        )

        shortlist_reasons: list[str] = []
        if signal_strength >= 0.72:
            shortlist_reasons.append("signal_strength")
        if capital_flow >= 0.55:
            shortlist_reasons.append("capital_confirmation")
        if trend >= 0.60:
            shortlist_reasons.append("trend_alignment")
        if price_volume >= 0.55:
            shortlist_reasons.append("price_volume_support")
        if execution_liquidity >= 0.55:
            shortlist_reasons.append("execution_ready")
        if max(risk_penalty, reason_penalty) >= 0.35:
            shortlist_reasons.append("risk_capped")

        return {
            "symbol": symbol,
            "score": raw_score,
            "grade": grade,
            "action": action,
            "suggested_position": suggested_position,
            "reasons": reasons,
            "prefilter_score": round(
                _as_float(detail.get("baseline_score"), default=0.0),
                2,
            ),
            "board_component": _extract_component_metric(
                reasons=reasons,
                prefix="board_component:",
            ),
            "completion_component": _extract_component_metric(
                reasons=reasons,
                prefix="completion_component:",
            ),
            "background_completion_score": round(background_completion_score, 6),
            "financial_data_complete": financial_data_complete,
            "background_data_complete": background_data_complete,
            "shortlist_score": shortlist_score,
            "shortlist_components": {
                "signal": round(signal_strength, 6),
                "capital_flow": round(capital_flow, 6),
                "trend": round(trend, 6),
                "price_volume": round(price_volume, 6),
                "execution_liquidity": round(execution_liquidity, 6),
                "risk_penalty": round(max(risk_penalty, reason_penalty), 6),
            },
            "shortlist_reasons": shortlist_reasons,
            "prefilter_reason_codes": [
                str(item).strip() for item in stage1.get("reason_codes", []) if str(item).strip()
            ][:6],
            "decision_trace": dict(decision_trace),
            "probabilities": probabilities,
            "snapshot_id": snapshot_id,
            "learning_protocol": dict(learning_protocol) if learning_protocol is not None else {},
        }

    def _idle_symbol_universe(
        self,
        *,
        task_id: str,
        max_symbols: int,
        min_symbols: int = 1,
    ) -> dict[str, object]:
        return self._idle_queue_service._idle_symbol_universe(
            task_id=task_id,
            max_symbols=max_symbols,
            min_symbols=min_symbols,
        )

    def _resolve_tdx_sync_vipdoc_root(self) -> Path:
        return self._market_sync_service._resolve_tdx_sync_vipdoc_root()

    def _resolve_tdx_sync_output_root(self) -> Path:
        return self._market_sync_service._resolve_tdx_sync_output_root()

    def _resolve_tdx_sync_auto_refresh(
        self,
        requested: bool | None = None,
    ) -> tuple[bool, str]:
        return self._market_sync_service._resolve_tdx_sync_auto_refresh(requested=requested)

    def _summarize_tdx_manifest(self, manifest: dict[str, object]) -> dict[str, object]:
        return self._market_sync_service._summarize_tdx_manifest(manifest)

    def _should_run_tdx_sync_build(
        self,
        *,
        source_freshness: dict[str, object],
        manifest: dict[str, object],
        force: bool,
    ) -> tuple[bool, str]:
        return self._market_sync_service._should_run_tdx_sync_build(
            source_freshness=source_freshness,
            manifest=manifest,
            force=force,
        )

    def _invalidate_market_data_cache(self) -> dict[str, object]:
        return self._market_sync_service._invalidate_market_data_cache()

    def _list_tdx_package_symbols(self, package_root: Path) -> list[str]:
        return self._market_sync_service._list_tdx_package_symbols(package_root)

    def _fetch_intraday_summaries(
        self,
        *,
        symbol: str,
        lookback_days: int,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        return self._market_sync_service._fetch_intraday_summaries(
            symbol=symbol,
            lookback_days=lookback_days,
        )

    def _safe_fetch_intraday_summary(
        self,
        *,
        symbol: str,
        interval: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        return self._market_sync_service._safe_fetch_intraday_summary(
            symbol=symbol,
            interval=interval,
            lookback_days=lookback_days,
        )

    def run_tdx_offline_sync(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        force: bool = False,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._market_sync_service.run_tdx_offline_sync(
            timestamp=timestamp,
            notify_enabled=notify_enabled,
            force=force,
            source_trace_id=source_trace_id,
        )

    def _notify_tdx_sync_if_needed(
        self,
        *,
        report: dict[str, object],
        notify_enabled: bool | None = None,
    ) -> None:
        self._market_sync_service._notify_tdx_sync_if_needed(
            report=report,
            notify_enabled=notify_enabled,
        )

    def _resolve_market_warehouse_db_path(self) -> Path:
        return self._market_sync_service._resolve_market_warehouse_db_path()

    def _resolve_market_warehouse_package_root(self) -> Path:
        return self._market_sync_service._resolve_market_warehouse_package_root()

    def _resolve_market_warehouse_bootstrap_source_root(self) -> Path:
        return self._market_sync_service._resolve_market_warehouse_bootstrap_source_root()

    def _can_bootstrap_market_warehouse_from_offline(self, source_root: Path) -> tuple[bool, str]:
        return self._market_sync_service._can_bootstrap_market_warehouse_from_offline(source_root)

    def _market_warehouse(self) -> MarketWarehouse:
        return self._market_sync_service._market_warehouse()

    def _resolve_market_warehouse_auto_refresh(
        self,
        *,
        requested: bool | None,
    ) -> tuple[bool, str]:
        return self._market_sync_service._resolve_market_warehouse_auto_refresh(requested=requested)

    def _build_market_warehouse_online_provider(self) -> MarketDataProvider:
        return self._market_sync_service._build_market_warehouse_online_provider()

    def _build_market_warehouse_online_single_provider(
        self,
        *,
        provider_name: str,
        request_interval: float,
        socket_timeout_sec: float,
        max_attempts: int,
    ) -> MarketDataProvider:
        return self._market_sync_service._build_market_warehouse_online_single_provider(
            provider_name=provider_name,
            request_interval=request_interval,
            socket_timeout_sec=socket_timeout_sec,
            max_attempts=max_attempts,
        )

    def _select_market_warehouse_symbols(
        self,
        *,
        warehouse: MarketWarehouse,
        package_root: Path,
        max_symbols: int,
    ) -> list[str]:
        return self._market_sync_service._select_market_warehouse_symbols(
            warehouse=warehouse,
            package_root=package_root,
            max_symbols=max_symbols,
        )

    def _resolve_market_warehouse_target_trade_date(self, *, now: datetime) -> date:
        return self._market_sync_service._resolve_market_warehouse_target_trade_date(now=now)

    def _resolve_market_warehouse_daily_lookback_days(
        self,
        *,
        latest_date: date | None,
        target_end_date: date,
        force: bool,
    ) -> tuple[int, str]:
        return self._market_sync_service._resolve_market_warehouse_daily_lookback_days(
            latest_date=latest_date,
            target_end_date=target_end_date,
            force=force,
        )

    def _collect_market_warehouse_focus_symbols(self, *, max_symbols: int) -> list[str]:
        return self._market_sync_service._collect_market_warehouse_focus_symbols(
            max_symbols=max_symbols
        )

    def _resolve_market_warehouse_intraday_symbols(
        self,
        *,
        symbol_list: list[str],
    ) -> list[str]:
        return self._market_sync_service._resolve_market_warehouse_intraday_symbols(
            symbol_list=symbol_list
        )

    def _carry_forward_market_warehouse_financial_fields(
        self,
        *,
        existing_daily: pd.DataFrame,
        fresh_daily: pd.DataFrame,
    ) -> pd.DataFrame:
        return self._market_sync_service._carry_forward_market_warehouse_financial_fields(
            existing_daily=existing_daily,
            fresh_daily=fresh_daily,
        )

    def _sync_market_warehouse_daily_symbol(
        self,
        *,
        warehouse: MarketWarehouse,
        online_provider: MarketDataProvider,
        symbol: str,
        force: bool,
        target_end_date: date,
        latest_daily: date | None = None,
        hard_timeout_sec: float | None = None,
    ) -> dict[str, object]:
        return self._market_sync_service._sync_market_warehouse_daily_symbol(
            warehouse=warehouse,
            online_provider=online_provider,
            symbol=symbol,
            force=force,
            target_end_date=target_end_date,
            latest_daily=latest_daily,
            hard_timeout_sec=hard_timeout_sec,
        )

    def _sync_market_warehouse_intraday_symbol(
        self,
        *,
        warehouse: MarketWarehouse,
        symbol: str,
        interval: str,
        force: bool,
        target_end_date: date,
        existing_latest: date | None = None,
    ) -> dict[str, object]:
        return self._market_sync_service._sync_market_warehouse_intraday_symbol(
            warehouse=warehouse,
            symbol=symbol,
            interval=interval,
            force=force,
            target_end_date=target_end_date,
            existing_latest=existing_latest,
        )

    def run_market_warehouse_sync(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        force: bool = False,
        source_trace_id: str = "",
        symbols: list[str] | None = None,
        retry_failed_only: bool = False,
        retry_report_trace_id: str = "",
        scheduler_lock_path: Path | None = None,
        scheduler_lock_owner_token: str = "",
    ) -> dict[str, object]:
        return self._market_sync_service.run_market_warehouse_sync(
            timestamp=timestamp,
            notify_enabled=notify_enabled,
            force=force,
            source_trace_id=source_trace_id,
            symbols=symbols,
            retry_failed_only=retry_failed_only,
            retry_report_trace_id=retry_report_trace_id,
            scheduler_lock_path=scheduler_lock_path,
            scheduler_lock_owner_token=scheduler_lock_owner_token,
        )

    def _write_post_market_warehouse_followup_state(
        self,
        *,
        stage: str,
        status: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        self._write_json_atomic(
            self._post_market_warehouse_followup_state_path,
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "stage": stage,
                "status": status,
                "payload": dict(payload or {}),
            },
        )

    def _post_market_warehouse_followup_stale_running_state(
        self,
        *,
        now: datetime,
        stale_after_hours: float = 6.0,
    ) -> dict[str, object] | None:
        previous = self.latest_post_market_warehouse_followup_state()
        if not isinstance(previous, Mapping):
            return None
        status = str(previous.get("status", "")).strip().lower()
        if status != "running":
            return None
        updated_at = _parse_iso_datetime(str(previous.get("updated_at", "")).strip())
        if updated_at is None:
            return {
                "status": "stale_running_reclaimed",
                "reason": "running_state_missing_updated_at",
                "previous": dict(previous),
            }
        age_hours = max(0.0, (now.timestamp() - updated_at.timestamp()) / 3600.0)
        if age_hours < max(0.0, float(stale_after_hours)):
            return None
        return {
            "status": "stale_running_reclaimed",
            "reason": "running_state_stale",
            "age_hours": round(age_hours, 4),
            "stale_after_hours": round(max(0.0, float(stale_after_hours)), 4),
            "previous": dict(previous),
        }

    def _pending_post_market_warehouse_followup_snapshot_ids(self) -> list[str]:
        return sorted(
            {
                outcome.snapshot_id
                for outcome in self._sample_store.list_outcomes()
                if outcome.maturity_status == MaturityStatus.PENDING
            }
        )

    def run_post_market_warehouse_retry_failed_sync(
        self,
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
        trigger: str = "manual",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        run_timestamp = timestamp or datetime.now()
        source_report = (
            dict(market_warehouse_report)
            if isinstance(market_warehouse_report, Mapping)
            else dict(self.latest_market_warehouse_report() or {})
        )
        source_trace_id = str(source_report.get("trace_id", "")).strip()
        source_status = str(source_report.get("status", "")).strip().lower()
        source_symbol_source = str(source_report.get("symbol_source", "")).strip().lower()
        failed_symbols = self._market_sync_service._extract_market_warehouse_failed_symbols(
            source_report,
        )
        failed_total, failed_complete = (
            self._market_sync_service._resolve_market_warehouse_retry_failed_total(
                source_report,
                extracted_symbols=failed_symbols,
            )
        )
        result: dict[str, object] = {
            "ok": True,
            "skipped": False,
            "trigger": str(trigger).strip() or "manual",
            "source_trace_id": source_trace_id,
            "source_status": source_status,
            "source_symbol_source": source_symbol_source,
            "source_failed_symbols_total": failed_total,
            "source_failed_symbols_complete": failed_complete,
            "source_failed_symbols_sample": failed_symbols[:20],
        }
        if not bool(self._config.market_warehouse.post_followup_retry_failed_enabled):
            result["skipped"] = True
            result["reason"] = "post_followup_retry_failed_disabled"
            return result
        if source_symbol_source == "retry_failed_only":
            result["skipped"] = True
            result["reason"] = "already_retry_report"
            return result
        if not source_trace_id:
            result["skipped"] = True
            result["reason"] = "source_trace_id_missing"
            return result
        if source_status not in {"ok", "partial", "failed"}:
            result["skipped"] = True
            result["reason"] = "source_report_not_retryable"
            return result
        if failed_total <= 0:
            result["skipped"] = True
            result["reason"] = "no_failed_symbols_to_retry"
            return result
        if not failed_complete:
            result["skipped"] = True
            result["reason"] = "retry_source_failed_symbols_incomplete"
            return result

        retry_trace_id = f"{source_trace_id}-retry"
        retry_report = self.run_market_warehouse_sync(
            timestamp=run_timestamp,
            notify_enabled=False,
            force=False,
            source_trace_id=retry_trace_id,
            retry_failed_only=True,
            retry_report_trace_id=source_trace_id,
        )
        result["retry_trace_id"] = retry_trace_id
        result["retry_report"] = retry_report
        result["retry_report_status"] = str(retry_report.get("status", "")).strip().lower()
        result["retry_report_reason"] = str(retry_report.get("reason", "")).strip()
        result["retry_failed_symbols_total"] = _as_int(
            retry_report.get("failed_symbols_total"),
            default=len(
                self._market_sync_service._extract_market_warehouse_failed_symbols(retry_report)
            ),
        )
        result["ok"] = bool(str(retry_report.get("status", "")).strip())
        return result

    def _resolve_post_market_warehouse_followup_effective_report(
        self,
        *,
        source_report: Mapping[str, object] | None = None,
        retry_payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        source = dict(source_report or {})
        if isinstance(retry_payload, Mapping):
            retry_report = retry_payload.get("retry_report")
            if (
                isinstance(retry_report, Mapping)
                and isinstance(retry_report.get("background_data"), Mapping)
                and str(retry_report.get("target_trade_date", "")).strip()
            ):
                return dict(retry_report)
        return source

    def evaluate_post_market_warehouse_followup_gate(
        self,
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        config = self._config.market_warehouse
        report = dict(market_warehouse_report or {})
        background_data = report.get("background_data")
        if isinstance(background_data, Mapping):
            background_snapshot = dict(background_data)
        else:
            background_snapshot = self.market_warehouse_background_data_status()
        report_status = str(report.get("status", "")).strip().lower()
        background_status = str(background_snapshot.get("status", "")).strip().lower()
        target_trade_date = str(report.get("target_trade_date", "")).strip()
        latest_trade_date = str(background_snapshot.get("latest_trade_date", "")).strip()
        coverage_ratio = _as_float(
            background_snapshot.get("latest_trade_date_coverage_ratio"),
            default=0.0,
        )
        min_coverage_ratio = min(
            1.0,
            max(
                0.0,
                _as_float(
                    config.post_followup_min_latest_trade_date_coverage_ratio,
                    default=0.95,
                ),
            ),
        )
        reasons: list[str] = []
        if not bool(config.post_followup_enabled):
            reasons.append("post_followup_disabled")
        if report_status not in {"ok", "partial"}:
            reasons.append("market_warehouse_sync_not_ready")
        if background_status in {"error", "missing", "empty"}:
            reasons.append("background_data_unavailable")
        if not target_trade_date:
            reasons.append("target_trade_date_missing")
        if not latest_trade_date:
            reasons.append("latest_trade_date_missing")
        if target_trade_date and latest_trade_date and latest_trade_date != target_trade_date:
            reasons.append("latest_trade_date_not_caught_up")
        if coverage_ratio < min_coverage_ratio:
            reasons.append("latest_trade_date_coverage_ratio_below_threshold")
        return {
            "allowed": not reasons,
            "reason": ",".join(reasons),
            "reasons": reasons,
            "market_warehouse_status": report_status,
            "background_data_status": background_status,
            "target_trade_date": target_trade_date,
            "latest_trade_date": latest_trade_date,
            "latest_trade_date_coverage_ratio": coverage_ratio,
            "min_latest_trade_date_coverage_ratio": min_coverage_ratio,
            "trace_id": str(report.get("trace_id", "")).strip(),
        }

    def run_post_market_warehouse_followup(
        self,
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
        trigger: str = "manual",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        run_timestamp = timestamp or datetime.now()
        source_report = (
            dict(market_warehouse_report)
            if isinstance(market_warehouse_report, Mapping)
            else dict(self.latest_market_warehouse_report() or {})
        )
        stale_running = self._post_market_warehouse_followup_stale_running_state(
            now=run_timestamp,
        )
        result: dict[str, object] = {
            "started_at": datetime.now(UTC).isoformat(),
            "ok": False,
            "skipped": False,
            "trigger": str(trigger).strip() or "manual",
            "source_market_warehouse_trace_id": str(source_report.get("trace_id", "")).strip(),
            "steps": {},
        }
        if stale_running is not None:
            result["stale_running_state"] = stale_running
            self._record_audit_event(
                event_type="post_market_warehouse_followup_stale_running_reclaimed",
                level="warn",
                payload=stale_running,
            )
        steps = cast(dict[str, object], result["steps"])
        self._write_post_market_warehouse_followup_state(
            stage="retry_failed_only",
            status="running",
            payload={
                "trigger": result["trigger"],
                "source_trace_id": result["source_market_warehouse_trace_id"],
                "stale_running_reclaimed": stale_running is not None,
            },
        )
        retry_payload = self.run_post_market_warehouse_retry_failed_sync(
            market_warehouse_report=source_report,
            trigger=result["trigger"],
            timestamp=run_timestamp,
        )
        steps["retry_failed_only"] = retry_payload
        self._write_post_market_warehouse_followup_state(
            stage="retry_failed_only",
            status="skipped" if bool(retry_payload.get("skipped", False)) else "completed",
            payload={
                "source_trace_id": str(retry_payload.get("source_trace_id", "")).strip(),
                "retry_trace_id": str(retry_payload.get("retry_trace_id", "")).strip(),
                "reason": str(retry_payload.get("reason", "")).strip(),
                "source_failed_symbols_total": _as_int(
                    retry_payload.get("source_failed_symbols_total"),
                    default=0,
                ),
                "retry_failed_symbols_total": _as_int(
                    retry_payload.get("retry_failed_symbols_total"),
                    default=0,
                ),
            },
        )
        effective_report = self._resolve_post_market_warehouse_followup_effective_report(
            source_report=source_report,
            retry_payload=retry_payload,
        )
        result["market_warehouse_trace_id"] = str(effective_report.get("trace_id", "")).strip()
        result["effective_market_warehouse_trace_id"] = result["market_warehouse_trace_id"]
        result["effective_market_warehouse_symbol_source"] = str(
            effective_report.get("symbol_source", "")
        ).strip()
        gate = self.evaluate_post_market_warehouse_followup_gate(
            market_warehouse_report=effective_report,
        )
        result["gate"] = gate
        self._write_post_market_warehouse_followup_state(
            stage="gate",
            status="running",
            payload={
                "trigger": result["trigger"],
                "gate": gate,
                "effective_trace_id": result["effective_market_warehouse_trace_id"],
            },
        )
        if not bool(gate.get("allowed", False)):
            result["ok"] = True
            result["skipped"] = True
            result["reason"] = str(gate.get("reason", "")).strip() or "followup_gate_blocked"
            result["finished_at"] = datetime.now(UTC).isoformat()
            self._write_json_atomic(self._post_market_warehouse_followup_result_path, result)
            self._write_post_market_warehouse_followup_state(
                stage="completed",
                status="skipped",
                payload={
                    "trigger": result["trigger"],
                    "reason": result["reason"],
                    "gate": gate,
                },
            )
            self._record_audit_event(
                event_type="post_market_warehouse_followup",
                trace_id=str(effective_report.get("trace_id", "")).strip(),
                level="info",
                payload={
                    "status": "skipped",
                    "trigger": result["trigger"],
                    "reason": result["reason"],
                    "gate": gate,
                    "retry": retry_payload,
                },
            )
            return result

        try:
            config = self._config.market_warehouse
            manifest_id = ""
            model_id = ""

            if bool(config.post_followup_run_week5):
                self._write_post_market_warehouse_followup_state(
                    stage="week5_scan",
                    status="running",
                    payload={
                        "sync_top_k_override": int(config.post_followup_week5_sync_top_k),
                        "force_universe_scan": bool(config.post_followup_force_universe_scan),
                    },
                )
                week5_payload = self.run_week5_scan(
                    symbols=None,
                    timestamp=run_timestamp,
                    notify_enabled=False,
                    sync_watchlist=True,
                    sync_reason=(
                        f"post_warehouse_full_refresh_{run_timestamp.strftime('%Y%m%d')}"
                    ),
                    sync_top_k_override=int(config.post_followup_week5_sync_top_k),
                    force_universe_scan=bool(config.post_followup_force_universe_scan),
                    scan_profile=str(config.post_followup_scan_profile).strip()
                    or "post_warehouse_full_refresh",
                )
                steps["week5_scan"] = week5_payload
                self._write_post_market_warehouse_followup_state(
                    stage="week5_scan",
                    status="completed",
                    payload={
                        "signal_count": int(len(week5_payload.get("signals", []) or [])),
                        "sync_watchlist": True,
                    },
                )
            else:
                steps["week5_scan"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "post_followup_run_week5_disabled",
                }

            if bool(config.post_followup_run_learning_backfill):
                pending_ids = self._pending_post_market_warehouse_followup_snapshot_ids()
                self._write_post_market_warehouse_followup_state(
                    stage="repair_learning_backfill",
                    status="running",
                    payload={"pending_snapshot_count": len(pending_ids)},
                )
                if pending_ids:
                    repair_payload = self.repair_learning_backfill(
                        snapshot_ids=pending_ids,
                        as_of=datetime.now(UTC),
                        source="post_warehouse_followup",
                    )
                else:
                    repair_payload = {
                        "ok": True,
                        "mode": "repair_backfill",
                        "skipped": True,
                        "reason": "no_pending_snapshot_ids",
                        "requested_snapshot_count": 0,
                    }
                steps["repair_learning_backfill"] = repair_payload
                self._write_post_market_warehouse_followup_state(
                    stage="repair_learning_backfill",
                    status="completed",
                    payload={
                        "pending_snapshot_count": len(pending_ids),
                        "repaired_snapshot_count": int(
                            repair_payload.get("repaired_snapshot_count", 0)
                        ),
                    },
                )
            else:
                steps["repair_learning_backfill"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "post_followup_run_learning_backfill_disabled",
                }

            if bool(config.post_followup_run_training):
                self._write_post_market_warehouse_followup_state(
                    stage="build_trainable_manifest",
                    status="running",
                )
                manifest_payload = self.build_learning_trainable_manifest()
                steps["build_trainable_manifest"] = manifest_payload
                if not bool(manifest_payload.get("ok", False)):
                    self._write_post_market_warehouse_followup_state(
                        stage="build_trainable_manifest",
                        status="fallback",
                        payload={"reason": "direct_manifest_build_failed"},
                    )
                    bootstrap_payload = self.bootstrap_learning_from_runtime_history(
                        build_manifest=True,
                    )
                    steps["learning_runtime_history_bootstrap"] = bootstrap_payload
                    manifest_payload = dict(bootstrap_payload.get("manifest", {}))
                    manifest_payload.setdefault(
                        "dataset_manifest_id",
                        str(bootstrap_payload.get("dataset_manifest_id", "")),
                    )
                    manifest_payload.setdefault(
                        "ok",
                        bool(bootstrap_payload.get("ok", False)),
                    )
                else:
                    steps["learning_runtime_history_bootstrap"] = {
                        "ok": True,
                        "skipped": True,
                        "reason": "direct_manifest_build_succeeded",
                    }
                manifest_id = str(manifest_payload.get("dataset_manifest_id", "")).strip()
                if not bool(manifest_payload.get("ok", False)) or not manifest_id:
                    raise RuntimeError(
                        "trainable_manifest_unavailable: "
                        + ",".join(
                            str(item)
                            for item in manifest_payload.get("errors", []) or []
                        )
                    )
                self._write_post_market_warehouse_followup_state(
                    stage="build_trainable_manifest",
                    status="completed",
                    payload={
                        "dataset_manifest_id": manifest_id,
                        "included_snapshot_count": int(
                            manifest_payload.get("included_snapshot_count", 0)
                        ),
                        "included_outcome_count": int(
                            manifest_payload.get("included_outcome_count", 0)
                        ),
                    },
                )

                self._write_post_market_warehouse_followup_state(
                    stage="learning_shadow_proposal",
                    status="running",
                    payload={"dataset_manifest_id": manifest_id},
                )
                auto_promotion_enabled = bool(self._config.auto_promotion.enabled)
                proposal_payload = self.run_learning_manifest_shadow_proposal(
                    dataset_manifest_id=manifest_id,
                    load_predictor=not auto_promotion_enabled,
                    approve_if_passed=True,
                    auto_approve=auto_promotion_enabled,
                    auto_release=auto_promotion_enabled,
                    auto_reload_predictor=bool(
                        self._config.auto_promotion.auto_load_predictor
                    ),
                    notify_on_rejection=bool(
                        self._config.auto_promotion.notify_on_rejection
                    ),
                    source_trace_id=str(effective_report.get("trace_id", "")).strip(),
                )
                steps["learning_shadow_proposal"] = proposal_payload
                steps["auto_promotion"] = dict(
                    proposal_payload.get("auto_promotion", {}) or {}
                )
                workflow_payload = dict(proposal_payload.get("workflow", {}) or {})
                shadow_validation_payload = dict(
                    workflow_payload.get("shadow_validation", {}) or {}
                )
                training_payload = dict(shadow_validation_payload.get("training", {}) or {})
                steps["train_learning_manifest"] = training_payload
                if not bool(workflow_payload.get("ok", False)):
                    errors = [
                        str(item).strip()
                        for item in proposal_payload.get("errors", []) or []
                        if str(item).strip()
                    ]
                    raise RuntimeError(
                        "learning_shadow_workflow_failed: " + ",".join(errors or ["unknown"])
                    )
                model_id = str(proposal_payload.get("shadow_model_id", "")).strip()
                proposal_id = str(
                    dict(proposal_payload.get("proposal", {}) or {}).get("proposal_id", "")
                ).strip()
                ticket_id = str(
                    dict(proposal_payload.get("auto_promotion", {}) or {}).get("ticket_id", "")
                ).strip()
                release_status = str(
                    dict(proposal_payload.get("proposal", {}) or {}).get("status", "")
                ).strip()
                self._write_post_market_warehouse_followup_state(
                    stage="learning_shadow_proposal",
                    status="completed",
                    payload={
                        "dataset_manifest_id": manifest_id,
                        "shadow_model_id": model_id,
                        "proposal_id": proposal_id,
                        "ticket_id": ticket_id,
                        "artifact_path": str(training_payload.get("artifact_path", "")),
                        "predictor_loaded": bool(
                            dict(proposal_payload.get("auto_promotion", {}) or {}).get(
                                "predictor_loaded", False
                            )
                        )
                        or bool(training_payload.get("predictor_loaded", False)),
                        "release_status": release_status,
                    },
                )
                result["learning_proposal_id"] = proposal_id
                result["learning_release_ticket_id"] = ticket_id
                result["learning_release_status"] = release_status
                if bool(self._config.auto_promotion.notify_on_training_summary):
                    steps["learning_summary_notification"] = (
                        self._notify_learning_workflow_summary(
                            proposal_payload=proposal_payload,
                            trace_id=str(effective_report.get("trace_id", "")).strip(),
                        )
                    )
                else:
                    steps["learning_summary_notification"] = {
                        "sent": False,
                        "reason": "training_summary_notification_disabled",
                    }
            else:
                steps["build_trainable_manifest"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "post_followup_run_training_disabled",
                }
                steps["learning_runtime_history_bootstrap"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "post_followup_run_training_disabled",
                }
                steps["train_learning_manifest"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "post_followup_run_training_disabled",
                }
                steps["learning_shadow_proposal"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "post_followup_run_training_disabled",
                }
                steps["auto_promotion"] = {
                    "enabled": False,
                    "status": "skipped",
                }
                steps["learning_summary_notification"] = {
                    "sent": False,
                    "reason": "post_followup_run_training_disabled",
                }

            if bool(config.post_followup_run_phase_d_tabular_deep):
                self._write_post_market_warehouse_followup_state(
                    stage="phase_d_tabular_deep",
                    status="running",
                    payload={"model_id": model_id},
                )
                if model_id:
                    phase_d_payload = self.build_phase_d_tabular_deep_report(model_id=model_id)
                else:
                    phase_d_payload = {
                        "ok": True,
                        "skipped": True,
                        "reason": "model_id_missing_after_manifest_training",
                    }
                steps["phase_d_tabular_deep"] = phase_d_payload
                self._write_post_market_warehouse_followup_state(
                    stage="phase_d_tabular_deep",
                    status=(
                        "skipped"
                        if bool(phase_d_payload.get("skipped", False))
                        else "completed"
                    ),
                    payload={
                        "model_id": model_id,
                        "output_path": str(phase_d_payload.get("output_path", "")),
                        "reason": str(phase_d_payload.get("reason", "")),
                    },
                )
            else:
                steps["phase_d_tabular_deep"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "post_followup_run_phase_d_tabular_deep_disabled",
                }

            result["ok"] = True
            result["finished_at"] = datetime.now(UTC).isoformat()
            result["dataset_manifest_id"] = manifest_id
            result["model_id"] = model_id
            self._write_json_atomic(self._post_market_warehouse_followup_result_path, result)
            self._write_post_market_warehouse_followup_state(
                stage="completed",
                status="completed",
                payload={
                    "dataset_manifest_id": manifest_id,
                    "model_id": model_id,
                },
            )
            self._record_audit_event(
                event_type="post_market_warehouse_followup",
                trace_id=str(effective_report.get("trace_id", "")).strip(),
                level="info",
                payload={
                    "status": "completed",
                    "trigger": result["trigger"],
                    "dataset_manifest_id": manifest_id,
                    "model_id": model_id,
                    "retry": retry_payload,
                },
            )
            return result
        except Exception as exc:
            result["finished_at"] = datetime.now(UTC).isoformat()
            error_payload = {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
            result["error"] = error_payload
            self._write_json_atomic(self._post_market_warehouse_followup_result_path, result)
            self._write_post_market_warehouse_followup_state(
                stage="failed",
                status="failed",
                payload=error_payload,
            )
            self._record_audit_event(
                event_type="post_market_warehouse_followup",
                trace_id=str(effective_report.get("trace_id", "")).strip(),
                level="warn",
                payload={
                    "status": "failed",
                    "trigger": result["trigger"],
                    "error": error_payload,
                    "retry": retry_payload,
                },
            )
            return result

    def _notify_market_warehouse_if_needed(
        self,
        *,
        report: dict[str, object],
        notify_enabled: bool | None = None,
    ) -> None:
        self._market_sync_service._notify_market_warehouse_if_needed(
            report=report,
            notify_enabled=notify_enabled,
        )

    def _evolution_runtime_mode(self) -> str:
        return self._evolution_core_service._evolution_runtime_mode()

    def _evolution_dry_run_policy(self) -> str:
        return self._evolution_core_service._evolution_dry_run_policy()

    def _evolution_dry_run_live_modes(self) -> set[str]:
        return self._evolution_core_service._evolution_dry_run_live_modes()

    def _resolve_evolution_dry_run(
        self,
        requested: bool | None = None,
    ) -> tuple[bool, str]:
        return self._evolution_core_service._resolve_evolution_dry_run(requested)

    def run_evolution_offhours(
        self,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        dry_run: bool | None = None,
        source_trace_id: str = "",
        records: list[dict[str, object]] | None = None,
        refresh_tdx_before_run: bool | None = None,
    ) -> dict[str, object]:
        return self._evolution_core_service.run_evolution_offhours(
            symbols=symbols,
            timestamp=timestamp,
            dry_run=dry_run,
            source_trace_id=source_trace_id,
            records=records,
            refresh_tdx_before_run=refresh_tdx_before_run,
        )

    def run_evolution_drill(
        self,
        timestamp: datetime | None = None,
        source_trace_id: str = "evolution-drill",
    ) -> dict[str, object]:
        return self._evolution_core_service.run_evolution_drill(
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def _create_universe_snapshot(
        self,
        *,
        symbols: list[str],
        decision_time: datetime,
    ) -> dict[str, object]:
        return self._evolution_core_service._create_universe_snapshot(
            symbols=symbols,
            decision_time=decision_time,
        )

    def _attach_universe_snapshot_metadata(
        self,
        *,
        records: list[dict[str, object]],
        universe_snapshot_id: str,
        universe_spec_hash: str,
    ) -> list[dict[str, object]]:
        return self._evolution_core_service._attach_universe_snapshot_metadata(
            records=records,
            universe_snapshot_id=universe_snapshot_id,
            universe_spec_hash=universe_spec_hash,
        )

    def run_evolution_m3_maintenance(
        self,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_core_service.run_evolution_m3_maintenance(
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def run_evolution_m3_search(
        self,
        vector: list[float],
        top_k: int = 5,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_core_service.run_evolution_m3_search(
            vector=vector,
            top_k=top_k,
            source_trace_id=source_trace_id,
        )

    def run_evolution_m8_suggest(
        self,
        symbols: list[str] | None = None,
        top_k: int | None = None,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
        records: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return self._evolution_core_service.run_evolution_m8_suggest(
            symbols=symbols,
            top_k=top_k,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
            records=records,
        )

    def evolution_preflight(self) -> dict[str, object]:
        return self._evolution_release_service.evolution_preflight()

    def latest_evolution_report(self) -> dict[str, object] | None:
        return self._evolution_core_service.latest_evolution_report()

    def evolution_history(self, limit: int = 20) -> dict[str, object]:
        return self._evolution_core_service.evolution_history(limit)

    def evolution_window_report(
        self,
        days: int = 10,
        min_runs: int = 5,
        now: datetime | None = None,
    ) -> dict[str, object]:
        return self._evolution_release_service.evolution_window_report(
            days,
            min_runs,
            now,
        )

    def attempt_evolution_release(
        self,
        days: int = 10,
        min_runs: int = 5,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_release_service.attempt_evolution_release(
            days,
            min_runs,
            now,
            source_trace_id,
        )

    def latest_evolution_release_gate(self) -> dict[str, object] | None:
        return self._evolution_release_service.latest_evolution_release_gate()

    def evolution_release_gate_history(self, limit: int = 20) -> dict[str, object]:
        return self._evolution_release_service.evolution_release_gate_history(limit)

    def record_evolution_release_approval(
        self,
        approver: str,
        approved: bool,
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_release_service.record_evolution_release_approval(
            approver,
            approved,
            note,
            timestamp,
            source_trace_id,
        )

    def latest_evolution_release_approval(self) -> dict[str, object] | None:
        return self._evolution_release_service.latest_evolution_release_approval()

    def evolution_release_approval_history(self, limit: int = 20) -> dict[str, object]:
        return self._evolution_release_service.evolution_release_approval_history(
            limit,
        )

    def issue_evolution_release_ticket(
        self,
        operator: str,
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_release_service.issue_evolution_release_ticket(
            operator,
            note,
            timestamp,
            source_trace_id,
        )

    def execute_evolution_release_ticket(
        self,
        executor: str,
        ticket_id: str = "",
        note: str = "",
        confirm_window: bool = True,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_release_service.execute_evolution_release_ticket(
            executor,
            ticket_id,
            note,
            confirm_window,
            timestamp,
            source_trace_id,
        )

    def rollback_evolution_release_ticket(
        self,
        rollback_by: str,
        ticket_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_release_service.rollback_evolution_release_ticket(
            rollback_by,
            ticket_id,
            note,
            timestamp,
            source_trace_id,
        )

    def confirm_evolution_release_ticket(
        self,
        confirmer: str,
        ticket_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_release_service.confirm_evolution_release_ticket(
            confirmer,
            ticket_id,
            note,
            timestamp,
            source_trace_id,
        )

    def run_evolution_release_confirmation_watchdog(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._evolution_release_service.run_evolution_release_confirmation_watchdog(
            now,
            source_trace_id,
        )

    def latest_evolution_release_ticket(self) -> dict[str, object] | None:
        return self._evolution_release_service.latest_evolution_release_ticket()

    def evolution_release_ticket_history(self, limit: int = 20) -> dict[str, object]:
        return self._evolution_release_service.evolution_release_ticket_history(limit)

    def evolution_release_ticket_timeline(
        self,
        ticket_id: str = "",
        status: str = "",
        limit: int = 200,
    ) -> dict[str, object]:
        return self._evolution_release_service.evolution_release_ticket_timeline(
            ticket_id,
            status,
            limit,
        )

    def _evolution_pending_confirmation_count(self) -> int:
        return self._evolution_release_service._evolution_pending_confirmation_count()

    def _write_evolution_compliance_event(
        self,
        state: ComplianceState,
        proposal_id: str,
        symbol: str,
        event_time: datetime,
        trace_id: str,
        code_commit_id: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._evolution_release_service._write_evolution_compliance_event(
            state,
            proposal_id,
            symbol,
            event_time,
            trace_id,
            code_commit_id,
            metadata,
        )

    def _try_train_models_from_learning_protocol(
        self,
        *,
        trainer: ModelTrainer,
        symbols: list[str],
        lookback_days: int,
        artifact_path: str | None,
    ) -> dict[str, object]:
        if (
            not hasattr(self, "_sample_store")
            or not hasattr(self, "_feature_schema_registry")
            or not hasattr(self, "_label_policy_registry")
        ):
            return {
                "attempted": False,
                "ok": False,
                "errors": [],
                "fallback_reason": "learning_protocol_unavailable",
                "protocol_candidate_rows": 0,
            }

        now = datetime.now()
        time_window_end = now
        time_window_start = now - timedelta(days=max(1, int(lookback_days)))
        requested_symbols = {
            normalized
            for item in symbols
            if (normalized := _normalize_a_share_symbol(item) or str(item).strip())
        }

        try:
            label_policy = self._label_policy_registry.register_from_config(self._config.labels)
            snapshots = self._sample_store.list_snapshots(
                label_policy_id=label_policy.label_policy_id,
                time_window_start=time_window_start,
                time_window_end=time_window_end,
            )
            if requested_symbols:
                snapshots = [
                    snapshot
                    for snapshot in snapshots
                    if (_normalize_a_share_symbol(snapshot.symbol) or snapshot.symbol.strip())
                    in requested_symbols
                ]
            if not snapshots:
                return {
                    "attempted": True,
                    "ok": False,
                    "errors": [],
                    "fallback_reason": "learning_protocol_no_snapshots",
                    "protocol_candidate_rows": 0,
                }

            outcome_map = {
                outcome.snapshot_id: outcome
                for outcome in self._sample_store.list_outcomes(
                    snapshot_ids=[snapshot.snapshot_id for snapshot in snapshots]
                )
            }
            mature_statuses = {
                MaturityStatus.LABEL_MATURED,
                MaturityStatus.RECONCILED,
                MaturityStatus.FULLY_MATURED,
            }
            exact_schema_groups: dict[tuple[str, str], list[SignalSnapshot]] = {}
            for snapshot in snapshots:
                outcome = outcome_map.get(snapshot.snapshot_id)
                if outcome is None or outcome.maturity_status not in mature_statuses:
                    continue
                key = (snapshot.feature_schema_id, snapshot.feature_schema_hash)
                exact_schema_groups.setdefault(key, []).append(snapshot)
            if not exact_schema_groups:
                return {
                    "attempted": True,
                    "ok": False,
                    "errors": [],
                    "fallback_reason": "learning_protocol_no_matured_outcomes",
                    "protocol_candidate_rows": 0,
                }

            max_rows = max(
                0,
                _as_int(self._config.training.bootstrap_dataset_max_rows, default=0),
            )
            per_symbol_rows_cap = max(
                0,
                _as_int(self._config.training.bootstrap_per_symbol_rows_cap, default=0),
            )
            best_candidate: dict[str, object] | None = None
            candidate_blueprints: list[dict[str, object]] = []
            registry_records = self._feature_schema_registry.list_records()
            registry_contract_keys = {
                (record.feature_schema_id, record.feature_schema_hash)
                for record in registry_records
            }
            for record in registry_records:
                compatible_records = (
                    self._feature_schema_registry.resolve_projection_compatible_records(
                        record.feature_schema_id
                    )
                )
                allowed_contracts = {
                    (compatible.feature_schema_id, compatible.feature_schema_hash)
                    for compatible in compatible_records
                }
                if not allowed_contracts:
                    allowed_contracts = {(record.feature_schema_id, record.feature_schema_hash)}
                candidate_snapshots = [
                    snapshot
                    for contract_key, grouped_rows in exact_schema_groups.items()
                    if contract_key in allowed_contracts
                    for snapshot in grouped_rows
                ]
                if not candidate_snapshots:
                    continue
                candidate_blueprints.append(
                    {
                        "feature_schema_id": record.feature_schema_id,
                        "feature_schema_hash": record.feature_schema_hash,
                        "feature_count": len(record.feature_names),
                        "schema_created_at": record.created_at,
                        "snapshots": candidate_snapshots,
                    }
                )

            for (feature_schema_id, feature_schema_hash), candidate_snapshots in exact_schema_groups.items():
                if (feature_schema_id, feature_schema_hash) in registry_contract_keys:
                    continue
                candidate_blueprints.append(
                    {
                        "feature_schema_id": feature_schema_id,
                        "feature_schema_hash": feature_schema_hash,
                        "feature_count": 0,
                        "schema_created_at": datetime.min.replace(tzinfo=UTC),
                        "snapshots": candidate_snapshots,
                    }
                )

            for blueprint in candidate_blueprints:
                candidate_snapshots = cast(list[SignalSnapshot], blueprint["snapshots"])
                capped_snapshots, truncated = _apply_learning_protocol_row_caps(
                    snapshots=candidate_snapshots,
                    max_rows=max_rows,
                    per_symbol_rows_cap=per_symbol_rows_cap,
                )
                if not capped_snapshots:
                    continue
                candidate = {
                    "feature_schema_id": str(blueprint["feature_schema_id"]),
                    "feature_schema_hash": str(blueprint["feature_schema_hash"]),
                    "feature_count": int(blueprint["feature_count"]),
                    "schema_created_at": cast(datetime, blueprint["schema_created_at"]),
                    "snapshots": capped_snapshots,
                    "truncated": truncated,
                    "latest_decision_time": max(
                        snapshot.decision_time for snapshot in capped_snapshots
                    ),
                }
                if best_candidate is None:
                    best_candidate = candidate
                    continue
                best_rows = len(cast(list[SignalSnapshot], best_candidate["snapshots"]))
                candidate_rows = len(capped_snapshots)
                if candidate_rows > best_rows or (
                    candidate_rows == best_rows
                    and int(candidate["feature_count"])
                    > int(best_candidate.get("feature_count", 0))
                ) or (
                    candidate_rows == best_rows
                    and int(candidate["feature_count"])
                    == int(best_candidate.get("feature_count", 0))
                    and cast(datetime, candidate["schema_created_at"])
                    > cast(
                        datetime,
                        best_candidate.get(
                            "schema_created_at",
                            datetime.min.replace(tzinfo=UTC),
                        ),
                    )
                ) or (
                    candidate_rows == best_rows
                    and int(candidate["feature_count"])
                    == int(best_candidate.get("feature_count", 0))
                    and cast(datetime, candidate["schema_created_at"])
                    == cast(
                        datetime,
                        best_candidate.get(
                            "schema_created_at",
                            datetime.min.replace(tzinfo=UTC),
                        ),
                    )
                    and candidate["latest_decision_time"]
                    > cast(datetime, best_candidate["latest_decision_time"])
                ):
                    best_candidate = candidate

            if best_candidate is None:
                return {
                    "attempted": True,
                    "ok": False,
                    "errors": [],
                    "fallback_reason": "learning_protocol_no_trainable_candidate",
                    "protocol_candidate_rows": 0,
                }

            selected_snapshots = cast(list[SignalSnapshot], best_candidate["snapshots"])
            candidate_rows = len(selected_snapshots)
            min_samples = max(1, int(self._config.training.min_samples))
            if candidate_rows < min_samples:
                return {
                    "attempted": True,
                    "ok": False,
                    "errors": [],
                    "fallback_reason": (
                        "learning_protocol_insufficient_samples:"
                        f"{candidate_rows}<{min_samples}"
                    ),
                    "protocol_candidate_rows": candidate_rows,
                }

            feature_schema_id = str(best_candidate["feature_schema_id"])
            feature_schema_hash = str(best_candidate["feature_schema_hash"])
            feature_schema = self._feature_schema_registry.get_by_id(feature_schema_id)
            if feature_schema is None:
                return {
                    "attempted": True,
                    "ok": False,
                    "errors": [],
                    "fallback_reason": "learning_protocol_feature_schema_missing",
                    "protocol_candidate_rows": candidate_rows,
                }
            if feature_schema.feature_schema_hash != feature_schema_hash:
                return {
                    "attempted": True,
                    "ok": False,
                    "errors": [],
                    "fallback_reason": "learning_protocol_feature_schema_hash_mismatch",
                    "protocol_candidate_rows": candidate_rows,
                }

            result = trainer.train_on_sample_store(
                store=self._sample_store,
                feature_schema_id=feature_schema_id,
                feature_schema_hash=feature_schema_hash,
                label_policy_id=label_policy.label_policy_id,
                label_policy_hash=label_policy.label_policy_hash,
                snapshot_ids=[snapshot.snapshot_id for snapshot in selected_snapshots],
                feature_schema_registry=self._feature_schema_registry,
                label_policy_registry=self._label_policy_registry,
                sample_selection_rule=(
                    "runtime_learning_protocol_v1:"
                    f"lookback_days={lookback_days};symbols={len(requested_symbols)};"
                    f"snapshots={candidate_rows}"
                ),
                time_window_start=time_window_start,
                time_window_end=time_window_end,
            )
            output_path = artifact_path or self._config.training.artifact_path
            result.artifact.save(output_path)
            unique_symbols = {
                _normalize_a_share_symbol(snapshot.symbol) or snapshot.symbol.strip()
                for snapshot in selected_snapshots
                if (_normalize_a_share_symbol(snapshot.symbol) or snapshot.symbol.strip())
            }
            return {
                "attempted": True,
                "ok": True,
                "status": (
                    "ok_learning_protocol_truncated"
                    if bool(best_candidate["truncated"])
                    else "ok_learning_protocol"
                ),
                "artifact_path": output_path,
                "symbols_used": len(unique_symbols),
                "dataset_rows": int(result.samples_total),
                "truncated": bool(best_candidate["truncated"]),
                "result": result.to_dict(),
                "errors": [],
                "fetch_failed": 0,
                "skipped": 0,
                "input_mode": "sample_store",
                "protocol_candidate_rows": candidate_rows,
                "dataset_manifest_id": result.artifact.dataset_manifest_id,
                "protocol_fallback_reason": "",
            }
        except Exception as exc:
            return {
                "attempted": True,
                "ok": False,
                "errors": [f"learning_protocol_failed:{exc.__class__.__name__}"],
                "fallback_reason": f"learning_protocol_exception:{exc}",
                "protocol_candidate_rows": 0,
            }

    def train_models(
        self,
        symbol: str = "",
        lookback_days: int = 600,
        artifact_path: str | None = None,
        full_market: bool = False,
        max_symbols: int | None = None,
        preferred_symbols: list[str] | None = None,
    ) -> dict[str, object]:
        if not full_market:
            normalized_symbol = str(symbol).strip()
            if not normalized_symbol:
                raise ValueError("symbol is required when full_market=false")
            bars = self._provider.fetch_daily_bars(
                symbol=normalized_symbol, lookback_days=lookback_days
            )
            intraday_1m, intraday_5m = self._fetch_intraday_summaries(
                symbol=normalized_symbol,
                lookback_days=max(lookback_days, len(bars) + 5),
            )
            trainer = self._build_model_trainer()
            result = trainer.train_and_save(
                bars=bars,
                output_path=artifact_path,
                intraday_1m=intraday_1m,
                intraday_5m=intraday_5m,
            )
            output_path = artifact_path or self._config.training.artifact_path
            loaded = self._pipeline.reload_predictor(artifact_path=artifact_path)
            model_registry_payload = self._register_model_artifact_if_supported(
                artifact_path=output_path,
                role=ModelRole.CHALLENGER,
                lifecycle_state=ModelLifecycleState.TRAINED,
                source="train_models_single_symbol",
            )
            return {
                "mode": "single_symbol",
                "symbol": normalized_symbol,
                "artifact_path": output_path,
                "predictor_loaded": loaded,
                "result": result.to_dict(),
                "input_mode": "bars",
                "protocol_attempted": False,
                "protocol_fallback_reason": "",
                "model_registry": model_registry_payload,
            }

        cfg_cap = max(0, _as_int(self._config.training.bootstrap_max_symbols, default=0))
        runtime_cap = max(0, _as_int(max_symbols, default=0))
        effective_cap = runtime_cap if runtime_cap > 0 else (cfg_cap if cfg_cap > 0 else None)
        preferred = [
            _normalize_a_share_symbol(item) or str(item).strip()
            for item in (preferred_symbols or [])
            if str(item).strip()
        ]
        symbol_list = _dedupe_preserve_order([item for item in preferred if item])
        if effective_cap is not None and effective_cap > 0:
            symbol_list = symbol_list[:effective_cap]
        universe = {
            "source": "preferred_symbols",
            "symbols": symbol_list,
            "count": len(symbol_list),
            "degraded": True,
            "errors": [],
            "cache_path": str(self._universe_cache_path),
        }
        if not symbol_list:
            universe = self._resolve_symbol_universe(
                max_symbols=effective_cap,
                allow_seed_fallback=True,
            )
            symbol_list = _string_list(universe.get("symbols", []))
        if not symbol_list:
            raise ValueError("full-market bootstrap aborted: empty symbol universe")

        training_payload = self._train_models_on_symbol_universe(
            symbols=symbol_list,
            lookback_days=lookback_days,
            artifact_path=artifact_path,
        )
        loaded = self._pipeline.reload_predictor(
            artifact_path=str(training_payload.get("artifact_path", ""))
        )

        self._training_bootstrap_state.update(
            {
                "completed": bool(training_payload.get("ok", False)),
                "bootstrap_runs": _as_int(
                    self._training_bootstrap_state.get("bootstrap_runs"),
                    default=0,
                )
                + 1,
                "last_bootstrap_at": datetime.now().isoformat(),
                "last_status": str(training_payload.get("status", "")),
                "last_symbols": _as_int(training_payload.get("symbols_used"), default=0),
                "last_error": _bootstrap_error_text(report=training_payload),
                "artifact_path": str(training_payload.get("artifact_path", "")),
            }
        )
        self._persist_training_bootstrap_state(self._training_bootstrap_state)
        if bool(training_payload.get("ok", False)):
            self._maybe_seed_watchlist_after_bootstrap()

        model_registry_payload = self._register_model_artifact_if_supported(
            artifact_path=str(training_payload.get("artifact_path", "")),
            role=ModelRole.CHALLENGER,
            lifecycle_state=ModelLifecycleState.TRAINED,
            source="train_models_full_market",
        )
        return {
            "mode": "full_market",
            "ok": bool(training_payload.get("ok", False)),
            "status": str(training_payload.get("status", "")),
            "artifact_path": str(training_payload.get("artifact_path", "")),
            "predictor_loaded": loaded,
            "universe_source": str(universe.get("source", "")),
            "universe_total": _as_int(universe.get("count"), default=0),
            "symbols_used": _as_int(training_payload.get("symbols_used"), default=0),
            "dataset_rows": _as_int(training_payload.get("dataset_rows"), default=0),
            "truncated": bool(training_payload.get("truncated", False)),
            "result": training_payload.get("result", {}),
            "errors": training_payload.get("errors", []),
            "input_mode": str(training_payload.get("input_mode", "bars")),
            "protocol_attempted": bool(training_payload.get("attempted", False)),
            "protocol_fallback_reason": str(
                training_payload.get("protocol_fallback_reason", "")
                or training_payload.get("fallback_reason", "")
            ),
            "dataset_manifest_id": str(training_payload.get("dataset_manifest_id", "")),
            "model_registry": model_registry_payload,
        }

    def _train_models_on_symbol_universe(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        artifact_path: str | None,
    ) -> dict[str, object]:
        trainer = self._build_model_trainer()
        engineer = FeatureEngineer()
        label_name = "label_soup_tp_before_sl"
        combined_frames: list[pd.DataFrame] = []
        errors: list[str] = []
        symbols_used = 0
        fetch_failed = 0
        skipped = 0
        dataset_rows = 0
        truncated = False

        max_rows = max(0, _as_int(self._config.training.bootstrap_dataset_max_rows, default=0))
        per_symbol_rows_cap = max(
            0,
            _as_int(self._config.training.bootstrap_per_symbol_rows_cap, default=0),
        )
        batch_size = max(20, _as_int(self._config.training.bootstrap_batch_size, default=200))
        protocol_payload = self._try_train_models_from_learning_protocol(
            trainer=trainer,
            symbols=symbols,
            lookback_days=lookback_days,
            artifact_path=artifact_path,
        )
        errors.extend(_string_list(protocol_payload.get("errors", [])))
        protocol_fallback_reason = str(protocol_payload.get("fallback_reason", "")).strip()
        if bool(protocol_payload.get("ok", False)):
            return protocol_payload

        for symbol in symbols:
            try:
                bars = self._provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
            except Exception:
                fetch_failed += 1
                continue
            if bars.empty:
                skipped += 1
                continue
            try:
                intraday_1m, intraday_5m = self._fetch_intraday_summaries(
                    symbol=symbol,
                    lookback_days=max(lookback_days, len(bars) + 5),
                )
                market_index = (
                    build_market_relative_frame(
                        self._provider,
                        bars=bars,
                        config=self._config.market_relative_feature,
                    )
                    if bool(self._config.market_relative_feature.enabled)
                    else None
                )
                features = engineer.transform(
                    bars,
                    intraday_1m=intraday_1m,
                    intraday_5m=intraday_5m,
                    market_index=market_index,
                )
                labels = build_soup_labels(
                    bars=bars,
                    take_profit_pct=self._config.labels.take_profit_pct,
                    stop_loss_pct=self._config.labels.stop_loss_pct,
                    horizon_days=self._config.labels.horizon_days,
                    price_basis=self._config.labels.pnl_price_basis,
                    exclude_untradable=self._config.labels.exclude_untradable,
                )
            except Exception:
                skipped += 1
                continue
            aligned = features.join(labels.rename(label_name), how="inner")
            aligned = aligned.dropna(subset=[label_name])
            if aligned.empty:
                skipped += 1
                continue
            if per_symbol_rows_cap > 0:
                aligned = aligned.tail(per_symbol_rows_cap)
            if aligned.empty:
                skipped += 1
                continue
            aligned.index = pd.MultiIndex.from_arrays(
                [[symbol] * len(aligned), list(aligned.index)],
                names=["symbol", "date"],
            )
            combined_frames.append(aligned)
            symbols_used += 1
            dataset_rows += int(len(aligned))
            if len(combined_frames) >= batch_size:
                combined_frames = [pd.concat(combined_frames, axis=0)]
            if max_rows > 0 and dataset_rows >= max_rows:
                truncated = True
                break

        if not combined_frames:
            return {
                "ok": False,
                "status": "failed_empty_dataset",
                "artifact_path": artifact_path or self._config.training.artifact_path,
                "symbols_used": symbols_used,
                "dataset_rows": 0,
                "truncated": False,
                "errors": errors + ["empty_dataset"],
                "input_mode": "bars",
                "attempted": bool(protocol_payload.get("attempted", False)),
                "protocol_fallback_reason": protocol_fallback_reason,
            }

        dataset = pd.concat(combined_frames, axis=0)
        labels = dataset[label_name]
        features = dataset.drop(columns=[label_name])
        try:
            result = trainer.train_on_feature_label(features=features, labels=labels)
            output_path = artifact_path or self._config.training.artifact_path
            result.artifact.save(output_path)
            status = "ok" if not truncated else "ok_truncated"
            return {
                "ok": True,
                "status": status,
                "artifact_path": output_path,
                "symbols_used": symbols_used,
                "dataset_rows": int(len(dataset)),
                "truncated": truncated,
                "result": result.to_dict(),
                "errors": errors,
                "fetch_failed": fetch_failed,
                "skipped": skipped,
                "input_mode": "bars",
                "attempted": bool(protocol_payload.get("attempted", False)),
                "protocol_fallback_reason": protocol_fallback_reason,
            }
        except Exception as exc:
            errors.append(f"train_failed:{exc.__class__.__name__}")
            return {
                "ok": False,
                "status": "failed_train_exception",
                "artifact_path": artifact_path or self._config.training.artifact_path,
                "symbols_used": symbols_used,
                "dataset_rows": int(len(dataset)),
                "truncated": truncated,
                "errors": errors,
                "error": str(exc),
                "fetch_failed": fetch_failed,
                "skipped": skipped,
                "input_mode": "bars",
                "attempted": bool(protocol_payload.get("attempted", False)),
                "protocol_fallback_reason": protocol_fallback_reason,
            }

    def run_walk_forward(self, symbol: str, lookback_days: int = 800) -> dict[str, object]:
        bars = self._provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
        engine = WalkForwardEngine(
            training=self._config.training,
            labels=self._config.labels,
            walk_forward=self._config.walk_forward,
            matcher=self._config.backtest_matcher,
            limit_rule=self._config.limit_rule,
            models=self._config.models,
            provider=self._provider,
            market_relative_feature=self._config.market_relative_feature,
        )
        report = engine.run_on_bars(bars)
        return {"symbol": symbol, "report": report.to_dict()}

    def generate_baseline_report(
        self,
        symbol: str,
        lookback_days: int = 800,
        output_path: str | None = None,
    ) -> dict[str, object]:
        normalized_symbol = str(symbol).strip()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        bars = self._provider.fetch_daily_bars(
            symbol=normalized_symbol, lookback_days=lookback_days
        )
        walk_forward = self.run_walk_forward(symbol=normalized_symbol, lookback_days=lookback_days)
        provider_status = self._pipeline.provider_status()
        dependency_status = inspect_model_backend_dependencies()
        report = {
            "generated_at": datetime.now().isoformat(),
            "symbol": normalized_symbol,
            "lookback_days": lookback_days,
            "baseline_type": _baseline_type_from_status(provider_status),
            "model_status": {
                "predictor_mode": str(provider_status.get("predictor_mode", "")),
                "lgbm_backend": str(provider_status.get("lgbm_backend", "")),
                "xgb_backend": str(provider_status.get("xgb_backend", "")),
                "lgbm_load_source": str(provider_status.get("lgbm_load_source", "")),
                "xgb_load_source": str(provider_status.get("xgb_load_source", "")),
                "native_sidecar_fallback_used": bool(
                    provider_status.get("native_sidecar_fallback_used", False)
                ),
                "degraded_model_mode": bool(provider_status.get("degraded_model_mode", False)),
                "artifact_path": str(provider_status.get("artifact_path", "")),
            },
            "dependency_status": dependency_status,
            "background_factor_coverage": _background_factor_coverage(bars),
            "walk_forward": walk_forward.get("report", {}),
        }
        target = Path(output_path or self._config.training.baseline_report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["output_path"] = str(target)
        return report

    def generate_training_evaluation_report(
        self,
        symbol: str,
        lookback_days: int = 800,
        output_path: str | None = None,
    ) -> dict[str, object]:
        return self._training_service.generate_training_evaluation_report(
            symbol=symbol,
            lookback_days=lookback_days,
            output_path=output_path,
        )

    def generate_label_conflict_shadow_report(
        self,
        symbol: str,
        lookback_days: int = 800,
        output_path: str | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_label_conflict_shadow_report(
            symbol=symbol,
            lookback_days=lookback_days,
            output_path=output_path,
        )

    def generate_m9_failure_retention_report(
        self,
        output_path: str | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_m9_failure_retention_report(
            output_path=output_path
        )

    def generate_portfolio_execution_report(
        self,
        *,
        output_path: str | None = None,
        symbols: list[str] | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_portfolio_execution_report(
            output_path=output_path,
            symbols=symbols,
        )

    def _acceptance_benchmark_symbols(
        self,
        *,
        seed_symbols: list[str] | None = None,
        limit: int = 5,
    ) -> list[str]:
        return self._acceptance_service._acceptance_benchmark_symbols(
            seed_symbols=seed_symbols,
            limit=limit,
        )

    def _week5_report_has_acceptance_evidence(self, report: Mapping[str, object]) -> bool:
        return self._acceptance_service._week5_report_has_acceptance_evidence(report)

    def _build_acceptance_week5_fallback_report(
        self,
        *,
        symbols: list[str],
    ) -> dict[str, object]:
        return self._acceptance_service._build_acceptance_week5_fallback_report(
            symbols=symbols
        )

    def _build_staged_take_profit_acceptance_summary(self) -> dict[str, object]:
        return self._acceptance_service._build_staged_take_profit_acceptance_summary()

    def _simulate_staged_take_profit_path(
        self,
        *,
        prices: list[float],
        first_take_profit: float,
        second_take_profit: float,
        trailing_stop_pct: float,
        stop_loss_pct: float,
    ) -> dict[str, object]:
        return self._acceptance_service._simulate_staged_take_profit_path(
            prices=prices,
            first_take_profit=first_take_profit,
            second_take_profit=second_take_profit,
            trailing_stop_pct=trailing_stop_pct,
            stop_loss_pct=stop_loss_pct,
        )

    def _simulate_single_exit_path(
        self,
        *,
        prices: list[float],
        second_take_profit: float,
        trailing_stop_pct: float,
        stop_loss_pct: float,
    ) -> dict[str, object]:
        return self._acceptance_service._simulate_single_exit_path(
            prices=prices,
            second_take_profit=second_take_profit,
            trailing_stop_pct=trailing_stop_pct,
            stop_loss_pct=stop_loss_pct,
        )

    def _build_hrp_shadow_acceptance_summary(
        self,
        *,
        symbols: list[str],
    ) -> dict[str, object]:
        return self._acceptance_service._build_hrp_shadow_acceptance_summary(
            symbols=symbols
        )

    def _return_series_max_drawdown(self, returns: pd.Series) -> float:
        return self._acceptance_service._return_series_max_drawdown(returns)

    def generate_phase_checkpoint(
        self,
        phase: str,
        baseline_report_path: str | None = None,
        output_path: str | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_phase_checkpoint(
            phase=phase,
            baseline_report_path=baseline_report_path,
            output_path=output_path,
        )

    def generate_v13_acceptance_report(
        self,
        *,
        baseline_report_path: str | None = None,
        output_path: str | None = None,
        week5_report: Mapping[str, object] | None = None,
        phase_checkpoints: Mapping[str, Mapping[str, object]] | None = None,
        m9_failure_retention_report: Mapping[str, object] | None = None,
        portfolio_execution_report: Mapping[str, object] | None = None,
        label_conflict_shadow_report: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_v13_acceptance_report(
            baseline_report_path=baseline_report_path,
            output_path=output_path,
            week5_report=week5_report,
            phase_checkpoints=phase_checkpoints,
            m9_failure_retention_report=m9_failure_retention_report,
            portfolio_execution_report=portfolio_execution_report,
            label_conflict_shadow_report=label_conflict_shadow_report,
        )

    def generate_v13_acceptance_bundle(
        self,
        *,
        symbol: str,
        lookback_days: int = 800,
        baseline_output_path: str | None = None,
        v13_output_path: str | None = None,
        run_week5_scan: bool = False,
        week5_symbols: list[str] | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_v13_acceptance_bundle(
            symbol=symbol,
            lookback_days=lookback_days,
            baseline_output_path=baseline_output_path,
            v13_output_path=v13_output_path,
            run_week5_scan=run_week5_scan,
            week5_symbols=week5_symbols,
        )

    def generate_acceptance_release_gate_report(
        self,
        *,
        v13_report_path: str | None = None,
        output_path: str | None = None,
        closed_loop_smoke_passed: bool = False,
        closed_loop_smoke_detail: str = "",
    ) -> dict[str, object]:
        return self._acceptance_service.generate_acceptance_release_gate_report(
            v13_report_path=v13_report_path,
            output_path=output_path,
            closed_loop_smoke_passed=closed_loop_smoke_passed,
            closed_loop_smoke_detail=closed_loop_smoke_detail,
        )

    def generate_phase_d_status_report(
        self,
        *,
        output_path: str | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_phase_d_status_report(output_path=output_path)

    def generate_phase_d6_registry_report(
        self,
        *,
        output_path: str | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.generate_phase_d6_registry_report(output_path=output_path)

    def _build_phase_d_research_dataset(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]], pd.DataFrame]:
        resolved_model_id = self._resolve_phase_d_research_model_id(model_id=model_id)
        dataset = self._build_shadow_dataset_builder().build_for_model(
            model_id=resolved_model_id,
            split_names=split_names,
            max_rows=max_rows,
            include_baseline_scores=True,
        )
        records = self._enrich_phase_d_research_records_with_market_fields(
            [row.to_dict() for row in dataset.rows]
        )
        frame = pd.DataFrame(records)
        return (
            {
                "model_id": dataset.model_id,
                "dataset_manifest_id": dataset.dataset_manifest_id,
                "feature_schema_id": dataset.feature_schema_id,
                "label_policy_id": dataset.label_policy_id,
                "requested_split_names": list(dataset.requested_split_names),
                "row_count": dataset.row_count,
                "split_counts": dict(dataset.split_counts),
            },
            records,
            frame,
        )

    def _build_phase_d_contextual_research_dataset(
        self,
        *,
        model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        min_rows_per_symbol: int = 1,
    ) -> tuple[dict[str, object], list[dict[str, object]], pd.DataFrame]:
        normalized_split_names = [
            str(name).strip()
            for name in (split_names or [])
            if str(name).strip()
        ]
        dataset_meta, records, frame = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        if not normalized_split_names or self._phase_d_research_has_symbol_history(
            records=records,
            min_rows_per_symbol=min_rows_per_symbol,
        ):
            return dataset_meta, records, frame

        context_meta, context_records, context_frame = self._build_phase_d_research_dataset(
            model_id=model_id,
            split_names=None,
            max_rows=max_rows,
        )
        context_meta["requested_split_names"] = normalized_split_names
        context_meta["requested_row_count"] = int(dataset_meta.get("row_count", len(records)))
        context_meta["requested_split_counts"] = dict(
            dataset_meta.get("split_counts", {})
            if isinstance(dataset_meta.get("split_counts"), Mapping)
            else {}
        )
        context_meta["context_expanded"] = True
        context_meta["context_row_count"] = int(context_meta.get("row_count", len(context_records)))
        context_meta["context_split_names"] = list(context_meta.get("split_counts", {}).keys())
        return context_meta, context_records, context_frame

    def _phase_d_research_has_symbol_history(
        self,
        *,
        records: Sequence[Mapping[str, object]],
        min_rows_per_symbol: int,
    ) -> bool:
        if min_rows_per_symbol <= 1:
            return bool(records)
        symbol_counts: dict[str, int] = {}
        for record in records:
            symbol = str(record.get("symbol", "")).strip()
            if not symbol:
                continue
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        return any(count >= min_rows_per_symbol for count in symbol_counts.values())

    def _resolve_phase_d_research_model_id(self, *, model_id: str = "") -> str:
        normalized_model_id = str(model_id).strip()
        if normalized_model_id:
            return normalized_model_id
        champion = self._model_registry.active_champion()
        if champion is not None:
            return champion.model_id
        records = self._model_registry.list_records(limit=1)
        if records:
            return records[0].model_id
        raise ValueError("no registered model available for phase D research")

    def _default_phase_d_report_path(self, research_id: str) -> str:
        normalized = str(research_id).strip().lower() or "phase_d_research"
        return str(
            self._resolve_evolution_path(
                f"artifacts/research/phase_d/{normalized}_latest.json"
            )
        )

    def _default_phase_d_bundle_dir(self, research_id: str) -> str:
        normalized = str(research_id).strip().lower() or "phase_d_research"
        return str(self._resolve_evolution_path(f"artifacts/research/phase_d/{normalized}_bundle"))

    def _record_phase_d_research_event(self, *, payload: Mapping[str, object]) -> None:
        self._record_audit_event(
            event_type="phase_d_research_report_built",
            level="info",
            message="phase D research sidecar report built",
            payload={
                "research_id": str(payload.get("research_id", "")),
                "model_id": str(payload.get("model_id", "")),
                "dataset_manifest_id": str(payload.get("dataset_manifest_id", "")),
                "status": str(payload.get("status", "")),
                "output_path": str(payload.get("output_path", "")),
            },
        )

    def _enrich_phase_d_research_records_with_market_fields(
        self,
        records: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        normalized_records = [dict(record) for record in records]
        if not normalized_records:
            return []
        requested_dates: dict[str, list[date]] = {}
        for record in normalized_records:
            symbol = str(record.get("symbol", "")).strip()
            trade_date = self._coerce_phase_d_trade_date(record)
            if not symbol or trade_date is None:
                continue
            requested_dates.setdefault(symbol, []).append(trade_date)

        market_index: dict[tuple[str, str], dict[str, float]] = {}
        for symbol, dates in requested_dates.items():
            unique_dates = sorted({item for item in dates})
            if not unique_dates:
                continue
            lookback_days = max(30, (unique_dates[-1] - unique_dates[0]).days + 10)
            try:
                bars = self._provider.fetch_daily_bars(symbol, lookback_days=lookback_days)
            except Exception:
                continue
            normalized_bars = self._normalize_phase_d_market_bars(bars)
            for _, row in normalized_bars.iterrows():
                row_trade_date = str(row.get("trade_date", "")).strip()
                if not row_trade_date:
                    continue
                market_index[(symbol, row_trade_date)] = {
                    "open": float(row.get("open", row.get("close", 0.0))),
                    "high": float(row.get("high", row.get("close", 0.0))),
                    "low": float(row.get("low", row.get("close", 0.0))),
                    "close": float(row.get("close", 0.0)),
                    "volume": float(row.get("volume", 0.0)),
                    "amount": float(
                        row.get(
                            "amount",
                            float(row.get("close", 0.0)) * float(row.get("volume", 0.0)),
                        )
                    ),
                }

        enriched: list[dict[str, object]] = []
        for record in normalized_records:
            item = dict(record)
            symbol = str(item.get("symbol", "")).strip()
            trade_date = self._coerce_phase_d_trade_date(item)
            key = (symbol, trade_date.isoformat()) if symbol and trade_date is not None else None
            if key is not None and key in market_index:
                for field_name, value in market_index[key].items():
                    item.setdefault(field_name, value)
            enriched.append(item)
        return enriched

    def _coerce_phase_d_trade_date(self, record: Mapping[str, object]) -> date | None:
        for key in ("trade_date", "date", "decision_time"):
            raw = record.get(key)
            if isinstance(raw, str) and raw.strip():
                text = raw.strip()
                if key == "decision_time" and "T" in text:
                    text = text.split("T", 1)[0]
                try:
                    return datetime.fromisoformat(text).date()
                except ValueError:
                    continue
        return None

    def _normalize_phase_d_market_bars(self, bars: object) -> pd.DataFrame:
        if not isinstance(bars, pd.DataFrame) or bars.empty:
            return pd.DataFrame()
        frame = bars.copy()
        if "trade_date" in frame.columns:
            date_series = pd.to_datetime(frame["trade_date"], errors="coerce")
        elif "date" in frame.columns:
            date_series = pd.to_datetime(frame["date"], errors="coerce")
        else:
            index_name = str(frame.index.name or "").strip().lower()
            if index_name in {"date", "trade_date", "datetime"}:
                date_series = pd.to_datetime(frame.index, errors="coerce")
            else:
                return pd.DataFrame()
        if isinstance(date_series, pd.Series):
            trade_dates = date_series.dt.date.astype(str)
        else:
            trade_dates = pd.Series(date_series.date.astype(str), index=frame.index)
        frame = frame.assign(trade_date=trade_dates)
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "close" not in frame.columns:
            return pd.DataFrame()
        if "open" not in frame.columns:
            frame["open"] = frame["close"]
        if "high" not in frame.columns:
            frame["high"] = frame["close"]
        if "low" not in frame.columns:
            frame["low"] = frame["close"]
        if "volume" not in frame.columns:
            frame["volume"] = 0.0
        if "amount" not in frame.columns:
            frame["amount"] = frame["close"].fillna(0.0) * frame["volume"].fillna(0.0)
        frame = frame.dropna(subset=["trade_date", "close"])
        return frame

    def execute_command(self, envelope: CommandEnvelope) -> dict[str, object]:
        result = self._command_processor.execute(envelope)
        payload = result.to_dict()
        command_trace_id = envelope.command_id
        if not result.accepted:
            self._record_audit_event(
                event_type="command_rejected",
                trace_id=command_trace_id,
                level="warn",
                message=result.message,
                payload={
                    "action": envelope.action,
                    "code": result.code,
                },
            )
            return payload

        action = envelope.action.upper()
        timestamp = datetime.fromtimestamp(envelope.timestamp)
        trace_id = command_trace_id
        command_update = self._apply_command_side_effect(
            action=action,
            command_payload=envelope.payload,
            timestamp=timestamp,
            trace_id=trace_id,
        )
        if command_update is not None:
            payload["command_update"] = command_update
        learning_outcome_update: dict[str, object] | None = None
        if command_update is not None:
            update_summary = self._update_learning_outcomes_from_command_update(
                command_update=command_update,
                timestamp=timestamp,
            )
            if _as_int(update_summary.get("updated"), default=0) > 0:
                learning_outcome_update = update_summary
                payload["learning_outcome_update"] = update_summary
        self._persist_runtime_state_to_disk()
        audit_payload: dict[str, object] = {
            "action": action,
            "code": result.code,
        }
        if command_update is not None:
            audit_payload["command_update"] = command_update
        if learning_outcome_update is not None:
            audit_payload["learning_outcome_update"] = learning_outcome_update
        self._record_audit_event(
            event_type="command_accepted",
            trace_id=trace_id,
            payload=audit_payload,
        )
        return payload

    def portfolio_positions(self) -> list[dict[str, object]]:
        self._refresh_runtime_state_from_disk_if_changed()
        return self._portfolio.positions()

    def portfolio_trades(self, limit: int = 100) -> list[dict[str, object]]:
        self._refresh_runtime_state_from_disk_if_changed()
        return self._portfolio.trades(limit=limit)

    def _build_signal_payload_with_recommendation_ids(
        self,
        *,
        signals: list[PipelineSignal],
        trace_id: str,
        timestamp: datetime,
    ) -> list[dict[str, object]]:
        timestamp_iso = timestamp.isoformat()
        payload: list[dict[str, object]] = []
        bars_cache: dict[str, pd.DataFrame] = {}
        for idx, signal in enumerate(signals):
            row = asdict(signal)
            normalized_symbol = (
                _normalize_a_share_symbol(signal.symbol) or str(signal.symbol).strip()
            )
            snapshot_id = _extract_learning_snapshot_id(signal.decision_trace)
            recommendation_id = _build_recommendation_id(
                trace_id=trace_id,
                symbol=normalized_symbol,
                strategy=signal.strategy,
                index=idx,
            )
            row["recommendation_id"] = recommendation_id
            row["recommendation_time"] = timestamp_iso
            if snapshot_id:
                row["snapshot_id"] = snapshot_id
            trade_plan = self._build_trade_plan_for_signal(
                signal=signal,
                timestamp=timestamp,
                recommendation_id=recommendation_id,
                bars_cache=bars_cache,
            )
            if trade_plan:
                row["trade_plan"] = trade_plan
            payload.append(row)
            snapshot = {
                "recommendation_id": recommendation_id,
                "symbol": normalized_symbol,
                "strategy": str(signal.strategy).strip(),
                "action": str(signal.action).strip().lower(),
                "score": round(float(signal.score), 4),
                "target_position": round(float(signal.target_position), 6),
                "recommendation_time": timestamp_iso,
                "trace_id": trace_id,
            }
            if snapshot_id:
                snapshot["snapshot_id"] = snapshot_id
            if trade_plan:
                snapshot["trade_plan"] = trade_plan
            self._recommendation_snapshot_by_id[recommendation_id] = snapshot
            normalized = _normalize_a_share_symbol(normalized_symbol)
            if normalized:
                self._recommendation_latest_id_by_symbol[normalized] = recommendation_id
        while len(self._recommendation_snapshot_by_id) > 4000:
            oldest = next(iter(self._recommendation_snapshot_by_id))
            self._recommendation_snapshot_by_id.pop(oldest, None)
        return payload

    def _build_trade_plan_for_signal(
        self,
        *,
        signal: PipelineSignal,
        timestamp: datetime,
        recommendation_id: str = "",
        bars_cache: dict[str, pd.DataFrame] | None = None,
    ) -> dict[str, object]:
        symbol = _normalize_a_share_symbol(signal.symbol)
        if not symbol:
            return {}
        normalized_action = str(signal.action).strip().lower()
        if normalized_action not in {"buy", "watch"}:
            return {}

        cache = bars_cache if bars_cache is not None else {}
        reference_price = self._resolve_latest_close_price(symbol=symbol, bars_cache=cache)
        stop_loss_pct = max(
            0.0,
            _as_float(self._config.soup_strategy.stop_loss, default=5.0),
        )
        take_profit_levels = sorted(
            {
                round(_as_float(item, default=0.0), 4)
                for item in self._config.soup_strategy.take_profit
                if _as_float(item, default=0.0) > 0
            }
        )
        if not take_profit_levels:
            take_profit_levels = [max(stop_loss_pct, 5.0)]
        max_hold_days = max(1, _as_int(self._config.soup_strategy.max_hold_days, default=10))
        invalid_after = timestamp + timedelta(days=max_hold_days)

        plan: dict[str, object] = {
            "version": "trade_plan_v1",
            "symbol": symbol,
            "strategy": str(signal.strategy).strip().lower() or "trend",
            "action": normalized_action,
            "recommendation_id": recommendation_id.strip(),
            "generated_at": timestamp.isoformat(),
            "max_hold_days": max_hold_days,
            "invalid_after": invalid_after.isoformat(),
            "stop_loss_pct": round(stop_loss_pct, 4),
            "take_profit_pct": take_profit_levels,
            "invalid_conditions": [
                "price_breaks_stop_loss",
                "recommendation_not_triggered_before_invalid_after",
                "model_or_risk_degrades_to_sell_alert",
                "position_exceeds_max_hold_days",
            ],
        }
        if reference_price is None or reference_price <= 0:
            plan["status"] = "price_unavailable"
            return plan

        ref = round(float(reference_price), 6)
        entry_low = round(ref * 0.985, 6)
        entry_high = round(ref * 1.005, 6)
        stop_price = round(ref * (1.0 - stop_loss_pct / 100.0), 6)
        take_prices = [round(ref * (1.0 + pct / 100.0), 6) for pct in take_profit_levels]
        plan.update(
            {
                "status": "ready",
                "reference_price": ref,
                "entry_low": entry_low,
                "entry_high": entry_high,
                "entry_range": [entry_low, entry_high],
                "stop_loss_price": stop_price,
                "take_profit_prices": take_prices,
            }
        )
        for idx, price in enumerate(take_prices[:3], start=1):
            plan[f"take_profit_{idx}"] = price
        return plan

    def _extract_recommendation_trade_plan(
        self,
        *,
        signal: PipelineSignal,
        timestamp: datetime,
        bars_cache: dict[str, pd.DataFrame],
    ) -> dict[str, object]:
        symbol = _normalize_a_share_symbol(signal.symbol)
        recommendation_id = ""
        if symbol:
            recommendation_id = str(
                self._recommendation_latest_id_by_symbol.get(symbol, "")
            ).strip()
        return self._build_trade_plan_for_signal(
            signal=signal,
            timestamp=timestamp,
            recommendation_id=recommendation_id,
            bars_cache=bars_cache,
        )

    def _append_recommendation_event(
        self,
        record: dict[str, object],
        *,
        timestamp: datetime,
        event: str,
        source: str,
        trace_id: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        raw_events = record.get("events")
        events = list(raw_events) if isinstance(raw_events, list) else []
        payload: dict[str, object] = {
            "timestamp": timestamp.isoformat(),
            "event": event,
            "source": source,
            "trace_id": trace_id,
        }
        if details:
            for key, value in details.items():
                if value not in (None, ""):
                    payload[str(key)] = value
        events.append(payload)
        record["events"] = events[-40:]

    def _merge_recommendation_extra_fields(
        self,
        record: dict[str, object],
        existing: Mapping[str, object],
        *,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        for key in _RECOMMENDATION_EXTRA_FIELDS:
            value = existing.get(key)
            if value not in (None, "", [], {}):
                record[key] = value
        raw_events = existing.get("events")
        if isinstance(raw_events, list):
            record["events"] = list(raw_events)[-40:]
        if not extra:
            return
        for key, value in extra.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, float):
                if math.isnan(value) or math.isinf(value):
                    continue
            record[str(key)] = value

    def _sync_recommendation_position_metrics(
        self,
        record: dict[str, object],
        *,
        timestamp: datetime,
    ) -> None:
        symbol = str(record.get("symbol", "")).strip()
        if not symbol:
            return
        position = self._portfolio.position_map().get(symbol)
        if not isinstance(position, dict):
            return
        if _as_float(record.get("entry_price"), default=0.0) <= 0:
            trade_reference = self._trade_reference_for_open_position(symbol)
            if trade_reference:
                self._merge_recommendation_extra_fields(record, {}, extra=trade_reference)
        entry_price = _as_float(position.get("entry_price"), default=0.0)
        if entry_price > 0:
            record.setdefault("entry_price", round(entry_price, 6))
            record.setdefault("entry_triggered_at", str(position.get("opened_at", "")))
        quantity = _as_int(position.get("quantity"), default=0)
        if quantity > 0:
            record.setdefault("entry_quantity", quantity)
        bars_cache: dict[str, pd.DataFrame] = {}
        latest_price = self._resolve_latest_close_price(symbol=symbol, bars_cache=bars_cache)
        if latest_price is not None and latest_price > 0:
            record["last_price"] = round(latest_price, 6)
            if entry_price > 0:
                record["unrealized_return_pct"] = round(latest_price / entry_price - 1.0, 6)
                record["current_return_pct"] = record["unrealized_return_pct"]
        opened_at = _parse_iso_datetime(position.get("opened_at"))
        if opened_at is not None:
            record["holding_days"] = max(0, (timestamp.date() - opened_at.date()).days)

    def _trade_reference_for_open_position(self, symbol: str) -> dict[str, object]:
        normalized = _normalize_a_share_symbol(symbol)
        if not normalized:
            return {}
        for trade in reversed(self._portfolio.trades(limit=2000)):
            if not isinstance(trade, dict) or str(trade.get("symbol", "")).strip() != normalized:
                continue
            if str(trade.get("side", "")).strip().lower() != "buy":
                continue
            entry_price = _as_float(trade.get("entry_price"), default=0.0)
            return {
                "entry_price": round(entry_price, 6) if entry_price > 0 else None,
                "entry_quantity": _as_int(trade.get("quantity"), default=0),
                "entry_trade_id": str(trade.get("trade_id", "")).strip(),
                "entry_triggered_at": str(trade.get("timestamp", "")).strip(),
                "outcome_status": "open",
            }
        return {}

    def _close_recommendation_record_metrics(
        self,
        record: dict[str, object],
        *,
        exit_price: float,
        timestamp: datetime,
        reason: str,
    ) -> None:
        entry_price = _as_float(record.get("entry_price"), default=0.0)
        if entry_price > 0 and exit_price > 0:
            realized = round(exit_price / entry_price - 1.0, 6)
            record["realized_return_pct"] = realized
            record["current_return_pct"] = realized
            record["outcome_status"] = "win" if realized > 0 else "loss"
        elif exit_price > 0:
            record["exit_price"] = round(exit_price, 6)
            record.setdefault("outcome_status", "unknown")
        record["closed_at"] = timestamp.isoformat()
        record["closed_reason"] = reason
        opened_at = _parse_iso_datetime(record.get("entry_triggered_at")) or _parse_iso_datetime(
            record.get("first_recommended_at")
        )
        if opened_at is not None:
            record["holding_days"] = max(0, (timestamp.date() - opened_at.date()).days)

    def _trade_reference_for_closed_position(self, symbol: str) -> dict[str, object]:
        normalized = _normalize_a_share_symbol(symbol)
        if not normalized:
            return {}
        latest_buy: dict[str, object] = {}
        latest_sell: dict[str, object] = {}
        for trade in reversed(self._portfolio.trades(limit=2000)):
            if not isinstance(trade, dict) or str(trade.get("symbol", "")).strip() != normalized:
                continue
            side = str(trade.get("side", "")).strip().lower()
            if side == "sell" and not latest_sell:
                latest_sell = trade
                continue
            if side == "buy" and not latest_buy:
                latest_buy = trade
                if latest_sell:
                    break
        entry_price = _as_float(latest_buy.get("entry_price"), default=0.0)
        exit_price = _as_float(latest_sell.get("exit_price"), default=0.0)
        if entry_price <= 0 and exit_price <= 0:
            return {}
        return {
            "entry_price": round(entry_price, 6) if entry_price > 0 else None,
            "entry_quantity": _as_int(latest_buy.get("quantity"), default=0),
            "entry_trade_id": str(latest_buy.get("trade_id", "")).strip(),
            "entry_triggered_at": str(latest_buy.get("timestamp", "")).strip(),
            "exit_price": round(exit_price, 6) if exit_price > 0 else None,
            "exit_quantity": _as_int(latest_sell.get("exit_quantity"), default=0),
            "exit_trade_id": str(latest_sell.get("trade_id", "")).strip(),
            "closed_at": str(latest_sell.get("timestamp", "")).strip(),
            "closed_reason": str(latest_sell.get("reason", "")).strip(),
        }

    def recommendation_lifecycle(self, status: str = "", limit: int = 120) -> dict[str, object]:
        self._refresh_runtime_state_from_disk_if_changed()
        capped_limit = max(1, min(limit, 1000))
        status_filter = str(status).strip().lower()
        if status_filter and status_filter not in _RECOMMENDATION_STATUSES:
            status_filter = ""

        position_map = self._portfolio.position_map()
        open_symbols = {str(symbol).strip() for symbol in position_map.keys()}
        rows: list[dict[str, object]] = []
        for symbol, item in self._recommendation_lifecycle.items():
            normalized_status = _normalize_recommendation_status(
                item.get("status"),
                default="watching",
            )
            if symbol in open_symbols and normalized_status in {"watching", "recommended"}:
                normalized_status = "holding"
            if status_filter and normalized_status != status_filter:
                continue
            trade_plan = item.get("trade_plan") if isinstance(item.get("trade_plan"), dict) else {}
            raw_events = item.get("events")
            row = {
                "symbol": symbol,
                "strategy": str(item.get("strategy", "manual")).strip() or "manual",
                "status": normalized_status,
                "first_recommended_at": str(item.get("first_recommended_at", "")),
                "last_signal_at": str(item.get("last_signal_at", "")),
                "last_signal_score": _as_float(item.get("last_signal_score"), default=0.0),
                "last_signal_action": str(item.get("last_signal_action", "")),
                "last_manual_update_at": str(item.get("last_manual_update_at", "")),
                "updated_at": str(item.get("updated_at", "")),
                "last_source": str(item.get("last_source", "")),
                "last_trace_id": str(item.get("last_trace_id", "")),
                "note": str(item.get("note", "")),
                "is_open_position": symbol in open_symbols,
                "recommendation_id": str(item.get("recommendation_id", "")),
                "trade_plan": dict(trade_plan) if isinstance(trade_plan, dict) else {},
                "entry_triggered_at": str(item.get("entry_triggered_at", "")),
                "entry_price": _as_float(item.get("entry_price"), default=0.0),
                "entry_quantity": _as_int(item.get("entry_quantity"), default=0),
                "entry_trade_id": str(item.get("entry_trade_id", "")),
                "exit_alert_at": str(item.get("exit_alert_at", "")),
                "exit_alert_reason": str(item.get("exit_alert_reason", "")),
                "exit_price": _as_float(item.get("exit_price"), default=0.0),
                "exit_quantity": _as_int(item.get("exit_quantity"), default=0),
                "exit_trade_id": str(item.get("exit_trade_id", "")),
                "closed_at": str(item.get("closed_at", "")),
                "closed_reason": str(item.get("closed_reason", "")),
                "last_price": _as_float(item.get("last_price"), default=0.0),
                "last_exit_action": str(item.get("last_exit_action", "")),
                "realized_return_pct": _as_float(item.get("realized_return_pct"), default=0.0),
                "realized_pnl_amount": _as_float(item.get("realized_pnl_amount"), default=0.0),
                "unrealized_return_pct": _as_float(
                    item.get("unrealized_return_pct"),
                    default=0.0,
                ),
                "current_return_pct": _as_float(item.get("current_return_pct"), default=0.0),
                "holding_days": _as_int(item.get("holding_days"), default=0),
                "outcome_status": str(item.get("outcome_status", "")),
                "events": list(raw_events)[-12:] if isinstance(raw_events, list) else [],
            }
            rows.append(row)

        rows = sorted(
            rows,
            key=lambda row: _report_timestamp({"timestamp": row.get("updated_at", "")}),
            reverse=True,
        )[:capped_limit]

        status_breakdown: dict[str, int] = {}
        open_linked = 0
        active_records = 0
        terminal_records = 0
        closed_records = 0
        win_records = 0
        realized_returns: list[float] = []
        open_returns: list[float] = []
        holding_days_values: list[int] = []
        for item in rows:
            item_status = str(item.get("status", "")).strip().lower() or "watching"
            status_breakdown[item_status] = status_breakdown.get(item_status, 0) + 1
            if bool(item.get("is_open_position", False)):
                open_linked += 1
            if item_status in _RECOMMENDATION_ACTIVE_STATUSES or bool(
                item.get("is_open_position", False)
            ):
                active_records += 1
            if item_status in _RECOMMENDATION_TERMINAL_STATUSES:
                terminal_records += 1
            if item_status in {"sold", "closed"}:
                closed_records += 1
                realized = _as_float(item.get("realized_return_pct"), default=0.0)
                realized_returns.append(realized)
                if str(item.get("outcome_status", "")).strip().lower() == "win" or realized > 0:
                    win_records += 1
            if bool(item.get("is_open_position", False)):
                open_returns.append(_as_float(item.get("current_return_pct"), default=0.0))
            holding_days = _as_int(item.get("holding_days"), default=0)
            if holding_days > 0:
                holding_days_values.append(holding_days)

        total_realized = round(sum(realized_returns), 6) if realized_returns else 0.0
        avg_realized = (
            round(total_realized / len(realized_returns), 6) if realized_returns else 0.0
        )
        avg_open_return = (
            round(sum(open_returns) / len(open_returns), 6) if open_returns else 0.0
        )
        avg_holding_days = (
            round(sum(holding_days_values) / len(holding_days_values), 2)
            if holding_days_values
            else 0.0
        )

        return {
            "records": len(rows),
            "status_filter": status_filter or "all",
            "summary": {
                "records": len(rows),
                "status_breakdown": status_breakdown,
                "open_position_linked": open_linked,
                "active_records": active_records,
                "terminal_records": terminal_records,
                "closed_records": closed_records,
                "open_records": active_records,
                "win_records": win_records,
                "loss_records": max(0, closed_records - win_records),
                "win_rate": round(win_records / closed_records, 4)
                if closed_records > 0
                else 0.0,
                "total_realized_return_pct": total_realized,
                "avg_realized_return_pct": avg_realized,
                "avg_open_return_pct": avg_open_return,
                "avg_holding_days": avg_holding_days,
                "best_realized_return_pct": round(max(realized_returns), 6)
                if realized_returns
                else 0.0,
                "worst_realized_return_pct": round(min(realized_returns), 6)
                if realized_returns
                else 0.0,
            },
            "items": rows,
        }

    def _sync_recommendation_lifecycle_from_signals(
        self,
        signals: list[PipelineSignal],
        timestamp: datetime,
        trace_id: str,
    ) -> dict[str, object]:
        updated = 0
        touched_symbols: list[str] = []
        now_iso = timestamp.isoformat()
        bars_cache: dict[str, pd.DataFrame] = {}
        open_symbols = {
            str(symbol).strip()
            for symbol in self._portfolio.position_map().keys()
            if str(symbol).strip()
        }
        for signal in signals:
            symbol = _normalize_a_share_symbol(signal.symbol)
            if not symbol:
                continue
            action_value = str(signal.action).strip().lower()
            existing = self._recommendation_lifecycle.get(symbol, {})
            is_watch_recommendation = action_value == "watch" and (
                "model_disagreement_probe" in {str(reason) for reason in signal.reasons}
                or "recovery_degraded_consensus" in {str(reason) for reason in signal.reasons}
                or signal.target_position > 0
            )
            if (action_value != "buy" or signal.target_position <= 0) and not is_watch_recommendation:
                if symbol not in open_symbols and symbol not in self._recommendation_lifecycle:
                    continue
                alert_reason = self._recommendation_exit_reason_from_signal(signal)
                if not alert_reason:
                    continue
                previous = dict(existing)
                strategy_value = (
                    str(signal.strategy).strip().lower()
                    or str(existing.get("strategy", "trend")).strip().lower()
                    or "trend"
                )
                first_recommended_at = (
                    str(existing.get("first_recommended_at", "")).strip() or now_iso
                )
                record = {
                    "symbol": symbol,
                    "strategy": strategy_value,
                    "status": "sell_alert",
                    "first_recommended_at": first_recommended_at,
                    "last_signal_at": now_iso,
                    "last_signal_score": round(float(signal.score), 4),
                    "last_signal_action": action_value,
                    "last_manual_update_at": str(existing.get("last_manual_update_at", "")),
                    "updated_at": now_iso,
                    "last_source": "pipeline_signal_exit_review",
                    "last_trace_id": trace_id,
                    "note": str(existing.get("note", "")),
                }
                self._merge_recommendation_extra_fields(
                    record,
                    existing,
                    extra={
                        "exit_alert_at": now_iso,
                        "exit_alert_reason": alert_reason,
                        "last_exit_action": "review",
                    },
                )
                latest_price = self._resolve_latest_close_price(
                    symbol=symbol,
                    bars_cache=bars_cache,
                )
                if latest_price is not None and latest_price > 0:
                    record["last_price"] = round(latest_price, 6)
                    entry_price = _as_float(record.get("entry_price"), default=0.0)
                    if entry_price > 0:
                        record["current_return_pct"] = round(latest_price / entry_price - 1.0, 6)
                self._sync_recommendation_position_metrics(record, timestamp=timestamp)
                self._append_recommendation_event(
                    record,
                    timestamp=timestamp,
                    event="sell_alert",
                    source="pipeline_signal_exit_review",
                    trace_id=trace_id,
                    details={"reason": alert_reason, "score": round(float(signal.score), 4)},
                )
                self._recommendation_lifecycle[symbol] = record
                if record != previous:
                    updated += 1
                    touched_symbols.append(symbol)
                continue

            previous = dict(existing)
            existing_status = _normalize_recommendation_status(
                existing.get("status"),
                default="watching",
            )
            strategy_value = (
                str(signal.strategy).strip().lower()
                or str(existing.get("strategy", "trend")).strip().lower()
                or "trend"
            )
            if existing_status in {"bought", "holding", "sell_alert"} or symbol in open_symbols:
                status_value = "holding" if existing_status != "bought" else "bought"
            elif existing_status in _RECOMMENDATION_TERMINAL_STATUSES:
                status_value = "recommended"
            elif is_watch_recommendation:
                status_value = "watching"
            else:
                status_value = "recommended"
            first_recommended_at = str(existing.get("first_recommended_at", "")).strip() or now_iso
            trade_plan = self._extract_recommendation_trade_plan(
                signal=signal,
                timestamp=timestamp,
                bars_cache=bars_cache,
            )

            record = {
                "symbol": symbol,
                "strategy": strategy_value,
                "status": status_value,
                "first_recommended_at": first_recommended_at,
                "last_signal_at": now_iso,
                "last_signal_score": round(float(signal.score), 4),
                "last_signal_action": str(signal.action).strip().lower(),
                "last_manual_update_at": str(existing.get("last_manual_update_at", "")),
                "updated_at": now_iso,
                "last_source": "pipeline_signal",
                "last_trace_id": trace_id,
                "note": str(existing.get("note", "")),
            }
            self._merge_recommendation_extra_fields(
                record,
                existing,
                extra={"trade_plan": trade_plan} if trade_plan else None,
            )
            self._sync_recommendation_position_metrics(record, timestamp=timestamp)
            self._append_recommendation_event(
                record,
                timestamp=timestamp,
                event="recommended" if status_value == "recommended" else "signal_refresh",
                source="pipeline_signal",
                trace_id=trace_id,
                details={"score": round(float(signal.score), 4), "action": action_value},
            )
            self._recommendation_lifecycle[symbol] = record
            if record != previous:
                updated += 1
                touched_symbols.append(symbol)
        return {
            "updated": updated,
            "tracked": len(self._recommendation_lifecycle),
            "symbols": sorted(touched_symbols),
        }

    def _recommendation_exit_reason_from_signal(self, signal: PipelineSignal) -> str:
        action_value = str(signal.action).strip().lower()
        if action_value == "sell":
            return "model_sell_signal"
        if action_value not in {"hold", "watch"}:
            return ""
        trace = signal.decision_trace if isinstance(signal.decision_trace, dict) else {}
        provider = trace.get("provider")
        risk = trace.get("risk") or trace.get("risk_gate")
        cross_review = trace.get("cross_review") or trace.get("cross_review_gate")
        soft_degraded = False
        hard_degraded = False
        risk_blocked = False
        if isinstance(provider, dict):
            soft_degraded = bool(provider.get("soft_degraded_mode", False))
            hard_degraded = bool(provider.get("hard_degraded_mode", False))
        if isinstance(risk, dict):
            risk_action = str(risk.get("action", "")).strip().lower()
            risk_blocked = risk_action in {"freeze", "degraded", "reduce"} or bool(
                risk.get("hard_degraded_mode", False)
            )
        cross_failed = isinstance(cross_review, dict) and not bool(
            cross_review.get("passed", True)
        )
        reasons = {str(item).strip().lower() for item in signal.reasons}
        if hard_degraded or risk_blocked:
            return "risk_degraded_exit_review"
        if soft_degraded or cross_failed or "cross_review" in reasons:
            return "model_signal_invalidated"
        return ""

    def _set_recommendation_status(
        self,
        *,
        symbol: str,
        status: str,
        strategy: str,
        timestamp: datetime,
        source: str,
        trace_id: str,
        note: str = "",
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        normalized_symbol = _normalize_a_share_symbol(symbol)
        normalized_status = _normalize_recommendation_status(status, default="")
        if not normalized_symbol or not normalized_status:
            return None
        existing = self._recommendation_lifecycle.get(normalized_symbol, {})
        now_iso = timestamp.isoformat()
        strategy_value = (
            strategy.strip().lower() or str(existing.get("strategy", "manual")).strip().lower()
        )
        if not strategy_value:
            strategy_value = "manual"
        note_value = note.strip() if note.strip() else str(existing.get("note", "")).strip()
        first_recommended_at = str(existing.get("first_recommended_at", "")).strip() or now_iso
        record = {
            "symbol": normalized_symbol,
            "strategy": strategy_value,
            "status": normalized_status,
            "first_recommended_at": first_recommended_at,
            "last_signal_at": str(existing.get("last_signal_at", "")),
            "last_signal_score": _as_float(existing.get("last_signal_score"), default=0.0),
            "last_signal_action": str(existing.get("last_signal_action", "")),
            "last_manual_update_at": now_iso,
            "updated_at": now_iso,
            "last_source": source,
            "last_trace_id": trace_id,
            "note": note_value,
        }
        self._merge_recommendation_extra_fields(record, existing, extra=extra)
        if normalized_status in {"bought", "holding", "entry_triggered"}:
            record.setdefault("entry_triggered_at", now_iso)
            record.setdefault("outcome_status", "open")
            self._sync_recommendation_position_metrics(record, timestamp=timestamp)
            event_name = "entry_triggered" if normalized_status == "entry_triggered" else "bought"
        elif normalized_status in {"sold", "closed"}:
            exit_price = _as_float(record.get("exit_price"), default=0.0)
            self._close_recommendation_record_metrics(
                record,
                exit_price=exit_price,
                timestamp=timestamp,
                reason=str(record.get("closed_reason", "")) or source,
            )
            event_name = "closed"
        elif normalized_status == "sell_alert":
            record.setdefault("exit_alert_at", now_iso)
            record.setdefault("outcome_status", "open")
            event_name = "sell_alert"
        else:
            event_name = normalized_status
        self._append_recommendation_event(
            record,
            timestamp=timestamp,
            event=event_name,
            source=source,
            trace_id=trace_id,
            details={"status": normalized_status, "note": note_value},
        )
        self._recommendation_lifecycle[normalized_symbol] = record
        return dict(record)

    def _load_recommendation_lifecycle_from_raw(self, raw: object) -> None:
        normalized: dict[str, dict[str, object]] = {}
        if not isinstance(raw, dict):
            self._recommendation_lifecycle = normalized
            return
        for symbol, item in raw.items():
            normalized_symbol = _normalize_a_share_symbol(symbol)
            if not normalized_symbol or not isinstance(item, dict):
                continue
            status = _normalize_recommendation_status(item.get("status"), default="watching")
            strategy_value = str(item.get("strategy", "manual")).strip().lower() or "manual"
            normalized[normalized_symbol] = {
                "symbol": normalized_symbol,
                "strategy": strategy_value,
                "status": status,
                "first_recommended_at": str(item.get("first_recommended_at", "")),
                "last_signal_at": str(item.get("last_signal_at", "")),
                "last_signal_score": _as_float(item.get("last_signal_score"), default=0.0),
                "last_signal_action": str(item.get("last_signal_action", "")),
                "last_manual_update_at": str(item.get("last_manual_update_at", "")),
                "updated_at": str(item.get("updated_at", "")),
                "last_source": str(item.get("last_source", "")),
                "last_trace_id": str(item.get("last_trace_id", "")),
                "note": str(item.get("note", "")),
            }
            extra_payload: dict[str, object] = {}
            for key in _RECOMMENDATION_EXTRA_FIELDS:
                if key in item:
                    extra_payload[key] = item.get(key)
            if "events" in item and isinstance(item.get("events"), list):
                extra_payload["events"] = list(item.get("events", []))[-40:]
            self._merge_recommendation_extra_fields(
                normalized[normalized_symbol],
                {},
                extra=extra_payload,
            )
        self._recommendation_lifecycle = normalized

    def _ensure_symbol_tracked_in_watchlist(
        self,
        *,
        symbol: str,
        source: str,
        trace_id: str,
    ) -> dict[str, object]:
        normalized = _normalize_a_share_symbol(symbol)
        before = len(self._state.watchlist)
        if not normalized:
            return {
                "symbol": "",
                "added": False,
                "watchlist_before": before,
                "watchlist_after": before,
                "reason": "invalid_symbol",
            }
        if normalized in self._state.watchlist:
            return {
                "symbol": normalized,
                "added": False,
                "watchlist_before": before,
                "watchlist_after": before,
                "reason": "already_tracked",
            }
        self._state.watchlist.append(normalized)
        after = len(self._state.watchlist)
        self._record_audit_event(
            event_type="watchlist_symbol_tracked",
            trace_id=trace_id,
            payload={
                "symbol": normalized,
                "source": source,
                "watchlist_before": before,
                "watchlist_after": after,
            },
        )
        return {
            "symbol": normalized,
            "added": True,
            "watchlist_before": before,
            "watchlist_after": after,
            "reason": source,
        }

    def _recommendation_reference_for_symbol(
        self,
        *,
        symbol: str,
        timestamp: datetime,
        recommendation_id: str = "",
    ) -> dict[str, object] | None:
        normalized_symbol = _normalize_a_share_symbol(symbol)
        bars_cache: dict[str, pd.DataFrame] = {}
        normalized_rec_id = recommendation_id.strip().upper()
        snapshot: dict[str, object] | None = None
        if normalized_rec_id:
            raw_snapshot = self._recommendation_snapshot_by_id.get(normalized_rec_id)
            if isinstance(raw_snapshot, dict):
                snapshot = dict(raw_snapshot)
        if snapshot is None and normalized_symbol:
            latest_id = self._recommendation_latest_id_by_symbol.get(normalized_symbol, "")
            if latest_id:
                raw_snapshot = self._recommendation_snapshot_by_id.get(latest_id)
                if isinstance(raw_snapshot, dict):
                    snapshot = dict(raw_snapshot)
        if snapshot is not None:
            ref_symbol = _normalize_a_share_symbol(snapshot.get("symbol")) or normalized_symbol
            reference_price = self._resolve_latest_close_price(
                symbol=ref_symbol,
                bars_cache=bars_cache,
            )
            snapshot["symbol"] = ref_symbol
            snapshot["reference_price"] = round(reference_price, 6) if reference_price else 0.0
            snapshot["reference_time"] = timestamp.isoformat()
            return snapshot

        if not normalized_symbol:
            return None
        raw_signals: list[object] | None = None
        latest_trace_id = self._last_signal_trace_id
        if self._last_signal_payload:
            raw_signals = cast(list[object], self._last_signal_payload)
        else:
            latest = self.latest_report()
            if not isinstance(latest, dict):
                return None
            payload_signals = latest.get("signals")
            if not isinstance(payload_signals, list):
                return None
            raw_signals = cast(list[object], payload_signals)
            latest_trace_id = str(latest.get("trace_id", ""))
        if raw_signals is None:
            return None

        target_signal: dict[str, object] | None = None
        target_index = 0
        for idx, item in enumerate(raw_signals):
            if not isinstance(item, dict):
                continue
            signal_symbol = _normalize_a_share_symbol(item.get("symbol"))
            if signal_symbol != normalized_symbol:
                continue
            target_signal = item
            target_index = idx
            break
        if target_signal is None:
            return None

        fallback_recommendation_id = _build_recommendation_id(
            trace_id=latest_trace_id,
            symbol=normalized_symbol,
            strategy=str(target_signal.get("strategy", "")),
            index=target_index,
        )
        generated_id = str(target_signal.get("recommendation_id", "")).strip().upper()
        recommendation_ref_id = generated_id or fallback_recommendation_id
        recommendation_time = (
            str(target_signal.get("recommendation_time", "")).strip() or timestamp.isoformat()
        )
        snapshot = {
            "recommendation_id": recommendation_ref_id,
            "symbol": normalized_symbol,
            "strategy": str(target_signal.get("strategy", "")).strip(),
            "action": str(target_signal.get("action", "")).strip().lower(),
            "score": round(_as_float(target_signal.get("score"), default=0.0), 4),
            "target_position": round(
                _as_float(target_signal.get("target_position"), default=0.0),
                6,
            ),
            "recommendation_time": recommendation_time,
            "trace_id": latest_trace_id,
        }
        snapshot_id = _extract_learning_snapshot_id(target_signal)
        if snapshot_id:
            snapshot["snapshot_id"] = snapshot_id
        self._recommendation_snapshot_by_id[recommendation_ref_id] = dict(snapshot)
        self._recommendation_latest_id_by_symbol[normalized_symbol] = recommendation_ref_id
        reference_price = self._resolve_latest_close_price(
            symbol=normalized_symbol,
            bars_cache=bars_cache,
        )
        snapshot["reference_price"] = round(reference_price, 6) if reference_price else 0.0
        snapshot["reference_time"] = timestamp.isoformat()
        return snapshot

    def execution_bias_report(self, days: int = 30, limit: int = 200) -> dict[str, object]:
        self._refresh_runtime_state_from_disk_if_changed()
        capped_days = max(1, days)
        capped_limit = max(1, min(limit, 1000))
        cutoff = datetime.now().timestamp() - capped_days * 86400
        items: list[dict[str, object]] = []

        for event in reversed(self._audit_events):
            if len(items) >= capped_limit:
                break
            if str(event.get("event_type", "")).strip().lower() != "command_accepted":
                continue
            if _report_timestamp(event) < cutoff:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            action = str(payload.get("action", "")).strip().upper()
            if action != "SET_POSITION":
                continue
            command_update = payload.get("command_update")
            if not isinstance(command_update, dict):
                continue
            status = str(command_update.get("status", "")).strip().lower()
            if status not in {"opened", "adjusted"}:
                continue
            symbol = str(command_update.get("symbol", "")).strip()
            target_position = _as_float(command_update.get("target_position"), default=0.0)

            manual_fill_raw = command_update.get("manual_fill")
            manual_fill = manual_fill_raw if isinstance(manual_fill_raw, dict) else {}
            entry_price = _as_float(manual_fill.get("entry_price"), default=0.0)
            quantity = _as_int(manual_fill.get("quantity"), default=0)

            ref_raw = command_update.get("recommendation_reference")
            recommendation_reference = ref_raw if isinstance(ref_raw, dict) else {}
            has_reference = bool(recommendation_reference)
            recommended_target = _as_float(
                recommendation_reference.get("target_position"),
                default=0.0,
            )
            reference_score = _as_float(recommendation_reference.get("score"), default=0.0)
            reference_price = _as_float(
                recommendation_reference.get("reference_price"), default=0.0
            )
            strategy = str(recommendation_reference.get("strategy", "")).strip() or "manual"

            position_bias: float | None = None
            if has_reference:
                position_bias = round(target_position - recommended_target, 6)
            price_bias_pct: float | None = None
            if entry_price > 0 and reference_price > 0:
                price_bias_pct = round(entry_price / reference_price - 1.0, 6)

            items.append(
                {
                    "timestamp": str(event.get("timestamp", "")),
                    "trace_id": str(event.get("trace_id", "")),
                    "symbol": symbol,
                    "recommendation_id": str(recommendation_reference.get("recommendation_id", "")),
                    "strategy": strategy,
                    "status": status,
                    "target_position": round(target_position, 6),
                    "recommended_target_position": round(recommended_target, 6),
                    "position_bias": position_bias,
                    "entry_price": round(entry_price, 6),
                    "reference_price": round(reference_price, 6),
                    "price_bias_pct": price_bias_pct,
                    "reference_score": round(reference_score, 4),
                    "quantity": quantity,
                    "has_recommendation_reference": has_reference,
                }
            )

        with_reference = sum(
            1 for item in items if bool(item.get("has_recommendation_reference", False))
        )
        with_price_ref = sum(1 for item in items if item.get("price_bias_pct") is not None)
        position_bias_values = [
            abs(_as_float(item["position_bias"], default=0.0))
            for item in items
            if isinstance(item.get("position_bias"), (int, float))
        ]
        price_bias_values = [
            abs(_as_float(item["price_bias_pct"], default=0.0))
            for item in items
            if isinstance(item.get("price_bias_pct"), (int, float))
        ]
        worse_price = sum(
            1
            for item in items
            if isinstance(item.get("price_bias_pct"), (int, float))
            and _as_float(item["price_bias_pct"], default=0.0) > 0.0
        )
        better_price = sum(
            1
            for item in items
            if isinstance(item.get("price_bias_pct"), (int, float))
            and _as_float(item["price_bias_pct"], default=0.0) < 0.0
        )
        worse_price_rate = round(worse_price / max(with_price_ref, 1), 6)
        better_price_rate = round(better_price / max(with_price_ref, 1), 6)

        position_dist = {
            "<1%": 0,
            "1-3%": 0,
            "3-5%": 0,
            ">=5%": 0,
        }
        price_dist = {
            "<1%": 0,
            "1-3%": 0,
            "3-5%": 0,
            ">=5%": 0,
        }
        strategy_stats: dict[str, dict[str, object]] = {}
        weekly_stats: dict[str, dict[str, object]] = {}
        monthly_stats: dict[str, dict[str, object]] = {}
        for item in items:
            strategy = str(item.get("strategy", "")).strip() or "manual"
            strategy_bucket = strategy_stats.setdefault(
                strategy,
                {
                    "records": 0,
                    "position_abs_sum": 0.0,
                    "position_records": 0,
                    "price_abs_sum": 0.0,
                    "price_records": 0,
                    "worse_price": 0,
                    "better_price": 0,
                },
            )
            strategy_bucket["records"] = _as_int(strategy_bucket.get("records"), default=0) + 1

            position_bias_raw = item.get("position_bias")
            if isinstance(position_bias_raw, (int, float)):
                abs_bias = abs(float(position_bias_raw))
                if abs_bias < 0.01:
                    position_dist["<1%"] += 1
                elif abs_bias < 0.03:
                    position_dist["1-3%"] += 1
                elif abs_bias < 0.05:
                    position_dist["3-5%"] += 1
                else:
                    position_dist[">=5%"] += 1
                strategy_bucket["position_abs_sum"] = (
                    _as_float(strategy_bucket.get("position_abs_sum"), default=0.0) + abs_bias
                )
                strategy_bucket["position_records"] = (
                    _as_int(strategy_bucket.get("position_records"), default=0) + 1
                )

            price_bias_raw = item.get("price_bias_pct")
            if isinstance(price_bias_raw, (int, float)):
                abs_price_bias = abs(float(price_bias_raw))
                if abs_price_bias < 0.01:
                    price_dist["<1%"] += 1
                elif abs_price_bias < 0.03:
                    price_dist["1-3%"] += 1
                elif abs_price_bias < 0.05:
                    price_dist["3-5%"] += 1
                else:
                    price_dist[">=5%"] += 1
                strategy_bucket["price_abs_sum"] = (
                    _as_float(strategy_bucket.get("price_abs_sum"), default=0.0) + abs_price_bias
                )
                strategy_bucket["price_records"] = (
                    _as_int(strategy_bucket.get("price_records"), default=0) + 1
                )
                if float(price_bias_raw) > 0:
                    strategy_bucket["worse_price"] = (
                        _as_int(strategy_bucket.get("worse_price"), default=0) + 1
                    )
                elif float(price_bias_raw) < 0:
                    strategy_bucket["better_price"] = (
                        _as_int(strategy_bucket.get("better_price"), default=0) + 1
                    )

            timestamp_raw = str(item.get("timestamp", ""))
            event_time = _parse_iso_datetime(timestamp_raw)
            if event_time is None:
                continue
            week_key = f"{event_time.isocalendar().year}-W{event_time.isocalendar().week:02d}"
            month_key = f"{event_time.year:04d}-{event_time.month:02d}"
            week_bucket = weekly_stats.setdefault(
                week_key,
                {
                    "records": 0,
                    "position_abs_sum": 0.0,
                    "position_records": 0,
                    "price_abs_sum": 0.0,
                    "price_records": 0,
                    "worse_price": 0,
                    "better_price": 0,
                },
            )
            month_bucket = monthly_stats.setdefault(
                month_key,
                {
                    "records": 0,
                    "position_abs_sum": 0.0,
                    "position_records": 0,
                    "price_abs_sum": 0.0,
                    "price_records": 0,
                    "worse_price": 0,
                    "better_price": 0,
                },
            )
            for bucket in (week_bucket, month_bucket):
                bucket["records"] = _as_int(bucket.get("records"), default=0) + 1
                if isinstance(position_bias_raw, (int, float)):
                    bucket["position_abs_sum"] = _as_float(
                        bucket.get("position_abs_sum"), default=0.0
                    ) + abs(float(position_bias_raw))
                    bucket["position_records"] = (
                        _as_int(bucket.get("position_records"), default=0) + 1
                    )
                if isinstance(price_bias_raw, (int, float)):
                    bucket["price_abs_sum"] = _as_float(
                        bucket.get("price_abs_sum"), default=0.0
                    ) + abs(float(price_bias_raw))
                    bucket["price_records"] = _as_int(bucket.get("price_records"), default=0) + 1
                    if float(price_bias_raw) > 0:
                        bucket["worse_price"] = _as_int(bucket.get("worse_price"), default=0) + 1
                    elif float(price_bias_raw) < 0:
                        bucket["better_price"] = _as_int(bucket.get("better_price"), default=0) + 1

        strategy_breakdown: list[dict[str, object]] = []
        for strategy, bucket in strategy_stats.items():
            position_records = max(1, _as_int(bucket.get("position_records"), default=0))
            price_records = max(1, _as_int(bucket.get("price_records"), default=0))
            strategy_breakdown.append(
                {
                    "strategy": strategy,
                    "records": _as_int(bucket.get("records"), default=0),
                    "avg_abs_position_bias": round(
                        _as_float(bucket.get("position_abs_sum"), default=0.0) / position_records,
                        6,
                    ),
                    "avg_abs_price_bias_pct": round(
                        _as_float(bucket.get("price_abs_sum"), default=0.0) / price_records,
                        6,
                    ),
                    "worse_price_rate": round(
                        _as_int(bucket.get("worse_price"), default=0) / price_records,
                        6,
                    ),
                    "better_price_rate": round(
                        _as_int(bucket.get("better_price"), default=0) / price_records,
                        6,
                    ),
                }
            )
        strategy_breakdown.sort(
            key=lambda item: (
                -_as_int(item.get("records"), default=0),
                _as_float(item.get("avg_abs_price_bias_pct"), default=0.0),
            )
        )

        def _build_period_rows(stats: dict[str, dict[str, object]]) -> list[dict[str, object]]:
            rows: list[dict[str, object]] = []
            for key in sorted(stats.keys(), reverse=True):
                bucket = stats[key]
                position_records = max(1, _as_int(bucket.get("position_records"), default=0))
                price_records = max(1, _as_int(bucket.get("price_records"), default=0))
                rows.append(
                    {
                        "period": key,
                        "records": _as_int(bucket.get("records"), default=0),
                        "avg_abs_position_bias": round(
                            _as_float(bucket.get("position_abs_sum"), default=0.0)
                            / position_records,
                            6,
                        ),
                        "avg_abs_price_bias_pct": round(
                            _as_float(bucket.get("price_abs_sum"), default=0.0) / price_records,
                            6,
                        ),
                        "worse_price_rate": round(
                            _as_int(bucket.get("worse_price"), default=0) / price_records,
                            6,
                        ),
                        "better_price_rate": round(
                            _as_int(bucket.get("better_price"), default=0) / price_records,
                            6,
                        ),
                    }
                )
            return rows

        return {
            "days": capped_days,
            "records": len(items),
            "summary": {
                "with_recommendation_reference": with_reference,
                "with_price_reference": with_price_ref,
                "avg_abs_position_bias": round(
                    sum(position_bias_values) / max(len(position_bias_values), 1),
                    6,
                ),
                "avg_abs_price_bias_pct": round(
                    sum(price_bias_values) / max(len(price_bias_values), 1),
                    6,
                ),
                "worse_price_count": worse_price,
                "better_price_count": better_price,
                "worse_price_rate": worse_price_rate,
                "better_price_rate": better_price_rate,
            },
            "distribution": {
                "position_bias": position_dist,
                "price_bias_pct": price_dist,
            },
            "period_summary": {
                "weekly": _build_period_rows(weekly_stats),
                "monthly": _build_period_rows(monthly_stats),
            },
            "strategy_breakdown": strategy_breakdown,
            "items": items,
        }

    def update_broker_snapshot(
        self,
        positions: list[dict[str, object]],
        source_trace_id: str = "",
        source: str = "manual",
    ) -> dict[str, object]:
        return self._reconcile_service.update_broker_snapshot(
            positions=positions,
            source_trace_id=source_trace_id,
            source=source,
        )

    def bootstrap_broker_snapshot_from_portfolio(
        self,
        *,
        source_trace_id: str = "",
        allow_empty: bool = False,
    ) -> dict[str, object]:
        return self._reconcile_service.bootstrap_broker_snapshot_from_portfolio(
            source_trace_id=source_trace_id,
            allow_empty=allow_empty,
        )


    def latest_reconcile_report(self) -> dict[str, object] | None:
        return self._reconcile_service.latest_reconcile_report()


    def reconcile_weekly_report(self, days: int = 7) -> dict[str, object]:
        return self._reconcile_service.reconcile_weekly_report(days=days)


    def runtime_history_archive_status(self, limit: int = 20) -> dict[str, object]:
        return self._runtime_ops_service.runtime_history_archive_status(limit=limit)

    def archive_runtime_history_if_needed(self, now: datetime | None = None) -> dict[str, object]:
        return self._runtime_ops_service.archive_runtime_history_if_needed(now=now)

    def archive_runtime_history(
        self,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        return self._runtime_ops_service.archive_runtime_history(now=now, force=force)

    def _build_runtime_history_archive_payload(
        self,
        *,
        now: datetime,
        max_records: int,
    ) -> dict[str, object]:
        return self._runtime_ops_service._build_runtime_history_archive_payload(
            now=now,
            max_records=max_records,
        )

    def _purge_runtime_history_archives(self, *, now: datetime) -> list[str]:
        return self._runtime_ops_service._purge_runtime_history_archives(now=now)

    def run_reconciliation(
        self,
        timestamp: datetime | None = None,
        trace_id: str = "",
    ) -> dict[str, object]:
        reconcile_time = timestamp or datetime.now()
        report = self._reconcile_service.run_reconciliation(
            timestamp=timestamp,
            trace_id=trace_id,
        )
        learning_outcome_update = self._update_learning_outcomes_from_reconcile_report(
            report=report,
            timestamp=reconcile_time,
        )
        if (
            _as_int(learning_outcome_update.get("updated"), default=0) > 0
            or str(learning_outcome_update.get("status", "")).startswith("skipped_")
        ):
            report["learning_outcome_update"] = learning_outcome_update
        return report


    def notify(
        self,
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        quiet_windows = list(self._config.notification_filter.quiet_windows)
        if self._config.security.suppress_plain_test_notifications and _is_plain_test_notification(
            title,
            content,
            trace_id=trace_id,
        ):
            payload = {
                "success": False,
                "channel": "suppressed",
                "error": "plain_test_notification_suppressed",
                "suppressed": True,
            }
        elif is_quiet_time(quiet_windows, now=datetime.now()):
            payload = {
                "success": False,
                "channel": "quiet_window",
                "error": "quiet_window_suppressed",
                "suppressed": True,
                "quiet_windows": quiet_windows,
            }
        else:
            message = NotificationMessage(title=title, content=content, level=level, trace_id=trace_id)
            result = self._notifier.send(message)
            payload = result.to_dict()
        self._record_audit_event(
            event_type="notification",
            trace_id=trace_id,
            level=level,
            payload={
                "title": title,
                "content": content,
                "delivery": payload,
            },
        )
        return payload

    def _notify_if_changed(
        self,
        *,
        dedup_key: str,
        title: str,
        content: str,
        dedup_value: str = "",
        level: str = "info",
        trace_id: str = "",
        ttl_sec: int = 18 * 3600,
    ) -> dict[str, object] | None:
        raw_dedup_value = dedup_value.strip() or f"{title}\n{level}\n{content}"
        fingerprint = hashlib.sha256(raw_dedup_value.encode()).hexdigest()
        if self._cache.get(dedup_key) == fingerprint:
            self._record_audit_event(
                event_type="notification_suppressed",
                trace_id=trace_id,
                level="info",
                payload={
                    "reason": "unchanged",
                    "dedup_key": dedup_key,
                    "title": title,
                },
            )
            return None
        self._cache.set(dedup_key, fingerprint, ttl_sec=max(60, ttl_sec))
        return self.notify(title=title, content=content, level=level, trace_id=trace_id)

    def audit_events(
        self,
        limit: int = 200,
        event_type: str = "",
        trace_id: str = "",
    ) -> dict[str, object]:
        return self._runtime_ops_service.audit_events(
            limit=limit,
            event_type=event_type,
            trace_id=trace_id,
        )

    def trace_replay(self, trace_id: str) -> dict[str, object]:
        return self._runtime_ops_service.trace_replay(trace_id=trace_id)

    def runtime_status(
        self,
        *,
        include_learning_governance: bool = True,
    ) -> dict[str, object]:
        return self._runtime_ops_service.runtime_status(
            include_learning_governance=include_learning_governance
        )

    def learning_protocol_status(self, manifest_limit: int = 5) -> dict[str, object]:
        return self._runtime_ops_service.learning_protocol_status(manifest_limit=manifest_limit)

    def learning_store_status(self) -> dict[str, object]:
        return self._runtime_ops_service.learning_store_status()

    def learning_store_metrics(self) -> dict[str, object]:
        return self._runtime_ops_service.learning_store_metrics()

    def learning_manifests_status(self, manifest_limit: int = 20) -> dict[str, object]:
        return self._runtime_ops_service.learning_manifests_status(manifest_limit=manifest_limit)

    def model_registry_status(self, limit: int = 20) -> dict[str, object]:
        return self._runtime_ops_service.model_registry_status(limit=limit)

    def shadow_v2_status(self, limit: int = 20) -> dict[str, object]:
        return self._runtime_ops_service.shadow_v2_status(limit=limit)

    def m3_profile_status(self) -> dict[str, object]:
        return self._runtime_ops_service.m3_profile_status()

    def train_learning_manifest(
        self,
        *,
        dataset_manifest_id: str = "",
        artifact_path: str | None = None,
        load_predictor: bool = False,
        register_model: bool = False,
    ) -> dict[str, object]:
        normalized_manifest_id = str(dataset_manifest_id).strip()
        manifest_source = "requested" if normalized_manifest_id else "latest"
        manifest = (
            self._sample_store.get_manifest(normalized_manifest_id)
            if normalized_manifest_id
            else None
        )
        if manifest is None and not normalized_manifest_id:
            latest_manifests = self._sample_store.list_manifests(limit=1)
            manifest = latest_manifests[0] if latest_manifests else None
        if manifest is None:
            payload = {
                "ok": False,
                "mode": "dataset_manifest_training",
                "input_mode": "dataset_manifest",
                "manifest_source": manifest_source,
                "dataset_manifest_id": normalized_manifest_id,
                "artifact_path": "",
                "predictor_loaded": False,
                "included_snapshot_count": 0,
                "included_outcome_count": 0,
                "feature_schema_id": "",
                "label_policy_id": "",
                "model_registry": {"registered": False, "reason": "manifest_not_found"},
                "errors": [
                    (
                        f"dataset_manifest_not_found:{normalized_manifest_id}"
                        if normalized_manifest_id
                        else "no_dataset_manifests"
                    )
                ],
            }
            self._record_audit_event(
                event_type="learning_manifest_trained",
                level="warn",
                message="learning manifest training failed",
                payload=payload,
            )
            return payload

        resolved_output_path = self._resolve_learning_manifest_artifact_path(
            dataset_manifest_id=manifest.dataset_manifest_id,
            artifact_path=artifact_path,
        )
        try:
            trainer = self._build_model_trainer()
            result = trainer.train_on_dataset_manifest(
                store=self._sample_store,
                dataset_manifest=manifest,
                feature_schema_registry=self._feature_schema_registry,
                label_policy_registry=self._label_policy_registry,
            )
            result.artifact.save(resolved_output_path)
            predictor_loaded = (
                self._pipeline.reload_predictor(artifact_path=str(resolved_output_path))
                if load_predictor
                else False
            )
            model_registry_payload = (
                self._register_model_artifact_if_supported(
                    artifact_path=str(resolved_output_path),
                    role=ModelRole.CHALLENGER,
                    lifecycle_state=ModelLifecycleState.TRAINED,
                    source="train_learning_manifest",
                )
                if register_model
                else {"registered": False, "reason": "registration_disabled"}
            )
            payload = {
                "ok": True,
                "mode": "dataset_manifest_training",
                "input_mode": "dataset_manifest",
                "manifest_source": manifest_source,
                "dataset_manifest_id": manifest.dataset_manifest_id,
                "artifact_path": str(resolved_output_path),
                "predictor_loaded": predictor_loaded,
                "included_snapshot_count": manifest.included_snapshot_count,
                "included_outcome_count": manifest.included_outcome_count,
                "feature_schema_id": manifest.feature_schema_id,
                "feature_schema_hash": manifest.feature_schema_hash,
                "label_policy_id": manifest.label_policy_id,
                "label_policy_hash": manifest.label_policy_hash,
                "model_registry": model_registry_payload,
                "result": result.to_dict(),
                "errors": [],
            }
            self._record_audit_event(
                event_type="learning_manifest_trained",
                level="info",
                message="learning manifest training completed",
                payload=payload,
            )
            return payload
        except Exception as exc:
            payload = {
                "ok": False,
                "mode": "dataset_manifest_training",
                "input_mode": "dataset_manifest",
                "manifest_source": manifest_source,
                "dataset_manifest_id": manifest.dataset_manifest_id,
                "artifact_path": str(resolved_output_path),
                "predictor_loaded": False,
                "included_snapshot_count": manifest.included_snapshot_count,
                "included_outcome_count": manifest.included_outcome_count,
                "feature_schema_id": manifest.feature_schema_id,
                "label_policy_id": manifest.label_policy_id,
                "model_registry": {"registered": False, "reason": "training_failed"},
                "errors": [f"manifest_training_failed:{exc.__class__.__name__}:{exc}"],
            }
            self._record_audit_event(
                event_type="learning_manifest_trained",
                level="warn",
                message="learning manifest training failed",
                payload=payload,
            )
            return payload

    def run_learning_manifest_shadow_validation(
        self,
        *,
        dataset_manifest_id: str = "",
        artifact_path: str | None = None,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_rows: bool = False,
        preview_limit: int = 5,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        load_predictor: bool = False,
        mark_shadow_validated: bool = False,
    ) -> dict[str, object]:
        normalized_splits = _dedupe_preserve_order(
            [text for item in (split_names or []) if (text := str(item).strip())]
        ) or ["test"]
        normalized_champion_model_id = str(champion_model_id).strip()
        training_payload = self.train_learning_manifest(
            dataset_manifest_id=dataset_manifest_id,
            artifact_path=artifact_path,
            load_predictor=load_predictor,
            register_model=True,
        )
        errors = _string_list(training_payload.get("errors", []))
        shadow_model_id = ""
        champion_resolved_model_id = normalized_champion_model_id
        shadow_dataset_payload: dict[str, object] = {
            "ok": False,
            "requested_split_names": list(normalized_splits),
        }
        champion_shadow_report_payload: dict[str, object] = {
            "ok": False,
            "requested_split_names": list(normalized_splits),
        }
        shadow_online_v2_payload: dict[str, object] = {
            "ok": False,
            "requested_split_names": list(normalized_splits),
        }
        registry_lifecycle_payload: dict[str, object] = {
            "updated": False,
            "reason": "mark_shadow_validated_disabled",
        }

        model_registry_payload = training_payload.get("model_registry", {})
        registered = (
            isinstance(model_registry_payload, Mapping)
            and bool(model_registry_payload.get("registered", False))
        )
        if registered:
            shadow_model_id = str(model_registry_payload.get("model_id", "")).strip()
        elif bool(training_payload.get("ok", False)):
            errors.append("shadow_model_registration_failed")

        if bool(training_payload.get("ok", False)) and registered and shadow_model_id:
            try:
                shadow_dataset_payload = self.build_shadow_dataset(
                    model_id=shadow_model_id,
                    split_names=normalized_splits,
                    max_rows=max_rows,
                    include_rows=include_rows,
                    preview_limit=preview_limit,
                )
                shadow_dataset_payload["ok"] = True
            except Exception as exc:
                error_text = (
                    f"shadow_dataset_failed:{exc.__class__.__name__}:{exc}"
                )
                errors.append(error_text)
                shadow_dataset_payload = {
                    "ok": False,
                    "error": error_text,
                    "model_id": shadow_model_id,
                    "dataset_manifest_id": str(training_payload.get("dataset_manifest_id", "")),
                    "requested_split_names": list(normalized_splits),
                }

            try:
                champion_shadow_report_payload = self.build_champion_shadow_report(
                    model_id=shadow_model_id,
                    champion_model_id=normalized_champion_model_id,
                    split_names=normalized_splits,
                    max_rows=max_rows,
                    signal_threshold=signal_threshold,
                    include_rows=include_rows,
                    preview_limit=preview_limit,
                )
                champion_shadow_report_payload["ok"] = True
                champion_resolved_model_id = str(
                    champion_shadow_report_payload.get("champion_model_id", "")
                ).strip()
            except Exception as exc:
                error_text = (
                    f"champion_shadow_report_failed:{exc.__class__.__name__}:{exc}"
                )
                errors.append(error_text)
                champion_shadow_report_payload = {
                    "ok": False,
                    "error": error_text,
                    "shadow_model_id": shadow_model_id,
                    "champion_model_id": normalized_champion_model_id,
                    "dataset_manifest_id": str(training_payload.get("dataset_manifest_id", "")),
                    "requested_split_names": list(normalized_splits),
                }

            try:
                shadow_online_v2_payload = self.build_shadow_online_v2_report(
                    model_id=shadow_model_id,
                    champion_model_id=normalized_champion_model_id,
                    split_names=normalized_splits,
                    max_rows=max_rows,
                    max_samples=max_samples,
                    min_samples=min_samples,
                    learning_rate=learning_rate,
                    signal_threshold=signal_threshold,
                    include_rows=include_rows,
                    preview_limit=preview_limit,
                )
                shadow_online_v2_payload["ok"] = True
                if not champion_resolved_model_id:
                    champion_resolved_model_id = str(
                        shadow_online_v2_payload.get("champion_model_id", "")
                    ).strip()
            except Exception as exc:
                error_text = (
                    f"shadow_online_v2_report_failed:{exc.__class__.__name__}:{exc}"
                )
                errors.append(error_text)
                shadow_online_v2_payload = {
                    "ok": False,
                    "error": error_text,
                    "shadow_model_id": shadow_model_id,
                    "champion_model_id": champion_resolved_model_id or normalized_champion_model_id,
                    "dataset_manifest_id": str(training_payload.get("dataset_manifest_id", "")),
                    "requested_split_names": list(normalized_splits),
                }

            if mark_shadow_validated:
                if errors:
                    registry_lifecycle_payload = {
                        "updated": False,
                        "reason": "shadow_validation_incomplete",
                    }
                else:
                    try:
                        record = self.update_model_registry_lifecycle(
                            model_id=shadow_model_id,
                            lifecycle_state=ModelLifecycleState.SHADOW_VALIDATED.value,
                        )
                        registry_lifecycle_payload = {
                            "updated": True,
                            "record": record,
                        }
                    except Exception as exc:
                        error_text = (
                            f"shadow_validated_transition_failed:{exc.__class__.__name__}:{exc}"
                        )
                        errors.append(error_text)
                        registry_lifecycle_payload = {
                            "updated": False,
                            "reason": error_text,
                        }

        elif not bool(training_payload.get("ok", False)):
            shadow_dataset_payload = {
                "ok": False,
                "reason": "training_failed",
                "requested_split_names": list(normalized_splits),
            }
            champion_shadow_report_payload = {
                "ok": False,
                "reason": "training_failed",
                "requested_split_names": list(normalized_splits),
            }
            shadow_online_v2_payload = {
                "ok": False,
                "reason": "training_failed",
                "requested_split_names": list(normalized_splits),
            }
            registry_lifecycle_payload = {
                "updated": False,
                "reason": "training_failed",
            }

        ok = bool(training_payload.get("ok", False)) and not errors and bool(shadow_model_id)
        payload = {
            "ok": ok,
            "mode": "learning_manifest_shadow_validation",
            "dataset_manifest_id": str(training_payload.get("dataset_manifest_id", "")),
            "shadow_model_id": shadow_model_id,
            "champion_model_id": champion_resolved_model_id,
            "evaluation_split_names": list(normalized_splits),
            "training": training_payload,
            "shadow_dataset": shadow_dataset_payload,
            "champion_shadow_report": champion_shadow_report_payload,
            "shadow_online_v2_report": shadow_online_v2_payload,
            "registry_lifecycle": registry_lifecycle_payload,
            "errors": errors,
        }
        self._record_audit_event(
            event_type="learning_manifest_shadow_validation",
            level="info" if ok else "warn",
            message=(
                "learning manifest shadow validation completed"
                if ok
                else "learning manifest shadow validation failed"
            ),
            payload={
                "ok": ok,
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "shadow_model_id": shadow_model_id,
                "champion_model_id": champion_resolved_model_id,
                "evaluation_split_names": list(normalized_splits),
                "shadow_dataset_id": str(shadow_dataset_payload.get("shadow_dataset_id", "")),
                "comparison_report_id": str(
                    champion_shadow_report_payload.get("comparison_report_id", "")
                ),
                "shadow_online_v2_report_id": str(
                    shadow_online_v2_payload.get("report_id", "")
                ),
                "registry_lifecycle_updated": bool(
                    registry_lifecycle_payload.get("updated", False)
                ),
                "errors": list(errors),
            },
        )
        return payload

    def evaluate_learning_model_promotion_gate(
        self,
        *,
        model_id: str,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        preview_limit: int = 5,
        min_shadow_v2_minus_champion_return: float = -0.02,
        max_shadow_v2_brier_delta: float = 0.05,
        max_shadow_v2_logloss_delta: float = 0.10,
        max_signal_divergence_ratio: float | None = None,
        approve_if_passed: bool = False,
        block_if_failed: bool = False,
    ) -> dict[str, object]:
        normalized_model_id = str(model_id).strip()
        normalized_champion_model_id = str(champion_model_id).strip()
        normalized_splits = _dedupe_preserve_order(
            [text for item in (split_names or []) if (text := str(item).strip())]
        ) or ["test"]
        required_samples = max(1, int(min_samples))
        divergence_limit = (
            _as_float(max_signal_divergence_ratio, default=0.35)
            if max_signal_divergence_ratio is not None
            else _as_float(self._config.evolution.m11_execution_divergence_limit, default=0.35)
        )

        checks: list[dict[str, object]] = []
        blockers: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []

        def add_check(
            name: str,
            status: str,
            detail: str,
            *,
            metric: object | None = None,
            threshold: object | None = None,
        ) -> None:
            payload: dict[str, object] = {
                "name": name,
                "status": status,
                "detail": detail,
            }
            if metric is not None:
                payload["metric"] = metric
            if threshold is not None:
                payload["threshold"] = threshold
            checks.append(payload)

        entry = self._model_registry.get_by_id(normalized_model_id) if normalized_model_id else None
        lifecycle_before = entry.lifecycle_state.value if entry is not None else ""
        role_before = entry.role.value if entry is not None else ""

        if not normalized_model_id:
            errors.append("model_id_missing")
            blockers.append("model_id_missing")
            add_check(
                "model_id_present",
                "fail",
                "model_id must not be empty",
            )
        elif entry is None:
            errors.append(f"model_id_not_found:{normalized_model_id}")
            blockers.append("model_id_not_found")
            add_check(
                "model_registry_entry",
                "fail",
                f"model_id not found: {normalized_model_id}",
            )
        else:
            add_check(
                "model_registry_entry",
                "pass",
                f"model found: role={entry.role.value}, lifecycle={entry.lifecycle_state.value}",
            )
            if entry.role == ModelRole.CHAMPION:
                blockers.append("champion_role_not_eligible")
                add_check(
                    "model_role_eligibility",
                    "fail",
                    "champion role cannot be evaluated as challenger/shadow promotion target",
                )
            else:
                add_check(
                    "model_role_eligibility",
                    "pass",
                    f"role={entry.role.value}",
                )
            if entry.lifecycle_state in {
                ModelLifecycleState.REGISTERED,
                ModelLifecycleState.REVOKED,
            }:
                blockers.append("model_lifecycle_not_eligible")
                add_check(
                    "model_lifecycle_eligibility",
                    "fail",
                    (
                        "promotion gate requires lifecycle in trained/shadow_validated/"
                        f"blocked/approved, got {entry.lifecycle_state.value}"
                    ),
                )
            else:
                add_check(
                    "model_lifecycle_eligibility",
                    "pass",
                    f"lifecycle={entry.lifecycle_state.value}",
                )

        champion_shadow_report_payload: dict[str, object] = {}
        shadow_online_v2_payload: dict[str, object] = {}
        execution_aware_payload: dict[str, object] = {}

        if not blockers:
            try:
                champion_shadow_report_payload = self.build_champion_shadow_report(
                    model_id=normalized_model_id,
                    champion_model_id=normalized_champion_model_id,
                    split_names=normalized_splits,
                    max_rows=max_rows,
                    signal_threshold=signal_threshold,
                    include_rows=False,
                    preview_limit=preview_limit,
                )
                add_check(
                    "champion_shadow_report",
                    "pass",
                    "champion-shadow comparison report built",
                    metric=_as_int(champion_shadow_report_payload.get("row_count"), default=0),
                )
            except Exception as exc:
                error_text = (
                    f"champion_shadow_report_failed:{exc.__class__.__name__}:{exc}"
                )
                errors.append(error_text)
                blockers.append("champion_shadow_report_failed")
                add_check(
                    "champion_shadow_report",
                    "fail",
                    error_text,
                )

            try:
                shadow_online_v2_payload = self.build_shadow_online_v2_report(
                    model_id=normalized_model_id,
                    champion_model_id=normalized_champion_model_id,
                    split_names=normalized_splits,
                    max_rows=max_rows,
                    max_samples=max_samples,
                    min_samples=required_samples,
                    learning_rate=learning_rate,
                    signal_threshold=signal_threshold,
                    include_rows=False,
                    preview_limit=preview_limit,
                )
                add_check(
                    "shadow_online_v2_report",
                    "pass",
                    "shadow online v2 report built",
                    metric=_as_int(shadow_online_v2_payload.get("row_count"), default=0),
                )
            except Exception as exc:
                error_text = (
                    f"shadow_online_v2_report_failed:{exc.__class__.__name__}:{exc}"
                )
                errors.append(error_text)
                blockers.append("shadow_online_v2_report_failed")
                add_check(
                    "shadow_online_v2_report",
                    "fail",
                    error_text,
                )

            execution_risk_latest = self.latest_execution_risk_training() or {}
            execution_risk_artifact_path = str(
                execution_risk_latest.get("artifact_path", "")
            ).strip()
            if execution_risk_artifact_path:
                try:
                    execution_aware_payload = self.build_execution_aware_report(
                        model_id=normalized_model_id,
                        execution_risk_artifact_path=execution_risk_artifact_path,
                        champion_model_id=normalized_champion_model_id,
                        split_names=normalized_splits,
                        max_rows=max_rows,
                        include_rows=False,
                        preview_limit=preview_limit,
                    )
                    add_check(
                        "execution_aware_report",
                        "pass",
                        "execution aware report built",
                        metric=_as_int(execution_aware_payload.get("row_count"), default=0),
                    )
                except Exception as exc:
                    warning_code = (
                        f"execution_aware_report_failed:{exc.__class__.__name__}:{exc}"
                    )
                    warnings.append("execution_aware_report_failed")
                    add_check(
                        "execution_aware_report",
                        "warn",
                        warning_code,
                    )
            else:
                add_check(
                    "execution_aware_report",
                    "skip",
                    "no execution risk artifact available",
                )

        champion_resolved_model_id = str(
            champion_shadow_report_payload.get("champion_model_id", "")
            or shadow_online_v2_payload.get("champion_model_id", "")
            or normalized_champion_model_id
        ).strip()
        dataset_manifest_id = str(
            champion_shadow_report_payload.get("dataset_manifest_id", "")
            or shadow_online_v2_payload.get("dataset_manifest_id", "")
            or (entry.dataset_manifest_id if entry is not None else "")
        ).strip()

        champion_summary = _coerce_object_mapping(
            champion_shadow_report_payload.get("summary_metrics", {})
        )
        champion_m11_report = _coerce_object_mapping(
            champion_shadow_report_payload.get("m11_report", {})
        )
        champion_m11_redlines = _coerce_object_mapping(
            champion_m11_report.get("redlines", {})
        )
        shadow_return_summary = _coerce_object_mapping(
            shadow_online_v2_payload.get("return_summary", {})
        )
        shadow_execution_summary = _coerce_object_mapping(
            shadow_online_v2_payload.get("execution_summary", {})
        )
        shadow_run_result = _coerce_object_mapping(
            shadow_online_v2_payload.get("run_result", {})
        )
        shadow_run_metrics = _coerce_object_mapping(shadow_run_result.get("metrics", {}))
        shadow_m11_v2_report = _coerce_object_mapping(
            shadow_online_v2_payload.get("m11_v2_report", {})
        )
        shadow_m11_v2_redlines = _coerce_object_mapping(
            shadow_m11_v2_report.get("redlines", {})
        )
        execution_aware_summary = _coerce_object_mapping(
            execution_aware_payload.get("summary_metrics", {})
        )
        shadow_hard_block_min_samples = max(required_samples, 10)
        shadow_hard_gate_enabled = False
        shadow_divergence_ratio = _as_float(
            shadow_execution_summary.get("shadow_v2_signal_divergence_ratio"),
            default=_as_float(shadow_run_metrics.get("signal_divergence_ratio"), default=0.0),
        )

        champion_row_count = _as_int(champion_shadow_report_payload.get("row_count"), default=0)
        if not errors:
            if champion_row_count < required_samples:
                blockers.append("champion_shadow_rows_below_threshold")
                add_check(
                    "champion_shadow_row_count",
                    "fail",
                    f"row_count={champion_row_count}, required>={required_samples}",
                    metric=champion_row_count,
                    threshold=required_samples,
                )
            else:
                add_check(
                    "champion_shadow_row_count",
                    "pass",
                    f"row_count={champion_row_count}, required>={required_samples}",
                    metric=champion_row_count,
                    threshold=required_samples,
                )

            champion_m11_status = str(champion_m11_report.get("status", "")).strip()
            champion_m11_breach = champion_m11_status == "redline_breach" or any(
                _coerce_bool(value) for value in champion_m11_redlines.values()
            )
            if champion_m11_breach:
                blockers.append("champion_shadow_m11_redline_breach")
                add_check(
                    "champion_shadow_m11_gate",
                    "fail",
                    f"status={champion_m11_status or 'unknown'}",
                    metric=champion_m11_report.get("score"),
                )
            else:
                add_check(
                    "champion_shadow_m11_gate",
                    "pass",
                    f"status={champion_m11_status or 'stable'}",
                    metric=champion_m11_report.get("score"),
                )

            shadow_v2_status = str(shadow_online_v2_payload.get("status", "")).strip()
            shadow_samples_used = _as_int(shadow_run_result.get("samples_used"), default=0)
            if shadow_v2_status != "updated":
                blockers.append(
                    f"shadow_online_v2_status_{shadow_v2_status or 'unknown'}"
                )
                add_check(
                    "shadow_online_v2_status",
                    "fail",
                    f"status={shadow_v2_status or 'unknown'}, required=updated",
                )
            else:
                add_check(
                    "shadow_online_v2_status",
                    "pass",
                    "status=updated",
                )

            if shadow_samples_used < required_samples:
                blockers.append("shadow_online_v2_samples_below_threshold")
                add_check(
                    "shadow_online_v2_samples_used",
                    "fail",
                    f"samples_used={shadow_samples_used}, required>={required_samples}",
                    metric=shadow_samples_used,
                    threshold=required_samples,
                )
            else:
                add_check(
                    "shadow_online_v2_samples_used",
                    "pass",
                    f"samples_used={shadow_samples_used}, required>={required_samples}",
                    metric=shadow_samples_used,
                    threshold=required_samples,
                )

            shadow_hard_gate_enabled = (
                shadow_v2_status == "updated"
                and shadow_samples_used >= shadow_hard_block_min_samples
            )
            if shadow_hard_gate_enabled:
                add_check(
                    "shadow_online_v2_hard_block_evidence",
                    "pass",
                    (
                        f"samples_used={shadow_samples_used}, "
                        f"hard_block_enabled_at>={shadow_hard_block_min_samples}"
                    ),
                    metric=shadow_samples_used,
                    threshold=shadow_hard_block_min_samples,
                )
            else:
                add_check(
                    "shadow_online_v2_hard_block_evidence",
                    "skip",
                    (
                        f"samples_used={shadow_samples_used}, "
                        f"hard_block_deferred_until>={shadow_hard_block_min_samples}"
                    ),
                    metric=shadow_samples_used,
                    threshold=shadow_hard_block_min_samples,
                )

            shadow_m11_v2_status = str(shadow_m11_v2_report.get("status", "")).strip()
            shadow_m11_v2_breach = shadow_m11_v2_status == "redline_breach" or any(
                _coerce_bool(value) for value in shadow_m11_v2_redlines.values()
            )
            if shadow_hard_gate_enabled and shadow_m11_v2_breach:
                blockers.append("shadow_online_v2_m11_redline_breach")
                add_check(
                    "shadow_online_v2_m11_gate",
                    "fail",
                    f"status={shadow_m11_v2_status or 'unknown'}",
                    metric=shadow_m11_v2_report.get("score"),
                )
            elif shadow_hard_gate_enabled:
                add_check(
                    "shadow_online_v2_m11_gate",
                    "pass",
                    f"status={shadow_m11_v2_status or 'stable'}",
                    metric=shadow_m11_v2_report.get("score"),
                )
            else:
                add_check(
                    "shadow_online_v2_m11_gate",
                    "skip",
                    (
                        f"status={shadow_m11_v2_status or 'stable'}, "
                        f"hard_block_deferred_until_samples>={shadow_hard_block_min_samples}"
                    ),
                    metric=shadow_m11_v2_report.get("score"),
                )

            return_delta = _as_float(
                shadow_return_summary.get("shadow_v2_minus_champion_return"),
                default=0.0,
            )
            if shadow_hard_gate_enabled and return_delta < float(min_shadow_v2_minus_champion_return):
                blockers.append("shadow_v2_return_delta_below_threshold")
                add_check(
                    "shadow_v2_return_delta",
                    "fail",
                    (
                        f"shadow_v2_minus_champion_return={return_delta:.6f}, "
                        f"required>={float(min_shadow_v2_minus_champion_return):.6f}"
                    ),
                    metric=round(return_delta, 6),
                    threshold=round(float(min_shadow_v2_minus_champion_return), 6),
                )
            elif shadow_hard_gate_enabled:
                add_check(
                    "shadow_v2_return_delta",
                    "pass",
                    (
                        f"shadow_v2_minus_champion_return={return_delta:.6f}, "
                        f"required>={float(min_shadow_v2_minus_champion_return):.6f}"
                    ),
                    metric=round(return_delta, 6),
                    threshold=round(float(min_shadow_v2_minus_champion_return), 6),
                )
            else:
                add_check(
                    "shadow_v2_return_delta",
                    "skip",
                    (
                        f"shadow_v2_minus_champion_return={return_delta:.6f}, "
                        f"required>={float(min_shadow_v2_minus_champion_return):.6f}, "
                        f"hard_block_deferred_until_samples>={shadow_hard_block_min_samples}"
                    ),
                    metric=round(return_delta, 6),
                    threshold=round(float(min_shadow_v2_minus_champion_return), 6),
                )

            if shadow_hard_gate_enabled and shadow_divergence_ratio > divergence_limit:
                blockers.append("shadow_v2_signal_divergence_limit_breach")
                add_check(
                    "shadow_v2_signal_divergence_ratio",
                    "fail",
                    (
                        f"shadow_v2_signal_divergence_ratio={shadow_divergence_ratio:.6f}, "
                        f"limit<={divergence_limit:.6f}"
                    ),
                    metric=round(shadow_divergence_ratio, 6),
                    threshold=round(divergence_limit, 6),
                )
            elif shadow_hard_gate_enabled:
                add_check(
                    "shadow_v2_signal_divergence_ratio",
                    "pass",
                    (
                        f"shadow_v2_signal_divergence_ratio={shadow_divergence_ratio:.6f}, "
                        f"limit<={divergence_limit:.6f}"
                    ),
                    metric=round(shadow_divergence_ratio, 6),
                    threshold=round(divergence_limit, 6),
                )
            else:
                add_check(
                    "shadow_v2_signal_divergence_ratio",
                    "skip",
                    (
                        f"shadow_v2_signal_divergence_ratio={shadow_divergence_ratio:.6f}, "
                        f"limit<={divergence_limit:.6f}, "
                        f"hard_block_deferred_until_samples>={shadow_hard_block_min_samples}"
                    ),
                    metric=round(shadow_divergence_ratio, 6),
                    threshold=round(divergence_limit, 6),
                )

            brier_delta = _as_float(shadow_run_metrics.get("delta_brier"), default=0.0)
            if brier_delta > float(max_shadow_v2_brier_delta):
                warnings.append("shadow_v2_brier_delta_above_threshold")
                add_check(
                    "shadow_v2_brier_delta",
                    "warn",
                    (
                        f"delta_brier={brier_delta:.6f}, "
                        f"warn_if>{float(max_shadow_v2_brier_delta):.6f}"
                    ),
                    metric=round(brier_delta, 6),
                    threshold=round(float(max_shadow_v2_brier_delta), 6),
                )
            else:
                add_check(
                    "shadow_v2_brier_delta",
                    "pass",
                    (
                        f"delta_brier={brier_delta:.6f}, "
                        f"warn_if>{float(max_shadow_v2_brier_delta):.6f}"
                    ),
                    metric=round(brier_delta, 6),
                    threshold=round(float(max_shadow_v2_brier_delta), 6),
                )

            logloss_delta = _as_float(shadow_run_metrics.get("delta_logloss"), default=0.0)
            if logloss_delta > float(max_shadow_v2_logloss_delta):
                warnings.append("shadow_v2_logloss_delta_above_threshold")
                add_check(
                    "shadow_v2_logloss_delta",
                    "warn",
                    (
                        f"delta_logloss={logloss_delta:.6f}, "
                        f"warn_if>{float(max_shadow_v2_logloss_delta):.6f}"
                    ),
                    metric=round(logloss_delta, 6),
                    threshold=round(float(max_shadow_v2_logloss_delta), 6),
                )
            else:
                add_check(
                    "shadow_v2_logloss_delta",
                    "pass",
                    (
                        f"delta_logloss={logloss_delta:.6f}, "
                        f"warn_if>{float(max_shadow_v2_logloss_delta):.6f}"
                    ),
                    metric=round(logloss_delta, 6),
                    threshold=round(float(max_shadow_v2_logloss_delta), 6),
                )

            if execution_aware_payload:
                execution_score_delta = _as_float(
                    execution_aware_summary.get("shadow_minus_champion_execution_score"),
                    default=0.0,
                )
                if execution_score_delta < -0.02:
                    warnings.append("execution_aware_score_delta_below_threshold")
                    add_check(
                        "execution_aware_score_delta",
                        "warn",
                        (
                            f"shadow_minus_champion_execution_score={execution_score_delta:.6f}, "
                            "warn_if<-0.020000"
                        ),
                        metric=round(execution_score_delta, 6),
                        threshold=-0.02,
                    )
                else:
                    add_check(
                        "execution_aware_score_delta",
                        "pass",
                        (
                            f"shadow_minus_champion_execution_score={execution_score_delta:.6f}, "
                            "warn_if<-0.020000"
                        ),
                        metric=round(execution_score_delta, 6),
                        threshold=-0.02,
                    )
                shadow_high_risk_ratio = _as_float(
                    execution_aware_summary.get("shadow_high_risk_ratio"),
                    default=0.0,
                )
                if shadow_high_risk_ratio > 0.5:
                    warnings.append("execution_aware_high_risk_ratio_above_threshold")
                    add_check(
                        "execution_aware_high_risk_ratio",
                        "warn",
                        f"shadow_high_risk_ratio={shadow_high_risk_ratio:.6f}, warn_if>0.500000",
                        metric=round(shadow_high_risk_ratio, 6),
                        threshold=0.5,
                    )
                else:
                    add_check(
                        "execution_aware_high_risk_ratio",
                        "pass",
                        f"shadow_high_risk_ratio={shadow_high_risk_ratio:.6f}, warn_if>0.500000",
                        metric=round(shadow_high_risk_ratio, 6),
                        threshold=0.5,
                    )

        status = "fail" if blockers else ("warn" if warnings else "pass")
        accepted = status == "pass"
        recommended_action = {
            "pass": "approve",
            "warn": "manual_review",
            "fail": "block",
        }[status]
        reason_codes = list(dict.fromkeys(blockers + warnings or ["promotion_gate_passed"]))

        registry_transition: dict[str, object] = {
            "updated": False,
            "action": "noop",
            "reason": (
                "approve_if_passed_disabled"
                if status == "pass"
                else ("block_if_failed_disabled" if status == "fail" else "manual_review_required")
            ),
            "records": [],
        }

        if status == "pass" and approve_if_passed and normalized_model_id:
            current_record = self._model_registry.get_by_id(normalized_model_id)
            transition_records: list[dict[str, object]] = []
            try:
                if current_record is None:
                    raise ValueError(f"model_id not found: {normalized_model_id}")
                if current_record.lifecycle_state == ModelLifecycleState.TRAINED:
                    transition_records.append(
                        self.update_model_registry_lifecycle(
                            model_id=normalized_model_id,
                            lifecycle_state=ModelLifecycleState.SHADOW_VALIDATED.value,
                        )
                    )
                    current_record = self._model_registry.get_by_id(normalized_model_id)
                elif current_record.lifecycle_state == ModelLifecycleState.BLOCKED:
                    transition_records.append(
                        self.update_model_registry_lifecycle(
                            model_id=normalized_model_id,
                            lifecycle_state=ModelLifecycleState.SHADOW_VALIDATED.value,
                        )
                    )
                    current_record = self._model_registry.get_by_id(normalized_model_id)
                if current_record is not None and current_record.lifecycle_state == ModelLifecycleState.SHADOW_VALIDATED:
                    transition_records.append(
                        self.update_model_registry_lifecycle(
                            model_id=normalized_model_id,
                            lifecycle_state=ModelLifecycleState.APPROVED.value,
                        )
                    )
                registry_transition = {
                    "updated": bool(transition_records),
                    "action": "approved" if transition_records else "already_approved",
                    "reason": "approval_applied" if transition_records else "already_approved",
                    "records": transition_records,
                }
            except Exception as exc:
                warning_code = f"approval_transition_failed:{exc.__class__.__name__}"
                warnings.append(warning_code)
                reason_codes = list(dict.fromkeys(reason_codes + [warning_code]))
                status = "warn" if status == "pass" else status
                accepted = False
                recommended_action = "manual_review"
                registry_transition = {
                    "updated": False,
                    "action": "noop",
                    "reason": f"{warning_code}:{exc}",
                    "records": transition_records,
                }
                add_check(
                    "registry_approval_transition",
                    "warn",
                    f"{warning_code}:{exc}",
                )
        elif status == "fail" and block_if_failed and normalized_model_id:
            current_record = self._model_registry.get_by_id(normalized_model_id)
            transition_records = []
            if current_record is not None and current_record.lifecycle_state not in {
                ModelLifecycleState.BLOCKED,
                ModelLifecycleState.REVOKED,
            }:
                try:
                    blocked_reason = ",".join(reason_codes)
                    transition_records.append(
                        self.update_model_registry_lifecycle(
                            model_id=normalized_model_id,
                            lifecycle_state=ModelLifecycleState.BLOCKED.value,
                            blocked_reason=blocked_reason,
                        )
                    )
                    registry_transition = {
                        "updated": True,
                        "action": "blocked",
                        "reason": "block_applied",
                        "records": transition_records,
                    }
                except Exception as exc:
                    registry_transition = {
                        "updated": False,
                        "action": "noop",
                        "reason": (
                            f"block_transition_failed:{exc.__class__.__name__}:{exc}"
                        ),
                        "records": transition_records,
                    }
                    add_check(
                        "registry_block_transition",
                        "fail",
                        registry_transition["reason"],
                    )
            else:
                registry_transition = {
                    "updated": False,
                    "action": "noop",
                    "reason": (
                        "already_blocked"
                        if current_record is not None
                        and current_record.lifecycle_state == ModelLifecycleState.BLOCKED
                        else "model_revoked"
                    ),
                    "records": [],
                }

        lifecycle_after_record = self._model_registry.get_by_id(normalized_model_id) if normalized_model_id else None
        lifecycle_after = (
            lifecycle_after_record.lifecycle_state.value
            if lifecycle_after_record is not None
            else lifecycle_before
        )

        metrics_snapshot = {
            "champion_shadow_row_count": champion_row_count,
            "champion_shadow_signal_divergence_ratio": _as_float(
                champion_summary.get("signal_divergence_ratio"),
                default=0.0,
            ),
            "champion_shadow_mean_abs_p_meta_delta": _as_float(
                champion_summary.get("mean_abs_p_meta_delta"),
                default=0.0,
            ),
            "champion_shadow_m11_score": _as_float(
                champion_m11_report.get("score"),
                default=0.0,
            ),
            "shadow_online_v2_status": str(shadow_online_v2_payload.get("status", "")).strip(),
            "shadow_online_v2_samples_used": _as_int(
                shadow_run_result.get("samples_used"),
                default=0,
            ),
            "shadow_online_v2_hard_block_min_samples": shadow_hard_block_min_samples,
            "shadow_online_v2_hard_gate_deferred": not shadow_hard_gate_enabled,
            "shadow_v2_minus_champion_return": _as_float(
                shadow_return_summary.get("shadow_v2_minus_champion_return"),
                default=0.0,
            ),
            "shadow_v2_minus_shadow_return": _as_float(
                shadow_return_summary.get("shadow_v2_minus_shadow_return"),
                default=0.0,
            ),
            "shadow_v2_delta_brier": _as_float(
                shadow_run_metrics.get("delta_brier"),
                default=0.0,
            ),
            "shadow_v2_delta_logloss": _as_float(
                shadow_run_metrics.get("delta_logloss"),
                default=0.0,
            ),
            "shadow_v2_signal_divergence_ratio": _as_float(
                shadow_divergence_ratio,
                default=0.0,
            ),
            "shadow_v2_m11_score": _as_float(
                shadow_m11_v2_report.get("score"),
                default=0.0,
            ),
            "execution_aware_shadow_minus_champion_score": _as_float(
                execution_aware_summary.get("shadow_minus_champion_execution_score"),
                default=0.0,
            ),
            "execution_aware_shadow_high_risk_ratio": _as_float(
                execution_aware_summary.get("shadow_high_risk_ratio"),
                default=0.0,
            ),
            "execution_aware_shadow_mean_can_fill": _as_float(
                execution_aware_summary.get("shadow_mean_can_fill"),
                default=0.0,
            ),
        }

        payload = {
            "ok": not errors,
            "mode": "learning_model_promotion_gate",
            "status": status,
            "accepted": accepted,
            "recommended_action": recommended_action,
            "shadow_model_id": normalized_model_id,
            "champion_model_id": champion_resolved_model_id,
            "dataset_manifest_id": dataset_manifest_id,
            "evaluation_split_names": list(normalized_splits),
            "lifecycle_before": lifecycle_before,
            "lifecycle_after": lifecycle_after,
            "role": role_before,
            "reason_codes": reason_codes,
            "blockers": blockers,
            "warnings": warnings,
            "gate_thresholds": {
                "min_samples": required_samples,
                "min_shadow_v2_minus_champion_return": float(
                    min_shadow_v2_minus_champion_return
                ),
                "max_shadow_v2_brier_delta": float(max_shadow_v2_brier_delta),
                "max_shadow_v2_logloss_delta": float(max_shadow_v2_logloss_delta),
                "max_signal_divergence_ratio": float(divergence_limit),
                "shadow_online_v2_hard_block_min_samples": shadow_hard_block_min_samples,
            },
            "metrics_snapshot": metrics_snapshot,
            "checks": checks,
            "champion_shadow_report": champion_shadow_report_payload,
            "shadow_online_v2_report": shadow_online_v2_payload,
            "execution_aware_report": execution_aware_payload,
            "registry_transition": registry_transition,
            "errors": errors,
        }
        self._record_audit_event(
            event_type="learning_model_promotion_gate_evaluated",
            level="info" if status == "pass" and not errors else "warn",
            message=(
                "learning model promotion gate passed"
                if status == "pass" and not errors
                else "learning model promotion gate requires review"
                if status == "warn" and not errors
                else "learning model promotion gate failed"
            ),
            payload={
                "ok": payload["ok"],
                "status": status,
                "accepted": accepted,
                "shadow_model_id": normalized_model_id,
                "champion_model_id": champion_resolved_model_id,
                "dataset_manifest_id": dataset_manifest_id,
                "reason_codes": list(reason_codes),
                "blockers": list(blockers),
                "warnings": list(warnings),
                "registry_transition": {
                    "updated": bool(registry_transition.get("updated", False)),
                    "action": str(registry_transition.get("action", "")),
                    "reason": str(registry_transition.get("reason", "")),
                },
            },
        )
        return payload

    def run_learning_manifest_shadow_promotion_gate(
        self,
        *,
        dataset_manifest_id: str = "",
        artifact_path: str | None = None,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_rows: bool = False,
        preview_limit: int = 5,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        load_predictor: bool = False,
        mark_shadow_validated: bool = True,
        min_shadow_v2_minus_champion_return: float = -0.02,
        max_shadow_v2_brier_delta: float = 0.05,
        max_shadow_v2_logloss_delta: float = 0.10,
        max_signal_divergence_ratio: float | None = None,
        approve_if_passed: bool = False,
        block_if_failed: bool = False,
    ) -> dict[str, object]:
        normalized_splits = _dedupe_preserve_order(
            [text for item in (split_names or []) if (text := str(item).strip())]
        ) or ["test"]
        shadow_validation_payload = self.run_learning_manifest_shadow_validation(
            dataset_manifest_id=dataset_manifest_id,
            artifact_path=artifact_path,
            champion_model_id=champion_model_id,
            split_names=normalized_splits,
            max_rows=max_rows,
            include_rows=include_rows,
            preview_limit=preview_limit,
            max_samples=max_samples,
            min_samples=min_samples,
            learning_rate=learning_rate,
            signal_threshold=signal_threshold,
            load_predictor=load_predictor,
            mark_shadow_validated=mark_shadow_validated,
        )
        shadow_errors = _string_list(shadow_validation_payload.get("errors", []))
        shadow_model_id = str(shadow_validation_payload.get("shadow_model_id", "")).strip()
        champion_resolved_model_id = str(
            shadow_validation_payload.get("champion_model_id", "") or champion_model_id
        ).strip()

        if bool(shadow_validation_payload.get("ok", False)) and shadow_model_id:
            promotion_gate_payload = self.evaluate_learning_model_promotion_gate(
                model_id=shadow_model_id,
                champion_model_id=champion_resolved_model_id,
                split_names=normalized_splits,
                max_rows=max_rows,
                max_samples=max_samples,
                min_samples=min_samples,
                learning_rate=learning_rate,
                signal_threshold=signal_threshold,
                preview_limit=preview_limit,
                min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
                max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
                max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
                max_signal_divergence_ratio=max_signal_divergence_ratio,
                approve_if_passed=approve_if_passed,
                block_if_failed=block_if_failed,
            )
        else:
            promotion_gate_payload = {
                "ok": False,
                "mode": "learning_model_promotion_gate",
                "status": "fail",
                "accepted": False,
                "recommended_action": "manual_review",
                "shadow_model_id": shadow_model_id,
                "champion_model_id": champion_resolved_model_id,
                "dataset_manifest_id": str(
                    shadow_validation_payload.get("dataset_manifest_id", "")
                ),
                "evaluation_split_names": list(normalized_splits),
                "lifecycle_before": "",
                "lifecycle_after": "",
                "role": "",
                "reason_codes": ["shadow_validation_failed"],
                "blockers": ["shadow_validation_failed"],
                "warnings": [],
                "gate_thresholds": {
                    "min_samples": max(1, int(min_samples)),
                    "min_shadow_v2_minus_champion_return": float(
                        min_shadow_v2_minus_champion_return
                    ),
                    "max_shadow_v2_brier_delta": float(max_shadow_v2_brier_delta),
                    "max_shadow_v2_logloss_delta": float(max_shadow_v2_logloss_delta),
                    "max_signal_divergence_ratio": (
                        float(max_signal_divergence_ratio)
                        if max_signal_divergence_ratio is not None
                        else _as_float(
                            self._config.evolution.m11_execution_divergence_limit,
                            default=0.35,
                        )
                    ),
                },
                "metrics_snapshot": {},
                "checks": [
                    {
                        "name": "shadow_validation_bundle",
                        "status": "fail",
                        "detail": "shadow validation workflow failed; promotion gate skipped",
                    }
                ],
                "champion_shadow_report": {},
                "shadow_online_v2_report": {},
                "registry_transition": {
                    "updated": False,
                    "action": "noop",
                    "reason": "shadow_validation_failed",
                    "records": [],
                },
                "errors": ["shadow_validation_failed", *shadow_errors],
            }

        gate_errors = _string_list(promotion_gate_payload.get("errors", []))
        combined_errors = shadow_errors + [
            error_text for error_text in gate_errors if error_text not in shadow_errors
        ]
        final_record = self._model_registry.get_by_id(shadow_model_id) if shadow_model_id else None
        final_lifecycle_state = (
            final_record.lifecycle_state.value if final_record is not None else ""
        )
        final_role = final_record.role.value if final_record is not None else ""

        shadow_validation_ok = bool(shadow_validation_payload.get("ok", False))
        promotion_gate_ok = bool(promotion_gate_payload.get("ok", False))
        promotion_gate_status = str(promotion_gate_payload.get("status", "")).strip().lower()
        ok = shadow_validation_ok
        payload = {
            "ok": ok,
            "mode": "learning_manifest_shadow_promotion_gate",
            "status": str(promotion_gate_payload.get("status", "")),
            "accepted": bool(promotion_gate_payload.get("accepted", False)),
            "recommended_action": str(
                promotion_gate_payload.get("recommended_action", "")
            ),
            "dataset_manifest_id": str(
                shadow_validation_payload.get("dataset_manifest_id", "")
                or promotion_gate_payload.get("dataset_manifest_id", "")
            ),
            "shadow_model_id": shadow_model_id,
            "champion_model_id": champion_resolved_model_id,
            "evaluation_split_names": list(normalized_splits),
            "final_lifecycle_state": final_lifecycle_state,
            "final_role": final_role,
            "shadow_validation_ok": shadow_validation_ok,
            "promotion_gate_ok": promotion_gate_ok,
            "shadow_validation": shadow_validation_payload,
            "promotion_gate": promotion_gate_payload,
            "errors": combined_errors,
        }
        self._record_audit_event(
            event_type="learning_manifest_shadow_promotion_gate",
            level="info" if shadow_validation_ok and promotion_gate_status == "pass" else "warn",
            message=(
                "learning manifest shadow promotion gate completed"
                if shadow_validation_ok and promotion_gate_status == "pass"
                else "learning manifest shadow promotion gate requires review"
                if shadow_validation_ok
                else "learning manifest shadow promotion gate failed"
            ),
            payload={
                "ok": ok,
                "status": payload["status"],
                "accepted": payload["accepted"],
                "shadow_validation_ok": shadow_validation_ok,
                "promotion_gate_ok": promotion_gate_ok,
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "shadow_model_id": shadow_model_id,
                "champion_model_id": champion_resolved_model_id,
                "evaluation_split_names": list(normalized_splits),
                "final_lifecycle_state": final_lifecycle_state,
                "promotion_reason_codes": _string_list(
                    promotion_gate_payload.get("reason_codes", [])
                ),
                "errors": list(combined_errors),
            },
        )
        return payload

    def create_learning_model_proposal(
        self,
        *,
        model_id: str,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        preview_limit: int = 5,
        min_shadow_v2_minus_champion_return: float = -0.02,
        max_shadow_v2_brier_delta: float = 0.05,
        max_shadow_v2_logloss_delta: float = 0.10,
        max_signal_divergence_ratio: float | None = None,
        approve_if_passed: bool = False,
        block_if_failed: bool = False,
        allow_warn_status: bool = True,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.create_learning_model_proposal(
            model_id=model_id,
            champion_model_id=champion_model_id,
            split_names=split_names,
            max_rows=max_rows,
            max_samples=max_samples,
            min_samples=min_samples,
            learning_rate=learning_rate,
            signal_threshold=signal_threshold,
            preview_limit=preview_limit,
            min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
            max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
            max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
            max_signal_divergence_ratio=max_signal_divergence_ratio,
            approve_if_passed=approve_if_passed,
            block_if_failed=block_if_failed,
            allow_warn_status=allow_warn_status,
            source_trace_id=source_trace_id,
        )

    def run_learning_manifest_shadow_proposal(
        self,
        *,
        dataset_manifest_id: str = "",
        artifact_path: str | None = None,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_rows: bool = False,
        preview_limit: int = 5,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        load_predictor: bool = False,
        mark_shadow_validated: bool = True,
        min_shadow_v2_minus_champion_return: float = -0.02,
        max_shadow_v2_brier_delta: float = 0.05,
        max_shadow_v2_logloss_delta: float = 0.10,
        max_signal_divergence_ratio: float | None = None,
        approve_if_passed: bool = False,
        block_if_failed: bool = False,
        allow_warn_status: bool = True,
        source_trace_id: str = "",
        auto_approve: bool = False,
        auto_release: bool = False,
        auto_reload_predictor: bool = True,
        notify_on_rejection: bool = False,
    ) -> dict[str, object]:
        return self._learning_governance_service.run_learning_manifest_shadow_proposal(
            dataset_manifest_id=dataset_manifest_id,
            artifact_path=artifact_path,
            champion_model_id=champion_model_id,
            split_names=split_names,
            max_rows=max_rows,
            include_rows=include_rows,
            preview_limit=preview_limit,
            max_samples=max_samples,
            min_samples=min_samples,
            learning_rate=learning_rate,
            signal_threshold=signal_threshold,
            load_predictor=load_predictor,
            mark_shadow_validated=mark_shadow_validated,
            min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
            max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
            max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
            max_signal_divergence_ratio=max_signal_divergence_ratio,
            approve_if_passed=approve_if_passed,
            block_if_failed=block_if_failed,
            allow_warn_status=allow_warn_status,
            source_trace_id=source_trace_id,
            auto_approve=auto_approve,
            auto_release=auto_release,
            auto_reload_predictor=auto_reload_predictor,
            notify_on_rejection=notify_on_rejection,
        )

    def latest_learning_model_proposal(self) -> dict[str, object] | None:
        return self._learning_governance_service.latest_learning_model_proposal()

    def learning_model_proposal_history(
        self,
        limit: int = 20,
        proposal_id: str = "",
        status: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.learning_model_proposal_history(
            limit=limit,
            proposal_id=proposal_id,
            status=status,
        )

    def record_learning_model_proposal_approval(
        self,
        approver: str,
        approved: bool,
        *,
        proposal_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.record_learning_model_proposal_approval(
            approver,
            approved,
            proposal_id=proposal_id,
            note=note,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def latest_learning_model_approval(self) -> dict[str, object] | None:
        return self._learning_governance_service.latest_learning_model_approval()

    def learning_model_approval_history(self, limit: int = 20) -> dict[str, object]:
        return self._learning_governance_service.learning_model_approval_history(limit=limit)

    def revoke_learning_model_proposal(
        self,
        revoked_by: str,
        *,
        proposal_id: str = "",
        note: str = "",
        revoke_model: bool = True,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.revoke_learning_model_proposal(
            revoked_by,
            proposal_id=proposal_id,
            note=note,
            revoke_model=revoke_model,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def issue_learning_model_release_ticket(
        self,
        operator: str,
        *,
        proposal_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.issue_learning_model_release_ticket(
            operator,
            proposal_id=proposal_id,
            note=note,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def execute_learning_model_release_ticket(
        self,
        executor: str,
        *,
        ticket_id: str = "",
        note: str = "",
        confirm_window: bool = True,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.execute_learning_model_release_ticket(
            executor,
            ticket_id=ticket_id,
            note=note,
            confirm_window=confirm_window,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def confirm_learning_model_release_ticket(
        self,
        confirmer: str,
        *,
        ticket_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.confirm_learning_model_release_ticket(
            confirmer,
            ticket_id=ticket_id,
            note=note,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def rollback_learning_model_release_ticket(
        self,
        rollback_by: str,
        *,
        ticket_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.rollback_learning_model_release_ticket(
            rollback_by,
            ticket_id=ticket_id,
            note=note,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
        )

    def run_learning_model_release_confirmation_watchdog(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._learning_governance_service.run_learning_model_release_confirmation_watchdog(
            now=now,
            source_trace_id=source_trace_id,
        )

    def latest_learning_model_release_ticket(self) -> dict[str, object] | None:
        return self._learning_governance_service.latest_learning_model_release_ticket()

    def learning_model_release_ticket_history(self, limit: int = 20) -> dict[str, object]:
        return self._learning_governance_service.learning_model_release_ticket_history(
            limit=limit
        )

    def learning_model_release_ticket_timeline(
        self,
        ticket_id: str = "",
        status: str = "",
        limit: int = 200,
    ) -> dict[str, object]:
        return self._learning_governance_service.learning_model_release_ticket_timeline(
            ticket_id=ticket_id,
            status=status,
            limit=limit,
        )

    def learning_model_governance_status(
        self,
        *,
        now: datetime | None = None,
        proposal_limit: int = 20,
        ticket_limit: int = 20,
    ) -> dict[str, object]:
        return self._learning_governance_service.learning_model_governance_status(
            now=now,
            proposal_limit=proposal_limit,
            ticket_limit=ticket_limit,
        )

    def _learning_pending_confirmation_count(self, now: datetime | None = None) -> int:
        return self._learning_governance_service._learning_pending_confirmation_count(now=now)

    def runtime_stage_snapshot(
        self,
        now: datetime | None = None,
        *,
        deep: bool = False,
    ) -> dict[str, object]:
        return self._runtime_ops_service.runtime_stage_snapshot(now=now, deep=deep)

    def sla_report(
        self,
        recent_runs: int = 50,
        session_scope: str = "all",
        job_scope: str = "all",
        target_ms: int = 60000,
        alert_target_ms: int = 30000,
        max_symbol_count: int | None = None,
    ) -> dict[str, object]:
        capped_runs = max(1, recent_runs)
        recent = self._latency_history_ms[-capped_runs:]
        scope = session_scope.strip().lower() or "all"
        normalized_job_scope = job_scope.strip().lower() or "all"
        resolved_target_ms = max(1, _as_int(target_ms, default=60000))
        resolved_alert_target_ms = max(1, _as_int(alert_target_ms, default=30000))
        resolved_max_symbol_count = (
            max(1, _as_int(max_symbol_count, default=0))
            if max_symbol_count is not None
            else None
        )
        observed_runs = len(recent)
        if scope != "all":
            recent = [item for item in recent if _sla_entry_matches_scope(item, scope)]
        session_scoped_runs = len(recent)
        if normalized_job_scope != "all":
            recent = [
                item for item in recent if _sla_entry_matches_job_scope(item, normalized_job_scope)
            ]
        job_scoped_runs = len(recent)
        if resolved_max_symbol_count is not None:
            recent = [
                item
                for item in recent
                if _as_int(item.get("symbol_count"), default=0) <= 0
                or _as_int(item.get("symbol_count"), default=0) <= resolved_max_symbol_count
            ]
        symbol_scoped_runs = len(recent)
        latencies = [_as_int(item.get("duration_ms"), default=0) for item in recent]
        if not latencies:
            return {
                "scope": scope,
                "job_scope": normalized_job_scope,
                "recent_runs": 0,
                "observed_runs": observed_runs,
                "session_scoped_runs": session_scoped_runs,
                "job_scoped_runs": job_scoped_runs,
                "excluded_by_session_scope": observed_runs - session_scoped_runs,
                "excluded_by_job_scope": session_scoped_runs - job_scoped_runs,
                "excluded_by_symbol_count": job_scoped_runs - symbol_scoped_runs,
                "target_ms": resolved_target_ms,
                "alert_target_ms": resolved_alert_target_ms,
                "max_symbol_count": resolved_max_symbol_count,
                "avg_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "max_ms": 0.0,
                "compliance_rate": 1.0,
                "alert_compliance_rate": 1.0,
                "status": "no_data",
                "slowest_runs": [],
            }

        compliance_count = sum(1 for item in latencies if item <= resolved_target_ms)
        alert_compliance_count = sum(1 for item in latencies if item <= resolved_alert_target_ms)
        compliance_rate = compliance_count / len(latencies)
        alert_compliance_rate = alert_compliance_count / len(latencies)

        return {
            "scope": scope,
            "job_scope": normalized_job_scope,
            "recent_runs": len(latencies),
            "observed_runs": observed_runs,
            "session_scoped_runs": session_scoped_runs,
            "job_scoped_runs": job_scoped_runs,
            "excluded_by_session_scope": observed_runs - session_scoped_runs,
            "excluded_by_job_scope": session_scoped_runs - job_scoped_runs,
            "excluded_by_symbol_count": job_scoped_runs - symbol_scoped_runs,
            "target_ms": resolved_target_ms,
            "alert_target_ms": resolved_alert_target_ms,
            "max_symbol_count": resolved_max_symbol_count,
            "avg_ms": round(sum(latencies) / len(latencies), 2),
            "p50_ms": round(_percentile(latencies, 0.50), 2),
            "p95_ms": round(_percentile(latencies, 0.95), 2),
            "max_ms": float(max(latencies)),
            "compliance_rate": round(compliance_rate, 4),
            "alert_compliance_rate": round(alert_compliance_rate, 4),
            "status": "ok" if compliance_rate >= 0.95 else "degraded",
            "slowest_runs": _slowest_latency_entries(recent, limit=5),
        }

    def _apply_c3_portfolio_constraints(
        self,
        *,
        signals: list[PipelineSignal],
        strategy: str,
    ) -> dict[str, object]:
        sector_limit = max(0, _as_int(self._config.soup_strategy.max_same_sector, default=0))
        correlation_threshold = max(
            0.0,
            _as_float(self._config.global_market.correlation_decay_threshold, default=0.45),
        )
        open_positions = {
            str(item.get("symbol", "")).strip(): item
            for item in self._portfolio.positions()
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        }
        projected_sector_counts = dict(self._portfolio.sector_counts())
        projected_symbols = {symbol for symbol in open_positions if symbol}
        returns_cache: dict[str, pd.Series | None] = {}
        sector_blocked = 0
        correlation_blocked = 0
        correlation_scaled = 0
        evaluated = 0

        buy_candidates = sorted(
            [
                signal
                for signal in signals
                if str(signal.action).strip().lower() == "buy" and float(signal.target_position) > 0
                and "model_disagreement_probe" not in {str(reason) for reason in signal.reasons}
            ],
            key=lambda item: (-float(item.score), str(item.symbol)),
        )
        for signal in buy_candidates:
            symbol = _normalize_a_share_symbol(signal.symbol)
            if not symbol:
                continue
            if symbol in open_positions:
                projected_symbols.add(symbol)
                continue
            evaluated += 1
            sector_tag = _infer_symbol_sector(symbol)
            if sector_limit > 0 and projected_sector_counts.get(sector_tag, 0) >= sector_limit:
                signal.action = "watch" if signal.grade in {"S", "A", "B"} else "hold"
                signal.target_position = 0.0
                reason = f"portfolio_sector_limit:{sector_tag}"
                if reason not in signal.reasons:
                    signal.reasons.append(reason)
                sector_blocked += 1
                continue

            correlation_pair = self._max_portfolio_correlation(
                symbol=symbol,
                peer_symbols=[peer for peer in projected_symbols if peer and peer != symbol],
                returns_cache=returns_cache,
            )
            if correlation_pair is not None:
                peer_symbol, corr_value = correlation_pair
                abs_corr = abs(corr_value)
                if abs_corr >= min(0.95, correlation_threshold + 0.20):
                    signal.action = "watch" if signal.grade in {"S", "A", "B"} else "hold"
                    signal.target_position = 0.0
                    reason = f"portfolio_corr_limit:{peer_symbol}:{abs_corr:.2f}"
                    if reason not in signal.reasons:
                        signal.reasons.append(reason)
                    correlation_blocked += 1
                    continue
                if abs_corr >= correlation_threshold:
                    scale = _clamp(1.0 - (abs_corr - correlation_threshold), 0.35, 0.85)
                    scaled_position = round(
                        _clamp(float(signal.target_position) * scale, 0.0, 1.0),
                        4,
                    )
                    if scaled_position < 0.01:
                        signal.action = "watch" if signal.grade in {"S", "A", "B"} else "hold"
                        signal.target_position = 0.0
                        reason = f"portfolio_corr_limit:{peer_symbol}:{abs_corr:.2f}"
                        if reason not in signal.reasons:
                            signal.reasons.append(reason)
                        correlation_blocked += 1
                        continue
                    if scaled_position < float(signal.target_position) - 1e-9:
                        signal.target_position = scaled_position
                        reason = f"portfolio_corr_scaled:{peer_symbol}:{abs_corr:.2f}"
                        if reason not in signal.reasons:
                            signal.reasons.append(reason)
                        correlation_scaled += 1

            projected_sector_counts[sector_tag] = projected_sector_counts.get(sector_tag, 0) + 1
            projected_symbols.add(symbol)

        return {
            "strategy": strategy,
            "evaluated": evaluated,
            "sector_limit": sector_limit,
            "correlation_threshold": round(correlation_threshold, 4),
            "sector_blocked": sector_blocked,
            "correlation_blocked": correlation_blocked,
            "correlation_scaled": correlation_scaled,
            "projected_sector_counts": projected_sector_counts,
        }

    def _recent_return_series(
        self,
        *,
        symbol: str,
        returns_cache: dict[str, pd.Series | None],
        lookback_days: int = 90,
        tail_days: int = 60,
    ) -> pd.Series | None:
        cached = returns_cache.get(symbol)
        if symbol in returns_cache:
            return cached
        try:
            bars = self._provider.fetch_daily_bars(
                symbol=symbol,
                lookback_days=max(lookback_days, tail_days + 10),
            )
        except Exception:
            returns_cache[symbol] = None
            return None
        if bars.empty or "close" not in bars.columns:
            returns_cache[symbol] = None
            return None
        close = _numeric_series(bars, "close")
        if len(close) < 25:
            returns_cache[symbol] = None
            return None
        returns = close.pct_change().dropna().tail(tail_days)
        if len(returns) < 20:
            returns_cache[symbol] = None
            return None
        returns_cache[symbol] = returns
        return returns

    def _max_portfolio_correlation(
        self,
        *,
        symbol: str,
        peer_symbols: list[str],
        returns_cache: dict[str, pd.Series | None],
    ) -> tuple[str, float] | None:
        if not peer_symbols:
            return None
        candidate_returns = self._recent_return_series(
            symbol=symbol,
            returns_cache=returns_cache,
        )
        if candidate_returns is None:
            return None
        best_pair: tuple[str, float] | None = None
        for peer_symbol in peer_symbols:
            peer_returns = self._recent_return_series(
                symbol=peer_symbol,
                returns_cache=returns_cache,
            )
            if peer_returns is None:
                continue
            aligned = pd.concat(
                [
                    candidate_returns.rename("candidate"),
                    peer_returns.rename("peer"),
                ],
                axis=1,
            ).dropna()
            if len(aligned) < 20:
                continue
            corr = float(aligned["candidate"].corr(aligned["peer"]))
            if corr != corr:
                continue
            if best_pair is None or abs(corr) > abs(best_pair[1]):
                best_pair = (peer_symbol, corr)
        return best_pair

    def _build_c3_position_management_items(
        self,
        *,
        now: datetime,
        persist_peak_state: bool,
    ) -> list[dict[str, object]]:
        positions = self.portfolio_positions()
        if not positions:
            return []

        stop_loss_pct = (
            max(0.0, _as_float(self._config.soup_strategy.stop_loss, default=5.0)) / 100.0
        )
        take_profit_levels = sorted(
            [
                _as_float(item, default=0.0)
                for item in self._config.soup_strategy.take_profit
                if _as_float(item, default=0.0) > 0
            ]
        )
        first_take_profit = (
            take_profit_levels[0] / 100.0 if take_profit_levels else max(stop_loss_pct, 0.05)
        )
        second_take_profit = (
            take_profit_levels[1] / 100.0
            if len(take_profit_levels) >= 2
            else first_take_profit + max(stop_loss_pct, 0.03)
        )
        trailing_stop_pct = (
            max(
                0.0,
                _as_float(self._config.soup_strategy.trailing_stop, default=5.0),
            )
            / 100.0
        )
        max_hold_days = max(1, _as_int(self._config.soup_strategy.max_hold_days, default=5))
        warn_hold_days = max(1, max_hold_days - 1)

        bars_cache: dict[str, pd.DataFrame] = {}
        items: list[dict[str, object]] = []
        for position in positions:
            symbol = str(position.get("symbol", "")).strip()
            if not symbol:
                continue
            entry_price = _as_float(position.get("entry_price"), default=0.0)
            current_target = max(0.0, _as_float(position.get("target_position"), default=0.0))
            if entry_price <= 0 or current_target <= 0:
                continue
            latest_price = self._resolve_latest_close_price(symbol=symbol, bars_cache=bars_cache)
            if latest_price is None or latest_price <= 0:
                continue

            pnl_pct = latest_price / entry_price - 1.0
            quantity = max(0, _as_int(position.get("quantity"), default=0))
            fee = max(0.0, _as_float(position.get("fee"), default=0.0))
            pnl_amount = (latest_price - entry_price) * quantity - fee if quantity > 0 else 0.0
            opened_at = _parse_iso_datetime(position.get("opened_at"))
            hold_days = max(0, (now.date() - opened_at.date()).days) if opened_at else 0

            take_profit_stage = max(0, _as_int(position.get("take_profit_stage"), default=0))
            stored_peak_price = max(0.0, _as_float(position.get("peak_price"), default=0.0))
            stored_peak_pnl_pct = _as_float(position.get("peak_pnl_pct"), default=0.0)
            peak_price = max(stored_peak_price, latest_price)
            peak_pnl_pct = max(stored_peak_pnl_pct, pnl_pct)
            drawdown_from_peak = latest_price / peak_price - 1.0 if peak_price > 0 else 0.0

            if persist_peak_state:
                self._portfolio.annotate_position_state(
                    symbol=symbol,
                    timestamp=now,
                    peak_price=peak_price,
                    peak_pnl_pct=peak_pnl_pct,
                )

            severity = ""
            reason = ""
            exit_action = ""
            next_target_position = current_target
            next_take_profit_stage = take_profit_stage
            if stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
                severity = "warn"
                reason = "stop_loss_threshold_reached"
                exit_action = "exit_full"
                next_target_position = 0.0
            elif take_profit_stage < 1 and first_take_profit > 0 and pnl_pct >= first_take_profit:
                severity = "info"
                reason = "take_profit_stage_1_reached"
                exit_action = "trim"
                next_target_position = round(current_target * (2.0 / 3.0), 4)
                next_take_profit_stage = 1
            elif take_profit_stage < 2 and second_take_profit > 0 and pnl_pct >= second_take_profit:
                severity = "info"
                reason = "take_profit_stage_2_reached"
                exit_action = "trim"
                next_target_position = round(current_target * 0.5, 4)
                next_take_profit_stage = 2
            elif (
                take_profit_stage >= 2
                and trailing_stop_pct > 0
                and peak_price > 0
                and drawdown_from_peak <= -trailing_stop_pct
            ):
                severity = "warn"
                reason = "trailing_stop_remainder_exit"
                exit_action = "exit_full"
                next_target_position = 0.0
            elif hold_days >= warn_hold_days:
                severity = "warn"
                reason = "max_hold_days_near_limit"
            if not severity:
                continue

            items.append(
                {
                    "symbol": symbol,
                    "strategy": str(position.get("strategy", "")),
                    "severity": severity,
                    "reason": reason,
                    "entry_price": round(entry_price, 6),
                    "latest_price": round(latest_price, 6),
                    "pnl_pct": round(pnl_pct, 6),
                    "pnl_amount": round(pnl_amount, 2),
                    "quantity": quantity,
                    "fee": round(fee, 6),
                    "hold_days": hold_days,
                    "max_hold_days": max_hold_days,
                    "updated_at": now.isoformat(),
                    "take_profit_stage": take_profit_stage,
                    "next_take_profit_stage": next_take_profit_stage,
                    "peak_price": round(peak_price, 6),
                    "peak_pnl_pct": round(peak_pnl_pct, 6),
                    "drawdown_from_peak": round(drawdown_from_peak, 6),
                    "trailing_stop_pct": round(trailing_stop_pct, 6),
                    "current_target_position": round(current_target, 4),
                    "next_target_position": round(max(0.0, next_target_position), 4),
                    "exit_action": exit_action,
                }
            )

        return sorted(
            items,
            key=lambda item: (
                0 if str(item.get("severity", "")) == "warn" else 1,
                -abs(_as_float(item.get("pnl_pct"), default=0.0)),
            ),
        )

    def _build_c3_hrp_shadow_portfolio(
        self,
        *,
        signals: list[PipelineSignal],
        strategy: str,
    ) -> dict[str, object]:
        candidate_signals = sorted(
            [
                signal
                for signal in signals
                if str(signal.action).strip().lower() in {"buy", "watch"}
            ],
            key=lambda item: (-float(item.score), str(item.symbol)),
        )
        candidates = candidate_signals[:12]
        candidate_items = [
            {
                "symbol": _normalize_a_share_symbol(signal.symbol),
                "score": round(float(signal.score), 4),
                "grade": str(signal.grade),
                "action": str(signal.action),
                "target_position": round(float(signal.target_position), 4),
                "sector": _infer_symbol_sector(signal.symbol),
            }
            for signal in candidates
            if _normalize_a_share_symbol(signal.symbol)
        ]
        if len(candidate_items) < 5:
            return {
                "status": "skipped",
                "strategy": strategy,
                "optimizer": "hrp_shadow",
                "reason": "insufficient_candidates",
                "candidate_count": len(candidate_items),
                "weights": [],
                "candidates": candidate_items,
            }

        returns_cache: dict[str, pd.Series | None] = {}
        returns_by_symbol: dict[str, pd.Series] = {}
        for item in candidate_items:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            series = self._recent_return_series(
                symbol=symbol,
                returns_cache=returns_cache,
            )
            if series is not None:
                returns_by_symbol[symbol] = series
        if len(returns_by_symbol) < 5:
            return {
                "status": "degraded",
                "strategy": strategy,
                "optimizer": "hrp_shadow",
                "reason": "insufficient_return_history",
                "candidate_count": len(candidate_items),
                "usable_symbols": sorted(returns_by_symbol),
                "weights": [],
                "candidates": candidate_items,
            }

        returns_frame = pd.DataFrame(returns_by_symbol).dropna(axis=1, how="all")
        clean_frame = returns_frame.dropna(axis=0, how="any")
        if clean_frame.shape[0] < 20 or clean_frame.shape[1] < 5:
            return {
                "status": "degraded",
                "strategy": strategy,
                "optimizer": "hrp_shadow",
                "reason": "insufficient_overlap_returns",
                "candidate_count": len(candidate_items),
                "usable_symbols": list(clean_frame.columns),
                "weights": [],
                "candidates": candidate_items,
            }

        try:
            from pypfopt.hierarchical_portfolio import HRPOpt  # type: ignore[import-not-found]

            optimizer = HRPOpt(returns=clean_frame.tail(60))
            raw_weights = optimizer.optimize()
            method = "pypfopt_hrp"
            status = "ready"
        except Exception:
            volatility = clean_frame.tail(60).std(ddof=0).replace(0.0, np.nan).dropna()
            if volatility.empty:
                raw_weights = {symbol: 1.0 / clean_frame.shape[1] for symbol in clean_frame.columns}
            else:
                inverse_vol = 1.0 / volatility
                raw_weights = (inverse_vol / inverse_vol.sum()).to_dict()
            method = "inverse_vol_fallback"
            status = "fallback"

        weights = [
            {
                "symbol": str(symbol),
                "weight": round(max(0.0, _as_float(value, default=0.0)), 6),
                "sector": _infer_symbol_sector(symbol),
            }
            for symbol, value in sorted(
                raw_weights.items(),
                key=lambda item: (-_as_float(item[1], default=0.0), str(item[0])),
            )
            if _as_float(value, default=0.0) > 0
        ]
        return {
            "status": status,
            "strategy": strategy,
            "optimizer": "hrp_shadow",
            "method": method,
            "candidate_count": len(candidate_items),
            "lookback_days": 60,
            "weights": weights,
            "candidates": candidate_items,
        }

    def holding_alerts(
        self,
        now: datetime | None = None,
        persist_peak_state: bool = True,
    ) -> dict[str, object]:
        current = now or datetime.now()
        items = self._build_c3_position_management_items(
            now=current,
            persist_peak_state=persist_peak_state,
        )
        if not items:
            return {"records": 0, "summary": {"warn": 0, "info": 0}, "items": []}
        warn_count = sum(1 for item in items if str(item.get("severity", "")) == "warn")
        info_count = sum(1 for item in items if str(item.get("severity", "")) == "info")
        return {
            "records": len(items),
            "summary": {"warn": warn_count, "info": info_count},
            "items": items,
        }

    def _suppress_holding_alerts_after_auto_exits(
        self,
        *,
        holding_alerts: Mapping[str, object],
        portfolio_update: Mapping[str, object],
    ) -> dict[str, object]:
        raw_executions = portfolio_update.get("executions")
        raw_items = holding_alerts.get("items")
        if not isinstance(raw_executions, list) or not isinstance(raw_items, list):
            return dict(holding_alerts)

        exit_symbols = {
            _normalize_a_share_symbol(item.get("symbol"))
            for item in raw_executions
            if isinstance(item, Mapping)
            and str(item.get("side", "")).strip().lower() == "sell"
            and str(item.get("status", "")).strip().lower() in {"trimmed", "closed"}
        }
        exit_symbols.discard("")
        if not exit_symbols:
            return dict(holding_alerts)

        actionable_reasons = {
            "stop_loss_threshold_reached",
            "take_profit_threshold_reached",
            "take_profit_stage_1_reached",
            "take_profit_stage_2_reached",
            "trailing_stop_remainder_exit",
        }
        kept: list[object] = []
        suppressed: list[dict[str, object]] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                kept.append(item)
                continue
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            reason = str(item.get("reason", "")).strip()
            if symbol in exit_symbols and reason in actionable_reasons:
                suppressed.append({"symbol": symbol, "reason": reason})
                continue
            kept.append(item)
        if not suppressed:
            return dict(holding_alerts)

        warn_count = sum(
            1
            for item in kept
            if isinstance(item, Mapping) and str(item.get("severity", "")) == "warn"
        )
        info_count = sum(
            1
            for item in kept
            if isinstance(item, Mapping) and str(item.get("severity", "")) == "info"
        )
        filtered = dict(holding_alerts)
        filtered["records"] = len(kept)
        filtered["summary"] = {"warn": warn_count, "info": info_count}
        filtered["items"] = kept
        filtered["suppressed_after_auto_execution"] = suppressed
        return filtered

    def _sync_recommendation_lifecycle_from_holding_alerts(
        self,
        *,
        holding_alerts: Mapping[str, object],
        timestamp: datetime,
        trace_id: str,
    ) -> dict[str, object]:
        raw_items = holding_alerts.get("items")
        if not isinstance(raw_items, list):
            return {"updated": 0, "symbols": []}
        updated = 0
        symbols: list[str] = []
        now_iso = timestamp.isoformat()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol:
                continue
            reason = str(item.get("reason", "")).strip() or "holding_alert"
            severity = str(item.get("severity", "")).strip().lower()
            exit_action = str(item.get("exit_action", "")).strip().lower()
            should_mark_alert = severity == "warn" or exit_action in {"trim", "exit_full"}
            if not should_mark_alert:
                continue
            existing = self._recommendation_lifecycle.get(symbol, {})
            previous = dict(existing)
            current_status = _normalize_recommendation_status(
                existing.get("status"),
                default="watching",
            )
            if current_status in _RECOMMENDATION_TERMINAL_STATUSES:
                continue
            strategy_value = (
                str(item.get("strategy", "")).strip().lower()
                or str(existing.get("strategy", "manual")).strip().lower()
                or "manual"
            )
            first_recommended_at = str(existing.get("first_recommended_at", "")).strip() or now_iso
            latest_price = _as_float(item.get("latest_price"), default=0.0)
            entry_price = _as_float(item.get("entry_price"), default=0.0)
            current_return = _as_float(item.get("pnl_pct"), default=0.0)
            record = {
                "symbol": symbol,
                "strategy": strategy_value,
                "status": "sell_alert",
                "first_recommended_at": first_recommended_at,
                "last_signal_at": str(existing.get("last_signal_at", "")),
                "last_signal_score": _as_float(existing.get("last_signal_score"), default=0.0),
                "last_signal_action": str(existing.get("last_signal_action", "")),
                "last_manual_update_at": str(existing.get("last_manual_update_at", "")),
                "updated_at": now_iso,
                "last_source": "holding_alert",
                "last_trace_id": trace_id,
                "note": str(existing.get("note", "")),
            }
            self._merge_recommendation_extra_fields(
                record,
                existing,
                extra={
                    "entry_price": round(entry_price, 6) if entry_price > 0 else None,
                    "entry_quantity": _as_int(item.get("quantity"), default=0),
                    "exit_alert_at": now_iso,
                    "exit_alert_reason": reason,
                    "exit_price": round(latest_price, 6) if latest_price > 0 else None,
                    "last_price": round(latest_price, 6) if latest_price > 0 else None,
                    "last_exit_action": exit_action,
                    "unrealized_return_pct": round(current_return, 6),
                    "current_return_pct": round(current_return, 6),
                    "holding_days": _as_int(item.get("hold_days"), default=0),
                    "outcome_status": "open",
                },
            )
            self._append_recommendation_event(
                record,
                timestamp=timestamp,
                event="sell_alert",
                source="holding_alert",
                trace_id=trace_id,
                details={
                    "reason": reason,
                    "severity": severity,
                    "exit_action": exit_action,
                    "current_return_pct": round(current_return, 6),
                },
            )
            self._recommendation_lifecycle[symbol] = record
            if record != previous:
                updated += 1
                symbols.append(symbol)
        return {"updated": updated, "symbols": sorted({symbol for symbol in symbols if symbol})}

    def _resolve_latest_close_price(
        self,
        *,
        symbol: str,
        bars_cache: dict[str, pd.DataFrame],
    ) -> float | None:
        bars = bars_cache.get(symbol)
        if bars is None:
            try:
                bars = self._provider.fetch_daily_bars(symbol=symbol, lookback_days=120)
            except Exception:
                bars = pd.DataFrame()
            bars_cache[symbol] = bars
        if bars.empty or "close" not in bars.columns:
            return None
        close = _numeric_series(bars, "close")
        if close.empty:
            return None
        return float(close.iloc[-1])

    def _live_auto_execution_enabled(self, *, use_live_runtime: bool) -> bool:
        return (
            not bool(self._config.app.advisory_only)
            and self._runtime_mode() == "simulation"
            and use_live_runtime
        )

    def _simulation_initial_cash(self) -> float:
        configured = _as_float(self._config.dashboard.default_total_asset, default=0.0)
        if configured > 0:
            return round(configured, 2)
        return 100000.0

    def _simulation_account_name(self) -> str:
        return "sim_auto"

    def _simulation_lot_size(self) -> int:
        rounding_rule = str(self._config.backtest_matcher.share_rounding_rule).strip().lower()
        if "100" in rounding_rule:
            return 100
        return 100

    def _simulation_cash_available(self) -> float:
        cash = self._simulation_initial_cash()
        portfolio_state = self._portfolio.export_state()
        raw_trades = portfolio_state.get("trades", []) if isinstance(portfolio_state, dict) else []
        if not isinstance(raw_trades, list):
            return cash
        for item in raw_trades:
            if not isinstance(item, dict):
                continue
            side = str(item.get("side", "")).strip().lower()
            if side == "buy":
                price = _as_float(item.get("entry_price"), default=0.0)
                quantity = _as_int(item.get("quantity"), default=0)
                fee = _as_float(item.get("fee"), default=0.0)
                if price > 0 and quantity > 0:
                    cash -= price * quantity + fee
            elif side == "sell":
                price = _as_float(item.get("exit_price"), default=0.0)
                quantity = _as_int(item.get("exit_quantity"), default=0)
                fee = _as_float(item.get("exit_fee"), default=0.0)
                if price > 0 and quantity > 0:
                    cash += price * quantity - fee
        return round(cash, 2)

    def _estimate_simulated_trade_fee(
        self,
        *,
        side: str,
        notional: float,
    ) -> float:
        if notional <= 0:
            return 0.0
        matcher = self._config.backtest_matcher
        commission = max(
            notional * _as_float(matcher.commission_rate, default=0.0),
            _as_float(matcher.min_commission_per_order, default=0.0),
        )
        transfer = notional * _as_float(matcher.transfer_fee_rate, default=0.0)
        stamp = 0.0
        if (
            side.strip().lower() == "sell"
            and str(matcher.stamp_tax_apply_on).strip().lower() == "sell_only"
        ):
            stamp = notional * _as_float(matcher.stamp_tax_rate, default=0.0)
        return round(commission + transfer + stamp, 2)

    def _select_simulated_trade_price(
        self,
        *,
        side: str,
        market_payload: dict[str, object],
    ) -> tuple[float, str]:
        normalized_side = side.strip().lower()
        if normalized_side == "buy":
            ask_levels = market_payload.get("ask_levels", [])
            if isinstance(ask_levels, list):
                for item in ask_levels:
                    if not isinstance(item, dict):
                        continue
                    price = _as_float(item.get("price"), default=0.0)
                    if price > 0:
                        return price, f"五档卖{_as_int(item.get('level'), default=1)}"
        if normalized_side == "sell":
            bid_levels = market_payload.get("bid_levels", [])
            if isinstance(bid_levels, list):
                for item in bid_levels:
                    if not isinstance(item, dict):
                        continue
                    price = _as_float(item.get("price"), default=0.0)
                    if price > 0:
                        return price, f"五档买{_as_int(item.get('level'), default=1)}"
        for field_name, label in (
            ("last_price", "最新价"),
            ("open_price", "开盘价"),
            ("prev_close", "昨收价"),
        ):
            price = _as_float(market_payload.get(field_name), default=0.0)
            if price > 0:
                return price, label
        return 0.0, "无可用价格"

    def _apply_live_auto_portfolio_signals(
        self,
        *,
        trace_id: str,
        timestamp: datetime,
        signals: list[PipelineSignal],
        use_live_runtime: bool,
        dry_run: bool = False,
    ) -> dict[str, object]:
        portfolio_state_before = self._portfolio.export_state() if dry_run else None
        watchlist_before = list(self._state.watchlist) if dry_run else []
        current_equity_before = float(self._state.current_equity) if dry_run else 0.0
        base_update = self._portfolio.apply_signals(
            trace_id=trace_id,
            timestamp=timestamp,
            signals=[],
        )
        executions: list[dict[str, object]] = []
        opened = 0
        adjusted = 0
        trimmed = 0
        closed_signals = 0
        skipped_max_holdings = _as_int(base_update.get("skipped_max_holdings"), default=0)
        skipped_same_sector = _as_int(base_update.get("skipped_same_sector"), default=0)
        skipped_no_cash = 0
        available_cash = self._simulation_cash_available()
        initial_cash = self._simulation_initial_cash()
        execution_attempts: dict[str, int] = {
            "signals": len(signals),
            "empty_symbol": 0,
            "exit_plan_items": 0,
            "exit_plan_executed": 0,
            "managed_exit_symbols": 0,
            "sell_signals": 0,
            "sell_no_position": 0,
            "sell_close_failed": 0,
            "sell_executed": 0,
            "buy_signals": 0,
            "buy_zero_target": 0,
            "buy_managed_exit_skipped": 0,
            "buy_existing_position": 0,
            "buy_existing_adjusted": 0,
            "buy_existing_unchanged": 0,
            "buy_new_candidates": 0,
            "buy_new_attempted": 0,
            "buy_new_rejected": 0,
            "buy_new_filled": 0,
            "non_buy_signals": 0,
        }

        relevant_symbols = _dedupe_preserve_order(
            [
                symbol
                for symbol in (
                    _normalize_a_share_symbol(getattr(signal, "symbol", "")) for signal in signals
                )
                if symbol
            ]
            + [
                str(item.get("symbol", "")).strip()
                for item in self._portfolio.positions()
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            ]
        )
        depth_snapshots = self._fetch_market_depth_snapshots(
            symbols=relevant_symbols,
            scope="signal_pool",
            force_refresh=True,
        )
        market_cache: dict[str, dict[str, object]] = {}

        def _market_payload_for(symbol: str) -> dict[str, object]:
            payload = market_cache.get(symbol)
            if payload is not None:
                return payload
            payload = self._build_week5_symbol_market_payload(
                symbol=symbol,
                prefer_online=use_live_runtime,
                depth_snapshot=depth_snapshots.get(symbol, {}),
            )
            market_cache[symbol] = payload
            return payload

        def _append_rejected_buy_execution(
            *,
            signal: PipelineSignal,
            symbol: str,
            status: str,
            reason: str,
            price: float = 0.0,
            price_source: str = "",
        ) -> None:
            execution_attempts["buy_new_rejected"] += 1
            block_category = ""
            if reason in {
                "auto_simulated_buy_no_cash",
                "auto_simulated_buy_no_cash_after_fee",
                "auto_simulated_buy_quantity_zero",
            }:
                execution_attempts["pre_trade_blocked"] = (
                    _as_int(execution_attempts.get("pre_trade_blocked"), default=0) + 1
                )
                block_category = "pre_trade_blocked"
            elif reason in {
                "auto_simulated_buy_max_holdings",
                "auto_simulated_buy_same_sector",
                "max_position_limit_reached",
                "max_holdings_reached",
            }:
                execution_attempts["risk_gate_blocked"] = (
                    _as_int(execution_attempts.get("risk_gate_blocked"), default=0) + 1
                )
                block_category = "risk_gate_blocked"
            executions.append(
                {
                    "trade_id": f"SKIP-{trace_id[:8]}-{symbol}-{status}",
                    "symbol": symbol,
                    "side": "buy",
                    "status": status,
                    "block_category": block_category,
                    "strategy": str(signal.strategy).strip() or "trend",
                    "target_position": round(max(0.0, float(signal.target_position)), 4),
                    "price": round(price, 6) if price > 0 else 0.0,
                    "quantity": 0,
                    "amount": 0.0,
                    "fee": 0.0,
                    "price_source": price_source.strip() or "no_fill",
                    "trade_time": timestamp.isoformat(),
                    "reason": reason,
                }
            )

        exit_plan_items = self._build_c3_position_management_items(
            now=timestamp,
            persist_peak_state=not dry_run,
        )
        actionable_exit_items = [
            item
            for item in exit_plan_items
            if str(item.get("exit_action", "")).strip() in {"trim", "exit_full"}
        ]
        execution_attempts["exit_plan_items"] = len(actionable_exit_items)
        managed_exit_symbols = {
            _normalize_a_share_symbol(str(item.get("symbol", "")))
            for item in actionable_exit_items
            if _normalize_a_share_symbol(str(item.get("symbol", "")))
        }
        execution_attempts["managed_exit_symbols"] = len(managed_exit_symbols)
        for item in actionable_exit_items:
            symbol = _normalize_a_share_symbol(str(item.get("symbol", "")))
            if not symbol:
                continue
            open_positions = {
                str(position.get("symbol", "")).strip(): position
                for position in self._portfolio.positions()
                if isinstance(position, dict) and str(position.get("symbol", "")).strip()
            }
            existing = open_positions.get(symbol)
            if not isinstance(existing, dict):
                continue
            current_quantity = _as_int(existing.get("quantity"), default=0)
            current_target = max(0.0, _as_float(existing.get("target_position"), default=0.0))
            next_target = max(0.0, _as_float(item.get("next_target_position"), default=0.0))
            market_payload = _market_payload_for(symbol)
            trade_price, price_source = self._select_simulated_trade_price(
                side="sell",
                market_payload=market_payload,
            )
            exit_quantity = current_quantity
            if current_quantity > 0 and current_target > 0 and next_target < current_target:
                remaining_ratio = max(0.0, min(1.0, next_target / current_target))
                remaining_quantity = int(round(current_quantity * remaining_ratio))
                remaining_quantity = min(current_quantity, max(0, remaining_quantity))
                if next_target > 0 and remaining_quantity >= current_quantity:
                    remaining_quantity = current_quantity - 1
                exit_quantity = max(0, current_quantity - remaining_quantity)
            fee_base_quantity = exit_quantity if exit_quantity > 0 else current_quantity
            fee = (
                self._estimate_simulated_trade_fee(
                    side="sell",
                    notional=trade_price * fee_base_quantity,
                )
                if trade_price > 0 and fee_base_quantity > 0
                else 0.0
            )
            close_fill: dict[str, object] = {
                "exit_price": round(trade_price, 6) if trade_price > 0 else None,
                "quantity": exit_quantity if exit_quantity > 0 else None,
                "fee": fee,
                "account": self._simulation_account_name(),
                "manual_trade_time": timestamp.isoformat(),
                "note": f"{item.get('reason', 'holding_exit')}:{price_source}",
            }
            exit_action = str(item.get("exit_action", "")).strip()
            reason = str(item.get("reason", "")).strip() or "holding_exit"
            if exit_action == "exit_full":
                closed = self._portfolio.close_position(
                    symbol=symbol,
                    timestamp=timestamp,
                    trace_id=trace_id,
                    reason=reason,
                    manual_fill=close_fill,
                )
                if not closed:
                    continue
                closed_signals += 1
                sell_quantity = exit_quantity if exit_quantity > 0 else current_quantity
                available_cash = round(
                    available_cash + max(0.0, trade_price * sell_quantity - fee),
                    2,
                )
                status = "closed"
                target_after = 0.0
            else:
                status = self._portfolio.reduce_position(
                    symbol=symbol,
                    target_position=next_target,
                    timestamp=timestamp,
                    trace_id=trace_id,
                    reason=reason,
                    manual_fill=close_fill,
                )
                if status not in {"trimmed", "closed"}:
                    continue
                if status == "closed":
                    closed_signals += 1
                    target_after = 0.0
                else:
                    trimmed += 1
                    target_after = next_target
                    self._portfolio.annotate_position_state(
                        symbol=symbol,
                        timestamp=timestamp,
                        take_profit_stage=_as_int(item.get("next_take_profit_stage"), default=0),
                        peak_price=_as_float(item.get("peak_price"), default=0.0),
                        peak_pnl_pct=_as_float(item.get("peak_pnl_pct"), default=0.0),
                    )
                sell_quantity = exit_quantity if exit_quantity > 0 else current_quantity
                available_cash = round(
                    available_cash + max(0.0, trade_price * sell_quantity - fee),
                    2,
                )
            trade = self._portfolio.trades(limit=1)[0]
            executions.append(
                {
                    "trade_id": str(trade.get("trade_id", "")).strip(),
                    "symbol": symbol,
                    "side": "sell",
                    "status": status,
                    "strategy": str(item.get("strategy", "")).strip() or "trend",
                    "target_position": round(target_after, 4),
                    "price": round(trade_price, 6) if trade_price > 0 else 0.0,
                    "quantity": sell_quantity,
                    "amount": round(trade_price * sell_quantity, 2)
                    if trade_price > 0 and sell_quantity > 0
                    else 0.0,
                    "fee": fee,
                    "price_source": price_source,
                    "trade_time": timestamp.isoformat(),
                    "reason": reason,
                }
            )
            execution_attempts["exit_plan_executed"] += 1

        for signal in signals:
            symbol = _normalize_a_share_symbol(signal.symbol)
            if not symbol:
                execution_attempts["empty_symbol"] += 1
                continue
            action = str(signal.action).strip().lower()
            if action == "sell":
                execution_attempts["sell_signals"] += 1
                open_positions = {
                    str(item.get("symbol", "")).strip(): item
                    for item in self._portfolio.positions()
                }
                existing = open_positions.get(symbol)
                if not isinstance(existing, dict):
                    execution_attempts["sell_no_position"] += 1
                    continue
                market_payload = _market_payload_for(symbol)
                trade_price, price_source = self._select_simulated_trade_price(
                    side="sell",
                    market_payload=market_payload,
                )
                quantity = _as_int(existing.get("quantity"), default=0)
                fee = (
                    self._estimate_simulated_trade_fee(
                        side="sell",
                        notional=trade_price * quantity,
                    )
                    if trade_price > 0 and quantity > 0
                    else 0.0
                )
                auto_close_fill: dict[str, object] = {
                    "exit_price": round(trade_price, 6) if trade_price > 0 else None,
                    "quantity": quantity if quantity > 0 else None,
                    "fee": fee,
                    "account": self._simulation_account_name(),
                    "manual_trade_time": timestamp.isoformat(),
                    "note": f"auto_simulated_sell:{price_source}",
                }
                closed = self._portfolio.close_position(
                    symbol=symbol,
                    timestamp=timestamp,
                    trace_id=trace_id,
                    reason="auto_simulated_sell",
                    manual_fill=auto_close_fill,
                )
                if not closed:
                    execution_attempts["sell_close_failed"] += 1
                    continue
                closed_signals += 1
                execution_attempts["sell_executed"] += 1
                trade = self._portfolio.trades(limit=1)[0]
                executions.append(
                    {
                        "trade_id": str(trade.get("trade_id", "")).strip(),
                        "symbol": symbol,
                        "side": "sell",
                        "status": "closed",
                        "strategy": str(signal.strategy).strip() or "trend",
                        "target_position": 0.0,
                        "price": round(trade_price, 6) if trade_price > 0 else 0.0,
                        "quantity": quantity,
                        "amount": round(trade_price * quantity, 2)
                        if trade_price > 0 and quantity > 0
                        else 0.0,
                        "fee": fee,
                        "price_source": price_source,
                        "trade_time": timestamp.isoformat(),
                        "reason": "auto_simulated_sell",
                    }
                )
                continue

            if action == "buy":
                execution_attempts["buy_signals"] += 1
            else:
                execution_attempts["non_buy_signals"] += 1
                continue

            if action == "buy" and symbol in managed_exit_symbols:
                execution_attempts["buy_managed_exit_skipped"] += 1
                continue
            if signal.target_position <= 0:
                execution_attempts["buy_zero_target"] += 1
                continue

            open_positions = {
                str(item.get("symbol", "")).strip(): item for item in self._portfolio.positions()
            }
            existing = open_positions.get(symbol)
            if isinstance(existing, dict):
                execution_attempts["buy_existing_position"] += 1
                changed = self._portfolio.update_target_position(
                    symbol=symbol,
                    target_position=max(0.0, float(signal.target_position)),
                    timestamp=timestamp,
                    reason="auto_simulated_adjust",
                )
                if changed:
                    adjusted += 1
                    execution_attempts["buy_existing_adjusted"] += 1
                    executions.append(
                        {
                            "trade_id": f"ADJ-{trace_id[:8]}-{symbol}",
                            "symbol": symbol,
                            "side": "buy",
                            "status": "adjusted",
                            "strategy": str(signal.strategy).strip() or "trend",
                            "target_position": round(float(signal.target_position), 4),
                            "price": _as_float(existing.get("entry_price"), default=0.0),
                            "quantity": _as_int(existing.get("quantity"), default=0),
                            "amount": round(
                                _as_float(existing.get("entry_price"), default=0.0)
                                * _as_int(existing.get("quantity"), default=0),
                                2,
                            ),
                            "fee": 0.0,
                            "price_source": "原持仓调整",
                            "trade_time": timestamp.isoformat(),
                            "reason": "auto_simulated_adjust",
                        }
                    )
                else:
                    execution_attempts["buy_existing_unchanged"] += 1
                continue

            execution_attempts["buy_new_candidates"] += 1
            execution_attempts["buy_new_attempted"] += 1
            desired_cash = min(
                initial_cash * max(0.0, float(signal.target_position)),
                available_cash,
            )
            market_payload = _market_payload_for(symbol)
            trade_price, price_source = self._select_simulated_trade_price(
                side="buy",
                market_payload=market_payload,
            )
            lot_size = self._simulation_lot_size()
            if trade_price <= 0 or desired_cash < trade_price * lot_size:
                skipped_no_cash += 1
                status = "rejected_price_unavailable" if trade_price <= 0 else "rejected_no_cash"
                reason = "auto_simulated_buy_price_unavailable" if trade_price <= 0 else "auto_simulated_buy_no_cash"
                _append_rejected_buy_execution(
                    signal=signal,
                    symbol=symbol,
                    status=status,
                    reason=reason,
                    price=trade_price,
                    price_source=price_source,
                )
                continue
            quantity = int(desired_cash // (trade_price * lot_size)) * lot_size
            if quantity <= 0:
                skipped_no_cash += 1
                _append_rejected_buy_execution(
                    signal=signal,
                    symbol=symbol,
                    status="rejected_quantity",
                    reason="auto_simulated_buy_quantity_zero",
                    price=trade_price,
                    price_source=price_source,
                )
                continue
            notional = trade_price * quantity
            fee = self._estimate_simulated_trade_fee(side="buy", notional=notional)
            total_cost = notional + fee
            while quantity > 0 and total_cost > available_cash:
                quantity -= lot_size
                notional = trade_price * quantity
                fee = self._estimate_simulated_trade_fee(side="buy", notional=notional)
                total_cost = notional + fee
            if quantity <= 0:
                skipped_no_cash += 1
                _append_rejected_buy_execution(
                    signal=signal,
                    symbol=symbol,
                    status="rejected_no_cash",
                    reason="auto_simulated_buy_no_cash_after_fee",
                    price=trade_price,
                    price_source=price_source,
                )
                continue

            status = self._portfolio.set_manual_position(
                symbol=symbol,
                strategy=str(signal.strategy).strip() or "trend",
                target_position=max(0.0, float(signal.target_position)),
                timestamp=timestamp,
                trace_id=trace_id,
                reason="auto_simulated_buy",
                manual_fill={
                    "entry_price": round(trade_price, 6),
                    "quantity": quantity,
                    "fee": fee,
                    "account": self._simulation_account_name(),
                    "manual_trade_time": timestamp.isoformat(),
                    "note": f"auto_simulated_buy:{price_source}",
                },
                sector_tag=_infer_symbol_sector(symbol),
            )
            if status == "rejected_max_holdings":
                skipped_max_holdings += 1
                _append_rejected_buy_execution(
                    signal=signal,
                    symbol=symbol,
                    status=status,
                    reason="auto_simulated_buy_max_holdings",
                    price=trade_price,
                    price_source=price_source,
                )
                continue
            if status == "rejected_same_sector":
                skipped_same_sector += 1
                _append_rejected_buy_execution(
                    signal=signal,
                    symbol=symbol,
                    status=status,
                    reason="auto_simulated_buy_same_sector",
                    price=trade_price,
                    price_source=price_source,
                )
                continue
            if status not in {"opened", "adjusted"}:
                _append_rejected_buy_execution(
                    signal=signal,
                    symbol=symbol,
                    status="rejected_execution",
                    reason=status or "auto_simulated_buy_rejected",
                    price=trade_price,
                    price_source=price_source,
                )
                continue
            available_cash = round(available_cash - (notional + fee), 2)
            opened += 1 if status == "opened" else 0
            adjusted += 1 if status == "adjusted" else 0
            execution_attempts["buy_new_filled"] += 1
            if not dry_run:
                self._ensure_symbol_tracked_in_watchlist(
                    symbol=symbol,
                    source="auto_simulated_buy",
                    trace_id=trace_id,
                )
            trade = self._portfolio.trades(limit=1)[0]
            executions.append(
                {
                    "trade_id": str(trade.get("trade_id", "")).strip(),
                    "symbol": symbol,
                    "side": "buy",
                    "status": status,
                    "strategy": str(signal.strategy).strip() or "trend",
                    "target_position": round(float(signal.target_position), 4),
                    "price": round(trade_price, 6),
                    "quantity": quantity,
                    "amount": round(notional, 2),
                    "fee": fee,
                    "price_source": price_source,
                    "trade_time": timestamp.isoformat(),
                    "reason": "auto_simulated_buy",
                }
            )

        equity_snapshot = self._refresh_simulation_equity_snapshot(
            timestamp=timestamp,
            use_live_runtime=use_live_runtime,
            market_cache=market_cache,
            persist_equity=not dry_run,
        )
        open_positions = len(self._portfolio.positions())
        current_equity_value = round(
            _as_float(
                equity_snapshot.get("current_equity"),
                default=self._state.current_equity,
            ),
            6,
        )
        if dry_run and portfolio_state_before is not None:
            open_positions = len(
                portfolio_state_before.get("positions", [])
                if isinstance(portfolio_state_before, dict)
                else []
            )
            current_equity_value = round(current_equity_before, 6)
            self._portfolio.restore_state(portfolio_state_before)
            self._state.watchlist = watchlist_before
            self._state.current_equity = current_equity_before
        return {
            "opened": opened,
            "adjusted": adjusted,
            "trimmed": trimmed,
            "closed_expired": _as_int(base_update.get("closed_expired"), default=0),
            "closed_signals": closed_signals,
            "skipped_max_holdings": skipped_max_holdings,
            "skipped_same_sector": skipped_same_sector,
            "skipped_no_cash": skipped_no_cash,
            "open_positions": open_positions,
            "status": "simulated_auto_dry_run" if dry_run else "simulated_auto_applied",
            "dry_run": bool(dry_run),
            "executions": executions,
            "execution_attempts": execution_attempts,
            "cash_available": round(
                _as_float(equity_snapshot.get("cash_available"), default=available_cash), 2
            ),
            "portfolio_value": round(
                _as_float(equity_snapshot.get("portfolio_value"), default=0.0), 2
            ),
            "net_asset_value": round(
                _as_float(equity_snapshot.get("net_asset_value"), default=0.0), 2
            ),
            "current_equity": current_equity_value,
        }

    def _refresh_simulation_equity_snapshot(
        self,
        *,
        timestamp: datetime,
        use_live_runtime: bool,
        market_cache: dict[str, dict[str, object]] | None = None,
        persist_equity: bool = True,
    ) -> dict[str, object]:
        initial_cash = self._simulation_initial_cash()
        cash_available = self._simulation_cash_available()
        portfolio_value = 0.0
        payload_cache = market_cache if market_cache is not None else {}
        depth_snapshots = self._fetch_market_depth_snapshots(
            symbols=[
                str(item.get("symbol", "")).strip()
                for item in self._portfolio.positions()
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            ],
            scope="watchlist",
            force_refresh=False,
        )
        for item in self._portfolio.positions():
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            quantity = _as_int(item.get("quantity"), default=0)
            if not symbol or quantity <= 0:
                continue
            market_payload = payload_cache.get(symbol)
            if market_payload is None:
                market_payload = self._build_week5_symbol_market_payload(
                    symbol=symbol,
                    prefer_online=use_live_runtime,
                    depth_snapshot=depth_snapshots.get(symbol, {}),
                )
                payload_cache[symbol] = market_payload
            mark_price = _as_float(market_payload.get("last_price"), default=0.0)
            if mark_price <= 0:
                mark_price = _as_float(item.get("entry_price"), default=0.0)
            if mark_price <= 0:
                continue
            portfolio_value += mark_price * quantity
        net_asset_value = cash_available + portfolio_value
        current_equity = net_asset_value / initial_cash if initial_cash > 0 else 1.0
        if persist_equity:
            self._state.current_equity = round(current_equity, 6)
        return {
            "timestamp": timestamp.isoformat(),
            "cash_available": round(cash_available, 2),
            "portfolio_value": round(portfolio_value, 2),
            "net_asset_value": round(net_asset_value, 2),
            "current_equity": round(current_equity, 6),
        }

    def _sync_recommendation_lifecycle_from_auto_execution(
        self,
        *,
        portfolio_update: Mapping[str, object],
        timestamp: datetime,
        trace_id: str,
    ) -> dict[str, object]:
        raw_executions = portfolio_update.get("executions")
        if not isinstance(raw_executions, list):
            return {"updated": 0, "symbols": []}
        updated = 0
        symbols: list[str] = []
        for item in raw_executions:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            strategy = str(item.get("strategy", "trend")).strip() or "trend"
            side = str(item.get("side", "")).strip().lower()
            status = str(item.get("status", "")).strip().lower()
            trade_price = _as_float(item.get("price"), default=0.0)
            if side == "buy" and status in {"opened", "adjusted"}:
                record = self._set_recommendation_status(
                    symbol=symbol,
                    status="holding",
                    strategy=strategy,
                    timestamp=timestamp,
                    source="auto_simulated_buy",
                    trace_id=trace_id,
                    note="auto simulated buy",
                    extra={
                        "entry_triggered_at": str(item.get("trade_time", ""))
                        or timestamp.isoformat(),
                        "entry_price": round(trade_price, 6) if trade_price > 0 else None,
                        "entry_quantity": _as_int(item.get("quantity"), default=0),
                        "entry_trade_id": str(item.get("trade_id", "")).strip(),
                        "last_price": round(trade_price, 6) if trade_price > 0 else None,
                        "outcome_status": "open",
                    },
                )
            elif side == "sell" and status == "closed":
                normalized_symbol = _normalize_a_share_symbol(symbol) or symbol
                existing = self._recommendation_lifecycle.get(normalized_symbol, {})
                entry_price = _as_float(existing.get("entry_price"), default=0.0)
                realized = (
                    round(trade_price / entry_price - 1.0, 6)
                    if trade_price > 0 and entry_price > 0
                    else None
                )
                record = self._set_recommendation_status(
                    symbol=symbol,
                    status="closed",
                    strategy=strategy,
                    timestamp=timestamp,
                    source="auto_simulated_sell",
                    trace_id=trace_id,
                    note="auto simulated sell",
                    extra={
                        "exit_price": round(trade_price, 6) if trade_price > 0 else None,
                        "exit_quantity": _as_int(item.get("quantity"), default=0),
                        "exit_trade_id": str(item.get("trade_id", "")).strip(),
                        "closed_at": str(item.get("trade_time", "")) or timestamp.isoformat(),
                        "closed_reason": str(item.get("reason", "")).strip()
                        or "auto_simulated_sell",
                        "realized_return_pct": realized,
                        "current_return_pct": realized,
                        "outcome_status": "win"
                        if realized is not None and realized > 0
                        else "loss"
                        if realized is not None
                        else "unknown",
                    },
                )
            else:
                record = None
            if record is not None:
                updated += 1
                symbols.append(str(record.get("symbol", "")).strip())
        return {"updated": updated, "symbols": sorted({symbol for symbol in symbols if symbol})}

    def _notify_simulated_trade_updates_if_needed(
        self,
        *,
        portfolio_update: Mapping[str, object],
        trace_id: str,
    ) -> None:
        raw_executions = portfolio_update.get("executions")
        if not isinstance(raw_executions, list):
            return
        pre_trade_block_reasons = {
            "auto_simulated_buy_no_cash",
            "auto_simulated_buy_no_cash_after_fee",
            "auto_simulated_buy_quantity_zero",
        }
        risk_gate_block_reasons = {
            "auto_simulated_buy_max_holdings",
            "auto_simulated_buy_same_sector",
            "max_position_limit_reached",
            "max_holdings_reached",
        }
        pre_trade_blocked_items: list[dict[str, object]] = []
        risk_gate_blocked_items: list[dict[str, object]] = []
        for item in raw_executions:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            trade_id = str(item.get("trade_id", "")).strip()
            side = str(item.get("side", "")).strip().lower()
            status = str(item.get("status", "")).strip().lower()
            reason = str(item.get("reason", "")).strip()
            block_category = str(item.get("block_category", "")).strip().lower()
            price = _as_float(item.get("price"), default=0.0)
            quantity = _as_int(item.get("quantity"), default=0)
            amount = _as_float(item.get("amount"), default=0.0)
            fee = _as_float(item.get("fee"), default=0.0)
            target_position = _as_float(item.get("target_position"), default=0.0)
            price_source = str(item.get("price_source", "")).strip() or "最新价"
            if not symbol or not trade_id:
                continue
            is_rejected = status.startswith("rejected")
            is_pre_trade_blocked = (
                block_category == "pre_trade_blocked" or status == "pre_trade_blocked"
                or (is_rejected and reason in pre_trade_block_reasons)
            )
            is_risk_gate_blocked = (
                block_category == "risk_gate_blocked" or status == "risk_gate_blocked"
                or (is_rejected and reason in risk_gate_block_reasons)
            )
            if side == "buy":
                is_adjusted = status == "adjusted" or reason == "auto_simulated_adjust"
                if is_pre_trade_blocked or is_risk_gate_blocked:
                    event_type = "pre_trade_blocked" if is_pre_trade_blocked else "risk_gate_blocked"
                    blocked_item = {
                        "symbol": symbol,
                        "reason": reason,
                        "quantity": quantity,
                        "target_position": target_position,
                        "status": status,
                        "block_category": block_category or event_type,
                    }
                    self._record_audit_event(
                        event_type=event_type,
                        trace_id=trace_id,
                        message=f"simulated buy blocked: {symbol} reason={reason} quantity={quantity}",
                        payload=blocked_item,
                    )
                    if is_pre_trade_blocked:
                        pre_trade_blocked_items.append(blocked_item)
                    else:
                        risk_gate_blocked_items.append(blocked_item)
                    continue
                if is_rejected:
                    title_summary = f"sim buy rejected {symbol}"
                else:
                    title_summary = f"sim adjust {symbol}" if is_adjusted else f"sim buy {symbol}"
                title = _push_title(priority="P1", category="signal", summary=title_summary)
                if is_rejected:
                    content = _notification_message_zh(
                        trigger=f"系统对标的【{symbol}】发起模拟买入尝试，但本次未成交。",
                        impact=(
                            f"参考价 {price:.2f}，计划数量 {quantity} 股，实际成交金额 {amount:.2f} 元，"
                            f"目标仓位 {target_position:.0%}；模拟持仓与可用资金未发生买入变化。"
                        ),
                        action="这是模拟盘拒单记录，无需券商端下单；如同一标的同一原因重复出现，系统会在当日抑制重复提醒。",
                        details=[
                            f"执行状态：{_sim_trade_status_zh(status)}",
                            f"价格来源：{price_source}",
                            f"手续费：{fee:.2f} 元",
                            f"可用资金：{_as_float(portfolio_update.get('cash_available'), default=0.0):.2f} 元",
                        ],
                    )
                else:
                    trigger_action = "调整模拟仓位" if is_adjusted else "执行模拟买入"
                    content = _notification_message_zh(
                        trigger=f"系统已对标的【{symbol}】{trigger_action}。",
                        impact=(
                            f"成交价 {price:.2f}，数量 {quantity} 股，成交金额 {amount:.2f} 元，"
                            f"目标仓位 {target_position:.0%}。"
                        ),
                        action="这是模拟盘自动成交，无需券商端下单；请在控制大屏核对持仓与盈亏变化。",
                        details=[
                            f"执行状态：{_sim_trade_status_zh(status)}",
                            f"价格来源：{price_source}",
                            f"手续费：{fee:.2f} 元",
                            f"可用资金：{_as_float(portfolio_update.get('cash_available'), default=0.0):.2f} 元",
                        ],
                    )
                level = "info"
            else:
                is_trimmed = status == "trimmed"
                if is_rejected:
                    title_summary = f"sim sell rejected {symbol}"
                elif is_trimmed:
                    title_summary = f"sim trim {symbol}"
                else:
                    title_summary = f"sim sell {symbol}"
                title = _push_title(priority="P0", category="action", summary=title_summary)
                if is_rejected:
                    content = _notification_message_zh(
                        trigger=f"系统对标的【{symbol}】发起模拟卖出尝试，但本次未成交。",
                        impact=(
                            f"参考价 {price:.2f}，计划数量 {quantity} 股，实际成交金额 {amount:.2f} 元；"
                            "模拟持仓未发生卖出变化。"
                        ),
                        action="这是模拟盘拒单记录，无需券商端下单；请在控制大屏复核拒单原因与持仓状态。",
                        details=[
                            f"执行状态：{_sim_trade_status_zh(status)}",
                            f"价格来源：{price_source}",
                            f"手续费：{fee:.2f} 元",
                            f"当前净值：{_as_float(portfolio_update.get('current_equity'), default=self._state.current_equity):.4f}",
                        ],
                    )
                else:
                    trade_action = "模拟减仓" if is_trimmed else "模拟卖出"
                    position_impact = (
                        "该持仓已完成部分减仓，剩余仓位仍保留在模拟盘中。"
                        if is_trimmed
                        else "该持仓已从模拟盘移出。"
                    )
                    followup_action = (
                        "这是模拟盘自动减仓记录，请在控制大屏复核剩余仓位、止盈阶段与 trailing stop 计划。"
                        if is_trimmed
                        else "这是模拟盘自动卖出记录，请在控制大屏复核该票的退出原因与后续观察状态。"
                    )
                    content = _notification_message_zh(
                        trigger=f"系统已对标的【{symbol}】执行{trade_action}。",
                        impact=(
                            f"成交价 {price:.2f}，数量 {quantity} 股，成交金额 {amount:.2f} 元，"
                            f"{position_impact}"
                        ),
                        action=followup_action,
                        details=[
                            f"执行状态：{_sim_trade_status_zh(status)}",
                            f"处理原因：{_translate_signal_reason_zh(reason)}",
                            f"价格来源：{price_source}",
                            f"手续费：{fee:.2f} 元",
                            f"当前净值：{_as_float(portfolio_update.get('current_equity'), default=self._state.current_equity):.4f}",
                        ],
                    )
                level = "warn"
            if is_rejected:
                trade_day = _sim_trade_notification_day(str(item.get("trade_time", "")).strip())
                dedup_key = f"notify:sim-trade-rejected:{trade_day}:{side}:{symbol}:{status}:{reason or '-'}"
                dedup_value = f"{trade_day}:{side}:{symbol}:{status}:{reason or '-'}"
            else:
                dedup_key = f"notify:sim-trade:{trade_id}"
                dedup_value = trade_id
            self._notify_if_changed(
                dedup_key=dedup_key,
                dedup_value=dedup_value,
                title=title,
                content=content,
                level=level,
                trace_id=trace_id,
                ttl_sec=30 * 3600,
            )
        blocked_groups = [
            (
                "pre-trade-blocked-summary",
                pre_trade_blocked_items,
                "sim pre trade blocked summary",
            ),
            (
                "risk-gate-blocked-summary",
                risk_gate_blocked_items,
                "sim risk gate blocked summary",
            ),
        ]
        for key_slug, blocked_items, title_summary in blocked_groups:
            if not blocked_items:
                continue
            symbols = sorted(
                {
                    str(entry.get("symbol", "")).strip()
                    for entry in blocked_items
                    if str(entry.get("symbol", "")).strip()
                }
            )
            reasons = sorted(
                {
                    str(entry.get("reason", "")).strip()
                    for entry in blocked_items
                    if str(entry.get("reason", "")).strip()
                }
            )
            first_trade_time = ""
            for raw_item in raw_executions:
                if isinstance(raw_item, dict):
                    first_trade_time = str(raw_item.get("trade_time", "")).strip()
                    if first_trade_time:
                        break
            trade_day = _sim_trade_notification_day(first_trade_time)
            title = _push_title(priority="P2", category="summary", summary=title_summary)
            content = _notification_message_zh(
                trigger=f"Simulated buy blocked for {len(symbols)} symbols.",
                impact=(
                    f"Symbols: {', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''}; "
                    f"reasons: {', '.join(reasons) or '-'}."
                ),
                action="No broker-side action is required; review cash, lot size, and portfolio gates before reenabling auto notifications.",
                details=[
                    f"blocked_symbols={len(symbols)}",
                    f"blocked_events={len(blocked_items)}",
                    f"reasons={', '.join(reasons) or '-'}",
                ],
            )
            dedup_value = f"{trade_day}:{len(blocked_items)}:{'|'.join(symbols)}:{'|'.join(reasons)}"
            self._notify_if_changed(
                dedup_key=f"notify:{key_slug}:{trade_day}",
                dedup_value=dedup_value,
                title=title,
                content=content,
                level="info",
                trace_id=trace_id,
                ttl_sec=24 * 3600,
            )

    def _notify_holding_alerts_if_needed(
        self,
        *,
        holding_alerts: dict[str, object],
        trace_id: str,
    ) -> None:
        raw_items = holding_alerts.get("items")
        if not isinstance(raw_items, list):
            return
        today = datetime.now().strftime("%Y%m%d")
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "")).strip().lower()
            reason = str(item.get("reason", "")).strip() or "holding_alert"
            actionable_take_profit = reason in {
                "take_profit_threshold_reached",
                "take_profit_stage_1_reached",
                "take_profit_stage_2_reached",
                "trailing_stop_remainder_exit",
            }
            if severity != "warn" and not actionable_take_profit:
                continue
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            dedup_key = f"holding-alert:{today}:{symbol}:{reason}"
            if self._cache.exists(dedup_key):
                continue
            self._cache.set(dedup_key, "1", ttl_sec=18 * 3600)
            pnl_pct = _as_float(item.get("pnl_pct"), default=0.0) * 100.0
            hold_days = _as_int(item.get("hold_days"), default=0)
            max_hold_days = max(1, _as_int(item.get("max_hold_days"), default=0))
            latest_price = _as_float(item.get("latest_price"), default=0.0)
            entry_price = _as_float(item.get("entry_price"), default=0.0)
            take_profit_stage = max(0, _as_int(item.get("take_profit_stage"), default=0))
            next_take_profit_stage = max(0, _as_int(item.get("next_take_profit_stage"), default=0))
            peak_pnl_pct = _as_float(item.get("peak_pnl_pct"), default=0.0) * 100.0
            drawdown_from_peak = _as_float(item.get("drawdown_from_peak"), default=0.0) * 100.0
            if reason == "stop_loss_threshold_reached":
                self.notify(
                    title=_push_title(
                        priority="P0", category="action", summary=f"sell instruction {symbol}"
                    ),
                    content=_notification_message_zh(
                        trigger=f"持有标的【{symbol}】的浮动盈亏已下探至 {pnl_pct:.2f}%，触发止损阈值。",
                        impact="继续持有将超出系统允许的单笔风险承受范围，自动保护逻辑已生效。",
                        action="请立刻在券商端执行减仓或清仓；处理完成后，请在本地监控雷达同步持仓状态。",
                        details=[
                            "处理类型：止损卖出",
                            f"持仓天数：{hold_days}",
                            f"建仓成本：{entry_price:.2f}",
                            f"最新价格：{latest_price:.2f}",
                        ],
                    ),
                    level="warn",
                    trace_id=trace_id,
                )
                continue
            if reason == "take_profit_threshold_reached":
                self.notify(
                    title=_push_title(
                        priority="P0",
                        category="action",
                        summary=f"take profit instruction {symbol}",
                    ),
                    content=_notification_message_zh(
                        trigger=f"持有标的【{symbol}】的浮动收益已达到 {pnl_pct:.2f}%，触发首档止盈阈值。",
                        impact="系统判断该标的已进入兑现区间，应优先保护既有利润，避免回吐。",
                        action="请按止盈计划分批减仓或直接兑现，并在成交后核对券商侧实际结果。",
                        details=[
                            "处理类型：止盈卖出",
                            f"持仓天数：{hold_days}",
                            f"建仓成本：{entry_price:.2f}",
                            f"最新价格：{latest_price:.2f}",
                        ],
                    ),
                    level="warn",
                    trace_id=trace_id,
                )
                continue
            if reason == "take_profit_stage_1_reached":
                self.notify(
                    title=_push_title(
                        priority="P0",
                        category="action",
                        summary=f"take profit instruction {symbol}",
                    ),
                    content=_notification_message_zh(
                        trigger=f"持有标的【{symbol}】的浮动收益已达到 {pnl_pct:.2f}%，触发第一档止盈计划。",
                        impact="系统判断该标的已进入兑现区间，应优先保护既有利润，避免回吐。",
                        action="请先执行第一档减仓，并在成交后核对券商侧实际结果；剩余仓位继续观察第二档与 trailing stop。",
                        details=[
                            "处理类型：第一档止盈",
                            f"持仓天数：{hold_days}",
                            f"建仓成本：{entry_price:.2f}",
                            f"最新价格：{latest_price:.2f}",
                            f"当前止盈阶段：{take_profit_stage} -> {next_take_profit_stage}",
                        ],
                    ),
                    level="warn",
                    trace_id=trace_id,
                )
                continue
            if reason == "take_profit_stage_2_reached":
                self.notify(
                    title=_push_title(
                        priority="P0",
                        category="action",
                        summary=f"take profit stage2 {symbol}",
                    ),
                    content=_notification_message_zh(
                        trigger=f"持有标的【{symbol}】的浮动收益已达到 {pnl_pct:.2f}%，触发第二档止盈计划。",
                        impact="系统判断该标的已进入更强兑现区间，建议继续锁定利润，将剩余仓位切换为 trailing stop 管理。",
                        action="请执行第二档减仓，保留的小仓位转入 trailing stop 跟踪。",
                        details=[
                            "处理类型：第二档止盈",
                            f"持仓天数：{hold_days}",
                            f"建仓成本：{entry_price:.2f}",
                            f"最新价格：{latest_price:.2f}",
                            f"当前止盈阶段：{take_profit_stage} -> {next_take_profit_stage}",
                        ],
                    ),
                    level="warn",
                    trace_id=trace_id,
                )
                continue
            if reason == "trailing_stop_remainder_exit":
                self.notify(
                    title=_push_title(
                        priority="P0",
                        category="action",
                        summary=f"trailing stop {symbol}",
                    ),
                    content=_notification_message_zh(
                        trigger=(
                            f"持有标的【{symbol}】自阶段高点已回撤 {abs(drawdown_from_peak):.2f}%，"
                            "触发剩余仓位 trailing stop。"
                        ),
                        impact="剩余利润保护逻辑已触发，若继续持有，回吐风险会明显升高。",
                        action="请执行剩余仓位退出，并将该票转回观察状态。",
                        details=[
                            "处理类型：Trailing Stop 退出",
                            f"峰值收益：{peak_pnl_pct:.2f}%",
                            f"当前收益：{pnl_pct:.2f}%",
                            f"建仓成本：{entry_price:.2f}",
                            f"最新价格：{latest_price:.2f}",
                        ],
                    ),
                    level="warn",
                    trace_id=trace_id,
                )
                continue
            if reason == "max_hold_days_near_limit":
                remaining_days = max(0, max_hold_days - hold_days)
                self.notify(
                    title=_push_title(
                        priority="P2",
                        category="risk",
                        summary=f"holding countdown {symbol}",
                    ),
                    content=_notification_message_zh(
                        trigger=(
                            f"持有标的【{symbol}】已来到第 {hold_days} 天，"
                            f"接近策略允许的最长持仓上限 {max_hold_days} 天。"
                        ),
                        impact="继续持有仍然可行，但已进入到期前的准备窗口，后续触发强制处理的概率升高。",
                        action="请提前制定退出计划，不建议无计划继续延长持仓；必要时在本地监控雷达标记处理优先级。",
                        details=[
                            f"持仓天数：{hold_days}/{max_hold_days}",
                            f"剩余天数：{remaining_days}",
                            f"当前盈亏：{pnl_pct:.2f}%",
                            f"建仓成本：{entry_price:.2f}",
                            f"最新价格：{latest_price:.2f}",
                        ],
                    ),
                    level="warn",
                    trace_id=trace_id,
                )
                continue
            if severity != "warn":
                continue
            self.notify(
                title=_push_title(
                    priority="P2", category="risk", summary=f"holding alert {symbol}"
                ),
                content=_notification_message_zh(
                    trigger=f"持有标的【{symbol}】出现新的持仓风险提示。",
                    impact="该标的暂未达到强制处理阈值，但已经进入需要人工关注的状态。",
                    action="请结合盘面变化和仓位计划复核该持仓，必要时准备减仓或止盈止损。",
                    details=[
                        f"风险原因：{_translate_signal_reason_zh(reason)}",
                        f"当前盈亏：{pnl_pct:.2f}%",
                        f"持仓天数：{hold_days}",
                    ],
                ),
                level="warn",
                trace_id=trace_id,
            )

    def _notify_expired_position_exits_if_needed(
        self,
        *,
        timestamp: datetime,
        closed_expired: int,
        trace_id: str,
    ) -> None:
        if closed_expired <= 0:
            return
        recent_trades = self._portfolio.trades(limit=max(5, closed_expired * 4))
        timestamp_iso = timestamp.isoformat()
        matched: list[dict[str, object]] = []
        for item in reversed(recent_trades):
            if not isinstance(item, dict):
                continue
            if str(item.get("side", "")).strip().lower() != "sell":
                continue
            if str(item.get("reason", "")).strip() != "max_hold_days_exit":
                continue
            if str(item.get("timestamp", "")).strip() != timestamp_iso:
                continue
            matched.append(item)
            if len(matched) >= closed_expired:
                break

        max_hold_days = max(1, _as_int(self._config.soup_strategy.max_hold_days, default=5))
        for item in reversed(matched):
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            dedup_key = f"expired-exit:{timestamp_iso}:{symbol}"
            if self._cache.exists(dedup_key):
                continue
            self._cache.set(dedup_key, "1", ttl_sec=18 * 3600)
            self.notify(
                title=_push_title(
                    priority="P0", category="action", summary=f"sell instruction {symbol}"
                ),
                content=_notification_message_zh(
                    trigger=f"持有标的【{symbol}】已达到最长持仓上限 {max_hold_days} 天。",
                    impact="系统模拟盘已按规则记为到期卖出，如券商侧未同步处理，将产生账实偏差。",
                    action="请立即核对券商端是否已完成卖出；若尚未处理，请尽快执行并回到本地监控雷达对账。",
                    details=[
                        "处理类型：到期卖出",
                        f"触发时间：{_format_notification_time_zh(timestamp_iso)}",
                    ],
                ),
                level="warn",
                trace_id=trace_id,
            )

    def dashboard_portfolio(self, days: int = 7, trade_limit: int = 120) -> dict[str, object]:
        return self._dashboard_service.dashboard_portfolio(days=days, trade_limit=trade_limit)

    def training_overview(self, history_limit: int = 6) -> dict[str, object]:
        return self._dashboard_service.training_overview(history_limit=history_limit)

    def _latest_evolution_modules(self) -> dict[str, object]:
        return self._dashboard_service._latest_evolution_modules()

    def _build_dashboard_evolution_m8_latest(self) -> dict[str, object] | None:
        return self._dashboard_service._build_dashboard_evolution_m8_latest()

    def _load_dashboard_m8_items(self, artifact_uri: str) -> list[dict[str, object]]:
        return self._dashboard_service._load_dashboard_m8_items(artifact_uri=artifact_uri)

    def _build_dashboard_evolution_m10_latest(self) -> dict[str, object] | None:
        return self._dashboard_service._build_dashboard_evolution_m10_latest()

    def _build_dashboard_evolution_m11_latest(self) -> dict[str, object] | None:
        return self._dashboard_service._build_dashboard_evolution_m11_latest()

    def run_stress_tests(self) -> dict[str, object]:
        return run_default_stress_suite(config=self._config)

    def run_week4_acceptance(
        self,
        sla_recent_runs: int = 50,
        timestamp: datetime | None = None,
        export_enabled: bool | None = None,
        notify_enabled: bool | None = None,
    ) -> dict[str, object]:
        return self._acceptance_service.run_week4_acceptance(
            sla_recent_runs=sla_recent_runs,
            timestamp=timestamp,
            export_enabled=export_enabled,
            notify_enabled=notify_enabled,
        )

    def _docker_assets_acceptance_status(self) -> tuple[bool, str]:
        return self._acceptance_service._docker_assets_acceptance_status()

    def latest_week4_acceptance_report(self) -> dict[str, object] | None:
        return self._acceptance_service.latest_week4_acceptance_report()

    def week4_acceptance_history(self, limit: int = 20) -> dict[str, object]:
        return self._acceptance_service.week4_acceptance_history(limit=limit)

    def run_week7_sim_broker_weekly(
        self,
        days: int = 7,
        timestamp: datetime | None = None,
        export_enabled: bool | None = None,
        notify_enabled: bool | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._week7_sim_broker_service.run_week7_sim_broker_weekly(
            days=days,
            timestamp=timestamp,
            export_enabled=export_enabled,
            notify_enabled=notify_enabled,
            source_trace_id=source_trace_id,
        )


    def latest_week7_sim_broker_report(self) -> dict[str, object] | None:
        return self._week7_sim_broker_service.latest_week7_sim_broker_report()


    def week7_sim_broker_history(self, limit: int = 20) -> dict[str, object]:
        return self._week7_sim_broker_service.week7_sim_broker_history(limit=limit)


    def _build_week7_sim_broker_drilldown(
        self,
        manual_trade_ratio: float,
    ) -> dict[str, object]:
        return self._week7_sim_broker_service._build_week7_sim_broker_drilldown(
            manual_trade_ratio=manual_trade_ratio,
        )


    def _build_week7_sim_broker_trend_preview(
        self,
        report: dict[str, object],
        limit: int = 12,
    ) -> dict[str, object]:
        return self._week7_sim_broker_service._build_week7_sim_broker_trend_preview(
            report=report,
            limit=limit,
        )


    def _latest_week5_no_buy_streak(self) -> int:
        latest_week5 = self._last_week5_scan_report
        if not isinstance(latest_week5, dict):
            return 0
        empty_signal = latest_week5.get("empty_signal")
        if not isinstance(empty_signal, dict):
            return 0
        return max(0, _as_int(empty_signal.get("no_buy_streak"), default=0))

    def _latest_runtime_drawdown_pct(self) -> float:
        latest_report = self.latest_report()
        if not isinstance(latest_report, dict):
            return 0.0
        risk = latest_report.get("risk")
        if not isinstance(risk, dict):
            return 0.0
        return max(0.0, _as_float(risk.get("drawdown_pct"), default=0.0))

    def _resolve_week5_offhours_scan_profile(
        self,
        *,
        now: datetime,
    ) -> dict[str, object]:
        return self._week5_service._resolve_week5_offhours_scan_profile(now=now)

    def run_week5_offhours_refresh(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool = True,
        sync_reason: str = "offhours_refresh",
        sync_top_k_override: int | None = None,
    ) -> dict[str, object]:
        return self._week5_service.run_week5_offhours_refresh(
            timestamp=timestamp,
            notify_enabled=notify_enabled,
            sync_watchlist=sync_watchlist,
            sync_reason=sync_reason,
            sync_top_k_override=sync_top_k_override,
        )

    def run_week5_market_radar(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
    ) -> dict[str, object]:
        return self._week5_service.run_week5_market_radar(
            timestamp=timestamp,
            notify_enabled=notify_enabled,
        )

    def queue_week5_research_symbols(
        self,
        *,
        symbols: list[str],
        source: str = "manual_research",
        timestamp: datetime | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self._week5_service.queue_week5_research_symbols(
            symbols=symbols,
            source=source,
            timestamp=timestamp,
            metadata=metadata,
        )

    def run_week5_scan(
        self,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool | None = None,
        sync_reason: str = "",
        sync_top_k_override: int | None = None,
        force_universe_scan: bool = False,
        prefilter_enabled_override: bool | None = None,
        prefilter_top_k_override: int | None = None,
        universe_max_symbols_override: int | None = None,
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
    ) -> dict[str, object]:
        return self._week5_service.run_week5_scan(
            symbols=symbols,
            timestamp=timestamp,
            notify_enabled=notify_enabled,
            sync_watchlist=sync_watchlist,
            sync_reason=sync_reason,
            sync_top_k_override=sync_top_k_override,
            force_universe_scan=force_universe_scan,
            prefilter_enabled_override=prefilter_enabled_override,
            prefilter_top_k_override=prefilter_top_k_override,
            universe_max_symbols_override=universe_max_symbols_override,
            pinned_symbols=pinned_symbols,
            scan_profile=scan_profile,
        )

    def _build_week5_scan_notification_content(
        self,
        *,
        symbol_list: list[str],
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
        watchlist_sync: dict[str, object],
        runtime_mode: str,
    ) -> str:
        return self._week5_service._build_week5_scan_notification_content(
            symbol_list=symbol_list,
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
            watchlist_sync=watchlist_sync,
            runtime_mode=runtime_mode,
        )

    def _week5_scan_action_hint(
        self,
        *,
        leaders: list[dict[str, object]],
        first_board_candidates: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        return self._week5_service._week5_scan_action_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )

    def _week5_scan_conclusion_hint(
        self,
        *,
        leaders: list[dict[str, object]],
        first_board_candidates: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        return self._week5_service._week5_scan_conclusion_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )

    def _week5_symbols_by_action(
        self,
        *,
        rows: list[dict[str, object]],
        action: str,
    ) -> list[str]:
        return self._week5_service._week5_symbols_by_action(
            rows=rows,
            action=action,
        )

    def _week5_scan_notification_signature(
        self,
        *,
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        return self._week5_service._week5_scan_notification_signature(
            first_board_candidates=first_board_candidates,
            leaders=leaders,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )

    def _enrich_reconcile_report_with_quantity_account(
        self,
        *,
        report: dict[str, object],
        strategy_snapshot: list[dict[str, object]],
        broker_snapshot: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        return self._reconcile_service._enrich_reconcile_report_with_quantity_account(
            report=report,
            strategy_snapshot=strategy_snapshot,
            broker_snapshot=broker_snapshot,
        )


    def latest_week5_scan_report(self) -> dict[str, object] | None:
        return self._week5_service.latest_week5_scan_report()

    def week5_scan_history(self, limit: int = 20) -> dict[str, object]:
        return self._week5_service.week5_scan_history(limit=limit)

    def week5_signal_pool_live(
        self, limit: int = 30, force_refresh: bool = False
    ) -> dict[str, object]:
        return self._week5_service.week5_signal_pool_live(
            limit=limit,
            force_refresh=force_refresh,
        )

    def week5_signal_pool_symbol_live(
        self,
        *,
        symbol: str,
        force_refresh: bool = False,
    ) -> dict[str, object]:
        return self._week5_service.week5_signal_pool_symbol_live(
            symbol=symbol,
            force_refresh=force_refresh,
        )

    def _build_week5_signal_pool_live_item(
        self,
        *,
        symbol: str,
        candidate: dict[str, object],
        force_refresh: bool,
        prefer_online: bool,
        depth_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._week5_service._build_week5_signal_pool_live_item(
            symbol=symbol,
            candidate=candidate,
            force_refresh=force_refresh,
            prefer_online=prefer_online,
            depth_snapshot=depth_snapshot,
        )

    def _build_week5_signal_pool_fallback_item(
        self,
        *,
        candidate: dict[str, object],
    ) -> dict[str, object]:
        return self._week5_service._build_week5_signal_pool_fallback_item(
            candidate=candidate,
        )

    def _build_week5_symbol_market_payload(
        self,
        *,
        symbol: str,
        prefer_online: bool,
        depth_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._week5_service._build_week5_symbol_market_payload(
            symbol=symbol,
            prefer_online=prefer_online,
            depth_snapshot=depth_snapshot,
        )

    def _load_week5_intraday_frame(
        self,
        *,
        symbol: str,
        prefer_online: bool,
    ) -> tuple[pd.DataFrame, str, str]:
        return self._week5_service._load_week5_intraday_frame(
            symbol=symbol,
            prefer_online=prefer_online,
        )

    def _derive_watchlist_candidates_from_week5(
        self,
        report: dict[str, object],
        top_k_override: int | None = None,
    ) -> list[str]:
        return self._week5_service._derive_watchlist_candidates_from_week5(
            report=report,
            top_k_override=top_k_override,
        )

    def _auto_sync_watchlist_from_week5_report(
        self,
        report: dict[str, object],
        reason: str,
        top_k_override: int | None = None,
        allow_signal_pool_fallback: bool = True,
    ) -> dict[str, object]:
        return self._week5_service._auto_sync_watchlist_from_week5_report(
            report=report,
            reason=reason,
            top_k_override=top_k_override,
            allow_signal_pool_fallback=allow_signal_pool_fallback,
        )

    def _run_multi_strategy_pipeline(
        self,
        symbols: list[str],
        current_equity: float,
        use_live_runtime: bool = False,
    ) -> dict[str, object]:
        pipeline = self._select_pipeline(use_live_runtime=use_live_runtime)
        trend_report = pipeline.run_once(
            symbols=symbols,
            strategy="trend",
            current_equity=current_equity,
        )
        controls = self._build_week6_execution_controls(
            strategy="multi",
            symbols=symbols,
            drawdown_pct=trend_report.risk.drawdown_pct,
        )
        raw_weights = controls.get("allocation_weights")
        allocation_weights: dict[str, float] = {}
        if isinstance(raw_weights, dict):
            for key, value in raw_weights.items():
                strategy = str(key).strip().lower()
                weight = _as_float(value, default=0.0)
                if strategy and weight > 0:
                    allocation_weights[strategy] = weight
        if not allocation_weights:
            allocation_weights = {"trend": 1.0}
        if "trend" not in allocation_weights:
            allocation_weights["trend"] = 0.0

        report_map: dict[str, list[PipelineSignal]] = {
            "trend": trend_report.signals,
        }
        for strategy in allocation_weights:
            if strategy == "trend":
                continue
            report = pipeline.run_once(
                symbols=symbols,
                strategy=strategy,
                current_equity=current_equity,
            )
            report_map[strategy] = report.signals

        merged_signals = self._merge_multi_strategy_signals(
            symbols=symbols,
            report_map=report_map,
            allocation_weights=allocation_weights,
        )
        source_runs = {strategy: len(signal_items) for strategy, signal_items in report_map.items()}
        return {
            "trace_id": trend_report.trace_id,
            "timestamp": trend_report.timestamp,
            "degraded_mode": trend_report.degraded_mode,
            "risk_payload": asdict(trend_report.risk),
            "drawdown_pct": trend_report.risk.drawdown_pct,
            "signals": merged_signals,
            "summary": {
                "source_runs": source_runs,
                "allocation_weights": allocation_weights,
                "merged_signals": len(merged_signals),
            },
        }

    def _merge_multi_strategy_signals(
        self,
        symbols: list[str],
        report_map: dict[str, list[PipelineSignal]],
        allocation_weights: dict[str, float],
    ) -> list[PipelineSignal]:
        symbol_map: dict[str, dict[str, PipelineSignal]] = {}
        for strategy, signal_items in report_map.items():
            for strategy_signal in signal_items:
                by_strategy = symbol_map.get(strategy_signal.symbol)
                if by_strategy is None:
                    by_strategy = {}
                    symbol_map[strategy_signal.symbol] = by_strategy
                by_strategy[strategy] = strategy_signal

        thresholds = self._strategy_thresholds(strategy="trend")
        merged: list[PipelineSignal] = []
        for symbol in symbols:
            by_strategy = symbol_map.get(symbol, {})
            weighted_score = 0.0
            weighted_probs = {"lgbm": 0.0, "xgb": 0.0, "meta": 0.0}
            buy_position = 0.0
            watch_seen = False
            reasons: list[str] = []

            for strategy, weight in allocation_weights.items():
                selected_signal = by_strategy.get(strategy)
                if selected_signal is None:
                    continue
                weighted_score += selected_signal.score * weight
                for prob_name in weighted_probs:
                    weighted_probs[prob_name] += (
                        selected_signal.probabilities.get(prob_name, 0.0) * weight
                    )
                if selected_signal.action == "buy":
                    buy_position += selected_signal.target_position * weight
                elif selected_signal.action == "watch":
                    watch_seen = True
                reasons.append(f"{strategy}:{selected_signal.action}:{selected_signal.score:.2f}")
                for reason in selected_signal.reasons[:2]:
                    composed = f"{strategy}:{reason}"
                    if composed not in reasons:
                        reasons.append(composed)

            action: SignalAction = "hold"
            target_position = 0.0
            if buy_position > 0:
                action = "buy"
                target_position = _clamp(buy_position, 0.0, 1.0)
            elif watch_seen:
                action = "watch"

            grade = _grade_by_threshold(score=weighted_score, thresholds=thresholds)
            merged.append(
                PipelineSignal(
                    symbol=symbol,
                    strategy="multi",
                    score=round(weighted_score, 2),
                    grade=grade,
                    action=action,
                    target_position=round(target_position, 4),
                    probabilities={
                        key: round(_clamp(value, 0.0, 1.0), 4)
                        for key, value in weighted_probs.items()
                    },
                    reasons=reasons[:12],
                )
            )
        return merged

    def _build_week6_execution_controls(
        self,
        strategy: str,
        symbols: list[str],
        drawdown_pct: float,
    ) -> dict[str, object]:
        return self._week6_service._build_week6_execution_controls(
            strategy=strategy,
            symbols=symbols,
            drawdown_pct=drawdown_pct,
        )

    def _apply_week6_execution_controls(
        self,
        signals: list[PipelineSignal],
        strategy: str,
        controls: dict[str, object],
    ) -> dict[str, object]:
        return self._week6_service._apply_week6_execution_controls(
            signals=signals,
            strategy=strategy,
            controls=controls,
        )

    def _strategy_thresholds(self, strategy: str) -> dict[str, float]:
        return self._week6_service._strategy_thresholds(strategy)

    def update_global_market_snapshot(
        self,
        snapshot: dict[str, object],
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._week6_service.update_global_market_snapshot(
            snapshot=snapshot,
            source_trace_id=source_trace_id,
        )

    def global_market_snapshot(self) -> dict[str, object]:
        return self._week6_service.global_market_snapshot()

    def global_market_history(self, limit: int = 50) -> dict[str, object]:
        return self._week6_service.global_market_history(limit=limit)

    def run_week6_data_prewarm(
        self,
        symbols: list[str] | None = None,
        lookback_days: int | None = None,
        notify_enabled: bool | None = None,
        source_trace_id: str = "",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        return self._week6_service.run_week6_data_prewarm(
            symbols=symbols,
            lookback_days=lookback_days,
            notify_enabled=notify_enabled,
            source_trace_id=source_trace_id,
            timestamp=timestamp,
        )

    def latest_week6_data_quality_report(self) -> dict[str, object] | None:
        return self._week6_service.latest_week6_data_quality_report()

    def week6_data_quality_history(self, limit: int = 20) -> dict[str, object]:
        return self._week6_service.week6_data_quality_history(limit=limit)

    def _store_week6_data_quality_report(self, report: dict[str, object]) -> None:
        self._week6_service._store_week6_data_quality_report(report)

    def set_regulatory_watchlist(
        self,
        entries: list[dict[str, object]],
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._week6_service.set_regulatory_watchlist(
            entries=entries,
            source_trace_id=source_trace_id,
        )

    def regulatory_watchlist(self) -> dict[str, object]:
        return self._week6_service.regulatory_watchlist()

    def run_week6_analysis(
        self,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
    ) -> dict[str, object]:
        return self._week6_service.run_week6_analysis(
            symbols=symbols,
            timestamp=timestamp,
            notify_enabled=notify_enabled,
        )

    def latest_week6_report(self) -> dict[str, object] | None:
        return self._week6_service.latest_week6_report()

    def week6_history(self, limit: int = 20) -> dict[str, object]:
        return self._week6_service.week6_history(limit=limit)

    def record_strategy_performance(
        self,
        month: str,
        strategy: str,
        strategy_return: float,
        benchmark_return: float,
        note: str = "",
        source_trace_id: str = "",
    ) -> dict[str, object]:
        normalized_strategy = strategy.strip().lower()
        normalized_month = _normalize_year_month(month)
        if not normalized_strategy:
            return {
                "accepted": False,
                "code": "invalid_strategy",
                "message": "strategy is required",
            }
        if not normalized_month:
            return {
                "accepted": False,
                "code": "invalid_month",
                "message": "month must be YYYY-MM",
            }

        timestamp = datetime.now().isoformat()
        alpha = strategy_return - benchmark_return
        record = {
            "month": normalized_month,
            "strategy": normalized_strategy,
            "strategy_return": round(strategy_return, 6),
            "benchmark_return": round(benchmark_return, 6),
            "alpha": round(alpha, 6),
            "underperform": alpha < 0.0,
            "note": note.strip(),
            "updated_at": timestamp,
        }
        replaced = False
        for idx, item in enumerate(self._strategy_performance_history):
            if (
                str(item.get("strategy", "")).strip().lower() == normalized_strategy
                and str(item.get("month", "")).strip() == normalized_month
            ):
                self._strategy_performance_history[idx] = record
                replaced = True
                break
        if not replaced:
            self._strategy_performance_history.append(record)

        self._strategy_performance_history.sort(
            key=lambda item: (
                str(item.get("month", "")),
                str(item.get("strategy", "")),
            )
        )
        history_limit = max(1, self._config.strategy_kill_switch.history_limit)
        if len(self._strategy_performance_history) > history_limit:
            overflow = len(self._strategy_performance_history) - history_limit
            if overflow > 0:
                self._strategy_performance_history = self._strategy_performance_history[overflow:]

        state = self._evaluate_strategy_kill_switch(
            strategy=normalized_strategy,
            source_trace_id=source_trace_id,
        )
        self._record_audit_event(
            event_type="week7_strategy_performance",
            trace_id=source_trace_id,
            level="warn" if bool(state.get("triggered", False)) else "info",
            payload={
                "record": record,
                "triggered": bool(state.get("triggered", False)),
                "consecutive_underperform": _as_int(
                    state.get("consecutive_underperform"),
                    default=0,
                ),
            },
        )
        return {
            "accepted": True,
            "record": record,
            "state": state,
            "records": len(self._strategy_performance_history),
            "replaced": replaced,
        }

    def strategy_kill_switch_history(
        self,
        strategy: str = "",
        limit: int = 60,
    ) -> dict[str, object]:
        normalized = strategy.strip().lower()
        capped_limit = max(1, min(limit, max(1, self._config.strategy_kill_switch.history_limit)))
        records = self._strategy_performance_history
        if normalized:
            records = [
                item
                for item in records
                if str(item.get("strategy", "")).strip().lower() == normalized
            ]
        recent = records[-capped_limit:]
        return {
            "strategy": normalized,
            "records": len(recent),
            "history": recent,
        }

    def strategy_kill_switch_status(self, strategy: str = "") -> dict[str, object]:
        normalized = strategy.strip().lower()
        if normalized:
            state = self._strategy_kill_switch_state.get(normalized)
            if state is None:
                state = self._evaluate_strategy_kill_switch(strategy=normalized)
            return {
                "enabled": self._config.strategy_kill_switch.enabled,
                "strategy": normalized,
                "state": state,
            }

        states: dict[str, dict[str, object]] = {}
        strategies = sorted(
            {
                str(item.get("strategy", "")).strip().lower()
                for item in self._strategy_performance_history
                if str(item.get("strategy", "")).strip()
            }
        )
        for name in strategies:
            current = self._strategy_kill_switch_state.get(name)
            if current is None:
                current = self._evaluate_strategy_kill_switch(strategy=name)
            states[name] = current
        triggered = [name for name, item in states.items() if bool(item.get("triggered", False))]
        return {
            "enabled": self._config.strategy_kill_switch.enabled,
            "strategies": states,
            "triggered_strategies": triggered,
            "pause_new_buy": self._state.pause_new_buy,
        }

    def reset_strategy_kill_switch(
        self,
        strategy: str = "",
        resume_new_buy: bool = False,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        normalized = strategy.strip().lower()
        if normalized:
            removed = 1 if normalized in self._strategy_kill_switch_state else 0
            self._strategy_kill_switch_state.pop(normalized, None)
            before = len(self._strategy_performance_history)
            self._strategy_performance_history = [
                item
                for item in self._strategy_performance_history
                if str(item.get("strategy", "")).strip().lower() != normalized
            ]
            removed_history = before - len(self._strategy_performance_history)
        else:
            removed = len(self._strategy_kill_switch_state)
            self._strategy_kill_switch_state = {}
            removed_history = len(self._strategy_performance_history)
            self._strategy_performance_history = []

        if resume_new_buy and not self._active_kill_switch_strategies():
            self._state.pause_new_buy = False
            self._persist_runtime_state_to_disk()

        payload = {
            "accepted": True,
            "strategy": normalized,
            "removed": removed,
            "removed_history": removed_history,
            "resume_new_buy": resume_new_buy,
            "pause_new_buy": self._state.pause_new_buy,
        }
        self._record_audit_event(
            event_type="week7_strategy_kill_switch_reset",
            trace_id=source_trace_id,
            payload=payload,
        )
        if removed > 0:
            strategy_text = _strategy_label_zh(normalized) if normalized else "全部策略"
            self.notify(
                title=_push_title(
                    priority="P2", category="risk", summary="strategy kill switch reset"
                ),
                content=_notification_message_zh(
                    trigger=f"{strategy_text} 的策略自毁保护已被人工解除。",
                    impact="相关策略不再处于自动停用状态；若同时恢复了新开仓开关，系统会重新允许买入评估。",
                    action="请继续观察一段时间的策略表现与风险状态，再决定是否正式恢复该策略参与实盘决策。",
                    details=[
                        f"解除策略：{strategy_text}",
                        f"清理状态条目：{removed}",
                        f"清理历史记录：{removed_history}",
                        f"恢复新开仓：{_bool_zh(resume_new_buy)}",
                        f"当前暂停新开仓：{_bool_zh(self._state.pause_new_buy)}",
                    ],
                ),
                level="info",
                trace_id=source_trace_id,
            )
        return payload

    def record_factor_lifecycle(
        self,
        month: str,
        strategy: str,
        top_features: list[dict[str, object]],
        psr: float,
        ic_mean: float = 0.0,
        note: str = "",
        source_trace_id: str = "",
    ) -> dict[str, object]:
        if not self._config.factor_lifecycle.enabled:
            return {
                "accepted": False,
                "code": "disabled",
                "message": "factor lifecycle disabled by config",
            }
        normalized_strategy = strategy.strip().lower()
        normalized_month = _normalize_year_month(month)
        if not normalized_strategy:
            return {
                "accepted": False,
                "code": "invalid_strategy",
                "message": "strategy is required",
            }
        if not normalized_month:
            return {
                "accepted": False,
                "code": "invalid_month",
                "message": "month must be YYYY-MM",
            }
        normalized_features = _normalize_factor_features(top_features)
        if not normalized_features:
            return {
                "accepted": False,
                "code": "invalid_features",
                "message": "top_features must contain at least one valid item",
            }

        previous = self._latest_factor_lifecycle_record(strategy=normalized_strategy)
        previous_month = ""
        previous_features: list[dict[str, object]] = []
        if previous is not None:
            previous_month = str(previous.get("month", ""))
            raw_prev_features = previous.get("top_features")
            if isinstance(raw_prev_features, list):
                previous_features = _normalize_factor_features(raw_prev_features)

        drift_ratio = _factor_top3_drift_ratio(
            previous=previous_features,
            current=normalized_features,
        )
        psr_breach = psr < self._config.factor_lifecycle.psr_min
        drift_breach = drift_ratio > self._config.factor_lifecycle.shap_drift_threshold
        observation_mode = bool(psr_breach or drift_breach)
        record = {
            "month": normalized_month,
            "strategy": normalized_strategy,
            "psr": round(psr, 6),
            "ic_mean": round(ic_mean, 6),
            "top_features": normalized_features,
            "top3_drift_ratio": round(drift_ratio, 6),
            "psr_breach": psr_breach,
            "drift_breach": drift_breach,
            "observation_mode": observation_mode,
            "previous_month": previous_month,
            "note": note.strip(),
            "updated_at": datetime.now().isoformat(),
        }

        replaced = False
        for idx, item in enumerate(self._factor_lifecycle_history):
            if (
                str(item.get("strategy", "")).strip().lower() == normalized_strategy
                and str(item.get("month", "")).strip() == normalized_month
            ):
                self._factor_lifecycle_history[idx] = record
                replaced = True
                break
        if not replaced:
            self._factor_lifecycle_history.append(record)

        self._factor_lifecycle_history.sort(
            key=lambda item: (
                str(item.get("month", "")),
                str(item.get("strategy", "")),
            )
        )
        history_limit = max(1, self._config.factor_lifecycle.history_limit)
        if len(self._factor_lifecycle_history) > history_limit:
            overflow = len(self._factor_lifecycle_history) - history_limit
            if overflow > 0:
                self._factor_lifecycle_history = self._factor_lifecycle_history[overflow:]

        state = self._evaluate_factor_lifecycle(strategy=normalized_strategy)
        graveyard_updates = self._update_factor_graveyard(
            strategy=normalized_strategy,
            previous_features=previous_features,
            current_features=normalized_features,
            state=state,
            month=normalized_month,
        )
        self._record_audit_event(
            event_type="week7_factor_lifecycle_record",
            trace_id=source_trace_id,
            level="warn" if bool(state.get("observation_mode", False)) else "info",
            payload={
                "record": record,
                "state": state,
                "graveyard_updates": graveyard_updates,
            },
        )
        return {
            "accepted": True,
            "record": record,
            "state": state,
            "records": len(self._factor_lifecycle_history),
            "replaced": replaced,
            "graveyard_updates": graveyard_updates,
        }

    def factor_lifecycle_history(self, strategy: str = "", limit: int = 60) -> dict[str, object]:
        normalized = strategy.strip().lower()
        max_limit = max(1, self._config.factor_lifecycle.history_limit)
        capped_limit = max(1, min(limit, max_limit))
        records = self._factor_lifecycle_history
        if normalized:
            records = [
                item
                for item in records
                if str(item.get("strategy", "")).strip().lower() == normalized
            ]
        recent = records[-capped_limit:]
        return {
            "strategy": normalized,
            "records": len(recent),
            "history": recent,
        }

    def factor_graveyard(self, strategy: str = "", limit: int = 60) -> dict[str, object]:
        normalized = strategy.strip().lower()
        history_limit = max(1, self._config.factor_lifecycle.history_limit)
        capped_limit = max(1, min(limit, history_limit))
        if normalized:
            rows = self._factor_graveyard.get(normalized, [])
            recent = rows[-capped_limit:]
            return {
                "strategy": normalized,
                "records": len(recent),
                "graveyard": recent,
            }

        merged: list[dict[str, object]] = []
        for rows in self._factor_graveyard.values():
            merged.extend(rows)
        merged.sort(
            key=lambda item: (
                str(item.get("month", "")),
                str(item.get("strategy", "")),
                str(item.get("factor", "")),
            )
        )
        recent = merged[-capped_limit:]
        return {
            "strategy": "",
            "records": len(recent),
            "graveyard": recent,
        }

    def factor_lifecycle_status(self, strategy: str = "") -> dict[str, object]:
        normalized = strategy.strip().lower()
        if normalized:
            state = self._factor_lifecycle_state.get(normalized)
            if state is None:
                state = self._evaluate_factor_lifecycle(strategy=normalized)
            return {
                "enabled": self._config.factor_lifecycle.enabled,
                "graveyard_enabled": self._config.factor_lifecycle.graveyard_enabled,
                "strategy": normalized,
                "state": state,
                "graveyard_records": len(self._factor_graveyard.get(normalized, [])),
            }

        states: dict[str, dict[str, object]] = {}
        strategies = sorted(
            {
                str(item.get("strategy", "")).strip().lower()
                for item in self._factor_lifecycle_history
                if str(item.get("strategy", "")).strip()
            }
        )
        for name in strategies:
            state = self._factor_lifecycle_state.get(name)
            if state is None:
                state = self._evaluate_factor_lifecycle(strategy=name)
            states[name] = state

        observation = [
            name for name, state in states.items() if bool(state.get("observation_mode", False))
        ]
        return {
            "enabled": self._config.factor_lifecycle.enabled,
            "graveyard_enabled": self._config.factor_lifecycle.graveyard_enabled,
            "strategies": states,
            "observation_strategies": observation,
            "records": len(self._factor_lifecycle_history),
            "graveyard_records": sum(len(items) for items in self._factor_graveyard.values()),
        }

    def reset_factor_lifecycle(
        self,
        strategy: str = "",
        source_trace_id: str = "",
    ) -> dict[str, object]:
        normalized = strategy.strip().lower()
        if normalized:
            removed_state = 1 if normalized in self._factor_lifecycle_state else 0
            self._factor_lifecycle_state.pop(normalized, None)
            before = len(self._factor_lifecycle_history)
            self._factor_lifecycle_history = [
                item
                for item in self._factor_lifecycle_history
                if str(item.get("strategy", "")).strip().lower() != normalized
            ]
            removed_records = before - len(self._factor_lifecycle_history)
            removed_graveyard = len(self._factor_graveyard.get(normalized, []))
            self._factor_graveyard.pop(normalized, None)
        else:
            removed_state = len(self._factor_lifecycle_state)
            removed_records = len(self._factor_lifecycle_history)
            removed_graveyard = sum(len(items) for items in self._factor_graveyard.values())
            self._factor_lifecycle_state = {}
            self._factor_lifecycle_history = []
            self._factor_graveyard = {}

        payload = {
            "accepted": True,
            "strategy": normalized,
            "removed_state": removed_state,
            "removed_records": removed_records,
            "removed_graveyard_records": removed_graveyard,
        }
        self._record_audit_event(
            event_type="week7_factor_lifecycle_reset",
            trace_id=source_trace_id,
            payload=payload,
        )
        return payload

    def _latest_factor_lifecycle_record(self, strategy: str) -> dict[str, object] | None:
        latest: dict[str, object] | None = None
        for item in self._factor_lifecycle_history:
            if str(item.get("strategy", "")).strip().lower() != strategy:
                continue
            if latest is None:
                latest = item
                continue
            if str(item.get("month", "")) >= str(latest.get("month", "")):
                latest = item
        return latest

    def _evaluate_factor_lifecycle(self, strategy: str) -> dict[str, object]:
        normalized = strategy.strip().lower()
        records = [
            item
            for item in self._factor_lifecycle_history
            if str(item.get("strategy", "")).strip().lower() == normalized
        ]
        records.sort(key=lambda item: str(item.get("month", "")))

        latest = records[-1] if records else {}
        observation_mode = bool(latest.get("observation_mode", False)) if latest else False
        consecutive_observation = 0
        for item in reversed(records):
            if bool(item.get("observation_mode", False)):
                consecutive_observation += 1
                continue
            break

        previous = self._factor_lifecycle_state.get(normalized, {})
        previous_mode = bool(previous.get("observation_mode", False))
        state = {
            "strategy": normalized,
            "enabled": self._config.factor_lifecycle.enabled,
            "records": len(records),
            "latest_month": str(latest.get("month", "")) if latest else "",
            "psr": _as_float(latest.get("psr"), default=0.0) if latest else 0.0,
            "ic_mean": _as_float(latest.get("ic_mean"), default=0.0) if latest else 0.0,
            "top3_drift_ratio": (
                _as_float(latest.get("top3_drift_ratio"), default=0.0) if latest else 0.0
            ),
            "psr_breach": bool(latest.get("psr_breach", False)) if latest else False,
            "drift_breach": bool(latest.get("drift_breach", False)) if latest else False,
            "observation_mode": observation_mode,
            "consecutive_observation": consecutive_observation,
            "shap_drift_threshold": self._config.factor_lifecycle.shap_drift_threshold,
            "psr_min": self._config.factor_lifecycle.psr_min,
            "graveyard_enabled": self._config.factor_lifecycle.graveyard_enabled,
            "graveyard_records": len(self._factor_graveyard.get(normalized, [])),
            "updated_at": datetime.now().isoformat(),
        }
        self._factor_lifecycle_state[normalized] = state

        if observation_mode and not previous_mode:
            self._record_audit_event(
                event_type="week7_factor_lifecycle_observation",
                level="warn",
                payload={
                    "strategy": normalized,
                    "latest_month": state["latest_month"],
                    "psr": state["psr"],
                    "top3_drift_ratio": state["top3_drift_ratio"],
                },
            )
        return state

    def _update_factor_graveyard(
        self,
        strategy: str,
        previous_features: list[dict[str, object]],
        current_features: list[dict[str, object]],
        state: dict[str, object],
        month: str,
    ) -> int:
        if not self._config.factor_lifecycle.graveyard_enabled:
            return 0
        if not bool(state.get("observation_mode", False)):
            return 0
        required_streak = max(1, int(self._config.factor_lifecycle.graveyard_observation_months))
        if _as_int(state.get("consecutive_observation"), default=0) < required_streak:
            return 0

        prev_top = {
            str(item.get("name", "")).strip(): _as_float(item.get("importance"), default=0.0)
            for item in previous_features[:3]
            if str(item.get("name", "")).strip()
        }
        curr_names = {
            str(item.get("name", "")).strip()
            for item in current_features[:3]
            if str(item.get("name", "")).strip()
        }
        if not prev_top:
            return 0

        bucket = self._factor_graveyard.setdefault(strategy, [])
        existing = {(str(item.get("month", "")), str(item.get("factor", ""))) for item in bucket}
        updates = 0
        for factor, importance in prev_top.items():
            if factor in curr_names:
                continue
            key = (month, factor)
            if key in existing:
                continue
            bucket.append(
                {
                    "month": month,
                    "strategy": strategy,
                    "factor": factor,
                    "last_importance": round(abs(importance), 6),
                    "reason": "top3_drift_drop",
                    "updated_at": datetime.now().isoformat(),
                }
            )
            existing.add(key)
            updates += 1

        history_limit = max(1, self._config.factor_lifecycle.history_limit)
        if len(bucket) > history_limit:
            overflow = len(bucket) - history_limit
            if overflow > 0:
                self._factor_graveyard[strategy] = bucket[overflow:]
        return updates

    def cloud_backup_ping(
        self,
        source: str = "manual",
        source_trace_id: str = "",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        self._refresh_cloud_backup_state_from_disk()
        now = timestamp or datetime.now()
        recovered = self._cloud_backup_alert_active
        self._cloud_backup_last_ping_at = now
        self._cloud_backup_last_ping_source = source.strip() or "manual"
        self._cloud_backup_armed = True
        self._cloud_backup_alert_active = False
        if recovered:
            self._cloud_backup_last_recovery_at = now
            self._persist_runtime_state_to_disk()
            if self._config.cloud_backup.notify_recovery:
                recovery_status = self.cloud_backup_status(now=now)
                self.notify(
                    title=_push_title(
                        priority="P2", category="system", summary="cloud backup recovered"
                    ),
                    content=_notification_message_zh(
                        trigger="云端冷备监控重新收到本地主机心跳，失联状态已解除。",
                        impact="本地自动盯盘、风控与推送链路重新回到受监控状态，冷备保护恢复正常。",
                        action="无需紧急处理；可在本地监控雷达查看系统状态页，确认心跳来源和最近恢复时间。",
                        details=[
                            f"心跳来源：{self._cloud_backup_last_ping_source or '-'}",
                            f"恢复时间：{_format_notification_time_zh(str(recovery_status.get('last_recovery_at', '')))}",
                        ],
                    ),
                    level="info",
                    trace_id=source_trace_id,
                )
            self._record_audit_event(
                event_type="week7_cloud_backup_recovered",
                trace_id=source_trace_id,
                payload={"source": self._cloud_backup_last_ping_source},
            )

        self._record_audit_event(
            event_type="week7_cloud_backup_ping",
            trace_id=source_trace_id,
            payload={
                "source": self._cloud_backup_last_ping_source,
                "recovered": recovered,
            },
        )
        self._persist_runtime_state_to_disk()
        status = self.cloud_backup_status(now=now)
        status["accepted"] = True
        status["source"] = self._cloud_backup_last_ping_source
        status["recovered"] = recovered
        return status

    def cloud_backup_status(self, now: datetime | None = None) -> dict[str, object]:
        self._refresh_cloud_backup_state_from_disk()
        current = now or datetime.now()
        armed = bool(self._cloud_backup_armed or self._cloud_backup_last_ping_at is not None)
        require_first_ping = bool(self._config.cloud_backup.require_first_ping_before_alert)
        if not self._config.cloud_backup.enabled:
            return {
                "enabled": False,
                "is_offline": False,
                "offline_seconds": 0.0,
                "offline_minutes": 0.0,
                "alert_after_offline_min": self._config.cloud_backup.alert_after_offline_min,
                "require_first_ping_before_alert": require_first_ping,
                "last_ping_at": "",
                "last_ping_source": self._cloud_backup_last_ping_source,
                "alert_active": False,
                "last_alert_at": "",
                "last_recovery_at": "",
                "armed": armed,
                "has_ping_history": armed,
            }

        if not armed and require_first_ping:
            offline_seconds = 0.0
        else:
            reference = self._cloud_backup_last_ping_at or self._service_started_at
            offline_seconds = max(0.0, (current - reference).total_seconds())
        threshold_sec = max(1, self._config.cloud_backup.alert_after_offline_min * 60)
        is_offline = (armed or not require_first_ping) and offline_seconds >= threshold_sec
        alert_active = bool(self._cloud_backup_alert_active and (armed or not require_first_ping))
        return {
            "enabled": True,
            "is_offline": is_offline,
            "offline_seconds": round(offline_seconds, 2),
            "offline_minutes": round(offline_seconds / 60.0, 4),
            "alert_after_offline_min": self._config.cloud_backup.alert_after_offline_min,
            "require_first_ping_before_alert": require_first_ping,
            "last_ping_at": (
                self._cloud_backup_last_ping_at.isoformat()
                if self._cloud_backup_last_ping_at is not None
                else ""
            ),
            "last_ping_source": self._cloud_backup_last_ping_source,
            "alert_active": alert_active,
            "last_alert_at": (
                self._cloud_backup_last_alert_at.isoformat()
                if self._cloud_backup_last_alert_at is not None
                else ""
            ),
            "last_recovery_at": (
                self._cloud_backup_last_recovery_at.isoformat()
                if self._cloud_backup_last_recovery_at is not None
                else ""
            ),
            "armed": armed,
            "has_ping_history": armed,
        }

    def run_cloud_backup_check(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        current = now or datetime.now()
        status = self.cloud_backup_status(now=current)
        if not bool(status.get("enabled", False)):
            return {
                "status": status,
                "alerted": False,
                "snapshot": {},
            }
        if (
            bool(status.get("require_first_ping_before_alert", True))
            and not bool(status.get("armed", False))
        ):
            if self._cloud_backup_alert_active:
                self._cloud_backup_alert_active = False
                status = self.cloud_backup_status(now=current)
            return {
                "status": status,
                "alerted": False,
                "snapshot": {},
            }

        alerted = False
        snapshot: dict[str, object] = {}
        if bool(status.get("is_offline", False)) and not self._cloud_backup_alert_active:
            self._cloud_backup_alert_active = True
            self._cloud_backup_last_alert_at = current
            alerted = True
            self._persist_runtime_state_to_disk()
            snapshot = self._cloud_backup_snapshot(now=current)
            self.notify(
                title=_push_title(priority="P0", category="system", summary="cloud backup offline"),
                content=_notification_message_zh(
                    trigger=(
                        "云端冷备监控发现本地主机已超过设定时长没有上报运行心跳，"
                        "核心主机被判定为失联。"
                    ),
                    impact="自动盯盘、止损推送与执行监控可能已全部失效，账户当前进入无人值守的风险窗口。",
                    action="请立刻检查本地主机是否断电、断网或死机；在恢复前，请您人工盯住当前持仓与风险敞口。",
                    details=[
                        f"离线时长：{_as_float(status.get('offline_minutes'), default=0.0):.2f} 分钟",
                        f"当前权益：{_as_float(snapshot.get('current_equity'), default=0.0):,.2f}",
                        f"持仓数量：{_as_int(snapshot.get('open_positions'), default=0)}",
                        f"持仓标的：{','.join(_string_list(snapshot.get('symbols', []))[:6]) or '-'}",
                    ],
                    detail_title="危机时刻最后已知资产快照",
                ),
                level="warn",
                trace_id=source_trace_id,
            )
            self._record_audit_event(
                event_type="week7_cloud_backup_offline_alert",
                trace_id=source_trace_id,
                level="warn",
                payload={
                    "status": status,
                    "snapshot": snapshot,
                },
            )
            self._persist_runtime_state_to_disk()
            status = self.cloud_backup_status(now=current)
        return {
            "status": status,
            "alerted": alerted,
            "snapshot": snapshot,
        }

    def _cloud_backup_snapshot(self, now: datetime) -> dict[str, object]:
        positions = self._portfolio.positions()
        symbols = sorted(
            {
                str(item.get("symbol", "")).strip()
                for item in positions
                if str(item.get("symbol", "")).strip()
            }
        )
        return {
            "timestamp": now.isoformat(),
            "current_equity": self._state.current_equity,
            "open_positions": len(positions),
            "symbols": symbols,
        }

    def _register_default_jobs(self) -> None:
        trading_weekdays = (0, 1, 2, 3, 4)
        trading_day_filter = is_a_share_trading_day
        self._scheduler.register(
            name="premarket_scan",
            trigger_hhmm=self._config.scheduler.premarket_time,
            callback=self._job_premarket_scan,
            latest_hhmm=self._config.scheduler.auction_report_time,
            weekdays=trading_weekdays,
            date_predicate=trading_day_filter,
        )
        self._scheduler.register(
            name="midday_news_brief",
            trigger_hhmm=self._config.scheduler.midday_news_time,
            callback=self._job_midday_news_brief,
            latest_hhmm="13:00",
            weekdays=trading_weekdays,
            date_predicate=trading_day_filter,
        )
        self._scheduler.register(
            name="auction_report",
            trigger_hhmm=self._config.scheduler.auction_report_time,
            callback=self._job_auction_report,
            latest_hhmm="09:35",
            weekdays=trading_weekdays,
            date_predicate=trading_day_filter,
        )
        self._scheduler.register(
            name="close_reconcile",
            trigger_hhmm=self._config.scheduler.close_reconcile_time,
            callback=self._job_close_reconcile,
            latest_hhmm="15:40",
            weekdays=trading_weekdays,
            date_predicate=trading_day_filter,
        )
        if self._config.market_warehouse.enabled and self._config.market_warehouse.auto_run:
            self._scheduler.register(
                name="market_warehouse_sync",
                trigger_hhmm=self._config.market_warehouse.run_time,
                callback=self._job_market_warehouse_sync,
                weekdays=trading_weekdays,
                date_predicate=trading_day_filter,
            )
        elif (
            self._config.tdx_sync.enabled
            and self._config.tdx_sync.auto_run
            and str(self._config.tdx_sync.vipdoc_root).strip()
        ):
            self._scheduler.register(
                name="tdx_offline_sync",
                trigger_hhmm=self._config.tdx_sync.run_time,
                callback=self._job_tdx_offline_sync,
                weekdays=trading_weekdays,
                date_predicate=trading_day_filter,
            )
        if self._config.acceptance.enabled and self._config.acceptance.auto_run:
            self._scheduler.register(
                name="week4_acceptance",
                trigger_hhmm=self._config.scheduler.week4_acceptance_time,
                callback=self._job_week4_acceptance,
                latest_hhmm="23:59",
                weekdays=trading_weekdays,
                date_predicate=trading_day_filter,
            )
        if self._config.week5.enabled and self._config.week5.auto_run:
            interval_profiles: list[tuple[str, str, int]] = []
            raw_profiles = list(self._config.week5.first_board_window_intervals)
            for raw_profile in raw_profiles:
                parsed_profile = _parse_hhmm_window_interval(str(raw_profile))
                if parsed_profile is None:
                    self._record_audit_event(
                        event_type="scheduler_config_warning",
                        level="warn",
                        message=(
                            "invalid week5.first_board_window_intervals item: "
                            f"{raw_profile} (expect HH:MM-HH:MM@N)"
                        ),
                    )
                    continue
                interval_profiles.append(parsed_profile)
            if not interval_profiles:
                fallback_interval = max(1, self._config.week5.first_board_interval_min)
                for window in self._config.week5.first_board_windows:
                    parsed = _parse_hhmm_window(window)
                    if parsed is None:
                        self._record_audit_event(
                            event_type="scheduler_config_warning",
                            level="warn",
                            message=f"invalid week5.first_board_windows item: {window}",
                        )
                        continue
                    interval_profiles.append((parsed[0], parsed[1], fallback_interval))
            for index, (start_hhmm, end_hhmm, interval_minutes) in enumerate(
                interval_profiles,
                start=1,
            ):
                try:
                    self._scheduler.register_interval(
                        name=f"week5_first_board_{index}",
                        window_start_hhmm=start_hhmm,
                        window_end_hhmm=end_hhmm,
                        interval_minutes=interval_minutes,
                        callback=self._job_week5_scan,
                        weekdays=trading_weekdays,
                        date_predicate=trading_day_filter,
                    )
                except ValueError as exc:
                    self._record_audit_event(
                        event_type="scheduler_config_warning",
                        level="warn",
                        message=(
                            "failed to register week5 interval profile "
                            f"{start_hhmm}-{end_hhmm}@{interval_minutes}: {exc}"
                        ),
                    )
            live_interval_profiles: list[tuple[str, str, int]] = []
            for raw_profile in self._config.week5.live_runtime_window_intervals:
                parsed_profile = _parse_hhmm_window_interval(str(raw_profile))
                if parsed_profile is None:
                    self._record_audit_event(
                        event_type="scheduler_config_warning",
                        level="warn",
                        message=(
                            "invalid week5.live_runtime_window_intervals item: "
                            f"{raw_profile} (expect HH:MM-HH:MM@N)"
                        ),
                    )
                    continue
                live_interval_profiles.append(parsed_profile)
            if not live_interval_profiles:
                live_interval_profiles = [
                    (start_hhmm, end_hhmm, max(5, interval_minutes))
                    for start_hhmm, end_hhmm, interval_minutes in interval_profiles
                ]
            for index, (start_hhmm, end_hhmm, interval_minutes) in enumerate(
                live_interval_profiles,
                start=1,
            ):
                try:
                    self._scheduler.register_interval(
                        name=f"week5_live_runtime_{index}",
                        window_start_hhmm=start_hhmm,
                        window_end_hhmm=end_hhmm,
                        interval_minutes=interval_minutes,
                        callback=self._job_week5_live_runtime,
                        weekdays=trading_weekdays,
                        date_predicate=trading_day_filter,
                    )
                except ValueError as exc:
                    self._record_audit_event(
                        event_type="scheduler_config_warning",
                        level="warn",
                        message=(
                            "failed to register week5 live runtime interval profile "
                            f"{start_hhmm}-{end_hhmm}@{interval_minutes}: {exc}"
                        ),
                    )
            if self._config.week5.market_radar_enabled:
                radar_profiles: list[tuple[str, str, int]] = []
                for raw_profile in self._config.week5.market_radar_window_intervals:
                    parsed_profile = _parse_hhmm_window_interval(str(raw_profile))
                    if parsed_profile is None:
                        self._record_audit_event(
                            event_type="scheduler_config_warning",
                            level="warn",
                            message=(
                                "invalid week5.market_radar_window_intervals item: "
                                f"{raw_profile} (expect HH:MM-HH:MM@N)"
                            ),
                        )
                        continue
                    radar_profiles.append(parsed_profile)
                for index, (start_hhmm, end_hhmm, interval_minutes) in enumerate(
                    radar_profiles,
                    start=1,
                ):
                    try:
                        self._scheduler.register_interval(
                            name=f"week5_market_radar_{index}",
                            window_start_hhmm=start_hhmm,
                            window_end_hhmm=end_hhmm,
                            interval_minutes=interval_minutes,
                            callback=self._job_week5_market_radar,
                            weekdays=trading_weekdays,
                            date_predicate=trading_day_filter,
                        )
                    except ValueError as exc:
                        self._record_audit_event(
                            event_type="scheduler_config_warning",
                            level="warn",
                            message=(
                                "failed to register week5 market radar interval profile "
                                f"{start_hhmm}-{end_hhmm}@{interval_minutes}: {exc}"
                            ),
                        )
        if self._config.week6.enabled and self._config.week6.auto_run:
            if self._config.week6.data_prewarm_enabled:
                self._scheduler.register(
                    name="week6_data_prewarm",
                    trigger_hhmm=self._config.week6.data_prewarm_time,
                    callback=self._job_week6_data_prewarm,
                    weekdays=trading_weekdays,
                    date_predicate=trading_day_filter,
                )
            daily_time = (
                self._config.week6.run_time.strip() or self._config.scheduler.week6_daily_time
            )
            self._scheduler.register(
                name="week6_daily",
                trigger_hhmm=daily_time,
                callback=self._job_week6_daily,
                latest_hhmm="16:00",
                weekdays=trading_weekdays,
                date_predicate=trading_day_filter,
            )
        if self._config.evolution.enabled and self._config.evolution.auto_run:
            self._scheduler.register(
                name="evolution_offhours",
                trigger_hhmm=self._config.evolution.offhours_time,
                callback=self._job_evolution_offhours,
            )
            m3_maintenance_interval = max(1, self._config.evolution.m3_maintenance_interval_min)
            self._scheduler.register_interval(
                name="evolution_m3_maintenance",
                window_start_hhmm="00:00",
                window_end_hhmm="23:59",
                interval_minutes=m3_maintenance_interval,
                callback=self._job_evolution_m3_maintenance,
            )
            confirmation_interval = max(
                1,
                self._config.evolution.release_confirmation_watchdog_interval_min,
            )
            self._scheduler.register_interval(
                name="evolution_release_confirmation_watchdog",
                window_start_hhmm="00:00",
                window_end_hhmm="23:59",
                interval_minutes=confirmation_interval,
                callback=self._job_evolution_release_confirmation_watchdog,
            )
        idle_enabled, _ = self._resolve_idle_queue_enabled()
        idle_auto_run, _ = self._resolve_idle_queue_auto_run()
        if idle_enabled and idle_auto_run:
            idle_interval = max(1, self._config.idle_queue.dispatch_interval_minutes)
            self._scheduler.register_interval(
                name="idle_queue_tick",
                window_start_hhmm="00:00",
                window_end_hhmm="23:59",
                interval_minutes=idle_interval,
                callback=self._job_idle_queue_tick,
            )
        if self._config.cloud_backup.enabled:
            interval_minutes = max(1, self._config.cloud_backup.ping_interval_min)
            self._scheduler.register_interval(
                name="week7_cloud_backup_watchdog",
                window_start_hhmm="00:00",
                window_end_hhmm="23:59",
                interval_minutes=interval_minutes,
                callback=self._job_week7_cloud_backup_watchdog,
            )

    def _job_premarket_scan(self) -> dict[str, object]:
        global_snapshot_report = self._collect_global_market_snapshot(source_trace_id="premarket")
        report = self.run_pipeline(strategy="trend", job_name="premarket_scan")
        news_watchlist = self.preview_news_watchlist(strategy="trend", limit=5)
        news_briefing = self.build_live_news_briefing(
            phase="premarket",
            strategy="trend",
            max_symbols=6,
            max_items=5,
            max_age_hours=18.0,
            record_audit=False,
        )
        trace_id = str(report.get("trace_id", ""))
        signal_count = _signals_count(report)
        actionable_count = _actionable_count(report)
        week6_execution = report.get("week6_execution", {})
        if not isinstance(week6_execution, dict):
            week6_execution = {}
        regime = str(week6_execution.get("regime", ""))
        global_risk_score = _as_float(week6_execution.get("global_risk_score"), default=50.0)

        weights = week6_execution.get("allocation_weights", {})
        if not isinstance(weights, dict):
            weights = {}

        regime_display = _regime_zh(regime)
        news_source = str(news_watchlist.get("source", "watchlist")).strip() or "watchlist"
        news_records = _as_int(news_watchlist.get("records"), default=0)
        news_summary = news_watchlist.get("summary", {})
        news_avg = 0.5
        if isinstance(news_summary, dict):
            news_avg = _as_float(news_summary.get("average_news_component"), default=0.5)

        content = self._build_premarket_strategy_brief_content(
            regime_display=regime_display,
            weights=weights,
            global_risk_score=global_risk_score,
            news_avg=news_avg,
            news_records=news_records,
            news_source=news_source,
            news_watchlist=news_watchlist,
            news_briefing=news_briefing,
            actionable_count=actionable_count,
        )
        title_summary = self._build_news_brief_push_summary(
            news_briefing=news_briefing,
            empty_summary="暂无真实新闻标题",
        )
        self._notify_if_changed(
            dedup_key=f"notify:premarket-brief:{datetime.now().strftime('%Y%m%d')}",
            title=_push_title(priority="P2", category="morning", summary=title_summary),
            content=content,
            level="info",
            trace_id=trace_id,
            ttl_sec=30 * 3600,
        )

        self._notify_actionable_signals(report, trace_id=trace_id, title_prefix="盘前")
        return {
            "trace_id": trace_id,
            "signals": signal_count,
            "actionable": actionable_count,
            "global_snapshot": global_snapshot_report,
            "news_watchlist": news_watchlist,
            "news_briefing": news_briefing,
        }

    def _job_midday_news_brief(self) -> dict[str, object]:
        news_briefing = self.build_live_news_briefing(
            phase="midday",
            strategy="trend",
            max_symbols=6,
            max_items=5,
            max_age_hours=8.0,
            record_audit=False,
        )
        week5_report = (
            self._last_week5_scan_report if isinstance(self._last_week5_scan_report, dict) else {}
        )
        content = self._build_midday_news_brief_content(
            news_briefing=news_briefing,
            week5_report=week5_report,
        )
        title_summary = self._build_news_brief_push_summary(
            news_briefing=news_briefing,
            empty_summary="暂无新的个股新闻标题",
        )
        dedup_payload = {
            "items": [
                {
                    "symbol": str(item.get("symbol", "")).strip(),
                    "title": str(item.get("title", "")).strip(),
                }
                for item in _coerce_mapping_list(news_briefing.get("items"))
            ],
            "week5_trace_id": str(week5_report.get("trace_id", "")),
        }
        self._notify_if_changed(
            dedup_key=f"notify:midday-news-brief:{datetime.now().strftime('%Y%m%d')}",
            dedup_value=json.dumps(dedup_payload, ensure_ascii=False, sort_keys=True),
            title=_push_title(priority="P2", category="midday", summary=title_summary),
            content=content,
            level="info",
            trace_id=str(week5_report.get("trace_id", "")),
            ttl_sec=20 * 3600,
        )
        return news_briefing

    def _build_premarket_strategy_brief_content(
        self,
        *,
        regime_display: str,
        weights: dict[str, object],
        global_risk_score: float,
        news_avg: float,
        news_records: int,
        news_source: str,
        news_watchlist: dict[str, object],
        news_briefing: dict[str, object] | None,
        actionable_count: int,
    ) -> str:
        effective_news_records = (
            _as_int(news_briefing.get("records"), default=0)
            if isinstance(news_briefing, dict)
            else 0
        )
        raw_news_records = (
            _as_int(news_briefing.get("raw_records"), default=effective_news_records)
            if isinstance(news_briefing, dict)
            else effective_news_records
        )
        news_record_text = f"新闻摘要={effective_news_records}"
        if raw_news_records > effective_news_records:
            news_record_text += f"（原始新闻{raw_news_records}条，已按股票去重）"
        impact_line = (
            f"影响=市场状态{regime_display}；建议分配={_strategy_weights_zh(weights)}；"
            f"全局风险分={global_risk_score:.2f}；新闻均值={news_avg:.3f}；"
            f"情绪覆盖标的={news_records}；{news_record_text}；来源={_news_source_zh(news_source)}"
        )
        highlight_lines = self._collect_news_highlights_for_notification(
            news_watchlist=news_watchlist,
            news_briefing=news_briefing,
            limit=3,
        )
        detailed_news_blocks = self._render_news_briefing_detail_blocks(
            news_briefing=news_briefing,
            limit=3,
        )
        if detailed_news_blocks:
            news_line = "重点新闻=\n" + "\n\n".join(detailed_news_blocks)
        elif highlight_lines:
            news_line = "重点新闻=" + "；".join(highlight_lines)
        else:
            fallback_rows: list[str] = []
            raw_items = news_watchlist.get("items", [])
            if isinstance(raw_items, list):
                for item in raw_items[:3]:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol", "")).strip()
                    if not symbol:
                        continue
                    score = _as_float(item.get("news_component"), default=0.5)
                    fallback_rows.append(
                        f"{self._format_symbol_display(symbol)}（{score:.3f}）"
                    )
            news_line = (
                "重点新闻=当前未接入真实新闻标题；代理情绪标的=" + "；".join(fallback_rows)
                if fallback_rows
                else "重点新闻=当前未接入真实新闻标题，请先补齐新闻源后再启用新闻摘要推送"
            )
        if global_risk_score < 40.0 or "急跌" in regime_display:
            action_line = "建议动作=今天以防守为主，优先看重点新闻标的反馈，尽量不追高"
        elif actionable_count > 0:
            action_line = (
                f"建议动作=先复核盘前可执行信号 {actionable_count} 条，再决定是否加入重点盯盘"
            )
        else:
            action_line = "建议动作=先跟踪重点新闻标的和开盘强弱，再决定是否进入盘中观察优先级"
        return "\n".join(
            [
                "事件=盘前每日策略简报",
                impact_line,
                news_line,
                action_line,
            ]
        )

    def _build_midday_news_brief_content(
        self,
        *,
        news_briefing: dict[str, object],
        week5_report: dict[str, object],
    ) -> str:
        effective_news_records = _as_int(news_briefing.get("records"), default=0)
        raw_news_records = _as_int(
            news_briefing.get("raw_records"),
            default=effective_news_records,
        )
        rendered_items = self._render_news_briefing_detail_blocks(
            news_briefing=news_briefing,
            limit=4,
        )
        week5_summary = week5_report.get("summary", {}) if isinstance(week5_report, dict) else {}
        if not isinstance(week5_summary, dict):
            week5_summary = {}
        leaders_count = _as_int(week5_summary.get("leaders"), default=0)
        anomalies_count = _as_int(week5_summary.get("anomalies"), default=0)
        summary_count_text = f"有效摘要={effective_news_records}"
        if raw_news_records > effective_news_records:
            summary_count_text += f"（原始新闻{raw_news_records}条，已按股票去重）"
        if rendered_items:
            news_line = "重点新闻=\n" + "\n\n".join(rendered_items)
        else:
            news_line = "重点新闻=当前未抓到新的个股新闻标题，午盘前请优先复核盘中异动和首板变化"
        return "\n".join(
            [
                "事件=午盘前新闻简报",
                (
                    f"影响=覆盖标的={_as_int(news_briefing.get('focus_count'), default=0)}；"
                    f"{summary_count_text}；"
                    f"盘中龙头={leaders_count}；异常项={anomalies_count}"
                ),
                news_line,
                "建议动作=13:00 开盘前优先复核重点新闻对应标的，再结合盘中龙头强弱决定是否继续观察",
            ]
        )

    def _build_news_brief_push_summary(
        self,
        *,
        news_briefing: dict[str, object] | None,
        empty_summary: str,
    ) -> str:
        if not isinstance(news_briefing, dict):
            return empty_summary
        raw_items = news_briefing.get("items", [])
        total_records = _as_int(news_briefing.get("records"), default=0)
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                title = str(item.get("title", "")).strip()
                if not symbol or not title:
                    continue
                name = str(item.get("name", "")).strip()
                symbol_label = self._format_symbol_display(symbol, name)
                title_preview = _news_title_preview_text(title=title, name=name)
                if symbol_label:
                    preview = f"{symbol_label}：{_truncate_text_zh(title_preview, 18)}"
                else:
                    preview = _truncate_text_zh(title_preview, 24)
                if total_records > 1:
                    return f"{preview} 等{total_records}条"
                return preview
        focus_count = _as_int(news_briefing.get("focus_count"), default=0)
        if focus_count > 0:
            return f"覆盖{focus_count}只，{empty_summary}"
        return empty_summary

    def _render_news_briefing_detail_blocks(
        self,
        *,
        news_briefing: dict[str, object] | None,
        limit: int,
    ) -> list[str]:
        if not isinstance(news_briefing, dict):
            return []
        raw_items = news_briefing.get("items", [])
        if not isinstance(raw_items, list):
            return []
        blocks: list[str] = []
        for index, item in enumerate(raw_items[: max(1, limit)], start=1):
            if not isinstance(item, dict):
                continue
            rendered = self._render_news_briefing_detail_block(item=item, index=index)
            if rendered:
                blocks.append(rendered)
        return blocks

    def _render_news_briefing_detail_block(
        self,
        *,
        item: Mapping[str, object],
        index: int,
    ) -> str:
        symbol = str(item.get("symbol", "")).strip()
        title = str(item.get("title", "")).strip()
        if not symbol or not title:
            return ""
        name = str(item.get("name", "")).strip()
        published_at = str(item.get("published_at", "")).strip()
        source = str(item.get("source", "")).strip()
        content = str(item.get("content", "")).strip()
        symbol_label = self._format_symbol_display(symbol, name)
        title_preview = _news_title_preview_text(title=title, name=name) or title
        summary_preview = _news_content_preview_text(
            title=title,
            content=content,
            name=name,
        )
        meta_parts = [
            symbol_label,
            _format_news_time_short_zh(published_at),
            _news_source_short_zh(source),
        ]
        lines = [f"{index}. {'｜'.join(part for part in meta_parts if part)}"]
        lines.append(f"   标题：{_truncate_text_zh(title_preview, 34)}")
        if summary_preview:
            lines.append(f"   摘要：{_truncate_text_zh(summary_preview, 68)}")
        return "\n".join(lines)

    def _collect_news_highlights_for_notification(
        self,
        *,
        news_watchlist: dict[str, object],
        news_briefing: dict[str, object] | None,
        limit: int = 3,
    ) -> list[str]:
        raw_brief_items = news_briefing.get("items", []) if isinstance(news_briefing, dict) else []
        rendered_real_news: list[str] = []
        if isinstance(raw_brief_items, list):
            for item in raw_brief_items[: max(1, limit)]:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                name = str(item.get("name", "")).strip()
                title = str(item.get("title", "")).strip()
                published_at = str(item.get("published_at", "")).strip()
                if not symbol or not title:
                    continue
                time_text = _format_news_time_short_zh(published_at)
                source_text = _news_source_short_zh(str(item.get("source", "")).strip())
                symbol_label = self._format_symbol_display(symbol, name)
                rendered_real_news.append(
                    f"{symbol_label} {time_text}｜{source_text}｜{_truncate_text_zh(title, 28)}"
                )
        if rendered_real_news:
            return rendered_real_news
        raw_selected = news_watchlist.get("selected_symbols", [])
        selected_symbols = (
            [str(item).strip() for item in raw_selected if str(item).strip()]
            if isinstance(raw_selected, list)
            else []
        )
        if not selected_symbols:
            return []
        score_map: dict[str, float] = {}
        raw_items = news_watchlist.get("items", [])
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if not symbol:
                    continue
                score_map[symbol] = _as_float(item.get("news_component"), default=0.5)
        records = self._build_evolution_m7_news_records(selected_symbols)
        highlights: list[tuple[float, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for item in records:
            if not isinstance(item, dict) or bool(item.get("proxy_generated", False)):
                continue
            symbol = str(item.get("symbol", "")).strip()
            headline = str(item.get("headline", "")).strip()
            if not symbol or not headline:
                continue
            pair_key = (symbol, headline)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            sentiment = _clamp(_as_float(item.get("sentiment"), default=0.0), -1.0, 1.0)
            confidence = _clamp(_as_float(item.get("llm_confidence"), default=0.5), 0.0, 1.0)
            news_component = score_map.get(symbol, 0.5)
            importance = (
                abs(sentiment) * 0.55 + confidence * 0.20 + abs(news_component - 0.5) * 0.50
            )
            highlights.append(
                (
                    importance,
                    (
                        f"{self._format_symbol_display(symbol)} "
                        f"{_sentiment_label_zh(sentiment)}｜{_truncate_text_zh(headline, 26)}"
                    ),
                )
            )
        highlights.sort(key=lambda item: (-item[0], item[1]))
        if highlights:
            return [text for _, text in highlights[: max(1, limit)]]
        return []

    def _job_auction_report(self) -> dict[str, object]:
        report = self.run_pipeline(strategy="trend", job_name="auction_report")
        trace_id = str(report.get("trace_id", ""))
        signals_count = _signals_count(report)
        actionable_count = _actionable_count(report)
        content = self._build_auction_report_content(
            report=report,
            actionable_count=actionable_count,
        )
        self._notify_if_changed(
            dedup_key=f"notify:auction-report:{datetime.now().strftime('%Y%m%d')}",
            title=_push_title(priority="P2", category="morning", summary="auction report"),
            content=content,
            dedup_value=f"auction-report:{datetime.now().strftime('%Y%m%d')}",
            level="info",
            trace_id=trace_id,
            ttl_sec=18 * 3600,
        )
        self._notify_actionable_signals(report, trace_id=trace_id, title_prefix="09:26竞价")
        return {
            "trace_id": trace_id,
            "signals": signals_count,
            "actionable": actionable_count,
        }

    def _build_auction_report_content(
        self,
        *,
        report: dict[str, object],
        actionable_count: int,
    ) -> str:
        raw_signals = report.get("signals")
        signals = (
            [item for item in raw_signals if isinstance(item, dict)]
            if isinstance(raw_signals, list)
            else []
        )
        week6_execution = report.get("week6_execution", {})
        if not isinstance(week6_execution, dict):
            week6_execution = {}

        regime_display = _regime_zh(str(week6_execution.get("regime", "")))
        global_risk_score = _as_float(week6_execution.get("global_risk_score"), default=50.0)
        execution_mode = str(report.get("execution_mode", "")).strip() or "unknown"
        watchlist_size = len(self._state.watchlist)

        action_counts = {"buy": 0, "watch": 0, "hold": 0}
        for item in signals:
            action = str(item.get("action", "")).strip().lower()
            if action in action_counts:
                action_counts[action] += 1

        sorted_signals = sorted(
            signals,
            key=lambda item: (
                0 if str(item.get("action", "")).strip().lower() == "buy" else 1,
                0 if str(item.get("action", "")).strip().lower() == "watch" else 1,
                -_as_float(item.get("score"), default=0.0),
                str(item.get("symbol", "")),
            ),
        )
        focus_signals = sorted_signals[:5]
        focus_lines: list[str] = []
        focus_symbols: list[str] = []
        for item in focus_signals:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            action = str(item.get("action", "")).strip().lower()
            score = _as_float(item.get("score"), default=0.0)
            target_position = _as_float(item.get("target_position"), default=0.0)
            action_label = _translate_signal_action_zh(action)
            focus_symbols.append(symbol)
            focus_lines.append(
                f"{symbol}｜结论={action_label}｜评分={score:.2f}｜目标仓位={target_position:.0%}"
            )

        trigger_line = "09:25 集合竞价数据已完成分析，系统在 09:26 输出开盘前最后一轮竞价速报。"
        impact_line = (
            f"观察池={watchlist_size}；候选信号={len(signals)}；可执行信号={actionable_count}；"
            f"市场状态={regime_display}；全局风险分={global_risk_score:.2f}；执行模式={execution_mode}"
        )
        if actionable_count > 0:
            action_line = "优先复核可执行买卖指令，再结合开盘后前 5 分钟量价变化决定是否处理。"
        elif focus_symbols:
            action_line = (
                "当前没有直接买卖指令；09:30 开盘后优先盯 "
                + "、".join(focus_symbols[:3])
                + " 的开盘强弱、量能和承接变化。"
            )
        else:
            action_line = "当前没有直接买卖指令；开盘后请优先观察观察池内是否出现超预期异动。"

        detail_lines = [
            (
                f"动作分布：买入 {action_counts['buy']} / 观察 {action_counts['watch']} / "
                f"持有或忽略 {action_counts['hold']}"
            )
        ]
        if focus_lines:
            detail_lines.extend(f"重点盯盘：{line}" for line in focus_lines)

        return _notification_message_zh(
            trigger=trigger_line,
            impact=impact_line,
            action=action_line,
            details=detail_lines,
            detail_title="竞价摘要",
        )

    def _job_close_reconcile(self) -> dict[str, object]:
        now = datetime.now()
        broker_snapshot_refresh = self._refresh_simulated_broker_snapshot_for_close_reconcile()
        report = self.run_reconciliation(timestamp=now)
        status = str(report.get("status", ""))
        if status == "missing_snapshot":
            self.notify(
                title=_push_title(priority="P1", category="close", summary="reconcile required"),
                content=_notification_message_zh(
                    trigger="今日收盘后未检测到券商持仓快照。",
                    impact="系统虽然仍在正常运行，但无法完成模拟盘与券商侧的收盘对账校验。",
                    action="请先上传券商持仓快照，再到本地监控雷达执行手工对账，确认账实一致。",
                    details=[
                        "当前状态：收盘存活正常",
                        "缺失项目：券商持仓快照",
                    ],
                ),
                level="warn",
            )
        elif status == "ok":
            current_equity = _as_float(report.get("current_equity"), default=0.0)
            self.notify(
                title=_push_title(
                    priority="P2", category="close", summary="reconcile confirmation"
                ),
                content=_notification_message_zh(
                    trigger="今日收盘后，系统已成功生成最新的模拟持仓与资金快照。",
                    impact="说明核心程序运行正常，且本轮自动对账结果显示账实一致。",
                    action="请打开券商软件做一次快速核对；如总权益偏差超过 0.5%，请及时前往本地监控雷达复核。",
                    details=[
                        f"模拟权益：{current_equity:,.2f}",
                        f"对账状态：{_reconcile_status_zh(status)}",
                        f"差异条数：{_as_int(report.get('mismatch_count'), default=0)}",
                    ],
                    detail_title="今日数据核验摘要",
                ),
                level="info",
            )
        daily_digest = self._notify_daily_digest_if_needed(now=now, reconcile_report=report)
        runtime_archive = self.archive_runtime_history_if_needed(now=now)
        return {
            "reconcile_required": self._state.reconcile_required,
            "broker_snapshot_refresh": broker_snapshot_refresh,
            "report": report,
            "daily_digest": daily_digest,
            "runtime_archive": runtime_archive,
        }

    def _refresh_simulated_broker_snapshot_for_close_reconcile(self) -> dict[str, object]:
        if not self._config.reconcile.auto_refresh_simulated_snapshot_at_close:
            return {"status": "disabled", "reason": "config_disabled"}
        if not self._reconcile_service.should_auto_refresh_simulated_snapshot_at_close():
            return {
                "status": "skipped",
                "reason": "broker_snapshot_source_not_portfolio",
                "broker_snapshot_source": str(self._broker_snapshot_source).strip(),
            }
        trace_id = f"close_reconcile_sim_snapshot_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
        snapshot = self.bootstrap_broker_snapshot_from_portfolio(
            source_trace_id=trace_id,
            allow_empty=True,
        )
        status = str(snapshot.get("status", "")).strip() or "unknown"
        payload = {
            "status": status,
            "source_trace_id": trace_id,
            "portfolio_positions": _as_int(snapshot.get("portfolio_positions"), default=0),
            "broker_positions": _as_int(snapshot.get("broker_positions"), default=0),
            "symbols": list(snapshot.get("symbols", []))
            if isinstance(snapshot.get("symbols"), list)
            else [],
        }
        self._record_audit_event(
            event_type="broker_snapshot_auto_refresh",
            trace_id=trace_id,
            level="info" if status == "ok" else "warn",
            payload=payload,
        )
        return payload

    def _notify_learning_workflow_summary(
        self,
        *,
        proposal_payload: dict[str, object],
        trace_id: str,
    ) -> dict[str, object]:
        workflow = dict(proposal_payload.get("workflow", {}) or {})
        shadow_validation = dict(workflow.get("shadow_validation", {}) or {})
        training = dict(shadow_validation.get("training", {}) or {})
        proposal = dict(proposal_payload.get("proposal", {}) or {})
        auto_promotion = dict(proposal_payload.get("auto_promotion", {}) or {})
        promotion_gate = dict(workflow.get("promotion_gate", {}) or {})
        gate_status = str(
            proposal.get("gate_status", "")
            or promotion_gate.get("status", "")
            or workflow.get("status", "")
        ).strip().lower()
        if bool(auto_promotion.get("auto_release", False)):
            return {
                "sent": False,
                "reason": "release_execute_notification_owned_by_governance",
                "gate_status": gate_status,
            }
        if gate_status != "pass" and bool(auto_promotion.get("rejection_notified", False)):
            return {
                "sent": False,
                "reason": "gate_rejection_already_notified",
                "gate_status": gate_status,
            }

        shadow_model_id = str(proposal_payload.get("shadow_model_id", "")).strip() or str(
            workflow.get("shadow_model_id", "")
        ).strip()
        dataset_manifest_id = str(
            proposal_payload.get("dataset_manifest_id", "")
            or workflow.get("dataset_manifest_id", "")
        ).strip()
        proposal_id = str(proposal.get("proposal_id", "")).strip()
        detail_lines = [
            f"dataset_manifest_id={dataset_manifest_id or '-'}",
            f"shadow_model_id={shadow_model_id or '-'}",
            f"proposal_id={proposal_id or '-'}",
        ]

        if not bool(shadow_validation.get("ok", False)):
            content = _notification_message_zh(
                trigger="训练或 shadow validation 未完成",
                impact="本次学习模型没有形成可进入发布治理的候选结果。",
                action="请优先检查 manifest、训练产物和 shadow validation 错误。",
                details=detail_lines
                + [
                    "errors="
                    + ",".join(
                        _string_list(shadow_validation.get("errors", []))
                        or _string_list(training.get("errors", []))
                        or _string_list(proposal_payload.get("errors", []))
                        or ["unknown"]
                    ),
                ],
                detail_title="训练摘要",
            )
            self.notify(
                title=_push_title(priority="P1", category="ops", summary="learning workflow"),
                content=content,
                level="warn",
                trace_id=trace_id,
            )
            return {"sent": True, "status": "training_failed", "gate_status": gate_status}

        if gate_status != "pass":
            content = _notification_message_zh(
                trigger="训练已完成，但门控未通过",
                impact="本次 shadow 模型不会进入自动发布，当前 champion 继续保持不变。",
                action="请查看门控原因，决定是否人工复核、调参或重新训练。",
                details=detail_lines
                + [
                    f"gate_status={gate_status or 'unknown'}",
                    "reason_codes="
                    + ",".join(
                        _string_list(promotion_gate.get("reason_codes", []))
                        or _string_list(proposal_payload.get("errors", []))
                        or ["unknown"]
                    ),
                ],
                detail_title="训练摘要",
            )
            self.notify(
                title=_push_title(priority="P1", category="ops", summary="learning gate blocked"),
                content=content,
                level="warn",
                trace_id=trace_id,
            )
            return {"sent": True, "status": "gate_blocked", "gate_status": gate_status}

        if not bool(self._config.auto_promotion.notify_on_manual_release_pending):
            return {
                "sent": False,
                "reason": "manual_release_pending_notification_disabled",
                "gate_status": gate_status,
            }

        content = _notification_message_zh(
            trigger="训练和门控已完成，等待人工发布",
            impact="候选模型已经可审阅，但当前 runtime predictor 尚未自动切换。",
            action="请在治理链路中确认 proposal 并决定是否发版。",
            details=detail_lines
            + [
                f"artifact_path={str(training.get('artifact_path', '')).strip() or '-'}",
                "predictor_loaded="
                + str(
                    bool(training.get("predictor_loaded", False))
                    or bool(auto_promotion.get("predictor_loaded", False))
                ).lower(),
            ],
            detail_title="训练摘要",
        )
        self.notify(
            title=_push_title(priority="P2", category="ops", summary="learning release pending"),
            content=content,
            level="info",
            trace_id=trace_id,
        )
        return {"sent": True, "status": "manual_release_pending", "gate_status": gate_status}

    def _notify_daily_digest_if_needed(
        self,
        *,
        now: datetime,
        reconcile_report: dict[str, object],
    ) -> dict[str, object]:
        day_key = now.strftime("%Y%m%d")
        dedup_key = f"daily-digest:{day_key}"
        if self._cache.exists(dedup_key):
            return {
                "sent": False,
                "reason": "dedup",
                "day": day_key,
            }
        digest = self._build_daily_digest_payload(now=now, reconcile_report=reconcile_report)
        summary = digest.get("summary", {})
        if not isinstance(summary, Mapping):
            summary = {}
        research_focus = (
            cast(list[object], digest.get("research_focus", []))
            if isinstance(digest.get("research_focus"), list)
            else []
        )
        research_overview = _coerce_object_mapping(digest.get("research_overview"))
        title = _push_title(priority="P2", category="close", summary="post-market research digest")
        content = self._build_post_market_research_digest_content(
            day_key=day_key,
            summary=summary,
            research_focus=research_focus,
            research_overview=research_overview,
        )
        self.notify(
            title=title,
            content=content,
            level="info",
            trace_id=f"daily-digest-{day_key}",
        )
        self._cache.set(dedup_key, "1", ttl_sec=20 * 3600)
        return {
            "sent": True,
            "day": day_key,
            "summary": dict(summary),
            "recommend_buy_symbols": _string_list(digest.get("recommend_buy_symbols", [])),
            "holding_warn_symbols": _string_list(digest.get("holding_warn_symbols", [])),
            "top_signal_candidates": [
                dict(_coerce_object_mapping(item))
                for item in cast(list[object], digest.get("top_signal_candidates", []))
            ],
            "research_focus": [dict(_coerce_object_mapping(item)) for item in research_focus],
            "research_overview": dict(research_overview),
        }

    def _build_daily_digest_payload(
        self,
        *,
        now: datetime,
        reconcile_report: dict[str, object],
    ) -> dict[str, object]:
        latest_signals = self.latest_signals_snapshot().get("signals", [])
        buy_symbols: list[str] = []
        top_signal_rows: list[dict[str, object]] = []
        if isinstance(latest_signals, list):
            for item in latest_signals:
                if not isinstance(item, Mapping):
                    continue
                row = _coerce_object_mapping(item)
                symbol = str(row.get("symbol", "")).strip()
                if not symbol:
                    continue
                action = str(row.get("action", "")).strip().lower() or "hold"
                if action == "buy" and symbol not in buy_symbols:
                    buy_symbols.append(symbol)
                top_signal_rows.append(
                    {
                        "symbol": symbol,
                        "action": action,
                        "score": round(
                            _as_float(
                                row.get("score"),
                                default=_as_float(row.get("total_score"), default=0.0),
                            ),
                            4,
                        ),
                        "grade": str(row.get("grade", "")).strip().upper(),
                        "reasons": _string_list(row.get("reasons", []))[:3],
                        "decision_trace": _coerce_object_mapping(row.get("decision_trace")),
                    }
                )
                if len(buy_symbols) >= 8 and len(top_signal_rows) >= 24:
                    break
        top_signal_rows.sort(
            key=lambda row: (
                {"buy": 0, "watch": 1, "hold": 2}.get(str(row.get("action", "")).lower(), 3),
                -_as_float(row.get("score"), default=0.0),
                str(row.get("symbol", "")),
            )
        )
        top_signal_candidates: list[dict[str, object]] = []
        seen_candidate_symbols: set[str] = set()
        for row in top_signal_rows:
            symbol = str(row.get("symbol", "")).strip()
            if not symbol or symbol in seen_candidate_symbols:
                continue
            seen_candidate_symbols.add(symbol)
            top_signal_candidates.append(dict(row))
            if len(top_signal_candidates) >= 5:
                break
        holding_alerts = self.holding_alerts(now=now)
        holding_items = holding_alerts.get("items")
        warn_symbols: list[str] = []
        if isinstance(holding_items, list):
            for item in holding_items:
                if not isinstance(item, Mapping):
                    continue
                row = _coerce_object_mapping(item)
                if str(row.get("severity", "")).strip().lower() != "warn":
                    continue
                symbol = str(row.get("symbol", "")).strip()
                if not symbol:
                    continue
                if symbol not in warn_symbols:
                    warn_symbols.append(symbol)
                if len(warn_symbols) >= 8:
                    break
        bias_report = self.execution_bias_report(days=7, limit=200)
        bias_summary = bias_report.get("summary", {})
        if not isinstance(bias_summary, Mapping):
            bias_summary = {}
        holding_summary = holding_alerts.get("summary", {})
        if not isinstance(holding_summary, Mapping):
            holding_summary = {}
        reconcile_status = str(reconcile_report.get("status", "")).strip() or "unknown"
        reconcile_mismatch = _as_int(reconcile_report.get("mismatch_count"), default=0)
        better_price_rate = _as_float(bias_summary.get("better_price_rate"), default=0.0)
        worse_price_rate = _as_float(bias_summary.get("worse_price_rate"), default=0.0)
        pipeline_context = self._latest_pipeline_run_context()
        research_focus = self._build_post_market_research_focus(
            candidates=top_signal_candidates,
            holding_warn_symbols=set(warn_symbols),
        )
        research_overview = self._build_post_market_research_overview(
            research_focus=research_focus,
            recommend_buy_count=len(buy_symbols),
            holding_warn_count=_as_int(holding_summary.get("warn"), default=0),
            reconcile_mismatch_count=reconcile_mismatch,
            pipeline_context=pipeline_context,
        )
        return {
            "date": now.strftime("%Y-%m-%d"),
            "summary": {
                "reconcile_status": reconcile_status,
                "reconcile_mismatch_count": reconcile_mismatch,
                "recommend_buy_count": len(buy_symbols),
                "top_signal_candidate_count": len(top_signal_candidates),
                "holding_warn_count": _as_int(holding_summary.get("warn"), default=0),
                "holding_info_count": _as_int(holding_summary.get("info"), default=0),
                "bias_records_7d": _as_int(bias_report.get("records"), default=0),
                "bias_better_price_rate": better_price_rate,
                "bias_worse_price_rate": worse_price_rate,
                "bias_better_price_rate_7d": better_price_rate,
                "bias_worse_price_rate_7d": worse_price_rate,
                "research_focus_count": len(research_focus),
                "market_regime": str(research_overview.get("market_regime", "")),
                "global_risk_score": _as_float(
                    research_overview.get("global_risk_score"),
                    default=50.0,
                ),
                "market_sentiment_label": str(
                    research_overview.get("sentiment_label", "")
                ).strip(),
                "market_risk_level": str(research_overview.get("risk_level", "")).strip(),
            },
            "recommend_buy_symbols": buy_symbols,
            "holding_warn_symbols": warn_symbols,
            "top_signal_candidates": top_signal_candidates,
            "research_focus": research_focus,
            "research_overview": research_overview,
        }

    def _latest_pipeline_run_context(self) -> dict[str, object]:
        for event in reversed(self._audit_events):
            if str(event.get("event_type", "")).strip().lower() != "pipeline_run":
                continue
            payload = _coerce_object_mapping(event.get("payload"))
            week6_execution = _coerce_object_mapping(payload.get("week6_execution"))
            return {
                "strategy": str(payload.get("strategy", "")).strip() or "trend",
                "regime": str(week6_execution.get("regime", "")).strip(),
                "global_risk_score": _as_float(
                    week6_execution.get("global_risk_score"),
                    default=50.0,
                ),
                "actionable": _as_int(payload.get("actionable"), default=0),
            }
        return {}

    def _build_post_market_research_focus(
        self,
        *,
        candidates: list[dict[str, object]],
        holding_warn_symbols: set[str],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for item in candidates[:3]:
            row = self._build_post_market_research_row(
                candidate=item,
                holding_warn_symbols=holding_warn_symbols,
            )
            if row:
                rows.append(row)
        return rows

    def _build_post_market_research_row(
        self,
        *,
        candidate: Mapping[str, object],
        holding_warn_symbols: set[str],
    ) -> dict[str, object]:
        row = _coerce_object_mapping(candidate)
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            return {}

        action = str(row.get("action", "")).strip().lower() or "hold"
        score = _as_float(row.get("score"), default=0.0)
        grade = str(row.get("grade", "")).strip().upper()
        reasons = _string_list(row.get("reasons", []))

        decision_trace = _coerce_object_mapping(row.get("decision_trace"))
        score_trace = _coerce_object_mapping(decision_trace.get("score"))
        components = _coerce_object_mapping(score_trace.get("components"))
        cross_review_gate = _coerce_object_mapping(decision_trace.get("cross_review_gate"))
        financial_gate = _coerce_object_mapping(decision_trace.get("financial_gate"))

        news_component = _as_float(
            components.get("news"),
            default=_metric_from_reasons(reasons, "news_component", 0.50),
        )
        board_component = _as_float(
            components.get("board"),
            default=_metric_from_reasons(reasons, "board_component", 0.50),
        )
        completion_component = _as_float(
            components.get("completion"),
            default=_metric_from_reasons(reasons, "completion_component", 0.50),
        )

        cross_review_passed = bool(
            cross_review_gate.get(
                "passed",
                not any(
                    reason.lower().startswith(("lgbm<", "xgb<", "meta<", "model_diff>"))
                    for reason in reasons
                ),
            )
        )
        financial_allowed = bool(
            financial_gate.get(
                "allowed",
                not any(
                    reason.lower().startswith(("financial_filter", "financial_penalty"))
                    or reason == "financial_filter_block"
                    for reason in reasons
                ),
            )
        )
        holding_warn = symbol in holding_warn_symbols
        rating = _post_market_research_rating(action=action, grade=grade, score=score)
        confidence = _post_market_research_confidence(
            action=action,
            grade=grade,
            score=score,
            news_component=news_component,
            cross_review_passed=cross_review_passed,
        )

        bull_points: list[str] = []
        bull_tags: list[str] = []
        if grade == "S" or score >= 78.0:
            bull_points.append("综合评分处于高位")
            bull_tags.append("高分信号")
        if news_component >= 0.60:
            bull_points.append("新闻/情绪偏正面")
            bull_tags.append("情绪支持")
        if board_component >= 0.60:
            bull_points.append("趋势结构尚未破坏")
            bull_tags.append("趋势未坏")
        if completion_component >= 0.60:
            bull_points.append("形态完整度较好")
            bull_tags.append("结构完整")
        if action == "buy":
            bull_points.append("当前已进入可执行候选")
            bull_tags.append("可执行候选")
        if not bull_points:
            bull_points.append("具备一定跟踪价值")
            bull_tags.append("跟踪价值")

        bear_points: list[str] = []
        bear_tags: list[str] = []
        if action != "buy":
            bear_points.append("仍需等待更强确认")
            bear_tags.append("等待确认")
        if news_component <= 0.45:
            bear_points.append("新闻催化不足")
            bear_tags.append("催化不足")
        if not cross_review_passed:
            bear_points.append("模型一致性一般")
            bear_tags.append("模型一致性一般")
        if not financial_allowed:
            bear_points.append("财务过滤存在约束")
            bear_tags.append("财务约束")
        if any(reason == "liquidity_failed" for reason in reasons):
            bear_points.append("流动性条件一般")
            bear_tags.append("流动性一般")
        if holding_warn:
            bear_points.append("当前持仓已有预警")
            bear_tags.append("持仓预警")
        if not bear_points:
            bear_points.append("短线追价性价比一般")
            bear_tags.append("追价风险")

        risk_label = "分歧放大风险"
        if holding_warn:
            risk_label = "持仓预警"
        elif not cross_review_passed:
            risk_label = "模型一致性不足"
        elif action == "buy":
            risk_label = "追高回撤风险"
        elif news_component <= 0.45:
            risk_label = "催化不足风险"

        if holding_warn:
            action_advice = "已有持仓预警，优先确认风险是否继续恶化"
        elif action == "buy":
            action_advice = "优先复核，强承接后再考虑执行"
        elif action == "watch":
            action_advice = "继续观察，满足确认条件后再升级"
        else:
            action_advice = "维持观察，不急于介入"

        leading_bull = bull_points[0]
        leading_bear = bear_points[0]
        if action == "buy":
            summary = f"{leading_bull}，可优先复核，但需防范{risk_label}"
        elif action == "watch":
            summary = f"{leading_bull}，但{leading_bear}"
        else:
            summary = f"{leading_bull}，当前更适合观察"

        return {
            "symbol": symbol,
            "display_symbol": symbol,
            "action": action,
            "score": round(score, 2),
            "grade": grade or "-",
            "rating": rating,
            "confidence": confidence,
            "summary": summary,
            "bull_case": "；".join(bull_points[:2]),
            "bear_case": "；".join(bear_points[:2]),
            "action_advice": action_advice,
            "risk_label": risk_label,
            "bull_tags": bull_tags[:3],
            "bear_tags": bear_tags[:3],
            "news_component": round(news_component, 4),
        }

    def _build_post_market_research_overview(
        self,
        *,
        research_focus: list[dict[str, object]],
        recommend_buy_count: int,
        holding_warn_count: int,
        reconcile_mismatch_count: int,
        pipeline_context: Mapping[str, object],
    ) -> dict[str, object]:
        avg_score = 0.0
        avg_news_component = 0.50
        if research_focus:
            avg_score = sum(_as_float(item.get("score"), default=0.0) for item in research_focus)
            avg_score = round(avg_score / len(research_focus), 2)
            avg_news_component = sum(
                _as_float(item.get("news_component"), default=0.50) for item in research_focus
            )
            avg_news_component = round(avg_news_component / len(research_focus), 4)

        regime = str(pipeline_context.get("regime", "")).strip()
        market_regime = _regime_zh(regime) if regime else _post_market_market_style(avg_score)
        global_risk_score = _as_float(pipeline_context.get("global_risk_score"), default=50.0)
        sentiment_label = _post_market_sentiment_label(avg_news_component)
        risk_level = _post_market_risk_level(
            global_risk_score=global_risk_score,
            reconcile_mismatch_count=reconcile_mismatch_count,
            holding_warn_count=holding_warn_count,
            recommend_buy_count=recommend_buy_count,
        )

        focus_count = len(research_focus)
        watch_count = sum(
            1
            for item in research_focus
            if str(item.get("action", "")).strip().lower() in {"watch", "hold"}
        )
        if recommend_buy_count > 0 and risk_level != "高":
            conclusion = (
                f"重点盯盘 {focus_count} 只，观察 {watch_count} 只，"
                "可优先复核高置信度候选"
            )
        else:
            conclusion = (
                f"重点盯盘 {focus_count} 只，观察 {watch_count} 只，"
                "暂无强确定性重仓机会"
            )

        top_bull_point = _dominant_research_phrase(
            [str(tag) for item in research_focus for tag in _string_list(item.get("bull_tags"))],
            mapping=_POST_MARKET_BULL_TAG_LABELS,
            fallback="重点标的仍具跟踪价值",
        )
        top_bear_point = _dominant_research_phrase(
            [str(tag) for item in research_focus for tag in _string_list(item.get("bear_tags"))],
            mapping=_POST_MARKET_BEAR_TAG_LABELS,
            fallback="仍需等待更强确认",
        )
        primary_risk = _dominant_research_phrase(
            [str(item.get("risk_label", "")).strip() for item in research_focus],
            mapping={},
            fallback="分歧放大风险",
        )
        if reconcile_mismatch_count > 0:
            primary_risk = "对账差异尚未完全消化"
        elif holding_warn_count > 0:
            primary_risk = "持仓预警需继续跟踪"

        next_day_watchpoints: list[str] = []
        if research_focus:
            next_day_watchpoints.append("09:30-10:00 是否出现放量承接")
            next_day_watchpoints.append("高分标的能否站稳前高或关键均线")
        if avg_news_component >= 0.55:
            next_day_watchpoints.append("板块龙头是否继续强化并带动联动")
        if holding_warn_count > 0:
            next_day_watchpoints.append("持仓预警标的是否继续恶化")
        if reconcile_mismatch_count > 0:
            next_day_watchpoints.append("对账差异是否已完成复核处理")
        if not next_day_watchpoints:
            next_day_watchpoints.append("观察重点标的是否获得增量资金承接")

        return {
            "conclusion": conclusion,
            "market_regime": market_regime,
            "global_risk_score": round(global_risk_score, 2),
            "sentiment_label": sentiment_label,
            "risk_level": risk_level,
            "top_bull_point": top_bull_point,
            "top_bear_point": top_bear_point,
            "primary_risk": primary_risk,
            "next_day_watchpoints": next_day_watchpoints[:4],
        }

    def _build_post_market_research_digest_content(
        self,
        *,
        day_key: str,
        summary: Mapping[str, object],
        research_focus: list[object],
        research_overview: Mapping[str, object],
    ) -> str:
        detail_lines = [
            f"今日结论={str(research_overview.get('conclusion', '')).strip() or '-'}",
            (
                "市场状态="
                f"{str(research_overview.get('market_regime', '')).strip() or '-'} / "
                f"{str(research_overview.get('sentiment_label', '')).strip() or '-'} / "
                f"风险等级{str(research_overview.get('risk_level', '')).strip() or '-'}"
            ),
            f"建议买入数={_as_int(summary.get('recommend_buy_count'), default=0)}",
            f"持仓预警数={_as_int(summary.get('holding_warn_count'), default=0)}",
            "重点标的=如下",
        ]

        focus_rows = [dict(_coerce_object_mapping(item)) for item in research_focus]
        if focus_rows:
            for item in focus_rows[:3]:
                symbol = (
                    str(item.get("display_symbol", "")).strip()
                    or str(item.get("symbol", "")).strip()
                )
                detail_lines.append(
                    f"{symbol}｜评级={item.get('rating', '-')}｜置信度="
                    f"{_as_float(item.get('confidence'), default=0.0):.2f}｜"
                    f"结论={str(item.get('summary', '')).strip() or '-'}"
                )
        else:
            detail_lines.append("暂无高优先级重点标的")

        detail_lines.extend(
            [
                "多空焦点=如下",
                f"最强看多点={str(research_overview.get('top_bull_point', '')).strip() or '-'}",
                f"最强看空点={str(research_overview.get('top_bear_point', '')).strip() or '-'}",
                f"主要风险={str(research_overview.get('primary_risk', '')).strip() or '-'}",
                "明日观察点=如下",
            ]
        )
        for point in _string_list(research_overview.get("next_day_watchpoints"))[:4]:
            detail_lines.append(point)

        if focus_rows:
            detail_lines.append("重点标的详情=如下")
            for item in focus_rows[:3]:
                symbol = (
                    str(item.get("display_symbol", "")).strip()
                    or str(item.get("symbol", "")).strip()
                )
                detail_lines.append(
                    f"{symbol}：看多={str(item.get('bull_case', '')).strip() or '-'}；"
                    f"看空={str(item.get('bear_case', '')).strip() or '-'}；"
                    f"动作={str(item.get('action_advice', '')).strip() or '-'}"
                )

        return _notification_message_zh(
            trigger=(
                f"{day_key} 收盘后盘后研究摘要已生成，系统已完成今日信号、"
                "持仓预警和重点标的复盘。"
            ),
            impact=(
                "这份摘要用于帮助您快速判断今晚需要重点复核的标的，以及"
                "明日开盘前需要优先关注的方向；不直接等同于交易指令。"
            ),
            action=(
                "请先看重点标的和明日观察点；若发现高置信度机会，再进入 "
                "Dashboard 查看单票完整研究详情。"
            ),
            details=detail_lines,
            detail_title="盘后研究摘要",
        )

    def _job_week4_acceptance(self) -> dict[str, object]:
        return self._acceptance_service._job_week4_acceptance()

    def _job_tdx_offline_sync(self) -> dict[str, object]:
        report = self.run_tdx_offline_sync(
            timestamp=datetime.now(),
            notify_enabled=None,
            force=False,
            source_trace_id="scheduler-tdx-sync",
        )
        return {"report": report}

    def _notify_market_warehouse_scheduler_issue_if_needed(
        self,
        *,
        payload: Mapping[str, object],
    ) -> None:
        status = str(payload.get("status", "")).strip().lower()
        if status != "already_running":
            return
        reason = str(payload.get("reason", "")).strip() or "already_running"
        source_trace_id = str(payload.get("source_trace_id", "")).strip() or "scheduler-market-warehouse"
        lock_payload = payload.get("lock")
        lock = dict(lock_payload) if isinstance(lock_payload, Mapping) else {}
        lock_trace_id = str(lock.get("trace_id", "")).strip()
        age_sec = _as_int(lock.get("age_sec"), default=0)
        thread_name = str(payload.get("thread_name", "")).strip()
        date_key = (str(payload.get("dispatch_timestamp", "")).strip() or datetime.now().isoformat())[:10]
        dedup_suffix = lock_trace_id or thread_name or reason
        detail_parts = [
            "事件：夜间 market_warehouse_sync 调度未拿到执行权。",
            f"原因：{reason}。",
        ]
        if lock_trace_id:
            detail_parts.append(f"当前持锁 trace_id：{lock_trace_id}。")
        if age_sec > 0:
            detail_parts.append(f"锁已持续约 {age_sec}s。")
        if thread_name:
            detail_parts.append(f"活跃 worker：{thread_name}。")
        detail_parts.append("影响：本次调度未启动新同步，调度器会在后续轮询继续重试。")
        self._notify_if_changed(
            dedup_key=(
                f"market_warehouse_scheduler_issue:{date_key}:{reason}:{dedup_suffix or 'unknown'}"
            ),
            dedup_value=json.dumps(
                {
                    "reason": reason,
                    "lock_trace_id": lock_trace_id,
                    "age_sec": age_sec,
                    "thread_name": thread_name,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            title="【重要】【运维】夜间 market_warehouse_sync 未取得执行权",
            content="".join(detail_parts),
            level="warn",
            trace_id=source_trace_id,
            ttl_sec=6 * 3600,
        )

    def _notify_post_market_warehouse_followup_issue_if_needed(
        self,
        *,
        report: Mapping[str, object],
        followup: Mapping[str, object],
    ) -> None:
        ok = bool(followup.get("ok", False))
        skipped = bool(followup.get("skipped", False))
        reason = str(followup.get("reason", "")).strip()
        benign_skip_reasons = {
            "market_warehouse_sync_in_progress",
            "post_followup_disabled",
        }
        if ok and not skipped:
            return
        if skipped and reason in benign_skip_reasons:
            return
        effective_trace_id = (
            str(followup.get("effective_market_warehouse_trace_id", "")).strip()
            or str(followup.get("market_warehouse_trace_id", "")).strip()
            or str(report.get("trace_id", "")).strip()
            or "scheduler-market-warehouse"
        )
        gate = followup.get("gate")
        gate_reason = (
            str(gate.get("reason", "")).strip() if isinstance(gate, Mapping) else ""
        )
        error_payload = followup.get("error")
        error_message = (
            str(error_payload.get("message", "")).strip()
            if isinstance(error_payload, Mapping)
            else ""
        )
        summary = reason or gate_reason or error_message or "followup_not_completed"
        status_text = "跳过" if skipped else "失败"
        content = (
            f"事件：market_warehouse_sync 后续流程{status_text}。"
            f"trace_id：{effective_trace_id}。"
            f"原因：{summary}。"
            "影响：盘后补扫/回填/训练链路可能未完成，请在运行阶段页面检查并按需补更。"
        )
        self._notify_if_changed(
            dedup_key=f"market_warehouse_followup_issue:{effective_trace_id}:{summary}",
            dedup_value=json.dumps(
                {
                    "trace_id": effective_trace_id,
                    "reason": reason,
                    "gate_reason": gate_reason,
                    "error_message": error_message,
                    "skipped": skipped,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            title="【重要】【运维】market_warehouse 后续流程异常",
            content=content,
            level="warn",
            trace_id=effective_trace_id,
            ttl_sec=24 * 3600,
        )

    def _execute_market_warehouse_scheduler_job(
        self,
        *,
        sync_timestamp: datetime,
        scheduler_lock_path: Path,
        scheduler_lock_owner_token: str,
    ) -> dict[str, object]:
        report = self.run_market_warehouse_sync(
            timestamp=sync_timestamp,
            notify_enabled=None,
            force=False,
            source_trace_id="scheduler-market-warehouse",
            scheduler_lock_path=scheduler_lock_path,
            scheduler_lock_owner_token=scheduler_lock_owner_token,
        )
        if str(report.get("reason", "")).strip() == "market_warehouse_sync_in_progress":
            self._notify_market_warehouse_scheduler_issue_if_needed(
                payload={
                    "status": "already_running",
                    "reason": "market_warehouse_sync_in_progress",
                    "source_trace_id": "scheduler-market-warehouse",
                    "dispatch_timestamp": sync_timestamp.isoformat(),
                    "lock": report.get("active_lock", {}),
                }
            )
            followup = {
                "ok": True,
                "skipped": True,
                "reason": "market_warehouse_sync_in_progress",
                "trigger": "scheduler_market_warehouse",
            }
        else:
            followup = self.run_post_market_warehouse_followup(
                market_warehouse_report=report,
                trigger="scheduler_market_warehouse",
                timestamp=datetime.now(),
            )
            if isinstance(followup, Mapping):
                self._notify_post_market_warehouse_followup_issue_if_needed(
                    report=report,
                    followup=followup,
                )
        return {"report": report, "followup": followup}

    def _run_market_warehouse_scheduler_job_worker(
        self,
        *,
        sync_timestamp: datetime,
        scheduler_lock_path: Path,
        scheduler_lock_owner_token: str,
    ) -> None:
        worker = current_thread()
        try:
            self._execute_market_warehouse_scheduler_job(
                sync_timestamp=sync_timestamp,
                scheduler_lock_path=scheduler_lock_path,
                scheduler_lock_owner_token=scheduler_lock_owner_token,
            )
        finally:
            with self._market_warehouse_scheduler_launch_lock:
                if self._market_warehouse_scheduler_thread is worker:
                    self._market_warehouse_scheduler_thread = None

    def _launch_market_warehouse_scheduler_job(self) -> dict[str, object]:
        sync_timestamp = datetime.now()
        source_trace_id = "scheduler-market-warehouse"
        with self._market_warehouse_scheduler_launch_lock:
            active_worker = self._market_warehouse_scheduler_thread
            if active_worker is not None and active_worker.is_alive():
                payload = {
                    "status": "already_running",
                    "launched": False,
                    "reason": "market_warehouse_scheduler_worker_active",
                    "dispatch_timestamp": sync_timestamp.isoformat(),
                    "source_trace_id": source_trace_id,
                    "thread_name": active_worker.name,
                    "_scheduler_ran": False,
                    "_scheduler_detail": "already_running",
                }
                self._record_audit_event(
                    event_type="market_warehouse_scheduler_dispatch",
                    trace_id=source_trace_id,
                    level="info",
                    payload=payload,
                )
                self._notify_market_warehouse_scheduler_issue_if_needed(payload=payload)
                return payload
            lock_path, lock_owner_token, active_lock = (
                self._market_sync_service._acquire_market_warehouse_sync_lock(
                    timestamp=sync_timestamp,
                    source_trace_id=source_trace_id,
                    force=False,
                )
            )
            if lock_path is None:
                payload = {
                    "status": "already_running",
                    "launched": False,
                    "reason": "market_warehouse_sync_lock_active",
                    "dispatch_timestamp": sync_timestamp.isoformat(),
                    "source_trace_id": source_trace_id,
                    "lock": active_lock,
                    "_scheduler_ran": False,
                    "_scheduler_detail": "already_running",
                }
                self._record_audit_event(
                    event_type="market_warehouse_scheduler_dispatch",
                    trace_id=source_trace_id,
                    level="info",
                    payload=payload,
                )
                self._notify_market_warehouse_scheduler_issue_if_needed(payload=payload)
                return payload
            worker = Thread(
                target=self._run_market_warehouse_scheduler_job_worker,
                kwargs={
                    "sync_timestamp": sync_timestamp,
                    "scheduler_lock_path": lock_path,
                    "scheduler_lock_owner_token": lock_owner_token,
                },
                name=f"market-warehouse-scheduler-{sync_timestamp.strftime('%Y%m%d%H%M%S')}",
                daemon=True,
            )
            self._market_warehouse_scheduler_thread = worker
            try:
                worker.start()
            except Exception:
                self._market_warehouse_scheduler_thread = None
                self._market_sync_service._release_market_warehouse_sync_lock(
                    lock_path=lock_path,
                    owner_token=lock_owner_token,
                )
                raise

        payload = {
            "status": "launched",
            "launched": True,
            "dispatch_timestamp": sync_timestamp.isoformat(),
            "source_trace_id": source_trace_id,
            "thread_name": worker.name,
            "lock_path": self._to_evolution_relative(lock_path),
            "_scheduler_ran": True,
            "_scheduler_detail": "launched",
        }
        self._record_audit_event(
            event_type="market_warehouse_scheduler_dispatch",
            trace_id=source_trace_id,
            level="info",
            payload=payload,
        )
        return payload

    def _job_market_warehouse_sync(self) -> dict[str, object]:
        return self._launch_market_warehouse_scheduler_job()

    def _week5_live_runtime_backpressure(self, *, now: datetime) -> dict[str, object] | None:
        if not bool(self._config.week5.live_runtime_backpressure_enabled):
            return None
        threshold_ms = max(
            1,
            _as_int(self._config.week5.live_runtime_backpressure_threshold_ms, default=60000),
        )
        cooldown_min = max(
            1,
            _as_int(self._config.week5.live_runtime_backpressure_cooldown_min, default=5),
        )
        latest = self._latest_run_summary_for_job("week5_live_runtime")
        if not latest:
            return None
        latest_duration_ms = _as_int(latest.get("duration_ms"), default=0)
        if latest_duration_ms < threshold_ms:
            return None
        latest_timestamp = _parse_iso_datetime(str(latest.get("timestamp", "")).strip())
        if latest_timestamp is None or latest_timestamp.date() != now.date():
            return None
        latest_finished_at = latest_timestamp + timedelta(milliseconds=latest_duration_ms)
        elapsed = now - latest_finished_at
        if elapsed.total_seconds() > 0 and elapsed > timedelta(minutes=cooldown_min):
            return None

        payload: dict[str, object] = {
            "status": "skipped",
            "reason": "sla_backpressure",
            "job_name": "week5_live_runtime",
            "previous_duration_ms": latest_duration_ms,
            "threshold_ms": threshold_ms,
            "cooldown_min": cooldown_min,
            "previous_timestamp": latest_timestamp.isoformat(),
            "previous_finished_at": latest_finished_at.isoformat(),
            "dispatch_timestamp": now.isoformat(),
            "_scheduler_ran": True,
            "_scheduler_detail": "skipped_sla_backpressure",
        }
        self._record_audit_event(
            event_type="week5_live_runtime_skipped",
            level="warn",
            payload=payload,
        )
        return payload

    def _latest_run_summary_for_job(self, job_name: str) -> dict[str, object] | None:
        normalized_job = job_name.strip()
        if not normalized_job:
            return None
        for item in reversed(self._run_summaries):
            if not isinstance(item, dict):
                continue
            if str(item.get("job_name", "")).strip() == normalized_job:
                return item
        return None

    def _week5_live_runtime_symbols(self) -> tuple[list[str], dict[str, object]]:
        configured_limit = max(
            1,
            _as_int(self._config.week5.live_runtime_max_symbols, default=8),
        )
        limit, cap_status = self._week5_live_runtime_effective_symbol_limit(
            configured_limit=configured_limit,
        )
        watchlist = [
            symbol
            for symbol in (_normalize_a_share_symbol(item) for item in self._state.watchlist)
            if symbol
        ]
        selected: list[str] = []
        source_counts: dict[str, int] = {}

        def add_symbol(value: object, source: str) -> None:
            symbol = _normalize_a_share_symbol(value)
            if not symbol or symbol in selected or len(selected) >= limit:
                return
            selected.append(symbol)
            source_counts[source] = source_counts.get(source, 0) + 1

        for position in self._portfolio.positions():
            if isinstance(position, dict):
                add_symbol(position.get("symbol"), "position")

        latest_week5 = (
            self._last_week5_scan_report if isinstance(self._last_week5_scan_report, dict) else {}
        )
        self._add_symbols_from_week5_report(
            latest_week5,
            add_symbol=add_symbol,
            source_prefix="week5",
        )

        latest_radar = (
            self._last_week5_market_radar_report
            if isinstance(self._last_week5_market_radar_report, dict)
            else {}
        )
        self._add_symbols_from_week5_report(
            latest_radar,
            add_symbol=add_symbol,
            source_prefix="radar",
        )

        for item in _coerce_mapping_list(self._market_radar_review_pool):
            add_symbol(item.get("symbol"), "radar_review_pool")

        for symbol in watchlist:
            add_symbol(symbol, "watchlist")

        return selected, {
            "limit": limit,
            "configured_limit": configured_limit,
            "selected_count": len(selected),
            "watchlist_count": len(watchlist),
            "source_counts": source_counts,
            "auto_cap": cap_status,
        }

    def _week5_live_runtime_effective_symbol_limit(
        self,
        *,
        configured_limit: int,
    ) -> tuple[int, dict[str, object]]:
        base_limit = max(1, configured_limit)
        min_symbols = min(
            base_limit,
            max(
                1,
                _as_int(self._config.week5.live_runtime_auto_cap_min_symbols, default=6),
            ),
        )
        cap_status: dict[str, object] = {
            "enabled": bool(self._config.week5.live_runtime_auto_cap_enabled),
            "applied": False,
            "configured_limit": base_limit,
            "effective_limit": base_limit,
            "min_symbols": min_symbols,
            "observed_runs": 0,
        }
        if not bool(self._config.week5.live_runtime_auto_cap_enabled):
            return base_limit, cap_status
        if base_limit <= min_symbols:
            return base_limit, cap_status

        window_runs = max(
            1,
            _as_int(self._config.week5.live_runtime_auto_cap_window_runs, default=5),
        )
        recent_runs: list[dict[str, object]] = []
        for item in reversed(self._latency_history_ms):
            if not isinstance(item, dict):
                continue
            if not _sla_entry_matches_job_scope(item, "live_runtime"):
                continue
            symbol_count = _as_int(item.get("symbol_count"), default=0)
            duration_ms = _as_int(item.get("duration_ms"), default=0)
            if symbol_count <= 0 or duration_ms <= 0:
                continue
            recent_runs.append(item)
            if len(recent_runs) >= window_runs:
                break
        cap_status["observed_runs"] = len(recent_runs)
        if not recent_runs:
            return base_limit, cap_status

        per_symbol_ms = sorted(
            _as_int(item.get("duration_ms"), default=0)
            / max(1, _as_int(item.get("symbol_count"), default=1))
            for item in recent_runs
        )
        observed_per_symbol_ms = per_symbol_ms[int((len(per_symbol_ms) - 1) * 0.8)]
        threshold_ms = max(
            1,
            _as_int(self._config.week5.live_runtime_backpressure_threshold_ms, default=60000),
        )
        safety_ratio = min(
            1.0,
            max(
                0.1,
                _as_float(self._config.week5.live_runtime_auto_cap_safety_ratio, default=0.75),
            ),
        )
        budget_ms = threshold_ms * safety_ratio
        recommended_limit = max(
            min_symbols,
            min(base_limit, int(budget_ms // max(1.0, observed_per_symbol_ms))),
        )
        cap_status.update(
            {
                "applied": recommended_limit < base_limit,
                "effective_limit": recommended_limit,
                "observed_per_symbol_ms": round(observed_per_symbol_ms, 2),
                "budget_ms": round(budget_ms, 2),
                "threshold_ms": threshold_ms,
                "safety_ratio": safety_ratio,
            }
        )
        return recommended_limit, cap_status

    def _add_symbols_from_week5_report(
        self,
        report: dict[str, object],
        *,
        add_symbol: Callable[[object, str], None],
        source_prefix: str,
    ) -> None:
        for key in ("selected_symbols", "symbols", "watchlist"):
            raw_values = report.get(key)
            if isinstance(raw_values, list):
                for value in raw_values:
                    add_symbol(value, f"{source_prefix}_{key}")

        for section_name in ("first_board", "market_radar", "market_radar_review"):
            section = report.get(section_name)
            if not isinstance(section, dict):
                continue
            for key in (
                "leaders",
                "candidates",
                "top_hits",
                "hits",
                "review_pool",
                "symbols",
            ):
                raw_values = section.get(key)
                if not isinstance(raw_values, list):
                    continue
                for value in raw_values:
                    if isinstance(value, dict):
                        add_symbol(value.get("symbol"), f"{source_prefix}_{section_name}_{key}")
                    else:
                        add_symbol(value, f"{source_prefix}_{section_name}_{key}")

    def _job_week5_scan(self) -> dict[str, object]:
        symbols = [str(item).strip() for item in self._state.watchlist if str(item).strip()]
        current = self._job_now()
        report = self.run_week5_scan(
            symbols=symbols if symbols else None,
            timestamp=current,
            notify_enabled=self._config.week5.auto_notify,
            sync_watchlist=True,
            sync_reason="scheduler_week5",
        )
        return {"report": report}

    def _job_week5_market_radar(self) -> dict[str, object]:
        current = self._job_now()
        report = self.run_week5_market_radar(
            timestamp=current,
            notify_enabled=self._config.week5.market_radar_notify,
        )
        return {"report": report}

    def _job_week5_live_runtime(self) -> dict[str, object]:
        now = self._job_now()
        backpressure = self._week5_live_runtime_backpressure(now=now)
        if backpressure is not None:
            return backpressure
        symbols, selection = self._week5_live_runtime_symbols()
        if not symbols:
            return {
                "status": "skipped",
                "reason": "empty_live_runtime_symbols",
                "selection": selection,
                "_scheduler_ran": True,
                "_scheduler_detail": "skipped_empty_symbols",
            }
        report = self.run_pipeline(
            symbols=symbols,
            strategy="trend",
            current_equity=self._state.current_equity,
            use_live_runtime=True,
            dry_run_execution=True,
            notify_enabled=False,
            job_name="week5_live_runtime",
        )
        trace_id = str(report.get("trace_id", ""))
        return {
            "symbol_count": len(symbols),
            "selection": selection,
            "trace_id": trace_id,
            "report": report,
        }

    def _job_week6_daily(self) -> dict[str, object]:
        current = self._job_now()
        report = self.run_week6_analysis(
            symbols=list(self._state.watchlist),
            timestamp=current,
            notify_enabled=self._config.week6.auto_notify,
        )
        return {"report": report}

    def _job_week6_data_prewarm(self) -> dict[str, object]:
        current = self._job_now()
        report = self.run_week6_data_prewarm(
            symbols=list(self._state.watchlist),
            lookback_days=self._config.week6.data_prewarm_lookback_days,
            notify_enabled=self._config.week6.data_quality_notify,
            source_trace_id="scheduler-week6-data-prewarm",
            timestamp=current,
        )
        return {"report": report}

    def _job_evolution_offhours(self, now: datetime | None = None) -> dict[str, object]:
        return self._evolution_core_service._job_evolution_offhours(now=now)

    def _job_evolution_m3_maintenance(self) -> dict[str, object]:
        return self._evolution_core_service._job_evolution_m3_maintenance()

    def _job_evolution_release_confirmation_watchdog(self) -> dict[str, object]:
        return self._evolution_release_service._job_evolution_release_confirmation_watchdog()

    def _job_idle_queue_tick(self) -> dict[str, object]:
        return self._idle_queue_service._job_idle_queue_tick()

    def _job_week7_cloud_backup_watchdog(self) -> dict[str, object]:
        return self.run_cloud_backup_check(
            now=datetime.now(),
            source_trace_id="week7-cloud-watchdog",
        )

    def _build_evolution_m9_records(self, symbols: list[str]) -> list[dict[str, object]]:
        return self._evolution_core_service._build_evolution_m9_records(symbols)

    def _resolve_adv60(self, bars: pd.DataFrame) -> tuple[float, bool]:
        return self._evolution_core_service._resolve_adv60(bars)

    def _classify_liquidity_tier(self, *, adv60: float, adv60_available: bool) -> tuple[str, bool]:
        return self._evolution_core_service._classify_liquidity_tier(
            adv60=adv60,
            adv60_available=adv60_available,
        )

    def _prepare_evolution_loader_inputs(
        self,
        symbols: list[str],
        now: datetime,
    ) -> dict[str, object]:
        return self._evolution_core_service._prepare_evolution_loader_inputs(symbols, now)

    def _ensure_evolution_loader_artifact(
        self,
        *,
        symbols: list[str],
        now: datetime,
        config_attr: str,
        module_name: str,
        default_filename: str,
        builder: Callable[[list[str]], list[dict[str, object]]],
    ) -> dict[str, object]:
        return self._evolution_core_service._ensure_evolution_loader_artifact(
            symbols=symbols,
            now=now,
            config_attr=config_attr,
            module_name=module_name,
            default_filename=default_filename,
            builder=builder,
        )

    def _build_evolution_m5_label_records(self, symbols: list[str]) -> list[dict[str, object]]:
        return self._evolution_core_service._build_evolution_m5_label_records(symbols)

    def _load_m7_news_records_from_local_sources(
        self,
        *,
        symbol_set: set[str],
    ) -> list[dict[str, object]]:
        return self._evolution_core_service._load_m7_news_records_from_local_sources(
            symbol_set=symbol_set
        )

    def _build_evolution_m7_news_records(self, symbols: list[str]) -> list[dict[str, object]]:
        return self._evolution_core_service._build_evolution_m7_news_records(symbols)

    def _build_evolution_m11_shadow_results(self, symbols: list[str]) -> list[dict[str, object]]:
        return self._evolution_core_service._build_evolution_m11_shadow_results(symbols)

    def _is_evolution_artifact_fresh(self, path: Path, now: datetime) -> bool:
        return self._evolution_core_service._is_evolution_artifact_fresh(path, now)

    def _count_evolution_records(self, path: Path) -> int:
        return self._evolution_core_service._count_evolution_records(path)

    def _write_jsonl_atomic(self, path: Path, records: list[dict[str, object]]) -> None:
        self._evolution_core_service._write_jsonl_atomic(path, records)

    def _write_json_atomic(self, path: Path, payload: Mapping[str, object]) -> None:
        self._evolution_core_service._write_json_atomic(path, payload)

    def _to_evolution_relative(self, path: Path) -> str:
        return self._evolution_core_service._to_evolution_relative(path)

    def _resolve_evolution_path(self, raw_path: str) -> Path:
        return self._evolution_core_service._resolve_evolution_path(raw_path)

    def _persist_evolution_report(self, report: dict[str, object]) -> None:
        self._evolution_core_service._persist_evolution_report(report)

    def _load_evolution_reports_from_disk(self, cutoff_ts: float) -> list[dict[str, object]]:
        return self._evolution_core_service._load_evolution_reports_from_disk(cutoff_ts)

    def _collect_global_market_snapshot(self, source_trace_id: str = "") -> dict[str, object]:
        proxies = {
            "us_index_change_pct": "513500",
            "a50_change_pct": "510050",
            "usd_cnh_change_pct": "USDCNH",
            "commodity_change_pct": "518880",
        }
        snapshot: dict[str, object] = {}
        errors: list[str] = []
        for key, symbol in proxies.items():
            value = self._daily_return_from_symbol(symbol=symbol, lookback_days=40)
            if value is None:
                snapshot[key] = 0.0
                errors.append(f"{key}:{symbol}")
                continue
            snapshot[key] = value

        correlation = self._estimate_a_share_correlation(
            a_share_proxy="000300",
            a50_proxy=proxies["a50_change_pct"],
            lookback_days=40,
        )
        if correlation is None:
            correlation = 0.60
            errors.append("a_share_correlation")
        snapshot["a_share_correlation"] = round(correlation, 4)
        result = self.update_global_market_snapshot(
            snapshot=snapshot,
            source_trace_id=source_trace_id,
        )
        return {
            "snapshot": result.get("snapshot", {}),
            "history_count": result.get("history_count", 0),
            "errors": errors,
            "proxies": proxies,
        }

    def _daily_return_from_symbol(self, symbol: str, lookback_days: int) -> float | None:
        try:
            bars = self._provider.fetch_daily_bars(
                symbol=symbol,
                lookback_days=max(5, lookback_days),
            )
        except Exception:
            return None
        if len(bars) < 2:
            return None
        latest = _as_float(bars.iloc[-1].get("close"), default=0.0)
        previous = _as_float(bars.iloc[-2].get("close"), default=0.0)
        if latest <= 0 or previous <= 0:
            return None
        return round(latest / previous - 1.0, 4)

    def _estimate_a_share_correlation(
        self,
        a_share_proxy: str,
        a50_proxy: str,
        lookback_days: int,
    ) -> float | None:
        try:
            a_share_bars = self._provider.fetch_daily_bars(
                symbol=a_share_proxy,
                lookback_days=max(25, lookback_days),
            )
            a50_bars = self._provider.fetch_daily_bars(
                symbol=a50_proxy,
                lookback_days=max(25, lookback_days),
            )
        except Exception:
            return None
        if len(a_share_bars) < 10 or len(a50_bars) < 10:
            return None

        a_share_ret = a_share_bars["close"].astype(float).pct_change().dropna()
        a50_ret = a50_bars["close"].astype(float).pct_change().dropna()
        aligned = pd.concat([a_share_ret, a50_ret], axis=1, join="inner").dropna()
        if len(aligned) < 10:
            return None
        corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
        if corr != corr:  # NaN check
            return None
        return _clamp(corr, -1.0, 1.0)

    def _build_first_board_candidate(
        self,
        symbol: str,
        bars: Any,
        signal: dict[str, object],
    ) -> dict[str, object] | None:
        latest = bars.iloc[-1]
        prev = bars.iloc[-2]
        prev_close = _as_float(prev.get("close"), default=0.0)
        close = _as_float(latest.get("close"), default=0.0)
        if prev_close <= 0 or close <= 0:
            return None

        limit_up_pct = max(0.0, self._config.week5.first_board_limit_up_pct)
        pct_change = close / prev_close - 1.0
        if pct_change < limit_up_pct:
            return None

        cap = max(1, self._config.week5.consecutive_limit_up_cap)
        streak = _limit_up_streak(bars=bars, threshold=limit_up_pct, cap=cap)
        turnover_ratio = _recent_ratio(bars=bars, column="turnover", lookback=5)
        score = _as_float(signal.get("score"), default=0.0)
        leader_score = _clamp(
            value=score * 0.65 + min(streak, cap) * 7.0 + min(turnover_ratio, 3.0) * 10.0,
            low=0.0,
            high=100.0,
        )
        board_stage = "first_board" if streak <= 1 else f"{streak}_board"
        return {
            "symbol": symbol,
            "board_stage": board_stage,
            "limit_up_pct": round(pct_change, 4),
            "consecutive_limit_up": streak,
            "turnover_ratio_5d": round(turnover_ratio, 4),
            "score": round(score, 2),
            "grade": str(signal.get("grade", "")),
            "action": str(signal.get("action", "")),
            "suggested_position": _as_float(signal.get("target_position"), default=0.0),
            "leader_score": round(leader_score, 2),
            "reasons": _coerce_text_list(signal.get("reasons"))[:8],
            "decision_trace": _coerce_object_mapping(signal.get("decision_trace")),
        }

    def _detect_symbol_anomaly(
        self,
        symbol: str,
        bars: Any,
    ) -> dict[str, object] | None:
        latest = bars.iloc[-1]
        prev = bars.iloc[-2]

        prev_close = _as_float(prev.get("close"), default=0.0)
        latest_open = _as_float(latest.get("open"), default=0.0)
        latest_high = _as_float(latest.get("high"), default=0.0)
        latest_low = _as_float(latest.get("low"), default=0.0)
        latest_close = _as_float(latest.get("close"), default=0.0)
        if prev_close <= 0 or latest_close <= 0:
            return None

        gap_pct = abs(latest_open / prev_close - 1.0)
        volume_ratio = _recent_ratio(bars=bars, column="volume", lookback=5)
        upper_shadow_pct = max(0.0, latest_high - max(latest_open, latest_close)) / latest_close
        lower_shadow_pct = max(0.0, min(latest_open, latest_close) - latest_low) / latest_close

        flags: list[str] = []
        if gap_pct >= self._config.week5.anomaly_gap_pct:
            flags.append("gap")
        if volume_ratio >= self._config.week5.anomaly_volume_ratio:
            flags.append("volume_spike")
        if upper_shadow_pct >= self._config.week5.anomaly_shadow_pct:
            flags.append("upper_shadow")
        if lower_shadow_pct >= self._config.week5.anomaly_shadow_pct:
            flags.append("lower_shadow")
        if not flags:
            return None
        return {
            "symbol": symbol,
            "types": flags,
            "gap_pct": round(gap_pct, 4),
            "volume_ratio_5d": round(volume_ratio, 4),
            "upper_shadow_pct": round(upper_shadow_pct, 4),
            "lower_shadow_pct": round(lower_shadow_pct, 4),
        }

    def _evaluate_empty_signal(
        self,
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

        no_buy_streak = 0
        for item in reversed(self._run_summaries):
            actionable = _as_int(item.get("actionable"), default=0)
            if actionable <= 0:
                no_buy_streak += 1
            else:
                break

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
        monster_report: dict[str, object],
        empty_signal: dict[str, object],
    ) -> dict[str, object]:
        positions = self._portfolio.positions()
        monster_positions = [
            _as_float(item.get("target_position"), default=0.0)
            for item in positions
            if str(item.get("strategy", "")).strip().lower() == "monster"
        ]
        total_monster_position = sum(monster_positions)
        max_monster_position = max(monster_positions) if monster_positions else 0.0
        sentiment_score = self._estimate_sentiment(monster_report=monster_report)

        reasons: list[str] = []
        soft_reasons: list[str] = []
        if total_monster_position >= self._config.monster_risk.max_total_position:
            reasons.append("max_total_position")
        if max_monster_position >= self._config.monster_risk.max_stock_position:
            reasons.append("max_stock_position")
        if sentiment_score < self._config.monster_risk.disable_if_sentiment_below:
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
        }

    def _estimate_sentiment(self, monster_report: dict[str, object]) -> float:
        raw_signals = monster_report.get("signals")
        scores: list[float] = []
        if isinstance(raw_signals, list):
            for item in raw_signals:
                if not isinstance(item, dict):
                    continue
                scores.append(_as_float(item.get("score"), default=0.0))
        base = (sum(scores) / len(scores)) if scores else 50.0

        risk = monster_report.get("risk", {})
        risk_action = ""
        if isinstance(risk, dict):
            risk_action = str(risk.get("action", ""))
        if risk_action in {"freeze", "degraded"}:
            base -= 15.0
        return _clamp(value=base, low=0.0, high=100.0)

    @staticmethod
    def _build_cache(config: StockAnalyzerConfig) -> CacheStore:
        if config.cache.backend == "redis" and config.cache.redis_url:
            try:
                return RedisCache(redis_url=config.cache.redis_url)
            except Exception:
                return InMemoryCache()
        return InMemoryCache()

    def _apply_command_side_effect(
        self,
        action: str,
        command_payload: dict[str, Any],
        timestamp: datetime,
        trace_id: str,
    ) -> dict[str, object] | None:
        if action == "CLOSE_POSITION":
            symbol = str(command_payload.get("symbol", "")).strip()
            close_fill = _parse_manual_close_fill_from_command_payload(
                payload=command_payload,
                fallback_time=timestamp,
            )
            closed = self._portfolio.close_position(
                symbol=symbol,
                timestamp=timestamp,
                trace_id=trace_id,
                reason="manual_close_command",
                manual_fill=close_fill,
            )
            if closed:
                trade_reference = self._trade_reference_for_closed_position(symbol)
                exit_price = _as_float(trade_reference.get("exit_price"), default=0.0)
                entry_price = _as_float(trade_reference.get("entry_price"), default=0.0)
                realized = (
                    round(exit_price / entry_price - 1.0, 6)
                    if exit_price > 0 and entry_price > 0
                    else None
                )
                self._set_recommendation_status(
                    symbol=symbol,
                    status="closed",
                    strategy="manual",
                    timestamp=timestamp,
                    source="manual_close_command",
                    trace_id=trace_id,
                    extra={
                        **trade_reference,
                        "realized_return_pct": realized,
                        "current_return_pct": realized,
                        "outcome_status": "win"
                        if realized is not None and realized > 0
                        else "loss"
                        if realized is not None
                        else "unknown",
                    },
                )
            response = {"action": action, "symbol": symbol, "closed": closed}
            if close_fill is not None:
                response["close_fill"] = close_fill
            return response

        if action == "CLOSE_ALL_POSITIONS":
            symbols = sorted(self._portfolio.position_map().keys())
            closed_symbols: list[str] = []
            for symbol in symbols:
                closed = self._portfolio.close_position(
                    symbol=symbol,
                    timestamp=timestamp,
                    trace_id=trace_id,
                    reason="manual_close_all_positions_command",
                )
                if closed:
                    closed_symbols.append(symbol)
                    trade_reference = self._trade_reference_for_closed_position(symbol)
                    exit_price = _as_float(trade_reference.get("exit_price"), default=0.0)
                    entry_price = _as_float(trade_reference.get("entry_price"), default=0.0)
                    realized = (
                        round(exit_price / entry_price - 1.0, 6)
                        if exit_price > 0 and entry_price > 0
                        else None
                    )
                    self._set_recommendation_status(
                        symbol=symbol,
                        status="closed",
                        strategy="manual",
                        timestamp=timestamp,
                        source="manual_close_all_positions_command",
                        trace_id=trace_id,
                        extra={
                            **trade_reference,
                            "realized_return_pct": realized,
                            "current_return_pct": realized,
                            "outcome_status": "win"
                            if realized is not None and realized > 0
                            else "loss"
                            if realized is not None
                            else "unknown",
                        },
                    )
            return {
                "action": action,
                "closed_count": len(closed_symbols),
                "closed_symbols": closed_symbols,
            }

        if action == "SET_POSITION":
            symbol = str(command_payload.get("symbol", "")).strip()
            strategy = str(command_payload.get("strategy", "manual")).strip() or "manual"
            target_position = float(command_payload.get("target_position", 0.0))
            recommendation_reference = self._recommendation_reference_for_symbol(
                symbol=symbol,
                timestamp=timestamp,
                recommendation_id=str(command_payload.get("recommendation_id", "")).strip(),
            )
            manual_fill = _parse_manual_fill_from_command_payload(
                payload=command_payload,
                fallback_time=timestamp,
            )
            status = self._portfolio.set_manual_position(
                symbol=symbol,
                strategy=strategy,
                target_position=target_position,
                timestamp=timestamp,
                trace_id=trace_id,
                reason="manual_set_position_command",
                manual_fill=manual_fill,
            )
            response = {
                "action": action,
                "symbol": symbol,
                "status": status,
                "target_position": target_position,
            }
            if manual_fill is not None:
                response["manual_fill"] = manual_fill
            if recommendation_reference is not None:
                response["recommendation_reference"] = recommendation_reference
            if status in {"opened", "adjusted"}:
                response["watchlist_sync"] = self._ensure_symbol_tracked_in_watchlist(
                    symbol=symbol,
                    source="manual_set_position_command",
                    trace_id=trace_id,
                )
                raw_note = command_payload.get("note")
                recommendation_extra: dict[str, object] = {}
                if manual_fill is not None:
                    entry_price = _as_float(manual_fill.get("entry_price"), default=0.0)
                    if entry_price > 0:
                        recommendation_extra["entry_price"] = round(entry_price, 6)
                        recommendation_extra["last_price"] = round(entry_price, 6)
                    quantity = _as_int(manual_fill.get("quantity"), default=0)
                    if quantity > 0:
                        recommendation_extra["entry_quantity"] = quantity
                    trade_time = str(manual_fill.get("manual_trade_time", "")).strip()
                    if trade_time:
                        recommendation_extra["entry_triggered_at"] = trade_time
                    recommendation_extra["outcome_status"] = "open"
                recommendation = self._set_recommendation_status(
                    symbol=symbol,
                    status="bought",
                    strategy=strategy,
                    timestamp=timestamp,
                    source="manual_set_position_command",
                    trace_id=trace_id,
                    note=raw_note.strip() if isinstance(raw_note, str) else "",
                    extra=recommendation_extra,
                )
                if recommendation is not None:
                    response["recommendation"] = recommendation
            return response
        if action == "SET_RECOMMENDATION_STATUS":
            symbol = str(command_payload.get("symbol", "")).strip()
            status = str(command_payload.get("status", "")).strip().lower()
            strategy = str(command_payload.get("strategy", "manual")).strip() or "manual"
            note = str(command_payload.get("note", "")).strip()
            recommendation = self._set_recommendation_status(
                symbol=symbol,
                status=status,
                strategy=strategy,
                timestamp=timestamp,
                source="manual_recommendation_status_command",
                trace_id=trace_id,
                note=note,
            )
            if recommendation is None:
                return {
                    "action": action,
                    "status": "ignored",
                    "reason": "invalid symbol or status",
                }
            return {
                "action": action,
                "symbol": recommendation["symbol"],
                "status": recommendation["status"],
                "recommendation": recommendation,
            }
        if action == "SET_BROKER_POSITIONS":
            positions = command_payload.get("positions", [])
            if not isinstance(positions, list):
                return {
                    "action": action,
                    "status": "ignored",
                    "reason": "positions must be list",
                }
            snapshot = self.update_broker_snapshot(positions=positions, source_trace_id=trace_id)
            return {"action": action, "snapshot": snapshot}

        if action == "RUN_RECONCILE":
            report = self.run_reconciliation(timestamp=timestamp, trace_id=trace_id)
            return {"action": action, "report": report}

        if action == "ACK_RECONCILE":
            self._state.reconcile_required = False
            return {"action": action, "status": "acknowledged"}

        return None

    @staticmethod
    def _apply_new_buy_pause(signals: list[PipelineSignal]) -> None:
        for signal in signals:
            if signal.action != "buy":
                continue
            signal.action = "hold"
            signal.target_position = 0.0
            signal.reasons.append("manual_pause_new_buy")

    def _apply_strategy_kill_switch(
        self,
        signals: list[PipelineSignal],
        strategy: str,
    ) -> dict[str, object]:
        if not self._config.strategy_kill_switch.enabled:
            return {
                "enabled": False,
                "strategy": strategy,
                "triggered_strategies": [],
                "blocked_buy": 0,
            }

        triggered: list[str]
        normalized_strategy = strategy.strip().lower()
        if normalized_strategy == "multi":
            triggered = self._active_kill_switch_strategies()
        else:
            state = self._strategy_kill_switch_state.get(normalized_strategy)
            if state is None:
                state = self._evaluate_strategy_kill_switch(strategy=normalized_strategy)
            triggered = [normalized_strategy] if bool(state.get("triggered", False)) else []

        if not triggered:
            return {
                "enabled": True,
                "strategy": normalized_strategy,
                "triggered_strategies": [],
                "blocked_buy": 0,
                "pause_new_buy": self._state.pause_new_buy,
            }

        blocked_buy = 0
        for signal in signals:
            if signal.action != "buy":
                continue
            signal.action = "hold"
            signal.target_position = 0.0
            if "strategy_kill_switch" not in signal.reasons:
                signal.reasons.append("strategy_kill_switch")
            blocked_buy += 1

        return {
            "enabled": True,
            "strategy": normalized_strategy,
            "triggered_strategies": triggered,
            "blocked_buy": blocked_buy,
            "pause_new_buy": self._state.pause_new_buy,
        }

    def _evaluate_strategy_kill_switch(
        self,
        strategy: str,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        normalized_strategy = strategy.strip().lower()
        required = max(1, self._config.strategy_kill_switch.underperform_months)
        history = [
            item
            for item in self._strategy_performance_history
            if str(item.get("strategy", "")).strip().lower() == normalized_strategy
        ]
        history.sort(key=lambda item: str(item.get("month", "")))

        consecutive_underperform = 0
        for item in reversed(history):
            if bool(item.get("underperform", False)):
                consecutive_underperform += 1
                continue
            break

        recent = history[-required:]
        triggered = (
            len(recent) >= required
            and len(recent) > 0
            and all(bool(item.get("underperform", False)) for item in recent)
        )
        previous = self._strategy_kill_switch_state.get(normalized_strategy, {})
        previous_triggered = bool(previous.get("triggered", False))

        if triggered and self._config.strategy_kill_switch.auto_pause_new_buy:
            self._state.pause_new_buy = True
            self._persist_runtime_state_to_disk()

        state = {
            "strategy": normalized_strategy,
            "triggered": triggered,
            "required_months": required,
            "consecutive_underperform": consecutive_underperform,
            "records": len(history),
            "latest_month": str(history[-1].get("month", "")) if history else "",
            "recent_months": [
                {
                    "month": str(item.get("month", "")),
                    "underperform": bool(item.get("underperform", False)),
                    "alpha": _as_float(item.get("alpha"), default=0.0),
                }
                for item in recent
            ],
            "pause_new_buy": self._state.pause_new_buy,
            "updated_at": datetime.now().isoformat(),
        }
        self._strategy_kill_switch_state[normalized_strategy] = state

        if triggered and not previous_triggered:
            self._record_audit_event(
                event_type="week7_strategy_kill_switch_triggered",
                level="warn",
                payload={
                    "strategy": normalized_strategy,
                    "required_months": required,
                    "consecutive_underperform": consecutive_underperform,
                    "auto_pause_new_buy": self._config.strategy_kill_switch.auto_pause_new_buy,
                },
            )
            latest_alpha = _as_float(recent[-1].get("alpha"), default=0.0) if recent else 0.0
            self.notify(
                title=_push_title(
                    priority="P1", category="risk", summary="strategy kill switch triggered"
                ),
                content=_notification_message_zh(
                    trigger=(
                        f"策略【{_strategy_label_zh(normalized_strategy)}】已连续 "
                        f"{required} 个月跑输基准，触发策略自毁保护。"
                    ),
                    impact=(
                        "该策略已被系统暂停；若配置允许，新的开仓动作也会同步进入暂停状态，"
                        "直到人工复核完成。"
                    ),
                    action="请在本地监控雷达查看该策略近月表现，确认是否继续停用、重训或手工恢复。",
                    details=[
                        f"连续跑输月数：{consecutive_underperform}",
                        f"最近月份：{str(history[-1].get('month', '')) if history else '-'}",
                        f"最近月超额收益：{latest_alpha:.2%}",
                        f"暂停新开仓：{_bool_zh(self._state.pause_new_buy)}",
                    ],
                ),
                level="warn",
                trace_id=source_trace_id,
            )
        return state

    def _active_kill_switch_strategies(self) -> list[str]:
        active: list[str] = []
        for strategy, state in self._strategy_kill_switch_state.items():
            if bool(state.get("triggered", False)):
                active.append(strategy)
        active.sort()
        return active

    def _notify_provider_health_if_needed(self, trace_id: str = "") -> None:
        status = self.provider_status()
        degraded = _provider_alert_degraded(status)
        if degraded and not self._provider_degraded_alert_active:
            health = status.get("health", {})
            if not isinstance(health, dict):
                health = {}
            consecutive_failures = _as_int(status.get("consecutive_failures"), default=0)
            success_rate = _as_float(health.get("success_rate"), default=1.0)
            avg_latency_sec = _as_float(health.get("avg_latency_sec"), default=0.0)
            self.notify(
                title=_push_title(priority="P1", category="ops", summary="data provider degraded"),
                content=_notification_message_zh(
                    trigger="核心行情数据源连续请求失败，系统已判定为数据链路异常。",
                    impact="系统已降级到备用迟缓模式，并暂停新的开仓信号，避免错价或脏数据误导决策。",
                    action="请不要修改配置；若该状态持续超过十分钟，请检查本机网络、外网连通性与数据源服务状态。",
                    details=[
                        f"退化原因：{_provider_degraded_reason_zh(status)}",
                        f"连续失败次数：{consecutive_failures}",
                        f"成功率：{success_rate:.2%}",
                        f"平均延迟：{avg_latency_sec:.2f} 秒",
                        f"最近错误：{_provider_last_error_zh(status)}",
                    ],
                ),
                level="warn",
                trace_id=trace_id,
            )
            self._provider_degraded_alert_active = True
            return
        if not degraded and self._provider_degraded_alert_active:
            self.notify(
                title=_push_title(priority="P2", category="ops", summary="data provider recovered"),
                content=_notification_message_zh(
                    trigger="核心行情数据链路恢复正常，已重新建立稳定连接。",
                    impact="系统已退出退化模式，新开仓信号与盘中监控能力恢复正常运行。",
                    action="无需紧急干预；可在本地监控雷达确认观察池、行情刷新和买卖信号是否已恢复同步。",
                    details=[
                        "恢复结果：新开仓通道已重新开放",
                        "风险状态：卖出止损链路始终保持可用",
                    ],
                ),
                level="info",
                trace_id=trace_id,
            )
            self._provider_degraded_alert_active = False

    def _notify_actionable_signals(
        self,
        report: dict[str, object],
        trace_id: str,
        title_prefix: str,
    ) -> None:
        raw = report.get("actionable_signals")
        if not isinstance(raw, list):
            return
        portfolio_update = report.get("portfolio_update", {})
        executed_pairs: set[tuple[str, str]] = set()
        if isinstance(portfolio_update, dict):
            raw_executions = portfolio_update.get("executions")
            if isinstance(raw_executions, list):
                for execution in raw_executions:
                    if not isinstance(execution, dict):
                        continue
                    symbol = str(execution.get("symbol", "")).strip()
                    side = str(execution.get("side", "")).strip().lower()
                    status = str(execution.get("status", "")).strip().lower()
                    if (
                        symbol
                        and side in {"buy", "sell"}
                        and status in {"opened", "closed", "adjusted"}
                    ):
                        executed_pairs.add((symbol, side))
        phase = title_prefix.strip() or "intraday"
        day_key = datetime.now().strftime("%Y%m%d")
        for item in raw:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            action = str(item.get("action", "")).strip().lower()
            if action not in ("buy", "sell"):
                continue
            if (symbol, action) in executed_pairs:
                continue
            target_position = _as_float(item.get("target_position"), default=0.0)
            strategy = str(item.get("strategy", "")).strip() or "-"
            reasons_raw = item.get("reasons", [])
            reasons = (
                [str(value).strip() for value in reasons_raw]
                if isinstance(reasons_raw, list)
                else []
            )
            reasons = [value for value in reasons if value]
            reason_text = _format_signal_reasons_zh(reasons, max_items=3)
            news_component = _extract_news_component(reasons)
            detail_lines = [
                f"扫描阶段：{phase}",
                f"所属策略：{_strategy_label_zh(strategy)}",
                f"核心原因：{reason_text}",
            ]
            if news_component is not None:
                sentiment = _news_component_sentiment(news_component)
                detail_lines.append(
                    f"新闻因子：{news_component:.3f}（情绪={_news_component_sentiment_zh(sentiment)}）"
                )

            if action == "buy":
                score = _as_float(item.get("score"), default=0.0)
                if score < self._buy_notification_min_score(strategy):
                    continue
                grade = str(item.get("grade", "")).strip() or "-"
                detail_lines.extend(
                    [
                        f"目标仓位：{target_position:.2%}",
                        f"评分等级：{score:.2f} 分 / {grade}",
                    ]
                )
                self._notify_if_changed(
                    dedup_key=f"notify:actionable:{day_key}:{phase}:buy:{symbol}",
                    title=_push_title(
                        priority="P1", category="signal", summary=f"buy candidate {symbol}"
                    ),
                    content=_notification_message_zh(
                        trigger=(
                            f"{phase} 监控发现标的【{symbol}】综合评分达到 {score:.2f} 分，"
                            "已越过优先买入观察线。"
                        ),
                        impact=(
                            f"当前系统允许开仓，该标的已进入可执行观察名单，建议投入仓位 {target_position:.2%}。"
                        ),
                        action=(
                            "如您当前没有主观禁投因素，请结合盘口与仓位计划分批处理；"
                            "操作完成后请在本地监控雷达同步持仓。"
                        ),
                        details=detail_lines,
                    ),
                    level="info",
                    trace_id=trace_id,
                    ttl_sec=20 * 3600,
                )
            else:
                detail_lines.extend(
                    [
                        "目标仓位：0.00%",
                        f"当前建议：{_translate_signal_action_zh(action)}",
                    ]
                )
                self._notify_if_changed(
                    dedup_key=f"notify:actionable:{day_key}:{phase}:sell:{symbol}",
                    title=_push_title(
                        priority="P0", category="action", summary=f"sell instruction {symbol}"
                    ),
                    content=_notification_message_zh(
                        trigger=f"{phase} 监控对标的【{symbol}】给出明确卖出处理指令。",
                        impact="继续持有会违背当前风控或止盈止损要求，系统已将目标仓位下调为零。",
                        action="请优先在券商端完成减仓或清仓，再返回本地监控雷达同步实际持仓。",
                        details=detail_lines,
                    ),
                    level="warn",
                    trace_id=trace_id,
                    ttl_sec=20 * 3600,
                )

    def _buy_notification_min_score(self, strategy: str) -> float:
        normalized = strategy.strip().lower()
        thresholds = self._config.score.thresholds
        if normalized in self._config.strategy_scores:
            thresholds = self._config.strategy_scores[normalized].thresholds
        return float(thresholds.a)

    def _notify_risk_status_if_needed(self, risk: dict[str, object], trace_id: str) -> None:
        action = str(risk.get("action", ""))
        reason = str(risk.get("reason", ""))
        drawdown_pct = _as_float(risk.get("drawdown_pct"), default=0.0)

        last_alert = bool(self._risk_circuit_breaker_alert_active)
        is_freeze = action in ("freeze", "pause") and (
            "circuit_breaker" in reason or "capital_curve" in reason
        )

        if is_freeze and not last_alert:
            self.notify(
                title=_push_title(
                    priority="P0", category="risk", summary="circuit breaker engaged"
                ),
                content=_notification_message_zh(
                    trigger=f"系统检测到账户回撤或风险亏损已达到 {drawdown_pct:.1f}%，触发风控熔断阈值。",
                    impact="系统已进入防守冻结状态，所有新的买入开仓将被拦截，但卖出止损通道仍然保持可用。",
                    action="请立即停止任何手动加仓行为，优先复核当前持仓与市场风险；需要恢复时请在本地监控雷达人工确认。",
                    details=[
                        f"风险动作：{_translate_capital_action_zh(action)}",
                        f"触发原因：{_translate_signal_reason_zh(reason) or '-'}",
                        f"当前回撤：{drawdown_pct:.1f}%",
                    ],
                ),
                level="warn",
                trace_id=trace_id,
            )
            self._risk_circuit_breaker_alert_active = True
        elif not is_freeze and last_alert:
            self.notify(
                title=_push_title(
                    priority="P1", category="risk", summary="circuit breaker released"
                ),
                content=_notification_message_zh(
                    trigger="账户风险已回到安全区间，或您已通过本地监控雷达人工解除熔断状态。",
                    impact="新开仓通道重新开放，系统的交易搜索与风控联动逻辑恢复正常。",
                    action="无需立即干预；可登录本地监控雷达核对资金曲线、持仓状态与观察池刷新情况。",
                    details=[
                        "恢复结果：新开仓通道已重新开放",
                        f"当前回撤：{drawdown_pct:.1f}%",
                    ],
                ),
                level="info",
                trace_id=trace_id,
            )
            self._risk_circuit_breaker_alert_active = False

        protection_level = ""
        if reason.startswith("capital_curve:") and action in {"alert", "reduce"}:
            protection_level = action

        if protection_level and protection_level != self._risk_capital_protection_level:
            self.notify(
                title=_push_title(
                    priority="P2", category="risk", summary="capital protection warning"
                ),
                content=_notification_message_zh(
                    trigger=f"账户风险状态已进入资金保护线【{_translate_capital_action_zh(action)}】阶段。",
                    impact="系统将自动提高谨慎度、收缩风险暴露，并降低新增仓位的可执行力度。",
                    action="请控制新增仓位，密切关注回撤是否继续扩大；如继续恶化，需准备进入更强防守状态。",
                    details=[
                        f"当前回撤：{drawdown_pct:.1f}%",
                        f"触发原因：{_translate_signal_reason_zh(reason) or '-'}",
                    ],
                ),
                level="warn",
                trace_id=trace_id,
            )
            self._risk_capital_protection_level = protection_level
        elif not protection_level and self._risk_capital_protection_level:
            self.notify(
                title=_push_title(
                    priority="P2", category="risk", summary="capital protection recovered"
                ),
                content=_notification_message_zh(
                    trigger="资金保护线预警已解除，账户风险水平回落到常规范围。",
                    impact="系统对新增风险暴露的限制已恢复正常，仓位控制不再额外收缩。",
                    action="无需立即干预；后续仍请按常规风险纪律执行，不建议因恢复提示而盲目加仓。",
                    details=[f"当前回撤：{drawdown_pct:.1f}%"],
                ),
                level="info",
                trace_id=trace_id,
            )
            self._risk_capital_protection_level = ""
        elif is_freeze:
            self._risk_capital_protection_level = ""

    def _store_reconcile_report(self, report: dict[str, object]) -> None:
        self._reconcile_service._store_reconcile_report(report)


    def _store_week5_scan_report(self, report: dict[str, object]) -> None:
        self._week5_service._store_week5_scan_report(report)

    def _store_week6_report(self, report: dict[str, object]) -> None:
        self._week6_service._store_week6_report(report)

    def _store_tdx_sync_report(self, report: dict[str, object]) -> None:
        self._last_tdx_sync_report = report
        self._tdx_sync_history.append(report)
        history_limit = max(1, _as_int(self._config.tdx_sync.history_limit, default=30))
        if len(self._tdx_sync_history) > history_limit:
            overflow = len(self._tdx_sync_history) - history_limit
            if overflow > 0:
                self._tdx_sync_history = self._tdx_sync_history[overflow:]
        self._persist_tdx_sync_history_to_disk()

    def _store_market_warehouse_report(self, report: dict[str, object]) -> None:
        self._last_market_warehouse_report = report
        self._market_warehouse_history.append(report)
        history_limit = max(
            1,
            _as_int(self._config.market_warehouse.history_limit, default=30),
        )
        if len(self._market_warehouse_history) > history_limit:
            overflow = len(self._market_warehouse_history) - history_limit
            if overflow > 0:
                self._market_warehouse_history = self._market_warehouse_history[overflow:]
        self._persist_market_warehouse_history_to_disk()

    def _store_market_warehouse_progress(self, progress: dict[str, object]) -> None:
        self._last_market_warehouse_progress = progress
        self._persist_market_warehouse_progress_to_disk()

    def _record_run_summary(
        self,
        report: dict[str, object],
        current_equity: float,
        actionable_count: int,
        duration_ms: int,
        job_name: str = "",
        strategy: str = "",
        symbol_count: int | None = None,
        use_live_runtime: bool = False,
    ) -> None:
        timestamp = str(report.get("timestamp", datetime.now().isoformat()))
        risk = report.get("risk", {})
        if not isinstance(risk, dict):
            risk = {}
        normalized_job = (
            job_name.strip()
            or str(report.get("job_name", "")).strip()
            or "pipeline_run"
        )
        week6_execution = report.get("week6_execution")
        week6_strategy = (
            str(week6_execution.get("strategy", "")).strip()
            if isinstance(week6_execution, dict)
            else ""
        )
        normalized_strategy = (
            strategy.strip()
            or str(report.get("strategy", "")).strip()
            or week6_strategy
        )
        runtime_role = _runtime_role_for_run(
            job_name=normalized_job,
            use_live_runtime=bool(use_live_runtime or report.get("use_live_runtime", False)),
        )
        resolved_symbol_count = (
            max(0, int(symbol_count))
            if symbol_count is not None
            else _as_int(report.get("symbol_count"), default=_signals_count(report))
        )
        entry = {
            "timestamp": timestamp,
            "trace_id": str(report.get("trace_id", "")),
            "job_name": normalized_job,
            "runtime_role": runtime_role,
            "strategy": normalized_strategy,
            "symbol_count": resolved_symbol_count,
            "use_live_runtime": bool(use_live_runtime or report.get("use_live_runtime", False)),
            "equity": current_equity,
            "drawdown_pct": _as_float(risk.get("drawdown_pct"), default=0.0),
            "risk_action": str(risk.get("action", "")),
            "signals": _signals_count(report),
            "actionable": actionable_count,
            "duration_ms": duration_ms,
        }
        self._run_summaries.append(entry)
        self._latency_history_ms.append(
            {
                "timestamp": timestamp,
                "trace_id": entry["trace_id"],
                "job_name": normalized_job,
                "runtime_role": runtime_role,
                "strategy": normalized_strategy,
                "symbol_count": resolved_symbol_count,
                "use_live_runtime": entry["use_live_runtime"],
                "duration_ms": duration_ms,
            }
        )

        run_limit = 2000
        if len(self._run_summaries) > run_limit:
            overflow = len(self._run_summaries) - run_limit
            if overflow > 0:
                self._run_summaries = self._run_summaries[overflow:]

        latency_limit = 5000
        if len(self._latency_history_ms) > latency_limit:
            overflow = len(self._latency_history_ms) - latency_limit
            if overflow > 0:
                self._latency_history_ms = self._latency_history_ms[overflow:]

    def _execution_quality_snapshot(
        self,
        days: int,
        trades: list[dict[str, object]],
    ) -> dict[str, object]:
        weekly = self.reconcile_weekly_report(days=days)
        sim_vs_broker = weekly.get("sim_vs_broker", {})
        if not isinstance(sim_vs_broker, dict):
            sim_vs_broker = {}

        manual_trades = 0
        for trade in trades:
            reason = str(trade.get("reason", ""))
            if reason.startswith("manual_"):
                manual_trades += 1

        latest = self.latest_reconcile_report()
        return {
            "manual_trade_ratio": round(manual_trades / len(trades), 4) if trades else 0.0,
            "manual_trade_count": manual_trades,
            "reconcile_alignment_rate": _as_float(
                sim_vs_broker.get("alignment_rate"),
                default=0.0,
            ),
            "reconcile_mismatch_records": _as_int(weekly.get("mismatch_records"), default=0),
            "max_reconcile_abs_diff": _as_float(sim_vs_broker.get("max_abs_diff"), default=0.0),
            "latest_reconcile_status": str(latest.get("status", "")) if latest else "",
        }

    def _persist_week4_acceptance_report(
        self,
        report: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        return self._acceptance_service._persist_week4_acceptance_report(
            report=report,
            now=now,
        )

    def _persist_week7_sim_broker_report(
        self,
        report: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        return self._week7_sim_broker_service._persist_week7_sim_broker_report(
            report=report,
            now=now,
        )


    def _resolve_acceptance_export_dir(self) -> Path:
        return self._acceptance_service._resolve_acceptance_export_dir()

    def _resolve_week7_sim_broker_export_dir(self) -> Path:
        return self._week7_sim_broker_service._resolve_week7_sim_broker_export_dir()


    def _resolve_week6_state_file(self) -> Path:
        return self._week6_service._resolve_week6_state_file()

    def _persist_week6_state(self) -> None:
        self._week6_service._persist_week6_state()

    def _load_week6_state(self) -> None:
        self._week6_service._load_week6_state()

    @staticmethod
    def _week6_persistence_enabled() -> bool:
        return os.getenv("PYTEST_CURRENT_TEST") is None

    def _record_audit_event(
        self,
        event_type: str,
        trace_id: str = "",
        level: str = "info",
        message: str = "",
        payload: dict[str, object] | None = None,
    ) -> None:
        with self._audit_lock:
            self._audit_seq += 1
            event_payload: dict[str, object] = payload if payload is not None else {}
            event: dict[str, object] = {
                "event_id": f"AUD-{self._audit_seq:08d}",
                "timestamp": datetime.now().isoformat(),
                "event_type": event_type,
                "trace_id": trace_id,
                "level": level,
                "message": message,
                "payload": event_payload,
            }
            self._audit_events.append(event)
            audit_limit = 5000
            if len(self._audit_events) > audit_limit:
                overflow = len(self._audit_events) - audit_limit
                if overflow > 0:
                    self._audit_events = self._audit_events[overflow:]
        self._persist_runtime_state_to_disk()
        # TODO(security): In dry-run mode, business state (portfolio/watchlist/trades)
        # is correctly rolled back, but this persist call writes audit_events and
        # run_summaries to runtime_state.json. Consider splitting audit persistence
        # from business state persistence, or gating this call on a dry_run flag.
        # See test: test_service_pipeline_dry_run_execution_does_not_mutate_portfolio_or_notify


def _signals_count(report: dict[str, object]) -> int:
    raw = report.get("signals")
    if isinstance(raw, list):
        return len(raw)
    return 0


def _audit_portfolio_update_summary(portfolio_update: Mapping[str, object]) -> dict[str, object]:
    scalar_fields = (
        "opened",
        "adjusted",
        "trimmed",
        "closed_expired",
        "closed_signals",
        "skipped_max_holdings",
        "skipped_same_sector",
        "skipped_no_cash",
        "open_positions",
        "status",
        "dry_run",
    )
    payload: dict[str, object] = {
        field: portfolio_update[field] for field in scalar_fields if field in portfolio_update
    }
    raw_executions = portfolio_update.get("executions")
    if isinstance(raw_executions, list):
        payload["executions"] = [
            _audit_portfolio_execution_summary(item)
            for item in raw_executions
            if isinstance(item, Mapping)
        ]
    raw_attempts = portfolio_update.get("execution_attempts")
    if isinstance(raw_attempts, Mapping):
        payload["execution_attempts"] = {
            str(key): _as_int(value, default=0) for key, value in raw_attempts.items()
        }
    raw_advisory_attempts = portfolio_update.get("advisory_attempts")
    if isinstance(raw_advisory_attempts, Mapping):
        payload["advisory_attempts"] = {
            str(key): _as_int(value, default=0) for key, value in raw_advisory_attempts.items()
        }
    raw_dry_run_attempts = portfolio_update.get("dry_run_attempts")
    if isinstance(raw_dry_run_attempts, Mapping):
        payload["dry_run_attempts"] = {
            str(key): _as_int(value, default=0) for key, value in raw_dry_run_attempts.items()
        }
    return payload


def _audit_portfolio_execution_summary(item: Mapping[str, object]) -> dict[str, object]:
    fields = (
        "trade_id",
        "symbol",
        "side",
        "status",
        "block_category",
        "strategy",
        "target_position",
        "price",
        "quantity",
        "amount",
        "fee",
        "price_source",
        "trade_time",
        "reason",
        "reference_price",
        "recommendation_id",
        "snapshot_id",
    )
    return {field: item[field] for field in fields if field in item}


def _actionable_count(report: dict[str, object]) -> int:
    raw = report.get("actionable_signals")
    if isinstance(raw, list):
        return len(raw)
    return 0


def _report_timestamp(report: dict[str, object]) -> float:
    raw = report.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _merge_evolution_reports(
    memory_reports: list[dict[str, object]],
    disk_reports: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for report in memory_reports + disk_reports:
        run_id = str(report.get("run_id", ""))
        timestamp = str(report.get("timestamp", ""))
        key = (run_id, timestamp)
        if key not in merged:
            merged[key] = report
    ordered = sorted(merged.values(), key=_report_timestamp)
    return ordered


def _build_positions_panel(
    positions: list[dict[str, object]],
    now: datetime,
) -> list[dict[str, object]]:
    panel: list[dict[str, object]] = []
    for item in positions:
        opened_raw = item.get("opened_at")
        opened_at = _parse_iso_datetime(opened_raw)
        hold_days = 0
        if opened_at is not None:
            hold_days = max(0, (now.date() - opened_at.date()).days)
        panel.append(
            {
                "symbol": str(item.get("symbol", "")),
                "strategy": str(item.get("strategy", "")),
                "target_position": _as_float(item.get("target_position"), default=0.0),
                "entry_price": _as_float(item.get("entry_price"), default=0.0),
                "quantity": _as_int(item.get("quantity"), default=0),
                "fee": _as_float(item.get("fee"), default=0.0),
                "account": str(item.get("account", "")),
                "manual_trade_time": str(item.get("manual_trade_time", "")),
                "note": str(item.get("note", "")),
                "status": str(item.get("status", "")),
                "hold_days": hold_days,
                "opened_at": str(item.get("opened_at", "")),
                "updated_at": str(item.get("updated_at", "")),
            }
        )
    return panel


def _entry_day_symbols(
    positions: list[dict[str, object]],
    now: datetime,
) -> set[str]:
    symbols: set[str] = set()
    for item in positions:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        opened_at = _parse_iso_datetime(item.get("opened_at"))
        if opened_at is None:
            continue
        if opened_at.date() == now.date():
            symbols.add(symbol)
    return symbols


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _legacy_production_model_id(artifact_uri: str) -> str:
    stem = Path(str(artifact_uri).strip() or "model_v1").stem.strip().lower()
    safe_stem = "".join(char if char.isalnum() else "_" for char in stem).strip("_")
    if not safe_stem:
        safe_stem = "model_v1"
    return f"{safe_stem}_prod_bootstrap_existing"


def _legacy_feature_schema_hash(*, artifact: ModelArtifact) -> str:
    payload = {
        "version": artifact.version,
        "feature_columns": list(artifact.feature_columns),
    }
    return f"legacy_production_feature_schema_hash_{_digest_json(payload, length=16)}"


def _legacy_label_policy_hash(*, artifact_uri: str) -> str:
    return f"legacy_production_label_policy_hash_{_digest_text(artifact_uri, length=16)}"


def _digest_text(value: object, *, length: int = 16) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[: max(1, int(length))]


def _digest_json(value: object, *, length: int = 16) -> str:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _digest_text(serialized, length=length)


def _normalize_recommendation_status(value: object, default: str = "watching") -> str:
    normalized = str(value).strip().lower()
    if normalized in _RECOMMENDATION_STATUSES:
        return normalized
    fallback = str(default).strip().lower()
    if fallback in _RECOMMENDATION_STATUSES:
        return fallback
    return ""


def _build_recommendation_id(
    *,
    trace_id: str,
    symbol: str,
    strategy: str,
    index: int,
) -> str:
    base = f"{trace_id.strip()}|{symbol.strip().upper()}|{strategy.strip().lower()}|{max(index, 0)}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16].upper()
    return f"REC-{digest}"


def _extract_learning_protocol_payload(source: object) -> Mapping[str, object] | None:
    if not isinstance(source, Mapping):
        return None
    direct = source.get("learning_protocol")
    if isinstance(direct, Mapping):
        return cast(Mapping[str, object], direct)
    decision_trace = source.get("decision_trace")
    if isinstance(decision_trace, Mapping):
        nested = decision_trace.get("learning_protocol")
        if isinstance(nested, Mapping):
            return cast(Mapping[str, object], nested)
    return None


def _extract_learning_snapshot_id(source: object) -> str:
    if isinstance(source, Mapping):
        direct_snapshot_id = str(source.get("snapshot_id", "")).strip()
        if direct_snapshot_id:
            return direct_snapshot_id
    protocol = _extract_learning_protocol_payload(source)
    if protocol is None:
        return ""
    return str(protocol.get("snapshot_id", "")).strip()


def _calculate_execution_slippage_bp(
    *,
    side: str,
    execution_price: float,
    reference_price: float,
) -> float | None:
    if execution_price <= 0 or reference_price <= 0:
        return None
    normalized_side = side.strip().lower()
    if normalized_side == "sell":
        delta = reference_price - execution_price
    else:
        delta = execution_price - reference_price
    return round((delta / reference_price) * 10000.0, 4)


def _parse_manual_fill_from_command_payload(
    payload: dict[str, object],
    fallback_time: datetime,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    has_manual_fields = any(
        key in payload
        for key in ("entry_price", "quantity", "fee", "account", "trade_time", "note")
    )
    if not has_manual_fields:
        return None
    entry_price_raw = payload.get("entry_price")
    quantity_raw = payload.get("quantity")
    fee_raw = payload.get("fee")
    account_raw = payload.get("account")
    note_raw = payload.get("note")
    trade_time_raw = payload.get("trade_time")

    entry_price = _as_float(entry_price_raw, default=0.0)
    if entry_price <= 0:
        entry_price_value: float | None = None
    else:
        entry_price_value = round(entry_price, 6)

    quantity = _as_int(quantity_raw, default=0)
    quantity_value = quantity if quantity > 0 else None

    fee = _as_float(fee_raw, default=0.0)
    fee_value = round(fee if fee >= 0 else 0.0, 6)

    account = str(account_raw).strip() if isinstance(account_raw, str) else ""
    note = str(note_raw).strip() if isinstance(note_raw, str) else ""
    parsed_trade_time = (
        _parse_iso_datetime(trade_time_raw) if isinstance(trade_time_raw, str) else None
    )
    manual_trade_time = (parsed_trade_time or fallback_time).isoformat()

    return {
        "entry_price": entry_price_value,
        "quantity": quantity_value,
        "fee": fee_value,
        "account": account,
        "manual_trade_time": manual_trade_time,
        "note": note,
    }


def _parse_manual_close_fill_from_command_payload(
    payload: dict[str, object],
    fallback_time: datetime,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    has_manual_fields = any(
        key in payload for key in ("exit_price", "quantity", "fee", "account", "trade_time", "note")
    )
    if not has_manual_fields:
        return None
    exit_price_raw = payload.get("exit_price")
    quantity_raw = payload.get("quantity")
    fee_raw = payload.get("fee")
    account_raw = payload.get("account")
    note_raw = payload.get("note")
    trade_time_raw = payload.get("trade_time")

    exit_price = _as_float(exit_price_raw, default=0.0)
    if exit_price <= 0:
        exit_price_value: float | None = None
    else:
        exit_price_value = round(exit_price, 6)

    quantity = _as_int(quantity_raw, default=0)
    quantity_value = quantity if quantity > 0 else None

    fee = _as_float(fee_raw, default=0.0)
    fee_value = round(fee if fee >= 0 else 0.0, 6)

    account = str(account_raw).strip() if isinstance(account_raw, str) else ""
    note = str(note_raw).strip() if isinstance(note_raw, str) else ""
    parsed_trade_time = (
        _parse_iso_datetime(trade_time_raw) if isinstance(trade_time_raw, str) else None
    )
    manual_trade_time = (parsed_trade_time or fallback_time).isoformat()

    return {
        "exit_price": exit_price_value,
        "quantity": quantity_value,
        "fee": fee_value,
        "account": account,
        "manual_trade_time": manual_trade_time,
        "note": note,
    }


def _bootstrap_error_text(report: dict[str, object]) -> str:
    explicit = str(report.get("error", "")).strip()
    if explicit:
        return explicit
    errors = report.get("errors")
    if isinstance(errors, list):
        values = [str(item).strip() for item in errors if str(item).strip()]
        if values:
            return values[0]
    status = str(report.get("status", "")).strip()
    return status


def _parse_trade_date(value: str) -> date | None:
    raw = value.strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def _normalize_year_month(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw, "%Y-%m")
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m")


def _normalize_factor_features(raw_features: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for item in raw_features:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        importance = _as_float(item.get("importance"), default=0.0)
        normalized.append({"name": name, "importance": round(importance, 6)})
    normalized.sort(
        key=lambda item: (
            -abs(_as_float(item.get("importance"), default=0.0)),
            str(item.get("name", "")),
        )
    )
    return normalized[:20]


def _factor_top3_drift_ratio(
    previous: list[dict[str, object]],
    current: list[dict[str, object]],
) -> float:
    if not previous:
        return 0.0
    prev_map: dict[str, float] = {
        str(item.get("name", "")): abs(_as_float(item.get("importance"), default=0.0))
        for item in previous[:3]
        if str(item.get("name", ""))
    }
    curr_map: dict[str, float] = {
        str(item.get("name", "")): abs(_as_float(item.get("importance"), default=0.0))
        for item in current[:3]
        if str(item.get("name", ""))
    }
    if not prev_map and not curr_map:
        return 0.0
    keys = set(prev_map.keys()) | set(curr_map.keys())
    diff_sum = 0.0
    prev_sum = 0.0
    for key in keys:
        prev_value = prev_map.get(key, 0.0)
        curr_value = curr_map.get(key, 0.0)
        diff_sum += abs(curr_value - prev_value)
        prev_sum += prev_value
    if prev_sum <= 1e-9:
        return 1.0 if diff_sum > 1e-9 else 0.0
    return diff_sum / prev_sum


def _parse_broker_positions(positions: list[dict[str, object]]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for item in positions:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        target = _as_float(item.get("target_position"), default=0.0)
        if target < 0:
            continue
        parsed[symbol] = target
    return parsed


def _parse_broker_position_details(
    positions: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    parsed: dict[str, dict[str, object]] = {}
    for item in positions:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        target = _as_float(item.get("target_position"), default=0.0)
        if target < 0:
            continue
        quantity = _as_int(item.get("quantity"), default=0)
        account = str(item.get("account", "")).strip()
        parsed[symbol] = {
            "target_position": target,
            "quantity": quantity if quantity > 0 else None,
            "account": account,
        }
    return parsed


def _notification_message_zh(
    *,
    trigger: str,
    impact: str,
    action: str,
    details: list[str] | tuple[str, ...] | None = None,
    detail_title: str = "详细追踪",
) -> str:
    lines = [
        f"触发事件：{trigger.strip() or '-'}",
        f"系统影响：{impact.strip() or '-'}",
        f"建议动作：{action.strip() or '-'}",
    ]
    detail_items = [str(item).strip() for item in (details or []) if str(item).strip()]
    if detail_items:
        lines.append(f"{detail_title}：")
        lines.extend(f"- {item}" for item in detail_items)
    return "\n".join(lines)


def _push_title(priority: str, category: str, summary: str) -> str:
    normalized = priority.strip().upper()
    badge_map = {
        "P0": "【紧急】",
        "P1": "【重要】",
        "P2": "【日常】",
        "P3": "【参考】",
    }
    badge = badge_map.get(normalized, "【日常】")
    category_text = _notification_category_zh(category)
    summary_text = _notification_summary_zh(summary)
    return f"{badge}【{category_text}】{summary_text}"


def _reconcile_status_zh(status: str) -> str:
    mapping = {
        "ok": "正常",
        "mismatch": "存在差异",
        "missing_snapshot": "缺少持仓快照",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status or "未知")


def _acceptance_status_zh(status: str) -> str:
    mapping = {
        "pass": "通过",
        "pass_with_warnings": "通过（含警告）",
        "fail": "失败",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status or "未知")


def _sim_broker_status_zh(status: str) -> str:
    mapping = {
        "healthy": "健康",
        "watch": "观察",
        "action_required": "需要处理",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status or "未知")


def _sim_trade_status_zh(status: str) -> str:
    mapping = {
        "opened": "已建仓",
        "adjusted": "已调仓",
        "closed": "已卖出",
        "rejected_no_cash": "未成交：现金或最小交易单位不足",
        "rejected_max_holdings": "未成交：达到最大持仓数",
        "rejected_same_sector": "未成交：同板块限制",
        "rejected_execution": "未成交：执行器拒绝",
        "rejected_price_unavailable": "未成交：缺少可用价格",
        "rejected_quantity": "未成交：数量不足",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status or "未知")


def _sim_trade_notification_day(trade_time: str) -> str:
    raw = trade_time.strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y%m%d")
        except ValueError:
            pass
    return date.today().strftime("%Y%m%d")


def _week6_quality_status_zh(status: str) -> str:
    mapping = {
        "healthy": "健康",
        "warning": "告警",
        "critical": "严重",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status or "未知")


def _regime_zh(regime: str) -> str:
    mapping = {
        "trend": "趋势",
        "range": "震荡",
        "crash": "急跌",
    }
    normalized = regime.strip().lower()
    return mapping.get(normalized, regime or "-")


def _week6_quality_fields_zh(fields: list[str]) -> str:
    if not fields:
        return "-"
    labels: list[str] = []
    seen: set[str] = set()
    for raw in fields:
        label = str(raw).strip()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return ", ".join(labels) if labels else "-"


def _risk_snapshot_zh(risk: object) -> str:
    if not isinstance(risk, dict):
        return "风险快照：不可用"
    action = _translate_capital_action_zh(str(risk.get("action", "")))
    drawdown_pct = _as_float(risk.get("drawdown_pct"), default=0.0)
    can_open = _bool_zh(bool(risk.get("can_open_new_position", False)))
    degraded = _bool_zh(bool(risk.get("degraded_mode", False)))
    raw_reason = str(risk.get("reason", "")).strip()
    reason = _translate_signal_reason_zh(raw_reason) if raw_reason else "-"
    return (
        f"风险动作={action}；回撤={drawdown_pct:.2f}%；可开新仓={can_open}；"
        f"退化模式={degraded}；原因={reason}"
    )


def _provider_degraded_detail_zh(status: dict[str, object]) -> str:
    health = status.get("health", {})
    if not isinstance(health, dict):
        health = {}
    degrade_reason = str(health.get("degrade_reason", "")).strip().lower()
    reason_map = {
        "low_success_rate": "成功率过低",
        "high_latency": "延迟过高",
    }
    reason = reason_map.get(degrade_reason, "数据源不稳定")
    success_rate = _as_float(health.get("success_rate"), default=1.0)
    avg_latency_sec = _as_float(health.get("avg_latency_sec"), default=0.0)
    consecutive_failures = _as_int(status.get("consecutive_failures"), default=0)
    last_error = (
        str(status.get("last_error", "")).strip() or str(status.get("cache_last_error", "")).strip()
    )
    summary = (
        f"原因={reason}；成功率={success_rate:.2%}；平均延迟={avg_latency_sec:.2f}秒；"
        f"连续失败次数={consecutive_failures}"
    )
    if last_error:
        return f"{summary}；最近错误={_notification_error_text_zh(last_error)}"
    return summary


def _provider_degraded_reason_zh(status: dict[str, object]) -> str:
    health = status.get("health", {})
    if not isinstance(health, dict):
        health = {}
    degrade_reason = str(health.get("degrade_reason", "")).strip().lower()
    mapping = {
        "low_success_rate": "成功率过低",
        "high_latency": "延迟过高",
    }
    return mapping.get(degrade_reason, "数据源不稳定")


def _provider_alert_degraded(status: dict[str, object]) -> bool:
    if not isinstance(status, dict):
        return False
    if "hard_degraded_mode" in status:
        return bool(status.get("hard_degraded_mode", False))
    health = status.get("health", {})
    if not isinstance(health, dict):
        health = {}
    if bool(health.get("degraded_mode", False)):
        return True
    evolution = status.get("evolution", {})
    if not isinstance(evolution, dict):
        evolution = {}
    return bool(status.get("degraded_mode", False)) and not bool(
        evolution.get("degraded_mode", False)
    )


def _provider_last_error_zh(status: dict[str, object]) -> str:
    last_error = (
        str(status.get("last_error", "")).strip() or str(status.get("cache_last_error", "")).strip()
    )
    if not last_error:
        return "-"
    return _notification_error_text_zh(last_error)


def _action_recommendation_zh(action: str) -> str:
    mapping = {
        "buy": "买入",
        "watch": "观察",
        "hold": "持有",
    }
    normalized = action.strip().lower()
    return mapping.get(normalized, f"动作={action}")


def _sla_entry_matches_scope(entry: dict[str, object], scope: str) -> bool:
    normalized_scope = scope.strip().lower()
    if normalized_scope in {"", "all"}:
        return True
    timestamp = str(entry.get("timestamp", "")).strip()
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if normalized_scope == "intraday":
        if not is_a_share_trading_day(parsed):
            return False
        current_time = parsed.time()
        return dt_time(hour=9, minute=15) <= current_time <= dt_time(hour=15, minute=30)
    return True


def _sla_entry_matches_job_scope(entry: dict[str, object], job_scope: str) -> bool:
    normalized_scope = job_scope.strip().lower()
    if normalized_scope in {"", "all"}:
        return True
    job_name = str(entry.get("job_name", "")).strip().lower()
    runtime_role = str(entry.get("runtime_role", "")).strip().lower()
    if normalized_scope == "live_runtime":
        if runtime_role:
            return runtime_role == "live_runtime"
        return job_name == "week5_live_runtime" or job_name.startswith("week5_live_runtime_")
    if normalized_scope == "scheduled":
        return bool(job_name)
    if normalized_scope in {"live_data_scan", "live_data_job", "batch_job"}:
        return runtime_role == normalized_scope
    return job_name == normalized_scope


def _runtime_role_for_run(*, job_name: str, use_live_runtime: bool) -> str:
    normalized_job = job_name.strip().lower()
    if normalized_job == "week5_live_runtime" or normalized_job.startswith("week5_live_runtime_"):
        return "live_runtime"
    if normalized_job == "week5_scan_monster":
        return "live_data_scan" if use_live_runtime else "batch_job"
    if use_live_runtime:
        return "live_data_job"
    return "batch_job"


def _slowest_latency_entries(
    entries: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    capped = max(1, int(limit))
    ordered = sorted(
        entries,
        key=lambda item: _as_int(item.get("duration_ms"), default=0),
        reverse=True,
    )
    slowest: list[dict[str, object]] = []
    for item in ordered[:capped]:
        slowest.append(
            {
                "timestamp": str(item.get("timestamp", "")).strip(),
                "duration_ms": _as_int(item.get("duration_ms"), default=0),
                "job_name": str(item.get("job_name", "")).strip(),
                "runtime_role": str(item.get("runtime_role", "")).strip(),
                "strategy": str(item.get("strategy", "")).strip(),
                "symbol_count": _as_int(item.get("symbol_count"), default=0),
                "use_live_runtime": bool(item.get("use_live_runtime", False)),
                "trace_id": str(item.get("trace_id", "")).strip(),
            }
        )
    return slowest


def _translate_signal_action_zh(action: str) -> str:
    mapping = {
        "buy": "买入",
        "sell": "卖出",
        "watch": "观察",
        "hold": "持有",
    }
    normalized = action.strip().lower()
    return mapping.get(normalized, action.strip() or "-")


def _week5_candidate_action_zh(
    *,
    action: str,
    suggested_position: float,
    isolated: bool,
) -> str:
    normalized = action.strip().lower()
    if isolated or suggested_position <= 0:
        if normalized == "buy":
            return "暂不执行"
        if normalized == "watch":
            return "观察"
        return "不买"
    return _action_recommendation_zh(normalized)


def _compress_series(values: list[float], max_points: int) -> list[float]:
    cleaned = [float(value) for value in values if math.isfinite(float(value))]
    if not cleaned:
        return []
    capped = max(2, max_points)
    if len(cleaned) <= capped:
        return cleaned
    if capped == 1:
        return [cleaned[-1]]
    last_index = len(cleaned) - 1
    sampled: list[float] = []
    for position in range(capped):
        idx = round(position * last_index / max(capped - 1, 1))
        sampled.append(cleaned[idx])
    return sampled


def _format_signal_reasons_zh(reasons: list[str], max_items: int = 3) -> str:
    if not reasons:
        return "无明确原因"
    translated: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        human = _translate_signal_reason_zh(reason)
        if not human or human in seen:
            continue
        seen.add(human)
        translated.append(human)
    if not translated:
        return "无明确原因"
    return "；".join(translated[: max(1, max_items)])


def _translate_signal_reason_zh(reason: str) -> str:
    raw = reason.strip()
    if not raw:
        return ""
    lowered = raw.lower()

    if lowered.startswith("lgbm<"):
        return f"LGBM 低于阈值:{raw.split('<', maxsplit=1)[1]}"
    if lowered.startswith("xgb<"):
        return f"XGB 低于阈值:{raw.split('<', maxsplit=1)[1]}"
    if lowered.startswith("meta<"):
        return f"Meta 低于阈值:{raw.split('<', maxsplit=1)[1]}"
    if lowered.startswith("model_diff>"):
        return f"模型分歧过大:{raw.split('>', maxsplit=1)[1]}"

    if ":" in raw:
        prefix, rest = raw.split(":", maxsplit=1)
        name = prefix.strip().lower()
        detail = rest.strip()
        if name == "financial_filter":
            return f"财务过滤:{_translate_financial_breach_zh(detail)}"
        if name == "financial_penalty":
            return f"财务惩罚:{_translate_financial_breach_zh(detail)}"
        if name == "data_source":
            return f"数据源:{detail or '-'}"
        if name == "capital_curve":
            return f"资金曲线:{_translate_capital_action_zh(detail)}"
        if name == "portfolio_sector_limit":
            return f"组合行业集中度限制:{detail or '-'}"
        if name == "portfolio_corr_limit":
            return f"组合相关性过高:{detail or '-'}"
        if name == "portfolio_corr_scaled":
            return f"组合相关性降仓:{detail or '-'}"
        if name in {"trend", "monster", "oversold", "event", "multi"}:
            return _translate_strategy_reason_zh(strategy=name, detail=detail)

    direct_map = {
        "hard_degraded_monitoring": "硬降级状态下保守运行",
        "soft_degraded_monitoring": "软降级状态下保守运行",
        "soup_entry": "汤匙形态入场",
        "watchlist": "命中观察池",
        "score_too_low": "评分过低",
        "risk_gate": "风险门未通过",
        "liquidity_filter": "流动性过滤",
        "liquidity_failed": "流动性不达标",
        "cross_review": "交叉复核未通过",
        "financial_filter_block": "财务过滤拦截",
        "feature_empty": "特征缺失",
        "regulatory_exclude": "监管排除",
        "regulatory_degrade": "监管降级",
        "week6_threshold_gate": "Week6 阈值门未通过",
        "week6_position_scaled": "Week6 仓位已缩放",
        "manual_pause_new_buy": "人工暂停新开仓",
        "strategy_kill_switch": "策略熔断器触发",
        "degraded_stop_new_buy": "数据源退化暂停新开仓",
        "circuit_breaker_pause": "熔断器暂停开仓",
        "circuit_breaker_reduce": "熔断器降仓",
        "drawdown_threshold": "达到回撤阈值",
        "no_buy_streak": "连续无买点",
        "risk_gate_without_buy": "风险门限制买入",
        "max_total_position": "达到总仓位上限",
        "max_stock_position": "达到单标的仓位上限",
        "low_sentiment": "情绪偏弱",
        "empty_signal": "空信号触发",
        "take_profit_stage_1_reached": "第一档止盈触发",
        "take_profit_stage_2_reached": "第二档止盈触发",
        "trailing_stop_remainder_exit": "剩余仓位追踪止损触发",
    }
    if lowered in direct_map:
        return direct_map[lowered]
    if "take_profit" in lowered:
        return "止盈限制"
    if "stop_loss" in lowered:
        return "止损限制"
    return f"原始标签:{raw}"


def _translate_strategy_reason_zh(strategy: str, detail: str) -> str:
    name = _strategy_label_zh(strategy)
    parts = [item.strip() for item in detail.split(":")]
    if len(parts) == 2 and parts[0].lower() in {"buy", "watch", "hold"}:
        action = _action_recommendation_zh(parts[0])
        score = _as_float(parts[1], default=0.0)
        return f"{name}:{action}:评分={score:.2f}"
    translated = _translate_signal_reason_zh(detail)
    if translated:
        return f"{name}:{translated}"
    return f"{name}:{detail or '-'}"


def _translate_capital_action_zh(action: str) -> str:
    mapping = {
        "normal": "正常",
        "alert": "预警",
        "reduce": "降仓",
        "freeze": "冻结",
        "degraded": "退化",
    }
    normalized = action.strip().lower()
    return mapping.get(normalized, action or "未知")


def _translate_evolution_actor_zh(actor: str) -> str:
    mapping = {
        "risk_committee": "风控委员会",
        "release_manager": "发布管理员",
        "system_watchdog": "系统看门狗",
    }
    normalized = actor.strip().lower()
    return mapping.get(normalized, actor.strip() or "-")


def _translate_evolution_ticket_status_zh(status: str) -> str:
    mapping = {
        "issued": "已签发",
        "executed": "已执行",
        "confirmed": "已确认",
        "rolled_back": "已回滚",
        "pending": "待确认",
        "not_required": "无需确认",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status.strip() or "-")


def _translate_evolution_note_zh(note: str) -> str:
    raw = note.strip()
    if not raw:
        return "-"
    lowered = raw.lower()
    mapping = {
        "post-check failed": "发布后复核未通过",
        "post-release checks passed": "发布后复核通过",
        "execution completed": "执行完成",
        "issue manual release order": "已签发人工发布单",
        "all checks passed": "全部检查通过",
        "attempt approve": "尝试审批",
    }
    if lowered in mapping:
        return mapping[lowered]
    if lowered.startswith("auto rollback: pending confirmation ttl exceeded"):
        return "超过确认时限，系统自动回滚"
    if lowered.startswith("auto rollback"):
        return "系统自动回滚"
    return raw


def _format_notification_time_zh(value: str) -> str:
    raw = value.strip()
    if not raw:
        return "-"
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw.replace("T", " ")


def _translate_financial_breach_zh(breach: str) -> str:
    mapping = {
        "st": "ST 风险",
        "delisting_risk": "退市风险",
        "missing_financial_data": "缺少财务数据",
        "missing_roe": "缺少 ROE",
        "missing_debt_ratio": "缺少资产负债率",
        "low_roe": "ROE 偏低",
        "high_debt_ratio": "资产负债率偏高",
        "invalid_symbol": "无效标的",
    }
    normalized = breach.strip().lower()
    return mapping.get(normalized, breach or "未知财务风险")


def _extract_news_component(reasons: list[str]) -> float | None:
    return _extract_component_metric(reasons=reasons, prefix="news_component:")


def _extract_component_metric(*, reasons: list[str], prefix: str) -> float | None:
    normalized_prefix = prefix.strip().lower()
    if not normalized_prefix:
        return None
    for item in reasons:
        raw = str(item).strip()
        if not raw:
            continue
        lowered = raw.lower()
        if not lowered.startswith(normalized_prefix):
            continue
        value_raw = raw.split(":", maxsplit=1)[1].strip()
        try:
            value = float(value_raw)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        return max(0.0, min(1.0, value))
    return None


def _news_component_sentiment(news_component: float) -> str:
    if news_component >= 0.67:
        return "positive"
    if news_component <= 0.33:
        return "negative"
    return "neutral"


def _m7_llm_verdict_from_sentiment(sentiment: float) -> str:
    if sentiment >= 0.05:
        return "approve"
    if sentiment <= -0.05:
        return "reject"
    return "review"


def _estimate_news_sentiment_heuristic(title: str, content: str) -> tuple[float, float]:
    text = f"{title} {content}".lower()
    positive_tokens = [
        "利好",
        "增长",
        "中标",
        "签约",
        "回购",
        "增持",
        "扭亏",
        "预增",
        "上调",
        "突破",
        "大涨",
        "盈利",
        "获批",
        "positive",
        "upgrade",
        "beat",
        "surge",
    ]
    negative_tokens = [
        "利空",
        "下滑",
        "减持",
        "亏损",
        "预亏",
        "处罚",
        "问询",
        "暴跌",
        "下调",
        "终止",
        "违约",
        "诉讼",
        "冻结",
        "negative",
        "downgrade",
        "miss",
        "plunge",
    ]
    positive_hits = sum(1 for token in positive_tokens if token in text)
    negative_hits = sum(1 for token in negative_tokens if token in text)
    if positive_hits == negative_hits == 0:
        return 0.0, 0.45
    raw_score = (positive_hits - negative_hits) / max(1, positive_hits + negative_hits)
    sentiment = _clamp(raw_score * 0.8, -1.0, 1.0)
    confidence = _clamp(0.55 + min(0.35, 0.10 * (positive_hits + negative_hits)), 0.0, 1.0)
    return sentiment, confidence


def _path_exists_in_mapping(payload: Mapping[str, object], dotted_path: str) -> bool:
    current: object = payload
    for key in dotted_path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return current is not None


def _bool_zh(value: bool) -> str:
    return "是" if value else "否"


def _truncate_text_zh(value: str, limit: int) -> str:
    text = value.strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _normalize_inline_text(value: str) -> str:
    if not value:
        return ""
    sanitized = (
        value.replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
        .replace("\u3000", " ")
    )
    return " ".join(item for item in sanitized.split(" ") if item)


def _is_plain_test_notification(title: str, content: str, *, trace_id: str = "") -> bool:
    if _normalize_inline_text(trace_id).strip():
        return False
    title_text = _normalize_inline_text(title).strip().lower()
    content_text = _normalize_inline_text(content).strip().lower()
    plain_values = {"test", "t", "c"}
    if not title_text and not content_text:
        return False
    return title_text in plain_values and content_text in plain_values


def _news_title_preview_text(*, title: str, name: str) -> str:
    normalized_title = title.strip()
    normalized_name = name.strip()
    if normalized_name and normalized_title.startswith(normalized_name):
        trimmed = normalized_title[len(normalized_name) :].lstrip("：: -")
        if trimmed:
            return trimmed
    return normalized_title


def _news_content_preview_text(*, title: str, content: str, name: str) -> str:
    normalized_content = _normalize_inline_text(content)
    if not normalized_content:
        return ""
    candidates = [
        title.strip(),
        _news_title_preview_text(title=title, name=name),
        name.strip(),
    ]
    for candidate in candidates:
        normalized_candidate = _normalize_inline_text(candidate)
        if not normalized_candidate or not normalized_content.startswith(normalized_candidate):
            continue
        trimmed = normalized_content[len(normalized_candidate) :].lstrip("：: -，。；;,.")
        if not trimmed:
            return ""
        normalized_content = trimmed
        break
    return normalized_content


def _format_news_time_short_zh(value: str) -> str:
    parsed = _parse_runtime_datetime(value)
    if parsed is None:
        raw = value.strip()
        return raw[11:16] if len(raw) >= 16 else (raw or "--:--")
    return parsed.strftime("%m-%d %H:%M")


def _parse_runtime_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _sentiment_label_zh(sentiment: float) -> str:
    if sentiment >= 0.20:
        return "偏多"
    if sentiment <= -0.20:
        return "偏空"
    return "中性"


def _format_week5_symbol_rows(rows: list[dict[str, object]], row_type: str, limit: int = 3) -> str:
    from stock_analyzer.runtime.services.week5_notification_service import (
        format_week5_symbol_rows,
    )

    return format_week5_symbol_rows(rows, row_type=row_type, limit=limit)


def _format_week5_anomaly_rows(rows: list[dict[str, object]], limit: int = 3) -> str:
    from stock_analyzer.runtime.services.week5_notification_service import (
        format_week5_anomaly_rows,
    )

    return format_week5_anomaly_rows(rows, limit=limit)


def _translate_week5_anomaly_type_zh(value: str) -> str:
    from stock_analyzer.runtime.services.week5_notification_service import (
        translate_week5_anomaly_type_zh,
    )

    return translate_week5_anomaly_type_zh(value)


def _board_stage_zh(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "first_board":
        return "首板"
    if normalized.endswith("_board"):
        prefix = normalized.removesuffix("_board")
        if prefix.isdigit():
            return f"{prefix}板"
    return value or "-"


def _notification_category_zh(category: str) -> str:
    mapping = {
        "bootstrap": "训练",
        "close": "收盘",
        "risk": "预警",
        "acceptance": "验收",
        "weekly": "周报",
        "week5": "盘中",
        "quality": "质量",
        "week6": "配置",
        "system": "系统",
        "morning": "盘前",
        "midday": "午盘前",
        "ops": "运维",
        "signal": "情报",
        "action": "行动",
        "evolution": "升级",
    }
    normalized = category.strip().lower()
    return mapping.get(normalized, category.strip() or "通知")


def _notification_summary_zh(summary: str) -> str:
    raw = summary.strip()
    lowered = raw.lower()
    if lowered == "training bootstrap recovered":
        return "训练引导已恢复"
    if lowered == "training bootstrap retry failed":
        return "训练引导重试失败"
    if lowered == "reconcile mismatch":
        return "对账异常"
    if lowered == "acceptance failed":
        return "验收失败"
    if lowered == "sim broker deviation":
        return "模拟券商偏差"
    if lowered == "intraday scan":
        return "盘中扫描"
    if lowered == "week6 data quality alert":
        return "Week6 数据质量告警"
    if lowered == "daily allocation":
        return "每日仓位配置"
    if lowered == "cloud backup recovered":
        return "云备份已恢复"
    if lowered == "cloud backup offline":
        return "云备份离线"
    if lowered == "daily strategy guide":
        return "每日策略简报"
    if lowered == "auction report":
        return "09:26竞价速报"
    if lowered == "midday news brief":
        return "午盘前新闻简报"
    if lowered == "reconcile required":
        return "需要对账"
    if lowered == "reconcile confirmation":
        return "对账确认"
    if lowered == "daily digest":
        return "每日摘要"
    if lowered == "post-market research digest":
        return "盘后研究摘要"
    if lowered == "data provider degraded":
        return "数据源退化"
    if lowered == "data provider recovered":
        return "数据源恢复"
    if lowered == "circuit breaker engaged":
        return "风控熔断已触发"
    if lowered == "circuit breaker released":
        return "风控熔断已解除"
    if lowered == "capital protection warning":
        return "资金保护线预警"
    if lowered == "capital protection recovered":
        return "资金保护线恢复"
    if lowered == "strategy kill switch triggered":
        return "策略自毁预警"
    if lowered == "strategy kill switch reset":
        return "策略自毁已解除"
    if lowered.startswith("holding alert "):
        return f"持仓预警 {raw[14:]}"
    if lowered.startswith("holding countdown "):
        return f"持仓倒计时 {raw[18:]}"
    if lowered.startswith("buy candidate "):
        return f"买入候选 {raw[14:]}"
    if lowered.startswith("sim buy rejected "):
        return f"模拟买入未成交 {raw[17:]}"
    if lowered.startswith("sim buy "):
        return f"模拟买入 {raw[8:]}"
    if lowered.startswith("sim adjust "):
        return f"模拟调仓 {raw[11:]}"
    if lowered.startswith("sim sell rejected "):
        return f"模拟卖出未成交 {raw[18:]}"
    if lowered.startswith("sim trim "):
        return f"模拟减仓 {raw[9:]}"
    if lowered.startswith("sim sell "):
        return f"模拟卖出 {raw[9:]}"
    if lowered.startswith("take profit instruction "):
        return f"止盈指令 {raw[24:]}"
    if lowered.startswith("sell instruction "):
        return f"卖出指令 {raw[17:]}"
    if lowered == "release executed":
        return "升级已执行"
    if lowered == "release confirmed":
        return "升级确认成功"
    if lowered == "release rolled back":
        return "升级已回滚"
    return raw or "系统通知"


_POST_MARKET_BULL_TAG_LABELS = {
    "高分信号": "综合评分处于高位",
    "情绪支持": "新闻/情绪仍有支撑",
    "趋势未坏": "趋势结构尚未破坏",
    "结构完整": "形态完整度较好",
    "可执行候选": "高置信度候选已出现",
    "跟踪价值": "重点标的仍具跟踪价值",
}

_POST_MARKET_BEAR_TAG_LABELS = {
    "等待确认": "仍需等待更强确认",
    "催化不足": "新闻催化不足",
    "模型一致性一般": "模型一致性一般",
    "财务约束": "财务过滤存在约束",
    "流动性一般": "流动性条件一般",
    "持仓预警": "当前持仓已有预警",
    "追价风险": "短线追价性价比一般",
}


def _metric_from_reasons(reasons: list[str], prefix: str, default: float) -> float:
    metric = _extract_component_metric(reasons=reasons, prefix=f"{prefix}:")
    if metric is None:
        return default
    return metric


def _post_market_research_rating(*, action: str, grade: str, score: float) -> str:
    normalized_action = action.strip().lower()
    normalized_grade = grade.strip().upper()
    if normalized_action == "buy":
        if normalized_grade == "S" or score >= 82.0:
            return "BUY"
        return "WATCH-BUY"
    if normalized_action == "watch":
        if normalized_grade in {"S", "A"} or score >= 70.0:
            return "WATCH-BUY"
        return "WATCH"
    if normalized_grade in {"A", "B"} or score >= 58.0:
        return "HOLD-WATCH"
    return "HOLD"


def _post_market_research_confidence(
    *,
    action: str,
    grade: str,
    score: float,
    news_component: float,
    cross_review_passed: bool,
) -> float:
    confidence = 0.42 + min(max(score, 0.0), 100.0) / 100.0 * 0.38
    if grade.strip().upper() == "S":
        confidence += 0.08
    elif grade.strip().upper() == "A":
        confidence += 0.04
    if action.strip().lower() == "buy":
        confidence += 0.05
    if news_component >= 0.60:
        confidence += 0.03
    if not cross_review_passed:
        confidence -= 0.06
    return round(_clamp(confidence, 0.35, 0.92), 2)


def _post_market_market_style(avg_score: float) -> str:
    if avg_score >= 75.0:
        return "趋势偏强"
    if avg_score >= 60.0:
        return "震荡偏强"
    if avg_score > 0:
        return "偏谨慎"
    return "中性"


def _post_market_sentiment_label(avg_news_component: float) -> str:
    if avg_news_component >= 0.65:
        return "情绪偏暖"
    if avg_news_component >= 0.55:
        return "情绪中性偏热"
    if avg_news_component <= 0.35:
        return "情绪偏冷"
    if avg_news_component <= 0.45:
        return "情绪中性偏弱"
    return "情绪中性"


def _post_market_risk_level(
    *,
    global_risk_score: float,
    reconcile_mismatch_count: int,
    holding_warn_count: int,
    recommend_buy_count: int,
) -> str:
    risk_points = 0
    if global_risk_score < 40.0:
        risk_points += 2
    elif global_risk_score < 55.0:
        risk_points += 1
    if reconcile_mismatch_count > 0:
        risk_points += 2
    if holding_warn_count > 0:
        risk_points += 1
    if recommend_buy_count == 0:
        risk_points += 1
    if risk_points >= 4:
        return "高"
    if risk_points >= 2:
        return "中"
    return "低"


def _dominant_research_phrase(
    items: list[str],
    *,
    mapping: Mapping[str, str],
    fallback: str,
) -> str:
    counts: dict[str, int] = {}
    for item in items:
        normalized = str(item).strip()
        if not normalized:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
    if not counts:
        return fallback
    top_key = max(counts.items(), key=lambda pair: (pair[1], pair[0]))[0]
    return mapping.get(top_key, top_key)


def _notification_error_text_zh(error_text: str) -> str:
    raw = error_text.strip()
    if not raw:
        return "-"
    lowered = raw.lower()
    source = "数据源"
    if "efinance" in lowered or "eastmoney" in lowered:
        source = "efinance/东方财富"
    elif "akshare" in lowered:
        source = "akshare"
    elif "bootstrap" in lowered:
        return "训练引导未完成，请查看运行日志"

    symbol = ""
    marker = " for "
    if marker in lowered:
        start = lowered.index(marker) + len(marker)
        token = raw[start:].split(":", maxsplit=1)[0].split(" ", maxsplit=1)[0].strip()
        if token:
            symbol = token

    if "unexpected_eof_while_reading" in lowered or "ssleoferror" in lowered:
        detail = "SSL 连接被对端提前断开"
    elif "ssl" in lowered:
        detail = "SSL 连接异常"
    elif "max retries exceeded" in lowered:
        detail = "请求重试次数已耗尽"
    elif "timed out" in lowered or "timeout" in lowered:
        detail = "请求超时"
    elif "connection" in lowered:
        detail = "网络连接异常"
    else:
        detail = "请求失败，请查看运行日志"

    if symbol:
        return f"{source} 请求失败（标的 {symbol}）：{detail}"
    return f"{source} 请求失败：{detail}"


def _strategy_label_zh(strategy: str) -> str:
    mapping = {
        "trend": "趋势策略",
        "monster": "龙头策略",
        "oversold": "超跌策略",
        "event": "事件策略",
        "multi": "多策略",
    }
    normalized = strategy.strip().lower()
    return mapping.get(normalized, strategy.strip() or "-")


def _strategy_weights_zh(weights: object) -> str:
    if not isinstance(weights, dict) or not weights:
        return "默认"
    parts: list[str] = []
    for key, value in weights.items():
        parts.append(f"{_strategy_label_zh(str(key))} {float(value) * 100:.0f}%")
    return " / ".join(parts)


def _news_source_zh(source: str) -> str:
    mapping = {
        "watchlist": "观察池",
        "portfolio": "持仓",
        "symbols": "指定标的",
    }
    normalized = source.strip().lower()
    return mapping.get(normalized, source or "-")


def _news_source_short_zh(source: str) -> str:
    normalized = source.strip()
    if not normalized:
        return "资讯"
    if len(normalized) <= 8:
        return normalized
    return _truncate_text_zh(normalized, 8)


def _news_component_sentiment_zh(sentiment: str) -> str:
    mapping = {
        "positive": "偏多",
        "neutral": "中性",
        "negative": "偏空",
    }
    normalized = sentiment.strip().lower()
    return mapping.get(normalized, sentiment or "-")


def _news_phase_label_zh(phase: str) -> str:
    mapping = {
        "premarket": "盘前",
        "midday": "午盘前",
        "manual": "手动",
    }
    normalized = phase.strip().lower()
    return mapping.get(normalized, phase.strip() or "盘前")


def _coerce_object_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _coerce_mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_coerce_object_mapping(item) for item in value if isinstance(item, Mapping)]


def _extract_week5_candidate_signals(report: object) -> list[dict[str, object]]:
    payload = _coerce_object_mapping(report)
    if not payload:
        return []
    rows: list[dict[str, object]] = []
    first_board = _coerce_object_mapping(payload.get("first_board"))
    for key in ("leaders", "candidates"):
        rows.extend(_coerce_mapping_list(first_board.get(key)))
    signal_pool = _coerce_object_mapping(payload.get("signal_pool"))
    rows.extend(_coerce_mapping_list(signal_pool.get("candidates")))

    signals: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in rows:
        symbol = _normalize_a_share_symbol(item.get("symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        score = _as_float(
            item.get("score"),
            default=_as_float(item.get("shortlist_score"), default=0.0),
        )
        signal = {
            "symbol": symbol,
            "strategy": str(item.get("strategy", "week5")).strip() or "week5",
            "score": score,
            "grade": str(item.get("grade", "")).strip() or _grade_by_score(score),
            "action": str(item.get("action", "")).strip().lower() or "hold",
            "reasons": _coerce_text_list(item.get("reasons")),
            "probabilities": _coerce_object_mapping(item.get("probabilities")),
            "decision_trace": _coerce_object_mapping(item.get("decision_trace")),
            "source": "week5_latest_candidates",
        }
        for key in (
            "shortlist_score",
            "execution_reranked_score",
            "execution_rerank_reason",
            "execution_rerank_applied",
            "execution_high_risk",
        ):
            if key in item:
                signal[key] = item[key]
        signals.append(signal)
    return signals


def _grade_by_score(score: float) -> str:
    if score >= 78.0:
        return "S"
    if score >= 60.0:
        return "A"
    if score >= 50.0:
        return "B"
    return "C"


def _coerce_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


def _compact_runtime_feedback_payload(payload: Mapping[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {}
    for key, value in payload.items():
        text = str(key).strip()
        if not text or value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            compact[text] = stripped
            continue
        if isinstance(value, list) and not value:
            continue
        if isinstance(value, Mapping) and not value:
            continue
        compact[text] = value
    return compact


def _as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _extract_intraday_record_fields(
    *,
    frame: pd.DataFrame,
    prefix: str,
    columns: tuple[str, ...],
) -> dict[str, object]:
    if frame.empty:
        return {
            f"{prefix}_latest_date": "",
            **{f"{prefix}_{column}": None for column in columns},
        }

    latest = frame.sort_index().iloc[-1]
    latest_date = pd.Timestamp(frame.index.max())
    payload: dict[str, object] = {f"{prefix}_latest_date": latest_date.date().isoformat()}
    for column in columns:
        payload[f"{prefix}_{column}"] = (
            _as_float(latest.get(column), default=0.0) if column in latest.index else None
        )
    return payload


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := str(item).strip())]


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(dtype=float)
    series = frame[column]
    if pd.api.types.is_float_dtype(series.dtype):
        return series.dropna()
    if pd.api.types.is_numeric_dtype(series.dtype):
        return series.astype(float).dropna()
    return pd.to_numeric(series, errors="coerce").dropna()


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _tail_return(series: pd.Series, periods: int) -> float:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if len(numeric) <= periods:
        return 0.0
    start = _as_float(numeric.iloc[-periods - 1], default=0.0)
    end = _as_float(numeric.iloc[-1], default=0.0)
    if start <= 0.0:
        return 0.0
    return end / start - 1.0


def _coerce_bool(value: object) -> bool:
    if _is_missing_scalar(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}
    try:
        return bool(value)
    except TypeError:
        return False


def _exchange_from_a_share_symbol(symbol: object) -> str:
    digits = _normalize_a_share_symbol(symbol)
    if not digits:
        return ""
    if digits.startswith(("6", "9")):
        return "SSE"
    if digits.startswith(("0", "1", "2", "3")):
        return "SZSE"
    if digits.startswith(("4", "8")):
        return "BSE"
    return ""


def _is_missing_scalar(value: object) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, (str, bytes, date, datetime, timedelta, int, float, complex)):
        return bool(pd.isna(value))
    return False


def _top_symbol_diffs(source: dict[str, float], limit: int = 5) -> list[dict[str, object]]:
    ranked = sorted(source.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [
        {
            "symbol": symbol,
            "cumulative_abs_diff": value,
        }
        for symbol, value in ranked
    ]


def _top_symbol_counts(source: dict[str, int], limit: int = 5) -> list[dict[str, object]]:
    ranked = sorted(source.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [
        {
            "symbol": symbol,
            "count": count,
        }
        for symbol, count in ranked
    ]


def _percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = q * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return (1.0 - weight) * sorted_values[lower] + weight * sorted_values[upper]


def _make_check(name: str, status: str, detail: str) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
    }


def _grade_by_threshold(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["s"]:
        return "S"
    if score >= thresholds["a"]:
        return "A"
    if score >= thresholds["b"]:
        return "B"
    return "C"


def _valid_hhmm(raw: str) -> bool:
    parts = raw.split(":")
    if len(parts) != 2:
        return False
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _parse_hhmm_window(raw: str) -> tuple[str, str] | None:
    parts = [item.strip() for item in raw.split("-", maxsplit=1)]
    if len(parts) != 2:
        return None
    start, end = parts
    if not _valid_hhmm(start) or not _valid_hhmm(end):
        return None
    start_hour, start_min = (int(x) for x in start.split(":"))
    end_hour, end_min = (int(x) for x in end.split(":"))
    start_total = start_hour * 60 + start_min
    end_total = end_hour * 60 + end_min
    if end_total < start_total:
        return None
    return start, end


def _parse_hhmm_window_interval(raw: str) -> tuple[str, str, int] | None:
    parts = [item.strip() for item in raw.split("@", maxsplit=1)]
    if len(parts) != 2:
        return None
    window, interval_raw = parts
    parsed_window = _parse_hhmm_window(window)
    if parsed_window is None:
        return None
    try:
        interval = int(interval_raw)
    except ValueError:
        return None
    if interval <= 0:
        return None
    return parsed_window[0], parsed_window[1], interval


def _limit_up_streak(bars: Any, threshold: float, cap: int) -> int:
    length = len(bars)
    if length < 2:
        return 0
    streak = 0
    for idx in range(length - 1, 0, -1):
        current = bars.iloc[idx]
        previous = bars.iloc[idx - 1]
        current_close = _as_float(current.get("close"), default=0.0)
        previous_close = _as_float(previous.get("close"), default=0.0)
        if current_close <= 0 or previous_close <= 0:
            break
        pct_change = current_close / previous_close - 1.0
        if pct_change >= threshold:
            streak += 1
            if streak >= cap:
                break
            continue
        break
    return streak


def _recent_ratio(bars: Any, column: str, lookback: int) -> float:
    if len(bars) < 2:
        return 0.0
    latest = _as_float(bars.iloc[-1].get(column), default=0.0)
    if latest <= 0:
        return 0.0
    available = min(max(1, lookback), len(bars) - 1)
    history = bars.iloc[-(available + 1) : -1]
    values: list[float] = []
    for _, row in history.iterrows():
        value = _as_float(row.get(column), default=0.0)
        if value > 0:
            values.append(value)
    if not values:
        return 0.0
    base = sum(values) / len(values)
    if base <= 0:
        return 0.0
    return latest / base


def _is_week6_quality_field_valid(field: str, value: object) -> bool:
    normalized = field.strip().lower()
    if normalized.endswith("_complete"):
        if isinstance(value, bool):
            return value
        if _is_missing_scalar(value):
            return False
        if isinstance(value, (int, float)):
            return float(value) > 0.0
        if isinstance(value, str):
            text = value.strip().lower()
            return text in {"1", "true", "yes", "y", "ok", "complete"}
        return bool(value)

    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        if not text or text in {"nan", "none", "null", "na"}:
            return False
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return not _is_missing_scalar(value)
    return not _is_missing_scalar(value)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _parse_hhmm_time(raw: str) -> dt_time:
    if not _valid_hhmm(raw):
        raise ValueError(f"invalid hh:mm: {raw}")
    hour, minute = (int(item) for item in raw.split(":"))
    return dt_time(hour=hour, minute=minute)


def _parse_hhmmss_time(raw: str) -> dt_time:
    parts = raw.split(":")
    if len(parts) == 2:
        return _parse_hhmm_time(raw)
    if len(parts) != 3:
        raise ValueError(f"invalid hh:mm:ss: {raw}")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2])
    return dt_time(hour=hour, minute=minute, second=second)


def _clock_to_seconds(clock: dt_time) -> int:
    return clock.hour * 3600 + clock.minute * 60 + clock.second


def _clock_shift_minutes(clock: dt_time, minutes: int) -> dt_time:
    base_seconds = _clock_to_seconds(clock)
    shifted = (base_seconds + minutes * 60) % (24 * 3600)
    hour = shifted // 3600
    minute = (shifted % 3600) // 60
    second = shifted % 60
    return dt_time(hour=hour, minute=minute, second=second)


def _min_clock(left: dt_time, right: dt_time) -> dt_time:
    return left if _clock_to_seconds(left) <= _clock_to_seconds(right) else right


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _apply_learning_protocol_row_caps(
    *,
    snapshots: list[SignalSnapshot],
    max_rows: int,
    per_symbol_rows_cap: int,
) -> tuple[list[SignalSnapshot], bool]:
    ordered = sorted(snapshots, key=lambda item: (item.decision_time, item.snapshot_id))
    if per_symbol_rows_cap > 0:
        snapshot_ids_to_keep: set[str] = set()
        grouped: dict[str, list[SignalSnapshot]] = {}
        for snapshot in ordered:
            symbol_key = _normalize_a_share_symbol(snapshot.symbol) or snapshot.symbol.strip()
            grouped.setdefault(symbol_key, []).append(snapshot)
        for items in grouped.values():
            for snapshot in items[-per_symbol_rows_cap:]:
                snapshot_ids_to_keep.add(snapshot.snapshot_id)
        ordered = [
            snapshot for snapshot in ordered if snapshot.snapshot_id in snapshot_ids_to_keep
        ]

    truncated = False
    if max_rows > 0 and len(ordered) > max_rows:
        ordered = ordered[-max_rows:]
        truncated = True
    return ordered, truncated


def _extract_a_share_symbols_from_frame(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return []

    best: list[str] = []
    for column in frame.columns:
        series = frame.get(column)
        if series is None:
            continue
        parsed = [
            normalized
            for normalized in (_normalize_a_share_symbol(value) for value in series.tolist())
            if normalized
        ]
        deduped = _dedupe_preserve_order(parsed)
        if len(deduped) > len(best):
            best = deduped

    if best:
        return best

    fallback_symbols: list[str] = []
    for column in list(frame.columns)[:3]:
        series = frame.get(column)
        if series is None:
            continue
        for value in series.tolist():
            normalized = _normalize_a_share_symbol(value)
            if normalized:
                fallback_symbols.append(normalized)
    return _dedupe_preserve_order(fallback_symbols)


def _pick_dataframe_column(frame: pd.DataFrame, aliases: tuple[str, ...]) -> str | None:
    normalized_columns = {str(column).strip().lower(): str(column) for column in frame.columns}
    for alias in aliases:
        candidate = normalized_columns.get(str(alias).strip().lower())
        if candidate:
            return candidate
    return None


def _parse_symbol_name_mapping(frame: object) -> dict[str, str]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {}
    code_col = _pick_dataframe_column(frame, ("code", "代码", "symbol", "证券代码"))
    name_col = _pick_dataframe_column(frame, ("name", "名称", "简称", "证券简称", "股票简称"))
    if code_col is None or name_col is None:
        return {}
    mapping: dict[str, str] = {}
    for _, row in frame.iterrows():
        symbol = _normalize_a_share_symbol(row.get(code_col))
        name = str(row.get(name_col, "")).strip()
        if symbol and name and name.lower() != "nan":
            mapping[symbol] = name
    return mapping


def _last_friday(current: date) -> date:
    # Monday=0 ... Friday=4
    weekday = current.weekday()
    offset = (weekday - 4) % 7
    return current - timedelta(days=offset)


def _safe_directory_size(root: Path) -> int:
    total = 0
    try:
        iterator = root.rglob("*")
    except OSError:
        return 0
    for path in iterator:
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _normalize_a_share_symbol(value: object) -> str:
    text = str(value).strip().upper()
    if not text:
        return ""
    primary = text.split(".", maxsplit=1)[0]
    digits = "".join(ch for ch in primary if ch.isdigit())
    if len(digits) != 6:
        digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) > 6:
        digits = digits[-6:]
    if len(digits) != 6:
        return ""
    if digits[0] not in {"0", "3", "4", "6", "8"}:
        return ""
    return digits


def _is_supported_universe_symbol(value: object) -> bool:
    normalized = _normalize_a_share_symbol(value)
    if not normalized:
        return False
    if normalized.startswith(("810", "899")):
        return False
    return True


def _filter_supported_universe_symbols(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return _dedupe_preserve_order(
        [
            normalized
            for item in values
            if (normalized := _normalize_a_share_symbol(item))
            and _is_supported_universe_symbol(normalized)
        ]
    )


def _infer_symbol_sector(symbol: str) -> str:
    digits = "".join(ch for ch in symbol if ch.isdigit())
    if len(digits) >= 3:
        return f"SEC-{digits[:3]}"
    normalized = symbol.strip().upper()
    return normalized[:6] if normalized else "UNKNOWN"


def _build_market_return_series(
    returns_by_symbol: dict[str, pd.Series],
) -> pd.Series | None:
    if not returns_by_symbol:
        return None
    frame = pd.DataFrame({key: value for key, value in returns_by_symbol.items()})
    frame = frame.dropna(axis=1, how="all")
    if frame.empty:
        return None
    market = frame.mean(axis=1, skipna=True).dropna()
    if market.empty:
        return None
    return market


def _compute_population_stability_index(
    baseline: list[float],
    current: list[float],
) -> float | None:
    baseline_series = pd.to_numeric(pd.Series(baseline), errors="coerce").dropna()
    current_series = pd.to_numeric(pd.Series(current), errors="coerce").dropna()
    if len(baseline_series) < 20 or len(current_series) < 5:
        return None

    quantiles = [idx / 10.0 for idx in range(11)]
    q_values = baseline_series.quantile(quantiles).tolist()
    unique_edges = sorted(
        {float(value) for value in q_values if value is not None and not pd.isna(value)}
    )
    if len(unique_edges) < 2:
        low = float(baseline_series.min())
        high = float(baseline_series.max())
        if math.isclose(low, high):
            return 0.0
        unique_edges = [low, high]

    interior_edges = unique_edges[1:-1]
    bins = [-float("inf"), *interior_edges, float("inf")]
    baseline_bins = pd.cut(
        baseline_series,
        bins=bins,
        include_lowest=True,
        right=True,
    )
    current_bins = pd.cut(
        current_series,
        bins=bins,
        include_lowest=True,
        right=True,
    )
    baseline_counts = baseline_bins.value_counts(sort=False)
    current_counts = current_bins.value_counts(sort=False)
    categories = baseline_counts.index
    total_base = max(int(baseline_counts.sum()), 1)
    total_current = max(int(current_counts.sum()), 1)
    epsilon = 1e-6
    category_count = max(len(categories), 1)
    psi = 0.0
    for category in categories:
        base = float(baseline_counts.get(category, 0.0))
        cur = float(current_counts.get(category, 0.0))
        base_pct = (base + epsilon) / (total_base + epsilon * category_count)
        cur_pct = (cur + epsilon) / (total_current + epsilon * category_count)
        psi += (cur_pct - base_pct) * math.log(cur_pct / base_pct)
    return float(psi)


def _baseline_type_from_status(status: Mapping[str, object]) -> str:
    predictor_mode = str(status.get("predictor_mode", "")).strip().lower()
    if predictor_mode == "controlled_heuristic":
        return "heuristic_baseline"
    lgbm_backend = str(status.get("lgbm_backend", "")).strip().lower()
    xgb_backend = str(status.get("xgb_backend", "")).strip().lower()
    if lgbm_backend.startswith("fallback") and xgb_backend.startswith("fallback"):
        return "fallback_baseline"
    return "native_baseline"


def _background_factor_coverage(bars: pd.DataFrame) -> dict[str, dict[str, float]]:
    fields = [
        "holder_count",
        "block_trade_net",
        "financing_balance",
        "margin_financing_balance",
        "northbound_net",
        "dragon_tiger_flag",
        "background_data_complete",
    ]
    coverage: dict[str, dict[str, float]] = {}
    for field in fields:
        if field not in bars.columns:
            coverage[field] = {"non_null_ratio": 0.0, "non_zero_ratio": 0.0}
            continue
        series = pd.to_numeric(pd.Series(bars[field]), errors="coerce")
        non_null_ratio = float(series.notna().mean()) if len(series) else 0.0
        non_zero_ratio = float(series.fillna(0.0).ne(0.0).mean()) if len(series) else 0.0
        coverage[field] = {
            "non_null_ratio": round(non_null_ratio, 6),
            "non_zero_ratio": round(non_zero_ratio, 6),
        }
    return coverage
