#!/usr/bin/env python3
"""把**生产自己打分过的票**放到尾盘规则下回测（改进计划 §4 的第三个对照臂）。

为什么需要它：§4 要求"与旧完整链路对照"，但此前只能声明"旧链路在尾盘时刻没有成交样本"。
生产信号库（`learning_protocol.duckdb` 的 `signal_snapshots × outcome_records`）里存着
每个决策日系统真实看过、打过分的那批票与它的 `p_meta` —— 只要那天有分钟 bar，
就能用**同一套尾盘规则**（14:30–14:50 确认、+8%/−5%、5 日持有、扣费用与滑点）
把它的净盈利率量出来，而不是继续用"没有样本"搪塞。

口径纪律（不遵守就不要引用这个数）：

* `feature_capture_mode` **分开报**：`observed_snapshot` 是系统当时真实打的分数，
  `replayed_recompute` 是事后重算的 —— 同一个胜率统计里混这两者会把数字抬高一个量级
  （NOTE 里已经记过一次这个坑）。
* `strategy` 也分开：本轮改进计划只管 trend，monster 有自己的策略。
* 分钟数据只到 2026-07-17 ⇒ 决策日窗口必须在那之前；那之后的生产推荐**没法**按尾盘规则回测，
  不得拿日线或开盘价顶替。
* 与"同一合格池"比，而不是与"全市场"比：池内基线来自全市场重放的成熟标签
  （2026 段 125 决策日净盈利率 0.3971）。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _open_csv(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def load_sessions(minute_db: str, years: tuple[int, ...]) -> list[date]:
    """从研究库取开市日；不用行情自己证明日历（与契约同一口径）。"""
    import duckdb

    con = duckdb.connect(minute_db, read_only=True)
    try:
        rows = con.sql(
            "select trade_date from ref_trade_calendar where is_open order by trade_date"
        ).fetchall()
    finally:
        con.close()
    sessions = [item if isinstance(item, date) else date.fromisoformat(str(item))
                for (item,) in rows]
    return [day for day in sessions if day.year in years]


def build_requests(
    csv_path: Path,
    sessions: list[date],
    *,
    strategies: tuple[str, ...],
    max_decision_date: date,
    capture_modes: tuple[str, ...] = (),
) -> tuple[list[dict], dict]:
    """每行 (symbol, decision_date) 去重；entry_date = 决策日之后的第一个开市日。"""
    next_session: dict[date, date] = {}
    for index, day in enumerate(sessions[:-1]):
        next_session[day] = sessions[index + 1]

    seen: set[tuple[str, str, str, str]] = set()
    requests: list[dict] = []
    skipped = {
        "no_next_session": 0, "after_cutoff": 0, "duplicate": 0, "other_strategy": 0,
        "other_capture_mode": 0,
    }
    with _open_csv(csv_path) as handle:
        for row in csv.DictReader(handle):
            if str(row.get("strategy") or "") not in strategies:
                skipped["other_strategy"] += 1
                continue
            # 口径必须**一次只跑一种**：标签行本身不带 capture_mode，
            # 同一 (symbol, 决策日) 在两种口径下各有一条请求时无法回填区分，
            # 混在一次运行里只能得到一个不可信的合并数（2026-10-10 就是这样错过一次）。
            if capture_modes and str(row.get("capture_mode") or "") not in capture_modes:
                skipped["other_capture_mode"] += 1
                continue
            day_text = str(row.get("decision_date") or "")[:10]
            try:
                decision = date.fromisoformat(day_text)
            except ValueError:
                continue
            if decision > max_decision_date:
                skipped["after_cutoff"] += 1
                continue
            entry = next_session.get(decision)
            if entry is None:
                skipped["no_next_session"] += 1
                continue
            symbol = str(row.get("symbol") or "").strip()
            key = (symbol, day_text, str(row.get("strategy")),
                   str(row.get("capture_mode")))
            if key in seen:
                skipped["duplicate"] += 1
                continue
            seen.add(key)
            requests.append({
                "symbol": symbol,
                "decision_date": day_text,
                "entry_date": entry.isoformat(),
                "capture_mode": str(row.get("capture_mode") or ""),
                "strategy": str(row.get("strategy") or ""),
                "p_meta": row.get("p_meta"),
            })
    stats = {"requests": len(requests), "skipped": skipped,
             "sessions_used": len(sessions)}
    return requests, stats


def group_labels(labels_path: Path, requests: list[dict]) -> dict:
    """把成熟标签按 (strategy, capture_mode) 聚合，并保留 p_meta 以便分层看区分度。"""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for line in labels_path.open("r", encoding="utf-8"):
        if not line.strip():
            continue
        item = json.loads(line)
        strategy = str(item.get("strategy") or "")
        mode = str(item.get("capture_mode") or "")
        if strategy not in {"trend", "monster"}:
            continue
        by_key[f"{strategy}|{mode}"].append(item)

    out: dict[str, dict] = {}
    for key, rows in sorted(by_key.items()):
        labelled = [row for row in rows if row.get("label") in (0, 1, 0.0, 1.0)]
        filled = [row for row in labelled if row.get("filled")]
        net = [row for row in filled if row.get("label") in (1, 1.0)]
        days = sorted({str(row.get("decision_date"))[:10] for row in rows})
        out[key] = {
            "requests": len(rows),
            "labelled": len(labelled),
            "decision_days": len(days),
            "first_day": days[0] if days else "",
            "last_day": days[-1] if days else "",
            "filled": len(filled),
            "fill_rate": round(len(filled) / len(labelled), 4) if labelled else None,
            "net_profits": len(net),
            "net_profit_rate": round(len(net) / len(filled), 4) if filled else None,
            "mean_net_return": (
                round(sum(float(row.get("net_return") or 0.0) for row in filled) / len(filled), 6)
                if filled else None
            ),
        }
    return out


def quintiles_by_score(labels_path: Path) -> dict:
    """只在同一段里按 p_meta 分五档：分档之间净盈利率有梯度才说明这个分数有用。"""
    buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
    rows: list[dict] = []
    for line in labels_path.open("r", encoding="utf-8"):
        if not line.strip():
            continue
        item = json.loads(line)
        if str(item.get("strategy")) != "trend":
            continue
        if item.get("label") not in (0, 1, 0.0, 1.0) or not item.get("filled"):
            continue
        score = item.get("p_meta")
        if score in (None, "", "nan"):
            continue
        try:
            item["_score"] = float(score)
        except (TypeError, ValueError):
            continue
        rows.append(item)
    if len(rows) < 50:
        return {"trend_filled_with_score": len(rows), "note": "样本不足以分五档"}
    rows.sort(key=lambda row: row["_score"])
    size = max(1, len(rows) // 5)
    for index in range(5):
        part = rows[index * size:(index + 1) * size] if index < 4 else rows[4 * size:]
        for row in part:
            buckets[(row["capture_mode"], index)].append(row)
    table = {}
    for (mode, index), part in sorted(buckets.items()):
        won = sum(1 for row in part if row.get("label") in (1, 1.0))
        table[f"{mode}|Q{index + 1}"] = {
            "fills": len(part),
            "net_profits": won,
            "net_profit_rate": round(won / len(part), 4) if part else None,
            "score_low": round(min(row["_score"] for row in part), 6),
            "score_high": round(max(row["_score"] for row in part), 6),
        }
    return {"trend_filled_with_score": len(rows), "quintiles": table}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", required=True, help="生产快照导出（csv 或 csv.gz）")
    parser.add_argument("--minute-db", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--max-decision-date", default="2026-07-10")
    parser.add_argument("--strategies", default="trend,monster")
    parser.add_argument("--capture-modes", default="",
                        help="逗号分隔；一次只跑一种口径（标签行不带口径，混跑无法分段）")
    parser.add_argument("--label-script", default="scripts/rebuild_tail_labels.py")
    parser.add_argument(
        "--reuse-labels", action="store_true",
        help="标签文件已存在时不重跑（聚合口径变了但标签没变时用，避免再花一小时）",
    )
    args = parser.parse_args(argv)

    strategies = tuple(item.strip() for item in args.strategies.split(",") if item.strip())
    cutoff = date.fromisoformat(args.max_decision_date)
    sessions = load_sessions(args.minute_db, years=(2025, 2026))
    modes = tuple(item.strip() for item in args.capture_modes.split(",") if item.strip())
    requests, stats = build_requests(
        Path(args.snapshots), sessions, strategies=strategies, max_decision_date=cutoff,
        capture_modes=modes,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    requests_path = out_dir / "prod_pick_requests.jsonl"
    with requests_path.open("w", encoding="utf-8") as handle:
        for item in requests:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(json.dumps({"requests_built": stats}, ensure_ascii=False), flush=True)

    labels_path = out_dir / "prod_pick_labels.jsonl"
    report_path = out_dir / "prod_pick_labels.json"
    label_rc: int | None = None
    if labels_path.exists() and report_path.exists() and args.reuse_labels:
        print(f"复用已有标签：{labels_path}", flush=True)
    else:
        command = [
            sys.executable, args.label_script,
            "--db", args.minute_db,
            "--requests", str(requests_path),
            "--labels", str(labels_path),
            "--report", str(report_path),
        ]
        print("running: " + " ".join(command), flush=True)
        label_rc = subprocess.run(command, check=False).returncode
    if not labels_path.exists():
        print(f"标签重建未产出（rc={label_rc}）", file=sys.stderr)
        return 3
    # rc=3 是 §3.1 的"参考数据不足 ⇒ 记为阻塞"，不是崩溃：它已经产出的成熟标签仍然可以
    # 用来量"真实推荐过的票最后怎么样了"，但**必须把阻塞计数一起写进工件**，
    # 否则这个数会被读成"全样本胜率"。这里的统计不构成 §4 的任何验收结论。
    label_report: dict = {}
    if report_path.exists():
        label_report = json.loads(report_path.read_text(encoding="utf-8"))
    blocked = bool(label_report.get("rebuild_blocked_on_reference_data")) or label_rc == 3

    # 把请求里的 strategy/capture_mode/p_meta 回填到标签行，才能分段统计。
    # 注意方向：**以请求为准并覆盖标签行**。标签器会给自己写的每一行填一个整轮恒定的
    # capture_mode（本轮实测为 replayed_recompute），用 setdefault 让它保留下来，
    # observed / replayed 两种口径就会被并成一个数——那正是这库上"混算抬高胜率"的老坑。
    index = {}
    for item in requests:
        index[(item["symbol"], item["decision_date"])] = item
    merged = out_dir / "prod_pick_labels_enriched.jsonl"
    unmatched = 0
    with merged.open("w", encoding="utf-8") as handle:
        for line in labels_path.open("r", encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            origin = index.get((str(row.get("symbol")), str(row.get("decision_date"))[:10]))
            if origin is None:
                unmatched += 1
            else:
                for key in ("strategy", "capture_mode", "p_meta"):
                    row[key] = origin.get(key)
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if unmatched:
        print(f"警告：{unmatched} 条标签行没匹配回请求，按未知口径处理（不并进任何分段）",
              file=sys.stderr, flush=True)

    summary = {
        "built": stats,
        "label_rebuild_rc": label_rc,
        "blocked_on_reference_data": blocked,
        "label_report": {
            key: label_report.get(key)
            for key in ("requests", "insufficient", "label_records", "filled",
                        "fill_rate", "net_profits", "missing_reference_inputs",
                        "status_reason_counts")
            if key in label_report
        },
        "caveats": [
            "本统计量的是**系统真实关注过的票**在尾盘规则下的成熟结果，不是 §4 的验收读数；"
            "§4 要求的是同池可比重建 + 滚动四折，那部分仍未通过（2025 段 −5.44pp）。",
            "observed_snapshot 与 replayed_recompute 分开报，不合并成一个胜率。",
            "决策日窗口止于分钟数据末端（2026-07-17）之前的可成熟段；"
            "此后的真实推荐没有分钟 bar，不能按尾盘规则回测。",
        ] + ([
            f"标签器按 §3.1 记为阻塞（rc=3，参考数据不足 {label_report.get('insufficient')} 条）；"
            "本文件统计的是它已产出的成熟标签，不得读成全样本胜率。"
        ] if blocked else []),
        "segments": group_labels(merged, requests),
        "score_quintiles": quintiles_by_score(merged),
        "artifacts": {
            "requests": str(requests_path),
            "labels": str(labels_path),
            "enriched": str(merged),
        },
    }
    (out_dir / "prod_pick_backtest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
