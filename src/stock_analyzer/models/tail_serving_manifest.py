"""尾盘 challenger 的在服清单：让影子链路能说清"我现在用的到底是哪份净盈利模型"。

为什么**不**复用 ``models/serving_manifest.py``（那是 ADR-001 的信任边界，改它的
schema 要单独决策）：``model_serving_manifest.v1`` 里**一个 commit 字段都没有**，
所以对着真实清单，尾盘模型身份永远只能读成 ``training_commit_unknown`` ⇒ 0 只。
计划 §3.3 要求"新模型原生训练或身份验证失败时停止，不静默切换替代模型"，§3.4 要求
"自动学习只生成 challenger，正式模型更新仍须经过验证和人工发布"——这两条都需要一个
**属于尾盘路径自己**的身份文件，而不是一行注释。

本模块只做三件事，且都跟"看起来对"无关：

1. ``build_tail_serving_manifest`` —— 工件事实（内容哈希 / 字节数 / mtime）从
   **实际文件**算出来，不接受调用方声明的哈希；身份字段读不到就直接拒写。
2. ``verify_tail_serving_manifest`` —— 复核时**重新哈希**被引用的工件：清单写着
   ``sha256:a``、盘上的文件是 ``sha256:b``，就是工件被换过，而不是"反正清单里有 id"。
   契约摘要、标签口径、概率字段同样逐字段比对，因为影子留档要回答的是
   "这批成交按哪套 TP/SL/持有期解释"。
3. 状态只允许 ``challenger``：晋升是人工发布动作，清单不自带"我已经在服"的权力。

所有 verify 失败都是**字符串**，不抛异常 —— 上层要的是
``model_identity.recording_failures`` 里点名道姓的原因（§3.1 记录失败必须可见）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    TrendStrategyContract,
)

TAIL_SERVING_MANIFEST_SCHEMA = "tail_model_serving_manifest.v1"
TAIL_SERVING_MANIFEST_FILENAME = "tail_model_serving_manifest.json"
DEFAULT_TAIL_SERVING_MANIFEST_PATH = f"artifacts/research/{TAIL_SERVING_MANIFEST_FILENAME}"

#: v1 只承认 challenger。正式在服由人工发布决定，不由这份文件自证。
TAIL_STATUS_CHALLENGER = "challenger"
TAIL_ALLOWED_STATUSES = frozenset({TAIL_STATUS_CHALLENGER})

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


class TailManifestError(ValueError):
    """清单构造输入不可验证（缺身份、工件读不出、口径与契约不一致）。"""


def tail_artifact_facts(path: str | Path) -> dict[str, Any]:
    """工件事实来自文件本身；哈希是内容哈希，不是"文件名看起来对"。"""
    resolved = Path(path)
    exists = resolved.is_file()
    if not exists:
        return {
            "artifact_path": str(resolved),
            "artifact_exists": False,
            "artifact_content_hash": "",
            "artifact_size_bytes": 0,
            "artifact_modified_at": "",
        }
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    stat = resolved.stat()
    return {
        "artifact_path": str(resolved),
        "artifact_exists": True,
        "artifact_content_hash": f"sha256:{digest.hexdigest()}",
        "artifact_size_bytes": int(stat.st_size),
        "artifact_modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=UTC
        ).isoformat(),
    }


def build_tail_serving_manifest(
    *,
    artifact_path: str | Path,
    model_id: str,
    training_code_commit: str,
    artifact: Mapping[str, Any] | None = None,
    label_policy_id: str = "",
    training_manifest_id: str = "",
    feature_compute_version: int | str = "",
    probability_field: str = NET_PROFIT_PROBABILITY_FIELD,
    split: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    training_code_commit_source: str = "",
    status: str = TAIL_STATUS_CHALLENGER,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """构造一份尾盘 challenger 清单；任何身份字段不可证就拒写，不写"以后补"。"""
    facts = tail_artifact_facts(artifact_path)
    if not facts["artifact_exists"]:
        raise TailManifestError(f"工件不存在，无法绑定身份: {artifact_path}")
    normalized_model_id = str(model_id or "").strip()
    if not normalized_model_id:
        raise TailManifestError("model_id must not be empty")
    commit = str(training_code_commit or "").strip().lower()
    if not _COMMIT_RE.match(commit):
        raise TailManifestError(f"training_code_commit 不可证: {training_code_commit!r}")
    if str(status).strip() not in TAIL_ALLOWED_STATUSES:
        raise TailManifestError(
            f"只允许生成 challenger 清单（§3.4 正式更新须人工发布），收到 status={status!r}"
        )
    # 标签口径/契约摘要优先取**工件自己声明的**：训练时用了什么口径是工件的事实，
    # 由调用方临时传参等于让发布动作重新解释历史样本。
    declared = dict(artifact or {})
    resolved_label_policy_id = str(
        label_policy_id or declared.get("label_policy_id") or ""
    ).strip()
    if not resolved_label_policy_id.startswith("label_policy_v4_"):
        raise TailManifestError(
            f"清单必须绑定净盈利尾盘标签口径（label_policy_v4_*），收到 "
            f"{resolved_label_policy_id!r}"
        )
    declared_digest = str(declared.get("contract_digest") or "").strip()
    if declared_digest and declared_digest != contract.digest():
        raise TailManifestError(
            f"工件是按另一套契约训练的（{declared_digest} != {contract.digest()}），"
            "不能进当前尾盘链路的在服清单"
        )
    stamp = generated_at or datetime.now(UTC)
    return {
        "schema": TAIL_SERVING_MANIFEST_SCHEMA,
        "status": str(status).strip(),
        "generated_at": stamp.isoformat(),
        "serving": {
            **facts,
            # 训练器自带的摘要与文件内容哈希是两个独立事实，都留档：前者说明
            # "这份参数的逻辑身份"，后者说明"盘上这个字节文件没被换过"。
            "artifact_internal_digest": str(
                declared.get("artifact_digest") or ""
            ).strip(),
            "probability_field": str(probability_field).strip(),
        },
        "identity": {
            "model_id": normalized_model_id,
            "training_code_commit": commit,
            "training_code_commit_source": str(training_code_commit_source).strip(),
            "training_manifest_id": str(training_manifest_id).strip(),
            "feature_compute_version": str(feature_compute_version).strip(),
        },
        "label": {"label_policy_id": resolved_label_policy_id},
        "contract": {
            "contract_version": contract.contract_version,
            "contract_digest": contract.digest(),
            "holding_days": int(contract.holding_days),
            "take_profit_pct": float(contract.take_profit_pct),
            "stop_loss_pct": float(contract.stop_loss_pct),
            "execution_price_basis": contract.execution_price_basis,
        },
        "features": {"feature_names": [str(n) for n in (
            (split or {}).get("feature_names")
            if split is not None else declared.get("feature_names")
        ) or ()]},
        "validation": {
            "split": dict(split or {}),
            "metrics": dict(metrics or {}),
        },
    }


def write_tail_serving_manifest(
    path: str | Path, payload: Mapping[str, Any]
) -> Path:
    """原子落盘（临时文件 + os.replace）：半个清单不该被下一次影子运行读到。"""
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temp = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temp, resolved)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return resolved


def read_tail_serving_manifest(path: str | Path) -> dict[str, Any]:
    """读不到就返回空 dict —— "没有专属清单"是一种合法状态，不是异常。"""
    resolved = Path(path)
    if not resolved.is_file():
        return {}
    try:
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def verify_tail_serving_manifest(
    payload: Mapping[str, Any],
    *,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    base_dir: str | Path | None = None,
    recheck_artifact: bool = True,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """逐字段复核 challenger 清单，返回 ``(清单, 失败原因)``；不抛异常、不猜。"""
    if not payload:
        return None, ("tail_serving_manifest_empty",)
    failures: list[str] = []
    schema = str(payload.get("schema") or "").strip()
    if schema != TAIL_SERVING_MANIFEST_SCHEMA:
        failures.append(f"tail_serving_manifest_schema_mismatch:{schema or 'none'}")
    status = str(payload.get("status") or "").strip()
    if status not in TAIL_ALLOWED_STATUSES:
        failures.append(f"tail_serving_manifest_status_not_challenger:{status or 'none'}")

    serving = dict(payload.get("serving") or {})
    identity = dict(payload.get("identity") or {})
    label = dict(payload.get("label") or {})
    contract_block = dict(payload.get("contract") or {})

    if not str(identity.get("model_id") or "").strip():
        failures.append("tail_serving_manifest_model_id_missing")
    commit = str(identity.get("training_code_commit") or "").strip().lower()
    if not _COMMIT_RE.match(commit):
        failures.append(f"tail_serving_manifest_training_commit_unverified:{commit or 'none'}")
    label_policy_id = str(label.get("label_policy_id") or "").strip()
    if not label_policy_id.startswith("label_policy_v4_"):
        failures.append(
            f"tail_serving_manifest_label_policy_not_v4:{label_policy_id or 'none'}"
        )
    recorded_digest = str(contract_block.get("contract_digest") or "").strip()
    if recorded_digest != contract.digest():
        failures.append(
            "tail_serving_manifest_contract_digest_mismatch:"
            f"{recorded_digest or 'none'}!={contract.digest()}"
        )
    probability_field = str(serving.get("probability_field") or "").strip()
    if probability_field != NET_PROFIT_PROBABILITY_FIELD:
        failures.append(
            f"tail_serving_manifest_probability_field_mismatch:{probability_field or 'none'}"
        )

    if recheck_artifact:
        facts = tail_artifact_facts(_resolve(serving.get("artifact_path"), base_dir))
        if not facts["artifact_exists"]:
            failures.append(
                f"tail_serving_manifest_artifact_missing:{facts['artifact_path'] or 'none'}"
            )
        else:
            recorded_hash = str(serving.get("artifact_content_hash") or "").strip()
            if recorded_hash != facts["artifact_content_hash"]:
                failures.append(
                    "tail_serving_manifest_artifact_content_hash_mismatch:"
                    f"declared={recorded_hash or 'none'} actual={facts['artifact_content_hash']}"
                )
    return dict(payload), tuple(dict.fromkeys(failures))


def _resolve(value: object, base_dir: str | Path | None) -> Path:
    path = Path(str(value or ""))
    if base_dir is not None and not path.is_absolute():
        return Path(base_dir) / path
    return path


__all__ = [
    "DEFAULT_TAIL_SERVING_MANIFEST_PATH",
    "TAIL_ALLOWED_STATUSES",
    "TAIL_SERVING_MANIFEST_FILENAME",
    "TAIL_SERVING_MANIFEST_SCHEMA",
    "TAIL_STATUS_CHALLENGER",
    "TailManifestError",
    "build_tail_serving_manifest",
    "read_tail_serving_manifest",
    "tail_artifact_facts",
    "verify_tail_serving_manifest",
    "write_tail_serving_manifest",
]
