from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pandas as pd

import stock_analyzer.runtime.services.week5_automation_service as automation_module
from stock_analyzer.runtime.services.week5_automation_service import (
    RuntimeWeek5AutomationService,
)
from stock_analyzer.runtime.services.week5_candidate_state import CandidateStateStore
from stock_analyzer.runtime.services.week5_market_snapshot_service import (
    Week5MarketSnapshotService,
    normalize_market_snapshot_frame,
)
from stock_analyzer.runtime.services.week5_state_service import RuntimeWeek5StateService


class FakeService:
    def __init__(self, tmp_path: Path) -> None:
        week5 = SimpleNamespace(
            candidate_state_path=str(tmp_path / "candidate_state.json"),
            market_snapshot_root=str(tmp_path / "snapshots"),
            market_snapshot_timeout_sec=10,
            market_snapshot_retention_hours=72.0,
            market_snapshot_max_files=500,
            candidate_pool_target=30,
            candidate_pool_max_symbols=50,
            overnight_top_k=5,
            night_quality_target=300,
            night_light_candidate_target=100,
            night_deep_candidate_target=50,
            auction_focus_target=10,
            auction_focus_max_symbols=12,
            auction_scan_top_n=20,
            auction_snapshot_max_age_sec=120,
            auction_baseline_min_days=5,
            market_radar_light_top_n=20,
            market_radar_continuity_required=2,
            market_radar_dynamic_score_margin=3.0,
            market_radar_min_residency_min=10,
            market_radar_circuit_breaker_failures=3,
            market_radar_circuit_breaker_slow_runs=2,
            market_radar_slow_run_sec=120,
            market_radar_circuit_breaker_cooldown_min=10,
            market_radar_timeout_sec=90,
            actionable_realtime_max_age_sec=120,
            anomaly_gap_pct=0.08,
            anomaly_volume_ratio=2.5,
            auto_sync_watchlist_min_score=65.0,
            auto_sync_watchlist_preserve_max_age_hours=18.0,
            live_runtime_max_symbols=8,
            weekend_learning_enabled=True,
        )
        self._config = SimpleNamespace(week5=week5)
        self._state = SimpleNamespace(watchlist=[], current_equity=100000.0)
        self._portfolio = SimpleNamespace(positions=lambda: [])
        self.scan_report: dict[str, object] = {}
        self.current_week5_data_version = ""
        self.scan_call_count = 0
        self.pipeline_response: dict[str, object] = {}
        self.pipeline_calls: list[dict[str, object]] = []
        self.watchlist_updates: list[list[str]] = []
        self.weekend_manifest: dict[str, object] = {"ok": False}

    def _resolve_evolution_path(self, value: str) -> Path:
        return Path(value)

    def run_week5_scan(self, **kwargs: object) -> dict[str, object]:
        self.scan_kwargs = kwargs
        self.scan_call_count += 1
        return self.scan_report

    def _replace_watchlist(self, *, symbols: list[str], reason: str) -> dict[str, object]:
        self._state.watchlist = list(symbols)
        self.watchlist_updates.append(list(symbols))
        return {"updated": True, "reason": reason}

    def run_pipeline(self, **kwargs: object) -> dict[str, object]:
        self.pipeline_calls.append(kwargs)
        return self.pipeline_response

    def learning_protocol_status(self, *, manifest_limit: int) -> dict[str, object]:
        return {"manifest_limit": manifest_limit}

    def build_learning_trainable_manifest(self) -> dict[str, object]:
        return self.weekend_manifest


def _automation(tmp_path: Path) -> RuntimeWeek5AutomationService:
    return RuntimeWeek5AutomationService(FakeService(tmp_path))


def _stub_live_snapshot(
    automation: RuntimeWeek5AutomationService,
    timestamp: datetime,
    symbols: list[str],
) -> None:
    automation._market_snapshots.capture = lambda timestamp: {
        "ok": True,
        "status": "captured",
        "snapshot_id": "live-test-snapshot",
        "timestamp": timestamp.isoformat(),
        "rows": [{"symbol": symbol, "snapshot_time": timestamp.isoformat()} for symbol in symbols],
    }


def test_candidate_state_ignores_older_writer(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path / "state.json")
    newer = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    older = newer - timedelta(minutes=1)
    store.update({"night_pool": [{"symbol": "600000"}]}, updated_at=newer)
    result = store.update({"night_pool": [{"symbol": "000001"}]}, updated_at=older)

    assert result["write_status"] == "stale_writer_ignored"
    assert store.load()["night_pool"] == [{"symbol": "600000"}]
    assert store.load()["state_revision"] == 1


