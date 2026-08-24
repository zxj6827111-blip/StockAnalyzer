"""把种子模型工件引导为内容寻址 bundle 并原子写运行时别名（幂等）。

容器首启流程：seed JSON（含可选的 <stem>_sidecars/）→ 不可变 bundle（
model_archive/<bundle_id>/）→ 别名 JSON（sidecar 指向 bundle 内部）。
别名已有效时直接跳过；别名损坏或缺失时从 seed 重建。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    seed_path = Path(os.environ.get("SEED_MODEL_PATH", "/app/bootstrap_seed/model_v1.json"))
    runtime_dir = Path(os.environ.get("RUNTIME_ARTIFACT_DIR", "/app/artifacts"))
    alias_path = Path(
        os.environ.get("RUNTIME_MODEL_PATH", str(runtime_dir / "model_v1.json"))
    )
    if not seed_path.is_file():
        print(f"[seed-bootstrap] no seed artifact at {seed_path}; skip", flush=True)
        return 0

    # 延迟导入：保证 --help/无依赖环境下的失败信息清晰。
    from stock_analyzer.models.bundle import (
        build_release_alias_payload,
        publish_bundle_from_artifact_directory,
        verify_artifact_integrity,
    )

    if alias_path.is_file():
        try:
            verify_artifact_integrity(alias_path)
            print(f"[seed-bootstrap] runtime alias already valid: {alias_path}; skip", flush=True)
            return 0
        except Exception as exc:
            print(
                f"[seed-bootstrap] existing alias invalid ({exc.__class__.__name__}); rebuilding",
                flush=True,
            )

    publication = publish_bundle_from_artifact_directory(
        seed_path, archive_root=runtime_dir / "model_archive"
    )
    payload = build_release_alias_payload(
        bundle_root=publication.root,
        registry_model_id=publication.bundle_id,
        content_hash=publication.content_hash,
        alias_parent=alias_path.parent,
    )
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = alias_path.with_name(f".{alias_path.name}.seed_tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, alias_path)
    print(
        f"[seed-bootstrap] published bundle={publication.bundle_id} alias={alias_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 引导失败信息必须完整可见
        print(f"[seed-bootstrap] failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
