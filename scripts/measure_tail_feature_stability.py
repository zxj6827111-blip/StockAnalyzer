#!/usr/bin/env python3
"""四组特征的时间外稳定性 + 信息重复度测量（改进计划 §2 / §3.2）。

为什么单独要这一步：训练门在校准窗方向为负时就按 §3.3 停机和拒发概率，
所以**折内 test AUC 永远拿不到**。但不训练不等于不能测量 —— 每折的测试窗相对
该折训练/校准窗都是时间外的，直接算列级 AUC 就是"时间外有效性"最朴素的证据；
再配一张秩相关矩阵回答"是不是同一份信息被反复使用"（§2 的第 2 个诊断问题）。

只做测量，不改任何判定：哪一列能进正式候选仍由 `tail_net_profit_trainer` 的门决定。

退出码：0=每折每列都算出来了 / 3=有折或有列算不出来（如实列出，不填 0）/ 5=输入不可用。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import (  # noqa: E402
    DEFAULT_TREND_CONTRACT,
)
from stock_analyzer.feature.trend_candidate_contract import (  # noqa: E402
    FEATURE_GROUPS,
    columns_for_groups,
)
from stock_analyzer.research.tail_walk_forward import (  # noqa: E402
    DEFAULT_CALIBRATION_SESSIONS,
    DEFAULT_MIN_TRAIN_SESSIONS,
    build_rolling_splits,
)

RC_OK = 0
RC_PARTIAL = 3
RC_ERROR = 5
DATE_FIELD = "entry_date"
LABEL_FIELD = "label"
REDUNDANCY_THRESHOLD = 0.90


def _rank_auc(values: pd.Series, labels: pd.Series) -> float | None:
    """Mann-Whitney AUC，用**平均秩**处理并列（早期手写版本按排序位置给秩，全列同值）。"""
    frame = pd.DataFrame({"value": pd.to_numeric(values, errors="coerce"),
                          "label": labels.astype(int)}).dropna()
    positives = int(frame["label"].sum())
    negatives = int(len(frame) - positives)
    if positives == 0 or negatives == 0:
        return None
    ranks = frame["value"].rank(method="average").to_numpy()
    rank_sum = float(ranks[frame["label"].to_numpy() == 1].sum())
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def load_labelled(path: Path) -> pd.DataFrame:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if payload.get(LABEL_FIELD) in (0, 1, 0.0, 1.0):
                rows.append(payload)
    if not rows:
        raise ValueError(f"{path} 里没有已打标的样本行")
    frame = pd.DataFrame(rows)
    frame["entry_date"] = pd.to_datetime(frame["entry_date"].astype(str)).dt.date
    return frame


def _fold_windows(frame: pd.DataFrame, splits) -> list[tuple[str, date, date, pd.DataFrame]]:
    out = []
    for index, split in enumerate(splits, start=1):
        test_days = set(split.test_dates)
        window = frame.loc[frame["entry_date"].isin(test_days)]
        if window.empty:
            continue
        out.append((f"fold{index}", min(test_days), max(test_days), window))
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    contract = DEFAULT_TREND_CONTRACT
    frame = load_labelled(Path(args.samples))
    columns = [c for c in columns_for_groups(tuple(FEATURE_GROUPS)) if c in frame.columns]
    if not columns:
        raise ValueError("样本里没有 trend 契约声明的四组特征列")

    days = sorted(set(frame["entry_date"].tolist()))
    splits = build_rolling_splits(
        days, folds=int(args.folds), contract=contract,
        calibration_sessions=int(args.calibration_sessions),
        min_train_sessions=int(args.min_train_sessions),
    )
    windows = _fold_windows(frame, splits)

    per_column: dict[str, dict[str, Any]] = {}
    unusable: list[dict[str, str]] = []
    for name in columns:
        aucs: dict[str, float | None] = {}
        for tag, _start, _end, window in windows:
            value = _rank_auc(window[name], window[LABEL_FIELD])
            if value is None:
                unusable.append({"fold": tag, "column": name, "reason": "single_class_or_all_null"})
                continue
            aucs[tag] = round(float(value), 4)
        observed = [value for value in aucs.values() if value is not None]
        per_column[name] = {
            "per_fold": aucs,
            "folds_measured": len(observed),
            "folds_above_half": sum(1 for value in observed if value > 0.5),
            "mean_abs_edge": round(float(np.mean([abs(v - 0.5) for v in observed])), 4)
            if observed else None,
        }

    per_group = {}
    for group in FEATURE_GROUPS:
        group_columns = [c for c in columns_for_groups((group,)) if c in per_column]
        edges = [
            value for name in group_columns
            for value in (per_column[name]["per_fold"].values()) if value is not None
        ]
        per_group[group] = {
            "columns": group_columns,
            "measurements": len(edges),
            "mean_auc": round(float(np.mean(edges)), 4) if edges else None,
            "share_above_half": round(
                float(np.mean([1.0 if v > 0.5 else 0.0 for v in edges])), 4
            ) if edges else None,
        }

    ranks = frame[columns].rank(method="average")
    corr = ranks.corr(method="spearman")
    group_of = {
        column: group for group in FEATURE_GROUPS
        for column in columns_for_groups((group,))
    }
    redundant = []
    for i, left in enumerate(columns):
        for right in columns[i + 1:]:
            value = corr.at[left, right]
            if pd.notna(value) and abs(float(value)) >= REDUNDANCY_THRESHOLD:
                redundant.append({
                    "a": left, "b": right, "spearman": round(float(value), 4),
                    "same_group": group_of.get(left) == group_of.get(right),
                })
    redundant.sort(key=lambda item: -abs(item["spearman"]))

    return {
        "ok": not unusable and len(windows) == len(splits),
        "labelled_rows": int(len(frame)),
        "trade_days": len(days),
        "folds_requested": int(args.folds),
        "folds_measured": len(windows),
        "fold_ranges": {
            tag: {"test_from": start.isoformat(), "test_to": end.isoformat(),
                  "rows": int(len(window))}
            for tag, start, end, window in windows
        },
        "columns": columns,
        "per_column": per_column,
        "per_group": per_group,
        "redundant_pairs": redundant,
        "redundancy_threshold": REDUNDANCY_THRESHOLD,
        "unusable": unusable,
        "contract_digest": contract.digest(),
        "note": (
            "列级 AUC 只是测量，不是准入：进正式候选仍由 trainer 的校准方向门与"
            "§4 质量门决定"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--calibration-sessions", type=int, default=DEFAULT_CALIBRATION_SESSIONS)
    parser.add_argument("--min-train-sessions", type=int, default=DEFAULT_MIN_TRAIN_SESSIONS)
    parser.add_argument("--out", default="artifacts/research/tail_feature_stability.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = run(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"样本不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    if not args.quiet:
        print(json.dumps({
            "ok": report["ok"], "folds": f"{report['folds_measured']}/{report['folds_requested']}",
            "per_group": report["per_group"],
            "redundant_pair_count": len(report["redundant_pairs"]),
            "unusable": len(report["unusable"]),
        }, ensure_ascii=False, indent=2))
    return RC_OK if report["ok"] else RC_PARTIAL


if __name__ == "__main__":
    raise SystemExit(main())
