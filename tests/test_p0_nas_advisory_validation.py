from __future__ import annotations

from pathlib import Path

from scripts.p0_nas_advisory_validation import build_validation_report, render_markdown_report


def _check_map(report: dict[str, object]) -> dict[str, bool]:
    checks = report["checks"]
    assert isinstance(checks, list)
    return {str(item["code"]): bool(item["passed"]) for item in checks}


def _ops_state() -> dict[str, object]:
    return {
        "mode": "simulation",
        "simulation_mode": True,
        "enabled": True,
        "toggle_enabled": True,
        "advisory_only": True,
        "execution_mode": "advisory_only",
    }


def _safe_config_snapshot() -> dict[str, object]:
    return {
        "auto_promotion": {"enabled": False},
        "financial_filter": {
            "enabled": True,
            "exclude_st": True,
            "exclude_delisting_risk": True,
            "min_roe": 0.04,
            "max_debt_ratio": 0.72,
        },
        "monster_risk": {
            "max_total_position": 0.25,
            "max_stock_position": 0.08,
            "disable_if_sentiment_below": 45,
        },
        "circuit_breaker": {
            "intraday_stop_after_losses": 2,
            "portfolio_daily_drawdown_stop": 2.5,
            "portfolio_weekly_drawdown_reduce": 4.0,
        },
        "capital_curve": {"drawdown_freeze": 15.0},
        "models": {"cross_review": {"degraded_consensus_enabled": False}},
        "soup_strategy": {
            "recovery_buy_enabled": False,
            "disagreement_probe_enabled": False,
        },
    }


def test_nas_advisory_validation_passes_controlled_advisory_evidence() -> None:
    report = build_validation_report(
        runtime_state={
            "latest_signals": {
                "trace_id": "trace-advisory",
                "timestamp": "2026-06-19T09:31:00",
                "source": "pipeline_run",
                "signals": [{"symbol": "600000", "action": "watch"}],
            }
        },
        runtime_state_path=Path("artifacts/runtime/runtime_state.json"),
        signals_latest={
            "trace_id": "trace-advisory",
            "timestamp": "2026-06-19T09:31:00",
            "source": "pipeline_run",
            "storage_source": "runtime_state",
            "signals": [{"symbol": "600000", "action": "watch"}],
        },
        audit_events=[
            {
                "event_id": "AUD-00000001",
                "timestamp": "2026-06-19T09:30:00",
                "event_type": "pipeline_run",
                "trace_id": "old",
                "payload": {"execution_mode": "portfolio_auto_apply"},
            },
            {
                "event_id": "AUD-00000002",
                "timestamp": "2026-06-19T09:31:00",
                "event_type": "pipeline_run",
                "trace_id": "trace-advisory",
                "payload": {
                    "execution_mode": "advisory_only",
                    "portfolio_update": {
                        "status": "simulated_auto_applied",
                        "execution_attempts": {},
                        "advisory_attempts": {"signals": 1, "buy_signals": 0},
                        "executions": [],
                    },
                },
            },
        ],
        signal_quality={
            "status": "ok",
            "signal_source": "pipeline_run",
            "signal_storage_source": "runtime_state",
            "source_signal_count": 1,
            "signal_loss_funnel": {
                "execution_attempts": {},
                "advisory_attempts": {"signals": 1, "buy_signals": 0},
                "dry_run_attempts": {},
                "execution_stages": {"buy_signals": 0},
                "data_gaps": [],
            },
        },
        ops_state=_ops_state(),
        config_snapshot=_safe_config_snapshot(),
    )

    assert report["status"] == "pass"
    checks = _check_map(report)
    assert all(checks.values())
    markdown = render_markdown_report(report)
    assert "PASS: runtime_state_latest_signals_source_is_pipeline_run" in markdown
    assert "latest_pipeline_execution_mode: advisory_only" in markdown
    assert "PASS: pipeline_has_advisory_attempt_fields" in markdown
    assert "PASS: auto_promotion_disabled" in markdown
    assert "risk_guardrails_status: pass" in markdown
    assert "enabled_experimental_entry_flags" in markdown


def test_nas_advisory_validation_flags_week5_fallback_and_execution_mix() -> None:
    report = build_validation_report(
        runtime_state={
            "latest_signals": {
                "trace_id": "trace-week5",
                "timestamp": "2026-06-19T09:31:00",
                "source": "week5_latest_candidates",
                "signals": [{"symbol": "600000", "action": "watch"}],
            }
        },
        runtime_state_path=Path("artifacts/runtime/runtime_state.json"),
        signals_latest={
            "source": "week5_latest_candidates",
            "storage_source": "runtime_state",
            "signals": [{"symbol": "600000", "action": "watch"}],
        },
        audit_events=[
            {
                "event_id": "AUD-00000002",
                "timestamp": "2026-06-19T09:31:00",
                "event_type": "pipeline_run",
                "trace_id": "trace-real",
                "payload": {
                    "execution_mode": "portfolio_auto_apply",
                    "portfolio_update": {
                        "execution_attempts": {"signals": 1, "buy_signals": 1},
                        "executions": [{"symbol": "600000", "side": "buy"}],
                    },
                },
            }
        ],
        signal_quality={
            "status": "ok",
            "signal_loss_funnel": {
                "execution_attempts": {"buy_signals": 1},
                "advisory_attempts": {},
                "dry_run_attempts": {},
            },
        },
        ops_state={
            "advisory_only": False,
            "execution_mode": "portfolio_auto_apply",
        },
        config_snapshot={
            **_safe_config_snapshot(),
            "auto_promotion": {"enabled": True},
            "financial_filter": {
                "enabled": True,
                "exclude_st": False,
                "exclude_delisting_risk": True,
                "min_roe": 0.01,
                "max_debt_ratio": 0.9,
            },
        },
    )

    assert report["status"] == "needs_review"
    checks = _check_map(report)
    assert checks["runtime_state_latest_signals_persisted"] is True
    assert checks["runtime_state_latest_signals_source_is_pipeline_run"] is False
    assert checks["signals_latest_uses_latest_not_week5_fallback"] is False
    assert checks["latest_pipeline_is_advisory_only"] is False
    assert checks["pipeline_has_empty_executions"] is False
    assert checks["signal_quality_keeps_advisory_out_of_execution"] is False
    assert checks["ops_state_confirms_advisory_only"] is False
    assert checks["auto_promotion_disabled"] is False
    assert checks["risk_guardrails_not_relaxed"] is False
