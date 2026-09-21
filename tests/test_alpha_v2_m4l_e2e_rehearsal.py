"""M4-L §34：端到端排演 —— fake 夜扫 → 生产漏斗工件 → capture → mature → KPI。

证明四件事（全部在临时 artifact root 上、用确定性时钟，永不污染生产）：

1. Production Deep50 provenance 可追溯（行上的三级 rank 全来自 funnel 工件）；
2. Alpha Rank 独立（Attack D：Alpha 自己排的 Top1/3/5 与生产名次不同时，
   cohort 仍以生产漏斗为准）；
3. clean_oos eligibility 正确（rehearsal 永不算 clean）；
4. 重复运行幂等（不重复写、不篡改已冻结行）。

同时把 §15 的循环顺序（data_health → capture → mature → KPI）在库层面跑通一次。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
from _alpha_v2_m3_fixtures import (
    open_epoch_for_manifest,
    write_freeze_manifest,
)
from _alpha_v2_research_helpers import _trading_days

from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
from stock_analyzer.alpha_v2.validation.frozen_model import (
    fit_frozen_model,
    persist_frozen_model,
)
from stock_analyzer.alpha_v2.validation.production_funnel import (
    build_source_evidence,
    emit_funnel_snapshot,
    extract_funnel_from_source_evidence,
    file_sha256,
    funnel_snapshot_hash,
    write_source_evidence,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (
    config_hash_of,
    resolve_runtime_code_identity,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import read_shadow_rows
from stock_analyzer.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]

SYMBOLS = ["600001", "600002", "600003", "600004", "600005", "600006"]
FEATURES = ["ret_1d", "ret_5d", "ma5", "volume_ratio_5"]
CALENDAR = _trading_days(date(2026, 3, 2), 90)
SIGNAL_DAY = CALENDAR[-12]  # 后面留 11 个交易日给 3d/5d outcome 成熟
MODEL_ID = "alpha_v2_shadow_rehearsal"


def _synthetic_market_db(path: Path) -> Path:
    """造一份最小可用的 daily_bars（raw 口径、含涨跌停与板块列）。"""
    rng = np.random.default_rng(42)
    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(SYMBOLS):
        close = 10.0 + index
        for day in CALENDAR:
            drift = 0.002 * (index - 2) + rng.normal(0.0, 0.004)
            open_ = close
            close = round(close * (1.0 + drift), 4)
            high = round(max(open_, close) * 1.01, 4)
            low = round(min(open_, close) * 0.99, 4)
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": float(1_000_000 + index * 10_000 + rng.integers(0, 5000)),
                    "turnover": float((1_000_000 + index * 10_000) * close),
                    "float_market_cap": float(5_000_000_000 + index * 1e8),
                    "board": "主板",
                    "is_st": False,
                    "is_delisting_risk": False,
                    "suspended": False,
                    "pre_close": open_,
                    "up_limit": round(open_ * 1.1, 4),
                    "down_limit": round(open_ * 0.9, 4),
                    "price_series_mode": "raw",
                }
            )
    frame = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.register("bars_frame", frame)
        connection.execute("CREATE TABLE daily_bars AS SELECT * FROM bars_frame")
    finally:
        connection.close()
    return path


def _train_and_persist_model(root: Path, *, code_commit: str) -> dict[str, object]:
    """在合成特征上训练一个最小冻结模型（与 capture 的 feature schema 对齐）。"""
    rng = np.random.default_rng(7)
    total = 240
    days = np.repeat(np.arange(total // 20), 20)[:total]
    frame = pd.DataFrame(
        {
            "decision_date": [f"2026-01-{int(d) + 1:02d}" for d in days],
            "symbol": [SYMBOLS[index % len(SYMBOLS)] for index in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in FEATURES},
        }
    )
    base = 0.3 * frame["ret_1d"] + 0.2 * frame["ma5"] - 0.1 * frame["volume_ratio_5"]
    for horizon in (3, 5, 10, 15):
        frame[f"net_return_{horizon}d"] = base * (horizon / 5.0) + rng.normal(0.0, 0.01, total)
        frame[f"excess_return_{horizon}d"] = frame[f"net_return_{horizon}d"] - 0.001
        frame[f"mae_{horizon}d"] = -np.abs(frame[f"net_return_{horizon}d"]) * 0.6
        frame[f"up_net_{horizon}d"] = (frame[f"net_return_{horizon}d"] > 0).astype(float)
        frame[f"up_excess_{horizon}d"] = (frame[f"excess_return_{horizon}d"] > 0).astype(float)
        frame[f"mae_le_5pct_{horizon}d"] = (frame[f"mae_{horizon}d"] <= -0.05).astype(float)
    frame["alpha_target_5d"] = frame["excess_return_5d"].groupby(frame["decision_date"]).rank(
        pct=True
    )
    frame["is_train"] = False
    frame["is_calibration"] = False
    frame.loc[:179, "is_train"] = True
    frame.loc[180:, "is_calibration"] = True
    model = fit_frozen_model(
        frame=frame,
        model_id=MODEL_ID,
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        provenance={"window": ["2025-09-01", "2026-03-01"]},
        # R4.1：训练身份必须等于本次运行身份，否则 capture 的绑定硬门会拒。
        extra_identity={"code_commit": code_commit},
    )
    persist_frozen_model(model, root)
    return {
        "model_id": MODEL_ID,
        "artifact_hash": str(model.manifest.get("artifact_hash", "")),
        "artifact_created_at": "2026-03-01T12:00:00+08:00",
        "artifact_path": str(root / "model" / MODEL_ID),
        "status": "frozen",
        "provenance": {"window": ["2025-09-01", "2026-03-01"]},
        "model_training_code_commit": code_commit,
    }


def _funnel_report(deep_order: list[str]) -> dict[str, object]:
    """Alpha 排序与生产名次刻意不同（Attack D 的构造）。"""
    quality = [{"symbol": symbol, "score": 90 - index} for index, symbol in enumerate(SYMBOLS)]
    light = [
        {"symbol": symbol, "baseline_score": 70 - index}
        for index, symbol in enumerate(SYMBOLS[:5])
    ]
    deep = [
        {"symbol": symbol, "funnel_score": 60 - index}
        for index, symbol in enumerate(deep_order)
    ]
    return {
        "funnel": {
            "policy": "snapshot_funnel",
            "deep_stage_ran": True,
            "selection_contract": {
                "selection_contract_id": "night_alpha_v2_v1",
                "quality_target": 300,
                "light_target": 100,
                "deep_target": 50,
            },
        },
        "prefilter": {
            "universe_quality_selection": {"selector_mode": "quality", "selected": quality},
            "shortlisted": light,
            "deep_stage": {"selected": deep},
            "pinned_symbols": [],
            "intraday_degraded": False,
        },
    }


@pytest.fixture
def rehearsal_env(tmp_path, monkeypatch):
    """排演环境：清单身份 = 真实运行身份的写照（否则身份硬门会（正确地）拦下）。

    execution_price_mode 通过 SA__ 环境变量把执行价口径切到 raw（生产也是这么切的），
    这样 manifest 里写的 raw 与 CLI 现场 resolve 出来的 raw 才是一回事。
    """
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    runtime_identity = resolve_runtime_code_identity(REPO_ROOT, validation_mode="rehearsal")
    assert runtime_identity.code_commit
    config = load_config(REPO_ROOT / "config" / "default.yaml")

    root = tmp_path / "alpha_v2"
    market_db = _synthetic_market_db(tmp_path / "warehouse" / "market.duckdb")
    model_block = _train_and_persist_model(root, code_commit=runtime_identity.code_commit)
    manifest = write_freeze_manifest(
        root,
        validation_mode="rehearsal",
        deterministic_clock=True,
        validation_start_date=CALENDAR[0].isoformat(),
        model=model_block,
        feature_columns=FEATURES,
        execution_price_mode="raw",
        code_commit=runtime_identity.code_commit,
        config_hash=config_hash_of(config),
    )
    epoch = open_epoch_for_manifest(root, manifest, opened_on_date=CALENDAR[0].isoformat())
    funnel_root = tmp_path / "runtime" / "production_funnel"
    return {
        "root": root,
        "market_db": market_db,
        "manifest": manifest,
        "epoch": epoch,
        "funnel_root": funnel_root,
        "tmp": tmp_path,
        "env": {**os.environ, "SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE": "raw"},
    }


def _emit_funnel(env, deep_order: list[str]) -> dict[str, object]:
    """R1：先落成员来源证据，再从它抽取 funnel（源指针指向证据文件字节哈希）。"""
    evidence = build_source_evidence(
        source_report=_funnel_report(deep_order),
        trade_date=SIGNAL_DAY.isoformat(),
        trace_id="week5-night-scan-20260312000000",
        created_at=f"{SIGNAL_DAY.isoformat()}T22:00:00+08:00",
    )
    evidence_path = write_source_evidence(funnel_root=env["funnel_root"], payload=evidence)
    payload = extract_funnel_from_source_evidence(
        evidence,
        source_artifact_path=str(evidence_path),
        source_artifact_sha256=file_sha256(evidence_path),
    )
    payload["signal_date"] = SIGNAL_DAY.isoformat()
    payload["trade_date"] = SIGNAL_DAY.isoformat()
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    emit_funnel_snapshot(funnel_root=env["funnel_root"], payload=payload)
    return payload


def _capture(env, *, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "alpha_v2_shadow_capture.py"),
            "--epoch-id", env["epoch"].epoch_id,
            "--signal-date", SIGNAL_DAY.isoformat(),
            "--capture-date", SIGNAL_DAY.isoformat(),
            "--market-db", str(env["market_db"]),
            "--out", str(env["root"]),
            "--cohort-source", "production_funnel",
            "--funnel-root", str(env["funnel_root"]),
            *(extra or []),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        env=env["env"],
    )


def test_rehearsal_end_to_end_production_cohort(rehearsal_env):
    env = rehearsal_env
    # Attack D 构造：生产 Deep50 顺序 600004 → 600002 → 600001，
    # 与 Alpha 自己的打分排序无任何关系。
    deep_order = ["600004", "600002", "600001"]
    _emit_funnel(env, deep_order)

    result = _capture(env)
    assert result.returncode == 0, result.stderr[-2000:]
    rows = read_shadow_rows(env["root"], env["epoch"].epoch_id, SIGNAL_DAY)
    assert [row["symbol"] for row in rows] == sorted(rows_symbols(rows))
    by_symbol = {row["symbol"]: row for row in rows}
    # cohort == 生产 Deep50（不是 Alpha 自选）
    assert set(by_symbol) == set(deep_order)
    # 生产名次来自 funnel，Alpha 只贡献分数与 v2_top1/3/5
    assert by_symbol["600004"]["deep_rank"] == 1
    assert by_symbol["600002"]["deep_rank"] == 2
    assert by_symbol["600001"]["deep_rank"] == 3
    assert all(row["quality_rank"] is not None for row in rows)
    assert all(row["light_rank"] is not None for row in rows)
    assert by_symbol["600004"]["in_deep_pool"] is True
    assert {row["quality_pool_source"] for row in rows} == {"production_selection_engine"}
    # rehearsal 永不算 clean
    assert {row["clean_oos_eligible"] for row in rows} == {False}
    # manifest 内嵌完整 funnel 证据（KPI 治理层的复核对象）
    manifest_path = (
        env["root"] / "validation" / env["epoch"].epoch_id / "manifests"
        / f"shadow_day_{SIGNAL_DAY.strftime('%Y%m%d')}.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cohort_source"] == "production_funnel"
    assert manifest["funnel"]["funnel_snapshot_hash"]
    assert [m["symbol"] for m in manifest["funnel"]["deep_members"]] == deep_order
    assert manifest["funnel"]["source_night_scan_artifact_sha256"]
    assert manifest["counts"]["production_deep"] == 3


def rows_symbols(rows: list[dict[str, object]]) -> list[str]:
    return [str(row["symbol"]) for row in rows]


def test_rehearsal_rerun_is_idempotent(rehearsal_env):
    env = rehearsal_env
    _emit_funnel(env, ["600004", "600002", "600001"])
    first = _capture(env)
    assert first.returncode == 0, first.stderr[-1500:]
    rows_before = read_shadow_rows(env["root"], env["epoch"].epoch_id, SIGNAL_DAY)
    second = _capture(env)
    assert second.returncode == 0, second.stderr[-1500:]
    rows_after = read_shadow_rows(env["root"], env["epoch"].epoch_id, SIGNAL_DAY)
    assert rows_before == rows_after  # 同一份内容，逐字节一致（含 recorded_at）


def test_rehearsal_mature_and_kpi_complete_the_cycle(rehearsal_env):
    """§15 循环顺序在库层面跑通：capture → mature → KPI 报告（rehearsal 永不算 clean）。"""
    env = rehearsal_env
    _emit_funnel(env, ["600004", "600002", "600001"])
    captured = _capture(env)
    assert captured.returncode == 0, captured.stderr[-1500:]

    evaluation_day = CALENDAR[-1]
    matured = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "alpha_v2_shadow_mature.py"),
            "--epoch-id", env["epoch"].epoch_id,
            "--evaluation-date", evaluation_day.isoformat(),
            "--market-db", str(env["market_db"]),
            "--out", str(env["root"]),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        env=env["env"],
    )
    assert matured.returncode == 0, matured.stderr[-1500:]
    outcome_file = (
        env["root"] / "validation" / env["epoch"].epoch_id / "outcomes"
        / f"{SIGNAL_DAY.year:04d}" / f"{SIGNAL_DAY.month:02d}"
        / f"outcome_{SIGNAL_DAY.strftime('%Y%m%d')}.jsonl"
    )
    assert outcome_file.exists(), list(env["root"].rglob("outcome_*.jsonl"))
    outcome_rows = [
        json.loads(line)
        for line in outcome_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {row["symbol"] for row in outcome_rows} == {"600004", "600002", "600001"}

    reported = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "alpha_v2_validation_report.py"),
            "--epoch-id", env["epoch"].epoch_id,
            "--out", str(env["root"]),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        env=env["env"],
    )
    assert reported.returncode == 0, reported.stderr[-1500:]
    kpi_files = sorted(
        (env["root"] / "validation" / env["epoch"].epoch_id / "reports").glob("*.json")
    )
    assert kpi_files, "KPI 报告未产出"
    kpi = json.loads(kpi_files[-1].read_text(encoding="utf-8"))
    governance = kpi["governance"]
    # 非空转断言：当天确实被捕获（captured_days>=1），且 clean 日 == 0（rehearsal 模式）
    assert int(governance["captured_days"]) >= 1, governance
    assert int(governance["clean_oos_days"]) == 0, governance
    assert kpi["sample_gate_status"]["failure_alert"]["reached"] is False


def test_attack_a_source_label_without_funnel_artifact_fails(rehearsal_env):
    """Attack A：只把 quality_pool_source 写成生产来源、不给真实 funnel 工件。

    在 rehearsal epoch 上以 production_funnel cohort 源运行但漏斗工件不存在——
    必须 exit 10、不落任何影子行（"声明来源"不等于"有证据"）。
    """
    env = rehearsal_env
    result = _capture(env)
    assert result.returncode == 10, (result.returncode, result.stderr[-500:])
    assert "生产漏斗硬门未通过" in result.stderr
    assert read_shadow_rows(env["root"], env["epoch"].epoch_id, SIGNAL_DAY) == []


def test_attack_h_model_artifact_tamper_after_epoch_is_rejected(rehearsal_env):
    """Attack H：epoch 开启后改模型工件——R4.1/工件哈希门必须拒绝后续捕获。

    （epoch 一旦锚定 artifact_hash，改文件就会在 capture 的模型加载门被拒；
    这正是"开了 epoch 再换模型"这条路被关死的证据。）
    """
    env = rehearsal_env
    _emit_funnel(env, ["600004", "600002", "600001"])
    model_dir = Path(env["root"]) / "model" / MODEL_ID
    booster = sorted(model_dir.glob("booster__*.txt"))[0]
    original = booster.read_text(encoding="utf-8")
    booster.write_text(original + "\n# tampered\n", encoding="utf-8")
    try:
        result = _capture(env)
        assert result.returncode != 0, result.stdout[-500:]
        assert "冻结模型校验失败" in result.stderr or "模型工件" in result.stderr
    finally:
        booster.write_text(original, encoding="utf-8")


def test_rehearsal_rejects_alpha_selfmade_cohort_in_production_mode(rehearsal_env):
    """Attack A 预演：production epoch 里写 research_proxy 直接被拒（exit 10）。"""
    env = rehearsal_env
    manifest = write_freeze_manifest(
        env["tmp"] / "prod_root",
        validation_mode="production",
        deterministic_clock=False,
        validation_start_date=date.today().isoformat(),
        model=env["manifest"]["model"],
        feature_columns=FEATURES,
    )
    open_epoch_for_manifest(
        env["tmp"] / "prod_root", manifest, opened_on_date=date.today().isoformat()
    )
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "alpha_v2_shadow_capture.py"),
            "--epoch-id", "alpha_v2_epoch_001",
            "--signal-date", date.today().isoformat(),
            "--market-db", str(env["market_db"]),
            "--out", str(env["tmp"] / "prod_root"),
            "--cohort-source", "research_proxy",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 10, (result.returncode, result.stderr[-500:])
    assert "research_proxy" in result.stderr
