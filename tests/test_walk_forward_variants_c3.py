"""C3 变体打分器与 raw 分数入口。

预注册定义在 ``docs/learning_chain_c3_preregistration_20260915.md``：变体参数在
跑评估之前固定，本文件既测行为也**锁住定义**（改参数必须同时改预注册文档，否则
这里先红）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.backtest.variants import (
    RIDGE_ALPHA,
    STUMP_MAX_DEPTH,
    STUMP_MIN_LEAF_FLOOR,
    VARIANTS,
    ModelScorer,
    ReversalScorer,
    build_fold_scorer,
    is_model_variant,
    variant_definitions,
)

FEATURES = ["f0", "f1", "ret_20d"]


def _train_frame(n: int = 200, seed: int = 7) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "f0": rng.normal(size=n),
            "f1": rng.normal(size=n),
            "ret_20d": rng.normal(scale=0.05, size=n),
        }
    )
    labels = pd.Series((frame["f0"] * 0.6 + rng.normal(scale=0.5, size=n)).to_numpy())
    return frame, labels


def _eval_frame(n: int = 30, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "symbol": [f"{300000 + i:06d}" for i in range(n)],
            "f0": rng.normal(size=n),
            "f1": rng.normal(size=n),
            "ret_20d": rng.normal(scale=0.05, size=n),
        }
    )


# --- 定义锁定 ----------------------------------------------------------------


def test_variant_set_and_definitions_are_locked() -> None:
    assert VARIANTS == ("blend", "raw_blend", "ridge", "stump", "reversal")
    defs = variant_definitions()
    assert set(defs) == set(VARIANTS)
    # 预注册参数不得被悄悄改掉（改了就要同步改文档并在 §6 留痕）
    assert defs["ridge"]["alpha"] == RIDGE_ALPHA
    assert defs["stump"]["max_depth"] == STUMP_MAX_DEPTH
    assert defs["reversal"]["score"] == "-ret_20d"
    assert defs["blend"]["calibration"] is True
    assert defs["raw_blend"]["calibration"] is False


def test_unknown_variant_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown_variant"):
        build_fold_scorer(variant="ridge_lasso", trainer=None, feature_columns=FEATURES)


def test_is_model_variant() -> None:
    assert is_model_variant("blend") and is_model_variant("RAW_BLEND")
    assert not is_model_variant("ridge")
    assert not is_model_variant("reversal")


# --- ridge / stump -----------------------------------------------------------


def _fit_score(scorer, train, eval_frame) -> pd.Series:
    features, labels = train
    scorer.fit(features=features, labels=labels)
    return scorer.score(eval_frame)


def test_ridge_scores_align_with_eval_index() -> None:
    scorer = build_fold_scorer(variant="ridge", trainer=None, feature_columns=FEATURES)
    eval_frame = _eval_frame().set_index("symbol")
    scores = _fit_score(scorer, _train_frame(), eval_frame)
    assert list(scores.index) == list(eval_frame.index)
    assert scores.notna().all()
    assert scores.nunique() > 1


def test_ridge_handles_missing_and_nonfinite_features() -> None:
    """线性对照不能因缺失值崩掉；缺失按折内均值填补。"""
    scorer = build_fold_scorer(variant="ridge", trainer=None, feature_columns=FEATURES)
    train = _train_frame()
    train[0].loc[train[0].index[:20], "f1"] = np.nan
    scorer.fit(features=train[0], labels=train[1])
    eval_frame = _eval_frame()
    eval_frame.loc[eval_frame.index[:5], "f0"] = np.nan
    eval_frame.loc[eval_frame.index[:3], "f1"] = np.inf
    scores = scorer.score(eval_frame)
    assert scores.notna().all()
    assert np.isfinite(scores.to_numpy()).all()


def test_stump_scores_are_heavily_coarsened() -> None:
    """强正则单树：深度 1 → 评估分数最多两个取值。"""
    scorer = build_fold_scorer(variant="stump", trainer=None, feature_columns=FEATURES)
    scores = _fit_score(scorer, _train_frame(), _eval_frame())
    assert scores.nunique() <= 2
    assert scores.notna().all()


def test_stump_min_leaf_floor_applies_to_small_folds() -> None:
    """小折也必须满足 min_samples_leaf 下限，否则单树退化成两个样本的均值。"""
    scorer = build_fold_scorer(variant="stump", trainer=None, feature_columns=FEATURES)
    features, labels = _train_frame(n=40)
    scorer.fit(features=features, labels=labels)  # type: ignore[attr-defined]
    assert scorer.min_child_samples >= STUMP_MIN_LEAF_FLOOR  # noqa: SLF001
    params = scorer._model.params  # noqa: SLF001 - LightGBM Booster 的参数回读
    assert int(params["max_depth"]) == STUMP_MAX_DEPTH  # noqa: SLF001
    assert scorer._model.num_trees() == 1  # noqa: SLF001


# --- reversal ----------------------------------------------------------------


def test_reversal_is_negative_past_return_and_needs_no_fit() -> None:
    eval_frame = _eval_frame()
    scorer = ReversalScorer()
    scorer.fit(features=pd.DataFrame(), labels=pd.Series(dtype=float))
    scores = scorer.score(eval_frame)
    expected = -eval_frame["ret_20d"].to_numpy(dtype=float)
    assert np.allclose(scores.to_numpy(dtype=float), expected)


def test_reversal_missing_column_yields_all_nan_not_zero() -> None:
    """缺列必须给 NaN（=无分数）而不是 0——0 会被当成"中间分数"参与排序。"""
    frame = _eval_frame().drop(columns=["ret_20d"])
    scores = ReversalScorer().score(frame)
    assert scores.isna().all()


def test_reversal_does_not_train() -> None:
    """固定基线的定义必须与数据无关：fit 传任何东西都不改变打分。"""
    frame = _eval_frame()
    scorer = ReversalScorer()
    scorer.fit(features=pd.DataFrame({"x": [1e9]}), labels=pd.Series([1.0]))
    assert np.allclose(
        scorer.score(frame).to_numpy(dtype=float),
        -frame["ret_20d"].to_numpy(dtype=float),
    )


# --- raw 分数独立入口 --------------------------------------------------------


def test_model_scorer_requires_fit_before_scoring() -> None:
    scorer = ModelScorer(trainer=None, feature_columns=FEATURES, use_raw=True)
    with pytest.raises(ValueError, match="scorer_not_fitted"):
        scorer.score(_eval_frame())


def test_predict_rows_contract_unchanged_and_raw_is_separate() -> None:
    """``predict_rows`` 的键集合必须保持三键（生产消费 + 精确字典断言），
    raw 分数只能走 ``predict_rows_with_raw``。"""
    import tempfile
    from pathlib import Path

    from stock_analyzer.models.predictor import SignalPredictor
    from tests.test_model_inference_safety import _artifact_from_payload, _fit_scaled_model

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.json"
        _artifact_from_payload(_fit_scaled_model(seed=23).to_dict(), path=path)
        predictor = SignalPredictor.load(path)
        frame = pd.DataFrame({f"f{i}": [0.1 * i, 0.2 * i] for i in range(4)})
        assert set(predictor.predict_rows(frame)) == {"lgbm", "xgb", "meta"}
        with_raw = predictor.predict_rows_with_raw(frame)
        assert set(with_raw) == {
            "raw_lgbm",
            "raw_xgb",
            "raw_blend",
            "lgbm",
            "xgb",
            "meta",
        }
        # 校准后与 raw 都必须是"每个模型各自的输出"，不能是同一串数
        assert with_raw["lgbm"] == predictor.predict_rows(frame)["lgbm"]
        assert with_raw["meta"] == predictor.predict_rows(frame)["meta"]
        assert len(with_raw["raw_blend"]) == 2


def test_predict_rows_with_raw_empty_frame() -> None:
    import tempfile
    from pathlib import Path

    from stock_analyzer.models.predictor import SignalPredictor
    from tests.test_model_inference_safety import _artifact_from_payload, _fit_scaled_model

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p2.json"
        _artifact_from_payload(_fit_scaled_model(seed=5).to_dict(), path=path)
        predictor = SignalPredictor.load(path)
        empty = pd.DataFrame(columns=[f"f{i}" for i in range(4)])
        assert predictor.predict_rows(empty) == {"lgbm": [], "xgb": [], "meta": []}
        assert predictor.predict_rows_with_raw(empty)["raw_blend"] == []


# --- 端到端接线（小合成面板，避开 NAS 重跑才发现接线错误） ------------------


def _synthetic_panel(days: int = 90, symbols: int = 40, seed: int = 3) -> pd.DataFrame:
    """构造够跑 1 折的最小 PIT 面板。

    列类型对齐真实面板（NAS `artifacts/phase2_label_remediation/pit_dataset_rank/*.parquet`）：
    ``trade_date`` 是 timestamp、``label_mature_trade_date`` 是字符串。用字符串
    造 ``trade_date`` 会掩盖 harness 里 ``d.isoformat()`` 这条真实成立的前提。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-01-05", periods=days)
    rows: list[dict[str, object]] = []
    for offset, day in enumerate(dates):
        mature = dates[min(offset + 5, len(dates) - 1)]
        for idx in range(symbols):
            f0 = float(rng.normal())
            f1 = float(rng.normal())
            past = float(rng.normal(scale=0.05))
            rows.append(
                {
                    "symbol": f"{300000 + idx:06d}",
                    "trade_date": pd.Timestamp(day),
                    "label_mature_trade_date": mature.strftime("%Y-%m-%d"),
                    "label": 1.0 if f0 > 0 else 0.0,
                    "fwd_return": 0.02 * f0 + float(rng.normal(scale=0.01)),
                    "f0": f0,
                    "f1": f1,
                    "ret_20d": past,
                    "is_st": False,
                    "is_delisting_risk": False,
                    "suspended": False,
                }
            )
    return pd.DataFrame(rows)


