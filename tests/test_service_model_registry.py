from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.runtime.service import StockAnalyzerService


class FailingBarsProvider:
    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        raise AssertionError(f"bars fallback should not run for {symbol}")

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        raise AssertionError(f"intraday fallback should not run for {symbol}:{interval}")


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _load_test_config(base_dir: Path | None = None) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.week5.auto_notify = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = base_dir or Path(tempfile.mkdtemp(prefix="stock_analyzer_registry_tests_"))
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state.json")
    config.training.artifact_path = str(temp_root / "protocol_model.json")
    config.training.min_samples = 20
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.evolution.auto_run = False
    config.evolution.report_dir = str(temp_root / "evolution_history")
    config.evolution.suggestions_dir = str(temp_root / "suggestions")
    config.evolution.manifest_path = str(temp_root / "run_manifest.json")
    config.evolution.compliance_db_path = str(temp_root / "compliance.duckdb")
    config.evolution.m2_state_path = str(temp_root / "m2_state.json")
    config.evolution.m3_store_dir = str(temp_root / "artifacts" / "evolution" / "m3")
    return config


def _new_service(
    config: StockAnalyzerConfig,
    provider: object | None = None,
) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    runtime_provider = provider or SyntheticProvider(seed_offset=2027)
    object.__setattr__(service, "_provider", runtime_provider)
    object.__setattr__(service._pipeline, "_provider", runtime_provider)
    object.__setattr__(service, "_realtime_provider", runtime_provider)
    if service._realtime_pipeline is not None:
        object.__setattr__(service._realtime_pipeline, "_provider", runtime_provider)
    return service


def _seed_learning_protocol_samples(
    service: StockAnalyzerService,
    *,
    symbols: list[str],
    rows_per_symbol: int,
) -> None:
    feature_record = service._feature_schema_registry.register_feature_names(
        feature_names=["feature_a", "feature_b"],
        feature_engineer_version="test",
        code_version="git:test",
    )
    label_record = service._label_policy_registry.register_from_config(service._config.labels)
    base_time = datetime.now(UTC) - timedelta(days=max(30, rows_per_symbol + 30))
    row_index = 0
    for symbol in symbols:
        for offset in range(rows_per_symbol):
            decision_time = base_time + timedelta(days=row_index)
            snapshot = SignalSnapshot(
                snapshot_id=f"{symbol}-snap-{offset:03d}",
                code_version="git:test",
                symbol=symbol,
                strategy="trend",
                decision_time=decision_time,
                feature_vector={
                    "feature_a": float((row_index % 5) / 5.0),
                    "feature_b": float((row_index % 7) - 3),
                },
                feature_schema_id=feature_record.feature_schema_id,
                feature_schema_hash=feature_record.feature_schema_hash,
                model_outputs={
                    "p_meta": 0.68 if row_index % 2 == 0 else 0.36,
                    "p_lgbm": 0.66 if row_index % 2 == 0 else 0.34,
                    "p_xgb": 0.69 if row_index % 2 == 0 else 0.37,
                },
                risk_context={"degraded_mode": row_index % 3 == 0},
                runtime_config_hash="runtime_hash_test",
                label_policy_id=label_record.label_policy_id,
                label_policy_hash=label_record.label_policy_hash,
            )
            outcome = OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=decision_time + timedelta(days=service._config.labels.horizon_days),
                realized_return=0.08 if row_index % 2 == 0 else -0.05,
                max_favorable_excursion=0.09 if row_index % 2 == 0 else 0.01,
                max_adverse_excursion=-0.01 if row_index % 2 == 0 else -0.07,
                execution_fill_ratio=0.96 if row_index % 2 == 0 else 0.78,
                realized_slippage_bp=7.0 if row_index % 2 == 0 else 16.0,
                reconcile_status="ok" if row_index % 2 == 0 else "mismatch",
                sim_vs_broker_diff=0.004 if row_index % 2 == 0 else 0.031,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
            service._sample_store.write_snapshot(snapshot)
            service._sample_store.upsert_outcome(outcome)
            row_index += 1


