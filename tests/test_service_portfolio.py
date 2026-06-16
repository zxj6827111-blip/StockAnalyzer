from __future__ import annotations

import tempfile
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.learning.sample_schema import MaturityStatus, OutcomeRecord, SignalSnapshot
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.types import PipelineReport, PipelineSignal, RiskStatus


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.app.advisory_only = False
    config.data_source.primary = "synthetic_test"
    config.cache.enabled = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_retry_enabled = False
    config.training.bootstrap_state_path = str(
        Path(tempfile.gettempdir())
        / f"stock_analyzer_service_portfolio_{time.time_ns()}"
        / "bootstrap_state.json"
    )

    # Force permissive gates so synthetic data can produce buy actions.
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0
    config.soup_strategy.max_holdings = 1
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")

    if "trend" in config.strategy_scores:
        config.strategy_scores["trend"].thresholds.s = 0.0
        config.strategy_scores["trend"].thresholds.a = 0.0
        config.strategy_scores["trend"].thresholds.b = 0.0

    config.notification_filter.min_score = 0.0
    config.notification_filter.allowed_actions = ["buy", "watch"]
    config.notification_filter.quiet_windows = []
    config.command_channel.secret_key = "test-secret"
    return config


def _sign(
    action: str,
    command_id: str,
    payload: dict[str, object],
    secret: str,
) -> CommandEnvelope:
    ts = int(time.time())
    signature = SignedCommandProcessor.build_signature(
        secret_key=secret,
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
    )
    return CommandEnvelope(
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
        signature=signature,
    )


def _bars_from_close(close_values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": close_values,
            "high": [round(value * 1.01, 4) for value in close_values],
            "low": [round(value * 0.99, 4) for value in close_values],
            "close": close_values,
            "volume": [100000 + idx * 1000 for idx in range(len(close_values))],
        }
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_path(value: object) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)
    raise AssertionError(f"Expected path-like value, got {value!r}")


class _StaticPipeline:
    def __init__(self, report: PipelineReport) -> None:
        self._report = report

    def run_once(
        self,
        symbols: list[str],
        strategy: str = "trend",
        current_equity: float = 1.0,
    ) -> PipelineReport:
        return self._report


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def test_service_pipeline_returns_portfolio_and_actionable_fields() -> None:
    service = StockAnalyzerService(config=_load_test_config())
    payload = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    assert "portfolio_update" in payload
    assert "actionable_signals" in payload
    assert payload["execution_mode"] == "portfolio_auto_apply"
    assert len(service.portfolio_positions()) <= 1


def test_service_pipeline_exposes_runtime_stage_metrics() -> None:
    service = StockAnalyzerService(config=_load_test_config())
    payload = service.run_pipeline(
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
    )
    runtime = _as_mapping(payload["runtime"])

    assert _as_int(runtime["duration_ms"]) >= 0
    assert _as_int(runtime["pipeline_ms"]) >= 0
    assert _as_int(runtime["post_pipeline_ms"]) >= 0
    assert _as_int(runtime["recommendation_sync_ms"]) >= 0
    assert _as_int(runtime["runtime_state_persist_ms"]) >= 0
    assert _as_int(runtime["runtime_state_persist_count"]) in {0, 1}
    assert isinstance(runtime["runtime_state_persist_reasons"], list)
    assert _as_int(runtime["runtime_state_persist_bytes"]) >= 0
    assert runtime["runtime_state_persist_enabled"] is False

    events = _as_mapping_list(service.audit_events(limit=5)["events"])
    pipeline_events = [
        item for item in events if str(item.get("event_type", "")) == "pipeline_run"
    ]
    assert pipeline_events
    audit_payload = _as_mapping(pipeline_events[-1]["payload"])
    audit_runtime = _as_mapping(audit_payload["runtime"])
    assert "pipeline_ms" in audit_runtime
    assert "runtime_state_persist_ms" in audit_runtime


def test_service_pipeline_coalesces_runtime_state_persist_reasons() -> None:
    service = StockAnalyzerService(config=_load_test_config())
    persist_calls: list[dict[str, object]] = []

    def _fake_persist(*, include_history_sidecars: bool = True) -> None:
        persist_calls.append({"include_history_sidecars": include_history_sidecars})

    _patch_attr(service, "_persist_runtime_state_to_disk", _fake_persist)
    _patch_attr(
        service,
        "_record_audit_event",
        lambda *args, **kwargs: None,
    )
    _patch_attr(
        service,
        "_sync_recommendation_lifecycle_from_signals",
        lambda **kwargs: {"updated": 1, "symbols": ["600000"]},
    )
    _patch_attr(
        service,
        "_sync_recommendation_lifecycle_from_holding_alerts",
        lambda **kwargs: {"updated": 1, "symbols": ["600000"]},
    )
    _patch_attr(
        service,
        "_sync_recommendation_lifecycle_from_auto_execution",
        lambda **kwargs: {"updated": 1, "symbols": ["600000"]},
    )

    payload = service.run_pipeline(
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
    )
    runtime = _as_mapping(payload["runtime"])

    assert persist_calls == [{"include_history_sidecars": False}]
    assert _as_int(runtime["runtime_state_persist_count"]) == 1
    assert runtime["runtime_state_persist_reasons"] == [
        "recommendation_update",
        "execution_recommendation_update",
        "holding_recommendation_update",
    ]


def test_service_live_auto_execution_opens_simulated_position() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    _patch_attr(service, "_build_week5_symbol_market_payload", lambda **kwargs: {
        "last_price": 10.08,
        "open_price": 10.0,
        "prev_close": 9.9,
        "ask_levels": [{"level": 1, "price": 10.1, "volume": 5000}],
        "bid_levels": [{"level": 1, "price": 10.0, "volume": 4000}],
    })
    _patch_attr(service, "_fetch_market_depth_snapshots", lambda **kwargs: {
        "600000": {
            "available": True,
            "ask_levels": [{"level": 1, "price": 10.1, "volume": 5000}],
            "bid_levels": [{"level": 1, "price": 10.0, "volume": 4000}],
        }
    })

    signal = PipelineSignal(
        symbol="600000",
        strategy="monster",
        score=86.0,
        grade="S",
        action="buy",
        target_position=0.10,
        probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
        reasons=["soup_entry"],
    )
    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-live-buy",
        timestamp=datetime.fromisoformat("2026-03-11T09:35:00"),
        signals=[signal],
        use_live_runtime=True,
    )

    assert update["status"] == "simulated_auto_applied"
    assert update["opened"] == 1
    assert update["skipped_no_cash"] == 0
    assert _as_float(update["cash_available"]) < 100000.0
    position = service.portfolio_positions()[0]
    assert position["symbol"] == "600000"
    assert position["entry_price"] == 10.1
    assert position["quantity"] == 900
    assert service.state.current_equity > 0


def test_service_live_auto_execution_skips_unchanged_adjustment() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 5
    service = StockAnalyzerService(config=config)
    _patch_attr(service, "_build_c3_position_management_items", lambda **kwargs: [])
    opened_at = datetime.fromisoformat("2026-03-11T09:35:00")
    _ = service._portfolio.set_manual_position(
        symbol="600956",
        strategy="trend",
        target_position=0.01,
        timestamp=opened_at,
        trace_id="seed-position",
        reason="auto_simulated_buy",
        manual_fill={"entry_price": 8.45, "quantity": 100},
    )
    signal = PipelineSignal(
        symbol="600956",
        strategy="trend",
        score=45.48,
        grade="C",
        action="buy",
        target_position=0.01,
        probabilities={"lgbm": 1.0, "xgb": 0.2578, "meta": 0.4891},
        reasons=["model_disagreement_probe"],
    )

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-same-target",
        timestamp=datetime.fromisoformat("2026-03-11T09:40:00"),
        signals=[signal],
        use_live_runtime=False,
    )

    assert update["adjusted"] == 0
    assert update["executions"] == []
    attempts = _as_mapping(update["execution_attempts"])
    assert attempts["signals"] == 1
    assert attempts["buy_signals"] == 1
    assert attempts["buy_existing_position"] == 1
    assert attempts["buy_existing_unchanged"] == 1
    assert attempts["buy_new_attempted"] == 0
    position = service.portfolio_positions()[0]
    assert position["target_position"] == 0.01
    assert position["open_reason"] == "auto_simulated_buy"
    assert len(service.portfolio_trades(limit=10)) == 1


def test_service_live_auto_execution_reports_attempts_when_no_execution() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    _patch_attr(service, "_build_c3_position_management_items", lambda **kwargs: [])
    _patch_attr(service, "_fetch_market_depth_snapshots", lambda **kwargs: {})

    signals = [
        PipelineSignal(
            symbol="600000",
            strategy="trend",
            score=42.0,
            grade="C",
            action="watch",
            target_position=0.0,
            probabilities={"lgbm": 0.3, "xgb": 0.3, "meta": 0.3},
            reasons=["watch_only"],
        ),
        PipelineSignal(
            symbol="000001",
            strategy="trend",
            score=50.0,
            grade="B",
            action="buy",
            target_position=0.0,
            probabilities={"lgbm": 0.6, "xgb": 0.6, "meta": 0.6},
            reasons=["zero_target"],
        ),
        PipelineSignal(
            symbol="600001",
            strategy="trend",
            score=30.0,
            grade="C",
            action=cast(Any, "sell"),
            target_position=0.0,
            probabilities={"lgbm": 0.2, "xgb": 0.2, "meta": 0.2},
            reasons=["sell_without_position"],
        ),
    ]

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-no-execution",
        timestamp=datetime.fromisoformat("2026-03-11T09:45:00"),
        signals=signals,
        use_live_runtime=True,
    )

    assert update["executions"] == []
    attempts = _as_mapping(update["execution_attempts"])
    assert attempts["signals"] == 3
    assert attempts["non_buy_signals"] == 1
    assert attempts["buy_signals"] == 1
    assert attempts["buy_zero_target"] == 1
    assert attempts["sell_signals"] == 1
    assert attempts["sell_no_position"] == 1
    assert attempts["buy_new_attempted"] == 0


