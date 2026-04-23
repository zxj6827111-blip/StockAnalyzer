from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import stock_analyzer.runtime.services.learning_governance_service as learning_governance_module
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.models.registry import ModelRegistryReadError
from stock_analyzer.runtime.service import StockAnalyzerService


class FailingBarsProvider:
    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> object:
        raise AssertionError(f"bars fallback should not run for {symbol}")

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> object:
        raise AssertionError(f"intraday fallback should not run for {symbol}:{interval}")


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _load_test_config(base_dir: Path | None = None) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.week5.auto_notify = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = base_dir or (root / "tmp_learning_governance")
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state.json")
    config.training.artifact_path = str(temp_root / "protocol_model.json")
    config.training.min_samples = 20
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.evolution.auto_run = False
    config.evolution.report_dir = str(temp_root / "evolution_history")
    config.evolution.suggestions_dir = str(temp_root / "suggestions")
    config.evolution.manifest_path = str(temp_root / "run_manifest.json")
    config.evolution.compliance_db_path = str(temp_root / "compliance.duckdb")
    config.evolution.m2_state_path = str(temp_root / "m2_state.json")
    config.evolution.m3_store_dir = str(temp_root / "artifacts" / "evolution" / "m3")
    return config


def _new_service(
    config: StockAnalyzerConfig,
    provider: object | None = None,
) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    runtime_provider = provider or SyntheticProvider(seed_offset=2027)
    object.__setattr__(service, "_provider", runtime_provider)
    object.__setattr__(service._pipeline, "_provider", runtime_provider)
    object.__setattr__(service, "_realtime_provider", runtime_provider)
    if service._realtime_pipeline is not None:
        object.__setattr__(service._realtime_pipeline, "_provider", runtime_provider)
    return service


def _seed_learning_protocol_samples(
    service: StockAnalyzerService,
    *,
    symbols: list[str],
    rows_per_symbol: int,
) -> None:
    feature_record = service._feature_schema_registry.register_feature_names(
        feature_names=["feature_a", "feature_b"],
        feature_engineer_version="test",
        code_version="git:test",
    )
    label_record = service._label_policy_registry.register_from_config(service._config.labels)
    base_time = datetime.now(UTC) - timedelta(days=max(30, rows_per_symbol + 30))
    row_index = 0
    for symbol in symbols:
        for offset in range(rows_per_symbol):
            decision_time = base_time + timedelta(days=row_index)
            snapshot = SignalSnapshot(
                snapshot_id=f"{symbol}-snap-{offset:03d}",
                code_version="git:test",
                symbol=symbol,
                strategy="trend",
                decision_time=decision_time,
                feature_vector={
                    "feature_a": float((row_index % 5) / 5.0),
                    "feature_b": float((row_index % 7) - 3),
                },
                feature_schema_id=feature_record.feature_schema_id,
                feature_schema_hash=feature_record.feature_schema_hash,
                runtime_config_hash="runtime_hash_test",
                label_policy_id=label_record.label_policy_id,
                label_policy_hash=label_record.label_policy_hash,
            )
            outcome = OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=decision_time + timedelta(days=service._config.labels.horizon_days),
                realized_return=0.08 if row_index % 2 == 0 else -0.05,
                max_favorable_excursion=0.09 if row_index % 2 == 0 else 0.01,
                max_adverse_excursion=-0.01 if row_index % 2 == 0 else -0.07,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
            service._sample_store.write_snapshot(snapshot)
            service._sample_store.upsert_outcome(outcome)
            row_index += 1


def _prepare_learning_governance_service(
    tmp_path: Path,
) -> tuple[StockAnalyzerService, Mapping[str, object], str, list[dict[str, object]]]:
    config = _load_test_config(tmp_path)
    service = _new_service(config, provider=FailingBarsProvider())
    notifications: list[dict[str, object]] = []
    service.notify = lambda **kwargs: notifications.append(dict(kwargs))  # type: ignore[method-assign]
    _seed_learning_protocol_samples(
        service,
        symbols=["600000", "000001"],
        rows_per_symbol=30,
    )

    champion_training = _as_mapping(
        service.train_models(
            full_market=True,
            lookback_days=240,
            preferred_symbols=["600000", "000001"],
            artifact_path=str(tmp_path / "champion_model.json"),
        )
    )
    champion_registry = _as_mapping(champion_training["model_registry"])
    champion_model_id = str(champion_registry["model_id"])
    service.update_model_registry_lifecycle(
        model_id=champion_model_id,
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=champion_model_id,
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=champion_model_id,
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    manifest = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )
    return service, manifest, champion_model_id, notifications


