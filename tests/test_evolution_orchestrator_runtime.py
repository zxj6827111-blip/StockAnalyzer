from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from stock_analyzer.config import load_config
from stock_analyzer.evolution.llm_semantic import LlmSemanticDecision
from stock_analyzer.evolution.m3_vector_profile import (
    M3_DEFAULT_VECTOR_PROFILE_ID,
    M3_LEGACY_VECTOR_PROFILE_ID,
    M3VectorProfileRegistry,
    build_default_m3_vector_profile,
    build_m3_vector_from_record,
)
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator


def _make_orchestrator(
    tmp_path: Path,
    *,
    m3_active_vector_profile_id: str | None = None,
    m3_allow_active_profile_fallback: bool | None = None,
) -> OffhoursEvolutionOrchestrator:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    if m3_active_vector_profile_id is not None:
        config.evolution.m3_active_vector_profile_id = m3_active_vector_profile_id
    if m3_allow_active_profile_fallback is not None:
        config.evolution.m3_allow_active_profile_fallback = m3_allow_active_profile_fallback
    return OffhoursEvolutionOrchestrator(
        config=config.evolution,
        project_root=tmp_path,
    )


def _as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def test_orchestrator_run_writes_manifest_and_proposal(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 40, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-evo",
        )
    )

    assert report["dry_run"] is True
    assert report["proposal"]["authorization_level"] == "C"
    assert report["m9"]["blackout_day"] is False
    assert "modules" in report
    assert "m1" in report["modules"]
    assert "m2" in report["modules"]
    assert "m5" in report["modules"]
    assert "m3" in report["modules"]
    assert "m6" in report["modules"]
    assert "m7" in report["modules"]
    assert "m10" in report["modules"]
    assert "m11" in report["modules"]
    assert "m8" in report["modules"]
    assert "m4" in report["modules"]
    assert "M4" in report["dag"]["module_scores"]
    assert "M2" in report["dag"]["module_scores"]
    assert "M5" in report["dag"]["module_scores"]
    assert "M6" in report["dag"]["module_scores"]
    assert "M7" in report["dag"]["module_scores"]
    assert "M10" in report["dag"]["module_scores"]
    assert "M11" in report["dag"]["module_scores"]
    assert "M8" in report["dag"]["module_scores"]
    runtime_controls = report["runtime_controls"]
    assert runtime_controls["source"] == "evolution"
    assert runtime_controls["source_run_id"] == report["run_id"]
    assert "m2" in runtime_controls
    assert "m4" in runtime_controls
    assert "m6" in runtime_controls
    assert "m10" in runtime_controls
    assert report["modules"]["m1"]["negative_case_count"] >= 0
    assert "reason_counts" in report["modules"]["m1"]
    assert "cases_preview" in report["modules"]["m1"]
    m3 = report["modules"]["m3"]
    assert m3["vector_profile_id"] == M3_DEFAULT_VECTOR_PROFILE_ID
    assert m3["configured_vector_profile_id"] == M3_DEFAULT_VECTOR_PROFILE_ID
    assert m3["active_vector_profile_id"] == M3_DEFAULT_VECTOR_PROFILE_ID
    assert m3["fallback_used"] is False
    assert m3["vector_dim"] == 20
    assert "constraint_pressure" in m3["feature_components"]
    m11 = report["modules"]["m11"]
    assert "redlines" in m11
    assert "attribution" in m11
    m7 = report["modules"]["m7"]
    assert str(m7.get("artifact_uri", "")).startswith("suggestions/m7/")
    assert (tmp_path / str(m7["artifact_uri"])).exists() is True

    manifest_path = Path(report["manifest_path"])
    assert manifest_path.exists() is True

    payload_uri = str(report["proposal"]["payload_uri"])
    payload_path = tmp_path / payload_uri
    assert payload_path.exists() is True
    proposal_payload = _as_mapping(json.loads(payload_path.read_text(encoding="utf-8")))
    artifacts = _as_mapping(proposal_payload.get("module_artifacts", {}))
    assert str(artifacts.get("M7", "")).startswith("suggestions/m7/")
    assert str(artifacts.get("M8", "")).startswith("suggestions/m8/")


