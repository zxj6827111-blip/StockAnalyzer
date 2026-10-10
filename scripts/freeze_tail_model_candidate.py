"""把一份已训练的尾盘净盈利工件冻结成 **challenger** 在服清单。

```bash
python scripts/freeze_tail_model_candidate.py \
    --artifact artifacts/research/models/tail_lr_v1.json \
    --registry-db data/learning_protocol.duckdb \
    --out artifacts/research/tail_model_serving_manifest.json
```

为什么要有这个命令：影子链路的模型身份此前只能读 ``model_serving_manifest.v1``，
而那份清单**一个 commit 字段都没有**，于是尾盘身份永远 ``training_commit_unknown``
⇒ 0 只（fail-closed 正确，但链路是黑的，§4 的影子验证根本不会开始累积成交）。
扩那份清单属于 ADR-001 的信任边界变更；本命令不改它，只**新增**一条属于尾盘路径
自己的身份文件（``models/tail_serving_manifest.py``）。

产出的清单状态只会是 ``challenger``：正式晋升是人工发布动作，这份文件不自带那个权力
（改进计划 §3.4）。

退出码是**真实退出码**：

- ``0`` 清单已写出且自检通过
- ``3`` 标签口径没注册进 registry —— 清单里的 label_policy_id 无法逐字段核对
- ``4`` 工件声明的契约摘要与在服契约不一致（那是另一套 TP/SL/持有期的模型）
- ``5`` 训练身份不可证 / 工件读不出 / 清单自检失败 —— 停，不换模型
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.labels.tail_net_profit import verify_tail_label_policy  # noqa: E402
from stock_analyzer.models.tail_model_artifact import (  # noqa: E402
    artifact_identity_view,
)
from stock_analyzer.models.tail_serving_manifest import (  # noqa: E402
    DEFAULT_TAIL_SERVING_MANIFEST_PATH,
    TailManifestError,
    build_tail_serving_manifest,
    read_tail_serving_manifest,
    verify_tail_serving_manifest,
    write_tail_serving_manifest,
)

RC_OK = 0
RC_LABEL_UNBOUND = 3
RC_CONTRACT_MISMATCH = 4
RC_IDENTITY_FAILED = 5


def _resolve_training_commit(*, validation_mode: str) -> tuple[str, str, list[str]]:
    """训练身份走**唯一的**运行身份解析入口；本 CLI 不自行解析 git HEAD（ADR-001 §7.2）。"""
    from stock_analyzer.alpha_v2.validation.runtime_identity import (
        resolve_runtime_code_identity,
    )

    identity = resolve_runtime_code_identity(
        _PROJECT_ROOT, validation_mode=validation_mode, require_build_identity=False
    )
    violations = [str(item) for item in identity.violations]
    if not identity.identity_verified or not str(identity.code_commit).strip():
        violations.append(f"training_identity_unverified:{identity.code_commit or 'empty'}")
    return str(identity.code_commit or "").strip(), str(
        identity.code_commit_source or identity.identity_source or ""
    ), violations


def _load_artifact(path: str) -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.is_file():
        raise FileNotFoundError(f"工件不存在: {artifact_path}")
    loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"工件必须是 JSON 对象: {artifact_path}")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True,
                        help="训练器落盘的净盈利模型工件（JSON）")
    parser.add_argument("--model-id", default="", help="默认取工件里的 model_id")
    parser.add_argument("--training-manifest-id", default="",
                        help="训练数据集清单 id（工件里的 dataset_manifest_id 优先）")
    parser.add_argument("--registry-db", default="",
                        help="给了就强制核对 label_policy_id 已注册且口径逐字段一致")
    parser.add_argument("--out", default=DEFAULT_TAIL_SERVING_MANIFEST_PATH)
    parser.add_argument("--validation-mode", default="production",
                        choices=("production", "rehearsal", "test"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    contract = DEFAULT_TREND_CONTRACT
    try:
        artifact = artifact_identity_view(_load_artifact(args.artifact))
    except (OSError, ValueError) as exc:
        print(f"工件读不出来: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_IDENTITY_FAILED

    declared_digest = str(artifact.get("contract_digest") or "").strip()
    if declared_digest and declared_digest != contract.digest():
        print(f"工件按另一套契约训练: {declared_digest} != {contract.digest()}",
              file=sys.stderr)
        return RC_CONTRACT_MISMATCH

    label_policy_id = str(artifact.get("label_policy_id") or "").strip()
    if args.registry_db:
        from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry

        _, failures = verify_tail_label_policy(
            LabelPolicyRegistry(args.registry_db),
            label_policy_id=label_policy_id, contract=contract,
        )
        if failures:
            print(f"标签口径未绑定: {list(failures)}", file=sys.stderr)
            return RC_LABEL_UNBOUND

    commit, commit_source, violations = _resolve_training_commit(
        validation_mode=args.validation_mode
    )
    if violations:
        print(f"训练身份不可证: {violations}", file=sys.stderr)
        return RC_IDENTITY_FAILED

    out_path = Path(args.out)
    try:
        payload = build_tail_serving_manifest(
            artifact_path=args.artifact,
            model_id=args.model_id or str(artifact.get("model_id") or ""),
            training_code_commit=commit,
            training_code_commit_source=commit_source,
            artifact=artifact,
            label_policy_id=label_policy_id,
            training_manifest_id=args.training_manifest_id or str(
                artifact.get("training_manifest_id")
                or artifact.get("dataset_manifest_id") or ""
            ),
            feature_compute_version=artifact.get("feature_compute_version", ""),
            probability_field=str(artifact.get("probability_field") or ""),
            split=artifact.get("split") if isinstance(artifact.get("split"), dict) else None,
            metrics=artifact.get("metrics") if isinstance(artifact.get("metrics"), dict) else None,
            contract=contract,
        )
        write_tail_serving_manifest(out_path, payload)
    except TailManifestError as exc:
        print(f"清单拒绝写出: {exc}", file=sys.stderr)
        return RC_IDENTITY_FAILED

    # 写完立刻以**读者**的身份复核一遍：产出的那一刻就验不过，等于没产出。
    _, self_check = verify_tail_serving_manifest(
        read_tail_serving_manifest(out_path), contract=contract
    )
    if self_check:
        print(f"清单自检失败: {list(self_check)}", file=sys.stderr)
        return RC_IDENTITY_FAILED

    result = {
        "manifest_path": str(out_path),
        "status": payload["status"],
        "model_id": payload["identity"]["model_id"],
        "artifact_content_hash": payload["serving"]["artifact_content_hash"],
        "training_code_commit": payload["identity"]["training_code_commit"],
        "label_policy_id": payload["label"]["label_policy_id"],
        "contract_digest": payload["contract"]["contract_digest"],
        "shadow_only": True,
    }
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
