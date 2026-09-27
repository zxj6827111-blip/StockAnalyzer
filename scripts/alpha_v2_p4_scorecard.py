"""Alpha V2 P4-B1 历史扫描效果成绩单 CLI。

用法::

    python scripts/alpha_v2_p4_scorecard.py \
        --m4h-root artifacts/alpha_v2/m4h \
        --out artifacts/alpha_v2/p4_scorecard

输出 ``scorecard.json`` 与 ``scorecard.md``，回答"系统过去扫描出来的股票，
实际表现如何？"。统计逻辑全部在
``src/stock_analyzer/alpha_v2/research/reporting.py``；本文件只负责参数与输出。

定位与边界（任务书 P4-B1）：

- 研究报告层，**不参与任何生产门**（同 ``alpha_v2_m4h_run.py`` / NOTE-001 §2）；
- 纯读取：不修改 M4-H 工件、不写 production 路径、不 import 训练/验证链路；
- fail closed：数据缺失即以真实退出码失败，不产出半份报告。

退出码：0 成功；2 参数/输入错误（含 out 位于 m4h root 内）；3 outcome 数据
缺失（``--allow-missing-outcomes`` 显式降级除外）；4 工件一致性违例。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from stock_analyzer.alpha_v2.research.reporting import (  # noqa: E402
    ScorecardError,
    run_scorecard,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alpha_v2_p4_scorecard",
        description="历史扫描效果成绩单（P4-B1）：纯读取 M4-H 工件，输出 scorecard.json/md",
    )
    parser.add_argument(
        "--m4h-root",
        type=Path,
        default=Path("artifacts/alpha_v2/m4h"),
        help="M4-H 工件根目录（须含 predictions/、audit/run_manifest.json、cache/）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/alpha_v2/p4_scorecard"),
        help="成绩单输出目录（不得位于 --m4h-root 内部）",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="失败分析保留的最低收益条数（默认 20）",
    )
    parser.add_argument(
        "--allow-missing-outcomes",
        action="store_true",
        help=(
            "outcome cache 缺失时显式降级：仅统计预测自带的 T+5（成熟度未知），"
            "其余 horizon 标注 unavailable。默认 fail closed。"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scorecard, outputs = run_scorecard(
            args.m4h_root,
            args.out,
            top_n=args.top_n,
            allow_missing_outcomes=args.allow_missing_outcomes,
        )
    except ScorecardError as exc:
        # 违例必须变成真实退出码（ADR-001 §3.7 纪律）。
        print(f"[alpha_v2_p4_scorecard] 失败（exit {exc.exit_code}）: {exc}", file=sys.stderr)
        return exc.exit_code

    overall = scorecard["overall"]
    horizon = overall["primary_horizon"]
    stats = scorecard["horizon_stats"][f"T+{horizon}"]
    print(f"[alpha_v2_p4_scorecard] 协议 {scorecard['provenance']['protocol_id']}")
    print(
        f"评估区间 {overall['evaluation_period']['from']} ~ {overall['evaluation_period']['to']}"
        f" | 决策日 {overall['decision_count']} | 预测行 {overall['prediction_rows']}"
    )
    print(
        f"主 horizon T+{horizon}: samples={stats['samples']}"
        f" 平均收益={stats['average_return']:.4%}"
        f" 胜率={stats['positive_rate']:.2%}"
    )
    print(
        f"评分分层单调性: "
        f"{'通过' if scorecard['score_buckets']['monotonic_nonincreasing'] else '未通过'}"
        f"（最高/最低桶差 {scorecard['score_buckets']['top_bottom_spread']}）"
    )
    print(f"输出: {outputs['json']} / {outputs['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