def test_orchestrator_drill_handles_degraded_m9_path(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    report = _as_mapping(
        orchestrator.run_drill(
            now=datetime(2026, 3, 2, 20, 41, tzinfo=UTC),
            source_trace_id="drill",
        )
    )
    assert report["dry_run"] is True
    assert report["m9"]["degraded"] is True
    assert report["proposal"]["authorization_level"] == "A"
    assert report["modules"]["m4"]["status"] == "skipped_by_m9"
    assert report["modules"]["m1"]["status"] == "skipped_by_m9"
    assert report["modules"]["m2"]["status"] == "degraded_run"
    assert report["modules"]["m5"]["status"] == "skipped_by_m9"
    assert report["modules"]["m6"]["status"] == "skipped_by_m9"
    assert report["modules"]["m7"]["status"] == "skipped_by_m9"
    assert report["modules"]["m10"]["status"] == "degraded_run"
    assert report["modules"]["m11"]["status"] == "degraded_run"
    assert report["modules"]["m8"]["status"] == "skipped_by_m9"
    assert report["online_update_audit"]["status"] == "degraded_run"
    assert report["shadow_online_report"]["status"] == "degraded_run"
    assert report["shadow_online_v2_report"]["status"] == "degraded_run"
    assert report["modules"]["eval_profiles"]["status"] == "degraded_run"
    assert report["modules"]["utility_execution"]["status"] == "degraded_run"
    assert report["modules"]["reconcile_drift"]["status"] == "degraded_run"
    assert report["modules"]["hard_gates"]["status"] == "degraded_run"
    assert report["modules"]["m2"]["degraded_reason"] == "m9_failed"
    assert report["modules"]["m10"]["degraded_reason"] == "m9_failed"
    assert report["modules"]["m11"]["degraded_reason"] == "m9_failed"
    assert report["modules"]["m2"]["degraded_at"] == report["timestamp"]
    assert report["modules"]["m10"]["degraded_at"] == report["timestamp"]
    assert report["modules"]["m11"]["degraded_at"] == report["timestamp"]
    assert report["online_update_audit"]["degraded_reason"] == "m9_failed"
    assert report["shadow_online_report"]["degraded_reason"] == "m9_failed"
    assert report["shadow_online_v2_report"]["degraded_reason"] == "m9_failed"


def test_orchestrator_persists_m2_state_across_restarts(tmp_path: Path) -> None:
    records = [
        {
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.0,
            "volume": 100.0,
        },
        {
            "symbol": "000001.SZ",
            "open": 10.1,
            "high": 10.3,
            "low": 10.0,
            "close": 10.0,
            "volume": 120.0,
        },
        {
            "symbol": "300001.SZ",
            "open": 10.2,
            "high": 10.4,
            "low": 10.1,
            "close": 10.0,
            "volume": 1_000_000.0,
        },
    ]
    first = _make_orchestrator(tmp_path)
    first_report = _as_mapping(
        first.run(
            records=records,
            now=datetime(2026, 3, 2, 20, 40, tzinfo=UTC),
            dry_run=True,
            source_trace_id="test-m2-persist-1",
        )
    )
    assert first_report["modules"]["m2"]["active_state"] == "range"
    assert first_report["modules"]["m2"]["pending_days"] == 1

    state_path = tmp_path / "artifacts" / "evolution" / "m2_state.json"
    assert state_path.exists() is True

    second = _make_orchestrator(tmp_path)
    second_report = _as_mapping(
        second.run(
            records=records,
            now=datetime(2026, 3, 3, 20, 40, tzinfo=UTC),
            dry_run=True,
            source_trace_id="test-m2-persist-2",
        )
    )
    assert second_report["modules"]["m2"]["active_state"] == "trend_up"
    assert second_report["modules"]["m2"]["switched"] is True


def test_orchestrator_m3_search_and_maintenance_interfaces(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    run_now = datetime(2026, 3, 2, 20, 40, tzinfo=UTC)
    orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=run_now,
        dry_run=True,
        source_trace_id="test-m3-run",
    )

    query_vector = build_m3_vector_from_record(
        {
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.1,
            "volume": 2_000_000,
        },
        vector_profile=build_default_m3_vector_profile(),
        regime_state="range",
    )
    assert query_vector is not None
    search = _as_mapping(
        orchestrator.m3_search(query_vector=query_vector, top_k=3)
    )
    assert "indices" in search
    assert "scores" in search
    assert "total_vectors" in search
    assert search["vector_profile_id"] == M3_DEFAULT_VECTOR_PROFILE_ID
    assert search["vector_dim"] == 20

    snapshot = orchestrator._m3_store.create_snapshot(now=run_now)
    pending = orchestrator._m3_store.safe_remove_snapshot(
        snapshot_path=snapshot,
        now=run_now,
    )
    old_ts = (run_now - timedelta(hours=25)).timestamp()
    os.utime(pending, (old_ts, old_ts))

    maintenance = _as_mapping(orchestrator.run_m3_maintenance(now=run_now + timedelta(hours=25)))
    assert maintenance["purged_count"] == 1


def test_orchestrator_registers_default_m3_vector_profile_once(tmp_path: Path) -> None:
    first = _make_orchestrator(tmp_path)
    second = _make_orchestrator(tmp_path)

    assert first._m3_vector_profile.vector_profile_id == M3_DEFAULT_VECTOR_PROFILE_ID
    assert second._m3_vector_profile.vector_profile_id == M3_DEFAULT_VECTOR_PROFILE_ID

    registry = M3VectorProfileRegistry(
        db_path=tmp_path / "artifacts" / "evolution" / "m3" / "vector_profiles.duckdb"
    )
    records = registry.list_records()
    assert len(records) == 2
    assert {item.vector_profile_id for item in records} == {
        M3_LEGACY_VECTOR_PROFILE_ID,
        M3_DEFAULT_VECTOR_PROFILE_ID,
    }
    assert max(item.vector_dim for item in records) == 20


def test_orchestrator_can_activate_legacy_m3_vector_profile_from_config(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(
        tmp_path,
        m3_active_vector_profile_id=M3_LEGACY_VECTOR_PROFILE_ID,
    )
    run_now = datetime(2026, 3, 2, 20, 40, tzinfo=UTC)
    report = _as_mapping(
        orchestrator.run(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.3,
                    "low": 9.9,
                    "close": 10.1,
                    "volume": 2_000_000,
                }
            ],
            now=run_now,
            dry_run=True,
            source_trace_id="test-m3-legacy-profile",
        )
    )

    assert orchestrator._m3_vector_profile.vector_profile_id == M3_LEGACY_VECTOR_PROFILE_ID
    assert report["modules"]["m3"]["vector_profile_id"] == M3_LEGACY_VECTOR_PROFILE_ID
    assert report["modules"]["m3"]["vector_dim"] == 5
    query_vector = build_m3_vector_from_record(
        {
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.1,
            "volume": 2_000_000,
        },
        vector_profile=orchestrator._m3_vector_profile,
        regime_state="range",
    )
    assert query_vector is not None
    assert len(query_vector) == 5
    search = _as_mapping(orchestrator.m3_search(query_vector=query_vector, top_k=3))
    assert search["vector_profile_id"] == M3_LEGACY_VECTOR_PROFILE_ID
    assert search["vector_dim"] == 5


