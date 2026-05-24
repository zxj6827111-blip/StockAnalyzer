from __future__ import annotations

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import stock_analyzer.main as main_module


class _FakeLearningBackfillService:
    def bootstrap_learning_from_runtime_history(
        self,
        *,
        archive_dir: str,
        symbols: list[str] | None,
        build_manifest: bool,
        calibration_ratio: float | None,
        test_ratio: float | None,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "learning_runtime_history_cold_start",
            "archive_dir": archive_dir,
            "symbols": symbols or [],
            "build_manifest": build_manifest,
            "calibration_ratio": calibration_ratio,
            "test_ratio": test_ratio,
            "processed_archives": 1,
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "included_snapshot_count": 12,
        }

    def learning_protocol_status(self, manifest_limit: int) -> dict[str, object]:
        return {
            "sample_store": {
                "signal_snapshots": 24,
                "outcome_records": 24,
                "dataset_manifests": 1,
            },
            "outcomes": {
                "records": 24,
                "maturity_breakdown": {"reconciled": 24},
                "fidelity_breakdown": {"gold": 24},
            },
            "manifests": {
                "records": 1,
                "latest": {"dataset_manifest_id": "dataset_manifest_v1_test"},
                "items": [{"dataset_manifest_id": "dataset_manifest_v1_test"}],
            },
            "latest_learning_backfill": {
                "event_type": "learning_backfill_runtime_history_cold_start"
            },
            "governance": {
                "proposal_summary": {"records": 1, "pending_approval": 1},
                "ticket_summary": {"records": 1, "pending_confirmation": 0},
                "monitoring": {"revoked_model_count": 0},
                "config": {"release_confirmation_required": True},
            },
            "cold_start_ready": True,
            "manifest_limit": manifest_limit,
        }

    def train_learning_manifest(
        self,
        *,
        dataset_manifest_id: str,
        artifact_path: str | None,
        load_predictor: bool,
        register_model: bool,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "dataset_manifest_training",
            "input_mode": "dataset_manifest",
            "manifest_source": "requested" if dataset_manifest_id else "latest",
            "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
            "artifact_path": artifact_path or "tmp/learning_manifest_artifact.json",
            "predictor_loaded": load_predictor,
            "included_snapshot_count": 24,
            "included_outcome_count": 24,
            "feature_schema_id": "feature_schema_v1_test",
            "feature_schema_hash": "feature_hash_test",
            "label_policy_id": "label_policy_v1_test",
            "label_policy_hash": "label_hash_test",
            "model_registry": {
                "registered": register_model,
                "model_id": "model_manifest_test",
                "role": "challenger",
                "lifecycle_state": "trained",
                "artifact_uri": artifact_path or "tmp/learning_manifest_artifact.json",
                "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
                "feature_schema_id": "feature_schema_v1_test",
                "label_policy_id": "label_policy_v1_test",
                "source": "train_learning_manifest",
            }
            if register_model
            else {"registered": False, "reason": "registration_disabled"},
            "errors": [],
        }

    def model_registry_entries(
        self,
        *,
        limit: int,
        role: str,
        lifecycle_state: str,
    ) -> dict[str, object]:
        return {
            "records": 1,
            "items": [
                {
                    "model_id": "model_manifest_test",
                    "role": role or "challenger",
                    "lifecycle_state": lifecycle_state or "trained",
                    "dataset_manifest_id": "dataset_manifest_v1_test",
                }
            ],
            "active_champion": None,
            "limit": limit,
        }

    def model_registry_entry(self, *, model_id: str) -> dict[str, object] | None:
        return {
            "model_id": model_id,
            "role": "challenger",
            "lifecycle_state": "trained",
            "dataset_manifest_id": "dataset_manifest_v1_test",
        }

    def register_model_artifact(
        self,
        *,
        artifact_path: str,
        role: str,
        lifecycle_state: str,
        source: str,
        parent_model_id: str,
    ) -> dict[str, object]:
        return {
            "registered": True,
            "model_id": "model_registered_test",
            "role": role,
            "lifecycle_state": lifecycle_state,
            "artifact_uri": artifact_path,
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "feature_schema_id": "feature_schema_v1_test",
            "label_policy_id": "label_policy_v1_test",
            "source": source,
            "parent_model_id": parent_model_id,
        }

    def bootstrap_active_champion_from_artifact(
        self,
        *,
        artifact_path: str,
        source: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "reason": "artifact_registered_as_active_champion",
            "model_id": "model_champion_test",
            "artifact_uri": artifact_path or "tmp/registered_model.json",
            "role": "champion",
            "lifecycle_state": "approved",
            "source": source,
        }

    def update_model_registry_lifecycle(
        self,
        *,
        model_id: str,
        lifecycle_state: str,
        blocked_reason: str,
        timestamp: object | None,
    ) -> dict[str, object]:
        return {
            "model_id": model_id,
            "lifecycle_state": lifecycle_state,
            "blocked_reason": blocked_reason,
            "updated_at": None if timestamp is None else str(timestamp),
        }

    def update_model_registry_role(
        self,
        *,
        model_id: str,
        role: str,
        timestamp: object | None,
    ) -> dict[str, object]:
        return {
            "model_id": model_id,
            "role": role,
            "updated_at": None if timestamp is None else str(timestamp),
        }

    def build_shadow_dataset(
        self,
        *,
        model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        include_rows: bool,
        preview_limit: int,
    ) -> dict[str, object]:
        return {
            "shadow_dataset_id": "shadow_dataset_test",
            "model_id": model_id,
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "requested_split_names": split_names or [],
            "row_count": max_rows or 3,
            "include_rows": include_rows,
            "preview_limit": preview_limit,
        }

    def build_champion_shadow_report(
        self,
        *,
        model_id: str,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        signal_threshold: float,
        include_rows: bool,
        preview_limit: int,
    ) -> dict[str, object]:
        return {
            "comparison_report_id": "champion_shadow_report_test",
            "shadow_model_id": model_id,
            "champion_model_id": champion_model_id or "champion_default_test",
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "requested_split_names": split_names or [],
            "row_count": max_rows or 4,
            "signal_threshold": signal_threshold,
            "include_rows": include_rows,
            "preview_limit": preview_limit,
        }

    def build_shadow_online_v2_report(
        self,
        *,
        model_id: str,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        max_samples: int | None,
        min_samples: int,
        learning_rate: float,
        signal_threshold: float,
        include_rows: bool,
        preview_limit: int,
    ) -> dict[str, object]:
        return {
            "report_id": "shadow_online_v2_report_test",
            "shadow_model_id": model_id,
            "champion_model_id": champion_model_id or "champion_default_test",
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "requested_split_names": split_names or [],
            "row_count": max_rows or 5,
            "max_samples": max_samples,
            "min_samples": min_samples,
            "learning_rate": learning_rate,
            "signal_threshold": signal_threshold,
            "include_rows": include_rows,
            "preview_limit": preview_limit,
            "status": "ok",
        }

    def train_execution_risk_model(
        self,
        *,
        artifact_path: str | None,
        maturity_statuses: list[str] | None,
        max_rows: int | None,
        min_samples_per_target: int,
        calibration_ratio: float,
        test_ratio: float,
        epochs: int,
        learning_rate: float,
        l2: float,
        seed: int,
        now: object | None,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "execution_risk_training",
            "status": "trained",
            "artifact_path": artifact_path or "tmp/execution_risk_artifact.json",
            "dataset_id": "execution_risk_dataset_v1_test",
            "trained_targets": ["can_fill", "likely_slippage_high"],
            "target_metrics": {"can_fill": {"auc": 0.71}},
            "target_row_counts": {"can_fill": 42},
            "metadata": {
                "requested_maturity_statuses": maturity_statuses or [],
                "seed": seed,
                "now": None if now is None else str(now),
            },
        }

    def execution_risk_status(self) -> dict[str, object]:
        return {
            "latest": {
                "status": "trained",
                "artifact_path": "tmp/execution_risk_artifact.json",
                "dataset_id": "execution_risk_dataset_v1_test",
            },
            "history_count": 1,
            "artifact_exists": True,
            "artifact_path": "tmp/execution_risk_artifact.json",
            "trained_targets": ["can_fill", "likely_slippage_high"],
            "dataset_id": "execution_risk_dataset_v1_test",
        }

    def execution_risk_training_history(self, *, limit: int) -> dict[str, object]:
        return {
            "records": 1,
            "items": [
                {
                    "status": "trained",
                    "artifact_path": "tmp/execution_risk_artifact.json",
                    "dataset_id": "execution_risk_dataset_v1_test",
                }
            ][:limit],
        }

    def build_execution_aware_report(
        self,
        *,
        model_id: str,
        execution_risk_artifact_path: str,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        include_rows: bool,
        preview_limit: int,
    ) -> dict[str, object]:
        return {
            "report_id": "execution_aware_report_test",
            "shadow_model_id": model_id,
            "champion_model_id": champion_model_id or "champion_default_test",
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "execution_risk_artifact_path": (
                execution_risk_artifact_path or "tmp/execution_risk_artifact.json"
            ),
            "requested_split_names": split_names or [],
            "row_count": max_rows or 6,
            "include_rows": include_rows,
            "preview_limit": preview_limit,
            "summary_metrics": {
                "shadow_mean_can_fill": 0.73,
                "shadow_high_risk_ratio": 0.18,
            },
        }

    def run_learning_manifest_shadow_validation(
        self,
        *,
        dataset_manifest_id: str,
        artifact_path: str | None,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        include_rows: bool,
        preview_limit: int,
        max_samples: int | None,
        min_samples: int,
        learning_rate: float,
        signal_threshold: float,
        load_predictor: bool,
        mark_shadow_validated: bool,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "learning_manifest_shadow_validation",
            "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
            "shadow_model_id": "model_shadow_test",
            "champion_model_id": champion_model_id or "model_champion_test",
            "evaluation_split_names": split_names or ["test"],
            "training": {
                "ok": True,
                "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
                "artifact_path": artifact_path or "tmp/learning_manifest_artifact.json",
                "predictor_loaded": load_predictor,
                "model_registry": {
                    "registered": True,
                    "model_id": "model_shadow_test",
                    "source": "train_learning_manifest",
                },
            },
            "shadow_dataset": {
                "ok": True,
                "shadow_dataset_id": "shadow_dataset_test",
                "row_count": max_rows or 3,
                "requested_split_names": split_names or ["test"],
                "include_rows": include_rows,
                "preview_limit": preview_limit,
            },
            "champion_shadow_report": {
                "ok": True,
                "comparison_report_id": "champion_shadow_report_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "shadow_model_id": "model_shadow_test",
                "signal_threshold": signal_threshold,
            },
            "shadow_online_v2_report": {
                "ok": True,
                "report_id": "shadow_online_v2_report_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "shadow_model_id": "model_shadow_test",
                "max_samples": max_samples,
                "min_samples": min_samples,
                "learning_rate": learning_rate,
                "status": "updated",
            },
            "registry_lifecycle": (
                {
                    "updated": True,
                    "record": {
                        "model_id": "model_shadow_test",
                        "lifecycle_state": "shadow_validated",
                    },
                }
                if mark_shadow_validated
                else {"updated": False, "reason": "mark_shadow_validated_disabled"}
            ),
            "errors": [],
        }

    def evaluate_learning_model_promotion_gate(
        self,
        *,
        model_id: str,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        max_samples: int | None,
        min_samples: int,
        learning_rate: float,
        signal_threshold: float,
        preview_limit: int,
        min_shadow_v2_minus_champion_return: float,
        max_shadow_v2_brier_delta: float,
        max_shadow_v2_logloss_delta: float,
        max_signal_divergence_ratio: float | None,
        approve_if_passed: bool,
        block_if_failed: bool,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "learning_model_promotion_gate",
            "status": "pass",
            "accepted": True,
            "recommended_action": "approve",
            "shadow_model_id": model_id,
            "champion_model_id": champion_model_id or "model_champion_test",
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "evaluation_split_names": split_names or ["test"],
            "lifecycle_before": "trained",
            "lifecycle_after": "approved" if approve_if_passed else "trained",
            "role": "challenger",
            "reason_codes": ["promotion_gate_passed"],
            "blockers": [],
            "warnings": [],
            "gate_thresholds": {
                "min_samples": min_samples,
                "min_shadow_v2_minus_champion_return": min_shadow_v2_minus_champion_return,
                "max_shadow_v2_brier_delta": max_shadow_v2_brier_delta,
                "max_shadow_v2_logloss_delta": max_shadow_v2_logloss_delta,
                "max_signal_divergence_ratio": (
                    max_signal_divergence_ratio
                    if max_signal_divergence_ratio is not None
                    else 0.35
                ),
            },
            "metrics_snapshot": {
                "shadow_online_v2_samples_used": max(min_samples, 7),
                "shadow_v2_minus_champion_return": 0.012,
                "shadow_v2_delta_brier": 0.008,
                "shadow_v2_delta_logloss": 0.012,
            },
            "checks": [
                {
                    "name": "shadow_online_v2_status",
                    "status": "pass",
                    "detail": "status=updated",
                }
            ],
            "champion_shadow_report": {
                "comparison_report_id": "champion_shadow_report_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "shadow_model_id": model_id,
                "row_count": max_rows or 10,
            },
            "shadow_online_v2_report": {
                "report_id": "shadow_online_v2_report_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "shadow_model_id": model_id,
                "status": "updated",
                "row_count": max_samples or 7,
            },
            "registry_transition": {
                "updated": approve_if_passed or block_if_failed,
                "action": "approved" if approve_if_passed else "noop",
                "reason": (
                    "approval_applied"
                    if approve_if_passed
                    else "approve_if_passed_disabled"
                ),
                "records": (
                    [{"model_id": model_id, "lifecycle_state": "approved"}]
                    if approve_if_passed
                    else []
                ),
            },
            "errors": [],
        }

    def run_learning_manifest_shadow_promotion_gate(
        self,
        *,
        dataset_manifest_id: str,
        artifact_path: str | None,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        include_rows: bool,
        preview_limit: int,
        max_samples: int | None,
        min_samples: int,
        learning_rate: float,
        signal_threshold: float,
        load_predictor: bool,
        mark_shadow_validated: bool,
        min_shadow_v2_minus_champion_return: float,
        max_shadow_v2_brier_delta: float,
        max_shadow_v2_logloss_delta: float,
        max_signal_divergence_ratio: float | None,
        approve_if_passed: bool,
        block_if_failed: bool,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "learning_manifest_shadow_promotion_gate",
            "status": "pass",
            "accepted": True,
            "recommended_action": "approve",
            "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
            "shadow_model_id": "model_shadow_test",
            "champion_model_id": champion_model_id or "model_champion_test",
            "evaluation_split_names": split_names or ["test"],
            "final_lifecycle_state": "approved" if approve_if_passed else "shadow_validated",
            "final_role": "challenger",
            "shadow_validation": {
                "ok": True,
                "mode": "learning_manifest_shadow_validation",
                "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
                "shadow_model_id": "model_shadow_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "evaluation_split_names": split_names or ["test"],
                "training": {
                    "ok": True,
                    "artifact_path": artifact_path or "tmp/learning_manifest_artifact.json",
                    "predictor_loaded": load_predictor,
                },
                "registry_lifecycle": (
                    {
                        "updated": True,
                        "record": {
                            "model_id": "model_shadow_test",
                            "lifecycle_state": "shadow_validated",
                        },
                    }
                    if mark_shadow_validated
                    else {"updated": False, "reason": "mark_shadow_validated_disabled"}
                ),
                "errors": [],
            },
            "promotion_gate": {
                "ok": True,
                "mode": "learning_model_promotion_gate",
                "status": "pass",
                "accepted": True,
                "recommended_action": "approve",
                "shadow_model_id": "model_shadow_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "evaluation_split_names": split_names or ["test"],
                "gate_thresholds": {
                    "min_samples": min_samples,
                    "min_shadow_v2_minus_champion_return": min_shadow_v2_minus_champion_return,
                    "max_shadow_v2_brier_delta": max_shadow_v2_brier_delta,
                    "max_shadow_v2_logloss_delta": max_shadow_v2_logloss_delta,
                    "max_signal_divergence_ratio": (
                        max_signal_divergence_ratio
                        if max_signal_divergence_ratio is not None
                        else 0.35
                    ),
                },
                "registry_transition": {
                    "updated": approve_if_passed or block_if_failed,
                    "action": "approved" if approve_if_passed else "noop",
                    "reason": (
                        "approval_applied"
                        if approve_if_passed
                        else "approve_if_passed_disabled"
                    ),
                    "records": (
                        [{"model_id": "model_shadow_test", "lifecycle_state": "approved"}]
                        if approve_if_passed
                        else []
                    ),
                },
                "errors": [],
            },
            "errors": [],
        }

    def create_learning_model_proposal(
        self,
        *,
        model_id: str,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        max_samples: int | None,
        min_samples: int,
        learning_rate: float,
        signal_threshold: float,
        preview_limit: int,
        min_shadow_v2_minus_champion_return: float,
        max_shadow_v2_brier_delta: float,
        max_shadow_v2_logloss_delta: float,
        max_signal_divergence_ratio: float | None,
        approve_if_passed: bool,
        block_if_failed: bool,
        allow_warn_status: bool,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "mode": "learning_model_promotion_proposal",
            "proposal": {
                "proposal_id": "LRN-PRP-0001",
                "status": "generated",
                "gate_status": "pass",
                "shadow_model_id": model_id,
                "champion_model_id": champion_model_id or "model_champion_test",
                "dataset_manifest_id": "dataset_manifest_v1_test",
                "evaluation_split_names": split_names or ["test"],
                "payload_uri": "suggestions/learning_model_governance/proposals/LRN-PRP-0001.json",
                "source_trace_id": source_trace_id,
                "allow_warn_status": allow_warn_status,
                "gate_thresholds": {
                    "min_samples": min_samples,
                    "max_rows": max_rows,
                    "max_samples": max_samples,
                    "learning_rate": learning_rate,
                    "signal_threshold": signal_threshold,
                    "preview_limit": preview_limit,
                    "min_shadow_v2_minus_champion_return": min_shadow_v2_minus_champion_return,
                    "max_shadow_v2_brier_delta": max_shadow_v2_brier_delta,
                    "max_shadow_v2_logloss_delta": max_shadow_v2_logloss_delta,
                    "max_signal_divergence_ratio": max_signal_divergence_ratio,
                    "approve_if_passed": approve_if_passed,
                    "block_if_failed": block_if_failed,
                },
            },
            "promotion_gate": {
                "ok": True,
                "status": "pass",
                "accepted": True,
                "shadow_model_id": model_id,
            },
            "errors": [],
        }

    def run_learning_manifest_shadow_proposal(
        self,
        *,
        dataset_manifest_id: str,
        artifact_path: str | None,
        champion_model_id: str,
        split_names: list[str] | None,
        max_rows: int | None,
        include_rows: bool,
        preview_limit: int,
        max_samples: int | None,
        min_samples: int,
        learning_rate: float,
        signal_threshold: float,
        load_predictor: bool,
        mark_shadow_validated: bool,
        min_shadow_v2_minus_champion_return: float,
        max_shadow_v2_brier_delta: float,
        max_shadow_v2_logloss_delta: float,
        max_signal_divergence_ratio: float | None,
        approve_if_passed: bool,
        block_if_failed: bool,
        allow_warn_status: bool,
        source_trace_id: str,
        auto_approve: bool = False,
        auto_release: bool = False,
        auto_reload_predictor: bool = True,
        notify_on_rejection: bool = False,
    ) -> dict[str, object]:
        auto_promotion_status = (
            "released"
            if auto_release
            else "approved"
            if auto_approve
            else "manual_review"
        )
        return {
            "ok": True,
            "mode": "learning_manifest_shadow_proposal",
            "status": "generated",
            "accepted": True,
            "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
            "shadow_model_id": "model_shadow_test",
            "champion_model_id": champion_model_id or "model_champion_test",
            "evaluation_split_names": split_names or ["test"],
            "workflow": {
                "ok": True,
                "mode": "learning_manifest_shadow_promotion_gate",
                "status": "pass",
                "accepted": True,
                "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
                "shadow_model_id": "model_shadow_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "artifact_path": artifact_path or "tmp/manifest_model.json",
                "include_rows": include_rows,
                "preview_limit": preview_limit,
                "max_rows": max_rows,
                "max_samples": max_samples,
                "min_samples": min_samples,
                "learning_rate": learning_rate,
                "signal_threshold": signal_threshold,
                "load_predictor": load_predictor,
                "mark_shadow_validated": mark_shadow_validated,
                "allow_warn_status": allow_warn_status,
                "source_trace_id": source_trace_id,
            },
            "proposal": {
                "proposal_id": "LRN-PRP-0002",
                "status": "generated",
                "gate_status": "pass",
                "shadow_model_id": "model_shadow_test",
                "champion_model_id": champion_model_id or "model_champion_test",
                "dataset_manifest_id": dataset_manifest_id or "dataset_manifest_v1_test",
                "payload_uri": "suggestions/learning_model_governance/proposals/LRN-PRP-0002.json",
            },
            "proposal_result": {"accepted": True, "errors": []},
            "auto_promotion": {
                "enabled": bool(auto_approve or auto_release),
                "proposal_id": "LRN-PRP-0002",
                "approval_id": "LRN-APR-0002" if auto_approve else "",
                "ticket_id": "LRN-TKT-0002" if auto_release else "",
                "auto_approve": auto_approve,
                "auto_release": auto_release,
                "predictor_loaded": bool(auto_release and auto_reload_predictor),
                "rejection_notified": bool(notify_on_rejection and not auto_approve),
                "status": auto_promotion_status,
                "errors": [],
            },
            "errors": [],
        }

    def latest_learning_model_proposal(self) -> dict[str, object] | None:
        return {"proposal_id": "LRN-PRP-0001", "status": "generated"}

    def learning_model_proposal_history(
        self,
        *,
        limit: int,
        proposal_id: str,
        status: str,
    ) -> dict[str, object]:
        return {
            "records": 1,
            "items": [
                {
                    "proposal_id": proposal_id or "LRN-PRP-0001",
                    "status": status or "generated",
                }
            ],
            "filters": {
                "limit": limit,
                "proposal_id": proposal_id,
                "status": status,
            },
        }

    def record_learning_model_proposal_approval(
        self,
        *,
        approver: str,
        approved: bool,
        proposal_id: str,
        note: str,
        timestamp: object | None,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "record": {
                "approval_id": "LRN-APR-0001",
                "approved": approved,
                "approver": approver,
                "proposal_id": proposal_id or "LRN-PRP-0001",
                "note": note,
                "timestamp": None if timestamp is None else str(timestamp),
                "source_trace_id": source_trace_id,
            },
            "proposal": {
                "proposal_id": proposal_id or "LRN-PRP-0001",
                "status": "approved" if approved else "rejected",
            },
        }

    def latest_learning_model_approval(self) -> dict[str, object] | None:
        return {"approval_id": "LRN-APR-0001", "approved": True}

    def learning_model_approval_history(self, limit: int) -> dict[str, object]:
        return {
            "records": 1,
            "items": [{"approval_id": "LRN-APR-0001", "approved": True}],
            "limit": limit,
        }

    def issue_learning_model_release_ticket(
        self,
        *,
        operator: str,
        proposal_id: str,
        note: str,
        timestamp: object | None,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "ticket": {
                "ticket_id": "LRN-TKT-0001",
                "status": "issued",
                "operator": operator,
                "proposal": {"proposal_id": proposal_id or "LRN-PRP-0001"},
                "note": note,
                "timestamp": None if timestamp is None else str(timestamp),
                "source_trace_id": source_trace_id,
            },
            "proposal": {
                "proposal_id": proposal_id or "LRN-PRP-0001",
                "status": "ticket_issued",
            },
        }

    def execute_learning_model_release_ticket(
        self,
        *,
        executor: str,
        ticket_id: str,
        note: str,
        confirm_window: bool,
        timestamp: object | None,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "ticket": {
                "ticket_id": ticket_id or "LRN-TKT-0001",
                "status": "executed",
                "execution": {
                    "executor": executor,
                    "note": note,
                    "confirm_window": confirm_window,
                    "timestamp": None if timestamp is None else str(timestamp),
                    "source_trace_id": source_trace_id,
                },
                "pending_confirmation": {
                    "required": True,
                    "state": "pending",
                },
            },
            "proposal": {"proposal_id": "LRN-PRP-0001", "status": "executed"},
        }

    def confirm_learning_model_release_ticket(
        self,
        *,
        confirmer: str,
        ticket_id: str,
        note: str,
        timestamp: object | None,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "ticket": {
                "ticket_id": ticket_id or "LRN-TKT-0001",
                "status": "confirmed",
                "confirmation": {
                    "confirmer": confirmer,
                    "note": note,
                    "timestamp": None if timestamp is None else str(timestamp),
                    "source_trace_id": source_trace_id,
                },
            },
            "proposal": {"proposal_id": "LRN-PRP-0001", "status": "confirmed"},
        }

    def latest_learning_model_release_ticket(self) -> dict[str, object] | None:
        return {"ticket_id": "LRN-TKT-0001", "status": "confirmed"}

    def learning_model_release_ticket_history(self, limit: int) -> dict[str, object]:
        return {
            "records": 1,
            "items": [{"ticket_id": "LRN-TKT-0001", "status": "confirmed"}],
            "limit": limit,
        }

    def learning_model_release_ticket_timeline(
        self,
        *,
        ticket_id: str,
        status: str,
        limit: int,
    ) -> dict[str, object]:
        return {
            "records": 1,
            "tickets": [
                {
                    "ticket_id": ticket_id or "LRN-TKT-0001",
                    "latest_status": status or "confirmed",
                    "events": [
                        {"event": "issued"},
                        {"event": "executed"},
                        {"event": "confirmed"},
                    ],
                }
            ],
            "filters": {
                "ticket_id": ticket_id,
                "status": status,
                "limit": limit,
            },
        }

    def revoke_learning_model_proposal(
        self,
        *,
        revoked_by: str,
        proposal_id: str,
        note: str,
        revoke_model: bool,
        timestamp: object | None,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "proposal": {
                "proposal_id": proposal_id or "LRN-PRP-0001",
                "status": "revoked",
                "release_state": "revoked",
            },
            "registry_transition": {
                "updated": revoke_model,
                "action": "revoked" if revoke_model else "noop",
            },
            "compliance_update": {
                "state": "invalidated",
                "written": True,
            },
            "revocation": {
                "revoked_by": revoked_by,
                "note": note,
                "timestamp": None if timestamp is None else str(timestamp),
                "source_trace_id": source_trace_id,
            },
        }

    def rollback_learning_model_release_ticket(
        self,
        *,
        rollback_by: str,
        ticket_id: str,
        note: str,
        timestamp: object | None,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "ticket": {
                "ticket_id": ticket_id or "LRN-TKT-0001",
                "status": "rolled_back",
                "rollback": {
                    "rollback_by": rollback_by,
                    "note": note,
                    "timestamp": None if timestamp is None else str(timestamp),
                    "source_trace_id": source_trace_id,
                },
            },
            "proposal": {"proposal_id": "LRN-PRP-0001", "status": "rolled_back"},
            "compliance_update": {"state": "rolled_back", "written": True},
        }

    def run_learning_model_release_confirmation_watchdog(
        self,
        *,
        now: object | None,
        source_trace_id: str,
    ) -> dict[str, object]:
        return {
            "timestamp": None if now is None else str(now),
            "checked": 1,
            "overdue": 1,
            "rolled_back": 1,
            "results": [{"accepted": True, "ticket": {"ticket_id": "LRN-TKT-0001"}}],
            "source_trace_id": source_trace_id,
        }

    def learning_model_governance_status(
        self,
        *,
        proposal_limit: int,
        ticket_limit: int,
    ) -> dict[str, object]:
        return {
            "proposal_latest": {"proposal_id": "LRN-PRP-0001", "status": "generated"},
            "proposal_summary": {
                "records": 1,
                "pending_approval": 1,
                "approved_pending_ticket": 0,
                "history_count": 2,
            },
            "approval_latest": {"approval_id": "LRN-APR-0001", "approved": True},
            "approval_history_count": 1,
            "ticket_latest": {"ticket_id": "LRN-TKT-0001", "status": "confirmed"},
            "ticket_summary": {
                "records": 1,
                "pending_confirmation": 0,
                "overdue_confirmation": 0,
                "history_count": 3,
            },
            "monitoring": {
                "pending_approval": 1,
                "approved_pending_ticket": 0,
                "pending_confirmation": 0,
                "overdue_confirmation": 0,
                "blocked_model_count": 0,
                "revoked_model_count": 0,
            },
            "config": {
                "history_limit": 20,
                "release_confirmation_required": True,
                "release_confirmation_ttl_days": 3,
            },
            "active_champion": {"model_id": "model_champion_test"},
            "limits": {
                "proposal_limit": proposal_limit,
                "ticket_limit": ticket_limit,
            },
        }


