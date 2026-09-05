"""Phase 2 横截面 Walk-Forward Harness——PIT 数据集生成器（方案 §5）。

从 market.duckdb daily_bars 生成全市场逐日 (symbol × trade_date) 的
PIT 特征/标签/成熟日数据集，供 walk-forward harness 训练与评估消费：

- **PIT Universe 快照**：方案 §3.4/B10 决策的「扩训练样本」落地——
  训练 universe = 全市场按日过滤后的有效样本（结构化过滤见
  ``_universe_mask``：非 ST/非退市/未停牌/有成交/有 float 市值），
  全程不做任何未来函数（过滤列全部为当日状态）。
- **逻辑键去重**：(symbol, trade_date) 唯一键（Phase 1 实测日内重复捕获
  会把 IC 拉低 2/3）。
- **特征无前视**：FeatureEngineer 仅用当日及历史 bars（滚动 + shift(1)），
  对每 symbol 取「截至 trade_date 的全部历史」做一次 transform 后取末行，
  等价于逐日 transform（rolling/min_periods=1 语义），一次性批量完成。
- **标签**：``build_soup_labels``（config 口径：T+1 开盘入场、入场日算第
  1 天、horizon 交易日、TP/SL 冲突 soft_label）；
  ``label_mature_trade_date`` = 入场日 + (horizon-1) 个交易日
  （入场日算第 1 天 → 第 N 个交易日收盘结算）。
- **分块落盘**：按月分块写 parquet + checkpoint，可断点续跑、幂等覆盖。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.labels.soup import build_soup_labels

MARKET_DB = "/app/artifacts/warehouse/market.duckdb"
DEFAULT_OUT_DIR = "/app/artifacts/phase2/pit_dataset"

_ENRICH_COLUMNS = [
    "is_st",
    "is_delisting_risk",
    "suspended",
    "float_market_cap",
    "name",
]


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


def load_daily_panel(
    market_db: str,
    *,
    start: date,
    end: date,
    warmup_start: date | None = None,
) -> dict[str, pd.DataFrame]:
    """读 [warmup_start, end] 的日线为 per-symbol 帧（date 索引，升序）。

    warmup_start 默认 start-400 天（保证窗口首日有完整 120+ 根历史计算特征）。
    """

    effective_start = warmup_start or (start - timedelta(days=400))
    con = duckdb.connect(market_db, read_only=True)
    try:
        frame = con.execute(
            """
            SELECT symbol, date, open, high, low, close, volume, turnover,
                   float_market_cap, suspended, is_st, is_delisting_risk, name
            FROM daily_bars
            WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
            ORDER BY symbol, date
            """,
            [effective_start.isoformat(), end.isoformat()],
        ).fetch_df()
    finally:
        con.close()
    if frame.empty:
        return {}
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    panel: dict[str, pd.DataFrame] = {}
    for symbol, group in frame.groupby("symbol", sort=True):
        ordered = group.set_index("date").sort_index()
        panel[str(symbol)] = ordered
    return panel


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


def _label_mature_trade_date(
    trading_dates: list[date],
    decision_date: date,
    horizon_days: int,
) -> date | None:
    """入场日 = decision_date 后第一个交易日；入场日算第 1 天，
    成熟日 = 入场日第 N（horizon）个交易日收盘。窗口耗尽返回 None（尾部未成熟）。"""

    import bisect

    idx = bisect.bisect_right(trading_dates, decision_date)
    entry_idx = idx
    mature_idx = entry_idx + horizon_days - 1
    if mature_idx >= len(trading_dates):
        return None
    return trading_dates[mature_idx]


def build_pit_dataset(
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
    checkpoint: bool = True,
) -> tuple[pd.DataFrame, PitDatasetMeta]:
    """生成 PIT 横截面数据集并按月分块落盘；返回 (dataframe, meta)。"""

    from stock_analyzer.config import get_config

    cfg = get_config()
    horizon = int(horizon_days if horizon_days is not None else cfg.labels.horizon_days)
    tp = float(take_profit_pct if take_profit_pct is not None else cfg.labels.take_profit_pct)
    sl = float(stop_loss_pct if stop_loss_pct is not None else cfg.labels.stop_loss_pct)
    basis = str(cfg.labels.pnl_price_basis)
    conflict_policy = str(cfg.labels.conflict_policy)
    soft_value = float(cfg.labels.conflict_soft_label_value)

    started = time.time()
    warmup_start = window_start - timedelta(days=warmup_days)
    print(f"[pit] loading daily bars {warmup_start}..{window_end}", flush=True)
    panel = load_daily_panel(
        market_db, start=window_start, end=window_end, warmup_start=warmup_start
    )
    if max_symbols > 0:
        panel = dict(sorted(panel.items())[:max_symbols])
    print(f"[pit] symbols: {len(panel)}", flush=True)

    # 全市场交易日历（窗口内出现在 daily_bars 的日期）。
    all_dates: set[date] = set()
    for bars in panel.values():
        all_dates.update(ts.date() for ts in bars.index)
    trading_dates = sorted(all_dates)
    trading_dates = [d for d in trading_dates if window_start <= d <= window_end]
    print(f"[pit] trading dates: {len(trading_dates)}", flush=True)

    engineer = FeatureEngineer()
    rows: list[dict[str, object]] = []
    last_date_by_symbol: dict[str, date] = {}

    for index, (symbol, bars) in enumerate(sorted(panel.items())):
        if bars.empty:
            continue
        try:
            features = engineer.transform(bars)
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
        except Exception as exc:  # noqa: BLE001 - 单票数据缺陷不阻塞整体
            print(f"[pit][warn] {symbol}: {type(exc).__name__}: {exc}", flush=True)
            continue
        common = features.index.intersection(labels.index)
        mask = _universe_mask(bars)
        closes = bars["close"]
        opens = bars["open"]
        for ts in common:
            day = ts.date() if hasattr(ts, "date") else ts
            if not (window_start <= day <= window_end):
                continue
            if day not in trading_dates:
                continue
            if not bool(mask.loc[ts]):
                continue
            feature_row = features.loc[ts]
            label_value = labels.loc[ts]
            # 前向收益（分位收益用）：T+1 开盘入场 → 成熟日收盘（非 TP/SL 路径，
            # 独立于 soup 标签的连续收益口径）。
            import bisect

            dec_idx = bisect.bisect_left(trading_dates, day)
            entry_idx = dec_idx + 1
            mature_idx = entry_idx + horizon - 1
            fwd_return = None
            mature = None
            if mature_idx < len(trading_dates):
                mature = trading_dates[mature_idx]
                entry_ts = pd.Timestamp(trading_dates[entry_idx])
                mature_ts = pd.Timestamp(mature)
                if (
                    entry_ts in opens.index
                    and mature_ts in closes.index
                    and pd.notna(opens.loc[entry_ts])
                    and float(opens.loc[entry_ts]) > 0
                ):
                    entry_price = float(opens.loc[entry_ts])
                    fwd_return = float(closes.loc[mature_ts]) / entry_price - 1.0
            row: dict[str, object] = {
                "symbol": symbol,
                "trade_date": day.isoformat(),
                "label": None if pd.isna(label_value) else float(label_value),
                "label_mature_trade_date": mature.isoformat() if mature else None,
                "fwd_return": fwd_return,
            }
            row.update({k: float(v) for k, v in feature_row.items()})
            rows.append(row)
        last_date_by_symbol[symbol] = (
            bars.index.max().date() if hasattr(bars.index.max(), "date") else bars.index.max()
        )
        if (index + 1) % 500 == 0:
            print(f"[pit] symbols done {index + 1}/{len(panel)} rows={len(rows):,}", flush=True)

    data = pd.DataFrame(rows)
    if data.empty:
        raise RuntimeError("pit dataset is empty")
    data = data.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    data = data.sort_values(["trade_date", "symbol"]).reset_index(drop=True)

    labeled = data.dropna(subset=["label"])
    meta = PitDatasetMeta(
        dataset_hash=hashlib.sha256(
            json.dumps(
                {
                    "rows": len(data),
                    "window": [window_start.isoformat(), window_end.isoformat()],
                    "horizon": horizon,
                    "tp": tp,
                    "sl": sl,
                    "basis": basis,
                    "conflict": conflict_policy,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16],
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        rows=int(len(data)),
        symbols=int(data["symbol"].nunique()),
        trade_dates=int(data["trade_date"].nunique()),
        positive_rate=round(float((labeled["label"] == 1.0).mean()), 6) if len(labeled) else 0.0,
        matured_rows=int(len(labeled)),
        generated_at=pd.Timestamp.now(tz="UTC").isoformat(),
        label_policy_note=(
            f"soup T+1 open basis horizon={horizon} tp={tp} sl={sl} "
            f"conflict={conflict_policy} soft={soft_value}"
        ),
    )

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    for month, group in data.groupby(data["trade_date"].dt.to_period("M")):
        chunk_path = out_path / f"pit_{month}.parquet"
        if not checkpoint or not chunk_path.exists():
            group.to_parquet(chunk_path, index=False)
    meta_path = out_path / "pit_meta.json"
    meta_path.write_text(json.dumps(meta.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[pit] done rows={len(data):,} symbols={meta.symbols} "
        f"dates={meta.trade_dates} positive_rate={meta.positive_rate} "
        f"in {time.time() - started:.0f}s -> {out_path}",
        flush=True,
    )
    return data, meta
