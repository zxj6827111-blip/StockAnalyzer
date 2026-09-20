"""生成不可变构建身份：``build_manifest.json``（可选同时写 ``.build_commit``）。

生产镜像在 **构建阶段** 调用本脚本（见 Dockerfile），两个产物必须由同一次调用产出，
"两源恒等"才是构造性事实而不是事后对齐。

```bash
python scripts/generate_build_manifest.py \
    --output /app/build_manifest.json \
    --build-commit-file /app/.build_commit \
    --commit "${SOURCE_COMMIT}" --short-commit "${SHORT}" \
    --dirty "${SOURCE_DIRTY}" --built-at-utc "${BUILT_AT_UTC}"
```

``dirty`` 的语义是"这份源码树是否可证干净"，因此：

- 显式给 ``1/true/yes`` / ``0/false/no`` → 采用（由部署脚本在**构建前**用 git 取证）；
- 显式给别的非空值（例如 ``unknown``）→ 如实写 ``unknown``；
- 完全不给 → 当场用 ``git status --porcelain`` 探测；**git 不可用也写 ``unknown``**，
  绝不把"探不到"写成 ``false``（那会让下游把无证据的树当成可信构建）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the immutable StockAnalyzer build manifest."
    )
    parser.add_argument("--output", default="build_manifest.json")
    parser.add_argument(
        "--build-commit-file",
        default="",
        help="同时把同一个 commit 写进该文件（生产镜像用 /app/.build_commit）",
    )
    parser.add_argument("--commit", default="")
    parser.add_argument("--short-commit", default="")
    parser.add_argument("--dirty", default="")
    parser.add_argument("--built-at-utc", default="")
    args = parser.parse_args()

    commit = args.commit.strip() or _git("rev-parse", "HEAD") or "unknown"
    short_commit = args.short_commit.strip() or (
        _git("rev-parse", "--short=12", "HEAD") if commit != "unknown" else "unknown"
    )
    payload = {
        "commit": commit,
        "short_commit": short_commit or commit[:12],
        "dirty": _resolve_dirty(args.dirty),
        "built_at_utc": args.built_at_utc.strip() or datetime.now(UTC).isoformat(),
        "config_schema": "stock-analyzer-config.v1",
        "runtime_state_schema": 9,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)

    commit_file = args.build_commit_file.strip()
    if commit_file:
        # 与 manifest 里的 commit 是同一个变量——两源相等由构造保证，不靠事后核对
        path = Path(commit_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{commit}\n", encoding="utf-8")
        print(path)
    return 0


def _resolve_dirty(raw: str) -> bool | str:
    """把 ``--dirty`` 归一成 ``True`` / ``False`` / ``"unknown"``（unknown = 无证据）。"""
    value = str(raw or "").strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    if value:
        return "unknown"
    probe = _git("status", "--porcelain")
    if probe is None:
        return "unknown"
    return bool(probe)


def _git(*args: str) -> str | None:
    """``None`` = 命令失败（无 git / 非仓库）；``""`` = 成功且无输出（干净树）。"""
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, check=False, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


if __name__ == "__main__":
    raise SystemExit(main())
