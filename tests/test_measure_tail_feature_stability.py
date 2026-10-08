"""四组特征时间外稳定性测量的用例（计划 §2 第 2 问 + §3.2"只有时间外有效才进正式候选"）。

这里钉的是**测量本身不许说谎**：
1. 折的划分必须与滚动验证同一套（`build_rolling_splits`），标签在测试窗前已成熟；
2. 算不出来的列/折要进 `unusable` 并退 3，不是悄悄给个 0.5；
3. 信息重复度用当日排序口径的秩相关，跨列同序必须被抓出来。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "measure_tail_feature_stability", REPO_ROOT / "scripts" / "measure_tail_feature_stability.py"
)
stab = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(stab)

START = date(2026, 1, 5)


def _days(count: int) -> list[date]:
    out, cursor = [], START
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _samples(tmp_path: Path, days: list[date], *, per_day: int = 14) -> Path:
    """good 列与标签同序；dup 列是 good 的单调复制（同信息）；noise 列与标签无关。"""
    rng = np.random.default_rng(11)
    rows = []
    for day in days:
        for slot in range(per_day):
            good = float(rng.uniform())
            label = 1 if good > 0.5 else 0
            rows.append({
                "entry_date": day.isoformat(), "symbol": f"{slot:06d}", "label": label,
                "excess_ret_20": good,
                "close_to_ma20": good * 3.0 + 1.0,
                "avg_turnover_20": float(rng.uniform()),
                "atr14_pct": float(rng.uniform()),
            })
    path = tmp_path / "samples.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


ARGS = ["--folds", "2", "--calibration-sessions", "4", "--min-train-sessions", "8"]


def test_run_reports_stability_and_redundancy(tmp_path: Path) -> None:
    days = _days(40)
    samples = _samples(tmp_path, days)
    rc = stab.main(["--samples", str(samples), "--out", str(tmp_path / "out.json"),
                    "--quiet", *ARGS])
    assert rc == stab.RC_OK
    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))

    assert report["folds_measured"] == 2 and report["unusable"] == []
    # 每折的测试窗互不重叠，且都在训练/校准窗之后（embargo 由 build_rolling_splits 保证）
    ranges = [
        (date.fromisoformat(v["test_from"]), date.fromisoformat(v["test_to"]))
        for v in report["fold_ranges"].values()
    ]
    assert ranges[0][1] < ranges[1][0]

    assert report["per_column"]["excess_ret_20"]["folds_above_half"] == 2
    assert all(value == 1.0 for value in
               report["per_column"]["excess_ret_20"]["per_fold"].values())
    assert report["per_column"]["excess_ret_20"]["per_fold"]["fold1"] == 1.0
    # dup 列与 good 列同序：必须被认成同一份信息，且跨组重复要标出来
    pairs = {(item["a"], item["b"]) for item in report["redundant_pairs"]}
    assert ("excess_ret_20", "close_to_ma20") in pairs
    cross = [item for item in report["redundant_pairs"]
             if {item["a"], item["b"]} == {"excess_ret_20", "close_to_ma20"}]
    assert cross and cross[0]["same_group"] is False
    assert cross[0]["spearman"] == 1.0


def test_column_that_cannot_be_measured_is_named_not_filled(tmp_path: Path) -> None:
    days = _days(40)
    samples = _samples(tmp_path, days)
    frame = pd.read_json(samples, lines=True)
    # 让 noise 列在第二折里全为同一个常数 + 标签单一类别 ⇒ 这一格无从测量
    second = sorted(set(frame["entry_date"]))[24:]
    frame.loc[frame["entry_date"].isin(second), "atr14_pct"] = 0.03
    frame.loc[frame["entry_date"].isin(second), "label"] = 1
    frame.to_json(samples, orient="records", lines=True, date_format="iso")

    rc = stab.main(["--samples", str(samples), "--out", str(tmp_path / "out.json"),
                    "--quiet", *ARGS])
    assert rc == stab.RC_PARTIAL
    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    reasons = {item["column"] for item in report["unusable"]}
    assert "atr14_pct" in reasons
    assert report["ok"] is False
