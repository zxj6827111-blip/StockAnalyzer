"""Real market breadth snapshot + versioned 0-100 score (PLAN P2-1).

盘中使用全市场实时行情 provider，盘后使用 warehouse/delta 批量聚合；
禁止从已筛选候选信号反推市场情绪。评分采用版本化、确定性的 0~100
映射（先 Shadow 回放校准，``disable_if_sentiment_below`` 暂保留为初始
阈值）。数据覆盖率 < 95% 或盘中心跳 > 10 分钟视为不可用；轻度过期时
trend 最低分提高 5 分且 monster 仅观察；严重过期或缺失时禁止新开仓。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCORE_VERSION = 1
DEFAULT_MIN_COVERAGE = 0.95
DEFAULT_MAX_INTRADAY_HEARTBEAT_SEC = 600  # 10 分钟
DEFAULT_STALE_TREND_LIFT = 5.0
DEFAULT_DISABLE_BELOW = 45.0

BREADTH_FILENAME = "market_breadth.json"


@dataclass(slots=True)
class BreadthScore:
    score: float
    available: bool
    coverage_ok: bool
    stale: bool
    reasons: list[str]


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def compute_breadth_score(
    *,
    advancers: int,
    decliners: int,
    limit_up_count: int,
    limit_down_count: int,
    median_return: float,
    new_highs_20d: int,
    new_lows_20d: int,
    turnover_change_pct: float,
    total_symbols: int,
    version: int = SCORE_VERSION,
) -> BreadthScore:
    """版本化确定性 0~100 映射（v1）。

    组件：
    - 上涨占比 (advancers/total) 映射 0~100；
    - 涨跌停差倾斜；
    - 20 日新高/新低倾斜；
    - 市场中位收益；
    - 成交额变化。

    任何组件数据缺失时按中性值处理，保证确定性（不引入随机性）。
    """
    if total_symbols <= 0:
        return BreadthScore(
            score=0.0, available=False, coverage_ok=False, stale=False, reasons=["no_universe"]
        )
    reasons: list[str] = []
    advancers = max(0, advancers)
    decliners = max(0, decliners)
    total = max(1, total_symbols)
    advancer_ratio = min(1.0, advancers / total)
    breadth_component = advancer_ratio * 100.0

    net_limit = (limit_up_count - limit_down_count) / max(
        1.0, float(limit_up_count + limit_down_count)
    )
    limit_component = 50.0 + net_limit * 20.0

    net_high_low = (new_highs_20d - new_lows_20d) / max(
        1.0, float(new_highs_20d + new_lows_20d)
    )
    highlow_component = 50.0 + net_high_low * 20.0

    median_component = 50.0 + max(-1.0, min(1.0, median_return * 100.0)) * 30.0

    turnover_component = 50.0 + max(-1.0, min(1.0, turnover_change_pct)) * 10.0

    score = (
        breadth_component * 0.45
        + limit_component * 0.15
        + highlow_component * 0.15
        + median_component * 0.15
        + turnover_component * 0.10
    )
    if version != SCORE_VERSION:
        reasons.append(f"unsupported_version:{version}")
    return BreadthScore(
        score=round(max(0.0, min(100.0, score)), 2),
        available=True,
        coverage_ok=True,
        stale=False,
        reasons=reasons,
    )


def _signature(payload: Mapping[str, Any]) -> str:
    """确定性指纹：同一输入 → 同一评分签名（防评分逻辑静默漂移）。"""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_breadth_snapshot(
    *,
    advancers: int,
    decliners: int,
    limit_up_count: int,
    limit_down_count: int,
    median_return: float,
    new_highs_20d: int,
    new_lows_20d: int,
    turnover_change_pct: float,
    total_symbols: int,
    coverage_ratio: float,
    as_of: datetime,
    source: str,
    freshness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 MarketBreadthSnapshot（含版本化评分与可用性判定）。"""
    min_coverage = DEFAULT_MIN_COVERAGE
    coverage_ok = coverage_ratio >= min_coverage
    scoring = compute_breadth_score(
        advancers=advancers,
        decliners=decliners,
        limit_up_count=limit_up_count,
        limit_down_count=limit_down_count,
        median_return=median_return,
        new_highs_20d=new_highs_20d,
        new_lows_20d=new_lows_20d,
        turnover_change_pct=turnover_change_pct,
        total_symbols=total_symbols,
    )
    available = scoring.available and coverage_ok
    snapshot: dict[str, Any] = {
        "advancers": advancers,
        "decliners": decliners,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "median_return": round(median_return, 6),
        "new_highs_20d": new_highs_20d,
        "new_lows_20d": new_lows_20d,
        "turnover_change_pct": round(turnover_change_pct, 6),
        "total_symbols": total_symbols,
        "coverage_ratio": round(coverage_ratio, 6),
        "coverage_ok": coverage_ok,
        "as_of": as_of.isoformat(),
        "source": source,
        "freshness": dict(freshness) if freshness else {},
        "score": {
            "version": SCORE_VERSION,
            "value": scoring.score,
            "available": available,
            "signature": _signature(
                {
                    "version": SCORE_VERSION,
                    "advancers": advancers,
                    "decliners": decliners,
                    "limit_up_count": limit_up_count,
                    "limit_down_count": limit_down_count,
                    "median_return": median_return,
                    "new_highs_20d": new_highs_20d,
                    "new_lows_20d": new_lows_20d,
                    "turnover_change_pct": turnover_change_pct,
                    "total_symbols": total_symbols,
                }
            ),
        },
    }
    return snapshot