def test_service_full_market_training_auto_registers_protocol_bound_model(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    payload = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "protocol_model.json"),
    )
    registry_payload = _as_mapping(payload["model_registry"])
    entry = _as_mapping(service.model_registry_entry(str(registry_payload["model_id"])))

    assert payload["ok"] is True
    assert payload["input_mode"] == "sample_store"
    assert registry_payload["registered"] is True
    assert entry["lifecycle_state"] == "trained"
    assert entry["role"] == "challenger"
    assert entry["dataset_manifest_id"] == payload["dataset_manifest_id"]

    shadow_validated = _as_mapping(
        service.update_model_registry_lifecycle(
            model_id=str(registry_payload["model_id"]),
            lifecycle_state="shadow_validated",
            timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
        )
    )
    approved = _as_mapping(
        service.update_model_registry_lifecycle(
            model_id=str(registry_payload["model_id"]),
            lifecycle_state="approved",
            timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
        )
    )
    champion = _as_mapping(
        service.update_model_registry_role(
            model_id=str(registry_payload["model_id"]),
            role="champion",
            timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
        )
    )
    status = _as_mapping(
        service.model_registry_entries(role="champion", lifecycle_state="approved", limit=10)
    )
    registered_events = _as_mapping(
        service.audit_events(limit=20, event_type="model_registry_registered")
    )

    assert shadow_validated["lifecycle_state"] == "shadow_validated"
    assert approved["lifecycle_state"] == "approved"
    assert champion["role"] == "champion"
    assert status["records"] == 1
    assert _as_mapping(status["active_champion"])["model_id"] == champion["model_id"]
    assert int(registered_events["records"]) >= 1


def test_service_train_learning_manifest_can_register_protocol_bound_model(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )
    manifest_payload = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )

    payload = _as_mapping(
        service.train_learning_manifest(
            dataset_manifest_id=str(manifest_payload["dataset_manifest_id"]),
            register_model=True,
        )
    )
    registry_payload = _as_mapping(payload["model_registry"])
    entry = _as_mapping(service.model_registry_entry(str(registry_payload["model_id"])))
    registered_events = _as_mapping(
        service.audit_events(limit=20, event_type="model_registry_registered")
    )

    assert payload["ok"] is True
    assert payload["input_mode"] == "dataset_manifest"
    assert registry_payload["registered"] is True
    assert registry_payload["source"] == "train_learning_manifest"
    assert entry["role"] == "challenger"
    assert entry["lifecycle_state"] == "trained"
    assert entry["dataset_manifest_id"] == payload["dataset_manifest_id"]
    assert entry["feature_schema_id"] == payload["feature_schema_id"]
    assert entry["label_policy_id"] == payload["label_policy_id"]
    assert int(registered_events["records"]) >= 1


def test_service_bootstrap_active_champion_from_existing_artifact(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )
    training = _as_mapping(
        service.train_models(
            full_market=True,
            lookback_days=240,
            preferred_symbols=["600000", "000001"],
            artifact_path=str(tmp_path / "protocol_model.json"),
        )
    )
    registry_payload = _as_mapping(training["model_registry"])

    payload = _as_mapping(
        service.bootstrap_active_champion_from_artifact(
            artifact_path=str(tmp_path / "protocol_model.json"),
            source="test_bootstrap",
        )
    )
    active = _as_mapping(service.model_registry_entries(limit=10)["active_champion"])

    assert payload["accepted"] is True
    assert payload["reason"] == "existing_registry_record_promoted"
    assert payload["model_id"] == registry_payload["model_id"]
    assert payload["role"] == "champion"
    assert payload["lifecycle_state"] == "approved"
    assert active["model_id"] == registry_payload["model_id"]
    assert service._config.evolution.active_champion_id == registry_payload["model_id"]


