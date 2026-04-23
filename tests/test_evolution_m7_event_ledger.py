from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer.evolution.modules.m7_event_ledger import M7EventLedger


def test_m7_event_ledger_deduplicates_tracks_effectiveness_and_archives(tmp_path: Path) -> None:
    ledger = M7EventLedger(
        db_path=tmp_path / "artifacts" / "evolution" / "m7_event_ledger.duckdb",
        archive_dir=tmp_path / "artifacts" / "evolution" / "m7_event_ledger_archive",
        ttl_days=3,
    )
    first = ledger.record_run(
        records=[
            {
                "event_id": "evt-1",
                "symbol": "600000.SH",
                "headline": "Broker sector rallies on stronger turnover",
                "sentiment": 0.8,
                "source": "wire-a",
                "published_at": "2026-03-02T09:00:00+00:00",
            },
            {
                "event_id": "evt-2",
                "symbol": "600000.SH",
                "headline": "Broker sector rallies on stronger turnover",
                "sentiment": 0.6,
                "source": "wire-b",
                "published_at": "2026-03-02T09:05:00+00:00",
            },
            {
                "event_id": "evt-3",
                "symbol": "000001.SZ",
                "headline": "Property policy outlook cools",
                "sentiment": -0.7,
                "source": "wire-c",
                "published_at": "2026-03-02T09:10:00+00:00",
            },
        ],
        now=datetime(2026, 3, 2, 20, 0, tzinfo=UTC),
        price_by_symbol={"600000.SH": 10.0, "000001.SZ": 8.0},
        source_trace_id="m7-ledger-first",
        regime_state="range",
    )
    assert first.ingest.inserted == 2
    assert first.ingest.deduplicated == 1
    assert first.effectiveness.active_events == 2
    assert first.effectiveness.archived_events == 0

    second = ledger.record_run(
        records=[],
        now=datetime(2026, 3, 3, 21, 0, tzinfo=UTC),
        price_by_symbol={"600000.SH": 10.7, "000001.SZ": 7.2},
        source_trace_id="m7-ledger-second",
        regime_state="trend_up",
    )
    assert second.ingest.inserted == 0
    assert second.effectiveness.matured_1d == 2
    assert second.effectiveness.hit_rate_1d == 1.0
    assert second.effectiveness.average_effectiveness == 1.0
    assert second.effectiveness.source_reliability[0]["samples"] >= 1

    third = ledger.record_run(
        records=[],
        now=datetime(2026, 3, 6, 21, 0, tzinfo=UTC),
        price_by_symbol={"600000.SH": 10.9, "000001.SZ": 7.0},
        source_trace_id="m7-ledger-third",
        regime_state="trend_up",
    )
    assert third.ingest.archived == 2
    assert third.effectiveness.archived_events == 2
    assert third.effectiveness.matured_3d == 2
    assert third.effectiveness.hit_rate_3d == 1.0
    assert any(Path(item).exists() for item in third.archive_paths)

    fourth = ledger.record_run(
        records=[
            {
                "event_id": "evt-4",
                "symbol": "600000.SH",
                "headline": "Broker sector rallies on stronger turnover",
                "sentiment": 0.9,
                "source": "wire-a",
                "published_at": "2026-03-02T09:00:00+00:00",
            }
        ],
        now=datetime(2026, 3, 7, 9, 0, tzinfo=UTC),
        price_by_symbol={"600000.SH": 11.0},
        source_trace_id="m7-ledger-fourth",
        regime_state="trend_up",
    )
    assert fourth.ingest.inserted == 1
    active_rows = ledger.list_records(status="active")
    archived_rows = ledger.list_records(status="archived")
    assert len(active_rows) == 1
    assert len(archived_rows) == 2
