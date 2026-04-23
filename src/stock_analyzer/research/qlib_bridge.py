"""Offline Qlib research bridge sidecar."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path

import pandas as pd

_BASE_COLUMNS = {
    "symbol",
    "instrument",
    "date",
    "trade_date",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "label",
}


def run_qlib_bridge(
    *,
    records: Sequence[Mapping[str, object]],
    feature_columns: Sequence[str] | None = None,
    label_column: str = "label",
    train_ratio: float = 0.6,
    valid_ratio: float = 0.2,
) -> dict[str, object]:
    frame = _normalize_frame(records=records, label_column=label_column)
    if frame.empty:
        return {
            "status": "empty",
            "engine": "qlib_bridge",
            "backend": _qlib_backend(),
            "records": 0,
            "instrument_count": 0,
            "feature_count": 0,
            "affects_runtime": False,
            "splits": {},
            "factor_packs": {},
        }

    active_features = (
        list(feature_columns) if feature_columns is not None else _infer_feature_columns(frame)
    )
    splits = _build_splits(
        dates=sorted(frame["datetime"].astype(str).unique().tolist()),
        train_ratio=train_ratio,
        valid_ratio=valid_ratio,
    )
    factor_packs = _factor_pack_summary(frame=frame, feature_columns=active_features)
    return {
        "status": "ok",
        "engine": "qlib_bridge",
        "backend": _qlib_backend(),
        "records": int(len(frame)),
        "instrument_count": int(frame["instrument"].nunique()),
        "feature_count": len(active_features),
        "date_range": {
            "start": str(frame["datetime"].min().date()),
            "end": str(frame["datetime"].max().date()),
        },
        "affects_runtime": False,
        "splits": splits,
        "factor_packs": factor_packs,
        "feature_columns": active_features,
        "label_column_present": label_column in frame.columns,
    }


def export_qlib_bridge_bundle(
    *,
    records: Sequence[Mapping[str, object]],
    output_dir: str | Path,
    feature_columns: Sequence[str] | None = None,
    label_column: str = "label",
) -> dict[str, str]:
    frame = _normalize_frame(records=records, label_column=label_column)
    report = run_qlib_bridge(
        records=records,
        feature_columns=feature_columns,
        label_column=label_column,
    )
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = target_dir / "manifest.json"
    dataset_path = target_dir / "qlib_dataset.csv"
    calendar_path = target_dir / "calendar.txt"
    instruments_path = target_dir / "instruments.csv"

    manifest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not frame.empty:
        export_columns = [
            "instrument",
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]
        for column in (
            list(feature_columns) if feature_columns is not None else _infer_feature_columns(frame)
        ):
            if column not in export_columns and column in frame.columns:
                export_columns.append(column)
        if label_column in frame.columns and label_column not in export_columns:
            export_columns.append(label_column)
        frame.loc[:, export_columns].to_csv(dataset_path, index=False, encoding="utf-8")
        pd.Series(sorted(frame["datetime"].astype(str).unique()), name="datetime").to_csv(
            calendar_path,
            index=False,
            header=False,
            encoding="utf-8",
        )
        instrument_rows = (
            frame.groupby("instrument")["datetime"]
            .agg(["min", "max"])
            .reset_index()
            .rename(columns={"min": "start_datetime", "max": "end_datetime"})
        )
        instrument_rows.to_csv(instruments_path, index=False, encoding="utf-8")
    else:
        dataset_path.write_text("", encoding="utf-8")
        calendar_path.write_text("", encoding="utf-8")
        instruments_path.write_text("", encoding="utf-8")
    return {
        "manifest_path": str(manifest_path),
        "dataset_path": str(dataset_path),
        "calendar_path": str(calendar_path),
        "instruments_path": str(instruments_path),
    }


def _normalize_frame(*, records: Sequence[Mapping[str, object]], label_column: str) -> pd.DataFrame:
    frame = pd.DataFrame(list(records))
    if frame.empty:
        return frame
    symbol_column = (
        "symbol"
        if "symbol" in frame.columns
        else ("instrument" if "instrument" in frame.columns else "")
    )
    date_column = (
        "trade_date"
        if "trade_date" in frame.columns
        else (
            "datetime"
            if "datetime" in frame.columns
            else ("date" if "date" in frame.columns else "")
        )
    )
    if not symbol_column or not date_column:
        return pd.DataFrame()

    working = frame.copy()
    working["instrument"] = working[symbol_column].astype(str).str.strip()
    working["datetime"] = pd.to_datetime(working[date_column], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount", label_column):
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")
    for required in ("open", "high", "low", "close"):
        if required not in working.columns:
            working[required] = working.get("close", 0.0)
    if "volume" not in working.columns:
        working["volume"] = 0.0
    if "amount" not in working.columns:
        working["amount"] = 0.0
    working = working.dropna(subset=["instrument", "datetime", "close"])
    working = working[working["instrument"] != ""]
    return working.sort_values(["datetime", "instrument"]).reset_index(drop=True)


def _infer_feature_columns(frame: pd.DataFrame) -> list[str]:
    feature_columns: list[str] = []
    for column in frame.columns:
        lowered = str(column).strip().lower()
        if lowered in _BASE_COLUMNS:
            continue
        series = pd.to_numeric(frame[column], errors="coerce")
        if series.notna().sum() < 3:
            continue
        feature_columns.append(str(column))
    return feature_columns


def _build_splits(
    *, dates: Sequence[str], train_ratio: float, valid_ratio: float
) -> dict[str, dict[str, object]]:
    if not dates:
        return {}
    train_cut = min(max(int(round(len(dates) * float(train_ratio))), 1), len(dates))
    valid_cut = min(
        max(train_cut + int(round(len(dates) * float(valid_ratio))), train_cut), len(dates)
    )
    train_dates = list(dates[:train_cut])
    valid_dates = list(dates[train_cut:valid_cut])
    test_dates = list(dates[valid_cut:])
    return {
        "train": _split_payload(train_dates),
        "valid": _split_payload(valid_dates),
        "test": _split_payload(test_dates),
    }


def _split_payload(dates: Sequence[str]) -> dict[str, object]:
    if not dates:
        return {"start": "", "end": "", "sessions": 0}
    return {
        "start": str(dates[0])[:10],
        "end": str(dates[-1])[:10],
        "sessions": len(dates),
    }


def _factor_pack_summary(
    *, frame: pd.DataFrame, feature_columns: Sequence[str]
) -> dict[str, object]:
    available_columns = {str(column).strip().lower() for column in frame.columns}
    ohlcv_present = {"open", "high", "low", "close", "volume"}.issubset(available_columns)
    return {
        "alpha158_ready": ohlcv_present,
        "alpha360_ready": ohlcv_present and len(feature_columns) >= 5,
        "base_market_fields": sorted(
            column
            for column in ("open", "high", "low", "close", "volume", "amount")
            if column in available_columns
        ),
        "custom_factor_candidates": list(feature_columns),
    }


def _qlib_backend() -> str:
    try:
        import_module("qlib")
        return "qlib_native_available"
    except Exception:
        return "qlib_manifest_fallback"
