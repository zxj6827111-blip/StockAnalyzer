from __future__ import annotations

import json
from types import SimpleNamespace

from pytest import MonkeyPatch
from typer.testing import CliRunner

import stock_analyzer.cli as cli_module


class _FakeService:
    def __init__(self, config: object) -> None:
        self._config = config

    def recommendation_lifecycle(self, status: str, limit: int) -> dict[str, object]:
        return {
            "records": 1,
            "status_filter": status or "all",
            "summary": {"records": 1, "status_breakdown": {"watching": 1}},
            "items": [{"symbol": "600000", "status": "watching", "strategy": "trend"}],
            "limit": limit,
        }

    def holding_alerts(self) -> dict[str, object]:
        return {
            "records": 2,
            "summary": {"warn": 1, "info": 1},
            "items": [
                {"symbol": "600000", "severity": "warn", "reason": "stop_loss_threshold_reached"},
                {"symbol": "000001", "severity": "info", "reason": "take_profit_threshold_reached"},
            ],
        }

    def execution_bias_report(self, days: int, limit: int) -> dict[str, object]:
        return {
            "records": 1,
            "summary": {"avg_abs_position_bias": 0.01, "avg_abs_price_bias_pct": 0.012},
            "items": [{"symbol": "600000", "position_bias": 0.01, "price_bias_pct": 0.012}],
            "days": days,
            "limit": limit,
        }

    def runtime_history_archive_status(self, limit: int) -> dict[str, object]:
        return {
            "enabled": True,
            "archive_dir": "artifacts/runtime/history",
            "retention_days": 30,
            "records": 1,
            "files": [
                {
                    "day": "20260305",
                    "path": "artifacts/runtime/history/runtime_history_20260305.json",
                }
            ],
            "limit": limit,
        }

    def archive_runtime_history(self, now: object, force: bool) -> dict[str, object]:
        return {
            "archived": True,
            "day": "20260305",
            "path": "artifacts/runtime/history/runtime_history_20260305.json",
            "force": force,
        }

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
            "archive_dir": archive_dir or "artifacts/runtime/history",
            "symbols": symbols or [],
            "build_manifest": build_manifest,
            "processed_archives": 2,
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "included_snapshot_count": 24,
            "backfill": {"ok": True, "processed_archives": 2},
            "manifest": {"ok": True, "dataset_manifest_id": "dataset_manifest_v1_test"},
            "calibration_ratio": calibration_ratio,
            "test_ratio": test_ratio,
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
                "shadow_model_id": model_id,
                "champion_model_id": champion_model_id or "model_champion_test",
                "evaluation_split_names": split_names or ["test"],
                "payload_uri": "suggestions/learning_model_governance/proposals/LRN-PRP-0001.json",
                "allow_warn_status": allow_warn_status,
                "source_trace_id": source_trace_id,
                "gate_thresholds": {
                    "max_rows": max_rows,
                    "max_samples": max_samples,
                    "min_samples": min_samples,
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
            "promotion_gate": {"ok": True, "status": "pass"},
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
                "payload_uri": "suggestions/learning_model_governance/proposals/LRN-PRP-0002.json",
            },
            "proposal_result": {"accepted": True},
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
            "filters": {"limit": limit, "proposal_id": proposal_id, "status": status},
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
            "proposal": {"proposal_id": proposal_id or "LRN-PRP-0001", "status": "ticket_issued"},
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
                "pending_confirmation": {"required": True, "state": "pending"},
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
            "filters": {"ticket_id": ticket_id, "status": status, "limit": limit},
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
            "compliance_update": {"state": "invalidated", "written": True},
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
            "proposal_summary": {
                "records": 1,
                "pending_approval": 1,
                "history_count": 2,
            },
            "ticket_summary": {
                "records": 1,
                "pending_confirmation": 0,
                "history_count": 3,
            },
            "monitoring": {
                "pending_approval": 1,
                "revoked_model_count": 0,
            },
            "config": {
                "release_confirmation_required": True,
                "release_confirmation_ttl_days": 3,
            },
            "limits": {
                "proposal_limit": proposal_limit,
                "ticket_limit": ticket_limit,
            },
        }

    def execute_command(self, envelope: object) -> dict[str, object]:
        return {
            "accepted": True,
            "action": str(getattr(envelope, "action", "")),
            "payload": dict(getattr(envelope, "payload", {})),
        }


def _fake_config() -> object:
    return SimpleNamespace(
        command_channel=SimpleNamespace(
            secret_key="unit-test-secret",
        )
    )


