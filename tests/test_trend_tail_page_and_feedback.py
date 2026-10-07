"""§3.4 收尾：页面视图（候选/最终推荐/成交状态分列）与成熟反馈闭环。

钉住两件事：

1. 页面必须**分列**候选、最终推荐与成交状态，并把"这个概率是什么口径"写进响应；
   元信息缺失时报错，而不是让前端自己猜。
2. 成熟反馈只走到 challenger 建议为止：净盈利率分母只含已实现样本，
   observed 与 replayed 不互借样本量，且输出里不存在任何晋升动作。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    STATUS_FILLED,
    STATUS_NOT_FILLED,
    STATUS_UNCERTAIN,
)
from stock_analyzer.labels.tail_net_profit import (
    CAPTURE_OBSERVED,
    CAPTURE_REPLAYED,
    TailLabelRecord,
)
from stock_analyzer.research.tail_mature_feedback import (
    MIN_FEEDBACK_DAYS,
    MIN_FEEDBACK_SAMPLES,
    STATE_CHALLENGER_SUGGESTED,
    STATE_KEEP_OBSERVING,
    STATE_SHADOW_ONLY,
    reject_reason_feedback,
    summarize_mature_feedback,
)
from stock_analyzer.runtime.services.trend_tail_shadow_service import (
    TrendTailShadowService,
    page_view,
    tail_shadow_history,
    tail_shadow_page,
)

CONTRACT = DEFAULT_TREND_CONTRACT
NOW = datetime(2026, 10, 9, 14, 45, 0)
START = date(2026, 4, 1)
#: 22 个决策日：刚好跨过 MIN_FEEDBACK_DAYS，用来证明门槛是按天数卡的。
DAYS = tuple(START + timedelta(days=offset) for offset in range(22))


def _record(**overrides) -> TailLabelRecord:
    fields: dict = {
        "symbol": "600000.SH",
        "decision_date": START,
        "entry_date": date(2026, 10, 12),
        "status": STATUS_FILLED,
        "reason": "take_profit",
        "confirmed": True,
        "filled": True,
        "trainable": True,
        "label": 1.0,
        "net_return": 0.08,
        "gross_return": 0.09,
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
        "sell_cost": 8.0,
        "take_profit_hit": True,
        "stop_loss_hit": False,
        "ambiguous_same_bar": False,
        "gap_exit": False,
        "deferred_sessions": 0,
        "corporate_action_uncertain": False,
        "model_version": "trend-tail-lgbm-2026q4",
        "market_state": "trend",
    }
    fields.update(overrides)
    return TailLabelRecord(**fields)


def _realized(count: int, *, net_return: float, days: tuple[date, ...] = (START,),
              **overrides) -> list[TailLabelRecord]:
    """count 个**已实现**样本（同向盈亏），按 days 轮转决策日。"""
    out = []
    for index in range(count):
        out.append(_record(
            net_return=net_return,
            gross_return=abs(net_return) * 1.1,
            label=1.0 if net_return > 0 else 0.0,
            take_profit_hit=net_return > 0,
            stop_loss_hit=net_return <= 0,
            reason="take_profit" if net_return > 0 else "stop_loss",
            decision_date=days[index % len(days)],
            **overrides,
        ))
    return out


def _no_outcome(count: int, *, status: str, reason: str,
                days: tuple[date, ...] = (START,)) -> list[TailLabelRecord]:
    """未成交 / 不确定样本：买不进或还没到可成交退出，不得计入盈亏。"""
    return [
        _record(
            status=status, reason=reason, trainable=False, label=None,
            net_return=None, gross_return=None,
            filled=(status != STATUS_NOT_FILLED),
            decision_date=days[index % len(days)],
        )
        for index in range(count)
    ]


# --- 页面视图 -------------------------------------------------------------


class FakeService:
    def __init__(self, *, manifest: dict | None, tmp_path: Path) -> None:
        self._manifest = manifest
        self._tmp = tmp_path
        self._config = type(
            "C", (), {
                "training": type("T", (), {
                    "serving_manifest_path": "artifacts/model_serving_manifest.json",
                })(),
                "week5": type("W", (), {"tail_shadow_report_dir": "shadow"})(),
            },
        )()

    def _resolve_evolution_path(self, value):
        return str(self._tmp / str(value).replace("artifacts/", ""))

    def _read_serving_manifest(self):
        return dict(self._manifest) if self._manifest else {}

    def _runtime_code_commit(self):
        return "cafe123"


def _tail_manifest() -> dict:
    return {
        "model_id": "trend-tail-lgbm-2026q4",
        "artifact_content_hash": "sha256:abcdef",
        "code_commit": "cafe123",
        "label_policy_id": "label_policy_v4_07335bbe3d3e",
    }


def _bars(price: float = 10.0) -> list:
    day = NOW.date()
    up_limit = round(price * 1.1, 2)
    return [
        (
            datetime(day.year, day.month, day.day, 14, minute),
            {"open": price, "high": price, "low": price, "close": price,
             "up_limit": up_limit, "trade_status": "normal"},
        )
        for minute in range(20, 45)
    ]


def _report(tmp_path: Path, symbols: list[str], probabilities: dict[str, float],
            *, bars: dict[str, list] | None = None) -> dict:
    service = FakeService(manifest=_tail_manifest(), tmp_path=tmp_path)
    svc = TrendTailShadowService(service, report_dir=tmp_path / "shadow")
    return svc.run(
        timestamp=NOW,
        watch_pool=[
            {"symbol": symbol, "risk_state": "",
             "features": {"excess_ret_20": 0.02, "atr14_pct": 0.02}}
            for symbol in symbols
        ],
        minute_bars=bars if bars is not None else {symbol: _bars() for symbol in symbols},
        probabilities=probabilities,
    )


def test_page_view_splits_candidates_final_recommendations_and_fills(tmp_path) -> None:
    report = _report(
        tmp_path,
        ["600000.SH", "600001.SH", "600002.SH"],
        {"600000.SH": 0.71, "600001.SH": 0.66, "600002.SH": 0.20},
    )
    view = page_view(report)

    assert view["candidates"] == ["600000.SH", "600001.SH", "600002.SH"]
    assert [row["symbol"] for row in view["final_recommendations"]] == [
        "600000.SH", "600001.SH",
    ]
    # 成交状态是独立一列，被拒的股票不会被顺带算成"已成交"。
    assert set(view["fills"]) == {"600000.SH", "600001.SH"}
    assert view["fills"]["600000.SH"]["filled"] is True
    assert view["fills"]["600000.SH"]["quantity"] == 1000
    assert view["rejection_reasons"]["final_ranking"]["below_threshold"] == ["600002.SH"]


def test_page_view_states_probability_meaning_notional_and_data_date(tmp_path) -> None:
    view = page_view(_report(tmp_path, ["600000.SH"], {"600000.SH": 0.71}))
    meta = view["meta"]

    assert meta["probability_field"] == NET_PROFIT_PROBABILITY_FIELD
    assert meta["strategy"] == "trend"
    assert meta["reference_notional_cny"] == 10_000.0
    assert meta["trade_date"] == "2026-10-09"
    assert meta["data_as_of"].startswith("2026-10-09")
    assert meta["contract_version"] == CONTRACT.contract_version
    assert meta["contract_digest"] == CONTRACT.digest()
    assert meta["entry_window"] == ["14:30", "14:50"]
    # 口径必须写成"扣费后净收益>0"，不能让页面把它读成涨幅预期或命中证明。
    assert "净收益" in meta["probability_meaning"]
    assert "滑点" in meta["probability_meaning"]


@pytest.mark.parametrize("missing", [
    "trade_date", "probability_field", "reference_notional",
    "contract_version", "contract_digest",
])
def test_page_view_refuses_to_guess_missing_meta(tmp_path, missing: str) -> None:
    report = dict(_report(tmp_path, ["600000.SH"], {"600000.SH": 0.71}))
    report[missing] = ""
    with pytest.raises(ValueError, match=missing):
        page_view(report)


def test_zero_final_recommendations_still_lists_candidates(tmp_path) -> None:
    view = page_view(_report(
        tmp_path, ["600000.SH", "600001.SH"], {"600000.SH": 0.31, "600001.SH": 0.59}
    ))
    assert view["final_recommendations"] == []
    assert view["fills"] == {}
    assert view["candidates"] == ["600000.SH", "600001.SH"]
    assert set(view["rejection_reasons"]["final_ranking"]["below_threshold"]) == {
        "600000.SH", "600001.SH",
    }


def test_model_blocked_run_is_visible_on_the_page(tmp_path) -> None:
    # 没有 p_net_profit_5d_tail 概率时：0 只推荐 + 原因可见，不补名额。
    view = page_view(_report(tmp_path, ["600000.SH"], {}))
    assert view["final_recommendations"] == []
    assert view["blocking_reason"] == "no_tail_probability_available"


def test_tail_shadow_page_reads_the_archived_report(tmp_path) -> None:
    service = FakeService(manifest=_tail_manifest(), tmp_path=tmp_path)
    report = _report(tmp_path, ["600000.SH"], {"600000.SH": 0.71})

    view = tail_shadow_page(service)
    assert view["status"] == "ok"
    assert view["meta"]["trade_date"] == "2026-10-09"
    assert [row["symbol"] for row in view["final_recommendations"]] == ["600000.SH"]
    # 读的是落盘留档，内容与 run() 当场返回的一致（页面不另算一套）。
    archived = json.loads(
        (tmp_path / "shadow" / f"tail_shadow_report_{report['trade_date']}.json")
        .read_text(encoding="utf-8")
    )
    assert page_view(archived) == view


def test_tail_shadow_page_without_report_says_so(tmp_path) -> None:
    service = FakeService(manifest=_tail_manifest(), tmp_path=tmp_path)
    view = tail_shadow_page(service)
    assert view["status"] == "no_report"
    assert view["blocking_reason"] == "no_tail_shadow_report"
    assert view["final_recommendations"] == []


def test_tail_shadow_history_lists_days_and_respects_limit(tmp_path) -> None:
    service = FakeService(manifest=_tail_manifest(), tmp_path=tmp_path)
    payload = json.loads(json.dumps(
        _report(tmp_path, ["600000.SH"], {"600000.SH": 0.71}), default=str
    ))
    directory = tmp_path / "shadow"
    for day in ("2026-10-10", "2026-10-13"):
        (directory / f"tail_shadow_report_{day}.json").write_text(
            json.dumps(dict(payload, trade_date=day,
                            final_recommendations=[], final_symbols=[])),
            encoding="utf-8",
        )

    history = tail_shadow_history(service, limit=3)
    assert history["count"] == 3
    assert [item["trade_date"] for item in history["days"]] == [
        "2026-10-09", "2026-10-10", "2026-10-13",
    ]
    assert history["days"][0]["final_symbols"] == ["600000.SH"]
    # 空的那天如实报空，不拿前一天的推荐顶着。
    assert history["days"][1]["final_symbols"] == []
    assert tail_shadow_history(service, limit=1)["count"] == 1


def test_tail_shadow_endpoints_are_read_only() -> None:
    # api.deps 通过 sys.modules 取主模块，先导入 main 才能导入 week5。
    from stock_analyzer import main as main_module  # noqa: F401
    from stock_analyzer.api import week5

    routes = {
        (method, str(route.path))
        for route in week5.router.routes
        for method in (route.methods or set())
    }
    assert ("GET", "/week5/tail-shadow/latest") in routes
    assert ("GET", "/week5/tail-shadow/history") in routes
    # 影子链路没有写入口：它只能被夜扫任务带起来，不能被 API 手动触发成生产动作。
    assert not [key for key in routes if "tail-shadow" in key[1] and key[0] != "GET"]


# --- 成熟反馈闭环 ---------------------------------------------------------


def test_net_profit_rate_denominator_excludes_unfilled_and_uncertain() -> None:
    records = (
        _realized(4, net_return=0.08)
        + _realized(2, net_return=-0.05)
        + _no_outcome(6, status=STATUS_NOT_FILLED, reason="limit_up_locked")
        + _no_outcome(3, status=STATUS_UNCERTAIN, reason="insufficient_data_at_series_end")
    )
    out = summarize_mature_feedback(records)
    (slice_,) = out["slices"]

    assert slice_["candidates"] == 15
    assert slice_["realized"] == 6
    # 6 个已实现里 4 个盈利：分母若把未成交/不确定算进去会变成 4/15。
    assert slice_["net_profits"] == 4
    assert slice_["net_profit_rate"] == pytest.approx(4 / 6)
    assert slice_["fill_rate"] == pytest.approx(6 / 15)
    assert slice_["uncertain"] == 3
    assert slice_["reject_reasons"]["limit_up_locked"] == 6


def test_feedback_is_sliced_by_model_version_and_market_state() -> None:
    records = (
        _realized(2, net_return=0.08, market_state="trend")
        + _realized(2, net_return=-0.08, market_state="range")
        + _realized(2, net_return=0.08, model_version="logistic-b")
    )
    out = summarize_mature_feedback(records)
    keyed = {
        (item["model_version"], item["market_state"]): item["net_profit_rate"]
        for item in out["slices"]
    }

    assert keyed == {
        ("logistic-b", "trend"): 1.0,
        ("trend-tail-lgbm-2026q4", "range"): 0.0,
        ("trend-tail-lgbm-2026q4", "trend"): 1.0,
    }


def test_missing_identity_falls_into_explicit_bucket_instead_of_vanishing() -> None:
    out = summarize_mature_feedback(
        _realized(2, net_return=0.08, model_version="", market_state="")
    )
    (slice_,) = out["slices"]
    assert slice_["model_version"] == "unattributed"
    assert slice_["market_state"] == "unknown"


def test_replayed_samples_do_not_borrow_observed_sample_count() -> None:
    # 重建样本再漂亮也不能顶掉 observed 的样本量门槛（本项目实测两者差一个量级）。
    out = summarize_mature_feedback(
        _realized(MIN_FEEDBACK_SAMPLES * 2, net_return=0.08,
                  capture_mode=CAPTURE_REPLAYED),
        baseline_rate=0.40,
    )

    assert out["state"] == STATE_SHADOW_ONLY
    assert "no_observed_snapshot_samples" in out["blockers"]
    assert out["observed_overall"]["net_profit_rate"] is None
    assert out["slices"][0]["capture_mode"] == CAPTURE_REPLAYED


def _conclusive() -> list[TailLabelRecord]:
    return (
        _realized(MIN_FEEDBACK_SAMPLES, net_return=0.08, days=DAYS)
        + _realized(MIN_FEEDBACK_SAMPLES, net_return=-0.02, days=DAYS)
    )


def test_thresholds_gate_the_challenger_suggestion() -> None:
    out = summarize_mature_feedback(_realized(6, net_return=0.08), baseline_rate=0.40)
    assert out["state"] == STATE_KEEP_OBSERVING
    assert f"realized_observed=6<{MIN_FEEDBACK_SAMPLES}" in out["blockers"]
    assert f"decision_days=1<{MIN_FEEDBACK_DAYS}" in out["blockers"]

    # 200 个已实现样本摊在 22 个决策日上，50% vs 40% 基线 = +10pp → 才允许建议。
    out = summarize_mature_feedback(_conclusive(), baseline_rate=0.40)
    assert out["state"] == STATE_CHALLENGER_SUGGESTED
    assert out["blockers"] == []
    assert out["observed_overall"]["net_profit_rate"] == pytest.approx(0.5)
    assert out["observed_overall"]["decision_days"] == len(DAYS)

    # 同一批样本，基线只差 2pp → 继续观察，不生成 challenger。
    out = summarize_mature_feedback(_conclusive(), baseline_rate=0.48)
    assert out["state"] == STATE_KEEP_OBSERVING
    assert "improvement_below_5pp" in out["blockers"]


def test_feedback_without_baseline_cannot_claim_improvement() -> None:
    out = summarize_mature_feedback(_realized(6, net_return=0.08))
    assert out["observed_overall"]["improvement_vs_baseline_pp"] is None
    assert "baseline_rate_not_supplied" in out["blockers"]
    assert out["state"] == STATE_KEEP_OBSERVING


def test_automatic_learning_stops_at_challenger() -> None:
    out = summarize_mature_feedback(_conclusive(), baseline_rate=0.30)
    assert out["state"] == STATE_CHALLENGER_SUGGESTED
    assert out["promotion"] == "manual_only_via_learning_governance_release_ticket"
    # 输出面是封闭的：没有任何"已晋升/已发布/已切换"字段——自动路径拿不到晋升权。
    assert set(out) == {"state", "blockers", "slices", "observed_overall",
                        "contract_digest", "promotion"}
    assert out["state"] in {STATE_CHALLENGER_SUGGESTED, STATE_KEEP_OBSERVING,
                            STATE_SHADOW_ONLY}


def test_reject_reason_feedback_keeps_unfilled_rejections_separate() -> None:
    rows = [
        {"reason": "below_threshold", "net_return": 0.09, "realized": True},
        {"reason": "below_threshold", "net_return": -0.02, "realized": True},
        {"reason": "below_threshold", "net_return": None, "realized": False},
        {"reason": "capital_budget_cap", "net_return": None, "realized": False},
    ]
    out = reject_reason_feedback(rows)

    assert out["below_threshold"]["n"] == 3
    assert out["below_threshold"]["n_realized"] == 2
    assert out["below_threshold"]["net_profit_rate"] == pytest.approx(0.5)
    # 全部未成交的拒绝原因：率是 None，不是 0，也不是 100%。
    assert out["capital_budget_cap"]["net_profit_rate"] is None
    assert out["capital_budget_cap"]["mean_net_return"] is None


def test_label_record_carries_feedback_dimensions() -> None:
    payload = _record(model_version="lgbm-c", market_state="range").to_dict()
    assert payload["model_version"] == "lgbm-c"
    assert payload["market_state"] == "range"
