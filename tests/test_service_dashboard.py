from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"

    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0
    config.soup_strategy.max_holdings = 2
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.command_channel.secret_key = "test-secret"
    config.notification_filter.min_score = 0.0
    config.notification_filter.quiet_windows = []
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


def test_service_dashboard_portfolio_includes_positions_quality_and_sla() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-dashboard-set-pos",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    _ = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )
    payload = _as_mapping(service.dashboard_portfolio(days=7, trade_limit=50))
    summary = _as_mapping(payload["summary"])
    assert _as_int(summary["open_positions"]) >= 1
    assert len(_as_mapping_list(payload["positions_panel"])) >= 1
    assert "recommendation_panel" in payload
    assert "holding_alerts" in payload
    assert "execution_bias" in payload
    assert len(_as_mapping_list(payload["recent_trades"])) >= 1
    assert _as_int(_as_mapping(payload["execution_quality"])["manual_trade_count"]) >= 1
    assert "sla" in payload
    assert "recent_events" in payload
    assert "week5_latest" in payload
    assert "week6_latest" in payload
    assert "week6_data_quality_latest" in payload
    assert "week7_kill_switch" in payload
    assert "week7_cloud_backup" in payload
    assert "week7_factor_lifecycle" in payload
    assert "week7_sim_broker_latest" in payload
    assert "news_watchlist_preview" in payload
    assert "evolution_latest" in payload
    assert "evolution_m8_latest" in payload
    assert "evolution_m10_latest" in payload
    assert "evolution_m11_latest" in payload
    assert "evolution_history_count" in payload
    assert "evolution_release_gate_latest" in payload
    assert "evolution_release_approval_latest" in payload
    assert "evolution_release_ticket_latest" in payload
    assert "evolution_release_confirmation_required" in payload
    assert "evolution_release_confirmation_ttl_days" in payload
    assert "evolution_release_confirmation_pending_count" in payload


def test_service_sla_report_has_percentiles_after_runs() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    _ = service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)
    _ = service.run_pipeline(symbols=["000001"], strategy="trend", current_equity=1.0)

    sla = _as_mapping(service.sla_report(recent_runs=10))
    assert _as_int(sla["recent_runs"]) >= 2
    assert _as_float(sla["p95_ms"]) >= _as_float(sla["p50_ms"])
    assert _as_float(sla["max_ms"]) >= _as_float(sla["p95_ms"])


def test_service_holding_alerts_use_manual_cost_basis() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-dashboard-holding-alert",
        payload={
            "symbol": "600000",
            "strategy": "manual",
            "target_position": 0.2,
            "entry_price": 9999.0,
            "quantity": 100,
            "fee": 2.0,
            "trade_time": "2026-03-01T10:10:10",
        },
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    payload = _as_mapping(service.dashboard_portfolio(days=7, trade_limit=20))
    alerts = _as_mapping_list(_as_mapping(payload["holding_alerts"])["items"])
    target = next(item for item in alerts if item.get("symbol") == "600000")
    assert target["severity"] == "warn"
    assert target["reason"] == "stop_loss_threshold_reached"
    assert target["entry_price"] == 9999.0


def test_service_execution_bias_report_aggregates_manual_vs_recommendation() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    service._record_audit_event(
        event_type="command_accepted",
        trace_id="cmd-bias-1",
        payload={
            "action": "SET_POSITION",
            "command_update": {
                "action": "SET_POSITION",
                "symbol": "600000",
                "status": "opened",
                "target_position": 0.2,
                "manual_fill": {"entry_price": 10.0, "quantity": 100},
                "recommendation_reference": {
                    "target_position": 0.15,
                    "reference_price": 9.5,
                    "score": 82.0,
                    "strategy": "trend",
                    "recommendation_id": "REC-TEST-1",
                },
            },
        },
    )

    report = _as_mapping(service.execution_bias_report(days=30, limit=20))
    summary = _as_mapping(report["summary"])
    assert _as_int(report["records"]) >= 1
    assert _as_int(summary["with_recommendation_reference"]) >= 1
    assert _as_int(summary["with_price_reference"]) >= 1
    assert "distribution" in report
    assert "period_summary" in report
    assert "strategy_breakdown" in report
    assert _as_float(summary["worse_price_rate"]) >= 0.0
    first = _as_mapping(_as_mapping_list(report["items"])[0])
    assert first["symbol"] == "600000"
    assert first["strategy"] == "trend"
    assert first["position_bias"] == 0.05
    assert first["price_bias_pct"] is not None


def test_service_run_stress_tests_returns_suite_summary() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    report = _as_mapping(service.run_stress_tests())
    assert _as_int(_as_mapping(report["summary"])["scenario_count"]) >= 6
    assert "scenarios" in report


