"""P0 双价格序列契约专项测试（DP-1..DP-10）。

价格契约的两半：

```text
Feature Series may be QFQ
Execution Series must be RAW
```

本文件证明这条契约在 Alpha V2 的冻结 / 成熟 / KPI / preflight 四个环节上都是
**结构性**的（fail closed），而不是"配置里写着 raw"：

======================  ==========================================================
DP-1                    QFQ feature + RAW execution → freeze 放行
DP-2                    QFQ feature + QFQ execution → 在构造特征矩阵**之前**拒绝
DP-3                    RAW execution 未认证 → 拒绝（含"完全没有口径声明"的形态）
DP-4                    corporate-action 夹具：feature == qfq 期望、label == raw 期望
DP-5                    mature 用 qfq → 拒绝且 0 行 outcome 落盘（函数层 + CLI 层）
DP-6                    mature 用 raw → 接受，行上 price_mode=raw / certified=true
DP-7                    KPI：``price_mode_certified=false`` 的 outcome 不进成熟证据
DP-8                    feature 指纹变化 → preflight BLOCKED
DP-9                    execution 指纹变化 → preflight BLOCKED
DP-10                   execution 库陈旧 → preflight BLOCKED
======================  ==========================================================
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
from _alpha_v2_m3_fixtures import (
    M3_MODEL_BLOCK,
    capture_at,
    open_epoch_for_manifest,
    shadow_row_identity,
    write_freeze_manifest,
)
from _alpha_v2_research_helpers import DAYS, bar as _bar, matcher as _matcher, panel as _panel

from stock_analyzer.alpha_v2.dual_price_series import (
    DB_ROLE_BINDING_DUAL,
    DB_ROLE_BINDING_LEGACY,
    DEFECT_REASON_CROSS_PANEL_DIVERGENCE,
    DEFECT_REASON_DATE_NOT_SESSION,
    DEFECT_REASON_SYMBOL_ABSENT,
    FILTER_REASON_NO_EXECUTION_BAR,
    PriceSeriesContractError,
    assert_decisions_aligned,
    certification_from_declaration,
    filter_decisions_by_execution_availability,
    price_series_identity_block,
    require_certified_execution_series,
    require_declared_feature_series,
    resolve_market_dbs,
)
from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint, compute_outcomes
from stock_analyzer.alpha_v2.research.panel import load_daily_panel
from stock_analyzer.alpha_v2.validation import dual_price_freeze as dpf
from stock_analyzer.alpha_v2.validation import preflight as pf
from stock_analyzer.alpha_v2.validation.outcome_maturation import (
    OutcomeMaturationError,
    mature_epoch_outcomes,
    outcome_path,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    build_shadow_rows,
    write_shadow_snapshot,
)
from stock_analyzer.alpha_v2.validation.training_data_fingerprint import (
    compute_training_data_fingerprint,
)
from stock_analyzer.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ["600000", "600001", "600002", "600003"]
# 决策日落在"除权日"当天：raw 的当日涨跌幅被除权跳变打穿，qfq 不受影响。
DECISION_INDEX = 20
# 第二个除权日落在**持有窗口内**（entry=T+1、3D 退出=T+3）：raw 的 3D 净收益被
# 除权跳变打穿，qfq 不受影响。两个夹具合起来才同时证明"特征取 qfq、label 取 raw"。
ACTION_INSIDE_HOLDING_INDEX = 22


# ---------------------------------------------------------------------------
# 夹具：两个口径真的不一样的行情库 / 面板
# ---------------------------------------------------------------------------


def _series(symbols: list[str], days: list[date], *, mode: str) -> list[dict[str, object]]:
    """造一段行情：``mode='raw'`` 含两次 2:1 除权跳变，``mode='qfq'`` 是平滑序列。

    两段序列的**比值**（即任何人能算出来的收益）在跳变日附近明显不同——这正是
    "特征取 qfq、label 取 raw"必须可观测的前提。
    """
    rows: list[dict[str, object]] = []
    step_days = {DECISION_INDEX, ACTION_INSIDE_HOLDING_INDEX}
    for index, symbol in enumerate(symbols):
        close = 20.0 + index * 2.0
        prev = close
        for day_index, day in enumerate(days):
            if mode == "raw" and day_index in step_days:
                close = round(close * 0.5, 2)  # 2:1 除权：价格腰斩
            open_ = prev
            prev = close
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "open": open_,
                    "high": round(max(open_, close) * 1.002, 2),
                    "low": round(min(open_, close) * 0.998, 2),
                    "close": close,
                    "volume": 1_000_000.0,
                    "turnover": 1_000_000.0 * close,
                    "float_market_cap": 5.0e9,
                    "board": "main",
                    "is_st": False,
                    "is_delisting_risk": False,
                    "suspended": False,
                    "pre_close": open_,
                    "up_limit": round(open_ * 1.1, 2),
                    "down_limit": round(open_ * 0.9, 2),
                    "price_series_mode": mode,
                }
            )
    return rows


def _write_db(
    path: Path, *, mode: str | None, days: list[date], symbols: list[str] | None = None
) -> Path:
    """写一个 daily_bars 库；``mode=None`` = **不写口径列**（口径只能靠探针判）。"""
    rows = _series(symbols or SYMBOLS, days, mode=mode or "raw")
    frame = pd.DataFrame(rows)
    if mode is None:
        frame = frame.drop(columns=["price_series_mode"])
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.register("bars_frame", frame)
        connection.execute("CREATE OR REPLACE TABLE daily_bars AS SELECT * FROM bars_frame")
    finally:
        connection.close()
    return path


def _load_panel(db: Path, *, start: date, end: date, warmup: int = 0, source: str = ""):
    return load_daily_panel(
        market_db=db,
        window_start=start,
        window_end=end,
        warmup_days=warmup,
        source=source or str(db),
    )


@pytest.fixture
def dual_dbs(tmp_path):
    """qfq（feature）与 raw（execution）两份库，覆盖整个窗口。"""
    days = list(DAYS[:35])
    qfq = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=days)
    raw = _write_db(tmp_path / "execution_raw.duckdb", mode="raw", days=days)
    return {"days": days, "qfq": qfq, "raw": raw}


@pytest.fixture
def dual_panels(dual_dbs):
    days = dual_dbs["days"]
    feature_panel = _load_panel(dual_dbs["qfq"], start=days[0], end=days[-1], warmup=5)
    execution_panel = _load_panel(dual_dbs["raw"], start=days[0], end=days[-1], warmup=5)
    return {
        "feature": feature_panel,
        "execution": execution_panel,
        "feature_cert": feature_panel.certify_price_mode(min_sample=1),
        "execution_cert": execution_panel.certify_price_mode(min_sample=1),
    }


def _decisions(panel, day: date) -> list[DecisionPoint]:
    return [DecisionPoint(symbol, day) for symbol in panel.symbols]


def _build(panels, *, day: date, execution_cert=None, slippage: float = 0.0):
    return dpf.build_dual_price_training_frame(
        feature_panel=panels["feature"],
        execution_panel=panels["execution"],
        decisions=_decisions(panels["feature"], day),
        matcher=_matcher(),
        slippage_ratio=slippage,
        execution_certification=execution_cert or panels["execution_cert"],
        feature_certification=panels["feature_cert"],
        context="dp_test",
    )


# ---------------------------------------------------------------------------
# DP-1 / DP-2 / DP-3：freeze 侧硬门
# ---------------------------------------------------------------------------


def test_dp1_qfq_feature_with_raw_execution_is_allowed(dual_panels, dual_dbs):
    """DP-1：QFQ feature + RAW execution → freeze 放行，且证据两条分开。"""
    built = _build(dual_panels, day=dual_dbs["days"][DECISION_INDEX])
    assert built.evidence["execution_price_mode"] == "raw"
    assert built.evidence["execution_price_mode_certified"] is True
    assert built.evidence["feature_panel_source"] == str(dual_dbs["qfq"])
    assert built.evidence["execution_panel_source"] == str(dual_dbs["raw"])
    assert built.evidence["decision_alignment"]["aligned"] is True
    assert not built.frame.empty
    # 每一条 outcome 行都自述 raw + certified（KPI 第二道闸的输入）
    assert set(built.execution_run.frame["price_mode"].astype(str)) == {"raw"}
    assert bool(built.execution_run.frame["price_mode_certified"].all())


def test_dp2_qfq_execution_is_rejected_before_feature_construction(
    dual_panels, dual_dbs, monkeypatch
):
    """DP-2：execution 是 qfq → 在构造完整特征矩阵**之前**就拒绝。

    这是"不允许跑 90 分钟才报错"的可执行形式：把特征构造换成会爆炸的探针，
    如果它被调到，测试就会以 AssertionError 而不是契约错误失败。
    """
    calls: list[object] = []

    def _explode(*args, **kwargs):  # pragma: no cover - 被调用即测试失败
        calls.append((args, kwargs))
        raise AssertionError("特征矩阵在 execution 口径硬门之前就被构造了")

    monkeypatch.setattr(dpf, "daily_feature_frame", _explode)
    qfq_panel = _load_panel(
        dual_dbs["qfq"], start=dual_dbs["days"][0], end=dual_dbs["days"][-1], warmup=5
    )
    with pytest.raises(PriceSeriesContractError):
        dpf.build_dual_price_training_frame(
            feature_panel=dual_panels["feature"],
            execution_panel=qfq_panel,
            decisions=_decisions(dual_panels["feature"], dual_dbs["days"][DECISION_INDEX]),
            matcher=_matcher(),
            slippage_ratio=0.0,
            execution_certification=qfq_panel.certify_price_mode(min_sample=1),
            context="dp_test",
        )
    assert calls == []


def test_dp3_uncertified_raw_execution_is_rejected(dual_panels, dual_dbs):
    """DP-3：execution 声称 raw 但未认证 → 拒绝；完全没有口径声明同样拒绝。"""
    with pytest.raises(PriceSeriesContractError):
        _build(
            dual_panels,
            day=dual_dbs["days"][DECISION_INDEX],
            execution_cert=certification_from_declaration(price_mode="raw", certified=False),
        )
    # 真实形态：库没有口径列、且价格序列违反涨跌停一致性 → 探针给 unknown/False
    bare = _write_db(
        dual_dbs["raw"].with_name("execution_bare.duckdb"),
        mode=None,
        days=dual_dbs["days"],
    )
    bare_panel = _load_panel(bare, start=dual_dbs["days"][0], end=dual_dbs["days"][-1], warmup=5)
    cert = bare_panel.certify_price_mode(min_sample=1)
    assert cert.certified is False
    with pytest.raises(PriceSeriesContractError):
        _build(dual_panels, day=dual_dbs["days"][DECISION_INDEX], execution_cert=cert)


# ---------------------------------------------------------------------------
# DP-4：特征取 qfq、label 取 raw
# ---------------------------------------------------------------------------


def test_dp4_feature_from_qfq_label_from_raw(dual_panels, dual_dbs):
    """DP-4：除权跳变让两个口径明显不同——特征等于 qfq 期望、label 等于 raw 期望。"""
    days = dual_dbs["days"]
    decision = days[DECISION_INDEX]
    built = _build(dual_panels, day=decision)
    frame = built.frame.set_index("symbol")

    feature_bars = dual_panels["feature"].symbol_bars("600000")
    execution_bars = dual_panels["execution"].symbol_bars("600000")

    # ① 特征 = qfq 期望（ret_1d 就是 close.pct_change）
    qfq_closes = feature_bars["close"].to_numpy(dtype=float)
    raw_closes = execution_bars["close"].to_numpy(dtype=float)
    qfq_ret = qfq_closes[DECISION_INDEX] / qfq_closes[DECISION_INDEX - 1] - 1.0
    raw_ret = raw_closes[DECISION_INDEX] / raw_closes[DECISION_INDEX - 1] - 1.0
    assert raw_ret < -0.4 and qfq_ret > -0.05, "夹具必须让两个口径的当日收益明显不同"
    assert float(frame.loc["600000", "ret_1d"]) == pytest.approx(qfq_ret, abs=1e-6)
    assert float(frame.loc["600000", "ret_1d"]) != pytest.approx(raw_ret, abs=1e-3)

    # ② label = raw 期望（入场 T+1 开盘、3D 退出 = 入场后第 2 个交易日收盘）
    entry_open = float(execution_bars["open"].iloc[DECISION_INDEX + 1])
    exit_close = float(execution_bars["close"].iloc[DECISION_INDEX + 3])
    row = built.execution_run.frame.set_index("symbol").loc["600000"]
    expected_raw_net = exit_close / entry_open - 1.0 - float(row["round_trip_cost_rate"])
    assert float(row["net_return_3d"]) == pytest.approx(expected_raw_net, abs=1e-9)
    assert float(row["entry_price_raw"]) == pytest.approx(entry_open, abs=1e-9)
    # 持有窗口内的除权跳变必须真的体现出来（raw 口径下是显著亏损）
    assert float(row["net_return_3d"]) < -0.3

    # ③ 同一决策在 qfq 面板上算出来的 3D 收益必须**不同**（否则本用例证明不了什么）
    qfq_run = compute_outcomes(
        panel=dual_panels["feature"],
        decisions=[DecisionPoint("600000", decision)],
        matcher=_matcher(),
        slippage_ratio=0.0,
        price_mode="qfq",
        price_mode_certified=False,
    )
    qfq_net = float(qfq_run.frame.iloc[0]["net_return_3d"])
    # qfq 序列在窗口内是平滑的 → 净收益只剩往返成本；raw 口径下则是显著亏损。
    assert qfq_net > -0.01
    assert abs(qfq_net - float(row["net_return_3d"])) > 0.3


# ---------------------------------------------------------------------------
# DP-5 / DP-6：mature 侧硬门
# ---------------------------------------------------------------------------


def _epoch_with_shadow(
    tmp_path,
    *,
    config,
    day: date,
    candidates: list[dict[str, object]] | None = None,
    model: dict[str, object] | None = None,
) -> tuple[object, list[date]]:
    """开一个 epoch 并写一天的 shadow 快照（成熟链路的真实输入）。"""
    from stock_analyzer.alpha_v2.validation.runtime_identity import (
        config_hash_of,
        git_head,
    )

    manifest = write_freeze_manifest(
        tmp_path,
        validation_mode="test",
        validation_start_date=day.isoformat(),
        code_commit=git_head(REPO_ROOT),
        config_hash=config_hash_of(config),
        execution_price_mode="raw",
        **({"model": model} if model is not None else {}),
    )
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date=day.isoformat())
    rows = build_shadow_rows(
        signal_date=day,
        signal_time="15:35",
        epoch=epoch,
        candidates=candidates
        if candidates is not None
        else [
            {"symbol": symbol, "in_quality_pool": True, "in_deep_pool": True, "v2_top5": True}
            for symbol in SYMBOLS
        ],
        identity=shadow_row_identity(epoch),
        recorded_at=f"{day.isoformat()}T15:35:00+08:00",
    )
    with capture_at(day):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)
    return epoch, list(DAYS)


def test_dp5_mature_with_qfq_writes_zero_outcomes(tmp_path, monkeypatch):
    """DP-5：mature 用 qfq → 拒绝，且**一行 outcome 都不写**（函数层 + CLI 层）。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    signal_day = DAYS[5]
    epoch, days = _epoch_with_shadow(tmp_path, config=config, day=signal_day)
    days = list(DAYS[:35])

    qfq_db = _write_db(tmp_path / "m_qfq.duckdb", mode="qfq", days=days)
    qfq_panel = _load_panel(qfq_db, start=days[0], end=days[30], warmup=5)
    cert = qfq_panel.certify_price_mode(min_sample=1)
    assert cert.mode == "qfq" and cert.certified is False

    with pytest.raises(PriceSeriesContractError):
        mature_epoch_outcomes(
            root=tmp_path,
            epoch=epoch,
            panel=qfq_panel,
            style_panel=qfq_panel,
            evaluation_date=days[30],
            matcher=_matcher(),
            slippage_ratio=0.0015,
            price_mode=cert.mode,
            price_mode_certified=cert.certified,
        )
    assert not outcome_path(tmp_path, epoch.epoch_id, signal_day).exists()

    # CLI 层：真实入口同样必须 exit 4 且不落任何 outcome
    completed = subprocess.run(  # noqa: S603 - 固定脚本 + 列表参数
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "alpha_v2_shadow_mature.py"),
            "--epoch-id",
            epoch.epoch_id,
            "--evaluation-date",
            days[30].isoformat(),
            "--rehearsal",
            "--market-db",
            str(qfq_db),
            "--out",
            str(tmp_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 4, (completed.returncode, completed.stderr[-800:])
    assert "execution 价格序列必须是 raw" in completed.stderr
    assert not outcome_path(tmp_path, epoch.epoch_id, signal_day).exists()


def test_dp6_mature_with_raw_writes_certified_outcomes(tmp_path, monkeypatch):
    """DP-6：mature 用 raw → 接受，行上 ``price_mode=raw`` / ``certified=true``。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    signal_day = DAYS[5]
    epoch, _ = _epoch_with_shadow(tmp_path, config=config, day=signal_day)
    days = list(DAYS[:35])

    raw_db = _write_db(tmp_path / "m_raw.duckdb", mode="raw", days=days)
    raw_panel = _load_panel(raw_db, start=days[0], end=days[30], warmup=5)
    cert = raw_panel.certify_price_mode(min_sample=1)
    assert cert.mode == "raw" and cert.certified is True

    summary = mature_epoch_outcomes(
        root=tmp_path,
        epoch=epoch,
        panel=raw_panel,
        style_panel=raw_panel,
        evaluation_date=days[30],
        matcher=_matcher(),
        slippage_ratio=0.0015,
        price_mode=cert.mode,
        price_mode_certified=cert.certified,
        execution_data_identity=price_series_identity_block(
            role="execution",
            db=str(raw_db),
            certification=cert,
            context="dp6:execution",
        ),
    )
    assert summary["rows_written"] == len(SYMBOLS)
    assert summary["price_mode"] == "raw"
    assert summary["execution_data_identity"]["price_series_mode"] == "raw"
    rows = [
        json.loads(line)
        for line in outcome_path(tmp_path, epoch.epoch_id, signal_day)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(rows) == len(SYMBOLS)
    for row in rows:
        assert row["price_mode"] == "raw"
        assert row["price_mode_certified"] is True
        assert row["execution_uncertain"] is False


# ---------------------------------------------------------------------------
# DP-7：KPI 第二道闸
# ---------------------------------------------------------------------------


def _kpi_epoch(tmp_path, *, config, day: date, certified: bool):
    """一天 epoch + shadow + 一行手工 outcome（可控制认证标记）。"""
    from stock_analyzer.alpha_v2.validation.validation_kpis import build_validation_kpi

    epoch, _ = _epoch_with_shadow(
        tmp_path,
        config=config,
        day=day,
        candidates=[
            {
                "symbol": "600000",
                "in_quality_pool": True,
                "in_deep_pool": True,
                "v2_top5": True,
                # clean 日要求 data_health 同日且 ok（夹具与生产同形）
                "data_health": {
                    "schema": "alpha_v2_capture_data_health.v1",
                    "status": "ok",
                    "source_status": "ok",
                    "as_of": day.isoformat(),
                    "generated_at": f"{day.isoformat()}T20:30:00+08:00",
                    "coverage": 0.99,
                    "source": "dp_test_fixture",
                    "aligned_to_signal_date": True,
                    "detail": "同日工件可用",
                },
            }
        ],
    )
    path = outcome_path(tmp_path, epoch.epoch_id, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "signal_date": day.isoformat(),
                "symbol": "600000",
                "executable": True,
                "price_mode": "raw",
                "price_mode_certified": certified,
                "matured_5d": True,
                "net_return_5d": 0.05,
                "excess_return_5d": 0.02,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return epoch, build_validation_kpi(root=tmp_path, epoch=epoch)


@pytest.mark.parametrize("certified", [True, False])
def test_dp7_kpi_rejects_non_certified_outcomes(tmp_path, monkeypatch, certified):
    """DP-7：``price_mode_certified=false`` 的 outcome 不进成熟证据（逐行判）。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    day = DAYS[5]
    _epoch, report = _kpi_epoch(tmp_path, config=config, day=day, certified=certified)
    evidence = report["price_series"]["all_captured"]
    if certified:
        assert report["clean_maturity"]["mature_dates_5d"] == 1
        assert report["governance"]["outcome_price_series_invalid_days"] == 0
        assert evidence["matured_non_certified_rows"] == 0
        assert evidence["certified_raw_rows"] == 1
    else:
        assert report["clean_maturity"]["mature_dates_5d"] == 0
        assert report["governance"]["outcome_price_series_invalid_days"] == 1
        assert evidence["matured_non_certified_rows"] == 1
        assert evidence["certified_raw_rows"] == 0
        reasons = " ".join(
            str(item) for item in report["governance"]["by_date"][0]["reasons"]
        )
        assert "outcome_price_mode_not_certified_raw" in reasons
        # 证据块本身也必须是空的（不能"治理层排除了、数字却还在"）
        assert report["returns"]["top1"]["net_5d"]["rows"] == 0


# ---------------------------------------------------------------------------
# DP-8 / DP-9 / DP-10：preflight 双数据源
# ---------------------------------------------------------------------------


def _model_identity_with_dual_identity(
    *, feature_db: Path, execution_db: Path, warmup_days: int
) -> dict[str, object]:
    """按真实指纹造一份 preflight 认识的 model_identity（两条身份齐备）。"""
    feature_fp = compute_training_data_fingerprint(
        feature_db,
        training_start=DAYS[0],
        training_end=DAYS[34],
        warmup_days=warmup_days,
    )
    execution_fp = compute_training_data_fingerprint(
        execution_db,
        training_start=DAYS[0],
        training_end=DAYS[34],
        warmup_days=warmup_days,
    )
    return {
        "model_id": "dp_model",
        "artifact_hash": "dp-hash",
        "artifact_hash_version": "v3",
        "feature_schema_hash": "dp-schema",
        "model_training_code_commit": "a" * 40,
        "provenance_warmup_days": warmup_days,
        "training_data_fingerprint": feature_fp["fingerprint"],
        "training_data_fingerprint_version": feature_fp["fingerprint_version"],
        "training_data_rows": feature_fp["rows"],
        "training_data_columns": list(feature_fp["columns"]),
        "provenance_source_window": list(feature_fp["source_window"]),
        "feature_data_identity": price_series_identity_block(
            role="feature",
            db=str(feature_db),
            certification=certification_from_declaration(price_mode="qfq", certified=False),
            fingerprint=feature_fp,
            context="dp_test:feature",
        ),
        "execution_data_identity": price_series_identity_block(
            role="execution",
            db=str(execution_db),
            certification=certification_from_declaration(price_mode="raw", certified=True),
            fingerprint=execution_fp,
            context="dp_test:execution",
        ),
        "validation_mode": "production",
        "feature_price_mode": "qfq",
        "execution_price_mode": "raw",
    }


def test_dp8_feature_fingerprint_change_is_blocked(tmp_path):
    """DP-8：feature 库内容被改写 → 指纹对不上 → BLOCKED。"""
    days = list(DAYS[:35])
    feature_db = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=days)
    execution_db = _write_db(tmp_path / "execution_raw.duckdb", mode="raw", days=days)
    identity = _model_identity_with_dual_identity(
        feature_db=feature_db, execution_db=execution_db, warmup_days=5
    )
    ok = pf.check_training_data_fingerprint(
        market_db=feature_db,
        model_identity=identity,
        training_start=days[0],
        training_end=days[-1],
    )
    assert ok.verdict == pf.VERDICT_PASS, ok.findings
    with duckdb.connect(str(feature_db)) as connection:
        connection.execute(
            "UPDATE daily_bars SET close = close * 1.01 WHERE symbol = '600000' AND date = ?",
            [days[3]],
        )
    blocked = pf.check_training_data_fingerprint(
        market_db=feature_db,
        model_identity=identity,
        training_start=days[0],
        training_end=days[-1],
    )
    assert blocked.verdict == pf.VERDICT_BLOCKED
    assert any("feature" in item for item in blocked.findings), blocked.findings