def test_service_run_learning_manifest_shadow_validation_builds_standard_bundle(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )
    manifest_payload = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )

    payload = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest_payload["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )
    training = _as_mapping(payload["training"])
    training_registry = _as_mapping(training["model_registry"])
    shadow_dataset = _as_mapping(payload["shadow_dataset"])
    champion_shadow_report = _as_mapping(payload["champion_shadow_report"])
    shadow_online_v2_report = _as_mapping(payload["shadow_online_v2_report"])
    registry_lifecycle = _as_mapping(payload["registry_lifecycle"])
    lifecycle_record = _as_mapping(registry_lifecycle["record"])
    entry = _as_mapping(service.model_registry_entry(str(payload["shadow_model_id"])))
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="learning_manifest_shadow_validation")
    )
    latest_event = _as_mapping(cast(list[object], audit_payload["events"])[-1])
    latest_payload = _as_mapping(latest_event["payload"])

    assert payload["ok"] is True
    assert payload["mode"] == "learning_manifest_shadow_validation"
    assert payload["dataset_manifest_id"] == manifest_payload["dataset_manifest_id"]
    assert payload["shadow_model_id"] == training_registry["model_id"]
    assert payload["champion_model_id"] == champion_registry["model_id"]
    assert payload["evaluation_split_names"] == ["test"]
    assert training["ok"] is True
    assert training_registry["registered"] is True
    assert shadow_dataset["ok"] is True
    assert int(shadow_dataset["row_count"]) >= 1
    assert champion_shadow_report["ok"] is True
    assert champion_shadow_report["champion_model_id"] == champion_registry["model_id"]
    assert shadow_online_v2_report["ok"] is True
    assert shadow_online_v2_report["champion_model_id"] == champion_registry["model_id"]
    assert registry_lifecycle["updated"] is True
    assert lifecycle_record["lifecycle_state"] == "shadow_validated"
    assert entry["lifecycle_state"] == "shadow_validated"
    assert int(audit_payload["records"]) >= 1
    assert latest_event["event_type"] == "learning_manifest_shadow_validation"
    assert latest_payload["shadow_model_id"] == payload["shadow_model_id"]
    assert latest_payload["champion_model_id"] == payload["champion_model_id"]


def test_service_build_shadow_dataset_uses_registered_model_manifest(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    training_payload = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "protocol_model.json"),
    )
    registry_payload = _as_mapping(training_payload["model_registry"])
    shadow_payload = _as_mapping(
        service.build_shadow_dataset(
            model_id=str(registry_payload["model_id"]),
            split_names=["test"],
            preview_limit=3,
        )
    )
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="shadow_dataset_built")
    )

    assert shadow_payload["model_id"] == registry_payload["model_id"]
    assert shadow_payload["dataset_manifest_id"] == training_payload["dataset_manifest_id"]
    assert shadow_payload["requested_split_names"] == ["test"]
    assert int(shadow_payload["row_count"]) >= 1
    assert _as_mapping(shadow_payload["predictor_mode"])["predictor_mode"] == "artifact_loaded"

    preview = shadow_payload["preview"]
    assert isinstance(preview, list)
    assert len(preview) >= 1
    assert _as_mapping(preview[0])["split_name"] == "test"

    rows = shadow_payload["rows"]
    assert isinstance(rows, list)
    first_row = _as_mapping(rows[0])
    baseline_scores = _as_mapping(first_row["baseline_scores"])
    assert 0.0 <= float(baseline_scores["p_meta"]) <= 1.0

    assert int(audit_payload["records"]) >= 1


