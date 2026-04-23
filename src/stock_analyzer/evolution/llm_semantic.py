"""OpenAI-compatible semantic judge for M8 Gate-3."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib import error, request

_JSON_BLOCK_PATTERN = re.compile(r"\{[\s\S]*\}")
_CONFIDENCE_PATTERN = re.compile(
    r"(?:confidence|置信度)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)%?",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LlmSemanticDecision:
    """One semantic verdict for M8 gate input."""

    verdict: str
    confidence: float
    reason: str
    error: str = ""
    raw: str = ""


@dataclass(frozen=True, slots=True)
class LlmNewsReview:
    """One semantic review for M7 news enrichment."""

    verdict: str
    sentiment: float
    confidence: float
    reason: str
    error: str = ""
    raw: str = ""


@dataclass(slots=True)
class OpenAICompatibleSemanticJudge:
    """Minimal OpenAI-compatible chat completion client."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_sec: int = 8
    temperature: float = 0.0
    max_tokens: int = 120

    @property
    def configured(self) -> bool:
        return bool(
            self.api_key.strip() and self.model.strip() and self.base_url.strip()
        )

    def judge(self, candidate: Mapping[str, object]) -> LlmSemanticDecision:
        """Call one LLM completion and return normalized decision."""
        if not self.configured:
            return LlmSemanticDecision(
                verdict="review",
                confidence=0.0,
                reason="",
                error="not_configured",
            )
        content, error = _request_openai_compatible_content(
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_sec=self.timeout_sec,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=_build_messages(candidate=candidate),
        )
        if error:
            return LlmSemanticDecision(
                verdict="review",
                confidence=0.0,
                reason="",
                error=error,
            )
        verdict, confidence, reason = parse_llm_semantic_output(content)
        return LlmSemanticDecision(
            verdict=verdict,
            confidence=confidence,
            reason=reason,
            raw=content,
        )


