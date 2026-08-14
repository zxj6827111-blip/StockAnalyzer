"""Warehouse freshness artifact + stale-data open-position guard (PLAN P1-4).

``write_warehouse_freshness`` 在同步闭环（dry-run/import/delta sync/完整性
验证）完成后写出 ``warehouse_freshness.json``，记录 package/delta 的
date_max、更新时间、校验结果、行数和数据源；freshness 读取以该 artifact
为准（旧 manifest 日期只作历史证据）。

``open_position_blocked_by_stale_data`` 实现"delta 优先、package fallback；
fallback 超过 2 个交易日时禁止新开仓，不再静默使用陈旧数据"的执行门。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

FRESHNESS_FILENAME = "warehouse_freshness.json"
DEFAULT_STALE_TRADE_DAYS = 2  # fallback 超过 2 个交易日禁止新开仓
MAX_TRADE_DAYS_PER_WEEK = 5


@dataclass(slots=True)
class FreshnessStatus:
    ok: bool
    source: str  # delta | package | none
    date_max: date | None
    stale_trade_days: int
    reason: str


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None
    return None


def _trade_days_between(earlier: date, later: date) -> int:
    """粗粒度交易日数（周内按自然日近似，非完整交易日历）。"""
    if later <= earlier:
        return 0
    days = 0
    cursor = earlier + timedelta(days=1)
    while cursor <= later:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def read_warehouse_freshness(path: str | Path) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def write_warehouse_freshness(
    *,
    path: str | Path,
    source: str,
    date_max: date | None,
    updated_at: datetime,
    verification_status: str,
    row_count: int | None,
    data_source: str,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """原子写入 freshness artifact（tmp + os.replace）。"""
    payload: dict[str, Any] = {
        "source": source,
        "date_max": date_max.isoformat() if date_max is not None else "",
        "updated_at": updated_at.isoformat(),
        "verification_status": verification_status,
        "row_count": row_count,
        "data_source": data_source,
    }
    if extra:
        payload.update(extra)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp, target)
    return target


def freshness_status(
    freshness: dict[str, Any] | None,
    *,
    now: date,
    max_stale_trade_days: int = DEFAULT_STALE_TRADE_DAYS,
) -> FreshnessStatus:
    """delta 优先、package fallback 的陈旧度判定。

    - 无 artifact：source=none，视为不可用（禁止开仓）；
    - date_max 缺失：不可用；
    - 超过 ``max_stale_trade_days``：source=delta|package 但 stale，禁止开仓；
    - 未超限：ok=True。
    """
    if freshness is None:
        return FreshnessStatus(
            ok=False,
            source="none",
            date_max=None,
            stale_trade_days=0,
            reason="freshness_artifact_missing",
        )
    date_max = _as_date(freshness.get("date_max"))
    source = str(freshness.get("source", "none")).strip() or "none"
    if date_max is None:
        return FreshnessStatus(
            ok=False,
            source=source,
            date_max=None,
            stale_trade_days=0,
            reason="date_max_missing",
        )
    stale_days = _trade_days_between(date_max, now)
    if stale_days > max(1, int(max_stale_trade_days)):
        return FreshnessStatus(
            ok=False,
            source=source,
            date_max=date_max,
            stale_trade_days=stale_days,
            reason=f"data_stale_{stale_days}_trade_days",
        )
    return FreshnessStatus(
        ok=True,
        source=source,
        date_max=date_max,
        stale_trade_days=stale_days,
        reason="fresh",
    )


def open_position_blocked_by_stale_data(
    freshness: dict[str, Any] | None,
    *,
    now: date,
    max_stale_trade_days: int = DEFAULT_STALE_TRADE_DAYS,
) -> dict[str, Any]:
    """执行门：fallback 超过 N 个交易日或 freshness 缺失时禁止新开仓。

    返回结构化判定（供执行层 / 日报消费）：
    ``blocked=True`` 时任何新开仓（含模拟盘）都应被拒绝；已持仓不受影响。
    """
    status = freshness_status(
        freshness,
        now=now,
        max_stale_trade_days=max_stale_trade_days,
    )
    return {
        "blocked": not status.ok,
        "source": status.source,
        "date_max": status.date_max.isoformat() if status.date_max is not None else "",
        "stale_trade_days": status.stale_trade_days,
        "reason": status.reason,
        "max_stale_trade_days": max(1, int(max_stale_trade_days)),
    }
