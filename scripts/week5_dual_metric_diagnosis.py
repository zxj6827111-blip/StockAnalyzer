#!/usr/bin/env python
"""方案 A 补充诊断：特征→label 与特征→fwd_return 的双重口径对比（2026-09-07）。

回答一个关键矛盾：Phase 2 模型 AUC=0.586（预测 TP/SL 标签不错）但分数
IC=-0.024（排序收益反向）。归因扫描只测了特征→fwd_return；本脚本补测
特征→label 口径，区分三种情形：
  A. 特征对两个口径都负 → 模型忠实学了反向规则，方向一（语义反转）成立
  B. 特征对 label 正、对 fwd_return 负 → label 与收益口径脱节，问题在标签
  C. 两个口径都不显著 → 模型 AUC 来自交互项，单特征归因不够

实现：在逐日 IC 矩阵的同一循环里，对每特征额外计算当日横截面 AUC
（label 为 0/1，含 soft 0.5）。复用 compute_auc_brier。
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
from stock_analyzer.learning.scoring_eval import _rankdata


def _rss_mib() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return -1.0
    return -1.0


def _daily_ic_auc_matrix(
    store: PitDatasetStore,
    *,
    eval_dates: list[str],
    feature_columns: list[str],
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """同日双口径：每特征对 fwd_return 的 Spearman IC + 对 label 的 AUC。"""

    n_features = len(feature_columns)
    valid_dates: list[str] = []
    ic_rows: list[np.ndarray] = []
    auc_rows: list[np.ndarray] = []
    for i, day in enumerate(eval_dates):
        cols = ", ".join(["fwd_return", "label"] + feature_columns)
        frame = store._con.execute(  # noqa: SLF001
            f"SELECT {cols} FROM pit_dedup WHERE trade_date = ?",
            [day],
        ).fetch_df()
        if frame.empty:
            continue
        fwd = frame["fwd_return"].to_numpy(dtype=float)
        lab = frame["label"].to_numpy(dtype=float)
        feat = frame[feature_columns].to_numpy(dtype=float)

        # --- IC 口径（fwd_return 秩）---
        rank_y = _rankdata(fwd)
        yc = rank_y - rank_y.mean()
        y_denom = math.sqrt(float((yc * yc).sum()))
        ic_row = np.full(n_features, np.nan, dtype=float)

        # --- AUC 口径（label 0/0.5/1，soft 0.5 计入正类近似）---
        auc_row = np.full(n_features, np.nan, dtype=float)
        pos_mask = lab > 0
        neg_mask = lab < 1
        n_pos = int(pos_mask.sum())
        n_neg = int(neg_mask.sum())

        for j in range(n_features):
            x = feat[:, j]
            mask = ~np.isnan(x)
            nv = int(mask.sum())
            if nv < 30:
                continue
            if y_denom > 0.0:
                rx = _rankdata(x[mask])
                xc = rx - rx.mean()
                x_denom = math.sqrt(float((xc * xc).sum()))
                if x_denom > 0.0:
                    ic_row[j] = float((xc * yc[mask]).sum() / (x_denom * y_denom))
            # AUC：正类（label>0，soft 0.5 近似计入）秩和法，只用特征有效行。
            if n_pos >= 10 and n_neg >= 10:
                xs = x[mask]
                ls = lab[mask]
                rs = _rankdata(xs)
                pm = ls > 0
                nm = ls < 1
                if pm.sum() >= 10 and nm.sum() >= 10:
                    rank_sum_pos = float(rs[pm].sum())
                    p_cnt = int(pm.sum())
                    n_cnt = int(nm.sum())
                    auc_row[j] = (rank_sum_pos - p_cnt * (p_cnt + 1) / 2.0) / (p_cnt * n_cnt)
        ic_rows.append(ic_row)
        auc_rows.append(auc_row)
        valid_dates.append(day)
        if (i + 1) % 40 == 0:
            print(f"[scan] {i + 1}/{len(eval_dates)} days rss={_rss_mib():.0f}MiB", flush=True)
    ic_matrix = np.vstack(ic_rows) if ic_rows else np.empty((0, n_features))
    auc_matrix = np.vstack(auc_rows) if auc_rows else np.empty((0, n_features))
    return ic_matrix, auc_matrix, valid_dates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default="/app/artifacts/phase2/pit_dataset_ext")
    parser.add_argument("--out-dir", default="/app/artifacts/phase2")
    args = parser.parse_args()

    t0 = time.time()
    store = PitDatasetStore(args.dataset_dir)
    feature_columns = store.feature_columns
    print(f"[1] rows={store.row_count():,} features={len(feature_columns)}", flush=True)

    ckpt_dir = Path(args.out_dir) / "checkpoints"
    eval_days: list[str] = []
    seen: set[str] = set()
    for fp in sorted(ckpt_dir.glob("fold_*.json")):
        d = json.loads(fp.read_text(encoding="utf-8"))
        for raw in d.get("eval_dates", []):
            day = str(raw)[:10]
            if day not in seen:
                seen.add(day)
                eval_days.append(day)
    eval_days.sort()
    print(f"[2] eval days: {len(eval_days)}", flush=True)

    ic_matrix, auc_matrix, valid_dates = _daily_ic_auc_matrix(
        store, eval_dates=eval_days, feature_columns=feature_columns
    )
    store.close()
    print(f"[3] matrices {ic_matrix.shape} in {time.time() - t0:.0f}s", flush=True)

    # bootstrap CI（日期块）
    rng = np.random.default_rng(20260907)
    n_days = ic_matrix.shape[0]
    n_features = ic_matrix.shape[1]
    n_boot = 1000
    boot_ic = np.empty((n_boot, n_features))
    boot_auc = np.empty((n_boot, n_features))
    for b in range(n_boot):
        idx = rng.integers(0, n_days, n_days)
        with np.errstate(invalid="ignore"):
            boot_ic[b] = np.nanmean(ic_matrix[idx], axis=0)
            boot_auc[b] = np.nanmean(auc_matrix[idx], axis=0)

    results = []
    for j, name in enumerate(feature_columns):
        ic_col = ic_matrix[:, j]
        auc_col = auc_matrix[:, j]
        ic_valid = ic_col[~np.isnan(ic_col)]
        auc_valid = auc_col[~np.isnan(auc_col)]
        ic_mean = float(np.mean(ic_valid)) if ic_valid.size else float("nan")
        auc_mean = float(np.mean(auc_valid)) if auc_valid.size else float("nan")

        def _ci(boot_col: np.ndarray) -> list[float]:
            v = boot_col[~np.isnan(boot_col)]
            if v.size == 0:
                return [float("nan"), float("nan")]
            return [float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))]

        results.append(
            {
                "feature": name,
                "ic_mean": ic_mean,
                "ic_ci95": _ci(boot_ic[:, j]),
                "auc_mean": auc_mean,
                "auc_ci95": _ci(boot_auc[:, j]),
                "days": int(ic_valid.size),
            }
        )

    # 分类：A=双负 / B=IC负但AUC正(>0.5) / C=其他
    pile_a: list[dict] = []
    pile_b: list[dict] = []
    for r in results:
        if math.isnan(r["ic_mean"]) or math.isnan(r["auc_mean"]):
            continue
        ic_neg = r["ic_ci95"][1] < 0
        auc_pos = r["auc_ci95"][0] > 0.5
        if ic_neg and auc_pos:
            pile_b.append(r)
        elif ic_neg and r["auc_mean"] < 0.5:
            pile_a.append(r)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "eval_days": len(valid_dates),
        "n_features": n_features,
        "pile_A_ic_neg_and_auc_neg": len(pile_a),
        "pile_B_ic_neg_but_auc_pos": len(pile_b),
        "features": results,
    }
    out_path = (
        Path(args.out_dir) / f"feature_dual_metric_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    )
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"[4] pile A (both negative): {len(pile_a)}", flush=True)
    print(f"[5] pile B (IC neg, AUC pos): {len(pile_b)}", flush=True)
    if pile_b:
        print("    --- pile B features (label/return 口径脱节) ---", flush=True)
        for r in sorted(pile_b, key=lambda r: -(r["auc_mean"] - 0.5))[:15]:
            ic_lo, ic_hi = r["ic_ci95"]
            au_lo, au_hi = r["auc_ci95"]
            print(
                f"    {r['feature']:<36} IC={r['ic_mean']:+.4f} "
                f"CI=[{ic_lo:+.4f},{ic_hi:+.4f}] "
                f"AUC={r['auc_mean']:.4f} CI=[{au_lo:.4f},{au_hi:.4f}]",
                flush=True,
            )
    print(f"[6] json={out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
