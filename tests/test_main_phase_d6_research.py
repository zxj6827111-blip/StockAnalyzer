from __future__ import annotations

import shutil
import tempfile
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
        tempfile.mkdtemp(prefix="stock_analyzer_phase_d6_research_")
    )
    temp_root = root / "tmp_phase_d6_research" / external_temp_root.name
    shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    relative_root = Path("tmp_phase_d6_research") / external_temp_root.name
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
        Path("suggestions") / "test_phase_d6_research" / external_temp_root.name
    )
    config.evolution.manifest_path = str(relative_root / "run_manifest.json")
    config.evolution.compliance_db_path = str(relative_root / "compliance.duckdb")
    config.evolution.m2_state_path = str(relative_root / "m2_state.json")
    config.evolution.m3_store_dir = str(relative_root / "artifacts" / "evolution" / "m3")
    return config


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2028)
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
        feature_names=["feature_a", "feature_b", "feature_c"],
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
                    "feature_c": float((row_index % 3) / 3.0),
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


def _prepare_phase_d6_service(tmp_path: Path) -> StockAnalyzerService:
    service = _new_service(_load_test_config(tmp_path))
    _seed_learning_protocol_samples(service, symbols=["600000", "000001"], rows_per_symbol=30)
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
    return service


def test_service_phase_d6_reports_run_and_persist(tmp_path: Path) -> None:
    service = _prepare_phase_d6_service(tmp_path)

    tabular = _as_mapping(service.build_phase_d_tabular_deep_report(split_names=["test"]))
    tft = _as_mapping(service.build_phase_d_tft_report(split_names=["test"], horizon=1, encoder_length=4))
    finrl = _as_mapping(service.build_phase_d_finrl_report(split_names=["test"], action_threshold=0.5))
    heavy_ts = _as_mapping(service.build_phase_d_heavy_ts_report(split_names=["test"], horizon=2, lookback=5))
    audit = _as_mapping(service.audit_events(limit=20, event_type="phase_d_research_report_built"))

    assert tabular["research_id"] == "tabnet_ft_transformer"
    assert Path(str(tabular["output_path"])).exists() is True
    assert tabular["recommended_family"] in {"tabnet", "ft_transformer"}

    assert tft["research_id"] == "tft_sidecar"
    assert Path(str(tft["output_path"])).exists() is True
    assert tft["status"] == "ok"

    assert finrl["research_id"] == "finrl_sidecar"
    assert Path(str(finrl["output_path"])).exists() is True
    assert "mean_reward" in _as_mapping(finrl["policy_metrics"])

    assert heavy_ts["research_id"] == "heavy_ts_shadow"
    assert Path(str(heavy_ts["output_path"])).exists() is True
    assert heavy_ts["status"] == "ok"

    assert int(audit["records"]) >= 4


def test_main_phase_d6_endpoints_run_with_service_backing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _prepare_phase_d6_service(tmp_path)
    monkeypatch.setattr(main_module, "_service", service)
    client = TestClient(main_module.app)

    tabular = client.post("/research/tabular-deep/report", json={"split_names": ["test"]})
    assert tabular.status_code == 200
    assert tabular.json()["research_id"] == "tabnet_ft_transformer"

    tft = client.post(
        "/research/tft/report",
        json={"split_names": ["test"], "horizon": 1, "encoder_length": 4},
    )
    assert tft.status_code == 200
    assert tft.json()["research_id"] == "tft_sidecar"

    finrl = client.post(
        "/research/finrl/report",
        json={"split_names": ["test"], "action_threshold": 0.5},
    )
    assert finrl.status_code == 200
    assert finrl.json()["research_id"] == "finrl_sidecar"

    heavy_ts = client.post(
        "/research/heavy-ts/report",
        json={"split_names": ["test"], "horizon": 2, "lookback": 5},
    )
    assert heavy_ts.status_code == 200
    assert heavy_ts.json()["research_id"] == "heavy_ts_shadow"