def test_service_live_auto_rejected_buy_updates_learning_outcome() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 1
    service = StockAnalyzerService(config=config)
    opened_at = datetime.fromisoformat("2026-03-11T09:35:00")
    service._portfolio.set_manual_position(
        symbol="600956",
        strategy="trend",
        target_position=0.01,
        timestamp=opened_at,
        trace_id="seed-position",
        reason="seed_position",
        manual_fill={"entry_price": 8.45, "quantity": 100},
    )
    feature_record = service._feature_schema_registry.register_feature_names(
        feature_names=["liquidity_score"],
        feature_schema_id="feature_schema_rejected_buy",
        feature_engineer_version="test",
        code_version="git:test",
    )
    label_record = service._label_policy_registry.register_from_config(
        config.labels,
        label_policy_id="label_policy_rejected_buy",
    )
    snapshot = SignalSnapshot(
        snapshot_id="snapshot-rejected-buy",
        code_version="git:test",
        symbol="000001",
        strategy="trend",
        decision_time=opened_at,
        feature_vector={"liquidity_score": 0.8},
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        runtime_config_hash="runtime_hash_rejected_buy",
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
    )
    service._sample_store.write_snapshot(snapshot)
    service._sample_store.upsert_outcome(
        OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            maturity_status=MaturityStatus.LABEL_MATURED,
        )
    )
    _patch_attr(
        service,
        "_build_week5_symbol_market_payload",
        lambda **kwargs: {
            "last_price": 10.08,
            "open_price": 10.0,
            "prev_close": 9.9,
            "ask_levels": [{"level": 1, "price": 10.1, "volume": 5000}],
            "bid_levels": [{"level": 1, "price": 10.0, "volume": 4000}],
        },
    )
    _patch_attr(
        service,
        "_fetch_market_depth_snapshots",
        lambda **kwargs: {
            "000001": {
                "available": True,
                "ask_levels": [{"level": 1, "price": 10.1, "volume": 5000}],
                "bid_levels": [{"level": 1, "price": 10.0, "volume": 4000}],
            }
        },
    )
    _patch_attr(service, "_build_c3_position_management_items", lambda **kwargs: [])
    _patch_attr(service, "_resolve_latest_close_price", lambda **kwargs: 10.0)
    signal = PipelineSignal(
        symbol="000001",
        strategy="trend",
        score=80.0,
        grade="A",
        action="buy",
        target_position=0.05,
        probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
        reasons=["sample_rejected_buy"],
        decision_trace={"learning_protocol": {"snapshot_id": snapshot.snapshot_id}},
    )

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-rejected-buy",
        timestamp=datetime.fromisoformat("2026-03-11T09:40:00"),
        signals=[signal],
        use_live_runtime=True,
    )
    learning = service._update_learning_outcomes_from_portfolio_update(
        signals=[signal],
        portfolio_update=update,
        timestamp=datetime.fromisoformat("2026-03-11T09:40:00"),
    )
    outcome = service._sample_store.get_outcome(snapshot.snapshot_id)

    assert update["skipped_max_holdings"] == 1
    executions = _as_mapping_list(update["executions"])
    rejected = next(item for item in executions if item["status"] == "rejected_max_holdings")
    assert rejected["quantity"] == 0
    assert learning["updated"] == 1
    assert outcome is not None
    assert outcome.execution_fill_ratio == 0.0
    assert outcome.realized_slippage_bp is None


def test_service_pipeline_dry_run_execution_does_not_mutate_portfolio_or_notify() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 2
    config.soup_strategy.trailing_stop = 5.0
    service = StockAnalyzerService(config=config)
    notify_calls: list[dict[str, object]] = []
    timestamp = datetime.fromisoformat("2026-03-11T09:35:00")
    service._portfolio.set_manual_position(
        symbol="001258",
        strategy="trend",
        target_position=0.01,
        timestamp=timestamp,
        trace_id="seed-dry-run-position",
        reason="seed_position",
        manual_fill={"entry_price": 10.0, "quantity": 100},
    )
    service._portfolio.annotate_position_state(
        symbol="001258",
        timestamp=timestamp,
        peak_price=10.21,
        peak_pnl_pct=0.021,
    )

    _patch_attr(
        service,
        "_notify_simulated_trade_updates_if_needed",
        lambda **kwargs: notify_calls.append(dict(kwargs)),
    )
    _patch_attr(
        service,
        "_notify_expired_position_exits_if_needed",
        lambda **kwargs: notify_calls.append(dict(kwargs)),
    )
    _patch_attr(
        service,
        "_notify_risk_status_if_needed",
        lambda *args, **kwargs: notify_calls.append(dict(kwargs)),
    )
    _patch_attr(
        service,
        "_notify_holding_alerts_if_needed",
        lambda **kwargs: notify_calls.append(dict(kwargs)),
    )
    _patch_attr(
        service,
        "_notify_provider_health_if_needed",
        lambda **kwargs: notify_calls.append(dict(kwargs)),
    )
    _patch_attr(
        service,
        "_build_week5_symbol_market_payload",
        lambda **kwargs: {
            "last_price": 10.08,
            "open_price": 10.0,
            "prev_close": 9.9,
            "ask_levels": [{"level": 1, "price": 10.1, "volume": 5000}],
            "bid_levels": [{"level": 1, "price": 10.0, "volume": 4000}],
        },
    )
    _patch_attr(
        service,
        "_fetch_market_depth_snapshots",
        lambda **kwargs: {
            "600000": {
                "available": True,
                "ask_levels": [{"level": 1, "price": 10.1, "volume": 5000}],
                "bid_levels": [{"level": 1, "price": 10.0, "volume": 4000}],
            }
        },
    )
    _patch_attr(
        service,
        "_resolve_latest_close_price",
        lambda *, symbol, bars_cache: 10.26 if symbol == "001258" else 10.0,
    )
    before_positions = service.portfolio_positions()
    before_trades = service.portfolio_trades(limit=10)
    before_watchlist = list(service.state.watchlist)
    before_equity = service.state.current_equity

    payload = service.run_pipeline(
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
        use_live_runtime=True,
        dry_run_execution=True,
        notify_enabled=False,
    )
    update = _as_mapping(payload["portfolio_update"])
    attempts = _as_mapping(update["execution_attempts"])

    assert payload["execution_mode"] == "portfolio_auto_apply_dry_run"
    assert payload["dry_run_execution"] is True
    assert payload["notify_enabled"] is False
    assert update["status"] == "simulated_auto_dry_run"
    assert update["dry_run"] is True
    assert attempts["buy_new_attempted"] >= 1
    assert service.portfolio_positions() == before_positions
    assert service.portfolio_trades(limit=10) == before_trades
    assert service.state.watchlist == before_watchlist
    assert service.state.current_equity == before_equity
    assert notify_calls == []

    events = _as_mapping_list(service.audit_events(limit=20, event_type="pipeline_run")["events"])
    audit_payload = _as_mapping(events[-1]["payload"])
    audit_update = _as_mapping(audit_payload["portfolio_update"])
    assert audit_payload["dry_run_execution"] is True
    assert audit_payload["notify_enabled"] is False
    assert audit_update["dry_run"] is True


