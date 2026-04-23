from __future__ import annotations

import tempfile
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _load_test_config(base_dir: Path | None = None) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    external_temp_root = base_dir or Path(
        tempfile.mkdtemp(prefix="stock_analyzer_runtime_status_")
    )
    temp_root = root / "tmp_runtime_status" / external_temp_root.name
    shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    relative_root = Path("tmp_runtime_status") / external_temp_root.name
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.week5.auto_notify = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state.json")
    config.training.artifact_path = str(temp_root / "protocol_model.json")
    config.training.min_samples = 20
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.evolution.auto_run = False
    config.evolution.report_dir = str(relative_root / "evolution_history")
    config.evolution.suggestions_dir = str(
        Path("suggestions") / "test_runtime_status" / external_temp_root.name
    )
    config.evolution.manifest_path = str(relative_root / "run_manifest.json")
    config.evolution.compliance_db_path = str(relative_root / "compliance.duckdb")
    config.evolution.m2_state_path = str(relative_root / "m2_state.json")
    config.evolution.m3_store_dir = str(relative_root / "artifacts" / "evolution" / "m3")
    return config


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2027)
    object.__setattr__(service, "_provider", provider)
    object.__setattr__(service._pipeline, "_provider", provider)
    object.__setattr__(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        object.__setattr__(service._realtime_pipeline, "_provider", provider)
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
                data_quality_score=0.92 if row_index % 2 == 0 else 0.78,
                sample_weight=1.2 if row_index % 2 == 0 else 0.9,
                runtime_config_hash="runtime_hash_test",
                label_policy_id=label_record.label_policy_id,
                label_policy_hash=label_record.label_policy_hash,
            )
            maturity = (
                MaturityStatus.FULLY_MATURED if row_index % 3 == 0 else MaturityStatus.RECONCILED
            )
            fidelity = (
                BackfillFidelityTier.GOLD if row_index % 2 == 0 else BackfillFidelityTier.SILVER
            )
            outcome = OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=maturity,
                label_mature_time=decision_time + timedelta(days=service._config.labels.horizon_days),
                realized_return=0.08 if row_index % 2 == 0 else -0.05,
                max_favorable_excursion=0.09 if row_index % 2 == 0 else 0.01,
                max_adverse_excursion=-0.01 if row_index % 2 == 0 else -0.07,
                execution_fill_ratio=0.96 if row_index % 2 == 0 else 0.78,
                realized_slippage_bp=7.0 if row_index % 2 == 0 else 16.0,
                reconcile_status="ok" if row_index % 2 == 0 else "mismatch",
                sim_vs_broker_diff=0.004 if row_index % 2 == 0 else 0.031,
                backfill_fidelity_tier=fidelity,
                backfill_source="runtime_observed",
            )
            service._sample_store.write_snapshot(snapshot)
            service._sample_store.upsert_outcome(outcome)
            row_index += 1


def _seed_runtime_status_artifacts(service: StockAnalyzerService, tmp_path: Path) -> None:
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )
    champion_training = _as_mapping(
        service.train_models(
            full_market=True,
            lookback_days=240,
            preferred_symbols=["600000", "000001"],
            artifact_path=str(tmp_path / "champion_model.json"),
        )
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    champion_model_id = str(champion_registry["model_id"])
    service.update_model_registry_lifecycle(
        model_id=champion_model_id,
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=champion_model_id,
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=champion_model_id,
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )
    shadow_training = _as_mapping(
        service.train_models(
            full_market=True,
            lookback_days=240,
            preferred_symbols=["600000"],
            artifact_path=str(tmp_path / "shadow_model.json"),
        )
    )
    shadow_registry = _as_mapping(shadow_training["model_registry"])
    service.build_shadow_online_v2_report(
        model_id=str(shadow_registry["model_id"]),
        split_names=["test"],
        min_samples=1,
        preview_limit=3,
    )
    service.run_evolution_offhours(
        symbols=["600000", "000001"],
        dry_run=True,
        timestamp=datetime(2026, 3, 30, 20, 40, tzinfo=UTC),
        source_trace_id="runtime-status-seed",
    )


def test_runtime_status_endpoints_cover_learning_and_registry_views(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _new_service(_load_test_config(tmp_path))
    _seed_runtime_status_artifacts(service, tmp_path)
    monkeypatch.setattr(main_module, "_service", service)
    client = TestClient(main_module.app)

    store_status = client.get("/learning/store/status")
    assert store_status.status_code == 200
    store_status_payload = store_status.json()
    assert store_status_payload["status"] == "ready"
    assert store_status_payload["cold_start_ready"] is True
    assert store_status_payload["sample_store"]["dataset_manifests"] >= 1

    store_metrics = client.get("/learning/store/metrics")
    assert store_metrics.status_code == 200
    store_metrics_payload = store_metrics.json()
    assert store_metrics_payload["maturity"]["fully_matured_count"] >= 1
    assert "meets_min_train_samples" in store_metrics_payload["promotion_readiness"]

    manifests_status = client.get("/learning/manifests/status", params={"manifest_limit": 5})
    assert manifests_status.status_code == 200
    manifests_payload = manifests_status.json()
    assert manifests_payload["records"] >= 1
    assert manifests_payload["latest"]["dataset_manifest_id"]

    registry_status = client.get("/models/registry/status", params={"limit": 10})
    assert registry_status.status_code == 200
    registry_payload = registry_status.json()
    assert registry_payload["records"] >= 2
    assert registry_payload["role_breakdown"]["champion"] >= 1
    assert registry_payload["active_champion"]["model_id"]


def test_runtime_status_endpoints_cover_shadow_v2_and_m3_views(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _new_service(_load_test_config(tmp_path))
    _seed_runtime_status_artifacts(service, tmp_path)
    monkeypatch.setattr(main_module, "_service", service)
    client = TestClient(main_module.app)

    shadow_status = client.get("/shadow/v2/status", params={"limit": 10})
    assert shadow_status.status_code == 200
    shadow_payload = shadow_status.json()
    assert shadow_payload["records"] >= 1
    assert shadow_payload["latest"]["report_id"]
    assert shadow_payload["latest"]["shadow_model_id"]

    m3_status = client.get("/m3/profile/status")
    assert m3_status.status_code == 200
    m3_payload = m3_status.json()
    assert m3_payload["profile"]["vector_profile_id"]
    assert m3_payload["profile"]["vector_dim"] >= 5
    assert m3_payload["store"]["vector_count"] >= 1
