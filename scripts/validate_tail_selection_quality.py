"""CLI：trend 尾盘链路的选股质量滚动验证（改进计划 §4 选股质量验收）。

样本来自历史重建或真实观察（``capture_mode`` 必须逐行声明，两类分开计数）；
折边界、训练、校准、排序、判定全部复用线上那套权威实现。

退出码是**真实退出码**，不是打印出来的字面数字：

- ``0`` 质量门通过（净盈利率提升 ≥5pp、分块 bootstrap CI 下界 >0、平均净收益 >0、
  尾部不明显恶化、折数达标）
- ``3`` 样本不足 → 记为 blocked，继续采集带时刻的分钟行情
- ``4`` 跑完了但质量门不过
- ``5`` 输入/训练/身份失败（包括要求 LightGBM 却没有原生 booster）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import (  # noqa: E402
    DEFAULT_TREND_CONTRACT,
    TrendContractError,
)
from stock_analyzer.models.tail_net_profit_trainer import (  # noqa: E402
    MIN_TEST_FOLDS,
    TailTrainingError,
)
from stock_analyzer.research.tail_walk_forward import (  # noqa: E402
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    run_walk_forward,
)

RC_PASS = 0
RC_BLOCKED = 3
RC_GATE_FAILED = 4
RC_ERROR = 5


def _as_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"sample line is not an object: {payload!r}")
        for field in ("decision_date", "entry_date"):
            day = _as_date(payload.get(field))
            if day is None:
                raise ValueError(f"sample row has no parseable {field}: {payload!r}")
            payload[field] = day
        rows.append(payload)
    if not rows:
        raise ValueError(f"sample file {path} is empty")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, help="JSONL：每行一个候选样本（标签+特征）")
    parser.add_argument("--features", required=True, help="逗号分隔的特征名，训练与打分共用")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--training-commit", required=True)
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--feature-compute-version", type=int, required=True)
    parser.add_argument("--label-policy-id", required=True)
    parser.add_argument("--baseline-field", default="composite_score",
                        help="匹配基线的排序键（同一合格池、同一上限，只换排序）")
    parser.add_argument("--folds", type=int, default=MIN_TEST_FOLDS)
    parser.add_argument("--out", default="artifacts/research/tail_walk_forward.json")
    args = parser.parse_args(argv)

    feature_names = [name.strip() for name in str(args.features).split(",") if name.strip()]
    if not feature_names:
        print("--features 不能为空", file=sys.stderr)
        return RC_ERROR

    try:
        rows = load_rows(Path(args.samples))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"样本不可用: {exc}", file=sys.stderr)
        return RC_ERROR

    try:
        report = run_walk_forward(
            rows=rows,
            feature_names=feature_names,
            model_id=args.model_id,
            training_commit=args.training_commit,
            runtime_commit=args.runtime_commit,
            feature_compute_version=args.feature_compute_version,
            label_policy_id=args.label_policy_id,
            baseline_rank_field=args.baseline_field,
            folds=args.folds,
            contract=DEFAULT_TREND_CONTRACT,
        )
    except (TailTrainingError, TrendContractError, ValueError) as exc:
        # 训练或身份失败就停：不降级、不换模型、不给出"看起来能跑完"的数字。
        print(f"training/identity failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    if report.get("status") == STATUS_BLOCKED:
        print(f"blocked: {report.get('blockers')}")
        print("选股质量验收记为阻塞，继续采集带时刻的分钟行情，不得用开盘回测代替")
        return RC_BLOCKED
    if report.get("status") != STATUS_COMPLETED:
        print(f"unexpected status: {report.get('status')!r}", file=sys.stderr)
        return RC_ERROR

    quality = report.get("quality") or {}
    print(f"folds = {report.get('fold_count')}")
    print(f"improvement_pp = {quality.get('improvement_pp')}")
    print(f"block bootstrap ci = [{quality.get('block_bootstrap', {}).get('ci_low')}, "
          f"{quality.get('block_bootstrap', {}).get('ci_high')}]")
    print(f"failed_gates = {quality.get('failed_gates')}")
    print(f"报告已写入 {out_path}")
    return RC_PASS if quality.get("passed") else RC_GATE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
