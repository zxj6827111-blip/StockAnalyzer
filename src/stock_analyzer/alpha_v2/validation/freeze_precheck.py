"""Alpha V2 M3 生产冻结硬门（`scripts/alpha_v2_validation_freeze.py` 的同源实现）。

把"生产 freeze / capture / mature 必须满足什么"从 CLI 的逐行 print/exit 里抽出成
一组纯函数：这份清单既被 CLI 调用（接了它才允许写盘），也被测试直接断言——否则
CLI 文字提示与 CI 断言会再次漂移成两个版本。

原则：每一个 gate 都是显式的 ``FreezeGateError``，顺序执行，first-fail-stop；
``--rehearsal`` 只换掉执行口径/工作区/构建身份/起始日期的硬门（清单如实标
``validation_mode=rehearsal``），feature schema 的空表只在 rehearsal 容忍。

**Runtime Identity Hardening（BLK-D1/BLK-D2）**：身份门改为
:func:`assert_runtime_identity`——它接一份 :class:`RuntimeCodeIdentity`（由
``runtime_identity.resolve_runtime_code_identity`` 产出，**先判环境再套规则**）：

```text
git_checkout             → git HEAD 四值一致 + 工作区必须可证干净
container_build_identity → .build_commit == build_manifest.commit、trusted、dirty=false
                           （容器里没有 git checkout，工作区门不适用）
```

这不是降低要求：两种 runtime context 各自使用**本环境可证**的可信身份来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from stock_analyzer.alpha_v2.validation.runtime_identity import (
    IDENTITY_SOURCE_GIT_CHECKOUT,
    FreezeGateError,
    RuntimeCodeIdentity,
    build_identity_violations,
    is_valid_commit,
    resolve_code_commit,
)

__all__ = [
    "FeatureSchemaResult",
    "FreezeGateError",
    "assert_build_identity",
    "assert_execution_price_raw",
    "assert_model_training_commit",
    "assert_runtime_identity",
    "assert_validation_start_date",
    "assert_worktree_clean",
    "resolve_code_commit",
    "resolve_feature_schema_columns",
]


@dataclass(frozen=True, slots=True)
class FeatureSchemaResult:
    feature_columns: tuple[str, ...]
    source: str


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


def assert_model_training_commit(
    *,
    model_training_code_commit: str,
    runtime_code_commit: str,
    validation_mode: str = "production",
) -> None:
    """**R4.1 模型训练身份绑定**：生产强不变量

    ```text
    runtime code_commit == frozen model training code_commit
    ```

    训练身份的唯一权威来源是冻结模型工件 manifest 的 ``code_commit``（由
    ``alpha_v2_shadow_model_freeze.py`` 在训练时从统一 Runtime Identity Resolver 取得）。
    生产模式下 missing / empty / ``unknown`` / 非法 SHA 一律拒绝——**不允许**任何形式的
    回退（不拿 runtime commit 顶替、不假设"就是当前这份"），因为那正是"用别的代码训过的
    模型在不知情的情况下被当成生产模型"的入口。

    非 production（rehearsal / test）不做此门：排演工件本来就不是生产身份。
    """
    if validation_mode != "production":
        return
    training = str(model_training_code_commit or "").strip()
    if not is_valid_commit(training):
        raise FreezeGateError(
            "冻结模型工件缺少可证的训练 code_commit"
            f"（读到的值 {training!r}）；生产模式不允许缺失/unknown/非法形态，"
            "也不允许回退成当前运行 commit",
            exit_code=5,
        )
    runtime = str(runtime_code_commit or "").strip()
    if training != runtime:
        raise FreezeGateError(
            f"模型训练身份 {training[:12]}… != 运行身份 {runtime[:12]}…；"
            "生产 epoch 只允许由「训练该模型的同一份代码」开启与推进"
            "（引入他处训练的模型需要显式的兼容性契约与迁移，不是静默放行）",
            exit_code=5,
        )


def assert_worktree_clean(
    dirty_entries: list[str] | None, *, validation_mode: str = "production"
) -> None:
    """``dirty_entries = None`` 表示 git 不可用 / 仓库损坏——生产一律视为不可证明干净。

    ⚠️ 只对 ``git_checkout`` 形态适用：不可变容器里根本没有工作区，容器形态的等价
    门是"构建身份两源一致 + trusted + clean"（见 :func:`assert_runtime_identity`）。
    """
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


def assert_runtime_identity(
    identity: RuntimeCodeIdentity, *, validation_mode: str = "production"
) -> str:
    """四个 CLI 共用的**唯一**运行身份门，返回通过校验的 ``code_commit``。

    - ``identity.violations`` 非空 → exit 5（构建身份/commit 不可证，两类 context 通用）；
    - 仅当 ``identity_source == git_checkout`` 时才追加工作区干净门：容器形态没有
      worktree 这一概念，拿它当门会让生产容器永远冻结不了（BLK-D1 的成因）。
    """
    if validation_mode != "production":
        return identity.code_commit
    if identity.violations:
        raise FreezeGateError(
            f"运行身份不可证（identity_source={identity.identity_source}，"
            f"code_commit={identity.code_commit or '(空)'}）：" + "；".join(identity.violations),
            exit_code=5,
        )
    if identity.identity_source == IDENTITY_SOURCE_GIT_CHECKOUT:
        assert_worktree_clean(identity.git_worktree_state, validation_mode="production")
    return identity.code_commit


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

    判定本体是 :func:`runtime_identity.build_identity_violations`（唯一实现）；
    本函数是它的 exit-code 呈现层，同时保留 R3 起被回归测试钉住的入参签名。
    容器内 git 不可得的形态由 :func:`assert_runtime_identity` 走完整 resolver 判定。
    """
    requested = str(requested_code_commit or "").strip()
    if validation_mode != "production":
        return requested
    problems = build_identity_violations(
        git_head=git_head,
        code_commit=requested,
        build_commit_file=str(build_commit_file or ""),
        build_manifest_commit=str(build_manifest_commit or ""),
        build_manifest_present=bool(build_manifest_present),
        build_manifest_trusted=build_manifest_trusted,
        build_manifest_dirty=build_manifest_dirty,
        # 冻结对象必须锚定构建身份：两源缺席也算不可证（R3 语义）
        require_build_identity=True,
        validation_mode="production",
    )
    if problems:
        raise FreezeGateError(
            "构建身份不完整（任一缺失即拒绝）：" + "；".join(problems), exit_code=5
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