def test_service_build_champion_shadow_report_uses_active_champion(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    shadow_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000"],
        artifact_path=str(tmp_path / "shadow_model.json"),
    )
    shadow_registry = _as_mapping(shadow_training["model_registry"])
    report_payload = _as_mapping(
        service.build_champion_shadow_report(
            model_id=str(shadow_registry["model_id"]),
            split_names=["test"],
            preview_limit=3,
        )
    )
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="champion_shadow_report_built")
    )

    assert report_payload["champion_model_id"] == champion_registry["model_id"]
    assert report_payload["shadow_model_id"] == shadow_registry["model_id"]
    assert int(report_payload["row_count"]) >= 1
    assert report_payload["split_counts"] == {"test": int(report_payload["row_count"])}
    assert _as_mapping(report_payload["m11_report"])["status"] in {
        "stable",
        "redline_breach",
        "no_data",
    }
    summary_metrics = _as_mapping(report_payload["summary_metrics"])
    assert "mean_abs_p_meta_delta" in summary_metrics

    preview = report_payload["preview"]
    assert isinstance(preview, list)
    assert len(preview) >= 1
    assert int(audit_payload["records"]) >= 1


def test_service_build_shadow_online_v2_report_emits_detailed_payload(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    shadow_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000"],
        artifact_path=str(tmp_path / "shadow_model.json"),
    )
    shadow_registry = _as_mapping(shadow_training["model_registry"])
    report_payload = _as_mapping(
        service.build_shadow_online_v2_report(
            model_id=str(shadow_registry["model_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
        )
    )
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="shadow_online_v2_report_built")
    )

    assert report_payload["champion_model_id"] == champion_registry["model_id"]
    assert report_payload["shadow_model_id"] == shadow_registry["model_id"]
    assert int(report_payload["row_count"]) >= 1
    assert "shadow_v2_cum_return" in _as_mapping(report_payload["return_summary"])
    assert "shadow_v2" in _as_mapping(report_payload["calibration_summary"])
    assert "shadow_v2_signal_divergence_ratio" in _as_mapping(
        report_payload["execution_summary"]
    )
    assert int(audit_payload["records"]) >= 1


def test_service_train_execution_risk_model_persists_status_and_history(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=36,
    )

    payload = _as_mapping(
        service.train_execution_risk_model(
            artifact_path=str(tmp_path / "execution_risk.json"),
            min_samples_per_target=12,
            epochs=120,
            seed=7,
        )
    )
    status = _as_mapping(service.execution_risk_status())
    history = _as_mapping(service.execution_risk_training_history(limit=10))
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="execution_risk_model_trained")
    )

    assert payload["ok"] is True
    assert payload["status"] == "trained"
    assert Path(str(payload["artifact_path"])).exists() is True
    assert "can_fill" in cast(list[object], payload["trained_targets"])
    assert status["artifact_exists"] is True
    assert "can_fill" in cast(list[object], status["trained_targets"])
    assert int(history["records"]) >= 1
    assert int(audit_payload["records"]) >= 1


def test_service_train_execution_risk_model_returns_preflight_when_targets_not_trainable(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    feature_record = service._feature_schema_registry.register_feature_names(
        feature_names=["feature_a"],
        feature_engineer_version="test",
        code_version="git:test",
    )
    label_record = service._label_policy_registry.register_from_config(service._config.labels)
    base_time = datetime(2026, 3, 1, 14, 30, tzinfo=UTC)
    for index in range(30):
        snapshot = SignalSnapshot(
            snapshot_id=f"single-class-{index:03d}",
            code_version="git:test",
            symbol="600000",
            strategy="trend",
            decision_time=base_time + timedelta(days=index),
            feature_vector={"feature_a": float(index)},
            feature_schema_id=feature_record.feature_schema_id,
            feature_schema_hash=feature_record.feature_schema_hash,
            runtime_config_hash="runtime_hash_test",
            label_policy_id=label_record.label_policy_id,
            label_policy_hash=label_record.label_policy_hash,
        )
        service._sample_store.write_snapshot(snapshot)
        service._sample_store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                reconcile_status="ok",
                sim_vs_broker_diff=0.0,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_history_archive",
            )
        )

    payload = _as_mapping(
        service.train_execution_risk_model(
            artifact_path=str(tmp_path / "execution_risk.json"),
            min_samples_per_target=24,
        )
    )
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="execution_risk_model_training_blocked")
    )
    status = _as_mapping(service.execution_risk_status())
    history = _as_mapping(service.execution_risk_training_history(limit=5))

    assert payload["ok"] is False
    assert payload["status"] == "blocked_no_trainable_targets"
    assert payload["trained_targets"] == []
    assert Path(str(tmp_path / "execution_risk.json")).exists() is False
    assert _as_mapping(payload["target_class_counts"])["reconcile_mismatch_risk"] == {
        "negative": 30,
        "positive": 0,
    }
    preflight = _as_mapping(payload["preflight"])
    outcome_coverage = _as_mapping(preflight["outcome_coverage"])
    assert _as_mapping(outcome_coverage["requested_field_coverage"])["reconcile_status"] == 30
    assert _as_mapping(outcome_coverage["requested_target_coverage"])[
        "reconcile_mismatch_risk"
    ] == 30
    assert preflight["requested_maturity_statuses"] == [
        "pending",
        "label_matured",
        "reconciled",
        "fully_matured",
    ]
    assert _as_mapping(status["latest"])["status"] == "blocked_no_trainable_targets"
    assert status["artifact_exists"] is False
    assert int(history["records"]) >= 1
    assert int(audit_payload["records"]) >= 1