@pytest.mark.parametrize("variant", ["ridge", "stump", "reversal"])
def test_run_fold_end_to_end_without_trainer(tmp_path, variant: str) -> None:
    """三个不依赖 ModelTrainer 的变体必须走完「fold → 打分 → IC → 组合量」全链。

    blend/raw_blend 也在同一路径上（只多一步训练），这里不重复付训练成本。
    """
    from stock_analyzer.backtest.walk_forward_xsec import (
        PitDatasetStore,
        plan_folds,
        run_fold,
    )

    frame = _synthetic_panel()
    frame.to_parquet(tmp_path / "pit_2026-01.parquet", index=False)
    store = PitDatasetStore(str(tmp_path))
    try:
        trading_dates = store.trading_dates()
        # 用 store 认定的特征列（与真实路径一致），而不是手挑子集
        feature_columns = store.feature_columns
        folds = plan_folds(
            trading_dates=trading_dates,
            dataset_first_date=trading_dates[0],
            dataset_last_date=trading_dates[-1],
            train_window=50,
            test_window=10,
            step=10,
            embargo_days=3,
        )
        assert folds, "合成面板应当至少产生 1 折"
        result = run_fold(
            fold=folds[0],
            store=store,
            trading_dates=trading_dates,
            trainer=None,
            feature_columns=feature_columns,
            embargo_days=3,
            k_precision=[5],
            variant=variant,
        )
    finally:
        store.close()

    assert result.status == "completed", result.invalid_reason
    assert result.daily_ic, "变体必须产出逐日 IC"
    assert result.portfolio_gross, "C5 组合量（毛收益/换手）必须被累计"
    assert len(result.portfolio_gross) == len(result.portfolio_turnover)
    assert result.lookahead_violations == 0


