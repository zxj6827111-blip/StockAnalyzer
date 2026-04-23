from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.service import StockAnalyzerService


class IntradaySyntheticProvider:
    def __init__(self, seed_offset: int) -> None:
        base = SyntheticProvider(seed_offset=seed_offset)
        self._fetch_daily_bars = cast(
            Callable[[str, int], pd.DataFrame],
            base.fetch_daily_bars,
        )

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        return self._fetch_daily_bars(symbol, lookback_days)

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=lookback_days)
        frame = pd.DataFrame(
            {
                "tail30_volume_share": [0.55] * len(dates),
                "morning30_volume_share": [0.25] * len(dates),
                "above_vwap_ratio": [0.60] * len(dates),
                "price_efficiency": [0.70] * len(dates),
            },
            index=dates,
        )
        frame.index.name = "date"
        return frame


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0
    config.week5.auto_notify = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = Path(tempfile.gettempdir()) / "stock_analyzer_tests"
    config.training.bootstrap_state_path = str(
        temp_root / "test_bootstrap_state_v13_acceptance.json"
    )
    return config


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_path(value: object) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)
    raise AssertionError(f"Expected path-like value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _new_service(
    config: StockAnalyzerConfig,
    provider: object | None = None,
) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    runtime_provider = provider if provider is not None else SyntheticProvider(seed_offset=2027)
    _patch_attr(service, "_provider", runtime_provider)
    _patch_attr(service._pipeline, "_provider", runtime_provider)
    _patch_attr(service, "_realtime_provider", runtime_provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", runtime_provider)
    return service


def test_v13_acceptance_data_utilization_section_passes_with_trained_native_artifact(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    artifact_path = tmp_path / "model.json"
    baseline_path = tmp_path / "artifacts" / "acceptance" / "baseline_report.json"
    report_path = tmp_path / "artifacts" / "acceptance" / "v13_acceptance_report.json"
    config.training.artifact_path = str(artifact_path)
    config.training.baseline_report_path = str(baseline_path)
    provider = IntradaySyntheticProvider(seed_offset=2468)
    service: StockAnalyzerService = _new_service(config, provider=provider)

    service.train_models(
        symbol="600000",
        lookback_days=320,
        artifact_path=str(artifact_path),
        full_market=False,
    )
    baseline = _as_mapping(
        service.generate_baseline_report(
            symbol="600000",
            lookback_days=320,
            output_path=str(baseline_path),
        )
    )
    week5 = service.run_week5_scan(symbols=["600000", "000001"], notify_enabled=False)
    report = _as_mapping(
        service.generate_v13_acceptance_report(
            baseline_report_path=str(_as_path(baseline["output_path"])),
            output_path=str(report_path),
            week5_report=week5,
        )
    )
    sections = _as_mapping(report["sections"])

    assert _as_path(report["output_path"]).exists() is True
    assert _as_mapping(sections["11.2_data_utilization"])["status"] == "pass"


def test_v13_acceptance_shortlist_section_passes_with_week5_candidates(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    artifact_path = tmp_path / "model.json"
    baseline_path = tmp_path / "artifacts" / "acceptance" / "baseline_report.json"
    report_path = tmp_path / "artifacts" / "acceptance" / "v13_acceptance_report.json"
    config.training.artifact_path = str(artifact_path)
    config.training.baseline_report_path = str(baseline_path)
    provider = IntradaySyntheticProvider(seed_offset=2469)
    service: StockAnalyzerService = _new_service(config, provider=provider)

    service.train_models(
        symbol="600000",
        lookback_days=320,
        artifact_path=str(artifact_path),
        full_market=False,
    )
    baseline = _as_mapping(
        service.generate_baseline_report(
            symbol="600000",
            lookback_days=320,
            output_path=str(baseline_path),
        )
    )
    week5 = service.run_week5_scan(symbols=["600000", "000001"], notify_enabled=False)
    report = _as_mapping(
        service.generate_v13_acceptance_report(
            baseline_report_path=str(_as_path(baseline["output_path"])),
            output_path=str(report_path),
            week5_report=week5,
        )
    )
    sections = _as_mapping(report["sections"])

    assert _as_path(report["output_path"]).exists() is True
    assert _as_mapping(sections["11.3_shortlist_quality"])["status"] == "pass"


def test_v13_acceptance_uses_pipeline_fallback_when_week5_evidence_is_empty(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    artifact_path = tmp_path / "model.json"
    baseline_path = tmp_path / "artifacts" / "acceptance" / "baseline_report.json"
    report_path = tmp_path / "artifacts" / "acceptance" / "v13_acceptance_report.json"
    config.training.artifact_path = str(artifact_path)
    config.training.baseline_report_path = str(baseline_path)
    provider = IntradaySyntheticProvider(seed_offset=2470)
    service: StockAnalyzerService = _new_service(config, provider=provider)

    service.train_models(
        symbol="600000",
        lookback_days=320,
        artifact_path=str(artifact_path),
        full_market=False,
    )
    baseline = _as_mapping(
        service.generate_baseline_report(
            symbol="600000",
            lookback_days=320,
            output_path=str(baseline_path),
        )
    )
    report = _as_mapping(
        service.generate_v13_acceptance_report(
            baseline_report_path=str(_as_path(baseline["output_path"])),
            output_path=str(report_path),
            week5_report={"signal_pool": {"candidates": []}},
        )
    )
    sections = _as_mapping(report["sections"])

    assert _as_path(report["output_path"]).exists() is True
    assert _as_mapping(sections["11.2_data_utilization"])["status"] == "pass"
    assert _as_mapping(sections["11.3_shortlist_quality"])["status"] == "pass"
    assert _as_mapping(sections["11.4_runtime_quality"])["status"] == "pass"
