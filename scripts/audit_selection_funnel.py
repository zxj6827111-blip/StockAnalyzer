"""把夜扫半段与尾盘半段的留档对成整条选股漏斗，并说明 §2 的诊断问题哪些还答不了。

```bash
python scripts/audit_selection_funnel.py \
    --night-funnel artifacts/alpha_v2/funnel/production_funnel_20261009.json \
    --tail-dir artifacts/runtime/trend_tail_shadow --trade-date 2026-10-09
```

用途是改进计划 §2 的收口：留档已经分两半存在（Quality300/Light100/Deep50 的成员与计数
在 ``production_funnel`` 快照里，夜间观察池之后的原因/特征/身份/数据时间在
``funnel_trace`` 里），但没人把它们对成一个视图，于是"最终推荐变差来自候选池、预测、
排序还是交易规则"这个问题每次都得靠人脑拼。本命令只读不改，且**缺记录就写缺记录**，
不会把"没留档"折算成"这层没淘汰股票"。

退出码是**真实退出码**：

- ``0`` 九层全部有留档
- ``3`` 有层缺记录 → 对应诊断问题不可回答（这是 §2 根因清单的一部分，不是 bug 噪声）
- ``5`` 输入读不出来，或一条尾盘留档都没有
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

from stock_analyzer.research.funnel_trace import read_trace  # noqa: E402
from stock_analyzer.research.selection_funnel_view import (  # noqa: E402
    build_selection_funnel_view,
)

RC_OK = 0
RC_INCOMPLETE = 3
RC_ERROR = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--night-funnel", default="",
                        help="production_funnel 快照（Quality300/Light100/Deep50 成员）")
    parser.add_argument("--tail-dir", required=True,
                        help="尾盘漏斗留档目录（funnel_trace_*.json）")
    parser.add_argument("--trade-date", default="", help="只看某个交易日（ISO）")
    parser.add_argument("--out", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    tail_dir = Path(args.tail_dir)
    if not tail_dir.is_dir():
        print(f"尾盘留档目录不存在: {tail_dir}", file=sys.stderr)
        return RC_ERROR
    paths = sorted(tail_dir.glob("funnel_trace_*.json"))
    if args.trade_date:
        raw = args.trade_date.strip()
        # 留档文件名是 ISO 带横线日期（funnel_trace_2026-10-09_night.json）；只按传进来的
        # 字面量匹配，"20261009" 会把每一天的留档都筛没，然后报"一条留档都没有"。
        iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 and raw.isdigit() else raw[:10]
        paths = [path for path in paths if iso in path.name]
    if not paths:
        print(f"没有任何尾盘漏斗留档：{tail_dir}", file=sys.stderr)
        return RC_ERROR

    try:
        traces = [read_trace(path) for path in paths]
        night = (json.loads(Path(args.night_funnel).read_text(encoding="utf-8"))
                 if args.night_funnel else None)
    except (OSError, ValueError) as exc:
        print(f"留档读不出来: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR
    if night is not None and not isinstance(night, dict):
        print("夜扫快照不是 JSON 对象", file=sys.stderr)
        return RC_ERROR

    view = build_selection_funnel_view(night_funnel=night, tail_traces=traces)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(view, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
    if not args.quiet:
        coverage = view["coverage"]
        print(f"留档覆盖 {coverage['layers_recorded']}/{coverage['layers_total']} 层；"
              f"来自漏斗留档：{coverage['layers_from_trace'] or '无'}；"
              f"缺记录：{coverage['layers_unrecorded'] or '无'}；"
              f"有成员但无拒绝原因：{coverage['layers_without_reasons'] or '无'}")
        for key, item in view["diagnosis"].items():
            mark = "可回答" if item["answerable_with_current_records"] else "答不了"
            print(f"  [{mark}] {item['question']} ({key})")
        if args.out:
            print(f"报告已写入 {args.out}")

    return RC_OK if not view["coverage"]["layers_unrecorded"] else RC_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