def test_orchestrator_rejects_unknown_m3_vector_profile_when_fallback_disabled(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="configured M3 vector profile is not registered"):
        _make_orchestrator(
            tmp_path,
            m3_active_vector_profile_id="missing_profile",
            m3_allow_active_profile_fallback=False,
        )


def test_orchestrator_can_fallback_to_default_m3_vector_profile(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(
        tmp_path,
        m3_active_vector_profile_id="missing_profile",
        m3_allow_active_profile_fallback=True,
    )

    assert orchestrator._m3_vector_profile.vector_profile_id == M3_DEFAULT_VECTOR_PROFILE_ID
    assert orchestrator._m3_vector_profile_resolution["configured_vector_profile_id"] == (
        "missing_profile"
    )
    assert orchestrator._m3_vector_profile_resolution["fallback_used"] is True
    report = _as_mapping(
        orchestrator.run(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.3,
                    "low": 9.9,
                    "close": 10.1,
                    "volume": 2_000_000,
                }
            ],
            now=datetime(2026, 3, 2, 20, 40, tzinfo=UTC),
            dry_run=True,
            source_trace_id="test-m3-profile-fallback",
        )
    )
    assert report["modules"]["m3"]["configured_vector_profile_id"] == "missing_profile"
    assert report["modules"]["m3"]["active_vector_profile_id"] == M3_DEFAULT_VECTOR_PROFILE_ID
    assert report["modules"]["m3"]["fallback_used"] is True


def test_orchestrator_m8_suggestions_directly_use_m3(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    run_now = datetime(2026, 3, 2, 20, 40, tzinfo=UTC)
    seed_records = [
        {
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 2_000_000,
        }
    ]
    orchestrator.run(
        records=seed_records,
        now=run_now,
        dry_run=True,
        source_trace_id="test-m8-seed",
    )
    report = _as_mapping(
        orchestrator.run_m8_suggestions(
            records=seed_records,
            top_k=3,
            now=run_now,
            source_trace_id="test-m8-suggest",
        )
    )
    assert "summary" in report
    assert "items" in report
    assert report["top_k"] == 3
    summary = _as_mapping(report["summary"])
    assert "gate_pass_rate" in summary
    assert "gate_failure_counts" in summary
    assert "gate_names" in summary
    artifact_uri = str(report["artifact_uri"])
    assert artifact_uri.startswith("suggestions/m8/")
    assert (tmp_path / artifact_uri).exists() is True
    first = _as_mapping(cast(list[object], report["items"])[0])
    assert first["symbol"] == "600000.SH"
    assert first["recommendation"] in {"promote", "review", "novel", "invalid"}
    assert "gate_checks" in first
    assert "passed_gates" in first
    assert "failed_gates" in first


def test_orchestrator_applies_configured_fusion_weights(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.score_fusion_weights = {
        "M4": 0.0,
        "M6": 0.0,
        "M10": 0.0,
        "M11": 0.0,
        "M1": 0.0,
        "M2": 0.0,
        "M5": 0.0,
        "M7": 0.0,
        "M3": 0.0,
        "M8": 1.0,
    }
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-fusion-weights",
        )
    )
    module_m8 = float(report["dag"]["module_scores"]["M8"])
    fused = float(report["score_fusion"]["fused_score"])
    assert abs(fused - module_m8) < 1e-9
    assert report["score_fusion"]["weights"]["M8"] == 1.0


def test_orchestrator_uses_configured_m8_parameters_in_main_flow(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.m8_top_k = 2
    config.evolution.m8_promote_similarity = 1.10
    config.evolution.m8_review_similarity = 1.05
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 46, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m8-config-main-flow",
        )
    )
    m8 = _as_mapping(report["modules"]["m8"])
    assert m8["top_k"] == 2
    assert abs(float(m8["promote_similarity"]) - 1.10) < 1e-9
    assert abs(float(m8["review_similarity"]) - 1.05) < 1e-9
    summary = _as_mapping(m8["summary"])
    assert "llm_semantic" in summary
    assert summary["llm_semantic"]["configured"] is False


def test_orchestrator_m8_llm_fallback_uses_backup_on_primary_error(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.llm_semantic_enabled = True
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)

    class _PrimaryFailJudge:
        configured = True

        def judge(self, candidate: object) -> LlmSemanticDecision:
            return LlmSemanticDecision(
                verdict="review",
                confidence=0.0,
                reason="",
                error="timeout",
            )

    class _BackupPassJudge:
        configured = True

        def judge(self, candidate: object) -> LlmSemanticDecision:
            return LlmSemanticDecision(
                verdict="approve",
                confidence=0.82,
                reason="backup accepted",
                error="",
            )

    orchestrator_any = cast(Any, orchestrator)
    orchestrator_any._llm_semantic_primary_judge = _PrimaryFailJudge()
    orchestrator_any._llm_semantic_backup_judge = _BackupPassJudge()

    records = [
        {
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.1,
            "volume": 2_000_000,
        }
    ]
    enriched, summary = orchestrator._enrich_m8_candidates_with_llm(records)
    first = _as_mapping(enriched[0])
    summary_view = _as_mapping(summary)
    assert first["llm_verdict"] == "approve"
    assert abs(float(first["llm_confidence"]) - 0.82) < 1e-9
    assert summary_view["succeeded"] == 1
    assert summary_view["failed"] == 0
    assert summary_view["primary_failed"] == 1
    assert summary_view["backup_calls"] == 1
    assert summary_view["fallback_used"] == 1


