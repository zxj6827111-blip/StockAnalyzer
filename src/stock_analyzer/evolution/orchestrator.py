"""Off-hours evolution orchestration for dry-run closed loop."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray

from stock_analyzer.config import EvolutionConfig
from stock_analyzer.evolution.core.fusion import ScoreFusionEngine
from stock_analyzer.evolution.eval_profiles import evaluate_dual_eval_profiles
from stock_analyzer.evolution.execution_sensitivity import evaluate_execution_sensitivity
from stock_analyzer.evolution.governance.authorization import (
    AuthorizationDecision,
    AuthorizationLevel,
    authorize_proposal,
)
from stock_analyzer.evolution.governance.compliance import (
    ComplianceEvent,
    ComplianceLogger,
    ComplianceState,
)
from stock_analyzer.evolution.governance.proposal import ProposalArtifact
from stock_analyzer.evolution.governance.rollback import (
    RollbackContext,
    RollbackPolicy,
    evaluate_rollback,
)
from stock_analyzer.evolution.hard_gates import evaluate_hard_gates
from stock_analyzer.evolution.latency_slo import evaluate_latency_slo
from stock_analyzer.evolution.llm_semantic import OpenAICompatibleSemanticJudge
from stock_analyzer.evolution.m3_vector_profile import (
    M3VectorProfileRegistry,
    build_default_m3_vector_profile,
    build_legacy_m3_vector_profile,
    build_m3_vector_from_record,
    resolve_active_m3_vector_profile,
)
from stock_analyzer.evolution.modules.m1_case_learning import run_m1_dual_learning
from stock_analyzer.evolution.modules.m2_regime_hmm import (
    M2OptunaLikeConfig,
    M2OptunaLikeResult,
    RegimeModelParams,
    RegimeObservation,
    RegimeStateController,
    evaluate_m2_regime,
    tune_regime_with_optuna_like_search,
)
from stock_analyzer.evolution.modules.m3_pattern_memory import PatternMemoryStore
from stock_analyzer.evolution.modules.m4_capital_flow import evaluate_m4_capital_flow
from stock_analyzer.evolution.modules.m5_label_loader import load_m5_label_records
from stock_analyzer.evolution.modules.m5_label_optimization import (
    build_m5_strategy_linkage,
    evaluate_m5_label_optimization,
)
from stock_analyzer.evolution.modules.m6_counterparty import evaluate_m6_counterparty
from stock_analyzer.evolution.modules.m7_event_ledger import M7EventLedger
from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records
from stock_analyzer.evolution.modules.m7_news_sentiment import evaluate_m7_news_sentiment
from stock_analyzer.evolution.modules.m8_memory_bridge import run_m8_memory_bridge
from stock_analyzer.evolution.modules.m9_data_quality import inspect_data_quality
from stock_analyzer.evolution.modules.m10_model_health import evaluate_m10_model_health
from stock_analyzer.evolution.modules.m11_shadow_loader import (
    M11ShadowObservation,
    load_m11_shadow_records,
    parse_m11_shadow_records,
)
from stock_analyzer.evolution.modules.m11_shadow_portfolio import evaluate_m11_shadow_portfolio
from stock_analyzer.evolution.modules.shadow_online_model import (
    run_shadow_online_model,
    shadow_online_result_to_dict,
)
from stock_analyzer.evolution.modules.shadow_online_model_v2 import (
    run_shadow_online_model_v2,
    shadow_online_v2_result_to_dict,
)
from stock_analyzer.evolution.online_samples import build_online_sample_audit
from stock_analyzer.evolution.online_update import run_online_partial_fit_policy
from stock_analyzer.evolution.ops.disk_sentinel import DiskSentinel
from stock_analyzer.evolution.ops.recovery import (
    ManifestCheckpoint,
    RunManifest,
    assert_recovery_time_window,
    check_environment_dependencies,
    load_manifest,
    save_manifest,
)
from stock_analyzer.evolution.reconcile_drift import evaluate_daily_reconcile_drift
from stock_analyzer.evolution.scheduler.circuit_breaker import CircuitBreaker
from stock_analyzer.evolution.scheduler.dag import EvolutionDag
from stock_analyzer.evolution.shadow_online_v2_metrics_store import ShadowOnlineV2MetricsStore
from stock_analyzer.evolution.shadow_online_v2_state_store import ShadowOnlineV2StateStore
from stock_analyzer.evolution.specs import build_spec_hash_bundle
from stock_analyzer.evolution.utility_execution import evaluate_utility_execution_policy


class RollbackEvaluationInput(TypedDict):
    diff_returns: list[float]
    shadow_champion_vol: float
    m11_status: str
    m11_input_source: str
    m11_path: str | None
    m11_path_exists: bool
    loaded_samples: int
    hard_drawdown_breach: bool
    tail_loss_triggered: bool


class OffhoursEvolutionOrchestrator:
    """Execute one off-hours evolution cycle and persist artifacts."""

    def __init__(
        self,
        config: EvolutionConfig,
        project_root: str | Path | None = None,
    ) -> None:
        self._config = config
        self._project_root = (
            Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
        )
        self._dag = EvolutionDag()
        self._breaker = CircuitBreaker()
        self._fusion_engine = ScoreFusionEngine(
            default_weights=config.score_fusion_weights,
            enable_bonus_cap=config.score_fusion_enable_bonus_cap,
            bonus_modules=tuple(config.score_fusion_bonus_modules),
            bonus_neutral_score=config.score_fusion_bonus_neutral_score,
            bonus_cap=config.score_fusion_bonus_cap,
            enable_veto=config.score_fusion_enable_veto,
            veto_modules=tuple(config.score_fusion_veto_modules),
            veto_score_threshold=config.score_fusion_veto_score_threshold,
            veto_score_cap=config.score_fusion_veto_score_cap,
            veto_confidence_gate=config.score_fusion_veto_confidence_gate,
        )
        self._m2_controller = RegimeStateController(active_state="range")
        self._m2_observation_history: list[RegimeObservation] = []
        self._m2_last_optuna: M2OptunaLikeResult | None = None
        self._m2_last_tuned_at: datetime | None = None
        self._m2_last_artifact_uri: str | None = None
        self._latency_breach_history: list[bool] = []
        self._execution_sensitivity_history: list[bool] = []
        self._online_update_state: dict[str, object] = {}
        self._shadow_online_state: dict[str, object] = {}
        self._shadow_online_v2_state: dict[str, object] = {}
        self._eval_profiles_report: dict[str, object] = {}
        self._utility_execution_report: dict[str, object] = {}
        self._reconcile_drift_report: dict[str, object] = {}
        self._utility_execution_state: dict[str, object] = {}
        self._reconcile_drift_state: dict[str, object] = {}
        self._m2_state_path = self._resolve_path(self._config.m2_state_path)
        self._shadow_online_state_path = self._resolve_path(
            self._config.shadow_online_model_state_path
        )
        self._shadow_online_v2_state_path = self._derive_shadow_online_v2_state_path()
        self._shadow_online_v2_metrics_path = self._derive_shadow_online_v2_metrics_path()
        self._m3_store_dir = self._resolve_path(self._config.m3_store_dir)
        self._m3_vector_profile_db_path = self._derive_m3_vector_profile_db_path()
        self._m7_event_ledger_db_path = self._resolve_path(self._config.m7_ledger_db_path)
        self._m7_event_ledger_archive_dir = self._resolve_path(
            self._config.m7_ledger_archive_dir
        )
        self._shadow_online_v2_state_store = ShadowOnlineV2StateStore(
            self._shadow_online_v2_state_path
        )
        self._shadow_online_v2_metrics_store = ShadowOnlineV2MetricsStore(
            self._shadow_online_v2_metrics_path
        )
        self._m7_event_ledger = M7EventLedger(
            db_path=self._m7_event_ledger_db_path,
            archive_dir=self._m7_event_ledger_archive_dir,
            ttl_days=self._config.m7_ledger_ttl_days,
        )
        self._m3_vector_profile_registry = M3VectorProfileRegistry(
            db_path=self._m3_vector_profile_db_path
        )
        self._m3_legacy_vector_profile = self._m3_vector_profile_registry.register(
            build_legacy_m3_vector_profile()
        )
        self._m3_default_vector_profile = self._m3_vector_profile_registry.register(
            build_default_m3_vector_profile()
        )
        self._m3_vector_profile, self._m3_vector_profile_resolution = (
            resolve_active_m3_vector_profile(
                self._m3_vector_profile_registry,
                configured_profile_id=self._config.m3_active_vector_profile_id,
                allow_fallback_to_default=self._config.m3_allow_active_profile_fallback,
            )
        )
        self._load_m2_state()
        self._load_shadow_online_state()
        self._load_shadow_online_v2_state()
        self._m3_store = PatternMemoryStore(
            base_dir=self._m3_store_dir,
            vector_dim=self._m3_vector_profile.vector_dim,
            batch_size=50_000,
            vector_profile_id=self._m3_vector_profile.vector_profile_id,
        )
        (
            self._llm_semantic_primary_judge,
            self._llm_semantic_backup_judge,
        ) = self._build_llm_semantic_judges()

    def run(
        self,
        records: Sequence[Mapping[str, object]],
        now: datetime | None = None,
        dry_run: bool | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        """Run one evolution cycle.

        Args:
            records: Market records used by M9 quality checks.
            now: Optional run timestamp.
            dry_run: Optional override for dry-run mode.
            source_trace_id: Optional source trace id.

        Returns:
            Run report payload.
        """
        run_now = now or datetime.now(UTC)
        active_dry_run = self._config.dry_run if dry_run is None else dry_run

        assert_recovery_time_window(now=run_now)
        dep_result = check_environment_dependencies(
            required_cli=tuple(self._config.dependency_required_cli),
            required_modules=tuple(self._config.dependency_required_modules),
        )
        if self._config.strict_dependency_check and not dep_result.all_available:
            missing = [item.name for item in dep_result.statuses if not item.available]
            raise RuntimeError(f"environment dependency precheck failed: {missing}")

        disk_report = DiskSentinel(
            base_dir=self._project_root,
            suggestions_dir=self._config.suggestions_dir,
            high_watermark=self._config.disk_high_watermark_pct,
        ).enforce(now=run_now)

        manifest = self._load_or_create_manifest()
        checkpoints = list(manifest.checkpoints)
        checkpoints.append(
            ManifestCheckpoint(
                step="recovery_firewall",
                status="passed",
                timestamp=run_now,
            )
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="dependency_precheck",
                status="ok" if dep_result.all_available else "warn",
                timestamp=run_now,
            )
        )

        m9_result = inspect_data_quality(
            records=records,
            required_fields=tuple(self._config.m9_required_fields),
        )
        m9_success = not m9_result.blackout_day and not m9_result.degraded
        self._breaker.record_result(module="M9", success=m9_success, day=run_now.date())
        if m9_result.blackout_day:
            self._breaker.set_blackout_day(day=run_now.date(), enabled=True)

        m9_retry = self._dag.should_retry("M9", success=m9_success)
        checkpoints.append(
            ManifestCheckpoint(
                step="M9",
                status="ok" if m9_success else ("retry_pending" if m9_retry else "failed"),
                timestamp=run_now,
            )
        )

        module_scores, module_details, module_checkpoints = self._run_batch2_modules(
            records=records,
            run_now=run_now,
            m9_success=m9_success,
            source_trace_id=source_trace_id,
            run_id=manifest.run_id,
        )
        checkpoints.extend(module_checkpoints)
        m2_details = module_details.get("m2", {})
        veto_confidence = (
            _as_float(m2_details.get("confidence"), default=0.0)
            if isinstance(m2_details, Mapping)
            else 0.0
        )
        fusion = self._fusion_engine.fuse(
            module_scores=module_scores,
            active_champion_id=self._config.active_champion_id,
            veto_confidence=veto_confidence,
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="score_fusion",
                status="ok",
                timestamp=run_now,
            )
        )

        spec_bundle = build_spec_hash_bundle(config=self._config)
        reproducibility = self._build_reproducibility_bundle()
        proposal = self._build_proposal(
            run_now=run_now,
            fused_score=fusion.fused_score,
            module_scores=module_scores,
            module_artifacts=self._collect_module_artifacts(module_details=module_details),
            spec_hashes={
                "execution_spec_hash": str(spec_bundle["execution_spec_hash"]),
                "runtime_config_hash": str(spec_bundle["runtime_config_hash"]),
                "universe_spec_hash": str(spec_bundle["universe_spec_hash"]),
            },
            reproducibility=reproducibility,
        )
        change_keys = self._build_change_keys(
            m9_success=m9_success,
            module_details=module_details,
        )
        auth_decision = authorize_proposal(
            proposal=proposal,
            change_keys=change_keys,
            active_code_commit_id=self._config.code_commit_id,
        )

        rollback_observations, rollback_m11_input = self._resolve_m11_observations(records=records)
        rollback_m11_detail_raw = module_details.get("m11", {})
        rollback_m11_detail: Mapping[str, object] = (
            rollback_m11_detail_raw if isinstance(rollback_m11_detail_raw, Mapping) else {}
        )
        rollback_context, rollback_input = _build_rollback_context_from_m11(
            observations=rollback_observations,
            m11_detail=rollback_m11_detail,
            m11_input=rollback_m11_input,
        )
        rollback = evaluate_rollback(
            diff_returns=rollback_input["diff_returns"],
            shadow_champion_vol=rollback_input["shadow_champion_vol"],
            context=rollback_context,
            policy=RollbackPolicy(),
            now=run_now,
            hard_drawdown_breach=bool(rollback_input["hard_drawdown_breach"]),
            tail_loss_triggered=bool(rollback_input["tail_loss_triggered"]),
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="governance",
                status="ok",
                timestamp=run_now,
            )
        )

        manifest = manifest.model_copy(update={"checkpoints": checkpoints, "updated_at": run_now})
        self._save_manifest(manifest)

        compliance_states = self._resolve_compliance_states(
            m9_success=m9_success,
            m9_retry=m9_retry,
            auth_decision=auth_decision,
            dry_run=active_dry_run,
        )
        compliance_written = self._write_compliance_events(
            states=compliance_states,
            proposal=proposal,
            symbol=self._best_effort_symbol(records),
            run_now=run_now,
            source_trace_id=source_trace_id,
        )
        universe = self._evaluate_universe_consistency(records=records)
        audit_fields = self._build_audit_fields(
            module_details=module_details,
            spec_bundle=spec_bundle,
            reproducibility=reproducibility,
            universe=universe,
        )
        runtime_controls = self._build_runtime_controls(
            module_details=module_details,
            run_now=run_now,
            m9_success=m9_success,
        )
        runtime_controls["source_run_id"] = manifest.run_id

        online_update_audit = module_details.get("online_update")
        shadow_online_report = module_details.get("shadow_online_model")
        shadow_online_v2_report = module_details.get("shadow_online_model_v2")
        return {
            "run_id": manifest.run_id,
            "timestamp": run_now.isoformat(),
            "source_trace_id": source_trace_id,
            "dry_run": active_dry_run,
            "dependencies": dep_result.model_dump(),
            "disk_sentinel": {
                "usage_percent": disk_report.usage_percent,
                "triggered": disk_report.triggered,
                "marked_for_deletion": [str(path) for path in disk_report.marked_for_deletion],
                "purged": [str(path) for path in disk_report.purged],
            },
            "m9": {
                "success": m9_success,
                "retry_pending": m9_retry,
                "degraded": m9_result.degraded,
                "blackout_day": m9_result.blackout_day,
                "frozen_symbols": list(m9_result.frozen_symbols),
                "freeze_reasons": m9_result.freeze_reasons,
            },
            "dag": {
                "m9_retry_count": self._dag.retry_count("M9"),
                "module_scores": module_scores,
            },
            "modules": module_details,
            "online_update_audit": dict(online_update_audit)
            if isinstance(online_update_audit, Mapping)
            else {},
            "shadow_online_report": dict(shadow_online_report)
            if isinstance(shadow_online_report, Mapping)
            else {},
            "shadow_online_v2_report": dict(shadow_online_v2_report)
            if isinstance(shadow_online_v2_report, Mapping)
            else {},
            "score_fusion": {
                "fused_score": fusion.fused_score,
                "base_score": fusion.base_score,
                "cache_key": fusion.cache_key,
                "from_cache": fusion.from_cache,
                "weights": dict(self._config.score_fusion_weights),
                "applied_rules": list(fusion.applied_rules),
                "bonus_raw": fusion.bonus_raw,
                "bonus_capped": fusion.bonus_capped,
                "veto_triggered": fusion.veto_triggered,
                "veto_module": fusion.veto_module,
                "veto_confidence": fusion.veto_confidence,
            },
            "proposal": {
                "proposal_id": proposal.proposal_id,
                "payload_uri": proposal.payload_uri,
                "change_keys": change_keys,
                "authorization_level": auth_decision.level.value,
                "auto_approved": auth_decision.auto_approved,
                "matched_rules": auth_decision.matched_rules,
            },
            "rollback": {
                **rollback.model_dump(),
                "input": {
                    "source": "m11_shadow",
                    "m11_input_source": rollback_input["m11_input_source"],
                    "m11_path": rollback_input["m11_path"],
                    "m11_path_exists": rollback_input["m11_path_exists"],
                    "loaded_samples": rollback_input["loaded_samples"],
                    "m11_status": rollback_input["m11_status"],
                    "observed_days": rollback_context.observed_days,
                    "trade_count": rollback_context.trade_count,
                    "consecutive_soft_days": rollback_context.consecutive_soft_days,
                    "consecutive_hard_days": rollback_context.consecutive_hard_days,
                    "diff_return_count": len(rollback_input["diff_returns"]),
                    "shadow_champion_vol": rollback_input["shadow_champion_vol"],
                    "hard_drawdown_breach": rollback_input["hard_drawdown_breach"],
                    "tail_loss_triggered": rollback_input["tail_loss_triggered"],
                },
            },
            "manifest_path": str(self._resolve_path(self._config.manifest_path)),
            "execution_spec_hash": spec_bundle["execution_spec_hash"],
            "runtime_config_hash": spec_bundle["runtime_config_hash"],
            "universe_spec_hash": spec_bundle["universe_spec_hash"],
            "specs": spec_bundle,
            "reproducibility": reproducibility,
            "audit_fields": audit_fields,
            "runtime_controls": runtime_controls,
            "universe": universe,
            "compliance": {
                "states": [state.value for state in compliance_states],
                "written": compliance_written,
                "db_path": str(self._resolve_path(self._config.compliance_db_path)),
            },
        }

    def run_drill(
        self,
        now: datetime | None = None,
        source_trace_id: str = "evolution-drill",
    ) -> dict[str, object]:
        """Run a deterministic end-to-end drill with synthetic records."""
        drill_records: list[dict[str, object]] = [
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 2_000_000,
            },
            {
                "symbol": "000001.SZ",
                "open": 12.0,
                "high": 12.2,
                "low": 11.7,
                "close": 12.1,
                "volume": 0,
            },
        ]
        return self.run(
            records=drill_records,
            now=now,
            dry_run=True,
            source_trace_id=source_trace_id,
        )

    def run_m3_maintenance(self, now: datetime | None = None) -> dict[str, object]:
        """Run M3 pending snapshot purge task.

        Args:
            now: Optional maintenance timestamp.

        Returns:
            Maintenance summary payload.
        """
        run_now = now or datetime.now(UTC)
        purged = self._m3_store.purge_pending(now=run_now)
        return {
            "timestamp": run_now.isoformat(),
            "purged_count": len(purged),
            "purged": [str(path) for path in purged],
        }

    def m3_search(self, query_vector: Sequence[float], top_k: int = 5) -> dict[str, object]:
        """Search M3 pattern memory by one query vector.

        Args:
            query_vector: One vector with the same dimension as M3 store.
            top_k: Number of nearest neighbors.

        Returns:
            Search result payload.
        """
        query = np.asarray([list(query_vector)], dtype=float)
        result = self._m3_store.search(query=query, top_k=top_k)
        return {
            "top_k": top_k,
            "indices": result.indices,
            "scores": result.scores,
            "total_vectors": self._m3_store.count(),
            **self._build_m3_profile_payload(),
        }

    def run_m8_suggestions(
        self,
        records: Sequence[Mapping[str, object]],
        top_k: int | None = None,
        promote_similarity: float | None = None,
        review_similarity: float | None = None,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        """Run M8 suggestion pass by directly querying M3 pattern memory.

        Args:
            records: Candidate records to evaluate.
            top_k: Optional nearest-neighbor count override.
            promote_similarity: Optional promote threshold override.
            review_similarity: Optional review threshold override.
            now: Optional timestamp override.
            source_trace_id: Optional source trace id.

        Returns:
            M8 suggestion report.
        """
        run_now = now or datetime.now(UTC)
        active_top_k = max(1, top_k if top_k is not None else self._config.m8_top_k)
        active_promote_similarity = (
            promote_similarity
            if promote_similarity is not None
            else self._config.m8_promote_similarity
        )
        active_review_similarity = (
            review_similarity
            if review_similarity is not None
            else self._config.m8_review_similarity
        )
        enriched_records, llm_semantic = self._enrich_m8_candidates_with_llm(records)
        m8_result = run_m8_memory_bridge(
            candidates=enriched_records,
            search_fn=self.m3_search,
            query_vector_builder=self._build_m8_query_vector,
            top_k=active_top_k,
            promote_similarity=active_promote_similarity,
            review_similarity=active_review_similarity,
            min_gate_passes_for_review=self._config.m8_min_gate_passes_for_review,
            pcv_min_score=self._config.m8_pcv_min_score,
            deflated_sharpe_min=self._config.m8_deflated_sharpe_min,
            fdr_alpha=self._config.m8_fdr_alpha,
            llm_min_confidence=self._config.m8_llm_min_confidence,
            noise_stability_min=self._config.m8_noise_stability_min,
            noise_trials=self._config.m8_noise_trials,
            noise_sigma=self._config.m8_noise_sigma,
            random_walk_trials=self._config.m8_random_walk_trials,
            random_walk_max_pvalue=self._config.m8_random_walk_max_pvalue,
            registry_blocked_signatures=self._config.m8_registry_blocked_signatures,
            registry_dedupe_within_run=self._config.m8_registry_dedupe_within_run,
            allow_similarity_proxies=self._config.m8_allow_similarity_proxies,
            strict_gate_inputs=self._config.m8_strict_gate_inputs,
        )
        report: dict[str, object] = {
            "timestamp": run_now.isoformat(),
            "score": m8_result.score,
            "top_k": m8_result.top_k,
            "vector_profile_id": self._m3_vector_profile.vector_profile_id,
            "promote_similarity": active_promote_similarity,
            "review_similarity": active_review_similarity,
            "summary": {
                "promoted": m8_result.promoted,
                "review": m8_result.review,
                "novel": m8_result.novel,
                "invalid": m8_result.invalid,
                "records": len(enriched_records),
                "gate_pass_rate": m8_result.gate_pass_rate,
                "gate_failure_counts": m8_result.gate_failure_counts,
                "gate_provenance_counts": m8_result.gate_provenance_counts,
                "gate_names": m8_result.gate_names,
                "min_gate_passes_for_review": self._config.m8_min_gate_passes_for_review,
                "strict_gate_inputs": m8_result.strict_gate_inputs,
                "llm_semantic": llm_semantic,
            },
            "items": [
                {
                    "symbol": item.symbol,
                    "recommendation": item.recommendation,
                    "best_similarity": item.best_similarity,
                    "indices": item.indices,
                    "scores": item.scores,
                    "total_vectors": item.total_vectors,
                    "passed_gates": item.passed_gates,
                    "gate_total": item.gate_total,
                    "failed_gates": item.failed_gates,
                    "missing_gate_inputs": item.missing_gate_inputs,
                    "derived_gate_inputs": item.derived_gate_inputs,
                    "registry_signature": item.registry_signature,
                    "gate_checks": [
                        {
                            "name": gate.name,
                            "passed": gate.passed,
                            "value": gate.value,
                            "threshold": gate.threshold,
                            "detail": gate.detail,
                            "provenance": gate.provenance,
                        }
                        for gate in item.gate_checks
                    ],
                }
                for item in m8_result.suggestions
            ],
        }
        artifact_uri = self._persist_m8_suggestions(
            run_now=run_now,
            report=report,
            source_trace_id=source_trace_id,
        )
        report["artifact_uri"] = artifact_uri
        return report

    def _build_llm_semantic_judges(
        self,
    ) -> tuple[OpenAICompatibleSemanticJudge | None, OpenAICompatibleSemanticJudge | None]:
        if not self._config.llm_semantic_enabled:
            return None, None
        primary = self._build_llm_semantic_judge(
            provider=self._config.llm_provider,
            api_key=self._config.llm_api_key,
            model=self._config.llm_model,
            base_url=self._config.llm_base_url,
        )
        backup_base_url = self._config.llm_backup_base_url.strip() or self._config.llm_base_url
        backup = self._build_llm_semantic_judge(
            provider=self._config.llm_backup_provider,
            api_key=self._config.llm_backup_api_key,
            model=self._config.llm_backup_model,
            base_url=backup_base_url,
        )
        return primary, backup

    def _build_llm_semantic_judge(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str,
    ) -> OpenAICompatibleSemanticJudge | None:
        normalized_provider = provider.strip().lower()
        if normalized_provider not in {"openai", "openai_compatible"}:
            return None
        return OpenAICompatibleSemanticJudge(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_sec=self._config.llm_timeout_sec,
            temperature=self._config.llm_temperature,
            max_tokens=self._config.llm_max_tokens,
        )

    def _enrich_m8_candidates_with_llm(
        self,
        records: Sequence[Mapping[str, object]],
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        candidates = [dict(item) for item in records]
        max_calls = max(0, self._config.llm_max_candidates_per_run)
        primary_judge = self._llm_semantic_primary_judge
        backup_judge = self._llm_semantic_backup_judge
        primary_ready = bool(primary_judge is not None and primary_judge.configured)
        backup_ready = bool(backup_judge is not None and backup_judge.configured)
        summary: dict[str, object] = {
            "enabled": self._config.llm_semantic_enabled,
            "provider": self._config.llm_provider,
            "configured": primary_ready or backup_ready,
            "fallback_enabled": primary_ready and backup_ready,
            "max_candidates_per_run": max_calls,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_existing": 0,
            "primary_calls": 0,
            "backup_calls": 0,
            "fallback_used": 0,
            "primary_failed": 0,
            "backup_failed": 0,
            "primary": {
                "provider": self._config.llm_provider,
                "model": self._config.llm_model,
                "base_url": self._config.llm_base_url,
                "configured": primary_ready,
            },
            "backup": {
                "provider": self._config.llm_backup_provider,
                "model": self._config.llm_backup_model,
                "base_url": (self._config.llm_backup_base_url.strip() or self._config.llm_base_url),
                "configured": backup_ready,
            },
        }
        if not self._config.llm_semantic_enabled:
            summary["reason"] = "disabled"
            return candidates, summary
        if not summary["configured"]:
            summary["reason"] = "missing_llm_credentials_or_model"
            return candidates, summary
        if max_calls <= 0:
            summary["reason"] = "llm_max_candidates_per_run<=0"
            return candidates, summary

        attempted = 0
        succeeded = 0
        failed = 0
        skipped_existing = 0
        primary_calls = 0
        backup_calls = 0
        fallback_used = 0
        primary_failed = 0
        backup_failed = 0
        last_error = ""
        for candidate in candidates:
            if _candidate_has_llm_gate_inputs(candidate):
                skipped_existing += 1
                continue
            if attempted >= max_calls:
                break
            attempted += 1
            decision = None
            primary_error = ""
            backup_error = ""
            if primary_ready and primary_judge is not None:
                primary_calls += 1
                primary_decision = primary_judge.judge(candidate)
                if primary_decision.error:
                    primary_failed += 1
                    primary_error = primary_decision.error
                else:
                    decision = primary_decision

            if decision is None and backup_ready and backup_judge is not None:
                backup_calls += 1
                if primary_error:
                    fallback_used += 1
                backup_decision = backup_judge.judge(candidate)
                if backup_decision.error:
                    backup_failed += 1
                    backup_error = backup_decision.error
                else:
                    decision = backup_decision

            if decision is None:
                failed += 1
                if primary_error and backup_error:
                    last_error = f"primary={primary_error};backup={backup_error}"
                elif primary_error:
                    last_error = primary_error
                elif backup_error:
                    last_error = backup_error
                continue
            candidate["llm_verdict"] = decision.verdict
            candidate["llm_confidence"] = decision.confidence
            if decision.reason:
                candidate["llm_reason"] = decision.reason
            succeeded += 1
        summary["attempted"] = attempted
        summary["succeeded"] = succeeded
        summary["failed"] = failed
        summary["skipped_existing"] = skipped_existing
        summary["primary_calls"] = primary_calls
        summary["backup_calls"] = backup_calls
        summary["fallback_used"] = fallback_used
        summary["primary_failed"] = primary_failed
        summary["backup_failed"] = backup_failed
        if last_error:
            summary["last_error"] = last_error
        return candidates, summary

    def _run_batch2_modules(
        self,
        records: Sequence[Mapping[str, object]],
        run_now: datetime,
        m9_success: bool,
        source_trace_id: str,
        run_id: str = "",
    ) -> tuple[dict[str, float], dict[str, object], list[ManifestCheckpoint]]:
        checkpoints: list[ManifestCheckpoint] = []
        latency_result = evaluate_latency_slo(
            records=records,
            now=run_now,
            required_inputs=tuple(self._config.runtime_spec.latency_required_inputs),
            max_data_latency_sec=float(self._config.runtime_spec.max_data_latency_sec),
            previous_breach_history=self._latency_breach_history,
        )
        self._latency_breach_history = list(latency_result.breach_history)
        sensitivity_result = evaluate_execution_sensitivity(
            records=records,
            sensitivity_threshold_bp=float(self._config.execution_spec.sensitivity_threshold_bp),
            sensitivity_days=int(self._config.execution_spec.sensitivity_days),
            previous_breach_history=self._execution_sensitivity_history,
        )
        self._execution_sensitivity_history = list(sensitivity_result.breach_history)
        if not m9_success:
            degraded_reason = "m9_failed"
            degraded_at = run_now.isoformat()

            m2_observation = self._build_m2_observation(records=records)
            self._m2_observation_history.append(m2_observation)
            history_limit = max(8, self._config.m2_optuna_history_limit)
            if len(self._m2_observation_history) > history_limit:
                overflow = len(self._m2_observation_history) - history_limit
                if overflow > 0:
                    self._m2_observation_history = self._m2_observation_history[overflow:]
            m2_optuna = self._run_m2_optuna_like_tuning(now=run_now)
            m2_result = evaluate_m2_regime(
                controller=self._m2_controller,
                observation=m2_observation,
            )
            m2_artifact_uri = self._persist_m2_params_snapshot(
                run_now=run_now,
                source_trace_id=source_trace_id,
                optuna_result=m2_optuna,
            )
            self._persist_m2_state(now=run_now)

            m10_result = evaluate_m10_model_health(
                records=records,
                conflict_warn=self._config.m10_conflict_warn,
                calibration_gap_warn=self._config.m10_calibration_gap_warn,
                return_volatility_warn=self._config.m10_return_volatility_warn,
                conflict_watch_ratio=self._config.m10_conflict_watch_ratio,
                conflict_degraded_ratio=self._config.m10_conflict_degraded_ratio,
                calibration_degraded_multiplier=self._config.m10_calibration_degraded_multiplier,
                limited_observability_score=self._config.m10_limited_observability_score,
            )
            m10_health_status = m10_result.status
            m10_score = m10_result.score
            if latency_result.limited_observability:
                m10_health_status = "limited_observability"
                m10_score = min(m10_score, 65.0)
            elif latency_result.latency_watch_flag and m10_health_status == "healthy":
                m10_health_status = "watch"
            if sensitivity_result.execution_sensitivity_alert and m10_health_status == "healthy":
                m10_health_status = "watch"

            m11_observations, m11_input = self._resolve_m11_observations(records=records)
            m11_result = evaluate_m11_shadow_portfolio(
                shadow_observations=m11_observations,
                drawdown_delta_limit=self._config.m11_drawdown_delta_limit,
                tail_loss_delta_limit=self._config.m11_tail_loss_delta_limit,
                execution_divergence_limit=self._config.m11_execution_divergence_limit,
            )

            checkpoints.extend(
                [
                    ManifestCheckpoint(step="M4", status="skipped_by_m9", timestamp=run_now),
                    ManifestCheckpoint(step="M1", status="skipped_by_m9", timestamp=run_now),
                    ManifestCheckpoint(step="M2", status="degraded_run", timestamp=run_now),
                    ManifestCheckpoint(step="M5", status="skipped_by_m9", timestamp=run_now),
                    ManifestCheckpoint(step="M3", status="skipped_by_m9", timestamp=run_now),
                    ManifestCheckpoint(step="M6", status="skipped_by_m9", timestamp=run_now),
                    ManifestCheckpoint(step="M7", status="skipped_by_m9", timestamp=run_now),
                    ManifestCheckpoint(step="M10", status="degraded_run", timestamp=run_now),
                    ManifestCheckpoint(
                        step="shadow_online_model",
                        status="degraded_run",
                        timestamp=run_now,
                    ),
                    ManifestCheckpoint(
                        step="shadow_online_model_v2",
                        status="degraded_run",
                        timestamp=run_now,
                    ),
                    ManifestCheckpoint(step="M11", status="degraded_run", timestamp=run_now),
                    ManifestCheckpoint(step="M8", status="skipped_by_m9", timestamp=run_now),
                ]
            )
            return (
                {
                    "M4": 40.0,
                    "M1": 35.0,
                    "M2": m2_result.score,
                    "M5": 35.0,
                    "M3": 35.0,
                    "M6": 35.0,
                    "M7": 35.0,
                    "M10": m10_score,
                    "M11": m11_result.score,
                    "M8": 35.0,
                },
                {
                    "m4": {"status": "skipped_by_m9"},
                    "m1": {"status": "skipped_by_m9"},
                    "m2": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "score": m2_result.score,
                        "active_state": m2_result.snapshot.active_state,
                        "switched": m2_result.snapshot.switched,
                        "pending_state": m2_result.snapshot.pending_state,
                        "pending_days": m2_result.snapshot.pending_days,
                        "confidence": m2_result.snapshot.confidence,
                        "confidence_tier": m2_result.snapshot.confidence_tier,
                        "artifact_uri": m2_artifact_uri,
                        "params": self._m2_controller.dump_params(),
                        "optuna": m2_optuna.to_dict(),
                    },
                    "online_update": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "block_online_update": True,
                        "blocked_reasons": [degraded_reason],
                        "online_samples_used": 0,
                        "online_samples_used_hash": "",
                        "online_samples_skipped": 0,
                        "online_samples_downweighted": 0,
                        "online_update_budget_ratio": 0.0,
                        "max_online_samples_per_day": max(
                            1,
                            int(self._config.runtime_spec.max_online_samples_per_day),
                        ),
                        "cooldown_days": max(1, int(self._config.runtime_spec.cooldown_days)),
                        "cooldown_remaining_days": 0,
                        "online_handoff_mode": "rebase_then_replay",
                        "replay_diff_p_meta_p50": 0.0,
                        "replay_diff_p_meta_p90": 0.0,
                        "replay_diff_p_meta_max": 0.0,
                        "replay_diff_turnover": 0.0,
                        "online_handoff_warning": False,
                        "rollback_trigger_source": "",
                        "tier_b_promoted": False,
                        "promotion_candidate": False,
                        "promotion_decision": "hold",
                        "promotion_reason_codes": [],
                        "promotion_revoked": False,
                        "revocation_reason_codes": [],
                        "deterministic_order_fields": [
                            "label_mature_time",
                            "trade_date",
                            "symbol",
                        ],
                        "deterministic_order_applied": True,
                        "skipped_not_matured": 0,
                        "skipped_invalid": 0,
                    },
                    "shadow_online_model": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "engine": "river_compatible_stub_logistic_v1",
                        "shadow_mode": True,
                        "affects_main_model": False,
                        "samples_considered": 0,
                        "samples_used": 0,
                        "metrics": {
                            "valid_samples": 0,
                            "updates_applied": 0,
                            "shadow_logloss": 0.0,
                            "baseline_logloss": 0.0,
                            "delta_logloss": 0.0,
                            "shadow_brier": 0.0,
                            "baseline_brier": 0.0,
                            "delta_brier": 0.0,
                            "shadow_accuracy": 0.0,
                            "baseline_accuracy": 0.0,
                            "avg_shadow_probability": 0.0,
                            "avg_baseline_probability": 0.0,
                        },
                        "reasons": [degraded_reason],
                        "preview": [],
                        "guardrail_context": {
                            "block_online_update": True,
                            "blocked_reasons": [degraded_reason],
                        },
                        "artifact_uri": "",
                    },
                    "shadow_online_model_v2": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "engine": "protocol_shadow_online_v2_lr",
                        "shadow_mode": True,
                        "affects_main_model": False,
                        "samples_considered": 0,
                        "samples_used": 0,
                        "metrics": {
                            "valid_samples": 0,
                            "updates_applied": 0,
                            "shadow_logloss": 0.0,
                            "baseline_logloss": 0.0,
                            "delta_logloss": 0.0,
                            "shadow_brier": 0.0,
                            "baseline_brier": 0.0,
                            "delta_brier": 0.0,
                            "shadow_accuracy": 0.0,
                            "baseline_accuracy": 0.0,
                            "avg_shadow_probability": 0.0,
                            "avg_baseline_probability": 0.0,
                            "avg_sample_weight": 0.0,
                            "avg_execution_fill_ratio": 0.0,
                            "avg_realized_slippage_bp": 0.0,
                            "signal_divergence_ratio": 0.0,
                        },
                        "reasons": [degraded_reason],
                        "preview": [],
                        "guardrail_context": {
                            "block_online_update": True,
                            "blocked_reasons": [degraded_reason],
                        },
                        "artifact_uri": "",
                    },
                    "eval_profiles": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "profile_set_id": self._config.runtime_spec.dual_eval_profile_set_id,
                        "profiles": {
                            "trading_eval_profile": {
                                "samples": 0,
                                "no_fill_count": 0,
                                "partial_fill_count": 0,
                                "full_fill_count": 0,
                                "no_fill_ratio": 0.0,
                                "partial_fill_ratio": 0.0,
                            },
                            "stockpick_eval_profile": {
                                "samples": 0,
                                "no_fill_count": 0,
                                "partial_fill_count": 0,
                                "full_fill_count": 0,
                                "no_fill_ratio": 0.0,
                                "partial_fill_ratio": 0.0,
                            },
                        },
                        "trading_distribution_gate": {
                            "pass": True,
                            "pass_absolute": True,
                            "pass_delta": True,
                            "reason_codes": [],
                            "no_fill_ratio_limit": (
                                self._config.runtime_spec.trading_no_fill_ratio_limit
                            ),
                            "partial_fill_ratio_limit": (
                                self._config.runtime_spec.trading_partial_fill_ratio_limit
                            ),
                            "no_fill_ratio_delta_limit": (
                                self._config.runtime_spec.trading_no_fill_ratio_delta_limit
                            ),
                            "partial_fill_ratio_delta_limit": (
                                self._config.runtime_spec.trading_partial_fill_ratio_delta_limit
                            ),
                        },
                        "no_fill_ratio": 0.0,
                        "partial_fill_ratio": 0.0,
                        "no_fill_ratio_delta": 0.0,
                        "partial_fill_ratio_delta": 0.0,
                    },
                    "utility_execution": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "mapping_level_used": "regime_x_liquidity_x_volatility",
                        "bucket_sample_count": 0,
                        "mapping_fallback_steps": [],
                        "k_base": self._config.runtime_spec.k_base,
                        "k_dynamic": self._config.runtime_spec.k_base,
                        "k_min": self._config.runtime_spec.k_min,
                        "constraint_pressure": 0.0,
                        "alpha": 1.0,
                        "trim_reason_codes": [],
                        "negative_u_filtered_count": 0,
                    },
                    "reconcile_drift": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "target_vs_filled_weight_p50": 0.0,
                        "target_vs_filled_weight_p90": 0.0,
                        "target_vs_filled_weight_max": 0.0,
                        "filled_vs_eod_weight_p50": 0.0,
                        "filled_vs_eod_weight_p90": 0.0,
                        "filled_vs_eod_weight_max": 0.0,
                        "position_drift_ratio": 0.0,
                        "position_drift_alert": False,
                        "position_drift_consecutive_days": 0,
                        "raise_u_threshold_bp_recommendation": 0,
                    },
                    "hard_gates": {
                        "status": "degraded_run",
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "all_passed": True,
                        "failed_gate_count": 0,
                        "failed_gates": [],
                        "checks": [],
                    },
                    "m5": {"status": "skipped_by_m9"},
                    "m3": {"status": "skipped_by_m9"},
                    "m6": {"status": "skipped_by_m9"},
                    "m7": {"status": "skipped_by_m9"},
                    "m10": {
                        "score": m10_score,
                        "status": "degraded_run",
                        "health_status": m10_health_status,
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "metrics": {
                            "valid_symbols": m10_result.metrics.valid_symbols,
                            "prediction_coverage_ratio": (
                                m10_result.metrics.prediction_coverage_ratio
                            ),
                            "mean_model_spread": m10_result.metrics.mean_model_spread,
                            "high_conflict_ratio": m10_result.metrics.high_conflict_ratio,
                            "calibration_gap": m10_result.metrics.calibration_gap,
                            "return_volatility": m10_result.metrics.return_volatility,
                            "data_latency_sec": latency_result.data_latency_sec,
                            "max_data_latency_sec": latency_result.max_data_latency_sec,
                            "latency_by_input": latency_result.latency_by_input,
                            "latency_worst_input": latency_result.latency_worst_input,
                            "data_latency_breach_ratio_20d": latency_result.breach_ratio_20d,
                            "latency_watch_flag": latency_result.latency_watch_flag,
                            "execution_sensitivity_max_diff_bp": sensitivity_result.max_diff_bp,
                            "execution_sensitivity_mean_diff_bp": sensitivity_result.mean_diff_bp,
                            "execution_sensitivity_run_breach": sensitivity_result.run_breach,
                            "execution_sensitivity_consecutive_days": (
                                sensitivity_result.consecutive_breach_days
                            ),
                            "execution_sensitivity_alert": (
                                sensitivity_result.execution_sensitivity_alert
                            ),
                        },
                        "latency_action": {
                            "state": latency_result.action.state,
                            "block_online_update": latency_result.action.block_online_update,
                            "raise_u_threshold_bp": latency_result.action.raise_u_threshold_bp,
                            "force_champion_only": latency_result.action.force_champion_only,
                        },
                        "execution_sensitivity": {
                            "max_diff_bp": sensitivity_result.max_diff_bp,
                            "mean_diff_bp": sensitivity_result.mean_diff_bp,
                            "run_breach": sensitivity_result.run_breach,
                            "consecutive_breach_days": sensitivity_result.consecutive_breach_days,
                            "execution_sensitivity_alert": (
                                sensitivity_result.execution_sensitivity_alert
                            ),
                            "sensitivity_threshold_bp": (
                                sensitivity_result.sensitivity_threshold_bp
                            ),
                            "sensitivity_days": sensitivity_result.sensitivity_days,
                            "worst_symbol": sensitivity_result.worst_symbol,
                            "breached_symbols": sensitivity_result.breached_symbols,
                        },
                    },
                    "m11": {
                        "score": m11_result.score,
                        "status": "degraded_run",
                        "shadow_status": m11_result.status,
                        "degraded_reason": degraded_reason,
                        "degraded_at": degraded_at,
                        "input": m11_input,
                        "redlines": m11_result.redlines,
                        "metrics": {
                            "valid_samples": m11_result.metrics.valid_samples,
                            "champion_cum_return": m11_result.metrics.champion_cum_return,
                            "challenger_cum_return": m11_result.metrics.challenger_cum_return,
                            "champion_max_drawdown": m11_result.metrics.champion_max_drawdown,
                            "challenger_max_drawdown": m11_result.metrics.challenger_max_drawdown,
                            "drawdown_delta": m11_result.metrics.drawdown_delta,
                            "tail_loss_delta": m11_result.metrics.tail_loss_delta,
                            "execution_divergence_ratio": (
                                m11_result.metrics.execution_divergence_ratio
                            ),
                            "champion_win_rate": m11_result.metrics.champion_win_rate,
                            "challenger_win_rate": m11_result.metrics.challenger_win_rate,
                        },
                        "attribution": [
                            {
                                "name": item.name,
                                "value": item.value,
                                "threshold": item.threshold,
                                "breached": item.breached,
                                "impact": item.impact,
                            }
                            for item in m11_result.attribution
                        ],
                    },
                    "m8": {"status": "skipped_by_m9"},
                },
                checkpoints,
            )

        m4_result = evaluate_m4_capital_flow(
            records=records,
            inflow_ratio_gate=self._config.m4_inflow_ratio_gate,
            concentration_warn=self._config.m4_concentration_warn,
            score_concentration_penalty=self._config.m4_score_concentration_penalty,
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="M4",
                status="warn" if m4_result.status == "outflow_dominant" else "ok",
                timestamp=run_now,
            )
        )

        m1_result = run_m1_dual_learning(
            records=records,
            asof_date=run_now.date(),
            shared_dir=self._resolve_path(f"{self._config.suggestions_dir}/shared"),
            now=run_now,
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="M1",
                status="ok" if m1_result.asof_pass else "warn",
                timestamp=run_now,
            )
        )

        m2_observation = self._build_m2_observation(records=records)
        self._m2_observation_history.append(m2_observation)
        history_limit = max(8, self._config.m2_optuna_history_limit)
        if len(self._m2_observation_history) > history_limit:
            overflow = len(self._m2_observation_history) - history_limit
            if overflow > 0:
                self._m2_observation_history = self._m2_observation_history[overflow:]
        m2_optuna = self._run_m2_optuna_like_tuning(now=run_now)
        m2_result = evaluate_m2_regime(
            controller=self._m2_controller,
            observation=m2_observation,
        )
        m2_artifact_uri = self._persist_m2_params_snapshot(
            run_now=run_now,
            source_trace_id=source_trace_id,
            optuna_result=m2_optuna,
        )
        self._persist_m2_state(now=run_now)
        checkpoints.append(ManifestCheckpoint(step="M2", status="ok", timestamp=run_now))

        m5_records, m5_input = self._resolve_m5_records(records=records)
        online_sample_audit = build_online_sample_audit(records=m5_records, now=run_now)
        m5_result = evaluate_m5_label_optimization(
            records=m5_records,
            label_coverage_floor=self._config.m5_label_coverage_floor,
            positive_ratio_low=self._config.m5_positive_ratio_low,
            positive_ratio_high=self._config.m5_positive_ratio_high,
            seed_consistency_floor=self._config.m5_seed_consistency_floor,
            alignment_floor=self._config.m5_alignment_floor,
            limited_observability_score=self._config.m5_limited_observability_score,
        )
        m5_linkage = build_m5_strategy_linkage(
            result=m5_result,
            min_labeled_samples=self._config.m5_strategy_min_labeled_samples,
            target_strategy="soup",
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="M5",
                status="ok" if m5_result.status == "optimized" else "warn",
                timestamp=run_now,
            )
        )
        eval_profiles = evaluate_dual_eval_profiles(
            records=records,
            profile_set_id=self._config.runtime_spec.dual_eval_profile_set_id,
            no_fill_ratio_limit=self._config.runtime_spec.trading_no_fill_ratio_limit,
            partial_fill_ratio_limit=self._config.runtime_spec.trading_partial_fill_ratio_limit,
            no_fill_ratio_delta_limit=self._config.runtime_spec.trading_no_fill_ratio_delta_limit,
            partial_fill_ratio_delta_limit=(
                self._config.runtime_spec.trading_partial_fill_ratio_delta_limit
            ),
            baseline_no_fill_ratio=self._config.runtime_spec.trading_no_fill_ratio_baseline,
            baseline_partial_fill_ratio=self._config.runtime_spec.trading_partial_fill_ratio_baseline,
        )
        self._eval_profiles_report = dict(eval_profiles.report)
        utility_execution = evaluate_utility_execution_policy(
            records=records,
            min_samples_per_bucket=self._config.runtime_spec.min_samples_per_bucket,
            mapping_fallback_order=self._config.runtime_spec.mapping_fallback_order,
            mapping_update_cooldown_days=self._config.runtime_spec.mapping_update_cooldown_days,
            mapping_ema_alpha=self._config.runtime_spec.mapping_ema_alpha,
            k_base=self._config.runtime_spec.k_base,
            k_min=self._config.runtime_spec.k_min,
            turnover_limit=self._config.runtime_spec.dynamic_k_turnover_limit,
            participation_cap=self._config.runtime_spec.dynamic_k_participation_cap,
            previous_state=self._utility_execution_state,
        )
        self._utility_execution_report = dict(utility_execution.report)
        self._utility_execution_state = dict(utility_execution.state)
        reconcile_drift = evaluate_daily_reconcile_drift(
            records=records,
            now=run_now,
            model_bundle_hash=self._config.code_commit_id,
            position_drift_alert_threshold=self._config.runtime_spec.position_drift_alert_threshold,
            position_drift_raise_u_threshold_bp=(
                self._config.runtime_spec.position_drift_raise_u_threshold_bp
            ),
            position_drift_consecutive_days_trigger=(
                self._config.runtime_spec.position_drift_consecutive_days_trigger
            ),
            previous_state=self._reconcile_drift_state,
        )
        self._reconcile_drift_report = dict(reconcile_drift.report)
        self._reconcile_drift_state = dict(reconcile_drift.state)

        m3_vectors = self._m3_vectors(records=records)
        m3_append = self._m3_store.append(vectors=m3_vectors)
        if run_now.weekday() >= 5:
            self._m3_store.create_snapshot(now=run_now)
        checkpoints.append(ManifestCheckpoint(step="M3", status="ok", timestamp=run_now))

        m6_result = evaluate_m6_counterparty(
            records=records,
            sell_pressure_gate=self._config.m6_sell_pressure_gate,
            bearish_ratio_gate=self._config.m6_bearish_ratio_gate,
            rejection_shadow_gate=self._config.m6_rejection_shadow_gate,
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="M6",
                status="warn" if m6_result.status == "heavy_sell_pressure" else "ok",
                timestamp=run_now,
            )
        )

        m7_records, m7_input = self._resolve_m7_records(records=records)
        m7_result = evaluate_m7_news_sentiment(
            records=m7_records,
            dedup_similarity_threshold=self._config.m7_dedup_similarity_threshold,
            daily_budget=self._config.m7_daily_budget,
            default_event_cost=self._config.m7_default_event_cost,
            sentiment_floor=self._config.m7_sentiment_floor,
            budget_warn_utilization=self._config.m7_budget_warn_utilization,
            max_clusters_in_report=self._config.m7_max_clusters_in_report,
            embedding_backend=self._config.m7_embedding_backend,
            embedding_dim=self._config.m7_embedding_dim,
            embedding_required=self._config.m7_embedding_required,
        )
        m7_ledger_report = self._m7_event_ledger.record_run(
            records=m7_records,
            now=run_now,
            price_by_symbol=self._build_symbol_price_lookup(records),
            source_trace_id=source_trace_id,
            regime_state=m2_result.snapshot.active_state,
        )
        m7_ledger_payload = m7_ledger_report.to_dict()
        raw_m7_paths = m7_ledger_payload.get("paths")
        if isinstance(raw_m7_paths, dict):
            raw_db_path = raw_m7_paths.get("db_path")
            if isinstance(raw_db_path, str):
                raw_m7_paths["db_path"] = self._to_relative_if_possible(raw_db_path) or raw_db_path
            raw_archive_dir = raw_m7_paths.get("archive_dir")
            if isinstance(raw_archive_dir, str):
                raw_m7_paths["archive_dir"] = (
                    self._to_relative_if_possible(raw_archive_dir) or raw_archive_dir
                )
            raw_archive_paths = raw_m7_paths.get("archive_paths")
            if isinstance(raw_archive_paths, list):
                raw_m7_paths["archive_paths"] = [
                    self._to_relative_if_possible(item) or item
                    for item in raw_archive_paths
                    if isinstance(item, str)
                ]
        m7_metrics_payload = {
            "total_events": m7_result.metrics.total_events,
            "valid_events": m7_result.metrics.valid_events,
            "unique_events": m7_result.metrics.unique_events,
            "deduplicated_events": m7_result.metrics.deduplicated_events,
            "dropped_by_budget": m7_result.metrics.dropped_by_budget,
            "budget_spend": m7_result.metrics.budget_spend,
            "budget_utilization": m7_result.metrics.budget_utilization,
            "mean_sentiment": m7_result.metrics.mean_sentiment,
            "mean_abs_sentiment": m7_result.metrics.mean_abs_sentiment,
            "positive_ratio": m7_result.metrics.positive_ratio,
            "negative_ratio": m7_result.metrics.negative_ratio,
            "symbol_coverage": m7_result.metrics.symbol_coverage,
            "embedding_events": m7_result.metrics.embedding_events,
            "provided_embeddings": m7_result.metrics.provided_embeddings,
            "generated_embeddings": m7_result.metrics.generated_embeddings,
            "embedding_coverage_ratio": m7_result.metrics.embedding_coverage_ratio,
            "embedding_backend": m7_result.metrics.embedding_backend,
        }
        m7_cluster_payload = [
            {
                "cluster_id": item.cluster_id,
                "representative_headline": item.representative_headline,
                "symbols": item.symbols,
                "members": item.members,
                "mean_sentiment": item.mean_sentiment,
                "cost": item.cost,
            }
            for item in m7_result.clusters
        ]
        checkpoints.append(
            ManifestCheckpoint(
                step="M7",
                status="warn" if m7_result.status in {"budget_capped", "degraded"} else "ok",
                timestamp=run_now,
            )
        )
        m7_report: dict[str, object] = {
            "timestamp": run_now.isoformat(),
            "score": m7_result.score,
            "status": m7_result.status,
            "input": m7_input,
            "metrics": m7_metrics_payload,
            "clusters": m7_cluster_payload,
            "ledger": m7_ledger_payload,
        }
        m7_artifact_uri = self._persist_m7_report(
            run_now=run_now,
            report=m7_report,
            source_trace_id=source_trace_id,
        )

        m10_result = evaluate_m10_model_health(
            records=records,
            conflict_warn=self._config.m10_conflict_warn,
            calibration_gap_warn=self._config.m10_calibration_gap_warn,
            return_volatility_warn=self._config.m10_return_volatility_warn,
            conflict_watch_ratio=self._config.m10_conflict_watch_ratio,
            conflict_degraded_ratio=self._config.m10_conflict_degraded_ratio,
            calibration_degraded_multiplier=self._config.m10_calibration_degraded_multiplier,
            limited_observability_score=self._config.m10_limited_observability_score,
        )
        m10_status = m10_result.status
        m10_score = m10_result.score
        if latency_result.limited_observability:
            m10_status = "limited_observability"
            m10_score = min(m10_score, 65.0)
        elif latency_result.latency_watch_flag and m10_status == "healthy":
            m10_status = "watch"
        if sensitivity_result.execution_sensitivity_alert and m10_status == "healthy":
            m10_status = "watch"
        shadow_online_result = run_shadow_online_model(
            records=m5_records,
            now=run_now,
            previous_state=self._shadow_online_state,
            max_samples=self._config.runtime_spec.max_online_samples_per_day,
            min_samples=self._config.shadow_online_min_samples,
            learning_rate=self._config.shadow_online_learning_rate,
            preview_limit=self._config.shadow_online_max_preview,
        )
        self._shadow_online_state = dict(shadow_online_result.state)
        self._persist_shadow_online_state(now=run_now)
        shadow_online_report = shadow_online_result_to_dict(shadow_online_result)
        shadow_online_v2_result = run_shadow_online_model_v2(
            records=m5_records,
            now=run_now,
            previous_state=self._shadow_online_v2_state,
            max_samples=self._config.runtime_spec.max_online_samples_per_day,
            min_samples=self._config.shadow_online_min_samples,
            learning_rate=self._config.shadow_online_learning_rate,
            preview_limit=self._config.shadow_online_max_preview,
            signal_threshold=0.5,
        )
        self._shadow_online_v2_state = dict(shadow_online_v2_result.state)
        self._persist_shadow_online_v2_state(
            now=run_now,
            source_trace_id=source_trace_id,
            run_id=run_id,
        )
        shadow_online_v2_report = shadow_online_v2_result_to_dict(shadow_online_v2_result)
        execution_promotion_gate = self._build_execution_promotion_gate(
            eval_profiles=eval_profiles.report,
            shadow_online_v2_report=shadow_online_v2_report,
        )
        reconcile_promotion_gate = self._build_reconcile_promotion_gate(
            reconcile_drift=reconcile_drift.report
        )
        online_policy = run_online_partial_fit_policy(
            records=m5_records,
            now=run_now,
            sample_audit=online_sample_audit,
            m10_status=m10_status,
            latency_block_online_update=bool(latency_result.action.block_online_update),
            max_online_samples_per_day=self._config.runtime_spec.max_online_samples_per_day,
            cooldown_days=self._config.runtime_spec.cooldown_days,
            promotion_min_healthy_days=self._config.runtime_spec.promotion_min_healthy_days,
            execution_promotion_gate=execution_promotion_gate,
            reconcile_promotion_gate=reconcile_promotion_gate,
            previous_state=self._online_update_state,
        )
        self._online_update_state = dict(online_policy.state)
        blocked_reasons_raw = online_policy.report.get("blocked_reasons", [])
        blocked_reasons = (
            [str(item).strip() for item in blocked_reasons_raw if str(item).strip()]
            if isinstance(blocked_reasons_raw, list)
            else []
        )
        shadow_online_report["guardrail_context"] = {
            "block_online_update": bool(online_policy.report.get("block_online_update", False)),
            "blocked_reasons": blocked_reasons,
        }
        shadow_online_artifact_uri = self._persist_shadow_online_report(
            run_now=run_now,
            report=shadow_online_report,
            source_trace_id=source_trace_id,
        )
        shadow_online_v2_report["guardrail_context"] = {
            "block_online_update": bool(online_policy.report.get("block_online_update", False)),
            "blocked_reasons": blocked_reasons,
        }
        self._shadow_online_v2_metrics_store.append_run(
            result=shadow_online_v2_report,
            now=run_now,
            metadata={
                "run_id": run_id,
                "source_trace_id": source_trace_id,
            },
        )
        shadow_online_v2_artifact_uri = self._persist_shadow_online_report(
            run_now=run_now,
            report=shadow_online_v2_report,
            source_trace_id=source_trace_id,
            report_kind="shadow_online_v2",
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="M10",
                status="ok" if m10_status == "healthy" else "warn",
                timestamp=run_now,
            )
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="shadow_online_model",
                status="ok" if shadow_online_result.status == "updated" else "warn",
                timestamp=run_now,
            )
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="shadow_online_model_v2",
                status="ok" if shadow_online_v2_result.status == "updated" else "warn",
                timestamp=run_now,
            )
        )

        m11_observations, m11_input = self._resolve_m11_observations(records=records)
        m11_result = evaluate_m11_shadow_portfolio(
            shadow_observations=m11_observations,
            drawdown_delta_limit=self._config.m11_drawdown_delta_limit,
            tail_loss_delta_limit=self._config.m11_tail_loss_delta_limit,
            execution_divergence_limit=self._config.m11_execution_divergence_limit,
        )
        checkpoints.append(
            ManifestCheckpoint(
                step="M11",
                status="warn" if m11_result.status == "redline_breach" else "ok",
                timestamp=run_now,
            )
        )

        m8_report = self.run_m8_suggestions(
            records=records,
            now=run_now,
        )
        checkpoints.append(ManifestCheckpoint(step="M8", status="ok", timestamp=run_now))
        m8_summary = m8_report.get("summary", {})
        promoted = _as_float(
            m8_summary.get("promoted") if isinstance(m8_summary, Mapping) else 0.0,
            default=0.0,
        )
        review = _as_float(
            m8_summary.get("review") if isinstance(m8_summary, Mapping) else 0.0,
            default=0.0,
        )
        records_count = _as_float(
            m8_summary.get("records") if isinstance(m8_summary, Mapping) else 0.0,
            default=0.0,
        )
        m8_coverage = (promoted + review) / max(records_count, 1.0)

        m3_score = min(100.0, 40.0 + np.log1p(max(m3_append.total, 0)) * 10.0)
        m8_score = _as_float(m8_report.get("score"), default=0.0)
        module_scores: dict[str, float] = {
            "M4": m4_result.score,
            "M1": m1_result.score,
            "M2": m2_result.score,
            "M5": m5_result.score,
            "M3": float(m3_score),
            "M6": m6_result.score,
            "M7": m7_result.score,
            "M10": m10_score,
            "M11": m11_result.score,
            "M8": m8_score,
        }
        module_details: dict[str, object] = {
            "m4": {
                "score": m4_result.score,
                "status": m4_result.status,
                "metrics": {
                    "net_flow_ratio": m4_result.metrics.net_flow_ratio,
                    "breadth_ratio": m4_result.metrics.breadth_ratio,
                    "concentration": m4_result.metrics.concentration,
                    "inflow_symbols": m4_result.metrics.inflow_symbols,
                    "outflow_symbols": m4_result.metrics.outflow_symbols,
                    "valid_symbols": m4_result.metrics.valid_symbols,
                    "estimated_turnover": m4_result.metrics.estimated_turnover,
                    "positive_flow": m4_result.metrics.positive_flow,
                    "negative_flow_abs": m4_result.metrics.negative_flow_abs,
                },
            },
            "m1": {
                "status": "ok" if m1_result.asof_pass else "warn",
                "score": m1_result.score,
                "asof_pass": m1_result.asof_pass,
                "poison_hits": m1_result.poison_hits,
                "bucket_counts": m1_result.bucket_counts,
                "negative_case_count": m1_result.negative_case_count,
                "reason_counts": m1_result.reason_counts,
                "cases_preview": m1_result.cases_preview,
                "shared_payload_uri": self._to_relative_if_possible(m1_result.shared_payload_uri),
            },
            "m2": {
                "score": m2_result.score,
                "active_state": m2_result.snapshot.active_state,
                "switched": m2_result.snapshot.switched,
                "pending_state": m2_result.snapshot.pending_state,
                "pending_days": m2_result.snapshot.pending_days,
                "confidence": m2_result.snapshot.confidence,
                "confidence_tier": m2_result.snapshot.confidence_tier,
                "artifact_uri": m2_artifact_uri,
                "params": self._m2_controller.dump_params(),
                "optuna": m2_optuna.to_dict(),
            },
            "m5": {
                "score": m5_result.score,
                "status": m5_result.status,
                "input": m5_input,
                "metrics": {
                    "valid_symbols": m5_result.metrics.valid_symbols,
                    "labeled_samples": m5_result.metrics.labeled_samples,
                    "label_coverage_ratio": m5_result.metrics.label_coverage_ratio,
                    "positive_label_ratio": m5_result.metrics.positive_label_ratio,
                    "label_balance_score": m5_result.metrics.label_balance_score,
                    "seed_consistency": m5_result.metrics.seed_consistency,
                    "seed_observability_ratio": m5_result.metrics.seed_observability_ratio,
                    "return_alignment": m5_result.metrics.return_alignment,
                    "alignment_samples": m5_result.metrics.alignment_samples,
                },
                "strategy_linkage": {
                    "mode": m5_linkage.mode,
                    "target_strategy": m5_linkage.target_strategy,
                    "confidence": m5_linkage.confidence,
                    "reason": m5_linkage.reason,
                    "change_keys": m5_linkage.change_keys,
                    "suggested_overrides": m5_linkage.suggested_overrides,
                },
            },
            "eval_profiles": dict(eval_profiles.report),
            "utility_execution": dict(utility_execution.report),
            "reconcile_drift": dict(reconcile_drift.report),
            "online_update": {
                **online_policy.report,
                "skipped_not_matured": online_sample_audit.skipped_not_matured,
                "skipped_invalid": online_sample_audit.skipped_invalid,
            },
            "shadow_online_model": {
                **shadow_online_report,
                "artifact_uri": shadow_online_artifact_uri,
            },
            "shadow_online_model_v2": {
                **shadow_online_v2_report,
                "artifact_uri": shadow_online_v2_artifact_uri,
            },
            "m3": {
                "appended": m3_append.appended,
                "total_vectors": m3_append.total,
                "batch_size": m3_append.batch_size,
                "used_faiss": m3_append.used_faiss,
                **self._build_m3_profile_payload(),
                "registry_db_path": self._to_relative_if_possible(
                    str(self._m3_vector_profile_db_path)
                ),
            },
            "m6": {
                "score": m6_result.score,
                "status": m6_result.status,
                "metrics": {
                    "pressure_index": m6_result.metrics.pressure_index,
                    "bearish_ratio": m6_result.metrics.bearish_ratio,
                    "rejection_ratio": m6_result.metrics.rejection_ratio,
                    "close_near_low_ratio": m6_result.metrics.close_near_low_ratio,
                    "valid_symbols": m6_result.metrics.valid_symbols,
                },
            },
            "m7": {
                "score": m7_result.score,
                "status": m7_result.status,
                "input": m7_input,
                "artifact_uri": m7_artifact_uri,
                "metrics": m7_metrics_payload,
                "clusters": m7_cluster_payload,
                "ledger": m7_ledger_payload,
            },
            "m10": {
                "score": m10_score,
                "status": m10_status,
                "metrics": {
                    "valid_symbols": m10_result.metrics.valid_symbols,
                    "prediction_coverage_ratio": m10_result.metrics.prediction_coverage_ratio,
                    "mean_model_spread": m10_result.metrics.mean_model_spread,
                    "high_conflict_ratio": m10_result.metrics.high_conflict_ratio,
                    "calibration_gap": m10_result.metrics.calibration_gap,
                    "return_volatility": m10_result.metrics.return_volatility,
                    "data_latency_sec": latency_result.data_latency_sec,
                    "max_data_latency_sec": latency_result.max_data_latency_sec,
                    "latency_by_input": latency_result.latency_by_input,
                    "latency_worst_input": latency_result.latency_worst_input,
                    "data_latency_breach_ratio_20d": latency_result.breach_ratio_20d,
                    "latency_watch_flag": latency_result.latency_watch_flag,
                    "execution_sensitivity_max_diff_bp": sensitivity_result.max_diff_bp,
                    "execution_sensitivity_mean_diff_bp": sensitivity_result.mean_diff_bp,
                    "execution_sensitivity_run_breach": sensitivity_result.run_breach,
                    "execution_sensitivity_consecutive_days": (
                        sensitivity_result.consecutive_breach_days
                    ),
                    "execution_sensitivity_alert": sensitivity_result.execution_sensitivity_alert,
                },
                "latency_action": {
                    "state": latency_result.action.state,
                    "block_online_update": latency_result.action.block_online_update,
                    "raise_u_threshold_bp": latency_result.action.raise_u_threshold_bp,
                    "force_champion_only": latency_result.action.force_champion_only,
                },
                "execution_sensitivity": {
                    "max_diff_bp": sensitivity_result.max_diff_bp,
                    "mean_diff_bp": sensitivity_result.mean_diff_bp,
                    "run_breach": sensitivity_result.run_breach,
                    "consecutive_breach_days": sensitivity_result.consecutive_breach_days,
                    "execution_sensitivity_alert": sensitivity_result.execution_sensitivity_alert,
                    "sensitivity_threshold_bp": sensitivity_result.sensitivity_threshold_bp,
                    "sensitivity_days": sensitivity_result.sensitivity_days,
                    "worst_symbol": sensitivity_result.worst_symbol,
                    "breached_symbols": sensitivity_result.breached_symbols,
                },
            },
            "m11": {
                "score": m11_result.score,
                "status": m11_result.status,
                "input": m11_input,
                "redlines": m11_result.redlines,
                "metrics": {
                    "valid_samples": m11_result.metrics.valid_samples,
                    "champion_cum_return": m11_result.metrics.champion_cum_return,
                    "challenger_cum_return": m11_result.metrics.challenger_cum_return,
                    "champion_max_drawdown": m11_result.metrics.champion_max_drawdown,
                    "challenger_max_drawdown": m11_result.metrics.challenger_max_drawdown,
                    "drawdown_delta": m11_result.metrics.drawdown_delta,
                    "tail_loss_delta": m11_result.metrics.tail_loss_delta,
                    "execution_divergence_ratio": m11_result.metrics.execution_divergence_ratio,
                    "champion_win_rate": m11_result.metrics.champion_win_rate,
                    "challenger_win_rate": m11_result.metrics.challenger_win_rate,
                },
                "attribution": [
                    {
                        "name": item.name,
                        "value": item.value,
                        "threshold": item.threshold,
                        "breached": item.breached,
                        "impact": item.impact,
                    }
                    for item in m11_result.attribution
                ],
            },
            "m8": {
                "score": m8_score,
                "top_k": m8_report.get("top_k"),
                "promote_similarity": m8_report.get("promote_similarity"),
                "review_similarity": m8_report.get("review_similarity"),
                "artifact_uri": m8_report.get("artifact_uri"),
                "summary": m8_summary,
                "coverage_promote_or_review": m8_coverage,
                "gate_pass_rate": (
                    m8_summary.get("gate_pass_rate") if isinstance(m8_summary, Mapping) else 0.0
                ),
                "gate_failure_counts": (
                    m8_summary.get("gate_failure_counts") if isinstance(m8_summary, Mapping) else {}
                ),
                "gate_provenance_counts": (
                    m8_summary.get("gate_provenance_counts")
                    if isinstance(m8_summary, Mapping)
                    else {}
                ),
                "strict_gate_inputs": (
                    m8_summary.get("strict_gate_inputs")
                    if isinstance(m8_summary, Mapping)
                    else False
                ),
            },
        }
        hard_gates = evaluate_hard_gates(
            module_details=module_details,
            min_samples_per_bucket=self._config.runtime_spec.min_samples_per_bucket,
        )
        module_details["hard_gates"] = hard_gates
        checkpoints.append(
            ManifestCheckpoint(
                step="hard_gates",
                status="ok" if bool(hard_gates.get("all_passed", False)) else "warn",
                timestamp=run_now,
            )
        )
        return module_scores, module_details, checkpoints

    def _resolve_m5_records(
        self,
        records: Sequence[Mapping[str, object]],
    ) -> tuple[list[Mapping[str, object]], dict[str, object]]:
        configured_path = self._config.m5_label_records_path.strip()
        if not configured_path:
            record_list = list(records)
            return record_list, {
                "source": "records",
                "path": None,
                "path_exists": False,
                "loaded_records": len(record_list),
                **_summarize_intraday_loader_records(record_list),
            }

        path = self._resolve_path(configured_path)
        loaded = load_m5_label_records(path=path)
        path_exists = path.exists()
        relative_path = self._to_relative_if_possible(str(path))
        if loaded:
            loaded_records: list[Mapping[str, object]] = [item for item in loaded]
            return loaded_records, {
                "source": "label_loader",
                "path": relative_path,
                "path_exists": path_exists,
                "loaded_records": len(loaded),
                **_summarize_intraday_loader_records(loaded_records),
            }

        record_list = list(records)
        return record_list, {
            "source": "records_fallback",
            "path": relative_path,
            "path_exists": path_exists,
            "loaded_records": len(record_list),
            **_summarize_intraday_loader_records(record_list),
        }

    def _resolve_m7_records(
        self,
        records: Sequence[Mapping[str, object]],
    ) -> tuple[list[Mapping[str, object]], dict[str, object]]:
        configured_path = self._config.m7_news_records_path.strip()
        if not configured_path:
            if self._config.m7_allow_records_fallback:
                return list(records), {
                    "source": "records_fallback_enabled",
                    "path": None,
                    "path_exists": False,
                    "loaded_records": len(records),
                }
            return [], {
                "source": "missing_news_input",
                "path": None,
                "path_exists": False,
                "loaded_records": 0,
            }

        path = self._resolve_path(configured_path)
        loaded = load_m7_news_records(path=path)
        path_exists = path.exists()
        relative_path = self._to_relative_if_possible(str(path))
        if loaded:
            loaded_records: list[Mapping[str, object]] = [item for item in loaded]
            return loaded_records, {
                "source": "news_loader",
                "path": relative_path,
                "path_exists": path_exists,
                "loaded_records": len(loaded),
            }

        if self._config.m7_allow_records_fallback:
            return list(records), {
                "source": "records_fallback",
                "path": relative_path,
                "path_exists": path_exists,
                "loaded_records": len(records),
            }
        return [], {
            "source": "empty_news_input",
            "path": relative_path,
            "path_exists": path_exists,
            "loaded_records": 0,
        }

    @staticmethod
    def _build_symbol_price_lookup(
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, float]:
        lookup: dict[str, float] = {}
        for record in records:
            symbol = str(record.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            price = 0.0
            for key in ("close", "price", "last", "open"):
                price = _as_float(record.get(key), default=0.0)
                if price > 0.0:
                    break
            if price > 0.0:
                lookup[symbol] = price
        return lookup

    def _build_execution_promotion_gate(
        self,
        *,
        eval_profiles: Mapping[str, object],
        shadow_online_v2_report: Mapping[str, object],
    ) -> dict[str, object]:
        trading_gate = eval_profiles.get("trading_distribution_gate", {})
        if not isinstance(trading_gate, Mapping):
            trading_gate = {}
        metrics = shadow_online_v2_report.get("metrics", {})
        if not isinstance(metrics, Mapping):
            metrics = {}

        reason_codes: list[str] = []
        if not bool(trading_gate.get("pass", True)):
            reason_codes.append("trading_distribution_failed")

        shadow_status = str(shadow_online_v2_report.get("status", "")).strip().lower()
        valid_samples = _as_int(metrics.get("valid_samples"), default=0)
        if shadow_status != "updated":
            reason_codes.append("shadow_v2_not_updated")

        signal_divergence_ratio = _as_float(
            metrics.get("signal_divergence_ratio"),
            default=0.0,
        )
        divergence_limit = float(self._config.m11_execution_divergence_limit)
        if valid_samples > 0 and signal_divergence_ratio > divergence_limit:
            reason_codes.append("shadow_v2_signal_divergence_limit_breach")

        return {
            "name": "execution",
            "passed": len(reason_codes) == 0,
            "reason_codes": _dedupe_strings(reason_codes),
            "metrics": {
                "trading_distribution_pass": bool(trading_gate.get("pass", True)),
                "shadow_v2_status": shadow_status,
                "shadow_v2_valid_samples": valid_samples,
                "shadow_v2_signal_divergence_ratio": round(signal_divergence_ratio, 6),
                "shadow_v2_signal_divergence_limit": round(divergence_limit, 6),
            },
        }

    def _build_reconcile_promotion_gate(
        self,
        *,
        reconcile_drift: Mapping[str, object],
    ) -> dict[str, object]:
        reason_codes: list[str] = []
        position_drift_alert = bool(reconcile_drift.get("position_drift_alert", False))
        if position_drift_alert:
            reason_codes.append("position_drift_alert")
        consecutive_days = _as_int(
            reconcile_drift.get("position_drift_consecutive_days"),
            default=0,
        )
        consecutive_trigger = max(
            1,
            self._config.runtime_spec.position_drift_consecutive_days_trigger,
        )
        if consecutive_days >= consecutive_trigger:
            reason_codes.append("position_drift_consecutive_breach")

        return {
            "name": "reconcile",
            "passed": len(reason_codes) == 0,
            "reason_codes": _dedupe_strings(reason_codes),
            "metrics": {
                "position_drift_alert": position_drift_alert,
                "position_drift_ratio": round(
                    _as_float(reconcile_drift.get("position_drift_ratio"), default=0.0),
                    6,
                ),
                "position_drift_consecutive_days": consecutive_days,
                "position_drift_consecutive_trigger": consecutive_trigger,
            },
        }

    def _build_change_keys(
        self,
        *,
        m9_success: bool,
        module_details: Mapping[str, object],
    ) -> list[str]:
        if not m9_success:
            return ["m9_failure_manual_review"]

        change_keys: list[str] = ["alert_threshold_volatility"]
        raw_m5 = module_details.get("m5")
        if isinstance(raw_m5, Mapping):
            raw_linkage = raw_m5.get("strategy_linkage")
            if isinstance(raw_linkage, Mapping):
                raw_keys = raw_linkage.get("change_keys")
                if isinstance(raw_keys, list):
                    for item in raw_keys:
                        if isinstance(item, str) and item.strip():
                            change_keys.append(item.strip())
        raw_m7 = module_details.get("m7")
        if isinstance(raw_m7, Mapping):
            m7_status = str(raw_m7.get("status", "")).strip().lower()
            if m7_status == "budget_capped":
                change_keys.append("observation_queue_news_budget")
            elif m7_status == "degraded":
                change_keys.append("observation_queue_news_quality")
        raw_eval_profiles = module_details.get("eval_profiles")
        if isinstance(raw_eval_profiles, Mapping):
            raw_gate = raw_eval_profiles.get("trading_distribution_gate")
            if isinstance(raw_gate, Mapping) and not bool(raw_gate.get("pass", True)):
                change_keys.append("trading_fill_distribution_gate")
                raw_reasons = raw_gate.get("reason_codes")
                if isinstance(raw_reasons, list):
                    for reason in raw_reasons:
                        if isinstance(reason, str) and reason.strip():
                            change_keys.append(f"trading_fill_gate_{reason.strip()}")
        raw_utility_execution = module_details.get("utility_execution")
        if isinstance(raw_utility_execution, Mapping):
            k_base = _as_int(raw_utility_execution.get("k_base"), default=0)
            k_dynamic = _as_int(raw_utility_execution.get("k_dynamic"), default=0)
            if k_base > 0 and 0 < k_dynamic < k_base:
                change_keys.append("dynamic_k_adjusted")
            raw_trim = raw_utility_execution.get("trim_reason_codes")
            if isinstance(raw_trim, list):
                for reason in raw_trim:
                    if isinstance(reason, str) and reason.strip():
                        change_keys.append(f"dynamic_k_trim_{reason.strip()}")
            raw_mapping_steps = raw_utility_execution.get("mapping_fallback_steps")
            if isinstance(raw_mapping_steps, list) and any(
                isinstance(item, str) and "bucket_min_samples" in item for item in raw_mapping_steps
            ):
                change_keys.append("mapping_bucket_fallback")
        raw_reconcile_drift = module_details.get("reconcile_drift")
        if isinstance(raw_reconcile_drift, Mapping):
            if bool(raw_reconcile_drift.get("position_drift_alert", False)):
                change_keys.append("position_drift_alert")
            consecutive_days = _as_int(
                raw_reconcile_drift.get("position_drift_consecutive_days"),
                default=0,
            )
            if consecutive_days >= max(
                1, self._config.runtime_spec.position_drift_consecutive_days_trigger
            ):
                change_keys.append("position_drift_consecutive_breach")
        raw_hard_gates = module_details.get("hard_gates")
        if isinstance(raw_hard_gates, Mapping) and not bool(raw_hard_gates.get("all_passed", True)):
            change_keys.append("hard_gate_failed")
            raw_failed_gates = raw_hard_gates.get("failed_gates")
            if isinstance(raw_failed_gates, list):
                for gate_name in raw_failed_gates:
                    if isinstance(gate_name, str) and gate_name.strip():
                        change_keys.append(f"hard_gate_{gate_name.strip()}_failed")
        raw_online_update = module_details.get("online_update")
        if isinstance(raw_online_update, Mapping):
            promotion_gate_relevant = bool(raw_online_update.get("promotion_candidate", False)) or (
                bool(raw_online_update.get("promotion_revoked", False))
            )
            if promotion_gate_relevant:
                if not bool(raw_online_update.get("promotion_gate_passed", True)):
                    change_keys.append("promotion_gate_blocked")
                raw_gate_reasons = raw_online_update.get("promotion_gate_reason_codes")
                if isinstance(raw_gate_reasons, list):
                    for reason in raw_gate_reasons:
                        if isinstance(reason, str) and reason.strip():
                            change_keys.append(f"promotion_gate_{reason.strip()}")
        raw_m10 = module_details.get("m10")
        if isinstance(raw_m10, Mapping):
            raw_sensitivity = raw_m10.get("execution_sensitivity")
            if isinstance(raw_sensitivity, Mapping) and bool(
                raw_sensitivity.get("execution_sensitivity_alert", False)
            ):
                change_keys.append("execution_sensitivity_alert")
        return _dedupe_strings(change_keys)

    def _resolve_m11_observations(
        self,
        records: Sequence[Mapping[str, object]],
    ) -> tuple[list[M11ShadowObservation], dict[str, object]]:
        configured_path = self._config.m11_shadow_results_path.strip()
        if not configured_path:
            record_list = list(records)
            parsed = parse_m11_shadow_records(records=record_list)
            return parsed, {
                "source": "records",
                "path": None,
                "path_exists": False,
                "loaded_samples": len(parsed),
                **_summarize_intraday_loader_records(record_list),
            }

        path = self._resolve_path(configured_path)
        loaded_records = load_m11_shadow_records(path=path)
        loaded = parse_m11_shadow_records(records=loaded_records)
        path_exists = path.exists()
        relative_path = self._to_relative_if_possible(str(path))
        if loaded:
            return loaded, {
                "source": "shadow_loader",
                "path": relative_path,
                "path_exists": path_exists,
                "loaded_samples": len(loaded),
                **_summarize_intraday_loader_records(loaded_records),
            }

        record_list = list(records)
        fallback = parse_m11_shadow_records(records=record_list)
        return fallback, {
            "source": "records_fallback",
            "path": relative_path,
            "path_exists": path_exists,
            "loaded_samples": len(fallback),
            **_summarize_intraday_loader_records(record_list),
        }

    def _build_m2_observation(self, records: Sequence[Mapping[str, object]]) -> RegimeObservation:
        if not records:
            return RegimeObservation(atr_ratio=0.02, sector_dispersion=0.20, turnover_zscore=0.0)

        atr_values: list[float] = []
        close_values: list[float] = []
        volume_values: list[float] = []
        for record in records:
            high = _as_float(record.get("high"), default=0.0)
            low = _as_float(record.get("low"), default=0.0)
            close = _as_float(record.get("close"), default=0.0)
            volume = _as_float(record.get("volume"), default=0.0)
            if close > 0.0:
                atr_values.append(max(0.0, (high - low) / close))
                close_values.append(close)
            if volume > 0.0:
                volume_values.append(volume)

        atr_ratio = float(np.mean(atr_values)) if atr_values else 0.02
        mean_close = float(np.mean(close_values)) if close_values else 0.0
        sector_dispersion = (
            float(np.std(close_values) / max(mean_close, 1e-6)) if close_values else 0.20
        )
        if volume_values:
            log_volume = np.log1p(np.asarray(volume_values, dtype=float))
            std_log_volume = float(np.std(log_volume))
            turnover_zscore = float(
                (float(log_volume[-1]) - float(np.mean(log_volume))) / max(std_log_volume, 1e-6)
            )
        else:
            turnover_zscore = 0.0
        return RegimeObservation(
            atr_ratio=atr_ratio,
            sector_dispersion=sector_dispersion,
            turnover_zscore=turnover_zscore,
        )

    def _build_m3_vector(self, record: Mapping[str, object]) -> list[float] | None:
        regime_state = str(self._m2_controller.dump_state().get("active_state", "range"))
        return build_m3_vector_from_record(
            record,
            vector_profile=self._m3_vector_profile,
            regime_state=regime_state,
            no_fill_ratio=_as_float(self._eval_profiles_report.get("no_fill_ratio"), default=0.0),
            partial_fill_ratio=_as_float(
                self._eval_profiles_report.get("partial_fill_ratio"), default=0.0
            ),
            constraint_pressure=_as_float(
                self._utility_execution_report.get("constraint_pressure"), default=0.0
            ),
            position_drift_ratio=_as_float(
                self._reconcile_drift_report.get("position_drift_ratio"), default=0.0
            ),
        )

    def _build_m8_query_vector(self, candidate: Mapping[str, object]) -> list[float] | None:
        return self._build_m3_vector(candidate)

    def _m3_vectors(self, records: Sequence[Mapping[str, object]]) -> NDArray[np.float64]:
        rows: list[list[float]] = []
        for record in records:
            vector = self._build_m3_vector(record)
            if vector is None:
                continue
            rows.append(vector)
        if not rows:
            return np.empty((0, self._m3_vector_profile.vector_dim), dtype=float)
        return np.asarray(rows, dtype=float)

    def _build_m3_profile_payload(self) -> dict[str, object]:
        return {
            "configured_vector_profile_id": self._m3_vector_profile_resolution[
                "configured_vector_profile_id"
            ],
            "active_vector_profile_id": self._m3_vector_profile_resolution[
                "active_vector_profile_id"
            ],
            "fallback_used": self._m3_vector_profile_resolution["fallback_used"],
            "fallback_reason": self._m3_vector_profile_resolution["fallback_reason"],
            "registry_record_count": self._m3_vector_profile_resolution["registry_record_count"],
            "registry_known_profile_ids": list(
                self._m3_vector_profile_resolution["registry_known_profile_ids"]
            ),
            "vector_profile_id": self._m3_vector_profile.vector_profile_id,
            "vector_dim": self._m3_vector_profile.vector_dim,
            "feature_components": list(self._m3_vector_profile.feature_components),
            "normalization_policy": self._m3_vector_profile.normalization_policy,
            "distance_metric": self._m3_vector_profile.distance_metric,
            "compatible_query_profiles": list(self._m3_vector_profile.compatible_query_profiles),
        }

    def _build_proposal(
        self,
        run_now: datetime,
        fused_score: float,
        module_scores: Mapping[str, float],
        module_artifacts: Mapping[str, str],
        spec_hashes: Mapping[str, str] | None = None,
        reproducibility: Mapping[str, object] | None = None,
    ) -> ProposalArtifact:
        proposal_id = f"prop_{run_now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        payload_path = self._resolve_path(
            f"{self._config.suggestions_dir}/evolution/{proposal_id}.json"
        )
        payload_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "proposal_id": proposal_id,
            "created_at": run_now.isoformat(),
            "module_scores": dict(module_scores),
            "module_artifacts": dict(module_artifacts),
            "fused_score": fused_score,
            "active_champion_id": self._config.active_champion_id,
            "dry_run": self._config.dry_run,
            "spec_hashes": dict(spec_hashes or {}),
            "reproducibility": dict(reproducibility or {}),
        }
        payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload_sha256 = hashlib.sha256(payload_path.read_bytes()).hexdigest()
        payload_uri = str(payload_path.relative_to(self._project_root)).replace("\\", "/")

        return ProposalArtifact.model_validate(
            {
                "proposal_id": proposal_id,
                "data_snapshot_id": f"snapshot_{run_now.strftime('%Y%m%d')}",
                "code_commit_id": self._config.code_commit_id,
                "random_seed": {
                    "optuna": self._config.m2_optuna_random_seed,
                    "sampling": 789,
                    "runtime": self._config.runtime_spec.random_seed,
                },
                "eval_protocol_id": "v7.2_cost_model",
                "llm_prompt_version": "classify_v3.1",
                "payload_uri": payload_uri,
                "payload_sha256": payload_sha256,
                "payload_diff_summary": "offhours dry-run proposal generation",
                "user_facing_summary": {
                    "pnl_diff": "+0.0%",
                    "risk_diff": "0.0%",
                    "ir_score": 0.0,
                    "turnover_change": "+0.0%",
                    "avg_trades_per_day": 0.0,
                    "key_reason": "framework dry-run integration",
                    "summary_window": {"oos_days": 60, "shadow_days": 14},
                    "baseline": "Champion_same_window_after_cost",
                },
            }
        )

    def _build_reproducibility_bundle(self) -> dict[str, object]:
        configured_hash = str(self._config.runtime_spec.library_versions_hash).strip()
        if configured_hash and configured_hash.lower() != "auto":
            library_hash = configured_hash
        else:
            versions = {
                "python": platform.python_version(),
                "numpy": np.__version__,
            }
            for package_name in ("lightgbm", "xgboost", "river", "torch", "duckdb", "faiss-cpu"):
                try:
                    versions[package_name] = version(package_name)
                except PackageNotFoundError:
                    continue
            library_hash = hashlib.sha256(
                json.dumps(
                    versions,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

        return {
            "random_seed": int(self._config.runtime_spec.random_seed),
            "num_threads": int(self._config.runtime_spec.num_threads),
            "deterministic_mode": bool(self._config.runtime_spec.deterministic_mode),
            "library_versions_hash": library_hash,
        }

    def _build_audit_fields(
        self,
        *,
        module_details: Mapping[str, object],
        spec_bundle: Mapping[str, object],
        reproducibility: Mapping[str, object],
        universe: Mapping[str, object],
    ) -> dict[str, object]:
        execution_spec = spec_bundle.get("execution_spec", {})
        if not isinstance(execution_spec, Mapping):
            execution_spec = {}
        m10 = module_details.get("m10", {})
        if not isinstance(m10, Mapping):
            m10 = {}
        sensitivity = m10.get("execution_sensitivity", {})
        if not isinstance(sensitivity, Mapping):
            sensitivity = {}
        online_update = module_details.get("online_update", {})
        if not isinstance(online_update, Mapping):
            online_update = {}
        eval_profiles = module_details.get("eval_profiles", {})
        if not isinstance(eval_profiles, Mapping):
            eval_profiles = {}
        utility_execution = module_details.get("utility_execution", {})
        if not isinstance(utility_execution, Mapping):
            utility_execution = {}
        m3 = module_details.get("m3", {})
        if not isinstance(m3, Mapping):
            m3 = {}
        reconcile_drift = module_details.get("reconcile_drift", {})
        if not isinstance(reconcile_drift, Mapping):
            reconcile_drift = {}
        hard_gates = module_details.get("hard_gates", {})
        if not isinstance(hard_gates, Mapping):
            hard_gates = {}
        raw_mapping_steps = utility_execution.get("mapping_fallback_steps")
        mapping_steps = (
            [str(item) for item in raw_mapping_steps if isinstance(item, str)]
            if isinstance(raw_mapping_steps, list)
            else []
        )
        raw_trim_reasons = utility_execution.get("trim_reason_codes")
        trim_reasons = (
            [str(item) for item in raw_trim_reasons if isinstance(item, str)]
            if isinstance(raw_trim_reasons, list)
            else []
        )
        return {
            "price_series_mode": execution_spec.get("price_series_mode", ""),
            "dividend_treatment": execution_spec.get("dividend_treatment", ""),
            "share_rounding_rule": execution_spec.get("share_rounding_rule", ""),
            "price_tick_rule": execution_spec.get("price_tick_rule", ""),
            "min_notional_per_order": execution_spec.get("min_notional_per_order", 0.0),
            "residual_order_policy": execution_spec.get("residual_order_policy", ""),
            "eval_profile_id": str(eval_profiles.get("profile_set_id", "")),
            "no_fill_ratio": _as_float(eval_profiles.get("no_fill_ratio"), default=0.0),
            "partial_fill_ratio": _as_float(eval_profiles.get("partial_fill_ratio"), default=0.0),
            "mapping_level_used": str(utility_execution.get("mapping_level_used", "")),
            "bucket_sample_count": _as_int(utility_execution.get("bucket_sample_count"), default=0),
            "mapping_fallback_steps": mapping_steps,
            "k_base": _as_int(utility_execution.get("k_base"), default=0),
            "k_dynamic": _as_int(utility_execution.get("k_dynamic"), default=0),
            "m3_vector_profile_id": str(m3.get("vector_profile_id", "")),
            "m3_vector_dim": _as_int(m3.get("vector_dim"), default=0),
            "constraint_pressure": _as_float(
                utility_execution.get("constraint_pressure"), default=0.0
            ),
            "alpha": _as_float(utility_execution.get("alpha"), default=1.0),
            "trim_reason_codes": trim_reasons,
            "negative_u_filtered_count": _as_int(
                utility_execution.get("negative_u_filtered_count"), default=0
            ),
            "target_vs_filled_weight_p50": _as_float(
                reconcile_drift.get("target_vs_filled_weight_p50"), default=0.0
            ),
            "target_vs_filled_weight_p90": _as_float(
                reconcile_drift.get("target_vs_filled_weight_p90"), default=0.0
            ),
            "target_vs_filled_weight_max": _as_float(
                reconcile_drift.get("target_vs_filled_weight_max"), default=0.0
            ),
            "filled_vs_eod_weight_p50": _as_float(
                reconcile_drift.get("filled_vs_eod_weight_p50"), default=0.0
            ),
            "filled_vs_eod_weight_p90": _as_float(
                reconcile_drift.get("filled_vs_eod_weight_p90"), default=0.0
            ),
            "filled_vs_eod_weight_max": _as_float(
                reconcile_drift.get("filled_vs_eod_weight_max"), default=0.0
            ),
            "position_drift_ratio": _as_float(
                reconcile_drift.get("position_drift_ratio"), default=0.0
            ),
            "hard_gate_all_passed": bool(hard_gates.get("all_passed", True)),
            "hard_gate_failed_count": _as_int(hard_gates.get("failed_gate_count"), default=0),
            "execution_sensitivity_alert": bool(
                sensitivity.get("execution_sensitivity_alert", False)
            ),
            "online_update_budget_ratio": _as_float(
                online_update.get("online_update_budget_ratio"), default=0.0
            ),
            "online_samples_used_hash": str(online_update.get("online_samples_used_hash", "")),
            "online_samples_used": _as_int(
                online_update.get("online_samples_used"),
                default=0,
            ),
            "online_samples_downweighted": _as_int(
                online_update.get("online_samples_downweighted"),
                default=0,
            ),
            "online_samples_skipped": _as_int(
                online_update.get("online_samples_skipped"),
                default=0,
            ),
            "tier_b_promoted": bool(online_update.get("tier_b_promoted", False)),
            "promotion_candidate": bool(online_update.get("promotion_candidate", False)),
            "promotion_gate_passed": bool(online_update.get("promotion_gate_passed", True)),
            "promotion_gate_reason_codes": _gate_reason_codes(
                {"reason_codes": online_update.get("promotion_gate_reason_codes", [])}
            ),
            "promotion_decision": str(online_update.get("promotion_decision", "")),
            "promotion_reason_codes": _gate_reason_codes(
                {"reason_codes": online_update.get("promotion_reason_codes", [])}
            ),
            "promotion_revoked": bool(online_update.get("promotion_revoked", False)),
            "revocation_reason_codes": _gate_reason_codes(
                {"reason_codes": online_update.get("revocation_reason_codes", [])}
            ),
            "execution_gate_passed": _gate_passed(
                online_update.get("execution_promotion_gate")
            ),
            "execution_gate_reason_codes": _gate_reason_codes(
                online_update.get("execution_promotion_gate")
            ),
            "reconcile_gate_passed": _gate_passed(
                online_update.get("reconcile_promotion_gate")
            ),
            "reconcile_gate_reason_codes": _gate_reason_codes(
                online_update.get("reconcile_promotion_gate")
            ),
            "universe_snapshot_id": str(universe.get("snapshot_id", "")),
            "universe_spec_hash": str(universe.get("universe_spec_hash", "")),
            "random_seed": int(_as_int(reproducibility.get("random_seed"), default=0)),
            "num_threads": int(_as_int(reproducibility.get("num_threads"), default=1)),
            "deterministic_mode": bool(reproducibility.get("deterministic_mode", False)),
            "library_versions_hash": str(reproducibility.get("library_versions_hash", "")),
        }

    def _build_runtime_controls(
        self,
        *,
        module_details: Mapping[str, object],
        run_now: datetime,
        m9_success: bool,
    ) -> dict[str, object]:
        raw_m2 = module_details.get("m2", {})
        m2 = raw_m2 if isinstance(raw_m2, Mapping) else {}
        raw_m4 = module_details.get("m4", {})
        m4 = raw_m4 if isinstance(raw_m4, Mapping) else {}
        raw_m6 = module_details.get("m6", {})
        m6 = raw_m6 if isinstance(raw_m6, Mapping) else {}
        raw_m10 = module_details.get("m10", {})
        m10 = raw_m10 if isinstance(raw_m10, Mapping) else {}

        reasons: list[str] = []
        threshold_shift = 0.0
        position_multiplier = 1.0
        global_risk_delta = 0.0
        conservative_mode = False

        m2_state = str(m2.get("active_state", "")).strip().lower() or "range"
        m2_confidence = _as_float(m2.get("confidence"), default=0.0)
        if m2_state == "range":
            threshold_shift += 1.0
            position_multiplier *= 0.95
            global_risk_delta -= 4.0
            reasons.append("m2_range")
        elif m2_state == "trend_down":
            threshold_shift += 3.0
            position_multiplier *= 0.80
            global_risk_delta -= 10.0
            conservative_mode = True
            reasons.append("m2_trend_down")
        elif m2_state == "extreme":
            threshold_shift += 5.0
            position_multiplier *= 0.65
            global_risk_delta -= 15.0
            conservative_mode = True
            reasons.append("m2_extreme")
        elif m2_state == "trend_up":
            global_risk_delta += 6.0
            reasons.append("m2_trend_up")

        m4_status = str(m4.get("status", "")).strip().lower()
        m4_metrics = m4.get("metrics", {})
        if not isinstance(m4_metrics, Mapping):
            m4_metrics = {}
        if m4_status == "outflow_dominant":
            threshold_shift += 2.5
            position_multiplier *= 0.85
            global_risk_delta -= 8.0
            conservative_mode = True
            reasons.append("m4_outflow_dominant")
        elif m4_status == "inflow_dominant":
            global_risk_delta += 4.0
            reasons.append("m4_inflow_dominant")

        m6_status = str(m6.get("status", "")).strip().lower()
        m6_metrics = m6.get("metrics", {})
        if not isinstance(m6_metrics, Mapping):
            m6_metrics = {}
        if m6_status == "heavy_sell_pressure":
            threshold_shift += 3.0
            position_multiplier *= 0.75
            global_risk_delta -= 10.0
            conservative_mode = True
            reasons.append("m6_heavy_sell_pressure")
        elif m6_status == "favorable":
            global_risk_delta += 2.0
            reasons.append("m6_favorable")

        raw_m10_metrics = m10.get("metrics", {})
        m10_metrics = raw_m10_metrics if isinstance(raw_m10_metrics, Mapping) else {}
        raw_sensitivity = m10.get("execution_sensitivity", {})
        sensitivity = raw_sensitivity if isinstance(raw_sensitivity, Mapping) else {}
        m10_runtime_status = str(m10.get("health_status", m10.get("status", ""))).strip().lower()
        degraded_mode = m10_runtime_status in {"degraded", "limited_observability", "no_data"}
        if degraded_mode:
            threshold_shift += 4.0
            position_multiplier *= 0.60
            global_risk_delta -= 15.0
            conservative_mode = True
            reasons.append(f"m10_{m10_runtime_status}")
        elif (
            m10_runtime_status == "watch"
            or bool(m10_metrics.get("latency_watch_flag", False))
            or bool(sensitivity.get("execution_sensitivity_alert", False))
        ):
            threshold_shift += 2.0
            position_multiplier *= 0.85
            global_risk_delta -= 6.0
            conservative_mode = True
            reasons.append("m10_watch")
        elif m10_runtime_status == "healthy":
            global_risk_delta += 4.0
            reasons.append("m10_healthy")

        if not m9_success:
            reasons.append("m9_manual_review")

        position_multiplier = max(0.25, min(position_multiplier, 1.0))
        global_risk_delta = max(-30.0, min(global_risk_delta, 12.0))
        threshold_shift = max(0.0, threshold_shift)
        regime_hint = (
            "trend"
            if m2_state == "trend_up"
            else ("crash" if m2_state in {"trend_down", "extreme"} else "range")
        )
        return {
            "source": "evolution",
            "source_timestamp": run_now.isoformat(),
            "m9_success": m9_success,
            "degraded_mode": degraded_mode,
            "conservative_mode": conservative_mode,
            "threshold_shift": round(threshold_shift, 4),
            "position_multiplier": round(position_multiplier, 4),
            "global_risk_delta": round(global_risk_delta, 4),
            "regime_hint": regime_hint,
            "reasons": _dedupe_strings(reasons),
            "m2": {
                "active_state": m2_state,
                "confidence": round(m2_confidence, 6),
            },
            "m4": {
                "status": m4_status,
                "net_flow_ratio": _as_float(m4_metrics.get("net_flow_ratio"), default=0.0),
                "breadth_ratio": _as_float(m4_metrics.get("breadth_ratio"), default=0.0),
            },
            "m6": {
                "status": m6_status,
                "pressure_index": _as_float(m6_metrics.get("pressure_index"), default=0.0),
                "bearish_ratio": _as_float(m6_metrics.get("bearish_ratio"), default=0.0),
            },
            "m10": {
                "status": m10_runtime_status,
                "score": _as_float(m10.get("score"), default=0.0),
                "latency_watch_flag": bool(m10_metrics.get("latency_watch_flag", False)),
                "execution_sensitivity_alert": bool(
                    sensitivity.get("execution_sensitivity_alert", False)
                ),
            },
        }

    def _resolve_compliance_states(
        self,
        m9_success: bool,
        m9_retry: bool,
        auth_decision: AuthorizationDecision,
        dry_run: bool,
    ) -> list[ComplianceState]:
        states = [ComplianceState.GENERATED, ComplianceState.VALIDATED]
        if not m9_success:
            if m9_retry:
                states.append(ComplianceState.RETRY_PENDING)
            else:
                states.append(ComplianceState.INVALIDATED)
            return states
        states.append(ComplianceState.SHADOWING)
        if auth_decision.level == AuthorizationLevel.C:
            states.append(ComplianceState.APPROVED)
            if not dry_run:
                states.append(ComplianceState.PROMOTED)
        return states

    def _write_compliance_events(
        self,
        states: Sequence[ComplianceState],
        proposal: ProposalArtifact,
        symbol: str,
        run_now: datetime,
        source_trace_id: str,
    ) -> bool:
        logger = ComplianceLogger(db_path=self._resolve_path(self._config.compliance_db_path))
        try:
            for state in states:
                logger.log_event(
                    ComplianceEvent(
                        trace_id=source_trace_id or f"evolution-{proposal.proposal_id}",
                        proposal_id=proposal.proposal_id,
                        state=state,
                        active_champion_id=self._config.active_champion_id,
                        symbol=symbol,
                        event_time=run_now,
                        code_commit_id=proposal.code_commit_id,
                    )
                )
            return True
        except Exception:
            fallback_path = self._resolve_path("artifacts/evolution/compliance_fallback.jsonl")
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with fallback_path.open("a", encoding="utf-8") as fp:
                for state in states:
                    fp.write(
                        json.dumps(
                            {
                                "trace_id": source_trace_id or f"evolution-{proposal.proposal_id}",
                                "proposal_id": proposal.proposal_id,
                                "state": state.value,
                                "active_champion_id": self._config.active_champion_id,
                                "symbol": symbol,
                                "event_time": run_now.isoformat(),
                                "code_commit_id": proposal.code_commit_id,
                            },
                            ensure_ascii=True,
                        )
                        + "\n"
                    )
            return False

    def _load_or_create_manifest(self) -> RunManifest:
        path = self._resolve_path(self._config.manifest_path)
        if not path.exists():
            return RunManifest(run_id=f"evo-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}")
        return load_manifest(path=path)

    def _save_manifest(self, manifest: RunManifest) -> None:
        save_manifest(path=self._resolve_path(self._config.manifest_path), manifest=manifest)

    def _load_m2_state(self) -> None:
        if not self._m2_state_path.exists():
            return
        try:
            payload = json.loads(self._m2_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        state_payload = payload.get("state", payload)
        if not isinstance(state_payload, Mapping):
            return
        try:
            self._m2_controller.load_state(payload=state_payload)
        except ValueError:
            return
        params_payload = payload.get("params")
        if isinstance(params_payload, Mapping):
            self._m2_controller.load_params(payload=params_payload)

        history_payload = payload.get("history")
        if isinstance(history_payload, list):
            loaded_history: list[RegimeObservation] = []
            for item in history_payload:
                if not isinstance(item, Mapping):
                    continue
                loaded_history.append(
                    RegimeObservation(
                        atr_ratio=_as_float(item.get("atr_ratio"), default=0.02),
                        sector_dispersion=_as_float(item.get("sector_dispersion"), default=0.20),
                        turnover_zscore=_as_float(item.get("turnover_zscore"), default=0.0),
                    )
                )
            self._m2_observation_history = loaded_history
        latency_history_payload = payload.get("latency_breach_history")
        if isinstance(latency_history_payload, list):
            self._latency_breach_history = [bool(item) for item in latency_history_payload][-20:]
        sensitivity_history_payload = payload.get("execution_sensitivity_history")
        if isinstance(sensitivity_history_payload, list):
            self._execution_sensitivity_history = [
                bool(item) for item in sensitivity_history_payload
            ][-30:]
        online_update_state = payload.get("online_update_state")
        if isinstance(online_update_state, Mapping):
            self._online_update_state = dict(online_update_state)
        utility_execution_state = payload.get("utility_execution_state")
        if isinstance(utility_execution_state, Mapping):
            self._utility_execution_state = dict(utility_execution_state)
        reconcile_drift_state = payload.get("reconcile_drift_state")
        if isinstance(reconcile_drift_state, Mapping):
            self._reconcile_drift_state = dict(reconcile_drift_state)

        optuna_payload = payload.get("optuna")
        if isinstance(optuna_payload, Mapping):
            raw_last_tuned = optuna_payload.get("last_tuned_at")
            if isinstance(raw_last_tuned, str) and raw_last_tuned.strip():
                try:
                    parsed = datetime.fromisoformat(raw_last_tuned)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    self._m2_last_tuned_at = parsed
                except ValueError:
                    self._m2_last_tuned_at = None
            artifact_uri = optuna_payload.get("artifact_uri")
            if isinstance(artifact_uri, str) and artifact_uri.strip():
                self._m2_last_artifact_uri = artifact_uri.strip()
            raw_last_result = optuna_payload.get("last_result")
            if isinstance(raw_last_result, Mapping):
                raw_params = raw_last_result.get("params")
                params_payload = raw_params if isinstance(raw_params, Mapping) else {}
                params = RegimeModelParams.from_mapping(
                    payload=params_payload,
                    default=self._m2_controller.params,
                )
                self._m2_last_optuna = M2OptunaLikeResult(
                    backend=str(raw_last_result.get("backend", "optuna_like_random_search")),
                    tuned=bool(raw_last_result.get("tuned", False)),
                    reason=str(raw_last_result.get("reason", "loaded_from_state")),
                    sample_count=_as_int(raw_last_result.get("sample_count"), default=0),
                    trials=_as_int(raw_last_result.get("trials"), default=0),
                    baseline_score=_as_float(raw_last_result.get("baseline_score"), default=0.0),
                    objective_score=_as_float(raw_last_result.get("objective_score"), default=0.0),
                    improvement=_as_float(raw_last_result.get("improvement"), default=0.0),
                    params=params,
                    tuned_at=(
                        str(raw_last_result.get("tuned_at"))
                        if raw_last_result.get("tuned_at") is not None
                        else None
                    ),
                )

    def _load_shadow_online_state(self) -> None:
        if not self._shadow_online_state_path.exists():
            return
        try:
            payload = json.loads(self._shadow_online_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(payload, Mapping):
            self._shadow_online_state = dict(payload)

    def _load_shadow_online_v2_state(self) -> None:
        self._shadow_online_v2_state = self._shadow_online_v2_state_store.load_state()

    def _persist_m2_state(self, now: datetime) -> None:
        self._m2_state_path.parent.mkdir(parents=True, exist_ok=True)
        history_limit = max(8, self._config.m2_optuna_history_limit)
        payload = {
            "state": self._m2_controller.dump_state(),
            "params": self._m2_controller.dump_params(),
            "history": [
                {
                    "atr_ratio": item.atr_ratio,
                    "sector_dispersion": item.sector_dispersion,
                    "turnover_zscore": item.turnover_zscore,
                }
                for item in self._m2_observation_history[-history_limit:]
            ],
            "latency_breach_history": [bool(item) for item in self._latency_breach_history[-20:]],
            "execution_sensitivity_history": [
                bool(item) for item in self._execution_sensitivity_history[-30:]
            ],
            "online_update_state": dict(self._online_update_state),
            "utility_execution_state": dict(self._utility_execution_state),
            "reconcile_drift_state": dict(self._reconcile_drift_state),
            "optuna": {
                "last_tuned_at": (
                    self._m2_last_tuned_at.isoformat()
                    if self._m2_last_tuned_at is not None
                    else None
                ),
                "artifact_uri": self._m2_last_artifact_uri,
                "last_result": self._m2_last_optuna.to_dict() if self._m2_last_optuna else None,
            },
            "updated_at": now.isoformat(),
        }
        self._m2_state_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _persist_shadow_online_state(self, now: datetime) -> None:
        self._shadow_online_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(self._shadow_online_state)
        payload["updated_at"] = now.isoformat()
        self._shadow_online_state_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _persist_shadow_online_v2_state(
        self,
        *,
        now: datetime,
        source_trace_id: str,
        run_id: str,
    ) -> None:
        metadata: dict[str, object] = {}
        if source_trace_id.strip():
            metadata["source_trace_id"] = source_trace_id.strip()
        if run_id.strip():
            metadata["run_id"] = run_id.strip()
        self._shadow_online_v2_state_store.save_state(
            state=self._shadow_online_v2_state,
            now=now,
            metadata=metadata,
        )

    def _run_m2_optuna_like_tuning(self, now: datetime) -> M2OptunaLikeResult:
        if not self._config.m2_optuna_enabled:
            result = M2OptunaLikeResult(
                backend="optuna_like_random_search",
                tuned=False,
                reason="disabled",
                sample_count=len(self._m2_observation_history),
                trials=0,
                baseline_score=0.0,
                objective_score=0.0,
                improvement=0.0,
                params=self._m2_controller.params,
                tuned_at=None,
            )
            self._m2_last_optuna = result
            return result

        interval_days = max(1, self._config.m2_optuna_retrain_interval_days)
        guard_now = _as_utc_datetime(now)
        if self._m2_last_tuned_at is not None:
            last_tuned = _as_utc_datetime(self._m2_last_tuned_at)
            if guard_now - last_tuned < timedelta(days=interval_days):
                baseline = tune_regime_with_optuna_like_search(
                    observations=self._m2_observation_history,
                    baseline_params=self._m2_controller.params,
                    config=M2OptunaLikeConfig(
                        n_trials=1,
                        min_samples=0,
                        min_improvement=0.0,
                        random_seed=self._config.m2_optuna_random_seed,
                    ),
                    now=now,
                )
                result = M2OptunaLikeResult(
                    backend="optuna_like_random_search",
                    tuned=False,
                    reason="interval_guard",
                    sample_count=len(self._m2_observation_history),
                    trials=0,
                    baseline_score=baseline.baseline_score,
                    objective_score=baseline.baseline_score,
                    improvement=0.0,
                    params=self._m2_controller.params,
                    tuned_at=last_tuned.isoformat(),
                )
                self._m2_last_optuna = result
                return result

        tune_result = tune_regime_with_optuna_like_search(
            observations=self._m2_observation_history,
            baseline_params=self._m2_controller.params,
            config=M2OptunaLikeConfig(
                n_trials=max(1, self._config.m2_optuna_trials),
                min_samples=max(8, self._config.m2_optuna_min_samples),
                min_improvement=max(0.0, self._config.m2_optuna_min_improvement),
                random_seed=self._config.m2_optuna_random_seed,
            ),
            now=now,
        )
        if tune_result.tuned:
            self._m2_controller.set_params(tune_result.params)
            self._m2_last_tuned_at = guard_now
        self._m2_last_optuna = tune_result
        return tune_result

    def _persist_m2_params_snapshot(
        self,
        run_now: datetime,
        source_trace_id: str,
        optuna_result: M2OptunaLikeResult,
    ) -> str:
        suffix = uuid4().hex[:8]
        output_path = self._resolve_path(
            f"{self._config.m2_optuna_artifact_dir}/"
            f"m2_hmm_params_{run_now.strftime('%Y%m%d_%H%M%S')}_{suffix}.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": run_now.isoformat(),
            "source_trace_id": source_trace_id,
            "active_state": self._m2_controller.dump_state(),
            "active_params": self._m2_controller.dump_params(),
            "optuna": optuna_result.to_dict(),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact_uri = self._to_relative_if_possible(str(output_path)) or str(output_path)
        self._m2_last_artifact_uri = artifact_uri
        return artifact_uri

    def _evaluate_universe_consistency(
        self,
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        snapshot_ids: set[str] = set()
        spec_hashes: set[str] = set()
        missing_snapshot_rows = 0
        for item in records:
            snapshot_id = str(item.get("universe_snapshot_id", "")).strip()
            if snapshot_id:
                snapshot_ids.add(snapshot_id)
            else:
                missing_snapshot_rows += 1
            spec_hash = str(item.get("universe_spec_hash", "")).strip()
            if spec_hash:
                spec_hashes.add(spec_hash)

        if not snapshot_ids and not spec_hashes:
            return {
                "status": "not_provided",
                "consistent": True,
                "snapshot_id": "",
                "universe_spec_hash": "",
                "unique_snapshot_ids": [],
                "unique_spec_hashes": [],
                "missing_snapshot_rows": int(missing_snapshot_rows),
            }

        consistent = len(snapshot_ids) <= 1 and len(spec_hashes) <= 1
        status = "consistent" if consistent and missing_snapshot_rows == 0 else "partial_missing"
        if not consistent:
            status = "inconsistent"
        ordered_snapshot_ids = sorted(snapshot_ids)
        ordered_spec_hashes = sorted(spec_hashes)
        return {
            "status": status,
            "consistent": consistent,
            "snapshot_id": ordered_snapshot_ids[0] if ordered_snapshot_ids else "",
            "universe_spec_hash": ordered_spec_hashes[0] if ordered_spec_hashes else "",
            "unique_snapshot_ids": ordered_snapshot_ids,
            "unique_spec_hashes": ordered_spec_hashes,
            "missing_snapshot_rows": int(missing_snapshot_rows),
        }

    def _persist_m8_suggestions(
        self,
        run_now: datetime,
        report: Mapping[str, object],
        source_trace_id: str,
    ) -> str:
        suffix = uuid4().hex[:8]
        output_path = self._resolve_path(
            f"{self._config.suggestions_dir}/m8/"
            f"m8_suggest_{run_now.strftime('%Y%m%d_%H%M%S')}_{suffix}.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": run_now.isoformat(),
            "source_trace_id": source_trace_id,
            "report": dict(report),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._to_relative_if_possible(str(output_path)) or str(output_path)

    def _persist_m7_report(
        self,
        run_now: datetime,
        report: Mapping[str, object],
        source_trace_id: str,
    ) -> str:
        suffix = uuid4().hex[:8]
        output_path = self._resolve_path(
            f"{self._config.suggestions_dir}/m7/"
            f"m7_report_{run_now.strftime('%Y%m%d_%H%M%S')}_{suffix}.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": run_now.isoformat(),
            "source_trace_id": source_trace_id,
            "report": dict(report),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._to_relative_if_possible(str(output_path)) or str(output_path)

    def _persist_shadow_online_report(
        self,
        run_now: datetime,
        report: Mapping[str, object],
        source_trace_id: str,
        report_kind: str = "shadow_online",
    ) -> str:
        normalized_report_kind = str(report_kind).strip() or "shadow_online"
        suffix = uuid4().hex[:8]
        output_path = self._resolve_path(
            f"{self._config.shadow_online_report_dir}/"
            f"{normalized_report_kind}_{run_now.strftime('%Y%m%d_%H%M%S')}_{suffix}.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": run_now.isoformat(),
            "source_trace_id": source_trace_id,
            "report": dict(report),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._to_relative_if_possible(str(output_path)) or str(output_path)

    def _collect_module_artifacts(
        self,
        module_details: Mapping[str, object],
    ) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        for module_key, detail in module_details.items():
            if not isinstance(module_key, str):
                continue
            if not isinstance(detail, Mapping):
                continue
            artifact_uri = detail.get("artifact_uri")
            if isinstance(artifact_uri, str) and artifact_uri.strip():
                artifacts[module_key.upper()] = artifact_uri
        return artifacts

    def _resolve_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return candidate
        return self._project_root / candidate

    def _derive_shadow_online_v2_state_path(self) -> Path:
        base_path = self._shadow_online_state_path
        stem = base_path.stem if base_path.suffix else base_path.name
        if stem.endswith("_state"):
            stem = f"{stem[:-6]}_v2_state"
        else:
            stem = f"{stem}_v2_state"
        suffix = base_path.suffix or ".json"
        return base_path.with_name(f"{stem}{suffix}")

    def _derive_shadow_online_v2_metrics_path(self) -> Path:
        base_path = self._shadow_online_state_path
        stem = base_path.stem if base_path.suffix else base_path.name
        if stem.endswith("_state"):
            stem = f"{stem[:-6]}_v2_metrics"
        else:
            stem = f"{stem}_v2_metrics"
        return base_path.with_name(f"{stem}.jsonl")

    def _derive_m3_vector_profile_db_path(self) -> Path:
        return self._m3_store_dir / "vector_profiles.duckdb"

    def _to_relative_if_possible(self, raw_path: str | None) -> str | None:
        if raw_path is None:
            return None
        path = Path(raw_path)
        try:
            return str(path.relative_to(self._project_root)).replace("\\", "/")
        except ValueError:
            return raw_path

    @staticmethod
    def _best_effort_symbol(records: Sequence[Mapping[str, object]]) -> str:
        if not records:
            return "UNKNOWN"
        return str(records[0].get("symbol", "UNKNOWN"))


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _candidate_has_llm_gate_inputs(candidate: Mapping[str, object]) -> bool:
    raw_verdict = candidate.get("llm_verdict")
    raw_confidence = candidate.get("llm_confidence")
    if not isinstance(raw_verdict, str) or not raw_verdict.strip():
        return False
    confidence = _as_float(raw_confidence, default=-1.0)
    return 0.0 <= confidence <= 1.0


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        deduped.append(stripped)
    return deduped


def _gate_passed(value: object) -> bool:
    if not isinstance(value, Mapping):
        return True
    return bool(value.get("passed", True))


def _gate_reason_codes(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    raw_reasons = value.get("reason_codes", [])
    if not isinstance(raw_reasons, list):
        return []
    return _dedupe_strings([str(item) for item in raw_reasons if isinstance(item, str)])


def _summarize_intraday_loader_records(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    total = len(records)
    intraday_1m_records = sum(
        1 for record in records if _record_has_intraday_context(record, prefix="intraday_1m")
    )
    intraday_5m_records = sum(
        1 for record in records if _record_has_intraday_context(record, prefix="intraday_5m")
    )
    denominator = max(total, 1)
    return {
        "intraday_1m_records": intraday_1m_records,
        "intraday_5m_records": intraday_5m_records,
        "intraday_1m_coverage_ratio": round(intraday_1m_records / denominator, 4)
        if total
        else 0.0,
        "intraday_5m_coverage_ratio": round(intraday_5m_records / denominator, 4)
        if total
        else 0.0,
    }


def _record_has_intraday_context(record: Mapping[str, object], *, prefix: str) -> bool:
    latest_date = str(record.get(f"{prefix}_latest_date", "")).strip()
    if latest_date:
        return True
    for key, value in record.items():
        if not str(key).startswith(f"{prefix}_"):
            continue
        if value not in (None, "", []):
            return True
    return False


def _build_rollback_context_from_m11(
    *,
    observations: Sequence[M11ShadowObservation],
    m11_detail: Mapping[str, object],
    m11_input: Mapping[str, object],
) -> tuple[RollbackContext, RollbackEvaluationInput]:
    diff_returns = [
        float(observation.challenger_shadow_return - observation.champion_shadow_return)
        for observation in observations
    ]
    champion_returns = [float(observation.champion_shadow_return) for observation in observations]
    trade_count = sum(
        1
        for observation in observations
        if (observation.champion_signal or 0) > 0 or (observation.challenger_signal or 0) > 0
    )
    soft_days = _trailing_condition_streak(diff_returns, threshold=0.0)
    shadow_vol = (
        float(np.std(np.asarray(champion_returns, dtype=float))) if champion_returns else 0.0
    )
    hard_floor = min(-0.001, -0.10 * max(shadow_vol, 0.001))
    hard_days = _trailing_condition_streak(diff_returns, threshold=hard_floor)
    context = RollbackContext(
        trade_count=trade_count,
        observed_days=len(diff_returns),
        consecutive_soft_days=soft_days,
        consecutive_hard_days=hard_days,
    )
    redlines_raw = m11_detail.get("redlines", {})
    redlines = redlines_raw if isinstance(redlines_raw, Mapping) else {}
    return context, {
        "diff_returns": diff_returns or [0.0],
        "shadow_champion_vol": shadow_vol,
        "m11_status": str(m11_detail.get("status", "unknown")),
        "m11_input_source": str(m11_input.get("source", "unknown")),
        "m11_path": (
            str(m11_input.get("path"))
            if isinstance(m11_input.get("path"), str)
            else None
        ),
        "m11_path_exists": bool(m11_input.get("path_exists", False)),
        "loaded_samples": _as_int(m11_input.get("loaded_samples"), default=len(observations)),
        "hard_drawdown_breach": bool(redlines.get("drawdown_delta", False)),
        "tail_loss_triggered": bool(redlines.get("tail_loss_delta", False)),
    }


def _trailing_condition_streak(values: Sequence[float], *, threshold: float) -> int:
    streak = 0
    for value in reversed(values):
        if float(value) < threshold:
            streak += 1
            continue
        break
    return streak
