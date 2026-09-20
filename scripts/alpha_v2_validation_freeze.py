"""Alpha V2 M3：生成/校验 Validation Freeze Manifest 并开关 epoch。

```bash
# 生产（默认）：冻结清单 + epoch 开关，fail-closed 的硬门全部生效
python scripts/alpha_v2_validation_freeze.py --epoch-id alpha_v2_epoch_001 \
    --model-dir artifacts/alpha_v2/validation/model/alpha_v2_shadow_epoch_001 \
    --start-date 2026-09-22 --open-epoch

# 排演（本机演练用）：放弃生产硬门，清单如实标 validation_mode=rehearsal，
# 由它写出来的日子永远 clean_oos_eligible=false（KPI 样本门不会计入）
python scripts/alpha_v2_validation_freeze.py --epoch-id alpha_v2_epoch_900 \
    --start-date 2026-03-01 --rehearsal
```

生产模式硬门（任一不过即退出非零；`validation/freeze_precheck.py` 是同源实现）：

1. ``execution_price_mode == raw``（否则 exit 4）
2. **运行身份必须可证**（`runtime_identity.resolve_runtime_code_identity` 先判环境，
   再套该环境的规则；任一违例 exit 5）：

   ```text
   git_checkout             git HEAD == --code-commit == .build_commit ==
                            build_manifest.commit，且工作区必须可证干净
   container_build_identity .build_commit == build_manifest.commit、
                            trusted=true、dirty=false
                            （容器里没有 git checkout，工作区门不适用）
   ```

3. ``--start-date`` 必须给出且不早于今天（exit 6）
4. feature schema 必须非空（来自 --feature-columns-file 或 --model-dir），两源都给必须一致
5. **模型训练身份绑定（R4.1）**：``--model-dir`` 工件的 ``code_commit`` 必须存在、形态合法、
   且等于本次运行 identity 的 code_commit（缺 / unknown / 非法 / 不一致都 exit 5）。
   也就是说 train=A 而 runtime=B 的模型，在**开 epoch 之前**就被拒——
   不允许"先开 epoch、等 capture 才发现"。

> 生产形状：`--model-dir` 指向**已经冻结**的 shadow 模型工件（`alpha_v2_shadow_model_freeze.py`
> 的产物），feature schema、model 身份与训练身份都从它派生——所以模型冻结必须先于本步骤执行。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.research.feature_audit import (  # noqa: E402
    FEATURE_GROUPS,
    safe_feature_columns,
)
from stock_analyzer.alpha_v2.validation.epoch import (  # noqa: E402
    EpochRegistryError,
    open_epoch,
)
from stock_analyzer.alpha_v2.validation.freeze import (  # noqa: E402
    build_validation_freeze,
    freeze_manifest_hash,
    write_validation_freeze,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (  # noqa: E402
    FreezeGateError,
    assert_execution_price_raw,
    assert_model_training_commit,
    assert_runtime_identity,
    assert_validation_start_date,
    resolve_feature_schema_columns,
)
from stock_analyzer.alpha_v2.validation.frozen_model import (  # noqa: E402
    frozen_model_identity_payload,
    load_frozen_model,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    IDENTITY_SOURCE_CONTAINER_BUILD,
    config_hash_of,
    is_valid_commit,
    price_contract_block,
    resolve_runtime_code_identity,
)
from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.contracts.alpha_v2 import resolve_selection_contract  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：冻结清单 + epoch 开关")
    parser.add_argument("--epoch-id", required=True)
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "alpha_v2"))
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "default.yaml"))
    parser.add_argument("--model-dir", default="", help="冻结模型工件目录（缺省 = pending_freeze）")
    parser.add_argument(
        "--feature-columns-file",
        default="",
        help="生产/冻结特征列清单（JSON 数组）；与 --model-dir 给必须一致",
    )
    parser.add_argument("--start-date", default="", help="首个 shadow 交易日（生产模式必填）")
    parser.add_argument("--open-epoch", action="store_true", help="写完清单后打开 epoch")
    parser.add_argument("--print-only", action="store_true")
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="排演模式：放弃生产硬门；清单 validation_mode=rehearsal，clean OOS 资格恒为 false",
    )
    parser.add_argument(
        "--code-commit",
        default="",
        help="显式指定 code_commit（必须与 git HEAD 一致；容器里 git 不可用时用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(Path(args.config))
    price = price_contract_block(config)
    validation_mode = "rehearsal" if args.rehearsal else "production"

    try:
        assert_execution_price_raw(
            str(price["execution_price_mode"]), validation_mode=validation_mode
        )
        # Runtime Identity Hardening（BLK-D1）：**先判运行环境再套规则**。
        # 源码检出用 git HEAD + 工作区干净门；不可变容器用构建身份
        # （.build_commit == build_manifest.commit、trusted、dirty=false）替代——
        # 旧实现在判定环境之前先要 git status，容器里永远撞 exit 5。
        identity = resolve_runtime_code_identity(
            REPO_ROOT,
            requested_code_commit=args.code_commit,
            validation_mode=validation_mode,
            require_build_identity=True,
        )
        code_commit = assert_runtime_identity(identity, validation_mode=validation_mode)
        code_commit_source = identity.code_commit_source
        # 清单结构上要求 code_commit 非空（assert_freeze_complete）。rehearsal 不做身份硬门，
        # 所以这里单独兜底：既没有 git HEAD、也没有构建身份时**干净拒绝**，
        # 而不是让 FreezeIncompleteError 从落盘层以 traceback 的形式冒出来。
        if not is_valid_commit(code_commit):
            raise FreezeGateError(
                f"无法解析运行 code_commit（{code_commit or '(空)'}）：既没有 git HEAD，"
                "也没有构建身份；冻结清单必须能证明跑的是哪一份代码",
                exit_code=5,
            )
        assert_validation_start_date(
            args.start_date,
            today=datetime.now().astimezone().date(),
            validation_mode=validation_mode,
        )
    except FreezeGateError as exc:
        print(f"[freeze] 拒绝（生产硬门 exit_code={exc.exit_code}）: {exc}", file=sys.stderr)
        return exc.exit_code

    # ── 模型与特征 schema（B4：schema 必须来自工件或显式文件，不允许空转）─────────
    model_block: dict[str, object]
    if args.model_dir:
        try:
            model_block = frozen_model_identity_payload(args.model_dir)
        except Exception as exc:  # noqa: BLE001 - CLI 边界：意图明确的失败
            print(f"[freeze] 模型工件核验失败: {exc}", file=sys.stderr)
            return 2
        # R4.1.1 / review-P2：生产 freeze 同时校验工件**内容完整性**（每个文件的 sha256 +
        # artifact_hash 复算），不再只读身份字段——否则"改写 manifest 身份冒充合法模型"
        # 的工件会先在 freeze 阶段被锚定、等到 capture 才被拒（身份能过、内容是假的）。
        # 冻结阶段就把它挡下，不给不一致的工件进入 epoch 的机会。
        # rehearsal 不做此校验：排演允许骨架工件（身份字段齐全但无可推理内容）。
        if validation_mode == "production":
            try:
                load_frozen_model(args.model_dir)
            except Exception as exc:  # noqa: BLE001 - CLI 边界：意图明确的失败
                print(f"[freeze] 拒绝（模型工件完整性）: {exc}", file=sys.stderr)
                return 5
    else:
        model_block = {}  # → pending_freeze（显式、可审计的空块，不假装有模型）

    file_columns: list[str] = []
    if args.feature_columns_file:
        import json

        file_columns = list(json.loads(Path(args.feature_columns_file).read_text(encoding="utf-8")))
    try:
        schema = resolve_feature_schema_columns(
            file_columns=file_columns,
            model_columns=list(model_block.get("feature_columns", []) or []),
            validation_mode=validation_mode,
        )
    except FreezeGateError as exc:
        print(f"[freeze] 拒绝（特征 schema 硬门）: {exc}", file=sys.stderr)
        return 6
    safe_features = list(safe_feature_columns(schema.feature_columns))
    feature_group_ids = [
        spec.group_id for spec in FEATURE_GROUPS if spec.in_base_v2
    ]

    # ── R4.1：模型训练身份绑定（生产强不变量）──────────────────────────────────
    # runtime code_commit 必须等于冻结模型工件的训练 code_commit。放在 schema 门之后、
    # 写盘/开 epoch 之前：训练身份不可证就绝不产出"合法 production freeze"，也绝不 open
    # epoch（train=A / runtime=B 必须死在这一步，而不是等 capture 才发现）。
    try:
        assert_model_training_commit(
            model_training_code_commit=str(model_block.get("model_training_code_commit", "")),
            runtime_code_commit=code_commit,
            validation_mode=validation_mode,
        )
    except FreezeGateError as exc:
        print(
            f"[freeze] 拒绝（模型训练身份硬门 exit_code={exc.exit_code}）: {exc}",
            file=sys.stderr,
        )
        return exc.exit_code

    contract = resolve_selection_contract(config, profile="night_scan")
    manifest = build_validation_freeze(
        validation_epoch_id=str(args.epoch_id),
        code_commit=code_commit,
        git_branch=identity.git_branch,
        config_hash=config_hash_of(config),
        config_hash_scope="effective_config_with_env_overrides",
        model=model_block,
        feature_columns=safe_features,
        feature_group_ids=feature_group_ids,
        selection_contract=contract.to_payload(),
        execution_price_mode=str(price["execution_price_mode"]),
        feature_price_mode=str(price["feature_price_mode"]),
        validation_start_date=(args.start_date or None),
        created_at=datetime.now().astimezone().isoformat(),
        legacy_invariants={
            "final_signal_min_threshold": config.week5.final_signal_min_threshold,
            "cross_review_p_lgbm_min": config.models.cross_review.p_lgbm_min,
            "cross_review_p_xgb_min": config.models.cross_review.p_xgb_min,
            "cross_review_p_meta_min": config.models.cross_review.p_meta_min,
        },
        validation_mode=validation_mode,
        feature_schema_source=schema.source,
        # R3：production 恒不声明确定性时钟（写入日必须等于系统真实日期）；
        # rehearsal 声明它，好让行上的 deterministic_clock 标记有据可查。
        deterministic_clock=bool(validation_mode != "production"),
        deterministic_clock_source=(
            "freeze_cli_rehearsal"
            if validation_mode != "production"
            else "freeze_cli_production_path"
        ),
    )
    manifest["code_commit_source"] = code_commit_source
    # R3：把构建身份与工作区状态写进清单（纳入 freeze_manifest_hash 覆盖）。
    # 硬化后同一块新增 identity_source / git_available / identity_verified / violations，
    # 让审计能直接看到"这次身份来自哪种 runtime context"。
    manifest["build_identity"] = identity.to_payload()
    manifest["freeze_manifest_hash"] = freeze_manifest_hash(manifest)

    if args.print_only:
        import json

        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    path = write_validation_freeze(manifest, root=args.out)
    print(f"[freeze] 冻结清单已写入: {path}")
    print(f"[freeze] freeze_manifest_hash = {manifest['freeze_manifest_hash']}")
    print(
        f"[freeze] 运行身份: identity_source={identity.identity_source} "
        f"(git_available={identity.git_available}) / code_commit={code_commit[:12]}…"
        f"（source={code_commit_source}）"
    )
    print(
        "[freeze] 构建身份: git_head="
        f"{identity.git_head[:12]}… / .build_commit="
        f"{(identity.build_commit or '(缺失)')[:12]}… / build_manifest="
        f"{(identity.build_manifest_commit or '(缺失)')[:12]}…"
        f"（trusted={identity.build_manifest_trusted}, "
        f"dirty={identity.build_manifest_dirty}）"
    )
    if identity.identity_source == IDENTITY_SOURCE_CONTAINER_BUILD:
        print(
            "[freeze] 不可变容器形态：身份取自镜像构建期写入的 .build_commit + "
            "build_manifest.json 双源互证（容器内没有 git checkout，工作区门不适用）"
        )
    elif code_commit_source == "cli_override_git_unavailable":
        print(
            "[freeze] 注意：git HEAD 不可得，本清单的 code_commit 由 --code-commit 声明、"
            "并由 .build_commit + build_manifest 双源互证（审计字段 code_commit_source 已落盘）",
            file=sys.stderr,
        )
    # R4.1：把训练身份绑定结果打出来（生产走到这里必然相等，非生产如实显示）
    training_commit = str(model_block.get("model_training_code_commit", "") or "")
    print(
        "[freeze] 模型训练身份: "
        f"{training_commit[:12] or '(缺失)'}…"
        f"（== 运行身份: {training_commit == code_commit}）"
    )
    if validation_mode == "rehearsal":
        print(
            "[freeze] 排演模式：本清单 clean_oos 资格恒为 false，KPI 样本门不会计入",
            file=sys.stderr,
        )

    if args.open_epoch:
        try:
            record = open_epoch(
                root=args.out,
                epoch_id=str(args.epoch_id),
                freeze_manifest_hash=str(manifest["freeze_manifest_hash"]),
                identity={
                    "code_commit": manifest["code_commit"],
                    "config_hash": manifest["config_hash"],
                    "model_id": manifest["model"]["model_id"],
                    "model_artifact_hash": manifest["model"]["artifact_hash"],
                    # R4.1：epoch 落账并审计模型训练身份。语义边界（评审 P3-1）：
                    # 它是**审计键**——经 freeze_manifest_hash 锚定（改清单会被
                    # require_epoch_identity_match 拦下），且 capture/mature 各自独立
                    # 重读清单模型块再比对；它**不在** FROZEN_IDENTITY_KEYS 的 8 键
                    # 门禁集合里（那里是 runtime identity 平面，门禁比对保持既有语义）。
                    "model_training_code_commit": manifest["model"]["model_training_code_commit"],
                    "feature_schema_hash": manifest["feature_schema_hash"],
                    "label_policy_hash": manifest["label_policy_hash"],
                    "selection_contract_id": manifest["selection_contract_id"],
                    "execution_price_mode": manifest["execution_price_mode"],
                },
                opened_on_date=(
                    args.start_date
                    or manifest.get("validation_start_date")
                    or datetime.now().astimezone().date().isoformat()
                ),
            )
            print(f"[freeze] epoch 已开启: {record.epoch_id} @ {record.opened_on_date}")
        except EpochRegistryError as exc:
            print(f"[freeze] epoch 未开: {exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