def test_orchestrator_m8_six_gate_thresholds_can_force_novel(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.m8_min_gate_passes_for_review = 6
    config.evolution.m8_random_walk_max_pvalue = 0.01
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 46, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m8-six-gate-thresholds",
        )
    )
    m8 = _as_mapping(report["modules"]["m8"])
    summary = _as_mapping(m8["summary"])
    assert int(summary["novel"]) >= 1
    gate_failures = summary["gate_failure_counts"]
    assert isinstance(gate_failures, dict)
    assert float(summary["gate_pass_rate"]) < 1.0


def test_orchestrator_m8_strict_gate_inputs_exposes_provenance(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.m8_strict_gate_inputs = True
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 46, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m8-strict-provenance",
        )
    )
    m8 = _as_mapping(report["modules"]["m8"])
    assert m8["strict_gate_inputs"] is True
    summary = _as_mapping(m8["summary"])
    assert "gate_provenance_counts" in summary
    artifact_path = tmp_path / str(m8["artifact_uri"])
    payload = _as_mapping(json.loads(artifact_path.read_text(encoding="utf-8")))
    item = _as_mapping(cast(list[object], _as_mapping(payload["report"])["items"])[0])
    assert "missing_gate_inputs" in item
    assert "derived_gate_inputs" in item
    first_gate = _as_mapping(cast(list[object], item["gate_checks"])[0])
    assert "provenance" in first_gate


def test_orchestrator_persists_m2_optuna_snapshot_artifact(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.m2_optuna_min_samples = 3
    config.evolution.m2_optuna_trials = 8
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            },
            {
                "symbol": "000001.SZ",
                "open": 9.8,
                "high": 10.0,
                "low": 9.7,
                "close": 9.9,
                "volume": 1_600_000,
            },
            {
                "symbol": "300001.SZ",
                "open": 8.3,
                "high": 8.6,
                "low": 8.2,
                "close": 8.5,
                "volume": 1_200_000,
            },
        ],
        now=datetime(2026, 3, 2, 20, 51, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m2-optuna-snapshot",
        )
    )
    m2 = _as_mapping(report["modules"]["m2"])
    assert str(m2.get("artifact_uri", "")).startswith("suggestions/m2/hmm_params/")
    assert isinstance(m2.get("optuna"), dict)
    assert isinstance(m2.get("params"), dict)


def test_orchestrator_m11_uses_shadow_loader_when_path_is_configured(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False

    shadow_path = tmp_path / "artifacts" / "evolution" / "m11_shadow_results.jsonl"
    shadow_path.parent.mkdir(parents=True, exist_ok=True)
    shadow_path.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","champion_shadow_return":0.020,'
                '"challenger_shadow_return":0.018,"champion_signal":1,"challenger_signal":1,'
                '"intraday_1m_latest_date":"2026-03-02","intraday_5m_latest_date":"2026-03-02"}',
                '{"symbol":"600000.SH","champion_shadow_return":0.010,'
                '"challenger_shadow_return":0.009,"champion_signal":1,"challenger_signal":1,'
                '"intraday_1m_latest_date":"2026-03-03","intraday_5m_latest_date":"2026-03-03"}',
                '{"symbol":"600000.SH","champion_shadow_return":-0.005,'
                '"challenger_shadow_return":-0.006,"champion_signal":0,"challenger_signal":0,'
                '"intraday_1m_latest_date":"2026-03-04","intraday_5m_latest_date":"2026-03-04"}',
            ]
        ),
        encoding="utf-8",
    )
    config.evolution.m11_shadow_results_path = str(shadow_path.relative_to(tmp_path))

    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 47, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m11-shadow-loader",
        )
    )
    m11 = _as_mapping(report["modules"]["m11"])
    assert m11["status"] != "no_data"
    assert m11["metrics"]["valid_samples"] == 3
    assert m11["input"]["source"] == "shadow_loader"
    assert m11["input"]["path_exists"] is True
    assert m11["input"]["intraday_1m_coverage_ratio"] == 1.0
    assert m11["input"]["intraday_5m_coverage_ratio"] == 1.0
    rollback_input = report["rollback"]["input"]
    assert rollback_input["source"] == "m11_shadow"
    assert rollback_input["m11_input_source"] == "shadow_loader"
    assert rollback_input["m11_path_exists"] is True
    assert rollback_input["loaded_samples"] == 3
    assert str(rollback_input["m11_path"]).endswith("artifacts/evolution/m11_shadow_results.jsonl")
    assert rollback_input["m11_status"] == m11["status"]
    assert rollback_input["diff_return_count"] == 3
    assert rollback_input["observed_days"] == 3
    assert rollback_input["trade_count"] == 2
    assert rollback_input["shadow_champion_vol"] > 0.0
    assert rollback_input["hard_drawdown_breach"] is False
    assert rollback_input["tail_loss_triggered"] is False


