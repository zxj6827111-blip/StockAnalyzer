"""Inference predictor based on trained artifact."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from stock_analyzer.models.adapters import LightGBMAdapter, XGBoostAdapter
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.bundle import compute_artifact_identity_hash
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.identity import content_hash_matches_stamp
from stock_analyzer.models.output_semantics import (
    output_semantics_for_basis,
    semantics_supports_event_label_metrics,
)


def _resolve_artifact_semantics(basis: str) -> tuple[str | None, str]:
    """工件 label 契约 → 输出语义；未登记口径**不抛异常**而是回传错误串。

    推理路径必须保持可用（未知/未登记契约退化为 ``semantics=None`` 并留痕），
    而登记/审计路径用 ``output_semantics.describe_output_semantics`` 做 fail-closed。
    """

    try:
        return output_semantics_for_basis(basis), ""
    except ValueError as exc:
        return None, str(exc)


SCORE_SOURCES = ("raw", "calibrated")

# 打分分量的取键：分量名沿用 ScoreEngine 权重表的 ``lgbm/xgb/meta``，但 ``raw``
# 口径下 ``meta`` 取 ``raw_blend``（校准前的加权混合），**不是**校准后的 meta。
_SCORE_SOURCE_KEYS: dict[str, dict[str, str]] = {
    "calibrated": {"lgbm": "lgbm", "xgb": "xgb", "meta": "meta"},
    "raw": {"lgbm": "raw_lgbm", "xgb": "raw_xgb", "meta": "raw_blend"},
}


def resolve_score_source(value: object) -> str:
    """规范化推理分数口径名；未知取值 fail-closed，不静默回退 ``calibrated``。

    静默回退会让"已切 raw"看起来生效而实际没切——与 2026-09-14 排障里最贵的
    那类假象同型（配置写错却被兜底掩盖）。
    """
    key = str(value if value is not None else "raw").strip().lower()
    if key not in SCORE_SOURCES:
        raise ValueError(
            f"unknown inference score source: {value!r}; expected one of {SCORE_SOURCES}"
        )
    return key


def score_source_keys(source: object) -> dict[str, str]:
    """口径 → 分量名到原始分数键的映射（批量路径按位置索引时用）。"""
    return dict(_SCORE_SOURCE_KEYS[resolve_score_source(source)])


def scoring_components(predictions: Mapping[str, float], *, source: object) -> dict[str, float]:
    """把一次推理的分数收敛成打分用的三个分量（键名固定 ``lgbm/xgb/meta``）。

    缺目标族的键时 fail-closed：宁可显式失败，也不用校准分数冒充 raw——
    那会让 A/B 结论建立在混口径的数字上。
    """
    keys = score_source_keys(source)
    missing = [name for name in keys.values() if name not in predictions]
    if missing:
        raise ValueError(f"missing score keys for source={source!r}: {missing}")
    return {component: float(predictions[key]) for component, key in keys.items()}


@dataclass(slots=True)
class SignalPredictor:
    """从工程特征输出 lgbm/xgb/meta 分数。

    **输出语义（C2）**：返回值是"分数"，其含义由工件的 label 契约决定，不是
    天然的概率——``event_probability``（soup 的 TP/SL 路径事件）可与 0/1 事件
    标签算 Brier/logloss；``rank_quantile``（return_rank v3）是同日横截面分位
    归属，**中间 40% 被剔除**，`0.5` 是"上尾 vs 下尾"而非"涨 vs 跌"，不得当
    全市场上涨概率用（见 ``models/output_semantics.py``）。
    """

    feature_columns: list[str]
    lgbm: LightGBMAdapter
    xgb: XGBoostAdapter
    lgbm_calibrator: IsotonicCalibrator
    xgb_calibrator: IsotonicCalibrator
    meta_weights: dict[str, float] = field(default_factory=lambda: {"lgbm": 0.5, "xgb": 0.5})
    artifact_metadata: dict[str, object] = field(default_factory=dict)
    label_policy_id: str = ""
    # S01 身份事实：这些字段直接来自加载的 ModelArtifact，是"报告必须报告谁"的唯一真相源。
    # 之所以在 predictor 上再存一份（而不是每次回读文件头），是为了让身份报告不依赖
    # 磁盘 IO——工件在加载后被换掉时，报告仍描述**本进程真正加载的那一份**，
    # 而不一致会由 artifact_content_hash 与盖章哈希的比对暴露出来。
    artifact_created_at: str = ""
    feature_schema_id: str = ""
    feature_schema_hash: str = ""
    label_policy_hash: str = ""
    dataset_manifest_id: str = ""
    artifact_path_requested: str = ""

    @property
    def output_semantics(self) -> str | None:
        """本工件输出语义（未登记契约返回 None，详情见 ``output_semantics_report``）。"""

        return _resolve_artifact_semantics(self.label_policy_id)[0]

    def model_identity_facts(self) -> dict[str, object]:
        """工件身份**事实**（S01）：实际加载路径、实算内容哈希、工件自述契约。

        registry 的登记值与 bootstrap 运行时状态都属于"补充"，只能由调用方在
        fact 之外另行合并（``models/identity.build_model_identity_report``），
        不得覆盖这里的任何字段。
        """
        return {
            "artifact_uri": str(self.artifact_metadata.get("artifact_uri", "")),
            "artifact_exists": True,
            "artifact_content_hash": str(
                self.artifact_metadata.get("artifact_content_hash", "")
            ),
            "artifact_created_at": self.artifact_created_at,
            "feature_schema_id": self.feature_schema_id,
            "feature_schema_hash": self.feature_schema_hash,
            "label_policy_id": self.label_policy_id,
            "label_policy_hash": self.label_policy_hash,
            "dataset_manifest_id": self.dataset_manifest_id,
            "artifact_path_requested": self.artifact_path_requested,
            "claimed_content_hash": str(self.artifact_metadata.get("bundle_content_hash", "")),
            "predictor_loaded": True,
            "output_semantics": self.output_semantics or "",
            "inference_allowed": not bool(self.inference_blocked_reason()),
            "inference_blocked_reason": self.inference_blocked_reason(),
            "load_error": "",
        }

    def output_semantics_report(self) -> dict[str, object]:
        """语义 + 未登记原因，供概率健康/审计字段落痕（C2）。"""

        semantics, error = _resolve_artifact_semantics(self.label_policy_id)
        return {
            "label_policy_id": self.label_policy_id,
            "output_semantics": semantics,
            "output_semantics_error": error,
            "event_label_metrics_allowed": semantics_supports_event_label_metrics(semantics),
        }

    @classmethod
    def from_artifact(
        cls,
        artifact: ModelArtifact,
        *,
        artifact_root: str | Path | None = None,
    ) -> SignalPredictor:
        meta_weights = _normalize_meta_weights(artifact.metadata.get("meta_blend_weights"))
        return cls(
            feature_columns=artifact.feature_columns,
            lgbm=LightGBMAdapter.from_dict(artifact.lgbm_model, base_path=artifact_root),
            xgb=XGBoostAdapter.from_dict(artifact.xgb_model, base_path=artifact_root),
            lgbm_calibrator=IsotonicCalibrator.from_dict(artifact.lgbm_calibrator),
            xgb_calibrator=IsotonicCalibrator.from_dict(artifact.xgb_calibrator),
            meta_weights=meta_weights,
            artifact_metadata=dict(artifact.metadata),
            label_policy_id=str(artifact.label_policy_id),
            artifact_created_at=str(artifact.created_at),
            feature_schema_id=str(artifact.feature_schema_id),
            feature_schema_hash=str(artifact.feature_schema_hash),
            label_policy_hash=str(artifact.label_policy_hash),
            dataset_manifest_id=str(artifact.dataset_manifest_id),
        )

    @classmethod
    def load(cls, path: str | Path) -> SignalPredictor:
        """加载工件，并把**本次实际加载的文件**钉成内容哈希记进元数据。

        2026-09-16 之前没有这一步：registry 有 ``artifact_content_hash`` 列、
        ``compute_artifact_identity_hash`` 也能算，但加载路径从不参与，于是"生产到底
        在跑哪个工件"只能靠文件 mtime 旁证（排查 raw A/B 自检失败时就只能猜是不是工件
        被换过）。哈希在**加载时算一次**并缓存，``mode_details()`` 只是读取，不给
        高频 health 端点增加每次请求的 IO。

        哈希失败不阻断加载（只在元数据留空串）——身份缺失要能被观测到，但不该让服务起不来。
        """
        artifact_path = Path(path)
        artifact = ModelArtifact.load(artifact_path)
        predictor = cls.from_artifact(artifact, artifact_root=artifact_path.parent)
        predictor.artifact_metadata["artifact_uri"] = str(artifact_path)
        predictor.artifact_path_requested = str(path)
        try:
            predictor.artifact_metadata["artifact_content_hash"] = compute_artifact_identity_hash(
                artifact_path
            )
        except Exception:  # noqa: BLE001 - 身份哈希失败不得阻断推理加载
            predictor.artifact_metadata["artifact_content_hash"] = ""
        return predictor

    def predict_row(self, features: pd.Series) -> dict[str, float]:
        batch = self.predict_rows(pd.DataFrame([features.to_dict()]))
        return {key: values[0] for key, values in batch.items()}

    def predict_row_with_raw(self, features: pd.Series) -> dict[str, float]:
        """同 ``predict_row``，但额外带校准前的 ``raw_*`` 分数（六键）。"""
        batch = self.predict_rows_with_raw(pd.DataFrame([features.to_dict()]))
        return {key: values[0] for key, values in batch.items()}

    def _predict_matrix(self, features: pd.DataFrame) -> dict[str, list[float]]:
        """raw 与 calibrated 的全量分数（内部单一实现，供两个公共入口共用）。

        返回 ``raw_lgbm/raw_xgb/raw_blend``（校准前）与 ``lgbm/xgb/meta``（校准后）。
        C3 的 ``raw_blend`` 变体靠它拿校准前分数：修后现有模型的校准器仍在塌
        （唯一值 7、spread 为负），需要区分伤害排序的是校准还是模型本身。
        """
        blocked = self.inference_blocked_reason()
        if blocked:
            raise ValueError(
                f"predictor_rejected:{blocked}; refusing production inference on a legacy artifact"
            )
        if features.empty:
            return {
                "raw_lgbm": [],
                "raw_xgb": [],
                "raw_blend": [],
                "lgbm": [],
                "xgb": [],
                "meta": [],
            }
        frame = features
        if any(column not in frame.columns for column in self.feature_columns):
            frame = frame.reindex(columns=self.feature_columns, fill_value=0.0)
        matrix = frame[self.feature_columns].to_numpy(dtype=float)

        raw_lgbm = self.lgbm.predict_proba(matrix)
        raw_xgb = self.xgb.predict_proba(matrix)
        lgbm_probs = self.lgbm_calibrator.predict(raw_lgbm)
        xgb_probs = self.xgb_calibrator.predict(raw_xgb)
        lgbm_weight = self.meta_weights.get("lgbm", 0.5)
        xgb_weight = self.meta_weights.get("xgb", 0.5)
        meta_probs = lgbm_probs * lgbm_weight + xgb_probs * xgb_weight
        raw_blend = raw_lgbm * lgbm_weight + raw_xgb * xgb_weight
        return {
            "raw_lgbm": [_clamp_prob(float(value)) for value in raw_lgbm],
            "raw_xgb": [_clamp_prob(float(value)) for value in raw_xgb],
            "raw_blend": [_clamp_prob(float(value)) for value in raw_blend],
            "lgbm": [_clamp_prob(float(value)) for value in lgbm_probs],
            "xgb": [_clamp_prob(float(value)) for value in xgb_probs],
            "meta": [_clamp_prob(float(value)) for value in meta_probs],
        }

    def predict_rows(self, features: pd.DataFrame) -> dict[str, list[float]]:
        """Vectorized inference for a matrix of engineered features.

        Missing feature columns are filled with 0.0, mirroring ``predict_row``.
        Returns per-column lists ``{"lgbm": [...], "xgb": [...], "meta": [...]}``.
        """
        full = self._predict_matrix(features)
        return {"lgbm": full["lgbm"], "xgb": full["xgb"], "meta": full["meta"]}

    def predict_rows_with_raw(self, features: pd.DataFrame) -> dict[str, list[float]]:
        """同 ``predict_rows``，但额外带**校准前**的 ``raw_*`` 分数。

        独立入口而非扩宽 ``predict_rows`` 的返回：后者有按精确字典的契约测试，
        且 ``service.py`` 在生产推理路径消费同一返回。raw 只服务诊断与研究
        （C3 的 raw_blend 变体、B2 的 raw/calibrated 同口径对比）。
        """
        return self._predict_matrix(features)

    def mode_details(self) -> dict[str, object]:
        lgbm_backend = str(self.artifact_metadata.get("lgbm_backend", self.lgbm.backend))
        xgb_backend = str(self.artifact_metadata.get("xgb_backend", self.xgb.backend))
        lgbm_load_source = str(getattr(self.lgbm, "load_source", "")).strip()
        xgb_load_source = str(getattr(self.xgb, "load_source", "")).strip()
        degraded_model_mode = bool(
            self.artifact_metadata.get(
                "degraded_model_mode",
                lgbm_backend.startswith("fallback") and xgb_backend.startswith("fallback"),
            )
        )
        native_sidecar_fallback_used = "fallback_sidecar" in {
            lgbm_load_source,
            xgb_load_source,
        }
        degraded_reason = ""
        if degraded_model_mode:
            degraded_reason = f"native_backends_unavailable:lgbm={lgbm_backend},xgb={xgb_backend}"
        inference_blocked_reason = self.inference_blocked_reason()
        status_timestamp = datetime.now().isoformat()
        return {
            "predictor_mode": "artifact_loaded",
            "lgbm_backend": lgbm_backend,
            "xgb_backend": xgb_backend,
            "lgbm_load_source": lgbm_load_source,
            "xgb_load_source": xgb_load_source,
            "native_sidecar_fallback_used": native_sidecar_fallback_used,
            "degraded_model_mode": degraded_model_mode,
            "degraded_reason": degraded_reason,
            "degraded_reason_at": status_timestamp if degraded_reason else "",
            "inference_allowed": not bool(inference_blocked_reason),
            "inference_blocked_reason": inference_blocked_reason,
            "status_timestamp": status_timestamp,
            "created_at": str(self.artifact_metadata.get("artifact_created_at", "")),
            "calibration_method": str(self.artifact_metadata.get("calibration_method", "")),
            "meta_blend_weights": dict(self.meta_weights),
            # 发布别名自描述字段：当前加载模型在 registry 中的身份与 bundle 内容哈希。
            "registry_model_id": str(self.artifact_metadata.get("registry_model_id", "")),
            "bundle_content_hash": str(self.artifact_metadata.get("bundle_content_hash", "")),
            # 加载时按**实际文件内容**算出的哈希与来源路径（identity.py 的对账输入）。
            # 上面两个字段是发布时**盖章**的"声称"，这里是加载时的"事实"：两者不等
            # 即说明磁盘上的工件与发布时不是同一个（换件/篡改），**不读注册表也能发现**。
            "artifact_uri": str(self.artifact_metadata.get("artifact_uri", "")),
            "artifact_content_hash": str(self.artifact_metadata.get("artifact_content_hash", "")),
            "content_hash_verified": content_hash_matches_stamp(
                claimed=self.artifact_metadata.get("bundle_content_hash", ""),
                actual=self.artifact_metadata.get("artifact_content_hash", ""),
            ),
        }

    def inference_blocked_reason(self) -> str:
        """Return a non-empty reason when production inference must be refused.

        A legacy fallback artifact without a feature scaler is loadable for
        diagnostics (``mode_details``) but must never produce predictions;
        native backends and scaler-bearing fallback models return "".
        """
        reasons: list[str] = []
        lgbm_blocked = self.lgbm.inference_blocked_reason()
        xgb_blocked = self.xgb.inference_blocked_reason()
        if lgbm_blocked:
            reasons.append(f"lgbm:{lgbm_blocked}")
        if xgb_blocked:
            reasons.append(f"xgb:{xgb_blocked}")
        return ";".join(reasons)


def _normalize_meta_weights(raw_value: object) -> dict[str, float]:
    if not isinstance(raw_value, dict):
        return {"lgbm": 0.5, "xgb": 0.5}
    lgbm = _safe_weight(raw_value.get("lgbm"), default=0.5)
    xgb = _safe_weight(raw_value.get("xgb"), default=0.5)
    total = lgbm + xgb
    if total <= 0:
        return {"lgbm": 0.5, "xgb": 0.5}
    return {"lgbm": lgbm / total, "xgb": xgb / total}


def _safe_weight(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value))
        except ValueError:
            return default
    return default


def _clamp_prob(value: float) -> float:
    return max(0.0, min(1.0, value))
