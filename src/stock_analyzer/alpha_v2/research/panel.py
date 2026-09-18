"""Alpha V2 研究面板：PIT 日线面板装载 + 价格口径认证（S11 起共用）。

**职责边界**：本模块只提供"读得到、可复现、口径自述"的原始事实，不做收益计算、
不选股、不训练。所有研究模块（S11 outcome / S12 benchmark / S13 recall /
S15 baseline / S16 head / S19 walk-forward）都从同一个 :class:`DailyPanel`
取数，保证"同一输入 → 同一结论"。

三条纪律：

1. **只用 ≤ 窗口末端的行**：SQL 谓词写死 ``date <= window_end``，面板里不存在
   未来 bar，下游不可能"不小心"读到未来数据；
2. **涨跌停价必须可推导或 fail-closed**：本地/生产面板的 ``up_limit`` /
   ``down_limit`` 常为空列，此时用 ``limit_rule.build_price_limits`` 的板块
   回退（前收 × 板块幅度）。板块名必须从数据源的英文代码（``main``/``gem``/
   ``star``/``bj``）显式映射到规则表用的中文名——**否则创业板/科创板会按 10%
   计算，把 20% 的一字涨停判成可成交**（见 :data:`BOARD_ALIASES`）；
3. **价格口径不靠猜**：:meth:`DailyPanel.certify_price_mode` 先用行内
   ``price_series_mode`` 自述，缺失时用"当日涨跌幅 vs 板块涨跌停"的**实测一致性
   探针**给出可量化证据；两者都失败就是 ``unverified``，样本进不了主评价集。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.config import LimitRuleConfig
from stock_analyzer.data.asof_universe import (
    AsofUniverseSnapshot,
    SymbolPitStats,
    build_pit_stats,
    history_window_days,
    resolve_asof_universe,
)
from stock_analyzer.data.limit_rule import build_price_limits

# 面板 bar 列（显式契约：缺列即报错，不静默补 NaN）
PANEL_BAR_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "board",
    "is_st",
    "is_delisting_risk",
    "suspended",
    "pre_close",
    "up_limit",
    "down_limit",
    "price_series_mode",
)

# ``pre_close`` 的来源口径（审计字段）：交易所前收（除权除息后）与自己算的上一根
# raw 收盘**不是一回事**——前者已除权、后者没有。除权日二者不等，用它算涨跌停价
# 会把当天的一字涨停判错。
PRE_CLOSE_SOURCE_FIELD = "source"
PRE_CLOSE_SOURCE_DERIVED = "derived_previous_close"

# 数据源板块代码 → 涨跌停规则表的板块名（limit_rule._normalize_board 认识的写法）。
# 缺了这一层，``gem``/``star`` 会在规则表里查不到，回退到默认 10%——
# 与真实的 20% 相差一倍，属"把 20% 一字涨停当可成交"的高危错误。
BOARD_ALIASES: dict[str, str] = {
    "main": "主板",
    "sz_main": "主板",
    "sh_main": "主板",
    "gem": "创业板",
    "chinext": "创业板",
    "star": "科创板",
    "kcb": "科创板",
    "bj": "北交所",
    "bse": "北交所",
}

PRICE_MODE_RAW = "raw"
PRICE_MODE_QFQ = "qfq"

CERT_SOURCE_ROW_DECLARED = "panel_row_declared"
CERT_SOURCE_EMPIRICAL = "empirical_limit_consistency"
CERT_SOURCE_UNAVAILABLE = "unavailable"

DEFAULT_EXCHANGE_BY_PREFIX: tuple[tuple[tuple[str, ...], str], ...] = (
    (("60", "68", "51", "58"), "SH"),
    (("00", "30", "12", "15", "16", "18"), "SZ"),
    (("8", "4", "9"), "BJ"),
)


def normalize_board(value: object, *, symbol: object = "") -> str:
    """把数据源板块代码规范成涨跌停规则表的板块名。"""
    raw = str(value or "").strip()
    if not raw:
        text = str(symbol or "").strip()
        if text.startswith("688"):
            return "科创板"
        if text.startswith(("300", "301")):
            return "创业板"
        if text.startswith(("8", "4")):
            return "北交所"
        return "主板"
    return BOARD_ALIASES.get(raw.lower(), raw)


def exchange_of(symbol: str) -> str:
    """按代码前缀推断交易所（用于分组统计；不影响任何判定）。"""
    text = str(symbol).strip()
    for prefixes, exchange in DEFAULT_EXCHANGE_BY_PREFIX:
        if text.startswith(prefixes):
            return exchange
    return "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PriceModeCertification:
    """价格口径认证结果（可审计：给了什么证据、阈值多少、样本多少）。"""

    mode: str
    source: str
    certified: bool
    evidence: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "price_mode": self.mode,
            "price_mode_source": self.source,
            "price_mode_certified": self.certified,
            "price_mode_evidence": dict(self.evidence),
        }


@dataclass
class DailyPanel:
    """一段窗口的 PIT 日线面板（单一事实来源）。"""

    bars: pd.DataFrame
    calendar: tuple[date, ...]
    symbols: tuple[str, ...]
    source: str
    window_start: date
    window_end: date
    warmup_days: int = 0
    _by_symbol: dict[str, pd.DataFrame] | None = None

    # -- 基本查询 ---------------------------------------------------------
    def symbol_bars(self, symbol: str) -> pd.DataFrame | None:
        """单票 bars（按 trade_date 升序、DatetimeIndex）。"""
        if self._by_symbol is None:
            grouped = {
                str(name): frame.sort_values("trade_date").set_index("trade_date")
                for name, frame in self.bars.groupby("symbol", sort=False)
            }
            self._by_symbol = grouped
        return self._by_symbol.get(str(symbol))

    def calendar_index(self, value: date) -> int:
        try:
            return self.calendar.index(value)
        except ValueError:
            return -1

    def session_after(self, value: date, *, count: int) -> list[date]:
        """``value`` 之后的 count 个交易日（不足则返回实际数量）。"""
        index = self.calendar_index(value)
        if index < 0:
            future = [item for item in self.calendar if item > value]
            return future[:count]
        return list(self.calendar[index + 1 : index + 1 + count])

    def public_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "warmup_days": int(self.warmup_days),
            "symbols": len(self.symbols),
            "trading_dates": len(self.calendar),
            "bars": int(len(self.bars)),
        }

    # -- PIT universe -----------------------------------------------------
    def pit_universe(
        self,
        *,
        as_of: date,
        min_history_days: int = 60,
        expected_active_lookback_days: int = 5,
    ) -> AsofUniverseSnapshot:
        """复用 S03 的 PIT 语义从面板构造 as_of 时点股票池。

        ``index_symbols`` 只当作候选名单；是否入选完全由 ≤ as_of 的 bar 事实决定，
        所以未来上市票（无 ≤ as_of 的 bar）与退出票都不会被算进来。

        注意：``build_pit_stats`` 认的是 ``date`` 列名，面板用的是 ``trade_date``
        （更贴近上游口径）。这里显式改名而不是让两边各自猜——列名对不上会让
        stats 静默变空，整个 PIT 池被误判成 ``future_listed``（2026-09-18 实测复现）。
        """
        metrics = self.bars.loc[
            self.bars["trade_date"] <= pd.Timestamp(as_of), ["symbol", "trade_date"]
        ].rename(columns={"trade_date": "date"})
        stats = build_pit_stats(
            metrics=metrics,
            as_of=as_of,
            lookback_days=expected_active_lookback_days,
            history_window_days=history_window_days(
                min_history_days=min_history_days, lookback_days=expected_active_lookback_days
            ),
        )
        return resolve_asof_universe(
            as_of=as_of,
            index_symbols=self.symbols,
            stats=stats,
            min_history_days=min_history_days,
            expected_active_lookback_days=expected_active_lookback_days,
        )

    def pit_stats(
        self, *, as_of: date, lookback_days: int = 5, history_window: int = 100
    ) -> dict[str, SymbolPitStats]:
        """返回 as_of 时点的 per-symbol 事实（与 :meth:`pit_universe` 同源）。"""
        metrics = self.bars.loc[
            self.bars["trade_date"] <= pd.Timestamp(as_of), ["symbol", "trade_date"]
        ].rename(columns={"trade_date": "date"})
        return build_pit_stats(
            metrics=metrics,
            as_of=as_of,
            lookback_days=lookback_days,
            history_window_days=history_window,
        )

    # -- 价格口径认证 -----------------------------------------------------
    def certify_price_mode(
        self,
        *,
        declared_by_config: str = "",
        max_violation_ratio: float = 0.005,
        min_sample: int = 2000,
        limit_tolerance: float = 0.01,
    ) -> PriceModeCertification:
        """判定执行价序列是否为 raw（fail-closed，证据可量化）。

        两条独立证据来源，任一成立即 ``certified``：

        1. **行内自述**：面板 ``price_series_mode`` 列全部取同一值且为 ``raw``
           （``qfq`` 直接判非 raw；空/多值则看第 2 条）；
        2. **实测一致性**：统计"当日涨跌幅超过板块涨跌停幅度（1% 容差）"的
           ``(symbol, date)`` 占比。raw 序列上该占比≈0（只有上市首日无涨跌幅
           等极少数例外）；前复权序列在每次除权日都会产生超限跳变，占比会到
           千分之几以上。样本不足或占比超阈值 → 不认证。

        任何一条为假都不算认证：**声明的 raw 必须与探针结论一致**，避免
        "配置说 raw、数据其实是复权"这种静默错配。
        """
        declared = [
            str(value).strip().lower()
            for value in self.bars.get("price_series_mode", pd.Series(dtype=object))
            .dropna()
            .unique()
            if str(value).strip()
        ]
        declared_modes = tuple(sorted(set(declared)))
        probe = self._limit_consistency_probe(
            max_violation_ratio=max_violation_ratio,
            min_sample=min_sample,
            limit_tolerance=limit_tolerance,
        )
        evidence: dict[str, object] = {
            "panel_declared_modes": list(declared_modes),
            "config_declared_execution_mode": str(declared_by_config or ""),
            **probe,
        }

        if declared_modes and all(mode == PRICE_MODE_RAW for mode in declared_modes):
            evidence["decision_rule"] = "panel_rows_declare_raw"
            return PriceModeCertification(
                mode=PRICE_MODE_RAW,
                source=CERT_SOURCE_ROW_DECLARED,
                certified=True,
                evidence=evidence,
            )
        if declared_modes:
            # 行内明确声明了非 raw（qfq）：不允许探针翻案。
            evidence["decision_rule"] = "panel_rows_declared_non_raw"
            return PriceModeCertification(
                mode=declared_modes[0],
                source=CERT_SOURCE_ROW_DECLARED,
                certified=False,
                evidence=evidence,
            )
        if probe.get("probe_passed") is True:
            evidence["decision_rule"] = "empirical_probe_passed"
            return PriceModeCertification(
                mode=PRICE_MODE_RAW,
                source=CERT_SOURCE_EMPIRICAL,
                certified=True,
                evidence=evidence,
            )
        evidence["decision_rule"] = "empirical_probe_failed"
        # 探针**跑过**（有样本）就标明证据来源是实测，只是结论为否；
        # 一点样本都拿不到才是真正的 unavailable。
        measured = int(probe.get("probe_sample", 0) or 0) > 0
        return PriceModeCertification(
            mode="unknown",
            source=CERT_SOURCE_EMPIRICAL if measured else CERT_SOURCE_UNAVAILABLE,
            certified=False,
            evidence=evidence,
        )

    def _limit_consistency_probe(
        self,
        *,
        max_violation_ratio: float,
        min_sample: int,
        limit_tolerance: float,
    ) -> dict[str, object]:
        """实测：全样本中"涨跌幅超过板块涨跌停幅度"的比例。"""
        frame = self.bars
        if frame.empty:
            return {"probe_passed": False, "probe_reason": "empty_panel", "probe_sample": 0}
        needed = {"close", "prev_close_raw", "board", "is_st"}
        if not needed.issubset(set(frame.columns)):
            return {
                "probe_passed": False,
                "probe_reason": "missing_columns_for_probe",
                "probe_missing": sorted(needed - set(frame.columns)),
                "probe_sample": 0,
            }
        sample = frame.dropna(subset=["close", "prev_close_raw"]).copy()
        sample = sample[(sample["close"] > 0) & (sample["prev_close_raw"] > 0)]
        if sample.empty:
            return {"probe_passed": False, "probe_reason": "no_usable_rows", "probe_sample": 0}

        limit_pct = LimitRuleConfig()
        per_board: dict[str, dict[str, float]] = {}
        violations = 0
        for board_name, group in sample.groupby("board", sort=False):
            resolved = _board_limit_pct(
                limit_pct,
                board=str(board_name),
                trade_date=self.window_end,
                is_st=False,
            )
            if resolved is None:
                continue
            move = (group["close"] / group["prev_close_raw"] - 1.0).abs()
            bad = int((move > resolved + limit_tolerance).sum())
            violations += bad
            per_board[str(board_name)] = {
                "limit_pct": float(resolved),
                "rows": float(len(group)),
                "violations": float(bad),
                "violation_ratio": round(bad / len(group), 6) if len(group) else 0.0,
            }
        total = len(sample)
        ratio = violations / total if total else 1.0
        enough = total >= max(1, int(min_sample))
        return {
            "probe_passed": bool(enough and ratio <= float(max_violation_ratio)),
            "probe_sample": int(total),
            "probe_min_sample": int(min_sample),
            "probe_violations": int(violations),
            "probe_violation_ratio": round(float(ratio), 6),
            "probe_max_violation_ratio": float(max_violation_ratio),
            "probe_limit_tolerance": float(limit_tolerance),
            "probe_per_board": per_board,
            "probe_reason": (
                "ok"
                if enough and ratio <= float(max_violation_ratio)
                else ("insufficient_sample" if not enough else "violation_ratio_above_threshold")
            ),
        }


def _board_limit_pct(
    config: LimitRuleConfig, *, board: object, trade_date: date, is_st: bool
) -> float | None:
    """按板块取涨跌停幅度：直接复用 limit_rule 的规则表（单一真相源）。"""
    from stock_analyzer.data.limit_rule import resolve_limit_pct

    return resolve_limit_pct(
        config=config,
        trade_date=trade_date,
        board=normalize_board(board),
        is_st=bool(is_st),
        listing_days=None,
    )


def load_daily_panel(
    *,
    market_db: str | Path,
    window_start: date,
    window_end: date,
    warmup_days: int = 30,
    symbols: Sequence[str] | None = None,
    max_symbols: int = 0,
    source: str = "market_duckdb",
) -> DailyPanel:
    """从 DuckDB 日线表装载面板（含 warmup 前的行用于算前收/滚动特征）。

    ``warmup_days`` 的自然日长度行只用于派生 ``prev_close`` 与滚动统计，**不会**
    出现在 :attr:`DailyPanel.calendar` 里（日历只覆盖 ``window_start..window_end``），
    因此下游按日历遍历时天然不会消费 warmup 段。
    """
    import duckdb

    warmup_start = window_start - timedelta(days=max(0, int(warmup_days)))
    available = _available_columns(str(market_db), "daily_bars")
    # 列名映射：daily_bars 的日期列叫 ``date``，面板统一叫 ``trade_date``。
    # 少这一层别名会让投影里根本没有日期列，装载结果静默变空。
    projection: list[str] = []
    missing: list[str] = []
    for column in PANEL_BAR_COLUMNS:
        source_column = "date" if column == "trade_date" else column
        if source_column in available:
            projection.append(f"{source_column} AS {column}" if source_column != column else column)
        else:
            missing.append(column)
    for column in ("up_limit", "down_limit"):
        if column not in available and column not in projection:
            missing.append(column)

    clauses = ["date >= CAST(? AS DATE)", "date <= CAST(? AS DATE)"]
    params: list[object] = [warmup_start.isoformat(), window_end.isoformat()]
    normalized_symbols = [str(item).strip() for item in (symbols or []) if str(item).strip()]
    if normalized_symbols:
        placeholders = ", ".join("?" for _ in normalized_symbols)
        clauses.append(f"symbol IN ({placeholders})")
        params.extend(normalized_symbols)

    con = duckdb.connect(str(market_db), read_only=True)
    try:
        frame = con.execute(
            f"""
            SELECT {", ".join(projection)}
            FROM daily_bars
            WHERE {" AND ".join(clauses)}
            ORDER BY symbol, date
            """,
            params,
        ).fetch_df()
    finally:
        con.close()

    for column in PANEL_BAR_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "symbol"])
    frame["symbol"] = frame["symbol"].astype(str)
    frame = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    frame["board"] = [
        normalize_board(value, symbol=symbol)
        for value, symbol in zip(frame["board"], frame["symbol"], strict=True)
    ]
    # 上一根 raw 收盘（用于涨跌停回退与前收基准）。
    frame["prev_close_raw"] = frame.groupby("symbol", sort=False)["close"].shift(1)
    # pre_close：数据源给了就用数据源的（已除权，权威），否则退回上一根 raw 收盘。
    # 两者语义不同，来源必须落列，否则除权日的一字涨停会被判成可成交。
    if "pre_close" in available:
        frame["pre_close_source"] = PRE_CLOSE_SOURCE_FIELD
        frame["pre_close"] = pd.to_numeric(frame["pre_close"], errors="coerce")
        missing_pre_close = frame["pre_close"].isna()
        frame.loc[missing_pre_close, "pre_close"] = frame.loc[missing_pre_close, "prev_close_raw"]
        frame.loc[missing_pre_close, "pre_close_source"] = PRE_CLOSE_SOURCE_DERIVED
    else:
        frame["pre_close"] = frame["prev_close_raw"]
        frame["pre_close_source"] = PRE_CLOSE_SOURCE_DERIVED
    # listing_days 下界：面板内该票截至当日的 bar 数（窗口外历史不可见）。
    frame["listing_days_lower_bound"] = frame.groupby("symbol", sort=False).cumcount() + 1

    if max_symbols and int(max_symbols) > 0:
        keep = sorted(frame["symbol"].unique())[: int(max_symbols)]
        frame = frame[frame["symbol"].isin(keep)].reset_index(drop=True)

    in_window = frame["trade_date"].dt.date
    calendar = tuple(sorted({day for day in in_window if window_start <= day <= window_end}))
    panel = DailyPanel(
        bars=frame,
        calendar=calendar,
        symbols=tuple(sorted(frame["symbol"].unique())),
        source=source,
        window_start=window_start,
        window_end=window_end,
        warmup_days=int(warmup_days),
    )
    panel.panel_schema = {  # type: ignore[attr-defined]
        "requested_columns": list(PANEL_BAR_COLUMNS),
        "available_columns": sorted(available),
        "missing_columns": missing,
    }
    return panel


def _available_columns(market_db: str, table: str) -> set[str]:
    import duckdb

    con = duckdb.connect(str(market_db), read_only=True)
    try:
        rows = con.execute(f"DESCRIBE {table}").fetchall()
    finally:
        con.close()
    return {str(row[0]) for row in rows}


def bar_view(
    bar: Any,
    *,
    board: object = "",
    symbol: str = "",
    listing_days: int | None = None,
) -> dict[str, object]:
    """把一行 panel 数据转成 ``ExecutionEngine`` / ``limit_rule`` 认识的 bar 视图。

    显式带上 ``pre_close`` / ``board`` / ``listing_days``——这三项决定涨跌停价能否
    正确推导；缺 ``pre_close`` 时引擎会 fail-closed（``no_valid_price_data``），
    正是 S02/S07 想要的语义，但研究侧应当尽量给全，避免把"数据没给"误记成"不可成交"。
    """
    payload = {
        "symbol": symbol or str(bar.get("symbol", "") or ""),
        "board": normalize_board(bar.get("board") if board == "" else board, symbol=symbol),
        "open": bar.get("open"),
        "high": bar.get("high"),
        "low": bar.get("low"),
        "close": bar.get("close"),
        "volume": bar.get("volume"),
        "suspended": bool(bar.get("suspended", False)),
        "is_st": bool(bar.get("is_st", False)),
        "up_limit": bar.get("up_limit"),
        "down_limit": bar.get("down_limit"),
        "pre_close": bar.get("pre_close"),
        "trade_date": bar.get("trade_date") or bar.get("date"),
    }
    if listing_days is not None:
        payload["listing_days"] = int(listing_days)
    return payload


def panel_limit_snapshot(
    bar: Any,
    *,
    limit_config: LimitRuleConfig | None = None,
    symbol: str = "",
    listing_days: int | None = None,
) -> dict[str, object]:
    """算出一根 bar 的涨跌停价与来源（审计用，不参与判定）。"""
    view = bar_view(bar, symbol=symbol, listing_days=listing_days)
    limits = build_price_limits(bar=view, config=limit_config or LimitRuleConfig())
    return {
        "up_limit": limits.up_limit,
        "down_limit": limits.down_limit,
        "limit_pct": limits.limit_pct,
        "limit_source": limits.source,
    }


def panel_fingerprint(panel: DailyPanel) -> str:
    """面板指纹：窗口 + 票数 + 行数 + 价格口径列的确定性摘要（不哈希全量数据）。"""
    payload = {
        "window": [panel.window_start.isoformat(), panel.window_end.isoformat()],
        "symbols": len(panel.symbols),
        "calendar": len(panel.calendar),
        "bars": int(len(panel.bars)),
        "columns": sorted(str(column) for column in panel.bars.columns),
        "source": panel.source,
    }
    import hashlib

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def iter_symbol_frames(panel: DailyPanel) -> Iterable[tuple[str, pd.DataFrame]]:
    """按 symbol 迭代（升序），供逐票计算使用。"""
    for symbol, frame in panel.bars.groupby("symbol", sort=True):
        yield str(symbol), frame.sort_values("trade_date").set_index("trade_date")


__all__ = [
    "BOARD_ALIASES",
    "CERT_SOURCE_EMPIRICAL",
    "CERT_SOURCE_ROW_DECLARED",
    "CERT_SOURCE_UNAVAILABLE",
    "DailyPanel",
    "PANEL_BAR_COLUMNS",
    "PRICE_MODE_QFQ",
    "PRICE_MODE_RAW",
    "PriceModeCertification",
    "bar_view",
    "exchange_of",
    "iter_symbol_frames",
    "load_daily_panel",
    "normalize_board",
    "panel_fingerprint",
    "panel_limit_snapshot",
]
