"""Tests for nightly_readiness single authoritative path and mirror drain."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from stock_analyzer.ops.nightly_readiness import (
    authoritative_readiness_path,
    check_nightly_readiness,
    consume_nightly_readiness,
    read_nightly_readiness,
    write_nightly_readiness,
)


def _write_artifacts(
    tmp_path: Path,
    *,
    target_trade_date: str,
    index_symbols: tuple[str, ...] = ("000001", "600000"),
    delta_symbols: tuple[str, ...] | None = None,
) -> tuple[Path, Path]:
    index_path = tmp_path / "vendor_overlay" / "daily_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "symbols_total": len(index_symbols),
                "symbols": {symbol: {"latest_date": target_trade_date} for symbol in index_symbols},
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "vendor_delta" / "market_delta.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stored_symbols = delta_symbols if delta_symbols is not None else index_symbols
    with duckdb.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE daily_bars (symbol VARCHAR, date DATE)")
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?)",
            [(symbol, target_trade_date) for symbol in stored_symbols],
        )
    return index_path, db_path


def test_write_to_authoritative_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    index_path, db_path = _write_artifacts(
        tmp_path,
        target_trade_date="2026-08-20",
    )
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    assert authoritative_readiness_path() == auth
    written = write_nightly_readiness(
        target_trade_date="2026-08-20",
        index_path=index_path,
        db_path=db_path,
        extra={"source": "test"},
    )
    assert written == auth
    assert auth.exists()
    payload = read_nightly_readiness()
    assert payload is not None
    assert payload["target_trade_date"] == "2026-08-20"
    assert payload["index"]["symbols_on_target_date"] == 2
    assert payload["delta"]["coverage_ratio"] == 1.0
    gate = check_nightly_readiness(expected_trade_date="2026-08-20")
    assert gate.ready is True
    gate2 = check_nightly_readiness(expected_trade_date="2026-08-19")
    assert gate2.ready is False


def test_consume_drains_all_mirrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = tmp_path / "auth" / "artifacts" / "runtime" / "nightly_data_ready.json"
    legacy = tmp_path / "legacy" / "artifacts" / "runtime" / "nightly_data_ready.json"
    index_path, db_path = _write_artifacts(
        tmp_path,
        target_trade_date="2026-08-20",
    )
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    write_nightly_readiness(
        target_trade_date="2026-08-20",
        index_path=index_path,
        db_path=db_path,
    )
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(auth.read_text(encoding="utf-8"), encoding="utf-8")
    import stock_analyzer.ops.nightly_readiness as mod

    orig_candidates = mod._candidate_readiness_paths

    def _patched_candidates() -> list[Path]:
        return [auth, legacy]

    monkeypatch.setattr(mod, "_candidate_readiness_paths", _patched_candidates)
    payload = consume_nightly_readiness()
    assert payload is not None
    assert not auth.exists()
    assert not legacy.exists()
    assert read_nightly_readiness() is None
    monkeypatch.setattr(mod, "_candidate_readiness_paths", orig_candidates)


def test_batch_readiness_file_contains_expected_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    index_path, db_path = _write_artifacts(
        tmp_path,
        target_trade_date="2026-08-19",
    )
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    write_nightly_readiness(
        target_trade_date="2026-08-19",
        db_path=db_path,
        index_path=index_path,
        extra={"source": "batch_update"},
    )
    payload = json.loads(auth.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["daily"]["ok"] is True
    assert payload["index"]["latest_trade_date"] == "2026-08-19"
    assert payload["delta"]["symbols_on_target_date"] == 2
    assert payload["target_trade_date"] == "2026-08-19"


def test_write_requires_index_and_delta_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="index_path is required"):
        write_nightly_readiness(
            target_trade_date="2026-08-20",
            path=tmp_path / "ready.json",
        )


def test_write_rejects_incomplete_delta_coverage(tmp_path: Path) -> None:
    index_path, db_path = _write_artifacts(
        tmp_path,
        target_trade_date="2026-08-20",
        delta_symbols=("000001",),
    )
    with pytest.raises(ValueError, match="coverage is incomplete"):
        write_nightly_readiness(
            target_trade_date="2026-08-20",
            index_path=index_path,
            db_path=db_path,
            path=tmp_path / "ready.json",
        )


def test_schema_v1_readiness_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "nightly_data_ready.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_trade_date": "2026-08-20",
                "daily": {"ok": True},
                "index": {"ok": True},
                "delta": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    gate = check_nightly_readiness(
        expected_trade_date="2026-08-20",
        path=path,
    )
    assert gate.ready is False
    assert gate.reason == "nightly_data_not_ready"


def test_invalidate_retires_all_mirrors_and_keeps_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """invalidate 必须原子失效 authoritative 与全部 legacy mirror。

    read_nightly_readiness 在 authoritative 缺失时会回退读 legacy mirror，
    因此更新开始前的失效不能只处理一个文件；consumed 文件不属于 candidate
    列表，必须原样保留供审计。
    """
    import stock_analyzer.ops.nightly_readiness as mod
    from stock_analyzer.ops.nightly_readiness import invalidate_nightly_readiness

    auth = tmp_path / "auth" / "artifacts" / "runtime" / "nightly_data_ready.json"
    legacy = tmp_path / "legacy" / "artifacts" / "runtime" / "nightly_data_ready.json"
    consumed = tmp_path / "auth" / "artifacts" / "runtime" / (
        "nightly_data_ready.consumed.json"
    )
    auth.parent.mkdir(parents=True, exist_ok=True)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema_version": 2, "target_trade_date": "2026-08-20"})
    auth.write_text(payload, encoding="utf-8")
    legacy.write_text(payload, encoding="utf-8")
    consumed.write_text(payload, encoding="utf-8")

    def _patched_candidates() -> list[Path]:
        return [auth, legacy]

    monkeypatch.setattr(mod, "_candidate_readiness_paths", _patched_candidates)
    invalidated = invalidate_nightly_readiness(stamp="20260821T120000Z")

    assert sorted(Path(item).name for item in invalidated) == [
        "nightly_data_ready.json",
        "nightly_data_ready.json",
    ]
    assert not auth.exists()
    assert not legacy.exists()
    stale_files = sorted(auth.parent.glob("nightly_data_ready.stale-*"))
    assert len(stale_files) == 1
    # stale 文件保留原始 payload，供故障审计；文件名带失效时间戳。
    assert json.loads(stale_files[0].read_text(encoding="utf-8"))["target_trade_date"] == (
        "2026-08-20"
    )
    assert "20260821T120000Z" in stale_files[0].name
    # consumed 文件不受影响。
    assert consumed.exists()


def test_invalidate_without_any_readiness_is_noop(tmp_path: Path) -> None:
    from stock_analyzer.ops.nightly_readiness import invalidate_nightly_readiness

    missing = tmp_path / "does-not-exist" / "nightly_data_ready.json"

    def _patched_candidates() -> list[Path]:
        return [missing]

    import stock_analyzer.ops.nightly_readiness as mod

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(mod, "_candidate_readiness_paths", _patched_candidates)
        assert invalidate_nightly_readiness() == []
    finally:
        monkey.undo()


def test_invalidate_reports_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生产强制模式必须能感知失效失败，不能留下可消费的旧文件。"""
    import stock_analyzer.ops.nightly_readiness as mod

    path = tmp_path / "runtime" / "nightly_data_ready.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": 2, "target_trade_date": "2026-08-20"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_candidate_readiness_paths", lambda: [path])

    def _deny_replace(source: Path, target: Path) -> None:
        _ = source, target
        raise PermissionError("read-only readiness directory")

    monkeypatch.setattr(mod.os, "replace", _deny_replace)

    with pytest.raises(OSError, match="failed to invalidate"):
        mod.invalidate_nightly_readiness(stamp="20260822T000000Z")
    assert path.exists()
