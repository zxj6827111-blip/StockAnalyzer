"""M4-H 分层后处理：按 calendar year 与 market regime 报告指标。

用户 §23/§24 要求按年单独报告，避免"某一年赚很多掩盖另外几年失效"。
分层**只在已落盘的逐折逐票打分上做**（不重训、不改模型），因此不引入任何新的
选择性偏差：它是对同一批 locked OOS 结果的重新聚合。

Regime 的构造（**ex-post evaluation strata，不得进入预测输入**）：
以全市场当日等权净收益（由 eligible 决策集合的 `net_return_5d` 按日聚合得到）为
市场序列，取其 20 个决策日滚动均值与滚动波动：
- ``bull``     : 滚动均值 > +1 个横截面标准差
- ``bear``     : 滚动均值 < -1 个横截面标准差
- ``sideways`` : 其余
- ``high_vol`` / ``low_vol`` : 滚动波动在全体决策日的中位数之上/之下

该定义在报告 §19 中显式声明为 EXPLORATORY 分层（协议冻结时未纳入具体阈值）。

用法::

    python scripts/alpha_v2_m4h_strata.py --root artifacts/alpha_v2/m4h
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from stock_analyzer.alpha_v2.research import metrics as met  # noqa: E402

SCHEMA = "alpha_v2_m4h_strata.v1"
PIPELINE_COLUMNS = (
    "decision_date",
    "symbol",
    "rank_score",
    "net_return_5d",
    "excess_return_5d",
    "net_return_3d",
    "excess_return_3d",
    "mae_5d",
    "mfe_5d",
    "executable",
)


def load_predictions(root: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted((root / "predictions").glob("fold_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records") or []
        if not records:
            continue
        frames.append(pd.DataFrame(records))
    if not frames:
        return pd.DataFrame(columns=list(PIPELINE_COLUMNS))
    merged = pd.concat(frames, ignore_index=True)
    for column in PIPELINE_COLUMNS:
        if column not in merged.columns:
            merged[column] = np.nan
    merged["decision_date"] = pd.to_datetime(merged["decision_date"], errors="coerce")
    return merged


def _metric_block(
    frame: pd.DataFrame, *, label: str, min_cross_section: int = 20
) -> dict[str, Any]:
    """一个小样本上的核心指标（IC + TopK + 分位 + 下行 + 成交）。"""
    block: dict[str, Any] = {"strata": label, "rows": int(len(frame))}
    if frame.empty:
        return {**block, "status": "empty"}
    days = int(frame["decision_date"].nunique())
    block["decision_dates"] = days
    for horizon in (5, 3):
        metric = met.metric_column("excess_return", horizon)
        if metric not in frame.columns or frame[metric].isna().all():
            continue
        daily = met.daily_rank_ic(
            frame, score_column="rank_score", metric_column_=metric,
            min_cross_section=min_cross_section,
        )
        summary = met.ic_summary(daily)
        block[f"rank_ic_{horizon}d"] = {
            "mean_ic": summary.get("mean_ic"),
            "median_ic": summary.get("median_ic"),
            "mature_dates": summary.get("mature_dates"),
            "ci95": summary.get("ci95"),
            "positive_ratio": summary.get("positive_ratio"),
        }
        block[f"topk_{horizon}d"] = met.topk_metrics(
            frame, score_column="rank_score", metric_columns=[metric], ks=(1, 3, 5)
        )
        block[f"quantile_{horizon}d"] = met.quantile_monotonicity(
            met.quantile_returns(
                frame,
                score_column="rank_score",
                metric_column_=metric,
                min_cross_section=min_cross_section,
            )
        )
    block["downside_5d"] = met.downside_metrics(frame, horizon=5, score_column="rank_score")
    executable = frame.get("executable")
    if executable is not None and len(frame):
        block["fill_rate"] = float(executable.astype(bool).mean())
    return block


def build_yearly_strata(frame: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if frame.empty:
        return out
    years = sorted({int(d.year) for d in frame["decision_date"].dropna().unique()})
    for year in years:
        subset = frame[frame["decision_date"].dt.year == year]
        out[str(year)] = _metric_block(subset, label=str(year))
    return out


def build_market_series(frame: pd.DataFrame, *, window: int = 20) -> pd.DataFrame:
    """市场序列：逐决策日的等权净收益（用可成交样本，不消费模型分数）。"""
    if frame.empty:
        return pd.DataFrame(columns=["decision_date", "market_mean", "market_vol"])
    usable = frame[frame["executable"].astype(bool)] if "executable" in frame.columns else frame
    daily = (
        usable.groupby("decision_date")["net_return_5d"]
        .agg(["mean", "std", "count"])
        .reset_index()
    )
    daily = daily.sort_values("decision_date").reset_index(drop=True)
    daily["market_mean"] = daily["mean"].rolling(window, min_periods=max(3, window // 4)).mean()
    daily["market_vol"] = daily["mean"].rolling(window, min_periods=max(3, window // 4)).std()
    return daily


def build_regime_strata(frame: pd.DataFrame) -> dict[str, Any]:
    """事后评估分层：把每个决策日归入一个 regime，再分别算指标。"""
    daily = build_market_series(frame)
    if daily.empty:
        return {}
    spread = float(daily["market_mean"].std(ddof=0) or 0.0)
    vol_median = float(daily["market_vol"].median() or 0.0)

    def _label(row: pd.Series) -> str:
        momentum = row["market_mean"]
        volatility = row["market_vol"]
        if not np.isfinite(momentum):
            return "unclassified"
        if np.isfinite(volatility) and volatility > vol_median:
            return "high_vol"
        if np.isfinite(volatility) and volatility <= vol_median:
            return "low_vol"
        return "unclassified"

    def _trend(row: pd.Series) -> str:
        momentum = row["market_mean"]
        if not np.isfinite(momentum) or spread <= 0:
            return "unclassified"
        if momentum > spread:
            return "bull"
        if momentum < -spread:
            return "bear"
        return "sideways"

    daily["vol_regime"] = daily.apply(_label, axis=1)
    daily["trend_regime"] = daily.apply(_trend, axis=1)
    labeled = frame.merge(
        daily[["decision_date", "trend_regime", "vol_regime"]], on="decision_date", how="left"
    )
    out: dict[str, Any] = {"definition": {
        "market_series": "eligible 决策集合的逐日等权 net_return_5d 均值",
        "window_decision_days": 20,
        "trend_threshold": "±1 个 market_mean 标准差",
        "vol_threshold": "market_vol 中位数",
        "status": "EXPLORATORY_EX_POST_STRATA",
    }}
    for column, name in (("trend_regime", "trend"), ("vol_regime", "volatility")):
        for value in sorted(labeled[column].dropna().unique()):
            subset = labeled[labeled[column] == value]
            out[f"{name}:{value}"] = _metric_block(subset, label=f"{name}:{value}")
    out["daily_series"] = daily.assign(
        decision_date=daily["decision_date"].dt.strftime("%Y-%m-%d")
    ).to_dict(orient="records")
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M4-H yearly / regime strata")
    parser.add_argument("--root", default=str(REPO_ROOT / "artifacts/alpha_v2/m4h"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    frame = load_predictions(root)
    print(f"[m4h-strata] prediction rows={len(frame):,}", flush=True)
    if frame.empty:
        print("[m4h-strata] no predictions found; nothing to do")
        return 1
    yearly = build_yearly_strata(frame)
    regime = build_regime_strata(frame)
    (root / "metrics").mkdir(parents=True, exist_ok=True)
    (root / "metrics" / "strata_yearly.json").write_text(
        json.dumps({"schema": SCHEMA, "years": yearly}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (root / "metrics" / "strata_regime.json").write_text(
        json.dumps(
            {"schema": SCHEMA, "regimes": regime}, ensure_ascii=False, indent=2, default=str
        ),
        encoding="utf-8",
    )
    print(f"[m4h-strata] years={list(yearly)} regimes={[k for k in regime if k != 'daily_series']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