def test_dp9_execution_fingerprint_change_is_blocked(tmp_path):
    """DP-9：execution 库内容被改写 → 指纹对不上 → BLOCKED。"""
    days = list(DAYS[:35])
    feature_db = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=days)
    execution_db = _write_db(tmp_path / "execution_raw.duckdb", mode="raw", days=days)
    identity = _model_identity_with_dual_identity(
        feature_db=feature_db, execution_db=execution_db, warmup_days=5
    )
    ok = pf.check_execution_data_fingerprint(
        execution_market_db=execution_db,
        model_identity=identity,
        training_start=days[0],
        training_end=days[-1],
    )
    assert ok.verdict == pf.VERDICT_PASS, ok.findings
    with duckdb.connect(str(execution_db)) as connection:
        connection.execute(
            "UPDATE daily_bars SET close = close * 1.01 WHERE symbol = '600000' AND date = ?",
            [days[3]],
        )
    blocked = pf.check_execution_data_fingerprint(
        execution_market_db=execution_db,
        model_identity=identity,
        training_start=days[0],
        training_end=days[-1],
    )
    assert blocked.verdict == pf.VERDICT_BLOCKED
    assert any("execution.fingerprint" in item for item in blocked.findings), blocked.findings


def test_dp10_stale_execution_db_is_blocked(tmp_path):
    """DP-10：execution 库比 feature 库落后（raw 链断供）→ BLOCKED。"""
    days = list(DAYS[:35])
    feature_db = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=days)
    fresh_execution = _write_db(tmp_path / "execution_raw.duckdb", mode="raw", days=days)
    stale_execution = _write_db(
        tmp_path / "execution_raw_stale.duckdb", mode="raw", days=days[:10]
    )
    identity = _model_identity_with_dual_identity(
        feature_db=feature_db, execution_db=fresh_execution, warmup_days=5
    )
    fresh = pf.check_execution_price_series(
        execution_market_db=fresh_execution,
        feature_market_db=feature_db,
        model_identity=identity,
        training_end=days[-1],
    )
    assert fresh.verdict == pf.VERDICT_PASS, fresh.findings
    stale = pf.check_execution_price_series(
        execution_market_db=stale_execution,
        feature_market_db=feature_db,
        model_identity=identity,
        training_end=days[-1],
    )
    assert stale.verdict == pf.VERDICT_BLOCKED
    findings = " ".join(stale.findings)
    assert "execution_market_db_stale" in findings
    assert "execution_market_db_does_not_cover_training_window" in findings


