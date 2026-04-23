from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator
from stock_analyzer.evolution.specs import build_spec_hash_bundle


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_build_spec_hash_bundle_is_deterministic_and_sensitive_to_changes() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")

    first = build_spec_hash_bundle(config=config.evolution)
    second = build_spec_hash_bundle(config=config.evolution)
    assert first["execution_spec_hash"] == second["execution_spec_hash"]
    assert first["runtime_config_hash"] == second["runtime_config_hash"]
    assert first["universe_spec_hash"] == second["universe_spec_hash"]

    config.evolution.execution_spec.min_notional_per_order = 6000.0
    changed = build_spec_hash_bundle(config=config.evolution)
    assert changed["execution_spec_hash"] != first["execution_spec_hash"]

    config.evolution.execution_spec.min_notional_per_order = 5000.0
    config.evolution.execution_spec.share_rounding_rule = "lot_down_100_v2"
    changed_rounding = build_spec_hash_bundle(config=config.evolution)
    assert changed_rounding["execution_spec_hash"] != first["execution_spec_hash"]

    config.evolution.execution_spec.share_rounding_rule = "lot_down_100"
    config.evolution.execution_spec.residual_order_policy = "rollover_v2"
    changed_residual = build_spec_hash_bundle(config=config.evolution)
    assert changed_residual["execution_spec_hash"] != first["execution_spec_hash"]


def test_orchestrator_report_contains_spec_hashes_and_payload_snapshot(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)

    report = orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000000,
            }
        ],
        now=datetime(2026, 3, 2, 20, 40, tzinfo=UTC),
        dry_run=True,
        source_trace_id="spec-hash-test",
    )

    execution_spec_hash = str(report.get("execution_spec_hash", ""))
    runtime_config_hash = str(report.get("runtime_config_hash", ""))
    universe_spec_hash = str(report.get("universe_spec_hash", ""))
    assert len(execution_spec_hash) == 64
    assert len(runtime_config_hash) == 64
    assert len(universe_spec_hash) == 64

    proposal = _as_mapping(report["proposal"])
    payload_path = tmp_path / str(proposal["payload_uri"])
    payload = _as_mapping(json.loads(payload_path.read_text(encoding="utf-8")))
    spec_hashes = _as_mapping(payload.get("spec_hashes", {}))
    assert spec_hashes["execution_spec_hash"] == execution_spec_hash
    assert spec_hashes["runtime_config_hash"] == runtime_config_hash
    assert spec_hashes["universe_spec_hash"] == universe_spec_hash
    reproducibility = _as_mapping(report.get("reproducibility", {}))
    assert reproducibility["random_seed"] == config.evolution.runtime_spec.random_seed
    assert reproducibility["num_threads"] == config.evolution.runtime_spec.num_threads
    assert reproducibility["deterministic_mode"] is True
    assert len(str(reproducibility["library_versions_hash"])) == 64
    payload_repro = _as_mapping(payload.get("reproducibility", {}))
    assert payload_repro["random_seed"] == reproducibility["random_seed"]
    assert payload_repro["num_threads"] == reproducibility["num_threads"]
