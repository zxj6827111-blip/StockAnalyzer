from __future__ import annotations

from stock_analyzer.evolution.llm_semantic import (
    OpenAICompatibleSemanticJudge,
    _build_responses_payload,
    _extract_responses_content,
    parse_llm_semantic_output,
)


def test_parse_llm_semantic_output_from_json() -> None:
    verdict, confidence, reason = parse_llm_semantic_output(
        '{"verdict":"approve","confidence":0.81,"reason":"trend is stable"}'
    )
    assert verdict == "approve"
    assert abs(confidence - 0.81) < 1e-9
    assert "trend" in reason


def test_parse_llm_semantic_output_supports_code_fence_and_percentage() -> None:
    verdict, confidence, reason = parse_llm_semantic_output(
        "```json\n{\"decision\":\"复核\",\"confidence\":\"72%\",\"reason\":\"信号中性\"}\n```"
    )
    assert verdict == "review"
    assert abs(confidence - 0.72) < 1e-9
    assert reason == "信号中性"


def test_openai_compatible_semantic_judge_not_configured() -> None:
    judge = OpenAICompatibleSemanticJudge(api_key="", model="")
    decision = judge.judge(
        {
            "symbol": "600000",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 2_000_000,
        }
    )
    assert decision.error == "not_configured"
    assert decision.verdict == "review"


def test_extract_responses_content_from_response_payload() -> None:
    content = _extract_responses_content(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"verdict":"approve"}'},
                    ],
                }
            ]
        }
    )
    assert content == '{"verdict":"approve"}'


def test_build_responses_payload_promotes_system_message_to_instructions() -> None:
    payload = _build_responses_payload(
        model="gpt-5.4",
        temperature=0.0,
        max_tokens=128,
        messages=[
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": "Hello"},
        ],
    )
    assert payload["model"] == "gpt-5.4"
    assert payload["instructions"] == "Return JSON only."
    assert payload["max_output_tokens"] == 128
    assert payload["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "Hello"}],
        }
    ]
