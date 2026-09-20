"""Production Runtime Identity Hardening 定向测试（BLK-D1 / BLK-D2）。

被钉住的契约：

```text
git_checkout              git HEAD 可读即可用；工作区门只在此形态适用
container_build_identity  .build_commit == build_manifest.commit、trusted、dirty=false
                          （容器里没有 git checkout → 工作区门不适用）
```

四条 CLI（validation freeze / shadow model freeze / shadow capture / shadow mature）
必须走同一个 resolver；容器里 code_commit 必须是构建身份，**不得**是 ``unknown``。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.validation.epoch import (  # noqa: E402
    epoch_identity_matches,
    open_epoch,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (  # noqa: E402
    FreezeGateError,
    assert_runtime_identity,
    assert_worktree_clean,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    CODE_COMMIT_SOURCE_CLI_OVERRIDE_CONTAINER,
    CODE_COMMIT_SOURCE_CONTAINER,
    CODE_COMMIT_SOURCE_GIT,
    IDENTITY_SOURCE_CONTAINER_BUILD,
    IDENTITY_SOURCE_GIT_CHECKOUT,
    build_identity_block,
    git_available,
    resolve_runtime_code_identity,
)

SHA = "a1b2c3d4" * 5
OTHER_SHA = "deadbeef" * 5
BUILD_TIME = "2026-09-19T00:00:00+00:00"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "alpha_v2_runtime_identity_smoke.py"
VERIFIER_SCRIPT = REPO_ROOT / "scripts" / "verify_container_build_identity.py"


# ---------------------------------------------------------------------------
# 夹具：两种 runtime context
# ---------------------------------------------------------------------------


def _write_container_identity(
    root: Path,
    *,
    file_commit: str | None = SHA,
    manifest_commit: object = SHA,
    dirty: object = False,
    write_commit_file: bool = True,
    write_manifest: bool = True,
) -> None:
    """写"镜像构建期"的两个产物。

    ``build_manifest.json`` **不存 trusted 字段**——trusted 由共享解析器按
    "commit 可证 且 dirty 可证"派生（见 ``build_identity._normalize_manifest``），
    所以"trusted=false 的镜像"用 ``commit=unknown`` 或 ``dirty=unknown`` 构造。
    """
    if write_commit_file and file_commit is not None:
        (root / ".build_commit").write_text(f"{file_commit}\n", encoding="utf-8")
    if write_manifest:
        (root / "build_manifest.json").write_text(
            json.dumps(
                {
                    "commit": manifest_commit,
                    "short_commit": str(manifest_commit)[:12],
                    "dirty": dirty,
                    "built_at_utc": BUILD_TIME,
                    "config_schema": "stock-analyzer-config.v1",
                    "runtime_state_schema": 9,
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture
def container_root(tmp_path: Path) -> Path:
    """不可变容器形态：没有 .git（也不依赖 PATH 里有没有 git）。"""
    root = tmp_path / "container"
    root.mkdir()
    _write_container_identity(root)
    assert git_available(root) is False, "夹具没在复刻容器：这个目录竟然是 git 仓库"
    return root


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=60
    )
    assert out.returncode == 0, (args, out.stderr)
    return out.stdout.strip()


@pytest.fixture
def git_root(tmp_path: Path) -> Path:
    """源码检出形态：真 git 仓库（真 HEAD，不是 mock）。"""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "runtime-identity@example.invalid")
    _git(root, "config", "user.name", "Runtime Identity Test")
    (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "module.py")
    _git(root, "commit", "-q", "-m", "init")
    assert git_available(root) is True
    return root


# ---------------------------------------------------------------------------
# A. 容器形态（BLK-D1 的核心）
# ---------------------------------------------------------------------------


def test_container_identity_resolves_to_build_identity(container_root: Path):
    resolved = resolve_runtime_code_identity(
        container_root, validation_mode="production", require_build_identity=True
    )
    assert resolved.git_available is False
    assert resolved.identity_source == IDENTITY_SOURCE_CONTAINER_BUILD
    assert resolved.code_commit == SHA
    assert resolved.code_commit_source == CODE_COMMIT_SOURCE_CONTAINER
    assert resolved.build_commit_present is True
    assert resolved.build_manifest_present is True
    assert resolved.build_manifest_trusted is True
    assert resolved.build_manifest_dirty is False
    assert resolved.violations == ()
    assert resolved.identity_verified is True
    # 容器里"git HEAD"这一项不适用，如实是 unknown——不是错误，也不参与对账
    assert resolved.git_head == "unknown"
    assert resolved.git_worktree_state is None


def test_container_freeze_gate_passes_where_worktree_gate_could_not(container_root: Path):
    """BLK-D1：容器里 freeze 曾经先撞"无法证明工作区干净"而永远 exit 5。"""
    resolved = resolve_runtime_code_identity(
        container_root, validation_mode="production", require_build_identity=True
    )
    # 旧路径（拿 worktree 当门）在容器里必然失败——这解释了 BLK-D1 的 exit 5
    with pytest.raises(FreezeGateError) as excinfo:
        assert_worktree_clean(resolved.git_worktree_state, validation_mode="production")
    assert excinfo.value.exit_code == 5
    # 新路径：容器形态改用构建身份，同一个环境放行
    assert assert_runtime_identity(resolved, validation_mode="production") == SHA


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        ({"write_commit_file": False}, ".build_commit 缺失"),
        ({"write_manifest": False}, "build_manifest.json 缺失"),
        ({"manifest_commit": OTHER_SHA}, "两源 commit 不同"),
        ({"file_commit": OTHER_SHA}, "两源 commit 不同（另一侧）"),
        ({"manifest_commit": ""}, "manifest.commit 缺失"),
        ({"manifest_commit": "not-a-sha"}, "manifest.commit 形态非法"),
        ({"manifest_commit": "unknown"}, "manifest.commit 是 unknown"),
        ({"file_commit": "unknown"}, ".build_commit 是 unknown"),
        ({"dirty": True}, "dirty=true"),
        ({"dirty": "unknown"}, "dirty 无证据"),
        ({"manifest_commit": "unknown", "file_commit": "unknown"}, "整体不可证"),
    ],
)
def test_container_fail_closed_matrix(container_root: Path, broken, reason):
    """§20 对抗矩阵：容器形态任一不满足都必须 FAIL（exit 5）。

    ⚠️ 夹具已经在 ``container_root`` 里写过一份完好身份，所以这里要先清掉再按
    ``broken`` 重写，避免"旧文件残留 → 看起来通过"。
    """
    for name in (".build_commit", "build_manifest.json"):
        (container_root / name).unlink(missing_ok=True)
    _write_container_identity(container_root, **broken)
    resolved = resolve_runtime_code_identity(
        container_root, validation_mode="production", require_build_identity=True
    )
    assert resolved.identity_verified is False, f"{reason} 竟然通过了"
    assert resolved.violations, f"{reason} 没有给出违例原因"
    with pytest.raises(FreezeGateError) as excinfo:
        assert_runtime_identity(resolved, validation_mode="production")
    assert excinfo.value.exit_code == 5


def test_container_manifest_unparsable_is_missing_not_trusted(container_root: Path):
    """不可解析的清单必须按"缺失"处理，绝不退回环境变量冒充产物存在。"""
    (container_root / "build_manifest.json").write_text("{ not json", encoding="utf-8")
    block = build_identity_block(str(container_root))
    assert block["build_manifest_present"] is False
    assert block["build_manifest_trusted"] is False
    resolved = resolve_runtime_code_identity(
        container_root, validation_mode="production", require_build_identity=True
    )
    assert resolved.identity_verified is False
    assert any("build_manifest.json" in v for v in resolved.violations)


def test_container_code_commit_never_unknown(container_root: Path):
    """BLK-D2 的正面要求：容器里 code_commit 必须可证，不能是 unknown。"""
    resolved = resolve_runtime_code_identity(container_root, validation_mode="production")
    assert resolved.code_commit not in {"", "unknown"}
    assert resolved.code_commit == SHA


def test_container_override_must_match_build_identity(container_root: Path):
    # 与构建身份一致 → 放行，但来源标签如实标"显式声明 + 构建身份互证"
    ok = resolve_runtime_code_identity(
        container_root, requested_code_commit=SHA, validation_mode="production"
    )
    assert ok.identity_verified is True
    assert ok.code_commit_source == CODE_COMMIT_SOURCE_CLI_OVERRIDE_CONTAINER
    # 不一致 → reject
    bad = resolve_runtime_code_identity(
        container_root, requested_code_commit=OTHER_SHA, validation_mode="production"
    )
    assert bad.identity_verified is False
    with pytest.raises(FreezeGateError):
        assert_runtime_identity(bad, validation_mode="production")


def test_non_production_modes_skip_identity_gate(tmp_path: Path):
    """rehearsal/test 不做身份硬门（清单会如实标 validation_mode），但不崩。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    for mode in ("rehearsal", "test"):
        resolved = resolve_runtime_code_identity(empty, validation_mode=mode)
        assert resolved.identity_verified is True
        assert resolved.violations == ()
        assert assert_runtime_identity(resolved, validation_mode=mode) == resolved.code_commit


