"""Alpha V2 M3：训练并冻结 Shadow 模型工件。

```bash
python scripts/alpha_v2_shadow_model_freeze.py \
    --market-db artifacts/warehouse/market.duckdb \
    --window-start 2025-06-02 --window-end 2026-08-31 \
    --warmup-days 200 --max-symbols 400 \
    --model-id alpha_v2_shadow_epoch_001 \
    --out artifacts/alpha_v2/validation
```

这正是 M3「冻结」语义里唯一允许写"模型"的入口：**一次**训练、落盘、锚定哈希，
之后每天的 Shadow 预测都从磁盘加载这份工件，不再训练。

- 训练/校准窗口是日历上互斥的两段（重叠直接抛错）；
- 特征=Base V2 安全列（S14 断言）；标签=S11 可执行 outcome + S12 主基准超额；
- 决策边界为 PIT 合格股票池（``panel.pit_universe``）；
- 工件 = ``validation/model/<model_id>/``（boosters + calibrators + manifest）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.research.benchmarks import (  # noqa: E402
    BenchmarkSpec,
    build_benchmark_suite,
    compute_style_features,
    merge_primary_excess,
)
from stock_analyzer.alpha_v2.research.feature_audit import (  # noqa: E402
    safe_feature_columns,
)
from stock_analyzer.alpha_v2.research.multi_head import (  # noqa: E402
    HeadFitSpec,
    build_head_targets,
)
from stock_analyzer.alpha_v2.research.outcomes import (  # noqa: E402
    DecisionPoint,
    OutcomeSpec,
    build_label_v2,
)
from stock_analyzer.alpha_v2.research.panel import (  # noqa: E402
    load_daily_panel,
    panel_fingerprint,
)
from stock_analyzer.alpha_v2.validation.feature_frame import (  # noqa: E402
    daily_feature_frame,
)
from stock_analyzer.alpha_v2.validation.frozen_model import (  # noqa: E402
    fit_frozen_model,
    persist_frozen_model,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    config_hash_of,
    git_branch,
    git_head,
    price_contract_block,
)
from stock_analyzer.backtest.matcher import ExecutionMatcher  # noqa: E402
from stock_analyzer.config import load_config  # noqa: E402

# 校准窗长度（交易日）：须足够覆盖 isotonic 的 50 行下限，且不与训练窗重叠。
DEFAULT_CALIBRATION_DAYS = 60


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：冻结 Shadow 模型训练")
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
    parser.add_argument("--window-start", required=True, help="训练窗口起点（含 warmup 之前）")
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--calibration-days", type=int, default=DEFAULT_CALIBRATION_DAYS)
    parser.add_argument("--warmup-days", type=int, default=200)
    parser.add_argument("--max-symbols", type=int, default=0, help="调试限产；0=全窗口")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--out", default="artifacts/alpha_v2/validation")
    parser.add_argument("--config", default="config/default.yaml")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    window_start = date.fromisoformat(args.window_start)
    window_end = date.fromisoformat(args.window_end)
    config = load_config(Path(args.config))

    panel = load_daily_panel(
        market_db=REPO_ROOT / args.market_db,
        window_start=window_start,
        window_end=window_end,
        warmup_days=args.warmup_days,
        max_symbols=int(args.max_symbols),
    )
    certification = panel.certify_price_mode(min_sample=1000)
    if not certification.certified:
        print(
            f"[freeze-model] 警告：面板价格口径未认证（{certification.mode}）；"
            "训练可以继续，但冻结清单会记 execution_uncertain",
            flush=True,
        )
    price = price_contract_block(config)

    # PIT 决策集合（逐日合格池）
    decisions: list[DecisionPoint] = [
        DecisionPoint(symbol, day)
        for day in panel.calendar
        for symbol in panel.pit_universe(as_of=day).eligible_symbols
    ]
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)
    slippage = matcher.static_slippage_ratio("trend")

    run = build_label_v2(
        panel=panel,
        decisions=decisions,
        spec=OutcomeSpec(),
        matcher=matcher,
        slippage_ratio=slippage,
        price_mode=certification.mode,
        price_mode_certified=certification.certified,
        source_meta={"panel_fingerprint": panel_fingerprint(panel)},
    )
    styles = compute_style_features(panel=panel, decisions=decisions)
    enriched = run.frame.merge(styles, on=["decision_date", "symbol"], how="left")
    suite = build_benchmark_suite(enriched, spec=BenchmarkSpec(style_min_peers=5))
    primary = merge_primary_excess(enriched, suite)

    # 训练帧：特征只取 Base V2 安全列（S14）；目标由 S11/S12 的真实 outcome 派生。
    features_all = daily_feature_frame(panel, decisions)
    safe = [c for c in features_all.columns if c not in {"decision_date", "symbol"}]
    safe = list(safe_feature_columns(safe))
    features = features_all[["decision_date", "symbol", *safe]].copy()
    frame = features.merge(primary, on=["decision_date", "symbol"], how="inner")
    frame = build_head_targets(frame)
    # 目标/派生列全集（由 build_head_targets 产出 + S11 outcome + 基准残留）；
    # fit 侧只留 [id + safe features + 全部目标列 + 掩码]，style_* 等对照列不进训练
    from stock_analyzer.alpha_v2.validation.frozen_model import frozen_targets

    target_cols = [t.target for t in frozen_targets()]
    keep = [
        c
        for c in frame.columns
        if c in {"decision_date", "symbol"} or c in set(safe) or c in set(target_cols)
    ]
    frame = frame[keep]
    # 掩码：校准窗 = 日历尾部 N 个交易日，训练窗 = 其前
    calendar = sorted({str(day) for day in frame["decision_date"].astype(str).unique()})
    cutoff_index = max(0, len(calendar) - int(args.calibration_days))
    calibration_days = set(calendar[cutoff_index:])
    frame["is_calibration"] = frame["decision_date"].astype(str).isin(calibration_days)
    frame["is_train"] = ~frame["is_calibration"]

    model = fit_frozen_model(
        frame=frame,
        model_id=str(args.model_id),
        spec=HeadFitSpec(),
        provenance={
            "market_db": str(args.market_db),
            "window": [window_start.isoformat(), window_end.isoformat()],
            "panel_fingerprint": panel_fingerprint(panel),
            "decision_rows": int(len(frame)),
            "quality_pool_source": str(suite.report.get("quality_pool_source", "research_proxy")),
            "price_mode": certification.mode,
            "price_mode_certified": certification.certified,
            "slippage_ratio": float(slippage),
        },
        extra_identity={
            "code_commit": git_head(REPO_ROOT),
            "git_branch": git_branch(REPO_ROOT),
            "config_hash": config_hash_of(config),
            "price_contract": price,
        },
    )
    out_dir = persist_frozen_model(model, Path(args.out))
    print(f"[freeze-model] 工件已冻结: {out_dir}")
    print(f"[freeze-model] artifact_hash={model.manifest.get('artifact_hash', '')}")
    trained = {
        key: value
        for key, value in model.diagnostics["targets"].items()
        if isinstance(value, dict) and value.get("status") == "ok"
    }
    print(f"[freeze-model] 训练成功目标: {len(trained)}/{len(model.manifest.get('targets', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
