"""AKShare financial snapshot adapter with resilient multi-source parsing."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from time import time
from typing import cast

import pandas as pd


@dataclass(slots=True)
class FinancialSnapshot:
    name: str = ""
    is_st: bool = False
    is_delisting_risk: bool = False
    roe: float | None = None
    debt_ratio: float | None = None
    source: str = "fallback"
    sources: list[str] = field(default_factory=list)
    latest_report_date: str = ""
    financial_data_complete: bool = False
    missing_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "is_st": self.is_st,
            "is_delisting_risk": self.is_delisting_risk,
            "roe": self.roe,
            "debt_ratio": self.debt_ratio,
            "source": self.source,
            "sources": list(self.sources),
            "latest_report_date": self.latest_report_date,
            "financial_data_complete": self.financial_data_complete,
            "missing_fields": list(self.missing_fields),
        }


@dataclass(frozen=True, slots=True)
class _MetricSample:
    source: str
    report_date: str
    roe: float | None
    debt_ratio: float | None


class AkshareFinancialAdapter:
    """Load financial quality fields used by financial risk filter."""

    def __init__(self, cache_ttl_sec: int = 6 * 3600, ak_module: object | None = None) -> None:
        self._cache_ttl_sec = max(60, int(cache_ttl_sec))
        self._snapshot_cache: dict[str, tuple[float, FinancialSnapshot]] = {}
        self._name_index: tuple[float, dict[str, str]] = (0.0, {})
        self._ak_module = ak_module

    def fetch_snapshot(self, symbol: str) -> dict[str, object]:
        normalized_symbol = symbol.strip()
        now = time()
        cached = self._snapshot_cache.get(normalized_symbol)
        if cached is not None:
            ts, snapshot = cached
            if now - ts <= self._cache_ttl_sec:
                return snapshot.to_dict()

        snapshot = FinancialSnapshot()
        ak = self._resolve_ak_module()
        if ak is None:
            snapshot.missing_fields = ["roe", "debt_ratio"]
            self._snapshot_cache[normalized_symbol] = (now, snapshot)
            return snapshot.to_dict()

        name = self._lookup_name(ak=ak, symbol=normalized_symbol)
        snapshot.name = name
        snapshot.is_st = _contains_st(name)
        snapshot.is_delisting_risk = _contains_delisting_risk(name)

        roe, debt_ratio, source, sources, report_date = self._fetch_financial_metrics(
            ak=ak,
            symbol=normalized_symbol,
        )
        snapshot.roe = roe
        snapshot.debt_ratio = debt_ratio
        snapshot.source = source
        snapshot.sources = sources
        snapshot.latest_report_date = report_date
        snapshot.financial_data_complete = roe is not None and debt_ratio is not None
        missing: list[str] = []
        if roe is None:
            missing.append("roe")
        if debt_ratio is None:
            missing.append("debt_ratio")
        snapshot.missing_fields = missing

        self._snapshot_cache[normalized_symbol] = (now, snapshot)
        return snapshot.to_dict()

    def _resolve_ak_module(self) -> object | None:
        if self._ak_module is not None:
            return self._ak_module
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except Exception:
            return None
        return cast(object, ak)

    def _lookup_name(self, ak: object, symbol: str) -> str:
        now = time()
        ts, mapping = self._name_index
        if mapping and now - ts <= self._cache_ttl_sec:
            return mapping.get(symbol, "")

        index: dict[str, str] = {}
        func = getattr(ak, "stock_info_a_code_name", None)
        if callable(func):
            try:
                frame = func()
            except Exception:
                frame = None
            parsed = _parse_name_mapping(frame)
            if parsed:
                index.update(parsed)

        self._name_index = (now, index)
        return index.get(symbol, "")

    def _fetch_financial_metrics(
        self,
        ak: object,
        symbol: str,
    ) -> tuple[float | None, float | None, str, list[str], str]:
        candidates = (
            "stock_financial_analysis_indicator",
            "stock_financial_abstract",
            "stock_financial_abstract_ths",
            "stock_yjbb_em",
            "stock_yjkb_em",
        )
        samples: list[_MetricSample] = []
        for func_name in candidates:
            frame = _call_dataframe_function(ak=ak, func_name=func_name, symbol=symbol)
            if frame is None or frame.empty:
                continue
            roe = _extract_numeric_metric(frame=frame, keywords=("净资产收益率", "ROE", "roe"))
            debt_ratio = _extract_numeric_metric(
                frame=frame,
                keywords=("资产负债率", "debt", "负债率"),
            )
            if roe is None and debt_ratio is None:
                continue
            report_date = _extract_latest_report_date(frame=frame)
            samples.append(
                _MetricSample(
                    source=func_name,
                    report_date=report_date,
                    roe=roe,
                    debt_ratio=debt_ratio,
                )
            )

        if not samples:
            return None, None, "fallback", [], ""

        ordered = sorted(samples, key=_sample_sort_key, reverse=True)
        roe = _select_metric_value(samples=ordered, metric="roe")
        debt_ratio = _select_metric_value(samples=ordered, metric="debt_ratio")
        sources = list(dict.fromkeys(sample.source for sample in ordered))
        source = ",".join(sources) if sources else "fallback"
        latest_report_date = ordered[0].report_date
        return roe, debt_ratio, source, sources, latest_report_date


def _sample_sort_key(sample: _MetricSample) -> tuple[int, str]:
    rank = _date_rank(sample.report_date)
    return rank, sample.source


def _select_metric_value(samples: list[_MetricSample], metric: str) -> float | None:
    for sample in samples:
        value = sample.roe if metric == "roe" else sample.debt_ratio
        if value is not None:
            return value
    return None


def _parse_name_mapping(frame: object) -> dict[str, str]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {}
    code_col = _pick_column(frame.columns, ("code", "代码", "symbol"))
    name_col = _pick_column(frame.columns, ("name", "名称", "简称"))
    if code_col is None or name_col is None:
        return {}
    index: dict[str, str] = {}
    for _, row in frame.iterrows():
        code = str(row.get(code_col, "")).strip()
        name = str(row.get(name_col, "")).strip()
        if code and name:
            index[code] = name
    return index


def _call_dataframe_function(ak: object, func_name: str, symbol: str) -> pd.DataFrame | None:
    func = getattr(ak, func_name, None)
    if not callable(func):
        return None
    for kwargs in (
        {"symbol": symbol},
        {"code": symbol},
        {"stock": symbol},
        {"ts_code": symbol},
    ):
        try:
            payload = func(**kwargs)
        except TypeError:
            continue
        except Exception:
            return None
        if isinstance(payload, pd.DataFrame):
            return payload
    try:
        payload = func(symbol)
    except Exception:
        return None
    if isinstance(payload, pd.DataFrame):
        return payload
    return None


def _extract_numeric_metric(frame: pd.DataFrame, keywords: tuple[str, ...]) -> float | None:
    # Pattern 1: metric is a dedicated column.
    for column in frame.columns:
        if _column_matches(column, keywords):
            numeric = pd.to_numeric(frame[column], errors="coerce").dropna()
            if not numeric.empty:
                value = _sanitize_numeric(float(numeric.iloc[-1]))
                if value is None:
                    continue
                if _looks_like_percent_column(column) and abs(value) > 1.0:
                    value /= 100.0
                return value

    # Pattern 2: first column stores metric names, following columns store values.
    metric_col = frame.columns[0]
    metric_text = frame[metric_col].astype(str)
    for keyword in keywords:
        mask = metric_text.str.contains(keyword, case=False, regex=False)
        if not mask.any():
            continue
        subset = frame.loc[mask]
        for _, row in subset.iterrows():
            for value in row.iloc[1:]:
                parsed = _optional_float(value)
                if parsed is not None:
                    if _looks_like_percent_keyword(keyword) and abs(parsed) > 1.0:
                        parsed /= 100.0
                    return parsed
    return None


def _extract_latest_report_date(frame: pd.DataFrame) -> str:
    date_col = _pick_column(frame.columns, ("报告期", "report", "日期", "截止"))
    candidates: list[str] = []
    if date_col is not None:
        parsed = pd.to_datetime(frame[date_col], errors="coerce")
        for value in parsed.dropna():
            candidates.append(value.strftime("%Y-%m-%d"))
    for col in frame.columns:
        parsed_col = pd.to_datetime([str(col)], errors="coerce")
        if not parsed_col.empty and pd.notna(parsed_col[0]):
            candidates.append(parsed_col[0].strftime("%Y-%m-%d"))
    if not candidates:
        return ""
    ordered = sorted(candidates, key=_date_rank, reverse=True)
    return ordered[0]


def _column_matches(column: object, keywords: tuple[str, ...]) -> bool:
    text = str(column).strip()
    if not text:
        return False
    lowered = text.lower()
    for keyword in keywords:
        if keyword.lower() in lowered:
            return True
    return False


def _pick_column(columns: pd.Index, keywords: tuple[str, ...]) -> str | None:
    for col in columns:
        text = str(col).strip().lower()
        for keyword in keywords:
            if keyword.lower() in text:
                return str(col)
    return None


def _looks_like_percent_column(column: object) -> bool:
    text = str(column).strip()
    return "%" in text or "率" in text


def _looks_like_percent_keyword(keyword: str) -> bool:
    text = keyword.strip().lower()
    return "%" in text or "roe" in text or "率" in text


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _sanitize_numeric(float(value))
    if isinstance(value, str):
        text = value.strip().replace("%", "")
        if not text:
            return None
        try:
            return _sanitize_numeric(float(text))
        except ValueError:
            return None
    return None


def _sanitize_numeric(value: float) -> float | None:
    if not math.isfinite(value):
        return None
    return value


def _date_rank(value: str) -> int:
    text = value.strip()
    if not text:
        return -1
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m", "%Y/%m", "%Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return int(dt.strftime("%Y%m%d"))
        except ValueError:
            continue
    parsed = pd.to_datetime([text], errors="coerce")
    if not parsed.empty and pd.notna(parsed[0]):
        return int(parsed[0].strftime("%Y%m%d"))
    return -1


def _contains_st(name: str) -> bool:
    text = name.strip().upper()
    return text.startswith("ST") or "*ST" in text


def _contains_delisting_risk(name: str) -> bool:
    text = name.strip().upper()
    return "退" in text or "DELIST" in text or "退市" in text
