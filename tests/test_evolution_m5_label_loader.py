from __future__ import annotations

from pathlib import Path

from stock_analyzer.evolution.modules.m5_label_loader import load_m5_label_records


def test_m5_label_loader_supports_jsonl_with_nested_payloads(tmp_path: Path) -> None:
    artifact = tmp_path / "m5_labels.jsonl"
    artifact.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","open":10.0,"close":10.2,"label":1}',
                '{"symbol":"000001.SZ","open":8.0,"close":7.8,'
                '"label_result":{"label_seed_1":0,"label_seed_2":1},"label":0}',
            ]
        ),
        encoding="utf-8",
    )
    records = load_m5_label_records(path=artifact)
    assert len(records) == 2
    assert records[0]["symbol"] == "600000.SH"
    assert records[1]["symbol"] == "000001.SZ"
    assert records[1]["label_seed_1"] == 0
    assert records[1]["label_seed_2"] == 1


def test_m5_label_loader_returns_empty_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "not_exists.json"
    records = load_m5_label_records(path=missing)
    assert records == []
