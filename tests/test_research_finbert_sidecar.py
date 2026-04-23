from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from stock_analyzer.research.finbert_sidecar import (
    persist_finbert_sidecar_report,
    run_finbert_sidecar,
)


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def test_finbert_sidecar_scores_news_with_fallback_heuristics() -> None:
    records = [
        {
            "symbol": "600000.SH",
            "headline": "Brokerage revenue growth with buyback plan",
            "summary": "Strong earnings beat and positive outlook.",
            "source": "wire",
        },
        {
            "symbol": "000001.SZ",
            "headline": "Project terminated after downgrade notice",
            "summary": "Weak demand and negative earnings miss.",
            "source": "wire",
        },
        {
            "symbol": "600000.SH",
            "headline": "Operations remain stable",
            "summary": "No strong directional signal.",
            "source": "wire",
        },
    ]

    report = run_finbert_sidecar(records=records)

    assert report["status"] == "ok"
    assert report["backend"] in {"finbert_local", "lexicon_sentiment_fallback"}
    symbols = {
        str(item.get("symbol", "")): item
        for item in cast(Sequence[Mapping[str, object]], report["symbols"])
    }
    assert _as_float(symbols["600000.SH"].get("mean_sentiment", 0.0)) > 0.0
    assert _as_float(symbols["000001.SZ"].get("mean_sentiment", 0.0)) < 0.0
    assert len(cast(Sequence[object], report["items"])) == 3


def test_finbert_sidecar_persists_report(tmp_path: Path) -> None:
    report = {
        "status": "ok",
        "engine": "finbert_sidecar",
        "backend": "lexicon_sentiment_fallback",
        "records": 2,
    }
    output_path = tmp_path / "research" / "finbert_report.json"
    written = persist_finbert_sidecar_report(report=report, output_path=output_path)

    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["engine"] == "finbert_sidecar"
    assert payload["records"] == 2
