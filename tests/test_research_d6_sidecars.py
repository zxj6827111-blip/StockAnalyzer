from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from stock_analyzer.research.finrl_sidecar import (
    persist_finrl_sidecar_report,
    run_finrl_sidecar,
)
from stock_analyzer.research.heavy_ts_shadow import (
    persist_heavy_ts_shadow_report,
    run_heavy_ts_shadow,
)
from stock_analyzer.research.tabular_deep_shadow import (
    persist_tabular_deep_shadow_report,
    run_tabular_deep_shadow,
)
from stock_analyzer.research.tft_sidecar import persist_tft_sidecar_report, run_tft_sidecar


def test_tabular_deep_shadow_runs_two_family_sidecars() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": f"2026-03-{day:02d}",
                "symbol": f"S{day:02d}",
                "momentum": 0.80 if day % 2 == 0 else 0.25,
                "quality": 0.75 if day % 2 == 0 else 0.30,
                "risk": 0.20 if day % 2 == 0 else 0.70,
                "turnover": 0.65 if day % 3 == 0 else 0.40,
                "label": 1 if day % 2 == 0 else 0,
                "p_meta": 0.58 if day % 2 == 0 else 0.42,
            }
            for day in range(1, 15)
        ]
    )

    report = run_tabular_deep_shadow(reference_frame=frame, test_ratio=0.25)

    assert report["status"] == "ok"
    assert report["affects_main_model"] is False
    assert len(report["families"]) == 2
    assert report["recommended_family"] in {"tabnet", "ft_transformer"}


def test_tft_sidecar_forecasts_returns_and_persists(tmp_path: Path) -> None:
    records = []
    for day in range(1, 15):
        records.append(
            {
                "trade_date": f"2026-03-{day:02d}",
                "symbol": "600000.SH",
                "close": 10.0 + day * 0.1,
                "volume": 1_000_000 + day * 10_000,
            }
        )
        records.append(
            {
                "trade_date": f"2026-03-{day:02d}",
                "symbol": "000001.SZ",
                "close": 8.0 + day * 0.06,
                "volume": 900_000 + day * 8_000,
            }
        )

    report = run_tft_sidecar(records=records, horizon=1, encoder_length=4)
    path = tmp_path / "research" / "tft_report.json"
    written = persist_tft_sidecar_report(report=report, output_path=path)

    assert report["status"] == "ok"
    assert report["sample_count"] > 0
    assert "rmse" in report["metrics"]
    assert Path(written).exists() is True


def test_finrl_sidecar_runs_policy_fallback_and_persists(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": f"2026-03-{day:02d}",
                "symbol": f"S{day:02d}",
                "momentum": 0.75 if day % 2 == 0 else 0.25,
                "quality": 0.80 if day % 2 == 0 else 0.35,
                "risk": 0.15 if day % 2 == 0 else 0.72,
                "realized_return": 0.07 if day % 2 == 0 else -0.04,
                "p_meta": 0.60 if day % 2 == 0 else 0.40,
            }
            for day in range(1, 15)
        ]
    )

    report = run_finrl_sidecar(reference_frame=frame, test_ratio=0.25)
    path = tmp_path / "research" / "finrl_report.json"
    written = persist_finrl_sidecar_report(report=report, output_path=path)
    payload = json.loads(Path(written).read_text(encoding="utf-8"))

    assert report["status"] == "ok"
    assert report["affects_runtime"] is False
    assert "mean_reward" in report["policy_metrics"]
    assert payload["backend"] == "policy_fallback_logit"


def test_heavy_ts_shadow_runs_sequence_proxy_and_persists(tmp_path: Path) -> None:
    records = []
    for day in range(1, 18):
        records.append(
            {
                "trade_date": f"2026-03-{day:02d}",
                "symbol": "600000.SH",
                "close": 10.0 + day * 0.12,
                "volume": 1_000_000 + day * 9_000,
                "p_meta": 0.58 if day % 2 == 0 else 0.42,
            }
        )
        records.append(
            {
                "trade_date": f"2026-03-{day:02d}",
                "symbol": "000001.SZ",
                "close": 8.0 + day * 0.05,
                "volume": 850_000 + day * 7_000,
                "p_meta": 0.56 if day % 2 == 0 else 0.44,
            }
        )

    report = run_heavy_ts_shadow(records=records, horizon=2, lookback=5, test_ratio=0.25)
    path = tmp_path / "research" / "heavy_ts_report.json"
    written = persist_heavy_ts_shadow_report(report=report, output_path=path)

    assert report["status"] == "ok"
    assert report["affects_runtime"] is False
    assert report["sample_count"] > 0
    assert Path(written).exists() is True
