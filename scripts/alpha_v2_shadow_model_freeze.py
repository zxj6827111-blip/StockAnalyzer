"""Alpha V2 M3：训练并冻结 Shadow 模型工件。

```bash
python scripts/alpha_v2_shadow_model_freeze.py \
    --feature-market-db /app/artifacts/vendor_delta/market_delta.duckdb \
    --execution-market-db /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb \
    --window-start 2025-06-02 --window-end 2026-08-31 \
    --warmup-days 200 --max-symbols 400 \
    --model-id alpha_v2_shadow_epoch_001 \
    --out artifacts/alpha_v2/validation
```

这正是 M3「冻结」语义里唯一允许写"模型"的入口：**一次**训练、落盘、锚定哈希，
之后每天的 Shadow 预测都从磁盘加载这份工件，不再训练。

**P0 双价格序列契约（2026-09-21）**：两个角色各用一份库，绝不共用：

```text
--feature-market-db    特征来源（qfq 是设计内口径）→ FeatureEngineer
--execution-market-db  成交/label 来源（必须 raw 且已认证）→ build_label_v2
                       净收益 / 超额 / MAE/MFE / 方向目标 / 全部基准序列
```

任何一份 execution 面板不是 ``raw + certified`` 就 **fail closed（exit 4）**，且守卫
在构造完整特征矩阵之前执行——不允许"跑 90 分钟才发现口径错了"。

``--market-db`` 是旧的单库参数：**只在 ``--rehearsal`` 下被接受**（两个角色绑同一份
库，provenance 如实标 ``db_role_binding=legacy_single_db`` 与
``validation_mode=rehearsal``）。生产形态不允许语义含糊：rehearsal 工件在
Production Preflight 一律 BLOCKED，进不了生产 epoch。

R1.1（训练 provenance 封存）：provenance 记录**完整**训练输入身份——决策窗
``window``、``warmup_days``、由二者推出的 ``source_window``，以及覆盖该 source
窗口全部面板源列的 ``training_data_fingerprint``（含版本）。

P0 起同一份 provenance 里还有**两条独立数据身份**：

```text
feature_data_identity    db / price_series_mode / 认证证据 / 指纹版本 /
                         source_window / fingerprint / columns / rows
execution_data_identity  同上 + certification（必须 raw + certified=true）
```

两者都由 ``frozen_model`` 的 v3 工件哈希保护：改动其中任何一个字段必然破坏工件
完整性（v2 工件只有单库自述，生产不再接受）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.dual_price_series import (  # noqa: E402
    DB_ROLE_BINDING_LEGACY,
    PriceSeriesContractError,
    price_series_identity_block,
    require_certified_execution_series,
    require_declared_feature_series,
    resolve_market_dbs,
)
from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec  # noqa: E402
from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint, OutcomeSpec  # noqa: E402
from stock_analyzer.alpha_v2.research.panel import (  # noqa: E402
    load_daily_panel,
    panel_fingerprint,
)
from stock_analyzer.alpha_v2.validation.dual_price_freeze import (  # noqa: E402
    build_dual_price_training_frame,
    select_frame_columns,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (  # noqa: E402
    FreezeGateError,
    assert_runtime_identity,
)
from stock_analyzer.alpha_v2.validation.frozen_model import (  # noqa: E402
    VALIDATION_MODE_PRODUCTION,
    VALIDATION_MODE_REHEARSAL,
    fit_frozen_model,
    frozen_targets,
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
    parser.add_argument(
        "--feature-market-db",
        default="",
        help="特征来源行情库（qfq 是设计内口径）；缺省回退 alpha_v2.feature_market_db",
    )
    parser.add_argument(
        "--execution-market-db",
        default="",
        help="成交/label 来源行情库（**必须 raw**）；缺省回退 alpha_v2.execution_market_db",
    )
    parser.add_argument(
        "--market-db",
        default=None,
        help=(
            "已废弃：单库形态（两个角色绑同一份库）。**只在 --rehearsal 下被接受**，"
            "生产请求双库必须用 --feature-market-db / --execution-market-db"
        ),
    )
    parser.add_argument("--window-start", required=True, help="训练窗口起点（含 warmup 之前）")
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--calibration-days", type=int, default=DEFAULT_CALIBRATION_DAYS)
    parser.add_argument("--warmup-days", type=int, default=200)
    parser.add_argument("--max-symbols", type=int, default=0, help="调试限产；0=全窗口")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--out", default="artifacts/alpha_v2/validation")
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help=(
            "排演模式：允许 --market-db 单库形态，并在 provenance 标 "
            "validation_mode=rehearsal（生产 preflight 会拒绝这份工件）。"
            "**不放松价格口径**：execution 面板仍必须 raw + certified"
        ),
    )
    return parser.parse_args(argv)


def _load_panel(*, db: str, window_start: date, window_end: date, warmup_days: int,
                max_symbols: int):
    return load_daily_panel(
        market_db=REPO_ROOT / db,
        window_start=window_start,
        window_end=window_end,
        warmup_days=int(warmup_days),
        max_symbols=int(max_symbols),
        source=str(db),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    window_start = date.fromisoformat(args.window_start)
    window_end = date.fromisoformat(args.window_end)
    config = load_config(Path(args.config))
    validation_mode = VALIDATION_MODE_REHEARSAL if args.rehearsal else VALIDATION_MODE_PRODUCTION

    # BLK-D2 同类修复：模型 provenance 的 code_commit 必须来自统一 resolver，
    # 与 validation freeze / capture / mature 是同一个值（容器里取构建身份）。
    # 放在面板加载**之前**：身份不可证就没必要跑几小时训练（fail-fast）。
    #
    # ``--rehearsal`` 与 validation freeze 同语义：不做身份硬门（工件会被标
    # validation_mode=rehearsal，生产 preflight 一律拒绝），但仍如实记录当前 commit。
    try:
        model_code_identity = resolve_runtime_code_identity(
            REPO_ROOT, validation_mode=validation_mode
        )
        model_code_commit = assert_runtime_identity(
            model_code_identity, validation_mode=validation_mode
        )
    except FreezeGateError as exc:
        print(f"[freeze-model] 运行身份硬门未通过: {exc}", file=sys.stderr)
        return 3

    # ── 双价格源解析（生产不允许语义含糊）────────────────────────────────────
    mapping = resolve_market_dbs(
        config,
        feature_db=args.feature_market_db,
        execution_db=args.execution_market_db,
        legacy_market_db=args.market_db,
    )
    if mapping.db_role_binding == DB_ROLE_BINDING_LEGACY and not args.rehearsal:
        print(
            "[freeze-model] 拒绝（exit_code=4）：生产形态不接受 --market-db 单库参数"
            "（语义含糊：同一份序列既当特征又当成交价）。请分别给 "
            "--feature-market-db 与 --execution-market-db；本地排演请显式加 --rehearsal",
            file=sys.stderr,
        )
        return 4
    if not mapping.execution_db:
        print(
            "[freeze-model] 拒绝（exit_code=4）：未配置 execution 行情库。"
            "execution 侧必须是 raw 序列（--execution-market-db 或 "
            "alpha_v2.execution_market_db / SA__ALPHA_V2__EXECUTION_MARKET_DB）；"
            "没有 raw 库就不存在合法的训练目标",
            file=sys.stderr,
        )
        return 4
    print(
        f"[freeze-model] 双价格源: feature={mapping.feature_db}"
        f"（{mapping.feature_source}） / execution={mapping.execution_db}"
        f"（{mapping.execution_source}）"
    )

    # ── execution 面板：认证 + 硬门（在任何重活之前）──────────────────────────
    try:
        execution_panel = _load_panel(
            db=mapping.execution_db,
            window_start=window_start,
            window_end=window_end,
            warmup_days=args.warmup_days,
            max_symbols=args.max_symbols,
        )
        execution_certification = execution_panel.certify_price_mode(min_sample=1000)
        require_certified_execution_series(
            execution_certification,
            context="freeze:execution_panel",
            db=mapping.execution_db,
        )
    except PriceSeriesContractError as exc:
        print(f"[freeze-model] 拒绝（exit_code={exc.exit_code}）: {exc}", file=sys.stderr)
        return exc.exit_code
    print(
        f"[freeze-model] execution 价格口径认证: mode={execution_certification.mode} "
        f"certified={execution_certification.certified} "
        f"source={execution_certification.source}"
    )

    feature_panel = _load_panel(
        db=mapping.feature_db,
        window_start=window_start,
        window_end=window_end,
        warmup_days=args.warmup_days,
        max_symbols=args.max_symbols,
    )
    feature_certification = feature_panel.certify_price_mode(min_sample=1000)
    try:
        require_declared_feature_series(
            feature_certification,
            context="freeze:feature_panel",
            db=mapping.feature_db,
        )
    except PriceSeriesContractError as exc:
        print(f"[freeze-model] 拒绝（exit_code={exc.exit_code}）: {exc}", file=sys.stderr)
        return exc.exit_code
    print(
        f"[freeze-model] feature 价格口径: mode={feature_certification.mode} "
        f"certified={feature_certification.certified} source={feature_certification.source} "
        "（feature 用 qfq 是设计内；执行侧另有 raw 硬门）"
    )
    price = price_contract_block(config)

    # PIT 决策集合（逐日合格池）：由 **feature 面板**决定"看哪些票、在哪一天决策"。
    decisions: list[DecisionPoint] = [
        DecisionPoint(symbol, day)
        for day in feature_panel.calendar
        for symbol in feature_panel.pit_universe(as_of=day).eligible_symbols
    ]
    if not decisions:
        print(
            f"[freeze-model] 拒绝（exit_code=6）：决策集合为空——窗口 "
            f"{window_start}..{window_end} 内 PIT 合格池一个都没选出来。"
            "检查 --window-start（必须晚于 warmup 段）与面板历史长度",
            file=sys.stderr,
        )
        return 6
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)
    slippage = matcher.static_slippage_ratio("trend")

    # ── 训练帧：feature 侧出 X，execution 侧出 y（守卫在函数第一步）───────────
    built = build_dual_price_training_frame(
        feature_panel=feature_panel,
        execution_panel=execution_panel,
        decisions=decisions,
        matcher=matcher,
        slippage_ratio=slippage,
        execution_certification=execution_certification,
        feature_certification=feature_certification,
        spec=OutcomeSpec(),
        context="freeze_model",
    )
    frame = select_frame_columns(
        built.frame,
        safe_features=built.safe_feature_columns,
        targets=[target.target for target in frozen_targets()],
    )
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
    #
    # P0：**两条**指纹——feature 侧（特征输入）与 execution 侧（label/target 输入）。
    try:
        feature_fingerprint = compute_training_data_fingerprint(
            REPO_ROOT / mapping.feature_db,
            training_start=window_start,
            training_end=window_end,
            warmup_days=int(args.warmup_days),
        )
        execution_fingerprint = compute_training_data_fingerprint(
            REPO_ROOT / mapping.execution_db,
            training_start=window_start,
            training_end=window_end,
            warmup_days=int(args.warmup_days),
        )
    except TrainingDataFingerprintError as exc:
        print(f"[freeze-model] 训练数据指纹不可计算（拒绝冻结）: {exc}", file=sys.stderr)
        return 5
    source_window = [str(item) for item in feature_fingerprint["source_window"]]
    try:
        _assert_panel_not_earlier_than_source_window(
            panel_earliest=(
                feature_panel.bars["trade_date"].min() if not feature_panel.bars.empty else None
            ),
            source_window=source_window,
            role="feature",
        )
        _assert_panel_not_earlier_than_source_window(
            panel_earliest=(
                execution_panel.bars["trade_date"].min()
                if not execution_panel.bars.empty
                else None
            ),
            source_window=[str(item) for item in execution_fingerprint["source_window"]],
            role="execution",
        )
    except PriceSeriesContractError as exc:
        print(f"[freeze-model] 拒绝（exit_code={exc.exit_code}）: {exc}", file=sys.stderr)
        return exc.exit_code
    for role, payload in (("feature", feature_fingerprint), ("execution", execution_fingerprint)):
        print(
            f"[freeze-model] {role}_fingerprint={str(payload['fingerprint'])[:16]}… "
            f"rows={payload['rows']} (version={payload['fingerprint_version']}, "
            f"source_window={payload['source_window'][0]}..{payload['source_window'][1]}, "
            f"warmup_days={payload['warmup_days']}, columns={len(payload['columns'])})"
        )

    feature_identity = price_series_identity_block(
        role="feature",
        db=mapping.feature_db,
        certification=feature_certification,
        fingerprint=feature_fingerprint,
        context="freeze_model:feature_panel",
    )
    execution_identity = price_series_identity_block(
        role="execution",
        db=mapping.execution_db,
        certification=execution_certification,
        fingerprint=execution_fingerprint,
        context="freeze_model:execution_panel",
    )

    model = fit_frozen_model(
        frame=frame,
        model_id=str(args.model_id),
        spec=HeadFitSpec(),
        provenance={
            # 兼容键：market_db / price_mode / training_data_* 描述的是**特征侧**
            # （历史消费者按此理解），execution 侧另有 execution_* 与两条身份块。
            "market_db": str(mapping.feature_db),
            "feature_price_mode": str(feature_certification.mode),
            "price_mode": str(feature_certification.mode),
            "price_mode_certified": bool(feature_certification.certified),
            "execution_price_mode": str(execution_certification.mode),
            "execution_price_mode_certified": bool(execution_certification.certified),
            "validation_mode": validation_mode,
            "db_role_binding": mapping.db_role_binding,
            "feature_data_identity": feature_identity,
            "execution_data_identity": execution_identity,
            # R1.1：warmup 身份与 source_window 必须进 provenance——它们决定指纹
            # 覆盖的行范围，preflight 要按同一组参数复算（§8）。
            "window": [window_start.isoformat(), window_end.isoformat()],
            "warmup_days": int(args.warmup_days),
            "source_window": list(source_window),
            "training_data_fingerprint": str(feature_fingerprint["fingerprint"]),
            "training_data_fingerprint_version": str(feature_fingerprint["fingerprint_version"]),
            "training_data_rows": int(feature_fingerprint["rows"]),
            "training_data_columns": list(feature_fingerprint["columns"]),
            "training_data_available_columns": list(
                feature_fingerprint["available_source_columns"]
            ),
            "training_data_missing_optional_columns": list(
                feature_fingerprint["missing_optional_source_columns"]
            ),
            "training_symbols_limit": int(args.max_symbols),
            "panel_fingerprint": panel_fingerprint(feature_panel),
            "decision_rows": int(len(frame)),
            "dual_price_evidence": built.evidence,
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
        "v3 = 训练 provenance + 双价格源身份均已纳入受保护身份）"
    )
    print(
        "[freeze-model] 数据身份: "
        f"feature[{feature_identity['price_series_mode']}] "
        f"{str(feature_identity['fingerprint'])[:12]}… / "
        f"execution[{execution_identity['price_series_mode']},"
        f"certified={execution_identity['price_series_certified']}] "
        f"{str(execution_identity['fingerprint'])[:12]}…"
    )
    trained = {
        key: value
        for key, value in model.diagnostics["targets"].items()
        if isinstance(value, dict) and value.get("status") == "ok"
    }
    print(f"[freeze-model] 训练成功目标: {len(trained)}/{len(model.manifest.get('targets', []))}")
    return 0


def _assert_panel_not_earlier_than_source_window(
    *, panel_earliest: object, source_window: list[str], role: str
) -> None:
    """证据（而非复述公式）：面板实际装载的最早 bar 不得早于指纹声明的 source_window 起点。

    若早于，说明指纹窗口比训练输入窄——那正是 R1.1 要关掉的缺口；两个角色都要过。
    """
    if panel_earliest is None:
        return
    earliest = panel_earliest.date() if hasattr(panel_earliest, "date") else panel_earliest
    if earliest < date.fromisoformat(source_window[0]):
        raise PriceSeriesContractError(
            f"{role} 面板装载的最早 bar {earliest} 早于 {role} 数据指纹的 source_window "
            f"起点 {source_window[0]}——指纹窗口比实际输入窄，拒绝冻结",
            exit_code=5,
        )


if __name__ == "__main__":
    raise SystemExit(main())
