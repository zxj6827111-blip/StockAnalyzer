"""Offline FinBERT-style news sentiment sidecar."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

_POSITIVE_TOKENS = [
    "利好",
    "增长",
    "中标",
    "签约",
    "回购",
    "增持",
    "扭亏",
    "预增",
    "上调",
    "突破",
    "大涨",
    "盈利",
    "获批",
    "positive",
    "upgrade",
    "beat",
    "surge",
    "strong",
]
_NEGATIVE_TOKENS = [
    "利空",
    "下滑",
    "减持",
    "亏损",
    "预亏",
    "处罚",
    "问询",
    "暴跌",
    "下调",
    "终止",
    "违约",
    "诉讼",
    "冻结",
    "negative",
    "downgrade",
    "miss",
    "plunge",
    "weak",
]


def run_finbert_sidecar(
    *,
    records: Sequence[Mapping[str, object]],
    model_path: str | Path = "",
    include_neutral: bool = True,
) -> dict[str, object]:
    normalized = [_normalize_record(record) for record in records]
    usable = [item for item in normalized if item["text"]]
    if not usable:
        return {
            "status": "no_data",
            "engine": "finbert_sidecar",
            "backend": "lexicon_sentiment_fallback",
            "records": 0,
            "symbol_coverage": 0,
            "mean_sentiment": 0.0,
            "positive_ratio": 0.0,
            "negative_ratio": 0.0,
            "items": [],
            "symbols": [],
        }

    backend = "lexicon_sentiment_fallback"
    scores: list[dict[str, object]] = []
    finbert_scorer = _load_finbert_scorer(model_path=model_path)
    if finbert_scorer is not None:
        backend = "finbert_local"

    for item in usable:
        if finbert_scorer is not None:
            sentiment, confidence = finbert_scorer(item["text"])
        else:
            sentiment, confidence = _estimate_sentiment_heuristic(text=item["text"])
        label = _sentiment_label(sentiment=sentiment, include_neutral=include_neutral)
        scores.append(
            {
                "symbol": item["symbol"],
                "headline": item["headline"],
                "sentiment": round(sentiment, 6),
                "confidence": round(confidence, 6),
                "label": label,
                "source": item["source"],
            }
        )

    filtered = [item for item in scores if include_neutral or str(item["label"]) != "neutral"]
    symbol_summary = _build_symbol_summary(filtered if filtered else scores)
    sentiments = [_coerce_float(item.get("sentiment")) for item in scores]
    positive_count = sum(1 for value in sentiments if value >= 0.20)
    negative_count = sum(1 for value in sentiments if value <= -0.20)
    denominator = max(1, len(sentiments))
    return {
        "status": "ok",
        "engine": "finbert_sidecar",
        "backend": backend,
        "records": len(scores),
        "symbol_coverage": len(symbol_summary),
        "mean_sentiment": round(sum(sentiments) / denominator, 6),
        "positive_ratio": round(positive_count / denominator, 6),
        "negative_ratio": round(negative_count / denominator, 6),
        "items": filtered if filtered else scores,
        "symbols": symbol_summary,
    }


def persist_finbert_sidecar_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _normalize_record(record: Mapping[str, object]) -> dict[str, str]:
    symbol = _first_text(record, ("symbol", "code", "ticker", "stock_code", "ts_code"))
    headline = _first_text(record, ("headline", "title"))
    content = _first_text(record, ("content", "summary", "body"))
    text = " ".join(part for part in (headline, content) if part).strip()
    source = _first_text(record, ("source", "channel", "publisher"))
    return {
        "symbol": symbol,
        "headline": headline,
        "text": text,
        "source": source,
    }


def _first_text(record: Mapping[str, object], keys: Sequence[str]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _load_finbert_scorer(model_path: str | Path) -> Callable[[str], tuple[float, float]] | None:
    raw_path = str(model_path).strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.exists():
        return None
    try:
        from transformers import pipeline  # type: ignore[import-not-found]

        classifier = pipeline(
            task="text-classification",
            model=str(path),
            tokenizer=str(path),
            truncation=True,
        )
    except Exception:
        return None

    def _score(text: str) -> tuple[float, float]:
        result = classifier(text[:512])[0]
        label = str(result.get("label", "")).strip().lower()
        score = float(result.get("score", 0.5))
        if "positive" in label:
            return min(1.0, score), max(0.5, score)
        if "negative" in label:
            return max(-1.0, -score), max(0.5, score)
        return 0.0, max(0.35, score)

    return _score


def _estimate_sentiment_heuristic(*, text: str) -> tuple[float, float]:
    lowered = text.lower()
    positive_hits = sum(1 for token in _POSITIVE_TOKENS if token in lowered)
    negative_hits = sum(1 for token in _NEGATIVE_TOKENS if token in lowered)
    if positive_hits == negative_hits == 0:
        return 0.0, 0.45
    raw_score = (positive_hits - negative_hits) / max(1, positive_hits + negative_hits)
    confidence = min(0.90, 0.55 + 0.10 * (positive_hits + negative_hits))
    return max(-1.0, min(1.0, raw_score * 0.8)), confidence


def _sentiment_label(*, sentiment: float, include_neutral: bool) -> str:
    if sentiment >= 0.20:
        return "positive"
    if sentiment <= -0.20:
        return "negative"
    return "neutral" if include_neutral else "filtered"


def _build_symbol_summary(items: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for item in items:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(item)
    summary: list[dict[str, object]] = []
    for symbol, rows in sorted(grouped.items()):
        sentiments = [_coerce_float(row.get("sentiment")) for row in rows]
        confidences = [_coerce_float(row.get("confidence")) for row in rows]
        mean_sentiment = sum(sentiments) / max(1, len(sentiments))
        summary.append(
            {
                "symbol": symbol,
                "events": len(rows),
                "mean_sentiment": round(mean_sentiment, 6),
                "mean_confidence": round(sum(confidences) / max(1, len(confidences)), 6),
                "label": _sentiment_label(sentiment=mean_sentiment, include_neutral=True),
            }
        )
    return summary


def _coerce_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default