def test_dp_extra_execution_db_must_not_reuse_feature_db(tmp_path):
    """补充：把 qfq 的 feature 库直接当 execution 库（P0 的原始形态）→ BLOCKED。"""
    days = list(DAYS[:35])
    qfq_db = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=days)
    identity = _model_identity_with_dual_identity(
        feature_db=qfq_db, execution_db=qfq_db, warmup_days=5
    )
    result = pf.check_execution_price_series(
        execution_market_db=qfq_db,
        feature_market_db=qfq_db,
        model_identity=identity,
        training_end=days[-1],
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "execution_market_db_equals_feature_market_db" in " ".join(result.findings)


# ---------------------------------------------------------------------------
# 补充：守卫语义 / 库解析 / 生产入口不留后门
# ---------------------------------------------------------------------------


def test_require_helpers_reject_and_accept_expected_forms():
    with pytest.raises(PriceSeriesContractError):
        require_certified_execution_series(
            certification_from_declaration(price_mode="qfq", certified=False),
            context="unit",
        )
    require_certified_execution_series(
        certification_from_declaration(price_mode="raw", certified=True), context="unit"
    )
    with pytest.raises(PriceSeriesContractError):
        require_declared_feature_series(
            certification_from_declaration(price_mode="unknown", certified=False),
            context="unit",
        )
    with pytest.raises(PriceSeriesContractError):
        require_declared_feature_series(
            certification_from_declaration(price_mode="raw", certified=True),
            context="unit",
            expected_mode="qfq",
        )
    require_declared_feature_series(
        certification_from_declaration(price_mode="qfq", certified=False), context="unit"
    )


def test_resolve_market_dbs_keeps_two_roles_separate():
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    dual = resolve_market_dbs(
        config,
        feature_db="qfq.duckdb",
        execution_db="raw.duckdb",
    )
    assert dual.db_role_binding == DB_ROLE_BINDING_DUAL
    assert dual.feature_db == "qfq.duckdb" and dual.execution_db == "raw.duckdb"
    legacy = resolve_market_dbs(config, legacy_market_db="one.duckdb")
    assert legacy.db_role_binding == DB_ROLE_BINDING_LEGACY
    assert legacy.feature_db == legacy.execution_db == "one.duckdb"
    unset = resolve_market_dbs(config)
    assert unset.execution_db == ""
    assert unset.execution_source == "unset"
    assert unset.dual_source is True


def test_production_entrypoints_have_no_escape_hatch():
    """生产两个入口不得出现任何"关掉 execution 价格守卫"的写法（结构守卫）。"""
    for script in ("alpha_v2_shadow_model_freeze.py", "alpha_v2_shadow_mature.py"):
        text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "enforce_execution_price_series=False" not in text
        assert "research_replay_reason" not in text
        assert "require_certified_execution_series" in text


def test_preflight_report_binds_both_identities_to_the_model(tmp_path, monkeypatch):
    """端到端对账：``run_production_preflight`` 产出的两条身份必须能过 freeze 的 gate。

    这条用例防的是一类真实缺陷：报告里某条身份少了字段（例如 ``columns`` 为空），
    gate 逐项对账就会拿冻结模型当"检查对象"却永远过不去——而单测夹具手写身份时
    看不出来。所以这里用**生产函数**产出报告，再交给 gate。
    """
    from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        fit_frozen_model,
        frozen_model_identity_payload,
        persist_frozen_model,
    )

    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    days = list(DAYS[:35])
    feature_db = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=days)
    execution_db = _write_db(tmp_path / "execution_raw.duckdb", mode="raw", days=days)

    rng = np.random.default_rng(3)
    total = 120
    frame = pd.DataFrame(
        {
            "decision_date": [days[i % 30].isoformat() for i in range(total)],
            "symbol": [f"6000{i % 4:02d}" for i in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in ("ret_1d", "ma5")},
        }
    )
    for horizon in (3, 5, 10, 15):
        frame[f"net_return_{horizon}d"] = rng.normal(0.0, 0.02, total)
        frame[f"excess_return_{horizon}d"] = frame[f"net_return_{horizon}d"] - 0.001
        frame[f"up_net_{horizon}d"] = (frame[f"net_return_{horizon}d"] > 0).astype(float)
        frame[f"up_excess_{horizon}d"] = (frame[f"excess_return_{horizon}d"] > 0).astype(float)
    frame["alpha_target_5d"] = frame.groupby("decision_date")["excess_return_5d"].rank(pct=True)
    frame["is_train"] = [i < 90 for i in range(total)]
    frame["is_calibration"] = [i >= 90 for i in range(total)]

    feature_fp = compute_training_data_fingerprint(
        feature_db, training_start=days[0], training_end=days[-1], warmup_days=5
    )
    execution_fp = compute_training_data_fingerprint(
        execution_db, training_start=days[0], training_end=days[-1], warmup_days=5
    )
    model = fit_frozen_model(
        frame=frame,
        model_id="dp_e2e",
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        provenance={
            "validation_mode": "production",
            "feature_price_mode": "qfq",
            "execution_price_mode": "raw",
            "window": [days[0].isoformat(), days[-1].isoformat()],
            "warmup_days": 5,
            "source_window": list(feature_fp["source_window"]),
            "training_data_fingerprint": feature_fp["fingerprint"],
            "training_data_fingerprint_version": feature_fp["fingerprint_version"],
            "training_data_rows": feature_fp["rows"],
            "training_data_columns": list(feature_fp["columns"]),
            "feature_data_identity": price_series_identity_block(
                role="feature",
                db=str(feature_db),
                certification=certification_from_declaration(price_mode="qfq", certified=False),
                fingerprint=feature_fp,
                context="dp_e2e:feature",
            ),
            "execution_data_identity": price_series_identity_block(
                role="execution",
                db=str(execution_db),
                certification=certification_from_declaration(price_mode="raw", certified=True),
                fingerprint=execution_fp,
                context="dp_e2e:execution",
            ),
        },
        extra_identity={"code_commit": "a" * 40, "config_hash": "cfg"},
    )
    model_dir = persist_frozen_model(model, tmp_path / "artifacts")
    model_block = dict(frozen_model_identity_payload(model_dir))
    config = load_config(REPO_ROOT / "config" / "default.yaml")

    payload = pf.run_production_preflight(
        config=config,
        repo_root=REPO_ROOT,
        market_db=feature_db,
        execution_market_db=execution_db,
        training_start=days[0],
        training_end=days[-1],
        feature_columns=["ret_1d", "ma5"],
        model_dir=model_dir,
        feature_probe_skipped_reason="dp_e2e",
        max_feature_probe_symbols=50,
    )
    # 本用例只验"两条身份能否绑到模型"：把与数据无关的检查（安全开关 / 生产前置 /
    # 运行身份）在负载上标成通过——那些各自有专门的门与用例。
    identity_checks = {
        "feature_price_series",
        "execution_price_series",
        "training_data_fingerprint",
        "execution_data_fingerprint",
        "model_identity",
    }
    assert all(
        item["verdict"] == pf.VERDICT_PASS
        for item in payload["checks"]
        if item["name"] in identity_checks
    ), [item for item in payload["checks"] if item["name"] in identity_checks]
    report = dict(payload)
    report["verdict"] = pf.VERDICT_WARN
    report["blocking_findings"] = []
    report["warnings"] = ["dp_e2e: unrelated checks neutralised"]
    report["runtime_identity"] = {"code_commit": "a" * 40}
    report["preflight_hash"] = pf.preflight_hash_of(report)
    path = tmp_path / "preflight_dual.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    block = pf.assert_preflight_gate(
        report_path=path,
        runtime_code_commit="a" * 40,
        model_block=model_block,
        max_age_hours=48.0,
        accept_warn=True,
    )
    assert block["execution_data_identity"]["price_series_mode"] == "raw"
    assert block["execution_data_identity"]["price_series_certified"] is True
    assert block["execution_data_identity"]["fingerprint"] == execution_fp["fingerprint"]
    assert block["feature_data_identity"]["price_series_mode"] == "qfq"