def test_learning_runtime_history_bootstrap_endpoint_returns_cold_start_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/learning/runtime-history/bootstrap",
        json={
            "archive_dir": "tmp/runtime_history",
            "symbols": ["600000"],
            "build_manifest": True,
            "calibration_ratio": 0.2,
            "test_ratio": 0.2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["archive_dir"] == "tmp/runtime_history"
    assert payload["symbols"] == ["600000"]
    assert payload["dataset_manifest_id"] == "dataset_manifest_v1_test"


def test_learning_status_endpoint_returns_learning_protocol_summary(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.get("/learning/status", params={"manifest_limit": 3})

    assert response.status_code == 200
    payload = response.json()
    assert payload["cold_start_ready"] is True
    assert payload["sample_store"]["dataset_manifests"] == 1
    assert payload["manifests"]["latest"]["dataset_manifest_id"] == "dataset_manifest_v1_test"
    assert payload["governance"]["proposal_summary"]["records"] == 1
    assert payload["governance"]["config"]["release_confirmation_required"] is True


def test_train_learning_manifest_endpoint_returns_manifest_training_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/train/learning-manifest",
        json={
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "artifact_path": "tmp/manifest_model.json",
            "load_predictor": True,
            "register_model": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["input_mode"] == "dataset_manifest"
    assert payload["dataset_manifest_id"] == "dataset_manifest_v1_test"
    assert payload["artifact_path"] == "tmp/manifest_model.json"
    assert payload["predictor_loaded"] is True
    assert payload["model_registry"]["registered"] is True
    assert payload["model_registry"]["source"] == "train_learning_manifest"


def test_model_registry_entries_endpoint_returns_registry_rows(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.get(
        "/models/registry",
        params={"limit": 5, "role": "challenger", "lifecycle_state": "trained"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["records"] == 1
    assert payload["items"][0]["role"] == "challenger"
    assert payload["items"][0]["lifecycle_state"] == "trained"
    assert payload["limit"] == 5


def test_model_registry_entry_endpoint_returns_single_row(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.get("/models/registry/model_manifest_test")

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "model_manifest_test"
    assert payload["dataset_manifest_id"] == "dataset_manifest_v1_test"


def test_register_model_artifact_endpoint_returns_registry_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/registry/register",
        json={
            "artifact_path": "tmp/registered_model.json",
            "role": "shadow",
            "lifecycle_state": "trained",
            "source": "api_test",
            "parent_model_id": "champion_a",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["registered"] is True
    assert payload["role"] == "shadow"
    assert payload["source"] == "api_test"
    assert payload["parent_model_id"] == "champion_a"


def test_bootstrap_active_champion_endpoint_returns_repair_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/registry/bootstrap-active-champion",
        json={
            "artifact_path": "tmp/registered_model.json",
            "source": "api_bootstrap_test",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["role"] == "champion"
    assert payload["lifecycle_state"] == "approved"
    assert payload["source"] == "api_bootstrap_test"


def test_update_model_registry_lifecycle_endpoint_accepts_timestamp(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/registry/lifecycle",
        json={
            "model_id": "model_manifest_test",
            "lifecycle_state": "blocked",
            "blocked_reason": "manual_review",
            "timestamp": "2026-03-30T10:00:00",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "model_manifest_test"
    assert payload["lifecycle_state"] == "blocked"
    assert payload["blocked_reason"] == "manual_review"


def test_update_model_registry_role_endpoint_accepts_timestamp(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/registry/role",
        json={
            "model_id": "model_manifest_test",
            "role": "champion",
            "timestamp": "2026-03-30T10:05:00",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "model_manifest_test"
    assert payload["role"] == "champion"


def test_build_shadow_dataset_endpoint_returns_shadow_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/shadow-dataset",
        json={
            "model_id": "model_manifest_test",
            "split_names": ["test"],
            "max_rows": 12,
            "include_rows": True,
            "preview_limit": 3,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["shadow_dataset_id"] == "shadow_dataset_test"
    assert payload["requested_split_names"] == ["test"]
    assert payload["row_count"] == 12
    assert payload["include_rows"] is True


def test_build_champion_shadow_report_endpoint_returns_report_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/champion-shadow-report",
        json={
            "model_id": "model_shadow_test",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 9,
            "signal_threshold": 0.65,
            "include_rows": False,
            "preview_limit": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["comparison_report_id"] == "champion_shadow_report_test"
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["signal_threshold"] == 0.65


def test_build_shadow_online_v2_report_endpoint_returns_report_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/shadow-online-v2-report",
        json={
            "model_id": "model_shadow_test",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 11,
            "max_samples": 20,
            "min_samples": 7,
            "learning_rate": 0.2,
            "signal_threshold": 0.6,
            "include_rows": False,
            "preview_limit": 4,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["report_id"] == "shadow_online_v2_report_test"
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["max_samples"] == 20


def test_train_execution_risk_endpoint_returns_training_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/train/execution-risk",
        json={
            "artifact_path": "tmp/execution_risk_artifact.json",
            "maturity_statuses": ["reconciled"],
            "max_rows": 50,
            "min_samples_per_target": 10,
            "epochs": 120,
            "seed": 9,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "execution_risk_training"
    assert payload["status"] == "trained"
    assert payload["dataset_id"] == "execution_risk_dataset_v1_test"
    assert "can_fill" in payload["trained_targets"]


def test_build_execution_aware_report_endpoint_returns_report_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/execution-aware-report",
        json={
            "model_id": "model_shadow_test",
            "execution_risk_artifact_path": "tmp/execution_risk_artifact.json",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 8,
            "include_rows": False,
            "preview_limit": 3,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["report_id"] == "execution_aware_report_test"
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["row_count"] == 8
    assert payload["execution_risk_artifact_path"] == "tmp/execution_risk_artifact.json"
    assert payload["summary_metrics"]["shadow_mean_can_fill"] == 0.73


def test_train_learning_manifest_shadow_validate_endpoint_returns_bundle_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/train/learning-manifest/shadow-validate",
        json={
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "artifact_path": "tmp/manifest_model.json",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 10,
            "include_rows": True,
            "preview_limit": 3,
            "max_samples": 20,
            "min_samples": 7,
            "learning_rate": 0.2,
            "signal_threshold": 0.6,
            "load_predictor": True,
            "mark_shadow_validated": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["mode"] == "learning_manifest_shadow_validation"
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["evaluation_split_names"] == ["test"]
    assert payload["training"]["model_registry"]["registered"] is True
    assert payload["shadow_dataset"]["row_count"] == 10
    assert payload["champion_shadow_report"]["comparison_report_id"] == "champion_shadow_report_test"
    assert payload["shadow_online_v2_report"]["report_id"] == "shadow_online_v2_report_test"
    assert payload["registry_lifecycle"]["updated"] is True


def test_model_registry_promotion_gate_endpoint_returns_gate_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/models/registry/promotion-gate",
        json={
            "model_id": "model_shadow_test",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 10,
            "max_samples": 20,
            "min_samples": 7,
            "learning_rate": 0.2,
            "signal_threshold": 0.6,
            "preview_limit": 4,
            "min_shadow_v2_minus_champion_return": -0.01,
            "max_shadow_v2_brier_delta": 0.03,
            "max_shadow_v2_logloss_delta": 0.08,
            "max_signal_divergence_ratio": 0.25,
            "approve_if_passed": True,
            "block_if_failed": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["mode"] == "learning_model_promotion_gate"
    assert payload["status"] == "pass"
    assert payload["accepted"] is True
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["evaluation_split_names"] == ["test"]
    assert payload["gate_thresholds"]["min_samples"] == 7
    assert payload["gate_thresholds"]["max_signal_divergence_ratio"] == 0.25
    assert payload["registry_transition"]["updated"] is True
    assert payload["registry_transition"]["action"] == "approved"


def test_train_learning_manifest_shadow_promote_endpoint_returns_workflow_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/train/learning-manifest/shadow-promote",
        json={
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "artifact_path": "tmp/manifest_model.json",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 10,
            "include_rows": True,
            "preview_limit": 3,
            "max_samples": 20,
            "min_samples": 7,
            "learning_rate": 0.2,
            "signal_threshold": 0.6,
            "load_predictor": True,
            "mark_shadow_validated": True,
            "min_shadow_v2_minus_champion_return": -0.01,
            "max_shadow_v2_brier_delta": 0.03,
            "max_shadow_v2_logloss_delta": 0.08,
            "max_signal_divergence_ratio": 0.25,
            "approve_if_passed": True,
            "block_if_failed": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["mode"] == "learning_manifest_shadow_promotion_gate"
    assert payload["status"] == "pass"
    assert payload["accepted"] is True
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["evaluation_split_names"] == ["test"]
    assert payload["final_lifecycle_state"] == "approved"
    assert payload["shadow_validation"]["mode"] == "learning_manifest_shadow_validation"
    assert payload["promotion_gate"]["mode"] == "learning_model_promotion_gate"
    assert payload["promotion_gate"]["registry_transition"]["updated"] is True


def test_learning_model_proposal_create_endpoint_returns_proposal_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/learning/models/proposal",
        json={
            "model_id": "model_shadow_test",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 10,
            "max_samples": 20,
            "min_samples": 7,
            "learning_rate": 0.2,
            "signal_threshold": 0.6,
            "preview_limit": 4,
            "min_shadow_v2_minus_champion_return": -0.01,
            "max_shadow_v2_brier_delta": 0.03,
            "max_shadow_v2_logloss_delta": 0.08,
            "max_signal_divergence_ratio": 0.25,
            "approve_if_passed": True,
            "allow_warn_status": True,
            "source_trace_id": "api-proposal-create",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["mode"] == "learning_model_promotion_proposal"
    assert payload["proposal"]["proposal_id"] == "LRN-PRP-0001"
    assert payload["proposal"]["shadow_model_id"] == "model_shadow_test"
    assert payload["proposal"]["allow_warn_status"] is True
    assert payload["proposal"]["gate_thresholds"]["max_signal_divergence_ratio"] == 0.25


def test_train_learning_manifest_shadow_proposal_endpoint_returns_workflow_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.post(
        "/train/learning-manifest/shadow-proposal",
        json={
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "artifact_path": "tmp/manifest_model.json",
            "champion_model_id": "model_champion_test",
            "split_names": ["test"],
            "max_rows": 10,
            "include_rows": True,
            "preview_limit": 3,
            "max_samples": 20,
            "min_samples": 7,
            "learning_rate": 0.2,
            "signal_threshold": 0.6,
            "load_predictor": True,
            "mark_shadow_validated": True,
            "min_shadow_v2_minus_champion_return": -0.01,
            "max_shadow_v2_brier_delta": 0.03,
            "max_shadow_v2_logloss_delta": 0.08,
            "max_signal_divergence_ratio": 0.25,
            "approve_if_passed": False,
            "allow_warn_status": True,
            "source_trace_id": "api-shadow-proposal",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["mode"] == "learning_manifest_shadow_proposal"
    assert payload["status"] == "generated"
    assert payload["proposal"]["proposal_id"] == "LRN-PRP-0002"
    assert payload["workflow"]["load_predictor"] is True
    assert payload["workflow"]["allow_warn_status"] is True


def test_learning_model_proposal_views_and_approval_endpoints_return_payloads(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    latest_response = client.get("/learning/models/proposal/latest")
    history_response = client.get(
        "/learning/models/proposal/history",
        params={"limit": 5, "proposal_id": "LRN-PRP-0001", "status": "generated"},
    )
    approval_response = client.post(
        "/learning/models/proposal/approval",
        json={
            "approver": "risk_committee",
            "approved": True,
            "proposal_id": "LRN-PRP-0001",
            "note": "gate passed",
            "now": "2026-03-30T10:00:00",
            "source_trace_id": "api-proposal-approval",
        },
    )
    approval_latest_response = client.get("/learning/models/proposal/approval/latest")
    approval_history_response = client.get(
        "/learning/models/proposal/approval/history",
        params={"limit": 5},
    )

    assert latest_response.status_code == 200
    assert latest_response.json()["proposal"]["proposal_id"] == "LRN-PRP-0001"
    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert history_payload["records"] == 1
    assert history_payload["filters"]["proposal_id"] == "LRN-PRP-0001"
    assert approval_response.status_code == 200
    approval_payload = approval_response.json()
    assert approval_payload["accepted"] is True
    assert approval_payload["record"]["approval_id"] == "LRN-APR-0001"
    assert approval_payload["proposal"]["status"] == "approved"
    assert approval_latest_response.status_code == 200
    assert approval_latest_response.json()["record"]["approval_id"] == "LRN-APR-0001"
    assert approval_history_response.status_code == 200
    assert approval_history_response.json()["records"] == 1


def test_learning_model_release_ticket_endpoints_return_payloads(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    issue_response = client.post(
        "/learning/models/release/ticket",
        json={
            "operator": "release_manager",
            "proposal_id": "LRN-PRP-0001",
            "note": "manual release",
            "now": "2026-03-30T10:05:00",
            "source_trace_id": "api-ticket-issue",
        },
    )
    execute_response = client.post(
        "/learning/models/release/ticket/execute",
        json={
            "executor": "release_manager",
            "ticket_id": "LRN-TKT-0001",
            "note": "release done",
            "confirm_window": True,
            "now": "2026-03-30T10:10:00",
            "source_trace_id": "api-ticket-execute",
        },
    )
    confirm_response = client.post(
        "/learning/models/release/ticket/confirm",
        json={
            "confirmer": "risk_committee",
            "ticket_id": "LRN-TKT-0001",
            "note": "checks passed",
            "now": "2026-03-30T10:15:00",
            "source_trace_id": "api-ticket-confirm",
        },
    )
    latest_response = client.get("/learning/models/release/ticket/latest")
    history_response = client.get(
        "/learning/models/release/ticket/history",
        params={"limit": 5},
    )
    timeline_response = client.get(
        "/learning/models/release/ticket/timeline",
        params={"ticket_id": "LRN-TKT-0001", "status": "confirmed", "limit": 5},
    )

    assert issue_response.status_code == 200
    issue_payload = issue_response.json()
    assert issue_payload["accepted"] is True
    assert issue_payload["ticket"]["ticket_id"] == "LRN-TKT-0001"
    assert issue_payload["proposal"]["status"] == "ticket_issued"
    assert execute_response.status_code == 200
    execute_payload = execute_response.json()
    assert execute_payload["ticket"]["status"] == "executed"
    assert execute_payload["ticket"]["pending_confirmation"]["state"] == "pending"
    assert confirm_response.status_code == 200
    confirm_payload = confirm_response.json()
    assert confirm_payload["ticket"]["status"] == "confirmed"
    assert latest_response.status_code == 200
    assert latest_response.json()["ticket"]["status"] == "confirmed"
    assert history_response.status_code == 200
    assert history_response.json()["records"] == 1
    assert timeline_response.status_code == 200
    timeline_payload = timeline_response.json()
    assert timeline_payload["tickets"][0]["latest_status"] == "confirmed"
    assert timeline_payload["tickets"][0]["events"][2]["event"] == "confirmed"


def test_learning_model_revoke_and_release_watchdog_endpoints_return_payloads(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    revoke_response = client.post(
        "/learning/models/proposal/revoke",
        json={
            "revoked_by": "risk_committee",
            "proposal_id": "LRN-PRP-0001",
            "note": "halt rollout",
            "revoke_model": True,
            "now": "2026-03-30T10:20:00",
            "source_trace_id": "api-proposal-revoke",
        },
    )
    rollback_response = client.post(
        "/learning/models/release/ticket/rollback",
        json={
            "rollback_by": "risk_committee",
            "ticket_id": "LRN-TKT-0001",
            "note": "rollback",
            "now": "2026-03-30T10:25:00",
            "source_trace_id": "api-ticket-rollback",
        },
    )
    watchdog_response = client.post(
        "/learning/models/release/confirmation/watchdog",
        json={
            "now": "2026-03-30T10:30:00",
            "source_trace_id": "api-learning-watchdog",
        },
    )

    assert revoke_response.status_code == 200
    revoke_payload = revoke_response.json()
    assert revoke_payload["accepted"] is True
    assert revoke_payload["proposal"]["status"] == "revoked"
    assert revoke_payload["compliance_update"]["state"] == "invalidated"
    assert rollback_response.status_code == 200
    rollback_payload = rollback_response.json()
    assert rollback_payload["accepted"] is True
    assert rollback_payload["ticket"]["status"] == "rolled_back"
    assert rollback_payload["compliance_update"]["state"] == "rolled_back"
    assert watchdog_response.status_code == 200
    watchdog_payload = watchdog_response.json()
    assert watchdog_payload["checked"] == 1
    assert watchdog_payload["overdue"] == 1
    assert watchdog_payload["rolled_back"] == 1


def test_learning_model_governance_status_endpoint_returns_monitoring_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "_service", _FakeLearningBackfillService())
    client = TestClient(main_module.app)

    response = client.get(
        "/learning/models/governance/status",
        params={"proposal_limit": 5, "ticket_limit": 7},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["proposal_summary"]["records"] == 1
    assert payload["ticket_summary"]["records"] == 1
    assert payload["monitoring"]["pending_approval"] == 1
    assert payload["config"]["release_confirmation_ttl_days"] == 3
    assert payload["limits"]["proposal_limit"] == 5
    assert payload["limits"]["ticket_limit"] == 7
