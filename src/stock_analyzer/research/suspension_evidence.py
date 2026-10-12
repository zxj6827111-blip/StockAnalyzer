"""Shared source contract for evidence that proves a full-day suspension."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlparse

TUSHARE_SUSPENSION_SOURCE = "tushare_suspend_d"
SSE_OFFICIAL_SUSPENSION_SOURCE = "sse_official_suspension_notice"
_OFFICIAL_HOSTS = frozenset({"star.sse.com.cn", "www.sse.com.cn", "dataclouds.cninfo.com.cn"})


def is_verified_full_day_suspension(evidence: Mapping[str, Any]) -> bool:
    """Validate persisted status plus its source-specific full-day evidence contract."""
    if not (
        evidence.get("suspended") is True
        and str(evidence.get("suspend_type") or "").strip().upper() == "S"
        and str(evidence.get("trade_status") or "").strip().lower() == "suspended"
        and evidence.get("coverage_complete") is True
        and str(evidence.get("as_of") or "").strip()
    ):
        return False

    source = str(evidence.get("source") or "").strip()
    if source == TUSHARE_SUSPENSION_SOURCE:
        return True
    if source != SSE_OFFICIAL_SUSPENSION_SOURCE:
        return False

    url = urlparse(str(evidence.get("evidence_url") or "").strip())
    digest = str(evidence.get("evidence_sha256") or "").strip().lower()
    trade_date = str(evidence.get("trade_date") or "").strip()[:10]
    document_date = str(evidence.get("evidence_document_date") or "").strip()[:10]
    period_start = str(evidence.get("evidence_start_date") or "").strip()[:10]
    period_end = str(evidence.get("evidence_end_date") or "").strip()[:10]
    try:
        trade_day = date.fromisoformat(trade_date)
        if bool(period_start) != bool(period_end):
            return False
        if period_start and period_end:
            start_day = date.fromisoformat(period_start)
            end_day = date.fromisoformat(period_end)
            period_valid = start_day <= trade_day <= end_day
        else:
            period_valid = document_date == trade_date
    except ValueError:
        return False
    return bool(
        url.scheme == "https"
        and url.hostname in _OFFICIAL_HOSTS
        and str(evidence.get("evidence_title") or "").strip()
        and period_valid
        and evidence.get("evidence_full_day") is True
        and str(evidence.get("evidence_as_of") or "").strip()
        and re.fullmatch(r"[0-9a-f]{64}", digest)
    )


def is_verified_no_trade_adj_factor_gap(
    evidence: Mapping[str, Any] | None,
    *,
    has_raw_daily_bar: bool,
    raw_minute_bar_count: int,
    action_coverage_complete: bool,
    has_corporate_action: bool,
    previous_factor: Any,
    next_factor: Any,
) -> bool:
    """Prove a missing factor row is inapplicable on one verified no-trade day.

    This never supplies an adjustment factor. It only accepts an absent row when
    the complete session was independently suspended, no RAW trade bars exist,
    corporate-action coverage is complete with no action, and the surrounding
    observed factors are equal.
    """
    if not is_verified_full_day_suspension(evidence or {}):
        return False
    if (
        has_raw_daily_bar
        or raw_minute_bar_count != 0
        or action_coverage_complete is not True
        or has_corporate_action
    ):
        return False
    try:
        before = float(previous_factor)
        after = float(next_factor)
    except (TypeError, ValueError):
        return False
    return bool(
        math.isfinite(before)
        and before > 0
        and math.isfinite(after)
        and after > 0
        and math.isclose(before, after, rel_tol=1e-12, abs_tol=0.0)
    )