# ---------------------------------------------------------------------------
# LIVE-F1..F3：capture 侧的每日 feature 口径门（P0 Final R1 / BLOCKER 1）
# ---------------------------------------------------------------------------


def _weekdays(count: int, *, end: date = DAYS[19]) -> list[date]:
    """从 ``end`` 往前取 ``count`` 个工作日（升序）——PIT 池需要 ≥60 根历史 bar。"""
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


#: capture 侧用的长历史日历（≥60 交易日历史才可能选出 PIT 合格票）
LIVE_CALENDAR: list[date] = _weekdays(160)
LIVE_SIGNAL_DAY: date = LIVE_CALENDAR[-1]


@functools.lru_cache(maxsize=4)
def _script_module(name: str):
    """按文件路径加载 ``scripts/<name>.py``（便于 monkeypatch 其模块级符号）。"""
    import importlib.util

    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_live_model(root: Path, *, feature_mode: str, db_label: str):
    """造一份**真实可加载**的冻结模型工件，provenance 声明冻结 feature 口径。"""
    from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        fit_frozen_model,
        frozen_model_identity_payload,
        persist_frozen_model,
    )
    from stock_analyzer.alpha_v2.validation.runtime_identity import git_head

    rng = np.random.default_rng(23)
    total = 60
    frame = pd.DataFrame(
        {
            "decision_date": [DAYS[i % 20].isoformat() for i in range(total)],
            "symbol": [f"6000{i % 4:02d}" for i in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in ("ret_1d", "ma5")},
        }
    )
    frame["net_return_5d"] = rng.normal(0.0, 0.02, total)
    frame["excess_return_5d"] = frame["net_return_5d"] - 0.001
    # alpha_target_5d 必须有：否则没有 alpha booster，predict 不会产出 alpha_rank_score，
    # 而 cohort 构造（deep50_position_records）依赖它。
    frame["alpha_target_5d"] = frame.groupby("decision_date")["excess_return_5d"].rank(pct=True)
    frame["is_train"] = [i < 40 for i in range(total)]
    frame["is_calibration"] = [i >= 40 for i in range(total)]
    provenance: dict[str, object] = {"window": [DAYS[0].isoformat(), DAYS[19].isoformat()]}
    if feature_mode:
        provenance["feature_data_identity"] = {
            "role": "feature",
            "db": db_label,
            "price_series_mode": feature_mode,
        }
    model = fit_frozen_model(
        frame=frame,
        model_id="live_feature_model",
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        provenance=provenance,
        extra_identity={"code_commit": git_head(REPO_ROOT)},
    )
    model_dir = persist_frozen_model(model, root)
    return model_dir, dict(frozen_model_identity_payload(model_dir))


def _capture_epoch(tmp_path: Path, *, day: date, feature_db: Path, feature_mode: str):
    """test-mode epoch + 模型工件 + 与 epoch 锚定一致的冻结清单。"""
    from stock_analyzer.alpha_v2.validation.runtime_identity import (
        config_hash_of,
        git_head,
    )

    model_dir, model_block = _write_live_model(
        tmp_path, feature_mode=feature_mode, db_label=str(feature_db)
    )
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    manifest = write_freeze_manifest(
        tmp_path,
        validation_mode="test",
        validation_start_date=day.isoformat(),
        code_commit=git_head(REPO_ROOT),
        config_hash=config_hash_of(config),
        execution_price_mode="raw",
        model=model_block,
    )
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date=day.isoformat())
    return epoch, model_dir


