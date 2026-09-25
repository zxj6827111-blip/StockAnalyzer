"""P3.3.1e — nightly readiness is a per-trading-day fact, not a one-shot token.

Production incident shape (2026-09 NAS): the vendor updater publishes readiness
around 20:35, ``evolution_offhours`` runs at 20:40 and acknowledged it by
``os.replace``-ing the published file away, so ``week5_night_scan`` at 21:45 and
``alpha_v2_shadow_cycle`` from 22:00 saw ``nightly_data_not_ready`` and the
22:30/23:30 notices reported the night as blocked.  These tests pin the fixed
contract: one consumer can never take another consumer's readiness away.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

from stock_analyzer.ops import nightly_readiness as mod
from stock_analyzer.ops.nightly_readiness import (
    CONSUME_ALREADY_CONSUMED,
    CONSUME_CONSUMED,
    CONSUME_NOT_READY,
    Consumption,
    check_nightly_readiness,
    consume_nightly_readiness,
    nightly_readiness_audit,
    read_nightly_readiness,
    write_nightly_readiness,
)

_TARGET = "2026-09-26"
_OTHER_CONSUMER = "week5_night_scan"
_FIRST_CONSUMER = "evolution_offhours"
_STRICT_CONSUMER = "alpha_v2_shadow_cycle"


def _write_artifacts(
    tmp_path: Path,
    *,
    target_trade_date: str,
    index_symbols: tuple[str, ...] = ("000001", "600000"),
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
    with duckdb.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE daily_bars (symbol VARCHAR, date DATE)")
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?)",
            [(symbol, target_trade_date) for symbol in index_symbols],
        )
    return index_path, db_path


def _publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trade_date: str = _TARGET,
    commit: str = "c1",
    tag: str = "",
) -> tuple[Path, str]:
    """Publish readiness to a hermetic authoritative path; return (path, bytes).

    ``_candidate_readiness_paths`` is narrowed to the authoritative file so the
    run cannot read or drain a real ``artifacts/runtime`` directory.
    """
    auth = tmp_path / "artifacts" / "runtime" / "nightly_data_ready.json"
    index_path, db_path = _write_artifacts(
        tmp_path / (tag or trade_date.replace("-", "")),
        target_trade_date=trade_date,
    )
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(auth))
    monkeypatch.setattr(mod, "_candidate_readiness_paths", lambda: [auth])
    write_nightly_readiness(
        target_trade_date=trade_date,
        index_path=index_path,
        db_path=db_path,
        updater_commit=commit,
    )
    return auth, auth.read_text(encoding="utf-8")


def _acks(directory: Path) -> list[Path]:
    return sorted(directory.glob("nightly_data_ready.consumed-*.json"))


def _publications(directory: Path) -> list[Path]:
    return sorted(directory.glob("nightly_data_ready.published-*.json"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ack(
    consumer: str,
    *,
    expected: str | date = _TARGET,
    **kwargs: Any,
) -> Consumption:
    """Production shape: a job always knows the date it is working for."""
    return consume_nightly_readiness(
        consumer=consumer, expected_trade_date=expected, **kwargs
    )


# ------------------------------------------------------------------ 多消费者契约


def test_two_consumers_acknowledge_the_same_release_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1：publish → A 确认 → B 确认，两者都必须成功。"""
    auth, published = _publish(tmp_path, monkeypatch)

    first = _ack(_FIRST_CONSUMER)
    assert first.status == CONSUME_CONSUMED
    assert first.payload["target_trade_date"] == _TARGET

    # 这就是回归锚：A 确认之后 B 依然看得见发布事实。
    gate = check_nightly_readiness(expected_trade_date=_TARGET)
    assert gate.ready is True, gate.reason
    assert auth.exists()

    second = _ack(_OTHER_CONSUMER)
    assert second.status == CONSUME_CONSUMED
    assert second.payload_sha256 == first.payload_sha256 == _sha256(published)
    assert [path.name for path in _acks(auth.parent)] == [
        "nightly_data_ready.consumed-20260926-evolution_offhours.json",
        "nightly_data_ready.consumed-20260926-week5_night_scan.json",
    ]


