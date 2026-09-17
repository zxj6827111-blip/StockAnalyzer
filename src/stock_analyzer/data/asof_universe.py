"""Point-in-Time 历史股票池（S03，原 P0-04）。

**问题**（蓝图 §2.11）：``AsOfMarketDataProvider.list_symbols()`` 直接透传完整
provider 索引，因此"当前索引状态"会污染历史股票池——未来上市的票、以及依赖当前
索引的覆盖率分母，都会让历史回测看起来"当时就选得到"。

**本模块只做一件事**：把"as_of=T 时刻可观测的 bar 事实"翻译成可审计的历史股票池，
并且**不假装**数据源具备完整历史退市覆盖。

规则（全部只用 ≤ as_of 的可观测事实，不读当前 ST/退市名单）：

====================================  ==================================================
eligible（有资格）                     history_window 内 bar 数 ≥ ``min_history_days``
future_listed                         ≤ as_of 一根 bar 都没有 → 未来上市/从未上市（硬排除）
insufficient_history_window_bars      窗口内 bar 数不足 —— **可能是新上市，也可能是长期
                                      停牌/数据缺口**；一次批量探针无法区分，故不谎称
                                      "上市太短"（诚实命名，见下）
expected_active                       在最近 ``expected_active_lookback_days`` 个交易日内
                                      有 ≥1 根合法 bar
known_suspended                       eligible 但 lookback 内 0 根 bar（停牌/停更），
                                      **单列不计入分母**
====================================  ==================================================

覆盖率分母 = ``expected_active``（不是完整 provider 索引）：
``valid_expected_active / expected_active``。

``survivorship_coverage`` 默认 ``incomplete_or_unknown``——vendor/warehouse 数据
都无法证明"历史退市股票已被完整覆盖"，除非调用方显式声明已验证
（``delisting_coverage_verified=True``），否则**不允许**报 complete。

设计取舍：第一版只用**一次批量探针**能拿到的观测量（窗口内 bar 数 + 最近 bar 日期），
不在 5500 只票上逐票扫全历史；因此"上市时长"是"窗口内 bar 数"的代理，语义在
``to_payload()`` 里如实标注（``listed_age_basis=history_window_bar_count``）。

已知限制（第一版窗口口径，明确记录而非隐藏）：

- 新上市与**长期停牌**（停牌时长超过"窗口 − min_history 交易日"的余量）都会落进
  ``insufficient_history_window_bars``——一次批量探针无法区分二者，因此只声称
  "窗口内历史不足"，不谎称原因；
- 只有短期停牌（停牌后仍留有 ≥ ``min_history_days`` 根窗口内 bar）才会被单列为
  ``known_suspended``；
- 完整退市覆盖无法从现有数据源证明，故 ``survivorship_coverage`` 默认
  ``incomplete_or_unknown``（蓝图 §P0-04 的"无法证明时标记"）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

# 状态取值（稳定契约，供报告与测试引用）
COVERAGE_COMPLETE_PIT = "complete_pit"
COVERAGE_INCOMPLETE_OR_UNKNOWN = "incomplete_or_unknown"

EXCLUDE_FUTURE_LISTED = "future_listed"
EXCLUDE_INSUFFICIENT_HISTORY = "insufficient_history_window_bars"
EXCLUDE_NO_BARS = "no_bars_before_as_of"

DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS = 5
DEFAULT_MIN_HISTORY_DAYS = 60


@dataclass(frozen=True, slots=True)
class SymbolPitStats:
    """单只标的在 as_of 时点的可观测 bar 事实（只含 ≤ as_of 的数据）。"""

    symbol: str
    bars_in_window: int = 0  # history_window 内合法 bar 数（上市时长代理）
    bars_in_lookback: int = 0  # 最近 expected_active_lookback_days 内 bar 数
    first_bar_date: date | None = None  # 窗口内最早 bar（不是真实上市日）
    last_bar_date: date | None = None  # ≤ as_of 的最近 bar


@dataclass(slots=True)
class AsofUniverseSnapshot:
    """as_of 时点的历史股票池快照（可审计、可复现）。"""

    universe_snapshot_id: str
    as_of: date
    eligible_symbols: tuple[str, ...] = ()
    expected_active_symbols: tuple[str, ...] = ()
    known_suspended_symbols: tuple[str, ...] = ()
    excluded_reasons: dict[str, str] = field(default_factory=dict)
    reason_counts: dict[str, int] = field(default_factory=dict)
    index_symbol_count: int = 0
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS
    expected_active_lookback_days: int = DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS
    survivorship_coverage: str = COVERAGE_INCOMPLETE_OR_UNKNOWN
    delisting_coverage_verified: bool = False

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_symbols)

    @property
    def expected_active_count(self) -> int:
        return len(self.expected_active_symbols)

    def coverage_ratio(self, *, valid_symbol_count: int) -> float:
        """``valid_expected_active / expected_active``；分母为 0 时返回 0（不得当满分）。"""
        denominator = self.expected_active_count
        if denominator <= 0:
            return 0.0
        return max(0.0, min(1.0, float(valid_symbol_count) / float(denominator)))

    def to_payload(self) -> dict[str, object]:
        return {
            "universe_snapshot_id": self.universe_snapshot_id,
            "as_of": self.as_of.isoformat(),
            "index_symbol_count": self.index_symbol_count,
            "eligible_count": self.eligible_count,
            "expected_active_count": self.expected_active_count,
            "known_suspended_count": len(self.known_suspended_symbols),
            "reason_counts": dict(self.reason_counts),
            "survivorship_coverage": self.survivorship_coverage,
            "delisting_coverage_verified": self.delisting_coverage_verified,
            "min_history_days": self.min_history_days,
            "expected_active_lookback_days": self.expected_active_lookback_days,
            # 口径必须自述：上市时长是"窗口内 bar 数"的代理，不是真实上市日。
            "listed_age_basis": "history_window_bar_count",
            "coverage_denominator": "expected_active",
            "known_suspended_symbols": list(self.known_suspended_symbols)[:50],
            "excluded_reasons_sample": dict(list(sorted(self.excluded_reasons.items()))[:50]),
        }


def build_pit_stats(
    *,
    metrics: pd.DataFrame,
    as_of: date,
    lookback_days: int,
    history_window_days: int,
) -> dict[str, SymbolPitStats]:
    """从批量质量探针（symbol/date 两列）构造 as_of 时点的 per-symbol 事实。

    只统计 ``date <= as_of`` 的行；晚于 as_of 的行直接丢弃（不参与任何计数），
    避免"探针多给了未来行"把未来上市票算成历史有效。
    """
    stats: dict[str, SymbolPitStats] = {}
    if not isinstance(metrics, pd.DataFrame) or metrics.empty:
        return stats
    if "symbol" not in metrics.columns or "date" not in metrics.columns:
        return stats

    frame = metrics[["symbol", "date"]].copy()
    frame["_date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["_date"])
    frame = frame[frame["_date"].dt.date <= as_of]
    if frame.empty:
        return stats
    as_of_ts = pd.Timestamp(as_of)
    history_cutoff = as_of_ts - pd.Timedelta(days=max(0, int(history_window_days)))
    lookback_cutoff = as_of_ts - pd.Timedelta(days=max(0, int(lookback_days)))

    grouped = frame.groupby(frame["symbol"].astype(str).str.strip(), sort=False)
    for symbol, rows in grouped:
        if not symbol:
            continue
        dates = rows["_date"]
        in_history = dates[dates >= history_cutoff]
        in_lookback = dates[dates >= lookback_cutoff]
        stats[symbol] = SymbolPitStats(
            symbol=symbol,
            bars_in_window=int(in_history.shape[0]),
            bars_in_lookback=int(in_lookback.shape[0]),
            first_bar_date=(
                in_history.min().date() if not in_history.empty else None
            ),
            last_bar_date=dates.max().date(),
        )
    return stats


def resolve_asof_universe(
    *,
    as_of: date,
    index_symbols: Iterable[str],
    stats: Mapping[str, SymbolPitStats],
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    expected_active_lookback_days: int = DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS,
    delisting_coverage_verified: bool = False,
) -> AsofUniverseSnapshot:
    """按"≤ as_of 可观测事实"生成历史股票池快照。

    ``index_symbols`` 只提供**候选名单**（当前完整索引）；是否入选完全由 stats 决定，
    因此未来上市的票（无 ≤ as_of 的 bar）自然被排除，不依赖任何当前状态名单。
    """
    normalized_index = []
    seen: set[str] = set()
    for raw in index_symbols:
        symbol = str(raw).strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized_index.append(symbol)

    eligible: list[str] = []
    expected_active: list[str] = []
    known_suspended: list[str] = []
    excluded: dict[str, str] = {}
    reason_counts: dict[str, int] = {}
    for symbol in normalized_index:
        stat = stats.get(symbol)
        if stat is None or (stat.bars_in_window <= 0 and stat.last_bar_date is None):
            excluded[symbol] = EXCLUDE_FUTURE_LISTED
            reason_counts[EXCLUDE_FUTURE_LISTED] = (
                reason_counts.get(EXCLUDE_FUTURE_LISTED, 0) + 1
            )
            continue
        if stat.bars_in_window < max(1, int(min_history_days)):
            # 只声称"窗口内历史不足"，不声称原因（新上市 / 长期停牌 / 数据缺口
            # 在一次批量探针里不可区分——蓝图 §P0-04「无法证明时如实标注」）。
            excluded[symbol] = EXCLUDE_INSUFFICIENT_HISTORY
            reason_counts[EXCLUDE_INSUFFICIENT_HISTORY] = (
                reason_counts.get(EXCLUDE_INSUFFICIENT_HISTORY, 0) + 1
            )
            continue
        eligible.append(symbol)
        if stat.bars_in_lookback > 0:
            expected_active.append(symbol)
        else:
            # eligible 但最近窗口内没有任何 bar：停牌/停更，单列且不进分母。
            known_suspended.append(symbol)
            reason_counts["known_suspended"] = reason_counts.get("known_suspended", 0) + 1

    snapshot_id = _snapshot_id(
        as_of=as_of,
        index_count=len(normalized_index),
        eligible=eligible,
        expected_active=expected_active,
        min_history_days=min_history_days,
        expected_active_lookback_days=expected_active_lookback_days,
    )
    return AsofUniverseSnapshot(
        universe_snapshot_id=snapshot_id,
        as_of=as_of,
        eligible_symbols=tuple(eligible),
        expected_active_symbols=tuple(expected_active),
        known_suspended_symbols=tuple(known_suspended),
        excluded_reasons=excluded,
        reason_counts=reason_counts,
        index_symbol_count=len(normalized_index),
        min_history_days=int(min_history_days),
        expected_active_lookback_days=int(expected_active_lookback_days),
        survivorship_coverage=(
            COVERAGE_COMPLETE_PIT
            if delisting_coverage_verified
            else COVERAGE_INCOMPLETE_OR_UNKNOWN
        ),
        delisting_coverage_verified=bool(delisting_coverage_verified),
    )


def history_window_days(*, min_history_days: int, lookback_days: int) -> int:
    """窗口的**自然日**长度：把交易日要求放宽 1.6 倍（含周末/节假日），再留 5 天余量。

    只影响"取多长的探针窗口"，不影响判定阈值本身；对 60 个交易日 ≈ 96 自然日。
    """
    trading_days = max(1, int(min_history_days)) + max(0, int(lookback_days))
    return int(trading_days * 1.6) + 5


def _snapshot_id(
    *,
    as_of: date,
    index_count: int,
    eligible: list[str],
    expected_active: list[str],
    min_history_days: int,
    expected_active_lookback_days: int,
) -> str:
    payload = json.dumps(
        {
            "as_of": as_of.isoformat(),
            "index_count": index_count,
            "eligible": sorted(eligible),
            "expected_active": sorted(expected_active),
            "min_history_days": int(min_history_days),
            "lookback": int(expected_active_lookback_days),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"asofuniv_{digest[:16]}"


def empty_stats_window_days() -> int:
    """默认窗口（供调用方在无配置时兜底）。"""
    return history_window_days(
        min_history_days=DEFAULT_MIN_HISTORY_DAYS,
        lookback_days=DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS,
    )


__all__ = [
    "COVERAGE_COMPLETE_PIT",
    "COVERAGE_INCOMPLETE_OR_UNKNOWN",
    "DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS",
    "DEFAULT_MIN_HISTORY_DAYS",
    "EXCLUDE_FUTURE_LISTED",
    "EXCLUDE_NO_BARS",
    "EXCLUDE_INSUFFICIENT_HISTORY",
    "AsofUniverseSnapshot",
    "SymbolPitStats",
    "build_pit_stats",
    "empty_stats_window_days",
    "history_window_days",
    "resolve_asof_universe",
]