def test_service_pipeline_audit_keeps_portfolio_execution_summary() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    trace_id = "trace-audit-execution"
    portfolio_update = {
        "opened": 0,
        "adjusted": 0,
        "trimmed": 0,
        "closed_expired": 0,
        "closed_signals": 0,
        "skipped_max_holdings": 1,
        "skipped_same_sector": 0,
        "skipped_no_cash": 0,
        "open_positions": 1,
        "status": "simulated_auto_applied",
        "cash_available": 98765.43,
        "execution_attempts": {"signals": 1, "buy_new_rejected": 1, "invalid": "2.7"},
        "executions": [
            {
                "trade_id": "SKIP-trace-000001-rejected_max_holdings",
                "symbol": "000001",
                "side": "buy",
                "status": "rejected_max_holdings",
                "strategy": "trend",
                "target_position": 0.05,
                "price": 10.1,
                "quantity": 0,
                "amount": 0.0,
                "fee": 0.0,
                "price_source": "ask1",
                "trade_time": "2026-03-11T09:40:00",
                "reason": "auto_simulated_buy_max_holdings",
                "internal_debug_blob": {"large": True},
            }
        ],
    }
    _patch_attr(
        service,
        "_apply_live_auto_portfolio_signals",
        lambda **kwargs: portfolio_update,
    )
    _patch_attr(service, "_live_auto_execution_enabled", lambda **kwargs: True)
    _patch_attr(service, "_notify_simulated_trade_updates_if_needed", lambda **kwargs: None)
    signal = PipelineSignal(
        symbol="000001",
        strategy="trend",
        score=80.0,
        grade="A",
        action="buy",
        target_position=0.05,
        probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
        reasons=["audit_execution"],
    )
    report = type(
        "Report",
        (),
        {
            "trace_id": trace_id,
            "timestamp": datetime.fromisoformat("2026-03-11T09:40:00"),
            "degraded_mode": False,
            "risk": type(
                "Risk",
                (),
                {
                    "action": "monitor",
                    "drawdown_pct": 0.0,
                    "degraded_mode": False,
                    "can_open_new_position": True,
                    "reason": "test",
                    "hard_degraded_mode": False,
                    "soft_degraded_mode": False,
                },
            )(),
            "signals": [signal],
        },
    )()
    _patch_attr(service._pipeline, "run_once", lambda **kwargs: report)

    payload = service.run_pipeline(
        symbols=["000001"],
        strategy="trend",
        current_equity=1.0,
        use_live_runtime=True,
    )
    events = _as_mapping_list(service.audit_events(limit=20, event_type="pipeline_run")["events"])
    audit_payload = _as_mapping(events[-1]["payload"])
    audit_portfolio_update = _as_mapping(audit_payload["portfolio_update"])
    audit_executions = _as_mapping_list(audit_portfolio_update["executions"])

    assert payload["portfolio_update"] == portfolio_update
    assert audit_portfolio_update["skipped_max_holdings"] == 1
    assert audit_portfolio_update["status"] == "simulated_auto_applied"
    assert audit_portfolio_update["execution_attempts"] == {
        "signals": 1,
        "buy_new_rejected": 1,
        "invalid": 2,
    }
    assert audit_executions[0]["status"] == "rejected_max_holdings"
    assert audit_executions[0]["quantity"] == 0
    assert "internal_debug_blob" not in audit_executions[0]


def test_service_simulated_rejected_buy_notification_is_deduped_by_reason() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    base_execution = {
        "symbol": "001223",
        "side": "buy",
        "status": "rejected_price_unavailable",
        "strategy": "trend",
        "target_position": 0.01,
        "quantity": 0,
        "amount": 0.0,
        "fee": 0.0,
        "price_source": "五档卖1",
        "trade_time": "2026-05-28T09:35:00",
        "reason": "auto_simulated_buy_price_unavailable",
    }
    service._notify_simulated_trade_updates_if_needed(
        portfolio_update={
            "cash_available": 97351.62,
            "executions": [
                {
                    **base_execution,
                    "trade_id": "SKIP-trace-001223-rejected-price-unavailable-1",
                    "price": 60.34,
                }
            ],
        },
        trace_id="trace-rejected-notify-1",
    )
    service._notify_simulated_trade_updates_if_needed(
        portfolio_update={
            "cash_available": 97351.62,
            "executions": [
                {
                    **base_execution,
                    "trade_id": "SKIP-trace-001223-rejected-price-unavailable-2",
                    "price": 59.79,
                }
            ],
        },
        trace_id="trace-rejected-notify-2",
    )

    assert len(notifications) == 1
    assert "模拟买入未成交 001223" in notifications[0]["title"]
    assert "本次未成交" in notifications[0]["content"]
    assert "模拟盘拒单记录" in notifications[0]["content"]
    assert "模拟盘自动成交" not in notifications[0]["content"]

    suppressed = service.audit_events(limit=5, event_type="notification_suppressed")
    suppressed_events = _as_mapping_list(suppressed["events"])
    assert suppressed_events
    assert (
        "notify:sim-trade-rejected:20260528:buy:001223:rejected_price_unavailable"
        in str(suppressed_events[0]["payload"])
    )


def test_service_simulated_blocked_buy_notifications_are_aggregated() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    service._notify_simulated_trade_updates_if_needed(
        portfolio_update={
            "cash_available": 100.0,
            "executions": [
                {
                    "trade_id": "SKIP-trace-001331-rejected_no_cash",
                    "symbol": "001331",
                    "side": "buy",
                    "status": "rejected_no_cash",
                    "strategy": "trend",
                    "target_position": 0.01,
                    "price": 60.34,
                    "quantity": 0,
                    "amount": 0.0,
                    "fee": 0.0,
                    "price_source": "ask1",
                    "trade_time": "2026-05-28T09:35:00",
                    "reason": "auto_simulated_buy_no_cash",
                },
                {
                    "trade_id": "SKIP-trace-000159-rejected_max_holdings",
                    "symbol": "000159",
                    "side": "buy",
                    "status": "rejected_max_holdings",
                    "strategy": "trend",
                    "target_position": 0.01,
                    "price": 12.0,
                    "quantity": 0,
                    "amount": 0.0,
                    "fee": 0.0,
                    "price_source": "ask1",
                    "trade_time": "2026-05-28T09:35:00",
                    "reason": "auto_simulated_buy_max_holdings",
                },
            ],
        },
        trace_id="trace-blocked-buy",
    )

    assert len(notifications) == 2
    titles = [item["title"] for item in notifications]
    assert any("sim pre trade blocked summary" in title for title in titles)
    assert any("sim risk gate blocked summary" in title for title in titles)
    assert all("sim buy rejected" not in item["title"].lower() for item in notifications)
    assert all("blocked_events=1" in item["content"] for item in notifications)

    pre_trade_events = _as_mapping_list(
        service.audit_events(limit=10, event_type="pre_trade_blocked")["events"]
    )
    risk_gate_events = _as_mapping_list(
        service.audit_events(limit=10, event_type="risk_gate_blocked")["events"]
    )
    assert len(pre_trade_events) == 1
    assert len(risk_gate_events) == 1


def test_service_simulated_blocked_buy_summary_dedup_distinguishes_symbols_and_reasons() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    def _pre_trade_execution(
        symbol: str,
        status: str,
        reason: str,
        suffix: str,
    ) -> dict[str, object]:
        return {
            "trade_id": f"SKIP-trace-{symbol}-{suffix}",
            "symbol": symbol,
            "side": "buy",
            "status": status,
            "block_category": "pre_trade_blocked",
            "strategy": "trend",
            "target_position": 0.01,
            "price": 10.0,
            "quantity": 0,
            "amount": 0.0,
            "fee": 0.0,
            "price_source": "ask1",
            "trade_time": "2026-05-28T09:35:00",
            "reason": reason,
        }

    service._notify_simulated_trade_updates_if_needed(
        portfolio_update={
            "cash_available": 100.0,
            "executions": [
                _pre_trade_execution(
                    "001331",
                    "rejected_no_cash",
                    "auto_simulated_buy_no_cash",
                    "a",
                ),
                _pre_trade_execution(
                    "000962",
                    "rejected_no_cash",
                    "auto_simulated_buy_no_cash",
                    "b",
                ),
            ],
        },
        trace_id="trace-blocked-buy-dedup-1",
    )
    service._notify_simulated_trade_updates_if_needed(
        portfolio_update={
            "cash_available": 100.0,
            "executions": [
                _pre_trade_execution(
                    "001267",
                    "rejected_quantity",
                    "auto_simulated_buy_quantity_zero",
                    "c",
                ),
                _pre_trade_execution(
                    "001359",
                    "rejected_quantity",
                    "auto_simulated_buy_quantity_zero",
                    "d",
                ),
            ],
        },
        trace_id="trace-blocked-buy-dedup-2",
    )

    assert len(notifications) == 2
    assert "001331" in notifications[0]["content"]
    assert "001267" in notifications[1]["content"]
    assert "auto_simulated_buy_no_cash" in notifications[0]["content"]
    assert "auto_simulated_buy_quantity_zero" in notifications[1]["content"]


def test_service_simulated_filled_buy_notifications_keep_trade_level_dedup() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    base_execution = {
        "symbol": "001223",
        "side": "buy",
        "status": "opened",
        "strategy": "trend",
        "target_position": 0.01,
        "price": 60.0,
        "quantity": 100,
        "amount": 6000.0,
        "fee": 5.0,
        "price_source": "五档卖1",
        "trade_time": "2026-05-28T09:35:00",
        "reason": "auto_simulated_buy",
    }
    for index in range(2):
        service._notify_simulated_trade_updates_if_needed(
            portfolio_update={
                "cash_available": 90000.0 - index,
                "executions": [
                    {
                        **base_execution,
                        "trade_id": f"SIM-trace-001223-opened-{index}",
                    }
                ],
            },
            trace_id=f"trace-filled-notify-{index}",
        )

    assert len(notifications) == 2
    assert all("模拟买入 001223" in item["title"] for item in notifications)
    assert all("模拟盘自动成交" in item["content"] for item in notifications)


def test_service_simulated_trim_notification_keeps_remaining_position_context() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    service._notify_simulated_trade_updates_if_needed(
        portfolio_update={
            "current_equity": 1.0005,
            "executions": [
                {
                    "trade_id": "TRIM-trace-600956-1",
                    "symbol": "600956",
                    "side": "sell",
                    "status": "trimmed",
                    "strategy": "trend",
                    "target_position": 0.0067,
                    "price": 9.13,
                    "quantity": 33,
                    "amount": 301.29,
                    "fee": 5.15,
                    "price_source": "最新价",
                    "trade_time": "2026-05-28T20:53:23",
                    "reason": "take_profit_stage_1_reached",
                }
            ],
        },
        trace_id="trace-trim-notify",
    )

    assert len(notifications) == 1
    assert "模拟减仓 600956" in notifications[0]["title"]
    assert "执行模拟减仓" in notifications[0]["content"]
    assert "剩余仓位仍保留在模拟盘中" in notifications[0]["content"]
    assert "已从模拟盘移出" not in notifications[0]["content"]
    assert "第一档止盈触发" in notifications[0]["content"]


