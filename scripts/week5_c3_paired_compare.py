"""C3/C6 配对比较：从 fold checkpoint 复算逐日 IC 与配对 ΔIC 的块 bootstrap CI。

为什么要有这个脚本：C3 §3 的配对表（`raw_blend − blend = +0.0098 [+0.0013, +0.0188]`
等）当初是一次性脚本算的，没进仓库——于是那些数字**不可复现**。本脚本把口径固化：

- 逐日 IC 直接从 `checkpoints_<variant>/fold_*.json` 的 `daily_ic` 读，不重训；
- 配对差 = 先按**交易日**取差（任一侧缺失即丢弃并计数），再对**差值序列**做
  连续交易日块 moving-block bootstrap（`date_block_bootstrap_ci`，块长预设）；
  不对两条序列各自求 CI 再比区间重叠——那不是配对检验；
- 输出 ΔIC 均值、95% CI、有效日/块数/重复日/未配对日、失效月数。

自检（预注册 §9 要求）：在 C3 的 `c3_variants` 目录上复现 §3 表里的
`raw_blend − blend = +0.0098 [+0.0013, +0.0188]`；复现不出，说明本脚本与当初的算法
不一致，任何基于它的结论同样不可用。

用法：
    python scripts/week5_c3_paired_compare.py \
        --out-dir artifacts/phase2_label_remediation/c3_variants \
        --pair raw_blend:blend --pair reversal:blend
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

# 允许直接从仓库根运行（容器内 site-packages 已装包，本地源码树用 src 布局）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from stock_analyzer.learning.scoring_eval import (  # noqa: E402
    DEFAULT_BLOCK_TRADING_DAYS,
    date_block_bootstrap_ci,
)


def load_daily_ic(out_dir: Path, variant: str) -> dict[str, float]:
    """读一个变体的逐日 IC（交易日 → 值）。缺 checkpoint 目录时抛错，不静默给空。"""
    ckpt_dir = out_dir / f"checkpoints_{variant.strip().lower()}"
    if not ckpt_dir.is_dir():
        raise SystemExit(f"checkpoint 目录不存在: {ckpt_dir}")
    merged: dict[str, float] = {}
    files = sorted(ckpt_dir.glob("fold_*.json"))
    if not files:
        raise SystemExit(f"checkpoint 目录为空: {ckpt_dir}")
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if str(raw.get("status", "")) not in {"completed", "completed_unlabeled"}:
            continue
        for day, value in raw.get("daily_ic", []):
            merged[str(day)] = float(value)
    return merged


def paired_delta(
    current: dict[str, float],
    baseline: dict[str, float],
    *,
    block_days: int,
) -> dict[str, Any]:
    """同日配对差值 + 块 bootstrap CI（口径见模块 docstring）。"""
    days = sorted(set(current) & set(baseline))
    diffs = [(day, current[day] - baseline[day]) for day in days]
    diffs = [(day, value) for day, value in diffs if math.isfinite(value)]
    ci = date_block_bootstrap_ci(diffs, block_days=block_days)
    months: dict[str, list[float]] = {}
    for day, value in diffs:
        months.setdefault(day[:7], []).append(value)
    month_means = {month: float(np.mean(vals)) for month, vals in months.items()}
    mean = float(np.mean([v for _, v in diffs])) if diffs else float("nan")
    return {
        "delta_ic_mean": mean,
        "delta_ic_ci95": [float(ci["ci_low"]), float(ci["ci_high"])],
        "valid_days": int(ci["valid_days"]),
        "block_days": ci.get("block_days"),
        "n_blocks": ci.get("n_blocks"),
        "duplicate_days": ci.get("duplicate_days"),
        "unpaired_days": max(len(current), len(baseline)) - len(days),
        "months_negative": sum(1 for value in month_means.values() if value < 0),
        "months_total": len(month_means),
    }


def _corr(a: dict[str, float], b: dict[str, float]) -> float:
    days = sorted(set(a) & set(b))
    if len(days) < 2:
        return float("nan")
    left = np.array([a[d] for d in days], dtype=float)
    right = np.array([b[d] for d in days], dtype=float)
    if float(np.nanstd(left)) == 0.0 or float(np.nanstd(right)) == 0.0:
        return float("nan")
    return float(np.corrcoef(left, right)[0, 1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C3/C6 配对 ΔIC 比较（checkpoint 复算）")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        help="形如 current:baseline 的配对，可重复（默认全部与 blend 比）",
    )
    parser.add_argument(
        "--block-days",
        type=int,
        default=int(DEFAULT_BLOCK_TRADING_DAYS),
        help="moving-block 块长；改它等于改口径，必须显式传并在报告里留痕",
    )
    parser.add_argument("--json-out", default="", help="把结果写到该 JSON 路径")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    pairs = [item for item in args.pair if ":" in item] or []
    if not pairs:
        parser.error("至少给一个 --pair current:baseline")

    variants: dict[str, dict[str, float]] = {}
    results: dict[str, dict[str, Any]] = {}
    for spec in pairs:
        current_name, baseline_name = (part.strip().lower() for part in spec.split(":", 1))
        for name in (current_name, baseline_name):
            if name not in variants:
                variants[name] = load_daily_ic(out_dir, name)
        results[spec] = paired_delta(
            variants[current_name], variants[baseline_name], block_days=int(args.block_days)
        )
        results[spec]["error_correlation_with_baseline"] = _corr(
            variants[current_name], variants[baseline_name]
        )
        results[spec]["baseline_days"] = len(variants[baseline_name])
        results[spec]["current_days"] = len(variants[current_name])

    payload = {"out_dir": str(out_dir), "block_days": int(args.block_days), "pairs": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
