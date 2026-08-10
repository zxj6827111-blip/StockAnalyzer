from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data import tushare_provider as tushare_provider_module
from stock_analyzer.data.delisted_symbols import (
    fetch_delisted_symbols_from_provider,
    load_delisted_symbols,
    persist_delisted_symbols,
)
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import TushareProvider
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [str(item) for item in value]
    assert len(items) == len(value)
    return items


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


def _write_payload(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.state_persist_path = str(tmp_path / "runtime_state.json")
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.evolution.auto_run = False
    config.acceptance.auto_run = False
    config.cloud_backup.enabled = False
    config.training.artifact_path = str(
        Path(tempfile.gettempdir())
        / f"stock_analyzer_delisted_symbols_{time.time_ns()}"
        / "nonexistent_test_model.json"
    )
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.idle_queue.universe_cache_path = str(tmp_path / "universe_cache.json")
    config.idle_queue.delisted_symbols_path = str(tmp_path / "delisted.json")
    config.idle_queue.universe_cache_max_age_hours = 24
    return config


def test_load_delisted_symbols_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_delisted_symbols(tmp_path / "does_not_exist.json") == {}


def test_load_delisted_symbols_corrupt_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "delisted.json"
    path.write_text("{not-json", encoding="utf-8")
    assert load_delisted_symbols(path) == {}


def test_load_delisted_symbols_wrong_root_type_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "delisted.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_delisted_symbols(path) == {}


def test_load_delisted_symbols_dict_shape(tmp_path: Path) -> None:
    path = tmp_path / "delisted.json"
    _write_payload(
        path,
        {
            "generated_at": "2026-08-10T12:00:00",
            "source": "tushare",
            "count": 2,
            "symbols": {
                "600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"},
                "000003": {"name": "PT金田A", "delist_date": "20020614"},
                "bad-symbol": {"name": "x"},
                "1234567": {"name": "y"},
            },
        },
    )
    loaded = load_delisted_symbols(path)
    assert loaded == {
        "600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"},
        "000003": {"name": "PT金田A", "delist_date": "2002-06-14"},
    }


def test_load_delisted_symbols_list_shape(tmp_path: Path) -> None:
    path = tmp_path / "delisted.json"
    _write_payload(
        path,
        {
            "symbols": [
                {"symbol": "600002.SH", "name": "齐鲁石化", "delist_date": "20200528"},
                {"symbol": "000003", "name": "PT金田A"},
                "not-a-dict",
            ]
        },
    )
    loaded = load_delisted_symbols(path)
    assert loaded == {
        "600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"},
        "000003": {"name": "PT金田A"},
    }


def test_persist_delisted_symbols_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "delisted.json"
    persist_delisted_symbols(
        path,
        {"600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"}},
        source="tushare",
    )
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == "tushare"
    assert payload["count"] == 1
    assert load_delisted_symbols(path) == {
        "600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"}
    }


class _StubDelistedProvider:
    def __init__(self, frame: pd.DataFrame | None = None, error: Exception | None = None) -> None:
        self._frame = frame
        self._error = error

    def fetch_delisted_stock_basic(self) -> pd.DataFrame:
        if self._error is not None:
            raise self._error
        return self._frame if self._frame is not None else pd.DataFrame()


class _NotDelistedCapable:
    pass


def test_fetch_delisted_symbols_from_provider_parses_frame(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "ts_code": ["600002.SH", "000003.SZ", "bad", "1234567.SZ"],
            "name": ["齐鲁石化", "PT金田A", "x", "y"],
            "delist_date": ["20200528", "20020614", "20200101", "20200102"],
        }
    )
    persist_path = tmp_path / "delisted.json"
    fetched = fetch_delisted_symbols_from_provider(
        _StubDelistedProvider(frame),
        persist_path=persist_path,
    )
    assert fetched == {
        "600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"},
        "000003": {"name": "PT金田A", "delist_date": "2002-06-14"},
    }
    assert persist_path.exists()
    assert load_delisted_symbols(persist_path) == fetched


def test_fetch_delisted_symbols_from_provider_missing_capability_returns_empty(
    tmp_path: Path,
) -> None:
    persist_path = tmp_path / "delisted.json"
    assert fetch_delisted_symbols_from_provider(
        _NotDelistedCapable(),
        persist_path=persist_path,
    ) == {}
    assert not persist_path.exists()


def test_fetch_delisted_symbols_from_provider_error_returns_empty(tmp_path: Path) -> None:
    persist_path = tmp_path / "delisted.json"
    assert fetch_delisted_symbols_from_provider(
        _StubDelistedProvider(error=DataSourceError("tushare token missing")),
        persist_path=persist_path,
    ) == {}
    assert not persist_path.exists()


def test_fetch_delisted_symbols_from_provider_empty_frame_returns_empty(tmp_path: Path) -> None:
    assert fetch_delisted_symbols_from_provider(_StubDelistedProvider(pd.DataFrame())) == {}


