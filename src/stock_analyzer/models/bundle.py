"""Content-addressed model bundles with atomic publication and integrity checks.

一个 bundle 是 ``model_archive/<bundle_id>/`` 下的不可变目录：
- ``model.json``：完整模型工件（含指向 ``model_sidecars/`` 的相对引用）；
- ``model_sidecars/``：原生模型二进制 sidecar。

目录内容哈希采用无歧义二进制编码（长度前缀），对文件集合唯一：
``SHA256( 按相对路径排序后逐文件的 u64_be(path_len)+path_utf8+u64_be(size)+bytes )``。
发布走同文件系统 staging 目录 + 原子 rename；目标已存在且哈希相同视为幂等重放，
不同则按冲突 fail-closed 拒绝。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

from stock_analyzer.models.artifact import ModelArtifact

ARTIFACT_FILENAME = "model.json"
_SIDECAR_DIR_SUFFIX = "_sidecars"


class BundleCollisionError(RuntimeError):
    """Raised when an identical bundle id already holds different content."""


class BundleIntegrityError(RuntimeError):
    """Raised when a bundle fails load/sidecar/integrity validation."""


@dataclass(frozen=True, slots=True)
class BundlePublication:
    bundle_id: str
    content_hash: str
    root: Path
    artifact_path: Path


def compute_bundle_content_hash(root: str | Path) -> str:
    """SHA256 over the sorted file set with unambiguous length-prefixed encoding."""

    root_path = Path(root)
    if not root_path.is_dir():
        raise BundleIntegrityError(f"bundle root is not a directory: {root_path}")
    entries: list[tuple[str, Path]] = []
    for current, _dir_names, file_names in os.walk(root_path):
        for name in file_names:
            file_path = Path(current) / name
            relative = file_path.relative_to(root_path).as_posix()
            entries.append((relative, file_path))
    digest = hashlib.sha256()
    for relative, file_path in sorted(entries, key=lambda item: item[0]):
        encoded_path = relative.encode("utf-8")
        size = file_path.stat().st_size
        digest.update(struct.pack(">Q", len(encoded_path)))
        digest.update(encoded_path)
        digest.update(struct.pack(">Q", size))
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def bundle_id_from_content_hash(content_hash: str) -> str:
    normalized = content_hash.strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"invalid bundle content hash: {content_hash!r}")
    return f"model_v2_{normalized[:20]}"


def verify_artifact_integrity(
    artifact_path: str | Path,
    *,
    expected_content_hash: str = "",
) -> dict[str, object]:
    """Fail-closed artifact integrity check: loadable JSON + sidecar sha256 (+ optional hash)."""

    resolved = Path(artifact_path).expanduser().resolve()
    try:
        artifact = ModelArtifact.load(resolved)
    except Exception as exc:
        raise BundleIntegrityError(
            f"artifact not loadable: {resolved}: {exc.__class__.__name__}: {exc}"
        ) from exc

    problems = _verify_declared_sidecars(artifact_payload_path=resolved, artifact=artifact)
    if problems:
        raise BundleIntegrityError(
            "artifact sidecar integrity check failed: " + "; ".join(problems)
        )

    content_hash = ""
    if expected_content_hash.strip():
        content_hash = compute_bundle_content_hash(resolved.parent)
        if content_hash != expected_content_hash.strip().lower():
            raise BundleIntegrityError(
                "bundle content hash mismatch: "
                f"expected={expected_content_hash.strip().lower()} actual={content_hash}"
            )
    return {
        "artifact_path": str(resolved),
        "content_hash": content_hash,
        "ok": True,
    }


def publish_model_bundle(
    artifact: ModelArtifact,
    *,
    archive_root: str | Path,
) -> BundlePublication:
    """Publish one immutable content-addressed bundle atomically.

    流程：写入同文件系统 staging 目录（可加载性 + sidecar sha256 校验）→ 计算
    内容哈希与 bundle id → 原子 rename 到最终目录。最终目录已存在且内容相同
    视为幂等重放；不同内容一律冲突拒绝（fail-closed）。
    """

    root = Path(archive_root)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging_{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        artifact.save(staging / ARTIFACT_FILENAME)
        # 发布前校验：产物必须可加载且声明的 sidecar 完整。
        staged_artifact_path = staging / ARTIFACT_FILENAME
        reloaded = ModelArtifact.load(staged_artifact_path)
        problems = _verify_declared_sidecars(
            artifact_payload_path=staged_artifact_path,
            artifact=reloaded,
        )
        if problems:
            raise BundleIntegrityError(
                "staged bundle failed integrity validation: " + "; ".join(problems)
            )
        return _finalize_staged_bundle(staging=staging, root=root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def publish_bundle_from_artifact_directory(
    source_artifact_path: str | Path,
    *,
    archive_root: str | Path,
) -> BundlePublication:
    """Bundle an existing on-disk artifact (JSON + sidecars) without re-serializing.

    种子引导专用：直接拷贝原 JSON 与其 sidecar 目录，保留原生二进制不被
    ``ModelArtifact.save`` 二次序列化丢弃。JSON 内部以 ``<旧stem>_sidecars/``
    开头的 sidecar 引用统一改写为 ``model_sidecars/``。
    """

    source = Path(source_artifact_path).expanduser().resolve()
    if not source.is_file():
        raise BundleIntegrityError(f"source artifact is not a file: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid source artifact payload: {source}")
    legacy_sidecar_prefix = f"{source.stem}{_SIDECAR_DIR_SUFFIX}/"

    root = Path(archive_root)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging_{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        for model_key in ("lgbm_model", "xgb_model"):
            model_payload = payload.get(model_key)
            if not isinstance(model_payload, dict):
                continue
            for sidecar_key in ("sidecar_path", "fallback_sidecar_path"):
                sidecar_value = model_payload.get(sidecar_key)
                if (
                    isinstance(sidecar_value, str)
                    and sidecar_value.startswith(legacy_sidecar_prefix)
                ):
                    model_payload[sidecar_key] = sidecar_value.replace(
                        legacy_sidecar_prefix,
                        f"model{_SIDECAR_DIR_SUFFIX}/",
                        1,
                    )
        target_json = staging / ARTIFACT_FILENAME
        target_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        source_sidecar_dir = source.parent / f"{source.stem}{_SIDECAR_DIR_SUFFIX}"
        if source_sidecar_dir.is_dir():
            shutil.copytree(
                source_sidecar_dir,
                staging / f"model{_SIDECAR_DIR_SUFFIX}",
            )
        reloaded = ModelArtifact.load(target_json)
        problems = _verify_declared_sidecars(
            artifact_payload_path=target_json,
            artifact=reloaded,
        )
        if problems:
            raise BundleIntegrityError(
                "staged seed bundle failed integrity validation: " + "; ".join(problems)
            )
        return _finalize_staged_bundle(staging=staging, root=root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _finalize_staged_bundle(*, staging: Path, root: Path) -> BundlePublication:
    """Hash → id → atomic rename，含幂等重放与冲突 fail-closed。"""

    content_hash = compute_bundle_content_hash(staging)
    bundle_id = bundle_id_from_content_hash(content_hash)
    final_root = root / bundle_id
    if final_root.exists():
        existing_hash = compute_bundle_content_hash(final_root)
        if existing_hash == content_hash:
            return BundlePublication(
                bundle_id=bundle_id,
                content_hash=content_hash,
                root=final_root,
                artifact_path=final_root / ARTIFACT_FILENAME,
            )
        raise BundleCollisionError(
            "bundle id collision with different content: "
            f"bundle_id={bundle_id} staged_hash={content_hash} "
            f"existing_hash={existing_hash} root={final_root}"
        )
    try:
        os.rename(staging, final_root)
    except OSError:
        # 并发竞态：目标目录在检查后被他人创建——内容一致则幂等，否则冲突。
        if not final_root.exists():
            raise
        existing_hash = compute_bundle_content_hash(final_root)
        if existing_hash != content_hash:
            raise BundleCollisionError(
                "bundle id collision with different content: "
                f"bundle_id={bundle_id} staged_hash={content_hash} "
                f"existing_hash={existing_hash} root={final_root}"
            ) from None
    return BundlePublication(
        bundle_id=bundle_id,
        content_hash=content_hash,
        root=final_root,
        artifact_path=final_root / ARTIFACT_FILENAME,
    )


def build_release_alias_payload(
    *,
    bundle_root: str | Path,
    registry_model_id: str,
    content_hash: str,
    alias_parent: str | Path,
) -> dict[str, object]:
    """Build an alias artifact payload whose sidecars resolve into the immutable bundle."""

    source_payload_path = Path(bundle_root) / ARTIFACT_FILENAME
    payload = json.loads(source_payload_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid bundle artifact payload: {source_payload_path}")
    alias_parent_path = Path(alias_parent)
    for model_key in ("lgbm_model", "xgb_model"):
        model_payload = payload.get(model_key)
        if not isinstance(model_payload, dict):
            continue
        for sidecar_key in ("sidecar_path", "fallback_sidecar_path"):
            sidecar_value = model_payload.get(sidecar_key)
            if not isinstance(sidecar_value, str) or not sidecar_value.strip():
                continue
            sidecar_absolute = (Path(bundle_root) / sidecar_value).resolve()
            relative = os.path.relpath(sidecar_absolute, start=alias_parent_path.resolve())
            model_payload[sidecar_key] = Path(relative).as_posix()
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    # 别名自描述：运行期 mode_details 可核对当前加载的模型身份与 bundle 内容。
    metadata["registry_model_id"] = str(registry_model_id)
    metadata["bundle_content_hash"] = str(content_hash)
    return payload


def latest_bundle_artifact_path(archive_root: str | Path) -> Path | None:
    """返回归档中最新的 bundle 工件路径（按修改时间）；无 bundle 时 None。

    供“别名尚未由发布流程写入”的场景（如训练后立即生成验收报告）回退使用。
    """

    root = Path(archive_root)
    if not root.is_dir():
        return None
    candidates = [
        item
        for item in root.glob(f"model_v2_*/{ARTIFACT_FILENAME}")
        if item.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _verify_declared_sidecars(
    *,
    artifact_payload_path: Path,
    artifact: ModelArtifact,
) -> list[str]:
    problems: list[str] = []
    parent = artifact_payload_path.parent
    for model_name, model_payload in (
        ("lgbm", artifact.lgbm_model),
        ("xgb", artifact.xgb_model),
    ):
        sidecar_relative = str(model_payload.get("sidecar_path", "")).strip()
        if not sidecar_relative:
            continue
        # 先拒绝逃逸出 bundle 根目录的相对路径（含 .. 与绝对路径），再查存在性。
        if ".." in Path(sidecar_relative).parts or Path(sidecar_relative).is_absolute():
            problems.append(f"{model_name}:unsafe_sidecar_path:{sidecar_relative}")
            continue
        expected_hash = str(model_payload.get("sidecar_sha256", "")).strip().lower()
        sidecar_path = (parent / sidecar_relative).resolve()
        if not sidecar_path.is_file():
            problems.append(f"{model_name}:sidecar_missing:{sidecar_relative}")
            continue
        actual_hash = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            problems.append(f"{model_name}:sidecar_sha256_mismatch:{sidecar_relative}")
    return problems
