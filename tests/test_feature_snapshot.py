"""Tests for the incremental feature snapshot layer and the week5 funnel."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.snapshot import (
    build_feature_snapshot,
    load_feature_snapshot,
    load_snapshot_tails,
    snapshot_is_current,
)
from stock_analyzer.runtime.service import StockAnalyzerService

_SYMBOLS = ["600519", "000001", "601318", "000858", "600000", "300750"]


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


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


def test_snapshot_publish_retries_transient_windows_permission_error(
    tmp_path: Path, monkeypatch
) -> None:
    """目录发布遇到瞬时 PermissionError 时有界重试，最终仍原子落盘。"""
    import stock_analyzer.feature.snapshot as snapshot_module

    root = tmp_path / "features_light"
    original_replace = Path.replace
    attempts = 0

    def _flaky_replace(self: Path, target: Path) -> Path:
        nonlocal attempts
        if self.name.endswith(".staging"):
            attempts += 1
            if attempts < 3:
                raise PermissionError("transient-directory-lock")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace)
    monkeypatch.setattr(snapshot_module, "sleep", lambda _seconds: None)
    snapshot_module._publish_snapshot_dir(
        root=root,
        snapshot_id="snap_retry_test",
        frame=pd.DataFrame([{"symbol": "600519", "score": 1.0}]),
    )

    assert attempts == 3
    assert (root / "snap_retry_test" / "market_features.parquet").exists()
    assert not (root / "snap_retry_test.staging").exists()


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


def test_full_snapshot_transform_failure_is_not_current(tmp_path: Path, monkeypatch) -> None:
    service, root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)

    import stock_analyzer.feature.snapshot as snapshot_module

    original_worker = snapshot_module._transform_row_worker

    def _fail_one(payload):
        if payload[0] == "000001":
            return payload[0], None
        return original_worker(payload)

    monkeypatch.setattr(snapshot_module, "_transform_row_worker", _fail_one)
    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=["600519", "000001", "601318"],
        lookback_days=250,
        force=True,
        max_workers=1,
    )

    manifest, frame = load_feature_snapshot(service._config)
    assert report["ok"] is False
    assert report["failed_symbols"] == 1
    assert report["failed_symbols_list"] == ["000001"]
    assert manifest is not None and frame is not None
    assert manifest.symbol_count == 2
    assert manifest.failed_symbols == 1
    assert manifest.failed_symbols_list == ["000001"]
    assert snapshot_is_current(manifest, service._config) is False
    assert (root / "current.json").exists()


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
    """SyntheticProvider wrapper that tracks fetch calls and reports dates.

    The underlying synthetic sequence is cached per symbol so probes (small
    lookback) observe the SAME bars as the full-window build — mirroring real
    vendor data where ``fetch(250)`` and ``fetch(5)`` agree on the tail.
    """

    def __init__(
        self,
        inner: SyntheticProvider,
        latest_dates: dict[str, object],
        fetch_log: list[tuple[str, int]],
    ) -> None:
        self._inner = inner
        self._latest_dates = latest_dates
        self._fetch_log = fetch_log
        self._cache: dict[str, pd.DataFrame] = {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120, **kwargs):
        self._fetch_log.append((symbol, int(lookback_days)))
        if symbol not in self._cache:
            self._cache[symbol] = self._inner.fetch_daily_bars(
                symbol=symbol, lookback_days=250, **kwargs
            )
        return self._cache[symbol].tail(max(1, int(lookback_days)))

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
    fetch_log: list[tuple[str, int]] = []
    tracked = _DateTrackingProvider(provider, latest_dates={}, fetch_log=fetch_log)
    first = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert first["ok"] is True
    # Full build fetches every symbol (plus one probe for the trade date).
    assert set(symbol for symbol, _ in fetch_log) == set(_SYMBOLS)

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
    # Rolling increment: NO full-window (250-day) refetch happens at all — the
    # refresh is served from the stored tail + a short probe, and only the
    # dirty symbols are re-engineered.
    full_window_fetches = [symbol for symbol, lookback in fetch_log if lookback >= 240]
    assert full_window_fetches == []
    # Every symbol is probed lightly (revision detection), dirty ones rebuilt.
    probed = {symbol for symbol, lookback in fetch_log if lookback < 240}
    assert {"600519", "000001"} <= probed

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


def test_snapshot_factor_rebuild_same_content_stays_current(tmp_path: Path) -> None:
    """Nightly factor-ZIP rebuilds with UNCHANGED content must not invalidate
    the snapshot (the hash is content-based, not mtime-based)."""
    import os
    import zipfile

    service, _root = _make_service(tmp_path)
    vendor_root = tmp_path / "vendor"
    factors_dir = vendor_root / "复权因子"
    factors_dir.mkdir(parents=True)
    archive = factors_dir / "复权因子_前复权.zip"
    content = "code,datetime,adj_factor\n600519,20250101,1.0\n"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("2025/600519.csv", content)
    service._config.data_source.vendor_zip_index_path = str(vendor_root / "index.json")
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    assert snapshot_is_current(manifest, service._config) is True

    # Simulate the nightly rebuild with identical content (mtime changes only).
    os.utime(archive, None)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("2025/600519.csv", content)
    assert snapshot_is_current(manifest, service._config) is True


def test_snapshot_incremental_preserves_clean_tails_and_fingerprints(
    tmp_path: Path,
) -> None:
    """Clean symbols keep their tail window AND their stored fingerprint
    across an incremental refresh, so later revisions/refreshes do not fall
    back to full-window reads or miss same-day revisions."""
    from datetime import date, timedelta

    from stock_analyzer.feature.snapshot import (
        load_snapshot_tails,
    )

    service, root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    fetch_log: list[tuple[str, int]] = []
    tracked = _DateTrackingProvider(provider, latest_dates={}, fetch_log=fetch_log)
    build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    baseline = {
        symbol: date.fromisoformat(manifest.per_symbol[symbol]["latest_date"])
        for symbol in _SYMBOLS
    }
    tracked._latest_dates = dict(baseline)
    tracked._latest_dates["600519"] = baseline["600519"] + timedelta(days=1)
    clean_fp_before = manifest.per_symbol["300750"]["fingerprint"]

    report = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250
    )
    assert report["incremental"] is True
    assert report["dirty_symbols"] == 1

    manifest2, _ = load_feature_snapshot(service._config)
    assert manifest2 is not None
    # Clean symbol's fingerprint is preserved (not blanked out).
    assert manifest2.per_symbol["300750"]["fingerprint"] == clean_fp_before
    # The new snapshot inherits ALL old tails (clean symbols included).
    tails = load_snapshot_tails(root, manifest2.data_snapshot_id)
    assert tails is not None
    assert {"300750", "601318", "000858", "600000"} <= set(tails["symbol"])


def test_snapshot_missing_blocks_scheduler_scan(tmp_path: Path) -> None:
    """Week5 性能改造后，universe 扫描会自动确保快照；只有快照构建失败时
    scheduler 才 fail-closed（不进入重型 fallback）。"""
    service, root = _make_service(tmp_path)  # require_current=True, no snapshot yet
    assert not (root / "current.json").exists()
    service._config.week5.universe_quality_selector_enabled = False
    service._resolve_symbol_universe = lambda **kw: {  # noqa: SLF001
        "symbols": ["600000", "000001"],
        "source": "test",
    }
    # 模拟快照构建失败：scheduler 路径必须 fail-closed。
    service.ensure_week5_feature_snapshot = lambda **kw: {  # noqa: SLF001
        "ok": False,
        "skipped": False,
        "build": {"ok": False},
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


def test_snapshot_partial_refresh_failure_is_not_current(tmp_path: Path) -> None:
    from datetime import date, timedelta

    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    fetch_log: list[str] = []
    tracked = _DateTrackingProvider(provider, latest_dates={}, fetch_log=fetch_log)
    first = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert first["ok"] is True
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    baseline = {
        symbol: date.fromisoformat(manifest.per_symbol[symbol]["latest_date"])
        for symbol in _SYMBOLS
    }
    tracked._latest_dates = dict(baseline)
    tracked._latest_dates["600519"] = baseline["600519"] + timedelta(days=1)
    tracked._latest_dates["000001"] = baseline["000001"] + timedelta(days=1)

    original_fetch = tracked.fetch_daily_bars

    def flaky_fetch(symbol: str, lookback_days: int = 120, **kwargs):
        if symbol == "000001":
            raise RuntimeError("source_down_for_symbol")
        return original_fetch(symbol=symbol, lookback_days=lookback_days, **kwargs)

    tracked.fetch_daily_bars = flaky_fetch  # type: ignore[method-assign]
    refresh = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250
    )
    assert refresh["ok"] is True
    assert refresh["failed_symbols"] == 1
    assert refresh["coverage_ratio"] == 0.5  # 1 of 2 dirty refreshed
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    # A partial-failure refresh is NOT fully current.
    assert snapshot_is_current(manifest, service._config) is False
    assert "000001" in manifest.failed_symbols_list
    # The market date advances to the freshest successfully refreshed row
    # (the real bar date of the refreshed symbol, not the old manifest date).
    assert manifest.trade_date == str(
        original_fetch(symbol="600519", lookback_days=10).index[-1].date()
    )

    # Source recovers -> the failed symbol is retried and the snapshot heals.
    tracked.fetch_daily_bars = original_fetch  # type: ignore[method-assign]
    healed = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250
    )
    assert healed["ok"] is True
    manifest2, _ = load_feature_snapshot(service._config)
    assert manifest2 is not None
    assert manifest2.failed_symbols == 0
    assert snapshot_is_current(manifest2, service._config) is True


def test_snapshot_same_day_revision_detected(tmp_path: Path) -> None:
    """A same-day content revision must be detected and rebuilt, even though
    the provider trade date does not advance."""
    from datetime import date

    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    fetch_log: list[tuple[str, int]] = []
    tracked = _DateTrackingProvider(provider, latest_dates={}, fetch_log=fetch_log)
    build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    baseline = {
        symbol: date.fromisoformat(manifest.per_symbol[symbol]["latest_date"])
        for symbol in _SYMBOLS
    }
    tracked._latest_dates = dict(baseline)  # dates unchanged

    # Same-day revision: mutate the latest bar's close in the cached series.
    frame = tracked._cache["600519"]
    frame.loc[frame.index[-1], "close"] *= 1.05
    stored_fp = manifest.per_symbol["600519"]["fingerprint"]

    report = build_feature_snapshot(
        service._config, tracked, symbols=_SYMBOLS, lookback_days=250
    )
    assert report["ok"] is True
    assert report["incremental"] is True
    assert report["dirty_symbols"] == 1  # only the revised symbol
    assert report["refreshed_count"] == 1
    assert report["failed_symbols"] == 0

    manifest2, frame2 = load_feature_snapshot(service._config)
    assert manifest2 is not None and frame2 is not None
    revised_fp = manifest2.per_symbol["600519"]["fingerprint"]
    assert revised_fp != "" and revised_fp != stored_fp
    assert manifest2.per_symbol["600519"]["latest_date"] == baseline["600519"].isoformat()


def test_recovery_mode_is_advisory_only(tmp_path: Path) -> None:
    """An explicit recovery scan with a missing snapshot runs against direct
    bars but is advisory only: it marks emergency_direct_scan, never mutates
    the watchlist, and its final selection carries the advisory flag."""
    service, root = _make_service(tmp_path)
    service._config.week5.feature_snapshot_require_current = True
    assert not (root / "current.json").exists()

    # Ordinary manual scan (no recovery flag) must NOT bypass the gate.
    ordinary = service.run_week5_scan(
        symbols=["600000", "000001"],
        sync_reason="manual_cli",
        notify_enabled=False,
    )
    assert str(ordinary.get("emergency_direct_scan", False)) != "True"
    assert ordinary.get("recovery_mode") is not True

    # Explicit recovery run: advisory only.
    report = service.run_week5_scan(
        symbols=["600000", "000001"],
        sync_reason="manual_cli",
        notify_enabled=False,
        recovery_mode=True,
    )
    assert report.get("emergency_direct_scan") is True
    assert report.get("recovery_mode") is True
    funnel = report.get("funnel") or {}
    final_selection = funnel.get("final_selection") or {}
    assert final_selection.get("advisory_only") is True
    assert final_selection.get("final_signals") == []
    assert all(
        bool(item.get("advisory"))
        for item in (final_selection.get("advisory_signals") or [])
    )
    watchlist_sync = report.get("watchlist_sync") or {}
    assert watchlist_sync.get("updated", False) is False


def test_data_gate_applies_week6_quality_thresholds(tmp_path: Path) -> None:
    from datetime import date

    service, _root = _make_service(tmp_path)
    service._config.week5.feature_snapshot_require_current = False  # isolate quality
    today = date.today().isoformat()

    def _gate(score: float | None) -> dict[str, object]:
        _patch_attr(
            service,
            "_last_week6_data_quality_report",
            {"overall_coverage_ratio": score} if score is not None else None,
        )
        return service._build_data_gate(
            snapshot_manifest=None,
            snapshot_current=False,
            latest_trade_date=today,
        )

    ok_gate = _gate(0.95)
    assert ok_gate["status"] == "ok"
    watch_gate = _gate(0.80)
    assert watch_gate["status"] == "watch_only"
    assert any("data_quality_watch" in str(r) for r in watch_gate["reasons"])
    blocked_gate = _gate(0.50)
    assert blocked_gate["status"] == "blocked"
    assert any("data_quality_blocked" in str(r) for r in blocked_gate["reasons"])
    # Missing quality report -> no quality judgement (other gates still apply).
    no_report = _gate(None)
    assert no_report["status"] == "ok"


# ---------------------------------------------------------------------------
# Week5 性能改造：候选集 scope / 成员维护 / batch 并行 / staging 发布
# ---------------------------------------------------------------------------
class _AdvancingProviderNoDates:
    """SyntheticProvider WITHOUT ``latest_daily_dates`` (mirrors the production
    CachedProvider/ResilientProvider topology) whose latest bar advances by one
    trading day when ``advance`` is enabled."""

    def __init__(self, seed_offset: int = 2026, advance: bool = False) -> None:
        self._inner = SyntheticProvider(seed_offset=seed_offset)
        self._advance = advance
        self._cache: dict[str, pd.DataFrame] = {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120, **kwargs):
        if symbol not in self._cache:
            self._cache[symbol] = self._inner.fetch_daily_bars(
                symbol=symbol, lookback_days=250, **kwargs
            )
        frame = self._cache[symbol].tail(max(1, int(lookback_days))).copy()
        if self._advance:
            # 模拟新交易日：最新 bar 日期 +1 且内容变化（保留 index name）。
            last = frame.index[-1] + timedelta(days=1)
            new_row = frame.iloc[[-1]].copy()
            new_row.index = pd.DatetimeIndex([last], name=frame.index.name)
            new_row["close"] = new_row["close"] * 1.01
            frame = pd.concat([frame, new_row])
        return frame


def test_snapshot_refreshes_on_new_trading_day_without_date_interface(
    tmp_path: Path,
) -> None:
    """P0 回归：生产拓扑（CachedProvider/ResilientProvider 包装）不暴露
    latest_daily_dates 时，快照必须通过指纹探测发现新交易日并刷新，
    而不是无限复用旧快照。"""
    service, _root = _make_service(tmp_path)
    provider = _AdvancingProviderNoDates(advance=False)
    first = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert first["ok"] is True
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    date0 = manifest.per_symbol["600519"]["latest_date"]

    provider._advance = True  # 数据推进一天
    second = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert second["skipped"] is False
    assert second["incremental"] is True
    manifest2, frame2 = load_feature_snapshot(service._config)
    assert manifest2 is not None and frame2 is not None
    assert manifest2.per_symbol["600519"]["latest_date"] > date0
    assert str(
        frame2.loc[frame2["symbol"] == "600519", "trade_date"].iloc[0]
    ) > date0


class _WindowDependentNoDatesProvider:
    """无 latest_daily_dates、且数据随 lookback 窗口变化的 provider：每次
    fetch 都按窗口重新生成，最新 bar 的 fingerprint 跨窗口不可比——正好
    复现 P1 误判所需的"日期相同、短窗口指纹不同"（与 _AdvancingProviderNoDates
    的缓存式 tail 切片相反）。"""

    def __init__(
        self,
        seed_offset: int = 2026,
        advance_symbols: set[str] | None = None,
    ) -> None:
        self._inner = SyntheticProvider(seed_offset=seed_offset)
        self._advance_symbols = set(advance_symbols or set())

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120, **kwargs):
        frame = self._inner.fetch_daily_bars(
            symbol=symbol, lookback_days=max(1, int(lookback_days)), **kwargs
        )
        if symbol in self._advance_symbols:
            # 只推进指定 symbol：追加一个新交易日（日期+内容都变化）。
            last = frame.index[-1] + timedelta(days=1)
            new_row = frame.iloc[[-1]].copy()
            new_row.index = pd.DatetimeIndex([last], name=frame.index.name)
            new_row["close"] = new_row["close"] * 1.01
            frame = pd.concat([frame, new_row])
        return frame


def test_snapshot_partial_advance_without_date_interface_not_all_dirty(
    tmp_path: Path,
) -> None:
    """P1 回归（复核 3）：无日期能力 provider 仅部分 symbol 推进时，其余
    symbol 的短窗口 fingerprint 差异不得被误判为 dirty——否则增量路径每次
    都会把全部候选标 dirty，退化成全量刷新。"""
    service, _root = _make_service(tmp_path)
    provider = _WindowDependentNoDatesProvider()
    first = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert first["ok"] is True
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    before = {symbol: manifest.per_symbol[symbol]["latest_date"] for symbol in _SYMBOLS}

    provider._advance_symbols = {"600519"}  # 仅一只票推进
    second = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert second["skipped"] is False
    assert second["incremental"] is True
    assert second["dirty_symbols"] == 1, second  # 不是全部候选
    assert second["refreshed_count"] == 1
    assert second["published_symbol_count"] == len(_SYMBOLS)

    manifest2, frame2 = load_feature_snapshot(service._config)
    assert manifest2 is not None and frame2 is not None
    assert manifest2.per_symbol["600519"]["latest_date"] > before["600519"]
    for symbol in _SYMBOLS:
        if symbol != "600519":
            assert manifest2.per_symbol[symbol]["latest_date"] == before[symbol]
            assert str(frame2.loc[frame2["symbol"] == symbol].iloc[0]["trade_date"]) == (
                before[symbol]
            )

    # 推进停止后再次构建 -> 数据未变 -> 正常跳过（无残留误判）。
    provider._advance_symbols = set()
    third = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert third["skipped"] is True, third


def test_snapshot_stale_recovery_and_skip_without_date_interface(
    tmp_path: Path,
) -> None:
    """P0 回归：无 latest_daily_dates 接口时——
    1) 数据未变 + 快照过期：指纹探测确认无变化后恢复 current（保守探测）；
    2) 数据未变 + 快照 current：正常跳过。"""
    service, root = _make_service(tmp_path)
    provider = _AdvancingProviderNoDates(advance=False)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None

    # 数据未变 + 快照过期 -> 指纹相同 -> 刷新时效戳恢复 current。
    payload = manifest.to_payload()
    payload["built_at"] = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    (root / "current.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    manifest_stale, _ = load_feature_snapshot(service._config)
    assert manifest_stale is not None
    assert snapshot_is_current(manifest_stale, service._config) is False
    third = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert third["ok"] is True
    manifest4, _ = load_feature_snapshot(service._config)
    assert manifest4 is not None
    assert snapshot_is_current(manifest4, service._config) is True

    # 数据未变 + current -> 跳过。
    fourth = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert fourth["skipped"] is True


def test_snapshot_wrapper_chain_without_date_capability_falls_back(
    tmp_path: Path,
) -> None:
    """P0/P1 回归（真实缺陷场景）：CachedProvider(ResilientProvider(无日期接口
    provider)) 的包装链。包装层不得把"无能力"伪装成空 dict（会把全部候选
    误判 dirty 导致每次全量刷新）；必须让 snapshot 走 probe 兜底：
    1) 首次构建；2) 数据不变再次构建 skipped=True；3) 数据推进后 skipped=False
    且日期推进。"""
    from stock_analyzer.config import DataSourceConfig
    from stock_analyzer.data.cached_provider import CachedProvider
    from stock_analyzer.data.resilient_provider import ResilientProvider
    from stock_analyzer.infra.cache import InMemoryCache

    service, _root = _make_service(tmp_path)
    inner = _AdvancingProviderNoDates(advance=False)
    resilient = ResilientProvider(
        primary=inner, config=DataSourceConfig(), backup=None
    )
    cache = InMemoryCache()
    provider = CachedProvider(
        inner=resilient, cache=cache, ttl_sec=60, key_prefix="test"
    )

    first = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert first["ok"] is True
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    date0 = manifest.per_symbol["600519"]["latest_date"]

    # 数据不变再次构建 -> 必须 skipped（包装链无能力时不误判全部 dirty）。
    second = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert second["skipped"] is True, second

    # 数据推进一天（模拟缓存 TTL 过期后的真实读取）-> 探测到新交易日并刷新。
    cache.delete_prefix("test")
    inner._advance = True
    third = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250
    )
    assert third["skipped"] is False
    assert third["incremental"] is True
    manifest3, frame3 = load_feature_snapshot(service._config)
    assert manifest3 is not None and frame3 is not None
    assert manifest3.per_symbol["600519"]["latest_date"] > date0
    assert str(
        frame3.loc[frame3["symbol"] == "600519", "trade_date"].iloc[0]
    ) > date0




def test_snapshot_builds_from_legacy_daily_cache_payload(tmp_path: Path) -> None:
    """Legacy daily cache hits must remain usable by the snapshot builder."""
    from stock_analyzer.data.cached_provider import CachedProvider
    from stock_analyzer.infra.cache import InMemoryCache

    service, _root = _make_service(tmp_path)
    upstream = SyntheticProvider(seed_offset=2026)
    cache = InMemoryCache()
    provider = CachedProvider(
        inner=upstream,
        cache=cache,
        ttl_sec=60,
        key_prefix="legacy_snapshot",
    )
    for symbol in _SYMBOLS:
        bars = upstream.fetch_daily_bars(symbol=symbol, lookback_days=250)
        cache.set(
            f"legacy_snapshot:bars:{symbol}:250:latest",
            bars.to_json(date_format="iso", orient="split"),
            ttl_sec=60,
        )

    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        force=True,
        max_workers=1,
    )

    assert report["ok"] is True, report
    assert report["failed_symbols"] == 0
    assert provider.cache_hits == len(_SYMBOLS)
    manifest, frame = load_feature_snapshot(service._config)
    assert manifest is not None and frame is not None
    assert len(frame) == len(_SYMBOLS)

def test_snapshot_transparent_latest_dates_through_wrappers(tmp_path: Path) -> None:
    """P0 回归：CachedProvider / ResilientProvider 透传 latest_daily_dates，
    生产拓扑可直接驱动日期增量。"""
    from datetime import date

    from stock_analyzer.config import DataSourceConfig
    from stock_analyzer.data.cached_provider import CachedProvider
    from stock_analyzer.data.resilient_provider import ResilientProvider
    from stock_analyzer.infra.cache import InMemoryCache

    class _NoDates:
        def fetch_daily_bars(self, symbol, lookback_days=120, **kwargs):
            raise AssertionError("not used in this test")

    class _WithDates:
        def latest_daily_dates(self, *, symbols=None):
            return {s: date(2026, 8, 12) for s in (symbols or [])}

    inner = _WithDates()
    cached = CachedProvider(
        inner=inner, cache=InMemoryCache(), ttl_sec=60, key_prefix="test"
    )
    assert callable(getattr(cached, "latest_daily_dates", None))
    assert cached.latest_daily_dates(symbols=["600519"]) == {
        "600519": date(2026, 8, 12)
    }
    # 内层无日期接口 -> 包装层必须返回 None（不是空 dict），
    # 否则 snapshot 会把全部候选误判为 dirty。
    no_dates = CachedProvider(
        inner=_NoDates(), cache=InMemoryCache(), ttl_sec=60, key_prefix="test"
    )
    assert no_dates.latest_daily_dates(symbols=["600519"]) is None
    no_dates_resilient = ResilientProvider(
        primary=_NoDates(), config=DataSourceConfig(), backup=None
    )
    assert no_dates_resilient.latest_daily_dates(symbols=["600519"]) is None

    resilient = ResilientProvider(
        primary=_WithDates(),
        config=DataSourceConfig(),
        backup=None,
    )
    assert callable(getattr(resilient, "latest_daily_dates", None))
    assert resilient.latest_daily_dates(symbols=["600519"]) == {
        "600519": date(2026, 8, 12)
    }


def test_snapshot_manifest_records_scope_and_candidate_counts(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        force=True,
        scope="universe_quality",
    )
    assert report["ok"] is True
    assert report["scope"] == "universe_quality"
    assert report["requested_symbol_count"] == len(_SYMBOLS)
    assert report["published_symbol_count"] == len(_SYMBOLS)
    assert report["universe_hash"] != ""
    assert "stages" in report
    assert set(report["stages"]) == {"snapshot_fetch", "snapshot_transform"}
    assert report["workers"] == 4

    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    assert manifest.scope == "universe_quality"
    assert manifest.universe_hash == report["universe_hash"]
    assert manifest.requested_symbol_count == len(_SYMBOLS)
    assert manifest.published_symbol_count == len(_SYMBOLS)
    # 相同的候选集再次构建 -> 直接跳过（不重复构建）。
    again = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        scope="universe_quality",
    )
    assert again["skipped"] is True


def test_snapshot_scope_change_forces_refresh_not_skip(tmp_path: Path) -> None:
    """scope 或候选集变化时，即使数据本身 current 也不允许无条件跳过。"""
    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        force=True,
        scope="universe_quality",
    )
    # 候选集不变但 scope 不同 -> 不跳过（重新落 scope 标签）。
    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        scope="manual_scope",
    )
    assert report["skipped"] is False
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    assert manifest.scope == "manual_scope"


def test_snapshot_incremental_drops_removed_and_adds_new_symbols(
    tmp_path: Path,
) -> None:
    """候选集变化时：移出股票必须从快照/记账/尾巴中删除，新增股票必须补齐。"""
    service, root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    first = build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    assert first["ok"] is True
    manifest0, frame0 = load_feature_snapshot(service._config)
    assert manifest0 is not None and frame0 is not None
    assert set(frame0["symbol"]) == set(_SYMBOLS)

    # 新候选集：移出 2 只（600519、000001），新增 2 只（002415、300999）。
    new_set = ["601318", "000858", "600000", "300750", "002415", "300999"]
    second = build_feature_snapshot(
        service._config, provider, symbols=new_set, lookback_days=250
    )
    assert second["ok"] is True
    assert second["skipped"] is False
    assert second["incremental"] is True
    assert set(second["removed_symbols"]) == {"600519", "000001"}

    manifest, frame = load_feature_snapshot(service._config)
    assert manifest is not None and frame is not None
    assert set(frame["symbol"]) == set(new_set)
    assert manifest.symbol_count == len(new_set)
    assert manifest.requested_symbol_count == len(new_set)
    assert manifest.published_symbol_count == len(new_set)
    # 记账中不残留移出符号。
    assert "600519" not in manifest.per_symbol
    assert "000001" not in manifest.per_symbol
    assert "002415" in manifest.per_symbol
    # 尾巴中不残留移出符号。
    tails = load_snapshot_tails(root, manifest.data_snapshot_id)
    assert tails is not None
    assert {"600519", "000001"}.isdisjoint(set(tails["symbol"]))


def test_snapshot_removed_only_refresh_drops_symbols_without_dirty(
    tmp_path: Path,
) -> None:
    """候选集缩小但无日期脏数据：必须发布子集而非原样跳过。"""
    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    build_feature_snapshot(
        service._config, provider, symbols=_SYMBOLS, lookback_days=250, force=True
    )
    subset = _SYMBOLS[:4]
    report = build_feature_snapshot(
        service._config, provider, symbols=subset, lookback_days=250
    )
    assert report["ok"] is True
    assert report["incremental"] is True
    assert report["dirty_symbols"] == 0
    assert set(report["removed_symbols"]) == set(_SYMBOLS[4:])
    manifest, frame = load_feature_snapshot(service._config)
    assert manifest is not None and frame is not None
    assert set(frame["symbol"]) == set(subset)
    assert manifest.symbol_count == len(subset)


def test_snapshot_removed_failed_symbol_no_longer_blocks_current(
    tmp_path: Path, monkeypatch
) -> None:
    """失败符号移出候选集后，快照必须恢复 current（否则永远不会自愈）。"""
    import stock_analyzer.feature.snapshot as snapshot_module

    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    original_worker = snapshot_module._transform_row_worker

    def _fail_one(payload):
        if payload[0] == "000001":
            return payload[0], None
        return original_worker(payload)

    monkeypatch.setattr(snapshot_module, "_transform_row_worker", _fail_one)
    first = build_feature_snapshot(
        service._config,
        provider,
        symbols=["600519", "000001", "601318"],
        lookback_days=250,
        force=True,
        max_workers=1,
    )
    assert first["ok"] is False
    manifest, _ = load_feature_snapshot(service._config)
    assert manifest is not None
    assert manifest.failed_symbols_list == ["000001"]
    assert snapshot_is_current(manifest, service._config) is False

    # 失败符号离开候选集 -> 快照（其余符号完整）恢复 current。
    monkeypatch.setattr(snapshot_module, "_transform_row_worker", original_worker)
    second = build_feature_snapshot(
        service._config,
        provider,
        symbols=["600519", "601318"],
        lookback_days=250,
    )
    assert second["ok"] is True
    manifest2, frame2 = load_feature_snapshot(service._config)
    assert manifest2 is not None and frame2 is not None
    assert set(frame2["symbol"]) == {"600519", "601318"}
    assert manifest2.failed_symbols_list == []
    assert snapshot_is_current(manifest2, service._config) is True


def test_snapshot_build_failure_publishes_no_half_product(
    tmp_path: Path, monkeypatch
) -> None:
    """transform 全失败时不得发布半成品：无 staging 残留、current.json 不被替换。"""
    import stock_analyzer.feature.snapshot as snapshot_module

    service, root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)

    def _always_fail(payload):
        return payload[0], None

    monkeypatch.setattr(snapshot_module, "_transform_row_worker", _always_fail)
    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        force=True,
        max_workers=1,
    )
    assert report["ok"] is False
    assert (root / "current.json").exists() is False
    # 无半成品目录：不残留 staging，也不存在指向失败产物的 snap_ 目录。
    if root.exists():
        leftovers = [
            item.name
            for item in root.iterdir()
            if item.name.startswith("snap_") or item.name.endswith(".staging")
        ]
        assert leftovers == []


def test_snapshot_transform_uses_process_pool_with_batching(
    tmp_path: Path, monkeypatch
) -> None:
    """max_workers>1 时确实走进程池并行，且按 batch 提交（任务数 = ceil(n/batch)）。"""
    import stock_analyzer.feature.snapshot as snapshot_module

    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    submitted: list[int] = []

    real_pool = snapshot_module.ProcessPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers: int = 1, **kwargs):
            submitted.append(max_workers)
            super().__init__(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(snapshot_module, "ProcessPoolExecutor", _RecordingPool)
    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        force=True,
        max_workers=2,
        batch_size=4,
    )
    assert report["ok"] is True
    # 进程池被真实创建（workers>1 分支），且按 batch_size=4 分块提交：
    # 6 只符号 -> 2 个 batch 任务。
    assert submitted == [2]
    assert report["batch_size"] == 4
    assert report["stages"]["snapshot_transform"]["completed"] == len(_SYMBOLS)


def test_snapshot_transform_sliding_window_limits_inflight_batches(
    tmp_path: Path, monkeypatch
) -> None:
    """P2 回归：滑动窗口提交保证任意时刻在途 batch <= workers
    （IPC 队列不驻留全部候选的 pickle）。"""
    import stock_analyzer.feature.snapshot as snapshot_module

    service, _root = _make_service(tmp_path)
    provider = SyntheticProvider(seed_offset=2026)
    real_pool = snapshot_module.ProcessPoolExecutor

    class _TrackingPool(real_pool):
        def __init__(self, max_workers: int = 1, **kwargs):
            self.max_workers_arg = max_workers
            self.submit_count = 0
            self._inflight = 0
            self.peak_inflight = 0
            super().__init__(max_workers=max_workers, **kwargs)

        def submit(self, fn, *args, **kwargs):
            self.submit_count += 1
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
            future = super().submit(fn, *args, **kwargs)
            future.add_done_callback(lambda _f: self._decrement())
            return future

        def _decrement(self) -> None:
            self._inflight -= 1

    tracking: list[_TrackingPool] = []

    def _make_tracking_pool(max_workers: int = 1, **kwargs):
        pool = _TrackingPool(max_workers=max_workers, **kwargs)
        tracking.append(pool)
        return pool

    monkeypatch.setattr(snapshot_module, "ProcessPoolExecutor", _make_tracking_pool)
    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        force=True,
        max_workers=2,
        batch_size=4,  # 6 只 -> 2 个 batch
    )
    assert report["ok"] is True
    assert len(tracking) == 1
    pool = tracking[0]
    assert pool.submit_count == 2  # ceil(6/4)
    # 任意时刻在途 batch 不超过 workers。
    assert pool.peak_inflight <= 2


def test_snapshot_progress_file_written_atomically(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path)
    progress = tmp_path / "progress.json"
    provider = SyntheticProvider(seed_offset=2026)
    report = build_feature_snapshot(
        service._config,
        provider,
        symbols=_SYMBOLS,
        lookback_days=250,
        force=True,
        progress_path=str(progress),
    )
    assert report["ok"] is True
    payload = json.loads(progress.read_text(encoding="utf-8"))
    assert payload["phase"] == "snapshot_transform"
    assert payload["completed"] == len(_SYMBOLS)
    assert payload["total"] == len(_SYMBOLS)
    assert payload["workers"] == 4
    assert "timestamp" in payload
    # 原子写入不留 .tmp 残留。
    assert not list(tmp_path.glob("*.tmp"))