def test_night_scan_persists_pool_when_final_signals_are_empty(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service.scan_report = {
        "status": "ok",
        "data_version": "snapshot-v1",
        "data_gate": {"status": "ok", "reasons": []},
        "prefilter": {
            "universe_count": 300,
            "eligible_count": 300,
            "batch_coverage_ratio": 1.0,
            "intraday_freshness": {"fresh_ratio": 1.0},
        },
        "signal_pool": {
            "candidates": [
                {"symbol": "600000", "shortlist_score": 82.0},
                {"symbol": "000001", "shortlist_score": 79.0},
            ]
        },
        "funnel": {"final_selection": {"final_signals": []}},
    }
    automation = RuntimeWeek5AutomationService(service)
    result = automation.run_night_scan(
        timestamp=datetime(2026, 8, 24, 21, 45, tzinfo=UTC),
        sync_watchlist=True,
    )

    assert result["actionable_signals"] == []
    assert [item["symbol"] for item in result["night_pool"]] == ["600000", "000001"]
    assert service.scan_kwargs["deep_candidate_target_override"] == 50
    assert service.watchlist_updates == [["600000", "000001"]]
    assert [item["symbol"] for item in automation.candidate_state()["night_pool"]] == [
        "600000",
        "000001",
    ]
    latest = automation.latest_night_scan()
    assert [item["symbol"] for item in latest["night_pool"]] == ["600000", "000001"]
    assert latest["overnight_top5"][0]["symbol"] == "600000"
    assert latest["candidate_data_gate"]["status"] == "watch_only"
    assert latest["actionable_data_gate"]["actionable"] is False


def test_night_scan_is_idempotent_for_trade_date_and_data_version(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service.current_week5_data_version = "snapshot-v1"
    service.scan_report = {
        "status": "ok",
        "data_snapshot_id": "snapshot-v1",
        "data_gate": {"status": "ok", "reasons": []},
        "prefilter": {
            "universe_count": 300,
            "eligible_count": 300,
            "batch_coverage_ratio": 1.0,
            "intraday_freshness": {"fresh_ratio": 1.0},
        },
        "signal_pool": {"candidates": [{"symbol": "600000", "shortlist_score": 82.0}]},
    }
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 24, 21, 45, tzinfo=UTC)
    readiness_calls = 0

    def readiness() -> dict[str, object]:
        nonlocal readiness_calls
        readiness_calls += 1
        return {
            "status": "ready" if readiness_calls == 1 else "blocked",
            "allowed": readiness_calls == 1,
            "waited_sec": 0.0,
        }

    automation._await_nightly_readiness = readiness

    first = automation.run_night_scan(timestamp=now)
    second = automation.run_night_scan(timestamp=now)

    assert first["status"] == "ok"
    assert second["status"] == "already_ran"
    assert second["idempotent"] is True
    assert service.scan_call_count == 1
    assert readiness_calls == 1


def test_legacy_global_quality_block_does_not_block_candidate_pool(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service.scan_report = {
        "status": "blocked_data_gate",
        "data_gate": {
            "status": "blocked",
            "reasons": ["data_quality_blocked:0.650"],
        },
        "prefilter": {
            "universe_count": 300,
            "eligible_count": 300,
            "batch_coverage_ratio": 1.0,
            "intraday_freshness": {"fresh_ratio": 1.0},
        },
        "signal_pool": {"candidates": [{"symbol": "600000", "shortlist_score": 82.0}]},
    }

    result = RuntimeWeek5AutomationService(service).run_night_scan(
        timestamp=datetime(2026, 8, 24, 21, 45, tzinfo=UTC)
    )

    assert [item["symbol"] for item in result["night_pool"]] == ["600000"]
    assert result["candidate_data_gate"]["status"] == "watch_only"
    assert "data_quality_blocked:0.650" in result["candidate_data_gate"]["reasons"]


def test_blocked_candidate_gate_clears_pool_and_watchlist(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service.scan_report = {
        "status": "blocked_data_gate",
        "data_gate": {"status": "ok", "reasons": []},
        "prefilter": {
            "universe_count": 100,
            "eligible_count": 100,
            "batch_coverage_ratio": 0.65,
        },
        "signal_pool": {"candidates": [{"symbol": "600000", "shortlist_score": 82.0}]},
    }

    result = RuntimeWeek5AutomationService(service).run_night_scan(
        timestamp=datetime(2026, 8, 24, 21, 45, tzinfo=UTC),
        sync_watchlist=True,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked_data_gate"
    assert result["candidate_data_gate"]["status"] == "blocked"
    assert result["night_pool"] == []
    assert service.watchlist_updates == []


def test_readiness_failure_falls_back_to_previous_night_pool(tmp_path: Path) -> None:
    """readiness 失败时必须回退使用前一晚池（Plan 验收口径），而不是清空。"""
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "600000", "score": 80.0}],
            "overnight_top5": [{"symbol": "600000", "score": 80.0}],
            "night_pool_updated_at": "2026-08-24T21:00:00+00:00",
            "night_pool_trade_date": "2026-08-24",
            "candidate_data_gate": {"status": "watch_only"},
        },
        updated_at=datetime(2026, 8, 24, 21, 0, tzinfo=UTC),
        trade_date="2026-08-24",
    )
    automation._await_nightly_readiness = lambda: {
        "status": "blocked",
        "allowed": False,
        "reason": "updater_not_ready",
    }

    result = automation.run_night_scan(timestamp=datetime(2026, 8, 25, 21, 45, tzinfo=UTC))

    state = automation.candidate_state()
    # 前一晚 21:00 生成的池在次日 21:45 仍处于 30h 有效期内。
    assert result["status"] == "fallback"
    assert result["fallback"]["applied"] is True
    assert [item["symbol"] for item in state["night_pool"]] == ["600000"]
    assert [item["symbol"] for item in state["overnight_top5"]] == ["600000"]
    assert state["night_pool_updated_at"] == "2026-08-24T21:00:00+00:00"
    assert service.watchlist_updates == []


def test_previous_pool_freshness_uses_night_scan_timestamp(tmp_path: Path) -> None:
    """过期锚点是 night_pool_updated_at（跨交易日口径），而非状态写入时间。"""
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    state = {
        "night_pool": [{"symbol": "600000"}],
        "night_pool_updated_at": "2026-08-24T21:00:00+00:00",
        "night_pool_trade_date": "2026-08-24",
        "candidate_data_gate": {"status": "watch_only"},
        "updated_at": "2026-08-25T08:00:00+00:00",
    }

    # 次日 16:00 读取：距夜池生成约 19h，仍在 night_pool_max_age_hours=30h 内。
    pool, expires_at = automation._fresh_previous_pool(
        state,
        datetime(2026, 8, 25, 16, 0, tzinfo=UTC),
    )

    assert [item["symbol"] for item in pool] == ["600000"]
    assert expires_at == "2026-08-26T03:00:00+00:00"


def test_missing_auction_ratio_excludes_only_that_symbol(tmp_path: Path) -> None:
    """个别股票缺竞价量比只剔除该股票，不得阻断其余有效候选（P0 验收口径）。"""
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "snapshot_id": snapshot_id,
        "timestamp": now.isoformat(),
        "rows": [
            {
                "symbol": "600000",
                "price": 10.0,
                "prev_close": 9.5,
                "change_pct": 5.0,
                "volume": 100.0,
                "auction_volume_ratio": None,
                "snapshot_time": now.isoformat(),
            },
            {
                "symbol": "600001",
                "price": 20.0,
                "prev_close": 19.0,
                "change_pct": 5.0,
                "volume": 200.0,
                "auction_volume_ratio": 3.0,
                "snapshot_time": now.isoformat(),
            },
        ],
    }

    result = automation.run_auction(timestamp=now, snapshot_id="partial-ratio")

    assert result["auction_applied"] is True
    assert result["ratio_missing_excluded"] == 1
    focus_symbols = [item["symbol"] for item in result["opening_focus"]]
    assert focus_symbols == ["600001"]
    assert service.pipeline_calls
    assert service.pipeline_calls[0]["symbols"] == ["600001"]


def test_dynamic_pool_requires_pipeline_validation(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "radar_continuity": {"600000": {"count": 1, "hit": True}},
            "dynamic_candidate_pool": [],
            "dynamic_candidate_pool_trade_date": "2026-08-25",
        },
        updated_at=now,
        trade_date="2026-08-25",
    )
    service.pipeline_response = {"ok": False, "signals": [], "actionable_signals": []}
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "snapshot_id": snapshot_id,
        "timestamp": now.isoformat(),
        "rows": [
            {
                "symbol": "600000",
                "change_pct": 10.0,
                "auction_volume_ratio": 3.0,
                "snapshot_time": now.isoformat(),
            }
        ],
    }

    result = automation.run_market_radar(timestamp=now, snapshot_id="pipeline-failed")

    assert result["promoted"]
    assert result["dynamic_candidate_pool"] == []
    assert automation.candidate_state()["dynamic_candidate_pool"] == []


def test_live_runtime_ignores_previous_day_dynamic_pool(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    automation._candidate_state.update(
        {
            "dynamic_candidate_pool": [{"symbol": "600000", "radar_score": 100}],
            "dynamic_candidate_pool_trade_date": "2026-08-24",
        },
        updated_at=datetime(2026, 8, 24, 14, 0, tzinfo=UTC),
        trade_date="2026-08-24",
    )

    result = automation.run_live_runtime(timestamp=datetime(2026, 8, 25, 9, 30, tzinfo=UTC))

    assert result["symbols"] == []
    assert service.pipeline_calls == []


def test_stale_auction_snapshot_cannot_create_actionable_signal(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 24, 9, 25, tzinfo=UTC)
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "snapshot_id": snapshot_id,
        "timestamp": (now - timedelta(minutes=3)).isoformat(),
        "rows": [
            {
                "symbol": "600000",
                "price": 10.0,
                "prev_close": 9.5,
                "change_pct": 5.0,
                "volume": 100.0,
                "auction_volume_ratio": 3.0,
                "snapshot_time": (now - timedelta(minutes=3)).isoformat(),
            }
        ],
    }

    result = automation.run_auction(timestamp=now, snapshot_id="saved-1")

    assert result["auction_applied"] is False
    assert result["actionable_signals"] == []
    assert service.pipeline_calls == []


