"""M3 运行身份小工具：从 git / 配置解析"当前运行到底是谁"。

CLI 层共用的只读事实源（不注入业务逻辑）：

- ``git_head()`` / ``git_branch()``：子进程读 git，失败如实的返回 ``unknown``；
- ``redacted_config_hash_of()``：复用 S00 的脱敏指纹；
- ``price_contract_block()``：execution/feature 价格口径（不是 raw 要如实暴露）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from stock_analyzer.backtest.price_contract import resolve_price_contract
from stock_analyzer.config_identity import redacted_config_hash


def _git(args: list[str], cwd: str | Path | None = None) -> str | None:
    """执行 git 命令；返回 stdout（``None`` = 命令失败；``""`` = 成功但无输出）。

    ⚠️ 空文本代表"命令成功但无输出"是可证语义（例如 ``git status --porcelain``
    在干净工作区输出为空）——不能把 ``""`` 误判成失败。
    """
    try:
        output = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if output.returncode != 0:
        return None
    return output.stdout.strip()


def git_head(root: str | Path | None = None) -> str:
    return _git(["rev-parse", "HEAD"], cwd=root) or "unknown"


def git_branch(root: str | Path | None = None) -> str:
    return _git(["branch", "--show-current"], cwd=root) or "unknown"


def config_hash_of(config: Any) -> str:
    return redacted_config_hash(config)


def price_contract_block(config: Any) -> dict[str, object]:
    contract = resolve_price_contract(config)
    payload = contract.to_payload()
    payload["execution_price_mode"] = contract.execution_price_mode
    payload["feature_price_mode"] = contract.feature_price_mode
    payload["execution_uncertain"] = contract.execution_uncertain
    return payload


# 部署期写入的身份标记文件：它们的存在本身不算"工作区脏"，但内容必须能对账。
BUILD_IDENTITY_UNTRACKED_WHITELIST: frozenset[str] = frozenset(
    {
        ".build_commit",
        "build_manifest.json",
    }
)


def git_worktree_dirt(
    root: str | Path | None = None, *, whitelist_untracked: frozenset[str] | None = None
) -> list[str] | None:
    """返回工作区"脏"条目（porcelain 行）；白名单仅在**未跟踪**行上豁免。

    已跟踪文件的修改/暂存永远算脏。"``git status`` 拿不到"返回 ``None``——
    生产冻结须把"无法证明干净"也当"不允许"（不能混同于真干净）。
    """
    output = _git(["status", "--porcelain"], cwd=root)
    if output is None:
        return None  # git 不可用 / 仓库损坏 → 上游 fail-closed
    exempt = set(whitelist_untracked or BUILD_IDENTITY_UNTRACKED_WHITELIST)
    dirty: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        prefix, name = line[:2], line[3:].strip()
        if prefix == "??" and name in exempt:
            continue
        dirty.append(line)
    return dirty


def read_build_commit(repo_root: str | Path | None = None) -> str:
    """读部署期写入的 ``.build_commit``（缺失 = 空串，不猜）。"""
    path = Path(repo_root) / ".build_commit" if repo_root else Path(".build_commit")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_build_manifest_file(repo_root: str | Path | None = None) -> dict[str, object]:
    """读部署期 ``build_manifest.json``（R3：**存在性本身就是证据**）。

    返回 ``{"present": bool, "path": str, "payload": {...}}``——缺失/不可解析一律
    ``present=False``，绝不拿环境变量兜底冒充"产物存在"（那会让双源硬门失效）。
    """
    candidates: list[Path] = []
    configured = os.getenv("STOCK_ANALYZER_BUILD_MANIFEST", "").strip()
    if configured:
        candidates.append(Path(configured))
    if repo_root:
        candidates.append(Path(repo_root) / "build_manifest.json")
    candidates.extend([Path("/app/build_manifest.json"), Path("build_manifest.json")])
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            return {"present": True, "path": str(path), "payload": payload}
    return {"present": False, "path": "", "payload": {}}


def build_identity_block(repo_root: str | None = None) -> dict[str, object]:
    """汇总"这份运行代码到底是谁"的四重证据：

    git HEAD / 工作区脏状态（白名单豁免后的具体条目；``None`` 表示 git 不可取证）/
    ``build_manifest.json``（含存在性、commit、trusted、dirty）/ ``.build_commit``。
    生产 freeze 的四值硬门由 CLI 以此为准（``freeze_precheck.assert_build_identity``），
    本函数只采集、不做判断。
    """
    file_block = read_build_manifest_file(repo_root)
    payload = dict(file_block.get("payload") or {})  # type: ignore[arg-type]
    commit = str(payload.get("commit", "") or "").strip()
    dirty_raw = payload.get("dirty", "unknown")
    if isinstance(dirty_raw, bool):
        dirty: object = dirty_raw
    elif str(dirty_raw).strip().lower() in {"1", "true", "yes"}:
        dirty = True
    elif str(dirty_raw).strip().lower() in {"0", "false", "no"}:
        dirty = False
    else:
        dirty = "unknown"
    return {
        "git_head": git_head(repo_root),
        "git_branch": git_branch(repo_root),
        "worktree_dirty_entries": git_worktree_dirt(repo_root),
        "build_manifest_present": bool(file_block["present"]),
        "build_manifest_path": str(file_block["path"]),
        "build_manifest_commit": commit,
        "build_manifest_dirty": dirty,
        "build_manifest_trusted": bool(commit and commit != "unknown" and dirty != "unknown"),
        "build_manifest_source": str(file_block["path"]) or "missing",
        "build_commit_file": read_build_commit(repo_root),
    }


__all__ = [
    "BUILD_IDENTITY_UNTRACKED_WHITELIST",
    "build_identity_block",
    "config_hash_of",
    "git_branch",
    "git_head",
    "git_worktree_dirt",
    "price_contract_block",
    "read_build_commit",
    "read_build_manifest_file",
]
