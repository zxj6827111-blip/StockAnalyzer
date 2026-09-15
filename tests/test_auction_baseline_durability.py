"""竞价 baseline 的持久副本：候选状态被重置也不许清零预热。

背景（2026-09-16 实测）：`week5_automation_auction` 持续 `unavailable`，根因链是
①东财 push2 被封后 09:25 竞价快照回落到 **akshare**；②akshare 不提供竞价量比 →
`auction_volume_ratio` 5562 行全 null；③设计上的救援是"历史 09:25 baseline 中位数"，
但它要每股 **≥5 天** 才生效；④baseline 实测只有 **2 天**（9/14、9/15）。

④ 的原因不是逻辑错，而是 **baseline 只活在可被重置的 candidate_state 里**——任何状态重置
都会静默清零 5 天预热，等于让这个任务再瘫 5 天。本文件钉住"取更丰富的一份"这一修法。
"""

from __future__ import annotations

from pathlib import Path

from stock_analyzer.runtime.services.week5_automation_service import (
    RuntimeWeek5AutomationService,
    _baseline_date_count,
    _richer_baseline,
)


def _baseline(dates: list[str], symbol: str = "600000", values: int = 0) -> dict[str, object]:
    return {
        "dates": list(dates),
        "by_symbol": {symbol: [float(i) for i in range(values or len(dates))]},
    }


# --- 取更丰富的一份 ----------------------------------------------------------


def test_richer_baseline_prefers_more_dates() -> None:
    """持久副本攒了 5 天、candidate_state 被重置只剩 2 天 → 必须用持久副本。"""
    durable = _baseline(["D1", "D2", "D3", "D4", "D5"])
    reset = _baseline(["D4", "D5"])
    assert _richer_baseline(reset, durable)["dates"] == ["D1", "D2", "D3", "D4", "D5"]


def test_richer_baseline_keeps_live_when_it_is_ahead() -> None:
    """并列取第一个：今天刚写入的 candidate_state 优先。"""
    live = _baseline(["D1", "D2", "D3"])
    durable = _baseline(["D1", "D2"])
    assert _richer_baseline(live, durable) is not None
    assert len(_richer_baseline(live, durable)["dates"]) == 3


def test_richer_baseline_tolerates_junk() -> None:
    """状态文件可能缺字段/类型不对——不能因此抛异常把竞价链带崩。"""
    assert _richer_baseline({}, {}, {}) == {}
    assert _richer_baseline({"dates": "not-a-list"}, {"dates": ["D1"]})["dates"] == ["D1"]
    assert _baseline_date_count({"dates": None}) == 0
    assert _baseline_date_count({"dates": ["a", "b"]}) == 2


# --- 持久副本读写 ------------------------------------------------------------


class _ServiceStub:
    """只为触发 Week5AutomationService.__init__ 里的路径推导。"""

    def __init__(self, root: Path) -> None:
        from stock_analyzer.config import load_config

        self._config = load_config()
        self._config.week5.candidate_state_path = str(root / "week5_candidate_state.json")
        self._root = root

    def _resolve_evolution_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self._root / path


def test_baseline_path_sits_next_to_candidate_state(tmp_path: Path) -> None:
    service = RuntimeWeek5AutomationService(_ServiceStub(tmp_path))
    assert service._auction_baseline_path == tmp_path / "auction_baseline.json"


def test_persist_and_read_round_trip(tmp_path: Path) -> None:
    service = RuntimeWeek5AutomationService(_ServiceStub(tmp_path))
    assert service._read_durable_auction_baseline() == {}

    payload = _baseline(["D1", "D2", "D3"])
    returned = service._persist_auction_baseline(payload)
    assert returned == payload
    assert service._read_durable_auction_baseline() == payload

    # 二次写入覆盖且不留 .tmp 残留
    service._persist_auction_baseline(_baseline(["D1", "D2", "D3", "D4"]))
    assert len(service._read_durable_auction_baseline()["dates"]) == 4
    assert not (tmp_path / "auction_baseline.json.tmp").exists()


def test_persist_failure_does_not_raise(tmp_path: Path) -> None:
    """落盘失败必须吞掉：竞价链不能因为一个旁路持久化而中断。"""
    service = RuntimeWeek5AutomationService(_ServiceStub(tmp_path / "nope" / "deeper"))
    (tmp_path / "nope").write_text("阻塞目录", encoding="utf-8")  # mkdir 会失败
    assert service._persist_auction_baseline(_baseline(["D1"])) == _baseline(["D1"])


def test_read_tolerates_missing_and_corrupt(tmp_path: Path) -> None:
    service = RuntimeWeek5AutomationService(_ServiceStub(tmp_path))
    assert service._read_durable_auction_baseline() == {}
    service._auction_baseline_path.write_text("{not json", encoding="utf-8")
    assert service._read_durable_auction_baseline() == {}