def test_radar_requires_two_hits_and_clears_a_missed_cycle(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    first = datetime(2026, 8, 24, 9, 30, tzinfo=UTC)

    def snapshot(symbol: str, timestamp: datetime) -> dict[str, object]:
        return {
            "ok": True,
            "snapshot_id": timestamp.strftime("%H%M%S"),
            "timestamp": timestamp.isoformat(),
            "rows": [
                {
                    "symbol": symbol,
                    "price": 10.0,
                    "prev_close": 9.0,
                    "change_pct": 10.0,
                    "volume": 100.0,
                    "auction_volume_ratio": 3.0,
                    "snapshot_time": timestamp.isoformat(),
                }
            ],
        }

    automation._market_snapshots.replay = lambda snapshot_id: snapshot("600000", first)
    first_result = automation.run_market_radar(timestamp=first, snapshot_id="r1")
    assert first_result["promoted"] == []
    assert service.pipeline_calls == []

    second = first + timedelta(minutes=2)
    automation._market_snapshots.replay = lambda snapshot_id: snapshot("600000", second)
    service.pipeline_response = {
        "signals": [{"symbol": "600000", "financial_trust_level": "reported"}],
        "actionable_signals": [{"symbol": "600000"}],
        "realtime_age_sec": 0.0,
    }
    second_result = automation.run_market_radar(timestamp=second, snapshot_id="r2")
    assert [item["symbol"] for item in second_result["promoted"]] == ["600000"]
    assert second_result["actionable_count"] == 1

    missed = second + timedelta(minutes=2)
    automation._market_snapshots.replay = lambda snapshot_id: snapshot("000001", missed)
    automation.run_market_radar(timestamp=missed, snapshot_id="r3")
    continuity = automation.candidate_state()["radar_continuity"]
    assert continuity["600000"]["count"] == 0
    assert continuity["600000"]["hit"] is False


def test_radar_tolerates_snapshot_rows_ahead_of_presampled_now(tmp_path: Path) -> None:
    """生产时序回归：now 先于抓取采样，行情源时间戳晚几秒属正常，不得误判 stale。"""
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 24, 9, 30, tzinfo=UTC)
    # 模拟 efinance 更新时间 = 抓取时刻（晚于预采样的 now 3 秒）。
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "snapshot_id": snapshot_id,
        "timestamp": now.isoformat(),
        "rows": [
            {
                "symbol": "600000",
                "price": 10.0,
                "prev_close": 9.0,
                "change_pct": 10.0,
                "volume": 100.0,
                "auction_volume_ratio": 3.0,
                "snapshot_time": (now + timedelta(seconds=3)).isoformat(),
            }
        ],
    }

    result = automation.run_market_radar(timestamp=now, snapshot_id="skew-1")

    assert result["status"] == "ok"
    assert result["hit_count"] == 1
    assert result["actionable_data_gate"]["realtime_age_sec"] == 0.0


