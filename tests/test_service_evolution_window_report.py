from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.evolution_core_service import _report_path_timestamp_hint


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.cloud_backup.enabled = False
    config.acceptance.auto_run = False
    config.evolution.enabled = True
    config.evolution.auto_run = False
    config.evolution.strict_dependency_check = False
    config.evolution.code_commit_id = "git:test"
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.report_dir = "artifacts/evolution/history"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.tdx_sync.refresh_before_evolution = False
    config.market_warehouse.refresh_before_evolution = False
    return config


def _valid_records() -> list[dict[str, object]]:
    return [
        {
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 2_000_000,
        }
    ]


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise AssertionError(f"Expected bool value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _new_service(config: StockAnalyzerConfig, tmp_path: Path) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2032)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_evolution_project_root", tmp_path)
    _patch_attr(
        service,
        "_evolution_orchestrator",
        OffhoursEvolutionOrchestrator(config=config.evolution, project_root=tmp_path),
    )
    return service


class _IntradaySyntheticProvider(SyntheticProvider):
    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        bars = self.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
        dates = pd.DatetimeIndex(bars.index)
        step = 1.0 if interval.strip().lower() == "1m" else 5.0
        frame = pd.DataFrame(index=dates)
        frame["session_return"] = [round(0.01 + idx * 0.001, 6) for idx in range(len(dates))]
        frame["realized_vol"] = [round(0.02 + idx * 0.0005, 6) for idx in range(len(dates))]
        frame["vwap_gap"] = [round(0.003 + idx * 0.0001, 6) for idx in range(len(dates))]
        frame["last30_return"] = [round(0.004 + idx * 0.0001, 6) for idx in range(len(dates))]
        frame["tail30_volume_share"] = [round(0.20 + idx * 0.001, 6) for idx in range(len(dates))]
        frame["close_position"] = [round(0.55 + min(idx, 10) * 0.01, 6) for idx in range(len(dates))]
        frame["am_pm_diff"] = [round(0.002 * step + idx * 0.0001, 6) for idx in range(len(dates))]
        frame["close_vwap_stability"] = [
            round(0.70 + min(idx, 10) * 0.01, 6) for idx in range(len(dates))
        ]
        frame["intraday_pullback_ratio"] = [
            round(0.08 + min(idx, 10) * 0.01, 6) for idx in range(len(dates))
        ]
        return frame


def test_evolution_window_report_passes_with_sufficient_runs(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config=config, tmp_path=tmp_path)

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
        dry_run=True,
        source_trace_id="window-1",
    )
    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
        dry_run=True,
        source_trace_id="window-2",
    )

    report = _as_mapping(
        service.evolution_window_report(
            days=10,
            min_runs=2,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
        )
    )
    assert _as_int(report["records"]) >= 2
    assert report["overall"] in {"pass", "pass_with_warnings"}
    checks = _as_mapping_list(report["checks"])
    runs_count = next(item for item in checks if item["name"] == "window_runs_count")
    assert runs_count["status"] == "pass"


def test_evolution_window_report_fails_with_insufficient_runs(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config=config, tmp_path=tmp_path)

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
        dry_run=True,
        source_trace_id="window-only",
    )
    report = _as_mapping(
        service.evolution_window_report(
            days=10,
            min_runs=3,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
        )
    )
    assert report["overall"] == "fail"
    checks = _as_mapping_list(report["checks"])
    runs_count = next(item for item in checks if item["name"] == "window_runs_count")
    assert runs_count["status"] == "fail"


def test_report_path_timestamp_hint_supports_fractional_seconds() -> None:
    hinted = _report_path_timestamp_hint(
        Path("20260327_212656.769397_evo-20260301072633.json")
    )
    expected = datetime.fromisoformat("2026-03-27T21:26:56").timestamp()
    assert hinted == expected


def test_report_path_timestamp_hint_returns_none_for_unstructured_names() -> None:
    assert _report_path_timestamp_hint(Path("evolution_latest.json")) is None


