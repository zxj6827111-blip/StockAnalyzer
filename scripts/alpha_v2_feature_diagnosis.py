"""Alpha V2 M3：DF-M2-003 特征诊断跑批。

对一段 PIT 面板跑特征工程 → 逐列诊断统计 → 配上游源探针（market.duckdb）→
落盘 JSON + Markdown（``artifacts/alpha_v2/validation/feature_diagnosis/``）。

```bash
python scripts/alpha_v2_feature_diagnosis.py \
    --market-db artifacts/warehouse/market.duckdb \
    --window-start 2025-06-02 --window-end 2026-03-31 --max-symbols 400
```

纪律：本脚本只诊断、不改特征集。任何"把某列从 Base V2 删掉"的决定都必须
走"关闭 epoch → 新 epoch"的路径（M3 §16）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.artifacts import write_json_atomic  # noqa: E402
from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint  # noqa: E402
from stock_analyzer.alpha_v2.research.panel import load_daily_panel  # noqa: E402
from stock_analyzer.alpha_v2.validation.feature_diagnosis import (  # noqa: E402
    build_diagnosis_markdown,
    diagnose_features,
    probe_market_duckdb_sources,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：DF-M2-003 特征诊断")
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--warmup-days", type=int, default=200)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument(
        "--only-suspects-json",
        default="",
        help="可选：S14 常数疑似清单（只诊断这些列而不是全量）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "artifacts" / "alpha_v2" / "validation" / "feature_diagnosis"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    window_start = date.fromisoformat(args.window_start)
    window_end = date.fromisoformat(args.window_end)

    panel = load_daily_panel(
        market_db=REPO_ROOT / args.market_db,
        window_start=window_start,
        window_end=window_end,
        warmup_days=int(args.warmup_days),
        max_symbols=int(args.max_symbols),
    )
    print(f"[feature-diag] panel: {panel.window_start} → {panel.window_end}, "
          f"{len(panel.symbols)} symbols, {len(panel.bars)} bars", flush=True)

    decisions: list[DecisionPoint] = [
        DecisionPoint(symbol, day)
        for day in panel.calendar
        for symbol in panel.pit_universe(as_of=day).eligible_symbols
    ]
    # 诊断要看的是 FeatureEngineer 的**原始输出**（含未过 Base V2 门禁的列），
    # 不能走 build_shared_feature_matrix——它一进来就把 88 列拒了。
    frame = _raw_feature_frame(panel, decisions)
    print(f"[feature-diag] frame: {frame.shape}", flush=True)

    # 上游探针（market_duckdb.daily_bars 的列存在性与空值率）
    try:
        probe = probe_market_duckdb_sources(REPO_ROOT / args.market_db)
    except Exception as exc:  # noqa: BLE001 - 探针失败不阻塞统计诊断
        print(f"[feature-diag] 上游探针失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        probe = {}

    columns: list[str] | None = None
    if args.only_suspects_json:
        import json

        columns = [
            str(item)
            for item in json.loads(Path(args.only_suspects_json).read_text(encoding="utf-8"))
        ]

    report = diagnose_features(frame, columns=columns, upstream_probe=probe)
    payload = report.to_payload()
    payload["window"] = [str(window_start), str(window_end)]
    payload["symbols"] = [str(s) for s in panel.symbols[:50]]
    payload["note"] = (
        "DF-M2-003 诊断：UPSTREAM_NOT_POPULATED / FILL_ZERO_ARTIFACT / DATA_MISSINGNESS "
        "是治理输入；REAL_CONSTANT 需人工复核是否为真信号"
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"feature_diagnosis_{window_start}_{window_end}.json"
    md_path = out_dir / f"feature_diagnosis_{window_start}_{window_end}.md"
    write_json_atomic(json_path, payload)
    md_path.write_text(build_diagnosis_markdown(report), encoding="utf-8")
    print(f"[feature-diag] JSON: {json_path}")
    print(f"[feature-diag] MD:   {md_path}")
    print(f"[feature-diag] 分类计数: {payload['classification_counts']}")
    return 0


def _raw_feature_frame(panel, decisions: list[DecisionPoint]) -> pd.DataFrame:
    """FeatureEngineer 原始输出（诊断用，不受 Base V2 准入约束）。

    保留 NaN：列级 NaN/常量形态是 DF-M2-003 诊断本体。
    """
    from stock_analyzer.feature.engineer import FeatureEngineer

    grouped: dict[str, list[DecisionPoint]] = {}
    for item in decisions:
        grouped.setdefault(str(item.symbol), []).append(item)
    engineer = FeatureEngineer()
    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(sorted(grouped)):
        frame = panel.symbol_bars(symbol)
        if frame is None or frame.empty:
            continue
        wanted = {item.decision_date for item in grouped[symbol]}
        try:
            features = engineer.transform(frame)
        except Exception:  # noqa: BLE001 - 诊断模块不吞"整票失败"：记录列缺失而非消失
            continue
        for ts, values in features.iterrows():
            day = ts.date() if hasattr(ts, "date") else ts
            if day not in wanted:
                continue
            row: dict[str, object] = {"decision_date": day.isoformat(), "symbol": symbol}
            for key, value in values.items():
                # 保留 NaN/inf → 记 NaN（诊断要靠它做 missing_ratio / 常量检测）
                if isinstance(value, bool):
                    row[str(key)] = float(value)
                elif isinstance(value, (int, float)):
                    row[str(key)] = float(value)
                else:
                    try:
                        row[str(key)] = float(value)
                    except (TypeError, ValueError):
                        row[str(key)] = float("nan")
            rows.append(row)
        if (index + 1) % 100 == 0:
            print(f"[feature-diag] feat {index + 1}/{len(grouped)}", flush=True)
    empty = pd.DataFrame(columns=["decision_date", "symbol"])
    return pd.DataFrame(rows) if rows else empty


if __name__ == "__main__":
    raise SystemExit(main())
