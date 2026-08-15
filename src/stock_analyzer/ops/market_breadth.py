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

import pandas as pd

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


def compute_market_breadth_from_warehouse(
    warehouse: object,
    *,
    limit_rule: object | None = None,
    lookback_days: int = 25,
    limit_tolerance: float = 0.001,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """从 market warehouse 全市场日线现算广度快照（无外部 API 调用）。

    一条批量查询（``fetch_universe_quality_metrics``）取全市场最近
    ``lookback_days`` 个交易日，按最新交易日统计：上涨/下跌家数、中位收益、
    涨跌停家数（用 ``resolve_limit_pct`` 现算，warehouse 的 up_limit/down_limit
    列为空）、20 日新高/新低家数、成交额环比、总家数与覆盖率。

    ``freshness.date_max`` 取最新交易日、``as_of`` 取当前时刻：数据落后于
    今天时广度门可判定"轻度过期"（trend 最低分 +5），而非仅二值新鲜/阻断。

    返回与 ``build_breadth_snapshot`` 输出一致的快照 dict；数据不足或
    计算异常返回 ``None``，调用方按"广度不可用"处理（不阻断扫描）。
    """
    from stock_analyzer.config import LimitRuleConfig
    from stock_analyzer.data.limit_rule import resolve_limit_pct

    # warehouse 的 board 用英文简写（main/gem/star/bj），limit_rule 需要中文；
    # 不映射会把科创板/创业板按 10% 而非 20% 判涨停，涨跌停家数严重高估。
    _BOARD_MAP = {
        "main": "主板",
        "gem": "创业板",
        "star": "科创板",
        "bj": "北交所",
        "st": "ST",
    }

    try:
        symbols = list(warehouse.list_symbols())
        if not symbols:
            return None
        frame = warehouse.fetch_universe_quality_metrics(
            symbols=symbols,
            lookback_days=max(5, int(lookback_days)),
        )
        if frame is None or not hasattr(frame, "empty") or frame.empty:
            return None
    except Exception:
        return None

    required = {"symbol", "date", "close"}
    if not required.issubset(frame.columns):
        return None
    frame = frame.sort_values(["symbol", "date"]).reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "turnover" in frame.columns:
        frame["turnover"] = pd.to_numeric(frame["turnover"], errors="coerce").fillna(0.0)
    else:
        frame["turnover"] = 0.0
    frame = frame.dropna(subset=["date", "close"]).sort_values(["symbol", "date"])
    if frame.empty:
        return None

    latest_date = frame["date"].max()
    latest = frame.loc[frame["date"] == latest_date].copy()
    if latest.empty:
        return None
    # prev_close：每 symbol 上一交易日 close（groupby shift 按 symbol/date 对齐，
    # 停牌后复牌日自然衔接前一收盘价）。
    frame["prev_close"] = frame.groupby("symbol")["close"].shift(1)
    latest["prev_close"] = frame.loc[latest.index, "prev_close"]

    valid = latest.dropna(subset=["prev_close"])
    valid = valid[valid["prev_close"] > 0]
    if valid.empty:
        return None
    pct = valid["close"] / valid["prev_close"] - 1.0
    advancers = int((pct > 0).sum())
    decliners = int((pct < 0).sum())
    median_return = float(pct.median())

    # 涨跌停：按 (board, is_st) 组合求 limit_pct 后向量化比较，避免 5000 行
    # 逐行 build_price_limits（同时修正 board 英文值→中文映射）。
    limit_config = limit_rule if limit_rule is not None else LimitRuleConfig()
    latest_date_d = latest_date.date() if hasattr(latest_date, "date") else latest_date
    board_col = (
        latest["board"]
        if "board" in latest.columns
        else pd.Series("", index=latest.index)
    )
    is_st_col = (
        latest["is_st"]
        if "is_st" in latest.columns
        else pd.Series(False, index=latest.index)
    )
    latest = latest.assign(
        board_cn=board_col.map(_BOARD_MAP).fillna(board_col),
        is_st_bool=is_st_col.astype(bool),
    )
    key_frame = latest[["board_cn", "is_st_bool"]].drop_duplicates()
    key_frame["limit_pct"] = key_frame.apply(
        lambda row: resolve_limit_pct(
            config=limit_config,
            trade_date=latest_date_d,
            board=str(row["board_cn"]),
            is_st=bool(row["is_st_bool"]),
            listing_days=None,
        ),
        axis=1,
    )
    latest = latest.merge(key_frame, on=["board_cn", "is_st_bool"], how="left")
    tol = max(0.0, float(limit_tolerance))
    limited = latest[latest["limit_pct"].notna()]
    if limited.empty:
        up_count = 0
        down_count = 0
    else:
        up_limit = limited["prev_close"] * (1.0 + limited["limit_pct"])
        down_limit = limited["prev_close"] * (1.0 - limited["limit_pct"])
        up_count = int((limited["close"] >= up_limit * (1.0 - tol)).sum())
        down_count = int((limited["close"] <= down_limit * (1.0 + tol)).sum())

    # 20 日新高/新低：当日 close 高于/低于此前 20 个交易日的最高/最低。
    high20 = 0
    low20 = 0
    for _symbol, group in frame.groupby("symbol"):
        closes = group["close"].dropna().reset_index(drop=True)
        if len(closes) < 21:
            continue
        tail = closes.iloc[-21:-1]
        current = closes.iloc[-1]
        if current >= tail.max():
            high20 += 1
        if current <= tail.min():
            low20 += 1

    dates = sorted(frame["date"].unique())
    turnover_change_pct = 0.0
    if len(dates) >= 2:
        cur_turnover = float(latest["turnover"].sum())
        prev_turnover = float(frame.loc[frame["date"] == dates[-2], "turnover"].sum())
        if prev_turnover > 0:
            turnover_change_pct = (cur_turnover - prev_turnover) / prev_turnover

    total_symbols = int(len(latest))
    coverage_ratio = total_symbols / max(1, len(symbols))
    as_of_dt = _as_aware(now) if now is not None else datetime.now(UTC)
    return build_breadth_snapshot(
        advancers=advancers,
        decliners=decliners,
        limit_up_count=up_count,
        limit_down_count=down_count,
        median_return=median_return,
        new_highs_20d=high20,
        new_lows_20d=low20,
        turnover_change_pct=turnover_change_pct,
        total_symbols=total_symbols,
        coverage_ratio=coverage_ratio,
        as_of=as_of_dt,
        source="warehouse_daily",
        freshness={"date_max": latest_date.strftime("%Y-%m-%d")},
    )