def test_live_runtime_caps_symbols_and_actionable_signals_without_orders(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    automation._candidate_state.update(
        {
            "dynamic_candidate_pool": [
                {"symbol": f"600{i:03d}", "radar_score": 100 - i} for i in range(10)
            ],
            "dynamic_candidate_pool_trade_date": "2026-08-24",
        },
        updated_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
    )
    now = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _stub_live_snapshot(automation, now, [f"600{i:03d}" for i in range(8)])
    service.pipeline_response = {
        "signals": [
            {
                "symbol": f"600{i:03d}",
                "decision_trace": {"financial_gate": {"trust_level": "reported"}},
            }
            for i in range(10)
        ],
        "actionable_signals": [{"symbol": f"600{i:03d}"} for i in range(10)],
    }

    result = automation.run_live_runtime(timestamp=now)

    assert result["symbol_count"] == 8
    assert result["actionable_count"] == 5
    assert service.pipeline_calls[0]["dry_run_execution"] is True
    assert service.pipeline_calls[0]["use_live_runtime"] is True


def test_live_runtime_excludes_missing_financial_trust(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    automation._candidate_state.update(
        {
            "dynamic_candidate_pool": [
                {"symbol": "600000", "radar_score": 100},
                {"symbol": "600001", "radar_score": 99},
            ],
            "dynamic_candidate_pool_trade_date": "2026-08-24",
        },
        updated_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
    )
    now = datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
    _stub_live_snapshot(automation, now, ["600000", "600001"])
    service.pipeline_response = {
        "signals": [
            {
                "symbol": "600000",
                "decision_trace": {"financial_gate": {"trust_level": "reported"}},
            },
            {
                "symbol": "600001",
                "decision_trace": {"financial_gate": {"trust_level": "missing"}},
            },
        ],
        "actionable_signals": [
            {"symbol": "600000"},
            {"symbol": "600001"},
        ],
    }

    result = automation.run_live_runtime(timestamp=now)

    assert result["actionable_count"] == 1
    assert result["actionable_signals"] == [{"symbol": "600000"}]


def test_market_radar_full_market_flag_blocks_manual_run(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service._config.week5.market_radar_full_market_enabled = False
    automation = RuntimeWeek5AutomationService(service)

    result = automation.run_market_radar(
        timestamp=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
        snapshot_id="should-not-run",
    )

    assert result["status"] == "disabled"
    assert result["actionable_signals"] == []
    assert result["actionable_data_gate"]["reasons"] == ["market_radar_full_market_disabled"]
    assert service.pipeline_calls == []


def test_market_radar_circuit_breaker_opens_after_failures_and_skips_work(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service._config.week5.market_radar_circuit_breaker_failures = 2
    service._config.week5.market_radar_circuit_breaker_slow_runs = 0
    service._config.week5.market_radar_circuit_breaker_cooldown_min = 10
    automation = RuntimeWeek5AutomationService(service)
    calls = 0

    def unavailable_snapshot(_: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": False, "status": "unavailable", "rows": []}

    automation._market_snapshots.replay = unavailable_snapshot
    first = automation.run_market_radar(
        timestamp=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
        snapshot_id="unavailable-1",
    )
    second = automation.run_market_radar(
        timestamp=datetime(2026, 8, 25, 9, 31, tzinfo=UTC),
        snapshot_id="unavailable-2",
    )
    third = automation.run_market_radar(
        timestamp=datetime(2026, 8, 25, 9, 32, tzinfo=UTC),
        snapshot_id="unavailable-3",
    )

    assert first["status"] == "unavailable"
    assert second["status"] == "unavailable"
    assert second["market_radar_health"]["status"] == "open"
    assert third["status"] == "circuit_open"
    assert calls == 2


def test_market_radar_circuit_breaker_recovers_after_cooldown_probe(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service._config.week5.market_radar_circuit_breaker_failures = 1
    service._config.week5.market_radar_circuit_breaker_slow_runs = 0
    service._config.week5.market_radar_circuit_breaker_cooldown_min = 10
    automation = RuntimeWeek5AutomationService(service)
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": False,
        "status": "unavailable",
        "snapshot_id": snapshot_id,
        "rows": [],
    }

    opened = automation.run_market_radar(
        timestamp=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
        snapshot_id="failure",
    )
    assert opened["market_radar_health"]["status"] == "open"

    probe_time = datetime(2026, 8, 25, 9, 41, tzinfo=UTC)
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "status": "captured",
        "snapshot_id": snapshot_id,
        "rows": [
            {
                "symbol": "600000",
                "change_pct": 0.0,
                "auction_volume_ratio": 0.0,
                "snapshot_time": probe_time.isoformat(),
            }
        ],
    }

    recovered = automation.run_market_radar(
        timestamp=probe_time,
        snapshot_id="probe",
    )

    assert recovered["status"] == "ok"
    assert recovered["market_radar_health"]["status"] == "closed"
    assert recovered["market_radar_health"]["last_transition"] == "closed"


def test_market_radar_circuit_breaker_opens_after_consecutive_slow_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = FakeService(tmp_path)
    service._config.week5.market_radar_circuit_breaker_failures = 0
    service._config.week5.market_radar_circuit_breaker_slow_runs = 2
    service._config.week5.market_radar_slow_run_sec = 10
    service._config.week5.market_radar_timeout_sec = 0
    automation = RuntimeWeek5AutomationService(service)
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "status": "captured",
        "snapshot_id": snapshot_id,
        "rows": [
            {
                "symbol": "600000",
                "change_pct": 0.0,
                "auction_volume_ratio": 0.0,
                "snapshot_time": "2026-08-25T09:30:00+00:00",
            }
        ],
    }
    clock = iter([0.0, 11.0, 20.0, 31.0])
    monkeypatch.setattr(automation_module, "monotonic", lambda: next(clock))

    first = automation.run_market_radar(
        timestamp=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
        snapshot_id="slow-1",
    )
    second = automation.run_market_radar(
        timestamp=datetime(2026, 8, 25, 9, 31, tzinfo=UTC),
        snapshot_id="slow-2",
    )

    assert first["market_radar_health"]["consecutive_slow_runs"] == 1
    assert second["market_radar_health"]["status"] == "open"
    assert second["market_radar_health"]["consecutive_slow_runs"] == 2


def test_auction_enriches_focus_with_five_level_depth(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "snapshot_id": snapshot_id,
        "timestamp": now.isoformat(),
        "rows": [
            {
                "symbol": "600000",
                "price": 10.0,
                "prev_close": 9.5,
                "change_pct": 5.0,
                "volume": 100.0,
                "auction_volume_ratio": 3.0,
                "snapshot_time": now.isoformat(),
            }
        ],
    }
    service._fetch_market_depth_snapshots = lambda **_: {
        "600000": {
            "available": True,
            "source": "test-depth",
            "timestamp": now.isoformat(),
            "imbalance": 0.42,
            "bid_total_volume": 1200.0,
            "ask_total_volume": 400.0,
            "bid_levels": [{"level": 1, "price": 10.0, "volume": 1200.0}],
            "ask_levels": [{"level": 1, "price": 10.1, "volume": 400.0}],
        }
    }

    result = automation.run_auction(timestamp=now, snapshot_id="depth-1")
    focus = result["opening_focus"][0]

    assert result["depth_applied"] is True
    assert result["depth_available_count"] == 1
    assert focus["depth_available"] is True
    assert focus["order_imbalance"] == 0.42
    assert len(focus["bid_levels"]) == 1
    assert len(focus["ask_levels"]) == 1


def test_weekend_learning_delegates_to_governed_we_learn_01(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    calls: list[tuple[str, dict[str, object]]] = []

    service._build_idle_context = lambda now: {
        "window": "weekend",
        "trade_date": "20260821",
        "now": now.isoformat(),
    }

    def governed(task_id: str, context: dict[str, object]) -> dict[str, object]:
        calls.append((task_id, context))
        return {"status": "skipped", "reason": "skipped: market_warehouse_gate_failed"}

    service._idle_execute_task_with_policy = governed
    service.build_learning_trainable_manifest = lambda: (_ for _ in ()).throw(
        AssertionError("week5 must reuse WE-LEARN-01")
    )

    result = automation.run_weekend_learning(timestamp=datetime(2026, 8, 29, 12, 0, tzinfo=UTC))

    assert result["status"] == "skipped"
    assert result["governed_by"] == "WE-LEARN-01"
    assert calls[0][0] == "WE-LEARN-01"
    assert calls[0][1]["trade_date"] == "20260821"


def test_market_snapshot_timeout_fails_closed(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service._config.week5.market_snapshot_timeout_sec = 0.01
    snapshot_service = Week5MarketSnapshotService(service)
    release = Event()
    snapshot_service._fetch_batch_frame = lambda: (
        release.wait(1.0) or pd.DataFrame(),
        "unavailable",
        ["blocked"],
    )

    result = snapshot_service.capture(
        timestamp=datetime(2026, 8, 25, 9, 25, tzinfo=UTC),
    )
    overlapping = snapshot_service.capture(
        timestamp=datetime(2026, 8, 25, 9, 25, 1, tzinfo=UTC),
    )
    release.set()

    assert result["status"] == "unavailable"
    assert "market_snapshot_timeout" in result["errors"]
    assert overlapping["status"] == "unavailable"
    assert "market_snapshot_previous_fetch_in_progress" in overlapping["errors"]


def test_market_radar_timeout_cancels_late_worker_and_blocks_overlap(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service._config.week5.market_radar_timeout_sec = 0.01
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "radar_continuity": {
                "600000": {"count": 1, "hit": True},
            }
        },
        updated_at=now,
        trade_date=now.date().isoformat(),
    )
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "snapshot_id": snapshot_id,
        "rows": [
            {
                "symbol": "600000",
                "change_pct": 10.0,
                "auction_volume_ratio": 3.0,
                "snapshot_time": now.isoformat(),
            }
        ],
    }
    started = Event()
    release = Event()
    notifications: list[dict[str, object]] = []
    service._notify_actionable_signals = lambda report, **_: notifications.append(report)

    def slow_pipeline(**_: object) -> dict[str, object]:
        started.set()
        release.wait(1.0)
        row = {
            "symbol": "600000",
            "decision_trace": {"financial_gate": {"trust_level": "reported"}},
        }
        return {"ok": True, "signals": [row], "actionable_signals": [row]}

    service.run_pipeline = slow_pipeline
    result = automation.run_market_radar(
        timestamp=now,
        snapshot_id="radar-timeout",
        notify_enabled=True,
    )

    assert started.wait(1.0)
    assert result["status"] == "timeout"
    assert "market_radar_timeout" in result["actionable_data_gate"]["reasons"]
    assert automation.run_market_radar(timestamp=now, snapshot_id="overlap")["status"] == "busy"

    release.set()
    worker = automation._market_radar_worker
    if worker is not None:
        worker.join(timeout=1.0)
    assert automation.candidate_state()["latest_market_radar"]["status"] == "timeout"
    assert notifications == []


def test_preserved_watchlist_rejects_stale_or_blocked_night_pool(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service._refresh_runtime_state_from_disk_if_changed = lambda: None
    service._last_week5_scan_report = {
        "night_pool": [{"symbol": "300001"}],
        "watchlist_sync": {"symbols": ["300001"]},
    }
    automation = RuntimeWeek5AutomationService(service)
    service._week5_automation_service = automation
    state_service = RuntimeWeek5StateService(service)
    now = datetime.now(UTC)

    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "600000"}],
            "night_pool_updated_at": now.isoformat(),
            "night_pool_trade_date": now.date().isoformat(),
            "candidate_data_gate": {"status": "blocked"},
        },
        updated_at=now,
        trade_date=now.date().isoformat(),
    )
    assert state_service.latest_preserved_watchlist_symbols() == []

    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "000001"}],
            # 40h 前生成的池已超出 night_pool_max_age_hours=30h，视为过期。
            "night_pool_updated_at": (now - timedelta(hours=40)).isoformat(),
            "night_pool_trade_date": now.date().isoformat(),
            "candidate_data_gate": {"status": "watch_only"},
        },
        updated_at=now,
        trade_date=now.date().isoformat(),
    )
    assert state_service.latest_preserved_watchlist_symbols() == []


