"""第一轮净盈利尾盘模型的训练入口：样本 → 工件（→ challenger 清单）。

```bash
python scripts/train_tail_net_profit_model.py \
    --samples artifacts/research/tail_label_samples.jsonl \
    --features excess_ret_20,atr14_pct \
    --model-id trend-tail-lgbm-2026q4 \
    --out artifacts/research/models/tail_lr_v1.json \
    --manifest-out artifacts/research/tail_model_serving_manifest.json \
    --registry-db data/learning_protocol.duckdb
```

这一条把 §3.3 的"训练一个与选股目标一致的候选模型"接到能真正落盘的产品上：
训练前 `tail_net_profit_trainer` 已经把特征白名单、日期折分与 embargo、单类窗口、
校准段方向这些前置检查做掉了，训练后 `tail_model_artifact` 把产物变成**可加载**的工件，
`tail_serving_manifest` 再把工件绑成影子链路能复核的 challenger 身份。三段共用一套
判定，不在这里再实现第二份选股或成本逻辑。

LightGBM 需要调用方注入原生 booster（``--kind lightgbm`` 走不通就直接失败），
**不会**降级成逻辑回归 —— §3.3 明令禁止静默替换模型。

退出码是**真实退出码**：

- ``0`` 工件（与可选清单）已写出
- ``3`` 训练前置条件不满足（样本/折分/方向/特征白名单），原文写进报告
- ``5`` 训练身份不可证，或工件/清单写出失败
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
from stock_analyzer.labels.tail_net_profit import (  # noqa: E402
    tail_label_policy_record,
    verify_tail_label_policy,
)
from stock_analyzer.models.tail_model_artifact import (  # noqa: E402
    TailArtifactError,
    artifact_identity_view,
    serialize_tail_artifact,
    write_tail_artifact,
)
from stock_analyzer.models.tail_net_profit_trainer import (  # noqa: E402
    KIND_LIGHTGBM,
    KIND_LOGISTIC,
    TailModelSpec,
    TailTrainingError,
    train_tail_net_profit_model,
)
from stock_analyzer.models.tail_serving_manifest import (  # noqa: E402
    TailManifestError,
    build_tail_serving_manifest,
    write_tail_serving_manifest,
)

RC_OK = 0
RC_TRAINING_STOPPED = 3
RC_IDENTITY_FAILED = 5


def _read_rows(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"样本行必须是对象: {line[:80]}")
        rows.append(record)
    if not rows:
        raise ValueError(f"样本文件为空: {path}")
    return rows


def _training_identity() -> tuple[str, str, list[str]]:
    """训练身份走共享解析入口；本 CLI 不自行读取 git HEAD（ADR-001 §7.2）。"""
    from stock_analyzer.alpha_v2.validation.runtime_identity import (
        resolve_runtime_code_identity,
    )

    identity = resolve_runtime_code_identity(
        _PROJECT_ROOT, validation_mode="production", require_build_identity=False
    )
    violations = [str(item) for item in identity.violations]
    commit = str(identity.code_commit or "").strip()
    if not identity.identity_verified or not commit or commit == "unknown":
        violations.append(f"training_identity_unverified:{commit or 'empty'}")
    return commit, str(identity.code_commit_source or identity.identity_source or ""), violations


def _feature_compute_version() -> int:
    from stock_analyzer.feature.engineer import FEATURE_COMPUTE_VERSION

    return int(FEATURE_COMPUTE_VERSION)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, help="JSONL：每行一条含特征与标签的样本")
    parser.add_argument("--features", required=True, help="逗号分隔特征名（训练白名单会复核）")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--kind", default=KIND_LOGISTIC,
                        choices=(KIND_LOGISTIC, KIND_LIGHTGBM))
    parser.add_argument("--label-field", default="label")
    parser.add_argument("--date-field", default="entry_date")
    parser.add_argument("--out", required=True, help="工件写出路径")
    parser.add_argument("--manifest-out", default="",
                        help="可选：同时写出 challenger 在服清单")
    parser.add_argument("--registry-db", default="",
                        help="给了就强制标签口径已在 registry 注册且逐字段一致")
    parser.add_argument("--report-out", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    contract = DEFAULT_TREND_CONTRACT
    feature_names = [name.strip() for name in args.features.split(",") if name.strip()]
    if not feature_names:
        print("--features 不能为空", file=sys.stderr)
        return RC_TRAINING_STOPPED

    try:
        rows = _read_rows(args.samples)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"样本读不出来: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_TRAINING_STOPPED

    commit, commit_source, violations = _training_identity()
    if violations:
        print(f"训练身份不可证: {violations}", file=sys.stderr)
        return RC_IDENTITY_FAILED

    label_policy_id = tail_label_policy_record(contract).label_policy_id
    if args.registry_db:
        from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry

        _, failures = verify_tail_label_policy(
            LabelPolicyRegistry(args.registry_db),
            label_policy_id=label_policy_id, contract=contract,
        )
        if failures:
            print(f"标签口径未绑定: {list(failures)}", file=sys.stderr)
            return RC_IDENTITY_FAILED

    spec = TailModelSpec(kind=args.kind)
    try:
        payload = train_tail_net_profit_model(
            rows=rows, feature_names=feature_names, spec=spec,
            label_field=args.label_field, date_field=args.date_field,
            contract=contract, model_id=args.model_id, training_commit=commit,
            feature_compute_version=_feature_compute_version(),
            label_policy_id=label_policy_id,
        )
    except TailTrainingError as exc:
        print(f"训练按要求停止: {exc}", file=sys.stderr)
        return RC_TRAINING_STOPPED

    try:
        artifact = serialize_tail_artifact(payload, contract=contract)
        artifact_path = write_tail_artifact(args.out, artifact)
    except (TailArtifactError, OSError) as exc:
        print(f"工件写出失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_IDENTITY_FAILED

    result: dict[str, Any] = {
        "artifact_path": str(artifact_path),
        "kind": artifact["identity"]["kind"],
        "model_id": artifact["identity"]["model_id"],
        "training_code_commit": commit,
        "training_code_commit_source": commit_source,
        "label_policy_id": artifact["identity"]["label_policy_id"],
        "artifact_digest": artifact["artifact_digest"],
        "split": artifact["split"],
        "metrics": artifact["metrics"],
        "sample_rows": len(rows),
        "shadow_only": True,
    }

    if args.manifest_out:
        try:
            declared = artifact_identity_view(artifact)
            manifest = build_tail_serving_manifest(
                artifact_path=artifact_path, model_id=args.model_id,
                training_code_commit=commit, training_code_commit_source=commit_source,
                artifact=declared, contract=contract,
                training_manifest_id=str(declared.get("training_manifest_id") or ""),
                split=declared["split"], metrics=declared["metrics"],
            )
            result["manifest_path"] = str(
                write_tail_serving_manifest(args.manifest_out, manifest)
            )
        except (TailManifestError, OSError) as exc:
            print(f"清单写出失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            return RC_IDENTITY_FAILED

    if args.report_out:
        report = Path(args.report_out)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                          encoding="utf-8")
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