def test_consumers_acknowledge_independently_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 2：反向顺序同样两个都成功——契约不得依赖到达次序。"""
    auth, _published = _publish(tmp_path, monkeypatch)

    strict = _ack(_STRICT_CONSUMER)
    assert strict.status == CONSUME_CONSUMED
    assert check_nightly_readiness(expected_trade_date=_TARGET).ready is True
    first = _ack(_FIRST_CONSUMER)
    assert first.status == CONSUME_CONSUMED
    assert read_nightly_readiness() is not None
    assert len(_acks(auth.parent)) == 2


def test_retry_of_one_consumer_does_not_disturb_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 3：A 连续两次执行 → 第二次 already_consumed，不写第二份、不伤 B。"""
    auth, published = _publish(tmp_path, monkeypatch)

    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED
    retry = _ack(_FIRST_CONSUMER)
    assert retry.status == CONSUME_ALREADY_CONSUMED
    assert retry.ok is True
    assert retry.audit_path is not None
    assert retry.audit_path == auth.parent / (
        "nightly_data_ready.consumed-20260926-evolution_offhours.json"
    )
    assert retry.payload_sha256 == _sha256(published)
    assert len(_acks(auth.parent)) == 1

    later = _ack(_OTHER_CONSUMER)
    assert later.status == CONSUME_CONSUMED
    assert check_nightly_readiness(expected_trade_date=_TARGET).ready is True


def test_state_lives_only_on_disk_so_a_restart_keeps_every_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 4：publish → A 确认 → 进程重启 → B 仍成功、A 仍是幂等重放。"""
    auth, _published = _publish(tmp_path, monkeypatch)
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED

    # The gate is used from three different containers (api / scheduler-heavy /
    # scheduler-critical); reload proves no consumer state was cached in a
    # process that a restart would drop.
    reloaded = importlib.reload(mod)
    monkeypatch.setattr(mod, "_candidate_readiness_paths", lambda: [auth])
    assert reloaded is mod

    repeat = reloaded.consume_nightly_readiness(
        consumer=_FIRST_CONSUMER, expected_trade_date=_TARGET
    )
    assert repeat.status == CONSUME_ALREADY_CONSUMED
    assert (
        reloaded.consume_nightly_readiness(
            consumer=_OTHER_CONSUMER, expected_trade_date=_TARGET
        ).status
        == CONSUME_CONSUMED
    )
    assert reloaded.check_nightly_readiness(expected_trade_date=_TARGET).ready is True
    assert len(_acks(auth.parent)) == 2


def test_previous_day_readiness_is_never_today_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 5：昨天的发布还在，今天没有新发布 → 不得放行，也不得留下今日凭证。"""
    auth, _published = _publish(tmp_path, monkeypatch, trade_date=_TARGET)
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED
    assert auth.exists()

    today = date(2026, 9, 27)
    assert check_nightly_readiness(expected_trade_date=today).ready is False
    assert check_nightly_readiness(expected_trade_date=today).reason == "nightly_data_not_ready"

    stale_ack = _ack(_OTHER_CONSUMER, expected=today)
    assert stale_ack.status == CONSUME_NOT_READY
    assert stale_ack.ok is False
    # 旧日期的凭证仍然只在 20260926 名下，不会伪装成今日消费。
    assert [path.name for path in _acks(auth.parent)] == [
        "nightly_data_ready.consumed-20260926-evolution_offhours.json",
    ]


