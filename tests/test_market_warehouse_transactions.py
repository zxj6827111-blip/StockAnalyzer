from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from stock_analyzer.data.market_warehouse import MarketWarehouse


class _FailingInsertConnection:
    def __init__(self, inner: Any, token: str) -> None:
        self._inner = inner
        self._token = token.upper()

    def execute(self, query: str, *args: object, **kwargs: object) -> Any:
        normalized = " ".join(str(query).upper().split())
        if self._token in normalized:
            raise RuntimeError("simulated insert failure")
        return self._inner.execute(query, *args, **kwargs)

    def register(self, *args: object, **kwargs: object) -> Any:
        return self._inner.register(*args, **kwargs)

    def unregister(self, *args: object, **kwargs: object) -> Any:
        return self._inner.unregister(*args, **kwargs)


def _warehouse(tmp_path: Path) -> MarketWarehouse:
    return MarketWarehouse(
        db_path=tmp_path / "market.duckdb",
        package_root=tmp_path / "package",
        package_writes_enabled=False,
    )


def _fail_insert(
    warehouse: MarketWarehouse,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    real_connect = warehouse._connect_write

    @contextmanager
    def _connect() -> Iterator[_FailingInsertConnection]:
        with real_connect() as connection:
            yield _FailingInsertConnection(connection, token)

    monkeypatch.setattr(warehouse, "_connect_write", _connect)


def _intraday_frame(session_return: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "minute_count": [240.0],
            "session_return": [session_return],
            "session_range_pct": [0.03],
            "realized_vol": [0.02],
            "vwap_gap": [0.001],
            "am_return": [0.003],
            "pm_return": [0.007],
            "am_pm_diff": [0.004],
            "last30_return": [0.002],
            "last30_volume_share": [0.15],
            "positive_bar_ratio": [0.55],
            "close_position": [0.7],
        },
        index=pd.to_datetime(["2026-08-18"]),
    )


def test_security_status_upsert_rolls_back_delete_when_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _warehouse(tmp_path)
    original = pd.DataFrame(
        {
            "effective_from": ["2026-08-18"],
            "effective_to": [None],
            "status_type": ["listing"],
            "status_value": ["listed"],
            "source": ["fixture"],
        }
    )
    warehouse.upsert_security_status(symbol="600000", frame=original)
    replacement = original.copy()
    replacement["status_value"] = "changed"
    _fail_insert(warehouse, monkeypatch, "INSERT OR REPLACE INTO SECURITY_STATUS")

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        warehouse.upsert_security_status(symbol="600000", frame=replacement)

    stored = warehouse.fetch_security_status(symbol="600000")
    assert stored["status_value"].tolist() == ["listed"]


def test_identity_mapping_upsert_rolls_back_delete_when_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _warehouse(tmp_path)
    original = pd.DataFrame(
        {
            "historical_symbol": ["830001"],
            "canonical_symbol": ["920001"],
            "effective_from": ["2026-08-18"],
            "effective_to": [None],
            "source": ["fixture"],
        }
    )
    warehouse.upsert_security_identity_mapping(frame=original)
    replacement = original.copy()
    replacement["canonical_symbol"] = "920002"
    _fail_insert(
        warehouse,
        monkeypatch,
        "INSERT OR REPLACE INTO SECURITY_IDENTITY_MAPPING",
    )

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        warehouse.upsert_security_identity_mapping(frame=replacement)

    stored = warehouse.fetch_security_identity_mapping(historical_symbol="830001")
    assert stored["canonical_symbol"].tolist() == ["920001"]


def test_intraday_replace_rolls_back_delete_when_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _warehouse(tmp_path)
    warehouse.replace_intraday_summary(
        symbol="600000",
        interval="1m",
        frame=_intraday_frame(0.01),
    )
    _fail_insert(warehouse, monkeypatch, "INSERT INTO INTRADAY_SUMMARY_1M")

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        warehouse.replace_intraday_summary(
            symbol="600000",
            interval="1m",
            frame=_intraday_frame(0.02),
        )

    stored = warehouse.fetch_intraday_summary(
        symbol="600000",
        interval="1m",
        lookback_days=5,
    )
    assert float(stored.iloc[-1]["session_return"]) == pytest.approx(0.01)
