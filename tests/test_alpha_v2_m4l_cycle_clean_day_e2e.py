"""M4-L R1 §10 DH-1..DH-7：真实端到端 clean-day / degraded-day 测试。

用**真实 CLI**（data_health / capture / mature / KPI）+ 合成市场库 + 真实冻结模型
工件跑 ``alpha_v2_shadow_cycle``，验证外部复核最关心的一件事：

```text
production prerequisites ready + S08 七项全齐 + funnel linked + active epoch
    -> shadow snapshot exists
    -> data_health.status == ok
    -> clean_oos_eligible == true
    -> validation KPI: clean_oos_days == 1     (DH-1)
```

反向变异（任一 S08 输入缺失）：deadline 前 → waiting 且**不写任何快照**；
deadline 后 → 落 missing 台账且 ``clean_oos_days == 0``（DH-2/4/5/6）。

时间基准用**真实当天**：capture 的写入窗口是真实墙钟，合成库的最后一个交易日
就是今天（``_trading_days_ending(today)``），因此不需要注入时钟也不产生 backfill。
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
from _alpha_v2_m3_fixtures import open_epoch_for_manifest, write_freeze_manifest

from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
from stock_analyzer.alpha_v2.validation.freeze import (
    freeze_manifest_hash,
    write_validation_freeze,
)
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
    link_funnel_to_report,
    write_source_evidence,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (
    config_hash_of,
    resolve_runtime_code_identity,
)
from stock_analyzer.config import load_config
from stock_analyzer.feature.snapshot import FORMAT_VERSION
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.live_shadow_cycle_service import (
    LiveShadowCycleService,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TODAY = date.today()
FEATURES = ["ret_1d", "ret_5d", "ma5", "volume_ratio_5"]
MODEL_ID = "alpha_v2_shadow_clean_day"


def _trading_days_ending(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


CALENDAR = _trading_days_ending(TODAY, 90)


def _write_market_db(
    path: Path,
    *,
    symbols: list[str],
    last_day: date = TODAY,
    null_close: bool = False,
    omit_board: bool = False,
    price_series_mode: str = "raw",
) -> Path:
    rng = np.random.default_rng(17)
    rows: list[dict[str, object]] = []
    calendar = _trading_days_ending(last_day, 90)
    for index, symbol in enumerate(symbols):
        close = 10.0 + index
        for day in calendar:
            drift = 0.002 * (index - len(symbols) / 2) + rng.normal(0.0, 0.004)
            open_ = close
            close = round(close * (1.0 + drift), 4)
            row = {
                "symbol": symbol,
                "date": day,
                "open": open_,
                "high": round(max(open_, close) * 1.01, 4),
                "low": round(min(open_, close) * 0.99, 4),
                "close": None if null_close else close,
                "volume": float(1_000_000 + index * 10_000),
                "turnover": float((1_000_000 + index * 10_000) * close),
                "float_market_cap": 5e9,
                "is_st": False,
                "is_delisting_risk": False,
                "suspended": False,
                "pre_close": open_,
                "up_limit": round(open_ * 1.1, 4),
                "down_limit": round(open_ * 0.9, 4),
                "price_series_mode": str(price_series_mode),
            }
            if not omit_board:
                row["board"] = "main"
            rows.append(row)
    frame = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.register("bars_frame", frame)
        connection.execute("CREATE OR REPLACE TABLE daily_bars AS SELECT * FROM bars_frame")
    finally:
        connection.close()
    return path


def _train_model(root: Path, *, code_commit: str, model_id: str = MODEL_ID) -> dict[str, object]:
    rng = np.random.default_rng(7)
    total = 240
    days = np.repeat(np.arange(total // 20), 20)[:total]
    frame = pd.DataFrame(
        {
            "decision_date": [f"2026-01-{int(d) + 1:02d}" for d in days],
            "symbol": [f"6001{index % 6:02d}" for index in range(total)],
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
    frame["alpha_target_5d"] = frame.groupby("decision_date")["excess_return_5d"].rank(pct=True)
    frame["is_train"] = False
    frame["is_calibration"] = False
    frame.loc[:179, "is_train"] = True
    frame.loc[180:, "is_calibration"] = True
    model = fit_frozen_model(
        frame=frame,
        model_id=model_id,
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        provenance={
            "window": ["2025-09-01", "2026-03-01"],
            "source": "clean_day_e2e",
            # P0 Final R1：Live 运行期要拿它复核"当天 feature 库口径 == 训练口径"，
            # 因此夹具必须与生产同形地声明 feature 数据身份。
            "feature_data_identity": {
                "role": "feature",
                "db": "clean_day_e2e_feature",
                "price_series_mode": "qfq",
            },
        },
        extra_identity={"code_commit": code_commit},
    )
    persist_frozen_model(model, root)
    return {
        "model_id": model_id,
        "artifact_hash": str(model.manifest.get("artifact_hash", "")),
        "artifact_created_at": "2026-03-01T12:00:00+08:00",
        "artifact_path": str(root / "model" / model_id),
        "status": "frozen",
        "provenance": {
            "window": ["2025-09-01", "2026-03-01"],
            "feature_data_identity": {"role": "feature", "price_series_mode": "qfq"},
        },
        "model_training_code_commit": code_commit,
    }


def _write_feature_snapshot(directory: Path, *, trade_date: date) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "data_snapshot_id": f"snap-{trade_date.isoformat()}",
        "trade_date": trade_date.isoformat(),
        "built_at": datetime.now(UTC).isoformat(),
        "feature_schema_hash": "",  # 空 = 跳过 schema 漂移比较（只关心"当前/对齐/覆盖"）
        "symbol_count": 24,
        "columns": FEATURES,
        "source_signature": "",
        "format_version": FORMAT_VERSION,
        "source_provider": "synthetic",
        "failed_symbols": 0,
        "coverage_ratio": 1.0,
        "max_trade_date": trade_date.isoformat(),
        "scope": "clean_day_e2e",
    }
    path = directory / "current.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class _StubAutomation:
    """readiness 替身。

    ``readiness_path`` 为空 = 既有测试的"恒就绪"（那些用例考察的是 data_health /
    漏斗 / capture，不是 readiness 本身）。给了路径就**走真实 gate**，于是
    "active epoch 必须要求双 delta readiness" 这条判据在这条真实 CLI 链路上也成立，
    不会被替身绕过。
    """

    def __init__(self, *, readiness_path: Path | None = None) -> None:
        self.readiness_path = readiness_path
        self.require_dual_delta_seen: list[bool] = []

    def probe_nightly_readiness(self, *, require_dual_delta: bool = False) -> dict[str, object]:
        self.require_dual_delta_seen.append(bool(require_dual_delta))
        if self.readiness_path is None:
            return {"status": "ready", "allowed": True, "reason": ""}
        from stock_analyzer.ops.nightly_readiness import check_nightly_readiness

        gate = check_nightly_readiness(
            expected_trade_date=TODAY,
            path=self.readiness_path,
            require_dual_delta=require_dual_delta,
        )
        return {
            "status": "ready" if gate.ready else "blocked",
            "allowed": bool(gate.ready),
            "reason": gate.reason,
            "expected_trade_date": gate.expected_trade_date,
            "payload": gate.payload,
        }


@pytest.fixture
def clean_day_env(tmp_path, monkeypatch):
    """一套"生产前置全齐"的合成环境（真实 CLI 全程使用）。"""
    symbols = [f"6001{index:02d}" for index in range(6)]
    # 生产同形：feature 库声明 qfq（特征口径），execution 库声明 raw（成交/label 口径）。
    market_db = _write_market_db(
        tmp_path / "warehouse" / "market.duckdb",
        symbols=symbols,
        price_series_mode="qfq",
    )
    execution_db = _write_market_db(
        tmp_path / "warehouse_raw" / "market_raw.duckdb",
        symbols=symbols,
        price_series_mode="raw",
    )
    features_root = tmp_path / "features_light"
    _write_feature_snapshot(features_root, trade_date=TODAY)

    # 子进程（真实 CLI）读的是 config/default.yaml + SA__ 环境覆盖，所以
    # 所有测试侧改动都必须走环境变量，parent 侧配置再镜像一份。
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    monkeypatch.setenv("SA__WEEK5__FEATURE_SNAPSHOT_ROOT", str(features_root))
    monkeypatch.setenv("SA__ALPHA_V2__EXECUTION_MARKET_DB", str(execution_db))
    funnel_root = tmp_path / "runtime" / "production_funnel"
    monkeypatch.setenv("SA__ALPHA_V2__PRODUCTION_FUNNEL_ROOT", str(funnel_root))

    runtime_identity = resolve_runtime_code_identity(REPO_ROOT, validation_mode="rehearsal")
    assert runtime_identity.code_commit
    config = load_config(REPO_ROOT / "config" / "default.yaml")

    root = tmp_path / "alpha_v2"
    model_block = _train_model(root, code_commit=runtime_identity.code_commit)
    manifest = write_freeze_manifest(
        root,
        validation_mode="test",
        deterministic_clock=True,
        validation_start_date=TODAY.isoformat(),
        model=model_block,
        feature_columns=FEATURES,
        execution_price_mode="raw",
        code_commit=runtime_identity.code_commit,
        config_hash=config_hash_of(config),
    )
    manifest["require_production_funnel"] = True
    manifest["freeze_manifest_hash"] = freeze_manifest_hash(manifest)
    write_validation_freeze(manifest, root=root)
    epoch = open_epoch_for_manifest(root, manifest, opened_on_date=TODAY.isoformat())

    # 生产漏斗工件 + 已链接的正式晚报（cyc 前置要求 report_id 非空）
    report_dir = tmp_path / "nightly_reports" / TODAY.isoformat()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / "nr-formal-01.json"
    report_file.write_text(
        json.dumps(
            {
                "report_id": "nr-formal-01",
                "report_kind": "formal",
                "trade_date": TODAY.isoformat(),
                "scan_status": "completed",
            }
        ),
        encoding="utf-8",
    )
    evidence = build_source_evidence(
        source_report={
            "funnel": {
                "policy": "snapshot_funnel",
                "deep_stage_ran": True,
                "selection_contract": {"selection_contract_id": "night_alpha_v2_v1"},
            },
            "prefilter": {
                "universe_quality_selection": {
                    "selector_mode": "quality",
                    "selected": [{"symbol": s, "score": 1.0} for s in symbols],
                },
                "shortlisted": [{"symbol": s, "baseline_score": 1.0} for s in symbols],
                "deep_stage": {"selected": [{"symbol": s, "funnel_score": 1.0} for s in symbols]},
                "pinned_symbols": [],
            },
        },
        trade_date=TODAY.isoformat(),
        trace_id="week5-night-scan-e2e",
        created_at=f"{TODAY.isoformat()}T22:00:00+08:00",
    )
    evidence_path = write_source_evidence(funnel_root=funnel_root, payload=evidence)
    payload = extract_funnel_from_source_evidence(
        evidence,
        source_artifact_path=str(evidence_path),
        source_artifact_sha256=file_sha256(evidence_path),
    )
    payload["signal_date"] = TODAY.isoformat()
    payload["trade_date"] = TODAY.isoformat()
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    emit_funnel_snapshot(funnel_root=funnel_root, payload=payload)
    link_funnel_to_report(
        funnel_root=funnel_root,
        trade_date=TODAY.isoformat(),
        report_id="nr-formal-01",
        report_path=report_file,
    )

    service = StockAnalyzerService.__new__(StockAnalyzerService)
    service._config = config
    config.alpha_v2.enabled = True
    config.alpha_v2.shadow_only = True
    config.alpha_v2.artifact_root = str(root)
    config.alpha_v2.production_funnel_root = str(funnel_root)
    config.week5.feature_snapshot_root = str(features_root)
    config.market_warehouse.db_path = str(market_db)
    config.alpha_v2.execution_market_db = str(execution_db)
    audits: list[dict[str, object]] = []
    service._record_audit_event = lambda **kwargs: audits.append(kwargs)
    service._job_now = lambda: datetime.combine(TODAY, datetime.min.time()).replace(
        hour=22, minute=40
    )
    service._week5_automation_service = _StubAutomation()
    cycle = LiveShadowCycleService(service)
    service._live_shadow_cycle = cycle
    return {
        "tmp": tmp_path,
        "root": root,
        "epoch": epoch,
        "service": service,
        "cycle": cycle,
        "audits": audits,
        "market_db": market_db,
        "execution_market_db": execution_db,
        "features_root": features_root,
        "funnel_root": funnel_root,
        "symbols": symbols,
    }


def _kpi_governance(root: Path, epoch_id: str) -> dict[str, object]:
    reports = sorted((root / "validation" / epoch_id / "reports").glob("*.json"))
    assert reports, "KPI 报告未产出"
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    return dict(payload["governance"])


def test_dh1_healthy_day_yields_one_clean_oos_day(clean_day_env):
    env = clean_day_env
    result = env["cycle"].run_daily_cycle()
    assert result["_scheduler_detail"] == "alpha_v2_cycle_completed", result
    epoch_dir = env["root"] / "validation" / env["epoch"].epoch_id
    shadow_files = list(epoch_dir.rglob("shadow_*.jsonl"))
    assert shadow_files, "影子快照未落盘"
    rows = [
        json.loads(line)
        for line in shadow_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {row["clean_oos_eligible"] for row in rows} == {True}
    assert {row["quality_pool_source"] for row in rows} == {"production_selection_engine"}
    assert {row["symbol"] for row in rows} == set(env["symbols"])
    health = json.loads((env["root"] / "runtime" / "data_health.json").read_text(encoding="utf-8"))
    assert health["status"] == "healthy", health
    assert health["as_of"] == TODAY.isoformat()
    governance = _kpi_governance(env["root"], env["epoch"].epoch_id)
    assert governance["clean_oos_days"] == 1, governance
    assert governance["captured_days"] == 1


def test_dh7_degraded_first_then_healthy_recovers(clean_day_env):
    """先缺 feature snapshot（degraded）→ 不 capture；补齐后同一晚 → clean +1。"""
    env = clean_day_env
    manifest_path = env["features_root"] / "current.json"
    backup = manifest_path.read_text(encoding="utf-8")
    manifest_path.unlink()
    first = env["cycle"].run_daily_cycle()
    assert first["_scheduler_detail"].startswith("alpha_v2_waiting:data_health_not_healthy")
    assert list(env["root"].rglob("shadow_*.jsonl")) == []
    manifest_path.write_text(backup, encoding="utf-8")
    second = env["cycle"].run_daily_cycle()
    assert second["_scheduler_detail"] == "alpha_v2_cycle_completed", second
    governance = _kpi_governance(env["root"], env["epoch"].epoch_id)
    assert governance["clean_oos_days"] == 1, governance


@pytest.mark.parametrize(
    ("variant", "sabotage", "expected_check", "expected_bucket"),
    [
        ("dh2_universe_coverage", "truncate_market_db", "expected_active_coverage", "broken"),
        (
            "dh4_feature_snapshot",
            "remove_feature_manifest",
            "feature_snapshot_coverage",
            "degraded",
        ),
        ("dh5_model_identity", "remove_model_artifact", "model_identity_health", "broken"),
        ("dh6_breadth", "null_close_market_db", "breadth_artifact", "degraded"),
    ],
)
def test_degraded_variants_do_not_capture(
    clean_day_env, variant, sabotage, expected_check, expected_bucket
):
    """DH-2/4/5/6：任一 S08 输入缺失 → waiting 且不写任何快照（并证明是哪一项降级）。"""
    env = clean_day_env
    if sabotage == "truncate_market_db":
        _write_market_db(
            env["market_db"],
            symbols=env["symbols"],
            last_day=TODAY - timedelta(days=1),
            price_series_mode="qfq",
        )
    elif sabotage == "remove_feature_manifest":
        (env["features_root"] / "current.json").unlink()
    elif sabotage == "remove_model_artifact":
        shutil.rmtree(env["root"] / "model" / MODEL_ID)
    elif sabotage == "null_close_market_db":
        _write_market_db(
            env["market_db"], symbols=env["symbols"], null_close=True, price_series_mode="qfq"
        )
    else:  # pragma: no cover - 参数表写错才会到这里
        raise AssertionError(variant)
    result = env["cycle"].run_daily_cycle()
    assert result["_scheduler_detail"].startswith("alpha_v2_waiting:"), result
    assert result["_scheduler_detail"].endswith("data_health_not_healthy:data_health_not_available")
    assert "steps" not in result  # 等待态：连 capture/mature/report 步骤都没开始
    assert list(env["root"].rglob("shadow_*.jsonl")) == []
    health = json.loads((env["root"] / "runtime" / "data_health.json").read_text(encoding="utf-8"))
    assert health["status"] != "healthy", health
    assert expected_check in health[f"{expected_bucket}_checks"], health


def test_dh2_at_deadline_records_missing_and_zero_clean_days(clean_day_env):
    """deadline 仍 degraded：落 missing 台账 + 不产生任何 clean 日 + 仍推进历史尾部。"""
    env = clean_day_env
    _write_market_db(
        env["market_db"],
        symbols=env["symbols"],
        last_day=TODAY - timedelta(days=1),
        price_series_mode="qfq",
    )
    env["service"]._job_now = lambda: datetime.combine(TODAY, datetime.min.time()).replace(
        hour=23, minute=56
    )
    result = env["cycle"].run_daily_cycle()
    assert result["missing_recorded"] is True
    assert [item["step"] for item in result["history_tail"]] == ["mature", "report"]
    from stock_analyzer.alpha_v2.validation.shadow_capture import list_missing_days

    missing = list_missing_days(env["root"], env["epoch"].epoch_id)
    assert [item["signal_date"] for item in missing] == [TODAY.isoformat()]
    governance = _kpi_governance(env["root"], env["epoch"].epoch_id)
    assert governance["clean_oos_days"] == 0, governance


def test_dh3_board_coverage_degradation_is_detected(tmp_path, monkeypatch):
    """DH-3：板块级覆盖塌陷（且整体覆盖仍达标）必须被判出来（单元级，隔离板块项）。"""
    from stock_analyzer.alpha_v2.validation.live_data_health_inputs import (
        derive_universe_facts,
    )
    from stock_analyzer.ops.data_health import evaluate_data_health

    # 21 只主板 + 2 只创业板；把其中 1 只创业板当天的 bar 拿掉：
    #   整体覆盖 = 21/22 = 0.9545（≥0.95 达标），创业板覆盖 = 1/2 = 0.5（塌陷）。
    symbols = [f"6002{index:02d}" for index in range(21)] + ["300001", "300002"]
    market_db = _write_market_db(tmp_path / "market.duckdb", symbols=symbols)
    connection = duckdb.connect(str(market_db))
    try:
        connection.execute(
            "DELETE FROM daily_bars WHERE symbol = '300001' AND date = CAST(? AS DATE)",
            [TODAY.isoformat()],
        )
        connection.execute(
            "UPDATE daily_bars SET board = '创业板' WHERE symbol IN ('300001','300002')"
        )
    finally:
        connection.close()
    snapshot, valid, board_coverage, _ = derive_universe_facts(
        market_db=market_db, as_of=TODAY, min_history_days=60
    )
    assert snapshot is not None and valid is not None and board_coverage is not None
    worst = min(board_coverage.values())
    assert worst < 0.95, board_coverage
    assert valid / int(snapshot["expected_active_count"]) >= 0.95
    report = evaluate_data_health(
        as_of=TODAY,
        latest_trade_date=TODAY,
        universe_snapshot=snapshot,
        valid_symbol_count=valid,
        board_coverage=board_coverage,
        feature_snapshot={"current": True, "coverage_ratio": 1.0},
        model_identity={"identity_verified": True, "status": "verified"},
        price_contract={"execution_uncertain": False, "execution_price_mode": "raw"},
        breadth_artifact_present=True,
    )
    assert report.status != "healthy"
    assert "board_coverage" in report.degraded_checks


# ---------------------------------------------------------------------------
# LIVE-F5 / LIVE-F6：feature 口径漂移进入**捕获前 prerequisite**（P0 Final R1 / BLOCKER 2）
# ---------------------------------------------------------------------------


def test_live_f5_feature_mode_mismatch_waits_then_recovers_same_night(clean_day_env):
    """LIVE-F5：当晚 feature 库口径漂移 → waiting 且不 capture；修好后同晚即可 capture。

    这是本轮最重要的 scheduler regression：旧实现在 capture 里才抛错，调度器只会一路
    ``alpha_v2_step_failed:capture``，到 23:55 也不会走 missing 台账。
    """
    env = clean_day_env
    # 漂移：feature 库被换成 raw（模型冻结的是 qfq）
    _write_market_db(
        env["market_db"],
        symbols=env["symbols"],
        price_series_mode="raw",
    )
    first = env["cycle"].run_daily_cycle()
    assert first["_scheduler_detail"] == "alpha_v2_waiting:feature_price_mode_mismatch", first
    assert first["_scheduler_success"] is True
    assert "steps" not in first  # 等待态：capture/mature/report 一步都没开始
    assert list(env["root"].rglob("shadow_*.jsonl")) == []
    evidence = first["feature_price_series"]
    assert evidence["expected_mode"] == "qfq"
    assert evidence["observed_mode"] == "raw"
    assert evidence["contract_ok"] is False
    assert evidence["enforced"] is True

    # 同一晚修回 qfq → 下一次槽位直接捕获 + clean 日成立
    _write_market_db(
        env["market_db"],
        symbols=env["symbols"],
        price_series_mode="qfq",
    )
    second = env["cycle"].run_daily_cycle()
    assert second["_scheduler_detail"] == "alpha_v2_cycle_completed", second
    assert list(env["root"].rglob("shadow_*.jsonl"))
    governance = _kpi_governance(env["root"], env["epoch"].epoch_id)
    assert governance["clean_oos_days"] == 1, governance


def test_live_f6_feature_mode_mismatch_until_deadline_records_missing(clean_day_env):
    """LIVE-F6：口径一直漂到 deadline → 记 missing 台账、不产生 capture。"""
    from stock_analyzer.alpha_v2.validation.shadow_capture import list_missing_days

    env = clean_day_env
    _write_market_db(
        env["market_db"],
        symbols=env["symbols"],
        price_series_mode="raw",
    )
    env["service"]._job_now = lambda: datetime.combine(TODAY, datetime.min.time()).replace(
        hour=23, minute=56
    )
    result = env["cycle"].run_daily_cycle()
    assert result["missing_recorded"] is True, result
    assert result["_scheduler_detail"].startswith(
        "alpha_v2_blocked_recorded_missing:feature_price_mode_mismatch"
    ), result
    assert list(env["root"].rglob("shadow_*.jsonl")) == []
    missing = list_missing_days(env["root"], env["epoch"].epoch_id)
    assert [item["signal_date"] for item in missing] == [TODAY.isoformat()]
    assert "feature_price_mode_mismatch" in str(missing[0]["reason"])
    governance = _kpi_governance(env["root"], env["epoch"].epoch_id)
    assert governance["clean_oos_days"] == 0, governance


def _dual_readiness_payload(*, schema_version: int) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "target_trade_date": TODAY.isoformat(),
        "daily": {"ok": True},
        "index": {"ok": True},
        "delta": {"ok": True},
    }
    if schema_version >= 3:
        payload["execution_delta"] = {"ok": True, "role": "execution"}
        payload["symbol_membership"] = {"membership_locked": True}
        payload["raw_delta_baseline"] = {"ok": True}
        # v3 起必须自带 QFQ 对账结论；缺这块等于"没证明过两份库逐键一致"，
        # active epoch 不放行（见 tests/test_nightly_readiness_qfq_parity.py::qfq5）。
        payload["qfq_parity"] = {"evaluated": True, "ok": True, "derivation_gap_count": 0}
    return payload


def test_alpha_rdy2_v2_then_v3_same_night_recovers_to_clean_day(clean_day_env):
    """ALPHA-RDY-2：同一晚 v2 → waiting；unified updater 补出 v3 → capture，clean +1。

    这条用例的真实价值在"v2 那一半"：如果没有严格档，第一次调用就会直接 capture 并
    记一个 clean day——而那天没有任何执行侧（raw）证据。等到 23:55 也一样记不到
    missing，因为根本没人拦它。
    """
    env = clean_day_env
    readiness_path = env["tmp"] / "runtime" / "nightly_data_ready.json"
    readiness_path.parent.mkdir(parents=True, exist_ok=True)
    automation = _StubAutomation(readiness_path=readiness_path)
    env["service"]._week5_automation_service = automation

    readiness_path.write_text(
        json.dumps(_dual_readiness_payload(schema_version=2)), encoding="utf-8"
    )
    first = env["cycle"].run_daily_cycle()
    assert first["_scheduler_detail"] == ("alpha_v2_waiting:nightly_dual_delta_not_ready"), first
    assert automation.require_dual_delta_seen == [True]
    assert list(env["root"].rglob("shadow_*.jsonl")) == []

    # 同一晚稍后：unified updater 写出 v3（双 delta 就绪）。
    readiness_path.write_text(
        json.dumps(_dual_readiness_payload(schema_version=3)), encoding="utf-8"
    )
    second = env["cycle"].run_daily_cycle()

    assert second["_scheduler_detail"] == "alpha_v2_cycle_completed", second
    epoch_dir = env["root"] / "validation" / env["epoch"].epoch_id
    shadow_files = list(epoch_dir.rglob("shadow_*.jsonl"))
    assert shadow_files, "影子快照未落盘"
    rows = [
        json.loads(line)
        for line in shadow_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {row["clean_oos_eligible"] for row in rows} == {True}
    governance = _kpi_governance(env["root"], env["epoch"].epoch_id)
    assert governance["clean_oos_days"] == 1, governance
