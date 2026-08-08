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


def test_snapshot_goes_stale_when_data_root_layout_changes(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path)
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    service._config.data_source.local_data_root = str(data_root)
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    assert snapshot_is_current(manifest, service._config) is True
    # A structural data-root change (new vendor archive) invalidates the
    # snapshot; a routine content update (mtime-only) must NOT.
    (data_root / "全A日K").mkdir()
    (data_root / "全A日K" / "2026.zip").write_bytes(b"x")
    assert snapshot_is_current(manifest, service._config) is False
    # Without force it must rebuild (not skip, not incremental).
    report = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert report["ok"] is True
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


class _DateTrackingProvider:
    """SyntheticProvider wrapper that tracks fetch calls and reports dates."""

    def __init__(
        self,
        inner: SyntheticProvider,
        latest_dates: dict[str, object],
        fetch_log: list[str],
    ) -> None:
        self._inner = inner
        self._latest_dates = latest_dates
        self._fetch_log = fetch_log

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120, **kwargs):
        self._fetch_log.append(symbol)
        return self._inner.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days, **kwargs)

    def latest_daily_dates(self, symbols=None):
        requested = set(symbols or [])
        return {
            symbol: value
            for symbol, value in self._latest_dates.items()
            if not requested or symbol in requested
        }

    def status(self) -> dict[str, object]:
        return {}


def _future_date(offset_days: int):
    from datetime import date, timedelta

    return date.today() + timedelta(days=offset_days)


def test_snapshot_incremental_refetches_only_dirty_symbols(tmp_path: Path) -> None:
    from datetime import date, timedelta

    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    fetch_log: list[str] = []
    tracked = _DateTrackingProvider(provider, latest_dates={}, fetch_log=fetch_log)
    first = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert first["ok"] is True
    assert set(fetch_log) == set(_SYMBOLS)  # full build fetches everything

    # Baseline dates come from the freshly built manifest (real bar dates).
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    baseline = {
        symbol: date.fromisoformat(manifest.per_symbol[symbol]["latest_date"])
        for symbol in _SYMBOLS
    }
    tracked._latest_dates = dict(baseline)

    # Advance only two symbols to a newer trade date.
    tracked._latest_dates["600519"] = baseline["600519"] + timedelta(days=1)
    tracked._latest_dates["000001"] = baseline["000001"] + timedelta(days=1)
    fetch_log.clear()
    second = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250
    )
    assert second["ok"] is True
    assert second["incremental"] is True
    assert second["dirty_symbols"] == 2
    assert sorted(fetch_log) == ["000001", "600519"]  # only dirty refetched

    manifest, frame = load_feature_snapshot(service._config)
    assert manifest is not None and frame is not None
    assert len(frame) == len(_SYMBOLS)
    # Dirty symbols' rows were replaced (their bars' real latest date is
    # recorded); the clean symbols' entries were untouched.
    real_latest = provider.fetch_daily_bars("600519", lookback_days=10).index[-1]
    assert manifest.per_symbol["600519"]["latest_date"] == str(real_latest.date())
    assert manifest.per_symbol["000001"]["fingerprint"] != ""
    assert "300750" in manifest.per_symbol  # clean symbol kept its entry
    assert (
        manifest.per_symbol["300750"]["latest_date"]
        == manifest.per_symbol["601318"]["latest_date"]
    )


def test_snapshot_incremental_matches_full_rebuild(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    fetch_log: list[str] = []
    tracked = _DateTrackingProvider(
        provider,
        latest_dates={symbol: _future_date(0) for symbol in _SYMBOLS},
        fetch_log=fetch_log,
    )
    build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    # Advance all symbols by one day, then incrementally refresh.
    tracked._latest_dates = {symbol: _future_date(1) for symbol in _SYMBOLS}
    inc = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250
    )
    assert inc["ok"] is True
    inc_manifest, inc_frame = load_feature_snapshot(service._config)
    assert inc_manifest is not None and inc_frame is not None
    assert inc_manifest.symbol_count == len(_SYMBOLS)
    # A forced full rebuild on the same provider data must be identical.
    full = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert full["ok"] is True
    full_manifest, full_frame = load_feature_snapshot(service._config)
    assert full_manifest is not None and full_frame is not None
    assert len(full_frame) == len(inc_frame)
    by_symbol_inc = {str(r["symbol"]): r for _, r in inc_frame.iterrows()}
    by_symbol_full = {str(r["symbol"]): r for _, r in full_frame.iterrows()}
    for symbol, inc_row in by_symbol_inc.items():
        full_row = by_symbol_full[symbol]
        assert float(inc_row["latest_close"]) == float(full_row["latest_close"])
        assert str(inc_row["trade_date"]) == str(full_row["trade_date"])
    assert inc_manifest.per_symbol == full_manifest.per_symbol


def test_snapshot_schema_change_triggers_rebuild(tmp_path: Path, monkeypatch) -> None:
    from stock_analyzer.feature.snapshot import FeatureEngineer

    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    assert snapshot_is_current(manifest, service._config) is True

    # A change to the feature column definitions must invalidate the snapshot.
    original_transform = FeatureEngineer.transform

    def patched_transform(self, bars, *args, **kwargs):
        frame = original_transform(self, bars, *args, **kwargs)
        frame["new_schema_column"] = 0.0
        return frame

    monkeypatch.setattr(FeatureEngineer, "transform", patched_transform)
    assert snapshot_is_current(manifest, service._config) is False
    rebuild = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert rebuild["ok"] is True
    assert rebuild["skipped"] is False  # full rebuild, not skipped/incremental


def test_snapshot_factor_archive_change_triggers_rebuild(
    tmp_path: Path, monkeypatch
) -> None:
    import zipfile

    service, _root = _make_service(tmp_path)
    vendor_root = tmp_path / "vendor"
    factors_dir = vendor_root / "复权因子"
    factors_dir.mkdir(parents=True)
    archive = factors_dir / "复权因子_前复权.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("2025/600519.csv", "code,datetime,adj_factor\n600519,20250101,1.0\n")
    service._config.data_source.vendor_zip_index_path = str(vendor_root / "index.json")
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    assert manifest.factor_archive_hash != ""
    assert snapshot_is_current(manifest, service._config) is True

    # Rewrite the factor archive -> factor-version drift -> snapshot stale.
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "2025/600519.csv", "code,datetime,adj_factor\n600519,20250101,1.0000001\n"
        )
    assert manifest.factor_archive_hash != ""
    assert snapshot_is_current(manifest, service._config) is False
    rebuild = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert rebuild["ok"] is True
    assert rebuild["skipped"] is False


def test_snapshot_missing_blocks_scheduler_scan(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path)  # require_current=True, no snapshot yet
    assert not (root / "current.json").exists()
    service._config.week5.universe_quality_selector_enabled = False
    service._resolve_symbol_universe = lambda **kw: {  # noqa: SLF001
        "symbols": ["600000", "000001"],
        "source": "test",
    }
    report = service.run_week5_scan(
        symbols=None,
        sync_reason="scheduler_week5_nightly",
        force_universe_scan=True,
        notify_enabled=False,
    )
    assert str(report.get("status", "")) == "blocked_data_gate"
    gate = report.get("data_gate") or {}
    assert str(gate.get("status", "")) == "blocked"
    assert any(
        "feature_snapshot_stale" in str(reason) for reason in gate.get("reasons") or []
    )
    # The scan must not have fallen into the heavy prefilter path.
    assert (report.get("prefilter") or {}).get("shortlisted_count", 0) == 0
