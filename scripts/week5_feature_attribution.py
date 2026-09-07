#!/usr/bin/env python
"""Phase 2 NO-GO 后特征归因扫描（方案 A 第一步，2026-09-07）。

目的：在 Phase 2 同一 PIT 数据集、同一批评估日上，把 208 个特征逐个按
横截面 rank IC 与前向收益对齐，产出：
  - 每特征逐评估日 rank IC 序列 → date-block bootstrap 95% CI（与
    walk_forward_xsec 的 aggregate IC 同口径：按日重采样取均值分位数）
  - 按评估日聚合的均值 IC（等权日均值，与 harness 的 aggregate_ic_mean
    完全同口径，便于与模型分数 IC -0.024 对照）
  - 分月 IC 均值（时间稳定性：某特征是否只在个别月份有效）
  - top/bottom 分位收益价差（信号的经济含义而不只是排序相关性）

资源纪律（继承 Phase 2 harness 的实测教训，勿重蹈）：
  - 单评估日单次 SQL 拉全部 208 特征（严禁 per-symbol / per-feature 查询，
    严禁 CAST 日期列——ISO 字符串直比才能下推 parquet 统计信息）；
  - 单日横截面 ~5k 行 × 208 列 float32 ≈ 4MB，逐日处理逐日释放；
  - 不 import 训练栈（无 LightGBM/XGBoost/C 层泄漏问题）；
  - bootstrap 以「评估日」为 block（342 日全集，n_boot=1000，208 特征
    纯 numpy，峰值内存 < 100MB）。

输出：out_dir/feature_attribution_<ts>.json（含全部特征明细与汇总）。
用法（NAS 容器内）：
  python -m scripts.week5_feature_attribution \
      --dataset-dir /app/artifacts/phase2/pit_dataset_ext \
      --out-dir /app/artifacts/phase2
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from stock_analyzer.backtest.walk_forward_xsec import PitDatasetStore
from stock_analyzer.learning.scoring_eval import (
    _rankdata,
)

# 动量/涨跌幅族的关键词（Phase 2 结论与 8 月回测三个独立证据都指向的族）。
_MOMENTUM_KEYWORDS = (
    "ret_",
    "momentum",
    "pct_chg",
    "chg_",
    "_return",
    "roc",
)


def _feature_family(name: str) -> str:
    """按命名前缀粗分特征族（供归因结果按族聚合解读）。"""

    n = name.lower()
    if any(k in n for k in _MOMENTUM_KEYWORDS):
        return "momentum"
    if "volume" in n or "vol_" in n or "amount" in n or "turnover" in n:
        return "volume"
    if "volat" in n or "atr" in n:
        return "volatility"
    if any(k in n for k in ("ma_", "_ma", "ema", "macd", "rsi", "kdj", "boll")):
        return "trend_oscillator"
    if any(k in n for k in ("pe", "pb", "ps_", "dv_", "market_cap", "float")):
        return "valuation_size"
    if any(k in n for k in ("holder", "inst", "north", "margin", "block")):
        return "holder_flow"
    if any(k in n for k in ("rev", "profit", "roe", "roa", "growth", "gross", "net_")):
        return "fundamental"
    return "other"


def _daily_ic_matrix(
    store: PitDatasetStore,
    *,
    eval_dates: list[str],
    feature_columns: list[str],
) -> tuple[np.ndarray, list[str]]:
    """逐评估日计算全特征横截面 Spearman IC。

    返回 (n_dates × n_features) 的 IC 矩阵与有效日期列表；单日 ~5k 行
    208 特征，整帧 rank 后按列对 fwd_return 的秩做 Pearson——等价于
    逐特征 Spearman，但一次 argsort 全特征共享（约 208 次 argsort 内
    联排成 O(D×F×N log N)，纯 numpy）。
    """

    n_features = len(feature_columns)
    valid_dates: list[str] = []
    ic_rows: list[np.ndarray] = []
    # 逐日拉取：谓词下推（ISO 字符串直比），单日峰值 ~10MB。
    for i, day in enumerate(eval_dates):
        cols = ", ".join(["fwd_return"] + feature_columns)
        frame = store._con.execute(  # noqa: SLF001 - store 内部连接，容器内单进程
            f"SELECT {cols} FROM pit_dedup WHERE trade_date = ?",
            [day],
        ).fetch_df()
        if frame.empty:
            continue
        fwd = frame["fwd_return"].to_numpy(dtype=float)
        feat = frame[feature_columns].to_numpy(dtype=float)
        # 逐特征 Spearman = rank(x) 与 rank(y) 的 Pearson；y 秩共享一次计算。
        rank_y = _rankdata(fwd)
        yc = rank_y - rank_y.mean()
        y_denom = math.sqrt(float((yc * yc).sum()))
        ic_row = np.full(n_features, np.nan, dtype=float)
        if y_denom > 0.0:
            # NaN 特征逐列掩码：列内有效值 <30（横截面太薄）记 NaN。
            for j in range(n_features):
                x = feat[:, j]
                mask = ~np.isnan(x)
                nv = int(mask.sum())
                if nv < 30:
                    continue
                # x 无方差（全常数列，如停牌填充）也记 NaN。
                rx = _rankdata(x[mask])
                xc = rx - rx.mean()
                x_denom = math.sqrt(float((xc * xc).sum()))
                if x_denom <= 0.0:
                    continue
                ic_row[j] = float((xc * yc[mask]).sum() / (x_denom * y_denom))
        ic_rows.append(ic_row)
        valid_dates.append(day)
        if (i + 1) % 20 == 0:
            print(
                f"[scan] {i + 1}/{len(eval_dates)} days rss={_rss_mib():.0f}MiB",
                flush=True,
            )
    matrix = np.vstack(ic_rows) if ic_rows else np.empty((0, n_features), dtype=float)
    return matrix, valid_dates


def _rss_mib() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return -1.0
    return -1.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default="/app/artifacts/phase2/pit_dataset_ext")
    parser.add_argument("--out-dir", default="/app/artifacts/phase2")
    parser.add_argument("--min-cross-section", type=int, default=30)
    args = parser.parse_args()

    t0 = time.time()
    store = PitDatasetStore(args.dataset_dir)
    feature_columns = store.feature_columns
    print(
        f"[1] dataset rows={store.row_count():,} features={len(feature_columns)}",
        flush=True,
    )

    # 评估日 = 18 个 fold 的 eval_dates 并集（与 Phase 2 NO-GO 结论完全
    # 同口径——包含全部 342 个评估日，不引入任何新样本选择）。
    meta_path = Path(args.dataset_dir) / "pit_meta.json"
    ckpt_dir = Path(args.out_dir) / "checkpoints"
    fold_files = sorted(ckpt_dir.glob("fold_*.json"))
    if not fold_files:
        raise SystemExit("no fold checkpoints found under out_dir/checkpoints")
    eval_days: list[str] = []
    seen: set[str] = set()
    for fp in fold_files:
        d = json.loads(fp.read_text(encoding="utf-8"))
        for raw in d.get("eval_dates", []):
            day = str(raw)[:10]
            if day not in seen:
                seen.add(day)
                eval_days.append(day)
    eval_days.sort()
    print(f"[2] eval days (18-fold union): {len(eval_days)}", flush=True)

    matrix, valid_dates = _daily_ic_matrix(
        store,
        eval_dates=eval_days,
        feature_columns=feature_columns,
    )
    store.close()
    print(
        f"[3] IC matrix {matrix.shape[0]} days × {matrix.shape[1]} features "
        f"in {time.time() - t0:.0f}s rss={_rss_mib():.0f}MiB",
        flush=True,
    )

    # 汇总统计：每特征 全期均值 / bootstrap CI / 分月均值 / 有效日数。
    rng = np.random.default_rng(20260907)
    n_days, n_features = matrix.shape
    n_boot = 1000
    boot_means = np.empty((n_boot, n_features), dtype=float)
    for b in range(n_boot):
        idx = rng.integers(0, n_days, n_days)
        sample = matrix[idx]
        with np.errstate(invalid="ignore"):
            boot_means[b] = np.nanmean(sample, axis=0)
    results: list[dict[str, object]] = []
    for j, name in enumerate(feature_columns):
        col = matrix[:, j]
        valid = col[~np.isnan(col)]
        # 分月均值与方向一致性
        month_vals: dict[str, list[float]] = {}
        for i, day in enumerate(valid_dates):
            v = matrix[i, j]
            if not math.isnan(v):
                month_vals.setdefault(day[:7], []).append(float(v))
        monthly = {m: float(np.mean(vs)) for m, vs in month_vals.items()}
        bm = boot_means[:, j]
        bm_valid = bm[~np.isnan(bm)]
        if bm_valid.size:
            ci_low = float(np.quantile(bm_valid, 0.025))
            ci_high = float(np.quantile(bm_valid, 0.975))
        else:
            ci_low = ci_high = float("nan")
        results.append(
            {
                "feature": name,
                "family": _feature_family(name),
                "ic_mean": float(np.mean(valid)) if valid.size else float("nan"),
                "ic_ci95": [ci_low, ci_high],
                "ic_std": float(np.std(valid)) if valid.size else float("nan"),
                "valid_days": int(valid.size),
                "monthly_ic": monthly,
                # 分月方向一致性：正均值月份数（排除当月无有效日的月份）
                "months_positive": int(sum(1 for v in monthly.values() if v > 0)),
                "months_total": len(monthly),
            }
        )

    # 排序：|ic_mean| 降序，便于一眼看到最强信号（无论方向）。
    results.sort(key=lambda r: -abs(float(r["ic_mean"]) if r["ic_mean"] == r["ic_mean"] else 0.0))

    # 汇总：族级聚合 + 方向分布。
    family_agg: dict[str, dict[str, float]] = {}
    for r in results:
        fam = str(r["family"])
        v = float(r["ic_mean"])
        if math.isnan(v):
            continue
        agg = family_agg.setdefault(
            fam, {"sum": 0.0, "count": 0.0, "positive": 0.0, "negative": 0.0}
        )
        agg["sum"] += v
        agg["count"] += 1
        if v > 0:
            agg["positive"] += 1
        else:
            agg["negative"] += 1
    family_summary = {
        fam: {
            "n_features": int(agg["count"]),
            "ic_mean_avg": agg["sum"] / agg["count"],
            "n_positive": int(agg["positive"]),
            "n_negative": int(agg["negative"]),
        }
        for fam, agg in family_agg.items()
    }

    ci_sig = [
        r
        for r in results
        if r["ic_ci95"][0] != float("nan")
        and r["ic_ci95"][1] != float("nan")
        and (r["ic_ci95"][0] > 0 or r["ic_ci95"][1] < 0)
    ]
    payload = {
        "generated_at": datetime.now().isoformat(),
        "dataset": json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {},
        "eval_days": len(valid_dates),
        "eval_day_range": [valid_dates[0], valid_dates[-1]] if valid_dates else [],
        "n_features": n_features,
        "ci_significant_count": len(ci_sig),
        "family_summary": family_summary,
        "features": results,
    }
    out_path = (
        Path(args.out_dir) / f"feature_attribution_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    )
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    # 摘要前 15 打到 stdout（NAS 侧快速查看）。
    print(f"[4] top |IC| features (of {n_features}):", flush=True)
    for r in results[:15]:
        print(
            f"  {r['feature']:<40} {r['family']:<16} "
            f"IC={r['ic_mean']:+.4f} CI=[{r['ic_ci95'][0]:+.4f},{r['ic_ci95'][1]:+.4f}] "
            f"days={r['valid_days']} months+={r['months_positive']}/{r['months_total']}",
            flush=True,
        )
    print(f"[5] ci_significant={len(ci_sig)} json={out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