class _RecordingStockBasicPro:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        self.stock_basic_calls: list[dict[str, str]] = []

    def stock_basic(
        self,
        *,
        ts_code: str = "",
        list_status: str = "",
        fields: str = "",
    ) -> object:
        self.stock_basic_calls.append({"ts_code": ts_code, "list_status": list_status})
        return self._frame

    def daily(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def daily_basic(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object:
        raise NotImplementedError

    def adj_factor(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def trade_cal(
        self,
        *,
        exchange: str = "",
        start_date: str = "",
        end_date: str = "",
        is_open: str = "",
    ) -> object:
        raise NotImplementedError

    def fina_indicator(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object:
        raise NotImplementedError

    def stk_limit(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def suspend_d(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def margin_detail(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def moneyflow(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def hk_hold(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def top_list(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def top_inst(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def block_trade(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError

    def index_daily(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        raise NotImplementedError


def test_tushare_provider_fetch_delisted_stock_basic_passes_list_status() -> None:
    frame = pd.DataFrame(
        {
            "ts_code": ["600002.SH"],
            "name": ["齐鲁石化"],
            "delist_date": ["20200528"],
        }
    )
    pro = _RecordingStockBasicPro(frame)
    provider = TushareProvider(token="dummy", pro_api=pro, retry_delay_sec=0.0)
    result = provider.fetch_delisted_stock_basic()
    assert isinstance(result, pd.DataFrame)
    assert not result.empty
    assert pro.stock_basic_calls == [{"ts_code": "", "list_status": "D"}]


def test_tushare_provider_fetch_delisted_stock_basic_missing_token_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tushare_provider_module, "_resolve_tushare_token", lambda: "")
    provider = TushareProvider(token="")
    with pytest.raises(DataSourceError, match="token missing"):
        provider.fetch_delisted_stock_basic()


def test_universe_excludes_delisted_symbols_from_spot_source(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "akshare"
    config.data_source.local_data_root = ""
    config.market_warehouse.package_root = ""
    service = StockAnalyzerService(config=config)

    def _raise_akshare() -> list[str]:
        raise RuntimeError("akshare boom")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_akshare)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_akshare)
    _patch_attr(
        service,
        "_fetch_a_share_universe_from_efinance",
        lambda: ["600000", "000001", "600002"],
    )
    _patch_attr(service, "_delisted_symbol_map", lambda: {"600002": {"name": "齐鲁石化"}})

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=2))
    symbols = _as_text_list(resolved.get("symbols", []))
    assert resolved["source"] == "efinance_spot"
    assert resolved["count"] == 2
    assert resolved["excluded_delisted_count"] == 1
    assert symbols == ["600000", "000001"]
    errors = _as_text_list(resolved.get("errors", []))
    assert any("excluded_delisted:1" in item for item in errors)


def test_universe_loads_delisted_list_from_local_file(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "akshare"
    config.data_source.local_data_root = ""
    config.market_warehouse.package_root = ""
    service = StockAnalyzerService(config=config)

    def _raise_akshare() -> list[str]:
        raise RuntimeError("akshare boom")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_akshare)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_akshare)
    _patch_attr(
        service,
        "_fetch_a_share_universe_from_efinance",
        lambda: ["600000", "000001", "600002"],
    )
    _write_payload(
        Path(config.idle_queue.delisted_symbols_path),
        {"symbols": {"600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"}}},
    )

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=2))
    symbols = _as_text_list(resolved.get("symbols", []))
    assert resolved["count"] == 2
    assert resolved["excluded_delisted_count"] == 1
    assert symbols == ["600000", "000001"]


def test_universe_exclude_delisted_disabled_keeps_symbols(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "akshare"
    config.data_source.local_data_root = ""
    config.market_warehouse.package_root = ""
    config.idle_queue.exclude_delisted = False
    service = StockAnalyzerService(config=config)

    def _raise_akshare() -> list[str]:
        raise RuntimeError("akshare boom")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_akshare)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_akshare)
    _patch_attr(
        service,
        "_fetch_a_share_universe_from_efinance",
        lambda: ["600000", "600002"],
    )
    _patch_attr(service, "_delisted_symbol_map", lambda: {"600002": {"name": "齐鲁石化"}})

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=2))
    symbols = _as_text_list(resolved.get("symbols", []))
    assert resolved["count"] == 2
    assert resolved["excluded_delisted_count"] == 0
    assert symbols == ["600000", "600002"]
    errors = _as_text_list(resolved.get("errors", []))
    assert not any("excluded_delisted" in item for item in errors)


def test_universe_delisted_filter_coexists_with_delisting_risk_named_symbols(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.data_source.primary = "akshare"
    config.data_source.local_data_root = ""
    config.market_warehouse.package_root = ""
    service = StockAnalyzerService(config=config)

    def _raise_akshare() -> list[str]:
        raise RuntimeError("akshare boom")

    _patch_attr(service, "_fetch_a_share_universe_from_akshare", _raise_akshare)
    _patch_attr(service, "_fetch_a_share_universe_catalog_from_akshare", _raise_akshare)
    _patch_attr(
        service,
        "_fetch_a_share_universe_from_efinance",
        lambda: ["600000", "000005", "600002"],
    )
    _patch_attr(service, "_delisted_symbol_map", lambda: {"600002": {"name": "齐鲁石化"}})

    resolved = _as_mapping(service._resolve_symbol_universe(min_symbols=2))
    symbols = _as_text_list(resolved.get("symbols", []))
    assert resolved["count"] == 2
    assert resolved["excluded_delisted_count"] == 1
    # 名单过滤只移除已退市 symbol；名字含"退"/"*ST" 的 is_delisting_risk
    # 过滤仍由下游 bar 级过滤（_prefilter_week5_universe_symbol_impl 等）处理。
    assert symbols == ["600000", "000005"]
