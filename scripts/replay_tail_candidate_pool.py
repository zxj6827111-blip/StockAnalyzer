"""CLI：按历史每一个交易日**重放**夜扫决策，产出尾盘重建所需的请求（改进计划 §4）。

``rebuild_tail_labels.py`` 吃 ``--requests``、``validate_tail_selection_quality.py`` 吃
``--samples``，但此前仓库里**没有任何生产者**按历史日期生成这两样东西 —— 所以
"≥4 折 / +5pp / 影子 60 天"一直只是没有输入的门槛。本脚本补的就是这个生产者：

```bash
python scripts/replay_tail_candidate_pool.py \\
    --warehouse artifacts/research/market_copy.duckdb \\
    --start 2025-01-06 --end 2026-06-30 \\
    --symbols-file artifacts/research/tail_symbols.txt \\
    --requests artifacts/research/tail_requests.jsonl \\
    --report artifacts/research/tail_replay_report.json
```

三条不许含糊的口径：

1. **只用 as-of 当日已收盘的日线**。四组特征里依赖日内分钟的两列
   （``last30_volume_share`` / ``tail_volatility_ratio``）在日频重放里**不可复现**，
   一律留 NaN 并写进报告，不填 0、也不进 ``--features``。
2. **截断用流动性容量排序，不是旧综合分**。历史归档里没有当时的 composite_score，
   所以这里明确叫 ``capacity_rank = avg_turnover_20``；旧排序的对照只能在
   有留档的那段历史上做，缺留档就如实报告缺（计划 §4）。
3. **硬门用契约里已登记的名字**，未登记的名字会被 ``apply_hard_gates`` 拒绝执行。
   缺 bar 不等于停牌：只有仓库显式声明 ``suspended`` 才按停牌出局，
   当日没有 bar 只是"这一天的池子里没有它"。

退出码：``0`` 产出了请求；``5`` 输入不可用（仓库读不到、日期区间无交易日、符号集为空）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.feature.trend_candidate_contract import (  # noqa: E402
    HARD,
    HARD_GATE_ATTRIBUTION_ORDER,
    TREND_FEATURE_CONTRACT_VERSION,
    FeatureAvailability,
    apply_hard_gates,
    build_trend_feature_frame,
    classify_rule,
    compute_then_truncate,
)
from stock_analyzer.research.tail_rebuild import RebuildRequest  # noqa: E402

#: as-of 时点历史 bar 不足 MIN_HISTORY_BARS 的票被剔出候选，但它们是"考虑过的"：
#: 前两层留档要把这类淘汰记成**原因**，而不是让它们在数字里消失。
PIT_REJECT_REASON = "insufficient_history_at_asof"

RC_OK = 0
RC_ERROR = 5

#: 日频重放拿不到的列 —— 它们是日内分钟统计，硬凑就是 look-ahead 的反向版本。
NOT_REPRODUCIBLE_AT_DAILY = ("last30_volume_share", "tail_volatility_ratio")
MIN_HISTORY_BARS = 120
#: 仓库的 daily_bars 里混着指数代码（如 899050 = 北证50）。股票池必须按板块前缀过滤，
#: 否则"全市场硬性资格检查"的第一层就把指数当股票选进来了。
A_SHARE_CODE_PREFIXES = ("60", "00", "30", "68", "43", "83", "87", "92")
#: 过热硬门：20 日涨幅与 60 日区间位置同时逼近极端时先出局（留档可复查阈值）。
OVEREXTENSION_RET_20 = 0.40
OVEREXTENSION_RANGE_POSITION = 0.98
OVEREXTENSION_ATR_PCT = 0.06
#: 距上一根日线的日历缺口超过这个天数 = 数据停更，不参与当日池。
#: 这不是停牌判定 —— 停牌只认仓库里显式声明的 ``suspended``（计划 §3.1）。
MAX_BAR_GAP_CALENDAR_DAYS = 30

FEATURE_COLUMNS: tuple[str, ...] = (
    "excess_ret_5", "excess_ret_20", "excess_ret_60", "relative_strength",
    "rs_ma5", "rs_ma20", "rolling_beta_60", "excess_vol", "market_trend",
    "ma5", "ma10", "ma20", "ma60", "close_to_ma20", "close_to_ma60",
    "ma20_slope", "range_position_60", "rank_ret_20",
    "volume_ratio_5", "turnover", "avg_turnover_20", "float_market_cap",
    "amount_to_float_cap", "positive_bar_ratio", "last30_volume_share",
    "atr14_pct", "realized_vol_20", "range_pct", "ret_5", "ret_10",
    "gap_up_pct", "close_position", "tail_volatility_ratio",
)


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value).strip())


def _suffix(symbol: str) -> str:
    code = str(symbol).strip().split(".")[0].zfill(6)
    if code[0] in ("6", "9"):
        return f"{code}.SH"
    if code[0] in ("4", "8"):
        return f"{code}.BJ"
    return f"{code}.SZ"


def load_panel(warehouse: Path, symbols: Iterable[str], start: date, end: date) -> pd.DataFrame:
    """≤ end 的日线；warm-up 从 start 再往前推一年，滚动窗口才有足够历史。"""
    wanted = [str(item).strip().split(".")[0].zfill(6) for item in symbols if str(item).strip()]
    if not wanted:
        return pd.DataFrame()
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        placeholders = ",".join("?" for _ in wanted)
        frame = con.execute(
            f"""
            SELECT symbol, date, open, high, low, close, volume, turnover,
                   float_market_cap, is_st, is_delisting_risk, suspended
            FROM daily_bars
            WHERE substr(CAST(symbol AS VARCHAR), 1, 6) IN ({placeholders})
              AND date BETWEEN ? AND ?
            ORDER BY symbol, date
            """,
            [*wanted, start - timedelta(days=380), end],
        ).fetchdf()
    finally:
        con.close()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].astype(str).str.split(".").str[0].str.zfill(6)
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def load_benchmark(warehouse: Path, index_code: str, start: date, end: date) -> pd.DataFrame:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        frame = con.execute(
            """
            SELECT trade_date, close FROM index_daily
            WHERE index_code = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date
            """,
            [index_code, start - timedelta(days=380), end],
        ).fetchdf()
    finally:
        con.close()
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def _rolling(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window)


def build_features(panel: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """按符号分组算四组特征。跨截面项（relative_strength / rank_ret_20）按日算。"""
    frame = panel.copy()
    bench_indexed = (
        benchmark.set_index("trade_date")["close"].sort_index() if not benchmark.empty else None
    )
    bench = bench_indexed
    groups = []
    for _symbol, rows in frame.groupby("symbol", sort=True):
        rows = rows.sort_values("date").reset_index(drop=True)
        close = rows["close"].astype(float)
        ret_1 = close.pct_change()
        rows["ret_1"] = ret_1
        rows["ret_5"] = close / close.shift(5) - 1.0
        rows["ret_10"] = close / close.shift(10) - 1.0
        rows["ret_20_raw"] = close / close.shift(20) - 1.0
        rows["ret_60_raw"] = close / close.shift(60) - 1.0
        for name, base, window in (
            ("excess_ret_5", "ret_5", 5),
            ("excess_ret_20", "ret_20_raw", 20),
            ("excess_ret_60", "ret_60_raw", 60),
        ):
            if bench is None:
                rows[name] = np.nan
                continue
            rows[name] = rows[base] - rows["date"].map(bench.pct_change(window))
        if bench is not None:
            bench_ma5 = _rolling(bench, 5).mean()
            bench_ma20 = _rolling(bench, 20).mean()
            bench_vol20 = _rolling(bench.pct_change(), 20).std() * np.sqrt(252)
            bench_ret20 = bench / bench.shift(20) - 1.0
            rows["rs_ma5"] = _rolling(close, 5).mean() / rows["date"].map(bench_ma5)
            rows["rs_ma20"] = _rolling(close, 20).mean() / rows["date"].map(bench_ma20)
            rows["market_trend"] = rows["date"].map(bench_ret20)
            cov = _rolling(rows["ret_1"], 60).cov(rows["date"].map(bench.pct_change()))
            var = _rolling(rows["date"].map(bench.pct_change()), 60).var()
            rows["rolling_beta_60"] = cov / var
            rows["realized_vol_20"] = _rolling(rows["ret_1"], 20).std() * np.sqrt(252)
            rows["excess_vol"] = rows["realized_vol_20"] - rows["date"].map(bench_vol20)
        else:
            for name in ("rs_ma5", "rs_ma20", "market_trend", "rolling_beta_60", "excess_vol"):
                rows[name] = np.nan
            rows["realized_vol_20"] = _rolling(rows["ret_1"], 20).std() * np.sqrt(252)
        for window in (5, 10, 20, 60):
            rows[f"ma{window}"] = _rolling(close, window).mean()
        rows["close_to_ma20"] = close / rows["ma20"] - 1.0
        rows["close_to_ma60"] = close / rows["ma60"] - 1.0
        rows["ma20_slope"] = rows["ma20"] / rows["ma20"].shift(5) - 1.0
        high_60 = _rolling(rows["high"].astype(float), 60).max()
        low_60 = _rolling(rows["low"].astype(float), 60).min()
        rows["range_position_60"] = (close - low_60) / (high_60 - low_60).replace(0.0, np.nan)
        rows["volume_ratio_5"] = rows["volume"].astype(float) / _rolling(
            rows["volume"].astype(float), 5
        ).mean()
        rows["avg_turnover_20"] = _rolling(rows["turnover"].astype(float), 20).mean()
        cap = rows["float_market_cap"].replace(0.0, np.nan)
        rows["amount_to_float_cap"] = rows["turnover"] / cap
        rows["positive_bar_ratio"] = _rolling((close > rows["open"]).astype(float), 20).mean()
        tr = (rows["high"] - rows["low"]).astype(float)
        rows["atr14_pct"] = _rolling(tr, 14).mean() / close
        rows["range_pct"] = (rows["high"] - rows["low"]) / close
        rows["gap_up_pct"] = rows["open"] / close.shift(1) - 1.0
        day_range = (rows["high"] - rows["low"]).replace(0.0, np.nan)
        rows["close_position"] = (close - rows["low"]) / day_range
        rows["prev_bar_date"] = rows["date"].shift(1)
        for name in NOT_REPRODUCIBLE_AT_DAILY:
            rows[name] = np.nan
        groups.append(rows)
    if not groups:
        return pd.DataFrame()
    engineered = pd.concat(groups, ignore_index=True)
    by_day = engineered.groupby("date")
    engineered["relative_strength"] = by_day["excess_ret_20"].rank(pct=True)
    engineered["rank_ret_20"] = by_day["ret_20_raw"].rank(pct=True)
    return engineered


def trading_calendar(engineered: pd.DataFrame) -> list[date]:
    return sorted(set(engineered["date"].tolist()))


#: 日内两列的真算口径：尾盘最后 30 分钟（14:30–15:00）相对全天的量占比与波动比。
#: 这两列在契约里本来就有（volume_liquidity / volatility_overheat 两组），
#: 此前只有日频源时**只能留 NaN**；分钟库就位后才第一次真的算得出来。
_INTRADAY_FEATURE_SQL = """
WITH bars AS (
    SELECT symbol, trade_date, bar_time, volume, close,
           LAG(close) OVER (PARTITION BY symbol, trade_date ORDER BY bar_time) AS prev_close,
           CASE WHEN strftime(bar_time, '%H:%M') >= '14:30' THEN 1 ELSE 0 END AS is_tail
    FROM minute_bars_1min
    WHERE trade_date BETWEEN ? AND ?
)
SELECT symbol AS symbol,
       trade_date AS date,
       SUM(CASE WHEN is_tail = 1 THEN volume ELSE 0.0 END)
         / NULLIF(SUM(volume), 0.0) AS last30_volume_share,
       STDDEV_SAMP(CASE WHEN is_tail = 1 AND prev_close > 0 THEN close / prev_close - 1.0 END)
         / NULLIF(STDDEV_SAMP(CASE WHEN prev_close > 0 THEN close / prev_close - 1.0 END), 0.0)
           AS tail_volatility_ratio