def test_orchestrator_rollback_uses_m11_redlines_from_shadow_loader(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False

    shadow_path = tmp_path / "artifacts" / "evolution" / "m11_shadow_redline.jsonl"
    shadow_path.parent.mkdir(parents=True, exist_ok=True)
    shadow_path.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","champion_shadow_return":0.010,'
                '"challenger_shadow_return":-0.050,"champion_signal":1,"challenger_signal":1}',
                '{"symbol":"600000.SH","champion_shadow_return":0.010,'
                '"challenger_shadow_return":-0.060,"champion_signal":1,"challenger_signal":1}',
                '{"symbol":"600000.SH","champion_shadow_return":0.010,'
                '"challenger_shadow_return":-0.070,"champion_signal":1,"challenger_signal":1}',
            ]
        ),
        encoding="utf-8",
    )
    config.evolution.m11_shadow_results_path = str(shadow_path.relative_to(tmp_path))

    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 48, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m11-rollback-redline",
        )
    )

    m11 = _as_mapping(report["modules"]["m11"])
    rollback = _as_mapping(report["rollback"])
    rollback_input = _as_mapping(rollback["input"])

    assert m11["status"] == "redline_breach"
    assert _as_mapping(m11["redlines"])["drawdown_delta"] is True
    assert _as_mapping(m11["redlines"])["tail_loss_delta"] is True
    assert rollback["state"] == "rolled_back"
    assert rollback["reason"] == "hard_circuit_breaker"
    assert rollback_input["source"] == "m11_shadow"
    assert rollback_input["m11_input_source"] == "shadow_loader"
    assert rollback_input["loaded_samples"] == 3
    assert rollback_input["observed_days"] == 3
    assert rollback_input["trade_count"] == 3
    assert rollback_input["hard_drawdown_breach"] is True
    assert rollback_input["tail_loss_triggered"] is True


def test_orchestrator_m5_strategy_linkage_escalates_authorization(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.m5_strategy_min_labeled_samples = 5

    label_path = tmp_path / "artifacts" / "evolution" / "m5_labels.jsonl"
    label_path.parent.mkdir(parents=True, exist_ok=True)
    label_path.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","open":10.0,"close":9.5,"label":1,'
                '"label_seed_1":1,"label_seed_2":0,'
                '"intraday_1m_latest_date":"2026-03-02","intraday_5m_latest_date":"2026-03-02"}',
                '{"symbol":"600000.SH","open":9.8,"close":9.3,"label":1,'
                '"label_seed_1":1,"label_seed_2":0,'
                '"intraday_1m_latest_date":"2026-03-03","intraday_5m_latest_date":"2026-03-03"}',
                '{"symbol":"600000.SH","open":9.6,"close":9.1,"label":1,'
                '"label_seed_1":1,"label_seed_2":0,'
                '"intraday_1m_latest_date":"2026-03-04","intraday_5m_latest_date":"2026-03-04"}',
                '{"symbol":"600000.SH","open":9.4,"close":8.9,"label":1,'
                '"label_seed_1":1,"label_seed_2":0,'
                '"intraday_1m_latest_date":"2026-03-05","intraday_5m_latest_date":"2026-03-05"}',
                '{"symbol":"600000.SH","open":9.2,"close":8.7,"label":1,'
                '"label_seed_1":1,"label_seed_2":0,'
                '"intraday_1m_latest_date":"2026-03-06","intraday_5m_latest_date":"2026-03-06"}',
                '{"symbol":"600000.SH","open":9.0,"close":8.5,"label":1,'
                '"label_seed_1":1,"label_seed_2":0,'
                '"intraday_1m_latest_date":"2026-03-07","intraday_5m_latest_date":"2026-03-07"}',
            ]
        ),
        encoding="utf-8",
    )
    config.evolution.m5_label_records_path = str(label_path.relative_to(tmp_path))

    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 48, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m5-linkage",
        )
    )
    m5 = _as_mapping(report["modules"]["m5"])
    assert m5["input"]["source"] == "label_loader"
    assert m5["input"]["intraday_1m_coverage_ratio"] == 1.0
    assert m5["input"]["intraday_5m_coverage_ratio"] == 1.0
    assert m5["strategy_linkage"]["mode"] == "propose_label_tuning"
    assert report["proposal"]["authorization_level"] == "A"
    assert any("label" in key for key in report["proposal"]["change_keys"])


def test_orchestrator_m7_uses_news_loader_and_budget_guard(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.m7_daily_budget = 0.40
    config.evolution.m7_default_event_cost = 0.20

    news_path = tmp_path / "artifacts" / "evolution" / "m7_news.jsonl"
    news_path.parent.mkdir(parents=True, exist_ok=True)
    news_path.write_text(
        "\n".join(
            [
                '{"event_id":"n1","symbol":"600000.SH","headline":"券商板块走强，成交放量",'
                '"sentiment":0.90,"cost":0.20}',
                '{"event_id":"n2","symbol":"600000.SH","headline":"券商板块走强，成交放量",'
                '"sentiment":0.85,"cost":0.20}',
                '{"event_id":"n3","symbol":"000001.SZ","headline":"地产政策预期升温",'
                '"sentiment":0.80,"cost":0.20}',
                '{"event_id":"n4","symbol":"300001.SZ","headline":"海外风险上升压制偏好",'
                '"sentiment":-0.70,"cost":0.20}',
            ]
        ),
        encoding="utf-8",
    )
    config.evolution.m7_news_records_path = str(news_path.relative_to(tmp_path))

    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 49, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-m7-loader-budget",
        )
    )
    m7 = _as_mapping(report["modules"]["m7"])
    assert m7["input"]["source"] == "news_loader"
    assert m7["metrics"]["valid_events"] >= 3
    assert m7["metrics"]["dropped_by_budget"] >= 1
    assert m7["status"] == "budget_capped"
    assert "observation_queue_news_budget" in report["proposal"]["change_keys"]