def write_breadth_snapshot(snapshot: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as fp:
        json.dump(dict(snapshot), fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp, target)
    return target


def read_breadth_snapshot(path: str | Path) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def breadth_usage_policy(
    snapshot: Mapping[str, Any] | None,
    *,
    now: datetime,
    trend_min_threshold: float,
    disable_if_below: float = DEFAULT_DISABLE_BELOW,
    max_intraday_heartbeat_sec: float = DEFAULT_MAX_INTRADAY_HEARTBEAT_SEC,
) -> dict[str, Any]:
    """评分过期/缺失策略（PLAN P2-1）。

    - snapshot 缺失或评分不可用：禁止新开仓；
    - 盘中心跳（as_of 距今）超过 10 分钟：视为不可用（禁止新开仓）；
    - 评分低于 ``disable_if_below``：禁止新开仓；
    - 轻度过期（数据日期早于 as_of 日）但心跳新鲜：trend 最低分 +5，
      monster 仅观察（不可执行）；
    - 正常：返回可用的趋势增强分。
    """
    now = _as_aware(now)
    if snapshot is None:
        return {
            "block_new_buy": True,
            "reason": "breadth_snapshot_missing",
            "trend_min_threshold_lift": 0.0,
            "monster_observe_only": False,
            "score": None,
        }
    score_block = snapshot.get("score")
    score_value = (
        _as_float(score_block.get("value"), default=0.0)
        if isinstance(score_block, dict)
        else 0.0
    )
    available = (
        bool(score_block.get("available", False)) if isinstance(score_block, dict) else False
    )
    if not available:
        return {
            "block_new_buy": True,
            "reason": "breadth_score_unavailable",
            "trend_min_threshold_lift": 0.0,
            "monster_observe_only": False,
            "score": score_value,
        }
    as_of = _parse_ts(snapshot.get("as_of"))
    heartbeat_sec = (
        (now - as_of).total_seconds() if as_of is not None else float("inf")
    )
    if heartbeat_sec > max(1.0, float(max_intraday_heartbeat_sec)):
        return {
            "block_new_buy": True,
            "reason": "breadth_heartbeat_stale",
            "trend_min_threshold_lift": 0.0,
            "monster_observe_only": False,
            "score": score_value,
        }
    if score_value < float(disable_if_below):
        return {
            "block_new_buy": True,
            "reason": "breadth_below_threshold",
            "trend_min_threshold_lift": 0.0,
            "monster_observe_only": False,
            "score": score_value,
        }
    freshness = snapshot.get("freshness")
    data_date = _parse_ts(freshness.get("date_max")) if isinstance(freshness, dict) else None
    stale = data_date is not None and as_of is not None and data_date.date() < as_of.date()
    if stale:
        return {
            "block_new_buy": False,
            "reason": "breadth_slightly_stale",
            "trend_min_threshold_lift": float(DEFAULT_STALE_TREND_LIFT),
            "monster_observe_only": True,
            "score": score_value,
        }
    return {
        "block_new_buy": False,
        "reason": "breadth_ok",
        "trend_min_threshold_lift": 0.0,
        "monster_observe_only": False,
        "score": score_value,
    }


def _as_aware(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC-aware (heartbeat/stale math)."""
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_aware(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return _as_aware(parsed)
    return None
