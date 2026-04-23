from __future__ import annotations

from datetime import datetime

from stock_analyzer.evolution.online_samples import build_online_sample_audit


def test_online_sample_audit_is_deterministic_for_different_input_order() -> None:
    now = datetime.fromisoformat("2026-03-05T20:40:00")
    records_a = [
        {
            "symbol": "000001.SZ",
            "trade_date": "2026-03-03",
            "label_mature_time": "2026-03-04T15:00:00",
        },
        {
            "symbol": "600000.SH",
            "trade_date": "2026-03-02",
            "label_mature_time": "2026-03-03T15:00:00",
        },
    ]
    records_b = [records_a[1], records_a[0]]

    audit_a = build_online_sample_audit(records=records_a, now=now)
    audit_b = build_online_sample_audit(records=records_b, now=now)
    assert audit_a.online_samples_used == 2
    assert audit_b.online_samples_used == 2
    assert audit_a.online_samples_used_hash == audit_b.online_samples_used_hash
    assert audit_a.deterministic_order_applied is True


def test_online_sample_audit_skips_not_matured_samples() -> None:
    now = datetime.fromisoformat("2026-03-05T20:40:00")
    records = [
        {
            "symbol": "000001.SZ",
            "trade_date": "2026-03-04",
            "label_mature_time": "2026-03-06T15:00:00",
        },
        {
            "symbol": "600000.SH",
            "trade_date": "2026-03-03",
            "label_mature_time": "2026-03-05T15:00:00",
        },
    ]
    audit = build_online_sample_audit(records=records, now=now)
    assert audit.online_samples_used == 1
    assert audit.skipped_not_matured == 1