def test_orchestrator_m7_exposes_event_ledger_effectiveness(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.m7_daily_budget = 10.0
    config.evolution.m7_default_event_cost = 0.20
    config.evolution.m7_ledger_ttl_days = 7

    news_path = tmp_path / "artifacts" / "evolution" / "m7_news_effective.jsonl"
    news_path.parent.mkdir(parents=True, exist_ok=True)
    news_path.write_text(
        "\n".join(
            [
                '{"event_id":"e1","symbol":"600000.SH","headline":"Broker sector rebounds",'
                '"sentiment":0.90,"cost":0.20,"source":"wire-a",'
                '"published_at":"2026-03-02T09:00:00+00:00"}',
                '{"event_id":"e2","symbol":"000001.SZ","headline":"Property pressure resumes",'
                '"sentiment":-0.80,"cost":0.20,"source":"wire-b",'
                '"published_at":"2026-03-02T09:10:00+00:00"}',
            ]
        ),
        encoding="utf-8",
    )
    config.evolution.m7_news_records_path = str(news_path.relative_to(tmp_path))

    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    _ = orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 2_000_000,
            },
            {
                "symbol": "000001.SZ",
                "open": 8.0,
                "high": 8.1,
                "low": 7.8,
                "close": 8.0,
                "volume": 1_500_000,
            },
        ],
        now=datetime(2026, 3, 2, 20, 30, tzinfo=UTC),
        dry_run=True,
        source_trace_id="m7-ledger-seed",
    )
    report = _as_mapping(
        orchestrator.run(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.2,
                    "high": 10.9,
                    "low": 10.1,
                    "close": 10.8,
                    "volume": 2_100_000,
                },
                {
                    "symbol": "000001.SZ",
                    "open": 7.9,
                    "high": 8.0,
                    "low": 7.1,
                    "close": 7.2,
                    "volume": 1_600_000,
                },
            ],
            now=datetime(2026, 3, 3, 21, 0, tzinfo=UTC),
            dry_run=True,
            source_trace_id="m7-ledger-followup",
        )
    )
    m7 = _as_mapping(report["modules"]["m7"])
    ledger = _as_mapping(m7["ledger"])
    ingest = _as_mapping(ledger["ingest"])
    effectiveness = _as_mapping(ledger["effectiveness"])
    paths = _as_mapping(ledger["paths"])
    assert ingest["deduplicated"] >= 2
    assert effectiveness["matured_1d"] >= 2
    assert effectiveness["hit_rate_1d"] == 1.0
    assert isinstance(effectiveness["source_reliability"], list)
    assert str(paths["db_path"]).endswith("artifacts/evolution/m7_event_ledger.duckdb")


def test_orchestrator_evaluates_universe_consistency_statuses(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    not_provided = _as_mapping(
        orchestrator._evaluate_universe_consistency(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
            }
        ]
        )
    )
    assert not_provided["status"] == "not_provided"
    assert not_provided["consistent"] is True
    assert not_provided["snapshot_id"] == ""
    assert not_provided["universe_spec_hash"] == ""

    consistent = _as_mapping(
        orchestrator._evaluate_universe_consistency(
        records=[
            {
                "symbol": "600000.SH",
                "universe_snapshot_id": "univ-20260302-aaaa1111",
                "universe_spec_hash": "hash-v1",
            },
            {
                "symbol": "000001.SZ",
                "universe_snapshot_id": "univ-20260302-aaaa1111",
                "universe_spec_hash": "hash-v1",
            },
        ]
        )
    )
    assert consistent["status"] == "consistent"
    assert consistent["consistent"] is True
    assert consistent["snapshot_id"] == "univ-20260302-aaaa1111"
    assert consistent["universe_spec_hash"] == "hash-v1"

    inconsistent = _as_mapping(
        orchestrator._evaluate_universe_consistency(
        records=[
            {
                "symbol": "600000.SH",
                "universe_snapshot_id": "univ-20260302-aaaa1111",
                "universe_spec_hash": "hash-v1",
            },
            {
                "symbol": "000001.SZ",
                "universe_snapshot_id": "univ-20260302-bbbb2222",
                "universe_spec_hash": "hash-v2",
            },
        ]
        )
    )
    assert inconsistent["status"] == "inconsistent"
    assert inconsistent["consistent"] is False
    assert inconsistent["unique_snapshot_ids"] == [
        "univ-20260302-aaaa1111",
        "univ-20260302-bbbb2222",
    ]
    assert inconsistent["unique_spec_hashes"] == ["hash-v1", "hash-v2"]