def _prepare_learning_model_proposal(
    tmp_path: Path,
) -> tuple[StockAnalyzerService, Mapping[str, object], str, dict[str, object], list[dict[str, object]]]:
    service, manifest, champion_model_id, notifications = _prepare_learning_governance_service(
        tmp_path
    )
    workflow_payload = _build_forced_pass_shadow_proposal_workflow(
        service,
        dataset_manifest_id=str(manifest["dataset_manifest_id"]),
        champion_model_id=champion_model_id,
    )
    object.__setattr__(
        service,
        "run_learning_manifest_shadow_promotion_gate",
        lambda **kwargs: deepcopy(workflow_payload),
    )
    workflow = _as_mapping(
        service.run_learning_manifest_shadow_proposal(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )
    proposal = dict(_as_mapping(workflow["proposal"]))
    assert proposal["status"] == "generated"
    return service, manifest, champion_model_id, proposal, notifications


def _build_forced_pass_shadow_proposal_workflow(
    service: StockAnalyzerService,
    *,
    dataset_manifest_id: str,
    champion_model_id: str,
) -> dict[str, object]:
    shadow_validation = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=dataset_manifest_id,
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )
    shadow_model_id = str(shadow_validation["shadow_model_id"])
    gate_payload = dict(
        _as_mapping(
            service.evaluate_learning_model_promotion_gate(
                model_id=shadow_model_id,
                champion_model_id=champion_model_id,
                split_names=["test"],
                min_samples=1,
                preview_limit=3,
            )
        )
    )
    gate_payload.update(
        {
            "ok": True,
            "status": "pass",
            "accepted": True,
            "recommended_action": "approve",
            "reason_codes": ["promotion_gate_passed"],
            "blockers": [],
            "warnings": [],
            "errors": [],
            "registry_transition": {
                "updated": False,
                "action": "noop",
                "reason": "test_forced_pass",
                "records": [],
            },
        }
    )
    shadow_entry = service._model_registry.get_by_id(shadow_model_id)
    assert shadow_entry is not None
    return {
        "ok": True,
        "mode": "learning_manifest_shadow_promotion_gate",
        "status": "pass",
        "accepted": True,
        "recommended_action": "approve",
        "dataset_manifest_id": dataset_manifest_id,
        "shadow_model_id": shadow_model_id,
        "champion_model_id": champion_model_id,
        "evaluation_split_names": ["test"],
        "final_lifecycle_state": shadow_entry.lifecycle_state.value,
        "final_role": shadow_entry.role.value,
        "shadow_validation_ok": True,
        "promotion_gate_ok": True,
        "shadow_validation": deepcopy(dict(shadow_validation)),
        "promotion_gate": gate_payload,
        "errors": [],
    }


