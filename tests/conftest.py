"""Test bootstrap."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS_DIR = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# tests/ 目录本身也需要显式加入 sys.path：conftest.py 在 pytest 收集测试文件
# 之前就被加载，此时 pytest 的按测试文件目录插入 sys.path 的机制还未生效，
# 直接 import tests/ 下的普通模块（如 _path_isolation.py）会失败。
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Keep pytest deterministic: disable long bootstrap and state persistence side effects.
os.environ.setdefault("SA__TRAINING__BOOTSTRAP_AUTO_RUN_ON_FIRST_START", "false")
os.environ.setdefault("SA__TRAINING__BOOTSTRAP_REQUIRE_COMPLETION_FOR_RUNTIME", "false")
os.environ.setdefault("SA__TRAINING__BOOTSTRAP_AUTO_SEED_WATCHLIST", "false")
os.environ.setdefault("SA__TRAINING__BOOTSTRAP_RETRY_ENABLED", "false")
os.environ.setdefault("SA__COMMAND_CHANNEL__STATE_PERSIST_ENABLED", "false")
os.environ.setdefault("SA__IDLE_QUEUE__RESOURCE_PAUSE_ENABLED", "false")
# Isolate the training bootstrap state (and its sibling learning_protocol.duckdb)
# out of the repo's artifacts/: the default shared path makes parallel workers
# contend on the DuckDB file lock (same class of fix as 2d551d1). Tests that
# set their own path explicitly are unaffected (setdefault semantics).
os.environ.setdefault(
    "SA__TRAINING__BOOTSTRAP_STATE_PATH",
    str(
        Path(tempfile.gettempdir())
        / "stock_analyzer_tests"
        / "bootstrap_state_conftest.json"
    ),
)
# 内容寻址 bundle 归档目录同样隔离出仓库 artifacts/，避免训练入口测试污染工作区。
os.environ.setdefault(
    "SA__TRAINING__MODEL_ARCHIVE_DIR",
    str(Path(tempfile.gettempdir()) / "stock_analyzer_tests" / "model_archive"),
)
# Local config files may enable API authentication for a developer runtime.
# Tests start with authentication disabled unless a test explicitly overrides it.
os.environ["SA__SECURITY__API_AUTH_ENABLED"] = "false"
os.environ["SA__SECURITY__API_TOKEN"] = ""
# PRD §8.7 trading-parameter freeze is disabled for tests by default so the
# suite stays green at any wall-clock time; freeze tests enable it explicitly.
os.environ["SA__PARAM_FREEZE__ENABLED"] = "false"
# Use a strong secret for tests so command channel is not rejected by weak-secret guard.
os.environ.setdefault("SA__COMMAND_CHANNEL__SECRET_KEY", "test-strong-secret-for-pytest-only")


# ---------------------------------------------------------------------------
# 测试路径隔离统一兜底（P2 技术债，方案见
# docs/test_isolation_conftest_proposal_20260828.md）。实现放在
# tests/_path_isolation.py（而非本文件内联定义），因为 pytest 加载
# conftest.py 的模块注册机制与普通 ``import conftest`` 不兼容，其它测试
# 文件无法 ``from conftest import isolate_config_paths``；普通模块文件
# 没有这个限制。这里只做 re-export，方便偶尔从 conftest 语境引用。
# ---------------------------------------------------------------------------
from _path_isolation import isolate_config_paths as isolate_config_paths  # noqa: E402
