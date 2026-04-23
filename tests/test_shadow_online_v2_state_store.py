from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer.evolution.shadow_online_v2_state_store import ShadowOnlineV2StateStore


def test_shadow_online_v2_state_store_round_trips_state_payload(tmp_path: Path) -> None:
    path = tmp_path / "shadow_v2" / "state.json"
    store = ShadowOnlineV2StateStore(path)

    payload = store.save_state(
        state={
            "engine": "protocol_shadow_online_v2_lr",
            "bias": 0.12,
            "weights": {"shadow_p_meta": 0.5, "execution_fill_ratio": 0.2},
            "feature_names": ["shadow_p_meta", "execution_fill_ratio"],
            "cumulative_updates": 18,
        },
        now=datetime(2026, 3, 25, 12, 0, tzinfo=UTC),
        metadata={"source": "unit_test"},
    )
    loaded_payload = store.load_payload()
    loaded_state = store.load_state()
    status = store.status()

    assert payload["schema_version"] == "1"
    assert loaded_payload["engine"] == "protocol_shadow_online_v2_lr"
    assert loaded_state["cumulative_updates"] == 18
    assert loaded_state["weights"] == {
        "shadow_p_meta": 0.5,
        "execution_fill_ratio": 0.2,
    }
    assert status["exists"] is True
    assert status["feature_count"] == 2
    assert status["cumulative_updates"] == 18


def test_shadow_online_v2_state_store_handles_missing_or_invalid_payload(tmp_path: Path) -> None:
    path = tmp_path / "shadow_v2" / "state.json"
    store = ShadowOnlineV2StateStore(path)

    assert store.load_payload() == {}
    assert store.load_state() == {}
    assert store.status()["exists"] is False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{invalid-json", encoding="utf-8")

    assert store.load_payload() == {}
    assert store.load_state() == {}
    assert store.status()["feature_count"] == 0
