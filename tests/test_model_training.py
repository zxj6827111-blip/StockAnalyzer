from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.models.adapters import inspect_model_backend_dependencies
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.trainer import ModelTrainer, _binary_auc


def test_model_training_and_predictor_roundtrip(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.validation_ratio = 0.2
    artifact_path = tmp_path / "model.json"

    bars = SyntheticProvider(seed_offset=2468).fetch_daily_bars(symbol="600000", lookback_days=320)
    trainer = ModelTrainer(training=config.training, labels=config.labels, models=config.models)
    result = trainer.train_and_save(bars=bars, output_path=artifact_path)

    assert artifact_path.exists()
    assert result.samples_total >= 40
    assert "__random_baseline__" in result.artifact.feature_columns

    predictor = SignalPredictor.load(artifact_path)
    features = FeatureEngineer().transform(bars)
    probabilities = predictor.predict_row(features.iloc[-1])
    assert set(probabilities.keys()) == {"lgbm", "xgb", "meta"}


def test_train_and_save_backups_existing_alias_before_overwrite(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.model_archive_retention_count = 5
    artifact_path = tmp_path / "model.json"
    bars = SyntheticProvider(seed_offset=1357).fetch_daily_bars(
        symbol="600000",
        lookback_days=320,
    )
    trainer = ModelTrainer(training=config.training, labels=config.labels, models=config.models)

    trainer.train_and_save(bars=bars, output_path=artifact_path)
    trainer.train_and_save(bars=bars, output_path=artifact_path)

    backups = list((tmp_path / ".model_overwrites").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "model.json").is_file()
    assert (backups[0] / "model_sidecars").is_dir() or not (
        tmp_path / "model_sidecars"
    ).exists()


def test_model_training_persists_native_sidecars_when_dependencies_are_available(
    tmp_path: Path,
) -> None:
    dependencies = inspect_model_backend_dependencies()
    if not dependencies["lightgbm"]["installed"] or not dependencies["xgboost"]["installed"]:
        pytest.skip("native model dependencies are unavailable")

    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.validation_ratio = 0.2
    artifact_path = tmp_path / "native_model.json"

    bars = SyntheticProvider(seed_offset=8642).fetch_daily_bars(symbol="600000", lookback_days=320)
    trainer = ModelTrainer(training=config.training, labels=config.labels, models=config.models)
    trainer.train_and_save(bars=bars, output_path=artifact_path)

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    lgbm_model = payload["lgbm_model"]
    xgb_model = payload["xgb_model"]
    lgbm_sidecar_path = artifact_path.parent / str(lgbm_model["sidecar_path"])
    xgb_sidecar_path = artifact_path.parent / str(xgb_model["sidecar_path"])

    assert lgbm_model["backend"] == "lightgbm"
    assert xgb_model["backend"] == "xgboost"
    assert "native_blob" not in lgbm_model
    assert "native_blob_b64" not in xgb_model
    assert lgbm_sidecar_path.exists() is True
    assert xgb_sidecar_path.exists() is True

    predictor = SignalPredictor.load(artifact_path)
    mode = predictor.mode_details()
    assert mode["lgbm_load_source"] == "current_sidecar"
    assert mode["xgb_load_source"] == "current_sidecar"
    assert mode["native_sidecar_fallback_used"] is False


def test_binary_auc_tied_probabilities_is_order_invariant() -> None:
    """全并列概率下 AUC 必须稳定为 0.5，与标签排列无关。

    np.argsort 在并列值上的秩分配不稳定，同一组概率会因标签顺序返回
    0.0/1.0 等任意值；rankdata(method="average") 使并列值取平均秩。
    """
    import itertools

    import numpy as np

    probs = np.array([0.5] * 6)
    results = {
        _binary_auc(np.array(perm, dtype=float), probs)
        for perm in itertools.permutations([1, 1, 1, 0, 0, 0])
    }
    assert results == {0.5}

    # 部分并列：多个 0.5 + 一个可区分值。0.8 是正类则 0.75，是负类则 0.25。
    probs2 = np.array([0.5, 0.5, 0.5, 0.8])
    distinct = {
        round(_binary_auc(np.array(perm, dtype=float), probs2), 6)
        for perm in set(itertools.permutations([1, 1, 0, 0]))
    }
    assert distinct == {0.25, 0.75}


def test_binary_auc_matches_average_rank_reference() -> None:
    """随机含并列样本与标准平均秩 AUC 实现一致。"""
    import numpy as np
    from scipy.stats import rankdata

    def reference(y: np.ndarray, p: np.ndarray) -> float:
        n_pos = int(np.sum(y >= 0.5))
        n_neg = len(y) - n_pos
        if n_pos == 0 or n_neg == 0:
            return 0.5
        ranks = rankdata(p, method="average")
        return float(
            (ranks[y >= 0.5].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
        )

    rng = np.random.default_rng(7)
    for _ in range(200):
        y = rng.integers(0, 2, 60).astype(float)
        p = rng.choice([0.0, 0.1, 0.2, 0.5, 0.5, 0.7, 0.9], 60)
        assert abs(_binary_auc(y, p) - reference(y, p)) < 1e-12