def test_market_snapshot_cleanup_keeps_configured_file_count(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service._config.week5.market_snapshot_retention_hours = 0
    service._config.week5.market_snapshot_max_files = 1
    snapshot_service = Week5MarketSnapshotService(service)
    frame = pd.DataFrame([{"代码": "600000", "最新价": 10.0}])

    snapshot_service.capture(
        timestamp=datetime(2026, 8, 25, 9, 25, tzinfo=UTC),
        snapshot_id="snapshot-old",
        frame=frame,
    )
    snapshot_service.capture(
        timestamp=datetime(2026, 8, 25, 9, 26, tzinfo=UTC),
        snapshot_id="snapshot-new",
        frame=frame,
    )

    assert snapshot_service.replay("snapshot-old")["status"] == "snapshot_not_found"
    assert snapshot_service.replay("snapshot-new")["status"] == "captured"


def test_weekend_learning_skipped_result_is_persisted(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    result = automation.run_weekend_learning(timestamp=datetime(2026, 8, 29, 12, 0, tzinfo=UTC))

    assert result["status"] == "skipped"
    assert automation.candidate_state()["latest_weekend_learning"]["reason"] == (
        "samples_not_mature"
    )


def test_candidate_state_merges_non_overlapping_stale_patch(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path / "state.json")
    newer = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    older = newer - timedelta(minutes=1)
    store.update({"latest_live_runtime": {"status": "ok"}}, updated_at=newer)

    result = store.update(
        {"radar_continuity": {"600000": {"count": 2}}},
        updated_at=older,
    )

    assert result["radar_continuity"] == {"600000": {"count": 2}}
    assert result["latest_live_runtime"] == {"status": "ok"}
    assert result["updated_at"] == newer.isoformat()
    assert result["state_revision"] == 2


def test_snapshot_normalizes_current_market_source_columns() -> None:
    timestamp = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    frame = pd.DataFrame(
        [
            {
                "代码": "600000",
                "名称": "浦发银行",
                "最新价": 10.5,
                "昨日收盘": 10.0,
                "量比": 2.4,
                "成交量": 1000,
                "成交额": 10500000,
                "更新时间": "09:25:00",
            }
        ]
    )

    normalized = normalize_market_snapshot_frame(
        frame,
        timestamp=timestamp,
        source="efinance",
    )
    row = normalized.iloc[0].to_dict()

    assert row["prev_close"] == 10.0
    assert row["auction_volume_ratio"] == 2.4
    assert row["snapshot_time"] == timestamp.isoformat()
    assert row["limit_up_distance"] == 5.0


def test_snapshot_without_time_column_falls_back_to_fetch_time() -> None:
    """备源无行情时间列时以抓取时刻兜底，保证降级链路可用。"""
    timestamp = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    normalized = normalize_market_snapshot_frame(
        pd.DataFrame([{"代码": "600000", "最新价": 10.5, "昨日收盘": 10.0, "量比": 2.0}]),
        timestamp=timestamp,
        source="akshare",
    )

    assert normalized.iloc[0]["snapshot_time"] == timestamp.isoformat()
    from stock_analyzer.runtime.services.week5_market_snapshot_service import enrich_auction_metrics

    result = enrich_auction_metrics(normalized, now=timestamp)
    assert result["status"] == "ok"
    assert result["fresh"] is True


def test_snapshot_with_empty_time_values_fails_freshness_closed() -> None:
    """时间列存在但取值为空时保持零容忍：判 stale，不得伪装新鲜。"""
    timestamp = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    normalized = normalize_market_snapshot_frame(
        pd.DataFrame([{"代码": "600000", "最新价": 10.5, "昨日收盘": 10.0, "更新时间": ""}]),
        timestamp=timestamp,
        source="efinance",
    )

    assert normalized.iloc[0]["snapshot_time"] == ""
    from stock_analyzer.runtime.services.week5_market_snapshot_service import enrich_auction_metrics

    result = enrich_auction_metrics(normalized, now=timestamp)
    assert result["status"] == "stale"
    assert result["fresh"] is False


def test_missing_coverage_is_not_treated_as_full_coverage(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)

    gate = automation._candidate_gate_from_report(
        {
            "data_gate": {"status": "ok", "reasons": []},
            "prefilter": {"eligible_count": 10},
        },
        [{"symbol": "600000", "score": 80.0}],
    )

    assert gate["coverage_ratio"] == 0.0
    assert gate["status"] == "blocked"
    assert "eligible_universe_coverage_below_80pct" in gate["reasons"]


def test_missing_auction_ratio_still_records_first_baseline_day(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    automation._market_snapshots.replay = lambda snapshot_id: {
        "ok": True,
        "snapshot_id": snapshot_id,
        "timestamp": now.isoformat(),
        "rows": [
            {
                "symbol": "600000",
                "price": 10.0,
                "prev_close": 9.5,
                "volume": 100.0,
                "auction_volume_ratio": None,
                "snapshot_time": now.isoformat(),
            }
        ],
    }

    result = automation.run_auction(timestamp=now, snapshot_id="baseline-day-1")

    assert result["auction_applied"] is False
    assert automation.candidate_state()["auction_baseline"]["dates"] == ["2026-08-25"]
    assert automation.candidate_state()["auction_baseline"]["by_symbol"]["600000"] == [100.0]


def test_live_runtime_arbitrates_night_and_dynamic_by_score(tmp_path: Path) -> None:
    """首轮（无持久化 live_runtime）：夜池作为现任种子入席，动态池行以挑战者
    身份参与；席位充裕时最终在席集合与排序仍由分数决定（交错入座）。
    紧张席位下的驻留保护由 test_live_runtime_first_run_seeds_night_as_incumbents 覆盖。"""
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 10, 2, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "night_pool": [
                {"symbol": "000001", "score": 99.5},
                {"symbol": "000002", "score": 98.5},
            ],
            "night_pool_updated_at": (now - timedelta(minutes=2)).isoformat(),
            "night_pool_trade_date": now.date().isoformat(),
            "dynamic_candidate_pool": [
                {"symbol": f"600{i:03d}", "radar_score": 100 - i} for i in range(10)
            ],
            "dynamic_candidate_pool_trade_date": now.date().isoformat(),
        },
        updated_at=now,
        trade_date=now.date().isoformat(),
    )

    symbols = automation._live_runtime_symbols(automation.candidate_state(), now)

    assert len(symbols) == 8
    # 席位充裕（cap=8 > 夜池 2 行）：夜池现任 + 动态挑战者按分数交错入座。
    assert symbols[:5] == ["600000", "000001", "600001", "000002", "600002"]
    assert {"000001", "000002"}.issubset(symbols)
    assert "600009" not in symbols


def test_live_runtime_first_run_seeds_night_as_incumbents(tmp_path: Path) -> None:
    """首轮回归：live_runtime 未持久化 + 夜池/动态池同时存在（radar 先于
    live runtime 运行、两调度进程无顺序保证）时，夜池成员作为现任种子，
    高分动态行不得绕过驻留检查立即夺席。"""
    service = FakeService(tmp_path)
    service._config.week5.live_runtime_max_symbols = 1
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "000001", "score": 70.0}],
            "night_pool_updated_at": (now - timedelta(minutes=5)).isoformat(),
            "night_pool_trade_date": now.date().isoformat(),
            "dynamic_candidate_pool": [
                {"symbol": "600000", "radar_score": 74.0, "entered_at": now.isoformat()}
            ],
            "dynamic_candidate_pool_trade_date": now.date().isoformat(),
        },
        updated_at=now,
        trade_date=now.date().isoformat(),
    )

    # 动态行 74 分满足 70+3 分差，但夜池现任驻留刚开始：必须保留夜池成员。
    assert automation._live_runtime_symbols(automation.candidate_state(), now) == ["000001"]


