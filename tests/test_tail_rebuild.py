"""历史重建消费侧（改进计划 §3.1 接线、§3.3 重建样本、§4 线上与历史一致）。

钉住的是接线本身，不是又一套判定逻辑：判定只有 ``build_tail_net_profit_label``
一个出口，这里保证它拿到的是**研究库里真的有的**精确涨跌停、停复牌与日线序列，
并且任何一样缺了都记成"参考数据不足"，而不是伪装成未成交、亏损或停牌。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    hard_gate_confirmation,
)
from stock_analyzer.labels.tail_net_profit import (
    CAPTURE_REPLAYED,
    build_tail_net_profit_label,
)
from stock_analyzer.research.minute_bar_store import MinuteBarStore
from stock_analyzer.research.tail_rebuild import (
    RebuildRequest,
    TailRebuildError,
    TailRebuildOutcome,
    rebuild_tail_label,
    rebuild_tail_labels,
    summarize_rebuild,
)
from stock_analyzer.research.tail_reference_store import TailReferenceStore

_SYMBOL = "600000.SH"
DECISION = date(2026, 3, 6)          # 周五夜扫
ENTRY = date(2026, 3, 9)             # 次一交易日：入场日 = 第 1 日
SESSIONS = [date(2026, 3, d) for d in (9, 10, 11, 12, 13)]
_AS_OF = "2026-03-13"

_CLI = Path(__file__).resolve().parents[1] / "scripts" / "rebuild_tail_labels.py"

ALWAYS_CONFIRM = lambda context: (True, "")  # noqa: E731


def _minute_frame(*, fill_open: float = 10.05) -> pd.DataFrame:
    """入场日 14:25–14:50 的 1 分钟 bar（bar 终点即完成时刻）。"""
    rows = []
    for minute in range(25, 51):
        price = 10.00 if minute <= 30 else fill_open
        rows.append({
            "symbol": _SYMBOL,
            "bar_time": datetime(2026, 3, 9, 14, minute),
            "open": price, "high": price + 0.02, "low": price - 0.02, "close": price,
            "volume": 1e5, "amount": price * 1e5,
        })
    return pd.DataFrame(rows)


def _exit_minute_frame() -> pd.DataFrame:
    """完整的 vendor bar_end 交易日，用于验证出场只消费 RAW 分钟价。"""
    times = [datetime(2000, 1, 1, 9, 30)]
    times.extend(datetime(2000, 1, 1, 9, minute) for minute in range(31, 60))
    times.extend(datetime(2000, 1, 1, 10, minute) for minute in range(60))
    times.extend(datetime(2000, 1, 1, 11, minute) for minute in range(31))
    times.extend(datetime(2000, 1, 1, 13, minute) for minute in range(1, 60))
    times.extend(datetime(2000, 1, 1, 14, minute) for minute in range(60))
    times.append(datetime(2000, 1, 1, 15, 0))
    prices = {SESSIONS[1]: 10.20, SESSIONS[2]: 10.30,
              SESSIONS[3]: 10.45, SESSIONS[4]: 10.60}
    rows = []
    for day, price in prices.items():
        for stamp in times:
            bar_time = datetime(day.year, day.month, day.day, stamp.hour, stamp.minute)
            rows.append({
                "symbol": _SYMBOL, "bar_time": bar_time,
                "open": price, "high": price, "low": price, "close": price,
                "volume": 1e5, "amount": price * 1e5,
            })
    return pd.DataFrame(rows)


def _seed(
    tmp_path: Path,
    *,
    minutes: bool = True,
    bars: bool = True,
    drop_day: date | None = None,
    limits: bool = True,
    approximated: bool = False,
    declare_status: bool = True,
    fill_open: float = 10.05,
    action_coverage: bool = True,
) -> Path:
    """写一个"五类参考数据齐 + 分钟齐"的研究库，按需抽掉某一样来触发不足分支。"""
    path = tmp_path / "research.duckdb"
    with TailReferenceStore(path) as store:
        store.upsert_calendar([DECISION, *SESSIONS], as_of=_AS_OF)
        if bars:
            days = [day for day in SESSIONS if day != drop_day]
            # 逐日抬价：入场 10.05，第 5 日收盘 10.60 → 未触及 +8% / -5%。
            closes = {SESSIONS[0]: 10.05, SESSIONS[1]: 10.20, SESSIONS[2]: 10.30,
                      SESSIONS[3]: 10.45, SESSIONS[4]: 10.60}
            store.upsert_daily_bars(
                pd.DataFrame([
                    {
                        "symbol": _SYMBOL, "date": day,
                        "open": closes[day], "high": closes[day] + 0.05,
                        "low": closes[day] - 0.10, "close": closes[day],
                        "volume": 1e6,
                    }
                    for day in days
                ]),
                price_basis="raw", as_of=_AS_OF,
            )
        if limits:
            store.upsert_limit_prices(
                pd.DataFrame([
                    {"symbol": _SYMBOL, "trade_date": day, "up_limit": 11.06,
                     "down_limit": 9.05}
                    for day in SESSIONS if day != drop_day
                ]),
                as_of=_AS_OF, approximated=approximated,
            )
        statuses = [
            {"symbol": _SYMBOL, "trade_date": day, "suspended": False}
            for day in SESSIONS if day != drop_day
        ]
        if declare_status:
            for row in statuses:
                row["trade_status"] = "normal"
        store.upsert_suspend_status(pd.DataFrame(statuses), as_of=_AS_OF)
        if action_coverage:
            store.upsert_corporate_action_coverage(
                pd.DataFrame([
                    {"ex_date": day, "coverage_complete": True,
                     "rows_returned": 0, "truncated": False}
                    for day in SESSIONS
                ]),
                source="test_fixture", as_of=_AS_OF,
            )
        store.upsert_adj_factors(pd.DataFrame([
            {"symbol": _SYMBOL, "trade_date": day, "adj_factor": 1.0,
             "coverage_complete": True}
            for day in [DECISION, *SESSIONS]
        ]), source="test_adj_factor", as_of=_AS_OF)
    if minutes:
        with MinuteBarStore(path) as store:
            store.upsert_frame(
                pd.concat([
                    _minute_frame(fill_open=fill_open), _exit_minute_frame(),
                ], ignore_index=True), interval="1m",
                price_basis="raw", bar_time_semantics="bar_end", source="vendor_zip",
            )
    return path


def _rebuild(path: Path, *, confirmation=ALWAYS_CONFIRM, **kwargs):
    request = kwargs.pop("request", None) or RebuildRequest(
        symbol=_SYMBOL, decision_date=DECISION, entry_date=ENTRY
    )
    with TailReferenceStore(path) as reference, MinuteBarStore(path) as minutes:
        return rebuild_tail_label(
            reference=reference, minutes=minutes, request=request,
            confirmation=confirmation, **kwargs,
        )


def test_a_complete_research_db_produces_a_matured_rebuilt_label(tmp_path) -> None:
    outcome = _rebuild(_seed(tmp_path))
    assert outcome.sufficient, outcome.missing
    record = outcome.record
    assert record is not None
    assert record.filled and record.trainable
    # 第 5 个交易日（3-13）收盘退出，这是"入场日算第 1 日"的口径。
    assert record.status == "filled_and_exited"
    assert record.net_return is not None and record.net_return > 0
    assert record.capture_mode == CAPTURE_REPLAYED


def test_day_limits_reach_the_minute_bars_so_limit_up_lock_is_provable(tmp_path) -> None:
    """研究库路径必须能自证"涨停锁死"：分钟源没有 up_limit，靠日级权威补进来。

    14:31 的开价 = 涨停价 → 确认通过但买不进，且这是**未成交**，不是亏损样本。
    """
    path = _seed(tmp_path, fill_open=11.06)
    outcome = _rebuild(path)
    record = outcome.record
    assert record is not None and record.confirmed
    assert record.filled is False and record.trainable is False
    assert record.label is None
    assert record.reason == "limit_up_locked"


def test_suspend_flag_from_the_reference_store_blocks_the_fill(tmp_path) -> None:
    """停复牌说这天停牌 → 判定是 suspended，而不是"没有分钟 bar"或亏损。"""
    path = _seed(tmp_path)
    with TailReferenceStore(path) as store:
        store.upsert_suspend_status(
            pd.DataFrame([{"symbol": _SYMBOL, "trade_date": ENTRY, "suspended": True,
                           "suspend_type": "S", "trade_status": "S"}]),
            as_of=_AS_OF,
        )
    record = _rebuild(path).record
    assert record is not None
    assert record.filled is False and record.reason == "suspended"


def test_verified_suspension_with_zero_volume_vendor_rows_is_not_a_minute_conflict(
    tmp_path,
) -> None:
    path = _seed(tmp_path)
    zero_volume_rows = _exit_minute_frame()
    zero_volume_rows = zero_volume_rows.loc[
        pd.to_datetime(zero_volume_rows["bar_time"]).dt.date == SESSIONS[1]
    ].copy()
    zero_volume_rows["volume"] = 0.0
    zero_volume_rows["amount"] = 0.0
    with MinuteBarStore(path) as store:
        store.upsert_frame(
            zero_volume_rows, interval="1m", price_basis="raw",
            bar_time_semantics="bar_end", source="vendor_zip",
        )
    with TailReferenceStore(path) as store:
        store.upsert_suspend_status(
            pd.DataFrame([{
                "symbol": _SYMBOL,
                "trade_date": SESSIONS[1],
                "suspended": True,
                "suspend_type": "S",
                "trade_status": "suspended",
                "coverage_complete": True,
            }]),
            source="tushare_suspend_d",
            as_of=_AS_OF,
        )

    outcome = _rebuild(path)

    assert outcome.sufficient is True
    assert outcome.record is not None
    assert outcome.record.status == "filled_and_exited"
    assert outcome.record.reason == ""
    assert outcome.record.details["suspended_zero_volume_placeholder_days"] == [
        SESSIONS[1].isoformat()
    ]
    assert outcome.detail["raw_minute_session_coverage"][SESSIONS[1].isoformat()][
        "zero_volume_placeholder"
    ] is True


def test_verified_full_day_suspension_resolves_missing_entry_prices_as_no_fill(tmp_path) -> None:
    path = _seed(tmp_path, bars=False, minutes=False, limits=False)
    with TailReferenceStore(path) as store:
        store.upsert_suspend_status(
            pd.DataFrame([{
                "symbol": _SYMBOL,
                "trade_date": ENTRY,
                "suspended": True,
                "suspend_type": "S",
                "trade_status": "suspended",
                "coverage_complete": True,
            }]),
            source="tushare_suspend_d",
            as_of=_AS_OF,
        )

    outcome = _rebuild(path)
    assert outcome.sufficient is True
    assert outcome.missing == ()
    assert outcome.minute_bar_count == 0
    assert outcome.detail["entry_day_status"] == "verified_full_day_suspension"
    assert outcome.record is not None
    assert outcome.record.status == "not_filled"
    assert outcome.record.reason == "suspended"
    assert outcome.record.confirmed is False
    assert outcome.record.filled is False
    assert outcome.record.trainable is False
    assert outcome.record.label is None
    assert outcome.record.net_return is None


def test_generic_or_incomplete_suspension_does_not_resolve_entry_price_gap(tmp_path) -> None:
    path = _seed(tmp_path, bars=False, minutes=False, limits=False)
    with TailReferenceStore(path) as store:
        store.upsert_suspend_status(
            pd.DataFrame([{
                "symbol": _SYMBOL,
                "trade_date": ENTRY,
                "suspended": True,
                "suspend_type": "S",
                "trade_status": "suspended",
                "coverage_complete": False,
            }]),
            source="warehouse",
            as_of=_AS_OF,
        )

    outcome = _rebuild(path)
    assert outcome.sufficient is False
    assert "entry_daily_bar" in outcome.missing
    assert "entry_day_limit_prices" in outcome.missing
    assert "entry_minute_bars" in outcome.missing


def test_trade_status_from_the_source_is_not_masked_by_a_default(tmp_path) -> None:
    """回归钉：分钟 bar 缺状态列时不能自己填 "normal"，否则日级 "S" 会被吞掉。"""
    path = _seed(tmp_path)
    with MinuteBarStore(path) as minutes:
        bare = minutes.bars_for(_SYMBOL, ENTRY)
        assert all("trade_status" not in bar for _, bar in bare)
        injected = minutes.bars_for(_SYMBOL, ENTRY, day_limits={"trade_status": "S"})
        assert all(bar["trade_status"] == "S" for _, bar in injected)

    # 分钟源自己声明了状态时，更细的那一层优先，日级值不得覆盖它。
    with MinuteBarStore(path) as minutes:
        minutes.upsert_frame(
            _minute_frame().assign(trade_status="normal"), interval="1m",
            price_basis="raw", bar_time_semantics="bar_end", source="vendor_zip",
        )
        kept = minutes.bars_for(_SYMBOL, ENTRY, day_limits={"trade_status": "S"})
        assert all(bar["trade_status"] == "normal" for _, bar in kept)


def test_missing_minute_bars_are_insufficient_and_never_an_open_price_backtest(
    tmp_path,
) -> None:
    """§5：历史分钟行情不足要记成阻塞，不许换成开盘价回测。"""
    outcome = _rebuild(_seed(tmp_path, minutes=False))
    assert outcome.sufficient is False
    assert outcome.missing == ("entry_minute_bars",)
    assert outcome.record is None          # 连未成交都不算，更不产出盈亏
    assert outcome.minute_bar_count == 0


def test_missing_next_bar_inside_entry_window_is_insufficient(tmp_path) -> None:
    path = _seed(tmp_path, minutes=False)
    with MinuteBarStore(path) as minutes:
        frame = _minute_frame().loc[
            lambda value: value["bar_time"] != datetime(2026, 3, 9, 14, 31)
        ]
        minutes.upsert_frame(
            frame, interval="1m", price_basis="raw", bar_time_semantics="bar_end",
            source="vendor_zip",
        )
    outcome = _rebuild(path)
    assert outcome.sufficient is False
    assert outcome.missing == ("entry_minute_window_incomplete",)
    assert outcome.detail["entry_minute_window_coverage"]["missing_times"] == ["14:31"]


def test_missing_corporate_action_coverage_never_emits_a_realized_label(tmp_path) -> None:
    outcome = _rebuild(_seed(tmp_path, action_coverage=False))
    record = outcome.record
    assert outcome.sufficient
    assert record is not None
    assert record.status == "uncertain"
    assert record.trainable is False and record.label is None
    assert record.reason == "corporate_action_coverage_incomplete"


@pytest.mark.parametrize("kwargs,expected", [
    ({"limits": False}, "entry_day_limit_prices"),
    ({"bars": False}, "entry_daily_bar"),
])
def test_reference_gaps_are_named_not_folded_into_the_fill_rate(
    tmp_path, kwargs, expected
) -> None:
    outcome = _rebuild(_seed(tmp_path, **kwargs))
    assert outcome.sufficient is False
    assert outcome.missing == (expected,)
    assert outcome.record is None


def test_approximated_limit_prices_are_not_used_for_the_lock_decision(tmp_path) -> None:
    outcome = _rebuild(_seed(tmp_path, limits=True, approximated=True))
    assert outcome.sufficient is False
    assert "entry_day_limit_prices" in outcome.missing


def test_a_session_hole_inside_the_window_blocks_instead_of_shifting_day_five(
    tmp_path,
) -> None:
    """日历说 3-11 开市、日线却没有：跳过去数"第 5 日"会落在错误日期上。"""
    outcome = _rebuild(_seed(tmp_path, drop_day=date(2026, 3, 11)))
    assert outcome.sufficient is False
    assert "daily_bar_session_holes" in outcome.missing
    assert "2026-03-11" in outcome.detail["session_holes_inside_window"]


def test_missing_status_declaration_yields_no_realized_label_but_is_not_a_gap(
    tmp_path,
) -> None:
    """§3.3：未知交易状态不得生成已实现盈亏标签 —— 但这是数据来源缺口，要留名。"""
    outcome = _rebuild(_seed(tmp_path, declare_status=False))
    record = outcome.record
    assert record is not None and record.filled
    assert record.trainable is False and record.label is None
    assert record.reason == "unknown_trade_status"
    assert outcome.detail["trade_status_source_gap"] is True


def test_live_and_rebuild_paths_agree_on_identical_bars(tmp_path) -> None:
    """§4：相同输入下线上与历史必须给出一致的筛选与交易判定。

    同一批 bar，一次以"线上"方式喂（带 ``quote_as_of`` 收盘时钟），一次走重建；
    判定的成交时点、价格与标签必须逐项相同。
    """
    path = _seed(tmp_path)
    outcome = _rebuild(path)
    with (
        TailReferenceStore(path) as reference,
        MinuteBarStore(path) as minutes,
    ):
        bars = minutes.bars_for(
            _SYMBOL, ENTRY, day_limits=reference.day_limits(_SYMBOL, ENTRY)
        )
        from stock_analyzer.research.tail_rebuild import _load_exit_minute_window

        session_series = reference.daily_bar_series(
            _SYMBOL, ENTRY, SESSIONS[-1]
        )
        exit_bars, exit_sessions, _detail, action_data = _load_exit_minute_window(
            reference=reference, minutes=minutes,
            request=RebuildRequest(symbol=_SYMBOL, decision_date=DECISION, entry_date=ENTRY),
            sessions=session_series["sessions"],
            missing_limit_days=session_series["missing_limit_price_days"],
            end=SESSIONS[-1], interval="1m",
        )
        live = build_tail_net_profit_label(
            symbol=_SYMBOL, decision_date=DECISION, entry_date=ENTRY,
            minute_bars=bars,
            daily_bars=exit_sessions,
            exit_minute_bars=exit_bars,
            corporate_actions=action_data["actions"],
            corporate_action_coverage=action_data["coverage_by_day"],
            confirmation=ALWAYS_CONFIRM,
            # 线上时钟：14:32，此时 14:31 那根已完成且不陈旧（契约 120 秒）。
            quote_as_of=datetime(2026, 3, 9, 14, 32),
            capture_mode="observed_snapshot",
        )
    rebuilt = outcome.record
    assert rebuilt is not None
    for field_name in ("confirmed", "filled", "trainable", "label", "net_return",
                       "confirmation_slot", "fill_time", "entry_price", "quantity"):
        assert getattr(live, field_name) == getattr(rebuilt, field_name), field_name


def test_request_rejects_an_entry_day_that_is_not_the_next_session(tmp_path) -> None:
    with pytest.raises(TailRebuildError, match="must be after"):
        RebuildRequest(symbol=_SYMBOL, decision_date=ENTRY, entry_date=DECISION)


def test_batch_rebuild_uses_symbol_order_internally_but_preserves_result_order(
    monkeypatch,
) -> None:
    from stock_analyzer.research import tail_rebuild

    first = RebuildRequest("600000", date(2026, 3, 9), date(2026, 3, 10))
    second = RebuildRequest("000001", date(2026, 3, 6), date(2026, 3, 9))
    third = RebuildRequest("600000", date(2026, 3, 6), date(2026, 3, 9))
    execution_order: list[RebuildRequest] = []

    def fake_rebuild(**kwargs):
        request = kwargs["request"]
        execution_order.append(request)
        return TailRebuildOutcome(request, True, (), None, 0, 0)

    monkeypatch.setattr(tail_rebuild, "rebuild_tail_label", fake_rebuild)
    results = rebuild_tail_labels(
        reference=object(), minutes=object(), requests=[first, second, third],
        confirmation=ALWAYS_CONFIRM,
    )

    assert execution_order == [second, third, first]
    assert [item.request for item in results] == [first, second, third]


def test_summary_keeps_gaps_fill_rate_and_labels_apart(tmp_path) -> None:
    """成交率分母只含判得动的样本；不足的单列，不冒充未成交。"""
    good = _seed(tmp_path / "good")
    locked = _seed(tmp_path / "locked", fill_open=11.06)
    blind = _seed(tmp_path / "blind", minutes=False)
    requests = [
        RebuildRequest(symbol=_SYMBOL, decision_date=DECISION, entry_date=ENTRY),
    ]
    outcomes = []
    for path in (good, locked, blind):
        outcomes.extend(rebuild_tail_labels(
            reference=TailReferenceStore(path), minutes=MinuteBarStore(path),
            requests=requests, confirmation=ALWAYS_CONFIRM,
        ))
    summary = summarize_rebuild(outcomes)
    assert summary["requests"] == 3
    assert summary["insufficient"] == 1
    assert summary["missing_reference_inputs"] == {"entry_minute_bars": 1}
    assert summary["label_records"] == 2
    assert summary["filled"] == 1
    # 涨停锁死算进成交率分母（判得动、只是买不进），不足的不算。
    assert summary["fill_rate"] == pytest.approx(0.5)
    assert summary["net_profits"] == 1
    assert summary["rebuild_blocked_on_reference_data"] is True


def test_summary_distinguishes_trade_status_gap_requests_symbols_and_days() -> None:
    requests = [
        RebuildRequest("000001", date(2026, 3, 2), date(2026, 3, 3)),
        RebuildRequest("000001", date(2026, 3, 3), date(2026, 3, 4)),
        RebuildRequest("600000", date(2026, 3, 2), date(2026, 3, 3)),
    ]
    undeclared_days = [
        ["2026-03-05"],
        ["2026-03-05", "2026-03-06"],
        ["2026-03-05"],
    ]
    outcomes = [
        TailRebuildOutcome(
            request=request, sufficient=True, missing=(), record=None,
            minute_bar_count=0, session_days=0,
            detail={
                "trade_status_source_gap": True,
                "undeclared_trade_status_days": days,
            },
        )
        for request, days in zip(requests, undeclared_days, strict=True)
    ]

    summary = summarize_rebuild(outcomes)

    assert summary["trade_status_source_gap_requests"] == 3
    assert summary["trade_status_source_gap_symbols"] == 2
    assert summary["trade_status_source_gap_symbol_days"] == 3


def test_contract_digest_is_recorded_on_every_rebuilt_label(tmp_path) -> None:
    """标签必须能被追溯到它是在哪一版契约下算出来的。"""
    record = _rebuild(_seed(tmp_path)).record
    assert record is not None
    assert record.contract_digest == DEFAULT_TREND_CONTRACT.digest()


def test_live_service_and_contract_share_one_confirmation_object() -> None:
    """§4 的一致性靠"同一个函数对象"保证，不是靠两边各写一份再对拍。"""
    from stock_analyzer.runtime.services import trend_tail_shadow_service as shadow

    assert shadow.hard_gate_confirmation is hard_gate_confirmation
    assert not hasattr(shadow, "_hard_gate_confirmation")


def _run_cli(tmp_path, args, *, db):
    env = {"PATH": "/usr/bin:/bin"}  # 不给 PYTHONPATH：脚本自身的 src 引导必须真的生效
    return subprocess.run(
        [sys.executable, str(_CLI), "--db", str(db), *args],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )


def _requests_file(tmp_path, rows) -> Path:
    path = tmp_path / "requests.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_cli_writes_labels_and_exits_zero_when_reference_is_complete(tmp_path) -> None:
    db = _seed(tmp_path)
    labels = tmp_path / "labels.jsonl"
    report = tmp_path / "report.json"
    requests = _requests_file(tmp_path, [{
        "symbol": _SYMBOL, "decision_date": DECISION.isoformat(),
        "entry_date": ENTRY.isoformat(),
        "overnight_features": {"ret_5": 0.03},
        "model_probabilities": {"p_net_profit_5d_tail": 0.63},
    }])
    result = _run_cli(
        tmp_path,
        ["--requests", str(requests), "--labels", str(labels), "--report", str(report),
         "--zero-cost", "--quiet"],
        db=db,
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in labels.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["capture_mode"] == CAPTURE_REPLAYED
    payload = json.loads(report.read_text(encoding="utf-8"))
    # 无费用标签要自己承认这一点，不能被当成验收数字。
    assert payload["cost_model"] == "zero_cost_debug"
    assert payload["confirmation_predicate"].endswith("hard_gate_confirmation")


def test_cli_exports_trade_status_source_gaps_separately(tmp_path) -> None:
    db = _seed(tmp_path, declare_status=False)
    requests = _requests_file(tmp_path, [{
        "symbol": _SYMBOL, "decision_date": DECISION.isoformat(),
        "entry_date": ENTRY.isoformat(),
    }])
    labels = tmp_path / "labels.jsonl"
    status_gaps = tmp_path / "status_gaps.jsonl"
    report = tmp_path / "report.json"
    result = _run_cli(
        tmp_path,
        ["--requests", str(requests), "--labels", str(labels),
         "--status-gaps", str(status_gaps), "--report", str(report),
         "--zero-cost", "--quiet"],
        db=db,
    )

    assert result.returncode == 0, result.stderr
    gap_rows = [json.loads(line) for line in status_gaps.read_text(encoding="utf-8").splitlines()]
    assert len(gap_rows) == 1
    assert gap_rows[0]["detail"]["trade_status_source_gap"] is True
    assert gap_rows[0]["detail"]["undeclared_trade_status_days"]
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["reference_gap_records"] == 0
    assert payload["trade_status_source_gap_records"] == 1
    assert payload["trade_status_source_gap_output"] == str(status_gaps)


def test_cli_exits_three_when_reference_data_is_insufficient(tmp_path) -> None:
    db = _seed(tmp_path, minutes=False)
    requests = _requests_file(tmp_path, [{
        "symbol": _SYMBOL, "decision_date": DECISION.isoformat(), "entry_date": ENTRY.isoformat(),
    }])
    labels = tmp_path / "labels.jsonl"
    gaps = tmp_path / "gaps.jsonl"
    report = tmp_path / "report.json"
    result = _run_cli(
        tmp_path, ["--requests", str(requests), "--labels", str(labels),
                   "--gaps", str(gaps),
                   "--report", str(report), "--zero-cost", "--quiet"],
        db=db,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "参考数据不足" in result.stdout
    # 不足的单列进报告，不进样本流：样本文件必须是空的，而不是"一条未成交"。
    assert labels.read_text(encoding="utf-8").strip() == ""
    gap_rows = [json.loads(line) for line in gaps.read_text(encoding="utf-8").splitlines()]
    assert len(gap_rows) == 1
    assert gap_rows[0]["missing_reference_inputs"] == ["entry_minute_bars"]
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["missing_reference_inputs"] == {"entry_minute_bars": 1}
    assert payload["label_records"] == 0
    assert payload["reference_gap_records"] == 1


def test_cli_rejects_a_malformed_request_line(tmp_path) -> None:
    db = _seed(tmp_path)
    requests = _requests_file(tmp_path, [{"symbol": _SYMBOL, "decision_date": "not-a-date"}])
    result = _run_cli(tmp_path, ["--requests", str(requests), "--zero-cost"], db=db)
    assert result.returncode == 5
    assert "请求不合法" in result.stderr

