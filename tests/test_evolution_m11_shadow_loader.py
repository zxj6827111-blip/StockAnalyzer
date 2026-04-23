from __future__ import annotations

from pathlib import Path

from stock_analyzer.evolution.modules.m11_shadow_loader import (
    load_m11_shadow_records,
    load_m11_shadow_observations,
    parse_m11_shadow_records,
)


def test_m11_shadow_loader_supports_jsonl_with_nested_payloads(tmp_path: Path) -> None:
    artifact = tmp_path / "shadow_results.jsonl"
    artifact.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","champion_shadow_return":0.01,'
                '"challenger_shadow_return":0.015,"champion_signal":1,"challenger_signal":1}',
                '{"symbol":"000001.SZ","shadow_result":{"champion_return":-0.02,'
                '"challenger_return":-0.01,"champion_signal":0,"challenger_signal":1}}',
            ]
        ),
        encoding="utf-8",
    )

    observations = load_m11_shadow_observations(path=artifact)
    assert len(observations) == 2
    assert observations[0].symbol == "600000.SH"
    assert observations[1].symbol == "000001.SZ"
    assert observations[1].champion_shadow_return == -0.02
    assert observations[1].challenger_shadow_return == -0.01
    assert observations[1].champion_signal == 0
    assert observations[1].challenger_signal == 1


def test_m11_shadow_loader_skips_invalid_records() -> None:
    observations = parse_m11_shadow_records(
        records=[
            {
                "symbol": "600000.SH",
                "champion_shadow_return": 0.01,
                "challenger_shadow_return": 0.02,
            },
            {"symbol": "000001.SZ", "champion_shadow_return": 0.01},
            {"symbol": "300001.SZ", "challenger_shadow_return": 0.02},
        ]
    )
    assert len(observations) == 1
    assert observations[0].symbol == "600000.SH"


def test_m11_shadow_loader_preserves_intraday_context_in_flattened_records(tmp_path: Path) -> None:
    artifact = tmp_path / "shadow_results_intraday.jsonl"
    artifact.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","champion_shadow_return":0.01,'
                '"challenger_shadow_return":0.015,'
                '"intraday_1m_latest_date":"2026-03-02",'
                '"intraday_5m_close_vwap_stability":0.82}',
                '{"symbol":"000001.SZ","shadow_result":{"champion_return":-0.02,'
                '"challenger_return":-0.01,'
                '"intraday_1m_latest_date":"2026-03-01",'
                '"intraday_5m_intraday_pullback_ratio":0.21}}',
            ]
        ),
        encoding="utf-8",
    )

    records = load_m11_shadow_records(path=artifact)
    assert len(records) == 2
    assert records[0]["intraday_1m_latest_date"] == "2026-03-02"
    assert records[0]["intraday_5m_close_vwap_stability"] == 0.82
    assert records[1]["intraday_1m_latest_date"] == "2026-03-01"
    assert records[1]["intraday_5m_intraday_pullback_ratio"] == 0.21
