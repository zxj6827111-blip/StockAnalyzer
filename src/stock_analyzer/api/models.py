"""Request models shared by the API router submodules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:

    class BaseModel:
        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def Field(
        default: Any = ...,
        *,
        default_factory: Any | None = None,
        **kwargs: Any,
    ) -> Any: ...
else:
    from pydantic import BaseModel, Field


class PipelineRunRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    strategy: str = "trend"
    current_equity: float = 1.0
    use_live_runtime: bool = False
    dry_run_execution: bool = False
    notify_enabled: bool = True


class NotificationRequest(BaseModel):
    title: str
    content: str
    level: str = "info"
    trace_id: str = ""


class SignalQualityAuditRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)
    include_audit_events: bool = True


class CommandRequest(BaseModel):
    command_id: str
    timestamp: int
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: str


class SchedulerRunRequest(BaseModel):
    now: str | None = None
    job: str = ""
    jobs: list[str] = Field(default_factory=list)


class BlacklistSymbolRequest(BaseModel):
    symbol: str


class TdxSyncRunRequest(BaseModel):
    now: str | None = None
    force: bool = False
    notify_enabled: bool | None = None
    source_trace_id: str = ""


class WarehouseSyncRunRequest(BaseModel):
    now: str | None = None
    force: bool = False
    notify_enabled: bool | None = None
    source_trace_id: str = ""
    symbols: list[str] = Field(default_factory=list)
    retry_failed_only: bool = False
    retry_report_trace_id: str = ""


class IdleQueueRunRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class IdleQueueAckRequest(BaseModel):
    task_id: str = ""
    clear_all: bool = False
    now: str | None = None


class EvolutionRunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    dry_run: bool | None = None
    now: str | None = None
    source_trace_id: str = ""


class EvolutionDrillRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = "evolution-drill"


class EvolutionM3MaintenanceRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class EvolutionM3SearchRequest(BaseModel):
    vector: list[float] = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=200)
    source_trace_id: str = ""


class EvolutionM8SuggestRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=200)
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseAttemptRequest(BaseModel):
    days: int = Field(default=10, ge=1, le=60)
    min_runs: int = Field(default=5, ge=1, le=1000)
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseApprovalRequest(BaseModel):
    approver: str
    approved: bool
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketRequest(BaseModel):
    operator: str
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketExecuteRequest(BaseModel):
    executor: str
    ticket_id: str = ""
    note: str = ""
    confirm_window: bool = True
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketConfirmRequest(BaseModel):
    confirmer: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketRollbackRequest(BaseModel):
    rollback_by: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseConfirmationWatchdogRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class TrainModelsRequest(BaseModel):
    symbol: str = ""
    lookback_days: int = 600
    artifact_path: str | None = None
    full_market: bool = False
    max_symbols: int | None = None


class WalkForwardRequest(BaseModel):
    symbol: str
    lookback_days: int = 800


class BaselineReportRequest(BaseModel):
    symbol: str
    lookback_days: int = 800
    output_path: str | None = None


class PhaseCheckpointRequest(BaseModel):
    phase: str
    baseline_report_path: str | None = None
    output_path: str | None = None


class V13AcceptanceRequest(BaseModel):
    baseline_report_path: str | None = None
    output_path: str | None = None


class V13AcceptanceBundleRequest(BaseModel):
    symbol: str
    lookback_days: int = 800
    baseline_output_path: str | None = None
    v13_output_path: str | None = None
    run_week5_scan: bool = False
    week5_symbols: list[str] = Field(default_factory=list)


class BrokerPositionItem(BaseModel):
    symbol: str
    target_position: float = Field(ge=0.0)
    quantity: int | None = Field(default=None, ge=0)
    account: str = ""


class BrokerSnapshotRequest(BaseModel):
    positions: list[BrokerPositionItem] = Field(default_factory=list)
    source_trace_id: str = ""


class ReconcileRunRequest(BaseModel):
    now: str | None = None


class RuntimeArchiveRunRequest(BaseModel):
    force: bool = False
    now: str | None = None


class LearningRuntimeHistoryColdStartRequest(BaseModel):
    archive_dir: str = ""
    symbols: list[str] = Field(default_factory=list)
    build_manifest: bool = True
    calibration_ratio: float | None = None
    test_ratio: float | None = None


class WeekendLearningRunRequest(BaseModel):
    now: str = ""


class LearningManifestTrainingRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    load_predictor: bool = False
    register_model: bool = False


class RegisterModelArtifactRequest(BaseModel):
    artifact_path: str
    role: str = "challenger"
    lifecycle_state: str = "trained"
    source: str = "manual_register_model_artifact"
    parent_model_id: str = ""


class BootstrapActiveChampionRequest(BaseModel):
    artifact_path: str = ""
    source: str = "manual_bootstrap_active_champion"
    allow_legacy_production_artifact: bool = False
    model_id: str = ""


class ModelRegistryLifecycleRequest(BaseModel):
    model_id: str
    lifecycle_state: str
    blocked_reason: str = ""
    timestamp: str | None = None


class ModelRegistryRoleRequest(BaseModel):
    model_id: str
    role: str
    timestamp: str | None = None


class ShadowDatasetBuildRequest(BaseModel):
    model_id: str
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5


class ChampionShadowReportBuildRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    signal_threshold: float = 0.5
    include_rows: bool = False
    preview_limit: int = 5


class ShadowOnlineV2ReportBuildRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    include_rows: bool = False
    preview_limit: int = 5


class PhaseDAlphalensReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    factor_columns: list[str] = Field(default_factory=list)
    horizons: list[int] = Field(default_factory=lambda: [1, 5, 10])
    quantiles: int = 5
    output_path: str = ""


class PhaseDShapReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    prediction_column: str = "p_meta"
    baseline_importance: dict[str, float] = Field(default_factory=dict)
    drift_threshold: float = 0.25
    top_k: int = 5
    output_path: str = ""


class PhaseDCatBoostShadowReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    label_column: str = "label"
    baseline_probability_column: str = "p_meta"
    test_ratio: float = 0.3
    random_seed: int = 2026
    output_path: str = ""


class PhaseDFinbertReportRequest(BaseModel):
    records: list[dict[str, object]] = Field(default_factory=list)
    model_path: str = ""
    include_neutral: bool = True
    output_path: str = ""


class PhaseDQlibBridgeReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    label_column: str = "label"
    train_ratio: float = 0.6
    valid_ratio: float = 0.2
    output_dir: str = ""


class PhaseDTabularDeepReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    label_column: str = "label"
    baseline_probability_column: str = "p_meta"
    test_ratio: float = 0.3
    random_seed: int = 2026
    output_path: str = ""


class PhaseDTftReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    horizon: int = 1
    encoder_length: int = 5
    train_ratio: float = 0.7
    output_path: str = ""


class PhaseDFinrlReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    reward_column: str = "realized_return"
    baseline_probability_column: str = "p_meta"
    test_ratio: float = 0.3
    random_seed: int = 2026
    action_threshold: float = 0.55
    output_path: str = ""


class PhaseDHeavyTsReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    horizon: int = 3
    lookback: int = 8
    test_ratio: float = 0.3
    random_seed: int = 2026
    output_path: str = ""


class ExecutionRiskTrainRequest(BaseModel):
    artifact_path: str | None = None
    maturity_statuses: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    min_samples_per_target: int = 24
    calibration_ratio: float = 0.2
    test_ratio: float = 0.2
    epochs: int = 240
    learning_rate: float = 0.05
    l2: float = 1e-3
    seed: int = 42
    now: str | None = None


class ExecutionAwareReportBuildRequest(BaseModel):
    model_id: str
    execution_risk_artifact_path: str = ""
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5


class LearningManifestShadowValidationRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    load_predictor: bool = False
    mark_shadow_validated: bool = False


class LearningModelPromotionGateRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    preview_limit: int = 5
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False


class LearningManifestShadowPromotionGateRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    load_predictor: bool = False
    mark_shadow_validated: bool = True
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False


class LearningModelProposalRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    preview_limit: int = 5
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False
    allow_warn_status: bool = True
    source_trace_id: str = ""


class LearningManifestShadowProposalRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    load_predictor: bool = False
    mark_shadow_validated: bool = True
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False
    allow_warn_status: bool = True
    source_trace_id: str = ""


class LearningModelProposalApprovalRequest(BaseModel):
    approver: str
    approved: bool
    proposal_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelProposalRevokeRequest(BaseModel):
    revoked_by: str
    proposal_id: str = ""
    note: str = ""
    revoke_model: bool = True
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketRequest(BaseModel):
    operator: str
    proposal_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketExecuteRequest(BaseModel):
    executor: str
    ticket_id: str = ""
    note: str = ""
    confirm_window: bool = True
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketConfirmRequest(BaseModel):
    confirmer: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketRollbackRequest(BaseModel):
    rollback_by: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseConfirmationWatchdogRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class DashboardQuickCommandRequest(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    command_id: str = ""


class DashboardQuickReconcileRequest(BaseModel):
    positions: list[BrokerPositionItem] = Field(default_factory=list)
    run_reconcile: bool = True
    source_trace_id: str = ""


class DashboardOpsToggleRequest(BaseModel):
    enabled: bool


class Week4AcceptanceRunRequest(BaseModel):
    sla_recent_runs: int = Field(default=50, ge=1, le=1000)
    export_enabled: bool | None = None
    notify_enabled: bool | None = None


class Week5ScanRunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    notify_enabled: bool | None = None
    sync_watchlist: bool | None = None
    sync_reason: str = ""
    recovery_mode: bool = Field(
        default=False,
        description="Explicit emergency recovery scan: advisory only, never "
        "feeds final signals/watchlist; bypasses a stale-snapshot block",
    )


class Week5AutomationRunRequest(BaseModel):
    snapshot_id: str = ""
    now: str = ""
    notify_enabled: bool = False
    sync_watchlist: bool = True


class Week6RunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    notify_enabled: bool | None = None


class Week6DataQualityRunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    lookback_days: int | None = Field(default=None, ge=20, le=500)
    notify_enabled: bool | None = None
    source_trace_id: str = ""


class Week6GlobalSnapshotRequest(BaseModel):
    us_index_change_pct: float = 0.0
    a50_change_pct: float = 0.0
    usd_cnh_change_pct: float = 0.0
    commodity_change_pct: float = 0.0
    a_share_correlation: float = 0.60
    source_trace_id: str = ""


class Week6RegulatoryEntry(BaseModel):
    symbol: str
    tag: str = ""
    note: str = ""


class Week6RegulatoryWatchlistRequest(BaseModel):
    entries: list[Week6RegulatoryEntry] = Field(default_factory=list)
    source_trace_id: str = ""


class Week7StrategyPerformanceRequest(BaseModel):
    month: str
    strategy: str
    strategy_return: float
    benchmark_return: float
    note: str = ""
    source_trace_id: str = ""


class Week7KillSwitchResetRequest(BaseModel):
    strategy: str = ""
    resume_new_buy: bool = False
    source_trace_id: str = ""


class Week7CloudBackupPingRequest(BaseModel):
    source: str = "manual"
    source_trace_id: str = ""


class Week7CloudBackupCheckRequest(BaseModel):
    now: str = ""
    source_trace_id: str = ""


class Week7FactorFeatureItem(BaseModel):
    name: str
    importance: float


class Week7FactorLifecycleRecordRequest(BaseModel):
    month: str
    strategy: str
    top_features: list[Week7FactorFeatureItem] = Field(default_factory=list)
    psr: float
    ic_mean: float = 0.0
    note: str = ""
    source_trace_id: str = ""


class Week7FactorLifecycleResetRequest(BaseModel):
    strategy: str = ""
    source_trace_id: str = ""


class Week7SimBrokerRunRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=30)
    export_enabled: bool | None = None
    notify_enabled: bool | None = None
    source_trace_id: str = ""


class NewsScoreCacheClearRequest(BaseModel):
    symbol: str = ""
    strategy: str = ""


class AsofBacktestRunRequest(BaseModel):
    """POST /backtest/asof-scan 请求体（PLAN Task 5）。

    ``date`` 用于单日回测；``start_date``/``end_date`` 用于日期区间（含端点）。
    两者至少要提供一种——路由层负责校验并在缺失/冲突时返回 400，而不是让
    校验散落在多个可选字段的隐式组合里。
    """

    date: str = ""
    start_date: str = ""
    end_date: str = ""
    symbols: list[str] = Field(default_factory=list)
    top_n: int | None = Field(default=None, ge=1, le=500)
    horizon_days: int | None = Field(default=None, ge=1, le=60)

