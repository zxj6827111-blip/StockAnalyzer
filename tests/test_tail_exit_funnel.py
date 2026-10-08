"""§2 漏斗最后一层"成交与退出"：把最终推荐与其后的成熟退出对上并留档。

入场那天写不出这一层（退出最多顺延 5 个交易日才成熟），所以它必须是**另一份**留档，
而不是回头覆盖入场当天的文件；而"推荐了却没有标签记录"必须留在证据里，不能被当成
"没赚没亏"悄悄抹平。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    FUNNEL_LAYERS,
    STATUS_FILLED,
    STATUS_NOT_FILLED,
    STATUS_UNCERTAIN,
)
from stock_analyzer.labels.tail_net_profit import CAPTURE_OBSERVED, TailLabelRecord
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    FunnelTraceError,
    build_funnel_trace,
    read_trace,
    write_trace,
)
from stock_analyzer.research.tail_mature_feedback import (
    DISPOSITION_LOSS,
    DISPOSITION_NO_RECORD,
    DISPOSITION_NOT_FILLED,
    DISPOSITION_PENDING,
    DISPOSITION_PROFIT,
    DISPOSITION_UNCERTAIN,
    attach_exit_outcomes,
    execution_exit_stage,
)
from stock_analyzer.runtime.services.trend_tail_shadow_service import (
    TrendTailShadowService,
)

CONTRACT = DEFAULT_TREND_CONTRACT
NOW = datetime(2026, 10, 12, 14, 45)
DAY = date(2026, 10, 12)

_CLI = Path(__file__).resolve().parents[1] / "scripts" / "record_tail_exit_funnel.py"


def _record(**overrides) -> TailLabelRecord:
    fields: dict = {
        "symbol": "600000.SH",
        "decision_date": DAY,
        "entry_date": DAY + timedelta(days=1),
        "status": STATUS_FILLED,
        "reason": "take_profit",
        "confirmed": True,
        "filled": True,
        "trainable": True,
        "label": 1.0,
        "net_return": 0.06,
        "gross_return": 0.07,
        "capture_mode": CAPTURE_OBSERVED,
        "contract_version": CONTRACT.contract_version,
        "contract_digest": CONTRACT.digest(),
        "cost_model_version": CONTRACT.cost_model_version,
        "price_basis": "tail_confirm_next_bar",
        "holding_days": CONTRACT.holding_days,
        "take_profit_pct": CONTRACT.take_profit_pct,
        "stop_loss_pct": CONTRACT.stop_loss_pct,
        "reference_notional": CONTRACT.reference_notional,
        "label_anchor_time": datetime(2026, 10, 12, 14, 31),
        "label_mature_time": datetime(2026, 10, 16, 15, 0),
        "confirmation_slot": datetime(2026, 10, 12, 14, 30),
        "fill_time": datetime(2026, 10, 12, 14, 31),
        "entry_price": 10.0,
        "quantity": 1000,
        "buy_cost": 5.0,
        "sell_cost": 15.0,
        "take_profit_hit": True,
        "stop_loss_hit": False,
        "ambiguous_same_bar": False,
        "gap_exit": False,
        "deferred_sessions": 0,
        "corporate_action_uncertain": False,
        "model_version": "trend-tail-lgbm-2026q4",
        "market_state": "range",
    }
    fields.update(overrides)
    return TailLabelRecord(**fields)


def _row(symbol: str, probability: float = 0.8) -> dict:
    return {
        "symbol": symbol,
        "rank": 1,
        "probability": probability,
        "model_identity": {"identity_recorded": True, "label_policy_id": "label_policy_v4_x",
                           "feature_compute_version": 3},
        "fill": {"filled": True},
    }


def _report(rows: list[dict]) -> dict:
    return {
        "trade_date": DAY.isoformat(),
        "contract_digest": CONTRACT.digest(),
        "final_recommendations": rows,
    }


# ---------------------------------------------------------------------------
# 留档读回
# ---------------------------------------------------------------------------


def test_record_round_trips_through_its_archived_form() -> None:
    record = _record()
    revived = TailLabelRecord.from_dict(record.to_dict())
    assert revived == record
    assert isinstance(revived.decision_date, date)
    assert isinstance(revived.label_mature_time, datetime)


def test_from_dict_refuses_a_truncated_archive_instead_of_defaulting_it() -> None:
    payload = _record().to_dict()
    del payload["net_return"]
    with pytest.raises(Exception, match="missing field"):
        TailLabelRecord.from_dict(payload)


# ---------------------------------------------------------------------------
# 退出归类
# ---------------------------------------------------------------------------


def test_every_recommendation_gets_an_explicit_disposition() -> None:
    rows = [_row(f"60000{i}.SH") for i in range(5)]
    records = [
        _record(symbol="600000.SH", net_return=0.06, label=1.0),
        _record(symbol="600001.SH", net_return=-0.05, label=0.0, reason="stop_loss",
                take_profit_hit=False, stop_loss_hit=True),
        _record(symbol="600002.SH", status=STATUS_UNCERTAIN, trainable=False,
                label=None, net_return=None, reason="unknown_trade_status"),
        _record(symbol="600003.SH", status=STATUS_NOT_FILLED, filled=False, confirmed=False,
                trainable=False, label=None, net_return=None, reason="limit_up_locked"),
        _record(symbol="600004.SH", trainable=False, label=None, net_return=None,
                reason="holding_not_matured"),
        # 600005.SH 只被推荐、没有标签记录：必须显式留在证据里。
    ]
    exits = attach_exit_outcomes(shadow_report=_report(rows + [_row("600005.SH")]),
                                records=records)
    dispositions = {key: value["disposition"] for key, value in exits["dispositions"].items()}
    assert dispositions == {
        "600000.SH": DISPOSITION_PROFIT,
        "600001.SH": DISPOSITION_LOSS,
        "600002.SH": DISPOSITION_UNCERTAIN,
        "600003.SH": DISPOSITION_NOT_FILLED,
        "600004.SH": DISPOSITION_PENDING,
        "600005.SH": DISPOSITION_NO_RECORD,
    }
    assert exits["realized"] == 2
    assert exits["net_profits"] == 1
    assert exits["net_profit_rate"] == 0.5
    assert "600004.SH" in exits["maturity_pending"]
    assert "recommended_symbols_without_label_record" in exits["caveats"]


def test_net_profit_rate_only_counts_realized_exits() -> None:
    rows = [_row("600000.SH"), _row("600001.SH"), _row("600002.SH")]
    records = [
        _record(symbol="600000.SH"),
        _record(symbol="600001.SH", net_return=-0.04, label=0.0),
        _record(symbol="600002.SH", trainable=False, label=None, net_return=None),
    ]
    exits = attach_exit_outcomes(shadow_report=_report(rows), records=records)
    assert exits["realized"] == 2
    assert exits["net_profit_rate"] == 0.5
    # 未成熟那条既不进分子也不进分母
    assert exits["counts"][DISPOSITION_PENDING] == 1


def test_flat_exit_is_not_counted_as_a_profit() -> None:
    exits = attach_exit_outcomes(
        shadow_report=_report([_row("600000.SH")]),
        records=[_record(net_return=0.0, label=0.0)],
    )
    assert exits["dispositions"]["600000.SH"]["disposition"] == DISPOSITION_LOSS


def test_joining_a_different_trade_date_is_refused() -> None:
    with pytest.raises(ValueError, match="report is dated"):
        attach_exit_outcomes(
            shadow_report=_report([_row("600000.SH")]),
            records=[_record()],
            trade_date=date(2026, 10, 13),
        )


def test_labels_from_another_decision_day_do_not_leak_in() -> None:
    exits = attach_exit_outcomes(
        shadow_report=_report([_row("600000.SH")]),
        records=[_record(decision_date=DAY + timedelta(days=1))],
    )
    assert exits["dispositions"]["600000.SH"]["disposition"] == DISPOSITION_NO_RECORD


# ---------------------------------------------------------------------------
# 落成漏斗的一层
# ---------------------------------------------------------------------------


def _exits() -> dict:
    return attach_exit_outcomes(
        shadow_report=_report([_row(f"60000{i}.SH") for i in range(3)]),
        records=[
            _record(symbol="600000.SH"),
            _record(symbol="600001.SH", net_return=-0.05, label=0.0),
            _record(symbol="600002.SH", trainable=False, label=None, net_return=None),
        ],
    )


def test_execution_exit_is_a_self_consistent_hard_gate_layer() -> None:
    stage = execution_exit_stage(exits=_exits())
    assert stage.stage == "execution_exit" and stage.stage in FUNNEL_LAYERS
    assert stage.kind == KIND_HARD_GATE
    assert stage.inputs == 3 and stage.advanced == 2
    assert stage.inputs == stage.advanced + sum(stage.rejected.values())
    assert stage.rejected[DISPOSITION_PENDING] == 1
    assert stage.rejected_symbols[DISPOSITION_PENDING] == ("600002.SH",)
    assert stage.calibrated_probabilities["600000.SH"] == pytest.approx(0.8)
    assert stage.model_identity["identity_recorded"] is True


def test_exit_layer_keeps_the_day_it_matured_as_its_data_time() -> None:
    stage = execution_exit_stage(exits=_exits())
    assert stage.data_as_of == "2026-10-16T15:00:00"


def test_missing_identity_stays_visible_in_the_exit_layer() -> None:
    exits = _exits()
    exits["model_identity"] = {"identity_recorded": False, "reason": "archived_identity_missing"}
    stage = execution_exit_stage(exits=exits)
    assert stage.model_identity["identity_recorded"] is False


def test_the_exit_trace_does_not_clobber_the_entry_trace(tmp_path) -> None:
    trace = build_funnel_trace(
        trade_date=DAY, stages=[execution_exit_stage(exits=_exits())], contract=CONTRACT
    )
    entry = write_trace(trace, tmp_path)
    exit_path = write_trace(trace, tmp_path, suffix="execution_exit")
    assert entry != exit_path
    assert exit_path.name == f"funnel_trace_{DAY.isoformat()}_execution_exit.json"
    assert read_trace(exit_path)["stages"][0]["stage"] == "execution_exit"


def test_an_unknown_layer_name_is_rejected_at_construction() -> None:
    from dataclasses import replace

    stage = execution_exit_stage(exits=_exits())
    with pytest.raises(FunnelTraceError, match="unknown funnel stage"):
        replace(stage, stage="vibes")


# ---------------------------------------------------------------------------
# CLI：用真实留档产出这一层
# ---------------------------------------------------------------------------


class FakeService:
    def __init__(self, *, tmp_path: Path) -> None:
        self._tmp = tmp_path

    def _resolve_evolution_path(self, value):
        return str(self._tmp / str(value).replace("artifacts/", ""))

    def _read_serving_manifest(self):
        return {
            "model_id": "trend-tail-lgbm-2026q4",
            "artifact_content_hash": "sha256:abcdef",
            "code_commit": "cafe123",
            "label_policy_id": "label_policy_v4_07335bbe3d3e",
        }

    def _runtime_code_commit(self):
        return "cafe123"


def _bars() -> list:
    return [
        (datetime(NOW.year, NOW.month, NOW.day, 14, minute),
         {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
          "up_limit": 11.0, "trade_status": "normal"})
        for minute in range(20, 45)
    ]


def _archived_report(tmp_path) -> dict:
    symbols = ["600000.SH", "600001.SH"]
    service = TrendTailShadowService(FakeService(tmp_path=tmp_path), report_dir="shadow")
    return service.run(
        timestamp=NOW,
        watch_pool=[{"symbol": s, "risk_state": "", "features": {"atr14_pct": 0.02}}
                    for s in symbols],
        minute_bars={s: _bars() for s in symbols},
        probabilities={s: 0.9 - index * 0.05 for index, s in enumerate(symbols)},
    )


def test_cli_writes_the_exit_layer_from_real_archived_artifacts(tmp_path) -> None:
    report = _archived_report(tmp_path)
    report_path = Path(report["artifact_paths"]["shadow_report"])
    labels = [
        _record(symbol="600000.SH", net_return=0.06),
        _record(symbol="600001.SH", net_return=-0.05, label=0.0,
                take_profit_hit=False, stop_loss_hit=True, reason="stop_loss"),
    ]
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(
        "\n".join(json.dumps(item.to_dict(), default=str) for item in labels),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(_CLI), "--report", str(report_path),
         "--labels", str(labels_path), "--out-dir", str(tmp_path / "shadow"),
         "--trade-date", DAY.isoformat()],
        capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(
        (tmp_path / "shadow" / f"funnel_trace_{DAY.isoformat()}_execution_exit.json")
        .read_text(encoding="utf-8")
    )
    stage = payload["stages"][0]
    assert stage["stage"] == "execution_exit" and stage["kind"] == KIND_HARD_GATE
    assert stage["inputs"] == 2 == stage["advanced"]
    assert payload["trade_date"] == DAY.isoformat()
    # 入场那天的留档仍在原处、没有被这次退出留档覆盖
    entry = json.loads(
        (tmp_path / "shadow" / f"funnel_trace_{DAY.isoformat()}.json").read_text("utf-8")
    )
    assert [item["stage"] for item in entry["stages"]] == [
        "night_watch_pool", "tail_confirmation", "final_recommendation",
    ]


def test_cli_refuses_a_report_from_another_contract(tmp_path) -> None:
    report = _archived_report(tmp_path)
    report_path = Path(report["artifact_paths"]["shadow_report"])
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["contract_digest"] = "deadbeef"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_CLI), "--report", str(report_path),
         "--labels", str(labels_path), "--out-dir", str(tmp_path / "out")],
        capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 5
    assert "契约" in result.stderr