# ---------------------------------------------------------------------------
# B. 源码检出形态（不得退化）
# ---------------------------------------------------------------------------


def test_git_checkout_identity_is_head(git_root: Path):
    head = _git(git_root, "rev-parse", "HEAD")
    resolved = resolve_runtime_code_identity(git_root, validation_mode="production")
    assert resolved.git_available is True
    assert resolved.identity_source == IDENTITY_SOURCE_GIT_CHECKOUT
    assert resolved.code_commit == head
    assert resolved.code_commit_source == CODE_COMMIT_SOURCE_GIT
    assert resolved.identity_verified is True
    assert resolved.git_worktree_state == []


def test_git_checkout_capture_mode_tolerates_missing_build_identity(git_root: Path):
    """capture/mature 形态（require_build_identity=False）：检出里没这两个文件是常态。"""
    resolved = resolve_runtime_code_identity(git_root, validation_mode="production")
    assert resolved.identity_verified is True
    assert resolved.build_manifest_present is False
    assert resolved.build_commit_present is False
    # 但生产 freeze 形态（require_build_identity=True）必须要求它们存在（R3 语义不变）
    strict = resolve_runtime_code_identity(
        git_root, validation_mode="production", require_build_identity=True
    )
    assert strict.identity_verified is False
    assert any(".build_commit" in v for v in strict.violations)


