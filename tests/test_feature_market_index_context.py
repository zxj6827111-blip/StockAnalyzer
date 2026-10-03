"""FeatureEngineer 市场相对族指数上下文（FEATURE_COMPUTE_VERSION v2）。

背景（D1 对拍，tmp_reversal_localization/FINDINGS_20261003.md）：v1 时代
快照链与 deep frame 从未把 market_index 传给 ``FeatureEngineer.transform``，
excess_ret/relative_strength/rs_ma/beta 一族在生产侧恒为 fillna(0) 的常数 0，
与 PIT 面板不可比。v2 引入 ``attach_market_index`` 实例上下文 +
``FEATURE_COMPUTE_VERSION`` 参与 schema hash。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.feature import snapshot as snapshot_module
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.snapshot import (
    _feature_schema_hash,
    _fetch_snapshot_benchmark_frame,
    _snapshot_row_for_symbol,
)
from stock_analyzer.runtime.services.week5_service import _build_fresh_deep_frame


def _bars(periods: int = 40) -> pd.DataFrame:
    dates = pd.bdate_range("2025-12-01", periods=periods)
    close = np.linspace(10.0, 20.0, num=periods) * (
        1 + np.sin(np.arange(periods) / 3.0) * 0.02
    )
    volume = np.linspace(1_000_000, 2_000_000, num=periods)
    frame = pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": volume,
            "turnover": close * volume,
            "float_market_cap": 10_000_000_000.0,
            "is_st": [False] * periods,
            "is_delisting_risk": [False] * periods,
            "board": ["main"] * periods,
        },
        index=dates,
    )
    frame.index.name = "date"
    return frame


def _benchmark(periods: int = 40, *, slope: float = 0.05) -> pd.DataFrame:
    dates = pd.bdate_range("2025-12-01", periods=periods)
    close = np.linspace(3000.0, 3000.0 * (1.0 + slope), num=periods)
    frame = pd.DataFrame({"close": close}, index=dates)
    frame.index.name = "date"
    return frame


def test_transform_without_market_index_zero_fills_market_relative_family() -> None:
    features = FeatureEngineer().transform(_bars())
    for column in ("excess_ret_5", "relative_strength_20", "rolling_beta_60"):
        assert column in features.columns
        assert float(features[column].abs().max()) == 0.0


def test_transform_with_market_index_computes_excess_returns() -> None:
    bars = _bars()
    benchmark = _benchmark(slope=0.05)
    features = FeatureEngineer().transform(bars, market_index=benchmark)

    column = features["excess_ret_5"]
    assert float(column.abs().max()) > 0.0
    # T-1 语义：features.iloc[i] 来自 raw.iloc[i-1]。
    i = 20
    stock_ret_5 = bars["close"].pct_change(5)
    idx_ret_5 = benchmark["close"].reindex(bars.index).pct_change(5)
    expected = float(stock_ret_5.iloc[i - 1] - idx_ret_5.iloc[i - 1])
    assert float(column.iloc[i]) == pytest.approx(expected, abs=1e-12)


def test_attach_market_index_context_and_explicit_override() -> None:
    bars = _bars()
    engineer = FeatureEngineer()
    engineer.attach_market_index(_benchmark(slope=0.05))
    with_context = engineer.transform(bars)
    assert float(with_context["excess_ret_5"].abs().max()) > 0.0

    # 显式实参（含 None 之外的帧）优先于实例上下文。
    flat = engineer.transform(bars, market_index=_benchmark(slope=0.0))
    assert float(flat["excess_ret_5"].abs().max()) > 0.0
    assert not np.allclose(
        with_context["excess_ret_5"].to_numpy(),
        flat["excess_ret_5"].to_numpy(),
    )

    # 空帧上下文等价于无指数：整族回落 0。
    engineer.attach_market_index(pd.DataFrame())
    fallback = engineer.transform(bars)
    assert float(fallback["excess_ret_5"].abs().max()) == 0.0


def test_feature_schema_hash_includes_compute_version(monkeypatch: pytest.MonkeyPatch) -> None:
    engineer = FeatureEngineer()
    baseline = _feature_schema_hash(engineer)
    monkeypatch.setattr(snapshot_module, "FEATURE_COMPUTE_VERSION", 999)
    assert _feature_schema_hash(FeatureEngineer()) != baseline


def test_fetch_snapshot_benchmark_frame_failures_return_empty() -> None:
    class WithIndex:
        def fetch_index_daily(self, **_kwargs: Any) -> pd.DataFrame:
            return _benchmark(periods=200)

    class BrokenIndex:
        def fetch_index_daily(self, **_kwargs: Any) -> pd.DataFrame:
            raise RuntimeError("boom")

    class NoIndex:
        pass

    assert not _fetch_snapshot_benchmark_frame(WithIndex(), lookback_days=250).empty
    assert _fetch_snapshot_benchmark_frame(BrokenIndex(), lookback_days=250).empty
    assert _fetch_snapshot_benchmark_frame(NoIndex(), lookback_days=250).empty


def test_snapshot_row_symbol_uses_attached_index() -> None:
    bars = _bars()

    bare = _snapshot_row_for_symbol(bars=bars, symbol="600000", engineer=FeatureEngineer())
    assert bare is not None
    assert float(bare["excess_ret_5"].iloc[0]) == 0.0

    attached = FeatureEngineer()
    attached.attach_market_index(_benchmark(periods=40, slope=0.08))
    row = _snapshot_row_for_symbol(bars=bars, symbol="600000", engineer=attached)
    assert row is not None
    assert float(row["excess_ret_5"].iloc[0]) != 0.0


class _FakeDeepProvider:
    """fetch_daily_bars 可用、fetch_index_daily 可选的最小 provider 替身。

    指数帧长度须 ≥ deep frame 请求的 lookback（``_fetch_index_daily_compat``
    会 ``tail(lookback)``），且日期与 bars 重叠，否则 reindex 后整族为 NaN。
    """

    def __init__(self, *, with_index: bool) -> None:
        self._with_index = with_index

    def fetch_daily_bars(self, *, symbol: str, lookback_days: int) -> pd.DataFrame:
        return _bars(periods=max(40, int(lookback_days)))

    def fetch_index_daily(self, **_kwargs: Any) -> pd.DataFrame:
        if not self._with_index:
            raise RuntimeError("no index feed")
        return _benchmark(periods=125, slope=0.10)


def test_build_fresh_deep_frame_attaches_market_index() -> None:
    result = _build_fresh_deep_frame(
        provider=_FakeDeepProvider(with_index=True),
        warehouse=None,
        vendor_overlay=None,
        symbols=["600000"],
        required_date="2026-01-30",
        lookback_days=60,
    )
    assert result["market_index_attached"] is True
    frame = result["frame"]
    assert isinstance(frame, pd.DataFrame) and len(frame) == 1
    assert float(frame["excess_ret_5"].iloc[0]) != 0.0

    bare = _build_fresh_deep_frame(
        provider=_FakeDeepProvider(with_index=False),
        warehouse=None,
        vendor_overlay=None,
        symbols=["600000"],
        required_date="2026-01-30",
        lookback_days=60,
    )
    assert bare["market_index_attached"] is False
    bare_frame = bare["frame"]
    assert isinstance(bare_frame, pd.DataFrame) and len(bare_frame) == 1
    assert float(bare_frame["excess_ret_5"].iloc[0]) == 0.0


class _FakeWarehouse:
    """最小 MarketWarehouse 替身：只带 fetch_index_daily，帧带来源 tag。"""

    def __init__(self, *, rows: int | None, error: bool = False, tag: str = "") -> None:
        self._rows = rows
        self._error = error
        self._tag = tag

    def fetch_index_daily(self, *, index_code: str) -> pd.DataFrame:
        if self._error:
            raise RuntimeError("warehouse unavailable")
        if self._rows is None:
            return pd.DataFrame()
        frame = _benchmark(periods=self._rows, slope=0.06)
        frame.attrs["source_tag"] = self._tag
        return frame


def _overlay_with_warehouses(
    enrichment: _FakeWarehouse | None, delta: _FakeWarehouse | None
) -> Any:
    """子类覆写两个惰性仓库解析器（slots 类不可打实例属性补丁）。"""
    from stock_analyzer.data.vendor_zip_overlay import VendorZipOverlayProvider

    class _Stub(VendorZipOverlayProvider):
        def __post_init__(self) -> None:
            self._stub_enrichment = enrichment
            self._stub_delta = delta

        def _market_enrichment(self) -> Any:
            return self._stub_enrichment

        def _delta_warehouse(self) -> Any:
            return self._stub_delta

    return _Stub(data_root="stub", index_path="stub", delta_db_path="stub")


def test_vendor_overlay_fetch_index_daily_prefers_configured_delta() -> None:
    # 配置库（warehouse_db_path，market sync 的写入目标）优先于 legacy 启发式库。
    overlay = _overlay_with_warehouses(
        enrichment=_FakeWarehouse(rows=125, tag="enrichment"),
        delta=_FakeWarehouse(rows=125, tag="delta"),
    )
    frame = overlay.fetch_index_daily(index_code="000300.SH")
    assert not frame.empty
    assert frame.attrs["source_tag"] == "delta"


def test_vendor_overlay_fetch_index_daily_fallback_and_empty() -> None:
    # 配置库缺表（空帧）→ legacy enrichment 兜底。
    fallback = _overlay_with_warehouses(_FakeWarehouse(rows=125), _FakeWarehouse(rows=None))
    assert not fallback.fetch_index_daily(index_code="000300.SH").empty

    # 两个来源都出空帧/出错 → 空帧（不阻断日线链、不伪造指数）。
    empty = _overlay_with_warehouses(
        _FakeWarehouse(rows=None, error=True), _FakeWarehouse(rows=None)
    )
    frame = empty.fetch_index_daily(index_code="000300.SH")
    assert isinstance(frame, pd.DataFrame) and frame.empty


def test_snapshot_benchmark_frame_accepts_overlay_provider() -> None:
    overlay = _overlay_with_warehouses(
        enrichment=_FakeWarehouse(rows=150), delta=_FakeWarehouse(rows=150)
    )
    frame = _fetch_snapshot_benchmark_frame(overlay, lookback_days=250)
    assert not frame.empty
