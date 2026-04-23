"""Test bootstrap."""

from __future__ import annotations

import os
import sys
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
