"""Alpha V2 冻结 Shadow 模型工件（M3 §3/§4/§7 的模型身份锚点）。

M2 的多 Head 是**研究内存态**（每次跑批重新训练）。M3 的真实 OOS 要求一个
**冻结工件**：训练一次、落到磁盘、记录内容哈希，之后 Shadow 每一天的预测都
只能来自这个工件——"用今天的数据重新训一个模型去解释昨天的预测"在 M3 里
是不允许存在的路径。

设计约束：

1. **确定性**：与 M2 同一套 LightGBM 原生参数（``lightgbm_train_params``），
   同 seed 同数据产出同一份工件；
2. **身份可追**：manifest 记录 feature 列集合、训练/校准窗口、样本量、
   各 booster/校准器文件的 sha256，以及聚合的 ``artifact_hash``——它进
   validation freeze manifest 与 epoch 注册表；
3. **加载即校验**：特征列/哈希不符直接抛错（fail-closed），绝不"差不多的
   schema 也先跑起来"；
4. **OOS 校准纪律**：Direction Head 的 isotonic 校准器只用**校准窗**数据拟合，
   校准窗与训练窗重叠直接抛错（与 M2 ``calibrate_direction`` 同规则）；
   没有校准器的方向列在输出层**不写概率字段**，由快照层写 ``not_available``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.research.feature_audit import (
    assert_safe_feature_columns,
    is_outcome_leak_column,
)
from stock_analyzer.alpha_v2.research.multi_head import (
    MAE_BREACH_COLUMN,
    TASK_BINARY,
    TASK_REGRESSION,
    HeadFitSpec,
    fit_lightgbm_native,
    lightgbm_train_params,
    predict_model_scores,
)
from stock_analyzer.alpha_v2.research.outcomes import HORIZONS, SHORT_HORIZONS
from stock_analyzer.config_identity import stable_payload_hash
from stock_analyzer.models.calibration import IsotonicCalibrator

FROZEN_MODEL_SCHEMA = "alpha_v2_shadow_model.v1"
MODEL_MANIFEST_FILENAME = "model_manifest.json"
MODEL_INDEX_FILENAME = "model_index.json"

ALPHA_TARGET_5D = "alpha_target_5d"

DIRECTION_OUTPUTS: tuple[str, ...] = tuple(
    f"p_{kind}_{int(h)}d" for h in SHORT_HORIZONS for kind in ("up_net", "up_excess")
)

DEFAULT_MIN_CALIBRATION_ROWS = 50


class FrozenModelError(RuntimeError):
    """冻结模型工件的构造/加载/推理违例（身份不符、字段缺失、窗口重叠等）。"""


@dataclass(frozen=True, slots=True)
class FrozenTarget:
    """一个可持久化训练目标（target 列 -> 输出列 + 任务类型）。"""

    target: str
    output_column: str
    task: str

    def to_payload(self) -> dict[str, object]:
        return {
            "target": self.target,
            "output_column": self.output_column,
            "task": self.task,
        }


def frozen_targets() -> tuple[FrozenTarget, ...]:
    """冻结模型的全部训练目标（与 M2 四 Head 的输出列一一对应）。"""
    targets: list[FrozenTarget] = [
        FrozenTarget(ALPHA_TARGET_5D, "alpha_rank_score", TASK_REGRESSION)
    ]
    for horizon in HORIZONS:
        targets.append(
            FrozenTarget(
                f"net_return_{int(horizon)}d",
                f"expected_net_return_{int(horizon)}d",
                TASK_REGRESSION,
            )
        )
        targets.append(
            FrozenTarget(
                f"excess_return_{int(horizon)}d",
                f"expected_excess_return_{int(horizon)}d",
                TASK_REGRESSION,
            )
        )
    for horizon in SHORT_HORIZONS:
        targets.append(
            FrozenTarget(f"up_net_{int(horizon)}d", f"p_up_net_{int(horizon)}d", TASK_BINARY)
        )
        targets.append(
            FrozenTarget(f"up_excess_{int(horizon)}d", f"p_up_excess_{int(horizon)}d", TASK_BINARY)
        )
        targets.append(
            FrozenTarget(f"mae_{int(horizon)}d", f"expected_mae_{int(horizon)}d", TASK_REGRESSION)
        )
        targets.append(
            FrozenTarget(
                MAE_BREACH_COLUMN.format(h=int(horizon)),
                f"p_mae_le_5pct_{int(horizon)}d",
                TASK_BINARY,
            )
        )
    return tuple(targets)


@dataclass
class FrozenModel:
    """内存态的冻结模型（训练完成后 / 从磁盘加载后）。"""

    model_id: str
    feature_columns: tuple[str, ...]
    boosters: dict[str, Any] = field(default_factory=dict)  # target -> lgbm.Booster
    calibrators: dict[str, IsotonicCalibrator] = field(default_factory=dict)  # 输出列 -> 校准器
    manifest: dict[str, object] = field(default_factory=dict)
    diagnostics: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 训练
# ---------------------------------------------------------------------------


def fit_frozen_model(
    *,
    frame: pd.DataFrame,
    model_id: str,
    spec: HeadFitSpec | None = None,
    train_mask_column: str = "is_train",
    calibration_mask_column: str = "is_calibration",
    created_at: str | None = None,
    provenance: Mapping[str, object] | None = None,
    extra_identity: Mapping[str, object] | None = None,
    min_calibration_rows: int = DEFAULT_MIN_CALIBRATION_ROWS,
) -> FrozenModel:
    """在一份已经带 mask 列/目标列的矩阵上训练冻结模型。

    ``frame`` 必须包含：``decision_date`` / ``symbol`` / Base V2 特征列 /
    全部可训练目标列（缺失的目标如实记入 diagnostics，不编造）。
    """
    resolved = spec or HeadFitSpec()
    if "decision_date" not in frame.columns or "symbol" not in frame.columns:
        raise FrozenModelError("训练矩阵必须含 decision_date/symbol 身份列")
    train_mask = _mask(frame, train_mask_column)
    calibration_mask = _mask(frame, calibration_mask_column)
    if bool(train_mask.any()) is False:
        raise FrozenModelError(f"训练掩码 {train_mask_column} 为空：没有可训练样本")
    if bool((train_mask & calibration_mask).any()):
        raise FrozenModelError("训练窗与校准窗重叠：OOS 校准必须用训练之外的样本")

    feature_columns = tuple(
        sorted(
            column
            for column in frame.columns
            if column not in {"decision_date", "symbol", train_mask_column, calibration_mask_column}
            and column not in _ALL_TARGET_COLUMNS
            # outcome/label 族（S14 黑名单同款）与非特征元数据列不算特征
            and not is_outcome_leak_column(column)
            and not any(column.startswith(prefix) for prefix in _NON_FEATURE_PREFIXES)
            and column not in _NON_FEATURE_COLUMNS
        )
    )
    # Base V2 准入断言（复用 S14）：未证明 PIT 的列进冻结模型 = 直接失败。
    feature_columns = tuple(assert_safe_feature_columns(feature_columns))

    model = FrozenModel(
        model_id=str(model_id),
        feature_columns=feature_columns,
    )
    diagnostics: dict[str, object] = {
        "targets": {},
        "train_rows_total": int(train_mask.sum()),
        "calibration_rows_total": int(calibration_mask.sum()),
        "train_date_min": None,
        "train_date_max": None,
        "calibration_date_min": None,
        "calibration_date_max": None,
    }
    train_dates = frame.loc[train_mask, "decision_date"].astype(str)
    cal_dates = frame.loc[calibration_mask, "decision_date"].astype(str)
    if not train_dates.empty:
        diagnostics["train_date_min"] = str(train_dates.min())
        diagnostics["train_date_max"] = str(train_dates.max())
    if not cal_dates.empty:
        diagnostics["calibration_date_min"] = str(cal_dates.min())
        diagnostics["calibration_date_max"] = str(cal_dates.max())

    features_all = _feature_frame(frame, feature_columns)

    for target in frozen_targets():
        if target.target not in frame.columns:
            diagnostics["targets"][target.target] = {  # type: ignore[index]
                "task": target.task,
                "output_column": target.output_column,
                "status": "target_column_missing",
            }
            continue
        labels = pd.to_numeric(frame[target.target], errors="coerce")
        fit_mask = train_mask & labels.notna()
        status: dict[str, object] = {"task": target.task, "output_column": target.output_column}
        if int(fit_mask.sum()) < resolved.min_train_rows:
            status["status"] = "insufficient_train_rows"
            status["train_rows"] = int(fit_mask.sum())
            diagnostics["targets"][target.target] = status  # type: ignore[index]
            continue
        if target.task == TASK_BINARY:
            positive_rate = float(labels[fit_mask].mean())
            if not (
                resolved.min_class_balance <= positive_rate <= 1.0 - resolved.min_class_balance
            ):
                status["status"] = "degenerate_class_balance"
                status["positive_rate"] = positive_rate
                diagnostics["targets"][target.target] = status  # type: ignore[index]
                continue
            status["positive_rate"] = positive_rate
        booster = fit_lightgbm_native(
            features=features_all[fit_mask.to_numpy()],
            labels=labels[fit_mask].to_numpy(dtype=float),
            task=target.task,
            spec=resolved,
        )
        model.boosters[target.target] = booster
        status["status"] = "ok"
        status["train_rows"] = int(fit_mask.sum())
        diagnostics["targets"][target.target] = status  # type: ignore[index]

        # Direction Head 的 OOS 校准（只用校准窗；其它 head 无此动作）
        if target.output_column in DIRECTION_OUTPUTS and bool(calibration_mask.any()):
            calibrated = _fit_calibrator(
                model=model,
                target=target,
                frame=frame,
                features_all=features_all,
                calibration_mask=calibration_mask,
                min_rows=int(min_calibration_rows),
                diagnostics=diagnostics,
            )
            if calibrated is not None:
                status["calibration"] = calibrated

    model.diagnostics = diagnostics
    model.manifest = _build_manifest(
        model,
        spec=resolved,
        created_at=created_at or datetime.now().astimezone().isoformat(),
        provenance=provenance,
        extra_identity=extra_identity,
        diagnostics=diagnostics,
    )
    return model


def _fit_calibrator(
    *,
    model: FrozenModel,
    target: FrozenTarget,
    frame: pd.DataFrame,
    features_all: np.ndarray,
    calibration_mask: pd.Series,
    min_rows: int,
    diagnostics: dict[str, object],
) -> dict[str, object] | None:
    """在**校准窗**上拟合 isotonic；样本不足则不加校准器（如实记录）。"""
    labels = pd.to_numeric(frame.get(target.target), errors="coerce")
    usable = calibration_mask & labels.notna()
    if int(usable.sum()) < max(1, int(min_rows)):
        diagnostics.setdefault("calibration_skipped", []).append(  # type: ignore[attr-defined]
            {
                "target": target.target,
                "reason": "calibration_rows_too_few",
                "rows": int(usable.sum()),
            }
        )
        return None
    scores = predict_model_scores(model.boosters[target.target], features_all[usable.to_numpy()])
    outcomes = labels[usable].to_numpy(dtype=float)
    finite = np.isfinite(scores) & np.isfinite(outcomes)
    if int(finite.sum()) < max(1, int(min_rows)):
        diagnostics.setdefault("calibration_skipped", []).append(  # type: ignore[attr-defined]
            {"target": target.target, "reason": "calibration_values_too_few"}
        )
        return None
    calibrator = IsotonicCalibrator()
    calibrator.fit(scores[finite], outcomes[finite])
    model.calibrators[target.output_column] = calibrator
    return {
        "method": "isotonic_oos",
        "calibration_rows": int(finite.sum()),
        "trained_on_training_window": False,
    }


# ---------------------------------------------------------------------------
# 推理
# ---------------------------------------------------------------------------


def predict_frozen_model_matrix(model: FrozenModel, frame: pd.DataFrame) -> pd.DataFrame:
    """对决策帧推理（行需含 decision_date/symbol 与全部冻结特征列）。

    缺特征列直接抛错——缺失列用 0/NaN 填充是把"我们不知道"伪装成"模型判断"。
    Direction 列只在存在校准器时同时给 ``*_calibrated``。
    """
    missing = [column for column in model.feature_columns if column not in frame.columns]
    if missing:
        raise FrozenModelError(f"推理帧缺冻结特征列: {missing[:20]}")
    if "decision_date" not in frame.columns or "symbol" not in frame.columns:
        raise FrozenModelError("推理帧必须含 decision_date/symbol")
    features = _feature_frame(frame, model.feature_columns)

    out = frame[["decision_date", "symbol"]].copy()
    for target in frozen_targets():
        booster = model.boosters.get(target.target)
        if booster is None:
            continue
        raw = predict_model_scores(booster, features)
        if target.target == ALPHA_TARGET_5D:
            # Head A：原样给出横截面 rank 分位（与 M2 的 alpha_rank_score 语义一致）。
            series = pd.Series(raw, index=frame.index, dtype=float)
            ranked = series.groupby(frame["decision_date"].astype(str)).rank(pct=True)
            out["alpha_rank_score"] = ranked
            out["alpha_rank_raw"] = raw
            continue
        out[target.output_column] = raw
        calibrator = model.calibrators.get(target.output_column)
        if calibrator is not None:
            finite = np.isfinite(raw)
            calibrated = np.full(raw.shape, np.nan, dtype=float)
            if bool(finite.any()):
                calibrated[finite] = np.asarray(calibrator.predict(raw[finite]), dtype=float)
            out[f"{target.output_column}_calibrated"] = calibrated
    # 校准状态自述：让下游明确知道"哪些方向列能叫概率"
    out["direction_calibration"] = (
        "isotonic_oos" if "p_up_net_5d" in model.calibrators else "none"
    )
    return out


def frozen_model_identity_payload(model_dir: str | Path) -> dict[str, object]:
    """从磁盘 manifest 取身份块（供 freeze manifest 的 model 段）。

    修复轮起同时带 ``feature_columns`` / ``feature_schema_hash`` —— 冻结清单的
    feature_schema 默认就是从工件派生，避免"清单有一个 schema、模型跑的是另一个"。
    """
    manifest = _read_manifest(model_dir)
    return {
        "model_id": manifest.get("model_id", ""),
        "artifact_hash": manifest.get("artifact_hash", ""),
        "artifact_created_at": manifest.get("created_at", ""),
        "artifact_path": str(Path(model_dir)),
        "heads": manifest.get("heads", []),
        "calibration": manifest.get("calibration", {}),
        "status": "frozen" if manifest.get("artifact_hash") else "pending_freeze",
        "provenance": manifest.get("provenance", {}),
        "feature_columns": list(manifest.get("feature_columns", []) or []),
        "feature_schema_hash": manifest.get("feature_schema_hash", ""),
        "training": dict(manifest.get("training", {}) or {}),
    }


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def persist_frozen_model(model: FrozenModel, root: str | Path) -> Path:
    """落盘 ``<root>/model/<model_id>/``；booster/calibrator 单独成文件并计哈希。"""
    model_dir = Path(root) / "model" / model.model_id
    model_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for target in sorted(model.boosters):
        path = model_dir / f"booster__{_safe_name(target)}.txt"
        booster = model.boosters[target]
        booster.save_model(str(path))
        files[path.name] = _file_sha256(path)
    for column in sorted(model.calibrators):
        path = model_dir / f"calibrator__{_safe_name(column)}.json"
        calibrator = model.calibrators[column]
        write_json_atomic(
            path,
            {
                "schema": "alpha_v2_isotonic_calibrator.v1",
                "column": column,
                "method": "isotonic_oos",
                **calibrator.to_dict(),
            },
        )
        files[path.name] = _file_sha256(path)

    manifest = dict(model.manifest)
    manifest["files"] = files
    manifest["artifact_hash"] = _artifact_hash(model, files)
    manifest_path = model_dir / MODEL_MANIFEST_FILENAME
    write_json_atomic(manifest_path, manifest)
    model.manifest = manifest

    # 让"当前冻结模型"有一个稳定指针（内容仍是 manifest 哈希锚定，不靠名字）。
    index_path = Path(root) / "model" / MODEL_INDEX_FILENAME
    write_json_atomic(
        index_path,
        {
            "schema": "alpha_v2_shadow_model_index.v1",
            "model_id": model.model_id,
            "model_dir": str(model_dir),
            "artifact_hash": manifest["artifact_hash"],
            "created_at": manifest.get("created_at", ""),
            "note": "指针只是便利；身份以 artifact_hash 为准",
        },
    )
    return model_dir


def load_frozen_model(
    model_dir: str | Path, *, expected_artifact_hash: str | None = None
) -> FrozenModel:
    """按 manifest 校验后加载；任一文件哈希不符 / 身份不符直接抛错。"""
    model_path = Path(model_dir)
    manifest = _read_manifest(model_path)
    files = manifest.get("files")
    if not isinstance(files, Mapping) or not files:
        raise FrozenModelError(f"冻结模型 manifest 缺 files 段: {model_path}")
    for name, recorded_hash in sorted(files.items()):
        path = model_path / str(name)
        if not path.exists():
            raise FrozenModelError(f"冻结模型缺文件: {path}")
        actual = _file_sha256(path)
        if actual != recorded_hash:
            raise FrozenModelError(
                f"冻结模型文件哈希不符: {name}"
                f"（manifest {str(recorded_hash)[:12]} != 实际 {actual[:12]}）"
            )
    recomputed = _artifact_hash_from_manifest(manifest)
    recorded = str(manifest.get("artifact_hash", "") or "")
    if recomputed != recorded:
        raise FrozenModelError(
            f"冻结模型 artifact_hash 与内容不符（{recorded[:12]} != {recomputed[:12]}）"
        )
    if expected_artifact_hash is not None and recorded != str(expected_artifact_hash):
        raise FrozenModelError(
            f"期望的冻结模型哈希为 {str(expected_artifact_hash)[:12]}…，实际为 {recorded[:12]}…"
        )

    import lightgbm as lgb

    boosters: dict[str, Any] = {}
    calibrators: dict[str, IsotonicCalibrator] = {}
    for name in files:
        path = model_path / str(name)
        if name.startswith("booster__"):
            target = _unsafe_name(name.removeprefix("booster__").removesuffix(".txt"))
            boosters[target] = lgb.Booster(model_file=str(path))
        elif name.startswith("calibrator__"):
            column = _unsafe_name(name.removeprefix("calibrator__").removesuffix(".json"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            calibrators[column] = IsotonicCalibrator.from_dict(payload)
    feature_columns = tuple(str(col) for col in manifest.get("feature_columns", []))
    if not feature_columns:
        raise FrozenModelError("冻结模型 manifest 无 feature_columns")
    return FrozenModel(
        model_id=str(manifest.get("model_id", "")),
        feature_columns=feature_columns,
        boosters=boosters,
        calibrators=calibrators,
        manifest=dict(manifest),
    )


def _read_manifest(model_dir: str | Path) -> dict[str, object]:
    path = Path(model_dir) / MODEL_MANIFEST_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FrozenModelError(f"冻结模型 manifest 不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FrozenModelError(f"冻结模型 manifest 无法解析: {path}") from exc
    if payload.get("schema") != FROZEN_MODEL_SCHEMA:
        raise FrozenModelError(
            f"冻结模型 manifest schema 应为 {FROZEN_MODEL_SCHEMA}，实际 {payload.get('schema')!r}"
        )
    return payload


# ---------------------------------------------------------------------------
# Manifest / 哈希
# ---------------------------------------------------------------------------


def _build_manifest(
    model: FrozenModel,
    *,
    spec: HeadFitSpec,
    created_at: str,
    provenance: Mapping[str, object] | None,
    extra_identity: Mapping[str, object] | None,
    diagnostics: Mapping[str, object],
) -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": FROZEN_MODEL_SCHEMA,
        "model_id": model.model_id,
        "created_at": str(created_at),
        "feature_columns": list(model.feature_columns),
        "feature_schema_hash": stable_payload_hash(
            {"feature_columns": sorted(model.feature_columns)}
        ),
        "params": lightgbm_train_params(task=TASK_REGRESSION, seed=spec.seed, n_jobs=spec.n_jobs),
        "head_fit_spec": spec.to_payload(),
        "targets": [target.to_payload() for target in frozen_targets()],
        "calibration": {
            "method": "isotonic_oos",
            "calibrated_direction_columns": sorted(model.calibrators),
            "uncalibrated_direction_columns": sorted(
                set(DIRECTION_OUTPUTS) - set(model.calibrators)
            ),
        },
        "heads": ["alpha_rank", "expected_return", "direction", "risk"],
        "provenance": dict(provenance or {}),
        "training": {
            key: value
            for key, value in diagnostics.items()
            if key in {"train_rows_total", "calibration_rows_total", "train_date_min",
                       "train_date_max", "calibration_date_min", "calibration_date_max"}
        },
        "target_diagnostics": diagnostics.get("targets", {}),
        "calibration_skipped": diagnostics.get("calibration_skipped", []),
    }
    if extra_identity:
        identity.update(dict(extra_identity))
    return identity


def _artifact_hash(model: FrozenModel, files: Mapping[str, str]) -> str:
    body = {
        "model_id": model.model_id,
        "feature_columns": list(model.feature_columns),
        "params": model.manifest.get("params", {}),
        "targets": model.manifest.get("targets", []),
        "calibration": model.manifest.get("calibration", {}),
        "files": dict(sorted(files.items())),
    }
    return stable_payload_hash(body)


def _artifact_hash_from_manifest(manifest: Mapping[str, object]) -> str:
    files = manifest.get("files")
    body = {
        "model_id": manifest.get("model_id", ""),
        "feature_columns": list(manifest.get("feature_columns", []) or []),
        "params": manifest.get("params", {}),
        "targets": manifest.get("targets", []),
        "calibration": manifest.get("calibration", {}),
        "files": dict(sorted(dict(files).items())) if isinstance(files, Mapping) else {},
    }
    return stable_payload_hash(body)


def _safe_name(column: str) -> str:
    return column.replace("/", "_")


def _unsafe_name(name: str) -> str:
    return name


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[column].fillna(False).astype(bool)


def _feature_frame(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    matrix = frame.loc[:, list(columns)].apply(pd.to_numeric, errors="coerce")
    return matrix.to_numpy(dtype=np.float32, copy=True)


# 目标列全集（含 alpha target 的派生列）；fit 时须把它们排除在特征列外。
def _compute_target_columns() -> set[str]:
    columns = {target.target for target in frozen_targets()}
    columns.update(target.output_column for target in frozen_targets())
    columns.add(ALPHA_TARGET_5D)
    return columns


_ALL_TARGET_COLUMNS = _compute_target_columns()

# 元数据/基准列：不是特征（即便出现在矩阵里也不能进训练特征集）。
_NON_FEATURE_PREFIXES: tuple[str, ...] = (
    "benchmark_return_",
    "benchmark_",
    "expected_",  # 模型输出回流进矩阵时必须被挡在特征外
    "p_up_",
    "p_mae_",
    "mae_le_5pct_",  # 非 SHORT horizon 的 mae_le 系列不是 frozen_targets，不得漏进特征
    "style_",  # S12 风格对照维度列（style_board/style_float_cap_log/...）不进特征
)
_NON_FEATURE_COLUMNS: frozenset[str] = frozenset(
    {
        "executable",
        "no_fill_reason",
        "benchmark_name",
        "quality_pool_source",
        "signal_date",
        "round_trip_cost_rate",
        "buy_cost_rate",
        "sell_cost_rate",
        "limit_source",
        "corporate_action_suspected",
        "corporate_action_flag_source",
        "price_mode",
        "price_mode_certified",
        "execution_uncertain",
        "alpha_rank_score",
        "alpha_rank_raw",
        "direction_calibration",
        "style_peer_count",
        "style_distance_mean",
        "style_fallback",
        "listing_days_lower_bound",
    }
)

__all__ = [
    "ALPHA_TARGET_5D",
    "DEFAULT_MIN_CALIBRATION_ROWS",
    "DIRECTION_OUTPUTS",
    "FROZEN_MODEL_SCHEMA",
    "FrozenModel",
    "FrozenModelError",
    "FrozenTarget",
    "MODEL_INDEX_FILENAME",
    "MODEL_MANIFEST_FILENAME",
    "fit_frozen_model",
    "frozen_model_identity_payload",
    "frozen_targets",
    "load_frozen_model",
    "persist_frozen_model",
    "predict_frozen_model_matrix",
]
