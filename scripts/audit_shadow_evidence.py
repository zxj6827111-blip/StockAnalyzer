"""影子验证门槛（发布清单 R12）到底走到哪一步了——只读留档，不改任何东西。

```bash
python scripts/audit_shadow_evidence.py --trace-dir artifacts/runtime/trend_tail_shadow
```

``shadow_readiness(≥60 完整交易日, ≥100 笔成熟模拟成交)`` 这两个输入此前没有生产者，
本命令就是那个生产者：它数 ``funnel_trace_*.json``，把不可信 / 时间不可证 / 被阻断的
留档**点名排除**后再算，并如实返回还差多少。

退出码是真实退出码：

- ``0`` 证据已达标，可以进入人工发布评审（**不等于**已发布）
- ``3`` 证据不足，继续保持影子状态（这是当前的真实状态，不是错误）
- ``5`` 目录不存在、一条留档都没有，或有文件根本读不出来
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.shadow_evidence import (  # noqa: E402
    CAPTURE_MODE_OBSERVED,
    summarize_shadow_evidence,
    trace_paths,
)

RC_OK = 0
RC_INSUFFICIENT = 3
RC_ERROR = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True,
                        help="尾盘漏斗留档目录（funnel_trace_*.json）")
    parser.add_argument("--capture-mode", default=CAPTURE_MODE_OBSERVED,
                        choices=[CAPTURE_MODE_OBSERVED, "replayed_recompute"],
                        help="observed 与 replayed 不得合并计数")
    parser.add_argument("--include-night-half", action="store_true",
                        help="把夜扫半段的 _night 留档也算进文件集合（默认不算观察日）")
    parser.add_argument("--out", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    directory = Path(args.trace_dir)
    if not directory.is_dir():
        print(f"留档目录不存在: {directory}", file=sys.stderr)
        return RC_ERROR
    paths = trace_paths(directory, include_night_half=args.include_night_half)
    if not paths:
        print(f"没有任何尾盘漏斗留档：{directory}", file=sys.stderr)
        return RC_ERROR

    try:
        summary = summarize_shadow_evidence(paths, capture_mode=args.capture_mode)
    except (OSError, ValueError) as exc:
        print(f"留档读不出来: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    readiness = summary["readiness"]
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
    if not args.quiet:
        readiness = summary["readiness"]
        print(f"[{summary['capture_mode']}] 完整观察日 {summary['observed_trade_days']}、"
              f"成熟模拟成交 {summary['matured_simulated_fills']}"
              f"（另有 {summary['pending_fills']} 笔未成熟、"
              f"{summary['recommendation_coverage']:.0%} 的天有推荐）")
        print(f"计入 {summary['trace_files_counted']}/{summary['trace_files_seen']} 份留档；"
              f"排除不可信 {len(summary['excluded_untrusted'])} 份、"
              f"时间不可证 {len(summary['excluded_time_ineligible'])} 份、"
              f"被阻断日 {len(summary['blocked_days'])} 天")
        for blocker in readiness["blockers"]:
            print(f"  仍不足以进入发布评审: {blocker}")
        if args.out:
            print(f"报告已写入 {args.out}")

    return RC_OK if readiness["ready_for_release_review"] else RC_INSUFFICIENT


if __name__ == "__main__":
    raise SystemExit(main())
