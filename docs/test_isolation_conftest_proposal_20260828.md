# 测试基建隔离缺口：conftest.py 统一兜底方案建议

## 背景

PR #37（`feat/asof-backtest-holding-curve`）排查 CI flaky 问题时发现：
`tests/test_service_week5.py`、`tests/test_service_evolution_scheduler.py`、
`tests/test_service_idle_queue.py` 三个文件的 `_load_test_config()` 均未把
`market_warehouse.db_path`/`market_warehouse.package_root`/
`data_source.warehouse_db_path` 重定向到 `tmp_path`，默认指向仓库共享真实路径
`artifacts/warehouse/market.duckdb`。这三个字段已在本次修复中补齐（见对应
commit）。

但 `config/default.yaml` 里指向仓库真实 `artifacts/`/`suggestions/`/`staging/`
路径的配置项一共有 30+ 个（`scheduler.leader_lock_path`、
`week5.scan_progress_path`、`evolution.compliance_db_path`、
`training.artifact_path` 等），分散在十几个测试文件里各自手动重定向，容易漏做
——这次的 market_warehouse 缺口就是典型案例，且已确认是系统性问题（
`test_service_week5.py` 自己的注释里就提到过因未隔离
`market_breadth.json` 而踩坑，当时用"关开关"而非"隔离路径"规避，属于治标不治本）。

## 建议方案

在 `tests/conftest.py`（当前项目似乎没有全局 `conftest.py`，需要新建）增加一个
自动生效的 fixture，在每个测试函数运行前，把 `StockAnalyzerConfig` 里所有
指向仓库相对路径 `artifacts/`、`suggestions/`、`staging/` 的字段统一重定向到
当前测试的 `tmp_path`。可选实现思路：

1. **反射式全量重定向**（推荐）：写一个工具函数
   `_redirect_repo_relative_paths_to_tmp(config, tmp_path)`，用 Pydantic 的
   `model_fields`（含嵌套 sub-model）递归遍历所有 `str` 类型字段，凡是值以
   `artifacts/`、`suggestions/`、`staging/` 开头的，统一替换为
   `tmp_path / <原始相对路径>`。这样不需要每次新增配置项时都记得去同步全部
   测试文件的 `_load_test_config()`。
2. **作为 autouse fixture 挂在 conftest.py**：
   ```python
   @pytest.fixture(autouse=True)
   def _isolate_repo_artifacts(monkeypatch, tmp_path):
       # 只在测试主动调用 load_config() 之后生效比较麻烦（config 对象在测试
       # 内部构造），更可行的方式是提供一个显式的 helper 函数
       # `isolate_config_paths(config, tmp_path)`，各测试文件的
       # `_load_test_config()` 在 return 前统一调用它，而不是完全隐式的
       # autouse patch（因为 config 构造时机因文件而异，autouse fixture
       # 拿不到 config 实例）。
       ...
   ```
   即：不追求完全隐式自动生效（技术上较难，因为每个文件的 `_load_test_config()`
   构造 config 的时机和方式不同），而是提供一个**统一、显式调用**的
   `isolate_config_paths(config, tmp_path)` helper，各文件在自己的
   `_load_test_config()` 末尾加一行调用它，替代当前"各自手动列举要重定向哪些
   字段"的模式。这样新增配置项时，只要该字段值符合
   `artifacts/`/`suggestions/`/`staging/` 前缀约定，就会被自动兜底，不需要
   逐个测试文件同步维护。
3. 对于**有意保留真实路径**的测试（例如 `test_release_preflight.py` 验证
   "生产配置默认值确实是 artifacts/warehouse/market.duckdb"），可以在调用
   `isolate_config_paths()` 之后再显式覆盖回真实路径，或者干脆不调用该
   helper——两种都比现状（隐式遗漏）更安全，因为"不调用兜底 helper"本身就是
   一个清晰、可 review 的信号。

## 风险与工作量

- 这是**测试基建重构**，不是 bug 修复，改动面覆盖当前所有测试文件的
  `_load_test_config()`（数量较多），需要逐一确认没有测试**故意**依赖真实
  `artifacts/` 路径下的既有数据（例如某些验收类测试可能故意读取仓库自带的
  `artifacts/model_v1.json` 种子模型）。
- 建议作为独立任务排期，配合一次全量测试套件跑通验证，不要在小修复 PR 里
  顺带做。

本文档仅记录建议，本轮 PR #37 未实施，只做了三个文件的点修复。
