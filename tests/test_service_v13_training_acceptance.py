from __future__ import annotations

import json
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


class IntradaySyntheticProvider:
    def __init__(self, seed_offset: int) -> None:
        base = SyntheticProvider(seed_offset=seed_offset)
        self._fetch_daily_bars = cast(Callable[[str, int], pd.DataFrame], base.fetch_daily_bars)

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        return self._fetch_daily_bars(symbol, lookback_days)

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=lookback_days)
        frame = pd.DataFrame(
            {
                "tail30_volume_share": [0.55] * len(dates),
                "morning30_volume_share": [0.25] * len(dates),
                "above_vwap_ratio": [0.60] * len(dates),
                "price_efficiency": [0.70] * len(dates),
            },
            index=dates,
        )
        frame.index.name = "date"
        return frame


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


def _load_test_config(base_dir: Path | None = None) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0
    config.week5.auto_notify = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = base_dir or Path(tempfile.mkdtemp(prefix="stock_analyzer_tests_"))
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_v13.json")
    return config


def _new_service(
    config: StockAnalyzerConfig,
    provider: object | None = None,
) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    runtime_provider = provider or SyntheticProvider(seed_offset=2027)
    _patch_attr(service, "_provider", runtime_provider)
    _patch_attr(service._pipeline, "_provider", runtime_provider)
    _patch_attr(service, "_realtime_provider", runtime_provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", runtime_provider)
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
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
            service._sample_store.write_snapshot(snapshot)
            service._sample_store.upsert_outcome(outcome)
            row_index += 1


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


def test_service_single_symbol_training_includes_intraday_summary_features(tmp_path: Path) -> None:
    config = _load_test_config()
    artifact_path = tmp_path / "model_with_intraday.json"
    config.training.artifact_path = str(artifact_path)
    provider = IntradaySyntheticProvider(seed_offset=2468)
    service = _new_service(config, provider=provider)

    payload = service.train_models(
        symbol="600000",
        lookback_days=320,
        artifact_path=str(artifact_path),
        full_market=False,
    )

    assert payload["predictor_loaded"] is True
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    feature_columns = artifact.get("feature_columns", [])
    assert any(str(name).startswith("i1m_") for name in feature_columns)
    assert any(str(name).startswith("i5m_") for name in feature_columns)


def test_week5_signal_pool_candidates_expose_acceptance_trace_fields() -> None:
    config = _load_test_config()
    config.training.artifact_path = str(
        Path(__file__).resolve().parents[1] / "artifacts" / "nonexistent_test_model.json"
    )
    service = _new_service(config, provider=SyntheticProvider(seed_offset=2027))

    report = service.run_week5_scan(symbols=["600000", "000001"], notify_enabled=False)

    signal_pool = _as_mapping(report["signal_pool"])
    candidate = _as_mapping(cast(list[object], signal_pool["candidates"])[0])
    assert "board_component" in candidate
    assert "completion_component" in candidate
    assert "background_completion_score" in candidate
    assert 0.0 <= _as_float(candidate["background_completion_score"]) <= 1.0


def test_service_full_market_training_prefers_learning_protocol_when_samples_exist(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.training.artifact_path = str(tmp_path / "protocol_model.json")
    config.training.min_samples = 20
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

    assert payload["ok"] is True
    assert payload["predictor_loaded"] is True
    assert payload["input_mode"] == "sample_store"
    assert payload["protocol_attempted"] is True
    assert payload["protocol_fallback_reason"] == ""
    assert str(payload["dataset_manifest_id"]).startswith("dataset_manifest_v1_")
    artifact_payload = _as_mapping(_as_mapping(payload["result"])["artifact"])
    assert artifact_payload["dataset_manifest_id"] == payload["dataset_manifest_id"]
    assert artifact_payload["feature_schema_id"] != ""


def test_service_full_market_training_falls_back_to_bars_when_protocol_samples_are_insufficient(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.training.artifact_path = str(tmp_path / "fallback_model.json")
    config.training.min_samples = 20
    service = _new_service(config, provider=IntradaySyntheticProvider(seed_offset=3579))
    _seed_learning_protocol_samples(
        service,
        symbols=["600000"],
        rows_per_symbol=5,
    )

    payload = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000", "000001"],
        artifact_path=str(tmp_path / "fallback_model.json"),
    )

    assert payload["ok"] is True
    assert payload["predictor_loaded"] is True
    assert payload["input_mode"] == "bars"
    assert payload["protocol_attempted"] is True
    assert str(payload["protocol_fallback_reason"]).startswith(
        "learning_protocol_insufficient_samples"
    )
    assert payload["dataset_manifest_id"] == ""


def test_service_full_market_training_prefers_richer_projection_compatible_schema(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.training.artifact_path = str(tmp_path / "protocol_projection_model.json")
    config.training.min_samples = 20
    service = _new_service(config, provider=FailingBarsProvider())
    current_record = _seed_projection_compatible_learning_protocol_samples(
        service,
        symbol="600000",
        legacy_rows=16,
        current_rows=8,
    )

    payload = service.train_models(
        full_market=True,
        lookback_days=240,
        preferred_symbols=["600000"],
        artifact_path=str(tmp_path / "protocol_projection_model.json"),
    )

    assert payload["ok"] is True
    assert payload["predictor_loaded"] is True
    assert payload["input_mode"] == "sample_store"
    assert payload["protocol_attempted"] is True
    assert payload["protocol_fallback_reason"] == ""
    assert payload["dataset_rows"] == 24
    artifact_payload = _as_mapping(_as_mapping(payload["result"])["artifact"])
    assert artifact_payload["feature_schema_id"] == current_record.feature_schema_id
    assert "feature_c" in cast(list[object], artifact_payload["feature_columns"])


def _seed_projection_compatible_learning_protocol_samples(
    service: StockAnalyzerService,
    *,
    symbol: str,
    legacy_rows: int,
    current_rows: int,
):
    legacy_record = service._feature_schema_registry.register_feature_names(
        feature_names=["feature_a", "feature_b"],
        feature_schema_id="feature_schema_legacy",
        feature_engineer_version="test",
        code_version="git:test",
    )
    current_record = service._feature_schema_registry.register_feature_names(
        feature_names=["feature_a", "feature_b", "feature_c"],
        feature_schema_id="feature_schema_current",
        feature_engineer_version="test",
        code_version="git:test",
        projection_compatible_from=[legacy_record.feature_schema_id],
    )
    label_record = service._label_policy_registry.register_from_config(service._config.labels)
    base_time = datetime.now(UTC) - timedelta(days=max(45, legacy_rows + current_rows + 30))

    for index in range(legacy_rows):
        decision_time = base_time + timedelta(days=index)
        snapshot = SignalSnapshot(
            snapshot_id=f"{symbol}-legacy-{index:03d}",
            code_version="git:test",
            symbol=symbol,
            strategy="trend",
            decision_time=decision_time,
            feature_vector={
                "feature_a": float((index % 5) / 5.0),
                "feature_b": float((index % 7) - 3),
            },
            feature_schema_id=legacy_record.feature_schema_id,
            feature_schema_hash=legacy_record.feature_schema_hash,
            runtime_config_hash="runtime_hash_test",
            label_policy_id=label_record.label_policy_id,
            label_policy_hash=label_record.label_policy_hash,
        )
        outcome = OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            maturity_status=MaturityStatus.RECONCILED,
            label_mature_time=decision_time + timedelta(days=service._config.labels.horizon_days),
            realized_return=0.08 if index % 2 == 0 else -0.05,
            max_favorable_excursion=0.09 if index % 2 == 0 else 0.01,
            max_adverse_excursion=-0.01 if index % 2 == 0 else -0.07,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        )
        service._sample_store.write_snapshot(snapshot)
        service._sample_store.upsert_outcome(outcome)

    for index in range(current_rows):
        decision_time = base_time + timedelta(days=legacy_rows + index)
        snapshot = SignalSnapshot(
            snapshot_id=f"{symbol}-current-{index:03d}",
            code_version="git:test",
            symbol=symbol,
            strategy="trend",
            decision_time=decision_time,
            feature_vector={
                "feature_a": float(((index + 1) % 5) / 5.0),
                "feature_b": float(((index + 2) % 7) - 3),
                "feature_c": float(index) / 10.0,
            },
            feature_schema_id=current_record.feature_schema_id,
            feature_schema_hash=current_record.feature_schema_hash,
            runtime_config_hash="runtime_hash_test",
            label_policy_id=label_record.label_policy_id,
            label_policy_hash=label_record.label_policy_hash,
        )
        outcome = OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            maturity_status=MaturityStatus.RECONCILED,
            label_mature_time=decision_time + timedelta(days=service._config.labels.horizon_days),
            realized_return=0.07 if index % 2 == 0 else -0.04,
            max_favorable_excursion=0.08 if index % 2 == 0 else 0.01,
            max_adverse_excursion=-0.01 if index % 2 == 0 else -0.06,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        )
        service._sample_store.write_snapshot(snapshot)
        service._sample_store.upsert_outcome(outcome)

    return current_record
