from __future__ import annotations

import json
from pathlib import Path

from stock_analyzer.research.qlib_bridge import export_qlib_bridge_bundle, run_qlib_bridge


def test_qlib_bridge_builds_offline_research_manifest() -> None:
    records = [
        {
            "trade_date": f"2026-03-{day:02d}",
            "symbol": "600000.SH" if day <= 4 else "000001.SZ",
            "open": 10.0 + day * 0.1,
            "high": 10.2 + day * 0.1,
            "low": 9.9 + day * 0.1,
            "close": 10.1 + day * 0.1,
            "volume": 1_000_000 + day * 10_000,
            "amount": 12_000_000 + day * 20_000,
            "momentum_5": 0.2 * day,
            "quality_score": 0.9 - day * 0.03,
            "volatility_10": 0.1 + day * 0.01,
            "turnover_ratio": 0.2 + day * 0.02,
            "label": 1 if day % 2 == 0 else 0,
        }
        for day in range(1, 9)
    ]

    report = run_qlib_bridge(records=records)

    assert report["status"] == "ok"
    assert report["affects_runtime"] is False
    assert report["instrument_count"] == 2
    feature_count = report["feature_count"]
    factor_packs = report["factor_packs"]
    assert isinstance(feature_count, int)
    assert feature_count >= 4
    assert isinstance(factor_packs, dict)
    assert factor_packs.get("alpha158_ready") is True


def test_qlib_bridge_exports_bundle_files(tmp_path: Path) -> None:
    records = [
        {
            "trade_date": "2026-03-01",
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 1_000_000,
            "amount": 12_000_000,
            "momentum_5": 0.4,
            "label": 1,
        },
        {
            "trade_date": "2026-03-02",
            "symbol": "600000.SH",
            "open": 10.1,
            "high": 10.3,
            "low": 10.0,
            "close": 10.2,
            "volume": 1_100_000,
            "amount": 12_300_000,
            "momentum_5": 0.5,
            "label": 0,
        },
        {
            "trade_date": "2026-03-03",
            "symbol": "000001.SZ",
            "open": 8.0,
            "high": 8.1,
            "low": 7.9,
            "close": 8.05,
            "volume": 900_000,
            "amount": 7_200_000,
            "momentum_5": 0.3,
            "label": 1,
        },
    ]

    written = export_qlib_bridge_bundle(records=records, output_dir=tmp_path / "qlib_bridge")

    manifest = Path(written["manifest_path"])
    dataset = Path(written["dataset_path"])
    assert manifest.exists() is True
    assert dataset.exists() is True
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["engine"] == "qlib_bridge"
