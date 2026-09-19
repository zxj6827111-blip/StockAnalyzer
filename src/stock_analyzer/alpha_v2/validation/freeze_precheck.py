"""Alpha V2 M3 生产冻结硬门（`scripts/alpha_v2_validation_freeze.py` 的同源实现）。

把"生产 freeze 必须满足什么"从 CLI 的逐行 print/exit 里抽出成一个纯函数集：
这份清单既被 CLI 调用（接了它才允许写盘），也被测试直接断言——否则 CLI
文字提示与 CI 断言会再次漂移成两个版本。

原则：每一个 gate 都是显式的 ``FreezeGateError``，顺序执行，first-fail-stop；
``--rehearsal`` 只换掉执行口径/工作区/构建身份/起始日期的硬门（清单如实标
``validation_mode=rehearsal``），feature schema 的空表只在 rehearsal 容忍。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


class FreezeGateError(RuntimeError):
    """生产冻结硬门未通过（CLI 接到它时应 exit ``exit_code``）。"""

    def __init__(self, message: str, *, exit_code: int = 6) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)


@dataclass(frozen=True, slots=True)
class FeatureSchemaResult:
    feature_columns: tuple[str, ...]
    source: str


def resolve_code_commit(
    override: str | None, *, git_head_value: str
) -> tuple[str, str]:
    """生产 code_commit：显式传参优先，但必须与 git HEAD 一致时才允许放行。

    返回 ``(code_commit, source)``。容器里 git 不可得时允许只靠
    --code-commit 证明（那时必须通过 .build_commit/build_manifest 双重验证，
    见 :func:`assert_build_identity`）；此时 source 会如实标为
    ``cli_override_git_unavailable``，让审计能看到"这一份不是 git 自证"。
    """
    given = str(override or "").strip()
    git_known = git_head_value not in {"", "unknown"}
    if given:
        if git_known and git_head_value != given:
            raise FreezeGateError(
                f"--code-commit {given[:12]}… 与 git HEAD {git_head_value[:12]}… 不一致",
                exit_code=5,
            )
        return given, ("cli_override_verified" if git_known else "cli_override_git_unavailable")
    if not git_known:
        raise FreezeGateError(
            "git rev-parse HEAD 不可用（容器内？）、且未传 --code-commit；"
            "生产模式不允许把 code_commit 写为 unknown",
            exit_code=5,
        )
    return git_head_value, "git_rev_parse"


def assert_execution_price_raw(
    execution_price_mode: str, *, validation_mode: str = "production"
) -> None:
    mode = str(execution_price_mode).strip().lower()
    if mode != "raw" and validation_mode == "production":
        raise FreezeGateError(
            f"execution_price_mode={mode} 不是 raw。生产的 freeze 必须 fail-closed；"
            "如需本地排练请用 rehearsal 模式，清单会如实标 validation_mode=rehearsal",
            exit_code=4,
        )


def assert_worktree_clean(
    dirty_entries: list[str] | None, *, validation_mode: str = "production"
) -> None:
    """``dirty_entries = None`` 表示 git 不可用 / 仓库损坏——生产一律视为不可证明干净。"""
    if validation_mode != "production":
        return
    if dirty_entries is None:
        raise FreezeGateError(
            "无法证明工作区干净（git status 在该仓库不可用/非标准输出）；"
            "生产冻结不允许按『可能干净』放行",
            exit_code=5,
        )
    if dirty_entries:
        preview = "\n".join(f"  {line}" for line in dirty_entries[:20])
        raise FreezeGateError(
            f"工作区不干净（{len(dirty_entries)} 处修改/暂存/未跟踪）：\n{preview}\n"
            "先 commit / 清理再冻结——'跑 M3 代码、记 M1/M2 commit' 这条路径必须封死",
            exit_code=5,
        )


def assert_build_identity(
    *,
    git_head: str,
    requested_code_commit: str,
    build_commit_file: str,
    build_manifest_commit: str,
    build_manifest_present: bool,
    build_manifest_trusted: object,
    build_manifest_dirty: object,
    validation_mode: str = "production",
) -> str:
    """构建身份**四值一致**硬门（R3 / BLK-R2-2），返回通过校验的 code_commit。

    生产模式要求下面四个值两两相等、且每个都可证：

    ```text
    git HEAD（可读时） == requested code_commit == .build_commit == build_manifest.commit
    ```

    任一项 missing / unknown / malformed / mismatch 都 fail-closed（exit 5）；
    ``build_manifest`` 的 ``trusted`` 必须为 True、``dirty`` 必须为 False。

    唯一的显式例外：**容器内 git 不可得**时，``git HEAD`` 这一项无法比较，
    此时必须 `--code-commit` 显式给值，并由 ``.build_commit`` 与 ``build_manifest``
    两源互相印证（比"任选一源"更强）；该例外会以
    ``cli_override_git_unavailable`` 记进冻结清单的 ``code_commit_source``。
    """
    requested = str(requested_code_commit or "").strip()
    git_value = str(git_head or "").strip()
    file_value = str(build_commit_file or "").strip()
    manifest_value = str(build_manifest_commit or "").strip()
    if validation_mode != "production":
        return requested
    problems: list[str] = []
    if not requested or requested.lower() == "unknown":
        problems.append("requested code_commit 缺失/unknown")
    if not build_manifest_present:
        problems.append("build_manifest.json 缺失或不可解析")
    elif manifest_value in {"", "unknown"}:
        problems.append("build_manifest.commit 缺失/unknown")
    if file_value in {"", "unknown"}:
        problems.append(".build_commit 缺失/不可读")
    if build_manifest_trusted is not True:
        problems.append(f"build_manifest.trusted={build_manifest_trusted!r}（要求 true）")
    if build_manifest_dirty is not False:
        problems.append(f"build_manifest.dirty={build_manifest_dirty!r}（要求 false）")
    if problems:
        raise FreezeGateError(
            "构建身份不完整（任一缺失即拒绝）：" + "；".join(problems), exit_code=5
        )
    if git_value not in {"", "unknown"} and git_value != requested:
        raise FreezeGateError(
            f"git HEAD {git_value[:12]}… != requested code_commit {requested[:12]}…",
            exit_code=5,
        )
    mismatched = [
        name
        for name, value in (
            (".build_commit", file_value),
            ("build_manifest.commit", manifest_value),
        )
        if value != requested
    ]
    if mismatched:
        raise FreezeGateError(
            f"构建身份不一致：{'、'.join(mismatched)} 与 code_commit "
            f"{requested[:12]}… 不同（两个来源都必须逐位相等）",
            exit_code=5,
        )
    return requested


def resolve_feature_schema_columns(
    *, file_columns: list[str], model_columns: list[str], validation_mode: str
) -> FeatureSchemaResult:
    """特征列来源优先策略：默认从冻结模型工件派生，其次开列清单文件。"""
    if file_columns and model_columns and sorted(file_columns) != sorted(model_columns):
        raise FreezeGateError(
            "--feature-columns-file 与模型工件的 feature_columns 不一致"
            f"（{len(file_columns)} vs {len(model_columns)}）",
            exit_code=6,
        )
    columns = sorted(set(file_columns or []) | set(model_columns or []))
    if not columns and validation_mode == "production":
        raise FreezeGateError(
            "生产模式下 feature schema 必须非空（来自 --feature-columns-file 或 "
            "--model-dir 的模型工件）；空清单会像旧排演一样让 feature_schema_hash 失去意义",
            exit_code=6,
        )
    if file_columns and model_columns:
        source = "cli_file_and_model_artifact"
    elif model_columns:
        source = "model_artifact"
    elif file_columns:
        source = "cli_file"
    else:
        source = "empty_rehearsal_only"
    return FeatureSchemaResult(feature_columns=tuple(columns), source=source)


def assert_validation_start_date(
    start_date_text: str, *, today: date, validation_mode: str = "production"
) -> None:
    """生产模式：必须显式给首个 shadow 交易日，且不允许把验证起点写到历史里。"""
    text = str(start_date_text or "").strip()
    if validation_mode != "production":
        return
    if not text:
        raise FreezeGateError("生产模式必须给 --start-date（首个 shadow 交易日）", exit_code=6)
    try:
        start = date.fromisoformat(text)
    except ValueError as exc:
        raise FreezeGateError(f"--start-date 无法解析: {text!r}", exit_code=6) from exc
    if start < today:
        raise FreezeGateError(
            f"--start-date={start} 早于今天 {today}；生产 epoch 不允许写到历史里",
            exit_code=6,
        )


__all__ = [
    "FeatureSchemaResult",
    "FreezeGateError",
    "assert_build_identity",
    "assert_execution_price_raw",
    "assert_validation_start_date",
    "assert_worktree_clean",
    "resolve_code_commit",
    "resolve_feature_schema_columns",
]
