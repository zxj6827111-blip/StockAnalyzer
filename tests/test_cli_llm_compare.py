from __future__ import annotations

from pytest import MonkeyPatch

from stock_analyzer.cli import _load_llm_compare_profiles, _score_llm_compare_result


def test_load_llm_compare_profiles_uses_defaults_and_shared_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SA_LLM_COMPARE_SHARED_API_KEY", "sk-shared")
    profiles = _load_llm_compare_profiles()
    assert len(profiles) == 4
    assert profiles[0]["name"] == "GLM-5"
    assert profiles[0]["model"] == "ZhipuAI/GLM-5"
    assert profiles[0]["api_key"] == "sk-shared"
    assert profiles[3]["name"] == "DeepSeek-V3.2"


def test_score_llm_compare_result_prefers_successful_output() -> None:
    ok = _score_llm_compare_result(
        error="",
        verdict="approve",
        confidence=0.81,
        reason="结构化回答完整，理由清晰。",
        latency_ms=1200,
    )
    failed = _score_llm_compare_result(
        error="timeout",
        verdict="review",
        confidence=0.0,
        reason="",
        latency_ms=16000,
    )
    assert ok > failed