def test_git_dirty_worktree_rejected_by_freeze_gate(git_root: Path):
    (git_root / "uncommitted_note.txt").write_text("dirty\n", encoding="utf-8")
    resolved = resolve_runtime_code_identity(git_root, validation_mode="production")
    # 身份本身仍可证（commit 就是 HEAD）；"脏"是**另一个轴**，由 freeze 门单独拦
    assert resolved.identity_verified is True
    assert resolved.git_worktree_state == ["?? uncommitted_note.txt"]
    with pytest.raises(FreezeGateError) as excinfo:
        assert_runtime_identity(resolved, validation_mode="production")
    assert excinfo.value.exit_code == 5
    assert "uncommitted_note.txt" in excinfo.value.args[0]
    # 清理后同一个环境放行（证明拒绝的是"脏"，不是"检出形态"）
    (git_root / "uncommitted_note.txt").unlink()
    assert assert_runtime_identity(
        resolve_runtime_code_identity(git_root, validation_mode="production"),
        validation_mode="production",
    ) == _git(git_root, "rev-parse", "HEAD")


def test_git_build_identity_files_are_whitelisted_when_consistent(git_root: Path):
    """部署期身份文件是**未跟踪白名单**：只要与 HEAD 一致就不算脏。"""
    head = _git(git_root, "rev-parse", "HEAD")
    _write_container_identity(git_root, file_commit=head, manifest_commit=head)
    resolved = resolve_runtime_code_identity(
        git_root, validation_mode="production", require_build_identity=True
    )
    assert resolved.git_worktree_state == []
    assert resolved.identity_verified is True
    assert assert_runtime_identity(resolved, validation_mode="production") == head


