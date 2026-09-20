"""在服模型清单（S05 / 原 P0-02）：serving 的独立身份真相源。

**为什么需要它**：registry 记录"训练/登记了什么"，alias 文件记录"某次发布切换到了
什么"，但两者都不直接回答"**现在生产在跑哪一份**"。2026-09 的实证是 registry 无
champion、15/21 条 ``artifact_uri`` 指向数据集清单、alias 与 immutable bundle 混用
（蓝图 §2.5 / §2.10）——只靠 alias + registry 无法把"在服身份"钉住。

本模块写出 ``artifacts/model_serving_manifest.json``：

- 事实（artifact 路径 / 内容哈希 / created_at / feature schema / label policy）来自
  **实际文件**（``models/identity.load_artifact_facts``）；
- registry 只作补充（model_id / lifecycle / 登记的哈希），并以 ``registry_`` 前缀标注；
- 显式区分 ``authority``：哪个文件是当前权威在服工件（含 alias 路径与哈希），
  避免"跟踪在仓库里的开发工件 vs NAS 在服工件"这类混淆（Codex N5）；
- alias **仍然兼容**（发布流程继续用它切换），但它不是身份真相源：真相源是
  本清单里的内容哈希 + registry 对账状态。

写入是最佳努力（best-effort）：清单写不出来时必须能被观测（返回 error），但不得
把已完成发布的别名切换回滚掉——身份缺失要可见，不是让服务起不来。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.models.identity import build_model_identity_report, load_artifact_facts

SERVING_MANIFEST_SCHEMA = "model_serving_manifest.v1"
SERVING_MANIFEST_FILENAME = "model_serving_manifest.json"
DEFAULT_SERVING_MANIFEST_PATH = f"artifacts/{SERVING_MANIFEST_FILENAME}"


def build_serving_manifest(
    *,
    artifact_path: str | Path,
    registry: object | None = None,
    alias_path: str | Path = "",
    source: str = "",
    generated_at: str | None = None,
) -> dict[str, object]:
    """构造在服模型清单（纯计算，不落盘）。"""
    facts = load_artifact_facts(artifact_path)
    report = build_model_identity_report(
        facts,
        registry=registry,
        claimed_content_hash=facts.get("claimed_content_hash", ""),
    )
    payload: dict[str, object] = {
        "schema": SERVING_MANIFEST_SCHEMA,
        "generated_at": generated_at or datetime.now().astimezone().isoformat(),
        "source": str(source).strip(),
        "serving": {
            "artifact_path": str(report.get("artifact_uri", "") or ""),
            "artifact_exists": bool(report.get("artifact_exists", False)),
            "artifact_content_hash": str(report.get("artifact_content_hash", "") or ""),
            "artifact_created_at": str(report.get("artifact_created_at", "") or ""),
            "feature_schema_id": str(report.get("feature_schema_id", "") or ""),
            "feature_schema_hash": str(report.get("feature_schema_hash", "") or ""),
            "label_policy_id": str(report.get("label_policy_id", "") or ""),
            "label_policy_hash": str(report.get("label_policy_hash", "") or ""),
            "dataset_manifest_id": str(report.get("dataset_manifest_id", "") or ""),
            "alias_path": str(alias_path or ""),
        },
        "registry": {
            "model_id": str(report.get("registry_model_id", "") or ""),
            "content_hash": str(report.get("registry_content_hash", "") or ""),
            "identity_status": str(report.get("status", "") or ""),
            "identity_detail": str(report.get("detail", "") or ""),
            "identity_verified": bool(report.get("identity_verified", False)),
            "registry_error": str(report.get("registry_error", "") or ""),
        },
        # 权威口径（Codex N5）：仓库里受跟踪的 artifacts/model_v1.json 是开发工件，
        # 与 NAS 在服工件可能不同件；"谁权威"必须由这份清单 + 内容哈希回答，
        # 而不是由"路径看起来像"回答。
        "authority": {
            "authoritative_artifact": str(report.get("artifact_uri", "") or ""),
            "authoritative_content_hash": str(report.get("artifact_content_hash", "") or ""),
            "basis": "serving_artifact_content_hash",
            "alias_is_identity_source": False,
            "note": (
                "alias 仅用于发布切换与加载路径；身份以本清单的内容哈希为准。"
                "仓库内其它同名工件（如受跟踪的开发工件）不构成在服身份。"
            ),
        },
        "research_fail_closed": bool(report.get("research_fail_closed", False)),
    }
    return payload


def write_serving_manifest(
    *,
    artifact_path: str | Path,
    registry: object | None = None,
    alias_path: str | Path = "",
    source: str = "",
    manifest_path: str | Path | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    """写清单并返回 ``{"written": bool, "path": str, "error": str, "payload": dict}``。

    最佳努力：任何写盘异常都只体现在返回值里（``written=False`` + ``error``），
    由调用方决定记录/报警——不得因为清单写不出来而回滚已完成的别名切换。
    """
    target = Path(manifest_path) if manifest_path else Path(DEFAULT_SERVING_MANIFEST_PATH)
    try:
        payload = build_serving_manifest(
            artifact_path=artifact_path,
            registry=registry,
            alias_path=alias_path,
            source=source,
            generated_at=generated_at,
        )
    except Exception as exc:  # noqa: BLE001 - 清单构造失败不得抛出到发布流程
        return {
            "written": False,
            "path": str(target),
            "error": f"build_failed:{type(exc).__name__}:{exc}",
            "payload": {},
        }
    try:
        write_json_atomic(target, payload)
    except Exception as exc:  # noqa: BLE001 - 同上
        return {
            "written": False,
            "path": str(target),
            "error": f"write_failed:{type(exc).__name__}:{exc}",
            "payload": payload,
        }
    return {"written": True, "path": str(target), "error": "", "payload": payload}


def read_serving_manifest(
    manifest_path: str | Path | None = None,
) -> dict[str, object] | None:
    """读取在服清单；不存在或不可解析时返回 None（调用方自行决定降级）。"""
    path = Path(manifest_path) if manifest_path else Path(DEFAULT_SERVING_MANIFEST_PATH)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "DEFAULT_SERVING_MANIFEST_PATH",
    "SERVING_MANIFEST_FILENAME",
    "SERVING_MANIFEST_SCHEMA",
    "build_serving_manifest",
    "read_serving_manifest",
    "write_serving_manifest",
]