def test_next_day_publication_never_pollutes_the_previous_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 6：两晚各自发布、各自确认，互不冒领。"""
    auth, day1 = _publish(tmp_path, monkeypatch, trade_date="2026-09-26", commit="c1", tag="d1")
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED
    assert _ack(_OTHER_CONSUMER).status == CONSUME_CONSUMED

    _publish(tmp_path, monkeypatch, trade_date="2026-09-27", commit="c2", tag="d2")
    assert check_nightly_readiness(expected_trade_date="2026-09-26").ready is False
    assert check_nightly_readiness(expected_trade_date="2026-09-27").ready is True

    # 同一消费者在新的一晚必须能再确认一次——幂等键是"这份发布"，不是"这个日期串"。
    assert _ack(_FIRST_CONSUMER, expected="2026-09-27").status == CONSUME_CONSUMED
    assert _ack(_OTHER_CONSUMER, expected="2026-09-27").status == CONSUME_CONSUMED

    records = {path.name: _record(path) for path in _acks(auth.parent)}
    assert set(records) == {
        "nightly_data_ready.consumed-20260926-evolution_offhours.json",
        "nightly_data_ready.consumed-20260926-week5_night_scan.json",
        "nightly_data_ready.consumed-20260927-evolution_offhours.json",
        "nightly_data_ready.consumed-20260927-week5_night_scan.json",
    }
    day1_hash = _sha256(day1)
    assert records["nightly_data_ready.consumed-20260926-evolution_offhours.json"][
        "payload_sha256"
    ] == day1_hash
    assert records["nightly_data_ready.consumed-20260927-evolution_offhours.json"][
        "payload_sha256"
    ] != day1_hash


def test_republished_same_day_gives_every_consumer_a_fresh_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同日补跑 updater：新 payload 必须让所有消费者重新可用，旧证据只追加。"""
    auth, first = _publish(tmp_path, monkeypatch, commit="c1", tag="run1")
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED

    _publish(tmp_path, monkeypatch, commit="c2", tag="run2")
    second = auth.read_text(encoding="utf-8")
    assert second != first
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED
    assert _ack(_OTHER_CONSUMER).status == CONSUME_CONSUMED

    records = [_record(path) for path in _acks(auth.parent)]
    assert len(records) == 3
    by_consumer_and_hash = {
        (str(item["consumer"]), str(item["payload_sha256"])[:8]) for item in records
    }
    assert by_consumer_and_hash == {
        (_FIRST_CONSUMER, _sha256(first)[:8]),
        (_FIRST_CONSUMER, _sha256(second)[:8]),
        (_OTHER_CONSUMER, _sha256(second)[:8]),
    }
    # 两份发布各自留下凭证，补跑不得盖掉第一晚的证据。
    assert len(_publications(auth.parent)) == 2


# ------------------------------------------------------------------ 遗留与失效


def test_legacy_fixed_name_consumed_file_is_neither_reused_nor_an_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 7：生产里现存的 nightly_data_ready.consumed.json 不删、不盖、不冒领。"""
    auth, _published = _publish(tmp_path, monkeypatch)
    legacy = auth.parent / "nightly_data_ready.consumed.json"
    legacy_bytes = json.dumps(
        {
            "schema_version": 2,
            "target_trade_date": _TARGET,
            "created_at": "2026-09-26T19:48:12+08:00",
        }
    )
    legacy.write_text(legacy_bytes, encoding="utf-8")

    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED
    assert legacy.read_text(encoding="utf-8") == legacy_bytes
    assert _record(legacy).get("record_type") is None

    # 遗留文件里没有 consumer / payload_sha256，因此它证明不了任何当前确认。
    audit = nightly_readiness_audit(
        target_trade_date=_TARGET,
        directory=auth.parent,
        expected_consumers={_FIRST_CONSUMER, _OTHER_CONSUMER},
    )
    assert audit["acknowledged_consumers"] == [_FIRST_CONSUMER]
    assert audit["unacknowledged_consumers"] == [_OTHER_CONSUMER]


def test_invalidate_retires_the_published_fact_and_leaves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消费不再删除发布，所以 invalidate 是唯一 retirement 入口——它必须仍然有效。"""
    auth, _published = _publish(tmp_path, monkeypatch)
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED

    invalidated = mod.invalidate_nightly_readiness(stamp="20260927T194500Z")

    assert [Path(item).name for item in invalidated] == ["nightly_data_ready.json"]
    assert read_nightly_readiness() is None
    assert check_nightly_readiness(expected_trade_date=_TARGET).ready is False
    assert _ack(_OTHER_CONSUMER).status == CONSUME_NOT_READY
    assert len(_acks(auth.parent)) == 1
    assert len(_publications(auth.parent)) == 1


