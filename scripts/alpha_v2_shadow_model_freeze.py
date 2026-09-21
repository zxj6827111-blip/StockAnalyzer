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

R1.1（训练 provenance 封存）：provenance 记录**完整**训练输入身份——决策窗
``window``、``warmup_days``、由二者推出的 ``source_window``，以及覆盖该 source
窗口全部面板源列的 ``training_data_fingerprint``（含版本）。这些字段由
``frozen_model`` 的 v2 工件哈希保护，改动它们必然破坏工件完整性。
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
from stock_analyzer.alpha_v2.validation.freeze_precheck import (  # noqa: E402
    FreezeGateError,
    assert_runtime_identity,
)
from stock_analyzer.alpha_v2.validation.frozen_model import (  # noqa: E402
    fit_frozen_model,
    persist_frozen_model,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    config_hash_of,
    price_contract_block,
    resolve_runtime_code_identity,
)
from stock_analyzer.alpha_v2.validation.training_data_fingerprint import (  # noqa: E402
    TrainingDataFingerprintError,
    compute_training_data_fingerprint,
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

    # BLK-D2 同类修复：模型 provenance 的 code_commit 必须来自统一 resolver，
    # 与 validation freeze / capture / mature 是同一个值（容器里取构建身份）。
    # 放在面板加载**之前**：身份不可证就没必要跑几小时训练（fail-fast）。
    try:
        model_code_identity = resolve_runtime_code_identity(REPO_ROOT)
        model_code_commit = assert_runtime_identity(model_code_identity)
    except FreezeGateError as exc:
        print(f"[freeze-model] 运行身份硬门未通过: {exc}", file=sys.stderr)
        return 3

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

    # M4-L R1（BLOCKER 4）：训练数据**内容**指纹——只含窗口内行，确定性。
    # 与 panel_fingerprint（只描述形状）不同：任何窗口内价格/量/额被改写都会改变它，
    # 而窗口外新增交易日不会。preflight 会用同一函数重算并逐字对账。
    #
    # R1.1：指纹窗口 = **source_window**（决策窗起点向前 warmup_days 自然日），
    # 与上面 ``load_daily_panel(warmup_days=...)`` 实际读取的行范围一致；列清单
    # 也从 ``PANEL_BAR_COLUMNS`` 派生（面板读什么就 hash 什么）。绝不能出现
    # "panel warmup=200 / fingerprint warmup=0" 这种身份与输入脱节。
    try:
        data_fingerprint = compute_training_data_fingerprint(
            REPO_ROOT / args.market_db,
            training_start=window_start,
            training_end=window_end,
            warmup_days=int(args.warmup_days),
        )
    except TrainingDataFingerprintError as exc:
        print(f"[freeze-model] 训练数据指纹不可计算（拒绝冻结）: {exc}", file=sys.stderr)
        return 5
    source_window = [str(item) for item in data_fingerprint["source_window"]]
    # 证据（而非复述公式）：面板实际装载的最早 bar 不得早于指纹声明的 source_window
    # 起点。若早于，说明指纹窗口比训练输入窄——那正是 R1.1 要关掉的缺口。
    panel_earliest = panel.bars["trade_date"].min() if not panel.bars.empty else None
    if panel_earliest is not None:
        panel_earliest_date = panel_earliest.date()
        if panel_earliest_date < date.fromisoformat(source_window[0]):
            print(
                f"[freeze-model] 拒绝：面板装载的最早 bar {panel_earliest_date} 早于训练数据"
                f"指纹的 source_window 起点 {source_window[0]}——指纹窗口比训练输入窄",
                file=sys.stderr,
            )
            return 5
    print(
        f"[freeze-model] training_data_fingerprint="
        f"{str(data_fingerprint['fingerprint'])[:16]}… rows={data_fingerprint['rows']} "
        f"(version={data_fingerprint['fingerprint_version']}, "
        f"source_window={source_window[0]}..{source_window[1]}, "
        f"warmup_days={data_fingerprint['warmup_days']}, "
        f"columns={len(data_fingerprint['columns'])}, "
        f"missing_optional={data_fingerprint['missing_optional_source_columns']})"
    )

    model = fit_frozen_model(
        frame=frame,
        model_id=str(args.model_id),
        spec=HeadFitSpec(),
        provenance={
            "market_db": str(args.market_db),
            "window": [window_start.isoformat(), window_end.isoformat()],
            # R1.1：warmup 身份与 source_window 必须进 provenance——它们决定指纹
            # 覆盖的行范围，preflight 要按同一组参数复算（§8）。
            "warmup_days": int(args.warmup_days),
            "source_window": list(source_window),
            "training_data_fingerprint": str(data_fingerprint["fingerprint"]),
            "training_data_fingerprint_version": str(data_fingerprint["fingerprint_version"]),
            "training_data_rows": int(data_fingerprint["rows"]),
            "training_data_columns": list(data_fingerprint["columns"]),
            "training_data_available_columns": list(data_fingerprint["available_source_columns"]),
            "training_data_missing_optional_columns": list(
                data_fingerprint["missing_optional_source_columns"]
            ),
            "training_symbols_limit": int(args.max_symbols),
            "panel_fingerprint": panel_fingerprint(panel),
            "decision_rows": int(len(frame)),
            "quality_pool_source": str(suite.report.get("quality_pool_source", "research_proxy")),
            "price_mode": certification.mode,
            "price_mode_certified": certification.certified,
            "slippage_ratio": float(slippage),
        },
        extra_identity={
            "code_commit": model_code_commit,
            "code_commit_source": model_code_identity.code_commit_source,
            "identity_source": model_code_identity.identity_source,
            "git_branch": model_code_identity.git_branch,
            "config_hash": config_hash_of(config),
            "price_contract": price,
        },
    )
    out_dir = persist_frozen_model(model, Path(args.out))
    print(f"[freeze-model] 工件已冻结: {out_dir}")
    print(
        f"[freeze-model] artifact_hash={model.manifest.get('artifact_hash', '')}"
        f"（version={model.manifest.get('artifact_hash_version', '')}；"
        "v2 = 训练 provenance 已纳入受保护身份）"
    )
    trained = {
        key: value
        for key, value in model.diagnostics["targets"].items()
        if isinstance(value, dict) and value.get("status") == "ok"
    }
    print(f"[freeze-model] 训练成功目标: {len(trained)}/{len(model.manifest.get('targets', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
