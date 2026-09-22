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


def _write_artifacts_with_entries(
    tmp_path: Path,
    *,
    target_trade_date: str,
    solid_symbols: dict[str, list[dict[str, object]]],
    hollow_symbols: tuple[str, ...] = (),
    delta_symbols: tuple[str, ...] | None = None,
) -> tuple[Path, Path]:
    """构造区分 entries 非空（有 ZIP 数据）与 entries 空列表（新股占位）的索引。"""
    index_path = tmp_path / "vendor_overlay" / "daily_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    symbols: dict[str, dict[str, object]] = {}
    for symbol, entries in solid_symbols.items():
        symbols[symbol] = {"latest_date": target_trade_date, "entries": entries}
    for symbol in hollow_symbols:
        symbols[symbol] = {"latest_date": target_trade_date, "entries": []}
    index_path.write_text(
        json.dumps({"symbols_total": len(symbols), "symbols": symbols}),
        encoding="utf-8",
    )

    db_path = tmp_path / "vendor_delta" / "market_delta.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stored = delta_symbols if delta_symbols is not None else tuple(solid_symbols)
    with duckdb.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE daily_bars (symbol VARCHAR, date DATE)")
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?)",
            [(symbol, target_trade_date) for symbol in stored],
        )
    return index_path, db_path


def test_hollow_index_symbol_excluded_from_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新股 entries 为空时不计入 coverage 分母，readiness 应成功。"""
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    index_path, db_path = _write_artifacts_with_entries(
        tmp_path,
        target_trade_date="2026-08-26",
        solid_symbols={
            "000001": [{"year": 2026, "zip": "全A日K/2026.zip", "entry": "2026/000001.SZ.csv"}],
            "600000": [{"year": 2026, "zip": "全A日K/2026.zip", "entry": "2026/600000.SH.csv"}],
        },
        hollow_symbols=("688835",),  # 新股 IPO，ZIP 里还没有 CSV
        delta_symbols=("000001", "600000"),  # delta 只有有数据的 2 只
    )
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    write_nightly_readiness(
        target_trade_date="2026-08-26",
        index_path=index_path,
        db_path=db_path,
    )
    payload = read_nightly_readiness()
    assert payload is not None
    assert payload["index"]["symbols_on_target_date"] == 2
    assert payload["delta"]["coverage_ratio"] == 1.0
    assert "688835" in payload["index"]["hollow_symbols"]
    gate = check_nightly_readiness(expected_trade_date="2026-08-26")
    assert gate.ready is True


def test_hollow_symbol_with_real_gap_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 entries 的 symbol 在 delta 缺失时仍必须失败——过滤不放松真丢数。"""
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    index_path, db_path = _write_artifacts_with_entries(
        tmp_path,
        target_trade_date="2026-08-26",
        solid_symbols={
            "000001": [{"year": 2026, "zip": "全A日K/2026.zip", "entry": "2026/000001.SZ.csv"}],
            "600000": [{"year": 2026, "zip": "全A日K/2026.zip", "entry": "2026/600000.SH.csv"}],
            "688836": [{"year": 2026, "zip": "全A日K/2026.zip", "entry": "2026/688836.SH.csv"}],
        },
        hollow_symbols=("688835",),
        delta_symbols=("000001", "600000"),  # 688836 有 entries 但 delta 缺
    )
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    with pytest.raises(ValueError, match="coverage is incomplete: 2<3"):
        write_nightly_readiness(
            target_trade_date="2026-08-26",
            index_path=index_path,
            db_path=db_path,
            path=auth,
        )


def test_legacy_index_without_entries_field_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """历史索引项不带 entries 字段，视作有数据（向后兼容）。"""
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    # 用原始 _write_artifacts（不带 entries 字段）
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
    payload = read_nightly_readiness()
    assert payload is not None
    assert payload["index"]["symbols_on_target_date"] == 2
    assert payload["delta"]["coverage_ratio"] == 1.0
    assert payload["index"]["hollow_symbols"] == []


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
    consumed = tmp_path / "auth" / "artifacts" / "runtime" / ("nightly_data_ready.consumed.json")
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


# ---------------------------------------------------------------------------
# P1 R1：双 delta 消费分档（RDY-1..7）
#
# 同一份 readiness 文件，两个消费者要得到**不同**结论：
#   Week5 / 夜扫 / Legacy final selection  → 只要 feature 数据就绪（v2 或 v3）
#   active Alpha epoch 的 capture          → 必须有执行侧证据（只有 v3）
# 这不是"两套判据"，而是同一个函数的一个显式参数；默认档保证既有语义不变。
# ---------------------------------------------------------------------------

_DUAL_TARGET = "2026-08-19"


def _v2_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "target_trade_date": _DUAL_TARGET,
        "daily": {"ok": True},
        "index": {"ok": True},
        "delta": {"ok": True},
    }


