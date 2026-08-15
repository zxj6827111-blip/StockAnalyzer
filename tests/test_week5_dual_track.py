"""P1 双轨输出：trend_candidates vs monster_watchlist + week5_output_mode 开关。"""

from __future__ import annotations

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.runtime.services.week5_service import RuntimeWeek5Service
from tests.test_service_portfolio import _load_test_config


def _service_config(dual: bool) -> StockAnalyzerConfig:
    config = _load_test_config()
    config.week5.week5_output_mode = "dual_track" if dual else "legacy"
    return config


def _build_service(dual: bool) -> RuntimeWeek5Service:
    from stock_analyzer.runtime.service import StockAnalyzerService

    service = StockAnalyzerService(config=_service_config(dual))
    return RuntimeWeek5Service(service)


def _final_selector(
    *,
    final_symbols: list[tuple[str, float]],
) -> dict[str, object]:
    return {
        "final_signals": [
            {"symbol": symbol, "score": score, "action": "buy"}
            for symbol, score in final_symbols
        ],
        "selected_count": len(final_symbols),
    }


def test_dual_track_split_outputs_trend_and_monster() -> None:
    worker = _build_service(dual=True)
    signal_map = {
        "600000": {"symbol": "600000", "score": 88.0, "reasons": ["limit_up_3d"]},
        "300001": {"symbol": "300001", "score": 75.0, "reasons": ["soup_entry"]},
    }
    # trend 轨只有 300001 进入 final；600000 因连板风险留在 monster 观察池
    trend_signal_map = {"300001": {"symbol": "300001", "score": 75.0, "reasons": ["soup_entry"]}}
    output = worker._build_dual_track_output(
        final_selector=_final_selector(final_symbols=[("300001", 75.0)]),
        signal_map=signal_map,
        trend_signal_map=trend_signal_map,
        dual_track=True,
    )
    assert output["mode"] == "dual_track"
    assert output["final_signals_source"] == "trend"
    trend_symbols = {item["symbol"] for item in output["trend_candidates"]}
    assert trend_symbols == {"300001"}
    assert all(item["executable"] is True for item in output["trend_candidates"])
    monster = {item["symbol"]: item for item in output["monster_watchlist"]}
    assert "600000" in monster
    assert monster["600000"]["executable"] is False
    assert "limit_up_3d" in monster["600000"]["risk_reasons"]
    # final 中的符号不会同时出现在 monster watchlist
    assert "300001" not in monster


def test_legacy_mode_keeps_legacy_structure() -> None:
    worker = _build_service(dual=False)
    output = worker._build_dual_track_output(
        final_selector=_final_selector(final_symbols=[("600000", 88.0)]),
        signal_map={"600000": {"symbol": "600000", "score": 88.0}},
        trend_signal_map={},
        dual_track=False,
    )
    assert output["mode"] == "legacy"
    assert output["final_signals_source"] == "legacy"
    assert output["trend_candidates"] == []
    assert output["monster_watchlist"] == []


def test_dual_track_config_round_trips() -> None:
    config = _service_config(dual=True)
    assert config.week5.week5_output_mode == "dual_track"
    legacy = _service_config(dual=False)
    assert legacy.week5.week5_output_mode == "legacy"


def test_monster_watchlist_sorted_by_score_desc() -> None:
    worker = _build_service(dual=True)
    signal_map = {
        "000001": {"symbol": "000001", "score": 60.0, "reasons": ["a"]},
        "600000": {"symbol": "600000", "score": 90.0, "reasons": ["b"]},
    }
    output = worker._build_dual_track_output(
        final_selector=_final_selector(final_symbols=[]),
        signal_map=signal_map,
        trend_signal_map={},
        dual_track=True,
    )
    symbols = [item["symbol"] for item in output["monster_watchlist"]]
    assert symbols == ["600000", "000001"]
