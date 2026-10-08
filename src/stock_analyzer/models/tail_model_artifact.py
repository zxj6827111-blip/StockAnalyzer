"""净盈利尾盘模型的**可加载工件**：训练产物 → JSON 工件 → 打分器，一条实现。

改进计划 §3.3 要求线上、训练标签与历史验证共用同一套判定；§4 要求"相同输入下，线上
与历史路径必须产生一致的筛选和交易决策"。这两条最怕的就是"训练里算一遍概率、
推理里再算一遍"。所以本模块只提供一种拿分方式：
``load_tail_model_predictor()`` 重建的是**训练时那个类 itself**
(``LogisticProbModel`` + ``IsotonicCalibrator``)，不是它对 JSON 的复刻。

三件事在加载时就绪，缺一就抛 ``TailArtifactError``（fail-closed，不降级打分）：

1. **完整性**：工件摘要按落盘内容重算比对 —— 改一个权重就是另一个模型；
2. **口径**：工件里的契约摘要必须等于在服契约，标签口径必须是 v4 净盈利标签，
   概率字段必须是 ``p_net_profit_5d_tail``（否则就是把另一套 TP/SL 的分数当本策略用）；
3. **特征**：缺哪个特征就报哪个，**不填零**。缺失填零会让"没数据"长得像"数据为 0"，
   而下游把它当成真实信息排序。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    TrendStrategyContract,
)
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.fallback import LogisticProbModel
from stock_analyzer.models.tail_net_profit_trainer import KIND_LIGHTGBM, KIND_LOGISTIC

TAIL_ARTIFACT_SCHEMA = "tail_model_artifact.v1"
#: 第一轮只有这两种形态可序列化：逻辑回归基线 + 既有 LightGBM 参数。
SERIALIZABLE_KINDS = frozenset({KIND_LOGISTIC, KIND_LIGHTGBM})


class TailArtifactError(ValueError):
    """工件不完整、口径不对或特征缺失 —— 不能拿它打分。"""


def _stable_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _as_float_list(values: Any, *, name: str) -> list[float]:
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise TailArtifactError(f"{name} 不是数值序列: {exc}") from exc
    if not numbers:
        raise TailArtifactError(f"{name} 为空")
    return numbers


def serialize_tail_artifact(
    payload: Mapping[str, Any],
    *,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> dict[str, Any]:
    """把训练产物序列化成 JSON 可落盘的工件（含自摘要）。"""
    kind = str(payload.get("kind") or "").strip()
    if kind not in SERIALIZABLE_KINDS:
        raise TailArtifactError(
            f"第一轮只能序列化 {sorted(SERIALIZABLE_KINDS)}，收到 kind={kind!r}"
        )
    model = payload.get("model")
    calibrator = payload.get("calibrator")
    feature_names = [str(name) for name in (payload.get("feature_names") or ())]
    if not feature_names:
        raise TailArtifactError("工件必须记录特征顺序：推理按这个名字表取数")

    params: dict[str, Any] = {"names": feature_names}
    if kind == KIND_LIGHTGBM:
        dump = getattr(model, "model_to_string", None)
        if not callable(dump):
            raise TailArtifactError(
                "LightGBM 工件需要原生 booster 对象；不序列化就无可加载模型"
            )
        params["booster_model_string"] = str(dump())
    else:
        weights = getattr(model, "weights", None)
        scaler = getattr(model, "scaler", None)
        if weights is None or scaler is None:
            raise TailArtifactError(
                "线性模型缺少权重或 scaler：scaler 缺失的工件推理会被拒绝，"
                "不能存成'看起来能加载'"
            )
        params["weights"] = _as_float_list(weights, name="weights")
        params["bias"] = float(getattr(model, "bias", 0.0) or 0.0)
        params["scaler"] = {
            "mean": _as_float_list(scaler["mean"], name="scaler.mean"),
            "scale": _as_float_list(scaler["scale"], name="scaler.scale"),
        }

    x_right = getattr(calibrator, "_x_right", None)
    y_hat = getattr(calibrator, "_y_hat", None)
    if x_right is None or y_hat is None:
        raise TailArtifactError("校准器未拟合：没有独立校准段就没有校准概率可言")

    body: dict[str, Any] = {
        "schema": TAIL_ARTIFACT_SCHEMA,
        "identity": {
            "model_id": str(payload.get("model_id") or ""),
            "kind": kind,
            "training_code_commit": str(payload.get("training_commit") or ""),
            "feature_compute_version": str(payload.get("feature_compute_version") or ""),
            "label_policy_id": str(payload.get("label_policy_id") or ""),
            "probability_field": str(payload.get("probability_field") or ""),
            # 训练器自己算的摘要：与文件级摘要互相独立，两个都对得上才叫同一份东西。
            "training_artifact_digest": str(payload.get("artifact_digest") or ""),
        },
        "contract": {
            "contract_version": contract.contract_version,
            "contract_digest": contract.digest(),
            "holding_days": int(contract.holding_days),
            "take_profit_pct": float(contract.take_profit_pct),
            "stop_loss_pct": float(contract.stop_loss_pct),
            "execution_price_basis": contract.execution_price_basis,
        },
        "model_params": params,
        "calibration": {
            "x_right": _as_float_list(x_right, name="calibration.x_right"),
            "y_hat": _as_float_list(y_hat, name="calibration.y_hat"),
        },
        "split": dict(payload.get("split") or {}),
        "metrics": dict(payload.get("metrics") or {}),
    }
    body["artifact_digest"] = _stable_digest(body)
    return body


def write_tail_artifact(path: str | Path, payload: Mapping[str, Any]) -> Path:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temp = resolved.with_name(f".{resolved.name}.tmp")
    try:
        temp.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        Path(temp).replace(resolved)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return resolved


def artifact_field(artifact: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    """工件字段只经这一个出口读（清单是分层的，顶层读会永远读空）。

    两个 CLI 都读同一份工件；如果各自写一套取值顺序，就会分叉成"三份副本"那类缺陷。
    """
    for name in names:
        for section in ("identity", "contract", "model_params", "metrics", "split"):
            block = artifact.get(section)
            if isinstance(block, Mapping) and str(block.get(name, "") or "").strip():
                return block[name]
        if str(artifact.get(name, "") or "").strip():
            return artifact[name]
    return default


def artifact_identity_view(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """把分层工件摊平成"声明事实"视图：清单构造与冻结 CLI 共用这一份取值顺序。"""
    params = artifact.get("model_params")
    return {
        "model_id": artifact_field(artifact, "model_id"),
        "kind": artifact_field(artifact, "kind"),
        "label_policy_id": artifact_field(artifact, "label_policy_id"),
        "contract_digest": artifact_field(artifact, "contract_digest"),
        "probability_field": artifact_field(artifact, "probability_field"),
        "feature_compute_version": artifact_field(artifact, "feature_compute_version"),
        "training_commit": artifact_field(artifact, "training_code_commit", "training_commit"),
        "training_manifest_id": artifact_field(
            artifact, "training_manifest_id", "dataset_manifest_id", "manifest_id"
        ),
        "artifact_digest": artifact_field(artifact, "training_artifact_digest"),
        "feature_names": (
            list(params.get("names") or ()) if isinstance(params, Mapping) and params
            else list(artifact.get("feature_names") or ())
        ),
        "split": dict(artifact.get("split") or {}),
        "metrics": dict(artifact.get("metrics") or {}),
    }


class TailModelPredictor:
    """加载后的尾盘打分器：一条路径，缺特征就报错。"""

    def __init__(
        self,
        *,
        raw_predict: Callable[[np.ndarray], Any],
        calibrator: IsotonicCalibrator,
        feature_names: tuple[str, ...],
        identity: Mapping[str, Any],
        contract_block: Mapping[str, Any],
        artifact_digest: str,
    ) -> None:
        self._raw_predict = raw_predict
        self._calibrator = calibrator
        self._feature_names = feature_names
        self._identity = dict(identity)
        self._contract = dict(contract_block)
        self._digest = artifact_digest

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._feature_names

    @property
    def identity(self) -> dict[str, Any]:
        return dict(self._identity)

    @property
    def contract_digest(self) -> str:
        return str(self._contract.get("contract_digest") or "")

    @property
    def artifact_digest(self) -> str:
        return self._digest

    def vector_for(self, features: Mapping[str, Any]) -> list[float]:
        values: list[float] = []
        for name in self._feature_names:
            if name not in features:
                # 填零会把"没有这个特征"伪装成"该特征等于 0"，而排序照它排。
                raise TailArtifactError(f"feature_missing:{name}")
            value = features[name]
            if isinstance(value, str) or value is None:
                raise TailArtifactError(f"feature_not_numeric:{name}")
            number = float(value)
            if not math.isfinite(number):
                raise TailArtifactError(f"feature_not_finite:{name}")
            values.append(number)
        return values

    def probability(self, features: Mapping[str, Any]) -> float:
        vector = np.asarray([self.vector_for(features)], dtype=float)
        raw = float(np.asarray(self._raw_predict(vector)).reshape(-1)[0])
        calibrated = float(np.asarray(self._calibrator.predict(np.asarray([raw]))).reshape(-1)[0])
        return calibrated

    def probabilities(
        self, rows: Iterable[tuple[str, Mapping[str, Any]]]
    ) -> dict[str, float]:
        """批量打分：字段名就是下游要的 ``p_net_profit_5d_tail`` 的值。"""
        return {str(symbol): self.probability(features) for symbol, features in rows}


def _rebuild_logistic(params: Mapping[str, Any]) -> LogisticProbModel:
    model = LogisticProbModel()
    # weights / bias / scaler 在训练类里是 init=False 的字段：直接赋值才能复用
    # predict() 里那条 scaler 变换，而不是在工件侧再实现一遍坐标系。
    model.weights = np.asarray(params["weights"], dtype=float)
    model.bias = float(params["bias"])
    model.scaler = {
        "mean": np.asarray(params["scaler"]["mean"], dtype=float),
        "scale": np.asarray(params["scaler"]["scale"], dtype=float),
    }
    return model


def load_tail_model_predictor(
    payload: Mapping[str, Any] | str | Path,
    *,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> TailModelPredictor:
    """加载并核验工件；任何一项对不上都抛错，不返回"大概能用"的打分器。"""
    if isinstance(payload, (str, Path)):
        raw = Path(payload)
        if not raw.is_file():
            raise TailArtifactError(f"artifact_missing:{raw}")
        try:
            loaded = json.loads(raw.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TailArtifactError(f"artifact_unreadable:{type(exc).__name__}") from exc
    else:
        loaded = dict(payload)
    if not isinstance(loaded, Mapping):
        raise TailArtifactError("artifact_malformed:not_an_object")
    body = {key: value for key, value in loaded.items() if key != "artifact_digest"}
    recorded = str(loaded.get("artifact_digest") or "")
    if _stable_digest(body) != recorded:
        raise TailArtifactError("artifact_digest_mismatch:内容被改过，这是另一个模型")
    if str(loaded.get("schema") or "") != TAIL_ARTIFACT_SCHEMA:
        raise TailArtifactError(f"artifact_schema_mismatch:{loaded.get('schema')!r}")

    identity = dict(loaded.get("identity") or {})
    contract_block = dict(loaded.get("contract") or ())
    if str(contract_block.get("contract_digest") or "") != contract.digest():
        raise TailArtifactError(
            "artifact_contract_mismatch:"
            f"{contract_block.get('contract_digest')!r} != 在服 {contract.digest()!r}"
        )
    label_policy_id = str(identity.get("label_policy_id") or "")
    if not label_policy_id.startswith("label_policy_v4_"):
        raise TailArtifactError(f"artifact_label_policy_not_v4:{label_policy_id or 'none'}")
    if str(identity.get("probability_field") or "") != NET_PROFIT_PROBABILITY_FIELD:
        raise TailArtifactError(
            f"artifact_probability_field_mismatch:{identity.get('probability_field')!r}"
        )
    if not str(identity.get("training_code_commit") or "").strip():
        raise TailArtifactError("artifact_training_commit_missing")

    params = dict(loaded.get("model_params") or ())
    names = tuple(str(name) for name in (params.get("names") or ()))
    if not names:
        raise TailArtifactError("artifact_feature_names_missing")
    kind = str(identity.get("kind") or "")
    if kind == KIND_LIGHTGBM:
        booster_string = str(params.get("booster_model_string") or "")
        if not booster_string:
            raise TailArtifactError("artifact_booster_string_missing")
        booster = _load_lightgbm_booster(booster_string)
        raw_predict = lambda matrix: booster.predict(matrix)  # noqa: E731
    else:
        model = _rebuild_logistic(params)
        raw_predict = lambda matrix: model.predict_proba(matrix)  # noqa: E731

    calibration = dict(loaded.get("calibration") or {})
    try:
        calibrator = IsotonicCalibrator.from_state(
            calibration.get("x_right") or (), calibration.get("y_hat") or ()
        )
    except ValueError as exc:
        raise TailArtifactError(f"artifact_calibration_state_invalid:{exc}") from exc

    return TailModelPredictor(
        raw_predict=raw_predict, calibrator=calibrator, feature_names=names,
        identity=identity, contract_block=contract_block, artifact_digest=recorded,
    )


def _load_lightgbm_booster(model_string: str) -> Any:
    """LightGBM 路径只在原生库可用时开启；不可用就停，不换模型（§3.3）。"""
    try:
        import lightgbm as lgb
    except Exception as exc:  # noqa: BLE001 - 环境缺原生库是事实，不是可以绕过的错误
        raise TailArtifactError(
            f"lightgbm_unavailable:{type(exc).__name__}: 第一轮不得用其他模型顶替"
        ) from exc
    booster = lgb.Booster(model_str=model_string)
    return booster


__all__ = [
    "SERIALIZABLE_KINDS",
    "TAIL_ARTIFACT_SCHEMA",
    "TailArtifactError",
    "TailModelPredictor",
    "artifact_field",
    "artifact_identity_view",
    "load_tail_model_predictor",
    "serialize_tail_artifact",
    "write_tail_artifact",
]
