"""Alpha V2 M3：outcome 成熟跑批（每天收盘后跑一次）。

```bash
python scripts/alpha_v2_shadow_mature.py --epoch-id alpha_v2_epoch_001 --evaluation-date 2026-09-18
```

面板只加载到 ``--evaluation-date``（物理截断）——"把未来数据装进来再慢慢过滤"
这种写法不是防御，是泄漏的温床。

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
from stock_analyzer.backtest.matcher import ExecutionMatcher  # noqa: E402
from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.config_identity import stable_payload_hash  # noqa: E402
from stock_analyzer.contracts.alpha_v2 import resolve_selection_contract  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：outcome 成熟")
    parser.add_argument("--epoch-id", required=True)
    parser.add_argument("--evaluation-date", required=True)
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
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

    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)
    slippage = matcher.static_slippage_ratio("trend")

    from stock_analyzer.alpha_v2.validation.shadow_capture import list_shadow_dates

    shadow_days = list_shadow_dates(root, epoch.epoch_id)
    if not shadow_days:
        print("[mature] 没有 shadow 日；无需运行", flush=True)
        return 0
    earliest = min(shadow_days)
    panel = load_daily_panel(
        market_db=REPO_ROOT / args.market_db,
        window_start=earliest,
        window_end=evaluation_date,
        warmup_days=int(args.warmup_days),
    )
    certification = panel.certify_price_mode(min_sample=1000)

    summary = mature_epoch_outcomes(
        root=root,
        epoch=epoch,
        panel=panel,
        evaluation_date=evaluation_date,
        matcher=matcher,
        slippage_ratio=slippage,
        price_mode=certification.mode,
        price_mode_certified=certification.certified,
        runtime_identity=runtime_identity,
    )
    print(f"[mature] {summary['status']}: 写入 {summary['rows_written']} 行; "
          f"days={summary['epoch_days']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
