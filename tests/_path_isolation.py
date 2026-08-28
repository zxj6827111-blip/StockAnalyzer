"""统一测试路径隔离 helper（P2 技术债，方案见
docs/test_isolation_conftest_proposal_20260828.md）。

单独放在这个模块（而非 tests/conftest.py）是因为 pytest 用特殊机制加载
conftest.py，注册进 sys.modules 的模块名与普通 ``import conftest`` 不兼容
——其它测试文件 ``from conftest import isolate_config_paths`` 会拿到
``ModuleNotFoundError``（pytest 收集期验证过）。普通模块文件不受这个限制，
可以被各测试文件正常 import。

背景：config/default.yaml 里有 30+ 个字段指向仓库相对路径
artifacts/ / suggestions/ / staging/（scheduler.leader_lock_path、
week5.scan_progress_path、evolution.compliance_db_path 等）。此前各测试
文件的 _load_test_config() 各自手动列举要重定向哪些字段，容易遗漏——
test_service_week5.py 曾因未隔离 market_warehouse.db_path 等字段读到仓库
共享真实文件，成为 PR #37 的 CI flaky 根因。

isolate_config_paths() 提供一个统一、显式调用的兜底：用 Pydantic v2 的
model_fields 递归遍历 config 的所有子模型，把值以 artifacts/ /
suggestions/ / staging/ 开头的字符串字段统一重定向到 tmp_path 下的同名
相对路径。各测试文件的 _load_test_config() 在 return 前调用一次即可，
新增配置项只要符合这个路径前缀约定就会被自动兜底，不需要同步维护调用点。

有意不做成 autouse fixture：config 对象的构造时机和方式因测试文件而异
（大多是模块级函数各自 load_config() 再逐个覆盖字段），autouse fixture
拿不到构造中的 config 实例，只能提供显式调用的 helper。
"""

from __future__ import annotations

from pathlib import Path

_ISOLATED_PATH_PREFIXES = ("artifacts/", "suggestions/", "staging/")


def isolate_config_paths(config: object, tmp_path: Path) -> object:
    """Redirect every repo-relative artifacts//suggestions//staging/ path field
    (recursively, across nested sub-models) to ``tmp_path``.

    Mutates ``config`` in place and returns it for convenient chaining.
    Values that are not plain repo-relative paths (absolute paths, values
    under a different prefix, non-string fields) are left untouched — tests
    that intentionally keep a real path (e.g. validating that the production
    default really is ``artifacts/warehouse/market.duckdb``) can call this
    first and then override the specific field back afterwards.
    """
    _redirect_model_fields(config, tmp_path)
    return config


def _redirect_model_fields(node: object, tmp_path: Path) -> None:
    model_fields = getattr(type(node), "model_fields", None)
    if not isinstance(model_fields, dict):
        return
    for name in model_fields:
        value = getattr(node, name, None)
        if isinstance(value, str):
            redirected = _redirect_if_repo_relative(value, tmp_path)
            if redirected is not None:
                setattr(node, name, redirected)
        elif isinstance(value, dict):
            for item in value.values():
                _redirect_model_fields(item, tmp_path)
        elif isinstance(value, list):
            for item in value:
                _redirect_model_fields(item, tmp_path)
        elif hasattr(type(value), "model_fields"):
            _redirect_model_fields(value, tmp_path)


def _redirect_if_repo_relative(value: str, tmp_path: Path) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    # 排除绝对路径和 Windows 盘符路径（如 D:\通达信\vipdoc）：只重定向真正
    # 意图指向仓库相对目录的配置项，避免误伤本来就该指向别处的字段。
    if normalized.startswith(("/", "\\")) or (len(normalized) > 1 and normalized[1] == ":"):
        return None
    posix_normalized = normalized.replace("\\", "/")
    for prefix in _ISOLATED_PATH_PREFIXES:
        if posix_normalized.startswith(prefix):
            return str(tmp_path / posix_normalized)
    return None