def test_git_build_identity_conflict_is_rejected(git_root: Path):
    """检出里存在构建身份但与 HEAD 矛盾 → 必须吵（不许静默忽略）。"""
    _write_container_identity(git_root, file_commit=OTHER_SHA, manifest_commit=OTHER_SHA)
    resolved = resolve_runtime_code_identity(
        git_root, validation_mode="production", require_build_identity=True
    )
    assert resolved.identity_verified is False
    joined = "；".join(resolved.violations)
    assert ".build_commit" in joined and "build_manifest.commit" in joined
    assert resolved.code_commit == _git(git_root, "rev-parse", "HEAD")
    with pytest.raises(FreezeGateError):
        assert_runtime_identity(resolved, validation_mode="production")


def test_git_override_must_match_head(git_root: Path):
    head = _git(git_root, "rev-parse", "HEAD")
    assert (
        resolve_runtime_code_identity(
            git_root, requested_code_commit=head, validation_mode="production"
        ).identity_verified
        is True
    )
    bad = resolve_runtime_code_identity(
        git_root, requested_code_commit=OTHER_SHA, validation_mode="production"
    )
    assert bad.identity_verified is False
    with pytest.raises(FreezeGateError):
        assert_runtime_identity(bad, validation_mode="production")


def test_unknown_git_state_without_container_identity_is_rejected(tmp_path: Path):
    """§21 末条：既拿不到 git、又没有合法容器身份 → FAIL。"""
    bare = tmp_path / "nothing"
    bare.mkdir()
    resolved = resolve_runtime_code_identity(
        bare, validation_mode="production", require_build_identity=True
    )
    assert resolved.git_available is False
    assert resolved.code_commit in {"", "unknown"}
    assert resolved.identity_verified is False
    with pytest.raises(FreezeGateError) as excinfo:
        assert_runtime_identity(resolved, validation_mode="production")
    assert excinfo.value.exit_code == 5


# ---------------------------------------------------------------------------
# C. 冻结身份对账（capture / mature / validation freeze 三者共用同一 code_commit）
# ---------------------------------------------------------------------------


def test_runtime_commit_must_equal_frozen_epoch_identity(container_root: Path):
    """freeze 时冻结的 code_commit 与运行期 code_commit 必须逐位一致。"""
    resolved = resolve_runtime_code_identity(
        container_root, validation_mode="production", require_build_identity=True
    )
    record = open_epoch(
        root=container_root / "artifacts",
        epoch_id="alpha_v2_epoch_901",
        freeze_manifest_hash="hash",
        identity={"code_commit": resolved.code_commit},
        opened_on_date="2026-09-19",
    )
    keys = ("code_commit",)
    assert epoch_identity_matches(record, {"code_commit": resolved.code_commit}, keys=keys) == []
    violations = epoch_identity_matches(record, {"code_commit": OTHER_SHA}, keys=keys)
    assert violations and "code_commit" in violations[0]
    # 身份缺失同样算违例（fail-closed，不是"跳过比较"）
    assert epoch_identity_matches(record, {"code_commit": ""}, keys=keys) != []


# ---------------------------------------------------------------------------
# D. CLI 接线契约（BLK-D2：不许再各自 git_head()）
# ---------------------------------------------------------------------------


def _called_names(source: str) -> set[str]:
    """源码里**实际被调用**的函数名集合（用 AST，避免被注释/文档字符串误伤）。"""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


@pytest.mark.parametrize(
    "script",
    [
        "alpha_v2_validation_freeze.py",
        "alpha_v2_shadow_model_freeze.py",
        "alpha_v2_shadow_capture.py",
        "alpha_v2_shadow_mature.py",
    ],
)
def test_alpha_v2_clis_use_shared_resolver_not_git_head(script: str):
    """BLK-D2 回归闸门：四个 CLI 必须走统一 resolver，且**不再调用** git_head/git_branch。"""
    called = _called_names((REPO_ROOT / "scripts" / script).read_text(encoding="utf-8"))
    assert "resolve_runtime_code_identity" in called, f"{script} 没有走统一 resolver"
    assert "assert_runtime_identity" in called, f"{script} 没有过统一身份门"
    assert "git_head" not in called, f"{script} 仍在直接解析 git HEAD（BLK-D2 回归）"
    assert "git_branch" not in called, f"{script} 仍在直接解析 git 分支"