def test_cost_report_and_variant_tag_land_in_payload(tmp_path) -> None:
    """aggregate_report 必须带变体名与成本报告（C5 验收项在报告里可查）。"""
    from stock_analyzer.backtest.walk_forward_xsec import FoldResult, aggregate_report

    fold = FoldResult(
        fold_id=0,
        train_start="2026-01-05",
        train_end="2026-03-01",
        eval_dates=["2026-03-02", "2026-03-03"],
        status="completed",
        daily_ic=[("2026-03-02", 0.1), ("2026-03-03", -0.05)],
        daily_top_bottom=[("2026-03-02", 0.02), ("2026-03-03", -0.01)],
        portfolio_gross=[("2026-03-02", 0.01), ("2026-03-03", -0.004)],
        portfolio_turnover=[("2026-03-02", 0.5), ("2026-03-03", 0.2)],
    )
    report = aggregate_report(
        folds=[fold],
        dataset_meta_rows=1000,
        train_window=50,
        test_window=10,
        step=10,
        embargo_days=3,
        variant="ridge",
        cost_bps=10.0,
    )
    assert report["variant"] == "ridge"
    cost = report["cost_report"]
    assert cost["days"] == 2
    assert cost["cost_bps"] == 10.0
    # 成本必须真的被扣掉：net < gross
    assert cost["net_mean"] < cost["gross_mean"]
    assert "failure_months" in cost


def test_checkpoint_dir_is_isolated_per_variant() -> None:
    """不同变体不得共用 checkpoint 目录（否则会静默复用上个变体的 fold 结果）。"""
    from stock_analyzer.backtest.walk_forward_xsec import checkpoint_dir

    assert checkpoint_dir("out", "blend") != checkpoint_dir("out", "ridge")
    assert checkpoint_dir("out", "RIDGE").name == "checkpoints_ridge"


def test_model_variants_score_raw_vs_calibrated_differently() -> None:
    """blend 取校准后、raw_blend 取校准前——两者的分数必须可区分。"""
    import tempfile
    from pathlib import Path

    from stock_analyzer.models.artifact import ModelArtifact
    from tests.test_model_inference_safety import _artifact_from_payload, _fit_scaled_model

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.json"
        _artifact_from_payload(_fit_scaled_model(seed=17).to_dict(), path=path)
        artifact = ModelArtifact.load(path)

        class _FakeTrained:
            def __init__(self, art) -> None:
                self.artifact = art

        class _FakeTrainer:
            def train_on_feature_label(self, *, features, labels):  # noqa: ANN001, ANN202
                return _FakeTrained(artifact)

        train = _train_frame(n=300, seed=19)
        eval_frame = _eval_frame()
        blend = build_fold_scorer(variant="blend", trainer=_FakeTrainer(), feature_columns=FEATURES)
        raw = build_fold_scorer(
            variant="raw_blend", trainer=_FakeTrainer(), feature_columns=FEATURES
        )
        for scorer in (blend, raw):
            scorer.fit(features=train[0], labels=train[1])
        blend_scores = blend.score(eval_frame).to_numpy(dtype=float)
        raw_scores = raw.score(eval_frame).to_numpy(dtype=float)
        assert np.isfinite(blend_scores).all() and np.isfinite(raw_scores).all()
        assert not np.allclose(blend_scores, raw_scores)
