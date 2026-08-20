"""Tests for nightly_readiness single authoritative path and mirror drain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_analyzer.ops.nightly_readiness import (
    authoritative_readiness_path,
    check_nightly_readiness,
    consume_nightly_readiness,
    read_nightly_readiness,
    write_nightly_readiness,
)


def test_write_to_authoritative_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    assert authoritative_readiness_path() == auth
    written = write_nightly_readiness(target_trade_date="2026-08-20", extra={"source": "test"})
    assert written == auth
    assert auth.exists()
    payload = read_nightly_readiness()
    assert payload is not None
    assert payload["target_trade_date"] == "2026-08-20"
    gate = check_nightly_readiness(expected_trade_date="2026-08-20")
    assert gate.ready is True
    gate2 = check_nightly_readiness(expected_trade_date="2026-08-19")
    assert gate2.ready is False


def test_consume_drains_all_mirrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = tmp_path / "auth" / "artifacts" / "runtime" / "nightly_data_ready.json"
    legacy = tmp_path / "legacy" / "artifacts" / "runtime" / "nightly_data_ready.json"
    # Write to authoritative
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    write_nightly_readiness(target_trade_date="2026-08-20")
    # Simulate legacy mirror with same payload
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(auth.read_text(encoding="utf-8"), encoding="utf-8")
    # Monkeypatch candidate list to include both
    import stock_analyzer.ops.nightly_readiness as mod

    orig_candidates = mod._candidate_readiness_paths

    def _patched_candidates() -> list[Path]:
        return [auth, legacy]

    monkeypatch.setattr(mod, "_candidate_readiness_paths", _patched_candidates)
    # Consume should drain both
    payload = consume_nightly_readiness()
    assert payload is not None
    assert not auth.exists()
    assert not legacy.exists()
    # No re-read
    assert read_nightly_readiness() is None
    # Restore
    monkeypatch.setattr(mod, "_candidate_readiness_paths", orig_candidates)


def test_batch_readiness_file_contains_expected_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    write_nightly_readiness(
        target_trade_date="2026-08-19",
        db_path="/tmp/delta.duckdb",
        index_path="/tmp/index.json",
        extra={"source": "batch_update"},
    )
    payload = json.loads(auth.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["daily"]["ok"] is True
    assert payload["target_trade_date"] == "2026-08-19"
