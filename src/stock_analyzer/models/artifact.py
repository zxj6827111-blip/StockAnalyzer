"""Model artifact serialization with explicit backend metadata and sidecars."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class ModelArtifact:
    """Serializable snapshot for dual-model inference."""

    version: str
    created_at: str
    feature_columns: list[str]
    lgbm_model: dict[str, object]
    xgb_model: dict[str, object]
    lgbm_calibrator: dict[str, object]
    xgb_calibrator: dict[str, object]
    training_metrics: dict[str, float]
    feature_schema_id: str = ""
    feature_schema_hash: str = ""
    label_policy_id: str = ""
    label_policy_hash: str = ""
    dataset_manifest_id: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        feature_schema_id: str = "",
        feature_schema_hash: str = "",
        label_policy_id: str = "",
        label_policy_hash: str = "",
        dataset_manifest_id: str = "",
        feature_columns: list[str],
        lgbm_model: dict[str, object],
        xgb_model: dict[str, object],
        lgbm_calibrator: dict[str, object],
        xgb_calibrator: dict[str, object],
        training_metrics: dict[str, float],
        metadata: dict[str, object] | None = None,
    ) -> ModelArtifact:
        return cls(
            version="v2",
            created_at=datetime.now().isoformat(),
            feature_schema_id=str(feature_schema_id).strip(),
            feature_schema_hash=str(feature_schema_hash).strip(),
            label_policy_id=str(label_policy_id).strip(),
            label_policy_hash=str(label_policy_hash).strip(),
            dataset_manifest_id=str(dataset_manifest_id).strip(),
            feature_columns=list(feature_columns),
            lgbm_model=dict(lgbm_model),
            xgb_model=dict(xgb_model),
            lgbm_calibrator=dict(lgbm_calibrator),
            xgb_calibrator=dict(xgb_calibrator),
            training_metrics=dict(training_metrics),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "feature_schema_id": self.feature_schema_id,
            "feature_schema_hash": self.feature_schema_hash,
            "label_policy_id": self.label_policy_id,
            "label_policy_hash": self.label_policy_hash,
            "dataset_manifest_id": self.dataset_manifest_id,
            "feature_columns": list(self.feature_columns),
            "lgbm_model": _sanitize_model_payload(self.lgbm_model),
            "xgb_model": _sanitize_model_payload(self.xgb_model),
            "lgbm_calibrator": dict(self.lgbm_calibrator),
            "xgb_calibrator": dict(self.xgb_calibrator),
            "training_metrics": dict(self.training_metrics),
            "metadata": dict(self.metadata),
        }

    def save(self, path: str | Path) -> None:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        previous_payload = _load_previous_payload(output_path)
        payload = self.to_dict()
        payload["lgbm_model"] = _persist_model_payload(
            model_payload=self.lgbm_model,
            artifact_path=output_path,
            model_name="lgbm",
            previous_payload=_nested_dict(previous_payload, "lgbm_model"),
        )
        payload["xgb_model"] = _persist_model_payload(
            model_payload=self.xgb_model,
            artifact_path=output_path,
            model_name="xgb",
            previous_payload=_nested_dict(previous_payload, "xgb_model"),
        )
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        _atomic_write_text(output_path, content)

    @classmethod
    def load(cls, path: str | Path) -> ModelArtifact:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid artifact payload")

        metrics_payload = payload.get("training_metrics", {})
        if not isinstance(metrics_payload, dict):
            raise ValueError("invalid artifact training_metrics")

        feature_columns = payload.get("feature_columns", [])
        if not isinstance(feature_columns, list):
            raise ValueError("invalid artifact feature_columns")

        lgbm_model = payload.get("lgbm_model", {})
        xgb_model = payload.get("xgb_model", {})
        lgbm_calibrator = payload.get("lgbm_calibrator", {})
        xgb_calibrator = payload.get("xgb_calibrator", {})
        metadata = payload.get("metadata", {})
        if not isinstance(lgbm_model, dict) or not isinstance(xgb_model, dict):
            raise ValueError("invalid artifact model payload")
        if not isinstance(lgbm_calibrator, dict) or not isinstance(xgb_calibrator, dict):
            raise ValueError("invalid artifact calibrator payload")
        if not isinstance(metadata, dict):
            metadata = {}

        training_metrics = {str(key): float(value) for key, value in metrics_payload.items()}
        return cls(
            version=str(payload.get("version", "v1")),
            created_at=str(payload.get("created_at", "")),
            feature_schema_id=str(payload.get("feature_schema_id", "")),
            feature_schema_hash=str(payload.get("feature_schema_hash", "")),
            label_policy_id=str(payload.get("label_policy_id", "")),
            label_policy_hash=str(payload.get("label_policy_hash", "")),
            dataset_manifest_id=str(payload.get("dataset_manifest_id", "")),
            feature_columns=[str(item) for item in feature_columns],
            lgbm_model=lgbm_model,
            xgb_model=xgb_model,
            lgbm_calibrator=lgbm_calibrator,
            xgb_calibrator=xgb_calibrator,
            training_metrics=training_metrics,
            metadata={str(key): value for key, value in metadata.items()},
        )


def _sanitize_model_payload(payload: dict[str, object]) -> dict[str, object]:
    sanitized = dict(payload)
    sanitized.pop("native_blob", None)
    sanitized.pop("native_blob_b64", None)
    return sanitized


def _persist_model_payload(
    *,
    model_payload: dict[str, object],
    artifact_path: Path,
    model_name: str,
    previous_payload: dict[str, object] | None,
) -> dict[str, object]:
    sanitized = _sanitize_model_payload(model_payload)
    inline_blob = model_payload.get("native_blob")
    inline_blob_b64 = model_payload.get("native_blob_b64")
    if inline_blob is None and inline_blob_b64 is None:
        return sanitized

    if isinstance(inline_blob, str):
        binary = inline_blob.encode("utf-8")
    elif isinstance(inline_blob_b64, str):
        import base64

        binary = base64.b64decode(inline_blob_b64.encode("ascii"))
    else:
        raise ValueError("native model payload must be text or base64")

    format_hint = str(model_payload.get("sidecar_format", "bin")).strip().lower() or "bin"
    ext = "txt" if format_hint == "txt" else "bin"
    sidecar_dir = artifact_path.parent / f"{artifact_path.stem}_sidecars"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    final_path = sidecar_dir / f"{model_name}_{stamp}.{ext}"
    tmp_path = sidecar_dir / f".{model_name}_{stamp}.{ext}.tmp"
    tmp_path.write_bytes(binary)
    os.replace(tmp_path, final_path)

    sanitized["sidecar_format"] = format_hint
    sanitized["sidecar_path"] = str(final_path.relative_to(artifact_path.parent)).replace("\\", "/")
    import hashlib

    sanitized["sidecar_sha256"] = hashlib.sha256(binary).hexdigest()

    if previous_payload:
        previous_path = previous_payload.get("sidecar_path")
        previous_hash = previous_payload.get("sidecar_sha256")
        if isinstance(previous_path, str) and previous_path.strip():
            sanitized["fallback_sidecar_path"] = previous_path
            if isinstance(previous_hash, str) and previous_hash.strip():
                sanitized["fallback_sidecar_sha256"] = previous_hash
    return sanitized


def _load_previous_payload(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _nested_dict(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    return value if isinstance(value, dict) else None


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
