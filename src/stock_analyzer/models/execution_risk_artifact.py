"""Serializable execution-risk sidecar artifact."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class ExecutionRiskArtifact:
    """Persistence format for execution-risk sidecar models."""

    version: str
    created_at: str
    dataset_id: str
    feature_names: list[str]
    trained_targets: list[str]
    target_models: dict[str, dict[str, object]]
    qualification_status: str = "shadow_only"
    qualification: dict[str, object] = field(default_factory=dict)
    training_summary: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        dataset_id: str,
        feature_names: list[str],
        target_models: dict[str, dict[str, object]],
        qualification_status: str = "shadow_only",
        qualification: dict[str, object] | None = None,
        training_summary: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ExecutionRiskArtifact:
        return cls(
            version="v1",
            created_at=datetime.now().isoformat(),
            dataset_id=str(dataset_id).strip(),
            feature_names=[str(item) for item in feature_names],
            trained_targets=sorted(str(item) for item in target_models.keys()),
            target_models={str(key): dict(value) for key, value in target_models.items()},
            qualification_status=_normalize_qualification_status(qualification_status),
            qualification=dict(qualification or {}),
            training_summary=dict(training_summary or {}),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "dataset_id": self.dataset_id,
            "feature_names": list(self.feature_names),
            "trained_targets": list(self.trained_targets),
            "target_models": {str(key): dict(value) for key, value in self.target_models.items()},
            "qualification_status": _normalize_qualification_status(
                self.qualification_status
            ),
            "qualification": dict(self.qualification),
            "training_summary": dict(self.training_summary),
            "metadata": dict(self.metadata),
        }

    def save(self, path: str | Path) -> None:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            output_path,
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
        )

    @classmethod
    def load(cls, path: str | Path) -> ExecutionRiskArtifact:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid execution-risk artifact payload")
        feature_names = payload.get("feature_names", [])
        trained_targets = payload.get("trained_targets", [])
        target_models = payload.get("target_models", {})
        if not isinstance(feature_names, list) or not isinstance(trained_targets, list):
            raise ValueError("invalid execution-risk artifact feature/target payload")
        if not isinstance(target_models, dict):
            raise ValueError("invalid execution-risk artifact models payload")
        training_summary = payload.get("training_summary", {})
        metadata = payload.get("metadata", {})
        qualification = payload.get("qualification", {})
        return cls(
            version=str(payload.get("version", "v1")),
            created_at=str(payload.get("created_at", "")),
            dataset_id=str(payload.get("dataset_id", "")),
            feature_names=[str(item) for item in feature_names],
            trained_targets=[str(item) for item in trained_targets],
            target_models={str(key): dict(value) for key, value in target_models.items()},
            # Artifacts created before qualification existed are never active by default.
            qualification_status=_normalize_qualification_status(
                payload.get("qualification_status", "shadow_only")
            ),
            qualification=dict(qualification) if isinstance(qualification, dict) else {},
            training_summary=(
                dict(training_summary) if isinstance(training_summary, dict) else {}
            ),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    @property
    def can_rerank(self) -> bool:
        return _normalize_qualification_status(self.qualification_status) == "qualified"


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _normalize_qualification_status(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"shadow_only", "qualified", "rejected"}:
        return normalized
    return "shadow_only"
