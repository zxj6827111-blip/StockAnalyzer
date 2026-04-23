from __future__ import annotations

from stock_analyzer.evolution.m3_vector_profile import build_default_m3_vector_profile
from stock_analyzer.evolution.modules.m8_memory_bridge import (
    build_m8_query_vector,
    run_m8_memory_bridge,
)


def test_build_m8_query_vector_requires_valid_close() -> None:
    assert build_m8_query_vector({"close": 0.0}) is None
    profile = build_default_m3_vector_profile()
    vector = build_m8_query_vector(
        {
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 2_000_000,
        }
    )
    assert vector is not None
    assert len(vector) == profile.vector_dim


def test_run_m8_memory_bridge_recommendation_mapping() -> None:
    def fake_search(vector: list[float], top_k: int) -> dict[str, object]:
        if vector[0] < -0.015:
            return {"indices": [1], "scores": [0.9], "total_vectors": 100}
        return {"indices": [2], "scores": [0.6], "total_vectors": 100}

    result = run_m8_memory_bridge(
        candidates=[
            {
                "symbol": "A",
                "open": 10.6,
                "high": 11.0,
                "low": 10.3,
                "close": 10.8,
                "volume": 10,
                "pcv_score": 0.9,
                "deflated_sharpe": 0.30,
                "fdr_p_value": 0.05,
                "llm_verdict": "approve",
                "llm_confidence": 0.9,
            },
            {
                "symbol": "B",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 20,
                "pcv_score": 0.7,
                "deflated_sharpe": 0.15,
                "fdr_p_value": 0.09,
                "llm_verdict": "review",
                "llm_confidence": 0.9,
            },
        ],
        search_fn=fake_search,
        top_k=3,
    )
    assert result.top_k == 3
    assert result.promoted == 1
    assert result.review == 1
    assert result.invalid == 0
    assert len(result.suggestions) == 2
    assert result.gate_pass_rate > 0.0
    assert "pcv" in result.gate_names
    assert "pcv" in result.gate_provenance_counts
    first = result.suggestions[0]
    assert first.vector_profile_id == ""
    assert first.passed_gates >= 1
    assert first.gate_total == 6
    assert len(first.gate_checks) == 6


def test_run_m8_memory_bridge_registry_gate_can_block_signatures() -> None:
    def fake_search(vector: list[float], top_k: int) -> dict[str, object]:
        return {"indices": [7], "scores": [0.88], "total_vectors": 100}

    blocked_signature = "M8:BLOCKED:ALPHA"
    result = run_m8_memory_bridge(
        candidates=[
            {
                "symbol": "A",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 100,
                "factor_signature": blocked_signature,
            }
        ],
        search_fn=fake_search,
        top_k=3,
        registry_blocked_signatures=[blocked_signature],
    )
    assert result.promoted == 0
    assert result.review == 1 or result.novel == 1
    assert result.gate_failure_counts["registry"] >= 1
    item = result.suggestions[0]
    assert item.registry_signature == blocked_signature
    assert "registry" in item.failed_gates


def test_run_m8_memory_bridge_strict_gate_inputs_blocks_missing_fields() -> None:
    def fake_search(vector: list[float], top_k: int) -> dict[str, object]:
        return {"indices": [1], "scores": [0.92], "total_vectors": 100}

    result = run_m8_memory_bridge(
        candidates=[
            {"symbol": "A", "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1, "volume": 10}
        ],
        search_fn=fake_search,
        strict_gate_inputs=True,
    )
    item = result.suggestions[0]
    assert result.strict_gate_inputs is True
    assert "pcv" in item.failed_gates
    assert "deflated_sharpe_fdr" in item.failed_gates
    assert "llm_semantic" in item.failed_gates
    assert "pcv" in item.missing_gate_inputs
    first_gate = next(gate for gate in item.gate_checks if gate.name == "pcv")
    assert first_gate.provenance == "missing"