def test_service_create_learning_model_proposal_writes_artifact_and_history(
    tmp_path: Path,
) -> None:
    service, manifest, _, _ = _prepare_learning_governance_service(tmp_path)
    shadow_bundle = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )

    proposal_result = _as_mapping(
        service.create_learning_model_proposal(
            model_id=str(shadow_bundle["shadow_model_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
        )
    )
    proposal = _as_mapping(proposal_result["proposal"])
    artifact_path = Path(str(proposal["payload_path"]))
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    history = _as_mapping(service.learning_model_proposal_history(limit=10))
    audit = _as_mapping(
        service.audit_events(limit=20, event_type="learning_model_proposal_created")
    )
    latest = service.latest_learning_model_proposal()

    assert proposal_result["accepted"] is True
    assert artifact_path.exists()
    assert payload["proposal_id"] == proposal["proposal_id"]
    assert payload["proposal_type"] == "learning_model_promotion"
    assert payload["protocol_contract"]["dataset_manifest_id"] == manifest["dataset_manifest_id"]
    assert proposal["status"] == "generated"
    assert proposal["gate_status"] == "pass"
    assert _as_mapping(proposal["compliance"])["generated"]["state"] == "generated"
    assert _as_mapping(proposal["compliance"])["validated"]["state"] == "validated"
    assert latest is not None
    assert latest["proposal_id"] == proposal["proposal_id"]
    assert int(history["records"]) >= 1
    assert int(audit["records"]) >= 1


def test_service_run_learning_manifest_shadow_proposal_builds_workflow_bundle(
    tmp_path: Path,
) -> None:
    service, manifest, champion_model_id, _ = _prepare_learning_governance_service(tmp_path)
    workflow_payload = _build_forced_pass_shadow_proposal_workflow(
        service,
        dataset_manifest_id=str(manifest["dataset_manifest_id"]),
        champion_model_id=champion_model_id,
    )
    object.__setattr__(
        service,
        "run_learning_manifest_shadow_promotion_gate",
        lambda **kwargs: deepcopy(workflow_payload),
    )

    payload = _as_mapping(
        service.run_learning_manifest_shadow_proposal(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )
    workflow = _as_mapping(payload["workflow"])
    proposal = _as_mapping(payload["proposal"])
    artifact_path = Path(str(proposal["payload_path"]))
    audit = _as_mapping(
        service.audit_events(limit=20, event_type="learning_manifest_shadow_proposal")
    )

    assert payload["ok"] is True
    assert payload["mode"] == "learning_manifest_shadow_proposal"
    assert payload["status"] == "generated"
    assert payload["accepted"] is True
    assert payload["dataset_manifest_id"] == manifest["dataset_manifest_id"]
    assert payload["champion_model_id"] == champion_model_id
    assert payload["shadow_model_id"] == proposal["shadow_model_id"]
    assert workflow["mode"] == "learning_manifest_shadow_promotion_gate"
    assert _as_mapping(workflow["promotion_gate"])["status"] == "pass"
    assert proposal["dataset_manifest_id"] == manifest["dataset_manifest_id"]
    assert proposal["status"] == "generated"
    assert artifact_path.exists()
    assert int(audit["records"]) >= 1


def test_service_shadow_proposal_auto_promotion_binds_generated_proposal_and_ticket_ids(
    tmp_path: Path,
) -> None:
    service, manifest, champion_model_id, _ = _prepare_learning_governance_service(tmp_path)
    workflow_payload = _build_forced_pass_shadow_proposal_workflow(
        service,
        dataset_manifest_id=str(manifest["dataset_manifest_id"]),
        champion_model_id=champion_model_id,
    )
    object.__setattr__(
        service,
        "run_learning_manifest_shadow_promotion_gate",
        lambda **kwargs: deepcopy(workflow_payload),
    )
    governance = service._learning_governance_service
    captured: dict[str, str] = {}

    original_approval = governance.record_learning_model_proposal_approval
    original_issue = governance.issue_learning_model_release_ticket
    original_execute = governance.execute_learning_model_release_ticket

    def _wrapped_approval(*args: object, **kwargs: object) -> dict[str, object]:
        captured["approval_proposal_id"] = str(kwargs.get("proposal_id", "")).strip()
        return original_approval(*args, **kwargs)

    def _wrapped_issue(*args: object, **kwargs: object) -> dict[str, object]:
        captured["ticket_proposal_id"] = str(kwargs.get("proposal_id", "")).strip()
        return original_issue(*args, **kwargs)

    def _wrapped_execute(*args: object, **kwargs: object) -> dict[str, object]:
        captured["execute_ticket_id"] = str(kwargs.get("ticket_id", "")).strip()
        return original_execute(*args, **kwargs)

    object.__setattr__(governance, "record_learning_model_proposal_approval", _wrapped_approval)
    object.__setattr__(governance, "issue_learning_model_release_ticket", _wrapped_issue)
    object.__setattr__(governance, "execute_learning_model_release_ticket", _wrapped_execute)

    payload = _as_mapping(
        service.run_learning_manifest_shadow_proposal(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
            auto_approve=True,
            auto_release=True,
            auto_reload_predictor=False,
        )
    )

    proposal = _as_mapping(payload["proposal"])
    auto_promotion = _as_mapping(payload["auto_promotion"])

    assert captured["approval_proposal_id"] == str(proposal["proposal_id"])
    assert captured["ticket_proposal_id"] == str(proposal["proposal_id"])
    assert captured["execute_ticket_id"] == str(auto_promotion["ticket_id"])
    assert auto_promotion["auto_approve"] is True
    assert auto_promotion["auto_release"] is True


def test_service_shadow_proposal_notifies_rejection_when_gate_is_blocked(
    tmp_path: Path,
) -> None:
    service, manifest, champion_model_id, notifications = _prepare_learning_governance_service(tmp_path)
    governance = service._learning_governance_service
    before_notifications = len(notifications)

    blocked_workflow = {
        "ok": True,
        "mode": "learning_manifest_shadow_promotion_gate",
        "status": "fail",
        "accepted": False,
        "dataset_manifest_id": str(manifest["dataset_manifest_id"]),
        "shadow_model_id": "model_shadow_test",
        "champion_model_id": champion_model_id,
        "evaluation_split_names": ["test"],
        "shadow_validation_ok": True,
        "promotion_gate_ok": False,
        "shadow_validation": {
            "ok": True,
            "training": {
                "ok": True,
                "artifact_path": str(tmp_path / "shadow_model.json"),
            },
        },
        "promotion_gate": {
            "ok": False,
            "status": "fail",
            "accepted": False,
            "reason_codes": ["shadow_underperform"],
            "errors": ["shadow_underperform"],
        },
        "errors": ["shadow_underperform"],
    }

    def _fake_run_shadow_gate(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return dict(blocked_workflow)

    def _fake_create_proposal(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "accepted": False,
            "proposal": {
                "proposal_id": "",
                "status": "rejected",
                "gate_status": "fail",
            },
            "errors": ["shadow_underperform"],
        }

    object.__setattr__(service, "run_learning_manifest_shadow_promotion_gate", _fake_run_shadow_gate)
    object.__setattr__(
        governance,
        "_create_learning_model_proposal_from_gate_payload",
        _fake_create_proposal,
    )

    payload = _as_mapping(
        service.run_learning_manifest_shadow_proposal(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            auto_approve=True,
            notify_on_rejection=True,
        )
    )

    auto_promotion = _as_mapping(payload["auto_promotion"])

    assert payload["ok"] is False
    assert auto_promotion["enabled"] is True
    assert auto_promotion["rejection_notified"] is True
    assert len(notifications) == before_notifications + 1
    assert notifications[-1]["title"] == "Learning gate rejected"


def test_service_learning_model_release_flow_updates_registry_timeline_and_status(
    tmp_path: Path,
) -> None:
    service, _, champion_model_id, proposal, notifications = _prepare_learning_model_proposal(
        tmp_path
    )
    proposal_id = str(proposal["proposal_id"])
    shadow_model_id = str(proposal["shadow_model_id"])

    approval = _as_mapping(
        service.record_learning_model_proposal_approval(
            "risk_committee",
            True,
            proposal_id=proposal_id,
            note="gate passed",
        )
    )
    ticket_payload = _as_mapping(
        service.issue_learning_model_release_ticket(
            "release_manager",
            proposal_id=proposal_id,
            note="manual release",
        )
    )
    ticket = _as_mapping(ticket_payload["ticket"])
    execute = _as_mapping(
        service.execute_learning_model_release_ticket(
            "release_manager",
            ticket_id=str(ticket["ticket_id"]),
            note="release done",
        )
    )
    confirm = _as_mapping(
        service.confirm_learning_model_release_ticket(
            "risk_committee",
            ticket_id=str(ticket["ticket_id"]),
            note="checks passed",
        )
    )
    timeline_before_rollback = _as_mapping(
        service.learning_model_release_ticket_timeline(ticket_id=str(ticket["ticket_id"]), limit=20)
    )
    rollback = _as_mapping(
        service.rollback_learning_model_release_ticket(
            "risk_committee",
            ticket_id=str(ticket["ticket_id"]),
            note="post-check drift",
        )
    )
    timeline_after_rollback = _as_mapping(
        service.learning_model_release_ticket_timeline(ticket_id=str(ticket["ticket_id"]), limit=20)
    )
    status = _as_mapping(service.learning_model_governance_status())
    active_champion = _as_mapping(status["active_champion"])
    latest_ticket = service.latest_learning_model_release_ticket()
    current_shadow_entry = _as_mapping(service.model_registry_entry(model_id=shadow_model_id))
    current_champion_entry = _as_mapping(service.model_registry_entry(model_id=champion_model_id))
    approval_record = _as_mapping(approval["record"])
    executed_ticket = _as_mapping(execute["ticket"])
    confirmed_ticket = _as_mapping(confirm["ticket"])
    rolled_back_ticket = _as_mapping(rollback["ticket"])
    timeline_events_before = cast(
        list[object],
        _as_mapping(cast(list[object], timeline_before_rollback["tickets"])[0])["events"],
    )
    timeline_events_after = cast(
        list[object],
        _as_mapping(cast(list[object], timeline_after_rollback["tickets"])[0])["events"],
    )

    assert approval["accepted"] is True
    assert approval_record["approved"] is True
    assert _as_mapping(approval_record["registry_transition"])["action"] == "approved"
    assert ticket_payload["accepted"] is True
    assert ticket["status"] == "issued"
    assert execute["accepted"] is True
    assert executed_ticket["status"] == "executed"
    assert _as_mapping(executed_ticket["pending_confirmation"])["state"] == "pending"
    assert confirm["accepted"] is True
    assert confirmed_ticket["status"] == "confirmed"
    assert rollback["accepted"] is True
    assert rolled_back_ticket["status"] == "rolled_back"
    assert current_shadow_entry["role"] == "challenger"
    assert current_shadow_entry["lifecycle_state"] == "revoked"
    assert current_champion_entry["role"] == "champion"
    assert active_champion["model_id"] == champion_model_id
    assert latest_ticket is not None
    assert latest_ticket["status"] == "rolled_back"
    assert _as_mapping(status["ticket_summary"])["status_breakdown"]["rolled_back"] == 1
    assert _as_mapping(status["monitoring"])["revoked_model_count"] >= 1
    assert any(_as_mapping(item)["event"] == "confirmed" for item in timeline_events_before)
    assert any(_as_mapping(item)["event"] == "rolled_back" for item in timeline_events_after)
    assert len(notifications) >= 3


def test_service_learning_governance_status_degrades_when_model_registry_is_locked(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _prepare_learning_governance_service(tmp_path)

    def _raise_registry_read_error(*args: object, **kwargs: object) -> list[object]:
        raise ModelRegistryReadError(
            'Could not set lock on file "/app/artifacts/training/learning_protocol.duckdb"'
        )

    object.__setattr__(service._model_registry, "list_records", _raise_registry_read_error)

    status = _as_mapping(service.learning_model_governance_status())
    registry = _as_mapping(status["model_registry"])
    monitoring = _as_mapping(status["monitoring"])

    assert status["status"] == "degraded"
    assert registry["status"] == "degraded"
    assert registry["reason"] == "model_registry_unavailable"
    assert monitoring["model_registry_status"] == "degraded"
    assert monitoring["blocked_model_count"] == 0
    assert monitoring["revoked_model_count"] == 0


def test_service_revoke_learning_model_proposal_revokes_model_and_blocks_ticket_issue(
    tmp_path: Path,
) -> None:
    service, _, _, proposal, _ = _prepare_learning_model_proposal(tmp_path)
    proposal_id = str(proposal["proposal_id"])
    shadow_model_id = str(proposal["shadow_model_id"])

    approval = _as_mapping(
        service.record_learning_model_proposal_approval(
            "risk_committee",
            True,
            proposal_id=proposal_id,
            note="approve then revoke",
        )
    )
    revoked = _as_mapping(
        service.revoke_learning_model_proposal(
            "risk_committee",
            proposal_id=proposal_id,
            note="halt rollout",
            revoke_model=True,
        )
    )
    current_shadow_entry = _as_mapping(service.model_registry_entry(model_id=shadow_model_id))
    ticket_attempt = service.issue_learning_model_release_ticket(
        "release_manager",
        proposal_id=proposal_id,
        note="should fail",
    )

    assert approval["accepted"] is True
    assert revoked["accepted"] is True
    assert _as_mapping(revoked["proposal"])["status"] == "revoked"
    assert _as_mapping(revoked["proposal"])["release_state"] == "revoked"
    assert _as_mapping(revoked["compliance_update"])["state"] == "invalidated"
    assert current_shadow_entry["lifecycle_state"] == "revoked"
    assert ticket_attempt["accepted"] is False
    assert ticket_attempt["code"] == "proposal_not_approved"


def test_service_learning_release_watchdog_auto_rolls_back_overdue_ticket(
    tmp_path: Path,
) -> None:
    service, _, champion_model_id, proposal, _ = _prepare_learning_model_proposal(tmp_path)
    proposal_id = str(proposal["proposal_id"])
    shadow_model_id = str(proposal["shadow_model_id"])

    approval = _as_mapping(
        service.record_learning_model_proposal_approval(
            "risk_committee",
            True,
            proposal_id=proposal_id,
            note="approve",
        )
    )
    ticket_payload = _as_mapping(
        service.issue_learning_model_release_ticket(
            "release_manager",
            proposal_id=proposal_id,
            note="manual release",
            timestamp=datetime(2026, 4, 1, 9, 10, tzinfo=UTC),
        )
    )
    execute = _as_mapping(
        service.execute_learning_model_release_ticket(
            "release_manager",
            ticket_id=str(_as_mapping(ticket_payload["ticket"])["ticket_id"]),
            note="release done",
            timestamp=datetime(2026, 4, 1, 9, 15, tzinfo=UTC),
        )
    )
    pending_confirmation = _as_mapping(_as_mapping(execute["ticket"])["pending_confirmation"])
    due_at = datetime.fromisoformat(str(pending_confirmation["due_at"]))
    watchdog = _as_mapping(
        service.run_learning_model_release_confirmation_watchdog(
            now=due_at + timedelta(minutes=1),
            source_trace_id="learning-watchdog-test",
        )
    )
    latest_ticket = service.latest_learning_model_release_ticket()
    current_shadow_entry = _as_mapping(service.model_registry_entry(model_id=shadow_model_id))
    current_champion_entry = _as_mapping(service.model_registry_entry(model_id=champion_model_id))
    audit = _as_mapping(
        service.audit_events(
            limit=20,
            event_type="learning_model_release_confirmation_watchdog",
        )
    )

    assert approval["accepted"] is True
    assert execute["accepted"] is True
    assert int(watchdog["checked"]) >= 1
    assert int(watchdog["overdue"]) == 1
    assert int(watchdog["rolled_back"]) == 1
    assert latest_ticket is not None
    assert latest_ticket["status"] == "rolled_back"
    assert current_shadow_entry["lifecycle_state"] == "revoked"
    assert current_champion_entry["role"] == "champion"
    assert int(audit["records"]) >= 1


def test_service_learning_governance_compliance_fallback_appends_jsonl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, manifest, _, _ = _prepare_learning_governance_service(tmp_path)
    shadow_bundle = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )

    def _raise_log_event(*args: object, **kwargs: object) -> str:
        raise RuntimeError("duckdb unavailable")

    monkeypatch.setattr(
        learning_governance_module.ComplianceLogger,
        "log_event",
        _raise_log_event,
    )
    fallback_path = service._resolve_evolution_path(
        "artifacts/evolution/learning_compliance_fallback.jsonl"
    )
    existing_lines = (
        fallback_path.read_text(encoding="utf-8").splitlines() if fallback_path.exists() else []
    )

    first = _as_mapping(
        service.create_learning_model_proposal(
            model_id=str(shadow_bundle["shadow_model_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
        )
    )
    second = _as_mapping(
        service.create_learning_model_proposal(
            model_id=str(shadow_bundle["shadow_model_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
        )
    )
    first_compliance = _as_mapping(_as_mapping(_as_mapping(first["proposal"])["compliance"])["generated"])
    second_compliance = _as_mapping(_as_mapping(_as_mapping(second["proposal"])["compliance"])["validated"])
    fallback_path = Path(str(first_compliance["fallback_path"]))
    lines = fallback_path.read_text(encoding="utf-8").splitlines()
    new_lines = lines[len(existing_lines) :]

    assert first_compliance["written"] is False
    assert second_compliance["written"] is False
    assert fallback_path.exists()
    assert len(new_lines) == 4
    for line in new_lines:
        payload = json.loads(line)
        assert payload["metadata"]["workflow"] == "learning_model_governance"