def test_service_live_auto_execution_closes_position_on_sell_signal() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    _ = service.execute_command(
        _sign(
            action="SET_POSITION",
            command_id="cmd-auto-sell-open",
            payload={
                "symbol": "600000",
                "strategy": "manual",
                "target_position": 0.1,
                "entry_price": 10.0,
                "quantity": 1000,
            },
            secret=config.command_channel.secret_key,
        )
    )
    _patch_attr(service, "_build_week5_symbol_market_payload", lambda **kwargs: {
        "last_price": 9.92,
        "open_price": 10.0,
        "prev_close": 10.1,
        "ask_levels": [{"level": 1, "price": 9.93, "volume": 5000}],
        "bid_levels": [{"level": 1, "price": 9.91, "volume": 4000}],
    })
    _patch_attr(service, "_fetch_market_depth_snapshots", lambda **kwargs: {
        "600000": {
            "available": True,
            "ask_levels": [{"level": 1, "price": 9.93, "volume": 5000}],
            "bid_levels": [{"level": 1, "price": 9.91, "volume": 4000}],
        }
    })

    signal = PipelineSignal(
        symbol="600000",
        strategy="monster",
        score=40.0,
        grade="C",
        action=cast(Any, "sell"),
        target_position=0.0,
        probabilities={"lgbm": 0.2, "xgb": 0.2, "meta": 0.2},
        reasons=["stop_loss"],
    )
    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-live-sell",
        timestamp=datetime.fromisoformat("2026-03-11T10:05:00"),
        signals=[signal],
        use_live_runtime=True,
    )

    assert update["closed_signals"] == 1
    assert len(service.portfolio_positions()) == 0
    trade = service.portfolio_trades(limit=1)[0]
    assert trade["side"] == "sell"
    assert trade["exit_price"] == 9.91


def test_service_c3_portfolio_constraints_limit_sector_and_correlation() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 5
    config.soup_strategy.max_same_sector = 1
    config.global_market.correlation_decay_threshold = 0.45
    service = StockAnalyzerService(config=config)
    now = datetime.fromisoformat("2026-03-11T09:30:00")
    _ = service._portfolio.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.2,
        timestamp=now,
        trace_id="seed-position",
        manual_fill={"entry_price": 10.0, "quantity": 1000},
        sector_tag="SEC-600",
    )

    correlated_close = (
        pd.Series([round(10.0 + idx * 0.08 + (idx % 4) * 0.02, 4) for idx in range(80)])
        .pct_change()
        .dropna()
    )
    fallback_close = (
        pd.Series([round(8.0 + idx * 0.05 + (idx % 5) * 0.03, 4) for idx in range(80)])
        .pct_change()
        .dropna()
    )
    returns_map = {
        "600000": correlated_close,
        "000001": correlated_close,
        "600001": fallback_close,
    }
    _patch_attr(
        service,
        "_recent_return_series",
        lambda *, symbol, returns_cache, lookback_days=90, tail_days=60: returns_map.get(symbol)
    )
    signals = [
        PipelineSignal(
            symbol="600001",
            strategy="trend",
            score=88.0,
            grade="S",
            action="buy",
            target_position=0.12,
            probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
            reasons=["soup_entry"],
        ),
        PipelineSignal(
            symbol="000001",
            strategy="trend",
            score=86.0,
            grade="A",
            action="buy",
            target_position=0.12,
            probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
            reasons=["soup_entry"],
        ),
    ]

    summary = service._apply_c3_portfolio_constraints(
        signals=signals,
        strategy="trend",
    )
    by_symbol = {signal.symbol: signal for signal in signals}

    assert summary["sector_blocked"] == 1
    assert summary["correlation_blocked"] == 1
    assert by_symbol["600001"].action == "watch"
    assert by_symbol["000001"].action == "watch"
    assert any(
        str(reason).startswith("portfolio_sector_limit:") for reason in by_symbol["600001"].reasons
    )
    assert any(
        str(reason).startswith("portfolio_corr_limit:") for reason in by_symbol["000001"].reasons
    )


def test_service_c3_portfolio_constraints_skip_model_disagreement_probe() -> None:
    config = _load_test_config()
    config.soup_strategy.max_same_sector = 1
    service = StockAnalyzerService(config=config)
    now = datetime.fromisoformat("2026-03-11T09:30:00")
    _ = service._portfolio.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.2,
        timestamp=now,
        trace_id="seed-position",
        manual_fill={"entry_price": 10.0, "quantity": 1000},
        sector_tag="SEC-600",
    )
    signal = PipelineSignal(
        symbol="600001",
        strategy="trend",
        score=45.0,
        grade="C",
        action="buy",
        target_position=0.01,
        probabilities={"lgbm": 1.0, "xgb": 0.26, "meta": 0.49},
        reasons=["model_disagreement_probe"],
    )

    summary = service._apply_c3_portfolio_constraints(signals=[signal], strategy="trend")

    assert summary["evaluated"] == 0
    assert summary["sector_blocked"] == 0
    assert signal.action == "buy"
    assert signal.target_position == 0.01


def test_service_holding_alerts_emit_staged_take_profit_and_trailing_stop() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 5
    service = StockAnalyzerService(config=config)
    now = datetime.fromisoformat("2026-03-11T15:00:00")
    positions = [
        ("600000", 0.18, 900, 10.0),
        ("000001", 0.12, 600, 10.0),
        ("300001", 0.06, 300, 10.0),
    ]
    for symbol, target_position, quantity, entry_price in positions:
        _ = service._portfolio.set_manual_position(
            symbol=symbol,
            strategy="manual",
            target_position=target_position,
            timestamp=now,
            trace_id=f"seed-{symbol}",
            manual_fill={"entry_price": entry_price, "quantity": quantity},
        )
    service._portfolio.annotate_position_state(
        symbol="000001",
        timestamp=now,
        take_profit_stage=1,
    )
    service._portfolio.annotate_position_state(
        symbol="300001",
        timestamp=now,
        take_profit_stage=2,
        peak_price=12.0,
        peak_pnl_pct=0.2,
    )

    latest_price_map = {
        "600000": 10.6,
        "000001": 10.95,
        "300001": 11.2,
    }

    _patch_attr(
        service,
        "_resolve_latest_close_price",
        lambda *, symbol, bars_cache: latest_price_map.get(symbol),
    )

    alerts = service.holding_alerts(now=now)
    reason_by_symbol = {
        str(item.get("symbol", "")).strip(): str(item.get("reason", "")).strip()
        for item in _as_mapping_list(alerts["items"])
    }

    assert reason_by_symbol["600000"] == "take_profit_stage_1_reached"
    assert reason_by_symbol["000001"] == "take_profit_stage_2_reached"
    assert reason_by_symbol["300001"] == "trailing_stop_remainder_exit"


def test_service_live_auto_execution_trims_position_on_take_profit_stage() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    now = datetime.fromisoformat("2026-03-11T14:50:00")
    _ = service._portfolio.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.18,
        timestamp=now,
        trace_id="seed-live-trim",
        manual_fill={"entry_price": 10.0, "quantity": 900},
        sector_tag="SEC-600",
    )
    _patch_attr(service, "_resolve_latest_close_price", lambda *, symbol, bars_cache: 10.6)
    _patch_attr(service, "_build_week5_symbol_market_payload", lambda **kwargs: {
        "last_price": 10.58,
        "open_price": 10.5,
        "prev_close": 10.4,
        "ask_levels": [{"level": 1, "price": 10.6, "volume": 5000}],
        "bid_levels": [{"level": 1, "price": 10.55, "volume": 5000}],
    })
    _patch_attr(service, "_fetch_market_depth_snapshots", lambda **kwargs: {
        "600000": {
            "available": True,
            "ask_levels": [{"level": 1, "price": 10.6, "volume": 5000}],
            "bid_levels": [{"level": 1, "price": 10.55, "volume": 5000}],
        }
    })

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-live-trim",
        timestamp=now,
        signals=[],
        use_live_runtime=True,
    )

    assert update["trimmed"] == 1
    assert update["closed_signals"] == 0
    assert any(
        item["reason"] == "take_profit_stage_1_reached" and item["status"] == "trimmed"
        for item in _as_mapping_list(update["executions"])
    )
    position = service.portfolio_positions()[0]
    assert _as_float(position["target_position"]) < 0.18
    assert position["take_profit_stage"] == 1