def test_validation_freeze_requires_build_identity():
    """生产 freeze 必须要求构建产物存在（R3 语义不得被本阶段放松）。"""
    source = (REPO_ROOT / "scripts" / "alpha_v2_validation_freeze.py").read_text(encoding="utf-8")
    assert "require_build_identity=True" in source
    assert "resolve_runtime_code_identity(" in source


# ---------------------------------------------------------------------------
# E. 部署期校验器与运行期 resolver 判定一致（防两套规则漂移）
# ---------------------------------------------------------------------------


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_container_build_identity", VERIFIER_SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"manifest_commit": OTHER_SHA},
        {"write_commit_file": False},
        {"write_manifest": False},
        {"dirty": True},
        {"dirty": "unknown"},
        {"manifest_commit": "not-a-sha"},
    ],
)
def test_deploy_verifier_agrees_with_runtime_resolver(tmp_path: Path, broken):
    root = tmp_path / "case"
    root.mkdir()
    _write_container_identity(root, **broken)
    verifier = _load_verifier()
    verifier_ok, _problems, _facts = verifier.verify(
        manifest_path=str(root / "build_manifest.json"),
        build_commit_file=str(root / ".build_commit"),
        expect_commit=SHA,
    )
    resolved = resolve_runtime_code_identity(
        root, validation_mode="production", require_build_identity=True
    )
    assert verifier_ok is resolved.identity_verified, (
        "部署期校验器与运行期 resolver 判定不一致——两套规则开始漂移"
    )


def test_deploy_verifier_checks_expected_commit(tmp_path: Path):
    """镜像里 commit 正确但与本次部署期望不同 → 必须 FAIL（防"传参对了、镜像是旧的"）。"""
    root = tmp_path / "stale"
    root.mkdir()
    _write_container_identity(root, file_commit=SHA, manifest_commit=SHA)
    verifier = _load_verifier()
    ok, problems, _ = verifier.verify(
        manifest_path=str(root / "build_manifest.json"),
        build_commit_file=str(root / ".build_commit"),
        expect_commit=OTHER_SHA,
    )
    assert ok is False
    assert any("期望" in problem for problem in problems)


# ---------------------------------------------------------------------------
# F. 端到端：no-git 容器沙箱里真跑四个 CLI
# ---------------------------------------------------------------------------


def test_no_git_container_smoke_passes(tmp_path: Path):
    """跑真实 CLI（不是 mock）：A–F 六步在"无 git 的沙箱"里全部走通。

    沙箱没有 .git，所以 resolver 必须自报 ``git_available=false`` 并走构建身份；
    CI 上不额外剥离 PATH（跨平台稳定），"连 git 二进制都没有"那层由
    ``--keep-git-on-path`` 之外的默认运行形态覆盖（见硬化报告 §No-Git Container Smoke）。
    """
    result = subprocess.run(
        [
            sys.executable,
            str(SMOKE_SCRIPT),
            "--out",
            str(tmp_path),
            "--keep-git-on-path",
        ],
        capture_output=True,
        text=True,
        timeout=2400,
        cwd=str(REPO_ROOT),
    )
    evidence = tmp_path / "no_git_container_smoke.json"
    assert evidence.exists(), (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["sandbox_has_git_dir"] is False
    assert payload["verdict"] == "PASS", json.dumps(payload, ensure_ascii=False, indent=2)
    steps = {str(step["step"]).split(" ", 1)[0]: step for step in payload["steps"]}
    assert set(steps) == {"A", "B", "C", "D", "E", "F", "G"}
    assert "container_build_identity" in steps["A"]["detail"]
    assert "container_build_identity" in steps["C"]["detail"]
    # §21 末条在 CLI 层：既无 git 又无构建身份 → 干净 exit 5（不是 traceback）
    assert steps["G"]["container_rc"] == 5
