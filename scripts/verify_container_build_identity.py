"""部署期校验：镜像内的构建身份是否真的可以当"生产运行身份"用。

```bash
# 在部署机上（把镜像里的两个文件取出来）
docker run --rm --entrypoint cat stock-analyzer:latest /app/build_manifest.json > /tmp/bm.json
docker run --rm --entrypoint cat stock-analyzer:latest /app/.build_commit > /tmp/bc.txt
python scripts/verify_container_build_identity.py \
    --manifest /tmp/bm.json --build-commit-file /tmp/bc.txt --expect-commit "${COMMIT}"

# 也可以在容器内直接跑（rsync/exec 进任何有本仓库脚本的地方）
python scripts/verify_container_build_identity.py \
    --manifest /app/build_manifest.json --build-commit-file /app/.build_commit
```

**为什么要有这个脚本**：`docker build --build-arg ...` 是**输入**，不是证据。
运行身份必须在构建完成后、从**落盘产物**上复核一遍——否则"传参正确、镜像里是别的
值"这种组合可以静默通过部署。

**与运行期门的关系**：这里实现的是容器形态的同一份契约
（``.build_commit == build_manifest.commit`` + ``dirty=false`` + commit 形态合法），
权威实现在 ``stock_analyzer.alpha_v2.validation.runtime_identity``。本脚本**只依赖
标准库**（部署机不一定装了项目的运行时依赖，所以不能 import 项目包）；两者判定
一致由 ``tests/test_alpha_v2_production_runtime_identity.py`` 的交叉测试钉住。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 与 runtime_identity._COMMIT_PATTERN 同一形态（改一处必须改另一处，交叉测试会红）
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _dirty_is_false(value: object) -> bool:
    if isinstance(value, bool):
        return value is False
    return str(value).strip().lower() in {"0", "false", "no"}


def verify(
    *, manifest_path: str, build_commit_file: str, expect_commit: str = ""
) -> tuple[bool, list[str], dict[str, object]]:
    """返回 ``(ok, problems, facts)``；``problems`` 为空即为通过。"""
    problems: list[str] = []
    commit_file = _read(build_commit_file)
    raw = _read(manifest_path)
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        payload = {}
        problems.append(f"build_manifest.json 不可解析: {exc}")
    if not isinstance(payload, dict):
        payload = {}
        problems.append("build_manifest.json 顶层不是对象")

    manifest_commit = str(payload.get("commit", "") or "").strip()
    dirty = payload.get("dirty", "unknown")

    if not commit_file:
        problems.append(".build_commit 缺失/不可读")
    elif not COMMIT_PATTERN.match(commit_file):
        problems.append(f".build_commit 内容非法: {commit_file!r}")
    if not manifest_commit:
        problems.append("build_manifest.commit 缺失")
    elif not COMMIT_PATTERN.match(manifest_commit):
        problems.append(f"build_manifest.commit 非法: {manifest_commit!r}")
    if commit_file and manifest_commit and commit_file != manifest_commit:
        problems.append(
            f".build_commit {commit_file[:12]}… != build_manifest.commit {manifest_commit[:12]}…"
        )
    if not _dirty_is_false(dirty):
        problems.append(f"build_manifest.dirty={dirty!r}（要求 false）")
    expected = str(expect_commit or "").strip()
    if expected and manifest_commit and manifest_commit != expected:
        problems.append(
            f"build_manifest.commit {manifest_commit[:12]}… != 期望 {expected[:12]}…"
        )

    facts = {
        "build_commit": commit_file,
        "build_manifest_commit": manifest_commit,
        "build_manifest_dirty": dirty,
        "build_manifest_short_commit": str(payload.get("short_commit", "") or ""),
        "build_manifest_built_at_utc": str(payload.get("built_at_utc", "") or ""),
        "expected_commit": expected,
        "manifest_path": manifest_path,
        "build_commit_file": build_commit_file,
    }
    return (not problems), problems, facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验容器构建身份（部署门）")
    parser.add_argument("--manifest", required=True, help="build_manifest.json 路径")
    parser.add_argument("--build-commit-file", required=True, help=".build_commit 路径")
    parser.add_argument("--expect-commit", default="", help="期望的源码 commit（可选）")
    parser.add_argument("--json", action="store_true", help="只输出一行 JSON 结论")
    args = parser.parse_args(argv)

    ok, problems, facts = verify(
        manifest_path=args.manifest,
        build_commit_file=args.build_commit_file,
        expect_commit=args.expect_commit,
    )
    verdict = {"verdict": "PASS" if ok else "FAIL", "problems": problems, **facts}
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "[build-identity] "
            f"{verdict['verdict']} commit={facts['build_commit'][:12] or '(缺失)'}… "
            f"dirty={facts['build_manifest_dirty']!r} "
            f"built_at={facts['build_manifest_built_at_utc'] or '(未知)'}"
        )
        for problem in problems:
            print(f"[build-identity]   - {problem}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