def test_service_pipeline_suppresses_next_stage_alert_after_same_run_auto_trim() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 5
    service = StockAnalyzerService(config=config)
    now = datetime.fromisoformat("2026-03-11T14:50:00")
    _ = service._portfolio.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.18,
        timestamp=now,
        trace_id="seed-live-stage-suppress",
        manual_fill={"entry_price": 10.0, "quantity": 900},
        sector_tag="SEC-600",
    )
    report = PipelineReport(
        trace_id="trace-stage-suppress",
        timestamp=now,
        degraded_mode=False,
        risk=RiskStatus(
            action="normal",
            drawdown_pct=0.0,
            degraded_mode=False,
            can_open_new_position=True,
            reason="ok",
        ),
        signals=[
            PipelineSignal(
                symbol="600000",
                strategy="trend",
                score=10.0,
                grade="C",
                action="watch",
                target_position=0.0,
                probabilities={"lgbm": 0.0, "xgb": 0.0, "meta": 0.0},
                reasons=["synthetic_watch"],
            )
        ],
    )
    _patch_attr(service, "_resolve_latest_close_price", lambda *, symbol, bars_cache: 10.85)
    _patch_attr(service, "_live_auto_execution_enabled", lambda **kwargs: True)
    _patch_attr(service, "_select_pipeline", lambda **kwargs: _StaticPipeline(report))
    _patch_attr(service, "_build_week5_symbol_market_payload", lambda **kwargs: {
        "last_price": 10.85,
        "open_price": 10.8,
        "prev_close": 10.7,
        "ask_levels": [{"level": 1, "price": 10.86, "volume": 5000}],
        "bid_levels": [{"level": 1, "price": 10.85, "volume": 5000}],
    })
    _patch_attr(service, "_fetch_market_depth_snapshots", lambda **kwargs: {
        "600000": {
            "available": True,
            "ask_levels": [{"level": 1, "price": 10.86, "volume": 5000}],
            "bid_levels": [{"level": 1, "price": 10.85, "volume": 5000}],
        }
    })
    _patch_attr(service, "_record_run_summary", lambda **kwargs: None)

    payload = service.run_pipeline(
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
        job_name="test-auto-trim-suppress",
        notify_enabled=False,
    )

    executions = _as_mapping_list(_as_mapping(payload["portfolio_update"])["executions"])
    assert any(
        item["symbol"] == "600000"
        and item["status"] == "trimmed"
        and item["reason"] == "take_profit_stage_1_reached"
        for item in executions
    )
    holding_alerts = _as_mapping(payload["holding_alerts"])
    assert _as_mapping_list(holding_alerts["items"]) == []
    assert holding_alerts["summary"] == {"warn": 0, "info": 0}
    assert holding_alerts["suppressed_after_auto_execution"] == [
        {"symbol": "600000", "reason": "take_profit_stage_2_reached"}
    ]
    position = service.portfolio_positions()[0]
    assert position["take_profit_stage"] == 1


def test_service_c3_hrp_shadow_outputs_fallback_weights() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 6
    service = StockAnalyzerService(config=config)
    symbols = ["600000", "000001", "000002", "300001", "300002"]

    returns_map = {
        symbol: pd.Series(
            [
                round(10.0 + idx * 0.04 + ((idx + offset) % 6) * 0.03 + offset * 0.08, 4)
                for idx in range(80)
            ]
        )
        .pct_change()
        .dropna()
        for offset, symbol in enumerate(symbols)
    }
    _patch_attr(
        service,
        "_recent_return_series",
        lambda *, symbol, returns_cache, lookback_days=90, tail_days=60: returns_map.get(symbol)
    )
    signals = [
        PipelineSignal(
            symbol=symbol,
            strategy="trend",
            score=90.0 - idx,
            grade="A" if idx else "S",
            action="buy" if idx < 3 else "watch",
            target_position=0.10,
            probabilities={"lgbm": 0.75, "xgb": 0.75, "meta": 0.75},
            reasons=["soup_entry"],
        )
        for idx, symbol in enumerate(symbols)
    ]

    shadow = service._build_c3_hrp_shadow_portfolio(
        signals=signals,
        strategy="trend",
    )
    weights = _as_mapping_list(shadow["weights"])
    total_weight = sum(_as_float(item["weight"]) for item in weights)

    assert shadow["status"] in {"fallback", "ready"}
    assert shadow["method"] in {"inverse_vol_fallback", "pypfopt_hrp"}
    assert len(weights) == 5
    assert 0.99 <= total_weight <= 1.01


def test_service_generates_portfolio_execution_acceptance_report(tmp_path: Path) -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 6
    service = StockAnalyzerService(config=config)
    symbols = ["600000", "000001", "000002", "300001", "300002"]

    returns_map = {
        "600000": pd.Series([0.0020 + (idx % 3) * 0.0004 for idx in range(80)]),
        "000001": pd.Series([0.0017 + (idx % 4) * 0.0003 for idx in range(80)]),
        "000002": pd.Series([0.0014 + (idx % 5) * 0.0005 for idx in range(80)]),
        "300001": pd.Series([(-0.0035 if idx % 7 == 0 else 0.0028) for idx in range(80)]),
        "300002": pd.Series([(-0.0042 if idx % 6 == 0 else 0.0031) for idx in range(80)]),
    }
    _patch_attr(
        service,
        "_recent_return_series",
        lambda *, symbol, returns_cache, lookback_days=90, tail_days=60: returns_map.get(symbol)
    )

    report = service.generate_portfolio_execution_report(
        output_path=str(tmp_path / "artifacts" / "acceptance" / "portfolio_execution_report.json"),
        symbols=symbols,
    )

    staged_take_profit = _as_mapping(report["staged_take_profit"])
    hrp_shadow = _as_mapping(report["hrp_shadow"])
    assert _as_path(report["output_path"]).exists() is True
    assert staged_take_profit["status"] == "pass"
    assert _as_float(staged_take_profit["average_return_delta"]) > 0
    assert hrp_shadow["status"] == "pass"
    assert _as_float(hrp_shadow["shadow_max_drawdown"]) <= _as_float(
        hrp_shadow["baseline_max_drawdown"]
    )


def test_service_advisory_only_mode_skips_portfolio_auto_apply() -> None:
    config = _load_test_config()
    config.app.advisory_only = True
    service = StockAnalyzerService(config=config)

    before_positions = service.portfolio_positions()
    payload = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    update = _as_mapping(payload["portfolio_update"])
    assert payload["execution_mode"] == "advisory_only"
    assert update["status"] == "skipped_advisory_only"
    assert update["opened"] == 0
    assert service.portfolio_positions() == before_positions


def test_service_pause_new_buy_command_blocks_new_positions() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    pause_cmd = _sign(
        action="PAUSE_NEW_BUY",
        command_id="cmd-pause",
        payload={},
        secret=config.command_channel.secret_key,
    )
    pause_result = service.execute_command(pause_cmd)
    assert pause_result["accepted"] is True

    payload = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    assert _as_mapping(payload["portfolio_update"])["opened"] == 0


def test_service_manual_portfolio_commands_are_applied() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    service.state.watchlist = [item for item in service.state.watchlist if item != "600000"]

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True
    command_update = _as_mapping(set_result["command_update"])
    assert command_update["status"] == "opened"
    watchlist_sync = _as_mapping(command_update["watchlist_sync"])
    assert watchlist_sync["symbol"] == "600000"
    assert watchlist_sync["added"] is True
    assert "600000" in service.state.watchlist
    assert len(service.portfolio_positions()) == 1

    close_cmd = _sign(
        action="CLOSE_POSITION",
        command_id="cmd-close-pos",
        payload={"symbol": "600000"},
        secret=config.command_channel.secret_key,
    )
    close_result = service.execute_command(close_cmd)
    assert close_result["accepted"] is True
    close_update = _as_mapping(close_result["command_update"])
    assert close_update["closed"] is True
    assert len(service.portfolio_positions()) == 0


def test_service_set_position_watchlist_sync_is_deduplicated() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    service.state.watchlist = [item for item in service.state.watchlist if item != "600000"]

    first = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-watch-a",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    first_result = service.execute_command(first)
    assert first_result["accepted"] is True
    first_update = _as_mapping(first_result["command_update"])
    first_sync = _as_mapping(first_update["watchlist_sync"])
    assert first_sync["added"] is True

    second = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-watch-b",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.18},
        secret=config.command_channel.secret_key,
    )
    second_result = service.execute_command(second)
    assert second_result["accepted"] is True
    second_update = _as_mapping(second_result["command_update"])
    second_sync = _as_mapping(second_update["watchlist_sync"])
    assert second_sync["added"] is False
    assert second_sync["reason"] == "already_tracked"
    assert service.state.watchlist.count("600000") == 1


