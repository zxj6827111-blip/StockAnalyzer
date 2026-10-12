"""CLI：把某日最终推荐的成熟退出落成 §2 漏斗最后一层"成交与退出"的留档（只读输入）。

入场那天写不出这一层——退出要等最多 5 个交易日才成熟，所以它必须是**另一份**留档
（``funnel_trace_<date>_execution_exit.json``），而不是回头覆盖入场当天的文件。

退出码：``0`` 写成功（未成熟也算写成功，原因留在层记录里）；``5`` 输入不可用、
日期自相矛盾或契约摘要与在服留档不一致（fail-closed，不产出"大概对"的证据）。
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

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.labels.tail_net_profit import TailLabelRecord  # noqa: E402
from stock_analyzer.research.funnel_trace import (  # noqa: E402
    build_funnel_trace,
    write_trace,
)
from stock_analyzer.research.tail_mature_feedback import (  # noqa: E402
    attach_exit_outcomes,
    execution_exit_stage,
)

RC_OK = 0
RC_ERROR = 5


def _load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("shadow report is not an object")
    if not payload.get("trade_date"):
        raise ValueError("shadow report has no trade_date")
    return payload


def _load_records(path: Path) -> list[TailLabelRecord]:
    records: list[TailLabelRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(TailLabelRecord.from_dict(json.loads(line)))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True,
                        help="tail_shadow_report_<date>.json（尾盘影子留档，只读）")
    parser.add_argument("--labels", required=True,
                        help="JSONL：每行一条 to_dict() 形态的尾盘标签记录")
    parser.add_argument("--out-dir", default="artifacts/runtime/trend_tail_shadow")
    parser.add_argument("--trade-date", default="", help="默认取留档自己的日期")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = _load_report(Path(args.report))
        records = _load_records(Path(args.labels))
        trade_date = (
            date.fromisoformat(args.trade_date[:10]) if args.trade_date else None
        )
        archived_digest = str(report.get("contract_digest") or "")
        if archived_digest and archived_digest != DEFAULT_TREND_CONTRACT.digest():
            raise ValueError(
                f"留档用的是契约 {archived_digest}，本 CLI 只能按在服的 "
                f"{DEFAULT_TREND_CONTRACT.digest()} 落证据；不然后退到猜"
            )
        exits = attach_exit_outcomes(
            shadow_report=report, records=records, trade_date=trade_date
        )
        stage = execution_exit_stage(exits=exits)
        trace = build_funnel_trace(
            trade_date=exits["trade_date"],
            stages=[stage],
            contract=DEFAULT_TREND_CONTRACT,
        )
        path = write_trace(trace, Path(args.out_dir), suffix="execution_exit")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"execution_exit 留档失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    if not args.quiet:
        print(f"trade_date = {exits['trade_date']}")
        print(f"recommended = {len(exits['recommended'])}, realized = {exits['realized']}")
        print(f"net_profit_rate = {exits['net_profit_rate']}")
        print(f"counts = {exits['counts']}")
        print(f"caveats = {exits['caveats']}")
        print(f"留档已写入 {path}")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
