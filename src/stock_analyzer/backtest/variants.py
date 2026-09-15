"""C3 配对比较的变体打分器。

变体定义**预注册**在 ``docs/learning_chain_c3_preregistration_20260915.md``：
参数在跑任何一次评估之前钉死，不得看完显著性再挑。本模块是那份文档的可执行形式，
``variant_definitions()`` 把定义原样带进报告，便于事后核对有没有偷改口径。

五个变体：

============  ==========================================================
``blend``     修后现有模型（LightGBM+XGBoost → isotonic → meta 混合），取校准后分数
``raw_blend`` 同一产物取校准前分数，回答「伤害排序的是校准还是模型」
``ridge``     折内标准化 + Ridge(alpha=1.0)，线性对照，不接校准
``stump``     折内 DecisionTree(max_depth=1)，强正则单树端点，不接校准
``reversal``  固定反转基线（−ret_20d），不训练
============  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import pandas as pd

from stock_analyzer.models.predictor import SignalPredictor

VARIANTS = ("blend", "raw_blend", "ridge", "stump", "reversal")

# 预注册参数（改这些值等于改口径，必须同步改预注册文档并留痕）。
REVERSAL_PAST_RETURN_COLUMN = "ret_20d"
RIDGE_ALPHA = 1.0
STUMP_MAX_DEPTH = 1
STUMP_MIN_LEAF_FRACTION = 0.01
STUMP_MIN_LEAF_FLOOR = 20

# C6 合并实验（预注册见 docs/learning_chain_c6_merge_experiment_preregistration_20260915.md）。
# 每日横截面内把模型分数与 −ret_20d 各自秩标准化后按 w 合并：
#   z_model = (rank_avg(score) - 0.5)/n，z_rev = (rank_avg(-ret_20d) - 0.5)/n
#   merged(w) = w·z_model + (1-w)·z_rev
# 均匀秩分数是单调不变的（纯秩空间），不引入额外的变换选择。0.50 是无参数的
# 唯一选择，故作主判据；0.25/0.75 只描述性报告，不得挑最好的那个下结论。
MERGE_WEIGHTS = (0.25, 0.5, 0.75)
MERGE_PRIMARY_WEIGHT = 0.5
# 合并横截面的最小样本下限：与残差诊断一致，样本太少时秩相关不可信。
MERGE_MIN_CROSS_SECTION = 5
# 噪声地板（C3 §5.1 实测：同配置两个复跑目录的 |ΔIC| ≈ 0.0019）。配对增量小于
# 它时不可解读为信号——这正是本实验要检出的效应量级，所以必须写成硬条件。
MERGE_NOISE_FLOOR = 0.0019


def merge_weight_label(weight: float) -> str:
    """权重 → 报告键（``0.5`` → ``w0.50``）；两位小数，避免 0.5/0.50 两种写法。"""
    return f"w{float(weight):.2f}"


def merge_anchor_labels() -> dict[str, str]:
    """两个端点在同一次运行内的键名（见预注册 §4：端点必须与网格同源）。"""
    return {"model": "anchor_model", "reversal": "anchor_reversal"}


def merge_experiment_definition() -> dict[str, object]:
    """预注册定义的机器可读副本，随报告落盘便于事后核对有没有偷改口径。"""
    return {
        "weights": [float(w) for w in MERGE_WEIGHTS],
        "primary_weight": float(MERGE_PRIMARY_WEIGHT),
        "rank": "uniform_index = (average_rank - 0.5) / n",
        "past_return_column": REVERSAL_PAST_RETURN_COLUMN,
        "min_cross_section": MERGE_MIN_CROSS_SECTION,
        "noise_floor": MERGE_NOISE_FLOOR,
        "pairing": "同一次 fold 运行内计算端点与网格（配对差里不含跨运行训练噪声）",
        "primary_rule": (
            "Δ(merged−model) 配对 CI 下界>0 且均值>=noise_floor 且 "
            "Δ(merged−reversal) 配对 CI 下界>0 且 merged 自身 CI 下界>0"
        ),
        "stability_gate": "0.25/0.75/0.50 三者相对模型的 ΔIC 符号必须一致，否则降级 INCONCLUSIVE",
    }


class FoldScorer(Protocol):
    """折内打分器：先 ``fit`` 折内训练段，再对评估日的横截面 ``score``。"""

    def fit(self, *, features: pd.DataFrame, labels: pd.Series) -> None: ...

    def score(self, frame: pd.DataFrame) -> pd.Series: ...


def variant_definitions() -> dict[str, dict[str, object]]:
    """预注册定义的机器可读副本，随报告一起落盘。"""
    return {
        "blend": {
            "family": "model",
            "score": "calibrated_meta_blend",
            "calibration": True,
            "note": "修后现有模型，生产口径",
        },
        "raw_blend": {
            "family": "model",
            "score": "raw_meta_blend",
            "calibration": False,
            "note": "同产物取校准前分数",
        },
        "ridge": {
            "family": "linear",
            "alpha": RIDGE_ALPHA,
            "calibration": False,
            "note": "折内标准化（只用折内训练段统计量）",
        },
        "stump": {
            "family": "tree",
            "max_depth": STUMP_MAX_DEPTH,
            "min_samples_leaf": (
                f"max({STUMP_MIN_LEAF_FLOOR}, ceil({STUMP_MIN_LEAF_FRACTION}*n_train))"
            ),
            "calibration": False,
            "note": "强正则单树，该族最强正则端点",
        },
        "reversal": {
            "family": "fixed_rule",
            "score": f"-{REVERSAL_PAST_RETURN_COLUMN}",
            "calibration": False,
            "note": "固定反转基线，不训练",
        },
    }


class _Imputation:
    """折内缺失值填充（只用训练段统计量，避免折外信息渗入）。

    只有线性变体需要它（LightGBM/XGBoost 系原生处理缺失，不经这里）。
    统一用训练段列均值填补，非有限值一律视作缺失——折外信息不得渗入。
    """

    def __init__(self) -> None:
        self._means: np.ndarray | None = None

    def fit(self, matrix: np.ndarray) -> _Imputation:
        frame = pd.DataFrame(matrix)
        self._means = frame.mean(axis=0, skipna=True).to_numpy(dtype=float)
        self._means = np.nan_to_num(self._means, nan=0.0)
        return self

    def transform(self, matrix: np.ndarray) -> np.ndarray:
        if self._means is None:
            raise ValueError("imputation_not_fitted")
        # copy()：to_numpy 可能给出只读视图，原地填补会抛 assignment destination is read-only
        out = pd.DataFrame(matrix).to_numpy(dtype=float).copy()
        bad = ~np.isfinite(out)
        if bad.any():
            out[bad] = np.take(self._means, np.where(bad)[1])
        return out


class ModelScorer:
    """``blend`` / ``raw_blend``：复用 ``ModelTrainer`` 与 ``SignalPredictor``。"""

    def __init__(self, *, trainer: Any, feature_columns: list[str], use_raw: bool) -> None:
        self._trainer = trainer
        self._feature_columns = list(feature_columns)
        self._use_raw = bool(use_raw)
        self._predictor: SignalPredictor | None = None

    def fit(self, *, features: pd.DataFrame, labels: pd.Series) -> None:
        trained = self._trainer.train_on_feature_label(features=features, labels=labels)
        self._predictor = SignalPredictor.from_artifact(trained.artifact)

    def score(self, frame: pd.DataFrame) -> pd.Series:
        if self._predictor is None:
            raise ValueError("scorer_not_fitted")
        block = frame[self._feature_columns]
        if self._use_raw:
            values = self._predictor.predict_rows_with_raw(block)["raw_blend"]
        else:
            values = self._predictor.predict_rows(block)["meta"]
        return pd.Series(np.asarray(values, dtype=float), index=frame.index, name="score")


@dataclass
class _LinearScorer:
    """``ridge``：折内标准化 + **闭式解** ridge，不接校准。

    用 numpy 闭式解而不是 sklearn：本变体只是「线性对照」，不值当为它引入项目
    当前没有的依赖（容器里没有 sklearn）。闭式解也顺带消除了求解器的随机性。
    NaN/非有限值按折内列均值填补（只用训练段统计量）。
    """

    feature_columns: list[str]
    alpha: float = RIDGE_ALPHA

    def __post_init__(self) -> None:
        self._impute = _Imputation()
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._coef: np.ndarray | None = None
        self._intercept = 0.0

    def fit(self, *, features: pd.DataFrame, labels: pd.Series) -> None:
        raw = self._matrix(features)
        matrix = self._impute.fit(raw).transform(raw)
        self._mean = matrix.mean(axis=0)
        self._std = matrix.std(axis=0)
        self._std[self._std == 0.0] = 1.0
        design = (matrix - self._mean) / self._std
        target = labels.to_numpy(dtype=float)
        self._intercept = float(np.mean(target))
        centered = target - self._intercept
        gram = design.T @ design + float(self.alpha) * np.eye(design.shape[1])
        self._coef = np.linalg.solve(gram, design.T @ centered)

    def score(self, frame: pd.DataFrame) -> pd.Series:
        if self._coef is None or self._mean is None or self._std is None:
            raise ValueError("scorer_not_fitted")
        matrix = self._impute.transform(self._matrix(frame))
        values = ((matrix - self._mean) / self._std) @ self._coef + self._intercept
        return pd.Series(values, index=frame.index, name="score")

    def _matrix(self, frame: pd.DataFrame) -> np.ndarray:
        block = frame.reindex(columns=self.feature_columns)
        return block.to_numpy(dtype=float)


class StumpScorer:
    """``stump``：强正则单树（深度 1、两个叶子），不接校准。

    用 LightGBM 单棵树而不是 sklearn：既有的 LightGBM 依赖原生处理缺失特征，
    无需填补，也让「强正则单树」与主线模型同一实现家族（少一处实现差异）。
    """

    def __init__(self, *, feature_columns: list[str]) -> None:
        self._feature_columns = list(feature_columns)
        self._model: Any | None = None
        self.min_child_samples = 0

    def fit(self, *, features: pd.DataFrame, labels: pd.Series) -> None:
        import lightgbm as lgb  # noqa: WPS433 - 仅此变体需要

        n_train = max(1, len(features))
        self.min_child_samples = max(
            STUMP_MIN_LEAF_FLOOR, int(np.ceil(STUMP_MIN_LEAF_FRACTION * n_train))
        )
        matrix = features.reindex(columns=self._feature_columns).to_numpy(dtype=float)
        # 用原生 lgb.train 而不是 LGBMRegressor：后者的 sklearn 包装层要求安装
        # scikit-learn，而项目依赖里没有它（主线适配器 models/adapters.py 同样走原生 API）。
        params = {
            "objective": "regression",
            "max_depth": STUMP_MAX_DEPTH,
            "num_leaves": 2,
            "min_data_in_leaf": self.min_child_samples,
            "learning_rate": 1.0,
            "verbose": -1,
            "deterministic": True,
            "seed": 0,
            "num_threads": 1,
        }
        dataset = lgb.Dataset(matrix, label=labels.to_numpy(dtype=float))
        self._model = lgb.train(params=params, train_set=dataset, num_boost_round=1)

    def score(self, frame: pd.DataFrame) -> pd.Series:
        if self._model is None:
            raise ValueError("scorer_not_fitted")
        matrix = frame.reindex(columns=self._feature_columns).to_numpy(dtype=float)
        return pd.Series(self._model.predict(matrix), index=frame.index, name="score")


class ReversalScorer:
    """``reversal``：固定反转基线，不训练（``fit`` 是空操作）。"""

    def __init__(self, *, column: str = REVERSAL_PAST_RETURN_COLUMN) -> None:
        self._column = str(column)

    def fit(self, *, features: pd.DataFrame, labels: pd.Series) -> None:  # noqa: WPS611
        """不训练：反转基线的定义必须与数据无关，否则不再是「固定」基线。"""

    def score(self, frame: pd.DataFrame) -> pd.Series:
        if self._column not in frame.columns:
            return pd.Series(np.nan, index=frame.index, name="score")
        values = pd.to_numeric(frame[self._column], errors="coerce").to_numpy(dtype=float)
        return pd.Series(-values, index=frame.index, name="score")


def build_fold_scorer(
    *,
    variant: str,
    trainer: Any,
    feature_columns: list[str],
) -> FoldScorer:
    """按预注册定义构造折内打分器；未知变体 fail-closed。"""
    key = str(variant).strip().lower()
    if key == "blend":
        return ModelScorer(trainer=trainer, feature_columns=feature_columns, use_raw=False)
    if key == "raw_blend":
        return ModelScorer(trainer=trainer, feature_columns=feature_columns, use_raw=True)
    if key == "ridge":
        return _LinearScorer(feature_columns=list(feature_columns))
    if key == "stump":
        return StumpScorer(feature_columns=list(feature_columns))
    if key == "reversal":
        return ReversalScorer()
    raise ValueError(f"unknown_variant:{variant!r}; expected one of {VARIANTS}")


def is_model_variant(variant: str) -> bool:
    """是否需要训练（决定能否跳过 trainer 构造，reversal 可省掉整次训练）。"""
    return str(variant).strip().lower() in {"blend", "raw_blend"}