def test_live_runtime_first_run_displaces_night_after_residency(tmp_path: Path) -> None:
    """首轮入席 → 持久化 → 驻留期满后高分动态挑战者可替换（完整闭环）。"""
    service = FakeService(tmp_path)
    service._config.week5.live_runtime_max_symbols = 1
    automation = RuntimeWeek5AutomationService(service)
    first_run = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "000001", "score": 70.0}],
            "night_pool_updated_at": (first_run - timedelta(minutes=5)).isoformat(),
            "night_pool_trade_date": first_run.date().isoformat(),
            "dynamic_candidate_pool": [
                {"symbol": "600000", "radar_score": 74.0, "entered_at": first_run.isoformat()}
            ],
            "dynamic_candidate_pool_trade_date": first_run.date().isoformat(),
        },
        updated_at=first_run,
        trade_date=first_run.date().isoformat(),
    )
    _stub_live_snapshot(automation, first_run, ["000001"])
    service.pipeline_response = {"signals": [], "actionable_signals": []}

    first = automation.run_live_runtime(timestamp=first_run)
    assert first["symbols"] == ["000001"]
    seeded = automation.candidate_state()["live_runtime"]
    assert seeded[0]["symbol"] == "000001"
    assert seeded[0]["source"] == "night_pool_seed"
    entered_at = seeded[0]["entered_at"]

    # 09:41：夜池现任驻留满 10 分钟，动态挑战者 74 >= 70+3 → 允许替换。
    second_run = datetime(2026, 8, 25, 9, 41, tzinfo=UTC)
    _stub_live_snapshot(automation, second_run, ["600000"])
    second = automation.run_live_runtime(timestamp=second_run)

    assert second["symbols"] == ["600000"]
    # 替换后新现任的 entered_at 为本轮（夜池成员已离席）。
    seated = automation.candidate_state()["live_runtime"]
    assert seated[0]["symbol"] == "600000"
    assert seated[0]["entered_at"] != entered_at


def test_live_runtime_keeps_seated_member_until_residency_satisfied(tmp_path: Path) -> None:
    """确定性探针：09:25 夜池 70 分入席，09:30 dynamic 74 分首次出现——
    驻留未满 10 分钟不得切换现任；满 10 分钟且分差达标才允许替换。"""
    service = FakeService(tmp_path)
    service._config.week5.live_runtime_max_symbols = 1
    automation = RuntimeWeek5AutomationService(service)
    seated_at = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "000001", "score": 70.0}],
            "night_pool_updated_at": seated_at.isoformat(),
            "night_pool_trade_date": seated_at.date().isoformat(),
            "live_runtime": [
                {"symbol": "000001", "score": 70.0, "entered_at": seated_at.isoformat()}
            ],
            "live_runtime_trade_date": seated_at.date().isoformat(),
        },
        updated_at=seated_at,
        trade_date=seated_at.date().isoformat(),
    )
    challenged_at = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "dynamic_candidate_pool": [
                {
                    "symbol": "600000",
                    "radar_score": 74.0,
                    "entered_at": challenged_at.isoformat(),
                }
            ],
            "dynamic_candidate_pool_trade_date": challenged_at.date().isoformat(),
        },
        updated_at=challenged_at,
        trade_date=challenged_at.date().isoformat(),
    )

    # 09:30 挑战者 74 分满足 70+3 分差，但现任（夜池入席）驻留仅 5 分钟：保留夜池成员。
    symbols = automation._live_runtime_symbols(automation.candidate_state(), challenged_at)
    assert symbols == ["000001"]

    # 09:36 驻留满 10 分钟：允许切换到 dynamic 成员。
    aged = datetime(2026, 8, 25, 9, 36, tzinfo=UTC)
    symbols = automation._live_runtime_symbols(automation.candidate_state(), aged)
    assert symbols == ["600000"]


def test_live_runtime_persists_entered_at_across_runs(tmp_path: Path) -> None:
    """run_live_runtime 持久化仲裁行：entered_at 跨轮连续，不得每轮重写归零。"""
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)
    first_run = datetime(2026, 8, 25, 9, 25, tzinfo=UTC)
    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "000001", "score": 70.0}],
            "night_pool_updated_at": first_run.isoformat(),
            "night_pool_trade_date": first_run.date().isoformat(),
        },
        updated_at=first_run,
        trade_date=first_run.date().isoformat(),
    )
    _stub_live_snapshot(automation, first_run, ["000001"])
    service.pipeline_response = {"signals": [], "actionable_signals": []}

    automation.run_live_runtime(timestamp=first_run)
    entered_at = automation.candidate_state()["live_runtime"][0]["entered_at"]

    second_run = datetime(2026, 8, 25, 9, 40, tzinfo=UTC)
    _stub_live_snapshot(automation, second_run, ["000001"])
    automation.run_live_runtime(timestamp=second_run)
    seated = automation.candidate_state()["live_runtime"]

    assert seated[0]["symbol"] == "000001"
    assert seated[0]["entered_at"] == entered_at


