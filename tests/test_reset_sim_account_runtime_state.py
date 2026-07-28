from __future__ import annotations

import json
from pathlib import Path

from scripts.reset_sim_account_runtime_state import main as reset_main


def test_reset_sim_account_runtime_state_script(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "runtime_state.json"
    state_path.write_text(
        json.dumps(
            {
                "current_equity": 0.809307,
                "pause_new_buy": True,
                "portfolio": {
                    "trade_seq": 3,
                    "positions": [{"symbol": "600000", "target_position": 0.1}],
                    "trades": [{"symbol": "600000", "side": "buy"}],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "reset_sim_account_runtime_state.py",
            "--state",
            str(state_path),
            "--equity",
            "1.0",
        ],
    )
    assert reset_main() == 0
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["current_equity"] == 1.0
    assert payload["pause_new_buy"] is False
    assert payload["portfolio"] == {"trade_seq": 0, "positions": [], "trades": []}
    backups = list(tmp_path.glob("runtime_state.json.bak.*"))
    assert len(backups) == 1
