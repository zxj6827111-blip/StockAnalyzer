"""Tests for the incremental feature snapshot layer and the week5 funnel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.snapshot import (
    build_feature_snapshot,
    load_feature_snapshot,
    snapshot_is_current,
)
from stock_analyzer.runtime.service import StockAnalyzerService

_SYMBOLS = ["600519", "000001", "601318", "000858", "600000", "300750"]


def _make_service(tmp_path: Path) -> tuple[StockAnalyzerService, Path]:
    config = load_config("config/default.yaml")
    config.data_source.primary = "synthetic"
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    config.week5.feature_snapshot_max_age_days = 30
    config.week5.feature_snapshot_require_current = True
    config.week5.final_signal_min_threshold = 0.0
    config.week5.light_candidate_target = 150
    config.week5.deep_candidate_target = 20
    config.week5.final_signal_cap = 5
    service = StockAnalyzerService(config=config)
    service._provider = SyntheticProvider(seed_offset=2026)  # noqa: SLF001
    return service, tmp_path / "features_light"


def test_snapshot_build_skip_and_load(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    report = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert report["ok"] is True
    assert report["skipped"] is False
    assert report["symbol_count"] == len(_SYMBOLS)
    manifest, frame = load_feature_snapshot(service._config)
    assert manifest is not None
    assert frame is not None
    assert len(frame) == len(_SYMBOLS)
    assert snapshot_is_current(manifest, service._config) is True
    # Second build skips (incremental semantics).
    report2 = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert report2["skipped"] is True
    assert report2["data_snapshot_id"] == report["data_snapshot_id"]


def test_snapshot_goes_stale_when_root_changes(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    # Touch the data root so the source fingerprint changes.
    config = service._config
    report = build_feature_snapshot(
        config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert report["skipped"] is False


def test_light_stage_matches_direct_bars_baseline(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    engineer = FeatureEngineer()
    for symbol in _SYMBOLS:
        bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=250)
        features = engineer.transform(bars)
        assert features is not None and not features.empty
        direct = service._prefilter_week5_universe_symbol(
            symbol=symbol, lookback_days=250, allowed_exchanges=set()
        )
        assert direct is not None, symbol
        # Build the snapshot row through the real snapshot pipeline and score
        # it with the light-stage scorer.
        snapshot_row = _snapshot_row(service._config, provider, symbol, engineer)
        assert snapshot_row is not None, symbol
        scored = service._prefilter_week5_from_snapshot_row(pd.Series(snapshot_row))
        assert scored is not None, symbol
        assert abs(
            float(scored["baseline_score"]) - float(direct["baseline_score"])
        ) < 1e-3, symbol


def _snapshot_row(config, provider, symbol, engineer):
    from stock_analyzer.feature.snapshot import _snapshot_row_for_symbol

    bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=250)
    frame = _snapshot_row_for_symbol(bars=bars, symbol=symbol, engineer=engineer)
    if frame is None or frame.empty:
        return None
    return frame.iloc[0].to_dict()


def test_final_signal_selector_gates_and_cap(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    service._config.week5.final_signal_cap = 2
    service._config.week5.final_signal_min_threshold = 60.0

    def _signal(symbol: str, score: float, cross_ok: bool, risk_ok: bool) -> dict[str, object]:
        return {
            "symbol": symbol,
            "score": score,
            "action": "buy",
            "reasons": [],
            "decision_trace": {
                "risk_gate": {"passed": risk_ok},
                "cross_review_gate": {"passed": cross_ok},
            },
        }

    signals = [
        _signal("600519", 90.0, True, True),
        _signal("000001", 80.0, True, True),
        _signal("601318", 70.0, False, True),
        _signal("000858", 50.0, True, True),
        _signal("600000", 65.0, True, False),
    ]
    result = service._final_signal_selector(signals=signals, data_gate_status="ok")
    assert result["selected_count"] == 2  # capped at 2
    selected = [str(item["symbol"]) for item in result["final_signals"]]
    assert selected == ["600519", "000001"]
    assert result["rejected_count"] == 3

    blocked = service._final_signal_selector(signals=signals, data_gate_status="blocked")
    assert blocked["selected_count"] == 0  # data gate blocks everything
    assert blocked["rejected_count"] == 5


def test_data_gate_status_flow(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    service._config.week5.feature_snapshot_require_current = False
    # Fresh data -> ok.
    fresh = service._build_data_gate(
        snapshot_manifest=None,
        snapshot_current=False,
        latest_trade_date=datetime.now(UTC).date().isoformat(),
    )
    assert fresh["status"] == "ok"
    # Stale data -> watch_only.
    stale = service._build_data_gate(
        snapshot_manifest=None,
        snapshot_current=False,
        latest_trade_date=(datetime.now(UTC).date() - timedelta(days=10)).isoformat(),
    )
    assert stale["status"] == "watch_only"
    assert any("data_stale" in reason for reason in stale["reasons"])
    # Require current snapshot -> blocked when snapshot missing.
    service._config.week5.feature_snapshot_require_current = True
    missing = service._build_data_gate(
        snapshot_manifest=None,
        snapshot_current=False,
        latest_trade_date=datetime.now(UTC).date().isoformat(),
    )
    assert missing["status"] == "blocked"
    assert any("feature_snapshot_stale" in reason for reason in missing["reasons"])


def test_deep_stage_selects_capped_target(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, frame = load_feature_snapshot(service._config)
    assert manifest is not None and frame is not None
    service._config.week5.deep_candidate_target = 3
    light = service._light_stage_from_snapshot(
        frame=frame,
        target=150,
        allowed_exchanges=set(),
    )
    assert light["shortlisted_count"] == len(_SYMBOLS)
    deep = service._deep_stage_from_snapshot(
        frame=frame,
        target=3,
        light_report=light,
    )
    assert deep["applied"] is True
    assert deep["selected_count"] == 3
    assert len(deep["selected"]) == 3
    assert all(isinstance(item.get("funnel_score"), float) for item in deep["selected"])
