"""trend 尾盘影子链路的运行时接线验收（改进计划 §3.4 + §4 影子验证）。

钉住的是"影子不接管旧输出、算不出来就 0 只且原因可见"这几条，
而不是"能不能凑出几只推荐"。
"""

from __future__ import annotations

import ast
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
)
from stock_analyzer.runtime.services.trend_tail_shadow_service import (
    TrendTailShadowService,
)

CONTRACT = DEFAULT_TREND_CONTRACT
NOW = datetime(2026, 10, 9, 14, 45, 0)


class FakeService:
    def __init__(self, *, manifest: dict | None, tmp_path: Path) -> None:
        self._manifest = manifest
        self._tmp = tmp_path
        self._config = type("C", (), {"training": type("T", (), {
            "serving_manifest_path": "artifacts/model_serving_manifest.json"})()})()

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


def _bars(price: float = 10.0, *, day: date | None = None, lock_at: int | None = None) -> list:
    """14:20–14:44 的已完成分钟 bar。

    必须一直铺到接近 NOW：14:45 的确认在实盘上看到的最新已完成 bar 就是 14:44，
    只给到 14:31 的话先撞上的会是行情陈旧度门而不是被测规则本身。
    """
    day = day or NOW.date()
    up_limit = round(price * 1.1, 2)
    out = []
    for minute in range(20, 45):
        value = up_limit if minute == lock_at else price
        out.append((
            datetime(day.year, day.month, day.day, 14, minute),
            {"open": value, "high": value, "low": value, "close": value,
             "up_limit": up_limit, "trade_status": "normal"},
        ))
    return out


def _pool(symbols: list[str], *, features: bool = True) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "risk_state": "",
            "features": ({"excess_ret_20": 0.02, "atr14_pct": 0.02} if features else {}),
        }
        for symbol in symbols
    ]


def _run(tmp_path, *, watch_pool, probabilities, bars=None, service=None, **kwargs):
    service = service or FakeService(manifest=_tail_manifest(), tmp_path=tmp_path)
    svc = TrendTailShadowService(service, report_dir=tmp_path / "shadow")
    minute_bars = bars if bars is not None else {
        str(row["symbol"]): _bars() for row in watch_pool
    }
    return svc.run(
        timestamp=kwargs.pop("timestamp", NOW),
        watch_pool=watch_pool,
        minute_bars=minute_bars,
        probabilities=probabilities,
        **kwargs,
    )


def test_no_tail_probability_yields_zero_and_says_why(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]), probabilities={})
    assert report["mode"] == "shadow"
    assert report["final_symbols"] == []
    assert report["blocking_reason"] == "no_tail_probability_available"
    assert report["model_identity"]["recorded"] is False


def test_serving_model_from_another_label_policy_is_not_usable(tmp_path) -> None:
    service = FakeService(
        manifest={**_tail_manifest(), "label_policy_id": "label_policy_v2_soup"},
        tmp_path=tmp_path,
    )
    report = _run(
        tmp_path,
        watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9},
        service=service,
    )
    assert report["blocking_reason"] == "serving_model_is_not_tail_label_policy"
    assert report["final_symbols"] == []


def test_missing_serving_manifest_is_visible_not_defaulted(tmp_path) -> None:
    service = FakeService(manifest=None, tmp_path=tmp_path)
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9}, service=service,
    )
    assert report["blocking_reason"] == "serving_manifest_missing"


def test_recommendations_follow_threshold_and_cap(tmp_path) -> None:
    pool = _pool(["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"])
    probs = {
        "600000.SH": 0.71, "600001.SH": 0.66, "600002.SH": 0.65,
        "600003.SH": 0.64, "600004.SH": 0.40,
    }
    report = _run(tmp_path, watch_pool=pool, probabilities=probs)
    assert report["final_symbols"] == ["600000.SH", "600001.SH", "600002.SH"]
    assert report["filled"] == 5
    assert report["counts"]["rejected"] == 2  # 4. 达标但超名额 + 5. 低于阈值
    assert report["max_recommendations_effective"] == 3


def test_zero_selection_is_allowed_when_nothing_clears_the_bar(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH", "600001.SH"]),
        probabilities={"600000.SH": 0.3, "600001.SH": 0.55},
    )
    assert report["final_symbols"] == []
    assert report["filled"] == 2
    assert report["final_rejections"]["below_threshold"] == ["600000.SH", "600001.SH"]
    # 确认与成交都过了，只是没达阈值 —— 所以不能出现在确认层的拒绝原因里
    assert "below_threshold" not in report["rejected_reasons"]


def test_same_probability_orders_by_symbol(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600009.SH", "600002.SH"]),
        probabilities={"600009.SH": 0.8, "600002.SH": 0.8},
    )
    assert report["final_symbols"] == ["600002.SH", "600009.SH"]


def test_missing_minute_bars_are_reported_not_replaced_by_open_price(tmp_path) -> None:
    """计划 §5：分钟行情不足 → 记为阻塞，不得用开盘口径顶替。"""
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9}, bars={},
    )
    assert report["rejected_reasons"]["minute_bars_unavailable"] == ["600000.SH"]
    assert report["filled"] == 0
    assert report["final_symbols"] == []