def _capture_argv(*, tmp_path: Path, epoch, model_dir: Path, day: date, market_db: Path):
    return [
        "--epoch-id",
        epoch.epoch_id,
        "--signal-date",
        day.isoformat(),
        "--capture-date",
        day.isoformat(),
        "--market-db",
        str(market_db),
        "--model-dir",
        str(model_dir),
        "--out",
        str(tmp_path),
        "--warmup-days",
        "120",
        "--cohort-source",
        "research_proxy",
    ]


def _capture_day_manifest(tmp_path: Path, epoch_id: str, day: date) -> dict[str, object]:
    path = (
        tmp_path
        / "validation"
        / epoch_id
        / "manifests"
        / f"shadow_day_{day.strftime('%Y%m%d')}.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_live_f1_frozen_qfq_with_qfq_feature_db_passes(tmp_path, monkeypatch):
    """LIVE-F1：冻结 qfq + 当天 feature 库 qfq → capture 放行且落日证据。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    day = LIVE_SIGNAL_DAY
    feature_db = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=LIVE_CALENDAR)
    epoch, model_dir = _capture_epoch(
        tmp_path, day=day, feature_db=feature_db, feature_mode="qfq"
    )
    module = _script_module("alpha_v2_shadow_capture")
    rc = module.main(
        _capture_argv(
            tmp_path=tmp_path, epoch=epoch, model_dir=model_dir, day=day, market_db=feature_db
        )
    )
    assert rc == 0
    evidence = _capture_day_manifest(tmp_path, epoch.epoch_id, day)["feature_price_series"]
    assert evidence["expected_mode"] == "qfq"
    assert evidence["observed_mode"] == "qfq"
    assert evidence["contract_ok"] is True
    assert evidence["mode_match"] is True
    assert evidence["enforced"] is True
    assert evidence["source_db"] == str(feature_db)
    assert list((tmp_path / "validation").rglob("shadow_*.jsonl"))


def test_live_f2_frozen_qfq_with_raw_feature_db_is_rejected_before_features(
    tmp_path, monkeypatch
):
    """LIVE-F2：冻结 qfq + 当天 feature 库 raw → 在特征/预测/写盘之前拒绝。

    证明方式：把 ``daily_feature_frame`` / ``predict_frozen_model_matrix`` 换成会爆炸的
    探针——若它们被调到，测试会以 AssertionError 失败，而不是以契约错误退出。
    """
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    day = LIVE_SIGNAL_DAY
    feature_db = _write_db(tmp_path / "feature_raw.duckdb", mode="raw", days=LIVE_CALENDAR)
    epoch, model_dir = _capture_epoch(
        tmp_path, day=day, feature_db=feature_db, feature_mode="qfq"
    )
    module = _script_module("alpha_v2_shadow_capture")
    touched: list[str] = []

    def _explode(name: str):
        def _inner(*args, **kwargs):  # pragma: no cover - 被调用即测试失败
            touched.append(name)
            raise AssertionError(f"{name} 在 feature 口径硬门之前就被调用了")

        return _inner

    monkeypatch.setattr(module, "daily_feature_frame", _explode("daily_feature_frame"))
    monkeypatch.setattr(
        module, "predict_frozen_model_matrix", _explode("predict_frozen_model_matrix")
    )
    rc = module.main(
        _capture_argv(
            tmp_path=tmp_path, epoch=epoch, model_dir=model_dir, day=day, market_db=feature_db
        )
    )
    assert rc == 11
    assert touched == []
    assert list((tmp_path / "validation").rglob("shadow_*.jsonl")) == []
    assert not list((tmp_path / "validation").rglob("shadow_day_*.json"))


def test_live_f3_unprovable_feature_mode_is_rejected(tmp_path, monkeypatch):
    """LIVE-F3：feature 库口径不可证（无声明 + 探针样本不足）→ 拒绝且不写快照。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    day = LIVE_SIGNAL_DAY
    bare_db = _write_db(tmp_path / "feature_bare.duckdb", mode=None, days=LIVE_CALENDAR)
    epoch, model_dir = _capture_epoch(
        tmp_path, day=day, feature_db=bare_db, feature_mode="qfq"
    )
    module = _script_module("alpha_v2_shadow_capture")
    rc = module.main(
        _capture_argv(
            tmp_path=tmp_path, epoch=epoch, model_dir=model_dir, day=day, market_db=bare_db
        )
    )
    assert rc == 11
    assert list((tmp_path / "validation").rglob("shadow_*.jsonl")) == []


def test_live_f1b_frozen_mode_missing_fails_closed(tmp_path, monkeypatch):
    """LIVE-F1b：冻结模型未声明 feature 口径（v2/未封存形态）→ 拒绝。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    day = LIVE_SIGNAL_DAY
    feature_db = _write_db(tmp_path / "feature_qfq.duckdb", mode="qfq", days=LIVE_CALENDAR)
    epoch, model_dir = _capture_epoch(
        tmp_path, day=day, feature_db=feature_db, feature_mode=""
    )
    module = _script_module("alpha_v2_shadow_capture")
    rc = module.main(
        _capture_argv(
            tmp_path=tmp_path, epoch=epoch, model_dir=model_dir, day=day, market_db=feature_db
        )
    )
    assert rc == 11
    assert list((tmp_path / "validation").rglob("shadow_*.jsonl")) == []


class _LiveCaptureStub:
    """最小 service 替身：只驱动 run_daily_cycle 到 capture argv 构造。"""

    def __init__(self, *, config, now: datetime, root: Path):
        self.config = config
        self.calls: list[tuple[str, list[str]]] = []
        self.now = now
        self.root = root

        class _Automation:
            @staticmethod
            def probe_nightly_readiness(
                *, require_dual_delta: bool = False
            ) -> dict[str, object]:
                # active epoch 下 capture 走严格档（P1 R1）；本夹具恒就绪，声明收到即可。
                _ = require_dual_delta
                return {"allowed": True, "status": "ready", "reason": ""}

        self.automation = _Automation()


def _live_capture_cycle(*, config, now: datetime, calls: list[tuple[str, list[str]]]):
    """构造一个 capture argv 可观察、其余步骤全打桩的 cycle。"""
    from stock_analyzer.runtime.services.live_shadow_cycle_service import (
        LiveShadowCycleService,
    )

    service = type("Svc", (), {})()
    service._config = config
    service._record_audit_event = lambda **kwargs: None
    service._job_now = lambda: now
    service._week5_automation_service = _LiveCaptureStub(
        config=config, now=now, root=Path(".")
    ).automation
    cycle = LiveShadowCycleService(service)
    service._live_shadow_cycle = cycle
    cycle._run_cli = lambda script, argv, timeout_sec: (
        calls.append((script, list(argv))) or (0, "stub")
    )
    cycle._funnel_ready = lambda *, trade_date: (True, "")
    cycle._ensure_data_health = lambda *, trade_date: (True, "", {})
    cycle._ensure_feature_price_series = lambda **kwargs: (True, "", {"stub": True})
    cycle._run_history_tail = lambda **kwargs: []
    return cycle


def test_live_f4_scheduler_capture_uses_feature_market_db_path(tmp_path):
    """LIVE-F4：capture 必须收到 ``alpha_v2.feature_market_db``（而不是 db_path）。"""
    from stock_analyzer.alpha_v2.validation.epoch import active_epoch
    from stock_analyzer.alpha_v2.validation.runtime_identity import (
        config_hash_of,
        git_head,
    )

    config = load_config(REPO_ROOT / "config" / "default.yaml")
    config.alpha_v2.enabled = True
    config.alpha_v2.shadow_only = True
    config.alpha_v2.enforce_final_selection = False
    config.alpha_v2.artifact_root = str(tmp_path)
    config.alpha_v2.feature_market_db = str(tmp_path / "feature_A.duckdb")
    config.market_warehouse.db_path = str(tmp_path / "warehouse_B.duckdb")
    day = DAYS[12]
    manifest = write_freeze_manifest(
        tmp_path,
        validation_mode="rehearsal",
        validation_start_date=day.isoformat(),
        code_commit=git_head(REPO_ROOT),
        config_hash=config_hash_of(config),
    )
    open_epoch_for_manifest(tmp_path, manifest, opened_on_date=day.isoformat())
    assert active_epoch(tmp_path) is not None

    calls: list[tuple[str, list[str]]] = []
    cycle = _live_capture_cycle(
        config=config, now=datetime.combine(day, datetime.min.time()), calls=calls
    )
    result = cycle.run_daily_cycle()
    assert result["_scheduler_detail"] == "alpha_v2_cycle_completed", result
    capture = [argv for script, argv in calls if script == "alpha_v2_shadow_capture.py"]
    assert capture, calls
    market_db_arg = capture[0][capture[0].index("--market-db") + 1]
    assert market_db_arg == str(tmp_path / "feature_A.duckdb")
    assert market_db_arg != str(tmp_path / "warehouse_B.duckdb")


# ---------------------------------------------------------------------------
# LIVE-M1..M5：mature 侧的冻结 feature 口径门（P0 Final R1 / BLOCKER 3+4）
# ---------------------------------------------------------------------------


#: 生产同形的模型块：模型块 provenance 声明冻结 feature 口径（mature/scheduler 从它取）。
LIVE_FEATURE_MODEL_BLOCK: dict[str, object] = {
    **M3_MODEL_BLOCK,
    "provenance": {
        "window": ["2026-01-05", "2026-02-02"],
        "feature_data_identity": {"role": "feature", "price_series_mode": "qfq"},
    },
}


def _mature_cli(
    tmp_path: Path,
    *,
    epoch,
    evaluation_date: date,
    feature_db: Path | None,
    execution_db: Path,
):
    argv = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "alpha_v2_shadow_mature.py"),
        "--epoch-id",
        epoch.epoch_id,
        "--evaluation-date",
        evaluation_date.isoformat(),
        "--execution-market-db",
        str(execution_db),
        "--out",
        str(tmp_path),
    ]
    if feature_db is not None:
        argv += ["--feature-market-db", str(feature_db)]
    return subprocess.run(  # noqa: S603 - 固定脚本 + 列表参数
        argv,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _live_mature_epoch(tmp_path, *, config, day: date):
    return _epoch_with_shadow(
        tmp_path, config=config, day=day, model=dict(LIVE_FEATURE_MODEL_BLOCK)
    )


def test_live_m1_mature_with_matching_feature_mode_passes(tmp_path, monkeypatch):
    """LIVE-M1：冻结 qfq + mature feature 库 qfq + execution raw/certified → 放行。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    days = list(DAYS[:35])
    signal_day = DAYS[5]
    epoch, _ = _live_mature_epoch(tmp_path, config=config, day=signal_day)
    feature_db = _write_db(tmp_path / "m_feature_qfq.duckdb", mode="qfq", days=days)
    execution_db = _write_db(tmp_path / "m_execution_raw.duckdb", mode="raw", days=days)

    completed = _mature_cli(
        tmp_path,
        epoch=epoch,
        evaluation_date=days[30],
        feature_db=feature_db,
        execution_db=execution_db,
    )
    assert completed.returncode == 0, completed.stderr[-1200:]
    rows = [
        json.loads(line)
        for line in outcome_path(tmp_path, epoch.epoch_id, signal_day)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(rows) == len(SYMBOLS)
    assert {row["price_mode"] for row in rows} == {"raw"}
    assert all(row["price_mode_certified"] is True for row in rows)


def test_live_m2_mature_with_drifted_feature_mode_fails_closed(tmp_path, monkeypatch):
    """LIVE-M2：冻结 qfq + mature feature 库 raw → 拒绝且 0 行新 outcome。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    days = list(DAYS[:35])
    signal_day = DAYS[5]
    epoch, _ = _live_mature_epoch(tmp_path, config=config, day=signal_day)
    feature_db = _write_db(tmp_path / "m_feature_raw.duckdb", mode="raw", days=days)
    execution_db = _write_db(tmp_path / "m_execution_raw.duckdb", mode="raw", days=days)

    completed = _mature_cli(
        tmp_path,
        epoch=epoch,
        evaluation_date=days[30],
        feature_db=feature_db,
        execution_db=execution_db,
    )
    assert completed.returncode == 11, (completed.returncode, completed.stderr[-1200:])
    assert "不接受退回 execution 面板" in completed.stderr or "口径" in completed.stderr
    assert not outcome_path(tmp_path, epoch.epoch_id, signal_day).exists()


def test_live_m3_mature_without_feature_db_fails_closed(tmp_path, monkeypatch):
    """LIVE-M3：feature 库缺失/不可读 → 拒绝（**不得**退回 execution 面板算 style）。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    days = list(DAYS[:35])
    signal_day = DAYS[5]
    epoch, _ = _live_mature_epoch(tmp_path, config=config, day=signal_day)
    execution_db = _write_db(tmp_path / "m_execution_raw.duckdb", mode="raw", days=days)

    missing = _mature_cli(
        tmp_path,
        epoch=epoch,
        evaluation_date=days[30],
        feature_db=tmp_path / "does_not_exist.duckdb",
        execution_db=execution_db,
    )
    assert missing.returncode == 11, (missing.returncode, missing.stderr[-1200:])
    assert not outcome_path(tmp_path, epoch.epoch_id, signal_day).exists()

    unset = _mature_cli(
        tmp_path,
        epoch=epoch,
        evaluation_date=days[30],
        feature_db=None,
        execution_db=execution_db,
    )
    assert unset.returncode == 11, (unset.returncode, unset.stderr[-1200:])
    assert not outcome_path(tmp_path, epoch.epoch_id, signal_day).exists()


def test_live_m4_function_level_production_rejects_style_fallback(tmp_path, monkeypatch):
    """LIVE-M4：绕开 CLI 直接调用函数，production/test 下 style_panel=None 同样拒绝。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    days = list(DAYS[:35])
    signal_day = DAYS[5]
    epoch, _ = _live_mature_epoch(tmp_path, config=config, day=signal_day)
    execution_db = _write_db(tmp_path / "m_execution_raw.duckdb", mode="raw", days=days)
    raw_panel = _load_panel(execution_db, start=days[0], end=days[30], warmup=5)
    cert = raw_panel.certify_price_mode(min_sample=1)
    assert cert.mode == "raw" and cert.certified is True

    for mode in ("production", "test"):
        with pytest.raises(OutcomeMaturationError):
            mature_epoch_outcomes(
                root=tmp_path,
                epoch=epoch,
                panel=raw_panel,
                style_panel=None,
                evaluation_date=days[30],
                matcher=_matcher(),
                slippage_ratio=0.0015,
                price_mode=cert.mode,
                price_mode_certified=cert.certified,
                validation_mode=mode,
            )
    # 默认值就是 production（忘传也不会拿到宽松行为）
    with pytest.raises(OutcomeMaturationError):
        mature_epoch_outcomes(
            root=tmp_path,
            epoch=epoch,
            panel=raw_panel,
            style_panel=None,
            evaluation_date=days[30],
            matcher=_matcher(),
            slippage_ratio=0.0015,
            price_mode=cert.mode,
            price_mode_certified=cert.certified,
        )
    assert not outcome_path(tmp_path, epoch.epoch_id, signal_day).exists()


def test_live_m5_rehearsal_style_fallback_is_labeled(tmp_path, monkeypatch):
    """LIVE-M5：rehearsal + style_panel=None → 允许，但摘要必须自曝降级来源。"""
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    days = list(DAYS[:35])
    signal_day = DAYS[5]
    epoch, _ = _live_mature_epoch(tmp_path, config=config, day=signal_day)
    execution_db = _write_db(tmp_path / "m_execution_raw.duckdb", mode="raw", days=days)
    raw_panel = _load_panel(execution_db, start=days[0], end=days[30], warmup=5)
    cert = raw_panel.certify_price_mode(min_sample=1)

    summary = mature_epoch_outcomes(
        root=tmp_path,
        epoch=epoch,
        panel=raw_panel,
        style_panel=None,
        evaluation_date=days[30],
        matcher=_matcher(),
        slippage_ratio=0.0015,
        price_mode=cert.mode,
        price_mode_certified=cert.certified,
        validation_mode="rehearsal",
    )
    assert summary["style_features_source"] == "execution_panel_fallback_rehearsal"
    assert summary["validation_mode"] == "rehearsal"
    assert summary["rows_written"] == len(SYMBOLS)
    assert outcome_path(tmp_path, epoch.epoch_id, signal_day).exists()


# ---------------------------------------------------------------------------
# DP-11..DP-18：P3.1 有效决策集契约（PIT 候选 → execution 可用 → 训练帧）
#
# 背景（2026-09-23 P3 Freeze Preparation 实测）：生产窗口上原来的逐项对齐门把
# 6602/1650654（0.400%）条 decision 判成"缺失"→ fail closed，freeze 根本跑不起来。
# 那不是数据缺失，而是契约写错了位置：``expected_active_lookback_days=5`` 的设计就是
# 让"最近 5 个交易日内交易过、当天停牌"（含退市/长停期间仍在 history 窗口内的票）
# 留在**候选**池里——候选集不是可交易集。本组用例把修正后的契约钉死：
#
# ==========================  ==============================================
# DP-11                       根因前提：PIT eligible 确实含"当天无 bar"的票
# DP-12                       Case 1/2：交易日保留、停牌日过滤（不是 error）
# DP-13                       Case 3a：整票缺席 execution 面板 → 仍然 fail
# DP-14                       Case 3b/3c：整天不是交易日 / 跨面板分歧 → fail
# DP-15                       量级闸：总体占比、单日占比
# DP-16                       不变性：过滤 ≡ 只喂对齐后的决策（不改变策略）
# DP-17                       严格版 assert_decisions_aligned 语义未被放松
# DP-18                       CLI：打印 alignment 报告；结构缺陷以 exit 4 退出
# ==========================  ==============================================
# ---------------------------------------------------------------------------


def _weekdays(count: int, *, start: date | None = None) -> list[date]:
    """从 ``start``（默认 DAYS[0]）起的连续 count 个工作日（与 helpers 同口径）。"""
    days: list[date] = []
    current = start or DAYS[0]
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _availability_bars(
    symbols: list[str],
    days: list[date],
    *,
    missing: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    mode: str,
    start_price: float = 10.0,
) -> list[dict[str, object]]:
    """造一段温和上行的合法行情；``missing`` 里的 ``(symbol, ISO 日期)`` 不产出 bar。

    停牌形态：中间**没有** bar（而不是补一根零成交），复牌那根的 ``prev_close`` 取
    上一根**实际存在**的 bar 收盘——这正是数据里的样子。
    """
    omitted = {(str(symbol), str(day)) for symbol, day in missing}
    bars: list[dict[str, object]] = []
    for index, symbol in enumerate(symbols):
        prev = start_price + index
        for day in days:
            if (str(symbol), day.isoformat()) in omitted:
                continue
            close = round(prev * 1.004, 2)
            bars.append(
                _bar(
                    symbol,
                    day,
                    open_=prev,
                    high=round(max(prev, close) * 1.001, 2),
                    low=round(min(prev, close) * 0.999, 2),
                    close=close,
                    prev_close=prev,
                    price_series_mode=mode,
                )
            )
            prev = close
    return bars


def _availability_panels(
    days: list[date],
    symbols: list[str],
    *,
    execution_missing: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    feature_missing: set[tuple[str, str]] | frozenset[tuple[str, str]] | None = None,
    execution_symbols: list[str] | None = None,
):
    """一对面板（feature=qfq / execution=raw）+ 各自的口径认证。

    ``feature_missing`` 默认与 ``execution_missing`` 相同——这是生产实测的形态
    （两侧都没有那根 bar，``feature_only=0``）。显式传不同值即构造"跨面板分歧"；
    ``execution_symbols`` 少于 feature 侧即构造"整票缺席"。
    """
    if feature_missing is None:
        feature_missing = execution_missing
    feature = _panel(_availability_bars(symbols, days, missing=feature_missing, mode="qfq"))
    execution = _panel(
        _availability_bars(
            symbols if execution_symbols is None else execution_symbols,
            days,
            missing=execution_missing,
            mode="raw",
        )
    )
    return {
        "feature": feature,
        "execution": execution,
        "feature_cert": feature.certify_price_mode(min_sample=1),
        "execution_cert": execution.certify_price_mode(min_sample=1),
    }


def _build_availability(panels, decisions):
    return dpf.build_dual_price_training_frame(
        feature_panel=panels["feature"],
        execution_panel=panels["execution"],
        decisions=decisions,
        matcher=_matcher(),
        slippage_ratio=0.0,
        execution_certification=panels["execution_cert"],
        feature_certification=panels["feature_cert"],
        context="dp_availability",
    )


def test_dp11_pit_eligible_universe_contains_symbols_without_bar_that_day():
    """DP-11（根因前提）：PIT eligible 池确实包含"当天没有 bar"的票——它不是可交易集。

    这是契约修正的**前提事实**，不是推论：``known_suspended``（eligible 但 lookback
    内 0 根 bar，停牌/停更）被列进 ``eligible_symbols``，于是"当天无 bar"的
    ``(symbol, date)`` 是候选池的正常成员。这条不成立，过滤就没有存在理由。
    """
    days = _weekdays(90)
    halted = "600001"
    halt_days = {days[index] for index in range(80, 90)}
    panels = _availability_panels(
        days,
        [halted, "600002"],
        execution_missing={(halted, day.isoformat()) for day in halt_days},
    )
    feature = panels["feature"]
    as_of = days[85]
    universe = feature.pit_universe(as_of=as_of, min_history_days=60)
    assert halted in universe.eligible_symbols, "前提：长停票仍在 PIT eligible 池里"
    assert halted in universe.known_suspended_symbols
    last_bar = feature.symbol_bars(halted).index.max().date()
    assert last_bar < as_of, "前提：该票在决策日当天没有 bar"
    assert last_bar == days[79], "前提：缺失是停牌（前面有 bar），不是面板缺这只票"


def test_dp12_trading_day_kept_and_halt_day_filtered(dual_dbs):
    """DP-12（Case 1 + Case 2）：交易日保留；当天停牌的行**过滤**而不是抛错。

    Case 1：``(600000, D)`` 有 execution bar → 保留。
    Case 2：``(600001, D)`` 停牌（两侧都没 bar）→ 过滤；且**同票的前一个交易日决策
    仍然保留**（过滤粒度是 ``(symbol, date)``，不是整票）。
    """
    days = list(DAYS[:35])
    decision_days = [days[19], days[DECISION_INDEX]]
    traded, halted = SYMBOLS[0], SYMBOLS[1]
    panels = _availability_panels(
        days,
        SYMBOLS,
        execution_missing={(halted, days[DECISION_INDEX].isoformat())},
    )
    decisions = [DecisionPoint(symbol, day) for day in decision_days for symbol in SYMBOLS]
    built = _build_availability(panels, decisions)
    report = built.evidence["decision_alignment"]

    assert report["decision_rows_before"] == 8
    assert report["filtered_missing_execution_rows"] == 1
    assert report["decision_rows_after"] == 7
    assert report["intersection"] == 7  # 与 decision_rows_after 同值（同一个交集）
    assert report["filter_reason"] == FILTER_REASON_NO_EXECUTION_BAR
    assert report["filtered_examples"] == [f"{halted}@{days[DECISION_INDEX].isoformat()}"]
    assert report["status"] == "PASS"
    assert report["aligned"] is False  # 发生了过滤（"零过滤"这个更强形态不成立）
    assert report["filtered_dates"] == 1
    assert report["universe"] == "pit_eligible_candidates"

    keys = {
        (str(row.symbol), str(row.decision_date))
        for row in built.frame[["symbol", "decision_date"]].itertuples(index=False)
    }
    assert (traded, days[DECISION_INDEX].isoformat()) in keys  # Case 1：保留
    assert (halted, days[DECISION_INDEX].isoformat()) not in keys  # Case 2：过滤
    assert (halted, days[19].isoformat()) in keys  # 同票其它交易日不受影响
    assert len(keys) == 7
    accounting = built.evidence["decision_accounting"]
    assert accounting["decision_universe_rows"] == 8
    assert accounting["execution_available_rows"] == 7


def test_dp13_symbol_absent_from_execution_panel_still_fails():
    """DP-13（Case 3a）：整票缺席 execution 面板 → **仍然 fail closed**。

    这是"真实数据缺失"的形态之一：feature 面板有这只票、execution 面板完全没有。
    静默过滤会把"整只票断供"伪装成"少了几行训练样本"，所以必须拦。
    """
    days = list(DAYS[:35])
    panels = _availability_panels(
        days,
        SYMBOLS,
        execution_symbols=SYMBOLS[:3],
    )
    decisions = [DecisionPoint(SYMBOLS[3], days[DECISION_INDEX])]
    with pytest.raises(PriceSeriesContractError) as excinfo:
        _build_availability(panels, decisions)
    assert DEFECT_REASON_SYMBOL_ABSENT in str(excinfo.value)


def test_dp14_date_not_a_session_and_cross_panel_divergence_still_fail():
    """DP-14（Case 3b/3c）：整天不是 execution 交易日 / 跨面板分歧 → **仍然 fail**。

    3b：决策日整份 execution 面板没有（被截断/换库）——不是停牌，是面板不对。
    3c：feature 侧**有**当天 bar 而 execution 没有——两份面板对同一事实给出不同答案，
        且这种行还会改变质量池排名分母，静默过滤同样不可接受。
    """
    days = list(DAYS[:35])
    decision_day = days[DECISION_INDEX]
    traded = SYMBOLS[0]

    whole_day_gone = {(symbol, decision_day.isoformat()) for symbol in SYMBOLS}
    panels_b = _availability_panels(days, SYMBOLS, execution_missing=whole_day_gone)
    with pytest.raises(PriceSeriesContractError) as excinfo_b:
        _build_availability(panels_b, [DecisionPoint(traded, decision_day)])
    assert DEFECT_REASON_DATE_NOT_SESSION in str(excinfo_b.value)

    panels_c = _availability_panels(
        days,
        SYMBOLS,
        execution_missing={(traded, decision_day.isoformat())},
        feature_missing=set(),
    )
    with pytest.raises(PriceSeriesContractError) as excinfo_c:
        _build_availability(panels_c, [DecisionPoint(traded, decision_day)])
    assert DEFECT_REASON_CROSS_PANEL_DIVERGENCE in str(excinfo_c.value)


def test_dp15_filter_ratio_ceilings_fail_closed():
    """DP-15：过滤量级闸——总体占比超 2% / 单日**过半**被过滤 → fail。

    判据本身是纯集合关系；量级闸是兜底，两条的语义不同（不能只留一个比例数）：

    - 总体占比：窗口级断供（实测合法值 0.400%，上限 2%）；
    - 单日占比：**这一天过半候选被过滤**=该日截面被毁（实测最大合法单日占比
      12.25%：2025-11-17 两侧同时缺 724 个 symbol 的当日 bar，属链路覆盖缺口）。
      只丢一侧由跨面板分歧检查逐键拦截，与量级无关，所以单日闸不必设得很紧。

    这里用显式阈值做单变量实验（默认行数下限 50 会掩盖阈值逻辑，故先断言默认不误杀）。
    """
    days = _weekdays(30)
    halted_day = days[25]
    panels = _availability_panels(
        days,
        ["600000", "600001", "600002", "600003"],
        execution_missing={
            ("600001", halted_day.isoformat()),
            ("600003", halted_day.isoformat()),
        },
    )
    execution = panels["execution"]
    traded = [DecisionPoint(symbol, halted_day) for symbol in ("600000", "600002")]
    halted = [DecisionPoint(symbol, halted_day) for symbol in ("600001", "600003")]
    decisions = [*traded, *halted]

    # 默认阈值（总体 2% / 单日 50% / 行数下限 50）：小样本不误杀
    ok = filter_decisions_by_execution_availability(
        decisions=decisions, execution_panel=execution, context="unit"
    )
    assert ok.filtered_rows == 2 and ok.report["status"] == "PASS"
    assert ok.report["decision_rows_after"] == 2
    assert ok.report["filtered_dates_top"][0]["decision_date"] == halted_day.isoformat()

    # 总体占比闸：floor=0、每日闸放到 99% → 2/4 = 50% > ceil(10%×4)=1 行
    with pytest.raises(PriceSeriesContractError) as overall:
        filter_decisions_by_execution_availability(
            decisions=decisions,
            execution_panel=execution,
            context="unit",
            max_filtered_ratio=0.10,
            max_daily_filtered_ratio=0.99,
            max_filtered_rows_floor=0,
        )
    assert "超过审计上限" in str(overall.value)

    # 单日占比闸：总体闸放到 99%（允许 4 行）→ 每日允许 ceil(10%×4)=1 行，实际 2 行
    with pytest.raises(PriceSeriesContractError) as daily:
        filter_decisions_by_execution_availability(
            decisions=decisions,
            execution_panel=execution,
            context="unit",
            max_filtered_ratio=0.99,
            max_daily_filtered_ratio=0.10,
            max_filtered_rows_floor=0,
        )
    assert "单日过滤量异常" in str(daily.value)


def test_dp15b_default_daily_ceiling_separates_partial_from_destroyed_cross_section():
    """DP-15b（默认阈值语义，实测形态）：单日 40% 被过滤 → PASS；单日 60% → fail。

    "部分覆盖缺口"（生产实测 2025-11-17 = 12.25%）必须放行并留证据，而"该日截面被毁"
    必须拦住——这是**默认阈值**下的行为：200 只票 × 30 个决策日（6000 条候选）里，
    某一天缺 80 票（该日 40%、全窗 1.3%）放行；缺 120 票（该日 60%）拦下。
    窗口内多放几个决策日是有意的：总体闸（2%）算的是全窗，不能让"一天=全窗"的夹具
    把总体闸和单日闸混成同一个判据。
    """
    days = _weekdays(30)
    decision_days = days[:30]
    partial_day = days[25]
    symbols = [f"{600000 + index:06d}" for index in range(200)]
    partial_missing = {(symbol, partial_day.isoformat()) for symbol in symbols[:80]}
    destroyed_missing = {(symbol, partial_day.isoformat()) for symbol in symbols[:120]}
    partial = _availability_panels(days, symbols, execution_missing=partial_missing)
    destroyed = _availability_panels(days, symbols, execution_missing=destroyed_missing)
    decisions = [DecisionPoint(symbol, day) for day in decision_days for symbol in symbols]

    passed = filter_decisions_by_execution_availability(
        decisions=decisions, execution_panel=partial["execution"], context="unit_partial"
    )
    assert passed.filtered_rows == 80 and passed.report["status"] == "PASS"
    assert passed.report["max_daily_filtered_ratio"] == pytest.approx(0.4)
    assert passed.report["filtered_ratio"] == pytest.approx(80 / len(decisions))
    assert passed.report["limits"]["max_daily_filtered_ratio"] == 0.50
    assert passed.report["filtered_dates_top"][0] == {
        "decision_date": partial_day.isoformat(),
        "filtered": 80,
        "decisions": 200,
        "ratio": 0.4,
    }

    with pytest.raises(PriceSeriesContractError) as excinfo:
        filter_decisions_by_execution_availability(
            decisions=decisions,
            execution_panel=destroyed["execution"],
            context="unit_destroyed",
        )
    assert "单日过滤量异常" in str(excinfo.value)


def test_dp16_filtering_is_row_dropping_only(dual_dbs):
    """DP-16（不变性）：过滤 ≡ 只把对齐后的决策喂进去——训练内容不受影响。

    这是"契约修正不是策略变更"的可执行形式：同一批判据下，``build(全部候选)`` 与
    ``build(只喂对齐行)`` 的**训练帧逐值相同**（行集合、特征、label、超额），
    基准层与安全特征列也相同；差别只有审计里的 before/after 计数。
    若有人把"过滤"改成"补行 / 回退 qfq / 改基准分母"，这条会立刻红。
    """
    days = list(DAYS[:35])
    halted, halted_day = SYMBOLS[2], days[DECISION_INDEX]
    panels = _availability_panels(
        days,
        SYMBOLS,
        execution_missing={(halted, halted_day.isoformat())},
    )
    decision_days = days[16:24]
    everything = [DecisionPoint(symbol, day) for day in decision_days for symbol in SYMBOLS]
    kept_only = [
        item for item in everything if (item.symbol, item.decision_date) != (halted, halted_day)
    ]
    filtered = _build_availability(panels, everything)
    prefiltered = _build_availability(panels, kept_only)

    report = filtered.evidence["decision_alignment"]
    assert report["decision_rows_before"] == len(everything)
    assert report["filtered_missing_execution_rows"] == 1
    assert report["decision_rows_after"] == len(kept_only)
    pd.testing.assert_frame_equal(
        filtered.frame.reset_index(drop=True), prefiltered.frame.reset_index(drop=True)
    )
    assert filtered.evidence["benchmark_layers"] == prefiltered.evidence["benchmark_layers"]
    assert (
        filtered.evidence["benchmark_primary_layer"]
        == prefiltered.evidence["benchmark_primary_layer"]
    )
    assert filtered.evidence["quality_pool_source"] == prefiltered.evidence["quality_pool_source"]
    assert filtered.safe_feature_columns == prefiltered.safe_feature_columns


def test_dp17_strict_alignment_assertion_is_not_relaxed():
    """DP-17：``assert_decisions_aligned`` 仍是**零容忍**版本（过滤只在训练帧路径生效）。"""
    days = list(DAYS[:35])
    panels = _availability_panels(
        days,
        SYMBOLS,
        execution_missing={(SYMBOLS[1], days[DECISION_INDEX].isoformat())},
    )
    decisions = [DecisionPoint(symbol, days[DECISION_INDEX]) for symbol in SYMBOLS]
    with pytest.raises(PriceSeriesContractError):
        assert_decisions_aligned(
            decisions=decisions,
            execution_panel=panels["execution"],
            context="unit_strict",
        )
    aligned = [
        DecisionPoint(symbol, days[DECISION_INDEX]) for symbol in SYMBOLS if symbol != SYMBOLS[1]
    ]
    payload = assert_decisions_aligned(
        decisions=aligned, execution_panel=panels["execution"], context="unit_strict"
    )
    assert payload["aligned"] is True and payload["missing"] == 0


def _freeze_cli(tmp_path: Path, *, feature_db: Path, execution_db: Path, days: list[date]):
    return subprocess.run(  # noqa: S603 - 固定脚本 + 列表参数
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "alpha_v2_shadow_model_freeze.py"),
            "--rehearsal",
            "--feature-market-db",
            str(feature_db),
            "--execution-market-db",
            str(execution_db),
            "--window-start",
            days[0].isoformat(),
            "--window-end",
            days[-1].isoformat(),
            "--warmup-days",
            "5",
            "--model-id",
            "dp_availability_cli",
            "--out",
            str(tmp_path / "out"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def test_dp18_freeze_cli_reports_alignment_and_exits_4_on_defect(tmp_path):
    """DP-18（Modification 2/3）：CLI 必须打印 alignment 报告；结构缺陷以 exit 4 退出。

    ① 结构缺陷（execution 面板少了整票）→ **exit 4**（文档化的契约退出码），而不是
       未捕获 traceback 的 exit 1；且不写任何工件。
    ② 健康面板 → 报告行必须出现且自洽（before == after、filtered == 0、status PASS）。
       ② 只断言那一行：完整 freeze 训练不在本用例范围内（同一入口的其它环节各有专项
       用例），所以这里不约束 ② 的退出码。

    夹具需要 ≥60 根 bar 才可能有 PIT eligible 票（``min_history_days=60``），
    故用 80 个工作日而不是 35。
    """
    days = _weekdays(80)
    broken_feature = _write_db(
        tmp_path / "cli_feature_qfq.duckdb", mode="qfq", days=days, symbols=SYMBOLS
    )
    broken_execution = _write_db(
        tmp_path / "cli_execution_raw.duckdb", mode="raw", days=days, symbols=SYMBOLS[:3]
    )
    failed = _freeze_cli(
        tmp_path, feature_db=broken_feature, execution_db=broken_execution, days=days
    )
    assert failed.returncode == 4, (failed.returncode, failed.stderr[-1500:])
    assert "Traceback" not in failed.stderr
    assert "exit_code=4" in failed.stderr
    assert DEFECT_REASON_SYMBOL_ABSENT in failed.stderr
    assert not (tmp_path / "out").exists()

    healthy_feature = _write_db(
        tmp_path / "cli_ok_feature_qfq.duckdb", mode="qfq", days=days, symbols=SYMBOLS
    )
    healthy_execution = _write_db(
        tmp_path / "cli_ok_execution_raw.duckdb", mode="raw", days=days, symbols=SYMBOLS
    )
    passed = _freeze_cli(
        tmp_path, feature_db=healthy_feature, execution_db=healthy_execution, days=days
    )
    match = re.search(
        r"Dual price alignment: before: (\d+) decision keys / "
        r"filtered: (\d+) unavailable execution bars / "
        r"after: (\d+) aligned keys / status: (\w+)",
        passed.stdout,
    )
    assert match is not None, passed.stdout[-2000:]
    before, filtered, after, status = match.groups()
    assert int(before) > 0 and int(before) == int(after) and int(filtered) == 0
    assert status == "PASS"