def test_missing_active_marker_is_not_ready_even_though_a_publication_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """红队 6：发布证据在、active marker 丢了 → 仍然 fail closed。

    发布记录只回答"那一晚发布过什么字节"，它不是就绪来源；否则 ``invalidate``
    的 fail-closed 语义会被凭证目录复活。
    """
    auth, _published = _publish(tmp_path, monkeypatch)
    assert len(_publications(auth.parent)) == 1

    auth.unlink()

    assert check_nightly_readiness(expected_trade_date=_TARGET).ready is False
    assert _ack(_FIRST_CONSUMER).status == CONSUME_NOT_READY
    assert _acks(auth.parent) == []


def test_consume_refuses_a_release_the_gate_itself_would_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消费前置门禁：v2 发布对严格档消费者不得成立凭证。"""
    auth, _published = _publish(tmp_path, monkeypatch)

    refused = _ack(_STRICT_CONSUMER, require_dual_delta=True)

    assert refused.status == CONSUME_NOT_READY
    assert refused.reason == mod.READINESS_REASON_DUAL_DELTA
    assert _acks(auth.parent) == []
    assert auth.exists()
    # 同一份发布对默认档消费者仍然是可确认的事实。
    assert _ack(_OTHER_CONSUMER).status == CONSUME_CONSUMED


def test_acknowledgement_matches_payload_content_not_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """红队 4：升级前（P3.3.1d 固定日期名）留下的凭证必须继续算数。

    幂等键是 (consumer, payload_sha256)，不是文件名——否则部署当晚每个消费者都会
    把同一份发布再消费一次。
    """
    auth, published = _publish(tmp_path, monkeypatch)
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED
    legacy_name = auth.parent / "nightly_data_ready.consumed-20260926.json"
    (auth.parent / "nightly_data_ready.consumed-20260926-evolution_offhours.json").rename(
        legacy_name
    )

    replay = _ack(_FIRST_CONSUMER)

    assert replay.status == CONSUME_ALREADY_CONSUMED
    assert replay.audit_path == legacy_name
    assert replay.payload_sha256 == _sha256(published)
    assert [path.name for path in _acks(auth.parent)] == [legacy_name.name]
    # 别的消费者不受这份历史凭证影响。
    assert _ack(_OTHER_CONSUMER).status == CONSUME_CONSUMED


def test_consume_without_expected_date_only_checks_self_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 expected_trade_date = 只核对 payload 自洽，生产调用方必须传日期。

    钉住这条边界：发布文件现在会活过一整晚，"哪天都行"的默认档正是旧日期被
    冒领成今日 readiness 的入口。
    """
    _publish(tmp_path, monkeypatch, trade_date="2026-09-20")

    assert consume_nightly_readiness(consumer=_FIRST_CONSUMER).status == CONSUME_CONSUMED
    assert (
        consume_nightly_readiness(
            consumer=_OTHER_CONSUMER, expected_trade_date=date(2026, 9, 27)
        ).status
        == CONSUME_NOT_READY
    )


def test_explicit_path_entry_keeps_the_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式 ``path=`` 也必须只写凭证、不动发布文件（旧实现会把它 rename 走）。"""
    auth, published = _publish(tmp_path, monkeypatch)

    result = consume_nightly_readiness(path=auth, consumer=_FIRST_CONSUMER)

    assert result.status == CONSUME_CONSUMED
    assert auth.exists()
    assert auth.read_text(encoding="utf-8") == published
    assert result.audit_path is not None
    assert _record(result.audit_path)["consumer"] == _FIRST_CONSUMER


# ------------------------------------------------------------------ 审计证据


def test_publication_record_answers_who_published_which_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, published = _publish(tmp_path, monkeypatch, commit="aaa1111")

    records = [_record(path) for path in _publications(auth.parent)]
    assert [path.name for path in _publications(auth.parent)] == [
        "nightly_data_ready.published-20260926.json"
    ]
    record = records[0]
    assert record["record_type"] == "nightly_readiness_publication"
    assert record["target_trade_date"] == _TARGET
    assert record["payload_sha256"] == _sha256(published)
    assert record["build_commit"] == "aaa1111"
    assert record["producer"] == "stock_updater.sh"
    assert record["readiness_payload"] == json.loads(published)


def test_every_acknowledgement_links_to_the_single_published_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 5：同一 target date 的所有凭证必须指向同一份发布 payload hash。"""
    auth, published = _publish(tmp_path, monkeypatch)
    digest = _sha256(published)

    for consumer in (_FIRST_CONSUMER, _OTHER_CONSUMER, _STRICT_CONSUMER):
        assert _ack(consumer).status == CONSUME_CONSUMED

    audit = nightly_readiness_audit(
        target_trade_date=_TARGET,
        directory=auth.parent,
        expected_consumers=(_FIRST_CONSUMER, _OTHER_CONSUMER, _STRICT_CONSUMER, "nightly_delivery"),
    )
    assert audit["published_payload_sha256"] == [digest]
    assert {str(item["payload_sha256"]) for item in audit["acknowledgements"]} == {digest}
    assert audit["acknowledged_consumers"] == sorted(
        (_FIRST_CONSUMER, _OTHER_CONSUMER, _STRICT_CONSUMER)
    )
    assert audit["unacknowledged_consumers"] == ["nightly_delivery"]

    first = _record(_acks(auth.parent)[0])
    assert first["target_trade_date"] == _TARGET
    assert first["published_at"] == str(json.loads(published)["created_at"])
    assert first["result_status"] == "consumed"
    assert first["readiness_payload"] == json.loads(published)