def test_stale_live_snapshot_blocks_confirmation(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9},
        timestamp=datetime(2026, 10, 9, 14, 59),
    )
    assert report["rejected_reasons"]["realtime_snapshot_stale"] == ["600000.SH"]
    assert report["final_symbols"] == []


def test_limit_up_locked_entry_is_recorded_as_no_fill(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9},
        bars={"600000.SH": _bars(lock_at=31)},
    )
    assert report["rejected_reasons"]["limit_up_locked"] == ["600000.SH"]


def test_capital_budget_can_only_tighten_the_cap(tmp_path) -> None:
    pool = _pool(["600000.SH", "600001.SH", "600002.SH"])
    probs = {"600000.SH": 0.9, "600001.SH": 0.85, "600002.SH": 0.8}
    one = _run(tmp_path, watch_pool=pool, probabilities=probs,
               capital_budget=CONTRACT.reference_notional)
    assert one["final_symbols"] == ["600000.SH"]
    assert one["max_recommendations_effective"] == 1

    broke = _run(tmp_path, watch_pool=pool, probabilities=probs, capital_budget=1000.0)
    assert broke["final_symbols"] == []
    assert broke["blocking_reason"] == "capital_budget_exhausted"

    generous = _run(tmp_path, watch_pool=pool, probabilities=probs,
                    capital_budget=10_000_000.0)
    assert len(generous["final_symbols"]) == 3


def test_archive_rows_carry_probability_meaning_and_fill_state(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.77})
    row = report["final_recommendations"][0]
    assert row["probability"] == pytest.approx(0.77)
    assert row["probability_field"] == NET_PROFIT_PROBABILITY_FIELD
    assert row["strategy"] == "trend"
    assert row["reference_notional"] == pytest.approx(10_000.0)
    assert row["contract_digest"] == CONTRACT.digest()
    assert row["data_as_of"].startswith("2026-10-09T14:31")  # 确认 14:30 → 成交 14:31
    assert row["fill"]["filled"] is True and row["fill"]["quantity"] == 1000
    assert row["feature_snapshot"]["excess_ret_20"] == pytest.approx(0.02)
    assert row["caveats"] == []


def test_missing_feature_snapshot_is_a_visible_caveat(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"], features=False),
                  probabilities={"600000.SH": 0.77})
    assert "feature_snapshot_missing" in report["final_recommendations"][0]["caveats"]


def test_shadow_artifacts_are_written_per_trade_date(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.77})
    paths = report["artifact_paths"]
    trace = json.loads(Path(paths["funnel_trace"]).read_text(encoding="utf-8"))
    assert trace["trade_date"] == "2026-10-09"
    assert trace["stages"][0]["stage"] == "night_watch_pool"
    assert trace["stages"][0]["kind"] == "predictive"
    assert trace["stages"][0]["model_identity"]["identity_recorded"] is True
    assert trace["final_recommendations"][0]["symbol"] == "600000.SH"
    stored = json.loads(Path(paths["shadow_report"]).read_text(encoding="utf-8"))
    assert stored["mode"] == "shadow"


def test_history_mode_without_a_clock_still_needs_bars(tmp_path) -> None:
    """历史模式（timestamp=None）不写成交易日期即整体拒绝，不猜当天。"""
    report = TrendTailShadowService(
        FakeService(manifest=_tail_manifest(), tmp_path=tmp_path),
        report_dir=tmp_path / "shadow",
    ).run(timestamp=None, watch_pool=_pool(["600000.SH"]),
          minute_bars={"600000.SH": _bars()},
          probabilities={"600000.SH": 0.9})
    assert report["final_symbols"] == []
    assert report["rejected_reasons"]["no_trade_date"] == ["600000.SH"]


# ---------------------------------------------------------------------------
# 接线守卫：影子链路必须在 live runtime 里被调用，且不动 actionable_signals
# ---------------------------------------------------------------------------

AUTOMATION_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src/stock_analyzer/runtime/services/week5_automation_service.py"
)


def _function_source(name: str) -> str:
    tree = ast.parse(AUTOMATION_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(
                AUTOMATION_SOURCE.read_text(encoding="utf-8"), node
            ) or ""
    raise AssertionError(f"{name} not found in week5_automation_service.py")


def test_live_runtime_calls_the_tail_shadow_service() -> None:
    body = _function_source("run_live_runtime")
    assert "_trend_tail_shadow_report(" in body
    assert '"trend_tail_shadow"' in body


def test_tail_shadow_never_touches_the_legacy_actionable_list() -> None:
    """影子就是影子：接线点只能新增键，不能改写 actionable_signals。"""
    body = _function_source("run_live_runtime")
    tail_index = body.index("_trend_tail_shadow_report(")
    assert 'actionable = ' not in body[tail_index:]
    assert 'report["actionable_signals"]' not in body[tail_index:]


def test_shadow_failures_are_reported_not_swallowed() -> None:
    body = _function_source("_trend_tail_shadow_report")
    assert '"ok": False' in body and '"error":' in body