def test_service_dashboard_portfolio_loads_m8_items_from_artifact(tmp_path: Path) -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    artifact_path = tmp_path / "m8_latest.json"
    artifact_payload = {
        "summary": {
            "records": 1,
            "gate_pass_rate": 0.5,
            "gate_failure_counts": {"registry": 1},
            "gate_names": ["registry", "pcv"],
            "min_gate_passes_for_review": 4,
        },
        "items": [
            {
                "symbol": "600000",
                "recommendation": "review",
                "best_similarity": 0.87,
                "passed_gates": 5,
                "gate_total": 6,
                "failed_gates": ["registry"],
                "registry_signature": "sig-001",
                "gate_checks": [
                    {
                        "name": "registry",
                        "passed": False,
                        "value": 0,
                        "threshold": 1,
                        "detail": "duplicated signature",
                    },
                    {
                        "name": "pcv",
                        "passed": True,
                        "value": 0.12,
                        "threshold": 0.05,
                        "detail": "stable",
                    },
                ],
            }
        ],
    }
    artifact_path.write_text(
        json.dumps(artifact_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    service._last_evolution_report = {
        "modules": {
            "m8": {
                "summary": artifact_payload["summary"],
                "artifact_uri": str(artifact_path),
            }
        }
    }

    payload = _as_mapping(service.dashboard_portfolio(days=7, trade_limit=20))
    m8_latest = _as_mapping(payload["evolution_m8_latest"])
    assert m8_latest["artifact_uri"] == str(artifact_path)
    gate_failure_counts = _as_mapping(_as_mapping(m8_latest["summary"])["gate_failure_counts"])
    assert gate_failure_counts["registry"] == 1
    items = _as_mapping_list(m8_latest["items"])
    assert len(items) == 1
    first = _as_mapping(items[0])
    assert first["symbol"] == "600000"
    assert first["passed_gates"] == 5
    assert len(_as_mapping_list(first["gate_checks"])) == 2


def test_service_dashboard_portfolio_handles_missing_m8_artifact() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    service._last_evolution_report = {
        "modules": {
            "m8": {
                "summary": {"records": 2},
                "artifact_uri": "suggestions/m8/not_found.json",
            }
        }
    }

    payload = service.dashboard_portfolio(days=7, trade_limit=20)
    m8_latest = payload["evolution_m8_latest"]
    assert isinstance(m8_latest, dict)
    assert m8_latest["items"] == []


def test_service_dashboard_portfolio_handles_corrupted_m8_artifact(tmp_path: Path) -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    artifact_path = tmp_path / "m8_bad.json"
    artifact_path.write_text("{bad_json", encoding="utf-8")
    service._last_evolution_report = {
        "modules": {
            "m8": {
                "summary": {"records": 1},
                "artifact_uri": str(artifact_path),
            }
        }
    }

    payload = service.dashboard_portfolio(days=7, trade_limit=20)
    m8_latest = payload["evolution_m8_latest"]
    assert isinstance(m8_latest, dict)
    assert m8_latest["items"] == []


def test_service_dashboard_portfolio_normalizes_m11_payload() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    service._last_evolution_report = {
        "modules": {
            "m11": {
                "status": 123,
                "score": "bad_score",
                "redlines": {"drawdown_delta": 1, "tail_loss_delta": 0},
                "metrics": {
                    "valid_samples": "bad_samples",
                    "drawdown_delta": 0.22,
                    "tail_loss_delta": 0.08,
                    "execution_divergence_ratio": "bad_ratio",
                },
                "attribution": [
                    {
                        "name": 99,
                        "value": "bad",
                        "threshold": 0.1,
                        "breached": "yes",
                        "impact": 0.5,
                    },
                    "invalid_item",
                ],
            }
        }
    }

    payload = service.dashboard_portfolio(days=7, trade_limit=20)
    m11_latest = payload["evolution_m11_latest"]
    assert isinstance(m11_latest, dict)
    assert m11_latest["status"] == "123"
    assert m11_latest["score"] == 0.0
    assert m11_latest["metrics"]["valid_samples"] == 0
    assert m11_latest["metrics"]["drawdown_delta"] == 0.22
    assert m11_latest["metrics"]["execution_divergence_ratio"] == 0.0
    assert m11_latest["redlines"]["drawdown_delta"] is True
    assert m11_latest["redlines"]["tail_loss_delta"] is False
    assert len(m11_latest["attribution"]) == 1
    assert m11_latest["attribution"][0]["name"] == "99"
    assert m11_latest["attribution"][0]["value"] == 0.0


def test_service_training_overview_uses_short_cache(tmp_path: Path, monkeypatch) -> None:
    config = _load_test_config()
    config.training.artifact_path = "artifacts/model_v1.json"
    config.training.baseline_report_path = "artifacts/acceptance/baseline_report.json"
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path

    acceptance_dir = tmp_path / "artifacts" / "acceptance"
    acceptance_dir.mkdir(parents=True)

    (tmp_path / "artifacts" / "model_v1.json").write_text(
        json.dumps(
            {
                "created_at": "2026-03-31T09:00:00",
                "feature_columns": ["f1", "f2", "f3"],
                "metadata": {
                    "artifact_created_at": "2026-03-31T09:00:00",
                    "train_samples": 128,
                    "calibration_samples": 32,
                    "test_samples": 16,
                    "dependency_status": {"lightgbm": "ok"},
                },
                "training_metrics": {
                    "accuracy": 0.61,
                    "auc": 0.73,
                    "validation_samples": 16,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (acceptance_dir / "baseline_report.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-03-31T09:05:00",
                "symbol": "600000",
                "lookback_days": 120,
                "baseline_type": "walk_forward",
                "model_status": {"predictor_mode": "online", "degraded_model_mode": False},
                "dependency_status": {"lightgbm": "ok"},
                "walk_forward": {
                    "summary": {
                        "folds": 4,
                        "total_trades": 18,
                        "final_equity": 1.12,
                    }
                },
                "background_factor_coverage": {"coverage_ratio": 0.98},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (acceptance_dir / "training_evaluation_report.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-03-31T09:10:00",
                "symbol": "600000",
                "lookback_days": 120,
                "dataset": {"samples": 176},
                "split_regimes": {
                    "strict_temporal": {
                        "regime": "strict_temporal",
                        "train_samples": 128,
                        "calibration_samples": 32,
                        "test_samples": 16,
                        "embargo_days": 3,
                        "metrics": {"accuracy": 0.59, "auc": 0.71},
                    },
                    "legacy_validation_only": {
                        "regime": "legacy_validation_only",
                        "train_samples": 144,
                        "calibration_samples": 16,
                        "test_samples": 16,
                        "embargo_days": 0,
                        "metrics": {"accuracy": 0.63, "auc": 0.75},
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    window_call_count = {"count": 0}
    direct_background_call_count = {"count": 0}

    monkeypatch.setattr(
        service,
        "training_bootstrap_status",
        lambda: {
            "completed": True,
            "last_bootstrap_at": "2026-03-31T09:00:00",
        },
    )
    monkeypatch.setattr(
        service,
        "latest_evolution_report",
        lambda: {
            "run_id": "evo-001",
            "timestamp": "2026-03-31T09:20:00",
            "dependencies": {"all_available": True},
            "runtime_controls": {"degraded_mode": False, "reasons": []},
            "market_warehouse_sync": {"daily_sync": {}, "intraday_sync": {}},
            "loader_inputs": {},
            "m9": {"success": True},
            "modules": {
                "m2": {"active_state": "ready"},
                "m5": {},
                "m10": {"status": "pass"},
                "m11": {"score": 0.18},
            },
        },
    )
    monkeypatch.setattr(
        service,
        "evolution_history",
        lambda limit=6: {
            "items": [
                {
                    "run_id": "evo-000",
                    "timestamp": "2026-03-30T09:20:00",
                    "runtime_controls": {"degraded_mode": False, "reasons": []},
                    "m9": {"success": True},
                    "modules": {
                        "m2": {"active_state": "ready"},
                        "m10": {"status": "pass"},
                        "m11": {"score": 0.12},
                    },
                }
            ][:limit]
        },
    )

    def _fake_evolution_window_report(*, days: int, min_runs: int) -> dict[str, object]:
        window_call_count["count"] += 1
        return {
            "overall": "pass",
            "summary": {"fail_count": 0, "warn_count": 0},
            "checks": [],
            "days": days,
            "min_runs": min_runs,
        }

    monkeypatch.setattr(service, "evolution_window_report", _fake_evolution_window_report)
    monkeypatch.setattr(
        service,
        "latest_week4_acceptance_report",
        lambda: {
            "timestamp": "2026-03-31T09:15:00",
            "overall": "pass",
            "summary": {"checks": 3},
            "acceptance_summary": {"approved": True},
            "stress_summary": {"status": "ok"},
            "sla": {"p95_ms": 1800},
            "runtime_sla": {"mode": "healthy"},
            "artifact": {"path": "artifacts/acceptance/latest.json"},
            "checks": [{"name": "gate_a", "status": "pass", "detail": "ok", "scope": "all"}],
        },
    )
    monkeypatch.setattr(
        service,
        "market_warehouse_background_data_status",
        lambda: direct_background_call_count.__setitem__(
            "count",
            direct_background_call_count["count"] + 1,
        )
        or {
            "latest_trade_date_coverage_ratio": 0.98,
            "symbols_stale": 5,
            "fields": {"holder_count": {"coverage": 0.97}},
        },
    )
    monkeypatch.setattr(
        service,
        "runtime_stage_snapshot",
        lambda: {
            "as_of": "2026-03-31T09:30:00",
            "summary": {
                "mode": "learning",
                "pending_next": {"label": "wait_for_close"},
            },
            "runtime_phase": {"label": "phase_2b"},
            "health": {"label": "healthy", "detail": "all green"},
            "latest_activity": {"label": "training overview refreshed"},
            "market_warehouse_background_data": {
                "latest_trade_date_coverage_ratio": 0.98,
                "symbols_stale": 5,
                "fields": {"holder_count": {"coverage": 0.97}},
            },
        },
    )

    first_payload = service.training_overview(history_limit=6)
    assert _as_mapping(_as_mapping(first_payload["evolution"])["window"])["overall"] == "pass"
    first_payload["generated_at"] = "mutated"

    second_payload = _as_mapping(service.training_overview(history_limit=6))
    assert second_payload["generated_at"] != "mutated"
    assert window_call_count["count"] == 1
    assert direct_background_call_count["count"] == 0
