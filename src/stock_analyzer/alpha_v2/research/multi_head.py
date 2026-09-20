"""Alpha V2 共享 Feature Matrix + 多 Head（S16 / 原 P1-06，含 M2 §18 的性能纪律）。

**资源纪律是硬要求**：NAS 只有 8 CPU / 16GB（API 4G / heavy 4G / critical 3G）。
生产证据表明真正贵的不是模型推理，而是 snapshot / bar fetch / 持久化。所以：

```text
fetch once  →  feature once  →  matrix once  →  predict N heads  →  persist once
```

禁止"完整全市场 Pipeline × 4"。本模块用**一份** :class:`SharedFeatureMatrix`
喂全部 Head，并把"构建/推理/落盘各发生几次"做成可断言的计数器
（:class:`BuildStats`），使"只跑一遍"从口头约定变成可机械验证的属性。

四个 Head 的语义与边界（蓝图 §P1-06）：

============ ========================== ====================== ==========================
Head         目标                        输出                   语义约束
============ ========================== ====================== ==========================
Alpha Rank   5D 可执行超额收益横截面 rank  ``alpha_rank_score``   只能是 **rank_score**
Return       3/5/10/15D 净/超额收益        ``expected_*``         只能是 **expected_return**
Direction    P(net>0) / P(excess>0)       ``p_up_*``             **必须 OOS 校准**才可称概率
Risk         MAE / P(MAE<=-5%)           ``expected_mae_*`` /   **risk_score**，不得回流进
                                         ``p_mae_le_5pct_*``    Alpha（禁止"用风险造 Alpha"）
============ ========================== ====================== ==========================

未校准的 Direction 输出在展示层一律标 ``未校准``，且 ``calibration="none"``——
按 S09 的语义守卫，不校准就**不得**被叫"上涨概率"。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.feature_audit import (
    assert_safe_feature_columns,
    classify_feature_columns,
)
from stock_analyzer.alpha_v2.research.metrics import (
    DEFAULT_HORIZONS,
    PRIMARY_HORIZON,
    SHORT_HORIZONS,
    metric_column,
)
from stock_analyzer.alpha_v2.research.panel import DailyPanel
from stock_analyzer.models.calibration import IsotonicCalibrator

NOT_AVAILABLE = "not_available"

HEAD_ALPHA_RANK = "alpha_rank"
HEAD_EXPECTED_RETURN = "expected_return"
HEAD_DIRECTION = "direction"
HEAD_RISK = "risk"

OUTPUT_KIND_RANK_SCORE = "rank_score"
OUTPUT_KIND_EXPECTED_RETURN = "expected_return"
OUTPUT_KIND_PROBABILITY = "probability"
OUTPUT_KIND_RISK_SCORE = "risk_score"

CALIBRATION_NONE = "none"
CALIBRATION_ISOTONIC_OOS = "isotonic_oos"

MATRIX_SCHEMA = "alpha_v2_shared_feature_matrix.v1"

ALPHA_TARGET_TEMPLATE = "alpha_target_{h}d"
MAE_BREACH_COLUMN = "mae_le_5pct_{h}d"
MAE_BREACH_THRESHOLD = -0.05

DEFAULT_SEED = 20260918
DEFAULT_N_JOBS = 2
DEFAULT_MIN_TRAIN_ROWS = 200
DEFAULT_MIN_CLASS_BALANCE = 0.02


@dataclass(frozen=True, slots=True)
class HeadSpec:
    """一个 Head 的目标契约与输出语义。"""

    name: str
    output_kind: str
    primary_target: str
    targets: tuple[str, ...]
    description: str
    requires_calibration: bool = False
    allowed_display_terms: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "output_kind": self.output_kind,
            "primary_target": self.primary_target,
            "targets": list(self.targets),
            "description": self.description,
            "requires_calibration": bool(self.requires_calibration),
            "allowed_display_terms": list(self.allowed_display_terms),
        }


def _alpha_targets() -> tuple[str, ...]:
    return tuple(ALPHA_TARGET_TEMPLATE.format(h=int(h)) for h in (PRIMARY_HORIZON,))


def _return_targets() -> tuple[str, ...]:
    columns: list[str] = []
    for horizon in DEFAULT_HORIZONS:
        columns.append(metric_column("net_return", horizon))
        columns.append(metric_column("excess_return", horizon))
    return tuple(columns)


def _direction_targets() -> tuple[str, ...]:
    columns: list[str] = []
    for horizon in SHORT_HORIZONS:
        columns.append(f"up_net_{int(horizon)}d")
        columns.append(f"up_excess_{int(horizon)}d")
    return tuple(columns)


def _risk_targets() -> tuple[str, ...]:
    columns: list[str] = []
    for horizon in SHORT_HORIZONS:
        columns.append(metric_column("mae", horizon))
        columns.append(MAE_BREACH_COLUMN.format(h=int(horizon)))
    return tuple(columns)


HEAD_SPECS: tuple[HeadSpec, ...] = (
    HeadSpec(
        name=HEAD_ALPHA_RANK,
        output_kind=OUTPUT_KIND_RANK_SCORE,
        primary_target=ALPHA_TARGET_TEMPLATE.format(h=PRIMARY_HORIZON),
        targets=_alpha_targets(),
        description="5D 可执行超额收益的**当日横截面 rank**（同一天候选中谁更值得买）",
        allowed_display_terms=("Alpha Rank", "alpha_rank_score", "排序分"),
    ),
    HeadSpec(
        name=HEAD_EXPECTED_RETURN,
        output_kind=OUTPUT_KIND_EXPECTED_RETURN,
        primary_target=metric_column("excess_return", PRIMARY_HORIZON),
        targets=_return_targets(),
        description="未来可执行净收益与超额收益的期望值（3/5/10/15D）",
        allowed_display_terms=("预期净收益", "预期超额收益", "expected_excess_return"),
    ),
    HeadSpec(
        name=HEAD_DIRECTION,
        output_kind=OUTPUT_KIND_PROBABILITY,
        primary_target="up_net_5d",
        targets=_direction_targets(),
        description="P(net_return_h>0) 与 P(excess_return_h>0)（3/5D）——只有本 Head 可称概率",
        requires_calibration=True,
        allowed_display_terms=("正收益概率", "方向分"),
    ),
    HeadSpec(
        name=HEAD_RISK,
        output_kind=OUTPUT_KIND_RISK_SCORE,
        primary_target=metric_column("mae", PRIMARY_HORIZON),
        targets=_risk_targets(),
        description="MAE 期望与 P(MAE_5d <= -5%)（下行尾部风险）",
        allowed_display_terms=("预期最大回撤", "下行风险"),
    ),
)

HEAD_NAMES: tuple[str, ...] = tuple(spec.name for spec in HEAD_SPECS)


def head_spec(name: str) -> HeadSpec:
    for spec in HEAD_SPECS:
        if spec.name == name:
            return spec
    raise KeyError(f"unknown head: {name}")


@dataclass
class BuildStats:
    """构建/推理/落盘次数计数器（"只跑一遍"的可机械验证形式）。"""

    feature_build_calls: int = 0
    matrix_build_calls: int = 0
    prediction_calls: int = 0
    persist_calls: int = 0

    def to_payload(self) -> dict[str, int]:
        return {
            "feature_build_calls": int(self.feature_build_calls),
            "matrix_build_calls": int(self.matrix_build_calls),
            "prediction_calls": int(self.prediction_calls),
            "persist_calls": int(self.persist_calls),
        }

    def assert_single_pass(self) -> None:
        if self.matrix_build_calls != 1:
            raise AssertionError(
                f"feature matrix 必须只构建一次，实际 {self.matrix_build_calls} 次"
                "（多 Head 必须共享同一份矩阵，禁止按 Head 重跑全流程）"
            )
        if self.prediction_calls > 1:
            raise AssertionError(
                f"多 Head 必须一次批量推理，实际推理调用 {self.prediction_calls} 次"
            )


@dataclass
class SharedFeatureMatrix:
    """一份共享矩阵：PIT-safe 特征 + 各 Head 的目标列。"""

    frame: pd.DataFrame
    feature_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    schema: str = MATRIX_SCHEMA
    stats: BuildStats = field(default_factory=BuildStats)
    diagnostics: dict[str, object] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return matrix_fingerprint(self.feature_columns, self.frame)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "rows": int(len(self.frame)),
            "feature_columns": len(self.feature_columns),
            "target_columns": list(self.target_columns),
            "matrix_fingerprint": self.fingerprint,
            "build_stats": self.stats.to_payload(),
            "diagnostics": dict(self.diagnostics),
        }


def matrix_fingerprint(feature_columns: Sequence[str], frame: pd.DataFrame) -> str:
    """矩阵指纹：列集合 + 行列规模 + decision_date 范围的确定性摘要。"""
    dates: list[str] = []
    if "decision_date" in frame.columns and not frame.empty:
        dates = sorted({str(value) for value in frame["decision_date"].unique()})
    payload = {
        "columns": sorted(str(column) for column in feature_columns),
        "rows": int(len(frame)),
        "dates": [dates[0], dates[-1]] if dates else [],
        "date_count": len(dates),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 矩阵构建
# ---------------------------------------------------------------------------


def build_head_targets(
    frame: pd.DataFrame,
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    mae_breach_threshold: float = MAE_BREACH_THRESHOLD,
) -> pd.DataFrame:
    """派生 Head 目标列（全部来自 S11 的真实 outcome，不引入任何新口径）。"""
    result = frame.copy()
    primary = int(PRIMARY_HORIZON)
    excess_column = metric_column("excess_return", primary)
    if excess_column in result.columns:
        values = pd.to_numeric(result[excess_column], errors="coerce")
        # 逐日横截面 rank 分位：Head A 学的是"同一天里排在多前"，不是绝对收益
        result[ALPHA_TARGET_TEMPLATE.format(h=primary)] = values.groupby(
            result["decision_date"]
        ).rank(pct=True)
    for horizon in horizons:
        key = int(horizon)
        mae_column = metric_column("mae", key)
        if mae_column not in result.columns:
            continue
        mae = pd.to_numeric(result[mae_column], errors="coerce")
        result[MAE_BREACH_COLUMN.format(h=key)] = (mae <= mae_breach_threshold).where(mae.notna())
    return result


def build_shared_feature_matrix(
    *,
    panel: DailyPanel,
    decisions: Sequence[object],
    outcomes: pd.DataFrame | None = None,
    feature_frame: pd.DataFrame | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    mae_breach_threshold: float = MAE_BREACH_THRESHOLD,
    stats: BuildStats | None = None,
) -> SharedFeatureMatrix:
    """**一次**构建研究矩阵：安全特征 + outcome + Head 目标。

    ``feature_frame`` 可传入已算好的特征（避免重复 FeatureEngineer）；不传则用
    同一份面板现算。无论哪条路径，特征列都要过 S14 的准入断言——未证明 PIT 的列
    会在这里直接抛错，而不是静默进入训练。
    """
    counters = stats if stats is not None else BuildStats()
    features = feature_frame
    if features is None:
        features = _compute_daily_features(panel=panel, decisions=decisions)
        counters.feature_build_calls += 1

    feature_columns = tuple(
        column for column in features.columns if column not in {"decision_date", "symbol"}
    )
    safe_columns = assert_safe_feature_columns(feature_columns)

    matrix = features.loc[:, ["decision_date", "symbol", *safe_columns]].copy()
    if outcomes is not None and not outcomes.empty:
        matrix = matrix.merge(outcomes, on=["decision_date", "symbol"], how="inner")
    matrix = build_head_targets(
        matrix, horizons=horizons, mae_breach_threshold=mae_breach_threshold
    )
    counters.matrix_build_calls += 1

    targets = tuple(
        column for spec in HEAD_SPECS for column in spec.targets if column in matrix.columns
    )
    assignment, unregistered = classify_feature_columns(feature_columns)
    diagnostics = {
        "allowed_feature_columns": len(safe_columns),
        "rejected_feature_columns": len(feature_columns) - len(safe_columns),
        "unregistered_columns": list(unregistered[:50]),
        "group_counts": _group_counts(assignment),
        "rows_with_any_target": int(
            matrix[list(targets)].notna().any(axis=1).sum() if targets else 0
        ),
    }
    return SharedFeatureMatrix(
        frame=matrix,
        feature_columns=safe_columns,
        target_columns=targets,
        stats=counters,
        diagnostics=diagnostics,
    )


def _group_counts(assignment: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in assignment.values():
        counts[group] = counts.get(group, 0) + 1
    return dict(sorted(counts.items()))


def _compute_daily_features(*, panel: DailyPanel, decisions: Sequence[object]) -> pd.DataFrame:
    from stock_analyzer.feature.engineer import FeatureEngineer

    grouped: dict[str, list[object]] = {}
    for item in decisions:
        grouped.setdefault(str(item.symbol), []).append(item)
    engineer = FeatureEngineer()
    rows: list[dict[str, object]] = []
    for symbol in sorted(grouped):
        frame = panel.symbol_bars(symbol)
        if frame is None or frame.empty:
            continue
        wanted = {item.decision_date for item in grouped[symbol]}
        try:
            features = engineer.transform(frame)
        except Exception:  # noqa: BLE001 - 单票特征缺失不阻塞整体矩阵
            continue
        for timestamp, values in features.iterrows():
            day = timestamp.date() if hasattr(timestamp, "date") else timestamp
            if day not in wanted:
                continue
            row: dict[str, object] = {
                "decision_date": day.isoformat(),
                "symbol": symbol,
            }
            row.update(
                {
                    str(key): float(value)
                    for key, value in values.items()
                    if isinstance(value, (int, float)) and math.isfinite(float(value))
                }
            )
            rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["decision_date", "symbol"])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 多 Head 训练/推理
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeadFitSpec:
    """训练/推理窗口与超参（全部显式，便于复现）。"""

    seed: int = DEFAULT_SEED
    n_jobs: int = DEFAULT_N_JOBS
    train_mask_column: str = "is_train"
    predict_mask_column: str = "is_predict"
    calibration_mask_column: str = "is_calibration"
    min_train_rows: int = DEFAULT_MIN_TRAIN_ROWS
    min_class_balance: float = DEFAULT_MIN_CLASS_BALANCE

    def to_payload(self) -> dict[str, object]:
        return {
            "seed": int(self.seed),
            "n_jobs": int(self.n_jobs),
            "train_mask_column": self.train_mask_column,
            "predict_mask_column": self.predict_mask_column,
            "calibration_mask_column": self.calibration_mask_column,
            "min_train_rows": int(self.min_train_rows),
            "min_class_balance": float(self.min_class_balance),
            "deterministic": True,
        }


@dataclass
class HeadOutput:
    """单个 Head 的推理结果（带语义与校准声明）。"""

    name: str
    output_kind: str
    columns: tuple[str, ...]
    calibration: str
    calibrated_columns: tuple[str, ...] = ()
    matrix_fingerprint: str = ""
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "output_kind": self.output_kind,
            "columns": list(self.columns),
            "calibration": self.calibration,
            "calibrated_columns": list(self.calibrated_columns),
            "matrix_fingerprint": self.matrix_fingerprint,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass
class MultiHeadResult:
    predictions: pd.DataFrame
    heads: dict[str, HeadOutput]
    matrix: SharedFeatureMatrix
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "alpha_v2_multi_head_result.v1",
            "matrix": self.matrix.to_payload(),
            "heads": {name: output.to_payload() for name, output in self.heads.items()},
            "rows": int(len(self.predictions)),
            "diagnostics": dict(self.diagnostics),
        }


def fit_and_predict_heads(
    *,
    matrix: SharedFeatureMatrix,
    head_names: Sequence[str] = HEAD_NAMES,
    spec: HeadFitSpec | None = None,
    stats: BuildStats | None = None,
) -> MultiHeadResult:
    """在**同一份**矩阵上一次性训练并推理全部 Head。"""
    resolved = spec or HeadFitSpec()
    counters = stats if stats is not None else matrix.stats
    frame = matrix.frame
    predictions = frame[["decision_date", "symbol"]].copy()
    heads: dict[str, HeadOutput] = {}
    fingerprint = matrix.fingerprint

    train_mask = _mask(frame, resolved.train_mask_column)
    predict_mask = _mask(frame, resolved.predict_mask_column)
    calibration_mask = _mask(frame, resolved.calibration_mask_column)
    if not bool(predict_mask.any()):
        # 未显式给推理掩码时，默认对全部行推理（研究遍历场景）
        predict_mask = pd.Series(True, index=frame.index)
    if not bool(train_mask.any()):
        train_mask = pd.Series(False, index=frame.index)
    if bool(calibration_mask.any()):
        # 校准窗必须与训练窗互斥；同时它也必须被推理——否则校准拿不到分数，
        # 只能退化成"校准行数不足"的静默跳过。
        if bool((calibration_mask & train_mask).any()):
            raise ValueError("校准窗口与训练窗口重叠：isotonic 只能用训练之外的样本")
        predict_mask = predict_mask | calibration_mask

    diagnostics: dict[str, object] = {}
    for name in head_names:
        head = head_spec(name)
        if name == HEAD_ALPHA_RANK:
            output, columns, diag = _fit_alpha_rank(
                frame, matrix.feature_columns, train_mask, predict_mask, resolved
            )
        elif name == HEAD_EXPECTED_RETURN:
            output, columns, diag = _fit_return_head(
                frame, matrix.feature_columns, train_mask, predict_mask, resolved
            )
        elif name == HEAD_DIRECTION:
            output, columns, diag = _fit_direction_head(
                frame, matrix.feature_columns, train_mask, predict_mask, resolved
            )
        elif name == HEAD_RISK:
            output, columns, diag = _fit_risk_head(
                frame, matrix.feature_columns, train_mask, predict_mask, resolved
            )
        else:  # pragma: no cover - head_spec 已保证闭集
            raise KeyError(f"unsupported head: {name}")
        for column in columns:
            predictions[column] = output[column].to_numpy()
        heads[name] = HeadOutput(
            name=name,
            output_kind=head.output_kind,
            columns=tuple(columns),
            # 训练/推理阶段**恒为未校准**：Direction 的 isotonic 必须由
            # calibrate_direction 在独立窗口上单独完成并回填 calibrated_columns。
            calibration=CALIBRATION_NONE,
            calibrated_columns=(),
            matrix_fingerprint=fingerprint,
            diagnostics=diag,
        )
        diagnostics[name] = diag
    counters.prediction_calls += 1
    _assert_shared_matrix(heads)
    return MultiHeadResult(
        predictions=predictions, heads=heads, matrix=matrix, diagnostics=diagnostics
    )


def _assert_shared_matrix(heads: Mapping[str, HeadOutput]) -> None:
    fingerprints = {output.matrix_fingerprint for output in heads.values()}
    if len(fingerprints) > 1:
        raise AssertionError(
            "多 Head 使用了不同的 feature matrix（指纹不一致）："
            f"{sorted(fingerprints)} —— 这与「共享矩阵」契约矛盾"
        )


def _mask(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[column].fillna(False).astype(bool)


def _feature_matrix(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    matrix = frame.loc[:, list(columns)].apply(pd.to_numeric, errors="coerce")
    return matrix.to_numpy(dtype=np.float32, copy=True)


TASK_REGRESSION = "regression"
TASK_BINARY = "binary"
DEFAULT_NUM_BOOST_ROUND = 120


def _lightgbm_params(*, task: str, seed: int, n_jobs: int) -> dict[str, Any]:
    params: dict[str, Any] = {
        "objective": "regression" if task == TASK_REGRESSION else "binary",
        "verbose": -1,
        "deterministic": True,
        "force_row_wise": True,
        "num_leaves": 31,
        "learning_rate": 0.05,
        "min_data_in_leaf": 50,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "feature_fraction": 0.8,
        "lambda_l2": 1.0,
        "num_threads": max(1, int(n_jobs)),
        "seed": int(seed),
    }
    if task == TASK_BINARY:
        params["metric"] = "binary_logloss"
    return params


def _fit_model(
    *,
    features: np.ndarray,
    labels: np.ndarray,
    task: str,
    spec: HeadFitSpec,
) -> Any:
    """用 LightGBM **原生 API** 训练（不经过 sklearn 包装层）。

    ``lightgbm.sklearn`` 需要 scikit-learn；本仓库运行环境不带 sklearn，
    因此研究链路只依赖原生 Booster（更少的依赖面，也更容易保证确定性）。
    """
    import lightgbm as lgb

    params = _lightgbm_params(task=task, seed=spec.seed, n_jobs=spec.n_jobs)
    dataset = lgb.Dataset(features, label=labels, free_raw_data=True)
    return lgb.train(params, dataset, num_boost_round=DEFAULT_NUM_BOOST_ROUND)


def _predict_model(model: Any, features: np.ndarray) -> np.ndarray:
    values = np.asarray(model.predict(features), dtype=float)
    return values.reshape(-1) if values.ndim > 1 else values


# ---------------------------------------------------------------------------
# 公开出口：M3 冻结模型（shadow 工件）必须与 M2 研究链路共用同一套
# LightGBM 原生参数与确定性设置，而不是复制一份容易漂移的实现。
# ---------------------------------------------------------------------------


def lightgbm_train_params(
    *, task: str, seed: int = DEFAULT_SEED, n_jobs: int = DEFAULT_N_JOBS
) -> dict[str, Any]:
    """训练参数（公开别名；冻结模型与研究链路同一口径，M3 冻结清单引用它）。"""
    return _lightgbm_params(task=task, seed=seed, n_jobs=n_jobs)


def fit_lightgbm_native(
    *, features: np.ndarray, labels: np.ndarray, task: str, spec: HeadFitSpec
) -> Any:
    """原生 LightGBM 训练（公开别名；确定性设置固定）。"""
    return _fit_model(features=features, labels=labels, task=task, spec=spec)


def predict_model_scores(model: Any, features: np.ndarray) -> np.ndarray:
    """原生 Booster 推理（公开别名）。"""
    return _predict_model(model, features)


def _fit_alpha_rank(
    frame: pd.DataFrame,
    feature_columns: Sequence[str],
    train_mask: pd.Series,
    predict_mask: pd.Series,
    spec: HeadFitSpec,
) -> tuple[pd.DataFrame, list[str], dict[str, object]]:
    target = ALPHA_TARGET_TEMPLATE.format(h=PRIMARY_HORIZON)
    column = "alpha_rank_score"
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    empty = pd.DataFrame({column: result})
    diagnostics: dict[str, object] = {"target": target, "output_kind": OUTPUT_KIND_RANK_SCORE}
    labels = pd.to_numeric(frame.get(target), errors="coerce")
    train = train_mask & labels.notna()
    if int(train.sum()) < spec.min_train_rows:
        diagnostics["status"] = "insufficient_train_rows"
        diagnostics["train_rows"] = int(train.sum())
        return empty, [column], diagnostics
    features = _feature_matrix(frame, feature_columns)
    model = _fit_model(
        features=features[train.to_numpy()],
        labels=labels[train].to_numpy(dtype=float),
        task=TASK_REGRESSION,
        spec=spec,
    )
    raw = _predict_model(model, features[predict_mask.to_numpy()])
    predicted = pd.Series(np.asarray(raw, dtype=float), index=frame.index[predict_mask.to_numpy()])
    # 截面 rank 分位：Head A 的输出语义是"当天排多前"，不是绝对收益
    ranked = predicted.groupby(frame.loc[predicted.index, "decision_date"]).rank(pct=True)
    result.loc[ranked.index] = ranked
    diagnostics.update(
        {
            "status": "ok",
            "train_rows": int(train.sum()),
            "predicted_rows": int(len(ranked)),
            "backend": "lightgbm.train(native, objective=regression)",
        }
    )
    return pd.DataFrame({column: result}), [column], diagnostics


def _fit_return_head(
    frame: pd.DataFrame,
    feature_columns: Sequence[str],
    train_mask: pd.Series,
    predict_mask: pd.Series,
    spec: HeadFitSpec,
) -> tuple[pd.DataFrame, list[str], dict[str, object]]:
    features = _feature_matrix(frame, feature_columns)
    outputs: dict[str, pd.Series] = {}
    diagnostics: dict[str, object] = {"targets": {}, "output_kind": OUTPUT_KIND_EXPECTED_RETURN}
    for column in head_spec(HEAD_EXPECTED_RETURN).targets:
        labels = pd.to_numeric(frame.get(column), errors="coerce")
        train = train_mask & labels.notna()
        if int(train.sum()) < spec.min_train_rows:
            diagnostics["targets"][column] = {"status": "insufficient_train_rows"}  # type: ignore[index]
            continue
        model = _fit_model(
            features=features[train.to_numpy()],
            labels=labels[train].to_numpy(dtype=float),
            task=TASK_REGRESSION,
            spec=spec,
        )
        raw = _predict_model(model, features[predict_mask.to_numpy()])
        name = f"expected_{column}"
        series = pd.Series(np.nan, index=frame.index, dtype=float)
        series.loc[frame.index[predict_mask.to_numpy()]] = np.asarray(raw, dtype=float)
        outputs[name] = series
        diagnostics["targets"][column] = {"status": "ok", "train_rows": int(train.sum())}  # type: ignore[index]
    result = (
        pd.DataFrame(outputs, index=frame.index) if outputs else pd.DataFrame(index=frame.index)
    )
    diagnostics["status"] = "ok" if outputs else "no_targets_fitted"
    return result, list(outputs), diagnostics


def _fit_direction_head(
    frame: pd.DataFrame,
    feature_columns: Sequence[str],
    train_mask: pd.Series,
    predict_mask: pd.Series,
    spec: HeadFitSpec,
) -> tuple[pd.DataFrame, list[str], dict[str, object]]:
    features = _feature_matrix(frame, feature_columns)
    outputs: dict[str, pd.Series] = {}
    diagnostics: dict[str, object] = {
        "targets": {},
        "output_kind": OUTPUT_KIND_PROBABILITY,
        "calibration": CALIBRATION_NONE,
        "requires_oos_calibration": True,
    }
    for column in head_spec(HEAD_DIRECTION).targets:
        labels = pd.to_numeric(frame.get(column), errors="coerce")
        train = train_mask & labels.notna()
        if int(train.sum()) < spec.min_train_rows:
            diagnostics["targets"][column] = {"status": "insufficient_train_rows"}  # type: ignore[index]
            continue
        positives = float(labels[train].mean())
        if not (spec.min_class_balance <= positives <= 1.0 - spec.min_class_balance):
            diagnostics["targets"][column] = {  # type: ignore[index]
                "status": "degenerate_class_balance",
                "positive_rate": positives,
            }
            continue
        model = _fit_model(
            features=features[train.to_numpy()],
            labels=labels[train].to_numpy(dtype=float),
            task=TASK_BINARY,
            spec=spec,
        )
        probabilities = _predict_model(model, features[predict_mask.to_numpy()])
        name = f"p_{column}"
        series = pd.Series(np.nan, index=frame.index, dtype=float)
        series.loc[frame.index[predict_mask.to_numpy()]] = probabilities
        outputs[name] = series
        diagnostics["targets"][column] = {  # type: ignore[index]
            "status": "ok",
            "train_rows": int(train.sum()),
            "positive_rate": positives,
            "calibration": CALIBRATION_NONE,
        }
    result = (
        pd.DataFrame(outputs, index=frame.index) if outputs else pd.DataFrame(index=frame.index)
    )
    diagnostics["status"] = "ok" if outputs else "no_targets_fitted"
    return result, list(outputs), diagnostics


def _fit_risk_head(
    frame: pd.DataFrame,
    feature_columns: Sequence[str],
    train_mask: pd.Series,
    predict_mask: pd.Series,
    spec: HeadFitSpec,
) -> tuple[pd.DataFrame, list[str], dict[str, object]]:
    features = _feature_matrix(frame, feature_columns)
    outputs: dict[str, pd.Series] = {}
    diagnostics: dict[str, object] = {"targets": {}, "output_kind": OUTPUT_KIND_RISK_SCORE}
    for column in head_spec(HEAD_RISK).targets:
        labels = pd.to_numeric(frame.get(column), errors="coerce")
        train = train_mask & labels.notna()
        if int(train.sum()) < spec.min_train_rows:
            diagnostics["targets"][column] = {"status": "insufficient_train_rows"}  # type: ignore[index]
            continue
        if column.startswith("mae_le_5pct_"):
            positives = float(labels[train].mean())
            if not (spec.min_class_balance <= positives <= 1.0 - spec.min_class_balance):
                diagnostics["targets"][column] = {  # type: ignore[index]
                    "status": "degenerate_class_balance",
                    "positive_rate": positives,
                }
                continue
            model = _fit_model(
                features=features[train.to_numpy()],
                labels=labels[train].to_numpy(dtype=float),
                task=TASK_BINARY,
                spec=spec,
            )
            values = _predict_model(model, features[predict_mask.to_numpy()])
            name = f"p_{column}"
        else:
            model = _fit_model(
                features=features[train.to_numpy()],
                labels=labels[train].to_numpy(dtype=float),
                task=TASK_REGRESSION,
                spec=spec,
            )
            values = _predict_model(model, features[predict_mask.to_numpy()])
            name = f"expected_{column}"
        series = pd.Series(np.nan, index=frame.index, dtype=float)
        series.loc[frame.index[predict_mask.to_numpy()]] = values
        outputs[name] = series
        diagnostics["targets"][column] = {"status": "ok", "train_rows": int(train.sum())}  # type: ignore[index]
    result = (
        pd.DataFrame(outputs, index=frame.index) if outputs else pd.DataFrame(index=frame.index)
    )
    diagnostics["status"] = "ok" if outputs else "no_targets_fitted"
    return result, list(outputs), diagnostics


# ---------------------------------------------------------------------------
# Direction 的 OOS 校准
# ---------------------------------------------------------------------------


def calibrate_direction(
    predictions: pd.DataFrame,
    *,
    probability_columns: Sequence[str],
    calibration_mask: pd.Series,
    labels: pd.DataFrame,
    train_mask: pd.Series,
    output_columns: Mapping[str, str] | None = None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """用**独立窗口**做 isotonic 校准；校准窗与训练窗重叠即拒绝。

    这是"只有 Direction Head 才允许被叫上涨概率"的可执行前提：未校准的输出
    ``calibration=none``，展示层必须写"未校准/方向分"。
    """
    overlap = bool((calibration_mask & train_mask).any())
    if overlap:
        raise ValueError(
            "校准窗口与训练窗口重叠：isotonic 必须用训练之外的样本，"
            "否则「校准后概率」只是过拟合的另一种写法"
        )
    result = predictions.copy()
    rename = dict(output_columns or {})
    diagnostics: dict[str, object] = {"calibrated": [], "skipped": []}
    for column in probability_columns:
        if column not in result.columns:
            diagnostics["skipped"].append({"column": column, "reason": "column_not_present"})
            continue
        target = _direction_target_for_probability_column(column, labels)
        if target is None or target not in labels.columns:
            diagnostics["skipped"].append({"column": column, "reason": "target_unavailable"})
            continue
        usable = calibration_mask & labels[target].notna() & result[column].notna()
        if int(usable.sum()) < 50:
            diagnostics["skipped"].append({"column": column, "reason": "calibration_rows_too_few"})
            continue
        scores = pd.to_numeric(result.loc[usable, column], errors="coerce").to_numpy(dtype=float)
        outcomes = pd.to_numeric(labels.loc[usable, target], errors="coerce").to_numpy(dtype=float)
        finite = np.isfinite(scores) & np.isfinite(outcomes)
        if finite.sum() < 50:
            diagnostics["skipped"].append(
                {"column": column, "reason": "calibration_values_too_few"}
            )
            continue
        calibrator = IsotonicCalibrator()
        calibrator.fit(scores[finite], outcomes[finite])
        target_column = f"{column}_calibrated"
        values = pd.to_numeric(result[column], errors="coerce").to_numpy(dtype=float)
        applied = np.full(values.shape, np.nan, dtype=float)
        valid = np.isfinite(values)
        if bool(valid.any()):
            applied[valid] = np.asarray(calibrator.predict(values[valid]), dtype=float)
        result[target_column] = applied
        rename[column] = target_column
        diagnostics["calibrated"].append(
            {
                "column": column,
                "calibrated_column": target_column,
                "rows": int(finite.sum()),
                "method": CALIBRATION_ISOTONIC_OOS,
            }
        )
    diagnostics["calibration_rows"] = int(calibration_mask.sum())
    return result, diagnostics


def _direction_target_for_probability_column(column: str, labels: pd.DataFrame) -> str | None:
    """从 ``p_up_net_5d`` 反推目标列 ``up_net_5d``。"""
    if column.startswith("p_") and column[2:] in labels.columns:
        return column[2:]
    return None


# ---------------------------------------------------------------------------
# 展示语义
# ---------------------------------------------------------------------------


def head_display_semantics(head: HeadSpec, *, calibrated: bool) -> dict[str, object]:
    """Head 的展示口径（未校准的 Direction 禁止被叫"上涨概率"）。"""
    if head.name == HEAD_DIRECTION:
        if calibrated:
            return {
                "output_kind": OUTPUT_KIND_PROBABILITY,
                "calibration": CALIBRATION_ISOTONIC_OOS,
                "display_terms": ["正收益概率"],
                "may_call_probability": True,
            }
        return {
            "output_kind": OUTPUT_KIND_PROBABILITY,
            "calibration": CALIBRATION_NONE,
            "display_terms": ["方向分（未校准）"],
            "may_call_probability": False,
            "reason": "未完成 OOS 校准，不得称为上涨概率/正收益概率",
        }
    return {
        "output_kind": head.output_kind,
        "calibration": CALIBRATION_NONE,
        "display_terms": list(head.allowed_display_terms),
        "may_call_probability": False,
    }


def multi_head_payload(result: MultiHeadResult) -> dict[str, object]:
    payload = result.to_payload()
    payload["head_semantics"] = {
        name: head_display_semantics(head_spec(name), calibrated=bool(output.calibrated_columns))
        for name, output in result.heads.items()
    }
    return payload


__all__ = [
    "ALPHA_TARGET_TEMPLATE",
    "BuildStats",
    "CALIBRATION_ISOTONIC_OOS",
    "CALIBRATION_NONE",
    "DEFAULT_SEED",
    "HEAD_ALPHA_RANK",
    "HEAD_DIRECTION",
    "HEAD_EXPECTED_RETURN",
    "HEAD_NAMES",
    "HEAD_RISK",
    "HEAD_SPECS",
    "HeadFitSpec",
    "HeadOutput",
    "HeadSpec",
    "MAE_BREACH_COLUMN",
    "MAE_BREACH_THRESHOLD",
    "MATRIX_SCHEMA",
    "MultiHeadResult",
    "OUTPUT_KIND_EXPECTED_RETURN",
    "OUTPUT_KIND_PROBABILITY",
    "OUTPUT_KIND_RANK_SCORE",
    "OUTPUT_KIND_RISK_SCORE",
    "SharedFeatureMatrix",
    "TASK_BINARY",
    "TASK_REGRESSION",
    "build_head_targets",
    "build_shared_feature_matrix",
    "calibrate_direction",
    "fit_and_predict_heads",
    "fit_lightgbm_native",
    "head_display_semantics",
    "head_spec",
    "lightgbm_train_params",
    "matrix_fingerprint",
    "multi_head_payload",
    "predict_model_scores",
]