def test_dynamic_pool_residency_protects_recent_incumbent(tmp_path: Path) -> None:
    """3 分优势 + 10 分钟驻留双条件：低分新候选不得立即替换刚入池的现任。"""
    service = FakeService(tmp_path)
    service._config.week5.live_runtime_max_symbols = 1
    automation = RuntimeWeek5AutomationService(service)
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    incumbent = {
        "symbol": "600000",
        "radar_score": 70.0,
        "entered_at": (now - timedelta(minutes=1)).isoformat(),
    }

    # 分差不足（72 < 70 + 3）：无法替换。
    kept = automation._arbitrate_dynamic_pool(
        existing=[dict(incumbent)],
        promoted=[{"symbol": "600001", "radar_score": 72.0}],
        now=now,
    )
    assert [item["symbol"] for item in kept] == ["600000"]

    # 满足 3 分优势但现任仅驻留 1 分钟（< 10 分钟）：仍不可替换。
    kept = automation._arbitrate_dynamic_pool(
        existing=[dict(incumbent)],
        promoted=[{"symbol": "600001", "radar_score": 74.0}],
        now=now,
    )
    assert [item["symbol"] for item in kept] == ["600000"]

    # 驻留已满 10 分钟且挑战者有 3 分优势：允许替换。
    aged = {**incumbent, "entered_at": (now - timedelta(minutes=11)).isoformat()}
    kept = automation._arbitrate_dynamic_pool(
        existing=[aged],
        promoted=[{"symbol": "600001", "radar_score": 74.0}],
        now=now,
    )
    assert [item["symbol"] for item in kept] == ["600001"]


def test_snapshot_age_sec_uses_oldest_timestamp_as_worst_case() -> None:
    """新鲜度必须取最旧时间戳：一只过期不得被其他刚更新的行掩盖。"""
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    rows = [
        {"symbol": "600000", "snapshot_time": (now - timedelta(minutes=5)).isoformat()},
        {"symbol": "600001", "snapshot_time": now.isoformat()},
    ]

    age = automation_module._snapshot_age_sec(rows, now)

    assert age is not None
    assert abs(age - 300.0) < 1e-6


def test_snapshot_age_sec_tolerates_small_future_skew() -> None:
    """小幅未来偏差（now 先于抓取采样导致的时延 + 时钟偏差）截断为 0，不得误判过期。"""
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    rows = [{"symbol": "600000", "snapshot_time": (now + timedelta(seconds=3)).isoformat()}]

    age = automation_module._snapshot_age_sec(rows, now)

    assert age == 0.0


def test_snapshot_age_sec_fails_closed_on_future_timestamps() -> None:
    """大幅未来时间戳（时钟回拨/时区误解释）无法核实新鲜度：返回 None 而非 0 秒。"""
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    beyond_tolerance = {
        "symbol": "600000",
        "snapshot_time": (now + timedelta(seconds=90)).isoformat(),
    }
    tz_bug_scale = {
        "symbol": "600000",
        "snapshot_time": (now + timedelta(hours=8)).isoformat(),
    }

    assert automation_module._snapshot_age_sec([beyond_tolerance], now) is None
    assert automation_module._snapshot_age_sec([tz_bug_scale], now) is None


def test_automation_now_defaults_to_market_timezone(tmp_path: Path) -> None:
    """API 未传 now 时默认时钟取市场时区（Asia/Shanghai，固定 UTC+8），而非 UTC。"""
    automation = RuntimeWeek5AutomationService(FakeService(tmp_path))

    now = automation._automation_now(None)

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=8)


def test_explicit_utc_now_converted_to_market_timezone(tmp_path: Path) -> None:
    """API 显式传入 UTC 时间：墙钟 09:25:00 必须按市场时区解释（年龄 300s 而非 None）。"""
    automation = RuntimeWeek5AutomationService(FakeService(tmp_path))

    converted = automation._automation_now(datetime(2026, 8, 25, 1, 30, tzinfo=UTC))

    # UTC 01:30 = 北京时间 09:30：换算后墙钟语义落在市场时区。
    assert converted.utcoffset() == timedelta(hours=8)
    assert converted.replace(tzinfo=None) == datetime(2026, 8, 25, 9, 30)

    frame = pd.DataFrame(
        [{"代码": "600000", "最新价": 10.5, "昨日收盘": 10.0, "更新时间": "09:25:00"}]
    )
    normalized = normalize_market_snapshot_frame(frame, timestamp=converted, source="efinance")
    rows = [{"symbol": "600000", "snapshot_time": normalized.iloc[0]["snapshot_time"]}]

    age = automation_module._snapshot_age_sec(rows, converted)

    assert age is not None
    assert abs(age - 300.0) < 1e-6


def test_market_wall_time_interpreted_in_market_timezone(tmp_path: Path) -> None:
    """行情源墙钟时间（如 09:25:00）必须按市场时区解释，不得落在 UTC。"""
    automation = RuntimeWeek5AutomationService(FakeService(tmp_path))
    frame = pd.DataFrame(
        [{"代码": "600000", "最新价": 10.5, "昨日收盘": 10.0, "更新时间": "09:25:00"}]
    )

    normalized = normalize_market_snapshot_frame(
        frame,
        timestamp=automation._automation_now(None),
        source="efinance",
    )

    parsed = datetime.fromisoformat(str(normalized.iloc[0]["snapshot_time"]))
    assert parsed.utcoffset() == timedelta(hours=8)


def test_candidate_gate_blocks_when_fresh_ratio_missing(tmp_path: Path) -> None:
    """freshness 报告存在但拿不到 fresh_ratio 时，candidate gate 必须拦截。"""
    service = FakeService(tmp_path)
    automation = RuntimeWeek5AutomationService(service)

    gate = automation._candidate_gate_from_report(
        {
            "data_gate": {"status": "ok", "reasons": []},
            "prefilter": {
                "universe_count": 100,
                "eligible_count": 100,
                "batch_coverage_ratio": 1.0,
                "intraday_freshness": {},
            },
        },
        [{"symbol": "600000", "score": 80.0}],
    )

    assert gate["status"] == "blocked"
    assert "intraday_freshness_missing" in gate["reasons"]


def test_fetch_batch_frame_backfills_partial_primary_from_backup(
    monkeypatch, tmp_path: Path
) -> None:
    """主源行数低于覆盖率门禁时，必须用备源补齐后合并交付。"""
    service = FakeService(tmp_path)
    service._config.week5.market_snapshot_min_rows = 2
    snapshot_service = Week5MarketSnapshotService(service)

    fake_ef = types.SimpleNamespace(
        stock=types.SimpleNamespace(
            get_realtime_quotes=lambda: pd.DataFrame([{"代码": "600000", "最新价": 10.0}])
        )
    )
    fake_ak = types.SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            [
                {"代码": "000001", "最新价": 9.0},
                {"代码": "600000", "最新价": 10.2},
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "efinance", fake_ef)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    frame, source, errors = snapshot_service._fetch_batch_frame()

    assert source == "efinance+akshare"
    assert any(str(item).startswith("efinance_partial_coverage") for item in errors)
    normalized = normalize_market_snapshot_frame(
        frame,
        timestamp=datetime(2026, 8, 25, 9, 25, tzinfo=UTC),
        source=source,
    )
    # 合并顺序保证重复 symbol 以主源（efinance）为准。
    assert sorted(normalized["symbol"].tolist()) == ["000001", "600000"]
    assert float(normalized.loc[normalized["symbol"] == "600000", "price"].iloc[0]) == 10.0


