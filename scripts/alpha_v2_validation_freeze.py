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
2. 工作区必须干净（部署期身份文件 `.build_commit`/``build_manifest.json`` 豁免）
3. code_commit 必须可证（git HEAD ↔ .build_commit / build_manifest 逐位一致）
4. ``--start-date`` 必须给出且不早于今天
5. feature schema 必须非空（来自 --feature-columns-file 或 --model-dir），两源都给必须一致
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
    assert_build_identity,
    assert_execution_price_raw,
    assert_validation_start_date,
    assert_worktree_clean,
    resolve_code_commit,
    resolve_feature_schema_columns,
)
from stock_analyzer.alpha_v2.validation.frozen_model import (  # noqa: E402
    frozen_model_identity_payload,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    build_identity_block,
    config_hash_of,
    git_branch,
    git_head,
    price_contract_block,
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
        code_commit, code_commit_source = resolve_code_commit(
            args.code_commit, git_head_value=git_head(REPO_ROOT)
        )
        assert_execution_price_raw(
            str(price["execution_price_mode"]), validation_mode=validation_mode
        )
        identity = build_identity_block(REPO_ROOT)
        assert_worktree_clean(
            identity.get("worktree_dirty_entries"),  # None = git 不可证 → 按脏处理
            validation_mode=validation_mode,
        )
        # R3/BLK-R2-2：四值一致（git HEAD / requested / .build_commit / build_manifest.commit），
        # 两源必须都存在、可解析、trusted=true 且 dirty=false —— 不再"任选一源"。
        assert_build_identity(
            git_head=str(identity.get("git_head") or ""),
            requested_code_commit=code_commit,
            build_commit_file=str(identity.get("build_commit_file") or ""),
            build_manifest_commit=str(identity.get("build_manifest_commit") or ""),
            build_manifest_present=bool(identity.get("build_manifest_present")),
            build_manifest_trusted=identity.get("build_manifest_trusted"),
            build_manifest_dirty=identity.get("build_manifest_dirty"),
            validation_mode=validation_mode,
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

    contract = resolve_selection_contract(config, profile="night_scan")
    manifest = build_validation_freeze(
        validation_epoch_id=str(args.epoch_id),
        code_commit=code_commit,
        git_branch=git_branch(REPO_ROOT),
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
    # R3：把构建身份四值与工作区状态写进清单（纳入 freeze_manifest_hash 覆盖）
    manifest["build_identity"] = {
        "git_head": str(identity.get("git_head") or ""),
        "build_commit_file": str(identity.get("build_commit_file") or ""),
        "build_manifest_commit": str(identity.get("build_manifest_commit") or ""),
        "build_manifest_present": bool(identity.get("build_manifest_present")),
        "build_manifest_trusted": identity.get("build_manifest_trusted"),
        "build_manifest_dirty": identity.get("build_manifest_dirty"),
        "build_manifest_path": str(identity.get("build_manifest_path") or ""),
        "code_commit": code_commit,
        "code_commit_source": code_commit_source,
        "worktree_dirty_entries": list(identity.get("worktree_dirty_entries") or []),
    }
    manifest["freeze_manifest_hash"] = freeze_manifest_hash(manifest)

    if args.print_only:
        import json

        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    path = write_validation_freeze(manifest, root=args.out)
    print(f"[freeze] 冻结清单已写入: {path}")
    print(f"[freeze] freeze_manifest_hash = {manifest['freeze_manifest_hash']}")
    print(
        "[freeze] 构建身份: git_head="
        f"{str(identity.get('git_head'))[:12]}… / .build_commit="
        f"{str(identity.get('build_commit_file') or '(缺失)')[:12]}… / build_manifest="
        f"{str(identity.get('build_manifest_commit') or '(缺失)')[:12]}…"
        f"（trusted={identity.get('build_manifest_trusted')}, "
        f"dirty={identity.get('build_manifest_dirty')}, source={code_commit_source}）"
    )
    if validation_mode == "production" and code_commit_source == "cli_override_git_unavailable":
        print(
            "[freeze] 注意：git HEAD 不可得，本清单的 code_commit 由 --code-commit 声明、"
            "并由 .build_commit + build_manifest 双源互证（审计字段 code_commit_source 已落盘）",
            file=sys.stderr,
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