def test_cli_recommendation_lifecycle_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["recommendation-lifecycle", "--status", "watching", "--limit", "20"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["records"] == 1
    assert payload["status_filter"] == "watching"
    assert payload["limit"] == 20


def test_cli_recommendation_status_set_outputs_signed_result(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "recommendation-status-set",
            "--symbol",
            "600000",
            "--status",
            "watching",
            "--strategy",
            "trend",
            "--note",
            "wait setup",
            "--command-id",
            "cmd-rec-001",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command_id"] == "cmd-rec-001"
    assert payload["result"]["accepted"] is True
    assert payload["result"]["action"] == "SET_RECOMMENDATION_STATUS"
    assert payload["result"]["payload"]["symbol"] == "600000"
    assert payload["result"]["payload"]["status"] == "watching"
    assert payload["result"]["payload"]["note"] == "wait setup"


def test_cli_portfolio_holding_alerts_supports_severity_filter(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["portfolio-holding-alerts", "--severity", "warn"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["severity_filter"] == "warn"
    assert payload["records"] == 1
    assert payload["summary"]["warn"] == 1
    assert payload["summary"]["info"] == 0
    assert payload["items"][0]["symbol"] == "600000"


def test_cli_portfolio_execution_bias_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["portfolio-execution-bias", "--days", "14", "--limit", "50"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["records"] == 1
    assert payload["days"] == 14
    assert payload["limit"] == 50


def test_cli_runtime_history_archive_status_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["runtime-history-archive-status", "--limit", "10"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["enabled"] is True
    assert payload["records"] == 1
    assert payload["limit"] == 10


def test_cli_runtime_history_archive_run_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["runtime-history-archive-run", "--force"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["archived"] is True
    assert payload["force"] is True


def test_cli_learning_runtime_history_bootstrap_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "learning-runtime-history-bootstrap",
            "--archive-dir",
            "tmp/history",
            "--symbols",
            "600000,000001",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["archive_dir"] == "tmp/history"
    assert payload["symbols"] == ["600000", "000001"]
    assert payload["dataset_manifest_id"] == "dataset_manifest_v1_test"


def test_cli_learning_status_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["learning-status", "--manifest-limit", "3"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cold_start_ready"] is True
    assert payload["sample_store"]["dataset_manifests"] == 1
    assert payload["manifests"]["latest"]["dataset_manifest_id"] == "dataset_manifest_v1_test"
    assert payload["governance"]["proposal_summary"]["records"] == 1
    assert payload["governance"]["config"]["release_confirmation_required"] is True


def test_cli_train_learning_manifest_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "train-learning-manifest",
            "--dataset-manifest-id",
            "dataset_manifest_v1_test",
            "--artifact-path",
            "tmp/manifest_model.json",
            "--load-predictor",
            "--register-model",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["input_mode"] == "dataset_manifest"
    assert payload["dataset_manifest_id"] == "dataset_manifest_v1_test"
    assert payload["artifact_path"] == "tmp/manifest_model.json"
    assert payload["predictor_loaded"] is True
    assert payload["model_registry"]["registered"] is True
    assert payload["model_registry"]["source"] == "train_learning_manifest"


def test_cli_model_registry_list_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["model-registry-list", "--limit", "5", "--role", "challenger", "--lifecycle-state", "trained"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["records"] == 1
    assert payload["items"][0]["role"] == "challenger"
    assert payload["items"][0]["lifecycle_state"] == "trained"
    assert payload["limit"] == 5


def test_cli_model_registry_entry_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["model-registry-entry", "--model-id", "model_manifest_test"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["model_id"] == "model_manifest_test"
    assert payload["dataset_manifest_id"] == "dataset_manifest_v1_test"


def test_cli_model_registry_register_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "model-registry-register",
            "--artifact-path",
            "tmp/registered_model.json",
            "--role",
            "shadow",
            "--lifecycle-state",
            "trained",
            "--source",
            "cli_test",
            "--parent-model-id",
            "champion_a",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["registered"] is True
    assert payload["role"] == "shadow"
    assert payload["source"] == "cli_test"
    assert payload["parent_model_id"] == "champion_a"


def test_cli_model_registry_set_lifecycle_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "model-registry-set-lifecycle",
            "--model-id",
            "model_manifest_test",
            "--lifecycle-state",
            "blocked",
            "--blocked-reason",
            "manual_review",
            "--timestamp",
            "2026-03-30T10:00:00",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["model_id"] == "model_manifest_test"
    assert payload["lifecycle_state"] == "blocked"
    assert payload["blocked_reason"] == "manual_review"


def test_cli_model_registry_set_role_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "model-registry-set-role",
            "--model-id",
            "model_manifest_test",
            "--role",
            "champion",
            "--timestamp",
            "2026-03-30T10:05:00",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["model_id"] == "model_manifest_test"
    assert payload["role"] == "champion"


def test_cli_shadow_dataset_build_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "shadow-dataset-build",
            "--model-id",
            "model_manifest_test",
            "--split-names",
            "test",
            "--max-rows",
            "12",
            "--include-rows",
            "--preview-limit",
            "3",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["shadow_dataset_id"] == "shadow_dataset_test"
    assert payload["requested_split_names"] == ["test"]
    assert payload["row_count"] == 12
    assert payload["include_rows"] is True


def test_cli_champion_shadow_report_build_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "champion-shadow-report-build",
            "--model-id",
            "model_shadow_test",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "9",
            "--signal-threshold",
            "0.65",
            "--preview-limit",
            "2",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["comparison_report_id"] == "champion_shadow_report_test"
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["signal_threshold"] == 0.65


def test_cli_shadow_online_v2_report_build_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "shadow-online-v2-report-build",
            "--model-id",
            "model_shadow_test",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "11",
            "--max-samples",
            "20",
            "--min-samples",
            "7",
            "--learning-rate",
            "0.2",
            "--signal-threshold",
            "0.6",
            "--preview-limit",
            "4",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report_id"] == "shadow_online_v2_report_test"
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["max_samples"] == 20
    assert payload["min_samples"] == 7
    assert payload["learning_rate"] == 0.2


def test_cli_train_execution_risk_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "train-execution-risk",
            "--artifact-path",
            "tmp/execution_risk_artifact.json",
            "--maturity-statuses",
            "reconciled",
            "--max-rows",
            "50",
            "--min-samples-per-target",
            "10",
            "--epochs",
            "120",
            "--seed",
            "9",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "execution_risk_training"
    assert payload["status"] == "trained"
    assert payload["dataset_id"] == "execution_risk_dataset_v1_test"


def test_cli_execution_aware_report_build_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "execution-aware-report-build",
            "--model-id",
            "model_shadow_test",
            "--execution-risk-artifact-path",
            "tmp/execution_risk_artifact.json",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "8",
            "--preview-limit",
            "3",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report_id"] == "execution_aware_report_test"
    assert payload["shadow_model_id"] == "model_shadow_test"
    assert payload["champion_model_id"] == "model_champion_test"
    assert payload["row_count"] == 8


def test_cli_train_learning_manifest_shadow_validate_outputs_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "train-learning-manifest-shadow-validate",
            "--dataset-manifest-id",
            "dataset_manifest_v1_test",
            "--artifact-path",
            "tmp/manifest_model.json",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "10",
            "--include-rows",
            "--preview-limit",
            "3",
            "--max-samples",
            "20",
            "--min-samples",
            "7",
            "--learning-rate",
            "0.2",
            "--signal-threshold",
            "0.6",
            "--load-predictor",
            "--mark-shadow-validated",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
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


def test_cli_model_registry_promotion_gate_outputs_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "model-registry-promotion-gate",
            "--model-id",
            "model_shadow_test",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "10",
            "--max-samples",
            "20",
            "--min-samples",
            "7",
            "--learning-rate",
            "0.2",
            "--signal-threshold",
            "0.6",
            "--preview-limit",
            "4",
            "--min-shadow-v2-minus-champion-return",
            "-0.01",
            "--max-shadow-v2-brier-delta",
            "0.03",
            "--max-shadow-v2-logloss-delta",
            "0.08",
            "--max-signal-divergence-ratio",
            "0.25",
            "--approve-if-passed",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
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


def test_cli_train_learning_manifest_shadow_promote_outputs_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "train-learning-manifest-shadow-promote",
            "--dataset-manifest-id",
            "dataset_manifest_v1_test",
            "--artifact-path",
            "tmp/manifest_model.json",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "10",
            "--include-rows",
            "--preview-limit",
            "3",
            "--max-samples",
            "20",
            "--min-samples",
            "7",
            "--learning-rate",
            "0.2",
            "--signal-threshold",
            "0.6",
            "--load-predictor",
            "--mark-shadow-validated",
            "--min-shadow-v2-minus-champion-return",
            "-0.01",
            "--max-shadow-v2-brier-delta",
            "0.03",
            "--max-shadow-v2-logloss-delta",
            "0.08",
            "--max-signal-divergence-ratio",
            "0.25",
            "--approve-if-passed",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
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


def test_cli_learning_model_proposal_create_outputs_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "learning-model-proposal-create",
            "--model-id",
            "model_shadow_test",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "10",
            "--max-samples",
            "20",
            "--min-samples",
            "7",
            "--learning-rate",
            "0.2",
            "--signal-threshold",
            "0.6",
            "--preview-limit",
            "4",
            "--max-signal-divergence-ratio",
            "0.25",
            "--approve-if-passed",
            "--source-trace-id",
            "cli-proposal-create",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["accepted"] is True
    assert payload["proposal"]["proposal_id"] == "LRN-PRP-0001"
    assert payload["proposal"]["source_trace_id"] == "cli-proposal-create"
    assert payload["proposal"]["gate_thresholds"]["max_signal_divergence_ratio"] == 0.25


def test_cli_train_learning_manifest_shadow_proposal_outputs_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "train-learning-manifest-shadow-proposal",
            "--dataset-manifest-id",
            "dataset_manifest_v1_test",
            "--artifact-path",
            "tmp/manifest_model.json",
            "--champion-model-id",
            "model_champion_test",
            "--split-names",
            "test",
            "--max-rows",
            "10",
            "--include-rows",
            "--preview-limit",
            "3",
            "--max-samples",
            "20",
            "--min-samples",
            "7",
            "--learning-rate",
            "0.2",
            "--signal-threshold",
            "0.6",
            "--load-predictor",
            "--mark-shadow-validated",
            "--allow-warn-status",
            "--source-trace-id",
            "cli-shadow-proposal",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "learning_manifest_shadow_proposal"
    assert payload["proposal"]["proposal_id"] == "LRN-PRP-0002"
    assert payload["workflow"]["load_predictor"] is True
    assert payload["workflow"]["allow_warn_status"] is True


def test_cli_learning_model_proposal_views_and_approval_output_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()

    latest = runner.invoke(cli_module.app, ["learning-model-proposal-latest"])
    history = runner.invoke(
        cli_module.app,
        [
            "learning-model-proposal-history",
            "--limit",
            "5",
            "--proposal-id",
            "LRN-PRP-0001",
            "--status",
            "generated",
        ],
    )
    approve = runner.invoke(
        cli_module.app,
        [
            "learning-model-proposal-approve",
            "--approver",
            "risk_committee",
            "--proposal-id",
            "LRN-PRP-0001",
            "--note",
            "gate passed",
            "--now",
            "2026-03-30T10:00:00",
            "--source-trace-id",
            "cli-proposal-approve",
        ],
    )
    approval_latest = runner.invoke(
        cli_module.app,
        ["learning-model-proposal-approval-latest"],
    )
    approval_history = runner.invoke(
        cli_module.app,
        ["learning-model-proposal-approval-history", "--limit", "5"],
    )

    assert latest.exit_code == 0
    assert json.loads(latest.stdout)["proposal"]["proposal_id"] == "LRN-PRP-0001"
    assert history.exit_code == 0
    history_payload = json.loads(history.stdout)
    assert history_payload["records"] == 1
    assert history_payload["filters"]["status"] == "generated"
    assert approve.exit_code == 0
    approve_payload = json.loads(approve.stdout)
    assert approve_payload["accepted"] is True
    assert approve_payload["record"]["approval_id"] == "LRN-APR-0001"
    assert approve_payload["proposal"]["status"] == "approved"
    assert approval_latest.exit_code == 0
    assert json.loads(approval_latest.stdout)["record"]["approval_id"] == "LRN-APR-0001"
    assert approval_history.exit_code == 0
    assert json.loads(approval_history.stdout)["records"] == 1


def test_cli_learning_model_release_ticket_commands_output_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()

    issue = runner.invoke(
        cli_module.app,
        [
            "learning-model-release-ticket-issue",
            "--operator",
            "release_manager",
            "--proposal-id",
            "LRN-PRP-0001",
            "--note",
            "manual release",
            "--now",
            "2026-03-30T10:05:00",
            "--source-trace-id",
            "cli-ticket-issue",
        ],
    )
    execute = runner.invoke(
        cli_module.app,
        [
            "learning-model-release-ticket-execute",
            "--executor",
            "release_manager",
            "--ticket-id",
            "LRN-TKT-0001",
            "--note",
            "release done",
            "--now",
            "2026-03-30T10:10:00",
            "--source-trace-id",
            "cli-ticket-execute",
        ],
    )
    confirm = runner.invoke(
        cli_module.app,
        [
            "learning-model-release-ticket-confirm",
            "--confirmer",
            "risk_committee",
            "--ticket-id",
            "LRN-TKT-0001",
            "--note",
            "checks passed",
            "--now",
            "2026-03-30T10:15:00",
            "--source-trace-id",
            "cli-ticket-confirm",
        ],
    )
    latest = runner.invoke(cli_module.app, ["learning-model-release-ticket-latest"])
    history = runner.invoke(
        cli_module.app,
        ["learning-model-release-ticket-history", "--limit", "5"],
    )
    timeline = runner.invoke(
        cli_module.app,
        [
            "learning-model-release-ticket-timeline",
            "--ticket-id",
            "LRN-TKT-0001",
            "--status",
            "confirmed",
            "--limit",
            "5",
        ],
    )

    assert issue.exit_code == 0
    issue_payload = json.loads(issue.stdout)
    assert issue_payload["accepted"] is True
    assert issue_payload["ticket"]["ticket_id"] == "LRN-TKT-0001"
    assert execute.exit_code == 0
    execute_payload = json.loads(execute.stdout)
    assert execute_payload["ticket"]["status"] == "executed"
    assert execute_payload["ticket"]["pending_confirmation"]["state"] == "pending"
    assert confirm.exit_code == 0
    confirm_payload = json.loads(confirm.stdout)
    assert confirm_payload["ticket"]["status"] == "confirmed"
    assert latest.exit_code == 0
    assert json.loads(latest.stdout)["ticket"]["status"] == "confirmed"
    assert history.exit_code == 0
    assert json.loads(history.stdout)["records"] == 1
    assert timeline.exit_code == 0
    timeline_payload = json.loads(timeline.stdout)
    assert timeline_payload["tickets"][0]["latest_status"] == "confirmed"
    assert timeline_payload["tickets"][0]["events"][1]["event"] == "executed"


def test_cli_learning_model_revoke_and_watchdog_commands_output_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()

    revoke = runner.invoke(
        cli_module.app,
        [
            "learning-model-proposal-revoke",
            "--revoked-by",
            "risk_committee",
            "--proposal-id",
            "LRN-PRP-0001",
            "--note",
            "halt rollout",
            "--now",
            "2026-03-30T10:20:00",
            "--source-trace-id",
            "cli-proposal-revoke",
        ],
    )
    rollback = runner.invoke(
        cli_module.app,
        [
            "learning-model-release-ticket-rollback",
            "--rollback-by",
            "risk_committee",
            "--ticket-id",
            "LRN-TKT-0001",
            "--note",
            "rollback",
            "--now",
            "2026-03-30T10:25:00",
            "--source-trace-id",
            "cli-ticket-rollback",
        ],
    )
    watchdog = runner.invoke(
        cli_module.app,
        [
            "learning-model-release-confirmation-watchdog",
            "--now",
            "2026-03-30T10:30:00",
            "--source-trace-id",
            "cli-learning-watchdog",
        ],
    )

    assert revoke.exit_code == 0
    revoke_payload = json.loads(revoke.stdout)
    assert revoke_payload["accepted"] is True
    assert revoke_payload["proposal"]["status"] == "revoked"
    assert revoke_payload["compliance_update"]["state"] == "invalidated"
    assert rollback.exit_code == 0
    rollback_payload = json.loads(rollback.stdout)
    assert rollback_payload["accepted"] is True
    assert rollback_payload["ticket"]["status"] == "rolled_back"
    assert rollback_payload["compliance_update"]["state"] == "rolled_back"
    assert watchdog.exit_code == 0
    watchdog_payload = json.loads(watchdog.stdout)
    assert watchdog_payload["checked"] == 1
    assert watchdog_payload["rolled_back"] == 1


def test_cli_learning_model_governance_status_outputs_json(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", _fake_config)
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "learning-model-governance-status",
            "--proposal-limit",
            "5",
            "--ticket-limit",
            "7",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["proposal_summary"]["records"] == 1
    assert payload["ticket_summary"]["records"] == 1
    assert payload["monitoring"]["pending_approval"] == 1
    assert payload["config"]["release_confirmation_ttl_days"] == 3
    assert payload["limits"]["proposal_limit"] == 5
    assert payload["limits"]["ticket_limit"] == 7
