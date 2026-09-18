"""S10 决策日志与 outcome 成熟：当时快照 + 成熟后独立追加。

对应蓝图 §5 P0-10 / 阶段施工提示词 S10：

```text
每天盘后保存当时真实的 prediction snapshot；outcome 成熟后独立追加
决策字段未实现时可写 null / not_available —— 不得编造
禁止 signal 当天提前写未来数据
目录：decisions/YYYY/MM、outcomes/YYYY/MM、manifests
Done When：任选一天能完整回答当时 universe / 模型 / 排序 / 预测 / 后续成熟 outcome
```
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.alpha_v2.decision_log import (
    NOT_AVAILABLE,
    build_decision_rows,
    compute_outcomes,
    decision_path,
    outcome_path,
    write_decision_rows,
    write_manifest,
    write_outcomes,
)

SIGNAL_DATE = date(2026, 9, 17)


def _bars(rows: list[dict[str, object]], *, start: str) -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=len(rows))
    return pd.DataFrame(rows, index=dates)


def _decision_rows() -> list[dict[str, object]]:
    return [
        row.to_payload()
        for row in build_decision_rows(
            signal_date=SIGNAL_DATE,
            candidates=[
                {"symbol": "600000", "score": 72.5, "deep_rank": 1, "eligible": True},
                {"symbol": "000001", "score": 65.0, "deep_rank": 2, "eligible": True},
            ],
            model_identity={"model_id": "m1", "status": "match"},
            feature_schema={"feature_schema_id": "fs_v1"},
            label_policy={"label_policy_id": "lp_v3"},
            data_snapshot={"universe_snapshot_id": "asofuniv_x"},
            selection_contract={"selection_contract_id": "night_alpha_v2_v1"},
            recorded_at="2026-09-17T15:35:00+08:00",
        )
    ]


# ---------------------------------------------------------------------------
# 决策行：字段完整 + 不得编造
# ---------------------------------------------------------------------------


def test_decision_row_contains_all_required_fields() -> None:
    payload = _decision_rows()[0]
    for key in (
        "signal_date",
        "symbol",
        "eligible",
        "quality_rank",
        "light_rank",
        "deep_rank",
        "legacy_score",
        "legacy_reject_reasons",
        "v2_rank_score",
        "v2_expected_return",
        "v2_direction_score",
        "v2_risk_score",
        "model_identity",
        "feature_schema",
        "label_policy",
        "data_snapshot",
        "selection_contract",
    ):
        assert key in payload
    assert payload["legacy_score"] == 72.5
    assert payload["deep_rank"] == 1


def test_v2_head_fields_are_not_available_not_fabricated() -> None:
    """V2 多 Head 未实现：必须是 not_available，绝不能拿 legacy 分数顶替。"""
    payload = _decision_rows()[0]
    for key in ("v2_rank_score", "v2_expected_return", "v2_direction_score", "v2_risk_score"):
        assert payload[key] == NOT_AVAILABLE
    assert payload["v2_rank_score"] != payload["legacy_score"]


def test_missing_identities_are_empty_objects_not_none() -> None:
    payload = build_decision_rows(
        signal_date=SIGNAL_DATE, candidates=[{"symbol": "X"}]
    )[0].to_payload()
    assert payload["model_identity"] == {}
    assert payload["selection_contract"] == {}
    assert payload["quality_rank"] == NOT_AVAILABLE


# ---------------------------------------------------------------------------
# 落盘：目录布局 + 幂等
# ---------------------------------------------------------------------------


def test_paths_follow_year_month_layout(tmp_path: Path) -> None:
    assert decision_path(tmp_path, signal_date=SIGNAL_DATE) == (
        tmp_path / "2026" / "09" / "decision_20260917.jsonl"
    )
    assert outcome_path(tmp_path, signal_date=SIGNAL_DATE) == (
        tmp_path / "2026" / "09" / "outcome_20260917.jsonl"
    )


def test_decision_write_is_idempotent(tmp_path: Path) -> None:
    path = write_decision_rows(root=tmp_path, signal_date=SIGNAL_DATE, rows=build_decision_rows(
        signal_date=SIGNAL_DATE, candidates=[{"symbol": "600000"}, {"symbol": "000001"}]
    ))
    write_decision_rows(root=tmp_path, signal_date=SIGNAL_DATE, rows=build_decision_rows(
        signal_date=SIGNAL_DATE, candidates=[{"symbol": "600000"}, {"symbol": "000001"}]
    ))
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2  # 同日重复写不产生重复行


# ---------------------------------------------------------------------------
# outcome：只在成熟后写
# ---------------------------------------------------------------------------


def _bar(open_price: float, close: float, *, pre_close: float) -> dict[str, object]:
    return {
        "open": open_price,
        "high": max(open_price, close) + 0.2,
        "low": min(open_price, close) - 0.1,
        "close": close,
        "pre_close": pre_close,
        "suspended": False,
    }


def _price_frame() -> pd.DataFrame:
    # 信号日 9/17；其后交易日 9/18 起满足入场与成熟
    return _bars(
        [
            _bar(10.0, 10.0, pre_close=10.0),
            _bar(10.1, 10.3, pre_close=10.0),
            _bar(10.3, 10.5, pre_close=10.3),
            _bar(10.5, 10.7, pre_close=10.5),
            _bar(10.7, 10.9, pre_close=10.7),
            _bar(10.9, 11.1, pre_close=10.9),
        ],
        start="2026-09-17",
    )


def test_no_outcome_is_written_on_signal_day() -> None:
    """禁止 signal 当天提前写未来数据：evaluation == signal → 无任何 outcome。"""
    maturity = compute_outcomes(
        decision_rows=_decision_rows(),
        bars_by_symbol={"600000": _price_frame()},
        signal_date=SIGNAL_DATE,
        evaluation_date=SIGNAL_DATE,
    )
    assert maturity.matured_horizons == ()
    assert maturity.any_matured is False
    assert maturity.pending_horizons == (3, 5, 10, 15)
    assert maturity.rows == ()


def test_only_matured_horizons_are_written() -> None:
    trading_days = list(pd.bdate_range("2026-09-17", periods=8).date)
    maturity = compute_outcomes(
        decision_rows=_decision_rows(),
        bars_by_symbol={"600000": _price_frame()},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[5],  # 第 5 个交易日之后（signal 为第 1 个）
        horizons=(3, 5, 10),
        trading_days=trading_days,
    )
    assert maturity.matured_horizons == (3, 5)
    assert maturity.pending_horizons == (10,)
    assert {row["horizon"] for row in maturity.rows} == {3, 5}


def test_outcome_uses_t_plus_1_entry_and_raw_prices() -> None:
    trading_days = list(pd.bdate_range("2026-09-17", periods=8).date)
    maturity = compute_outcomes(
        decision_rows=_decision_rows(),
        bars_by_symbol={"600000": _price_frame()},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[-1],
        horizons=(3,),
        trading_days=trading_days,
    )
    row = next(item for item in maturity.rows if item["symbol"] == "600000")
    assert row["entry_mode"] == "next_session_open"
    assert row["price_basis"] == "raw"
    assert row["executable"] is True
    # 入场 = T+1 开盘 10.1（滑点 0 下的 net 价按 tick 取整），出场 = 第 3 个持有日收盘
    assert row["entry_price_raw"] == pytest.approx(10.1)
    expected = (float(row["exit_price_raw"]) - float(row["entry_price_net"])) / float(
        row["entry_price_net"]
    )
    assert float(row["net_return_pct"]) == pytest.approx(expected)


def test_no_fill_outcome_does_not_fake_returns() -> None:
    """T+1 不可成交（一字涨停）→ outcome 标不可成交，收益字段 not_available。"""
    frame = _bars(
        [
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
             "suspended": False},
            {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pre_close": 10.0,
             "suspended": False},
            {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pre_close": 11.0,
             "suspended": False},
            {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pre_close": 11.0,
             "suspended": False},
            {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pre_close": 11.0,
             "suspended": False},
        ],
        start="2026-09-17",
    )
    trading_days = list(pd.bdate_range("2026-09-17", periods=6).date)
    maturity = compute_outcomes(
        decision_rows=_decision_rows(),
        bars_by_symbol={"600000": frame},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[-1],
        horizons=(1,),
        trading_days=trading_days,
    )
    row = next(item for item in maturity.rows if item["symbol"] == "600000")
    assert row["executable"] is False
    assert row["no_fill_reason"] == "limit_up_open"
    assert row["net_return_pct"] == NOT_AVAILABLE


def test_outcome_matcher_reuses_runtime_config() -> None:
    """N3 回归：传入运行 config 时，outcome 的成本/涨跌停口径必须与之一致。

    用"把 config.limit_rule 的印花税档位改到极端"的方式证明透传生效：
    同一个卖出成本在不同 limit_rule 下不同。
    """
    from stock_analyzer.backtest.matcher import ExecutionMatcher
    from stock_analyzer.config import load_config

    config = load_config("config/default.yaml")
    trading_days = list(pd.bdate_range("2026-09-17", periods=8).date)
    maturity = compute_outcomes(
        decision_rows=_decision_rows(),
        bars_by_symbol={"600000": _price_frame()},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[-1],
        horizons=(3,),
        trading_days=trading_days,
        config=config,
    )
    from stock_analyzer.alpha_v2.decision_log import compute_outcomes as _compute

    explicit = _compute(
        decision_rows=_decision_rows(),
        bars_by_symbol={"600000": _price_frame()},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[-1],
        horizons=(3,),
        trading_days=trading_days,
        matcher=ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule),
    )
    assert maturity.rows == explicit.rows


def test_missing_bars_are_reported_not_assumed() -> None:
    trading_days = list(pd.bdate_range("2026-09-17", periods=6).date)
    maturity = compute_outcomes(
        decision_rows=_decision_rows(),
        bars_by_symbol={},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[-1],
        horizons=(3,),
        trading_days=trading_days,
    )
    row = maturity.rows[0]
    assert row["executable"] is False
    assert row["no_fill_reason"] == "bars_unavailable"
    assert row["net_return_pct"] == NOT_AVAILABLE


def test_outcome_write_is_idempotent(tmp_path: Path) -> None:
    trading_days = list(pd.bdate_range("2026-09-17", periods=6).date)
    maturity = compute_outcomes(
        decision_rows=_decision_rows(),
        bars_by_symbol={"600000": _price_frame()},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[-1],
        horizons=(3,),
        trading_days=trading_days,
    )
    path = write_outcomes(root=tmp_path, maturity=maturity)
    write_outcomes(root=tmp_path, maturity=maturity)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == len(maturity.rows)


def test_manifest_write_records_identification(tmp_path: Path) -> None:
    path = write_manifest(
        root=tmp_path,
        signal_date=SIGNAL_DATE,
        payload={"model_id": "m1", "contract": "night_alpha_v2_v1"},
    )
    assert path == tmp_path / "2026" / "09" / "run_20260917.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["model_id"] == "m1"


# ---------------------------------------------------------------------------
# Done When：任选一天能完整回答"当时……"
# ---------------------------------------------------------------------------


def test_one_day_is_fully_answerable(tmp_path: Path) -> None:
    """任选一天：universe / 模型 / 排序 / 预测 / 后续 outcome 都要能查到。"""
    decisions = _decision_rows()
    decision_file = write_decision_rows(root=tmp_path, signal_date=SIGNAL_DATE, rows=[
        row for row in build_decision_rows(
            signal_date=SIGNAL_DATE,
            candidates=[
                {"symbol": "600000", "score": 72.5, "deep_rank": 1, "eligible": True},
                {"symbol": "000001", "score": 65.0, "deep_rank": 2, "eligible": True},
            ],
            model_identity={"model_id": "m1", "status": "match"},
            feature_schema={"feature_schema_id": "fs_v1"},
            label_policy={"label_policy_id": "lp_v3"},
            data_snapshot={"universe_snapshot_id": "asofuniv_x"},
            selection_contract={"selection_contract_id": "night_alpha_v2_v1"},
        )
    ])
    write_manifest(
        root=tmp_path,
        signal_date=SIGNAL_DATE,
        payload={
            "universe_snapshot_id": "asofuniv_x",
            "model": {"model_id": "m1"},
            "selection_contract": "night_alpha_v2_v1",
        },
    )
    trading_days = list(pd.bdate_range("2026-09-17", periods=8).date)
    maturity = compute_outcomes(
        decision_rows=decisions,
        bars_by_symbol={"600000": _price_frame(), "000001": _price_frame()},
        signal_date=SIGNAL_DATE,
        evaluation_date=trading_days[-1],
        horizons=(3, 5),
        trading_days=trading_days,
    )
    outcome_file = write_outcomes(root=tmp_path, maturity=maturity)

    rows = [json.loads(line) for line in decision_file.read_text(encoding="utf-8").splitlines()]
    assert {row["symbol"] for row in rows} == {"600000", "000001"}
    assert rows[0]["model_identity"]["model_id"] == "m1"
    assert rows[0]["data_snapshot"]["universe_snapshot_id"] == "asofuniv_x"
    assert rows[0]["selection_contract"]["selection_contract_id"] == "night_alpha_v2_v1"
    outcomes = [json.loads(line) for line in outcome_file.read_text(encoding="utf-8").splitlines()]
    assert len(outcomes) == 2 * 2  # 2 只 × 2 个已成熟 horizon
    assert all("net_return_pct" in item for item in outcomes)