# ------------------------------------------------------------------ 并发安全


def test_different_consumers_racing_all_get_exactly_one_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """红队 9：多消费者同一分钟到达，谁都不能把别人挤掉。"""
    auth, published = _publish(tmp_path, monkeypatch)
    consumers = [f"job_{index}" for index in range(12)]
    barrier = threading.Barrier(len(consumers))

    def _run(name: str) -> Consumption:
        barrier.wait()
        return _ack(name)

    with ThreadPoolExecutor(max_workers=len(consumers)) as pool:
        results = list(pool.map(_run, consumers))

    assert [item.status for item in results] == [CONSUME_CONSUMED] * len(consumers)
    assert len(_acks(auth.parent)) == len(consumers)
    assert {item.payload_sha256 for item in results} == {_sha256(published)}
    assert check_nightly_readiness(expected_trade_date=_TARGET).ready is True


def test_same_consumer_racing_writes_a_single_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """红队 9：同一消费者并发重试，只能有一份凭证、一次 consumed。"""
    auth, _published = _publish(tmp_path, monkeypatch)
    attempts = 8
    barrier = threading.Barrier(attempts)

    def _run() -> str:
        barrier.wait()
        return _ack(_FIRST_CONSUMER).status

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        statuses = list(pool.map(lambda _i: _run(), range(attempts)))

    assert statuses.count(CONSUME_CONSUMED) == 1
    assert statuses.count(CONSUME_ALREADY_CONSUMED) == attempts - 1
    assert [path.name for path in _acks(auth.parent)] == [
        "nightly_data_ready.consumed-20260926-evolution_offhours.json"
    ]
    assert list(auth.parent.glob("*.claiming")) == []


# ------------------------------------------------------- 真实调度入口（Phase 7）


def test_probe_reports_ready_after_another_consumer_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """week5/shadow 的真实入口是 ``probe_nightly_readiness``；A 确认之后它必须放行。"""
    from stock_analyzer.runtime.services.week5_automation_service import (
        RuntimeWeek5AutomationService,
    )

    auth, _published = _publish(tmp_path, monkeypatch)

    class _Service:
        def _resolve_nightly_expected_trade_date(self) -> str:
            return _TARGET

    probe_host = object.__new__(RuntimeWeek5AutomationService)
    probe_host._service = _Service()

    assert probe_host.probe_nightly_readiness()["allowed"] is True
    assert _ack(_FIRST_CONSUMER).status == CONSUME_CONSUMED

    probe = probe_host.probe_nightly_readiness()
    assert probe["allowed"] is True
    assert probe["status"] == "ready"
    assert probe["reason"] == "ok"
    assert Path(str(probe["payload"]["target_trade_date"])) is not None

    strict = probe_host.probe_nightly_readiness(require_dual_delta=True)
    assert strict["reason"] == mod.READINESS_REASON_DUAL_DELTA
    assert auth.exists()