def test_evolution_window_report_reuses_persisted_cache_across_services(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config=config, tmp_path=tmp_path)
    timestamp = datetime.fromisoformat("2026-03-03T20:40:00")
    now = datetime.fromisoformat("2026-03-04T12:00:00")

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=timestamp,
        dry_run=True,
        source_trace_id="window-cache-1",
    )
    first_report = _as_mapping(service.evolution_window_report(days=10, min_runs=1, now=now))
    cache_path = tmp_path / "artifacts" / "evolution" / "window_report_cache.json"
    assert cache_path.exists() is True

    second_service = _new_service(config=config, tmp_path=tmp_path)

    def _unexpected_disk_load(*, cutoff_ts: float) -> list[dict[str, object]]:
        raise AssertionError(f"expected persisted cache hit, got cutoff_ts={cutoff_ts}")

    _patch_attr(second_service, "_load_evolution_reports_from_disk", _unexpected_disk_load)
    second_report = _as_mapping(second_service.evolution_window_report(days=10, min_runs=1, now=now))
    assert second_report["records"] == first_report["records"]
    assert second_report["overall"] == first_report["overall"]


def test_service_offhours_auto_wires_m5_m7_m11_loader_inputs(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.auto_generate_loader_inputs = True
    news_path = tmp_path / "artifacts" / "evolution" / "inputs" / "m7_news_latest.jsonl"
    news_path.parent.mkdir(parents=True, exist_ok=True)
    news_path.write_text(
        "\n".join(
            [
                (
                    '{"event_id":"news-1","symbol":"600000.SH","headline":"券商板块走强，成交放量",'
                    '"sentiment":0.90,"cost":0.20,"source":"新华网","proxy_generated":false}'
                ),
                (
                    '{"event_id":"news-2","symbol":"000001.SZ","headline":"地产政策预期升温",'
                    '"sentiment":0.80,"cost":0.20,"source":"证券时报","proxy_generated":false}'
                ),
            ]
        ),
        encoding="utf-8",
    )
    service = _new_service(config=config, tmp_path=tmp_path)
    provider = _IntradaySyntheticProvider(seed_offset=2032)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)

    report = _as_mapping(
        service.run_evolution_offhours(
            symbols=["600000.SH", "000001.SZ"],
            timestamp=datetime.fromisoformat("2026-03-03T20:40:00"),
            dry_run=True,
            source_trace_id="service-loader-auto-wire",
        )
    )
    modules = _as_mapping(report["modules"])
    m5_input = _as_mapping(_as_mapping(modules["m5"])["input"])
    m7_input = _as_mapping(_as_mapping(modules["m7"])["input"])
    m11_input = _as_mapping(_as_mapping(modules["m11"])["input"])
    assert m5_input["source"] == "label_loader"
    assert m7_input["source"] == "news_loader"
    assert m11_input["source"] == "shadow_loader"
    assert _as_int(m5_input["loaded_records"]) > 0
    assert _as_int(m7_input["loaded_records"]) > 0
    assert _as_int(m11_input["loaded_samples"]) > 0
    assert float(m5_input["intraday_1m_coverage_ratio"]) > 0.0
    assert float(m5_input["intraday_5m_coverage_ratio"]) > 0.0
    assert float(m11_input["intraday_1m_coverage_ratio"]) > 0.0
    assert float(m11_input["intraday_5m_coverage_ratio"]) > 0.0

    loader_inputs = _as_mapping(report["loader_inputs"])
    assert _as_bool(_as_mapping(loader_inputs["m5"])["fresh"]) is True
    assert _as_bool(_as_mapping(loader_inputs["m7"])["fresh"]) is True
    assert _as_bool(_as_mapping(loader_inputs["m11"])["fresh"]) is True
    assert float(_as_mapping(loader_inputs["m5"])["intraday_1m_coverage_ratio"]) > 0.0
    assert float(_as_mapping(loader_inputs["m11"])["intraday_5m_coverage_ratio"]) > 0.0
    assert (tmp_path / str(config.evolution.m5_label_records_path)).exists() is True
    assert (tmp_path / str(config.evolution.m7_news_records_path)).exists() is True
    assert (tmp_path / str(config.evolution.m11_shadow_results_path)).exists() is True

    m5_records = [
        json.loads(line)
        for line in (tmp_path / str(config.evolution.m5_label_records_path))
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    m11_records = [
        json.loads(line)
        for line in (tmp_path / str(config.evolution.m11_shadow_results_path))
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert m5_records
    assert m11_records
    first_m5 = m5_records[0]
    first_m11 = m11_records[0]
    assert first_m5["intraday_1m_latest_date"] == first_m5["date"]
    assert first_m5["intraday_5m_latest_date"] == first_m5["date"]
    assert "intraday_5m_close_vwap_stability" in first_m5
    assert first_m11["intraday_1m_latest_date"] == first_m11["date"]
    assert first_m11["intraday_5m_latest_date"] == first_m11["date"]
    assert "intraday_5m_intraday_pullback_ratio" in first_m11


def test_service_offhours_leaves_m7_unavailable_without_real_news_or_proxy_fallback(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.auto_generate_loader_inputs = True
    config.evolution.m7_market_proxy_fallback_enabled = False
    service = _new_service(config=config, tmp_path=tmp_path)

    report = _as_mapping(
        service.run_evolution_offhours(
            symbols=["600000.SH", "000001.SZ"],
            timestamp=datetime.fromisoformat("2026-03-03T20:40:00"),
            dry_run=True,
            source_trace_id="service-loader-real-news-required",
        )
    )

    loader_inputs = _as_mapping(report["loader_inputs"])
    m7_loader = _as_mapping(loader_inputs["m7"])
    assert m7_loader["source"] == "unavailable"
    assert m7_loader["reason"] == "no_valid_news_input"

    modules = _as_mapping(report["modules"])
    m7_input = _as_mapping(_as_mapping(modules["m7"])["input"])
    assert m7_input["source"] == "missing_news_input"
    assert _as_int(m7_input["loaded_records"]) == 0


def test_service_offhours_can_explicitly_opt_into_m7_market_proxy_fallback(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.auto_generate_loader_inputs = True
    config.evolution.m7_market_proxy_fallback_enabled = True
    service = _new_service(config=config, tmp_path=tmp_path)

    report = _as_mapping(
        service.run_evolution_offhours(
            symbols=["600000.SH", "000001.SZ"],
            timestamp=datetime.fromisoformat("2026-03-03T20:40:00"),
            dry_run=True,
            source_trace_id="service-loader-market-proxy-fallback",
        )
    )

    loader_inputs = _as_mapping(report["loader_inputs"])
    m7_loader = _as_mapping(loader_inputs["m7"])
    assert m7_loader["source"] == "generated"
    generated_path = tmp_path / str(m7_loader["path"])
    assert generated_path.exists() is True
    loaded_records = load_m7_news_records(generated_path)
    assert len(loaded_records) > 0
    assert any(bool(item.get("proxy_generated", False)) for item in loaded_records)


def test_evolution_window_report_allows_live_mode_when_dry_run_requirement_disabled(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.app.mode = "production"
    config.evolution.dry_run_policy = "auto"
    config.evolution.validation_require_dry_run = False
    service = _new_service(config=config, tmp_path=tmp_path)

    report1 = _as_mapping(
        service.run_evolution_offhours(
            records=_valid_records(),
            timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
            dry_run=None,
            source_trace_id="window-live-1",
        )
    )
    report2 = _as_mapping(
        service.run_evolution_offhours(
            records=_valid_records(),
            timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
            dry_run=None,
            source_trace_id="window-live-2",
        )
    )
    assert _as_bool(report1["dry_run"]) is False
    assert _as_bool(report2["dry_run"]) is False

    window = _as_mapping(
        service.evolution_window_report(
            days=10,
            min_runs=2,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
        )
    )
    checks = _as_mapping_list(window["checks"])
    dry_run_check = next(item for item in checks if item["name"] == "dry_run_consistency")
    assert dry_run_check["status"] == "pass"
    assert window["overall"] in {"pass", "pass_with_warnings"}
