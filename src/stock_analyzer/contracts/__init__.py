"""Alpha V2 契约（蓝图 §4 / §15）。"""

from __future__ import annotations

from stock_analyzer.contracts.alpha_v2 import (
    LEGACY_PROFILE_CONTRACT_ID,
    NIGHT_ALPHA_V2_CONTRACT_ID,
    NIGHT_CONTRACT_PROFILES,
    SelectionContract,
    resolve_selection_contract,
)

__all__ = [
    "LEGACY_PROFILE_CONTRACT_ID",
    "NIGHT_ALPHA_V2_CONTRACT_ID",
    "NIGHT_CONTRACT_PROFILES",
    "SelectionContract",
    "resolve_selection_contract",
]
