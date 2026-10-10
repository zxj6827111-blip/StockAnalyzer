"""CLI：把夜扫特征快照与重建标签拼成验证器要的一条样本（改进计划 §4）。

``replay_tail_candidate_pool.py`` 出请求、``rebuild_tail_labels.py`` 出标签，
``validate_tail_selection_quality.py`` 要的是**同一行里既有特征又有标签**的样本 ——
此前仓库里同样没有这个 joiner，所以 §4 拿不到输入。这里只做拼接与如实标注，
不重算任何判定（判定只有 ``build_tail_net_profit_label`` 一个出处）。

```bash
python scripts/assemble_tail_samples.py \\
    --requests artifacts/research/tail_requests.jsonl \\
    --labels artifacts/research/tail_replayed_labels.jsonl \\
    --out artifacts/research/tail_samples.jsonl \\
    --baseline-field avg_turnover_20 \\
    --report artifacts/research/tail_samples_report.json
```

两条不许含糊的口径：

* 标签缺 ``trainable`` 的行照样输出但标 ``trainable=false`` —— 成交率与命中率分开报，
  未成交不是 0 收益（§3.3）。
* ``--baseline-field`` 是**被明说命名的**对照分数。历史归档里没有当时的 composite_score，
  所以这里给的通常是可复现的容量/风格分，报告里写清它**不是**旧综合分。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_RC_ERROR = 5
_RC_OK = 0

_LABEL_FIELDS = (
    "status", "reason", "confirmed", "filled", "trainable", "label", "net_return",
    "gross_return", "fill_time", "entry_price", "quantity", "holding_days",
    "deferred_sessions", "gap_exit", "take_profit_hit", "stop_loss_hit",
    "ambiguous_same_bar", "corporate_action_uncertain", "market_state",
    "contract_version", "contract_digest", "cost_model_version", "price_basis",
    "label_policy_basis", "label_anchor_time", "label_mature_time",
    "p_net_profit_5d_tail_label",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path} 第 {line_number} 行不是合法 JSON: {exc}") from exc
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", default="")
    parser.add_argument("--baseline-field", default="avg_turnover_20")
    parser.add_argument("--capture-mode", default="replayed_recompute")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        requests = _read_jsonl(Path(args.requests))
        labels = _read_jsonl(Path(args.labels))
    except (OSError, SystemExit) as exc:
        print(f"assemble blocked: {exc}", file=sys.stderr)
        return _RC_ERROR

    by_key = {
        (str(row.get("symbol")), str(row.get("decision_date"))): row for row in requests
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped_no_request = 0
    status_counts: dict[str, int] = {}
    with out_path.open("w", encoding="utf-8") as handle:
        for label_row in labels:
            key = (str(label_row.get("symbol")), str(label_row.get("decision_date")))
            request = by_key.get(key)
            if request is None:
                skipped_no_request += 1
                continue
            features = dict(request.get("overnight_features") or {})
            sample = {
                "symbol": key[0],
                "decision_date": key[1],
                "entry_date": label_row.get("entry_date"),
                "capture_mode": str(label_row.get("capture_mode") or args.capture_mode),
                "probability_field": "p_net_profit_5d_tail",
                **features,
            }
            for field in _LABEL_FIELDS:
                if field in label_row:
                    sample[field] = label_row[field]
            baseline = features.get(str(args.baseline_field))
            sample["baseline_field"] = str(args.baseline_field)
            sample["baseline_score"] = baseline
            # 验证器按 baseline_rank_field 取旧排序；名字照传进来的写，不做暗示。
            sample[str(args.baseline_field)] = baseline
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
            written += 1
            status = str(label_row.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

    report = {
        "ok": written > 0,
        "out": str(out_path),
        "requests": len(requests),
        "labels": len(labels),
        "samples": written,
        "labels_without_matching_request": skipped_no_request,
        "status_counts": dict(sorted(status_counts.items())),
        "baseline_field": str(args.baseline_field),
        "baseline_caveat": (
            "历史归档里没有当时的 composite_score/等级，--baseline-field 给的是"
            "可复现的容量或风格分，不能当成旧综合分的替身。"
        ),
        "trainable_samples": sum(
            1 for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("trainable")
        ),
    }
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if written == 0:
        print("没有拼出任何样本：requests 与 labels 的 (symbol, decision_date) 对不上",
              file=sys.stderr)
        return _RC_ERROR
    return _RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
