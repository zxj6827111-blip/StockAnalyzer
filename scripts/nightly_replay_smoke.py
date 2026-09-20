#!/usr/bin/env python
"""晚间选股交付链路的离线回放与通道冒烟（方案 §5.4 第 1、2 步）。

用途：
1. **离线回放**（默认）：用已完成的夜扫产物生成正式报告并落盘，**不发送**，
   检查日期、候选口径与正文渲染是否符合预期；
2. **通道冒烟**（--send）：把同一份回放报告通过**真实交付链路**发给现有目标，
   核对飞书 API 回执与用户实际收件；
3. 查看/补发（--status / --retry）。

回放报告与正式报告共用构造与渲染，但报告类型是 ``replay``、report_id 前缀
``rp-``、正文首行明确标注"历史数据，不是当日结果"，并且**不占用正式报告的
指针**——做过一次回放不会顶掉当天真正的结果。

用法（在容器内执行）::

    python scripts/nightly_replay_smoke.py --from-job-result <path>          # 离线回放
    python scripts/nightly_replay_smoke.py --from-job-result <path> --send   # 通道冒烟
    python scripts/nightly_replay_smoke.py --status rp-20260916
    python scripts/nightly_replay_smoke.py --retry rp-20260916 [--confirm-unknown]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from stock_analyzer.config import get_config
from stock_analyzer.runtime.service import StockAnalyzerService

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _load_night_scan_from_job_result(path: Path) -> dict[str, object]:
    """从调度器 job 结果里取出夜扫的 ``report``（含 night_pool/night_pool_top5）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            inner = item.get("payload")
            if isinstance(inner, dict) and isinstance(inner.get("report"), dict):
                report = dict(inner["report"])
                report.setdefault("trace_id", str(item.get("trace_id", "")))
                return report
    if isinstance(payload.get("report"), dict):
        return dict(payload["report"])
    raise SystemExit("job 结果里没有找到夜扫 report")


def _trade_date_of(night_scan: dict[str, object]) -> str:
    timestamp = str(night_scan.get("timestamp", "")).strip()
    if len(timestamp) >= 10:
        return timestamp[:10]
    state_date = str(night_scan.get("trade_date", "")).strip()
    return state_date[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="晚间选股交付链路离线回放/冒烟")
    parser.add_argument("--from-job-result", default="", help="夜扫 job 结果 JSON 路径")
    parser.add_argument("--send", action="store_true", help="真实发送（通道冒烟）")
    parser.add_argument("--status", default="", help="查询指定 report_id 的交付状态")
    parser.add_argument("--retry", default="", help="补发指定 report_id 的未成功目标")
    parser.add_argument(
        "--confirm-unknown",
        action="store_true",
        help="补发时确认可能重复（用于上一次结果不确定且已超出 uuid 去重窗）",
    )
    parser.add_argument("--source-label", default="", help="回放来源标注（正文会引用）")
    args = parser.parse_args()

    service = StockAnalyzerService(config=get_config())

    # 显式声明"这次会写到哪里"。2026-09-17 出现过一次带外写入：有人在容器里跑本脚本
    # 但以为在临时目录（实际写进了生产 artifacts），事后只能靠比对 report_id 形式才
    # 推断出来源。无条件打印目标路径，至少让这类误操作在输出里可见。
    print(f"[nightly-replay] reports_root  = {service._nightly_report_service.root}")
    print(f"[nightly-replay] delivery_root = {service._nightly_delivery_service.root}")
    print(
        "[nightly-replay] 若不是有意写这里，请加 "
        "-e SA__NIGHTLY__REPORTS_ROOT=<tmp> -e SA__NIGHTLY__DELIVERY_ROOT=<tmp>"
    )

    if args.status:
        print(
            json.dumps(
                service._nightly_delivery_service.status(args.status),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.retry:
        result = service._nightly_delivery_service.request_retry(
            args.retry, confirm_unknown=args.confirm_unknown
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("queued"):
            summary = service._nightly_delivery_service.deliver_now(
                service._nightly_report_service.load_report(args.retry) or {},
                max_targets=2,
            )
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0

    if not args.from_job_result:
        raise SystemExit("需要 --from-job-result（或 --status/--retry）")

    source = Path(args.from_job_result)
    night_scan = _load_night_scan_from_job_result(source)
    trade_date = _trade_date_of(night_scan)
    if not trade_date:
        raise SystemExit("无法从夜扫产物确定交易日")

    report_service = service._nightly_report_service
    report = report_service.build_replay_report(
        night_scan=night_scan,
        trade_date=trade_date,
        generated_at=datetime.now(),
        source_label=args.source_label or f"{trade_date} 的历史夜扫产物（{source.name}）",
    )
    published = report_service.publish(report)
    frozen = published["report"]
    rendered = report_service.render(frozen)

    print("=" * 72)
    print(f"report_id   : {frozen['report_id']}（kind={frozen['report_kind']}）")
    print(f"trade_date  : {frozen['trade_date']}")
    print(f"scan_status : {frozen['scan_status']}")
    print(f"published   : {published['published']}（{published.get('reason', '')}）")
    print(f"报告文件    : {report_service.report_path(frozen['trade_date'], frozen['report_id'])}")
    observation_count = len(frozen["observation_candidates"])
    incomplete_count = len(frozen["incomplete_candidates"])
    print(f"候选        : 观察 {observation_count} / 待补全 {incomplete_count}")
    print(f"漏斗        : {frozen['funnel_counts']}")
    print(f"正文长度    : {len(rendered.content)}（截断={rendered.truncated}）")
    print("-" * 72)
    print(f"[标题] {rendered.title}")
    print(rendered.content)
    print("=" * 72)

    if not args.send:
        print("\n未发送（离线回放）。加 --send 走真实交付链路做通道冒烟。")
        return 0

    if not bool(getattr(service._config.nightly, "enabled", False)):
        print("\n注意：nightly.enabled=false，自动链未开启；本次为手动冒烟投递。")
    summary = service._nightly_delivery_service.deliver_now(frozen, max_targets=2)
    print("\n[投递结果]")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print("\n[交付记录]")
    print(
        json.dumps(
            service._nightly_delivery_service.status(frozen["report_id"]),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
