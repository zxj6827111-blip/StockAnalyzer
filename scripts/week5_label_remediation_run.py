#!/usr/bin/env python
"""方向一' NAS 重训 runner：重建 PIT 数据集（return_rank label）+ 18-fold 验证。

在 scheduler-critical 容器内执行（docker exec -d 后台 + </dev/null）：
  python3 /app/scripts/week5_label_remediation_run.py --stage generate
  python3 /app/scripts/week5_label_remediation_run.py --stage walkforward

工程纪律（任务书）：内存（流式分片/store 视图/每 fold 新 trainer——
harness 已内置）；执行窗口（避开 21:30-23:00 cron）；输出全部落
phase2_label_remediation/ 新目录，不覆盖旧产物。
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path

PHASE2_ROOT = Path("/app/artifacts/phase2_label_remediation")
DATASET_DIR = PHASE2_ROOT / "pit_dataset_rank"
# 窗口与旧数据集完全一致（对照可比性：仅 label/特征链变化）。
WINDOW_START = date(2024, 9, 2)
WINDOW_END = date(2026, 8, 28)


def _rss_mib() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return -1.0
    return -1.0


def stage_generate() -> int:
    from stock_analyzer.backtest.pit_dataset import generate_pit_dataset

    started = time.time()
    meta = generate_pit_dataset(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        out_dir=str(DATASET_DIR),
        label_basis="return_rank",
        resume=True,
    )
    print(
        f"[generate] rows={meta.rows:,} symbols={meta.symbols} "
        f"dates={meta.trade_dates} positive_rate={meta.positive_rate} "
        f"note={meta.label_policy_note} in {time.time() - started:.0f}s "
        f"rss={_rss_mib():.0f}MiB",
        flush=True,
    )
    return 0


def stage_walkforward() -> int:
    from stock_analyzer.backtest.walk_forward_xsec import main as wf_main

    return wf_main()


def stage_coverage() -> int:
    """98 特征覆盖率报告：新数据集逐特征非 NaN 率（验收清单第 2 项）。"""

    import json

    import duckdb

    attribution_path = Path(
        "/app/artifacts/phase2/feature_attribution_20260907T170151.json"
    )
    nan_features: list[str] = []
    if attribution_path.exists():
        payload = json.loads(attribution_path.read_text(encoding="utf-8"))
        for row in payload.get("features", []):
            value = row.get("ic_mean")
            if value is None or (isinstance(value, float) and value != value):
                nan_features.append(str(row["feature"]))
    chunks = sorted(DATASET_DIR.glob("pit_*.parquet"))
    if not chunks:
        raise SystemExit("dataset not built yet")
    files = ", ".join(f"'{f}'" for f in chunks)
    con = duckdb.connect(database=":memory:")
    con.execute("SET memory_limit='1.5GB'")
    con.execute("SET threads=2")
    report: dict[str, object] = {
        "nan_feature_count_in_attribution": len(nan_features),
        "features": {},
    }
    total_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet([{files}])"
    ).fetchone()[0]
    # 逐特征单列扫（98 列 × 2.4M 行单列 ≈ 每列 ~1s，内存安全）。
    # 指标口径（诚实性关键）：FeatureEngineer 末端 fillna(0) 会把"源列
    # 全缺失"的特征变成常量 0 列——nonnull_rate 对此恒为 1.0，完全掩蔽
    # 缺失。真正要回答"98 特征是否有了真实值"必须看：
    #   nonzero_rate  —— 非零行占比（常量 0 列 ≈ 0）；
    #   n_distinct    —— 去重值个数（常量列 = 1）。
    # 分档语义：nonzero>1% 且 n_distinct>10 → 数据真实到位；
    #   n_distinct<=2 → 常量填充（无信息，仍视为缺失）；
    #   两档之间 → 部分覆盖（如 intraday 雪崩月、hk/inst 空库）。
    for name in nan_features:
        quoted = f'"{name}"'
        try:
            row = con.execute(
                f"SELECT COUNT({quoted}), "
                f"COUNT(*) FILTER (WHERE {quoted} IS NOT NULL AND {quoted} <> 0), "
                f"COUNT(DISTINCT {quoted}) "
                f"FROM read_parquet([{files}])"
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 - 列不存在也记录
            report["features"][name] = {"exists": False, "error": str(exc)}
            continue
        nonnull, nonzero, distinct = int(row[0]), int(row[1]), int(row[2])
        nonzero_rate = nonzero / max(1, int(total_rows))
        if distinct <= 2:
            tier = "constant_fill"
        elif nonzero_rate > 0.01 and distinct > 10:
            tier = "real_data"
        else:
            tier = "partial"
        report["features"][name] = {
            "exists": True,
            "nonnull_rate": round(nonnull / max(1, int(total_rows)), 6),
            "nonzero_rate": round(nonzero_rate, 6),
            "n_distinct": distinct,
            "tier": tier,
        }
    out = PHASE2_ROOT / "nan_feature_coverage.json"
    out.write_text(
        json.dumps(
            {"total_rows": int(total_rows), **report}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(f"[coverage] report={out}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=["generate", "walkforward", "coverage", "all"],
        default="all",
    )
    parser.add_argument("--max-symbols", type=int, default=0)
    args = parser.parse_args()

    PHASE2_ROOT.mkdir(parents=True, exist_ok=True)
    if args.stage in {"generate", "all"}:
        stage_generate()
    if args.stage in {"walkforward", "all"}:
        stage_walkforward()
    if args.stage in {"coverage", "all"}:
        stage_coverage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
