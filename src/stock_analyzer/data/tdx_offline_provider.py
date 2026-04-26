"""Local offline provider that loads prebuilt symbol bars from disk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from stock_analyzer.data.intraday_summary import load_intraday_summary
from stock_analyzer.data.provider import DataSourceError

_REQUIRED_BASE_COLUMNS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
}

_SELECTED_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "suspended",
    "name",
    "is_st",
    "is_delisting_risk",
    "roe",
    "debt_ratio",
    "financial_data_complete",
    "financial_missing_fields",
    "financial_source",
    "financial_report_date",
    "holder_count",
    "block_trade_net",
    "financing_balance",
    "margin_financing_balance",
    "northbound_net",
    "dragon_tiger_flag",
    "background_data_source",
    "background_data_complete",
    "board",
]

_NUMERIC_COLUMNS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "roe",
    "debt_ratio",
    "holder_count",
    "block_trade_net",
    "financing_balance",
    "margin_financing_balance",
    "northbound_net",
    "dragon_tiger_flag",
}

_BOOLEAN_COLUMNS = {
    "suspended",
    "is_st",
    "is_delisting_risk",
    "financial_data_complete",
    "background_data_complete",
}

_DEFAULT_VALUES: dict[str, str | bool | float] = {
    "suspended": False,
    "name": "",
    "is_st": False,
    "is_delisting_risk": False,
    "roe": 0.08,
    "debt_ratio": 0.55,
    "financial_data_complete": True,
    "financial_missing_fields": "",
    "financial_source": "tdx_offline",
    "financial_report_date": "",
    "holder_count": 60_000.0,
    "block_trade_net": 0.0,
    "financing_balance": 2_500_000_000.0,
    "margin_financing_balance": 2_500_000_000.0,
    "northbound_net": 0.0,
    "dragon_tiger_flag": 0.0,
    "background_data_source": "tdx_offline",
    "background_data_complete": True,
    "board": "main",
}

_NUMERIC_DEFAULT_VALUES: dict[str, float] = {
    "roe": 0.08,
    "debt_ratio": 0.55,
    "holder_count": 60_000.0,
    "block_trade_net": 0.0,
    "financing_balance": 2_500_000_000.0,
    "margin_financing_balance": 2_500_000_000.0,
    "northbound_net": 0.0,
    "dragon_tiger_flag": 0.0,
}


@dataclass(slots=True)
class TdxOfflineProvider:
    """Load bars from `bars/{symbol}.csv` or `bars/{symbol}.parquet`."""

    data_root: str
    _root: Path = field(init=False)
    _cache: dict[str, tuple[int, pd.DataFrame]] = field(default_factory=dict, init=False)
    _intraday_cache: dict[str, tuple[int, pd.DataFrame]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._root = Path(self.data_root).expanduser()
        if not self._root.exists():
            raise DataSourceError(f"tdx offline data root does not exist: {self._root}")

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not normalized_symbol:
            raise DataSourceError("symbol is empty")

        frame = self._load_symbol_frame(normalized_symbol)
        if frame.empty:
            raise DataSourceError(f"tdx offline bars are empty for {normalized_symbol}")
        if end_date is not None:
            frame = frame.loc[pd.DatetimeIndex(frame.index) <= pd.Timestamp(end_date)]
        return frame.tail(max(1, int(lookback_days))).copy()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        normalized_symbol = _normalize_symbol(symbol)
        if not normalized_symbol:
            return pd.DataFrame()
        interval_key = interval.strip().lower()
        cache_key = f"{interval_key}:{normalized_symbol}"
        path = _resolve_intraday_summary_path(self._root, normalized_symbol, interval_key)
        if path is None:
            return pd.DataFrame()
        cache_item = self._intraday_cache.get(cache_key)
        stat = path.stat()
        mtime_ns = int(stat.st_mtime_ns)
        if cache_item is not None and cache_item[0] == mtime_ns:
            return cache_item[1].tail(max(1, int(lookback_days))).copy()
        frame = load_intraday_summary(
            root=self._root,
            symbol=normalized_symbol,
            interval=interval_key,
            lookback_days=max(1, int(lookback_days)),
        )
        self._intraday_cache[cache_key] = (mtime_ns, frame)
        return frame.copy()

    def _load_symbol_frame(self, symbol: str) -> pd.DataFrame:
        symbol_path = _resolve_symbol_path(self._root, symbol)
        if symbol_path is None:
            raise DataSourceError(f"tdx offline file not found for symbol {symbol}")

        cache_item = self._cache.get(symbol)
        stat = symbol_path.stat()
        mtime_ns = int(stat.st_mtime_ns)
        if cache_item is not None and cache_item[0] == mtime_ns:
            return cache_item[1]

        if symbol_path.suffix.lower() == ".parquet":
            frame = pd.read_parquet(symbol_path)
        else:
            frame = pd.read_csv(symbol_path)

        normalized = _normalize_frame(frame=frame, symbol=symbol)
        self._cache[symbol] = (mtime_ns, normalized)
        return normalized


def _resolve_symbol_path(root: Path, symbol: str) -> Path | None:
    candidates = (
        root / "bars" / f"{symbol}.parquet",
        root / "bars" / f"{symbol}.csv",
        root / "bars" / f"{symbol}.csv.gz",
        root / f"{symbol}.parquet",
        root / f"{symbol}.csv",
        root / f"{symbol}.csv.gz",
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _resolve_intraday_summary_path(root: Path, symbol: str, interval: str) -> Path | None:
    candidates = (
        root / "intraday_summary" / interval / f"{symbol}.csv.gz",
        root / "intraday_summary" / interval / f"{symbol}.csv",
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _normalize_frame(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if frame.empty:
        return frame

    normalized = frame.copy()
    if "date" in normalized.columns:
        index = pd.DatetimeIndex(pd.to_datetime(normalized["date"], errors="coerce"))
        normalized = normalized.drop(columns=["date"])
    else:
        index = pd.DatetimeIndex(pd.to_datetime(normalized.index, errors="coerce"))
    normalized.index = index
    normalized = normalized[normalized.index.notna()]
    normalized.index.name = "date"
    normalized = normalized.sort_index()
    normalized = normalized[~normalized.index.duplicated(keep="last")]

    missing_required = _REQUIRED_BASE_COLUMNS - set(normalized.columns)
    if missing_required:
        raise DataSourceError(
            f"tdx offline file missing required columns for {symbol}: {sorted(missing_required)}"
        )

    for col in _DEFAULT_VALUES:
        if col not in normalized.columns:
            default_value = _DEFAULT_VALUES[col]
            if col == "board":
                default_value = _infer_board(symbol)
            normalized[col] = default_value

    for col in _NUMERIC_COLUMNS:
        if col in normalized.columns:
            normalized[col] = pd.to_numeric(normalized[col], errors="coerce")
            if col in _NUMERIC_DEFAULT_VALUES:
                normalized[col] = normalized[col].fillna(_NUMERIC_DEFAULT_VALUES[col])

    for col in _BOOLEAN_COLUMNS:
        if col in normalized.columns:
            raw = normalized[col]
            if raw.dtype == bool:
                normalized[col] = raw
            else:
                text = raw.astype(str).str.strip().str.lower()
                normalized[col] = text.isin({"1", "true", "yes", "y"})

    if "name" in normalized.columns:
        name_series = normalized["name"].map(_clean_text_value)
        normalized["name"] = name_series.replace("", pd.NA).ffill().fillna("")

    for col in (
        "financial_missing_fields",
        "financial_source",
        "financial_report_date",
        "board",
    ):
        if col in normalized.columns:
            normalized[col] = normalized[col].map(_clean_text_value)

    selected = normalized[_SELECTED_COLUMNS].copy()
    selected.index.name = "date"
    return selected


def _clean_text_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return "" if text.lower() in {"nan", "null", "none", "undefined"} else text


def _normalize_symbol(symbol: str) -> str:
    text = symbol.strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def _infer_board(symbol: str) -> str:
    text = symbol.strip()
    if text.startswith("688"):
        return "star"
    if text.startswith("300") or text.startswith("301"):
        return "gem"
    if text.startswith("8") or text.startswith("4"):
        return "bj"
    return "main"
