"""Point-in-time financial snapshot helpers for Tushare fina_indicator.

Semantics
---------
- financial_report_date = end_date (report period end)
- financial_as_of = ann_date (disclosure / effective date for PIT)
- financial_source = tushare_fina_indicator
- financial_trust_level = reported (only when real ann_date present)
- Tushare percentage fields (roe, debt_to_assets) convert to system decimals:
  12.5 -> 0.125 when abs(value) > 1.5
- Rows without valid ann_date never enter the PIT snapshot store
- Same end_date revisions are kept as separate events keyed by ann_date
- Same-day multi-snapshot tie-break (stable):
  1) later end_date
  2) higher update_flag
  3) later row order
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

_FINANCIAL_SOURCE = "tushare_fina_indicator"
_TRUST_REPORTED = "reported"
_REQUIRED_METRICS = ("roe", "debt_ratio")


def _to_float(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")
    if result != result:  # NaN check
        return float("nan")
    return result


def percent_to_ratio(value: object) -> float:
    """Convert Tushare percent-style fields to system decimal ratios.

    Tushare fina_indicator roe/debt_to_assets are always in percentage points
    (e.g. roe=1.2 means 1.2%, roe=12.5 means 12.5%). Always divide by 100.
    """
    raw = _to_float(value)
    if np.isnan(raw):
        return float("nan")
    return raw / 100.0


def normalize_fina_indicator_rows(
    raw: pd.DataFrame,
    *,
    symbol: str,
) -> pd.DataFrame:
    """Normalize fina_indicator API rows into warehouse snapshot rows."""
    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        return _empty_snapshot_frame()

    frame = raw.copy()
    colmap = {str(c).strip().lower(): c for c in frame.columns}
    end_col = colmap.get("end_date")
    ann_col = colmap.get("ann_date")
    if end_col is None or ann_col is None:
        return _empty_snapshot_frame()

    roe_col = colmap.get("roe")
    debt_col = colmap.get("debt_to_assets") or colmap.get("debt_ratio")
    update_col = colmap.get("update_flag")

    code6 = str(symbol).zfill(6)[-6:]
    out = pd.DataFrame(index=frame.index.copy())
    out["symbol"] = code6
    out["end_date"] = pd.to_datetime(frame[end_col].astype(str), format="%Y%m%d", errors="coerce")
    out["ann_date"] = pd.to_datetime(frame[ann_col].astype(str), format="%Y%m%d", errors="coerce")
    out["roe"] = frame[roe_col].map(percent_to_ratio) if roe_col is not None else np.nan
    out["debt_ratio"] = (
        frame[debt_col].map(percent_to_ratio) if debt_col is not None else np.nan
    )
    if update_col is not None:
        out["update_flag"] = pd.to_numeric(frame[update_col], errors="coerce").fillna(0).astype(int)
    else:
        out["update_flag"] = 0
    out["_row_ord"] = np.arange(len(out))

    out = out.dropna(subset=["ann_date"]).copy()
    if out.empty:
        return _empty_snapshot_frame()

    out = out.sort_values(
        by=["ann_date", "end_date", "update_flag", "_row_ord"],
        ascending=[True, True, True, True],
        kind="mergesort",
    )
    out = out.drop_duplicates(subset=["end_date", "ann_date"], keep="last")

    missing_list: list[str] = []
    complete_flags: list[bool] = []
    completeness_vals: list[float] = []
    for _, row in out.iterrows():
        missing: list[str] = []
        if pd.isna(row["roe"]):
            missing.append("roe")
        if pd.isna(row["debt_ratio"]):
            missing.append("debt_ratio")
        missing_list.append(",".join(missing))
        complete_flags.append(len(missing) == 0)
        completeness_vals.append(
            1.0 - (len(missing) / float(len(_REQUIRED_METRICS))) if missing else 1.0
        )

    out["financial_report_date"] = out["end_date"].dt.strftime("%Y-%m-%d")
    out["financial_as_of"] = out["ann_date"].dt.strftime("%Y-%m-%d")
    out["financial_source"] = _FINANCIAL_SOURCE
    out["financial_trust_level"] = _TRUST_REPORTED
    out["financial_missing_fields"] = missing_list
    out["financial_data_complete"] = complete_flags
    out["financial_completeness"] = completeness_vals
    out["coverage_complete"] = True
    out["as_of"] = out["financial_as_of"]
    out["source"] = _FINANCIAL_SOURCE

    keep = [
        "symbol",
        "end_date",
        "ann_date",
        "roe",
        "debt_ratio",
        "update_flag",
        "financial_report_date",
        "financial_as_of",
        "financial_source",
        "financial_trust_level",
        "financial_missing_fields",
        "financial_data_complete",
        "financial_completeness",
        "coverage_complete",
        "as_of",
        "source",
    ]
    return out[keep].reset_index(drop=True)


def select_pit_snapshot_for_date(
    snapshots: pd.DataFrame,
    *,
    as_of: date | pd.Timestamp,
) -> dict[str, object] | None:
    """Pick latest disclosed snapshot effective on as_of (inclusive)."""
    if snapshots is None or snapshots.empty:
        return None
    frame = snapshots.copy()
    if "ann_date" not in frame.columns:
        return None
    frame["ann_date"] = pd.to_datetime(frame["ann_date"], errors="coerce")
    if "end_date" in frame.columns:
        frame["end_date"] = pd.to_datetime(frame["end_date"], errors="coerce")
    else:
        frame["end_date"] = pd.NaT
    if "update_flag" not in frame.columns:
        frame["update_flag"] = 0
    as_of_ts = pd.Timestamp(as_of)
    valid = frame.dropna(subset=["ann_date"])
    valid = valid.loc[valid["ann_date"] <= as_of_ts]
    if valid.empty:
        return None
    valid = valid.sort_values(
        by=["ann_date", "end_date", "update_flag"],
        ascending=[True, True, True],
        kind="mergesort",
    )
    row = valid.iloc[-1]
    return {str(k): row[k] for k in row.index}


def apply_financial_snapshots_asof(
    daily: pd.DataFrame,
    snapshots: pd.DataFrame,
    *,
    only_fill_pending: bool = True,
) -> pd.DataFrame:
    """As-of join financial snapshots onto daily bars without pre-announcement leak."""
    if daily is None or daily.empty:
        return daily
    out = daily.copy()
    if snapshots is None or snapshots.empty:
        return out

    index_was_date = isinstance(out.index, pd.DatetimeIndex) or (
        out.index.name in {"date", "trade_date"}
    )
    work = out.reset_index() if index_was_date and "date" not in out.columns else out.copy()
    if "date" not in work.columns and index_was_date:
        if "index" in work.columns:
            work = work.rename(columns={"index": "date"})
        elif "level_0" in work.columns:
            work = work.rename(columns={"level_0": "date"})
    if "date" not in work.columns:
        return out

    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    snap = snapshots.copy()
    snap["ann_date"] = pd.to_datetime(snap["ann_date"], errors="coerce")
    if "end_date" in snap.columns:
        snap["end_date"] = pd.to_datetime(snap["end_date"], errors="coerce")
    if "update_flag" not in snap.columns:
        snap["update_flag"] = 0
    snap = snap.dropna(subset=["ann_date"]).sort_values(
        by=["ann_date", "end_date", "update_flag"],
        ascending=[True, True, True],
        kind="mergesort",
    )
    if snap.empty:
        return out

    roe_vals: list[float] = []
    debt_vals: list[float] = []
    complete_vals: list[bool] = []
    missing_vals: list[str] = []
    source_vals: list[str] = []
    report_vals: list[str] = []
    asof_vals: list[str] = []
    trust_vals: list[str] = []
    completeness_vals: list[float] = []

    for _, row in work.iterrows():
        bar_ts = row["date"]
        if pd.isna(bar_ts):
            roe_vals.append(float("nan"))
            debt_vals.append(float("nan"))
            complete_vals.append(False)
            missing_vals.append("roe,debt_ratio")
            source_vals.append(str(row.get("financial_source", "") or ""))
            report_vals.append(str(row.get("financial_report_date", "") or ""))
            asof_vals.append(str(row.get("financial_as_of", "") or ""))
            trust_vals.append(str(row.get("financial_trust_level", "") or "missing"))
            completeness_vals.append(0.0)
            continue

        if only_fill_pending:
            cur_trust = str(row.get("financial_trust_level", "") or "").strip().lower()
            cur_source = str(row.get("financial_source", "") or "").strip().lower()
            if cur_trust in {"reported", "derived"} and cur_source not in {
                "",
                "missing",
                "tushare_pending",
                "tdx_offline",
                "heuristic",
                "default",
                "akshare_tx_default",
                "efinance_default",
            }:
                roe_vals.append(_to_float(row.get("roe")))
                debt_vals.append(_to_float(row.get("debt_ratio")))
                complete_vals.append(bool(row.get("financial_data_complete", False)))
                missing_vals.append(str(row.get("financial_missing_fields", "") or ""))
                source_vals.append(str(row.get("financial_source", "") or ""))
                report_vals.append(str(row.get("financial_report_date", "") or ""))
                asof_vals.append(str(row.get("financial_as_of", "") or ""))
                trust_vals.append(cur_trust)
                completeness_vals.append(_to_float(row.get("financial_completeness")) or 0.0)
                continue

        picked = select_pit_snapshot_for_date(snap, as_of=bar_ts)
        if picked is None:
            roe_vals.append(float("nan"))
            debt_vals.append(float("nan"))
            complete_vals.append(False)
            missing_vals.append("roe,debt_ratio")
            source_vals.append("tushare_pending")
            report_vals.append("")
            asof_vals.append("")
            trust_vals.append("missing")
            completeness_vals.append(0.0)
            continue

        roe_vals.append(_to_float(picked.get("roe")))
        debt_vals.append(_to_float(picked.get("debt_ratio")))
        complete_vals.append(bool(picked.get("financial_data_complete", False)))
        missing_vals.append(str(picked.get("financial_missing_fields", "") or ""))
        source_vals.append(str(picked.get("financial_source", "") or _FINANCIAL_SOURCE))
        report_vals.append(str(picked.get("financial_report_date", "") or ""))
        asof_vals.append(str(picked.get("financial_as_of", "") or ""))
        trust_vals.append(str(picked.get("financial_trust_level", "") or _TRUST_REPORTED))
        completeness_vals.append(_to_float(picked.get("financial_completeness")) or 0.0)

    work["roe"] = roe_vals
    work["debt_ratio"] = debt_vals
    work["financial_data_complete"] = complete_vals
    work["financial_missing_fields"] = missing_vals
    work["financial_source"] = source_vals
    work["financial_report_date"] = report_vals
    work["financial_as_of"] = asof_vals
    work["financial_trust_level"] = trust_vals
    work["financial_completeness"] = completeness_vals

    if index_was_date:
        work = work.set_index("date").sort_index()
        work.index.name = "date"
        return work
    return work


def _empty_snapshot_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "symbol",
            "end_date",
            "ann_date",
            "roe",
            "debt_ratio",
            "update_flag",
            "financial_report_date",
            "financial_as_of",
            "financial_source",
            "financial_trust_level",
            "financial_missing_fields",
            "financial_data_complete",
            "financial_completeness",
            "coverage_complete",
            "as_of",
            "source",
        ]
    )


def merge_snapshot_frames(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> pd.DataFrame:
    """Idempotent merge of financial snapshot events."""
    if existing is None or existing.empty:
        return normalize_merge_ready(incoming)
    if incoming is None or incoming.empty:
        return normalize_merge_ready(existing)
    combined = pd.concat(
        [normalize_merge_ready(existing), normalize_merge_ready(incoming)],
        axis=0,
        ignore_index=True,
    )
    combined = combined.sort_values(
        by=["ann_date", "end_date", "update_flag"],
        ascending=[True, True, True],
        kind="mergesort",
    )
    combined = combined.drop_duplicates(
        subset=["symbol", "end_date", "ann_date", "financial_source"],
        keep="last",
    )
    return combined.reset_index(drop=True)


def normalize_merge_ready(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return _empty_snapshot_frame()
    out = frame.copy()
    out["symbol"] = out["symbol"].astype(str).str.zfill(6).str[-6:]
    out["end_date"] = pd.to_datetime(out["end_date"], errors="coerce")
    out["ann_date"] = pd.to_datetime(out["ann_date"], errors="coerce")
    if "update_flag" not in out.columns:
        out["update_flag"] = 0
    if "financial_source" not in out.columns:
        out["financial_source"] = _FINANCIAL_SOURCE
    return out.dropna(subset=["ann_date"])