def test_fetch_batch_frame_rejects_when_all_sources_below_coverage(
    monkeypatch, tmp_path: Path
) -> None:
    """主备源都不满足覆盖率门禁时整体拒绝，不接受残缺快照。"""
    service = FakeService(tmp_path)
    service._config.week5.market_snapshot_min_rows = 5
    snapshot_service = Week5MarketSnapshotService(service)

    fake_ef = types.SimpleNamespace(
        stock=types.SimpleNamespace(
            get_realtime_quotes=lambda: pd.DataFrame([{"代码": "600000", "最新价": 10.0}])
        )
    )
    fake_ak = types.SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            [
                {"代码": "000001", "最新价": 9.0},
                {"代码": "600000", "最新价": 10.2},
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "efinance", fake_ef)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    frame, source, errors = snapshot_service._fetch_batch_frame()

    assert frame.empty
    assert source == "unavailable"
    assert any(str(item).startswith("efinance_partial_coverage") for item in errors)
    assert any(str(item).startswith("stock_zh_a_spot_em_merge_partial_coverage") for item in errors)


def test_fetch_batch_frame_counts_distinct_symbols_for_coverage(
    monkeypatch, tmp_path: Path
) -> None:
    """合并覆盖率按去重股票数判定：两源高度重叠时不得用重复行虚增通过。"""
    service = FakeService(tmp_path)
    service._config.week5.market_snapshot_min_rows = 3
    snapshot_service = Week5MarketSnapshotService(service)

    fake_ef = types.SimpleNamespace(
        stock=types.SimpleNamespace(
            get_realtime_quotes=lambda: pd.DataFrame([{"代码": "600000", "最新价": 10.0}])
        )
    )
    fake_ak = types.SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            [
                {"代码": "000001", "最新价": 9.0},
                {"代码": "600000", "最新价": 10.2},
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "efinance", fake_ef)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    frame, source, errors = snapshot_service._fetch_batch_frame()

    # 原始合并行数为 3（恰达阈值），但去重后只有 2 只 → 必须拒绝。
    assert frame.empty
    assert source == "unavailable"
    assert any(str(item) == "stock_zh_a_spot_em_merge_partial_coverage:2" for item in errors)


def test_fetch_batch_frame_primary_duplicates_do_not_pass_coverage(
    monkeypatch, tmp_path: Path
) -> None:
    """主源覆盖率同样按去重股票数判定：3 行重复股票不得按原始行数通过门禁。"""
    service = FakeService(tmp_path)
    service._config.week5.market_snapshot_min_rows = 3
    snapshot_service = Week5MarketSnapshotService(service)

    fake_ef = types.SimpleNamespace(
        stock=types.SimpleNamespace(
            get_realtime_quotes=lambda: pd.DataFrame(
                [
                    {"代码": "600000", "最新价": 10.0},
                    {"代码": "600000", "最新价": 10.1},
                    {"代码": "600000", "最新价": 10.2},
                ]
            )
        )
    )
    fake_ak = types.SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame([{"代码": "600000", "最新价": 10.3}])
    )
    monkeypatch.setitem(sys.modules, "efinance", fake_ef)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    frame, source, errors = snapshot_service._fetch_batch_frame()

    # 去重后仅 1 只（< 3）：不得以"efinance 有效快照"直接放行。
    assert frame.empty
    assert source == "unavailable"
    assert any(str(item) == "efinance_partial_coverage:1" for item in errors)
    assert any(str(item) == "stock_zh_a_spot_em_merge_partial_coverage:1" for item in errors)


def test_fetch_batch_frame_merges_heterogeneous_symbol_columns(
    monkeypatch, tmp_path: Path
) -> None:
    """真实列名契约：efinance 用 股票代码、AkShare 用 代码——合并后两源股票都必须存活。"""
    service = FakeService(tmp_path)
    service._config.week5.market_snapshot_min_rows = 3
    snapshot_service = Week5MarketSnapshotService(service)

    fake_ef = types.SimpleNamespace(
        stock=types.SimpleNamespace(
            get_realtime_quotes=lambda: pd.DataFrame(
                [
                    {"股票代码": "600000", "最新价": 10.0, "更新时间": "09:25:00"},
                    {"股票代码": "600001", "最新价": 11.0, "更新时间": "09:25:00"},
                ]
            )
        )
    )
    fake_ak = types.SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            [
                {"代码": "000001", "最新价": 9.0},
                {"代码": "000002", "最新价": 8.0},
                {"代码": "600000", "最新价": 10.2},
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "efinance", fake_ef)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    frame, source, errors = snapshot_service._fetch_batch_frame()

    assert source == "efinance+akshare"
    # 主源不足触发合并的诊断信息是预期行为（这正是合并发生的原因）。
    assert errors == ["efinance_partial_coverage:2"]
    # capture 阶段的二次规范化（与生产路径一致）：异构合并后两源股票都在
    # （4 只），重复 600000 以主源（efinance）价格为准。
    normalized = normalize_market_snapshot_frame(
        frame,
        timestamp=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
        source=source,
    )
    assert sorted(normalized["symbol"].tolist()) == ["000001", "000002", "600000", "600001"]
    assert float(normalized.loc[normalized["symbol"] == "600000", "price"].iloc[0]) == 10.0
    # 每行时间戳经二次规范化保留（efinance 墙钟 + 备源抓取兜底），不丢失。
    assert (normalized["snapshot_time"].astype(str) != "").all()


def test_fallback_night_scan_does_not_satisfy_idempotent_rerun(tmp_path: Path) -> None:
    """readiness 失败回退后，同晚重跑不得被旧 data_version 的 already_ran 短路。"""
    service = FakeService(tmp_path)
    service.current_week5_data_version = "snapshot-v1"
    service.scan_report = {
        "status": "ok",
        "data_snapshot_id": "snapshot-v1",
        "data_gate": {"status": "ok", "reasons": []},
        "prefilter": {
            "universe_count": 100,
            "eligible_count": 100,
            "batch_coverage_ratio": 1.0,
            "intraday_freshness": {"fresh_ratio": 1.0},
        },
        "signal_pool": {"candidates": [{"symbol": "600000", "shortlist_score": 82.0}]},
    }
    automation = RuntimeWeek5AutomationService(service)
    # 模拟前一晚成功扫描留下的状态：池 + data_version。
    automation._candidate_state.update(
        {
            "night_pool": [{"symbol": "600000", "score": 80.0}],
            "night_pool_updated_at": "2026-08-24T21:00:00+00:00",
            "candidate_data_gate": {"status": "watch_only"},
        },
        updated_at=datetime(2026, 8, 24, 21, 0, tzinfo=UTC),
        trade_date="2026-08-24",
        data_version="snapshot-v1",
    )
    automation._await_nightly_readiness = lambda: {
        "status": "blocked",
        "allowed": False,
        "reason": "updater_not_ready",
    }

    fallback_result = automation.run_night_scan(timestamp=datetime(2026, 8, 25, 21, 45, tzinfo=UTC))
    assert fallback_result["status"] == "fallback"
    assert service.scan_call_count == 0

    # readiness 恢复后同晚重跑：必须真正执行扫描，而不是 already_ran。
    automation._await_nightly_readiness = lambda: {
        "status": "ready",
        "allowed": True,
        "waited_sec": 0.0,
    }
    rerun = automation.run_night_scan(timestamp=datetime(2026, 8, 25, 22, 0, tzinfo=UTC))

    assert rerun["status"] == "ok"
    assert service.scan_call_count == 1