def _v3_payload() -> dict[str, object]:
    payload = _v2_payload()
    payload["schema_version"] = 3
    payload["execution_delta"] = {
        "ok": True,
        "role": "execution",
        "price_series_mode": "raw",
    }
    payload["symbol_membership"] = {"membership_locked": True}
    payload["raw_delta_baseline"] = {"ok": True}
    return payload


def _write_payload(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_rdy1_v2_readiness_still_ready_for_week5(tmp_path: Path) -> None:
    """RDY-1：默认档（Week5 / Legacy）在 v2 上必须仍然 ready。

    这是"Alpha V2 的引入不得收紧 Legacy release 契约"的直接证明。
    """
    path = _write_payload(tmp_path / "ready.json", _v2_payload())
    gate = check_nightly_readiness(expected_trade_date=_DUAL_TARGET, path=path)
    assert gate.ready is True
    assert gate.reason == "ok"


def test_rdy2_same_v2_file_is_not_ready_for_dual_delta_consumer(tmp_path: Path) -> None:
    """RDY-2：**同一个文件**在严格档下必须不 ready，且原因码可辨。"""
    path = _write_payload(tmp_path / "ready.json", _v2_payload())
    gate = check_nightly_readiness(
        expected_trade_date=_DUAL_TARGET, path=path, require_dual_delta=True
    )
    assert gate.ready is False
    assert gate.reason == "nightly_dual_delta_not_ready"


def test_rdy3_full_v3_readiness_passes_strict_consumer(tmp_path: Path) -> None:
    """RDY-3：完整 v3 在严格档下 PASS。"""
    path = _write_payload(tmp_path / "ready.json", _v3_payload())
    gate = check_nightly_readiness(
        expected_trade_date=_DUAL_TARGET, path=path, require_dual_delta=True
    )
    assert gate.ready is True
    assert gate.reason == "ok"


def test_rdy4_v3_without_execution_block_fails_strict_consumer(tmp_path: Path) -> None:
    """RDY-4：v3 声明了双 delta 却没有 execution 块 → 不 ready（不许按 v2 降级）。"""
    payload = _v3_payload()
    payload.pop("execution_delta")
    path = _write_payload(tmp_path / "ready.json", payload)
    gate = check_nightly_readiness(
        expected_trade_date=_DUAL_TARGET, path=path, require_dual_delta=True
    )
    assert gate.ready is False
    assert gate.reason == "nightly_dual_delta_not_ready"


def test_rdy5_v3_with_unhealthy_execution_delta_fails(tmp_path: Path) -> None:
    """RDY-5：execution_delta.ok=false → 不 ready。"""
    payload = _v3_payload()
    payload["execution_delta"] = {"ok": False, "role": "execution"}
    path = _write_payload(tmp_path / "ready.json", payload)
    gate = check_nightly_readiness(
        expected_trade_date=_DUAL_TARGET, path=path, require_dual_delta=True
    )
    assert gate.ready is False
    assert gate.reason == "nightly_dual_delta_not_ready"


def test_rdy6_v3_with_unlocked_membership_fails(tmp_path: Path) -> None:
    """RDY-6：成员锁步未成立 → 不 ready（计数相同但成员不同的最后一公里）。"""
    payload = _v3_payload()
    payload["symbol_membership"] = {"membership_locked": False}
    path = _write_payload(tmp_path / "ready.json", payload)
    gate = check_nightly_readiness(
        expected_trade_date=_DUAL_TARGET, path=path, require_dual_delta=True
    )
    assert gate.ready is False
    assert gate.reason == "nightly_dual_delta_not_ready"


def test_rdy7_v3_without_certified_baseline_fails(tmp_path: Path) -> None:
    """RDY-7：raw_delta_baseline.ok=false → 不 ready（执行侧来路不明）。"""
    payload = _v3_payload()
    payload["raw_delta_baseline"] = {"ok": False}
    path = _write_payload(tmp_path / "ready.json", payload)
    gate = check_nightly_readiness(
        expected_trade_date=_DUAL_TARGET, path=path, require_dual_delta=True
    )
    assert gate.ready is False
    assert gate.reason == "nightly_dual_delta_not_ready"


def test_rdy_broken_v3_still_reports_data_not_ready_for_week5(tmp_path: Path) -> None:
    """默认档看到坏 v3 时报的仍是数据原因码，不是双 delta 原因码。

    原因码是给调度器分流用的：把"数据没好"和"形状不对"混成一个码，会让 Week5
    的重试决策失去依据。
    """
    payload = _v3_payload()
    payload["daily"] = {"ok": False}
    path = _write_payload(tmp_path / "ready.json", payload)
    gate = check_nightly_readiness(expected_trade_date=_DUAL_TARGET, path=path)
    assert gate.ready is False
    assert gate.reason == "nightly_data_not_ready"