def test_orchestrator_sets_execution_sensitivity_alert_after_consecutive_days(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    config.evolution.execution_spec.sensitivity_threshold_bp = 30
    config.evolution.execution_spec.sensitivity_days = 3
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    records = [
        {
            "symbol": "600000.SH",
            "open": 10.05,
            "high": 10.20,
            "low": 9.90,
            "close": 10.00,
            "volume": 2_000_000,
            "vwap_proxy_open": 10.05,
            "vwap_proxy_day": 10.00,
        }
    ]

    run1 = _as_mapping(
        orchestrator.run(
            records=records,
            now=datetime(2026, 3, 2, 20, 50, tzinfo=UTC),
            dry_run=True,
            source_trace_id="sensitivity-1",
        )
    )
    run2 = _as_mapping(
        orchestrator.run(
            records=records,
            now=datetime(2026, 3, 3, 20, 50, tzinfo=UTC),
            dry_run=True,
            source_trace_id="sensitivity-2",
        )
    )
    run3 = _as_mapping(
        orchestrator.run(
            records=records,
            now=datetime(2026, 3, 4, 20, 50, tzinfo=UTC),
            dry_run=True,
            source_trace_id="sensitivity-3",
        )
    )
    m10_run1 = run1["modules"]["m10"]
    m10_run2 = run2["modules"]["m10"]
    m10_run3 = run3["modules"]["m10"]
    assert m10_run1["execution_sensitivity"]["execution_sensitivity_alert"] is False
    assert m10_run2["execution_sensitivity"]["execution_sensitivity_alert"] is False
    assert m10_run3["execution_sensitivity"]["execution_sensitivity_alert"] is True
    assert "execution_sensitivity_alert" in run3["proposal"]["change_keys"]


def test_orchestrator_emits_online_samples_used_hash_from_m5_records(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False

    label_path = tmp_path / "artifacts" / "evolution" / "m5_labels_online_hash.jsonl"
    label_path.parent.mkdir(parents=True, exist_ok=True)
    label_path.write_text(
        "\n".join(
            [
                '{"symbol":"000001.SZ","trade_date":"2026-03-03","label_mature_time":"2026-03-04T15:00:00",'
                '"open":10.0,"close":10.1,"label":1,"label_seed_1":1,"label_seed_2":1}',
                '{"symbol":"600000.SH","trade_date":"2026-03-02","label_mature_time":"2026-03-03T15:00:00",'
                '"open":10.0,"close":9.9,"label":0,"label_seed_1":0,"label_seed_2":0}',
            ]
        ),
        encoding="utf-8",
    )
    config.evolution.m5_label_records_path = str(label_path.relative_to(tmp_path))

    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
                "p_lgbm": 0.52,
                "p_xgb": 0.51,
                "p_meta": 0.515,
            }
        ],
        now=datetime(2026, 3, 5, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="test-online-samples-hash",
        )
    )
    online = _as_mapping(report["online_update_audit"])
    assert online["online_samples_used"] == 2
    assert len(str(online["online_samples_used_hash"])) == 64
    assert online["deterministic_order_fields"] == ["label_mature_time", "trade_date", "symbol"]


def test_orchestrator_appends_change_keys_when_trading_fill_distribution_breaches(
    tmp_path: Path,
) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 2_000_000,
                "fill_status": "no_fill",
            },
            {
                "symbol": "000001.SZ",
                "open": 11.0,
                "high": 11.1,
                "low": 10.9,
                "close": 11.0,
                "volume": 1_500_000,
                "fill_status": "no_fill",
            },
            {
                "symbol": "300750.SZ",
                "open": 12.0,
                "high": 12.4,
                "low": 11.8,
                "close": 12.2,
                "volume": 1_300_000,
                "fill_status": "partial_fill",
            },
        ],
        now=datetime(2026, 3, 5, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="dual-eval-breach",
        )
    )
    change_keys = report["proposal"]["change_keys"]
    assert "trading_fill_distribution_gate" in change_keys
    assert "trading_fill_gate_no_fill_ratio_limit_breach" in change_keys
    assert "trading_fill_gate_no_fill_ratio_delta_limit_breach" in change_keys
    assert "hard_gate_failed" in change_keys
    assert "hard_gate_trading_fill_distribution_failed" in change_keys
    hard_gates = _as_mapping(report["modules"]["hard_gates"])
    assert hard_gates["all_passed"] is False
    assert "trading_fill_distribution" in hard_gates["failed_gates"]


