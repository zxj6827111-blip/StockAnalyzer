"""Test bootstrap."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

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