FROM bars
GROUP BY symbol, trade_date
"""


def load_intraday_features(minute_db: str | Path, start: date, end: date) -> pd.DataFrame:
    """从独立研究库的分钟 bar 聚合出日内两列；库不可用时返回空帧（调用方保持 NaN）。"""
    path = Path(str(minute_db)).expanduser()
    if not path.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(path), read_only=True)
    try:
        frame = con.execute(_INTRADAY_FEATURE_SQL, [start, end]).fetch_df()
    finally:
        con.close()
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.split(".").str[0].str.zfill(6)
    return frame[["symbol", "date", "last30_volume_share", "tail_volatility_ratio"]]


def _universe_fact(
    *,
    decision_date: date,
    warehouse: Path | str,
    considered: Sequence[str],
    advanced: Sequence[str],
    rejected: Mapping[str, Sequence[str]],
    pit_excluded: Sequence[str],
    coverage: str,
    delisting_verified: bool,
) -> dict[str, Any]:
    """一个决策日的**符号级** universe / hard_eligibility 事实（计划 §2 前两层）。

    生产夜扫的选择器只给逐原因计数，而 ``StageTrace`` 不许拿计数冒充成员，
    所以留档用的事实由这条研究侧路径自己产。一只票被多条硬门同时淘汰时只记
    **第一条**原因：``StageTrace`` 的计数恒等式要求"晋级 + 各原因淘汰 = 输入"，
    一只票进两个桶会让留档自相矛盾，宁可不落。
    """
    first_reason: dict[str, str] = {}
    # 归因顺序来自契约，不来自 rejected 的插入顺序：换一行代码就改数会让留档说谎。
    precedence = [name for name in HARD_GATE_ATTRIBUTION_ORDER if name in rejected]
    precedence += sorted(set(str(name) for name in rejected) - set(precedence))
    for rule in precedence:
        for symbol in rejected[rule]:
            first_reason.setdefault(str(symbol), str(rule))
    for symbol in pit_excluded:
        first_reason.setdefault(str(symbol), PIT_REJECT_REASON)
    undeclared = sorted({reason for reason in first_reason.values()
                         if classify_rule(reason) != HARD})
    if undeclared:
        # 留档 vocabulary 必须闭合：这条层记的是**硬性资格检查**，写进未登记的名字
        # （或被归为 predictive 的旧预测规则）会让 §2 的原因分布与线上不是一套语言，
        # 消融实验也会漏掉它。
        raise SystemExit(f"硬门留档里出现非 HARD 规则名: {undeclared}")
    universe = sorted(set(considered))
    return {
        "decision_date": decision_date.isoformat(),
        "as_of": decision_date.isoformat(),
        "universe_snapshot_id": f"replay:{Path(str(warehouse)).name}:{decision_date.isoformat()}",
        "eligible_symbols": universe,
        "expected_active_symbols": sorted(set(advanced)),
        # 缺 bar 不等于停牌：这一份清单只在有正向停牌证据时才填。
        "known_suspended_symbols": [],
        "excluded_reasons": {
            symbol: first_reason[symbol] for symbol in sorted(first_reason)
            if symbol in set(universe)
        },
        "survivorship_coverage": coverage,
        "delisting_coverage_verified": bool(delisting_verified),
    }


def daily_gates(
    rows: pd.DataFrame, *, min_turnover: float, min_float_cap: float
) -> dict[str, tuple[str, ...]]:
    """只用契约里已登记为 hard_gate 的规则名。

    布尔列在仓库里可空：NULL 一律按"没有停牌/ST 声明"处理（不当真、也不报错），
    但缺 bar 不会走到这里 —— 那由 ``stale_market_data`` 单独出局。
    """
    def dropped(mask: pd.Series) -> tuple[str, ...]:
        return tuple(sorted({str(s) for s in rows.loc[mask.fillna(False).astype(bool), "symbol"]}))

    def declared_true(column: str) -> pd.Series:
        return pd.to_numeric(rows[column], errors="coerce").fillna(0) > 0

    return {
        "board_eligibility": dropped(
            ~rows["symbol"].astype(str).str.slice(0, 2).isin(A_SHARE_CODE_PREFIXES)
        ),
        "is_st": dropped(declared_true("is_st")),
        "is_delisting_risk": dropped(declared_true("is_delisting_risk")),
        "suspended": dropped(declared_true("suspended")),
        "min_avg_turnover_20": dropped(rows["avg_turnover_20"] < min_turnover),
        "min_float_market_cap": dropped(rows["float_market_cap"] < min_float_cap),
        "stale_market_data": dropped(
            pd.to_datetime(rows["date"]) - pd.to_datetime(rows["prev_bar_date"])
            > pd.Timedelta(days=MAX_BAR_GAP_CALENDAR_DAYS)
        ),
        "overextension_risk": dropped(
            (rows["ret_20_raw"] > OVEREXTENSION_RET_20)
            & (rows["range_position_60"] > OVEREXTENSION_RANGE_POSITION)
            & (rows["atr14_pct"] > OVEREXTENSION_ATR_PCT)
        ),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    warehouse = Path(str(args.warehouse)).expanduser()
    if not warehouse.exists():
        raise SystemExit(f"warehouse not found: {warehouse}")
    symbols = [line.strip() for line in Path(str(args.symbols_file)).expanduser().read_text(
        encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    start, end = _parse_date(args.start), _parse_date(args.end)
    panel = load_panel(warehouse, symbols, start, end)
    if panel.empty:
        raise SystemExit("no daily bars in the requested window — 先确认符号清单与仓库路径")
    benchmark = load_benchmark(warehouse, args.benchmark_code, start, end)
    engineered = build_features(panel, benchmark)
    # 日内两列：分钟库就位就真算，拿不到就整列不出现在帧里（不是填 0、也不是留 NaN 占位）。
    engineered = engineered.drop(
        columns=[name for name in NOT_REPRODUCIBLE_AT_DAILY if name in engineered.columns]
    )
    intraday = (
        load_intraday_features(args.minute_db, start - timedelta(days=380), end)
        if str(args.minute_db or "").strip()
        else pd.DataFrame()
    )
    intraday_columns: list[str] = []
    if not intraday.empty:
        engineered = engineered.merge(intraday, on=["symbol", "date"], how="left")
        intraday_columns = [
            name for name in NOT_REPRODUCIBLE_AT_DAILY if name in engineered.columns
        ]
    calendar = trading_calendar(engineered)
    availability_stale = benchmark.empty or (
        (end - benchmark["trade_date"].max()).days > args.max_benchmark_staleness_days
    )
    availability = FeatureAvailability(
        benchmark_available=not benchmark.empty,
        benchmark_code=str(args.benchmark_code),
        benchmark_last_date=benchmark["trade_date"].max() if not benchmark.empty else None,
        stale_benchmark_days=int((end - benchmark["trade_date"].max()).days)
        if not benchmark.empty else 10_000,
        max_benchmark_staleness_days=int(args.max_benchmark_staleness_days),
        notes=(
            ()
            if not availability_stale
            else ("benchmark_missing_or_stale: market_relative 组整体置 NaN",)
        ),
    )

    liquid = engineered.loc[
        (engineered["date"] >= start) & (engineered["date"] <= end), "avg_turnover_20"
    ].dropna()
    cap = engineered.loc[
        (engineered["date"] >= start) & (engineered["date"] <= end), "float_market_cap"
    ].dropna()
    if liquid.empty or cap.empty:
        raise SystemExit("liquidity / float-cap columns are entirely empty — 阈值无从推导")
    min_turnover = float(liquid.quantile(args.min_turnover_quantile))
    min_float_cap = float(cap.quantile(args.min_float_cap_quantile))

    requests: list[RebuildRequest] = []
    gate_totals: dict[str, int] = {}
    day_stats: list[dict[str, Any]] = []
    universe_facts: list[dict[str, Any]] = []
    dates = [item for item in calendar if start <= item <= end]
    date_index = {item: position for position, item in enumerate(calendar)}
    for decision_date in dates:
        rows = engineered.loc[engineered["date"] == decision_date].copy()
        if rows.empty:
            continue
        history = engineered.loc[engineered["date"] <= decision_date]
        bars_in_window = history.groupby("symbol")["date"].count()
        pit_excluded = tuple(
            str(symbol) for symbol in rows["symbol"]
            if int(bars_in_window.get(str(symbol), 0)) < MIN_HISTORY_BARS
        )
        rows = rows.loc[~rows["symbol"].isin(pit_excluded)]
        position = date_index[decision_date]
        if position + 1 >= len(calendar):
            continue  # 入场日不存在：标签不可能成熟，不能拿它当样本
        entry_date = calendar[position + 1]
        frame = build_trend_feature_frame(
            engineered=rows, availability=availability, decision_date=decision_date
        )
        eligible_frame = frame.frame
        gates = daily_gates(eligible_frame, min_turnover=min_turnover, min_float_cap=min_float_cap)
        outcome = apply_hard_gates(symbols=eligible_frame["symbol"].tolist(), gates=gates)
        for rule, names in outcome.rejected.items():
            gate_totals[rule] = gate_totals.get(rule, 0) + len(names)
        scored = {
            str(symbol): float(value)
            for symbol, value in zip(
                eligible_frame["symbol"], eligible_frame["avg_turnover_20"], strict=True
            )
            if pd.notna(value)
        }
        pool = compute_then_truncate(eligible=outcome.eligible, scored=scored, limit=args.pool_size)
        columns = list(frame.feature_columns)
        for symbol in pool:
            row = eligible_frame.loc[eligible_frame["symbol"] == symbol].iloc[-1]
            snapshot = {
                name: (None if pd.isna(row[name]) else float(row[name]))
                for name in columns
                if name in row.index
            }
            requests.append(
                RebuildRequest(
                    symbol=str(symbol),
                    decision_date=decision_date,
                    entry_date=entry_date,
                    overnight_features=snapshot,
                )
            )
        day_stats.append({
            "decision_date": decision_date.isoformat(),
            "entry_date": entry_date.isoformat(),
            "considered": int(len(eligible_frame)),
            "pit_excluded": len(pit_excluded),
            "eligible": len(outcome.eligible),
            "pooled": len(pool),
            "unknown_gate_rules": list(outcome.unknown_rules),
        })
        universe_facts.append(_universe_fact(
            decision_date=decision_date,
            warehouse=warehouse,
            considered=[str(symbol) for symbol in eligible_frame["symbol"]] + list(pit_excluded),
            advanced=[str(symbol) for symbol in outcome.eligible],
            rejected=outcome.rejected,
            pit_excluded=list(pit_excluded),
            coverage=str(args.survivorship_coverage),
            delisting_verified=bool(int(args.delisting_coverage_verified)),
        ))

    if not requests:
        raise SystemExit("重放没有产出任何请求 —— 硬门全灭或日期区间不足")
    universe_facts_path = ""
    if str(args.universe_facts or "").strip():
        # 符号级事实单独成一份 sidecar：夜扫报告里只有计数，而 StageTrace 不许用计数
        # 冒充成员。分开落也避免把上万代码塞进重放报告。
        universe_facts_path = str(Path(str(args.universe_facts)).expanduser())
        Path(universe_facts_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(universe_facts_path).open("w", encoding="utf-8") as handle:
            for fact in universe_facts:
                handle.write(json.dumps(fact, ensure_ascii=False) + "\n")
    return {
        "ok": True,
        "universe_facts_path": universe_facts_path,
        "universe_fact_days": len(universe_facts),
        "universe_symbols_considered": sum(
            len(fact["eligible_symbols"]) for fact in universe_facts
        ),
        "feature_contract_version": TREND_FEATURE_CONTRACT_VERSION,
        "warehouse": str(warehouse),
        "benchmark_code": str(args.benchmark_code),
        "benchmark_available": bool(not benchmark.empty),
        "benchmark_last_date": (
            benchmark["trade_date"].max().isoformat() if not benchmark.empty else None
        ),
        "market_relative_unavailable": bool(availability.unavailable_groups()),
        "decision_days": len(day_stats),
        "first_decision_date": dates[0].isoformat(),
        "last_decision_date": dates[-1].isoformat(),
        "requests": len(requests),
        "pool_size": int(args.pool_size),
        "capacity_rank": "avg_turnover_20",
        "thresholds": {
            "min_avg_turnover_20": min_turnover,
            "min_float_market_cap": min_float_cap,
            "min_history_bars": MIN_HISTORY_BARS,
            "overextension_ret_20": OVEREXTENSION_RET_20,
            "overextension_range_position_60": OVEREXTENSION_RANGE_POSITION,
            "overextension_atr14_pct": OVEREXTENSION_ATR_PCT,
        },
        "not_reproducible_at_daily": [
            name for name in NOT_REPRODUCIBLE_AT_DAILY if name not in intraday_columns
        ],
        "intraday_feature_columns": sorted(intraday_columns),
        "old_chain_ordering_available": False,
        "old_chain_ordering_note": (
            "历史归档里没有当时的 composite_score/等级，旧排序对照无法在重放段上做；"
            "这不是把流动性容量排序冒充旧排序的理由（计划 §4 要求如实报告缺）。"
        ),
        "gate_rejection_totals": dict(sorted(gate_totals.items())),
        "per_day_head": day_stats[:5],
        "per_day_tail": day_stats[-5:],
        "contract": {
            "contract_digest": DEFAULT_TREND_CONTRACT.digest(),
            "reference_notional": float(DEFAULT_TREND_CONTRACT.reference_notional),
            "take_profit_pct": float(DEFAULT_TREND_CONTRACT.take_profit_pct),
            "stop_loss_pct": float(DEFAULT_TREND_CONTRACT.stop_loss_pct),
        },
    }, requests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--symbols-file", required=True)
    parser.add_argument(
        "--minute-db",
        default="",
        help="独立研究库（分钟 bar）；给了才能真算契约里的日内两列",
    )
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--requests", required=True)
    parser.add_argument(
        "--universe-facts",
        default="",
        help="符号级 universe/硬门事实 sidecar；漏斗前两层留档的唯一合法输入",
    )
    parser.add_argument(
        "--survivorship-coverage",
        default="incomplete_or_unknown",
        help="退市/改名历史的覆盖口径；security_status 无数据时不要写成 complete",
    )
    parser.add_argument(
        "--delisting-coverage-verified",
        type=int,
        default=0,
        help="1=已用证券历史证明退市覆盖；0=没证明（默认，留档里会照写）",
    )
    parser.add_argument("--report", default="artifacts/research/tail_replay_report.json")
    parser.add_argument("--benchmark-code", default="000300.SH")
    parser.add_argument("--pool-size", type=int, default=300)
    parser.add_argument("--max-benchmark-staleness-days", type=int, default=5)
    parser.add_argument("--min-turnover-quantile", type=float, default=0.30)
    parser.add_argument("--min-float-cap-quantile", type=float, default=0.10)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        report, requests = run(args)
    except SystemExit as exc:
        print(f"replay blocked: {exc}", file=sys.stderr)
        return RC_ERROR

    requests_path = Path(str(args.requests)).expanduser()
    requests_path.parent.mkdir(parents=True, exist_ok=True)
    with requests_path.open("w", encoding="utf-8") as handle:
        for item in requests:
            handle.write(json.dumps({
                "symbol": item.symbol,
                "decision_date": item.decision_date.isoformat(),
                "entry_date": item.entry_date.isoformat(),
                "overnight_features": dict(item.overnight_features),
                "capture_mode": "replayed_recompute",
            }, ensure_ascii=False) + "\n")
    report["requests_path"] = str(requests_path)
    report_path = Path(str(args.report)).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not args.quiet:
        print(json.dumps({
            "decision_days": report["decision_days"],
            "requests": report["requests"],
            "benchmark_available": report["benchmark_available"],
            "market_relative_unavailable": report["market_relative_unavailable"],
            "gate_rejection_totals": report["gate_rejection_totals"],
        }, ensure_ascii=False, indent=2))
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
