"""Alpha V2 M3：outcome 成熟跑批（每天收盘后跑一次）。

```bash
python scripts/alpha_v2_shadow_mature.py --epoch-id alpha_v2_epoch_001 \
    --evaluation-date 2026-09-22 \
    --feature-market-db /app/artifacts/vendor_delta/market_delta.duckdb \
    --execution-market-db /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
```

面板只加载到 ``--evaluation-date``（物理截断）——"把未来数据装进来再慢慢过滤"
这种写法不是防御，是泄漏的温床。

**P0 双价格序列契约（2026-09-21）**：成熟动作每天**重新**认证 execution 库：

```text
--execution-market-db  → 成交/label/净收益/MAE/MFE/基准（必须 raw + certified）
--feature-market-db    → 风格维度（同板块 kNN 对照用；取冻结的 feature 侧契约）
```

execution 不是 raw 就 **FAIL CLOSED 且一行 outcome 都不写**（exit 4）——复权价算出来的
"未来收益"进不了 KPI。生产形态不接受 ``--market-db`` 单库参数（语义含糊）；它只在
``--rehearsal`` 下被接受。

风格面板按 epoch 内出现过的 symbol 过滤加载：成熟日窗口会随 epoch 变长而变长，
再整份装一遍 feature 库既慢又占内存，而风格维度只需要这些票的窗口行（收益仍来自
execution 面板）。

修复轮 (F2/F3)：成熟任务与影子写入同一条"epoch 开 + 冻结清单锚定 + 身份一致"
的门，先过门再加载面板（fail-fast）。

Runtime Identity Hardening（BLK-D2）：运行身份同样走
``runtime_identity.resolve_runtime_code_identity``——源码检出取 git HEAD，
不可变容器取镜像构建身份；两者都必须与 epoch 冻结身份逐位一致。
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
from stock_analyzer.alpha_v2.research.outcomes import OutcomeSpec  # noqa: E402
from stock_analyzer.alpha_v2.research.panel import load_daily_panel  # noqa: E402
from stock_analyzer.alpha_v2.validation.epoch import (  # noqa: E402
    EpochRegistryError,
    get_epoch,
    require_epoch_identity_match,
)
from stock_analyzer.alpha_v2.validation.freeze import (  # noqa: E402
    label_policy_payload,
    load_validation_freeze,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (  # noqa: E402
    FreezeGateError,
    assert_model_training_commit,
    assert_runtime_identity,
)
from stock_analyzer.alpha_v2.validation.outcome_maturation import (  # noqa: E402
    mature_epoch_outcomes,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    config_hash_of,
    price_contract_block,
    resolve_runtime_code_identity,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (  # noqa: E402
    list_shadow_dates,
    read_shadow_rows,
)
from stock_analyzer.backtest.matcher import ExecutionMatcher  # noqa: E402
from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.config_identity import stable_payload_hash  # noqa: E402
from stock_analyzer.contracts.alpha_v2 import resolve_selection_contract  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：outcome 成熟")
    parser.add_argument("--epoch-id", required=True)
    parser.add_argument("--evaluation-date", required=True)
    parser.add_argument(
        "--execution-market-db",
        default="",
        help="成交/label 来源（**必须 raw**）；缺省回退 alpha_v2.execution_market_db",
    )
    parser.add_argument(
        "--feature-market-db",
        default="",
        help="风格维度来源（feature 侧契约）；缺省回退 alpha_v2.feature_market_db",
    )
    parser.add_argument(
        "--market-db",
        default=None,
        help="已废弃：单库形态，**只在 --rehearsal 下被接受**",
    )
    parser.add_argument("--rehearsal", action="store_true", help="排演模式（允许 --market-db）")
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "alpha_v2"))
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "default.yaml"))
    parser.add_argument("--warmup-days", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    evaluation_date = date.fromisoformat(args.evaluation_date)
    root = Path(args.out)
    epoch = get_epoch(root, args.epoch_id)
    if epoch is None:
        print(f"[mature] epoch 不存在: {args.epoch_id}", file=sys.stderr)
        return 2

    config = load_config(Path(args.config))
    contract = resolve_selection_contract(config, profile="night_scan")
    price = price_contract_block(config)
    # BLK-D2：运行身份走与 freeze / capture 同一个 resolver（容器里取构建身份，
    # 源码检出取 git HEAD），不允许再直接 git_head(REPO_ROOT)。
    freeze = load_validation_freeze(root)
    validation_mode = str((freeze or {}).get("validation_mode", "production"))
    try:
        runtime_code_commit = assert_runtime_identity(
            resolve_runtime_code_identity(REPO_ROOT, validation_mode=validation_mode),
            validation_mode=validation_mode,
        )
    except FreezeGateError as exc:
        print(f"[mature] 运行身份硬门未通过: {exc}", file=sys.stderr)
        return 3
    # R4.1：模型训练身份绑定——本 epoch 冻结的训练 commit 必须等于当前运行身份
    try:
        assert_model_training_commit(
            model_training_code_commit=str(
                (dict(freeze or {}).get("model") or {}).get("model_training_code_commit", "")
            ),
            runtime_code_commit=runtime_code_commit,
            validation_mode=validation_mode,
        )
    except FreezeGateError as exc:
        print(f"[mature] 模型训练身份硬门未通过: {exc}", file=sys.stderr)
        return 3
    runtime_identity = {
        "code_commit": runtime_code_commit,
        "config_hash": config_hash_of(config),
        "label_policy_hash": stable_payload_hash(label_policy_payload(OutcomeSpec())),
        "selection_contract_id": str(
            contract.to_payload().get("selection_contract_id", "night_alpha_v2_v1")
        ),
        "execution_price_mode": str(price["execution_price_mode"]),
    }
    # 幂等检查在加载面板之前做——closed epoch / 清单被换 / 身份漂移都直接拒；
    # 对 runtime 给不了的键（model_* / feature_schema_hash）由成熟任务在影子行上核对
    try:
        require_epoch_identity_match(
            root=root,
            epoch_id=epoch.epoch_id,
            identity=runtime_identity,
            keys=tuple(runtime_identity),
        )
    except EpochRegistryError as exc:
        print(f"[mature] 冻结锚定/身份校验失败: {exc}", file=sys.stderr)
        return 3

    shadow_days = list_shadow_dates(root, epoch.epoch_id)
    if not shadow_days:
        print("[mature] 没有 shadow 日；无需运行", flush=True)
        return 0

    # ── 双价格源解析（生产不允许语义含糊）────────────────────────────────────
    mapping = resolve_market_dbs(
        config,
        feature_db=args.feature_market_db,
        execution_db=args.execution_market_db,
        legacy_market_db=args.market_db,
    )
    if mapping.db_role_binding == DB_ROLE_BINDING_LEGACY and validation_mode == "production":
        print(
            "[mature] 拒绝（exit_code=4）：生产形态不接受 --market-db 单库参数"
            "（无法证明成交价来自 raw）。请分别给 --execution-market-db 与 "
            "--feature-market-db；排演请显式加 --rehearsal",
            file=sys.stderr,
        )
        return 4
    if not mapping.execution_db:
        print(
            "[mature] 拒绝（exit_code=4）：未配置 execution 行情库"
            "（--execution-market-db 或 alpha_v2.execution_market_db）——"
            "raw 不可证就不写 outcome",
            file=sys.stderr,
        )
        return 4

    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)
    slippage = matcher.static_slippage_ratio("trend")

    earliest = min(shadow_days)
    # ① execution 面板：全票种装载（日历必须完整——成熟门按市场日历判）。
    try:
        panel = load_daily_panel(
            market_db=REPO_ROOT / mapping.execution_db,
            window_start=earliest,
            window_end=evaluation_date,
            warmup_days=int(args.warmup_days),
            source=str(mapping.execution_db),
        )
    except Exception as exc:  # noqa: BLE001 - CLI 边界：库不可读就是明确失败
        print(f"[mature] execution 行情库不可读: {exc}", file=sys.stderr)
        return 4
    # P0：**每天**重新认证（不是复用训练时的结论）。
    certification = panel.certify_price_mode(min_sample=1000)
    try:
        require_certified_execution_series(
            certification, context="mature:execution_panel", db=mapping.execution_db
        )
    except PriceSeriesContractError as exc:
        print(f"[mature] 拒绝（exit_code={exc.exit_code}）: {exc}", file=sys.stderr)
        return exc.exit_code
    print(
        f"[mature] execution 价格口径: mode={certification.mode} "
        f"certified={certification.certified} source={certification.source} "
        f"db={mapping.execution_db}"
    )

    # ② feature 面板（仅风格维度）：按 epoch 内出现过的 symbol 过滤，避免把整份
    #    feature 库再装一遍（成熟窗口随 epoch 变长而变长，整装会撞 NAS 内存上限）。
    shadow_symbols = sorted(
        {
            str(row.get("symbol", "") or "")
            for day in shadow_days
            for row in read_shadow_rows(root, epoch.epoch_id, day)
        }
        - {""}
    )
    style_panel = None
    if mapping.feature_db and shadow_symbols:
        try:
            style_panel = load_daily_panel(
                market_db=REPO_ROOT / mapping.feature_db,
                window_start=earliest,
                window_end=evaluation_date,
                warmup_days=int(args.warmup_days),
                symbols=shadow_symbols,
                source=str(mapping.feature_db),
            )
            feature_certification = style_panel.certify_price_mode(min_sample=1000)
            require_declared_feature_series(
                feature_certification,
                context="mature:feature_panel",
                db=mapping.feature_db,
            )
        except PriceSeriesContractError as exc:
            print(f"[mature] 拒绝（exit_code={exc.exit_code}）: {exc}", file=sys.stderr)
            return exc.exit_code
        except Exception as exc:  # noqa: BLE001 - feature 面板缺失不阻断成熟（风格层降级）
            print(
                f"[mature] 警告：feature 面板不可用（{exc.__class__.__name__}: {exc}）；"
                "风格维度退回 execution 面板（摘要会标 style_features_source）",
                file=sys.stderr,
            )
            style_panel = None

    execution_identity = price_series_identity_block(
        role="execution",
        db=mapping.execution_db,
        certification=certification,
        context="mature:execution_panel",
    )

    summary = mature_epoch_outcomes(
        root=root,
        epoch=epoch,
        panel=panel,
        style_panel=style_panel,
        evaluation_date=evaluation_date,
        matcher=matcher,
        slippage_ratio=slippage,
        price_mode=certification.mode,
        price_mode_certified=certification.certified,
        runtime_identity=runtime_identity,
        execution_data_identity=execution_identity,
    )
    print(
        f"[mature] {summary['status']}: 写入 {summary['rows_written']} 行; "
        f"days={summary['epoch_days']}; "
        f"style_features_source={summary.get('style_features_source')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