@dataclass(slots=True)
class OpenAICompatibleNewsJudge:
    """Minimal OpenAI-compatible reviewer for news items."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_sec: int = 8
    temperature: float = 0.0
    max_tokens: int = 160

    @property
    def configured(self) -> bool:
        return bool(
            self.api_key.strip() and self.model.strip() and self.base_url.strip()
        )

    def review(self, news_item: Mapping[str, object]) -> LlmNewsReview:
        """Review one news item and return normalized sentiment payload."""
        if not self.configured:
            return LlmNewsReview(
                verdict="neutral",
                sentiment=0.0,
                confidence=0.0,
                reason="",
                error="not_configured",
            )
        content, error = _request_openai_compatible_content(
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_sec=self.timeout_sec,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=_build_news_review_messages(news_item=news_item),
        )
        if error:
            return LlmNewsReview(
                verdict="neutral",
                sentiment=0.0,
                confidence=0.0,
                reason="",
                error=error,
            )
        verdict, sentiment, confidence, reason = parse_llm_news_review_output(content)
        return LlmNewsReview(
            verdict=verdict,
            sentiment=sentiment,
            confidence=confidence,
            reason=reason,
            raw=content,
        )


def parse_llm_semantic_output(content: str) -> tuple[str, float, str]:
    """Parse provider text into normalized ``(verdict, confidence, reason)`` tuple."""
    cleaned = _strip_code_fence(content.strip())
    verdict: str | None = None
    confidence: float | None = None
    reason = ""

    payload = _extract_json_payload(cleaned)
    if payload is not None:
        verdict = _normalize_verdict(
            _as_non_empty_str(
                payload.get("verdict")
                or payload.get("decision")
                or payload.get("recommendation")
                or payload.get("label")
            )
        )
        confidence = _as_probability(
            payload.get("confidence")
            or payload.get("probability")
            or payload.get("score")
            or payload.get("conf")
        )
        reason = _as_non_empty_str(payload.get("reason") or payload.get("rationale")) or ""

    if verdict is None:
        verdict = _normalize_verdict(cleaned)
    if confidence is None:
        confidence = _extract_confidence(cleaned)
    if verdict is None:
        verdict = "review"
    if confidence is None:
        confidence = 0.55 if verdict == "review" else 0.60
    if not reason:
        reason = _truncate_text(cleaned, limit=120)
    return verdict, _clamp01(confidence), reason


def _build_messages(candidate: Mapping[str, object]) -> list[dict[str, str]]:
    fields = {
        "symbol": _as_non_empty_str(candidate.get("symbol")) or "UNKNOWN",
        "headline": _as_non_empty_str(candidate.get("headline") or candidate.get("news") or ""),
        "open": _to_float(candidate.get("open"), default=0.0),
        "high": _to_float(candidate.get("high"), default=0.0),
        "low": _to_float(candidate.get("low"), default=0.0),
        "close": _to_float(candidate.get("close"), default=0.0),
        "volume": _to_float(candidate.get("volume"), default=0.0),
        "pcv_score": _to_float(candidate.get("pcv_score"), default=0.0),
        "deflated_sharpe": _to_float(candidate.get("deflated_sharpe"), default=0.0),
        "fdr_p_value": _to_float(candidate.get("fdr_p_value"), default=1.0),
    }
    system_prompt = (
        "你是A股量化系统M8模块的语义判定器。"
        "请基于给定信息给出 verdict/confidence/reason。"
        "verdict 只能是 approve、review、reject。"
        "confidence 为 0 到 1 的小数。"
        "只输出 JSON，不要额外文本。"
    )
    user_prompt = (
        "请判断以下候选是否应通过语义门控：\n"
        f"{json.dumps(fields, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _build_news_review_messages(news_item: Mapping[str, object]) -> list[dict[str, str]]:
    fields = {
        "symbol": _as_non_empty_str(news_item.get("symbol")) or "UNKNOWN",
        "headline": _as_non_empty_str(
            news_item.get("headline") or news_item.get("title") or news_item.get("news") or ""
        ),
        "content": _truncate_text(
            _as_non_empty_str(news_item.get("content") or news_item.get("summary") or "") or "",
            limit=240,
        ),
        "source": _as_non_empty_str(news_item.get("source")) or "",
        "published_at": _as_non_empty_str(news_item.get("published_at")) or "",
    }
    system_prompt = (
        "你是A股交易系统的新闻审核器。"
        "请根据新闻标题与内容，判断该消息对对应股票的短线交易影响方向。"
        "只输出 JSON，字段为 "
        '{"verdict":"positive|neutral|negative","sentiment":-1到1之间小数,'
        '"confidence":0到1之间小数,"reason":"不超过40字"}。'
    )
    user_prompt = "请审核以下新闻：\n" + json.dumps(fields, ensure_ascii=False)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _extract_message_content(payload: Mapping[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            if parts:
                return "\n".join(parts)
    text = first.get("text")
    if isinstance(text, str):
        return text
    return ""


def _extract_responses_content(payload: Mapping[str, object]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "message":
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, Mapping):
                    continue
                text = content_item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        else:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def _build_responses_payload(
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    messages: list[dict[str, str]],
) -> dict[str, object]:
    instructions_parts: list[str] = []
    input_items: list[dict[str, object]] = []
    for message in messages:
        role = _as_non_empty_str(message.get("role")) or "user"
        content = _as_non_empty_str(message.get("content")) or ""
        if not content:
            continue
        if role == "system":
            instructions_parts.append(content)
            continue
        normalized_role = role if role in {"user", "assistant", "developer"} else "user"
        input_items.append(
            {
                "role": normalized_role,
                "content": [{"type": "input_text", "text": content}],
            }
        )
    if not input_items:
        input_items.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "请返回 JSON。"}],
            }
        )
    payload: dict[str, object] = {
        "model": model,
        "input": input_items,
        "temperature": _clamp01(temperature),
        "max_output_tokens": max(32, max_tokens),
    }
    instructions = "\n\n".join(part for part in instructions_parts if part.strip()).strip()
    if instructions:
        payload["instructions"] = instructions
    return payload


def _request_openai_responses_content(
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout_sec: int,
    temperature: float,
    max_tokens: int,
    messages: list[dict[str, str]],
) -> tuple[str, str]:
    endpoint = base_url.rstrip("/") + "/responses"
    body = _build_responses_payload(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=messages,
    )
    req = request.Request(
        url=endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "StockAnalyzer/1.0",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with request.urlopen(req, timeout=max(1, timeout_sec)) as resp:
            status = _to_int(getattr(resp, "status", 200), default=200)
            payload = resp.read().decode("utf-8", errors="replace")
        if status < 200 or status >= 300:
            return "", f"http_{status}"
        parsed = json.loads(payload)
    except Exception as exc:  # pragma: no cover - network dependent.
        return "", str(exc)
    if not isinstance(parsed, Mapping):
        return "", "invalid_response_payload"
    content = _extract_responses_content(parsed)
    if not content:
        return "", "empty_response_content"
    return content, ""


def _request_openai_compatible_content(
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout_sec: int,
    temperature: float,
    max_tokens: int,
    messages: list[dict[str, str]],
) -> tuple[str, str]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "temperature": _clamp01(temperature),
        "max_tokens": max(32, max_tokens),
        "messages": messages,
    }
    req = request.Request(
        url=endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "StockAnalyzer/1.0",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with request.urlopen(req, timeout=max(1, timeout_sec)) as resp:
            status = _to_int(getattr(resp, "status", 200), default=200)
            payload = resp.read().decode("utf-8", errors="replace")
        if status < 200 or status >= 300:
            return "", f"http_{status}"
        parsed = json.loads(payload)
    except error.HTTPError as exc:  # pragma: no cover - network dependent.
        response_text = exc.read().decode("utf-8", errors="replace")
        lowered = response_text.lower()
        if (
            exc.code in {400, 404}
            and (
                "unsupported legacy protocol" in lowered
                or "/v1/responses" in lowered
                or '"object":"response"' in lowered
            )
        ):
            return _request_openai_responses_content(
                api_key=api_key,
                model=model,
                base_url=base_url,
                timeout_sec=timeout_sec,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=messages,
            )
        return "", f"HTTP Error {exc.code}: {response_text or exc.reason}"
    except Exception as exc:  # pragma: no cover - network dependent.
        return "", str(exc)
    if not isinstance(parsed, Mapping):
        return "", "invalid_response_payload"
    content = _extract_message_content(parsed)
    if not content:
        return "", "empty_response_content"
    return content, ""


def _extract_json_payload(content: str) -> Mapping[str, object] | None:
    if not content:
        return None
    try:
        parsed = json.loads(content)
        if isinstance(parsed, Mapping):
            return parsed
    except json.JSONDecodeError:
        pass

    match = _JSON_BLOCK_PATTERN.search(content)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _normalize_verdict(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    approve_tokens = (
        "approve",
        "approved",
        "pass",
        "promote",
        "buy",
        "bull",
        "通过",
        "买入",
        "看多",
    )
    reject_tokens = ("reject", "rejected", "deny", "block", "sell", "bear", "拒绝", "卖出", "看空")
    review_tokens = ("review", "hold", "neutral", "watch", "pending", "复核", "观望", "中性")
    if any(token in normalized for token in approve_tokens):
        return "approve"
    if any(token in normalized for token in reject_tokens):
        return "reject"
    if any(token in normalized for token in review_tokens):
        return "review"
    if normalized in {"a", "1", "yes"}:
        return "approve"
    if normalized in {"r", "-1", "no"}:
        return "reject"
    return None


def _normalize_news_verdict(value: str | None) -> str:
    if not value:
        return "neutral"
    normalized = value.strip().lower()
    if not normalized:
        return "neutral"
    positive_tokens = ("positive", "bullish", "利好", "正面", "看多", "买入")
    negative_tokens = ("negative", "bearish", "利空", "负面", "看空", "卖出")
    neutral_tokens = ("neutral", "review", "中性", "观望", "未知")
    if normalized in positive_tokens or any(token in normalized for token in positive_tokens):
        return "positive"
    if normalized in negative_tokens or any(token in normalized for token in negative_tokens):
        return "negative"
    if normalized in neutral_tokens or any(token in normalized for token in neutral_tokens):
        return "neutral"
    return "neutral"


def _normalize_sentiment_value(value: object, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return max(-1.0, min(1.0, default))
    if isinstance(value, (int, float)):
        return max(-1.0, min(1.0, float(value)))
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned:
            return max(-1.0, min(1.0, default))
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1].strip()
            try:
                return max(-1.0, min(1.0, float(cleaned) / 100.0))
            except ValueError:
                return max(-1.0, min(1.0, default))
        try:
            return max(-1.0, min(1.0, float(cleaned)))
        except ValueError:
            verdict = _normalize_news_verdict(cleaned)
            return _sentiment_from_news_verdict(verdict)
    return max(-1.0, min(1.0, default))


def _sentiment_from_news_verdict(verdict: str) -> float:
    if verdict == "positive":
        return 0.6
    if verdict == "negative":
        return -0.6
    return 0.0


def _news_verdict_from_sentiment(sentiment: float) -> str:
    if sentiment >= 0.15:
        return "positive"
    if sentiment <= -0.15:
        return "negative"
    return "neutral"


def parse_llm_news_review_output(content: str) -> tuple[str, float, float, str]:
    """Parse provider text into normalized news review tuple."""
    cleaned = _strip_code_fence(content.strip())
    verdict = "neutral"
    sentiment = 0.0
    confidence: float | None = None
    reason = ""

    payload = _extract_json_payload(cleaned)
    if payload is not None:
        verdict = _normalize_news_verdict(
            _as_non_empty_str(
                payload.get("verdict")
                or payload.get("label")
                or payload.get("decision")
            )
        )
        sentiment = _normalize_sentiment_value(
            payload.get("sentiment")
            or payload.get("score")
            or payload.get("polarity"),
            default=_sentiment_from_news_verdict(verdict),
        )
        confidence = _as_probability(
            payload.get("confidence")
            or payload.get("probability")
            or payload.get("conf")
        )
        reason = _as_non_empty_str(payload.get("reason") or payload.get("rationale")) or ""

    if payload is None:
        lowered = cleaned.lower()
        verdict = _normalize_news_verdict(lowered)
        sentiment = _normalize_sentiment_value(None, default=_sentiment_from_news_verdict(verdict))
        confidence = _extract_confidence(cleaned)
    if confidence is None:
        confidence = 0.65 if verdict != "neutral" else 0.55
    if not reason:
        reason = _truncate_text(cleaned, limit=120)
    verdict_sentiment = _sentiment_from_news_verdict(verdict)
    sign_mismatch = math.copysign(1.0, sentiment) != math.copysign(1.0, verdict_sentiment)
    if sign_mismatch and not math.isclose(sentiment, 0.0):
        verdict = _news_verdict_from_sentiment(sentiment)
    return verdict, sentiment, _clamp01(confidence), reason


def _extract_confidence(text: str) -> float | None:
    match = _CONFIDENCE_PATTERN.search(text)
    if match is None:
        return None
    try:
        parsed = float(match.group(1))
    except ValueError:
        return None
    if parsed > 1.0 and parsed <= 100.0:
        parsed = parsed / 100.0
    return _clamp01(parsed)


def _as_probability(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            cleaned = value.strip()
            is_percent = cleaned.endswith("%")
            if is_percent:
                cleaned = cleaned[:-1].strip()
            parsed = float(cleaned)
            if is_percent:
                parsed = parsed / 100.0
        except ValueError:
            return None
    else:
        return None
    if parsed > 1.0 and parsed <= 100.0:
        parsed = parsed / 100.0
    if parsed < 0.0 or parsed > 1.0:
        return None
    return parsed


def _as_non_empty_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _strip_code_fence(content: str) -> str:
    if content.startswith("```") and content.endswith("```"):
        lines = content.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return content


def _truncate_text(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip()


def _to_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _to_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
