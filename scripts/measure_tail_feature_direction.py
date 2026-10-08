"""在全市场合格池上量每类信息的**时间外**判别力（改进计划 §3.2 / §4）。

为什么单独量这个：`validate_tail_selection_quality.py` 在第 1 折校准段就 fail-closed
停住了（raw AUC=0.4720，方向不为正），于是四折净盈利率根本还没开始算。停是对的，
但它只说"这四条一起没有方向"，说不出**哪一类**有方向、哪一类在拖。这条脚本把同一批
样本按特征与特征组拆开，各自给校准段与时间外测试段的 AUC，让"只有时间外有效的特征
进入正式候选"这条要求有可读的证据，而不是靠一次整体失败下结论。

只做只读分析，不改标签、不改排序、不产推荐。AUC 在这里是**判别力**度量，不是命中率。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.feature.trend_candidate_contract import (  # noqa: E402
    FEATURE_GROUPS,
)

LABEL_FIELD = "label"
DATE_FIELD = "decision_date"

#: 第一轮校验过的四条 + 每组再补一条，用来区分"组没信息"与"这条特征没信息"。
PROBE_FEATURES = (
    "excess_ret_20",
    "relative_strength",
    "ma20_slope",
    "close_to_ma20",
    "range_position_60",
    "avg_turnover_20",
    "volume_ratio_5",
    "turnover",
    "atr14_pct",
    "realized_vol_20",
    "gap_up_pct",
)


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get(LABEL_FIELD) in (0, 1, 0.0, 1.0) and row.get(DATE_FIELD):
                rows.append(row)
    rows.sort(key=lambda r: (str(r[DATE_FIELD]), str(r.get("symbol", ""))))
    return rows


def _flat_value(row: dict[str, Any], name: str) -> float | None:
    for container in (row, row.get("overnight_features") or {}):
        if name in container:
            value = container[name]
            if isinstance(value, (int, float)) and value == value:
                return float(value)
            return None
    return None


def auc(values: list[float], labels: list[int]) -> float | None:
    """秩和 AUC（含并列取均值秩）；单侧样本或全同类返回 None，不返回 0.5 假装中性。"""
    if not values or len(set(labels)) < 2:
        return None
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    positives = sum(labels)
    negatives = len(labels) - positives
    rank_sum = sum(rank for rank, label in zip(ranks, labels, strict=True) if label == 1)
    return round((rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives), 4)


def split_by_date(rows: list[dict[str, Any]], calib_tail: int, test_tail: int) -> tuple:
    """按决策日切三段：早期训练、校准（训练后一段）、时间外测试（最后一段）。

    这里不用 walk-forward 的折，因为整体失败已经发生在第 1 折；本脚本的目的是把
    "哪一类信息有方向"和"折怎么切"这两件事分开，切法在报告里写清楚。
    """
    days = sorted({str(r[DATE_FIELD]) for r in rows})
    if len(days) < 12:
        return [], [], []
    test_days = set(days[-test_tail:])
    calib_days = set(days[-(test_tail + calib_tail):-test_tail])
    keep = test_days | calib_days
    train = [r for r in rows if str(r[DATE_FIELD]) not in keep]
    calib = [r for r in rows if str(r[DATE_FIELD]) in calib_days]
    test = [r for r in rows if str(r[DATE_FIELD]) in test_days]
    return train, calib, test


def measure(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    train, calib, test = split_by_date(rows, calib_tail=15, test_tail=15)
    out: dict[str, Any] = {"feature": name}
    for tag, part in (("train", train), ("calibration", calib), ("out_of_sample_test", test)):
        pairs = [(v, int(r[LABEL_FIELD])) for r in part if (v := _flat_value(r, name)) is not None]
        out[f"{tag}_rows"] = len(pairs)
        out[f"{tag}_auc"] = auc([p[0] for p in pairs], [p[1] for p in pairs])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--features", default=",".join(PROBE_FEATURES))
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.samples))
    if not rows:
        print("没有可用样本", file=sys.stderr)
        return 4
    labels = [int(r[LABEL_FIELD]) for r in rows]
    group_of = {
        feature: group
        for group, features in FEATURE_GROUPS.items()
        for feature in features
    }
    report: dict[str, Any] = {
        "samples": len(rows),
        "decision_days": len({str(r[DATE_FIELD]) for r in rows}),
        "base_rate_net_profit": round(sum(labels) / len(labels), 4),
        "split_note": "train=前段决策日；calibration=倒数第 16-30 天；"
                      "out_of_sample_test=最后 15 天",
        "features": [
            {**measure(rows, name.strip()), "group": group_of.get(name.strip(), "unmapped")}
            for name in args.features.split(",")
            if name.strip()
        ],
    }
    by_group: dict[str, list[float | None]] = defaultdict(list)
    for item in report["features"]:
        if item["out_of_sample_test_auc"] is not None:
            by_group[item["group"]].append(item["out_of_sample_test_auc"])
    report["group_best_oos_auc"] = {
        group: max(values) for group, values in sorted(by_group.items())
    }
    report["groups_without_signal"] = sorted(
        group for group, values in by_group.items() if all(v <= 0.5 for v in values)
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"base_rate = {report['base_rate_net_profit']} over {report['samples']} samples "
          f"/ {report['decision_days']} days")
    for item in report["features"]:
        print(f"{item['feature']:<20} group={item['group']:<22} "
              f"calib_auc={item['calibration_auc']} oos_auc={item['out_of_sample_test_auc']}")
    print(f"group_best_oos_auc = {report['group_best_oos_auc']}")
    print(f"groups_without_signal = {report['groups_without_signal']}")
    print(f"报告已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
