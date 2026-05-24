"""Configuration loader and schema for StockAnalyzer."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field, field_validator

load_dotenv()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppConfig(_StrictModel):
    timezone: str = "Asia/Shanghai"
    mode: str = "simulation"
    advisory_only: bool = False


class DataSourceConfig(_StrictModel):
    primary: str = "akshare"
    local_data_root: str = ""
    warehouse_db_path: str = ""
    enable_cache_fallback: bool = True
    switch_after_failures: int = 3
    request_interval_sec: float = 0.5
    degrade_stops_new_buy: bool = True
    runtime_live_enabled: bool = True
    runtime_live_provider: str = "sina"
    runtime_live_interval_priority: list[str] = Field(default_factory=lambda: ["1m", "5m"])
    runtime_live_timeout_sec: int = 3
    runtime_live_cache_ttl_sec: int = 30
    runtime_live_session_only: bool = True


class MarketDepthConfig(_StrictModel):
    enabled: bool = True
    primary: str = "easyquotation_sina"
    backup: str = "mootdx"
    cache_ttl_sec: int = 5
    timeout_sec: int = 5
    max_symbols_per_poll: int = 100
    poll_scopes: list[str] = Field(default_factory=lambda: ["watchlist", "signal_pool"])


class TdxSyncConfig(_StrictModel):
    enabled: bool = True
    auto_run: bool = True
    run_time: str = "18:20"
    vipdoc_root: str = ""
    output_root: str = "artifacts/imports/tdx_offline_package"
    include_bj: bool = True
    skip_gp: bool = False
    max_symbols: int = 0
    timeout_sec: int = 7200
    history_limit: int = 30
    refresh_before_evolution: bool = True
    block_evolution_on_failure: bool = False
    notify_on_success: bool = False
    notify_on_failure: bool = True


class MarketWarehouseConfig(_StrictModel):
    enabled: bool = True
    auto_run: bool = True
    run_time: str = "21:45"
    post_followup_enabled: bool = True
    post_followup_retry_failed_enabled: bool = True
    post_followup_min_latest_trade_date_coverage_ratio: float = 0.95
    post_followup_run_week5: bool = True
    post_followup_week5_sync_top_k: int = 50
    post_followup_force_universe_scan: bool = False
    post_followup_scan_profile: str = "post_warehouse_full_refresh"
    post_followup_run_learning_backfill: bool = True
    post_followup_run_training: bool = True
    post_followup_run_phase_d_tabular_deep: bool = True
    db_path: str = "artifacts/warehouse/market.duckdb"
    package_root: str = "artifacts/warehouse/package"
    bootstrap_source_root: str = "artifacts/imports/tdx_offline_package"
    bootstrap_on_first_sync: bool = True
    offline_bootstrap_enabled: bool = False
    online_bootstrap_lookback_days: int = 750
    online_daily_primary: str = "akshare"
    online_daily_backup: str = "efinance"
    request_interval_sec: float = 0.6
    online_socket_timeout_sec: float = 6.0
    daily_symbol_hard_timeout_sec: float = 20.0
    daily_symbol_hard_timeout_sec_full_universe: float = 20.0
    online_max_attempts: int = 1
    daily_lookback_days: int = 120
    daily_incremental_enabled: bool = True
    daily_incremental_cushion_days: int = 5
    max_symbols: int = 0
    intraday_sync_enabled: bool = True
    intraday_intervals: list[str] = Field(default_factory=lambda: ["1m", "5m"])
    intraday_sync_scope: str = "focus"
    intraday_focus_max_symbols: int = 80
    history_limit: int = 30
    refresh_before_evolution: bool = True
    block_evolution_on_failure: bool = False
    notify_on_success: bool = False
    notify_on_failure: bool = True


class CacheConfig(_StrictModel):
    enabled: bool = True
    backend: str = "memory"
    redis_url: str = ""
    ttl_sec: int = 60


class DataHealthConfig(_StrictModel):
    window_size: int = 20
    min_success_rate: float = 0.95
    max_latency_sec: float = 120.0


class LiquidityFilterConfig(_StrictModel):
    min_daily_turnover: float
    min_float_market_cap: float
    max_turnover_rate: float


class FinancialFilterConfig(_StrictModel):
    enabled: bool = True
    exclude_st: bool = True
    exclude_delisting_risk: bool = True
    min_roe: float = 0.03
    max_debt_ratio: float = 0.75
    apply_to: list[str] = Field(default_factory=lambda: ["trend", "oversold"])
    trend_mode: str = "score_penalty"
    trend_penalty: float = 6.0
    monster_mode: str = "score_penalty"
    monster_penalty: float = 10.0
    missing_data_policy: str = "allow"


class CrossReviewConfig(_StrictModel):
    p_lgbm_min: float = 0.60
    p_xgb_min: float = 0.55
    max_diff: float = 0.18
    p_meta_min: float = 0.54
    champion_auc_low: float = 0.55
    champion_auc_high: float = 0.62
    relax_threshold_delta: float = 0.02
    relax_max_diff_delta: float = 0.03
    tighten_threshold_delta: float = 0.01
    tighten_max_diff_delta: float = 0.02
    degraded_consensus_enabled: bool = True
    degraded_lgbm_saturation_min: float = 0.995
    degraded_xgb_min: float = 0.36
    degraded_meta_min: float = 0.56
    degraded_merged_min: float = 0.62


class ModelsConfig(_StrictModel):
    calibration: str = "isotonic"
    cross_review: CrossReviewConfig
    overfit_gap_threshold: float = 0.15
    include_random_feature_baseline: bool = True
    recent_data_weight_years: int = 3


class ScoreThresholdConfig(_StrictModel):
    s: float = 78.0
    a: float = 60.0
    b: float = 50.0


class ScoreConfig(_StrictModel):
    weights: dict[str, float]
    thresholds: ScoreThresholdConfig


class StrategyScoreConfig(_StrictModel):
    thresholds: ScoreThresholdConfig
    weights: dict[str, float]


class SoupStrategyConfig(_StrictModel):
    entry_mode: str = "tail_confirm"
    entry_window: list[str] = Field(default_factory=lambda: ["14:30", "14:50"])
    take_profit: list[float] = Field(default_factory=lambda: [5.0, 8.0, 12.0])
    stop_loss: float = 5.0
    trailing_stop: float = 5.0
    max_hold_days: int = 10
    max_holdings: int = 3
    max_same_sector: int = 2
    dynamic_position: str = "min(0.15, 0.02/(ATR14/close))"
    recovery_buy_enabled: bool = True
    recovery_min_score: float = 50.0
    recovery_max_position: float = 0.03
    recovery_allowed_grades: list[str] = Field(default_factory=lambda: ["S", "A", "B"])


class CapitalCurveConfig(_StrictModel):
    drawdown_alert: float = 5.0
    drawdown_reduce: float = 10.0
    drawdown_freeze: float = 15.0
    protect_line: float = 0.95


class CircuitBreakerConfig(_StrictModel):
    intraday_stop_after_losses: int = 2
    consecutive_fail_reduce: int = 5
    consecutive_fail_pause: int = 10
    portfolio_daily_drawdown_stop: float = 2.5
    portfolio_weekly_drawdown_reduce: float = 4.0


class MonsterRiskConfig(_StrictModel):
    max_total_position: float = 0.25
    max_stock_position: float = 0.08
    disable_if_sentiment_below: float = 45.0


class Week5Config(_StrictModel):
    enabled: bool = True
    auto_run: bool = True
    auto_notify: bool = True
    history_limit: int = 500
    market_radar_enabled: bool = True
    market_radar_notify: bool = True
    market_radar_universe_max_symbols: int = 600
    market_radar_scan_top_n: int = 80
    market_radar_notify_top_k: int = 5
    market_radar_review_pool_max_symbols: int = 80
    market_radar_review_pool_retention_hours: float = 72.0
    market_radar_min_baseline_score: float = 55.0
    market_radar_window_intervals: list[str] = Field(
        default_factory=lambda: ["09:35-11:20@10", "13:05-14:50@10"]
    )
    universe_prefilter_enabled: bool = True
    universe_prefilter_lookback_days: int = 240
    universe_prefilter_top_k: int = 500
    universe_prefilter_shortlist_top_n: int = 50
    monster_scan_intraday_max_symbols: int = 15
    monster_scan_max_symbols: int = 120
    monster_scan_sla_target_ms: int = 900_000
    monster_scan_sla_alert_target_ms: int = 600_000
    offhours_universe_refresh_enabled: bool = True
    offhours_weekday_universe_max_symbols: int = 300
    offhours_research_pool_top_k: int = 200
    offhours_friday_full_deep_scan_enabled: bool = False
    offhours_weekend_full_deep_scan_enabled: bool = False
    offhours_weekend_universe_max_symbols: int = 500
    offhours_force_full_deep_scan_on_watchlist_below: int = 25
    offhours_force_full_deep_scan_on_no_buy_streak: int = 0
    offhours_force_full_deep_scan_on_drawdown_pct: float = 15.0
    offhours_watchlist_sync_top_k: int = 50
    auto_sync_watchlist: bool = True
    auto_sync_watchlist_top_k: int = 50
    auto_sync_watchlist_min_score: float = 65.0
    auto_sync_watchlist_allowed_actions: list[str] = Field(
        default_factory=lambda: ["buy", "watch"]
    )
    auto_sync_watchlist_keep_if_empty: bool = True
    auto_sync_watchlist_empty_grace_runs: int = 1
    auto_sync_watchlist_preserve_max_age_hours: float = 18.0
    first_board_interval_min: int = 1
    first_board_window_intervals: list[str] = Field(default_factory=list)
    first_board_windows: list[str] = Field(default_factory=lambda: ["09:30-10:30", "13:00-14:00"])
    live_runtime_window_intervals: list[str] = Field(
        default_factory=lambda: [
            "09:30-10:30@5",
            "10:30-11:30@5",
            "13:00-14:00@5",
            "14:00-14:57@5",
        ]
    )
    live_runtime_max_symbols: int = 8
    live_runtime_auto_cap_enabled: bool = True
    live_runtime_auto_cap_min_symbols: int = 6
    live_runtime_auto_cap_window_runs: int = 5
    live_runtime_auto_cap_safety_ratio: float = 0.75
    live_runtime_backpressure_enabled: bool = True
    live_runtime_backpressure_threshold_ms: int = 60_000
    live_runtime_backpressure_cooldown_min: int = 5
    first_board_limit_up_pct: float = 0.095
    consecutive_limit_up_cap: int = 5
    anomaly_gap_pct: float = 0.08
    anomaly_volume_ratio: float = 2.5
    anomaly_shadow_pct: float = 0.06
    empty_signal_drawdown_pct: float = 10.0
    empty_signal_no_buy_runs: int = 5


class HolidayRiskConfig(_StrictModel):
    pre_holiday_reduce_days: int = 3
    max_position_multiplier: float = 0.5


class GlobalMarketConfig(_StrictModel):
    enabled: bool = True
    correlation_decay_threshold: float = 0.45
    threshold_adjust_max: float = 2.0
    position_adjust_max_pct: float = 0.10


class RegulatoryFactorConfig(_StrictModel):
    enabled: bool = True
    action: str = "auto_degrade_or_exclude"
    penalty_score: float = 10.0
    exclude_tags: list[str] = Field(default_factory=lambda: ["watchlist", "inquiry"])


class Week6MainForceConfig(_StrictModel):
    lookback_days: int = 60
    strong_score: float = 65.0


class LimitRuleVersionEntry(_StrictModel):
    from_date: str = Field(alias="from")
    board: str
    limit_pct: float | None = None
    ipo_no_limit_days: int = 0


class CostScheduleEntry(_StrictModel):
    from_date: str = Field(alias="from")
    stamp_tax_rate: float = 0.0005


class LimitRuleConfig(_StrictModel):
    use_source_first: bool = True
    fallback_by_board: bool = True
    rule_version_by_date: list[LimitRuleVersionEntry] = Field(default_factory=list)
    cost_schedule_by_date: list[CostScheduleEntry] = Field(default_factory=list)


class Week6AllocationProfilesConfig(_StrictModel):
    trend: dict[str, float] = Field(
        default_factory=lambda: {"trend": 0.70, "oversold": 0.10, "event": 0.20}
    )
    range: dict[str, float] = Field(
        default_factory=lambda: {"trend": 0.30, "oversold": 0.40, "event": 0.30}
    )
    crash: dict[str, float] = Field(
        default_factory=lambda: {"trend": 0.10, "oversold": 0.60, "event": 0.30}
    )


class Week6Config(_StrictModel):
    enabled: bool = True
    auto_run: bool = True
    auto_notify: bool = True
    run_time: str = "15:25"
    data_prewarm_enabled: bool = True
    data_prewarm_time: str = "20:20"
    data_prewarm_lookback_days: int = 120
    data_quality_warn_threshold: float = 0.85
    data_quality_critical_threshold: float = 0.65
    data_quality_notify: bool = True
    data_quality_history_limit: int = 240
    data_quality_fields: list[str] = Field(
        default_factory=lambda: [
            "financial_data_complete",
            "roe",
            "debt_ratio",
            "holder_count",
            "block_trade_net",
            "financing_balance",
            "northbound_net",
            "dragon_tiger_flag",
            "background_data_complete",
        ]
    )
    data_quality_core_fields: list[str] = Field(
        default_factory=lambda: [
            "financial_data_complete",
            "roe",
            "debt_ratio",
            "holder_count",
            "financing_balance",
            "background_data_complete",
        ]
    )
    history_limit: int = 500
    main_force: Week6MainForceConfig = Field(default_factory=Week6MainForceConfig)
    allocation_profiles: Week6AllocationProfilesConfig = Field(
        default_factory=Week6AllocationProfilesConfig
    )


class StrategyKillSwitchConfig(_StrictModel):
    enabled: bool = True
    underperform_months: int = 3
    history_limit: int = 240
    auto_pause_new_buy: bool = True


class CloudBackupConfig(_StrictModel):
    enabled: bool = True
    ping_interval_min: int = 10
    alert_after_offline_min: int = 15
    notify_recovery: bool = True
    require_first_ping_before_alert: bool = True


class FactorLifecycleConfig(_StrictModel):
    enabled: bool = True
    shap_drift_threshold: float = 0.25
    psr_min: float = 0.60
    history_limit: int = 240
    graveyard_enabled: bool = True
    graveyard_observation_months: int = 2


class SimBrokerWeeklyConfig(_StrictModel):
    enabled: bool = True
    history_limit: int = 240
    export_enabled: bool = True
    export_dir: str = "artifacts/week7/sim_broker_weekly"
    auto_notify: bool = True


class NotificationsConfig(_StrictModel):
    enabled: bool = True
    primary: str = "console"
    backup: str = "console"
    pushplus_token: str = ""
    wecom_webhook: str = ""
    feishu_webhook: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_app_receive_id: str = ""
    feishu_app_receive_id_type: str = "open_id"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_thread_id: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 465
    email_use_ssl: bool = True
    email_starttls: bool = False
    email_sender: str = ""
    email_password: str = ""
    email_receivers: list[str] = Field(default_factory=list)
    custom_webhook_url: str = ""
    custom_webhook_bearer_token: str = ""
    timeout_sec: int = 5

    @field_validator("primary", "backup")
    @classmethod
    def _validate_channel_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        supported = {
            "console",
            "pushplus",
            "wecom",
            "wechat",
            "feishu",
            "lark",
            "feishu_app",
            "lark_app",
            "telegram",
            "tg",
            "email",
            "smtp",
            "custom",
            "webhook",
            "custom_webhook",
        }
        if normalized not in supported:
            supported_text = ",".join(sorted(supported))
            raise ValueError(
                f"unsupported notification channel: {value} (supported: {supported_text})"
            )
        return normalized

    @field_validator("feishu_app_receive_id_type")
    @classmethod
    def _validate_feishu_app_receive_id_type(cls, value: str) -> str:
        normalized = value.strip().lower() or "open_id"
        supported = {"chat_id", "email", "open_id", "union_id", "user_id"}
        if normalized not in supported:
            supported_text = ",".join(sorted(supported))
            raise ValueError(
                "unsupported feishu_app_receive_id_type: "
                f"{value} (supported: {supported_text})"
            )
        return normalized


class WeComInteractionConfig(_StrictModel):
    enabled: bool = False
    token: str = ""
    verify_signature: bool = True
    allowed_users: list[str] = Field(default_factory=list)
    auto_reconcile_after_broker_snapshot: bool = True
    encoding_aes_key: str = ""
    receive_id: str = ""
    enforce_receive_id: bool = False


class FeishuInteractionConfig(_StrictModel):
    enabled: bool = False
    subscription_mode: str = "webhook"
    verification_token: str = ""
    allowed_users: list[str] = Field(default_factory=list)
    auto_reconcile_after_broker_snapshot: bool = True

    @field_validator("subscription_mode")
    @classmethod
    def _validate_subscription_mode(cls, value: str) -> str:
        normalized = value.strip().lower() or "webhook"
        supported = {"webhook", "long_connection", "long_conn", "ws", "websocket"}
        if normalized not in supported:
            supported_text = ",".join(sorted(supported))
            raise ValueError(
                "unsupported feishu_interaction.subscription_mode: "
                f"{value} (supported: {supported_text})"
            )
        if normalized in {"long_conn", "ws", "websocket"}:
            return "long_connection"
        return normalized


class NotificationFilterConfig(_StrictModel):
    enabled: bool = True
    cooldown_sec: int = 300
    min_score: float = 60.0
    min_score_by_action: dict[str, float] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=lambda: ["buy", "watch"])
    max_signals_per_run: int = 5
    quiet_windows: list[str] = Field(default_factory=list)
    dedup_by_symbol_action: bool = True
    t_day_entry_silence_enabled: bool = True
    t_day_silence_reason_keywords: list[str] = Field(
        default_factory=lambda: ["take_profit", "stop_loss"]
    )


class CommandChannelConfig(_StrictModel):
    enabled: bool = True
    secret_key: str = "change-me"
    dedup_ttl_sec: int = 86400
    max_clock_skew_sec: int = 300
    state_persist_enabled: bool = True
    state_persist_path: str = "artifacts/runtime/runtime_state.json"
    history_archive_enabled: bool = True
    history_archive_dir: str = "artifacts/runtime/history"
    history_archive_retention_days: int = 30
    history_archive_max_records: int = 2000


class SchedulerConfig(_StrictModel):
    enabled: bool = True
    premarket_time: str = "08:30"
    midday_news_time: str = "12:30"
    auction_report_time: str = "09:26"
    close_reconcile_time: str = "15:30"
    week4_acceptance_time: str = "20:35"
    week6_daily_time: str = "15:25"


class LabelsConfig(_StrictModel):
    primary: str = "soup_10d_tp8_before_sl5"
    take_profit_pct: float = 0.08
    stop_loss_pct: float = 0.05
    horizon_days: int = 10
    exclude_untradable: bool = True
    pnl_price_basis: str = "close"
    conflict_policy: str = "bar_shape_heuristic"
    conflict_soft_label_value: float = 0.5


class MarketRelativeFeatureConfig(_StrictModel):
    enabled: bool = False
    benchmark_symbol: str = "000300"
    fallback_symbol: str = "399001"


class TrainingConfig(_StrictModel):
    enabled: bool = True
    min_samples: int = 200
    validation_ratio: float = 0.2
    calibration_ratio: float = 0.1
    test_ratio: float = 0.1
    embargo_days: int = 0
    precision_at_k_ratio: float = 0.1
    learning_feedback_weighting_enabled: bool = True
    learning_feedback_weight_clip_low: float = 0.35
    learning_feedback_weight_clip_high: float = 3.0
    artifact_path: str = "artifacts/model_v1.json"
    baseline_report_path: str = "artifacts/acceptance/baseline_report.json"
    bootstrap_auto_run_on_first_start: bool = True
    bootstrap_require_completion_for_runtime: bool = True
    bootstrap_full_market: bool = True
    bootstrap_lookback_days: int = 2500
    bootstrap_batch_size: int = 50
    bootstrap_max_symbols: int = 0
    bootstrap_dataset_max_rows: int = 500_000
    bootstrap_per_symbol_rows_cap: int = 500
    bootstrap_auto_seed_watchlist: bool = True
    bootstrap_seed_watchlist_size: int = 200
    bootstrap_retry_enabled: bool = True
    bootstrap_retry_interval_min: int = 15
    bootstrap_retry_notify: bool = True
    bootstrap_seed_symbols: list[str] = Field(
        default_factory=lambda: [
            "600000",
            "600036",
            "600519",
            "601318",
            "600276",
            "600887",
            "601166",
            "601688",
            "601888",
            "300750",
            "300059",
            "000001",
            "000333",
            "000858",
            "000977",
            "002415",
            "002594",
            "688981",
            "601899",
            "603259",
        ]
    )
    bootstrap_state_path: str = "artifacts/training/bootstrap_state.json"


class AutoPromotionConfig(_StrictModel):
    enabled: bool = False
    auto_load_predictor: bool = True
    notify_on_rejection: bool = True
    notify_on_training_summary: bool = True
    notify_on_manual_release_pending: bool = True


class BacktestMatcherConfig(_StrictModel):
    enforce_t_plus_1: bool = True
    reject_limit_up_buy: bool = True
    reject_limit_down_sell: bool = True
    suspended_defer: bool = True
    stop_loss_next_tradable: bool = True
    dynamic_slippage_enabled: bool = True
    max_dynamic_slippage_ratio: float = 0.012
    slippage_by_strategy: dict[str, float] = Field(
        default_factory=lambda: {"trend": 0.0015, "monster": 0.0025}
    )
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.0005
    stamp_tax_apply_on: str = "sell_only"
    transfer_fee_rate: float = 0.00001
    min_commission_per_order: float = 5.0
    max_exit_carry_days: int = 8
    forced_liquidation_discount_bp: int = 50
    share_rounding_rule: str = "lot_down_100"
    price_tick_rule: str = "exchange_tick"
    min_notional_per_order: float = 5000.0
    residual_order_policy: str = "day_cancel_then_recalc"


class WalkForwardConfig(_StrictModel):
    enabled: bool = True
    train_window: int = 120
    test_window: int = 20
    step: int = 20
    decision_threshold: float = 0.60


class ReconcileConfig(_StrictModel):
    enabled: bool = True
    position_tolerance: float = 0.01
    require_broker_snapshot_at_close: bool = True
    auto_notify_on_mismatch: bool = True
    history_limit: int = 500


class AcceptanceConfig(_StrictModel):
    enabled: bool = True
    auto_run: bool = True
    auto_notify: bool = True
    notify_on_pass: bool = False
    sla_recent_runs: int = 100
    runtime_sla_recent_runs: int = 10
    export_enabled: bool = True
    export_dir: str = "artifacts/acceptance"
    history_limit: int = 500


class EvolutionExecutionSpecConfig(_StrictModel):
    settlement_lag: int = 1
    price_series_mode: str = "qfq"
    dividend_treatment: str = "implicit_by_qfq"
    day_window: str = "09:35-14:55"
    open_window: str = "09:35-10:30"
    sensitivity_threshold_bp: int = 30
    sensitivity_days: int = 5
    max_search_days: int = 8
    max_exit_carry_days: int = 8
    forced_liquidation_discount_bp: int = 50
    impact_eta: float = 0.60
    impact_k_tier_table: dict[str, float] = Field(
        default_factory=lambda: {"large": 0.08, "mid": 0.12, "small": 0.18}
    )
    limit_buffer_bp_tier_table: dict[str, float] = Field(
        default_factory=lambda: {"large": 10.0, "mid": 15.0, "small": 20.0}
    )
    slippage_tier_table: dict[str, float] = Field(
        default_factory=lambda: {"large": 3.0, "mid": 6.0, "small": 10.0}
    )
    participation_cap_tier_table: dict[str, float] = Field(
        default_factory=lambda: {"large": 0.02, "mid": 0.01, "small": 0.005}
    )
    order_amount_mode: str = "target_delta_weight_nav"
    ref_quote_rule: str = "t1_first_trade_0935"
    share_rounding_rule: str = "lot_down_100"
    price_tick_rule: str = "exchange_tick"
    min_notional_per_order: float = 5000.0
    residual_order_policy: str = "day_cancel_then_recalc"


class EvolutionUniverseSpecConfig(_StrictModel):
    universe_ruleset_id: str = "a_share_default_v1"
    board_scope: list[str] = Field(default_factory=lambda: ["SSE", "SZSE"])
    min_list_days: int = 60
    signal_analysis_lookback_days: int = 240
    signal_fetch_lookback_days: int = 500
    first_board_scan_lookback_days: int = 60
    st_filter_rule: str = "exclude"
    suspension_filter_rule: str = "exclude_ge_20d"


class EvolutionRuntimeSpecConfig(_StrictModel):
    max_data_latency_sec: int = 120
    latency_required_inputs: list[str] = Field(
        default_factory=lambda: [
            "market_price",
            "suspension_status",
            "industry_mapping",
            "adjustment_factor",
        ]
    )
    latency_formula_version: str = "max_latency_v1"
    latency_violation_policy: str = "watch_or_limited_observability"
    latency_watch_policy: str = "raise_u_threshold_and_block_online_update"
    random_seed: int = 42
    num_threads: int = 1
    deterministic_mode: bool = True
    library_versions_hash: str = "auto"
    max_online_samples_per_day: int = 1500
    cooldown_days: int = 3
    online_handoff_mode: str = "rebase_then_replay"
    online_replay_days: int = 5
    promotion_min_healthy_days: int = 3
    dual_eval_profile_set_id: str = "dual_eval_v1"
    trading_no_fill_ratio_limit: float = 0.20
    trading_partial_fill_ratio_limit: float = 0.35
    trading_no_fill_ratio_delta_limit: float = 0.05
    trading_partial_fill_ratio_delta_limit: float = 0.08
    trading_no_fill_ratio_baseline: float = 0.12
    trading_partial_fill_ratio_baseline: float = 0.20
    min_samples_per_bucket: int = 300
    mapping_fallback_order: list[str] = Field(
        default_factory=lambda: [
            "regime_x_liquidity_x_volatility",
            "regime_x_liquidity",
            "regime",
            "global",
        ]
    )
    mapping_update_cooldown_days: int = 3
    mapping_ema_alpha: float = 0.30
    k_base: int = 20
    k_min: int = 8
    dynamic_k_turnover_limit: float = 1.0
    dynamic_k_participation_cap: float = 0.01
    position_drift_alert_threshold: float = 0.05
    position_drift_raise_u_threshold_bp: int = 20
    position_drift_consecutive_days_trigger: int = 3


class EvolutionConfig(_StrictModel):
    enabled: bool = True
    auto_run: bool = True
    dry_run: bool = True
    dry_run_policy: str = "auto"
    dry_run_live_modes: list[str] = Field(default_factory=lambda: ["production"])
    validation_require_dry_run: bool = True
    offhours_time: str = "20:40"
    history_limit: int = 500
    report_dir: str = "artifacts/evolution/history"
    runtime_controls_max_age_hours: float = 36.0
    suggestions_dir: str = "suggestions"
    compliance_db_path: str = "artifacts/evolution/compliance.duckdb"
    manifest_path: str = "artifacts/evolution/run_manifest.json"
    m2_state_path: str = "artifacts/evolution/m2_state.json"
    m2_optuna_enabled: bool = True
    m2_optuna_trials: int = 48
    m2_optuna_min_samples: int = 20
    m2_optuna_min_improvement: float = 0.01
    m2_optuna_random_seed: int = 42
    m2_optuna_history_limit: int = 180
    m2_optuna_retrain_interval_days: int = 7
    m2_optuna_artifact_dir: str = "suggestions/m2/hmm_params"
    m3_store_dir: str = "artifacts/evolution/m3"
    m3_maintenance_interval_min: int = 180
    m3_active_vector_profile_id: str = "m3_price_shape_execution_v2"
    m3_allow_active_profile_fallback: bool = False
    m4_inflow_ratio_gate: float = 0.02
    m4_concentration_warn: float = 0.45
    m4_score_concentration_penalty: float = 12.0
    m5_label_coverage_floor: float = 0.40
    m5_positive_ratio_low: float = 0.30
    m5_positive_ratio_high: float = 0.70
    m5_seed_consistency_floor: float = 0.70
    m5_alignment_floor: float = 0.52
    m5_limited_observability_score: float = 62.0
    m5_label_records_path: str = ""
    m5_strategy_min_labeled_samples: int = 5
    m6_sell_pressure_gate: float = 0.58
    m6_bearish_ratio_gate: float = 0.55
    m6_rejection_shadow_gate: float = 0.55
    m7_news_records_path: str = ""
    m7_dedup_similarity_threshold: float = 0.85
    m7_daily_budget: float = 15.0
    m7_default_event_cost: float = 0.20
    m7_sentiment_floor: float = 0.05
    m7_budget_warn_utilization: float = 0.80
    m7_max_clusters_in_report: int = 5
    m7_embedding_backend: str = "bge_m3_hash"
    m7_embedding_dim: int = 24
    m7_embedding_required: bool = False
    m7_allow_records_fallback: bool = False
    m7_market_proxy_fallback_enabled: bool = False
    m7_ledger_db_path: str = "artifacts/evolution/m7_event_ledger.duckdb"
    m7_ledger_archive_dir: str = "artifacts/evolution/m7_event_ledger_archive"
    m7_ledger_ttl_days: int = 14
    m7_pipeline_max_age_days: int = 3
    m7_pipeline_half_life_hours: float = 24.0
    m7_pipeline_confidence_floor: float = 0.25
    m7_live_news_enabled: bool = False
    m7_live_news_provider: str = "akshare_em"
    m7_live_news_max_symbols: int = 24
    m7_live_news_per_symbol_limit: int = 5
    m7_live_news_max_age_hours: float = 24.0
    m7_live_news_artifact_max_records: int = 2000
    m7_ai_review_enabled: bool = False
    m7_ai_review_max_items_per_run: int = 12
    m10_conflict_warn: float = 0.25
    m10_calibration_gap_warn: float = 0.15
    m10_return_volatility_warn: float = 0.06
    m10_conflict_watch_ratio: float = 0.25
    m10_conflict_degraded_ratio: float = 0.50
    m10_calibration_degraded_multiplier: float = 1.5
    m10_limited_observability_score: float = 65.0
    m11_drawdown_delta_limit: float = 0.05
    m11_tail_loss_delta_limit: float = 0.03
    m11_execution_divergence_limit: float = 0.35
    m11_shadow_results_path: str = ""
    shadow_online_model_state_path: str = "artifacts/evolution/shadow_online_model_state.json"
    shadow_online_report_dir: str = "suggestions/shadow_online"
    shadow_online_learning_rate: float = 0.15
    shadow_online_min_samples: int = 5
    shadow_online_max_preview: int = 5
    m8_top_k: int = 5
    m8_promote_similarity: float = 0.80
    m8_review_similarity: float = 0.55
    m8_min_gate_passes_for_review: int = 4
    m8_pcv_min_score: float = 0.55
    m8_deflated_sharpe_min: float = 0.10
    m8_fdr_alpha: float = 0.10
    m8_llm_min_confidence: float = 0.55
    llm_semantic_enabled: bool = False
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "https://gmn.chuangzuoli.com/v1"
    llm_model: str = "gpt-5.4"
    llm_api_key: str = ""
    llm_backup_provider: str = "openai_compatible"
    llm_backup_base_url: str = "https://gmn.chuangzuoli.com/v1"
    llm_backup_model: str = "gpt-5.2"
    llm_backup_api_key: str = ""
    llm_timeout_sec: int = 8
    llm_max_candidates_per_run: int = 8
    llm_temperature: float = 0.0
    llm_max_tokens: int = 120
    m8_noise_stability_min: float = 0.60
    m8_noise_trials: int = 3
    m8_noise_sigma: float = 0.01
    m8_random_walk_trials: int = 16
    m8_random_walk_max_pvalue: float = 0.35
    m8_registry_blocked_signatures: list[str] = Field(default_factory=list)
    m8_registry_dedupe_within_run: bool = True
    m8_allow_similarity_proxies: bool = True
    m8_strict_gate_inputs: bool = False
    score_fusion_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "M4": 1.0,
            "M6": 1.0,
            "M10": 1.0,
            "M11": 1.0,
            "M1": 1.0,
            "M2": 1.0,
            "M5": 1.0,
            "M7": 1.0,
            "M3": 1.0,
            "M8": 1.0,
        }
    )
    score_fusion_enable_bonus_cap: bool = True
    score_fusion_bonus_modules: list[str] = Field(default_factory=lambda: ["M3", "M7"])
    score_fusion_bonus_neutral_score: float = 50.0
    score_fusion_bonus_cap: float = 15.0
    score_fusion_enable_veto: bool = True
    score_fusion_veto_modules: list[str] = Field(default_factory=lambda: ["M1", "M6"])
    score_fusion_veto_score_threshold: float = 55.0
    score_fusion_veto_score_cap: float = 65.0
    score_fusion_veto_confidence_gate: float = 0.75
    active_champion_id: str = "champion_v7"
    code_commit_id: str = "git:auto"
    disk_high_watermark_pct: float = 75.0
    m9_lookback_days: int = 2
    m9_required_fields: list[str] = Field(
        default_factory=lambda: ["open", "high", "low", "close", "volume"]
    )
    auto_generate_loader_inputs: bool = True
    loader_artifact_dir: str = "artifacts/evolution/inputs"
    loader_max_age_hours: int = 36
    strict_dependency_check: bool = False
    release_confirmation_required: bool = True
    release_confirmation_ttl_days: int = 3
    release_confirmation_watchdog_interval_min: int = 60
    dependency_required_cli: list[str] = Field(default_factory=lambda: ["cpulimit"])
    dependency_required_modules: list[str] = Field(default_factory=lambda: ["duckdb", "faiss"])
    execution_spec: EvolutionExecutionSpecConfig = Field(
        default_factory=EvolutionExecutionSpecConfig
    )
    universe_spec: EvolutionUniverseSpecConfig = Field(default_factory=EvolutionUniverseSpecConfig)
    runtime_spec: EvolutionRuntimeSpecConfig = Field(default_factory=EvolutionRuntimeSpecConfig)


def _default_idle_write_whitelist() -> list[dict[str, object]]:
    return [
        {
            "task": "WE-P2-08",
            "paths": ["artifacts/faiss_snapshots/", "artifacts/shadow_logs/"],
            "actions": ["compress", "delete_via_queue"],
        }
    ]


class IdleQueueConfig(_StrictModel):
    enabled: bool = False
    auto_run: bool = False
    enabled_policy: str = "auto"
    auto_run_policy: str = "auto"
    enabled_modes: list[str] = Field(default_factory=lambda: ["simulation", "staging"])
    auto_run_modes: list[str] = Field(default_factory=lambda: ["simulation", "staging"])
    production_canary_ratio: float = 0.0
    production_canary_key: str = ""
    dispatch_interval_minutes: int = 5
    pause_sleep_seconds: int = 5
    resource_pause_enabled: bool = True
    resource_pause_metric: str = "disk_usage_pct"
    resource_pause_high_watermark_pct: float = 88.0
    resource_pause_low_watermark_pct: float = 82.0
    workday_start_time: str = "20:30"
    weekend_start_time: str = "12:00"
    base_hard_stop: str = "08:45"
    hard_kill_grace_seconds: int = 30
    soft_stop_lead_minutes: int = 10
    report_deadline_lead_minutes: int = 7
    report_min_budget_minutes: int = 5
    report_default_trigger_time: str = "08:15"
    output_root: str = "staging/idle_cache"
    checkpoint_interval_minutes: int = 10
    max_checkpoint_retention: int = 10
    forbidden_write_paths: list[str] = Field(
        default_factory=lambda: ["data/", "config/", "suggestions/", "artifacts/"]
    )
    write_whitelist: list[dict[str, object]] = Field(
        default_factory=_default_idle_write_whitelist
    )
    fallback_ttl_workday_runs: int = 2
    fallback_ttl_weekend_runs: int = 9
    fallback_ttl_low_freq_runs: int = 16
    default_retry_max_retries: int = 1
    default_retry_delay_seconds: int = 0
    default_retry_only_on: list[str] = Field(
        default_factory=lambda: ["transient_io_error", "network_timeout", "file_handle_busy"]
    )
    default_no_retry_on: list[str] = Field(
        default_factory=lambda: ["data_unavailable", "forbidden_path", "schema_mismatch"]
    )
    manual_ack_required: bool = True
    unblock_after_consecutive_success_runs: int = 2
    retention_days_workday: int = 14
    retention_days_weekend: int = 14
    retention_days_low_freq: int = 18
    history_memory_limit: int = 500
    history_disk_limit: int = 5000
    history_persist_path: str = "staging/idle_cache/_meta/idle_history.jsonl"
    staging_growth_sla_workday_mb: int = 500
    staging_growth_sla_weekend_mb: int = 5120
    universe_cache_path: str = "artifacts/universe/a_share_symbols.json"
    universe_cache_max_age_hours: int = 24
    universe_min_symbols: int = 500


class DashboardConfig(_StrictModel):
    default_total_asset: float = 0.0


class StockAnalyzerConfig(_StrictModel):
    app: AppConfig
    data_source: DataSourceConfig
    market_depth: MarketDepthConfig = Field(default_factory=MarketDepthConfig)
    tdx_sync: TdxSyncConfig = Field(default_factory=TdxSyncConfig)
    market_warehouse: MarketWarehouseConfig = Field(default_factory=MarketWarehouseConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    health_check: DataHealthConfig = Field(default_factory=DataHealthConfig)
    liquidity_filter_trend: LiquidityFilterConfig
    liquidity_filter_monster: LiquidityFilterConfig
    financial_filter: FinancialFilterConfig = Field(default_factory=FinancialFilterConfig)
    models: ModelsConfig
    score: ScoreConfig
    strategy_scores: dict[str, StrategyScoreConfig] = Field(default_factory=dict)
    soup_strategy: SoupStrategyConfig
    capital_curve: CapitalCurveConfig
    circuit_breaker: CircuitBreakerConfig
    monster_risk: MonsterRiskConfig = Field(default_factory=MonsterRiskConfig)
    week5: Week5Config = Field(default_factory=Week5Config)
    holiday_risk: HolidayRiskConfig = Field(default_factory=HolidayRiskConfig)
    global_market: GlobalMarketConfig = Field(default_factory=GlobalMarketConfig)
    regulatory_factor: RegulatoryFactorConfig = Field(default_factory=RegulatoryFactorConfig)
    limit_rule: LimitRuleConfig = Field(default_factory=LimitRuleConfig)
    week6: Week6Config = Field(default_factory=Week6Config)
    strategy_kill_switch: StrategyKillSwitchConfig = Field(default_factory=StrategyKillSwitchConfig)
    cloud_backup: CloudBackupConfig = Field(default_factory=CloudBackupConfig)
    factor_lifecycle: FactorLifecycleConfig = Field(default_factory=FactorLifecycleConfig)
    sim_broker_weekly: SimBrokerWeeklyConfig = Field(default_factory=SimBrokerWeeklyConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    wecom_interaction: WeComInteractionConfig = Field(default_factory=WeComInteractionConfig)
    feishu_interaction: FeishuInteractionConfig = Field(default_factory=FeishuInteractionConfig)
    notification_filter: NotificationFilterConfig = Field(default_factory=NotificationFilterConfig)
    command_channel: CommandChannelConfig = Field(default_factory=CommandChannelConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    market_relative_feature: MarketRelativeFeatureConfig = Field(
        default_factory=MarketRelativeFeatureConfig
    )
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    auto_promotion: AutoPromotionConfig = Field(default_factory=AutoPromotionConfig)
    backtest_matcher: BacktestMatcherConfig = Field(default_factory=BacktestMatcherConfig)
    walk_forward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    reconcile: ReconcileConfig = Field(default_factory=ReconcileConfig)
    acceptance: AcceptanceConfig = Field(default_factory=AcceptanceConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    idle_queue: IdleQueueConfig = Field(default_factory=IdleQueueConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)


def _parse_env_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass
    if raw.startswith("{") or raw.startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _set_nested_value(target: dict[str, Any], keys: list[str], value: Any) -> None:
    cursor: dict[str, Any] = target
    for key in keys[:-1]:
        next_value = cursor.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[key] = next_value
        cursor = next_value
    cursor[keys[-1]] = value


def _merge_nested_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_nested_dict(current, value)
            continue
        merged[key] = value
    return merged


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    overridden = dict(data)
    prefix = "SA__"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        nested_keys = key.removeprefix(prefix).lower().split("__")
        _set_nested_value(overridden, nested_keys, _parse_env_value(value))
    return overridden


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        raw_data = yaml.safe_load(fp) or {}
    if not isinstance(raw_data, dict):
        msg = f"config file must contain a mapping: {path}"
        raise TypeError(msg)
    return raw_data


def _discover_local_override_paths(config_path: Path) -> list[Path]:
    config_dir = config_path.parent
    same_name_override = config_dir / f"{config_path.stem}.local.yaml"
    discovered: list[Path] = []
    if same_name_override.exists() and same_name_override != config_path:
        discovered.append(same_name_override)
    for candidate in sorted(config_dir.glob("*.local.yaml")):
        if candidate == config_path or candidate in discovered:
            continue
        discovered.append(candidate)
    return discovered


def _load_config_mapping(
    config_path: Path,
    *,
    include_local_overrides: bool,
) -> dict[str, Any]:
    raw_data = _load_yaml_mapping(config_path)
    if not include_local_overrides:
        return raw_data
    merged = dict(raw_data)
    for override_path in _discover_local_override_paths(config_path):
        override = _load_yaml_mapping(override_path)
        merged = _merge_nested_dict(merged, override)
    return merged


def load_config(path: str | Path | None = None) -> StockAnalyzerConfig:
    config_path = Path(path) if path else _default_config_path()
    raw_data = _load_config_mapping(config_path, include_local_overrides=False)
    merged = _apply_env_overrides(raw_data)
    return StockAnalyzerConfig.model_validate(merged)


@lru_cache(maxsize=1)
def get_config(path: str | None = None) -> StockAnalyzerConfig:
    env_path = os.getenv("STOCK_ANALYZER_CONFIG")
    config_path = Path(path) if path else Path(env_path or _default_config_path())
    raw_data = _load_config_mapping(config_path, include_local_overrides=True)
    merged = _apply_env_overrides(raw_data)
    return StockAnalyzerConfig.model_validate(merged)
