"""Phase 2 横截面 Walk-Forward Harness——PIT 数据集生成器（方案 §5）。

从 market.duckdb daily_bars 生成全市场逐日 (symbol × trade_date) 的
PIT 特征/标签/成熟日数据集，供 walk-forward harness 训练与评估消费：

- **PIT Universe 快照**：方案 §3.4/B10 决策的「扩训练样本」落地——
  训练 universe = 全市场按日过滤后的有效样本（结构化过滤见
  ``_universe_mask``：非 ST/非退市/未停牌/有成交），过滤列全部为当日状态。
- **逻辑键去重**：(symbol, trade_date) 唯一键（Phase 1 实测日内重复捕获
  会把 IC 拉低 2/3）。
- **特征无前视**：FeatureEngineer 仅用当日及历史 bars（滚动 + shift(1)）。
- **标签**：``build_soup_labels``（config 口径：T+1 开盘入场、入场日算第
  1 天、horizon 交易日、TP/SL 冲突 soft_label）；
  ``label_mature_trade_date`` = 入场日第 N（horizon）个交易日收盘。
- **流式分片**（v2，防 OOM）：逐 symbol 独立查询→立即写
  ``shards/shard_<idx>_<symbol>.parquet``（即 checkpoint，断点续跑按分片
  跳过）→全部完成后合并为月度 ``pit_YYYY-MM.parquet`` + pit_meta.json。
  任何时刻内存只持有单 symbol 的数据（< 200MB）。
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


def _fetch_symbol_bars(con, symbol: str, start: str, end: str) -> pd.DataFrame | None:
    frame = con.execute(
        """
        SELECT date, open, high, low, close, volume, turnover,
               float_market_cap, suspended, is_st, is_delisting_risk, name
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


def _trading_calendar(con, window_start: str, window_end: str) -> list[date]:
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
) -> PitDatasetMeta:
    """流式生成 PIT 数据集：分片落盘（checkpoint）→ 合并月度块 → meta。"""

    from stock_analyzer.config import get_config

    cfg = get_config()
    horizon = int(horizon_days if horizon_days is not None else cfg.labels.horizon_days)
    tp = float(take_profit_pct if take_profit_pct is not None else cfg.labels.take_profit_pct)
    sl = float(stop_loss_pct if stop_loss_pct is not None else cfg.labels.stop_loss_pct)
    basis = str(cfg.labels.pnl_price_basis)
    conflict_policy = str(cfg.labels.conflict_policy)
    soft_value = float(cfg.labels.conflict_soft_label_value)

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
    print(
        f"[pit] symbols={len(symbols)} trading_dates={len(trading_dates)}",
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
            mask = _universe_mask(bars)
            common = features.index.intersection(labels.index)
            symbol_rows: list[dict[str, object]] = []
            for ts in common:
                day = ts.date() if hasattr(ts, "date") else ts
                if not (window_start <= day <= window_end):
                    continue
                dec_idx = bisect.bisect_left(calendar_str, day.isoformat())
                if not bool(mask.loc[ts]):
                    continue
                mature = _mature_of(dec_idx)
                label_value = labels.loc[ts]
                fwd_return = None
                entry_idx = dec_idx + 1
                if mature is not None:
                    entry_ts = pd.Timestamp(trading_dates[entry_idx])
                    mature_ts = pd.Timestamp(mature)
                    if (
                        entry_ts in bars.index
                        and mature_ts in bars.index
                        and pd.notna(bars.at[entry_ts, "open"])
                        and float(bars.at[entry_ts, "open"]) > 0
                    ):
                        fwd_return = float(bars.at[mature_ts, "close"]) / float(
                            bars.at[entry_ts, "open"]
                        ) - 1.0
                row: dict[str, object] = {
                    "symbol": symbol,
                    "trade_date": day.isoformat(),
                    "label": None if pd.isna(label_value) else float(label_value),
                    "label_mature_trade_date": mature.isoformat() if mature else None,
                    "fwd_return": fwd_return,
                }
                row.update(
                    {str(k): float(v) for k, v in features.loc[ts].items()}
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
    )


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
) -> PitDatasetMeta:
    out_path = Path(out_dir)
    shards = sorted((out_path / "shards").glob("shard_*.parquet"))
    frames = [pd.read_parquet(shard) for shard in shards]
    data = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["symbol", "trade_date", "label"])
    )
    if data.empty:
        raise RuntimeError("pit dataset is empty after merging shards")
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    data = data.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    data = data.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    for month, group in data.groupby(data["trade_date"].dt.to_period("M")):
        group.to_parquet(out_path / f"pit_{month}.parquet", index=False)
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
    (out_path / "pit_meta.json").write_text(
        json.dumps(meta.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[pit] done rows={len(data):,} symbols={meta.symbols} dates={meta.trade_dates} "
        f"positive_rate={meta.positive_rate} shards={len(shards)} "
        f"(processed={done} skipped={skipped}) in {time.time() - started:.0f}s",
        flush=True,
    )
    return meta
