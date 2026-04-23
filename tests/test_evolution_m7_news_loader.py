from __future__ import annotations

from pathlib import Path

from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records


def test_m7_news_loader_supports_jsonl_with_nested_payload(tmp_path: Path) -> None:
    artifact = tmp_path / "m7_news.jsonl"
    artifact.write_text(
        "\n".join(
            [
                '{"event_id":"n1","symbol":"600000.SH","headline":"券商走强","sentiment":0.8}',
                '{"event_id":"n2","news":{"symbol":"000001.SZ","headline":"地产修复","sentiment":0.7}}',
            ]
        ),
        encoding="utf-8",
    )
    records = load_m7_news_records(path=artifact)
    assert len(records) == 2
    assert records[0]["event_id"] == "n1"
    assert records[1]["symbol"] == "000001.SZ"


def test_m7_news_loader_returns_empty_for_missing_file(tmp_path: Path) -> None:
    records = load_m7_news_records(path=tmp_path / "missing.json")
    assert records == []
