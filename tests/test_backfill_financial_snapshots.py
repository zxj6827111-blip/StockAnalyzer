"""Tests for scripts/backfill_financial_snapshots.py.

The CLI wiring is exercised with a fake provider/warehouse config so no
network, Tushare token or real config file is needed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
_BACKFILL_PATH = ROOT / "scripts" / "backfill_financial_snapshots.py"


def _load_backfill() -> object:
    module_name = "backfill_financial_snapshots"
    spec = importlib.util.spec_from_file_location(module_name, _BACKFILL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def backfill() -> object:
    return _load_backfill()


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail_symbols: set[str] = set()
        self.empty_symbols: set[str] = set()

    def list_symbols(self) -> list[str]:
        return ["600000", "000001", "300750"]

    def fetch_fina_indicator(
        self, *, symbol: str, end_date: object, start_date: object
    ) -> pd.DataFrame:
        self.calls.append((symbol, str(end_date), str(start_date)))
        if symbol in self.fail_symbols:
            raise RuntimeError(f"boom {symbol}")
        if symbol in self.empty_symbols:
            return pd.DataFrame()
        raw = pd.DataFrame(
            {
                "ts_code": [symbol],
                "ann_date": ["20250320"],
                "end_date": ["20241231"],
                "roe": [0.10],
                "debt_to_assets": [0.40],
                "update_flag": [0],
            }
        )
        from stock_analyzer.data.financial_pit import normalize_fina_indicator_rows

        return normalize_fina_indicator_rows(raw, symbol=symbol)


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        cache=SimpleNamespace(enabled=False, ttl_sec=0),
        data_source=SimpleNamespace(request_interval_sec=0.0),
        market_warehouse=SimpleNamespace(
            online_daily_primary="tushare",
            online_daily_backup="",
            request_interval_sec=0.0,
            tushare_token="",
        ),
    )


def _run_backfill(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    args: list[str],
    provider: object,
) -> tuple[int, str]:
    def _fake_load_config(_path: str) -> object:
        return _fake_config()

    def _fake_build_runtime_provider(runtime_data_source: object, **kwargs: object) -> object:
        _ = runtime_data_source, kwargs
        return provider

    def _fake_build_online_provider(self: object) -> object:
        _ = self
        return provider

    class _FakeService:
        def _resolve_runtime_data_source_config(self, config: object) -> SimpleNamespace:
            _ = config
            return SimpleNamespace(
                warehouse_db_path=str(root / "delta" / "market_delta.duckdb"),
                local_data_root=str(root / "delta" / "package"),
            )

    monkeypatch.setattr("stock_analyzer.config.load_config", _fake_load_config)
    monkeypatch.setattr(
        "stock_analyzer.data.provider_factory.build_runtime_provider",
        _fake_build_runtime_provider,
    )
    from stock_analyzer.runtime.services.market_sync_service import RuntimeMarketSyncService

    monkeypatch.setattr(
        RuntimeMarketSyncService,
        "_build_market_warehouse_online_provider",
        _fake_build_online_provider,
    )
    monkeypatch.setattr("stock_analyzer.runtime.service.StockAnalyzerService", _FakeService)

    module = _load_backfill()
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        exit_code = module._main(args)
    return int(exit_code), stream.getvalue()


def _summary_json(output: str) -> dict[str, object]:
    start = output.index("{")
    return json.loads(output[start:])


def test_backfill_dry_run_fetches_nothing_and_writes_no_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _FakeProvider()
    checkpoint = tmp_path / "checkpoint.json"
    exit_code, output = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--checkpoint",
            str(checkpoint),
            "--dry-run",
        ],
    )

    assert exit_code == 0
    assert provider.calls == []
    assert not checkpoint.exists()
    summary = _summary_json(output)
    assert summary["dry_run"] is True
    assert summary["ok"] == 3
    assert "dry-run: would backfill 000001 (1/3)" in output


def test_backfill_writes_snapshots_then_resume_skips_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from stock_analyzer.data.market_warehouse import MarketWarehouse

    provider = _FakeProvider()
    checkpoint = tmp_path / "checkpoint.json"
    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--checkpoint",
            str(checkpoint),
        ],
    )

    assert exit_code == 0
    assert len(provider.calls) == 3
    assert checkpoint.exists()
    stored = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(stored) == 3
    assert any(key.endswith("|600000") for key in stored)

    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "market_delta.duckdb",
        package_root=tmp_path / "delta" / "package",
    )
    snapshots = warehouse.fetch_financial_snapshots(symbol="600000")
    assert len(snapshots) == 1
    assert float(snapshots.iloc[0]["roe"]) == pytest.approx(0.001)

    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--checkpoint",
            str(checkpoint),
            "--resume",
        ],
    )
    assert exit_code == 0
    assert len(provider.calls) == 3


def test_backfill_strict_returns_1_on_failure_and_records_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _FakeProvider()
    provider.fail_symbols = {"300750"}
    exit_code, output = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--strict",
        ],
    )

    assert exit_code == 1
    summary = _summary_json(output)
    assert summary["ok"] == 2
    assert summary["failed"] == 1
    assert any("300750" in failure for failure in summary["failures"])


def test_backfill_missing_provider_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _NoIndicatorProvider:
        def list_symbols(self) -> list[str]:
            return ["600000"]

    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=_NoIndicatorProvider(),
        args=["--config", str(tmp_path / "config.yaml")],
    )
    assert exit_code == 2


def test_backfill_empty_universe_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _EmptyProvider(_FakeProvider):
        def list_symbols(self) -> list[str]:
            return []

    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=_EmptyProvider(),
        args=["--config", str(tmp_path / "config.yaml")],
    )
    assert exit_code == 2


def test_backfill_failures_exit_1_even_without_strict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _FakeProvider()
    provider.fail_symbols = {"300750"}
    exit_code, output = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=["--config", str(tmp_path / "config.yaml")],
    )

    assert exit_code == 1
    summary = _summary_json(output)
    assert summary["ok"] == 2
    assert summary["failed"] == 1


def test_backfill_empty_returns_1_by_default_and_allow_empty_opts_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _FakeProvider()
    provider.empty_symbols = {"000001"}

    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=["--config", str(tmp_path / "config.yaml")],
    )
    assert exit_code == 1

    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--allow-empty",
        ],
    )
    assert exit_code == 0

    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--allow-empty",
            "--strict",
        ],
    )
    assert exit_code == 1


def test_backfill_empty_symbols_are_not_checkpointed_and_retried_on_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _FakeProvider()
    provider.empty_symbols = {"300750"}
    checkpoint = tmp_path / "checkpoint.json"
    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--checkpoint",
            str(checkpoint),
            "--allow-empty",
        ],
    )
    assert exit_code == 0
    stored = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(stored) == 2
    assert not any(key.endswith("|300750") for key in stored)

    provider.empty_symbols = set()
    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--checkpoint",
            str(checkpoint),
            "--resume",
        ],
    )
    assert exit_code == 0
    assert len(provider.calls) == 4


def test_backfill_normalizes_suffix_symbols_from_universe_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _FakeProvider()
    universe_file = tmp_path / "universe.txt"
    universe_file.write_text("600000.SH\n000001.SZ\n300750.BJ\n", encoding="utf-8")
    exit_code, output = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--universe-file",
            str(universe_file),
            "--dry-run",
        ],
    )
    assert exit_code == 0
    assert "dry-run: would backfill 000001 (1/3)" in output
    assert "dry-run: would backfill 300750 (2/3)" in output
    assert "dry-run: would backfill 600000 (3/3)" in output
    assert "000.SH" not in output


def test_backfill_rate_limits_after_failures_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import time as time_module

    provider = _FakeProvider()
    provider.fail_symbols = {"300750"}
    sleeps: list[float] = []
    monkeypatch.setattr(time_module, "sleep", lambda secs: sleeps.append(secs))
    exit_code, _ = _run_backfill(
        monkeypatch,
        root=tmp_path,
        provider=provider,
        args=[
            "--config",
            str(tmp_path / "config.yaml"),
            "--request-interval-sec",
            "0.01",
        ],
    )
    assert exit_code == 1
    assert len(sleeps) == 2


def test_backfill_checkpoint_key_includes_start_date_and_provider(
    backfill: object,
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "checkpoint.json"
    marker = backfill._marker_key(  # type: ignore[attr-defined]
        end_date=date(2026, 8, 1),
        start_date=date(2021, 1, 1),
        provider="tushare",
        symbol="600000",
    )
    backfill._save_checkpoint(path, {}, marker)  # type: ignore[attr-defined]
    loaded = backfill._load_checkpoint(path)  # type: ignore[attr-defined]
    assert "v1|2026-08-01|2021-01-01|tushare|600000" in loaded


def test_backfill_checkpoint_roundtrip(
    backfill: object,
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "checkpoint.json"
    backfill._save_checkpoint(path, {}, "2026-08-01|600000")  # type: ignore[attr-defined]
    loaded = backfill._load_checkpoint(path)  # type: ignore[attr-defined]
    assert "2026-08-01|600000" in loaded
    assert backfill._load_checkpoint(tmp_path / "missing.json") == {}  # type: ignore[attr-defined]