def test_service_build_execution_aware_report_emits_execution_reranking_payload(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=36,
    )
    service.train_execution_risk_model(
        artifact_path=str(tmp_path / "execution_risk.json"),
        min_samples_per_target=12,
        epochs=120,
        seed=11,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    shadow_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000"],
        artifact_path=str(tmp_path / "shadow_model.json"),
    )
    shadow_registry = _as_mapping(shadow_training["model_registry"])
    report_payload = _as_mapping(
        service.build_execution_aware_report(
            model_id=str(shadow_registry["model_id"]),
            split_names=["test"],
            preview_limit=3,
            include_rows=False,
        )
    )
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="execution_aware_report_built")
    )

    assert report_payload["shadow_model_id"] == shadow_registry["model_id"]
    assert report_payload["champion_model_id"] == champion_registry["model_id"]
    assert int(report_payload["row_count"]) >= 1
    assert "shadow_mean_can_fill" in _as_mapping(report_payload["summary_metrics"])
    assert "shadow_high_risk_ratio" in _as_mapping(report_payload["summary_metrics"])
    assert int(audit_payload["records"]) >= 1


def test_service_evaluate_learning_model_promotion_gate_can_auto_approve(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    manifest_payload = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )
    shadow_bundle = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest_payload["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=False,
        )
    )

    gate_payload = _as_mapping(
        service.evaluate_learning_model_promotion_gate(
            model_id=str(shadow_bundle["shadow_model_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            approve_if_passed=True,
        )
    )
    entry = _as_mapping(service.model_registry_entry(str(shadow_bundle["shadow_model_id"])))
    registry_transition = _as_mapping(gate_payload["registry_transition"])
    records = cast(list[object], registry_transition["records"])
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="learning_model_promotion_gate_evaluated")
    )
    latest_event = _as_mapping(cast(list[object], audit_payload["events"])[-1])
    latest_event_payload = _as_mapping(latest_event["payload"])

    assert gate_payload["ok"] is True
    assert gate_payload["mode"] == "learning_model_promotion_gate"
    assert gate_payload["status"] == "pass"
    assert gate_payload["accepted"] is True
    assert gate_payload["recommended_action"] == "approve"
    assert gate_payload["shadow_model_id"] == shadow_bundle["shadow_model_id"]
    assert gate_payload["champion_model_id"] == champion_registry["model_id"]
    assert gate_payload["lifecycle_before"] == "trained"
    assert gate_payload["lifecycle_after"] == "approved"
    assert gate_payload["reason_codes"] == ["promotion_gate_passed"]
    assert registry_transition["updated"] is True
    assert registry_transition["action"] == "approved"
    assert len(records) == 2
    assert _as_mapping(records[0])["lifecycle_state"] == "shadow_validated"
    assert _as_mapping(records[1])["lifecycle_state"] == "approved"
    assert entry["lifecycle_state"] == "approved"
    assert int(audit_payload["records"]) >= 1
    assert latest_event["event_type"] == "learning_model_promotion_gate_evaluated"
    assert latest_event_payload["status"] == "pass"
    assert latest_event_payload["accepted"] is True