def test_orchestrator_blocks_promotion_when_execution_gate_fails(
    tmp_path: Path,
) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._config.runtime_spec.promotion_min_healthy_days = 1
    label_path = tmp_path / "artifacts" / "evolution" / "m5_promotion_gate_labels.jsonl"
    label_path.parent.mkdir(parents=True, exist_ok=True)
    label_path.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","trade_date":"2026-03-01","label_mature_time":"2026-03-02T15:00:00","open":10.0,"high":10.2,"low":9.9,"close":10.1,"volume":1500000,"label":1,"p_meta":0.45}',
                '{"symbol":"000001.SZ","trade_date":"2026-03-01","label_mature_time":"2026-03-02T15:00:00","open":10.0,"high":10.1,"low":9.7,"close":9.8,"volume":1400000,"label":0,"p_meta":0.56}',
                '{"symbol":"300750.SZ","trade_date":"2026-03-02","label_mature_time":"2026-03-03T15:00:00","open":12.0,"high":12.2,"low":11.9,"close":12.1,"volume":1300000,"label":1,"p_meta":0.47}',
                '{"symbol":"002594.SZ","trade_date":"2026-03-02","label_mature_time":"2026-03-03T15:00:00","open":11.0,"high":11.1,"low":10.6,"close":10.7,"volume":1200000,"label":0,"p_meta":0.58}',
                '{"symbol":"688981.SH","trade_date":"2026-03-03","label_mature_time":"2026-03-04T15:00:00","open":9.8,"high":10.1,"low":9.7,"close":10.0,"volume":1100000,"label":1,"p_meta":0.49}',
            ]
        ),
        encoding="utf-8",
    )
    orchestrator._config.m5_label_records_path = str(label_path.relative_to(tmp_path))

    report = _as_mapping(
        orchestrator.run(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 2_000_000,
                    "fill_status": "no_fill",
                    "p_lgbm": 0.52,
                    "p_xgb": 0.51,
                    "p_meta": 0.515,
                },
                {
                    "symbol": "000001.SZ",
                    "open": 11.0,
                    "high": 11.1,
                    "low": 10.9,
                    "close": 11.0,
                    "volume": 1_500_000,
                    "fill_status": "no_fill",
                    "p_lgbm": 0.51,
                    "p_xgb": 0.50,
                    "p_meta": 0.505,
                },
            ],
            now=datetime(2026, 3, 5, 20, 45, tzinfo=UTC),
            dry_run=True,
            source_trace_id="promotion-gate-execution-block",
        )
    )
    online_update = _as_mapping(report["modules"]["online_update"])
    audit = _as_mapping(report["audit_fields"])
    change_keys = report["proposal"]["change_keys"]

    assert online_update["promotion_candidate"] is True
    assert online_update["promotion_gate_passed"] is False
    assert online_update["promotion_decision"] == "hold"
    assert "execution_trading_distribution_failed" in online_update["promotion_reason_codes"]
    assert "promotion_gate_blocked" in change_keys
    assert "promotion_gate_execution_trading_distribution_failed" in change_keys
    assert audit["promotion_gate_passed"] is False
    assert "execution_trading_distribution_failed" in audit["promotion_gate_reason_codes"]


def test_orchestrator_appends_change_keys_when_dynamic_k_adjusted(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 2_000_000,
                "turnover_ratio": 1.8,
                "own_participation_ratio": 0.02,
            },
            {
                "symbol": "000001.SZ",
                "open": 11.0,
                "high": 11.1,
                "low": 10.9,
                "close": 11.0,
                "volume": 1_500_000,
                "turnover_ratio": 1.4,
                "own_participation_ratio": 0.015,
            },
        ],
        now=datetime(2026, 3, 5, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="dynamic-k-adjusted",
        )
    )
    change_keys = report["proposal"]["change_keys"]
    assert "dynamic_k_adjusted" in change_keys
    assert "dynamic_k_trim_turnover_excess" in change_keys
    assert "dynamic_k_trim_capacity_excess" in change_keys
    audit = _as_mapping(report["audit_fields"])
    assert audit["k_dynamic"] < audit["k_base"]
    assert float(audit["constraint_pressure"]) > 0.0


def test_orchestrator_appends_change_keys_when_position_drift_consecutive_breach(
    tmp_path: Path,
) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    records = [
        {
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 2_000_000,
            "target_weight": 0.30,
            "filled_weight": 0.20,
            "end_of_day_position_weight": 0.10,
        }
    ]
    orchestrator.run(
        records=records,
        now=datetime(2026, 3, 3, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="position-drift-1",
    )
    orchestrator.run(
        records=records,
        now=datetime(2026, 3, 4, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="position-drift-2",
    )
    third = _as_mapping(
        orchestrator.run(
            records=records,
            now=datetime(2026, 3, 5, 20, 45, tzinfo=UTC),
            dry_run=True,
            source_trace_id="position-drift-3",
        )
    )
    change_keys = third["proposal"]["change_keys"]
    assert "position_drift_alert" in change_keys
    assert "position_drift_consecutive_breach" in change_keys
    drift = _as_mapping(third["modules"]["reconcile_drift"])
    assert drift["position_drift_consecutive_days"] == 3
    assert drift["raise_u_threshold_bp_recommendation"] == 20


def test_orchestrator_exposes_audit_fields_bundle(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    report = _as_mapping(
        orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
                "universe_snapshot_id": "univ-20260305-a1b2c3d4",
                "universe_spec_hash": "hash-universe-v1",
            }
        ],
        now=datetime(2026, 3, 5, 20, 40, tzinfo=UTC),
        dry_run=True,
        source_trace_id="audit-fields",
        )
    )
    audit = _as_mapping(report["audit_fields"])
    assert audit["price_series_mode"] in {"raw", "qfq", "hfq"}
    assert audit["dividend_treatment"] in {"implicit_by_qfq", "explicit_cashflow"}
    assert audit["share_rounding_rule"] != ""
    assert audit["price_tick_rule"] != ""
    assert audit["residual_order_policy"] != ""
    assert audit["eval_profile_id"] != ""
    assert abs(float(audit["no_fill_ratio"])) < 1e-9
    assert abs(float(audit["partial_fill_ratio"])) < 1e-9
    assert audit["mapping_level_used"] != ""
    assert audit["k_base"] >= audit["k_dynamic"]
    assert isinstance(audit["mapping_fallback_steps"], list)
    assert isinstance(audit["trim_reason_codes"], list)
    assert "target_vs_filled_weight_p50" in audit
    assert "filled_vs_eod_weight_p50" in audit
    assert "position_drift_ratio" in audit
    assert audit["hard_gate_all_passed"] is True
    assert audit["hard_gate_failed_count"] == 0
    assert audit["universe_snapshot_id"] == "univ-20260305-a1b2c3d4"
    assert audit["universe_spec_hash"] == "hash-universe-v1"
    assert len(str(audit["library_versions_hash"])) == 64
