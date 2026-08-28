"""测试 tests/conftest.py 的 isolate_config_paths 统一路径隔离 helper。

对应 docs/test_isolation_conftest_proposal_20260828.md 的方案落地：反射式
遍历 StockAnalyzerConfig 的所有嵌套子模型，把 artifacts//suggestions//
staging/ 前缀的字符串字段重定向到 tmp_path，替代此前各测试文件手动列举字段
的模式（该模式曾漏做 market_warehouse 相关字段，是 PR #37 CI flaky 根因）。
"""

from __future__ import annotations

from pathlib import Path

from _path_isolation import isolate_config_paths
from stock_analyzer.config import load_config

_ROOT = Path(__file__).resolve().parents[1]


def _default_config():
    return load_config(_ROOT / "config" / "default.yaml")


def test_isolate_config_paths_redirects_top_level_artifacts_field(tmp_path: Path) -> None:
    config = _default_config()
    original = config.scheduler.leader_lock_path
    assert original.startswith("artifacts/")

    isolate_config_paths(config, tmp_path)

    assert config.scheduler.leader_lock_path == str(tmp_path / original)


def test_isolate_config_paths_redirects_nested_submodel_field(tmp_path: Path) -> None:
    config = _default_config()
    original = config.week5.scan_progress_path
    assert original.startswith("artifacts/")

    isolate_config_paths(config, tmp_path)

    assert config.week5.scan_progress_path == str(tmp_path / original)


def test_isolate_config_paths_redirects_market_warehouse_fields(tmp_path: Path) -> None:
    """回归 PR #37 CI flaky 的具体触发字段：market_warehouse 三个路径字段。"""
    config = _default_config()
    original_db_path = config.market_warehouse.db_path
    original_package_root = config.market_warehouse.package_root
    original_warehouse_db_path = config.data_source.warehouse_db_path

    isolate_config_paths(config, tmp_path)

    assert config.market_warehouse.db_path == str(tmp_path / original_db_path)
    assert config.market_warehouse.package_root == str(tmp_path / original_package_root)
    assert config.data_source.warehouse_db_path == str(tmp_path / original_warehouse_db_path)


def test_isolate_config_paths_leaves_non_repo_relative_fields_untouched(tmp_path: Path) -> None:
    """Windows 盘符路径 / 绝对路径 / 空字符串不应被误伤为仓库相对路径。"""
    config = _default_config()
    config.tdx_sync.vipdoc_root = "D:\\通达信\\vipdoc"

    isolate_config_paths(config, tmp_path)

    assert config.tdx_sync.vipdoc_root == "D:\\通达信\\vipdoc"


def test_isolate_config_paths_returns_the_same_mutated_config(tmp_path: Path) -> None:
    config = _default_config()

    returned = isolate_config_paths(config, tmp_path)

    assert returned is config


def test_isolate_config_paths_allows_explicit_override_afterwards(tmp_path: Path) -> None:
    """方案文档要求：调用后仍可显式覆盖回真实路径，用于故意验证生产默认值的测试。"""
    config = _default_config()
    real_default = config.market_warehouse.db_path

    isolate_config_paths(config, tmp_path)
    config.market_warehouse.db_path = real_default

    assert config.market_warehouse.db_path == real_default
