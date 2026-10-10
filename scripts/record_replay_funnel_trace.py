#!/usr/bin/env python3
"""把重放 sidecar 里的符号级事实落成漏斗**前两层**留档（改进计划 §2）。

生产夜扫的选择器只给"逐原因淘汰了多少只"的计数，而 ``StageTrace`` 不许用计数冒充成员，
所以全市场 / 硬性资格检查这两层此前没有任何历史留档。本脚本只读 sidecar、只写证据，
不参与任何选股判定；某日记不出事实就如实计数，不补一条看起来完整的留档。

退出码：0=每天都落出来了 / 3=有日子没落出来（原因见 not_emitted）/ 5=输入不可用。
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
from stock_analyzer.research.funnel_trace import (  # noqa: E402
    build_funnel_trace,
    verify_trace,
    write_trace,
)
from stock_analyzer.research.night_scan_funnel_trace import (  # noqa: E402
    build_universe_stage_traces,
)

RC_OK = 0
RC_PARTIAL = 3
RC_ERROR = 5
TRACE_SUFFIX = "__replay_universe"


def load_facts(path: Path) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                facts.append(json.loads(line))
    if not facts:
        raise ValueError(f"{path} 里没有符号级事实")
    return facts


def run(args: argparse.Namespace) -> dict[str, Any]:
    facts = load_facts(Path(args.universe_facts))
    contract = DEFAULT_TREND_CONTRACT
    out_dir = Path(args.out_dir)
    emitted: list[str] = []
    not_emitted: list[dict[str, str]] = []
    for fact in facts:
        day = str(fact.get("decision_date") or "")
        stages = build_universe_stage_traces(
            universe=fact,
            data_as_of=str(fact.get("as_of") or day),
            contract=contract,
        )
        if not stages:
            # 生产者拒收只有计数的输入；这里把它变成读得懂的事实而不是静默跳过。
            not_emitted.append({"decision_date": day, "reason": "no_symbol_level_facts"})
            continue
        trace = build_funnel_trace(
            trade_date=date.fromisoformat(day), stages=stages, contract=contract
        )
        problems = verify_trace(trace.as_dict(), contract=contract)
        if problems:
            not_emitted.append({
                "decision_date": day,
                "reason": "trace_verification_failed:" + ";".join(str(p) for p in problems)[:180],
            })
            continue
        emitted.append(str(write_trace(trace, out_dir, suffix=TRACE_SUFFIX, contract=contract)))
    return {
        "ok": not not_emitted,
        "days_in": len(facts),
        "emitted": len(emitted),
        "not_emitted": not_emitted,
        "layers": [item.stage for item in build_universe_stage_traces(
            universe=facts[0], data_as_of=str(facts[0].get("as_of") or ""),
            contract=contract,
        )] or [],
        "out_dir": str(out_dir),
        "trace_paths": emitted[:5],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-facts", required=True)
    parser.add_argument(
        "--out-dir", required=True, help="留档目录（与夜扫/尾盘留档同目录或独立目录）"
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = run(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"sidecar 不可用: {exc}", file=sys.stderr)
        return RC_ERROR
    except Exception as exc:  # noqa: BLE001 - 留档失败要变真实退出码，不是裸崩栈
        print(f"留档失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return RC_OK if report["ok"] else RC_PARTIAL


if __name__ == "__main__":
    raise SystemExit(main())
