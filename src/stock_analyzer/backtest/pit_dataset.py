"""Phase 2 横截面 Walk-Forward Harness——PIT 数据集生成器（方案 §5）。

从 market.duckdb daily_bars 生成全市场逐日 (symbol × trade_date) 的
PIT 特征/标签/成熟日数据集，供 walk-forward harness 训练与评估消费：

- **PIT Universe 快照**：方案 §3.4/B10 决策的「扩训练样本」落地——
  训练 universe = 全市场按日过滤后的有效样本（结构化过滤见
  ``_universe_mask``：非 ST/非退市/未停牌/有成交），过滤列全部为当日状态。
- **逻辑键去重**：(symbol, trade_date) 唯一键（Phase 1 实测日内重复捕获
  会把 IC 拉低 2/3）。
- **特征无前视**：FeatureEngineer 仅用当日及历史 bars（滚动 + shift(1)）。
- **标签**：按 ``basis`` 二选一——
  - ``soup``（默认，生产口径）：``build_soup_labels``（T+1 开盘入场、
    入场日算第 1 天、horizon 交易日、TP/SL 冲突 soft_label）；
  - ``return_rank``（方向一'，2026-09-07）：以 IC 评估同口径 fwd_return
    （mature close / entry open - 1）为基础的**同一交易日横截面分位**
    （top 30% → 1 / bottom 30% → 0 / 中间剔除），由
    ``build_return_rank_labels`` 逐日截面计算，schema v3 registry 契约。
  两种 basis 的 ``label_mature_trade_date`` 完全一致（入场日起第 horizon
  个交易日收盘），embargo 不变。
  **98 个 NaN 特征接入（2026-09-07 方向一'任务 2）**：v1 生成器只拉
  daily_bars 的 12 个价格列，FeatureEngineer 需要的背景/资金列
  （holder_count、北向、融资、大宗、moneyflow、hk、inst、roe、
  debt_ratio、board）、分钟 summary（intraday_summary_1m/5m）与市场
  指数（index_daily）从未进入 bars——归因扫描里 98/208 特征全 NaN 的
  根因即此（不是"生成器没读"，是"查询根本没拉"）。v2 在
  ``_fetch_symbol_bars`` 一次性拉齐全部背景列，并新增
  ``_fetch_intraday_panel`` / ``_fetch_market_index``。
- **流式分片**（v2，防 OOM）：逐 symbol 独立查询→立即写
  ``shards/shard_<idx>_<symbol>.parquet``（即 checkpoint，断点续跑按分片
  跳过）→全部完成后合并为月度 ``pit_YYYY-MM.parquet`` + pit_meta.json。
  任何时刻内存只持有单 symbol 的数据（< 200MB）。

PIT 纪律（98 特征接入的时点语义）：
- 背景/资金列随 daily_bars 行内携带（当日状态，按交易日可得）；
- 财务列（roe/debt_ratio）由 9/5 data_gate 修复的
  ``enrich_daily_financial_pit`` 按公告日 as-of 物化进 daily_bars——
  生成器只读行内值，**不重算 as-of join**（严禁把未来披露映射到过去
  日期，行内值已按 PIT 语义定稿）；
- bg_is_st 等只有回填日之后有值的字段如实保留 NaN/行内值，不假填充。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import duckdb
import numpy as np
import pandas as pd

from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.labels.return_rank import build_return_rank_labels
from stock_analyzer.labels.soup import build_soup_labels

MARKET_DB = "/app/artifacts/warehouse/market.duckdb"
DEFAULT_OUT_DIR = "/app/artifacts/phase2/pit_dataset"

# FeatureEngineer 消费的 daily_bars 背景/资金/状态列（98 个 NaN 特征的
# 源列）。逐列列出而非 SELECT *：显式契约，缺列即查询报错（fail-closed）。
_BACKGROUND_COLUMNS = (
    "roe",
    "debt_ratio",
    "holder_count",
    "block_trade_net",
    "margin_financing_balance",
    "northbound_net",
    "dragon_tiger_flag",
    "moneyflow_net_amount",
    "hk_hold_ratio",
    "hk_hold_change",
    "inst_net_amount",
    "block_trade_amount",
    "block_trade_volume",
    "block_trade_premium_discount",
    "board",
    "background_data_complete",
)

# intraday_summary 表列（summarize_minute_bars 的每日聚合产物）。
_INTRADAY_SUMMARY_COLUMNS = (
    "minute_count",
    "session_return",
    "session_range_pct",
    "realized_vol",
    "vwap_gap",
    "am_return",
    "pm_return",
    "am_pm_diff",
    "last30_return",
    "last30_volume_share",
    "tail30_volume_share",
    "morning30_volume_share",
    "positive_bar_ratio",
    "close_position",
    "above_vwap_ratio",
    "price_efficiency",
    "am_pm_reversal_strength",
    "tail_volatility_ratio",
    "close_vwap_stability",
    "intraday_pullback_ratio",
)

# 市场相对特征的基准指数行（index_daily，唯一索引保证单行/日）。
_BENCHMARK_INDEX_CODES = ("000300", "399001", "000001", "399106")


@dataclass(frozen=True)
class PitDatasetMeta:
    dataset_hash: str
    window_start: str
    window_end: str
    rows: int
    symbols: int
    trade_dates: int
    positive_rate: float
    matured_rows: int
    generated_at: str
    label_policy_note: str


def _universe_mask(bars: pd.DataFrame) -> pd.Series:
    """当日 PIT 结构过滤（全部为当日状态列，无未来信息）。"""

    is_st = (
        bars["is_st"].fillna(False).astype(bool)
        if "is_st" in bars.columns
        else pd.Series(False, index=bars.index)
    )
    delisting = (
        bars["is_delisting_risk"].fillna(False).astype(bool)
        if "is_delisting_risk" in bars.columns
        else pd.Series(False, index=bars.index)
    )
    suspended = (
        bars["suspended"].fillna(False).astype(bool)
        if "suspended" in bars.columns
        else pd.Series(False, index=bars.index)
    )
    has_volume = bars["volume"].fillna(0.0) > 0.0
    return ~is_st & ~delisting & ~suspended & has_volume


def _fetch_symbol_bars(
    con: Any, symbol: str, start: str, end: str
) -> pd.DataFrame | None:
    price_columns = "date, open, high, low, close, volume, turnover, float_market_cap"
    columns = [price_columns]
    columns.extend(_BACKGROUND_COLUMNS)
    columns.extend(["suspended", "is_st", "is_delisting_risk", "name"])
    # 日期谓词 ISO 字符串直比：CAST 会退化为全表扫描（Phase 2 OOM 实测）。
    # 背景/财务列为 9/5 data_gate 修复后已按 PIT 语义物化在行内的值。
    frame = con.execute(
        f"""
        SELECT {", ".join(columns)}
        FROM daily_bars
        WHERE symbol = ? AND date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
        ORDER BY date
        """,
        [symbol, start, end],
    ).fetch_df()
    if frame.empty:
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).set_index("date").sort_index()
    return frame if not frame.empty else None


def _fetch_intraday_panel(
    con: Any, symbols: list[str], start: str, end: str
) -> dict[str, dict[str, pd.DataFrame]]:
    """逐 symbol×interval 拉分钟 summary 面板（一次性窗口查询，非逐日）。

    返回 {interval: {symbol: DataFrame(index=date)}}；表列缺失的列在
    frame 上以 NaN 呈现（FeatureEngineer._prepare_intraday_summary 对
    缺列/NaN 的既有语义不变）。
    """

    panels: dict[str, dict[str, pd.DataFrame]] = {}
    available_columns = {
        str(row[0])
        for row in con.execute("DESCRIBE intraday_summary_1m").fetchall()
    }
    summary_columns = tuple(c for c in _INTRADAY_SUMMARY_COLUMNS if c in available_columns)
    if not summary_columns:
        return panels
    # symbol 批量谓词：分批 IN (?,...) 防超长 SQL（~5k symbol 单批可行，
    # 但 DuckDB 参数上限保守取 1000/批）。
    for interval in ("1m", "5m"):
        table = f"intraday_summary_{interval}"
        try:
            has_table = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                [table],
            ).fetchone()[0]
        except Exception:  # noqa: BLE001 - 表不存在（旧库）视为无数据
            has_table = 0
        if not has_table:
            continue
        per_symbol: dict[str, pd.DataFrame] = {}
        batch = 1000
        for offset in range(0, len(symbols), batch):
            chunk = symbols[offset : offset + batch]
            placeholders = ", ".join("?" for _ in chunk)
            frame = con.execute(
                f"""
                SELECT symbol, date, {", ".join(summary_columns)}
                FROM {table}
                WHERE symbol IN ({placeholders})
                  AND date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
                """,
                [*chunk, start, end],
            ).fetch_df()
            if frame.empty:
                continue
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
            frame = frame.dropna(subset=["date"])
            frame = frame.drop_duplicates(subset=["symbol", "date"], keep="last")
            for sym, group in frame.groupby("symbol"):
                per_symbol[str(sym)] = group.set_index("date").sort_index().drop(
                    columns=["symbol"]
                )
        panels[interval] = per_symbol
    return panels


def _fetch_market_index(con: Any, start: str, end: str) -> pd.DataFrame | None:
    """拉基准指数日线（market_relative 特征族：excess_ret/rs_ma/beta 等 11 个）。

    index_daily 的 index_code 带交易所后缀（NAS 实测 '000300.SH'）——
    匹配用前缀剥离（``split('.')[0]``），否则裸代码永远查空、11 个市场
    相对特征继续全 NaN。
    """

    frame = con.execute(
        """
        SELECT index_code, trade_date AS date, close
        FROM index_daily
        WHERE trade_date >= CAST(? AS DATE) AND trade_date <= CAST(? AS DATE)
        """,
        [start, end],
    ).fetch_df()
    if frame.empty:
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    if frame.empty:
        return None
    # 剥后缀后按优先级取首个命中：000300（配置默认基准）> 399001 > 000001 > 399106。
    frame["index_code"] = frame["index_code"].astype(str).str.split(".").str[0]
    for code in _BENCHMARK_INDEX_CODES:
        selected = frame[frame["index_code"] == code]
        if not selected.empty:
            return cast(
                "pd.DataFrame",
                selected.set_index("date")
                .sort_index()[["close"]]
                .rename(columns={"close": "benchmark_close"}),
            )
    return None


def _trading_calendar(con: Any, window_start: str, window_end: str) -> list[date]:
    rows = con.execute(
        "SELECT DISTINCT date FROM daily_bars "
        "WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) ORDER BY 1",
        [window_start, window_end],
    ).fetchall()
    return [r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])) for r in rows]


def generate_pit_dataset(
    *,
    market_db: str = MARKET_DB,
    window_start: date,
    window_end: date,
    out_dir: str = DEFAULT_OUT_DIR,
    horizon_days: int | None = None,
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
    warmup_days: int = 400,
    max_symbols: int = 0,
    resume: bool = True,
    label_basis: str = "soup",
    benchmark_index: pd.DataFrame | None = None,
) -> PitDatasetMeta:
    """流式生成 PIT 数据集：分片落盘（checkpoint）→ 合并月度块 → meta。

    ``label_basis``：
    - ``soup``：TP/SL 路径标签（生产口径，v1 行为逐位不变）；
    - ``return_rank``：横截面收益排序标签（方向一'）——在**月度合并前**
      按完整横截面计算（分片内逐票无法算分位，必须等该决策日全部
      symbol 的分片就绪；实现：先流式落盘带 fwd_return 的分片，合并
      阶段逐日截面计算 rank label 后回写 label 列）。
    """

    from stock_analyzer.config import get_config

    cfg = get_config()
    horizon = int(horizon_days if horizon_days is not None else cfg.labels.horizon_days)
    tp = float(take_profit_pct if take_profit_pct is not None else cfg.labels.take_profit_pct)
    sl = float(stop_loss_pct if stop_loss_pct is not None else cfg.labels.stop_loss_pct)
    basis = str(cfg.labels.pnl_price_basis)
    conflict_policy = str(cfg.labels.conflict_policy)
    soft_value = float(cfg.labels.conflict_soft_label_value)
    top_q = float(cfg.labels.return_rank_top_quantile)
    bottom_q = float(cfg.labels.return_rank_bottom_quantile)
    drop_middle = bool(cfg.labels.return_rank_drop_middle)
    min_cross = int(cfg.labels.return_rank_min_cross_section)
    label_basis = label_basis.strip().lower()
    if label_basis not in {"soup", "return_rank"}:
        raise ValueError(f"unsupported label_basis: {label_basis}")

    started = time.time()
    warmup_start = (window_start - timedelta(days=warmup_days)).isoformat()
    window_start_s = window_start.isoformat()
    window_end_s = window_end.isoformat()

    out_path = Path(out_dir)
    shard_dir = out_path / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(market_db, read_only=True)
    symbols = [
        str(r[0])
        for r in con.execute(
            "SELECT DISTINCT symbol FROM daily_bars WHERE date <= CAST(? AS DATE) ORDER BY 1",
            [window_end_s],
        ).fetchall()
    ]
    trading_dates = _trading_calendar(con, window_start_s, window_end_s)
    if max_symbols > 0:
        symbols = symbols[:max_symbols]

    # 分钟面板与基准指数：窗口级一次性拉取（共用，非逐 symbol 查询——
    # 逐 symbol SQL 会是 5k 次/表的全表扫描）。
    intraday_panels = _fetch_intraday_panel(
        con, symbols, warmup_start, window_end_s
    )
    index_frame = benchmark_index
    if index_frame is None:
        index_frame = _fetch_market_index(con, warmup_start, window_end_s)
    print(
        f"[pit] symbols={len(symbols)} trading_dates={len(trading_dates)} "
        f"intraday_symbols_1m={len(intraday_panels.get('1m', {}))} "
        f"intraday_symbols_5m={len(intraday_panels.get('5m', {}))} "
        f"benchmark={'yes' if index_frame is not None else 'none'}",
        flush=True,
    )

    def _mature_of(decision_idx: int) -> date | None:
        entry_idx = decision_idx + 1
        mature_idx = entry_idx + horizon - 1
        return trading_dates[mature_idx] if mature_idx < len(trading_dates) else None

    import bisect

    calendar_str = [d.isoformat() for d in trading_dates]
    engineer = FeatureEngineer()
    done = 0
    skipped = 0
    for index, symbol in enumerate(symbols):
        shard_path = shard_dir / f"shard_{index:05d}_{symbol}.parquet"
        if resume and shard_path.exists():
            skipped += 1
            done += 1
            continue
        try:
            bars = _fetch_symbol_bars(con, symbol, warmup_start, window_end_s)
            if bars is None or len(bars) < 40:
                done += 1
                continue
            intraday_1m = intraday_panels.get("1m", {}).get(str(symbol))
            intraday_5m = intraday_panels.get("5m", {}).get(str(symbol))
            features = engineer.transform(
                bars,
                intraday_1m=intraday_1m,
                intraday_5m=intraday_5m,
                market_index=index_frame,
            )
            mask = _universe_mask(bars)
            common = features.index
            symbol_rows: list[dict[str, object]] = []
            for ts in common:
                day = ts.date() if hasattr(ts, "date") else ts
                if not (window_start <= day <= window_end):
                    continue
                dec_idx = bisect.bisect_left(calendar_str, day.isoformat())
                if not bool(mask.loc[ts]):
                    continue
                mature = _mature_of(dec_idx)
                entry_idx = dec_idx + 1
                # fwd_return：IC 评估同口径（return_rank 的基础，soup 模式
                # 下也一并落盘供归因工具对照）。
                fwd_return = None
                if mature is not None:
                    entry_ts = pd.Timestamp(trading_dates[entry_idx])
                    mature_ts = pd.Timestamp(mature)
                    if (
                        entry_ts in bars.index
                        and mature_ts in bars.index
                        and pd.notna(bars.at[entry_ts, "open"])
                        and float(bars.at[entry_ts, "open"]) > 0  # type: ignore[arg-type]
                    ):
                        fwd_return = float(bars.at[mature_ts, "close"]) / float(  # type: ignore[arg-type]
                            bars.at[entry_ts, "open"]  # type: ignore[arg-type]
                        ) - 1.0
                # soup 模式：label = TP/SL 路径标签（v1 行为不变）。
                # return_rank 模式：分片阶段只落 fwd_return（分位必须等
                # 全截面，见 _finalize 阶段的 _apply_return_rank_labels）。
                label_value = None
                if label_basis == "soup":
                    labels = build_soup_labels(
                        bars,
                        take_profit_pct=tp,
                        stop_loss_pct=sl,
                        horizon_days=horizon,
                        price_basis=basis,
                        exclude_untradable=True,
                        conflict_policy=conflict_policy,
                        conflict_soft_label_value=soft_value,
                    )
                    value = labels.loc[ts] if ts in labels.index else None
                    label_value = None if value is None or pd.isna(value) else float(value)
                row: dict[str, object] = {
                    "symbol": symbol,
                    "trade_date": day.isoformat(),
                    "label": label_value,
                    "label_mature_trade_date": mature.isoformat() if mature else None,
                    "fwd_return": fwd_return,
                }
                row.update(
                    {
                        str(k): np.float32(v)
                        for k, v in features.loc[ts].items()
                        if pd.notna(v)
                    }
                )
                symbol_rows.append(row)
            if symbol_rows:
                shard = pd.DataFrame(symbol_rows)
                shard = shard.drop_duplicates(subset=["trade_date"], keep="last")
                shard.to_parquet(shard_path, index=False)
            done += 1
        except Exception as exc:  # noqa: BLE001 - 单票缺陷不阻塞整体
            print(f"[pit][warn] {symbol}: {type(exc).__name__}: {exc}", flush=True)
            done += 1
        if (index + 1) % 500 == 0:
            print(f"[pit] symbols done {index + 1}/{len(symbols)}", flush=True)
    con.close()

    return _finalize_pit_dataset(
        out_dir=str(out_path),
        window_start=window_start,
        window_end=window_end,
        horizon=horizon,
        tp=tp,
        sl=sl,
        basis=basis,
        conflict_policy=conflict_policy,
        soft_value=soft_value,
        started=started,
        done=done,
        skipped=skipped,
        label_basis=label_basis,
        top_q=top_q,
        bottom_q=bottom_q,
        drop_middle=drop_middle,
        min_cross=min_cross,
    )


def _apply_return_rank_labels(
    month_frame: pd.DataFrame,
    *,
    top_q: float,
    bottom_q: float,
    drop_middle: bool,
    min_cross: int,
) -> pd.DataFrame:
    """对一个月度块按逐日横截面计算 return_rank label（合并阶段专用）。

    横截面分位必须基于该 trade_date 的**完整截面**：月度块恰含整月全部
    symbol 分片（合并阶段保证），逐日 groupby 即完整截面，无前视、无
    截面截断。
    """

    fwd = month_frame["fwd_return"]
    valid = fwd.notna()
    labels = pd.Series(float("nan"), index=month_frame.index, dtype=float)
    if valid.any():
        # 逐日切片调用（严禁对整列一次调用：单层 RangeIndex 下全部行会
        # 落入同一组，跨日混截面）。day 掩码保证分位严格限于当日截面。
        day_labels_parts: list[pd.Series] = []
        for day in month_frame["trade_date"].unique():
            day_mask = month_frame["trade_date"] == day
            day_fwd = fwd[day_mask]
            if not day_fwd.notna().any():
                continue
            part = build_return_rank_labels(
                day_fwd,
                top_quantile=top_q,
                bottom_quantile=bottom_q,
                drop_middle=drop_middle,
                min_cross_section=min_cross,
            )
            day_labels_parts.append(part)
        if day_labels_parts:
            labels = pd.concat(day_labels_parts).reindex(month_frame.index)
    month_frame = month_frame.copy()
    # label 列回写：NaN 保持 None（parquet 缺失语义与 soup 模式一致，
    # drop_middle 剔除的行 label 为空）。
    month_frame["label"] = labels.where(labels.notna())
    return month_frame


def _finalize_pit_dataset(
    *,
    out_dir: str,
    window_start: date,
    window_end: date,
    horizon: int,
    tp: float,
    sl: float,
    basis: str,
    conflict_policy: str,
    soft_value: float,
    started: float,
    done: int,
    skipped: int,
    label_basis: str = "soup",
    top_q: float = 0.3,
    bottom_q: float = 0.3,
    drop_middle: bool = True,
    min_cross: int = 30,
) -> PitDatasetMeta:
    out_path = Path(out_dir)
    shards = sorted((out_path / "shards").glob("shard_*.parquet"))
    if not shards:
        raise RuntimeError("pit dataset is empty after merging shards")

    # 流式合并（2026-09-06 防 OOM 修复）：一次全量 concat 5,570 分片
    # （~240 万行 × 222 列 float64 ≈ 4GB）曾把容器顶爆。改为按批
    # （500 分片）读入 → 按月桶拆分 → 满批即写盘；统计走增量计数器。
    month_buckets: dict[str, pd.DataFrame | None] = {}
    batch_size = 500
    total_rows = 0
    labeled_rows = 0
    labeled_positive = 0
    symbols_seen: set[str] = set()
    trade_dates_seen: set[str] = set()

    def _flush_months() -> None:
        for month_key, frame in month_buckets.items():
            if frame is None:
                continue
            frame = frame.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
            frame = frame.sort_values(["trade_date", "symbol"])
            if label_basis == "return_rank":
                # 月度块 = 整月完整截面（本轮所有分片已落盘），
                # 此处逐日横截面分位是全市场截面，非批内子集。
                frame = _apply_return_rank_labels(
                    frame,
                    top_q=top_q,
                    bottom_q=bottom_q,
                    drop_middle=drop_middle,
                    min_cross=min_cross,
                )
            target = out_path / f"pit_{month_key}.parquet"
            if target.exists():
                previous = pd.read_parquet(target)
                frame = pd.concat([previous, frame], ignore_index=True)
                frame = frame.drop_duplicates(
                    subset=["symbol", "trade_date"], keep="last"
                ).sort_values(["trade_date", "symbol"])
            frame.to_parquet(target, index=False)
        month_buckets.clear()

    for offset in range(0, len(shards), batch_size):
        batch = shards[offset : offset + batch_size]
        frames = [pd.read_parquet(shard) for shard in batch]
        batch_frame = pd.concat(frames, ignore_index=True)
        del frames
        total_rows += len(batch_frame)
        if "label" in batch_frame.columns:
            labeled_mask = batch_frame["label"].notna()
            labeled_rows += int(labeled_mask.sum())
            labeled_positive += int(
                (batch_frame.loc[labeled_mask, "label"] == 1.0).sum()
            )
        symbols_seen.update(str(s) for s in batch_frame["symbol"].unique())
        trade_dates_seen.update(str(d)[:10] for d in batch_frame["trade_date"].unique())
        batch_frame["trade_date"] = pd.to_datetime(batch_frame["trade_date"])
        batch_frame["__month"] = batch_frame["trade_date"].dt.strftime("%Y-%m")
        for month_key, group in batch_frame.groupby("__month"):
            group = group.drop(columns=["__month"])
            key = str(month_key)
            current = month_buckets.get(key)
            month_buckets[key] = (
                pd.concat([current, group], ignore_index=True)
                if current is not None
                else group
            )
        _flush_months()
        del batch_frame

    if total_rows == 0:
        raise RuntimeError("pit dataset is empty after merging shards")
    positive_rate = round(labeled_positive / labeled_rows, 6) if labeled_rows else 0.0
    if label_basis == "return_rank":
        # return_rank 的标签统计在月度块上重算（drop_middle 剔除的行不算）。
        labeled_rows = 0
        labeled_positive = 0
        for chunk in sorted(out_path.glob("pit_*.parquet")):
            frame = pd.read_parquet(chunk, columns=["label"])
            labeled_rows += int(frame["label"].notna().sum())
            labeled_positive += int((frame["label"] == 1.0).sum())
        positive_rate = round(labeled_positive / labeled_rows, 6) if labeled_rows else 0.0
        policy_note = (
            f"return_rank T+1 open basis horizon={horizon} top_q={top_q} "
            f"bottom_q={bottom_q} drop_middle={drop_middle} min_cross={min_cross} "
            f"(schema v3, cross-sectional fwd_return quantile)"
        )
    else:
        policy_note = (
            f"soup T+1 open basis horizon={horizon} tp={tp} sl={sl} "
            f"conflict={conflict_policy} soft={soft_value}"
        )
    meta = PitDatasetMeta(
        dataset_hash=hashlib.sha256(
            json.dumps(
                {
                    "rows": total_rows,
                    "window": [window_start.isoformat(), window_end.isoformat()],
                    "horizon": horizon,
                    "tp": tp,
                    "sl": sl,
                    "basis": basis,
                    "conflict": conflict_policy,
                    "label_basis": label_basis,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16],
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        rows=int(total_rows),
        symbols=len(symbols_seen),
        trade_dates=len(trade_dates_seen),
        positive_rate=positive_rate,
        matured_rows=int(labeled_rows),
        generated_at=pd.Timestamp.now(tz="UTC").isoformat(),
        label_policy_note=policy_note,
    )
    (out_path / "pit_meta.json").write_text(
        json.dumps(meta.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[pit] done rows={total_rows:,} symbols={meta.symbols} dates={meta.trade_dates} "
        f"positive_rate={meta.positive_rate} shards={len(shards)} "
        f"(processed={done} skipped={skipped}) in {time.time() - started:.0f}s",
        flush=True,
    )
    return meta
