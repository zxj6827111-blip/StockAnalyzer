"""从独立研究库**重建**尾盘样本（改进计划 §3.3：训练可使用经认证的历史重建数据）。

```bash
python scripts/rebuild_tail_labels.py \\
    --db artifacts/research/tail_minute_bars.duckdb \\
    --requests artifacts/research/tail_requests.jsonl \\
    --labels artifacts/research/tail_replayed_labels.jsonl \\
    --report artifacts/research/tail_rebuild_report.json
```

``--requests`` 每行一条 JSON：

```json
{"symbol": "600000.SH", "decision_date": "2026-03-06", "entry_date": "2026-03-09",
 "overnight_features": {"ret_5": 0.031}, "model_probabilities": {"p_net_profit_5d_tail": 0.63}}
```

确认谓词用的是契约里的 ``hard_gate_confirmation`` —— 与线上影子链路同一个函数对象；
成交与出场也仍由 ``build_tail_net_profit_label`` 一个人算。这个脚本不重算任何判定，
只负责把参考数据是否够、以及判定结果如实落到文件上。

费用与滑点取自 ``config/default.yaml`` 的按日期冻结成本表。零费用标签看着更像"盈利"，
所以默认必须读到成本表才开工；确实只想看流程通不通时显式传 ``--zero-cost``，
它会把这一事实写进报告（``cost_model = zero_cost_debug``），不会伪装成可用样本。
产出的样本一律标注 ``replayed_recompute``，与真实观察样本分开。

退出码是**真实退出码**：

- ``0`` 所有请求都判得动（可以有未成交与不确定，那是策略结果）
- ``3`` 有请求因参考数据不足被判 ``insufficient_reference_data`` → 尾盘验证记为阻塞，
  继续采集；不足的单列，不折算成未成交或亏损
- ``5`` 输入不可用或口径不成立（请求文件坏、日期不合法、成本表读不出）
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

from stock_analyzer.backtest.matcher import ExecutionMatcher  # noqa: E402
from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.contracts.trend_strategy import hard_gate_confirmation  # noqa: E402
from stock_analyzer.labels.tail_net_profit import resolve_tail_slippage_ratio  # noqa: E402
from stock_analyzer.research.minute_bar_store import MinuteBarStore  # noqa: E402
from stock_analyzer.research.tail_rebuild import (  # noqa: E402
    RebuildRequest,
    TailRebuildError,
    rebuild_tail_labels,
    summarize_rebuild,
)
from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    REFERENCE_DB_DEFAULT,
    TailReferenceError,
    TailReferenceStore,
)

RC_OK = 0
RC_INSUFFICIENT = 3
RC_ERROR = 5

_DEFAULT_CONFIG = _PROJECT_ROOT / "config" / "default.yaml"


def _read_requests(path: Path) -> list[RebuildRequest]:
    requests: list[RebuildRequest] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {lineno} 行不是合法 JSON: {exc}") from exc
        try:
            requests.append(RebuildRequest(
                symbol=str(payload["symbol"]),
                decision_date=date.fromisoformat(str(payload["decision_date"])[:10]),
                entry_date=date.fromisoformat(str(payload["entry_date"])[:10]),
                overnight_features=dict(payload.get("overnight_features") or {}),
                model_probabilities={
                    str(key): float(value)
                    for key, value in (payload.get("model_probabilities") or {}).items()
                },
            ))
        except (KeyError, TypeError, ValueError, TailRebuildError) as exc:
            raise ValueError(f"第 {lineno} 行请求不合法: {type(exc).__name__}: {exc}") from exc
    if not requests:
        raise ValueError("--requests 文件里没有有效请求")
    return requests


def _cost_side(config_path: Path, entry_date: date) -> tuple[Any, float]:
    """返回 ``(cost_estimator, slippage_ratio)``，都取自冻结成本表。

    ``resolve_tail_slippage_ratio`` 的 ``matcher`` 参数要的是**配置**
    （``BacktestMatcherConfig``），不是 ``ExecutionMatcher`` 那层壳：传壳会静默取到
    0 滑点，让标签平白好看起来。
    """
    config = load_config(config_path)
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)

    def estimate_cost(side: str, price: float, quantity: int, when: Any) -> float:
        return float(matcher.estimate_cost(
            side, price=float(price), quantity=int(quantity), trade_date=when
        ))

    slippage = resolve_tail_slippage_ratio(
        matcher=config.backtest_matcher, trade_date=entry_date, limit_rule=config.limit_rule
    )
    return estimate_cost, slippage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=REFERENCE_DB_DEFAULT, help="研究库（分钟 + 参考数据）")
    parser.add_argument("--requests", required=True)
    parser.add_argument("--labels", default="", help="标签记录 JSONL 输出（仅判得动的样本）")
    parser.add_argument("--report", default="")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG))
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--model-version", default="")
    parser.add_argument("--market-state", default="")
    parser.add_argument("--zero-cost", action="store_true",
                        help="显式承认这是无费用调试标签，不得用于验收")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        requests = _read_requests(Path(args.requests))
    except (OSError, ValueError) as exc:
        print(f"请求不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    cost_estimator: Any = None
    slippage_ratio = 0.0
    if args.zero_cost:
        print("[warn] --zero-cost：产出的是无费用调试标签，不得用于任何验收", file=sys.stderr)
    else:
        try:
            cost_estimator, slippage_ratio = _cost_side(Path(args.config), requests[0].entry_date)
        except Exception as exc:  # noqa: BLE001 - 成本表读不出来就别产标签
            print(f"冻结成本表不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
            return RC_ERROR

    try:
        with (
            TailReferenceStore(args.db) as reference,
            MinuteBarStore(args.db) as minutes,
        ):
            outcomes = rebuild_tail_labels(
                reference=reference, minutes=minutes, requests=requests,
                confirmation=hard_gate_confirmation, interval=str(args.interval),
                cost_estimator=cost_estimator, slippage_ratio=slippage_ratio,
                model_version=args.model_version, market_state=args.market_state,
            )
    except (TailRebuildError, TailReferenceError, OSError) as exc:
        print(f"研究库不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    summary = summarize_rebuild(outcomes)
    summary["db"] = str(args.db)
    summary["confirmation_predicate"] = (
        f"{hard_gate_confirmation.__module__}.hard_gate_confirmation"
    )
    summary["cost_model"] = "zero_cost_debug" if args.zero_cost else str(args.config)
    summary["slippage_ratio"] = slippage_ratio

    records = [item.record.to_dict() for item in outcomes if item.record is not None]
    if args.labels:
        out = Path(args.labels)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if summary["rebuild_blocked_on_reference_data"]:
        print(f"参考数据不足，尾盘验证记为阻塞: {summary['missing_reference_inputs']}")
        return RC_INSUFFICIENT
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
