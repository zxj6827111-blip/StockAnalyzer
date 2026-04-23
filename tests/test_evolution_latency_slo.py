from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.evolution.latency_slo import evaluate_latency_slo
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def test_latency_slo_enters_latency_watch_on_mild_breach() -> None:
    now = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    result = evaluate_latency_slo(
        records=[
            {
                "market_price_available_time": (now - timedelta(seconds=130)).isoformat(),
                "suspension_status_available_time": now.isoformat(),
            }
        ],
        now=now,
        required_inputs=["market_price", "suspension_status"],
        max_data_latency_sec=120.0,
        previous_breach_history=[],
    )
    assert result.data_latency_sec > 120.0
    assert result.latency_watch_flag is True
    assert result.limited_observability is False
    assert result.action.state == "latency_watch"
    assert result.action.block_online_update is True
    assert result.action.raise_u_threshold_bp == 10


def test_latency_slo_enters_limited_observability_on_severe_breach() -> None:
    now = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    result = evaluate_latency_slo(
        records=[
            {
                "market_price_available_time": (now - timedelta(seconds=300)).isoformat(),
                "suspension_status_available_time": now.isoformat(),
            }
        ],
        now=now,
        required_inputs=["market_price", "suspension_status"],
        max_data_latency_sec=120.0,
        previous_breach_history=[],
    )
    assert result.data_latency_sec > 240.0
    assert result.limited_observability is True
    assert result.action.state == "limited_observability"
    assert result.action.block_online_update is True
    assert result.action.raise_u_threshold_bp == 30


def test_latency_slo_enters_limited_observability_on_sustained_breach_ratio() -> None:
    now = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    history = [True, True, True, True] + [False] * 15
    result = evaluate_latency_slo(
        records=[{"market_price_available_time": (now - timedelta(seconds=60)).isoformat()}],
        now=now,
        required_inputs=["market_price"],
        max_data_latency_sec=120.0,
        previous_breach_history=history,
    )
    assert result.data_latency_sec <= 120.0
    assert result.breach_ratio_20d > 0.15
    assert result.limited_observability is True
    assert result.action.force_champion_only is True


def test_orchestrator_m10_includes_latency_state_and_action(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    orchestrator = OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path)

    run_now = datetime(2026, 3, 2, 20, 40, tzinfo=UTC)
    stale_time = (run_now - timedelta(seconds=300)).isoformat()
    report = orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000000,
                "market_price_available_time": stale_time,
            }
        ],
        now=run_now,
        dry_run=True,
        source_trace_id="latency-integration",
    )
    modules = _as_mapping(report["modules"])
    m10 = _as_mapping(modules["m10"])
    assert m10["status"] == "limited_observability"
    metrics = _as_mapping(m10["metrics"])
    assert _as_float(metrics["data_latency_sec"]) > 240.0
    assert bool(metrics["latency_watch_flag"]) is False
    action = _as_mapping(m10["latency_action"])
    assert bool(action["block_online_update"]) is True
    assert _as_int(action["raise_u_threshold_bp"]) == 30