def test_service_promotion_gate_reads_execution_aware_metrics_when_available(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )
    service.train_execution_risk_model(
        artifact_path=str(tmp_path / "execution_risk.json"),
        min_samples_per_target=10,
        epochs=120,
        seed=13,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    manifest_payload = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )
    shadow_bundle = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest_payload["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=False,
        )
    )
    gate_payload = _as_mapping(
        service.evaluate_learning_model_promotion_gate(
            model_id=str(shadow_bundle["shadow_model_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
        )
    )
    metrics_snapshot = _as_mapping(gate_payload["metrics_snapshot"])
    execution_aware_report = _as_mapping(gate_payload["execution_aware_report"])

    assert "execution_aware_shadow_minus_champion_score" in metrics_snapshot
    assert "execution_aware_shadow_high_risk_ratio" in metrics_snapshot
    assert execution_aware_report["shadow_model_id"] == shadow_bundle["shadow_model_id"]
    assert execution_aware_report["champion_model_id"] == champion_registry["model_id"]


def test_service_evaluate_learning_model_promotion_gate_can_warn_without_transition(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    manifest_payload = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )
    shadow_bundle = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest_payload["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )

    gate_payload = _as_mapping(
        service.evaluate_learning_model_promotion_gate(
            model_id=str(shadow_bundle["shadow_model_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            max_shadow_v2_brier_delta=0.01,
            max_shadow_v2_logloss_delta=0.02,
        )
    )
    entry = _as_mapping(service.model_registry_entry(str(shadow_bundle["shadow_model_id"])))
    registry_transition = _as_mapping(gate_payload["registry_transition"])

    assert gate_payload["ok"] is True
    assert gate_payload["status"] == "warn"
    assert gate_payload["accepted"] is False
    assert gate_payload["recommended_action"] == "manual_review"
    assert "shadow_v2_brier_delta_above_threshold" in cast(list[object], gate_payload["reason_codes"])
    assert "shadow_v2_logloss_delta_above_threshold" in cast(list[object], gate_payload["reason_codes"])
    assert registry_transition["updated"] is False
    assert registry_transition["action"] == "noop"
    assert entry["lifecycle_state"] == "shadow_validated"


def test_service_evaluate_learning_model_promotion_gate_can_block_failed_model(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    manifest_payload = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )
    shadow_bundle = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest_payload["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )
    service.build_shadow_online_v2_report = lambda **kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "report_id": "shadow_online_v2_report_bad",
        "champion_model_id": str(champion_registry["model_id"]),
        "shadow_model_id": str(shadow_bundle["shadow_model_id"]),
        "dataset_manifest_id": str(manifest_payload["dataset_manifest_id"]),
        "row_count": 12,
        "status": "updated",
        "run_result": {
            "status": "updated",
            "samples_considered": 12,
            "samples_used": 12,
            "metrics": {
                "delta_brier": 0.001,
                "delta_logloss": 0.001,
                "signal_divergence_ratio": 0.72,
            },
        },
        "return_summary": {
            "shadow_v2_minus_champion_return": -0.12,
            "shadow_v2_minus_shadow_return": -0.03,
        },
        "execution_summary": {
            "shadow_v2_signal_divergence_ratio": 0.72,
        },
        "m11_v2_report": {
            "status": "redline_breach",
            "score": 41.0,
            "redlines": {"tail_loss_delta": True},
        },
    }

    gate_payload = _as_mapping(
        service.evaluate_learning_model_promotion_gate(
            model_id=str(shadow_bundle["shadow_model_id"]),
            split_names=["test"],
            min_samples=3,
            preview_limit=3,
            block_if_failed=True,
        )
    )
    entry = _as_mapping(service.model_registry_entry(str(shadow_bundle["shadow_model_id"])))
    registry_transition = _as_mapping(gate_payload["registry_transition"])
    transition_records = cast(list[object], registry_transition["records"])

    assert gate_payload["ok"] is True
    assert gate_payload["status"] == "fail"
    assert gate_payload["accepted"] is False
    assert gate_payload["recommended_action"] == "block"
    assert "shadow_online_v2_m11_redline_breach" in cast(list[object], gate_payload["reason_codes"])
    assert "shadow_v2_return_delta_below_threshold" in cast(
        list[object], gate_payload["reason_codes"]
    )
    assert "shadow_v2_signal_divergence_limit_breach" in cast(
        list[object], gate_payload["reason_codes"]
    )
    assert registry_transition["updated"] is True
    assert registry_transition["action"] == "blocked"
    assert len(transition_records) == 1
    assert _as_mapping(transition_records[0])["lifecycle_state"] == "blocked"
    assert entry["lifecycle_state"] == "blocked"
    assert "shadow_online_v2_m11_redline_breach" in str(entry["blocked_reason"])


def test_service_run_learning_manifest_shadow_promotion_gate_can_auto_approve(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "champion_model.json"),
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=str(champion_registry["model_id"]),
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=str(champion_registry["model_id"]),
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )
    manifest_payload = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )

    payload = _as_mapping(
        service.run_learning_manifest_shadow_promotion_gate(
            dataset_manifest_id=str(manifest_payload["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
            approve_if_passed=True,
        )
    )
    shadow_validation = _as_mapping(payload["shadow_validation"])
    promotion_gate = _as_mapping(payload["promotion_gate"])
    entry = _as_mapping(service.model_registry_entry(str(payload["shadow_model_id"])))
    audit_payload = _as_mapping(
        service.audit_events(limit=20, event_type="learning_manifest_shadow_promotion_gate")
    )
    latest_event = _as_mapping(cast(list[object], audit_payload["events"])[-1])
    latest_event_payload = _as_mapping(latest_event["payload"])

    assert payload["ok"] is True
    assert payload["mode"] == "learning_manifest_shadow_promotion_gate"
    assert payload["status"] == "pass"
    assert payload["accepted"] is True
    assert payload["recommended_action"] == "approve"
    assert payload["dataset_manifest_id"] == manifest_payload["dataset_manifest_id"]
    assert payload["champion_model_id"] == champion_registry["model_id"]
    assert payload["evaluation_split_names"] == ["test"]
    assert payload["final_lifecycle_state"] == "approved"
    assert payload["final_role"] == "challenger"
    assert shadow_validation["mode"] == "learning_manifest_shadow_validation"
    assert shadow_validation["ok"] is True
    assert promotion_gate["mode"] == "learning_model_promotion_gate"
    assert promotion_gate["status"] == "pass"
    assert _as_mapping(promotion_gate["registry_transition"])["updated"] is True
    assert entry["lifecycle_state"] == "approved"
    assert int(audit_payload["records"]) >= 1
    assert latest_event["event_type"] == "learning_manifest_shadow_promotion_gate"
    assert latest_event_payload["status"] == "pass"
    assert latest_event_payload["accepted"] is True


def test_service_run_learning_manifest_shadow_promotion_gate_skips_gate_when_shadow_validation_fails(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())

    payload = _as_mapping(
        service.run_learning_manifest_shadow_promotion_gate(
            dataset_manifest_id="missing_manifest_id",
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
        )
    )
    shadow_validation = _as_mapping(payload["shadow_validation"])
    promotion_gate = _as_mapping(payload["promotion_gate"])

    assert payload["ok"] is False
    assert payload["mode"] == "learning_manifest_shadow_promotion_gate"
    assert payload["status"] == "fail"
    assert payload["accepted"] is False
    assert payload["shadow_model_id"] == ""
    assert shadow_validation["ok"] is False
    assert promotion_gate["ok"] is False
    assert promotion_gate["reason_codes"] == ["shadow_validation_failed"]
    assert "shadow_validation_failed" in cast(list[object], payload["errors"])
