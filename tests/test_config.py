from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from stock_analyzer.config import get_config, load_config
from stock_analyzer.evolution.m3_vector_profile import M3_DEFAULT_VECTOR_PROFILE_ID


def test_load_default_config_values(monkeypatch: MonkeyPatch) -> None:
    for key in list(os.environ.keys()):
        if key.startswith("SA__"):
            monkeypatch.delenv(key, raising=False)
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    assert config.data_source.primary == "market_warehouse"
    assert config.data_source.local_data_root == "artifacts/warehouse/package"
    assert config.app.advisory_only is False
    assert config.financial_filter.missing_data_policy == "allow"
    assert config.financial_filter.trend_mode == "score_penalty"
    assert config.financial_filter.trend_penalty == 6.0
    assert config.models.cross_review.p_lgbm_min == 0.60
    assert config.models.cross_review.p_xgb_min == 0.55
    assert config.models.cross_review.max_diff == 0.18
    assert config.models.cross_review.p_meta_min == 0.54
    assert config.models.cross_review.champion_auc_low == 0.55
    assert config.models.cross_review.champion_auc_high == 0.62
    assert config.models.cross_review.relax_threshold_delta == 0.02
    assert config.models.cross_review.tighten_threshold_delta == 0.01
    assert config.liquidity_filter_trend.min_daily_turnover == 80_000_000
    assert config.command_channel.dedup_ttl_sec == 86_400
    assert config.notification_filter.cooldown_sec == 300
    assert config.notification_filter.min_score == 60.0
    assert config.notification_filter.max_signals_per_run == 5
    assert config.backtest_matcher.dynamic_slippage_enabled is True
    assert config.backtest_matcher.max_dynamic_slippage_ratio == 0.012
    assert config.scheduler.week4_acceptance_time == "20:35"
    assert config.acceptance.auto_run is True
    assert config.acceptance.runtime_sla_recent_runs == 10
    assert config.acceptance.export_enabled is True
    assert config.tdx_sync.enabled is False
    assert config.tdx_sync.auto_run is False
    assert config.tdx_sync.run_time == "18:20"
    assert config.tdx_sync.output_root == "artifacts/imports/tdx_offline_package"
    assert config.tdx_sync.refresh_before_evolution is False
    assert config.data_source.warehouse_db_path == "artifacts/warehouse/market.duckdb"
    assert config.data_source.runtime_live_enabled is True
    assert config.data_source.runtime_live_provider == "sina"
    assert config.data_source.runtime_live_interval_priority == ["1m", "5m"]
    assert config.data_source.runtime_live_timeout_sec == 3
    assert config.data_source.runtime_live_cache_ttl_sec == 30
    assert config.data_source.runtime_live_session_only is True
    assert config.market_depth.enabled is True
    assert config.market_depth.primary == "easyquotation_sina"
    assert config.market_depth.backup == "mootdx"
    assert config.market_depth.cache_ttl_sec == 5
    assert config.market_depth.timeout_sec == 5
    assert config.market_depth.max_symbols_per_poll == 100
    assert config.market_depth.poll_scopes == ["watchlist", "signal_pool"]
    assert config.market_warehouse.enabled is True
    assert config.market_warehouse.auto_run is True
    assert config.market_warehouse.run_time == "21:45"
    assert config.market_warehouse.db_path == "artifacts/warehouse/market.duckdb"
    assert (
        config.market_warehouse.package_root
        == "artifacts/warehouse/package"
    )
    assert (
        config.market_warehouse.bootstrap_source_root
        == "artifacts/imports/tdx_offline_package"
    )
    assert config.market_warehouse.bootstrap_on_first_sync is True
    assert config.market_warehouse.offline_bootstrap_enabled is False
    assert config.market_warehouse.online_bootstrap_lookback_days == 750
    assert config.market_warehouse.online_daily_primary == "akshare"
    assert config.market_warehouse.online_daily_backup == "efinance"
    assert config.market_warehouse.post_followup_force_universe_scan is False
    assert config.market_warehouse.daily_symbol_hard_timeout_sec == 20.0
    assert config.market_warehouse.daily_symbol_hard_timeout_sec_full_universe == 20.0
    assert config.market_warehouse.intraday_intervals == ["1m", "5m"]
    assert config.market_warehouse.refresh_before_evolution is True
    assert config.week5.auto_run is True
    assert config.week5.universe_prefilter_enabled is True
    assert config.week5.universe_prefilter_lookback_days == 240
    assert config.week5.universe_prefilter_top_k == 500
    assert config.week5.universe_prefilter_shortlist_top_n == 50
    assert config.week5.monster_scan_intraday_max_symbols == 15
    assert config.week5.monster_scan_max_symbols == 120
    assert config.week5.monster_scan_sla_target_ms == 900_000
    assert config.week5.monster_scan_sla_alert_target_ms == 600_000
    assert config.week5.offhours_universe_refresh_enabled is True
    assert config.week5.offhours_weekday_universe_max_symbols == 300
    assert config.week5.offhours_research_pool_top_k == 200
    assert config.week5.offhours_friday_full_deep_scan_enabled is False
    assert config.week5.offhours_weekend_full_deep_scan_enabled is False
    assert config.week5.offhours_weekend_universe_max_symbols == 500
    assert config.week5.offhours_force_full_deep_scan_on_watchlist_below == 25
    assert config.week5.offhours_force_full_deep_scan_on_no_buy_streak == 0
    assert config.week5.offhours_force_full_deep_scan_on_drawdown_pct == 15.0
    assert config.week5.offhours_watchlist_sync_top_k == 50
    assert config.week5.auto_sync_watchlist_top_k == 50
    assert config.week5.first_board_interval_min == 1
    assert config.monster_risk.max_total_position == 0.25
    assert config.week6.auto_run is True
    assert config.week6.run_time == "15:25"
    assert config.week6.data_prewarm_enabled is True
    assert config.week6.data_prewarm_time == "15:20"
    assert config.scheduler.week6_daily_time == "15:25"
    assert config.scheduler.close_reconcile_time < config.week6.run_time
    assert config.week6.data_prewarm_time < config.week6.run_time
    assert config.evolution.offhours_time < config.market_warehouse.run_time
    assert config.scheduler.week4_acceptance_time < config.market_warehouse.run_time
    assert config.week5.first_board_window_intervals == [
        "09:30-10:30@1",
        "10:30-11:30@5",
        "13:00-14:00@1",
        "14:00-14:57@5",
    ]
    assert config.week5.live_runtime_window_intervals == [
        "09:30-10:30@5",
        "10:30-11:30@5",
        "13:00-14:00@5",
        "14:00-14:57@5",
    ]
    assert config.week5.live_runtime_max_symbols == 8
    assert config.week5.live_runtime_auto_cap_enabled is True
    assert config.week5.live_runtime_auto_cap_min_symbols == 6
    assert config.week5.live_runtime_auto_cap_window_runs == 5
    assert config.week5.live_runtime_auto_cap_safety_ratio == 0.75
    assert config.week5.live_runtime_backpressure_enabled is True
    assert config.week5.live_runtime_backpressure_threshold_ms == 60_000
    assert config.week5.live_runtime_backpressure_cooldown_min == 5
    assert config.training.bootstrap_retry_enabled is True
    assert config.training.bootstrap_retry_interval_min == 15
    assert config.training.bootstrap_full_market is True
    assert config.training.bootstrap_seed_watchlist_size == 200
    assert len(config.training.bootstrap_seed_symbols) >= 10
    assert config.market_relative_feature.enabled is False
    assert config.market_relative_feature.benchmark_symbol == "000300"
    assert config.market_relative_feature.fallback_symbol == "399001"
    assert config.auto_promotion.enabled is False
    assert config.auto_promotion.auto_load_predictor is True
    assert config.auto_promotion.notify_on_rejection is True
    assert config.auto_promotion.notify_on_training_summary is True
    assert config.auto_promotion.notify_on_manual_release_pending is True
    assert config.reconcile.auto_refresh_simulated_snapshot_at_close is False
    assert config.evolution.universe_spec.signal_analysis_lookback_days == 240
    assert config.global_market.enabled is True
    assert config.strategy_kill_switch.enabled is True
    assert config.strategy_kill_switch.underperform_months == 3
    assert config.cloud_backup.enabled is True
    assert config.cloud_backup.ping_interval_min == 5
    assert config.factor_lifecycle.enabled is True
    assert config.factor_lifecycle.shap_drift_threshold == 0.25
    assert config.sim_broker_weekly.enabled is True
    assert config.sim_broker_weekly.export_enabled is True
    assert config.notifications.primary == "console"
    assert config.notifications.feishu_webhook == ""
    assert config.notifications.feishu_app_id == ""
    assert config.notifications.feishu_app_secret == ""
    assert config.notifications.feishu_app_receive_id == ""
    assert config.notifications.feishu_app_receive_id_type == "open_id"
    assert config.notifications.telegram_bot_token == ""
    assert config.notifications.email_receivers == []
    assert config.notifications.custom_webhook_url == ""
    assert config.wecom_interaction.enabled is False
    assert config.wecom_interaction.verify_signature is True
    assert config.wecom_interaction.encoding_aes_key == ""
    assert config.wecom_interaction.receive_id == ""
    assert config.wecom_interaction.enforce_receive_id is False
    assert config.feishu_interaction.enabled is False
    assert config.feishu_interaction.subscription_mode == "webhook"
    assert config.feishu_interaction.verification_token == ""
    assert config.feishu_interaction.allowed_users == []
    assert config.feishu_interaction.auto_reconcile_after_broker_snapshot is True
    assert config.evolution.enabled is True
    assert config.evolution.dry_run is True
    assert config.evolution.dry_run_policy == "auto"
    assert config.evolution.dry_run_live_modes == ["production"]
    assert config.evolution.validation_require_dry_run is True
    assert config.evolution.offhours_time == "20:30"
    assert config.evolution.code_commit_id == "git:auto"
    assert config.evolution.strict_dependency_check is False
    assert config.evolution.disk_high_watermark_pct == 75.0
    assert config.evolution.report_dir == "artifacts/evolution/history"
    assert config.evolution.m3_active_vector_profile_id == M3_DEFAULT_VECTOR_PROFILE_ID
    assert config.evolution.m3_allow_active_profile_fallback is False
    assert config.evolution.universe_spec.signal_analysis_lookback_days == 240
    assert config.evolution.universe_spec.signal_fetch_lookback_days == 500
    assert config.evolution.universe_spec.first_board_scan_lookback_days == 60
    assert config.evolution.runtime_spec.dual_eval_profile_set_id == "dual_eval_v1"
    assert config.evolution.runtime_spec.trading_no_fill_ratio_limit == 0.20
    assert config.evolution.runtime_spec.trading_partial_fill_ratio_limit == 0.35
    assert config.evolution.runtime_spec.min_samples_per_bucket == 300
    assert config.evolution.runtime_spec.mapping_update_cooldown_days == 3
    assert config.evolution.runtime_spec.k_base == 20
    assert config.evolution.runtime_spec.k_min == 8
    assert config.evolution.runtime_spec.position_drift_alert_threshold == 0.05
    assert config.evolution.runtime_spec.position_drift_raise_u_threshold_bp == 20
    assert config.evolution.runtime_spec.position_drift_consecutive_days_trigger == 3
    assert config.evolution.m7_pipeline_max_age_days == 3
    assert config.evolution.m7_pipeline_half_life_hours == 24.0
    assert config.evolution.m7_pipeline_confidence_floor == 0.25
    assert config.evolution.m7_live_news_enabled is False
    assert config.evolution.m7_live_news_provider == "akshare_em"
    assert config.evolution.m7_live_news_max_symbols == 24
    assert config.evolution.m7_live_news_per_symbol_limit == 5
    assert config.evolution.m7_live_news_max_age_hours == 24.0
    assert config.evolution.m7_live_news_artifact_max_records == 2000
    assert config.evolution.m7_market_proxy_fallback_enabled is False
    assert config.evolution.m7_ai_review_enabled is False
    assert config.evolution.m7_ai_review_max_items_per_run == 12
    assert config.evolution.llm_semantic_enabled is False
    assert config.evolution.llm_provider == "openai_compatible"
    assert config.evolution.llm_base_url == "https://gmn.chuangzuoli.com/v1"
    assert config.evolution.llm_model == "gpt-5.4"
    assert config.evolution.llm_backup_base_url == "https://gmn.chuangzuoli.com/v1"
    assert config.evolution.llm_backup_model == "gpt-5.2"
    assert config.score.thresholds.a == 60.0
    assert config.score.thresholds.b == 50.0
    assert config.strategy_scores["trend"].thresholds.a == 60.0
    assert config.strategy_scores["trend"].thresholds.b == 50.0
    assert config.strategy_scores["monster"].thresholds.a == 60.0
    assert config.strategy_scores["monster"].thresholds.b == 50.0
    assert config.notification_filter.min_score == 60.0
    assert config.notification_filter.max_signals_per_run == 5
    assert config.idle_queue.manual_ack_required is True
    assert config.idle_queue.enabled_policy == "auto"
    assert config.idle_queue.auto_run_policy == "auto"
    assert config.idle_queue.enabled_modes == ["simulation", "staging"]
    assert config.idle_queue.auto_run_modes == ["simulation", "staging"]
    assert config.idle_queue.production_canary_ratio == 0.0
    assert config.idle_queue.pause_sleep_seconds == 5
    assert config.idle_queue.resource_pause_enabled is True
    assert config.idle_queue.resource_pause_high_watermark_pct == 88.0
    assert config.idle_queue.resource_pause_low_watermark_pct == 82.0
    assert config.idle_queue.default_retry_max_retries == 1
    assert config.idle_queue.default_retry_delay_seconds == 0
    assert config.idle_queue.default_retry_only_on == [
        "transient_io_error",
        "network_timeout",
        "file_handle_busy",
    ]
    assert config.idle_queue.default_no_retry_on == [
        "data_unavailable",
        "forbidden_path",
        "schema_mismatch",
    ]
    assert config.idle_queue.history_memory_limit == 500
    assert config.idle_queue.history_disk_limit == 5000
    assert config.dashboard.default_total_asset == 100000.0


