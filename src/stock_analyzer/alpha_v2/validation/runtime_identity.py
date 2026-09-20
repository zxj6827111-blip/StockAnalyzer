"""M3 运行身份：从 git / 构建产物解析"当前运行到底是谁"。

CLI 层共用的只读事实源（不注入业务逻辑）：

- ``git_head()`` / ``git_branch()`` / ``git_worktree_dirt()``：子进程读 git，失败如实返回；
- ``config_hash_of()``：复用 S00 的脱敏指纹；
- ``price_contract_block()``：execution/feature 价格口径（不是 raw 要如实暴露）。

**Runtime Identity Hardening（BLK-D1/BLK-D2）**：上面这些是"事实采集"，
``resolve_runtime_code_identity()`` 是唯一的"运行身份裁决"入口——freeze / capture /
mature / shadow model freeze 四个 CLI 都必须走它，不允许各自解析一次 git。

两种 runtime context（互斥，且**不是**优先级关系）：

```text
git_checkout             源码检出（开发机 / NAS 宿主仓库）：git HEAD 自证
container_build_identity 不可变容器（无 git 二进制、无 .git）：构建期写入的
                         .build_commit == build_manifest.commit 互证
```

容器里"没有 git"**不是错误**，只是另一种 context；但容器必须有可证的构建身份，
否则 fail-closed。反过来，源码检出里"没有 .build_commit"也不是错误（开发机常态），
只有当它存在却与 git HEAD 矛盾时才拒绝。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stock_analyzer.backtest.price_contract import resolve_price_contract
from stock_analyzer.build_identity import MANIFEST_SOURCE_ENVIRONMENT, get_build_manifest
from stock_analyzer.config_identity import redacted_config_hash

UNKNOWN = "unknown"

# 运行身份来源（runtime context）：只有这两种，互斥且都不是"优先级"
IDENTITY_SOURCE_GIT_CHECKOUT = "git_checkout"
IDENTITY_SOURCE_CONTAINER_BUILD = "container_build_identity"

# code_commit 的取值来源（写进 freeze manifest 的 code_commit_source，供审计）。
# 前两个值是 R3 起就落盘的历史标签，不得改名（否则旧清单与新清单不可比）；
# 后两个是本次硬化新增的容器形态标签。
CODE_COMMIT_SOURCE_GIT = "git_rev_parse"
CODE_COMMIT_SOURCE_CLI_OVERRIDE = "cli_override_verified"
CODE_COMMIT_SOURCE_CONTAINER = "container_build_identity"
CODE_COMMIT_SOURCE_CLI_OVERRIDE_CONTAINER = "cli_override_container_build_identity"
# 两种来源都拿不到（既无 git HEAD、也无构建身份）时的如实标记
CODE_COMMIT_SOURCE_UNRESOLVED = "unresolved"

# git 对象名：7~64 位十六进制（短 SHA / 完整 SHA；兼容 sha256 仓库）。
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


class FreezeGateError(RuntimeError):
    """冻结/身份硬门未通过（CLI 接到它时应 exit ``exit_code``）。

    定义放在运行身份模块里：身份门就是生产冻结门（exit 5），四个 CLI 共用同一个
    异常类型；``freeze_precheck`` 只是把它连同断言一起再导出，保持旧导入路径可用。
    """

    def __init__(self, message: str, *, exit_code: int = 6) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)


def is_valid_commit(value: object) -> bool:
    """commit 形态自证：``unknown`` / 空 / 非十六进制 / 长度不对一律 False。"""
    return bool(_COMMIT_PATTERN.match(str(value or "").strip()))


def resolve_code_commit(override: str | None, *, git_head_value: str) -> tuple[str, str]:
    """**git-checkout 形态**的 code_commit 解析（唯一实现，容器形态见 resolver）。

    显式传参优先，但必须与 git HEAD 一致时才允许放行；git 不可得时允许只靠
    ``--code-commit`` 声明（此时 source 如实标 ``cli_override_git_unavailable``，
    让审计能看到"这一份不是 git 自证"）。

    返回 ``(code_commit, code_commit_source)``；违反时抛 :class:`FreezeGateError`（exit 5）。
    容器形态（``git_available=False``）不走本函数，其 ``code_commit`` 来自构建身份，
    见 :func:`resolve_runtime_code_identity`。
    """
    given = str(override or "").strip()
    git_known = git_head_value not in {"", UNKNOWN}
    if given:
        if git_known and git_head_value != given:
            raise FreezeGateError(
                f"--code-commit {given[:12]}… 与 git HEAD {git_head_value[:12]}… 不一致",
                exit_code=5,
            )
        return given, (
            CODE_COMMIT_SOURCE_CLI_OVERRIDE if git_known else "cli_override_git_unavailable"
        )
    if not git_known:
        raise FreezeGateError(
            "git rev-parse HEAD 不可用（容器内？）、且未传 --code-commit；"
            "生产模式不允许把 code_commit 写为 unknown",
            exit_code=5,
        )
    return git_head_value, CODE_COMMIT_SOURCE_GIT


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
    return _git(["rev-parse", "HEAD"], cwd=root) or UNKNOWN


def git_branch(root: str | Path | None = None) -> str:
    return _git(["branch", "--show-current"], cwd=root) or UNKNOWN


def git_available(root: str | Path | None = None) -> bool:
    """该目录能否用 git 取证。

    ``git`` 二进制不存在 / 目录不是仓库 / 仓库损坏 → ``False``。
    这是"环境事实"，不是错误：生产容器本来就没有 git。
    """
    marker = _git(["rev-parse", "--is-inside-work-tree"], cwd=root)
    return bool(marker) and marker.strip().lower() == "true"


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
    """读部署期写入的 ``.build_commit``（缺失 = 空串，不猜）。

    容器里这份文件由镜像构建阶段写入（Dockerfile 同一条 RUN 里同时写
    ``build_manifest.json``），与宿主仓库根那份同名同义。
    """
    path = Path(repo_root) / ".build_commit" if repo_root else Path(".build_commit")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_build_manifest_file(repo_root: str | Path | None = None) -> dict[str, object]:
    """读部署期 ``build_manifest.json``（R3：**存在性本身就是证据**）。

    返回 ``{"present": bool, "path": str, "payload": {...}}``——缺失/不可解析一律
    ``present=False``，绝不拿环境变量兜底冒充"产物存在"（那会让双源硬门失效）。
    解析统一走 :func:`stock_analyzer.build_identity.get_build_manifest`：全项目
    只有一份 manifest schema / 一份 trusted-dirty 语义（禁止平行造第二套）。
    """
    manifest = get_build_manifest(repo_root)
    source = str(manifest.get("source", "") or "")
    present = bool(source) and source != MANIFEST_SOURCE_ENVIRONMENT
    return {
        "present": present,
        "path": source if present else "",
        "payload": dict(manifest) if present else {},
    }


def build_identity_block(repo_root: str | None = None) -> dict[str, object]:
    """汇总"这份运行代码到底是谁"的四重证据（**只采集、不裁决**）：

    git 可得性 / git HEAD / 分支 / 工作区脏状态（白名单豁免后的具体条目；
    ``None`` 表示 git 不可取证）/ ``build_manifest.json``（含存在性、commit、
    trusted、dirty）/ ``.build_commit``。

    裁决在 :func:`resolve_runtime_code_identity`（两模式各自的信任规则），
    本函数不做任何判断。
    """
    file_block = read_build_manifest_file(repo_root)
    payload = dict(file_block.get("payload") or {})  # type: ignore[arg-type]
    commit = str(payload.get("commit", "") or "").strip()
    dirty = payload.get("dirty", UNKNOWN)
    available = git_available(repo_root)
    return {
        "git_available": available,
        "git_head": git_head(repo_root),
        "git_branch": git_branch(repo_root),
        "worktree_dirty_entries": git_worktree_dirt(repo_root) if available else None,
        "build_manifest_present": bool(file_block["present"]),
        "build_manifest_path": str(file_block["path"]),
        "build_manifest_commit": commit,
        "build_manifest_dirty": dirty,
        # trusted 语义由共享解析器给出（commit 可证 且 dirty 可证），本函数不另立一套
        "build_manifest_trusted": payload.get("trusted") if file_block["present"] else False,
        "build_manifest_source": str(file_block["path"]) or "missing",
        "build_manifest_sha256": str(payload.get("manifest_sha256", "") or ""),
        "build_commit_file": read_build_commit(repo_root),
    }


@dataclass(frozen=True, slots=True)
class RuntimeCodeIdentity:
    """运行代码身份的唯一裁决结果（四个 CLI 共用同一份）。"""

    code_commit: str
    identity_source: str
    code_commit_source: str
    git_available: bool
    git_head: str
    git_branch: str
    git_worktree_state: list[str] | None
    build_commit_present: bool
    build_commit: str
    build_manifest_present: bool
    build_manifest_commit: str
    build_manifest_trusted: object
    build_manifest_dirty: object
    build_manifest_path: str
    identity_verified: bool
    violations: tuple[str, ...] = field(default_factory=tuple)

    def to_payload(self) -> dict[str, object]:
        """进 freeze manifest ``build_identity`` 块的审计条目。

        键名沿用 R3 起就在清单里的那套（``build_commit_file`` /
        ``worktree_dirty_entries``），只**追加**硬化新增的字段——不无意义地
        重命名既有 schema，让新旧清单可以直接对比。
        """
        return {
            "code_commit": self.code_commit,
            "code_commit_source": self.code_commit_source,
            "identity_source": self.identity_source,
            "git_available": self.git_available,
            "git_head": self.git_head,
            "git_branch": self.git_branch,
            "worktree_dirty_entries": (
                list(self.git_worktree_state) if self.git_worktree_state is not None else None
            ),
            "build_commit_file": self.build_commit,
            "build_manifest_present": self.build_manifest_present,
            "build_manifest_commit": self.build_manifest_commit,
            "build_manifest_trusted": self.build_manifest_trusted,
            "build_manifest_dirty": self.build_manifest_dirty,
            "build_manifest_path": self.build_manifest_path,
            "identity_verified": self.identity_verified,
            "violations": list(self.violations),
        }


def build_identity_violations(
    *,
    git_head: str,
    code_commit: str,
    build_commit_file: str,
    build_manifest_commit: str,
    build_manifest_present: bool,
    build_manifest_trusted: object = False,
    build_manifest_dirty: object = UNKNOWN,
    require_build_identity: bool = True,
    validation_mode: str = "production",
) -> list[str]:
    """构建身份硬门的**唯一**判定实现（R3/BLK-R2-2 的四值一致语义）。

    生产模式要求下面四个值两两相等、且每个都可证：

    ```text
    git HEAD（可读时） == code_commit == .build_commit == build_manifest.commit
    ```

    任一项 missing / unknown / malformed / mismatch 都是违例；``build_manifest`` 的
    ``trusted`` 必须为 True、``dirty`` 必须为 False。

    ``require_build_identity``：是否要求**必须存在**构建产物。生产 freeze 为 True
    （冻结对象必须锚定构建身份）；源码检出里的日常动作（capture/mature）为 False，
    此时这两份文件缺席不吵，存在则必须与 ``code_commit`` 一致。容器恒按 True 处理
    ——那是容器唯一的身份来源。

    ``git HEAD`` 是否"可读"由 ``is_valid_commit(git_head)`` 判定：容器里它会是
    ``unknown``，这不是错误，而是"该来源不适用"。
    """
    if validation_mode != "production":
        return []
    git_ok = is_valid_commit(git_head)
    file_value = str(build_commit_file or "").strip()
    manifest_value = str(build_manifest_commit or "").strip()
    must_exist = require_build_identity or not git_ok

    problems: list[str] = []
    if not is_valid_commit(code_commit):
        if git_ok:
            problems.append(
                f"运行 code_commit 不可证/格式非法: {code_commit!r}"
                f"（identity_source={IDENTITY_SOURCE_GIT_CHECKOUT}）"
            )
        else:
            problems.append(
                f"运行 code_commit 不可证/格式非法: {code_commit!r}"
                "（identity_source=container_build_identity：构建身份缺失）"
            )
    if must_exist or build_manifest_present or file_value:
        if not build_manifest_present:
            problems.append("build_manifest.json 缺失或不可解析")
        elif not is_valid_commit(manifest_value):
            problems.append(f"build_manifest.commit 缺失/非法: {manifest_value!r}")
        if not file_value:
            problems.append(".build_commit 缺失/不可读")
        elif not is_valid_commit(file_value):
            problems.append(f".build_commit 内容非法: {file_value!r}")
    if build_manifest_present:
        if build_manifest_trusted is not True:
            problems.append(f"build_manifest.trusted={build_manifest_trusted!r}（要求 true）")
        if build_manifest_dirty is not False:
            problems.append(f"build_manifest.dirty={build_manifest_dirty!r}（要求 false）")
    if problems:
        return problems

    if git_ok and git_head != code_commit:
        problems.append(f"git HEAD {git_head[:12]}… != code_commit {code_commit[:12]}…")
    for name, value in (
        (".build_commit", file_value),
        ("build_manifest.commit", manifest_value),
    ):
        if not value and not must_exist:
            continue
        if value != code_commit:
            problems.append(
                f"{name} {value[:12] or '(空)'}… != code_commit {code_commit[:12]}…"
                "（两个来源都必须逐位相等）"
            )
    return problems


def resolve_runtime_code_identity(
    root: str | Path | None = None,
    *,
    requested_code_commit: str | None = None,
    validation_mode: str = "production",
    require_build_identity: bool = False,
) -> RuntimeCodeIdentity:
    """**唯一的运行身份解析入口**（freeze / capture / mature / model freeze 共用）。

    先判定 runtime context，再套用该 context 的信任规则——顺序本身就是修复的一部分：
    BLK-D1 的成因正是"在判定环境之前先要 git status"，于是容器永远过不去。
    本函数**不抛异常**：把违例如实收进 ``violations``，由调用方按自己的退出码呈现。

    - ``git_checkout``：``git HEAD`` 可读即可用（override 与 HEAD 的矛盾仍硬拦）；
      ``.build_commit`` / ``build_manifest.json`` 存在时必须与 HEAD 一致
      （矛盾要吵、缺席不吵），``require_build_identity=True``（生产 freeze）时缺席也吵。
    - ``container_build_identity``：``.build_commit`` 与 ``build_manifest.commit``
      必须都存在、可证、逐位相等，且 ``trusted=true`` / ``dirty=false``；
      ``code_commit`` 取自构建身份，**不再查 git**（容器里没有 git 是设计内）。
    - 非 production（rehearsal / test）：不做身份硬门（清单会如实标 validation_mode），
      但仍给出当前可解析到的 ``code_commit``，避免 CLI 拿到 ``unknown``。
    """
    block = build_identity_block(root)
    git_ok = bool(block.get("git_available"))
    head = str(block.get("git_head") or UNKNOWN)
    file_value = str(block.get("build_commit_file") or "").strip()
    manifest_value = str(block.get("build_manifest_commit") or "").strip()
    requested = str(requested_code_commit or "").strip()

    violations: list[str] = []
    if git_ok:
        identity_source = IDENTITY_SOURCE_GIT_CHECKOUT
        try:
            code_commit, code_commit_source = resolve_code_commit(requested, git_head_value=head)
        except FreezeGateError as exc:
            violations.append(str(exc))
            code_commit, code_commit_source = head, CODE_COMMIT_SOURCE_GIT
    else:
        identity_source = IDENTITY_SOURCE_CONTAINER_BUILD
        derived = file_value or manifest_value
        if requested:
            code_commit = requested
            code_commit_source = CODE_COMMIT_SOURCE_CLI_OVERRIDE_CONTAINER
        else:
            code_commit = derived
            code_commit_source = (
                CODE_COMMIT_SOURCE_CONTAINER if derived else CODE_COMMIT_SOURCE_UNRESOLVED
            )

    violations.extend(
        build_identity_violations(
            git_head=head,
            code_commit=code_commit,
            build_commit_file=file_value,
            build_manifest_commit=manifest_value,
            build_manifest_present=bool(block.get("build_manifest_present")),
            build_manifest_trusted=block.get("build_manifest_trusted"),
            build_manifest_dirty=block.get("build_manifest_dirty"),
            require_build_identity=require_build_identity,
            validation_mode=validation_mode,
        )
    )

    return RuntimeCodeIdentity(
        code_commit=code_commit,
        identity_source=identity_source,
        code_commit_source=code_commit_source,
        git_available=git_ok,
        git_head=head,
        git_branch=str(block.get("git_branch") or UNKNOWN),
        git_worktree_state=block.get("worktree_dirty_entries"),  # type: ignore[arg-type]
        build_commit_present=bool(file_value),
        build_commit=file_value,
        build_manifest_present=bool(block.get("build_manifest_present")),
        build_manifest_commit=manifest_value,
        build_manifest_trusted=block.get("build_manifest_trusted"),
        build_manifest_dirty=block.get("build_manifest_dirty"),
        build_manifest_path=str(block.get("build_manifest_path") or ""),
        identity_verified=not violations,
        violations=tuple(violations),
    )


__all__ = [
    "BUILD_IDENTITY_UNTRACKED_WHITELIST",
    "CODE_COMMIT_SOURCE_CLI_OVERRIDE",
    "CODE_COMMIT_SOURCE_CLI_OVERRIDE_CONTAINER",
    "CODE_COMMIT_SOURCE_CONTAINER",
    "CODE_COMMIT_SOURCE_GIT",
    "CODE_COMMIT_SOURCE_UNRESOLVED",
    "IDENTITY_SOURCE_CONTAINER_BUILD",
    "IDENTITY_SOURCE_GIT_CHECKOUT",
    "UNKNOWN",
    "FreezeGateError",
    "RuntimeCodeIdentity",
    "build_identity_block",
    "build_identity_violations",
    "config_hash_of",
    "git_available",
    "git_branch",
    "git_head",
    "git_worktree_dirt",
    "is_valid_commit",
    "price_contract_block",
    "read_build_commit",
    "read_build_manifest_file",
    "resolve_code_commit",
    "resolve_runtime_code_identity",
]