def test_service_manual_position_stores_fill_fields() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-fill",
        payload={
            "symbol": "600000",
            "strategy": "manual",
            "target_position": 0.2,
            "entry_price": 12.34,
            "quantity": 1200,
            "fee": 5.6,
            "account": "acc-a",
            "trade_time": "2026-03-01T10:11:12",
            "note": "manual buy",
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True
    set_update = _as_mapping(set_result["command_update"])
    manual_fill = _as_mapping(set_update["manual_fill"])
    assert manual_fill["entry_price"] == 12.34
    assert manual_fill["quantity"] == 1200
    assert manual_fill["fee"] == 5.6
    assert manual_fill["account"] == "acc-a"
    assert manual_fill["manual_trade_time"] == "2026-03-01T10:11:12"
    assert manual_fill["note"] == "manual buy"

    position = service.portfolio_positions()[0]
    assert position["entry_price"] == 12.34
    assert position["quantity"] == 1200
    assert position["fee"] == 5.6
    assert position["account"] == "acc-a"
    assert position["manual_trade_time"] == "2026-03-01T10:11:12"
    assert position["note"] == "manual buy"

    trade = service.portfolio_trades(limit=1)[0]
    assert trade["entry_price"] == 12.34
    assert trade["quantity"] == 1200
    assert trade["fee"] == 5.6
    assert trade["account"] == "acc-a"
    assert trade["manual_trade_time"] == "2026-03-01T10:11:12"
    assert trade["note"] == "manual buy"


def test_service_manual_close_stores_fill_fields() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    open_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-close-fill-open",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    assert service.execute_command(open_cmd)["accepted"] is True

    close_cmd = _sign(
        action="CLOSE_POSITION",
        command_id="cmd-close-fill-close",
        payload={
            "symbol": "600000",
            "exit_price": 13.21,
            "quantity": 1200,
            "fee": 4.2,
            "account": "acc-a",
            "trade_time": "2026-03-01T14:56:01",
            "note": "manual sell",
        },
        secret=config.command_channel.secret_key,
    )
    close_result = service.execute_command(close_cmd)
    assert close_result["accepted"] is True
    close_update = _as_mapping(close_result["command_update"])
    close_fill = _as_mapping(close_update["close_fill"])
    assert close_fill["exit_price"] == 13.21
    assert close_fill["quantity"] == 1200
    assert close_fill["fee"] == 4.2
    assert close_fill["account"] == "acc-a"
    assert close_fill["manual_trade_time"] == "2026-03-01T14:56:01"
    assert close_fill["note"] == "manual sell"

    trade = service.portfolio_trades(limit=1)[0]
    assert trade["side"] == "sell"
    assert trade["exit_price"] == 13.21
    assert trade["exit_quantity"] == 1200
    assert trade["exit_fee"] == 4.2
    assert trade["exit_account"] == "acc-a"
    assert trade["exit_trade_time"] == "2026-03-01T14:56:01"
    assert trade["exit_note"] == "manual sell"


def test_service_recommendation_lifecycle_tracks_manual_transitions() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-rec-set",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True
    recommendation = _as_mapping(_as_mapping(set_result["command_update"])["recommendation"])
    assert recommendation["status"] == "bought"

    lifecycle = service.recommendation_lifecycle()
    first = next(
        item for item in _as_mapping_list(lifecycle["items"]) if item["symbol"] == "600000"
    )
    assert first["status"] == "bought"

    close_cmd = _sign(
        action="CLOSE_POSITION",
        command_id="cmd-rec-close",
        payload={"symbol": "600000"},
        secret=config.command_channel.secret_key,
    )
    close_result = service.execute_command(close_cmd)
    assert close_result["accepted"] is True

    after_close = service.recommendation_lifecycle()
    second = next(
        item for item in _as_mapping_list(after_close["items"]) if item["symbol"] == "600000"
    )
    assert second["status"] == "closed"

    drop_cmd = _sign(
        action="SET_RECOMMENDATION_STATUS",
        command_id="cmd-rec-drop",
        payload={"symbol": "600000", "status": "dropped", "note": "manual drop"},
        secret=config.command_channel.secret_key,
    )
    drop_result = service.execute_command(drop_cmd)
    assert drop_result["accepted"] is True
    drop_update = _as_mapping(drop_result["command_update"])
    assert drop_update["status"] == "dropped"

    dropped = service.recommendation_lifecycle(status="dropped")
    final_item = next(
        item for item in _as_mapping_list(dropped["items"]) if item["symbol"] == "600000"
    )
    assert final_item["status"] == "dropped"
    assert final_item["note"] == "manual drop"


def test_service_recommendation_lifecycle_adds_trade_plan_summary_and_sell_alert() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    _patch_attr(
        service,
        "_resolve_latest_close_price",
        lambda *, symbol, bars_cache: 10.0,
    )
    signal = PipelineSignal(
        symbol="600000",
        strategy="trend",
        score=88.0,
        grade="S",
        action="buy",
        target_position=0.12,
        probabilities={"lgbm": 0.8, "xgb": 0.78, "meta": 0.76},
        reasons=["soup_entry"],
    )

    update = service._sync_recommendation_lifecycle_from_signals(
        signals=[signal],
        timestamp=datetime.fromisoformat("2026-03-11T09:35:00"),
        trace_id="trade-plan-test",
    )
    assert update["updated"] == 1
    lifecycle = service.recommendation_lifecycle()
    item = next(row for row in _as_mapping_list(lifecycle["items"]) if row["symbol"] == "600000")
    assert item["status"] == "recommended"
    plan = _as_mapping(item["trade_plan"])
    assert plan["status"] == "ready"
    assert _as_float(plan["entry_low"]) == pytest.approx(9.85)
    assert _as_float(plan["entry_high"]) == pytest.approx(10.05)
    assert _as_float(plan["stop_loss_price"]) > 0

    alert_update = service._sync_recommendation_lifecycle_from_holding_alerts(
        holding_alerts={
            "items": [
                {
                    "symbol": "600000",
                    "strategy": "trend",
                    "severity": "warn",
                    "reason": "stop_loss_threshold_reached",
                    "exit_action": "exit_full",
                    "entry_price": 10.0,
                    "latest_price": 9.3,
                    "pnl_pct": -0.07,
                    "quantity": 1000,
                    "hold_days": 3,
                }
            ]
        },
        timestamp=datetime.fromisoformat("2026-03-14T15:00:00"),
        trace_id="holding-alert-test",
    )
    assert alert_update["updated"] == 1
    after_alert = service.recommendation_lifecycle(status="sell_alert")
    alert_item = next(
        row for row in _as_mapping_list(after_alert["items"]) if row["symbol"] == "600000"
    )
    assert alert_item["exit_alert_reason"] == "stop_loss_threshold_reached"
    assert _as_float(alert_item["current_return_pct"]) == pytest.approx(-0.07)


def test_service_recommendation_lifecycle_tracks_probe_watch_signal() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    _patch_attr(
        service,
        "_resolve_latest_close_price",
        lambda *, symbol, bars_cache: 10.0,
    )
    signal = PipelineSignal(
        symbol="300483",
        strategy="monster",
        score=46.71,
        grade="C",
        action="watch",
        target_position=0.0,
        probabilities={"lgbm": 1.0, "xgb": 0.2694, "meta": 0.4970},
        reasons=["model_disagreement_probe"],
    )

    update = service._sync_recommendation_lifecycle_from_signals(
        signals=[signal],
        timestamp=datetime.fromisoformat("2026-03-11T09:35:00"),
        trace_id="probe-watch-lifecycle",
    )

    assert update["updated"] == 1
    lifecycle = service.recommendation_lifecycle(status="watching")
    item = next(row for row in _as_mapping_list(lifecycle["items"]) if row["symbol"] == "300483")
    assert item["status"] == "watching"
    assert item["last_signal_action"] == "watch"
    assert _as_mapping(item["trade_plan"])["status"] == "ready"


def test_service_recommendation_lifecycle_closes_with_realized_return() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    timestamp = datetime.fromisoformat("2026-03-11T09:35:00")

    buy_update = service._sync_recommendation_lifecycle_from_auto_execution(
        portfolio_update={
            "executions": [
                {
                    "trade_id": "TRD-BUY-1",
                    "symbol": "600000",
                    "side": "buy",
                    "status": "opened",
                    "strategy": "trend",
                    "price": 10.0,
                    "quantity": 1000,
                    "trade_time": timestamp.isoformat(),
                }
            ]
        },
        timestamp=timestamp,
        trace_id="auto-buy-test",
    )
    assert buy_update["updated"] == 1
    holding = service.recommendation_lifecycle(status="holding")
    holding_item = next(
        row for row in _as_mapping_list(holding["items"]) if row["symbol"] == "600000"
    )
    assert _as_float(holding_item["entry_price"]) == pytest.approx(10.0)

    sell_update = service._sync_recommendation_lifecycle_from_auto_execution(
        portfolio_update={
            "executions": [
                {
                    "trade_id": "TRD-SELL-1",
                    "symbol": "600000",
                    "side": "sell",
                    "status": "closed",
                    "strategy": "trend",
                    "price": 10.8,
                    "quantity": 1000,
                    "trade_time": "2026-03-15T14:55:00",
                    "reason": "take_profit_stage_2_reached",
                }
            ]
        },
        timestamp=datetime.fromisoformat("2026-03-15T14:55:00"),
        trace_id="auto-sell-test",
    )
    assert sell_update["updated"] == 1
    closed = service.recommendation_lifecycle(status="closed")
    closed_item = next(
        row for row in _as_mapping_list(closed["items"]) if row["symbol"] == "600000"
    )
    assert _as_float(closed_item["realized_return_pct"]) == pytest.approx(0.08)
    assert closed_item["outcome_status"] == "win"
    summary = _as_mapping(closed["summary"])
    assert summary["closed_records"] == 1
    assert summary["win_rate"] == pytest.approx(1.0)


def test_service_pipeline_includes_manual_cost_holding_alerts() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-holding-alert-pos",
        payload={
            "symbol": "600000",
            "strategy": "manual",
            "target_position": 0.2,
            "entry_price": 9999.0,
            "quantity": 100,
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    payload = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    assert "holding_alerts" in payload
    alerts = _as_mapping_list(_as_mapping(payload["holding_alerts"])["items"])
    target = next(item for item in alerts if item.get("symbol") == "600000")
    assert target["reason"] == "stop_loss_threshold_reached"


def test_service_holding_alert_notifications_cover_stop_profit_and_countdown() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    service._notify_holding_alerts_if_needed(
        holding_alerts={
            "items": [
                {
                    "symbol": "600000",
                    "severity": "warn",
                    "reason": "stop_loss_threshold_reached",
                    "pnl_pct": -0.061,
                    "hold_days": 2,
                    "max_hold_days": 5,
                    "entry_price": 10.0,
                    "latest_price": 9.39,
                },
                {
                    "symbol": "000001",
                    "severity": "info",
                    "reason": "take_profit_threshold_reached",
                    "pnl_pct": 0.053,
                    "hold_days": 3,
                    "max_hold_days": 5,
                    "entry_price": 10.0,
                    "latest_price": 10.53,
                },
                {
                    "symbol": "605001",
                    "severity": "warn",
                    "reason": "max_hold_days_near_limit",
                    "pnl_pct": 0.012,
                    "hold_days": 4,
                    "max_hold_days": 5,
                    "entry_price": 10.0,
                    "latest_price": 10.12,
                },
            ]
        },
        trace_id="holding-alert-test",
    )

    assert len(notifications) == 3
    assert any("卖出指令 600000" in item["title"] for item in notifications)
    assert any("止盈指令 000001" in item["title"] for item in notifications)
    assert any("持仓倒计时 605001" in item["title"] for item in notifications)
    assert any("处理类型：止损卖出" in item["content"] for item in notifications)
    assert any("处理类型：止盈卖出" in item["content"] for item in notifications)
    assert any("持仓天数：4/5" in item["content"] for item in notifications)


def test_service_expired_position_exit_notification_emits_sell_instruction() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)
    trade_time = datetime.fromisoformat("2026-03-06T10:00:00")
    _patch_attr(service._portfolio, "trades", lambda limit=100: [
        {
            "side": "sell",
            "symbol": "600000",
            "reason": "max_hold_days_exit",
            "timestamp": trade_time.isoformat(),
        }
    ])

    service._notify_expired_position_exits_if_needed(
        timestamp=trade_time,
        closed_expired=1,
        trace_id="expired-exit-test",
    )

    assert len(notifications) == 1
    assert "卖出指令 600000" in notifications[0]["title"]
    assert str(config.soup_strategy.max_hold_days) in notifications[0]["content"]
    assert "处理类型：到期卖出" in notifications[0]["content"]


def test_service_risk_status_notifies_capital_protection_on_state_change() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    service._notify_risk_status_if_needed(
        {"action": "alert", "reason": "capital_curve:alert", "drawdown_pct": 5.0},
        trace_id="capital-alert-1",
    )
    service._notify_risk_status_if_needed(
        {"action": "alert", "reason": "capital_curve:alert", "drawdown_pct": 5.1},
        trace_id="capital-alert-2",
    )
    service._notify_risk_status_if_needed(
        {"action": "reduce", "reason": "capital_curve:reduce", "drawdown_pct": 10.0},
        trace_id="capital-reduce",
    )
    service._notify_risk_status_if_needed(
        {"action": "normal", "reason": "capital_curve:normal", "drawdown_pct": 1.2},
        trace_id="capital-recovered",
    )

    assert len(notifications) == 3
    assert "资金保护线预警" in notifications[0]["title"]
    assert "资金保护线【预警】阶段" in notifications[0]["content"]
    assert "资金保护线【降仓】阶段" in notifications[1]["content"]
    assert "资金保护线恢复" in notifications[2]["title"]


def test_service_set_position_can_bind_recommendation_id_reference() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    _ = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    latest_signals = service.latest_signals_snapshot().get("signals", [])
    assert isinstance(latest_signals, list)
    assert len(latest_signals) >= 1
    first = latest_signals[0]
    recommendation_id = str(first.get("recommendation_id", "")).strip()
    symbol = str(first.get("symbol", "")).strip()
    assert recommendation_id
    assert symbol

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-rec-id",
        payload={
            "symbol": symbol,
            "strategy": "manual",
            "target_position": 0.2,
            "recommendation_id": recommendation_id,
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True
    reference = _as_mapping(_as_mapping(set_result["command_update"])["recommendation_reference"])
    assert reference["recommendation_id"] == recommendation_id


def test_service_set_position_updates_learning_execution_outcome(tmp_path: Path) -> None:
    config = _load_test_config()
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    service = StockAnalyzerService(config=config)

    _ = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    latest_signals = service.latest_signals_snapshot().get("signals", [])
    assert isinstance(latest_signals, list)
    first = _as_mapping(latest_signals[0])
    snapshot_id = str(first.get("snapshot_id", "")).strip()
    recommendation_id = str(first.get("recommendation_id", "")).strip()
    symbol = str(first.get("symbol", "")).strip()
    assert snapshot_id
    assert recommendation_id
    assert symbol

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-learning-outcome",
        payload={
            "symbol": symbol,
            "strategy": "manual",
            "target_position": 0.2,
            "recommendation_id": recommendation_id,
            "entry_price": 10.25,
            "quantity": 1000,
            "fee": 3.5,
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True
    learning_outcome_update = _as_mapping(set_result["learning_outcome_update"])
    assert learning_outcome_update["updated"] == 1

    command_update = _as_mapping(set_result["command_update"])
    reference = _as_mapping(command_update["recommendation_reference"])
    assert reference["snapshot_id"] == snapshot_id
    reference_price = _as_float(reference["reference_price"])
    assert reference_price > 0

    outcome = service._sample_store.get_outcome(snapshot_id)  # noqa: SLF001
    assert outcome is not None
    assert outcome.execution_fill_ratio == pytest.approx(1.0)
    expected_slippage_bp = round(((10.25 - reference_price) / reference_price) * 10000.0, 4)
    assert outcome.realized_slippage_bp == pytest.approx(expected_slippage_bp)


def test_service_close_all_positions_command_closes_everything() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    first_set = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-a",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    second_set = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-b",
        payload={"symbol": "000001", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    assert service.execute_command(first_set)["accepted"] is True
    assert service.execute_command(second_set)["accepted"] is True
    assert len(service.portfolio_positions()) == 1

    close_all = _sign(
        action="CLOSE_ALL_POSITIONS",
        command_id="cmd-close-all",
        payload={},
        secret=config.command_channel.secret_key,
    )
    close_result = service.execute_command(close_all)
    assert close_result["accepted"] is True
    assert _as_mapping(close_result["command_update"])["closed_count"] == 1
    assert len(service.portfolio_positions()) == 0


def test_service_reconcile_with_broker_snapshot_command() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-r1",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    broker_cmd = _sign(
        action="SET_BROKER_POSITIONS",
        command_id="cmd-broker-r1",
        payload={"positions": [{"symbol": "600000", "target_position": 0.2}]},
        secret=config.command_channel.secret_key,
    )
    broker_result = service.execute_command(broker_cmd)
    assert broker_result["accepted"] is True
    snapshot = _as_mapping(_as_mapping(broker_result["command_update"])["snapshot"])
    assert snapshot["broker_positions"] == 1

    reconcile_cmd = _sign(
        action="RUN_RECONCILE",
        command_id="cmd-reconcile-r1",
        payload={},
        secret=config.command_channel.secret_key,
    )
    reconcile_result = service.execute_command(reconcile_cmd)
    assert reconcile_result["accepted"] is True
    reconcile_report = _as_mapping(_as_mapping(reconcile_result["command_update"])["report"])
    assert reconcile_report["status"] == "ok"

    weekly = service.reconcile_weekly_report(days=7)
    assert _as_float(weekly["records"]) >= 1
    assert weekly["mismatch_records"] == 0


def test_service_bootstrap_broker_snapshot_from_simulated_portfolio() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-sim-snapshot",
        payload={
            "symbol": "600000",
            "strategy": "manual",
            "target_position": 0.2,
            "quantity": 1000,
            "account": "sim-main",
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    snapshot = service.bootstrap_broker_snapshot_from_portfolio(
        source_trace_id="sim-snapshot-from-portfolio",
    )

    assert snapshot["status"] == "ok"
    assert snapshot["source"] == "portfolio"
    assert snapshot["portfolio_positions"] == 1
    assert snapshot["broker_positions"] == 1
    assert snapshot["quantity_records"] == 1
    assert snapshot["account_records"] == 1
    assert snapshot["symbols"] == ["600000"]

    report = service.run_reconciliation(timestamp=datetime.fromisoformat("2026-03-01T15:30:00"))
    assert report["status"] == "ok"
    assert report["quantity_matched_count"] == 1
    assert report["account_matched_count"] == 1
    assert service.state.reconcile_required is False


def test_service_reconcile_normalizes_exchange_suffix_symbols() -> None:
    config = _load_test_config()
    config.soup_strategy.max_holdings = 2
    service = StockAnalyzerService(config=config)

    first_set = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-suffix-a",
        payload={
            "symbol": "000159",
            "strategy": "manual",
            "target_position": 0.2,
            "quantity": 1000,
            "account": "sim-main",
        },
        secret=config.command_channel.secret_key,
    )
    second_set = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-suffix-b",
        payload={
            "symbol": "600956",
            "strategy": "manual",
            "target_position": 0.1,
            "quantity": 500,
            "account": "sim-main",
        },
        secret=config.command_channel.secret_key,
    )
    assert service.execute_command(first_set)["accepted"] is True
    assert service.execute_command(second_set)["accepted"] is True
    assert len(service.portfolio_positions()) == 2

    _ = service.update_broker_snapshot(
        positions=[
            {
                "symbol": "000159.SZ",
                "target_position": 0.2,
                "quantity": 1000,
                "account": "sim-main",
            },
            {
                "symbol": "600956.SH",
                "target_position": 0.1,
                "quantity": 500,
                "account": "sim-main",
            },
        ],
        source_trace_id="suffix-normalized-snapshot",
    )
    service._broker_positions = {"000159.SZ": 0.2, "600956.SH": 0.1}  # noqa: SLF001

    report = service.run_reconciliation(timestamp=datetime.fromisoformat("2026-03-01T15:30:00"))

    assert report["status"] == "ok"
    assert report["matched_count"] == 2
    assert report["mismatch_count"] == 0
    assert report["missing_in_strategy"] == []
    assert report["missing_in_broker"] == []
    assert report["strategy_positions"] == 2
    assert report["broker_positions"] == 2
    assert report["quantity_matched_count"] == 2
    assert report["account_matched_count"] == 2
    assert report["quantity_mismatch_count"] == 0
    assert report["account_mismatch_count"] == 0


def test_service_bootstrap_broker_snapshot_from_portfolio_skips_empty_by_default() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    snapshot = service.bootstrap_broker_snapshot_from_portfolio(
        source_trace_id="sim-snapshot-empty-default",
    )

    assert snapshot["status"] == "skipped_empty_portfolio"
    assert snapshot["broker_positions"] == 0
    assert snapshot["allow_empty"] is False
    assert service._broker_snapshot_updated_at == ""  # noqa: SLF001


def test_service_bootstrap_broker_snapshot_from_portfolio_allows_explicit_empty() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    snapshot = service.bootstrap_broker_snapshot_from_portfolio(
        source_trace_id="sim-snapshot-empty-explicit",
        allow_empty=True,
    )

    assert snapshot["status"] == "ok"
    assert snapshot["source"] == "portfolio"
    assert snapshot["portfolio_positions"] == 0
    assert snapshot["broker_positions"] == 0
    assert snapshot["symbols"] == []
    assert snapshot["allow_empty"] is True
    assert service._broker_snapshot_updated_at  # noqa: SLF001


def test_service_reconciliation_promotes_learning_outcome_when_label_is_mature(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    service = StockAnalyzerService(config=config)

    _ = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    latest_signals = service.latest_signals_snapshot().get("signals", [])
    assert isinstance(latest_signals, list)
    first = _as_mapping(latest_signals[0])
    snapshot_id = str(first.get("snapshot_id", "")).strip()
    recommendation_id = str(first.get("recommendation_id", "")).strip()
    symbol = str(first.get("symbol", "")).strip()
    assert snapshot_id
    assert recommendation_id
    assert symbol

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-learning-reconcile",
        payload={
            "symbol": symbol,
            "strategy": "manual",
            "target_position": 0.2,
            "recommendation_id": recommendation_id,
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    pending_outcome = service._sample_store.get_outcome(snapshot_id)  # noqa: SLF001
    assert pending_outcome is not None
    service._sample_store.upsert_outcome(  # noqa: SLF001
        pending_outcome.model_copy(
            update={
                "maturity_status": MaturityStatus.LABEL_MATURED,
                "label_mature_time": datetime.fromisoformat("2026-03-01T15:00:00"),
            },
            deep=True,
        )
    )

    _ = service.update_broker_snapshot(
        positions=[{"symbol": symbol, "target_position": 0.2}],
        source_trace_id="learning-outcome-reconcile",
    )
    report = service.run_reconciliation(timestamp=datetime.fromisoformat("2026-03-01T15:30:00"))
    assert report["status"] == "ok"
    learning_outcome_update = _as_mapping(report["learning_outcome_update"])
    assert learning_outcome_update["updated"] == 1
    assert learning_outcome_update["promoted"] == 1

    outcome = service._sample_store.get_outcome(snapshot_id)  # noqa: SLF001
    assert outcome is not None
    assert outcome.maturity_status == MaturityStatus.FULLY_MATURED
    assert outcome.reconcile_status == "ok"
    assert outcome.sim_vs_broker_diff == pytest.approx(0.0)


def test_service_reconcile_requires_snapshot_when_enabled() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-r2",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    _ = service.execute_command(set_cmd)

    report = service.run_reconciliation()
    assert report["status"] == "missing_snapshot"
    learning_outcome_update = _as_mapping(report["learning_outcome_update"])
    assert learning_outcome_update["updated"] == 0
    assert learning_outcome_update["status"] == "skipped_missing_snapshot"
    assert service.state.reconcile_required is True


def test_service_reconcile_skips_learning_outcome_when_broker_snapshot_is_stale(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    service = StockAnalyzerService(config=config)
    snapshot_id = "snap-stale-reconcile"
    symbol = "600000"

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-learning-reconcile-stale",
        payload={
            "symbol": symbol,
            "strategy": "manual",
            "target_position": 0.2,
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    service._sample_store.upsert_outcome(  # noqa: SLF001
        OutcomeRecord(
            snapshot_id=snapshot_id,
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime.fromisoformat("2026-03-01T15:00:00"),
        )
    )

    _ = service.update_broker_snapshot(
        positions=[{"symbol": symbol, "target_position": 0.2}],
        source_trace_id="learning-outcome-reconcile-stale",
    )
    service._broker_snapshot_updated_at = "2026-03-01T09:00:00"  # noqa: SLF001

    report = service.run_reconciliation(timestamp=datetime.fromisoformat("2026-03-02T15:30:00"))
    assert report["status"] == "stale_snapshot"
    broker_snapshot = _as_mapping(report["broker_snapshot"])
    assert broker_snapshot["fresh"] is False
    assert broker_snapshot["reason"] == "stale_broker_snapshot"
    learning_outcome_update = _as_mapping(report["learning_outcome_update"])
    assert learning_outcome_update["updated"] == 0
    assert learning_outcome_update["status"] == "skipped_stale_snapshot"

    outcome = service._sample_store.get_outcome(snapshot_id)  # noqa: SLF001
    assert outcome is not None
    assert outcome.maturity_status == MaturityStatus.LABEL_MATURED
    assert outcome.reconcile_status == ""


def test_service_reconcile_skips_snapshot_requirement_when_no_positions() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    report = service.run_reconciliation()

    assert report["status"] == "ok"
    assert report["strategy_positions"] == 0
    assert report["broker_positions"] == 0
    assert report["note"] == "no positions; reconcile skipped without broker snapshot"
    assert service.state.reconcile_required is False


def test_service_reconcile_weekly_report_contains_sim_vs_broker_fields() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-r3",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    _ = service.execute_command(set_cmd)

    _ = service.update_broker_snapshot(
        positions=[{"symbol": "600000", "target_position": 0.1}],
        source_trace_id="snapshot-r3-mismatch",
    )
    mismatch_report = service.run_reconciliation(
        timestamp=datetime.fromisoformat("2026-03-01T15:30:00")
    )
    assert mismatch_report["status"] == "mismatch"

    _ = service.update_broker_snapshot(
        positions=[{"symbol": "600000", "target_position": 0.2}],
        source_trace_id="snapshot-r3-ok",
    )
    ok_report = service.run_reconciliation(timestamp=datetime.fromisoformat("2026-03-02T15:30:00"))
    assert ok_report["status"] == "ok"

    weekly = service.reconcile_weekly_report(days=7)
    assert weekly["records"] == 2
    status_breakdown = _as_mapping(weekly["status_breakdown"])
    assert status_breakdown["mismatch"] == 1
    assert status_breakdown["ok"] == 1

    sim_vs_broker = _as_mapping(weekly["sim_vs_broker"])
    cause_breakdown = _as_mapping(sim_vs_broker["cause_breakdown"])
    top_diff_symbols = _as_mapping_list(sim_vs_broker["top_diff_symbols"])
    assert cause_breakdown["position_diff"] == 1
    assert _as_float(sim_vs_broker["alignment_rate"]) > 0.0
    assert top_diff_symbols[0]["symbol"] == "600000"


def test_service_reconcile_detects_quantity_and_account_mismatch() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-set-pos-r4",
        payload={
            "symbol": "600000",
            "strategy": "manual",
            "target_position": 0.2,
            "entry_price": 10.0,
            "quantity": 1000,
            "account": "acc-a",
            "trade_time": "2026-03-01T10:00:00",
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    _ = service.update_broker_snapshot(
        positions=[
            {
                "symbol": "600000",
                "target_position": 0.2,
                "quantity": 900,
                "account": "acc-b",
            }
        ],
        source_trace_id="snapshot-r4-mismatch",
    )
    report = service.run_reconciliation(timestamp=datetime.fromisoformat("2026-03-01T15:30:00"))
    assert report["status"] == "mismatch"
    assert report["quantity_mismatch_count"] == 1
    assert report["account_mismatch_count"] == 1
    assert report["detail_mismatch_count"] == 2
    assert _as_float(report["mismatch_count"]) >= 2
    assert len(_as_mapping_list(report["quantity_diffs"])) == 1
    assert len(_as_mapping_list(report["account_diffs"])) == 1

    weekly = service.reconcile_weekly_report(days=7)
    cause_breakdown = _as_mapping(_as_mapping(weekly["sim_vs_broker"])["cause_breakdown"])
    assert _as_float(cause_breakdown["quantity_diff"]) >= 1
    assert _as_float(cause_breakdown["account_diff"]) >= 1