def test_env_override_is_applied(monkeypatch: MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("SA__DATA_SOURCE__SWITCH_AFTER_FAILURES", "5")
    monkeypatch.setenv("SA__EVOLUTION__LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SA__EVOLUTION__LLM_BACKUP_MODEL", "gpt-5.2")
    monkeypatch.setenv("SA__WECOM_INTERACTION__ENABLED", "true")
    monkeypatch.setenv("SA__WECOM_INTERACTION__ENFORCE_RECEIVE_ID", "true")
    monkeypatch.setenv("SA__FEISHU_INTERACTION__ENABLED", "true")
    monkeypatch.setenv("SA__FEISHU_INTERACTION__SUBSCRIPTION_MODE", "ws")
    monkeypatch.setenv("SA__FEISHU_INTERACTION__ALLOWED_USERS", "[\"ou_xxx\"]")
    monkeypatch.setenv("SA__NOTIFICATIONS__PRIMARY", "feishu_app")
    monkeypatch.setenv("SA__NOTIFICATIONS__FEISHU_APP_RECEIVE_ID_TYPE", "email")
    monkeypatch.setenv("SA__TDX_SYNC__RUN_TIME", "18:35")
    config = load_config(root / "config" / "default.yaml")
    assert config.data_source.switch_after_failures == 5
    assert config.evolution.llm_api_key == "sk-test"
    assert config.evolution.llm_backup_model == "gpt-5.2"
    assert config.wecom_interaction.enabled is True
    assert config.wecom_interaction.enforce_receive_id is True
    assert config.feishu_interaction.enabled is True
    assert config.feishu_interaction.subscription_mode == "long_connection"
    assert config.feishu_interaction.allowed_users == ["ou_xxx"]
    assert config.notifications.primary == "feishu_app"
    assert config.notifications.feishu_app_receive_id_type == "email"
    assert config.tdx_sync.run_time == "18:35"


def test_invalid_notification_channel_is_rejected(monkeypatch: MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("SA__NOTIFICATIONS__PRIMARY", "not-a-channel")
    with pytest.raises(ValidationError, match="unsupported notification channel"):
        _ = load_config(root / "config" / "default.yaml")


def test_get_config_merges_local_override_files(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    for key in list(os.environ.keys()):
        if key.startswith("SA__"):
            monkeypatch.delenv(key, raising=False)
    base = tmp_path / "default.yaml"
    root = Path(__file__).resolve().parents[1]
    base.write_text(
        (root / "config" / "default.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "llm.local.yaml").write_text(
        "\n".join(
            [
                "evolution:",
                "  llm_semantic_enabled: true",
                '  llm_api_key: "sk-test"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STOCK_ANALYZER_CONFIG", str(base))
    get_config.cache_clear()
    try:
        config = get_config()
    finally:
        get_config.cache_clear()
    assert config.evolution.llm_semantic_enabled is True
    assert config.evolution.llm_api_key == "sk-test"
